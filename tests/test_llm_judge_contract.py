"""Gate2(LLMJudge) 계약 — 등급 경계·파싱 실패·백엔드 선택.

2026-09-16 야간 뮤테이션 스윕에서 이 모듈의 가드 11곳을 뒤집어도 전체 스위트가
초록이었다. 특히 `llm_tier < gate1_floor` / `> gate1_floor` 경계는 '같을 때'의
동작(concur)이 아무 테스트에도 고정돼 있지 않았다 — 같은 등급을 낸 정상 응답이
injection_suspect 로 기록돼도 몰랐다는 뜻이다.
"""

from __future__ import annotations

import dataclasses

import pytest

from liot.core.contracts import CandidateAction, ContextSnapshot, Judgment
from liot.core.risk import RiskTier
from liot.layers.l3_judgment.guardrail import Gate1Result, Guardrail
from liot.layers.l3_judgment.llm_judge import (
    FakeLLM,
    LLMBackend,
    LLMJudge,
    LLMResult,
    LLMUnavailable,
    build_llm_backend,
)

CAND = CandidateAction(capability="light", action="turn_on", params={},
                       entity_id="light.living_room", category="light")
# 안전 카테고리라 Gate1 하한이 EMBER 인 후보 — '하한보다 낮추려는 시도'를 만들려면
# 하한이 바닥(AMBIENT)이 아니어야 한다.
SAFETY_CAND = CandidateAction(capability="valve", action="open", params={},
                              entity_id="switch.gas_valve", category="gas")


def _ctx() -> ContextSnapshot:
    return ContextSnapshot(
        snapshot_id="s", trace_id="t", home_id="singleton", trigger={},
        structured={"time_bucket": "night"}, nl_summary="", summary_template_sha="x",
        created_at="2026-06-02T22:00:00+00:00",
    )


class _StubBackend:
    """고정된 tier 를 내는 백엔드(또는 예외)."""

    model_id = "stub-v1"

    def __init__(self, tier: RiskTier | None = None, raw: object = None,
                 exc: Exception | None = None) -> None:
        self._tier, self._raw, self._exc = tier, raw, exc

    def judge_raw(
        self, candidate: CandidateAction, ctx: ContextSnapshot | None, gate1_floor: RiskTier
    ) -> LLMResult:
        if self._exc is not None:
            raise self._exc
        if self._raw is not None:
            return self._raw  # type: ignore[return-value]  # 관용 파서 입력(문자열) 테스트용
        # AMBIENT 는 0 이라 falsy — `or` 를 쓰면 하한으로 새 버린다(실제로 겪었다).
        tier = self._tier if self._tier is not None else gate1_floor
        return LLMResult(tier=tier, confidence=0.77,
                         rationale="stub", model_id=self.model_id)


def _judge(
    backend: LLMBackend, candidate: CandidateAction = CAND
) -> tuple[Judgment, Gate1Result]:
    g = Guardrail()
    gate1 = g.evaluate(candidate, _ctx())
    jd = LLMJudge(backend=backend).judge(candidate, _ctx(), gate1,
                                         ruleset_version=g.ruleset_version)
    return jd, gate1


# ── 등급 경계 ───────────────────────────────────────────────────────────
def test_same_tier_as_floor_is_concur_not_suspicion() -> None:
    """LLM 이 하한과 **같은** 등급을 내면 동의(concur)다 — 의심도 승격도 아니다."""
    floor = Guardrail().evaluate(SAFETY_CAND, _ctx()).floor_tier
    jd, gate1 = _judge(_StubBackend(tier=floor), SAFETY_CAND)
    assert gate1.floor_tier is floor
    assert jd.risk_tier is floor
    assert jd.gate2_verdict == "concur", jd.gate2_verdict
    assert jd.confidence == pytest.approx(0.77)  # 동의면 LLM 신뢰도를 채택한다


def test_lower_than_floor_is_clamped_and_flagged() -> None:
    """하한보다 낮추려 하면 무시하고 injection_suspect 로 남긴다(불변식 2)."""
    jd, gate1 = _judge(_StubBackend(tier=RiskTier.AMBIENT), SAFETY_CAND)
    assert gate1.floor_tier is RiskTier.EMBER  # 안전 카테고리 하한
    assert jd.risk_tier is RiskTier.EMBER
    assert jd.gate2_verdict == "injection_suspect"
    assert jd.confidence == 0.0  # 하한을 무르려는 시도의 신뢰도는 채택하지 않는다


