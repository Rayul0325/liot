"""불변식 2′ — LLM 은 안전 floor 를 낮출 수 없다 (candidate-origin 무관 안전).

LLM 의 역할이 거부권에서 후보 생성·설명으로 이동해도, 안전의 권위는
결정론(가드레일/카탈로그/화이트리스트)에 남는다:
  1) SafetyGate(=Guardrail)는 후보의 출처(rule/llm)를 보지 않는다 —
     같은 행동이면 같은 floor. LLM 표식으로 floor 를 낮출 방법이 없다.
  2) LLM 이 금지 행동(여는 방향·안전 카테고리·화이트리스트 밖)을 후보로
     내면 후보 집합에서 폐기되고, 만에 하나 통과해도 실행 화이트리스트가
     한 번 더 막는다(방어심층).
  3) LLM 후보가 선택돼도 기존 게이트(Gate1+Gate2+BREATH 상한)를 그대로
     지난다 — 최종 위험도는 결정론 floor 이상.
"""

from __future__ import annotations

import pytest

from liot.core.clock import Clock
from liot.core.contracts import CandidateAction
from liot.core.risk import RiskTier
from liot.layers.l2_context.candidate_llm import FakeCandidateLLM
from liot.layers.l2_context.proactive import ProactiveDecision, ProactiveGenerator
from liot.layers.l2_context.proactive_rules import load_proactive_ruleset
from liot.layers.l3_judgment.guardrail import Guardrail, SafetyGate
from liot.layers.l4_execution.device_catalog import load_device_catalog
from liot.layers.l4_execution.executor import (
    ExecDecision,
    ExecutionDenied,
    ExecutionPolicy,
    FakeExecutor,
)

