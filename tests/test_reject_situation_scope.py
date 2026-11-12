"""D2(결정 0015): 상황 알림의 '거절'은 그 상황의 사다리만 멈춘다.

- 경고 상황(문 열림)을 거절·닫은 뒤, 30일 안의 두 번째 에피소드도 첫 알림이 즉시 나간다.
- 상황이 아닌 제안(편의 등)의 거절은 예전처럼 같은 류를 30일 침묵시킨다.
- 음소거는 경고 상황에도 그대로 적용된다(30일 조용히 하려면 음소거).

시각은 고정 Clock 을 옮겨서 잰다. 상황 알림이 ``situation_id`` 를 싣는지가 가드의 핵심이라,
같은 키(capability:action:entity:category)로 상황 제안과 비상황 제안을 나란히 둔다.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta

from liot.app import LiotApp
from liot.core.clock import Clock
from liot.core.contracts import CandidateAction, FeedbackEvent, Proposal, SensorReading
from liot.core.risk import RiskTier
from liot.db.database import init_db
from liot.layers.l1_sensing.normalizer import normalize
from liot.layers.l4_execution.notifier import dedup_key, sign_feedback
from liot.layers.l4_execution.ntfy_transport import FakeNtfyTransport
from liot.service import LiotService

T0 = "2026-06-02T12:00:00+00:00"
SECRET = b"test-secret-key"
TOPIC = "liot-test"
DOOR = "binary_sensor.front_door"
CATS = {DOOR: "door", "light.living_room": "light"}
HOME = "singleton"


def _reading(eid: str, raw: object) -> SensorReading:
    return SensorReading(
        entity_id=eid, source="test", raw_value=raw, norm_value=normalize(raw),
        ts_utc=T0, home_id=HOME,
    )


def _at(seconds: float) -> str:
    return (datetime.fromisoformat(T0) + timedelta(seconds=seconds)).isoformat()


async def _make():
    clock = Clock(fixed=T0)
    db = await init_db(":memory:")
    app = LiotApp(clock=clock, categories=CATS)
    transport = FakeNtfyTransport()
    svc = LiotService(app, db, clock=clock, transport=transport, topic=TOPIC, secret=SECRET)
    return svc, db, transport, clock


async def _reject_click(svc: LiotService, proposal: Proposal) -> str:
    """알림 버튼 '거절' 을 서명 링크로 누른다(거주자의 명시 응답)."""
    pid = proposal.proposal_id
    nonce = svc._nonce[pid]
    sig = sign_feedback(SECRET, pid, proposal.risk_tier, "reject", nonce)
    status, _ = await svc._route_feedback(
        f"GET /feedback?p={pid}&t={int(proposal.risk_tier)}&n={nonce}&sig={sig}&a=reject HTTP/1.1"
    )
    return status


def _comfort(entity: str = "light.living_room") -> Proposal:
    return Proposal(
        proposal_id=f"p_{uuid.uuid4().hex[:10]}",
        judgment_id="j1",
        home_id=HOME,
        risk_tier=RiskTier.BREATH,
        action=CandidateAction(capability="light", action="turn_off", category="light",
                               entity_id=entity, params={}),
        rationale="test",
        required_confirmation=RiskTier.BREATH.required_confirmation,
        perishable=False,
        expires_at=None,
        state="pending",
        created_at=T0,
    )


async def test_rejected_warning_situation_does_not_mute_next_episode() -> None:
    svc, db, transport, clock = await _make()
    try:
        first = await svc.handle_reading(_reading(DOOR, "on"))
        assert first is not None and first.proposal.situation_id is not None
        assert first.decision == "IMMEDIATE" and first.sent is True
        await svc.drain_phase2()

        # 거절: 그 상황의 사다리는 멈춘다(확인됨).
        assert await _reject_click(svc, first.proposal) == "200 OK"
        ladder = await svc.safety.get_ladder(first.proposal.situation_id)
        assert ladder is not None and ladder.acked is True
        stored = await svc.proposals.get(first.proposal.proposal_id)
        assert stored is not None and stored.state == "dismissed"

        # 상황이 닫히고, 30일 안에(3시간 뒤: dedup 2시간 창도 지난 뒤) 새 에피소드가 열린다.
        clock.set_fixed(_at(60))
        assert await svc.handle_reading(_reading(DOOR, "off")) is None
        clock.set_fixed(_at(3 * 3600))
        second = await svc.handle_reading(_reading(DOOR, "on"))
        assert second is not None
        assert second.proposal.situation_id != first.proposal.situation_id
        assert second.decision == "IMMEDIATE"
        assert second.sent is True
        assert len(transport.sent) == 3  # 첫 알림 + 해소 알림 + 두 번째 에피소드 첫 알림
        await svc.drain_phase2()

        # 상황 제안은 30일 거절 이력에 들어가지 않는다.
        assert await svc.proposals.recently_rejected_keys() == frozenset()
    finally:
        await db.close()


async def test_rejected_comfort_proposal_still_suppresses_same_key() -> None:
    svc, db, transport, clock = await _make()
    try:
        p1 = _comfort()
        r1 = await svc.dispatch(p1)
        assert r1.decision == "IMMEDIATE" and r1.sent is True
        await svc.proposals.apply_feedback(
            FeedbackEvent(proposal_id=p1.proposal_id, trace_id="fb", user_action="dismissed")
        )
        assert await svc.proposals.recently_rejected_keys() == frozenset({dedup_key(p1)})

        # dedup(2시간) 혼선을 없애고, 30일 안에서 같은 류가 침묵하는지 본다.
        svc._recent_notified.clear()
        clock.set_fixed(_at(29 * 24 * 3600))
        r2 = await svc.dispatch(_comfort())
        assert r2.decision == "SUPPRESS" and r2.sent is False

        # 30일이 지나면 풀린다.
        clock.set_fixed(_at(31 * 24 * 3600))
        r3 = await svc.dispatch(_comfort())
        assert r3.decision == "IMMEDIATE"
    finally:
        await db.close()


async def test_muted_warning_category_is_still_suppressed() -> None:
    svc, db, transport, clock = await _make()
    try:
        await svc.prefs.set("muted_categories", ["door"])
        res = await svc.handle_reading(_reading(DOOR, "on"))
        assert res is not None and res.proposal.situation_id is not None
        assert res.decision == "SUPPRESS" and res.sent is False
        assert transport.sent == []
        await svc.drain_phase2()
    finally:
        await db.close()