def test_higher_than_floor_escalates() -> None:
    """더 높은 등급은 그대로 승격된다(안전 쪽으로는 열려 있다)."""
    jd, gate1 = _judge(_StubBackend(tier=RiskTier.EMBER))
    assert gate1.floor_tier < RiskTier.EMBER
    assert jd.risk_tier is RiskTier.EMBER
    assert jd.gate2_verdict == "escalated"


# ── 파싱·실패 경로 ──────────────────────────────────────────────────────
@pytest.mark.parametrize("raw", ['[1, 2]', '"문자열"', "42", "null"])
def test_non_object_model_output_falls_back_to_degraded(raw: str) -> None:
    """JSON 이지만 객체가 아니면 degraded fail-safe — 하한은 유지된다."""
    jd, gate1 = _judge(_StubBackend(raw=raw))
    assert jd.degraded is True
    assert jd.risk_tier >= gate1.floor_tier
    assert jd.gate2_verdict == "degraded_fail_safe"


def test_successful_judgment_is_not_degraded() -> None:
    """정상 판정은 degraded 가 아니다."""
    jd, _ = _judge(_StubBackend(tier=RiskTier.BREATH))
    assert jd.degraded is False


def test_valid_json_object_output_is_parsed_not_degraded() -> None:
    """JSON **객체** 응답은 정상 파싱된다 — 객체 검사가 뒤집히면 이게 degraded 가 된다."""
    raw = '{"tier": "ember", "confidence": 0.9, "rationale": "가스"}'
    jd, _ = _judge(_StubBackend(raw=raw))
    assert jd.degraded is False
    assert jd.risk_tier is RiskTier.EMBER
    assert jd.gate2_verdict == "escalated"


def test_model_id_comes_from_the_result_not_the_backend_label() -> None:
    """실제 판정을 낸 모델 id 가 기록된다(백엔드 라벨이 아니라).

    둘이 같은 값이면 이 구분이 테스트에 안 잡힌다 — 일부러 다르게 둔다.
    """
    class _Renaming(_StubBackend):
        def judge_raw(
            self, candidate: CandidateAction, ctx: ContextSnapshot | None,
            gate1_floor: RiskTier,
        ) -> LLMResult:
            return LLMResult(tier=RiskTier.BREATH, confidence=0.5, rationale="r",
                             model_id="resolved-v2")

    jd, _ = _judge(_Renaming())
    assert jd.model_id == "resolved-v2", jd.model_id


def test_backend_failure_keeps_floor_and_marks_degraded() -> None:
    jd, gate1 = _judge(_StubBackend(exc=LLMUnavailable("no model")))
    assert jd.degraded is True and jd.risk_tier >= gate1.floor_tier


def test_llm_result_is_immutable() -> None:
    """LLMResult 도 얼려 둔다 — 판정 결과를 하류가 고쳐 쓰지 못한다."""
    res = LLMResult(tier=RiskTier.BREATH, confidence=0.5, rationale="r", model_id="m")
    with pytest.raises(dataclasses.FrozenInstanceError):
        res.tier = RiskTier.AMBIENT  # type: ignore[misc]


# ── 백엔드 선택 ─────────────────────────────────────────────────────────
@pytest.mark.parametrize("value", ["", "   ", "fake", "FakeLLM", "none"])
def test_unset_or_fake_backend_uses_fake(monkeypatch: pytest.MonkeyPatch, value: str) -> None:
    monkeypatch.setenv("LIOT_LLM_BACKEND", value)
    assert isinstance(build_llm_backend(), FakeLLM)


@pytest.mark.parametrize("value", ["ollama", "gemma", "home", "local"])
def test_ollama_family_selects_the_real_backend(
    monkeypatch: pytest.MonkeyPatch, value: str
) -> None:
    from liot.layers.l3_judgment.ollama_backend import OllamaBackend

    monkeypatch.setenv("LIOT_LLM_BACKEND", value)
    assert isinstance(build_llm_backend(), OllamaBackend)


def test_unknown_backend_name_is_a_stub_that_always_degrades(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """모르는 이름은 '연결된 척'이 아니라 항상 LLMUnavailable 인 스텁이다."""
    monkeypatch.setenv("LIOT_LLM_BACKEND", "litellm")
    backend = build_llm_backend()
    assert not isinstance(backend, FakeLLM)
    with pytest.raises(LLMUnavailable):
        backend.judge_raw(CAND, _ctx(), RiskTier.BREATH)
