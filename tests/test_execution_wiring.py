"""조작 배선(불변식 8) 검증 — LiotService + ExecutionPolicy + Executor.

축:
  - 비활성(기본): AUTO 대상이라도 실행 안 함(제안-만 유지).
  - AUTO: 비안전 ≤BREATH 는 dispatch 에서 즉시 실행.
  - CONFIRM(DUSK): dispatch 보류 → 승인 후 실행.
  - DOUBLE_CONFIRM(EMBER/안전): 1차 보류 → 2차 승인에서 실행.
  - undo: 자동 실행 후 서명된 되돌리기로 복원.
"""

from __future__ import annotations

import uuid

from liot.app import LiotApp
from liot.core.clock import Clock
from liot.core.contracts import CandidateAction, Proposal
from liot.core.risk import RiskTier
from liot.db.database import init_db
from liot.layers.l4_execution.executor import ExecutionPolicy, FakeExecutor
from liot.layers.l4_execution.notifier import sign_feedback
from liot.layers.l4_execution.ntfy_transport import FakeNtfyTransport
from liot.service import LiotService

DAY = "2026-06-02T12:00:00+00:00"
SECRET = b"test-secret-key"
CATS = {"light.living_room": "light", "lock.front_door": "lock"}


async def _service(*, enabled: bool, fail: bool = False):
    clock = Clock(fixed=DAY)
    db = await init_db(":memory:")
    app = LiotApp(clock=clock, categories=CATS)
    fx = FakeExecutor(fail=fail)
    svc = LiotService(
        app, db, clock=clock, transport=FakeNtfyTransport(), topic="liot-test",
        secret=SECRET, executor=fx, policy=ExecutionPolicy(enabled=enabled),
    )
    return svc, fx, db


def _proposal(cap: str, action: str, category: str, tier: RiskTier, entity: str) -> Proposal:
    return Proposal(
        proposal_id=f"p_{uuid.uuid4().hex[:10]}",
        judgment_id="j1",
        home_id="singleton",
        risk_tier=tier,
        action=CandidateAction(capability=cap, action=action, category=category,
                               entity_id=entity, params={}),
        rationale="test",
        required_confirmation=tier.required_confirmation,
        perishable=False,
        expires_at=None,
        state="pending",
        created_at=DAY,
    )


async def _approve(svc: LiotService, prop: Proposal, action: str = "accept") -> str:
    tier_int = int(prop.risk_tier)
    sig = sign_feedback(SECRET, prop.proposal_id, tier_int, action)
    line = f"GET /feedback?p={prop.proposal_id}&t={tier_int}&sig={sig}&a={action} HTTP/1.1"
    _status, body = await svc._route_feedback(line)
    return body


# ── 비활성: 제안-만 유지 ────────────────────────────────────────────────
async def test_disabled_does_not_execute():
    svc, fx, db = await _service(enabled=False)
    try:
        prop = _proposal("light", "turn_off", "light", RiskTier.BREATH, "light.living_room")
        res = await svc.dispatch(prop)
        assert res.executed is False
        assert fx.executed == []
    finally:
        await db.close()


# ── AUTO: 비안전 ≤BREATH 즉시 실행 ─────────────────────────────────────
async def test_auto_executes_comfort():
    svc, fx, db = await _service(enabled=True)
    try:
        prop = _proposal("light", "turn_off", "light", RiskTier.BREATH, "light.living_room")
        res = await svc.dispatch(prop)
        assert res.executed is True and res.sent is True
        assert len(fx.executed) == 1 and fx.executed[0].entity_id == "light.living_room"
    finally:
        await db.close()


# ── CONFIRM(DUSK): dispatch 보류 → 승인 후 실행 ────────────────────────
async def test_dusk_defers_until_approval():
    svc, fx, db = await _service(enabled=True)
    try:
        prop = _proposal("light", "turn_off", "light", RiskTier.DUSK, "light.living_room")
        res = await svc.dispatch(prop)
        assert res.executed is False and fx.executed == []  # 보류
        body = await _approve(svc, prop)
        assert "executed" in body and len(fx.executed) == 1
    finally:
        await db.close()


# ── DOUBLE_CONFIRM(안전 lock): 1차 보류 → 2차 실행 ─────────────────────
async def test_safety_double_confirm():
    svc, fx, db = await _service(enabled=True)
    try:
        prop = _proposal("lock", "lock", "lock", RiskTier.EMBER, "lock.front_door")
        res = await svc.dispatch(prop)
        assert res.executed is False  # 안전은 절대 자동 아님
        first = await _approve(svc, prop)
        assert "1/2" in first and fx.executed == []  # 1차는 보류
        second = await _approve(svc, prop)
        assert "2/2" in second and len(fx.executed) == 1
    finally:
        await db.close()


# ── undo: 자동 실행 후 서명된 되돌리기 ─────────────────────────────────
async def test_undo_after_auto():
    svc, fx, db = await _service(enabled=True)
    try:
        prop = _proposal("light", "turn_off", "light", RiskTier.BREATH, "light.living_room")
        await svc.dispatch(prop)
        assert len(fx.executed) == 1
        body = await _approve(svc, prop, action="undo")
        assert body == "undone" and len(fx.undone) == 1
    finally:
        await db.close()


# ── 위조 undo 서명 거부 ────────────────────────────────────────────────
async def test_undo_rejects_bad_signature():
    svc, fx, db = await _service(enabled=True)
    try:
        prop = _proposal("light", "turn_off", "light", RiskTier.BREATH, "light.living_room")
        await svc.dispatch(prop)
        line = f"GET /feedback?p={prop.proposal_id}&t=1&sig=deadbeef&a=undo HTTP/1.1"
        status, _ = await svc._route_feedback(line)
        assert status.startswith("403")
        assert fx.undone == []
    finally:
        await db.close()
