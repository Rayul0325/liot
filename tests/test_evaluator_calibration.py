"""고르기 점수 보정(2026-10-19) — 위험도 감점은 DUSK 이상만, 규칙이 선언한 기대 효과(benefit).

고정 수치(리터럴):
  - 권유형(advice, BREATH, benefit 0.7, 에너지 기본 0.3, 방해 0.1) × 중립 선호 0.5
      = 0.40·0.7 + 0.30·0.5 − 0.10·0.3 − 0.10·0.1 − 0 = 0.39  > 무행동 0.35
  - 같은 후보 × 선호 0.35 = 0.345 < 0.35 → 무행동
  - 한 번 거절(선호 0.1) = 0.27 → 무행동
  - 위험도 감점(가중 0.5 × max(0, 단계−1)/2): AMBIENT·BREATH 0, DUSK 0.25, EMBER 0.5
  - 공기질은 PM2.5 한국 등급(좋음 ≤15 · 나쁨 ≥36) 하나로 본다. aqi 만 오면 pm25 를 유도한다.
"""

from __future__ import annotations

import pytest

from liot.core.contracts import (
    ActionCandidate,
    CandidateAction,
    CandidateSet,
    ExternalContext,
    make_noop_candidate,
    pm25_from_aqi,
)
from liot.layers.l2_context.evaluator import CandidateEvaluator
from liot.layers.l4_execution.device_catalog import DeviceCatalog

TS = "2026-06-02T00:00:00+00:00"

CATALOG = DeviceCatalog(
    {
        "capabilities": {
            "advice": {"ventilate": {"tier": "breath"}},
            "light": {"turn_on": {"tier": "ambient"}},
            "cover": {"open": {"tier": "dusk"}, "close": {"tier": "breath"}},
            "valve": {"open": {"tier": "ember"}},
        }
    }
)


class _Pref:
    """모든 행동 후보에 같은 선호 신호를 주는 가짜 FeedbackStore."""

    def __init__(self, value: float) -> None:
        self.value = value

    def preference_signal(self, candidate: CandidateAction) -> float:
        return self.value


def _ca(cap: str, action: str, entity: str, category: str = "info") -> CandidateAction:
    return CandidateAction(
        capability=cap, action=action, params={}, entity_id=entity, category=category
    )


ADVICE = _ca("advice", "ventilate", "advice.home")


def _cset(*items: ActionCandidate) -> CandidateSet:
    cands = [*items, ActionCandidate(candidate=make_noop_candidate())]
    return CandidateSet(
        trace_id="t", snapshot_hash="h", candidates=cands, selected_index=len(cands) - 1
    )


def _score(
    ac: ActionCandidate,
    pref: float,
    *,
    catalog: DeviceCatalog | None = CATALOG,
    external: ExternalContext | None = None,
) -> tuple[float, bool]:
    res = CandidateEvaluator(catalog=catalog).evaluate(
        _cset(ac), external=external, feedback=_Pref(pref)
    )
    return res.scored[0].final_score, res.selected_is_noop()


# ── 선언 기대 효과 + 중립 선호 ────────────────────────────────────────────────


def test_advice_with_benefit_beats_noop_at_neutral_preference() -> None:
    score, noop = _score(ActionCandidate(candidate=ADVICE, declared_benefit=0.7), 0.5)
    assert score == 0.39
    assert noop is False


def test_advice_loses_to_noop_at_preference_035() -> None:
    score, noop = _score(ActionCandidate(candidate=ADVICE, declared_benefit=0.7), 0.35)
    assert score == 0.345
    assert noop is True


def test_advice_loses_after_one_rejection() -> None:
    score, noop = _score(ActionCandidate(candidate=ADVICE, declared_benefit=0.7), 0.1)
    assert score == 0.27
    assert noop is True


def test_benefit_replaces_default_effect_and_skips_external_bonus() -> None:
    # 환기형(cover.open 은 DUSK 라 감점이 섞이므로 catalog=None 으로 효과만 본다).
    window = _ca("window", "open", "window.living", "comfort")
    clean = ExternalContext(pm25=5.0, fetched_at=TS)
    plain = CandidateEvaluator(catalog=None).evaluate(
        _cset(ActionCandidate(candidate=window)), external=clean, feedback=_Pref(0.5)
    )
    declared = CandidateEvaluator(catalog=None).evaluate(
        _cset(ActionCandidate(candidate=window, declared_benefit=0.2)),
        external=clean,
        feedback=_Pref(0.5),
    )
    assert plain.scored[0].expected_effect == 1.0  # 기본 0.7 + 맑은 바깥 0.3
    assert declared.scored[0].expected_effect == 0.2  # 선언값 그대로(외기 보정 없음)


