"""U5: 첫 거주자 알림 송신이 실패하면 30초 뒤 다시 보낸다(게이트 1: 안전 알림은 늦지 않는다).

E2 C2.9 가 100/100 로 잡은 결함: 상황이 열릴 때 첫 거주자 알림이 NtfyError 로 실패하면
상황은 열린 채 침묵하고, 다음 시도는 사다리의 첫 재알림(위험 60초·경고 600초)이었다.

고친 규칙(docs/business-rules.md '알림 사다리'):
  - 첫 알림이 못 나갔으면 사다리의 다음 발화를 LADDER_RETRY(30초) 뒤로 당긴다. 성공할 때까지 30초마다.
  - 기준 시각은 **상황이 열린 시각** 그대로다 — 재알림(60초·600초)·보호자 전달(90초)·119 안내(90초+5분)
    시각은 한 칸도 움직이지 않는다. 첫 알림 재시도는 재알림 횟수(WARNING 3회)를 쓰지 않는다.
  - 헌장 OFF·음소거·최근 거절로 막힌 첫 알림은 송신 실패가 아니라 사용자가 정한 침묵이라 재시도가 없다.

시각은 고정 Clock 으로 옮긴다(잠자기 없음). 송신 실패는 거주자 토픽으로 가는 앞의 N 번만 실패시킨다.
"""

from __future__ import annotations

from datetime import timedelta
from typing import Any

from liot.core.risk import RiskTier
from liot.core.severity import SeverityLevel
from liot.layers.l4_execution import escalation
from liot.layers.l4_execution.notifier import dedup_key
from liot.layers.l4_execution.ntfy_transport import FakeNtfyTransport, NtfyError
from tests.test_escalation_ladder import (
    DOOR,
    GAS,
    TOPIC,
    _advance,
    _at,
    _forwards,
    _guardian,
    _make,
    _open,
    _reading,
    _to,
)

RETRY_S = escalation.LADDER_RETRY.total_seconds()


class _FailFirstResidentSends(FakeNtfyTransport):
    """거주자 토픽(TOPIC)으로 가는 앞의 n 번만 실패시킨다. 그 뒤로는 복구(기록).

    `attempts` 는 거주자 토픽으로 시도한 횟수(실패 포함)다. 보호자 토픽은 항상 성공한다.
    """

    def __init__(self, failures: int) -> None:
        super().__init__()
        self.failures = failures
        self.attempts = 0

    def send(self, topic: str, sanitized: dict[str, Any]) -> None:
        if topic == TOPIC:
            self.attempts += 1
            if self.attempts <= self.failures:
                raise NtfyError("forced first-alert failure")
        super().send(topic, sanitized)


def test_retry_constant_is_the_guardian_retry_interval() -> None:
    # 새 매직 넘버를 만들지 않았다: 첫 알림 재시도 간격 = 보호자 전달 재시도 간격 = 30초.
    assert timedelta(seconds=30) == escalation.LADDER_RETRY


# ==========================================================================
# 1) CRITICAL 하한: 첫 알림은 +30초(+60초 아님)에 나가고, 그 뒤 사다리는 명세 그대로
# ==========================================================================
async def test_critical_first_alert_retried_at_30s_not_60s() -> None:
    svc, db, transport, clock = await _make(transport=_FailFirstResidentSends(1))
    try:
        res = await _open(svc)
        sid = res.proposal.situation_id
        assert res.sent is False and res.error is not None  # 첫 송신 실패
        assert _to(transport, TOPIC) == []
        ladder = await svc.safety.get_ladder(sid)
        assert ladder is not None and ladder.next_fire_at == _at(RETRY_S)

        await _advance(svc, clock, RETRY_S - 1)
        assert _to(transport, TOPIC) == []
        await _advance(svc, clock, RETRY_S)
        delivered = _to(transport, TOPIC)
        assert len(delivered) == 1
        assert delivered[0]["severity_label"] == SeverityLevel.CRITICAL.label_ko

        # 성공한 뒤에는 재알림이 상황 열림 기준 1분 주기로 이어진다(+60, +120).
        ladder = await svc.safety.get_ladder(sid)
        assert ladder is not None and ladder.next_fire_at == _at(60)
        assert ladder.stage == 0  # 재시도는 재알림이 아니다
        await _advance(svc, clock, 59)
        assert len(_to(transport, TOPIC)) == 1
        await _advance(svc, clock, 60)
        assert len(_to(transport, TOPIC)) == 2
        await _advance(svc, clock, 120)
        assert len(_to(transport, TOPIC)) == 3
    finally:
        await db.close()


