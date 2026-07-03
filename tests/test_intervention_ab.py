"""개입 정책 조건 A/B — "동일 선택, 개입만 차등" + 개입 하한.

연구 실험 설계의 핵심 제약을 고정한다:
  1) 조건 A/B 에서 L2 의 후보 선택은 완전히 동일하다(실험 오염 방지).
  2) 갈리는 것은 required_confirmation(개입 방식)뿐이다.
  3) 어떤 개입 정책도 티어 기준선보다 약한 확인을 못 낸다(개입 하한) —
     안전 임계(EMBER)는 조건과 무관하게 double.
"""

from __future__ import annotations

import pytest

from liot.app import LiotApp
from liot.core.clock import Clock
from liot.core.contracts import CandidateAction, Judgment, SensorReading
from liot.core.invariants import InvariantError, assert_execution_gated
from liot.core.risk import RiskTier
from liot.layers.l1_sensing.normalizer import normalize
from liot.layers.l2_context.proactive import ProactiveGenerator
from liot.layers.l2_context.proactive_rules import load_proactive_ruleset
from liot.layers.l4_execution.device_catalog import load_device_catalog
from liot.layers.l4_execution.intervention import (
    CONFIRMATION_ORDER,
    TieredIntervention,
    UniformIntervention,
    build_intervention,
)
from liot.layers.l4_execution.risk_router import RiskRouter

DAY = "2026-06-02T12:00:00+00:00"
CATS = {
    "binary_sensor.living_motion": "motion",
    "light.living_room": "light",
    "binary_sensor.kitchen_gas": "gas",
}
COMFORT_YAML = """
version: "1.0.0"
name: t
limits: {cooldown_minutes: 60, max_per_bucket: 5, max_per_day: 10}
rules:
  - id: vacant-light-off
    match: {occupancy: vacant}
    state: [{entity: light.living_room, op: eq, value: true}]
    propose: {target: light.living_room, capability: light, action: turn_off}
    ttl_minutes: 15
"""


def _reading(eid: str, raw: object) -> SensorReading:
    return SensorReading(
        entity_id=eid,
        source="test",
        raw_value=raw,
        norm_value=normalize(raw),
        ts_utc=DAY,
        home_id="singleton",
    )


def _app(tmp_path, condition: str) -> LiotApp:
    clock = Clock(fixed=DAY)
    p = tmp_path / "t.yaml"
    p.write_text(COMFORT_YAML, encoding="utf-8")
    gen = ProactiveGenerator(
        ruleset=load_proactive_ruleset(p),
        catalog=load_device_catalog(),
        categories=CATS,
        clock=clock,
    )
    return LiotApp(clock=clock, categories=CATS, proactive=gen, condition=condition)


async def _proactive_proposal(app: LiotApp):
    for eid, raw in [
        ("binary_sensor.living_motion", "off"),
        ("light.living_room", "on"),
    ]:
        await app.process_reading(_reading(eid, raw), force_snapshot=True)
    return await app.tick()


# ==========================================================================
# 1+2) 동일 선택, 개입만 차등
# ==========================================================================
async def test_same_selection_different_intervention(tmp_path) -> None:
    p_a = await _proactive_proposal(_app(tmp_path, "A"))
    p_b = await _proactive_proposal(_app(tmp_path, "B"))
    assert p_a is not None and p_b is not None

    # 선택된 행동은 완전히 동일(후보 생성·평가·선택은 condition 을 안 본다).
    for attr in ("capability", "action", "entity_id", "category"):
        assert getattr(p_a.action, attr) == getattr(p_b.action, attr)
    assert p_a.risk_tier == p_b.risk_tier  # 위험도 판정도 동일

    # 개입만 다르다: B=티어 차등(경량), A=단일 승인(explicit 로 상향).
    assert p_b.required_confirmation in ("none", "light")
    assert p_a.required_confirmation == "explicit"


async def test_safety_path_identical_across_conditions(tmp_path) -> None:
    # 안전 임계(가스→EMBER)는 조건 A/B 에서 완전히 동일해야 한다.
    results = {}
    for cond in ("A", "B"):
        app = _app(tmp_path, cond)
        p = await app.process_reading(
            _reading("binary_sensor.kitchen_gas", "on"), force_snapshot=True
        )
        assert p is not None
        results[cond] = p
    assert results["A"].risk_tier == results["B"].risk_tier == RiskTier.EMBER
    assert (
        results["A"].required_confirmation
        == results["B"].required_confirmation
        == "double"
    )


# ==========================================================================
# 3) 개입 하한
# ==========================================================================
def test_uniform_never_weakens_below_baseline() -> None:
    uni = UniformIntervention()
    tiered = TieredIntervention()
    for tier in RiskTier:
        base = tier.required_confirmation
        assert tiered.confirmation_for(tier) == base
        got = uni.confirmation_for(tier)
        assert CONFIRMATION_ORDER[got] >= CONFIRMATION_ORDER[base]
    # EMBER 는 단일 승인 조건에서도 double 유지.
    assert uni.confirmation_for(RiskTier.EMBER) == "double"


def test_router_rejects_weakening_policy() -> None:
    class _WeakPolicy:
        condition = "X"

        def confirmation_for(self, tier):  # 기준선 무시 — 항상 최약 확인
            return "none"

    router = RiskRouter(intervention=_WeakPolicy())
    judgment = Judgment(
        judgment_id="j1",
        trace_id="t1",
        home_id="singleton",
        snapshot_hash="h1",
        action=CandidateAction(
            capability="valve", action="close", params={}, entity_id="valve.gas", category="gas"
        ),
        risk_tier=RiskTier.EMBER,
        confidence=1.0,
        rationale="test",
        gate1_verdict="gate1:allow floor=EMBER (test)",
        bundle_id="b1",
        model_id="m1",
    )
    with pytest.raises(InvariantError):
        router.route(judgment)


def test_build_intervention_and_static_gate() -> None:
    assert build_intervention("a").condition == "A"
    assert build_intervention("B").condition == "B"
    assert build_intervention(None).condition == "B"  # 기본 = 현행(차등)
    with pytest.raises(ValueError):
        build_intervention("C")
    # 정적 불변식 게이트(항목 4: 개입 하한)도 통과해야 한다.
    assert_execution_gated()
