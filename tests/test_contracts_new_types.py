"""Phase 1 — 다중 후보 파이프라인 신규 contract 타입.

기존 타입은 건드리지 않고 추가된 ActionCandidate/CandidateSet/ExternalContext/
EvaluationResult + NoOp 헬퍼의 핵심 불변(무행동 후보 강제·재현성)을 고정한다.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from liot.core.contracts import (
    ActionCandidate,
    CandidateAction,
    CandidateSet,
    EvaluationResult,
    ExternalContext,
    compute_snapshot_hash,
    is_noop,
    make_noop_candidate,
)


def _ac(cap: str, action: str, entity: str, category: str) -> ActionCandidate:
    return ActionCandidate(
        candidate=CandidateAction(
            capability=cap, action=action, entity_id=entity, category=category
        )
    )


def test_make_noop_candidate_is_noop() -> None:
    noop = make_noop_candidate()
    assert is_noop(noop)
    assert noop.capability == "noop"
    assert not is_noop(
        CandidateAction(
            capability="light", action="turn_off", entity_id="light.x", category="comfort"
        )
    )


def test_candidate_set_requires_noop() -> None:
    # 무행동 후보가 없으면 거부.
    with pytest.raises(ValidationError):
        CandidateSet(
            trace_id="t1",
            snapshot_hash="h",
            candidates=[_ac("light", "turn_off", "light.x", "comfort")],
        )


def test_candidate_set_with_noop_ok_and_selection() -> None:
    cs = CandidateSet(
        trace_id="t1",
        snapshot_hash="h",
        candidates=[
            _ac("light", "turn_off", "light.x", "comfort"),
            ActionCandidate(candidate=make_noop_candidate()),
        ],
        selected_index=1,
    )
    assert cs.selected_is_noop()
    assert cs.selected_action().capability == "noop"
    # 선택을 행동 후보로 바꾸면 침묵이 아니다.
    cs2 = cs.model_copy(update={"selected_index": 0})
    assert not cs2.selected_is_noop()
    assert cs2.selected_action().capability == "light"


def test_candidate_set_selected_index_bounds() -> None:
    with pytest.raises(ValidationError):
        CandidateSet(
            trace_id="t1",
            snapshot_hash="h",
            candidates=[ActionCandidate(candidate=make_noop_candidate())],
            selected_index=5,
        )


def test_candidate_set_roundtrip_serialization() -> None:
    cs = CandidateSet(
        trace_id="t1",
        snapshot_hash="h",
        candidates=[
            _ac("fan", "turn_on", "fan.air", "comfort"),
            ActionCandidate(candidate=make_noop_candidate()),
        ],
        selected_index=0,
    )
    again = CandidateSet.model_validate(cs.model_dump())
    assert again == cs


def test_external_context_merge_changes_snapshot_hash() -> None:
    base = {"a": 1, "b": 2}
    clean = ExternalContext(outdoor_temp_c=20.0, outdoor_aqi=30, fetched_at="2026-01-01T00:00:00+00:00")
    dirty = ExternalContext(outdoor_temp_c=-3.0, outdoor_aqi=180, fetched_at="2026-01-01T00:00:00+00:00")
    h_clean = compute_snapshot_hash({**base, **clean.to_structured()})
    h_dirty = compute_snapshot_hash({**base, **dirty.to_structured()})
    # 외기가 다르면 snapshot_hash 가 달라진다(후보 우선순위 변동의 근거).
    assert h_clean != h_dirty


def test_external_context_to_structured_excludes_timestamp() -> None:
    # fetched_at 은 structured 에 포함되지 않아야 재현성이 유지된다.
    a = ExternalContext(outdoor_temp_c=20.0, outdoor_aqi=30, fetched_at="2026-01-01T00:00:00+00:00")
    b = ExternalContext(outdoor_temp_c=20.0, outdoor_aqi=30, fetched_at="2026-12-31T23:59:59+00:00")
    assert a.to_structured() == b.to_structured()
    assert "fetched_at" not in a.to_structured()


def test_evaluation_result_basic() -> None:
    er = EvaluationResult(
        scored=[ActionCandidate(candidate=make_noop_candidate())],
        selected_index=0,
        rationale="아무 행동도 적절하지 않음",
    )
    assert er.selected_index == 0
    assert er.scored[0].candidate.capability == "noop"