# ==========================================================================
# 2) WARNING 하한: +30초(+600초 아님), 재알림 3회 예산은 그대로
# ==========================================================================
async def test_warning_first_alert_retried_at_30s_and_realert_budget_intact() -> None:
    svc, db, transport, clock = await _make(transport=_FailFirstResidentSends(1))
    try:
        res = await _open(svc, DOOR)
        sid = res.proposal.situation_id
        assert res.proposal.severity == SeverityLevel.WARNING
        assert res.sent is False and res.error is not None
        assert _to(transport, TOPIC) == []

        await _advance(svc, clock, RETRY_S - 1)
        assert _to(transport, TOPIC) == []
        await _advance(svc, clock, RETRY_S)
        assert len(_to(transport, TOPIC)) == 1  # 첫 알림이 30초에 나갔다

        ladder = await svc.safety.get_ladder(sid)
        assert ladder is not None and ladder.stage == 0
        assert ladder.next_fire_at == _at(600)  # 재알림 기준은 열린 시각 그대로

        await _advance(svc, clock, 599)
        assert len(_to(transport, TOPIC)) == 1
        # 재알림 3회(600·1200·1800)가 전부 남아 있다 — 첫 알림 재시도가 하나도 쓰지 않았다.
        for n, t in enumerate((600, 1200, 1800), start=2):
            await _advance(svc, clock, t)
            assert len(_to(transport, TOPIC)) == n
        await _advance(svc, clock, 2400)
        await _advance(svc, clock, 9000)
        assert len(_to(transport, TOPIC)) == 1 + 3
        ladder = await svc.safety.get_ladder(sid)
        assert ladder is not None and ladder.next_fire_at is None
    finally:
        await db.close()


# ==========================================================================
# 3) 반복 실패: 성공할 때까지 30초마다, 성공한 뒤에는 중복 없음
# ==========================================================================
async def test_repeated_failure_retries_every_30s_until_success_without_duplicates() -> None:
    # 첫 알림 + 재시도 2번 실패, 세 번째 재시도에서 성공.
    svc, db, transport, clock = await _make(transport=_FailFirstResidentSends(3))
    try:
        res = await _open(svc, DOOR)
        sid = res.proposal.situation_id
        assert transport.attempts == 1

        for t, attempts in ((30, 2), (60, 3)):
            await _advance(svc, clock, t - 1)
            assert transport.attempts == attempts - 1  # 경계 직전엔 시도 없음
            await _advance(svc, clock, t)
            assert transport.attempts == attempts
            assert _to(transport, TOPIC) == []  # 아직 실패 중
            ladder = await svc.safety.get_ladder(sid)
            assert ladder is not None and ladder.next_fire_at == _at(t + RETRY_S)

        await _advance(svc, clock, 90)
        assert transport.attempts == 4
        assert len(_to(transport, TOPIC)) == 1  # 네 번째 시도에서 처음 나갔다

        # 성공 뒤 같은 상황에서 더는 첫 알림을 다시 보내지 않는다.
        for t in (91, 120, 150, 300, 599):
            await _advance(svc, clock, t)
        assert transport.attempts == 4 and len(_to(transport, TOPIC)) == 1
        await _advance(svc, clock, 600)  # 그 다음은 WARNING 의 첫 재알림 시각
        assert len(_to(transport, TOPIC)) == 2
    finally:
        await db.close()


async def test_critical_tick_where_retry_and_realert_coincide_sends_once() -> None:
    # 위험: +60 틱은 첫 알림 재시도와 첫 재알림이 겹친다. 그 틱에서 송신 시도는 한 번뿐이다
    # (그 시도가 실패해도 같은 틱에 또 보내지 않고 +90 에 다시 시도한다).
    svc, db, transport, clock = await _make(transport=_FailFirstResidentSends(3))
    try:
        res = await _open(svc)
        await _advance(svc, clock, 30)
        assert transport.attempts == 2  # 재시도 1 실패
        await _advance(svc, clock, 60)
        assert transport.attempts == 3  # 겹친 틱: 한 번만 시도(실패)
        assert _to(transport, TOPIC) == []
        ladder = await svc.safety.get_ladder(res.proposal.situation_id)
        assert ladder is not None and ladder.next_fire_at == _at(60 + RETRY_S)
        await _advance(svc, clock, 90)
        assert transport.attempts == 4 and len(_to(transport, TOPIC)) == 1
    finally:
        await db.close()


