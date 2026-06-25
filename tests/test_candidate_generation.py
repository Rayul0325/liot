"""Phase 2 — CandidateGenerator: 단일 → 다중 + 무행동.

generate()/candidate_set_for() 가 여러 후보를 만들고, 무행동을 항상 포함하며,
LLM 후보가 금지/화이트리스트 밖이면 폐기되고 무행동이 생존하는지(fail-closed),
같은 입력이 같은 후보 집합을 내는지(재현성, 불변식 4), 선택이 propose 승자와
일치하는지(동작 보존)를 고정한다.
"""

from __future__ import annotations

from liot.core.clock import Clock
from liot.core.contracts import is_noop
from liot.layers.l2_context.candidate_llm import FakeCandidateLLM
from liot.layers.l2_context.proactive import ProactiveDecision, ProactiveGenerator
from liot.layers.l2_context.proactive_rules import load_proactive_ruleset
from liot.layers.l4_execution.device_catalog import load_device_catalog

DAY = "2026-06-02T12:00:00+00:00"  # time_bucket 'day'
CLOCK = Clock(fixed=DAY)
CATS = {
    "binary_sensor.living_motion": "motion",
    "light.living_room": "light",
    "light.bedroom": "light",
}

TWO_RULES = """
version: "1.0.0"
name: t
limits: {cooldown_minutes: 60, max_per_bucket: 5, max_per_day: 10}
rules:
  - id: vacant-living-off
    match: {occupancy: vacant}
    state: [{entity: light.living_room, op: eq, value: true}]
    propose: {target: light.living_room, capability: light, action: turn_off}
    ttl_minutes: 15
  - id: vacant-bedroom-off
    match: {occupancy: vacant}
    state: [{entity: light.bedroom, op: eq, value: true}]
    propose: {target: light.bedroom, capability: light, action: turn_off}
    ttl_minutes: 15
"""

# 둘 다 켜져 있고 비어 있음(motion false) → 두 규칙 모두 매칭.
CUR_BOTH_ON = {
    "binary_sensor.living_motion": False,
    "light.living_room": True,
    "light.bedroom": True,
}
# 아무 규칙도 매칭 안 되는 상태(불 꺼짐).
CUR_NONE = {
    "binary_sensor.living_motion": False,
    "light.living_room": False,
    "light.bedroom": False,
}


def _gen(tmp_path, *, text=TWO_RULES, llm=None, state=None):
    p = tmp_path / "t.yaml"
    p.write_text(text, encoding="utf-8")
    return ProactiveGenerator(
        ruleset=load_proactive_ruleset(p),
        catalog=load_device_catalog(),
        categories=CATS,
        clock=CLOCK,
        state=state,
        llm_candidate_backend=llm,
    )


def _actions(cset):
    return [
        (c.candidate.capability, c.candidate.action, c.candidate.entity_id)
        for c in cset.candidates
    ]


def test_multiple_candidates_plus_noop(tmp_path):
    cset = _gen(tmp_path).generate(CUR_BOTH_ON)
    assert len(cset.candidates) == 3  # 규칙 후보 2 + 무행동 1
    assert sum(1 for c in cset.candidates if is_noop(c.candidate)) == 1
    caps = _actions(cset)
    assert ("light", "turn_off", "light.living_room") in caps
    assert ("light", "turn_off", "light.bedroom") in caps


def test_noop_always_present_even_no_match(tmp_path):
    cset = _gen(tmp_path).generate(CUR_NONE)
    assert len(cset.candidates) == 1
    assert cset.selected_is_noop()  # 침묵


def test_selection_matches_propose_winner(tmp_path):
    winner = _gen(tmp_path).propose(CUR_BOTH_ON).candidate
    assert winner is not None
    cset = _gen(tmp_path).generate(CUR_BOTH_ON)
    sel = cset.selected_action()
    assert (sel.capability, sel.action, sel.entity_id) == (
        winner.capability,
        winner.action,
        winner.entity_id,
    )
    assert not cset.selected_is_noop()


def test_llm_forbidden_candidate_discarded(tmp_path):
    # LLM 이 안전(가스 밸브 열기)을 제안해도 폐기되고 무행동은 생존.
    llm = FakeCandidateLLM(
        [
            {
                "capability": "valve",
                "action": "open",
                "params": {},
                "entity_id": "switch.gas_valve",
                "category": "gas",
            }
        ]
    )
    cset = _gen(tmp_path, llm=llm).generate(CUR_NONE)
    assert ("valve", "open", "switch.gas_valve") not in _actions(cset)
    assert any(is_noop(c.candidate) for c in cset.candidates)


def test_llm_valid_candidate_included(tmp_path):
    llm = FakeCandidateLLM(
        [
            {
                "capability": "light",
                "action": "turn_off",
                "params": {},
                "entity_id": "light.kitchen",
                "category": "light",
            }
        ]
    )
    cset = _gen(tmp_path, llm=llm).generate(CUR_NONE)
    assert ("light", "turn_off", "light.kitchen") in _actions(cset)
    llm_c = next(c for c in cset.candidates if c.candidate.entity_id == "light.kitchen")
    assert llm_c.origin == "llm"
    # __origin__ 이 주입돼 출처 추적이 보장된다.
    assert llm_c.candidate.params.get("__origin__") == "proactive:llm"


def test_llm_broken_candidate_failclosed(tmp_path):
    # 구조가 깨진 후보(필수 필드 없음)는 조용히 폐기(fail-closed).
    llm = FakeCandidateLLM([{"capability": "light"}, {"nonsense": 1}])
    cset = _gen(tmp_path, llm=llm).generate(CUR_NONE)
    assert not any(c.origin == "llm" for c in cset.candidates)
    assert any(is_noop(c.candidate) for c in cset.candidates)


def test_generate_reproducible(tmp_path):
    # 같은 입력 → 같은 후보집합(불변식 4). 신선한 생성기 둘로 비교(상태 독립).
    a = _gen(tmp_path).generate(CUR_BOTH_ON)
    b = _gen(tmp_path).generate(CUR_BOTH_ON)
    assert a.snapshot_hash == b.snapshot_hash
    assert _actions(a) == _actions(b)
    assert a.selected_index == b.selected_index


def test_candidate_set_for_is_pure(tmp_path):
    # candidate_set_for 는 쿨다운/예산 상태를 바꾸지 않는다(기록은 propose 만).
    g = _gen(tmp_path)
    none_decision = ProactiveDecision(
        candidate=None,
        origin="proactive:none",
        ttl_minutes=0,
        matched_rule_ids=(),
        suppressed_by="no_match",
    )
    g.candidate_set_for(CUR_BOTH_ON, none_decision)
    g.candidate_set_for(CUR_BOTH_ON, none_decision)
    # 두 번 호출해도 예산 미차감 → 이후 propose 가 정상 발의.
    assert g.propose(CUR_BOTH_ON).candidate is not None
