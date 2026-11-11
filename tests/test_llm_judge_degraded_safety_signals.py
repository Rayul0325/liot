"""Gate2 degraded 경로: 안전 판단의 두 신호가 각각 단독으로 EMBER 를 유지시킨다.

E8 뮤테이션 빈틈(llm_judge.py:257): `gate1.safety_critical or is_safety_category(category)` 의
`or` 가 `and` 로 바뀌어도 아무 테스트도 안 깨졌다. 모델이 죽었을 때(불변식 6, Safety-Never-
Silent) 두 신호 중 **하나만** 참인 경우에도 EMBER 가 유지돼야 한다. 기대값은 리터럴.
"""

from __future__ import annotations

import pytest

from liot.core.contracts import CandidateAction, ContextSnapshot
from liot.core.risk import RiskTier
from liot.layers.l3_judgment.guardrail import Gate1Result
from liot.layers.l3_judgment.llm_judge import LLMJudge, LLMUnavailable


class _DeadBackend:
    model_id = "dead"

    def judge_raw(self, candidate, ctx, gate1_floor, severity_floor=None):  # noqa: ANN001
        raise LLMUnavailable("no model")


def _ctx() -> ContextSnapshot:
    return ContextSnapshot(
        snapshot_id="s", trace_id="t", home_id="singleton", trigger={},
        structured={"time_bucket": "night"}, nl_summary="", summary_template_sha="x",
        created_at="2026-06-02T22:00:00+00:00",
    )


def _cand(category: str) -> CandidateAction:
    return CandidateAction(capability="valve", action="open", params={},
                           entity_id="switch.x", category=category)


def _gate1(*, safety_critical: bool, floor: RiskTier = RiskTier.AMBIENT) -> Gate1Result:
    return Gate1Result(floor_tier=floor, gate1_verdict="t", blocked=False,
                       matched_rule_ids=(), safety_critical=safety_critical)


def _degraded(category: str, gate1: Gate1Result):
    return LLMJudge(backend=_DeadBackend()).judge(
        _cand(category), _ctx(), gate1, ruleset_version="rs-test"
    )


@pytest.mark.parametrize(
    ("category", "gate1_flag"),
    [
        ("light", True),  # Gate1 만 안전 임계라고 함(카테고리는 비안전)
        ("gas", False),  # 카테고리만 안전(Gate1 플래그는 거짓)
        ("gas", True),  # 둘 다
    ],
)
def test_degraded_keeps_ember_when_either_safety_signal_is_true(
    category: str, gate1_flag: bool
) -> None:
    jd = _degraded(category, _gate1(safety_critical=gate1_flag))
    assert jd.degraded is True
    assert jd.gate2_verdict == "degraded_fail_safe"
    assert jd.risk_tier is RiskTier.EMBER, jd.risk_tier


def test_degraded_without_any_safety_signal_is_only_bumped_to_dusk() -> None:
    # 대조군: 둘 다 거짓이면 EMBER 가 아니라 '한 단계 승격, 최소 DUSK'.
    jd = _degraded("light", _gate1(safety_critical=False))
    assert jd.degraded is True
    assert jd.risk_tier is RiskTier.DUSK, jd.risk_tier
