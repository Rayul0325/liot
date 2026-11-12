"""U6: 상황 행이 커밋된 뒤 프로세스가 죽어도 열린 상황이 침묵하거나 사다리 없이 남지 않는다.

C4.9(c) 문장 단위 kill 스윕이 잡은 게이트 1 틈 두 개(가스 한 건 = DB 문장 24개):
  창 1  상황 행 커밋(문장 4) 뒤·첫 송신(문장 11 뒤) 전에 죽으면 상황은 열려 있는데 알림도 사다리도
        없다. 이어지는 이상 신호는 '열린 상황 이어감'으로 삼켜져 센서가 정상이 될 때까지 조용하다.
        (dispatch 가 제안을 커밋하는 문장 7 전 = 제안 없음, 그 뒤 = 제안만 있음.)
  창 2  첫 알림은 나갔는데 사다리 행(문장 23) 전에 죽으면 재알림·보호자 전달·119 안내가 사라진다.

고친 규칙: start() 가 사다리 없는 열린 상황을 복구한다 — 알림 기록이 없으면 첫 알림을 보내고(제안이
있으면 재송신, 없으면 상황 행에서 다시 만든다), 어느 쪽이든 사다리를 세운다. 알림이 이미 나갔으면
다시 보내지 않는다. 사용자가 정한 침묵(헌장 OFF·음소거)은 그대로다.

죽음은 결정적으로 흉내 낸다: 파일 DB 위에서 정해진 SQL(또는 송신)에서 DB 연결이 죽는 프록시를 쓴다.
죽은 뒤에는 어떤 DB 문장도 송신도 되지 않는다(BaseException — 서비스의 닫기 가드도 쓰지 못한다, kill -9).
그 파일 DB 를 새 LiotApp+LiotService 로 열어 start() 한다. 시각은 고정 Clock, 잠자기 없음.
"""

from __future__ import annotations

import asyncio
import re
from pathlib import Path
from typing import Any

import pytest

from liot.core.clock import Clock
from liot.core.severity import SeverityLevel
from liot.db.database import init_db
from liot.layers.l4_execution.ntfy_transport import FakeNtfyTransport, NtfyError
from liot.service import LiotService
from tests.test_escalation_ladder import (
    DOOR,
    GAS,
    HOME,
    T0,
    TOPIC,
    _advance,
    _at,
    _forwards,
    _guardian,
    _make,
    _reading,
    _to,
)


class _Killed(BaseException):
    """프로세스가 여기서 죽었다(kill -9). BaseException: 서비스의 정리 코드가 잡지 못한다."""


class _KillableDB:
    """aiosqlite 연결 프록시. 정해진 SQL 이 실행되기 직전에 죽고, 죽은 뒤엔 모든 문장이 죽는다.

    after 를 주면 그 SQL 이 한 번 지나간 뒤부터 kill_on 을 센다.
    """

    def __init__(self, db: Any, kill_on: str | None, after: str | None = None) -> None:
        self._db = db
        self.dead = False
        self._kill_on = re.compile(kill_on) if kill_on else None
        self._after = re.compile(after) if after else None
        self._armed = after is None

    def _tick(self, sql: Any = "") -> None:
        if self.dead:
            raise _Killed
        text = " ".join(str(sql).split())
        if not self._armed and self._after is not None and self._after.search(text):
            self._armed = True
            return
        if self._armed and self._kill_on is not None and self._kill_on.search(text):
            self.dead = True
            raise _Killed

    def execute(self, sql: Any, *a: Any, **k: Any) -> Any:
        self._tick(sql)
        return self._db.execute(sql, *a, **k)

    def executemany(self, sql: Any, *a: Any, **k: Any) -> Any:
        self._tick(sql)
        return self._db.executemany(sql, *a, **k)

    async def commit(self) -> None:
        self._tick("COMMIT")
        await self._db.commit()

    def __getattr__(self, name: str) -> Any:
        return getattr(self._db, name)