DAY = "2026-06-02T12:00:00+00:00"
CLOCK = Clock(fixed=DAY)
CATS = {
    "binary_sensor.living_motion": "motion",
    "light.living_room": "light",
    "light.bedroom": "light",
}
BASE_YAML = """
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
CUR = {
    "binary_sensor.living_motion": False,
    "light.living_room": True,
    "light.bedroom": True,
}


def _gen(tmp_path, llm=None) -> ProactiveGenerator:
    p = tmp_path / "t.yaml"
    p.write_text(BASE_YAML, encoding="utf-8")
    return ProactiveGenerator(
        ruleset=load_proactive_ruleset(p),
        catalog=load_device_catalog(),
        categories=CATS,
        clock=CLOCK,
        llm_candidate_backend=llm,
    )


# ==========================================================================
# 1) SafetyGate 는 출처를 보지 않는다
# ==========================================================================
def test_safety_gate_is_guardrail_alias() -> None:
    assert SafetyGate is Guardrail


def test_gate_floor_ignores_candidate_origin() -> None:
    gate = SafetyGate()
    base = dict(capability="valve", action="open", entity_id="switch.gas_valve", category="gas")
    from_rule = CandidateAction(**base, params={"__origin__": "proactive:rule:x@y"})
    from_llm = CandidateAction(**base, params={"__origin__": "proactive:llm"})
    r_rule = gate.evaluate(from_rule)
    r_llm = gate.evaluate(from_llm)
    # 출처 표식이 무엇이든 floor 동일 — LLM 표식으로 낮출 방법이 없다.
    assert r_rule.floor_tier == r_llm.floor_tier == RiskTier.EMBER
    assert r_rule.safety_critical and r_llm.safety_critical


# ==========================================================================
# 2) 금지 행동은 후보 폐기 + 실행 화이트리스트 이중 차단
# ==========================================================================
# 능동(proactive) 금지 후보 — LLM 이 발의해도 후보 집합에서 폐기돼야 한다.
FORBIDDEN_PROACTIVE = [
    # (capability, action, entity, category) — 여는 방향/안전 카테고리/화이트리스트 밖
    ("valve", "open", "switch.gas_valve", "gas"),
    ("lock", "unlock", "lock.front_door", "lock"),
    ("cover", "open", "cover.living", "curtain"),
    ("oven", "turn_on", "switch.oven", "oven"),
]

# 실행 계층에서도 원천 차단되는 부분집합. cover.open 은 여기 없다 — 능동
# 발의만 금지될 뿐, 사용자가 명시 승인하면 실행 가능(카탈로그가 DUSK 로
# 승격해 CONFIRM 을 요구). 남는 셋은 어떤 경로로도 자동/실행 불가.
FORBIDDEN_EXEC = [
    ("valve", "open", "switch.gas_valve", "gas"),
    ("lock", "unlock", "lock.front_door", "lock"),
    ("oven", "turn_on", "switch.oven", "oven"),
]


def test_llm_forbidden_candidates_discarded(tmp_path) -> None:
    llm = FakeCandidateLLM(
        [
            {
                "capability": cap,
                "action": act,
                "params": {},
                "entity_id": ent,
                "category": cat,
            }
            for cap, act, ent, cat in FORBIDDEN_PROACTIVE
        ]
    )
    gen = _gen(tmp_path, llm=llm)
    cset = gen.candidate_set_for(CUR, gen.propose(CUR))
    caps = {(a.candidate.capability, a.candidate.action) for a in cset.candidates}
    for cap, act, _, _ in FORBIDDEN_PROACTIVE:
        assert (cap, act) not in caps, f"금지 후보 {cap}.{act} 가 살아남음"
    # 규칙 후보와 무행동은 생존.
    assert ("light", "turn_off") in caps
    assert any(a.candidate.capability == "noop" for a in cset.candidates)


async def test_forbidden_actions_blocked_at_execution_layer() -> None:
    # 방어심층: 후보 게이트를 우회해도 실행 계층이 막는다.
    policy = ExecutionPolicy(enabled=True)
    executor = FakeExecutor()
    for cap, act, ent, cat in FORBIDDEN_EXEC:
        action = CandidateAction(
            capability=cap, action=act, params={}, entity_id=ent, category=cat
        )
        # 정책: 어떤 티어에서도 AUTO 불가(안전) 또는 NOTIFY_ONLY(비화이트리스트).
        for tier in RiskTier:
            assert policy.decide(action, tier) != ExecDecision.AUTO
        # 실행기: 화이트리스트 재검에서 거부.
        with pytest.raises(ExecutionDenied):
            await executor.execute(action)


# ==========================================================================
# 3) 유효한 LLM 후보도 결정론 floor 이상으로만 판정된다
# ==========================================================================
def test_valid_llm_candidate_joins_pool_with_llm_origin(tmp_path) -> None:
    llm = FakeCandidateLLM(
        [
            {
                "capability": "light",
                "action": "turn_off",
                "params": {},
                "entity_id": "light.bedroom",
                "category": "light",
            }
        ]
    )
    gen = _gen(tmp_path, llm=llm)
    cset = gen.candidate_set_for(CUR, gen.propose(CUR))
    llm_cands = [a for a in cset.candidates if a.origin == "llm"]
    assert len(llm_cands) == 1
    # LLM 후보에도 출처가 박힌다(감사 추적).
    assert llm_cands[0].candidate.params.get("__origin__") == "proactive:llm"
    # 선택은 여전히 propose 승자(규칙 후보) — LLM 후보가 선택을 강탈하지 못한다.
    assert cset.selected_action().entity_id == "light.living_room"


def test_llm_candidate_gate_floor_unchanged(tmp_path) -> None:
    # 선택된 행동이 LLM 산이어도 SafetyGate floor 는 카테고리 결정론 그대로.
    gate = SafetyGate()
    llm_light = CandidateAction(
        capability="light",
        action="turn_off",
        params={"__origin__": "proactive:llm"},
        entity_id="light.bedroom",
        category="light",
    )
    assert gate.evaluate(llm_light).floor_tier == RiskTier.AMBIENT  # light 기준선
