"""Phase 3 — CandidateEvaluator: 점수화·선택.

외기에 따른 환기/청정기 우선순위 변동, 거절 기록이 선택을 바꾸는지,
외부 degraded 시 보수적 무행동, safety_risk 가 LLM 값이 아니라 카탈로그 값으로
덮이는지를 고정한다.
"""

from __future__ import annotations

import pytest

from liot.core.contracts import (
    ActionCandidate,
    CandidateAction,
    CandidateSet,
    ExternalContext,
    make_noop_candidate,
)
from liot.layers.l2_context.evaluator import CandidateEvaluator
from liot.layers.l2_context.feedback_store import InMemoryFeedbackStore, candidate_key
from liot.layers.l4_execution.device_catalog import load_device_catalog

TS = "2026-01-01T00:00:00+00:00"


def _ca(cap, action, entity, category="comfort", params=None):
    return CandidateAction(
        capability=cap,
        action=action,
        params=params or {},
        entity_id=entity,
        category=category,
    )


def _cset(*cands):
    items = [ActionCandidate(candidate=c) for c in cands]
    items.append(ActionCandidate(candidate=make_noop_candidate()))
    return CandidateSet(
        trace_id="t", snapshot_hash="h", candidates=items, selected_index=len(items) - 1
    )


def test_external_aqi_bad_prefers_purifier():
    # 안전 위험도 격리(catalog=None) → 외기 로직만 검증.
    ev = CandidateEvaluator(catalog=None)
    cset = _cset(
        _ca("window", "open", "window.living"),
        _ca("purifier", "turn_on", "purifier.living"),
    )
    ext = ExternalContext(outdoor_aqi=180, fetched_at=TS)
    res = ev.evaluate(cset, external=ext)
    # 바깥이 나쁘면 환기 대신 공기청정기.
    assert res.selected_action().capability == "purifier"


def test_external_aqi_good_prefers_ventilation():
    ev = CandidateEvaluator(catalog=None)
    cset = _cset(
        _ca("window", "open", "window.living"),
        _ca("purifier", "turn_on", "purifier.living"),
    )
    ext = ExternalContext(outdoor_aqi=20, fetched_at=TS)
    res = ev.evaluate(cset, external=ext)
    # 바깥이 맑으면 환기가 최선.
    assert res.selected_action().capability == "window"


def test_rejection_history_changes_selection():
    ev = CandidateEvaluator(catalog=load_device_catalog())
    light = _ca("light", "turn_off", "light.living_room", "ambient")
    cset = _cset(light)
    # 이력 없으면 행동 선택.
    res0 = ev.evaluate(cset)
    assert not res0.selected_is_noop()
    assert res0.selected_action().capability == "light"
    # 최근 거절되면 선호 감점 → 무행동(침묵)으로 선택이 바뀐다.
    fb = InMemoryFeedbackStore(rejected_keys=frozenset({candidate_key(light)}))
    res1 = ev.evaluate(cset, feedback=fb)
    assert res1.selected_is_noop()


def test_degraded_external_selects_noop():
    ev = CandidateEvaluator(catalog=None)
    cset = _cset(_ca("light", "turn_off", "light.living_room"))
    ext = ExternalContext(fetched_at=TS, degraded=True)
    res = ev.evaluate(cset, external=ext)
    assert res.selected_is_noop()
    assert "보수" in res.rationale or "degraded" in res.rationale


def test_safety_risk_overridden_by_catalog():
    ev = CandidateEvaluator(catalog=load_device_catalog())
    # LLM 이 안전하다 주장(safety_risk=0)해도 valve.open 은 카탈로그상 EMBER.
    danger = ActionCandidate(
        candidate=_ca("valve", "open", "switch.gas_valve", "gas"),
        safety_risk=0.0,
    )
    cset = CandidateSet(
        trace_id="t",
        snapshot_hash="h",
        candidates=[danger, ActionCandidate(candidate=make_noop_candidate())],
        selected_index=1,
    )
    res = ev.evaluate(cset)
    scored_valve = next(a for a in res.scored if a.candidate.capability == "valve")
    # EMBER(3)/3 = 1.0 — LLM 이 제출한 0.0 을 무시하고 카탈로그 값으로 덮음.
    assert scored_valve.safety_risk == pytest.approx(1.0)
    # 위험해서 선택되지 않는다(무행동이 이긴다).
    assert res.selected_is_noop()


def test_noop_score_is_baseline():
    ev = CandidateEvaluator(catalog=None)
    cset = _cset(_ca("light", "turn_off", "light.x"))
    res = ev.evaluate(cset)
    noop = next(a for a in res.scored if a.candidate.capability == "noop")
    assert noop.final_score == pytest.approx(0.35)
    assert noop.safety_risk == 0.0