class _SendKillsTransport(FakeNtfyTransport):
    """거주자 토픽 송신 순간 프로세스가 죽는다(제안은 커밋됐고 알림은 안 나간 자리)."""

    def __init__(self, db: _KillableDB) -> None:
        super().__init__()
        self._kdb = db

    def send(self, topic: str, sanitized: dict[str, Any]) -> None:
        if self._kdb.dead:
            raise _Killed
        if topic == TOPIC:
            self._kdb.dead = True
            raise _Killed
        super().send(topic, sanitized)


async def _kill_during(
    path: str,
    clock: Clock,
    entity: str,
    *,
    kill_on: str | None = None,
    after: str | None = None,
    kill_on_send: bool = False,
    prefs: dict[str, Any] | None = None,
    guardian: bool = False,
) -> dict[str, Any]:
    """파일 DB 에서 이상 신호 하나를 처리하다 죽는다. 죽은 프로세스가 남긴 것을 돌려준다."""
    raw = await init_db(path)
    kdb = _KillableDB(raw, kill_on, after)
    transport = _SendKillsTransport(kdb) if kill_on_send else FakeNtfyTransport()
    svc, _, _, _ = await _make(db=kdb, transport=transport, clock=clock)
    gtopic = None
    if guardian:
        _, _, gtopic = await _guardian(svc)
    for key, value in (prefs or {}).items():
        await svc.prefs.set(key, value)
    with pytest.raises(_Killed):
        await svc.handle_reading(_reading(entity, "on"))
    assert kdb.dead
    sent_before_death = list(transport.sent)
    # 프로세스가 사라진다: 커밋 안 된 일은 연결을 닫으며 되감긴다.
    for task in list(svc._phase2_tasks):
        task.cancel()
    await raw.close()
    return {"sent": sent_before_death, "guardian_topic": gtopic}


async def _restart(path: str, clock: Clock, *, transport: Any = None, start: bool = True):
    """같은 파일 DB 를 새 LiotApp+LiotService 로 연다(프로세스 재시작)."""
    db = await init_db(path)
    svc, _, transport, _ = await _make(db=db, transport=transport, clock=clock)
    if start:
        await svc.start()
    return svc, db, transport


async def _rows(db: Any, sql: str, params: tuple[Any, ...] = ()) -> list[tuple[Any, ...]]:
    cur = await db.execute(sql, params)
    try:
        return [tuple(r) for r in await cur.fetchall()]
    finally:
        await cur.close()


async def _only_open_situation(svc: LiotService) -> Any:
    sits = await svc.safety.list_open_situations(HOME)
    assert len(sits) == 1
    return sits[0]


# 죽는 자리: 'dispatch 가 제안을 커밋하기 전' = 제안 없음, '커밋한 뒤·송신 전' = 제안만 있음,
# '송신·알림 기록 뒤·사다리 행 생성 전' = 창 2.
NO_PROPOSAL: dict[str, Any] = {"kill_on": r"INSERT INTO proposals"}
PROPOSAL_ONLY: dict[str, Any] = {"kill_on_send": True}
NOTIFIED_NO_LADDER: dict[str, Any] = {"kill_on": r"INSERT INTO escalation_ladders"}


# ==========================================================================
# 죽은 직후의 DB 상태가 정말 우리가 말한 모양인지(시험이 빈 껍데기가 아니게)
# ==========================================================================
@pytest.mark.parametrize(
    ("kill", "proposals", "notifications", "ladders"),
    [(NO_PROPOSAL, 0, 0, 0), (PROPOSAL_ONLY, 1, 0, 0), (NOTIFIED_NO_LADDER, 1, 1, 0)],
)
async def test_the_simulated_kill_leaves_the_state_it_claims(
    tmp_path: Path, kill: dict[str, Any], proposals: int, notifications: int, ladders: int
) -> None:
    path = str(tmp_path / "liot.db")
    clock = Clock(fixed=T0)
    await _kill_during(path, clock, GAS, **kill)
    db = await init_db(path)
    try:
        assert len(await _rows(db, "SELECT 1 FROM situations WHERE closed_at IS NULL")) == 1
        assert len(await _rows(db, "SELECT 1 FROM proposals")) == proposals
        assert len(await _rows(db, "SELECT 1 FROM notifications")) == notifications
        assert len(await _rows(db, "SELECT 1 FROM escalation_ladders")) == ladders
    finally:
        await db.close()