# ==========================================================================
# 4) 보호자 90초·119(90초+5분)는 첫 알림 재시도와 상관없이 그대로
# ==========================================================================
async def test_guardian_90s_and_119_timing_unchanged_by_first_alert_retry() -> None:
    # 거주자 첫 알림이 앞의 3번(+0, +30, +60) 실패한다. 보호자 토픽은 정상.
    svc, db, transport, clock = await _make(transport=_FailFirstResidentSends(3))
    try:
        _, gid, gtopic = await _guardian(svc)
        res = await _open(svc)
        sid = res.proposal.situation_id
        await _advance(svc, clock, 30)
        await _advance(svc, clock, 60)
        assert transport.attempts == 3
        assert _to(transport, gtopic) == []
        await _advance(svc, clock, 89)
        assert _to(transport, gtopic) == [] and await _forwards(svc) == []
        await _advance(svc, clock, 90)
        assert len(_to(transport, gtopic)) == 1  # 정확히 90초
        assert await _forwards(svc) == [(gid, "forward")]
        assert len(_to(transport, TOPIC)) == 1  # 같은 틱에 거주자 첫 알림도 복구됐다

        await _advance(svc, clock, 90 + 299)
        assert (None, "guidance_119") not in await _forwards(svc)
        await _advance(svc, clock, 90 + 300)
        assert await _forwards(svc) == [(gid, "forward"), (None, "guidance_119")]
        ladder = await svc.safety.get_ladder(sid)
        assert ladder is not None and ladder.guidance_119_sent is True
    finally:
        await db.close()


async def test_forward_timing_with_one_failed_first_alert() -> None:
    # 가장 흔한 경우(첫 알림 한 번 실패)에도 전달은 90초, 119 는 90초+5분.
    svc, db, transport, clock = await _make(transport=_FailFirstResidentSends(1))
    try:
        _, gid, gtopic = await _guardian(svc)
        await _open(svc)
        await _advance(svc, clock, 30)
        await _advance(svc, clock, 89)
        assert _to(transport, gtopic) == []
        await _advance(svc, clock, 90)
        assert len(_to(transport, gtopic)) == 1
        await _advance(svc, clock, 389)
        assert (None, "guidance_119") not in await _forwards(svc)
        await _advance(svc, clock, 390)
        assert (None, "guidance_119") in await _forwards(svc)
    finally:
        await db.close()


# ==========================================================================
# 5) 사용자가 정한 침묵(헌장 OFF·음소거)은 송신 실패가 아니다 — 재시도 없음
# ==========================================================================
async def test_charter_off_has_no_first_alert_retry() -> None:
    svc, db, transport, clock = await _make()
    try:
        await svc.prefs.set("charter_enabled", False)
        res = await _open(svc)
        assert res.sent is False and res.error is None
        ladder = await svc.safety.get_ladder(res.proposal.situation_id)
        assert ladder is not None and ladder.next_fire_at == _at(60)  # 30초 재시도가 없다
        await _advance(svc, clock, 30)
        await _advance(svc, clock, 59)
        assert transport.sent == []
    finally:
        await db.close()


async def test_charter_turned_off_after_failed_first_alert_stops_the_retry() -> None:
    svc, db, transport, clock = await _make(transport=_FailFirstResidentSends(1))
    try:
        await _open(svc)
        await svc.prefs.set("charter_enabled", False)
        await _advance(svc, clock, 30)
        assert transport.attempts == 1 and _to(transport, TOPIC) == []
    finally:
        await db.close()


async def test_muted_warning_has_no_first_alert_retry() -> None:
    svc, db, transport, clock = await _make()
    try:
        await svc.prefs.set("muted_entities", [DOOR])
        res = await _open(svc, DOOR)
        assert res.sent is False and res.error is None
        ladder = await svc.safety.get_ladder(res.proposal.situation_id)
        assert ladder is not None and ladder.next_fire_at == _at(600)
        await _advance(svc, clock, 30)
        assert transport.sent == []
    finally:
        await db.close()


# ==========================================================================
# 6) 재시작: 재시도는 DB 상태에서 다시 읽혀 유지된다
# ==========================================================================
async def test_restart_rearm_preserves_pending_first_alert_retry() -> None:
    svc, db, first, clock = await _make(transport=_FailFirstResidentSends(1))
    try:
        res = await _open(svc)
        sid = res.proposal.situation_id
        assert _to(first, TOPIC) == []
        ladder = await svc.safety.get_ladder(sid)
        assert ladder is not None and ladder.next_fire_at == _at(RETRY_S)

        # 10초 뒤 데몬이 다시 시작한다: 같은 DB, 새 서비스·새 transport, 메모리 상태 없음.
        clock.set_fixed(_at(10))
        svc2, _, transport2, _ = await _make(db=db, clock=clock)
        await svc2.start()
        ladder = await svc2.safety.get_ladder(sid)
        # 이미 잡힌 재시도(+30)를 미루지 않는다.
        assert ladder is not None and ladder.next_fire_at == _at(RETRY_S)
        await _advance(svc2, clock, RETRY_S - 1)
        assert transport2.sent == []
        await _advance(svc2, clock, RETRY_S)
        sent = _to(transport2, TOPIC)
        assert len(sent) == 1 and sent[0]["proposal_id"] == res.proposal.proposal_id
    finally:
        await db.close()


