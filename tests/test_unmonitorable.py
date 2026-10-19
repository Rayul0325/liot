"""HA 읽기 불가(`unavailable`·`unknown`) = 판정 보류 → 거주자 감시 불가 알림.

규칙(계획 2026-10-19 Phase 1):
  - 정규화 값이 문자열 `unavailable`·`unknown` 이면 '판정 보류'다. 이상도 정상도 아니다.
    그 밖의 정규화 불가 값(깨진 문자열·빈 값·NaN)은 지금처럼 이상(fail-closed)이다.
  - 보류는 상황을 열지 않고, 열린 상황을 닫지 않고, 열린 사다리를 건드리지 않고,
    보호자 경로에 들어가지 않는다.
  - 거주자에게만 요약 알림 경로로 '<센서 종류> 센서 · 감시 불가(연결 끊김)'(주의)를
    보낸다. 헌장 OFF 만 존중하고 음소거는 무시한다.
  - 표식(엔티티별, 메모리): 전송에 성공했을 때만 남긴다. 실패하면 다음 읽기 불가 값에서
    다시 시도하되 같은 엔티티는 10분에 한 번만. 헌장 OFF 면 보내지 않고 표식은 남긴다.
    정상·이상 값이 오면 표식을 지운다.

시각은 전부 고정 Clock 을 옮겨서 잰다(실시간 잠자기 없음).
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta
from typing import Any

import pytest

from liot.app import LiotApp
from liot.core.clock import Clock
from liot.core.contracts import CandidateAction, ContextSnapshot, SensorReading
from liot.core.egress_gate import NTFY_ALLOWED_KEYS
from liot.core.risk import RiskTier
from liot.core.severity import SeverityLevel
from liot.db.database import init_db
from liot.layers.l1_sensing.normalizer import normalize
from liot.layers.l3_judgment.llm_judge import LLMResult
from liot.layers.l4_execution import escalation
from liot.layers.l4_execution.ntfy_transport import FakeNtfyTransport, HTTPNtfyTransport
from liot.service import UNMONITORABLE_RETRY, LiotService

T0 = "2026-06-02T12:00:00+00:00"
SECRET = b"test-secret-key"
TOPIC = "liot-test"
GAS = "binary_sensor.kitchen_gas"
SMOKE = "binary_sensor.hall_smoke"
TEMP = "sensor.kitchen_temp"
CATS = {GAS: "gas", SMOKE: "smoke", TEMP: "high_temp", "light.living_room": "light"}
HOME = "singleton"
GAS_NOTICE = "가스 센서 · 감시 불가(연결 끊김)"


class _Backend:
    """하한 그대로 돌려주는 결정 모델."""

    model_id = "unmonitorable-test-model"

    def judge_raw(self, candidate: CandidateAction, ctx: ContextSnapshot | None,
                  gate1_floor: RiskTier,
                  severity_floor: SeverityLevel | None = None) -> LLMResult:
        return LLMResult(tier=RiskTier.EMBER, confidence=0.8, rationale="test",
                         model_id=self.model_id, severity=None)


# ── 도우미 ───────────────────────────────────────────────────────────────────
def _reading(eid: str, raw: object) -> SensorReading:
    return SensorReading(
        entity_id=eid, source="test", raw_value=raw, norm_value=normalize(raw),
        ts_utc=T0, home_id=HOME,
    )


def _at(seconds: float) -> str:
    return (datetime.fromisoformat(T0) + timedelta(seconds=seconds)).isoformat()


async def _make(*, transport: Any = None):
    clock = Clock(fixed=T0)
    db = await init_db(":memory:")
    app = LiotApp(clock=clock, categories=CATS, llm_backend=_Backend())
    transport = transport if transport is not None else FakeNtfyTransport()
    svc = LiotService(app, db, clock=clock, transport=transport, topic=TOPIC, secret=SECRET)
    return svc, db, transport, clock


async def _guardian(svc: LiotService) -> tuple[str, str]:
    rid = await svc.safety.add_resident(HOME, "엄마")
    gid, topic = await svc.safety.add_guardian(HOME, "딸")
    await svc.safety.grant_consent(HOME, gid, rid)
    return gid, topic


async def _count(db: Any, table: str) -> int:
    cur = await db.execute(f"SELECT COUNT(*) FROM {table}")  # noqa: S608 — 테스트 고정 이름
    try:
        row = await cur.fetchone()
    finally:
        await cur.close()
    return int(row[0])


def _notices(transport: FakeNtfyTransport) -> list[dict[str, Any]]:
    """거주자 토픽으로 나간 감시 불가 알림만."""
    return [
        p for t, p in transport.sent
        if t == TOPIC and "감시 불가" in str(p.get("situation_label", ""))
    ]


async def _hold(svc: LiotService, eid: str = GAS, raw: object = "unavailable") -> None:
    assert await svc.handle_reading(_reading(eid, raw)) is None


# ==========================================================================
# 1) 판정: 보류는 이상도 정상도 아니다
# ==========================================================================
def test_classify_safety_marks_unavailable_and_unknown_unmonitorable() -> None:
    app = LiotApp(clock=Clock(fixed=T0), categories=CATS, llm_backend=_Backend())
    for raw in ("unavailable", "unknown", "Unavailable", " UNKNOWN "):
        sig = app.classify_safety(_reading(GAS, raw))
        assert sig is not None
        assert sig.unmonitorable is True and sig.abnormal is False
    temp = app.classify_safety(_reading(TEMP, "unavailable"))
    assert temp is not None and temp.unmonitorable is True and temp.abnormal is False
    for raw, abnormal in (("on", True), ("off", False), ("??garbled??", True), ("", True)):
        sig = app.classify_safety(_reading(GAS, raw))
        assert sig is not None
        assert sig.unmonitorable is False and sig.abnormal is abnormal


def test_message_label_uses_the_sensor_kind_not_the_device() -> None:
    assert escalation.message_label("gas", "unmonitorable") == GAS_NOTICE
    assert escalation.message_label("smoke", "unmonitorable") == "연기 센서 · 감시 불가(연결 끊김)"
    assert escalation.message_label("lock", "unmonitorable") == "잠금장치 센서 · 감시 불가(연결 끊김)"
    # 모르는 카테고리도 문구가 끊기지 않는다(기기 이름으로 채우지 않는다).
    assert escalation.message_label("co2", "unmonitorable") == "안전 센서 · 감시 불가(연결 끊김)"
    # 기존 라벨은 그대로.
    assert escalation.message_label("gas", "forward") == "가스 누출"


# ==========================================================================
# 2) 열린 상황 없음: 상황·사다리·전달 0, 거주자 알림 1
# ==========================================================================
async def test_unavailable_without_open_situation_sends_one_resident_notice_only() -> None:
    svc, db, transport, _ = await _make()
    try:
        _, gtopic = await _guardian(svc)
        await _hold(svc)
        assert await _count(db, "situations") == 0
        assert await _count(db, "escalation_ladders") == 0
        assert await _count(db, "guardian_forwards") == 0
        assert await _count(db, "proposals") == 0
        assert await svc.safety.get_open_situation(HOME, "gas", GAS) is None
        assert [t for t, _ in transport.sent] == [TOPIC]
        assert [p for t, p in transport.sent if t == gtopic] == []
        (notice,) = _notices(transport)
        assert notice["situation_label"] == GAS_NOTICE
        assert notice["severity_label"] == SeverityLevel.NOTICE.label_ko
    finally:
        await db.close()


async def test_repeated_unavailable_sends_nothing_more() -> None:
    svc, db, transport, clock = await _make()
    try:
        await _hold(svc)
        await _hold(svc)
        await _hold(svc, raw="unknown")
        clock.set_fixed(_at(3600))
        await _hold(svc)
        assert len(_notices(transport)) == 1
        assert len(transport.sent) == 1
    finally:
        await db.close()


async def test_normal_then_unavailable_again_sends_one_more() -> None:
    svc, db, transport, _ = await _make()
    try:
        await _hold(svc)
        assert await svc.handle_reading(_reading(GAS, "off")) is None
        await _hold(svc)
        assert len(_notices(transport)) == 2
        assert await _count(db, "situations") == 0
    finally:
        await db.close()


async def test_markers_are_per_entity() -> None:
    svc, db, transport, _ = await _make()
    try:
        await _hold(svc, GAS)
        await _hold(svc, SMOKE)
        await _hold(svc, GAS)
        labels = [p["situation_label"] for p in _notices(transport)]
        assert labels == [GAS_NOTICE, "연기 센서 · 감시 불가(연결 끊김)"]
    finally:
        await db.close()


async def test_flicker_unavailable_then_on_still_opens_the_situation() -> None:
    # 보류가 진짜 위험을 가리지 않는다: 끊겼다가 'on' 이 오면 상황이 열린다.
    svc, db, transport, _ = await _make()
    try:
        await _hold(svc)
        res = await svc.handle_reading(_reading(GAS, "on"))
        await svc.drain_phase2()
        assert res is not None and res.proposal.severity == SeverityLevel.CRITICAL
        assert await svc.safety.get_open_situation(HOME, "gas", GAS) is not None
        # 이상 값이 표식을 지웠으므로 다시 끊기면 한 건 더(상황은 열린 채).
        await _hold(svc)
        assert len(_notices(transport)) == 2
        assert await svc.safety.get_open_situation(HOME, "gas", GAS) is not None
    finally:
        await db.close()


# ==========================================================================
# 3) 열린 상황 중: 상황·사다리 그대로, 90초 보호자 전달도 그대로
# ==========================================================================
async def test_open_situation_stays_open_and_ladder_unchanged() -> None:
    svc, db, transport, clock = await _make()
    try:
        gid, gtopic = await _guardian(svc)
        res = await svc.handle_reading(_reading(GAS, "on"))
        await svc.drain_phase2()
        assert res is not None
        sid = res.proposal.situation_id
        assert sid is not None
        before = await svc.safety.get_ladder(sid)
        assert before is not None and before.next_fire_at == _at(60)

        clock.set_fixed(_at(10))
        await _hold(svc)
        situation = await svc.safety.get_open_situation(HOME, "gas", GAS)
        assert situation is not None and situation.situation_id == sid
        assert await svc.safety.get_ladder(sid) == before
        assert await _count(db, "situations") == 1
        assert len(_notices(transport)) == 1
        assert [p for t, p in transport.sent if t == gtopic] == []

        clock.set_fixed(_at(89))
        await svc.ladder_tick()
        assert [p for t, p in transport.sent if t == gtopic] == []
        clock.set_fixed(_at(90))
        await svc.ladder_tick()
        forwards = [(f.guardian_id, f.kind) for f in await svc.safety.list_forwards(HOME)]
        assert forwards == [(gid, "forward")]
        assert len([p for t, p in transport.sent if t == gtopic]) == 1
        assert await svc.safety.get_open_situation(HOME, "gas", GAS) is not None
    finally:
        await db.close()


# ==========================================================================
# 4) 게이트: 음소거 무시, 헌장 OFF 존중(표식은 남김)
# ==========================================================================
@pytest.mark.parametrize("key,value", [("muted_categories", ["gas"]), ("muted_entities", [GAS])])
async def test_mute_does_not_hide_the_notice(key: str, value: list[str]) -> None:
    svc, db, transport, _ = await _make()
    try:
        await svc.prefs.set(key, value)
        await _hold(svc)
        assert len(_notices(transport)) == 1
    finally:
        await db.close()


async def test_charter_off_sends_nothing_but_marks() -> None:
    svc, db, transport, _ = await _make()
    try:
        await svc.prefs.set("charter_enabled", False)
        await _hold(svc)
        assert transport.sent == []
        # 헌장을 다시 켜도 값이 돌아오기 전까지는 알림이 없다(결정: 표식이 남는다).
        await svc.prefs.set("charter_enabled", True)
        await _hold(svc)
        assert transport.sent == []
        assert await svc.handle_reading(_reading(GAS, "off")) is None
        await _hold(svc)
        assert len(_notices(transport)) == 1
    finally:
        await db.close()


# ==========================================================================
# 5) 전송 실패 → 10분 뒤 다음 읽기 불가 값에서 재시도
# ==========================================================================
def test_retry_interval_is_ten_minutes() -> None:
    assert timedelta(minutes=10) == UNMONITORABLE_RETRY


async def test_failed_send_retries_after_ten_minutes() -> None:
    transport = FakeNtfyTransport(fail=True)
    svc, db, _, clock = await _make(transport=transport)
    try:
        await _hold(svc)
        assert transport.sent == []
        transport.fail = False
        clock.set_fixed(_at(60))
        await _hold(svc)
        clock.set_fixed(_at(599))
        await _hold(svc)
        assert transport.sent == []  # 10분 안에는 다시 시도하지 않는다
        clock.set_fixed(_at(600))
        await _hold(svc)
        assert len(_notices(transport)) == 1
        clock.set_fixed(_at(1300))
        await _hold(svc)
        assert len(_notices(transport)) == 1  # 성공 뒤엔 표식이 막는다
    finally:
        await db.close()


async def test_failed_attempts_are_throttled_while_failing() -> None:
    transport = FakeNtfyTransport(fail=True)
    attempts: list[str] = []
    original = transport.send

    def counting_send(topic: str, sanitized: dict[str, Any]) -> None:
        attempts.append(topic)
        original(topic, sanitized)

    transport.send = counting_send  # type: ignore[method-assign]
    svc, db, _, clock = await _make(transport=transport)
    try:
        for t in (0, 30, 300, 599):
            clock.set_fixed(_at(t))
            await _hold(svc)
        assert attempts == [TOPIC]
        clock.set_fixed(_at(600))
        await _hold(svc)
        assert attempts == [TOPIC, TOPIC]
    finally:
        await db.close()


async def test_sanitize_only_mode_does_not_crash_and_does_not_mark() -> None:
    clock = Clock(fixed=T0)
    db = await init_db(":memory:")
    app = LiotApp(clock=clock, categories=CATS, llm_backend=_Backend())
    svc = LiotService(app, db, clock=clock, transport=None, topic=TOPIC, secret=SECRET)
    try:
        await _hold(svc)
        assert await _count(db, "situations") == 0
        # 배달이 없었으므로 표식이 없다 — transport 가 붙은 뒤(10분 뒤) 다시 시도한다.
        svc.transport = FakeNtfyTransport()
        clock.set_fixed(_at(600))
        await _hold(svc)
        assert len(_notices(svc.transport)) == 1
    finally:
        await db.close()


# ==========================================================================
# 6) 페이로드: 기기·방 없음, egress 통과, 링크·버튼 없음
# ==========================================================================
async def test_payload_has_no_entity_or_room_and_passes_egress() -> None:
    svc, db, transport, _ = await _make()
    try:
        await _hold(svc)
        await _hold(svc, TEMP)
        notices = _notices(transport)
        assert len(notices) == 2
        for p in notices:
            assert set(p) <= NTFY_ALLOWED_KEYS
            assert set(p) == {"channel", "situation_label", "home_alias", "at", "severity_label"}
            assert p["home_alias"] == "우리 집"
            assert p["at"] == T0
            dumped = json.dumps(p, ensure_ascii=False)
            for forbidden in (GAS, TEMP, "kitchen", "주방", "entity_id", "room", "device"):
                assert forbidden not in dumped
        tr = HTTPNtfyTransport("http://ntfy.invalid")
        headers = tr._headers(notices[0])
        assert headers["Priority"] == SeverityLevel.NOTICE.ntfy_priority
        assert "Click" not in headers
        assert "tel:119" not in headers.get("Actions", "")
    finally:
        await db.close()


async def test_home_alias_rejected_by_egress_falls_back_to_default_alias() -> None:
    svc, db, transport, _ = await _make()
    try:
        await svc.safety.set_home_alias(HOME, "010-1234-5678 집")  # PII 스캔에 걸린다
        await _hold(svc)
        (notice,) = _notices(transport)
        assert notice["home_alias"] == "우리 집"
        assert "010" not in json.dumps(transport.sent, ensure_ascii=False)
    finally:
        await db.close()


async def test_configured_home_alias_is_used_when_clean() -> None:
    svc, db, transport, _ = await _make()
    try:
        await svc.safety.set_home_alias(HOME, "할머니 댁")
        await _hold(svc)
        (notice,) = _notices(transport)
        assert notice["home_alias"] == "할머니 댁"
    finally:
        await db.close()


# ==========================================================================
# 7) 판정 불가지만 보류가 아닌 값은 여전히 위험 상황
# ==========================================================================
@pytest.mark.parametrize("raw", ["??garbled??", ""])
async def test_garbled_value_still_opens_a_critical_situation(raw: str) -> None:
    svc, db, transport, _ = await _make()
    try:
        res = await svc.handle_reading(_reading(GAS, raw))
        await svc.drain_phase2()
        assert res is not None and res.proposal.action.action == "alert"
        assert res.proposal.severity == SeverityLevel.CRITICAL
        assert await svc.safety.get_open_situation(HOME, "gas", GAS) is not None
        assert _notices(transport) == []
    finally:
        await db.close()