# ==========================================================================
# 창 1(a): 제안은 저장됐는데 알림이 안 나갔다 → 시작하자마자 첫 알림 + 사다리
# ==========================================================================
async def test_staged_proposal_without_notification_is_sent_on_start_and_ladder_armed(
    tmp_path: Path,
) -> None:
    path = str(tmp_path / "liot.db")
    clock = Clock(fixed=T0)
    died = await _kill_during(path, clock, GAS, **PROPOSAL_ONLY)
    assert died["sent"] == []  # 죽기 전에 아무것도 나가지 않았다
    svc, db, transport = await _restart(path, clock)
    try:
        sit = await _only_open_situation(svc)
        sent = _to(transport, TOPIC)
        assert len(sent) == 1  # 시작 즉시 첫 알림
        assert sent[0]["severity_label"] == SeverityLevel.CRITICAL.label_ko
        ladder = await svc.safety.get_ladder(sit.situation_id)
        assert ladder is not None and ladder.next_fire_at == _at(60)  # 열린 시각 기준 첫 재알림
        # 같은 제안이다(새 제안을 만들지 않았다) + 알림이 기록됐다.
        assert len(await _rows(db, "SELECT 1 FROM proposals")) == 1
        assert len(await _rows(db, "SELECT 1 FROM notifications")) == 1
    finally:
        await db.close()


async def test_resend_failure_on_start_is_covered_by_the_30s_ladder_retry(tmp_path: Path) -> None:
    path = str(tmp_path / "liot.db")
    clock = Clock(fixed=T0)
    await _kill_during(path, clock, GAS, **PROPOSAL_ONLY)

    class _FailOnce(FakeNtfyTransport):
        failed = 0

        def send(self, topic: str, sanitized: dict[str, Any]) -> None:
            if topic == TOPIC and self.failed < 1:
                self.failed += 1
                raise NtfyError("forced")
            super().send(topic, sanitized)

    svc, db, transport = await _restart(path, clock, transport=_FailOnce())
    try:
        sit = await _only_open_situation(svc)
        assert _to(transport, TOPIC) == []
        ladder = await svc.safety.get_ladder(sit.situation_id)
        assert ladder is not None and ladder.next_fire_at == _at(30)
        await _advance(svc, clock, 30)
        assert len(_to(transport, TOPIC)) == 1
    finally:
        await db.close()


# ==========================================================================
# 창 1(b): 제안도 없다(저장 전에 죽음) → 상황 행에서 첫 알림을 다시 만들어 보낸다
# ==========================================================================
async def test_situation_without_proposal_is_rebuilt_and_alerted_on_start(
    tmp_path: Path,
) -> None:
    path = str(tmp_path / "liot.db")
    clock = Clock(fixed=T0)
    await _kill_during(path, clock, GAS, **NO_PROPOSAL)
    svc, db, transport = await _restart(path, clock)
    try:
        sit = await _only_open_situation(svc)  # 닫히지 않았다 — 같은 상황이다
        sent = _to(transport, TOPIC)
        assert len(sent) == 1
        assert sent[0]["severity_label"] == SeverityLevel.CRITICAL.label_ko
        rows = await _rows(db, "SELECT situation_id FROM proposals")
        assert rows == [(sit.situation_id,)]
        ladder = await svc.safety.get_ladder(sit.situation_id)
        assert ladder is not None and ladder.next_fire_at == _at(60)
        # 판단 행도 남는다(감사 기록) — 합성 읽기라 결정 모델은 돌리지 않는다.
        assert len(await _rows(db, "SELECT 1 FROM judgments")) == 1
        assert svc._phase2_tasks == set()
    finally:
        await db.close()


