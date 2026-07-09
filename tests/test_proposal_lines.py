"""Phase 1 — 2갈래 제안 라인 + 병합 검증.

- 병합셋에 NoOp + 양 라인 후보 존재(같은 행동 동의 시 origin 합쳐짐).
- 모든 후보는 validate_proactive_candidate 통과(비선택 후보도).
- 금지 후보는 fail-closed 폐기(NoOp 생존).
- 동일 입력 재현(같은 snapshot_hash + 같은 행동).
- 임베딩은 line_confidence(선호)만 바꾸고 안전은 안 바꾼다 + 풀 해시에 반영.
- 맥락 미충족(재실) → NoOp 만.
"""

from __future__ import annotations

from liot.core.contracts import (
    ActionCandidate,
    CandidateAction,
    CandidateSet,
    compute_snapshot_hash,
    is_noop,
    make_noop_candidate,
)
from liot.layers.l2_context.evaluator import CandidateEvaluator
from liot.layers.l2_context.feature_vector import FakePreferenceEmbedder, cell_key
from liot.layers.l2_context.proactive import validate_proactive_candidate
from liot.layers.l2_context.proposal_lines import (
    LLMProposalLine,
    MLProposalLine,
    ProposalLine,
    augment_candidate_set,
    merge_candidate_set,
)
from liot.layers.l4_execution.device_catalog import load_device_catalog

CATALOG = load_device_catalog()
ACTS = CATALOG.available_actions()
FACTS = {"time_bucket": "evening", "occupancy": "vacant"}
CUR = {"light.living_room": True}


def _merge(lines, embedder=None, cur=None, facts=None):
    return merge_candidate_set(
        trace_id="t1",
        facts=FACTS if facts is None else facts,
        cur=CUR if cur is None else cur,
        lines=lines,
        catalog=CATALOG,
        embedder=embedder,
        available_actions=ACTS,
    )


def _key(c: ActionCandidate):
    return (c.candidate.capability, c.candidate.action, c.candidate.entity_id)


def test_lines_satisfy_protocol():
    assert isinstance(MLProposalLine(), ProposalLine)
    assert isinstance(LLMProposalLine(), ProposalLine)


def test_merged_has_noop_and_both_lines():
    cs = _merge([MLProposalLine(), LLMProposalLine()], FakePreferenceEmbedder())
    assert any(is_noop(c.candidate) for c in cs.candidates)
    off = [
        c
        for c in cs.candidates
        if c.candidate.capability == "light" and c.candidate.action == "turn_off"
    ]
    assert len(off) == 1  # 두 라인이 같은 행동 → 하나로 병합
    assert "line_a:ml" in off[0].origin and "line_b:llm" in off[0].origin


def test_all_candidates_pass_validate():
    cs = _merge([MLProposalLine(), LLMProposalLine()], FakePreferenceEmbedder())
    for c in cs.candidates:
        if is_noop(c.candidate):
            continue
        assert validate_proactive_candidate(c.candidate, CATALOG) is None


def test_forbidden_candidate_discarded():
    class ForbiddenLine:
        name = "line_x:bad"

        def propose(self, facts, cur, *, embedder=None, available_actions=None):
            return [
                ActionCandidate(
                    candidate=CandidateAction(
                        capability="valve",
                        action="open",
                        params={},
                        entity_id="valve.main",
                        category="water_main",
                    ),
                    origin=self.name,
                    line_confidence=0.99,
                )
            ]

    cs = _merge([ForbiddenLine()])
    # 금지 후보는 폐기 → NoOp 만 생존
    assert len(cs.candidates) == 1
    assert is_noop(cs.candidates[0].candidate)


def test_reproducible():
    cs1 = _merge([MLProposalLine(), LLMProposalLine()], FakePreferenceEmbedder())
    cs2 = _merge([MLProposalLine(), LLMProposalLine()], FakePreferenceEmbedder())
    assert cs1.snapshot_hash == cs2.snapshot_hash
    assert [_key(c) for c in cs1.candidates] == [_key(c) for c in cs2.candidates]


def test_embedding_changes_confidence_and_hash_not_safety():
    off_ca = CandidateAction(
        capability="light",
        action="turn_off",
        params={},
        entity_id="light.living_room",
        category="light",
    )
    key = cell_key("evening", "vacant", off_ca)
    cs_hi = _merge([MLProposalLine()], FakePreferenceEmbedder({key: 1.0}))
    cs_lo = _merge([MLProposalLine()], FakePreferenceEmbedder({key: 0.0}))
    off_hi = [c for c in cs_hi.candidates if not is_noop(c.candidate)][0]
    off_lo = [c for c in cs_lo.candidates if not is_noop(c.candidate)][0]
    # 선호 임베딩이 높을수록 라인 자신감 ↑
    assert off_hi.line_confidence > off_lo.line_confidence
    # 임베딩은 안전을 못 바꾼다(여전히 validate 통과)
    assert validate_proactive_candidate(off_hi.candidate, CATALOG) is None
    # 임베딩이 풀 우선순위를 바꾸므로 해시에 반영(불변식 4 커버리지)
    assert cs_hi.snapshot_hash != cs_lo.snapshot_hash


def test_no_context_only_noop():
    cs = _merge(
        [MLProposalLine(), LLMProposalLine()],
        FakePreferenceEmbedder(),
        facts={"time_bucket": "evening", "occupancy": "present"},
    )
    assert len(cs.candidates) == 1
    assert is_noop(cs.candidates[0].candidate)


# ── Phase 2a: augment(규칙 풀 + 라인) + 아비터(evaluator) ──────────────


def _base_rule_set():
    """규칙 풀 모사: light.turn_off(rule) + NoOp."""
    rule = ActionCandidate(
        candidate=CandidateAction(
            capability="light",
            action="turn_off",
            params={},
            entity_id="light.living_room",
            category="light",
        ),
        origin="rule:vacant-light-off",
    )
    noop = ActionCandidate(candidate=make_noop_candidate(), origin="noop")
    return CandidateSet(
        trace_id="t",
        snapshot_hash=compute_snapshot_hash({"a": 1}),
        candidates=[rule, noop],
        selected_index=1,
    )


def _augment(base):
    return augment_candidate_set(
        base,
        [MLProposalLine(), LLMProposalLine()],
        facts=FACTS,
        cur=CUR,
        catalog=CATALOG,
        embedder=FakePreferenceEmbedder(),
        available_actions=ACTS,
    )


def test_augment_merges_origins_and_preserves_hash_noop():
    base = _base_rule_set()
    aug = _augment(base)
    assert aug.snapshot_hash == base.snapshot_hash  # 맥락 동일 → 해시 유지
    assert sum(1 for c in aug.candidates if is_noop(c.candidate)) == 1
    off = [
        c
        for c in aug.candidates
        if c.candidate.capability == "light" and c.candidate.action == "turn_off"
    ][0]
    # 규칙 + 두 라인이 같은 행동에 동의 → origin 합쳐짐
    assert "rule:vacant-light-off" in off.origin
    assert "line_a:ml" in off.origin and "line_b:llm" in off.origin


def test_arbiter_selects_deterministically_safety_from_catalog():
    aug = _augment(_base_rule_set())
    ev = CandidateEvaluator(catalog=CATALOG).evaluate(aug)
    sel = ev.selected_action()
    assert (sel.capability, sel.action) == ("light", "turn_off")
    # 안전위험은 catalog 값(light=ambient tier 0 → 0.0), 라인/임베딩과 무관
    scored_off = [a for a in ev.scored if a.candidate.capability == "light"][0]
    assert scored_off.safety_risk == 0.0