async def test_restart_after_first_alert_delivered_does_not_resend_it() -> None:
    svc, db, transport, clock = await _make()
    try:
        res = await _open(svc)
        assert len(_to(transport, TOPIC)) == 1
        svc2, _, transport2, _ = await _make(db=db, clock=clock)
        await svc2.start()
        ladder = await svc2.safety.get_ladder(res.proposal.situation_id)
        assert ladder is not None and ladder.next_fire_at == _at(60)
        await _advance(svc2, clock, RETRY_S)
        assert transport2.sent == []
    finally:
        await db.close()


# ==========================================================================
# 7) 재시도 전에 상황이 해소되면 더는 보내지 않는다
# ==========================================================================
async def test_resolution_before_retry_stops_it() -> None:
    svc, db, transport, clock = await _make(transport=_FailFirstResidentSends(1))
    try:
        await _open(svc)
        await svc.handle_reading(_reading(GAS, "off"))  # 해소(거주자 해소 알림 시도)
        attempts_after_close = transport.attempts
        sent_after_close = len(transport.sent)
        await _advance(svc, clock, RETRY_S)
        await _advance(svc, clock, 120)
        assert transport.attempts == attempts_after_close
        assert len(transport.sent) == sent_after_close
    finally:
        await db.close()


# ==========================================================================
# 8) 가드 하나하나를 못 박는 시험(뮤테이션 검사로 각각 정확히 이 시험만 깨지는지 확인)
# ==========================================================================
async def test_ack_landing_while_evaluating_unsent_alert_prevents_the_resend() -> None:
    """발화가 '못 나갔나'를 평가하는 사이 확인(ack)이 들어오면 재전송하지 않는다."""
    svc, db, transport, clock = await _make(transport=_FailFirstResidentSends(1))
    try:
        res = await _open(svc)
        sid = res.proposal.situation_id
        assert transport.attempts == 1

        real = svc._first_alert_unsent

        async def _ack_during_evaluation(info: Any) -> Any:
            found = await real(info)
            assert found is not None  # 평가 시점엔 아직 못 나간 알림이다
            assert await svc._ack_situation(sid, "resident")  # 평가 도중 확인이 도착
            return found

        svc._first_alert_unsent = _ack_during_evaluation  # type: ignore[method-assign]
        await _advance(svc, clock, RETRY_S)
        assert transport.attempts == 1 and _to(transport, TOPIC) == []
        ladder = await svc.safety.get_ladder(sid)
        assert ladder is not None and ladder.acked
    finally:
        await db.close()


async def test_service_without_transport_neither_attempts_nor_records_a_resend() -> None:
    """배달 경로가 없는 서비스(sanitize-only)는 첫 알림 재시도를 잡지도, 기록하지도 않는다."""
    svc, db, transport, clock = await _make(transport=_FailFirstResidentSends(1))
    try:
        res = await _open(svc)
        sid = res.proposal.situation_id
        svc.transport = None  # 배달 경로가 사라졌다(예: sanitize-only 로 재시작)
        info = await escalation.load_situation(db, sid)
        assert info is not None
        assert await svc._first_alert_unsent(info) is None

        await svc._arm_ladder(sid)
        ladder = await svc.safety.get_ladder(sid)
        assert ladder is not None and ladder.next_fire_at == _at(60)  # 30초 재시도가 없다
        await _advance(svc, clock, RETRY_S)
        cur = await db.execute("SELECT COUNT(*) FROM notifications")
        row = await cur.fetchone()
        await cur.close()
        assert row is not None and row[0] == 0  # 기록된 알림 없음
        assert transport.attempts == 1  # 송신 시도도 없다
    finally:
        await db.close()


async def test_resend_updates_the_dedup_window_like_a_normal_first_alert() -> None:
    """재전송이 성공하면 dedup 창이 갱신돼, 같은 류의 비우회 제안이 2시간 안에 두 번 나가지 않는다."""
    svc, db, transport, clock = await _make(transport=_FailFirstResidentSends(1))
    try:
        res = await _open(svc)
        key = dedup_key(res.proposal)
        assert key not in svc._recent_notified_keys(clock.now_dt())  # 첫 송신 실패: 오염 없음
        await _advance(svc, clock, RETRY_S)
        assert len(_to(transport, TOPIC)) == 1
        assert key in svc._recent_notified_keys(clock.now_dt())

        twin = res.proposal.model_copy(
            update={
                "proposal_id": "twin-proposal",
                "risk_tier": RiskTier.BREATH,
                "severity": None,
                "perishable": False,
                "situation_id": None,
            }
        )
        out = await svc.dispatch(twin)
        assert out.decision == "SUPPRESS" and out.sent is False
    finally:
        await db.close()