async def test_rebuilt_warning_alert_keeps_the_warning_floor_and_ladder_timing(
    tmp_path: Path,
) -> None:
    path = str(tmp_path / "liot.db")
    clock = Clock(fixed=T0)
    await _kill_during(path, clock, DOOR, **NO_PROPOSAL)
    svc, db, transport = await _restart(path, clock)
    try:
        sit = await _only_open_situation(svc)
        sent = _to(transport, TOPIC)
        assert len(sent) == 1
        assert sent[0]["severity_label"] == SeverityLevel.WARNING.label_ko
        ladder = await svc.safety.get_ladder(sit.situation_id)
        assert ladder is not None and ladder.next_fire_at == _at(600)
    finally:
        await db.close()


async def test_rebuilt_alert_never_lowers_a_persisted_higher_floor(tmp_path: Path) -> None:
    # 취약 거주자 올림으로 열린 문 상황(저장 하한 CRITICAL)이 거주자 특성 삭제 뒤 다시 만들어져도
    # 하한은 내려가지 않는다.
    path = str(tmp_path / "liot.db")
    clock = Clock(fixed=T0)
    raw = await init_db(path)
    kdb = _KillableDB(raw, NO_PROPOSAL["kill_on"])
    svc, _, _, _ = await _make(db=kdb, clock=clock)
    rid = await svc.safety.add_resident(HOME, "엄마")
    await svc.safety.set_trait(rid, "age_75_plus", True, "resident")
    with pytest.raises(_Killed):
        await svc.handle_reading(_reading(DOOR, "on"))
    await raw.close()

    svc2, db, transport = await _restart(path, clock, start=False)
    try:
        sit = await _only_open_situation(svc2)
        assert sit.severity_floor == SeverityLevel.CRITICAL
        assert await svc2.safety.delete_resident(rid)  # 특성이 사라졌다 → 표 값은 WARNING
        await svc2.start()
        sent = _to(transport, TOPIC)
        assert len(sent) == 1 and sent[0]["severity_label"] == SeverityLevel.CRITICAL.label_ko
        sit2 = await _only_open_situation(svc2)
        assert sit2.severity_floor == SeverityLevel.CRITICAL
    finally:
        await db.close()


# ==========================================================================
# 창 2: 알림은 나갔는데 사다리가 없다 → 다시 보내지 않고 사다리만 세운다(열린 시각 기준)
# ==========================================================================
async def test_notified_situation_without_ladder_gets_its_ladder_and_no_second_push(
    tmp_path: Path,
) -> None:
    path = str(tmp_path / "liot.db")
    clock = Clock(fixed=T0)
    died = await _kill_during(path, clock, GAS, guardian=True, **NOTIFIED_NO_LADDER)
    gtopic = died["guardian_topic"]
    svc, db, transport = await _restart(path, clock)
    try:
        sit = await _only_open_situation(svc)
        assert _to(transport, TOPIC) == []  # 이미 나간 첫 알림을 다시 보내지 않는다
        ladder = await svc.safety.get_ladder(sit.situation_id)
        assert ladder is not None and ladder.next_fire_at == _at(60)
        # 이후 사다리는 명세 그대로: +60 재알림, +90 보호자 전달, +90+300 119 안내.
        await _advance(svc, clock, 60)
        assert len(_to(transport, TOPIC)) == 1
        await _advance(svc, clock, 90)
        assert [kind for _, kind in await _forwards(svc)] == ["forward"]
        assert len(_to(transport, gtopic)) == 1
        await _advance(svc, clock, 390)
        assert [kind for _, kind in await _forwards(svc)] == ["forward", "guidance_119"]
    finally:
        await db.close()


async def test_warning_notified_without_ladder_gets_a_ladder_too(tmp_path: Path) -> None:
    path = str(tmp_path / "liot.db")
    clock = Clock(fixed=T0)
    await _kill_during(path, clock, DOOR, **NOTIFIED_NO_LADDER)
    svc, db, transport = await _restart(path, clock)
    try:
        sit = await _only_open_situation(svc)
        assert _to(transport, TOPIC) == []
        ladder = await svc.safety.get_ladder(sit.situation_id)
        assert ladder is not None and ladder.next_fire_at == _at(600)
    finally:
        await db.close()