def test_declared_benefit_must_be_in_unit_range() -> None:
    with pytest.raises(ValueError):
        ActionCandidate(candidate=ADVICE, declared_benefit=1.5)


# ── 위험도 감점 ──────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("cap", "action", "risk", "penalty"),
    [
        ("light", "turn_on", 0.0, 0.0),  # AMBIENT
        ("cover", "close", 0.0, 0.0),  # BREATH
        ("cover", "open", 0.5, 0.25),  # DUSK
        ("valve", "open", 1.0, 0.5),  # EMBER
    ],
)
def test_safety_penalty_only_from_dusk(cap: str, action: str, risk: float, penalty: float) -> None:
    ac = ActionCandidate(candidate=_ca(cap, action, f"{cap}.x", "comfort"))
    with_catalog = CandidateEvaluator(catalog=CATALOG).evaluate(_cset(ac), feedback=_Pref(0.5))
    without = CandidateEvaluator(catalog=None).evaluate(_cset(ac), feedback=_Pref(0.5))
    assert with_catalog.scored[0].safety_risk == risk
    assert round(without.scored[0].final_score - with_catalog.scored[0].final_score, 4) == penalty


def test_unknown_action_is_ember_penalty() -> None:
    # 카탈로그에 없는 (capability, action) = default-deny EMBER → 감점 0.5.
    ac = ActionCandidate(candidate=_ca("advice", "unlock_everything", "advice.home"))
    res = CandidateEvaluator(catalog=CATALOG).evaluate(_cset(ac), feedback=_Pref(0.5))
    assert res.scored[0].safety_risk == 1.0


# ── 공기질 = PM2.5 한국 등급 ─────────────────────────────────────────────────


def _vent_effect(*, pm25: float | None = None, outdoor_aqi: int | None = None) -> float:
    window = ActionCandidate(candidate=_ca("window", "open", "window.living", "comfort"))
    ext = ExternalContext(fetched_at=TS, pm25=pm25, outdoor_aqi=outdoor_aqi)
    res = CandidateEvaluator(catalog=None).evaluate(
        _cset(window), external=ext, feedback=_Pref(0.5)
    )
    return res.scored[0].expected_effect


@pytest.mark.parametrize(
    ("pm25", "effect"),
    [(15.0, 1.0), (15.1, 0.7), (35.9, 0.7), (36.0, 0.4)],
)
def test_ventilation_effect_follows_korean_pm25_bands(pm25: float, effect: float) -> None:
    assert _vent_effect(pm25=pm25) == pytest.approx(effect)


def test_purifier_bonus_only_when_pm25_bad() -> None:
    def purifier_effect(pm25: float) -> float:
        ac = ActionCandidate(candidate=_ca("fan", "set_speed", "fan.living_purifier", "climate"))
        res = CandidateEvaluator(catalog=None).evaluate(
            _cset(ac), external=ExternalContext(pm25=pm25, fetched_at=TS), feedback=_Pref(0.5)
        )
        return res.scored[0].expected_effect

    assert purifier_effect(35.0) == pytest.approx(0.7)
    assert purifier_effect(36.0) == pytest.approx(0.9)


@pytest.mark.parametrize(
    ("aqi", "pm25"),
    [(0, 0.0), (50, 9.0), (51, 9.1), (100, 35.4), (101, 35.5), (180, 96.9), (600, 325.4)],
)
def test_pm25_is_derived_from_legacy_aqi(aqi: int, pm25: float) -> None:
    assert pm25_from_aqi(aqi) == pm25
    assert ExternalContext(outdoor_aqi=aqi, fetched_at=TS).pm25 == pm25


def test_explicit_pm25_wins_over_legacy_aqi() -> None:
    ctx = ExternalContext(outdoor_aqi=180, pm25=10.0, fetched_at=TS)
    assert ctx.pm25 == 10.0
    assert _vent_effect(outdoor_aqi=180, pm25=10.0) == pytest.approx(1.0)


def test_cloud_cover_must_be_a_fraction() -> None:
    with pytest.raises(ValueError):
        ExternalContext(cloud_cover=1.2, fetched_at=TS)