async def test_late_restart_keeps_the_timing_anchored_to_the_open_time(tmp_path: Path) -> None:
    # 죽은 지 한참 뒤(+200초)에 시작해도 90초 보호자 전달은 열린 시각 기준으로 이미 밀려 있다.
    path = str(tmp_path / "liot.db")
    clock = Clock(fixed=T0)
    died = await _kill_during(path, clock, GAS, guardian=True, **NOTIFIED_NO_LADDER)
    clock.set_fixed(_at(200))
    svc, db, transport = await _restart(path, clock)
    try:
        sit = await _only_open_situation(svc)
        ladder = await svc.safety.get_ladder(sit.situation_id)
        assert ladder is not None and ladder.next_fire_at is not None
        assert ladder.next_fire_at <= _at(200)  # 지금 바로 발화 대상(처음부터 다시 세지 않는다)
        await svc.ladder_tick()
        assert [kind for _, kind in await _forwards(svc)] == ["forward"]
        assert len(_to(transport, died["guardian_topic"])) == 1
    finally:
        await db.close()


# ==========================================================================
# 사용자가 정한 침묵은 그대로: 보내지 않는다(복구가 사다리는 세워도 알림은 없다)
# ==========================================================================
@pytest.mark.parametrize("kill", [NO_PROPOSAL, {**PROPOSAL_ONLY}], ids=["no_proposal", "proposal_only"])
async def test_charter_off_stays_silent_after_recovery(tmp_path: Path, kill: dict[str, Any]) -> None:
    path = str(tmp_path / "liot.db")
    clock = Clock(fixed=T0)
    if "kill_on_send" in kill:
        # 헌장 OFF 면 송신까지 가지 않는다 → 제안 저장 직후(첫 선호 조회)에서 죽인다.
        kill = {"kill_on": r"SELECT value FROM preferences", "after": r"INSERT INTO proposals"}
    await _kill_during(path, clock, GAS, prefs={"charter_enabled": False}, **kill)
    svc, db, transport = await _restart(path, clock)
    try:
        sit = await _only_open_situation(svc)
        assert transport.sent == []
        assert len(await _rows(db, "SELECT 1 FROM notifications")) == 0
        assert await svc.safety.get_ladder(sit.situation_id) is not None
        # 반복되는 이상 신호도 알림을 만들지 않는다.
        await svc.handle_reading(_reading(GAS, "on"))
        assert transport.sent == []
    finally:
        await db.close()


async def test_muted_warning_stays_silent_after_recovery(tmp_path: Path) -> None:
    path = str(tmp_path / "liot.db")
    clock = Clock(fixed=T0)
    await _kill_during(
        path, clock, DOOR, prefs={"muted_entities": [DOOR]},
        kill_on=r"SELECT value FROM preferences", after=r"INSERT INTO proposals",
    )
    svc, db, transport = await _restart(path, clock)
    try:
        sit = await _only_open_situation(svc)
        assert transport.sent == []
        assert len(await _rows(db, "SELECT 1 FROM notifications")) == 0
        # 알림은 없어도 사다리는 선다(복구가 일했다는 증거 + 재알림 게이트는 음소거를 그대로 본다).
        assert await svc.safety.get_ladder(sit.situation_id) is not None
    finally:
        await db.close()


# ==========================================================================
# 이상 신호 삼킴: 사다리도 알림도 없는 열린 상황에 이상 신호가 이어지면 그 자리에서 복구
# ==========================================================================
async def test_repeated_abnormal_reading_is_not_swallowed_by_an_unnotified_open_situation(
    tmp_path: Path,
) -> None:
    # 시작 시 복구를 거치지 않은 프로세스(복구가 실패했거나 건너뛴 경우)를 흉내 낸다.
    path = str(tmp_path / "liot.db")
    clock = Clock(fixed=T0)
    await _kill_during(path, clock, GAS, **NO_PROPOSAL)
    svc, db, transport = await _restart(path, clock, start=False)
    svc._started = True
    try:
        res = await svc.handle_reading(_reading(GAS, "on"))
        await svc.drain_phase2()
        assert res is None  # 새 상황이 아니다(반환은 여는 쪽 몫)
        assert len(_to(transport, TOPIC)) == 1  # 그래도 삼키지 않고 알린다
        sit = await _only_open_situation(svc)
        assert await svc.safety.get_ladder(sit.situation_id) is not None
        assert len(await _rows(db, "SELECT 1 FROM judgments")) >= 1
        # 한 번 알리고 사다리가 서면 이어지는 신호는 다시 알리지 않는다(재알림은 사다리 몫).
        await svc.handle_reading(_reading(GAS, "on"))
        await svc.drain_phase2()
        assert len(_to(transport, TOPIC)) == 1
        assert len(await _rows(db, "SELECT 1 FROM proposals")) == 1
    finally:
        await db.close()


async def test_repeated_abnormal_reading_after_recovery_does_not_duplicate_the_alert(
    tmp_path: Path,
) -> None:
    path = str(tmp_path / "liot.db")
    clock = Clock(fixed=T0)
    await _kill_during(path, clock, GAS, **PROPOSAL_ONLY)
    svc, db, transport = await _restart(path, clock)
    try:
        assert len(_to(transport, TOPIC)) == 1
        res = await svc.handle_reading(_reading(GAS, "on"))
        await svc.drain_phase2()
        assert res is None
        assert len(_to(transport, TOPIC)) == 1
        assert len(await _rows(db, "SELECT 1 FROM proposals")) == 1
    finally:
        await db.close()


async def test_reading_during_an_in_flight_opening_is_not_taken_for_an_orphan() -> None:
    # 같은 센서의 이상 신호 둘이 동시에 들어와도 첫 알림은 한 번이다(여는 도중의 상황은 고아가 아니다).
    svc, db, transport, _ = await _make()
    try:
        await asyncio.gather(
            svc.handle_reading(_reading(GAS, "on")), svc.handle_reading(_reading(GAS, "on"))
        )
        await svc.drain_phase2()
        assert len(_to(transport, TOPIC)) == 1
        assert len(await _rows(db, "SELECT 1 FROM proposals")) == 1
    finally:
        await db.close()


# ==========================================================================
# 정상 경로는 그대로(회귀): 죽지 않은 상황은 복구가 건드리지 않는다
# ==========================================================================
async def test_healthy_open_situation_is_not_touched_by_recovery(tmp_path: Path) -> None:
    path = str(tmp_path / "liot.db")
    clock = Clock(fixed=T0)
    db = await init_db(path)
    svc, _, transport, _ = await _make(db=db, clock=clock)
    res = await svc.handle_reading(_reading(GAS, "on"))
    await svc.drain_phase2()
    assert res is not None and res.sent
    await db.close()

    svc2, db2, transport2 = await _restart(path, clock)
    try:
        assert transport2.sent == []
        assert await svc2._recover_open_situations() == 0
    finally:
        await db2.close()


async def test_list_open_situations_is_scoped_to_the_home_and_to_open_episodes(
    tmp_path: Path,
) -> None:
    db = await init_db(str(tmp_path / "liot.db"))
    svc, _, _, _ = await _make(db=db)
    try:
        sid_a, _ = await svc.safety.open_or_get_situation(
            HOME, "gas", GAS, SeverityLevel.CRITICAL, T0
        )
        sid_b, _ = await svc.safety.open_or_get_situation(
            HOME, "door", DOOR, SeverityLevel.WARNING, _at(5)
        )
        await svc.safety.open_or_get_situation("other", "gas", GAS, SeverityLevel.CRITICAL, T0)
        await svc.safety.close_situation(sid_b, _at(9))
        listed = await svc.safety.list_open_situations(HOME)
        assert [s.situation_id for s in listed] == [sid_a]
    finally:
        await db.close()
