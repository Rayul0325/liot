"""Gate2 — LLM 판단(LLMJudge) + 결정론 FakeLLM.

불변식 강제:

  - **불변식 2 (Gate2 는 Gate1 을 무를 수 없다)**: 최종 위험도는
    ``escalate_only(gate1_floor, llm_tier) == max(...)``. LLM 이 더 낮은 단계를
    내밀면 그 의도는 *무시*되고, gate2_verdict 에 ``injection_suspect`` 가 기록된다.
  - **불변식 6 (Safety-Never-Silent)**: 안전 임계 카테고리는 LLM 가용성과
    무관하게 Gate1 의 EMBER 하한이 그대로 유지된다. LLM 예외/타임아웃/파싱
    실패가 나도 안전 카테고리는 절대 침묵하지 않는다.
  - **불변식 4 (재현 가능)**: Judgment 에 snapshot_hash + bundle_id +
    rationale + model_id + degraded 를 함께 채운다.

LLM 백엔드:
  - 기본은 결정론적 ``FakeLLM`` (네트워크 없음, category 기반 tier·rationale).
  - 환경변수 ``LIOT_LLM_BACKEND`` 가 설정되면 실제 백엔드(예: litellm) *스텁*을
    쓰되, 본 Phase1 에서는 연결하지 않고 미설정 시 FakeLLM 으로 폴백한다.
"""

from __future__ import annotations

import json
import os
import uuid
from dataclasses import dataclass
from typing import Protocol, runtime_checkable

from ...core.contracts import CandidateAction, ContextSnapshot, Judgment
from ...core.risk import SAFETY_CRITICAL_CATEGORIES, RiskTier, escalate_only
from .guardrail import Gate1Result
from .prompt_registry import BundleManifest, PromptRegistry

__all__ = [
    "LLMResult",
    "LLMUnavailable",
    "LLMBackend",
    "FakeLLM",
    "LLMJudge",
    "build_llm_backend",
]


class LLMUnavailable(Exception):
    """LLM 백엔드가 가용하지 않거나 호출이 실패/타임아웃했음을 표시."""


@dataclass(frozen=True)
class LLMResult:
    """LLM 백엔드의 원시 판단 결과(아직 Gate1 과 합성 전)."""

    tier: RiskTier
    confidence: float
    rationale: str
    model_id: str


@runtime_checkable
class LLMBackend(Protocol):
    """동기 판단 백엔드 인터페이스(Phase1 단일 프로세스용)."""

    model_id: str

    def judge_raw(
        self, candidate: CandidateAction, ctx: ContextSnapshot | None, gate1_floor: RiskTier
    ) -> LLMResult: ...


# category → FakeLLM 이 제안하는 (tier, rationale 키워드). 결정론적.
_FAKE_CATEGORY_TIER: dict[str, RiskTier] = {
    "light": RiskTier.AMBIENT,
    "scene": RiskTier.AMBIENT,
    "info": RiskTier.AMBIENT,
    "ambient": RiskTier.AMBIENT,
    "climate": RiskTier.BREATH,
    "media": RiskTier.BREATH,
    "noise": RiskTier.BREATH,
    "blind": RiskTier.BREATH,
    "curtain": RiskTier.BREATH,
    "appliance": RiskTier.DUSK,
    "power": RiskTier.DUSK,
}


class FakeLLM:
    """결정론적 가짜 LLM. 네트워크 없음.

    category 로 tier 를 정하고(안전 카테고리는 EMBER), 한 줄 rationale 을 만든다.
    같은 입력이면 항상 같은 출력 → 재현 가능.
    """

    model_id = "fake-llm-v1"

    def judge_raw(
        self, candidate: CandidateAction, ctx: ContextSnapshot | None, gate1_floor: RiskTier
    ) -> LLMResult:
        category = (candidate.category or "").strip().lower()
        if category in SAFETY_CRITICAL_CATEGORIES:
            tier = RiskTier.EMBER
            reason = f"안전 임계 카테고리 '{category}': 즉시 확인 필요"
        else:
            tier = _FAKE_CATEGORY_TIER.get(category, RiskTier.DUSK)
            reason = (
                f"'{category}' 액션 '{candidate.action}' 의 일상 위험도 추정 {tier.label_ko}"
            )
        # confidence: 안전/명시 카테고리는 높게, 미지 카테고리는 낮게.
        confidence = 0.9 if category in _FAKE_CATEGORY_TIER or tier == RiskTier.EMBER else 0.5
        return LLMResult(
            tier=tier, confidence=confidence, rationale=reason, model_id=self.model_id
        )


class _LiteLLMStub:
    """실제 백엔드 스텁(LIOT_LLM_BACKEND 설정 시 선택되지만 Phase1 에선 미연결).

    네트워크 연결 대신 항상 LLMUnavailable 을 던져, LLMJudge 의 degraded
    fail-safe 경로(불변식 6 포함)가 동작함을 보장한다. 실제 연결은 후속 Phase.
    """

    def __init__(self, backend_name: str) -> None:
        self.model_id = f"litellm-stub:{backend_name}"

    def judge_raw(
        self, candidate: CandidateAction, ctx: ContextSnapshot | None, gate1_floor: RiskTier
    ) -> LLMResult:
        raise LLMUnavailable(
            f"real LLM backend {self.model_id!r} not wired in Phase1 (no network)"
        )


def build_llm_backend() -> LLMBackend:
    """환경에 따라 백엔드를 고른다.

    LIOT_LLM_BACKEND 미설정 → FakeLLM. 설정 → 스텁(Phase1 미연결).
    """
    backend = os.environ.get("LIOT_LLM_BACKEND", "").strip()
    if not backend or backend.lower() in ("fake", "fakellm", "none"):
        return FakeLLM()
    if backend.lower() in ("ollama", "gemma", "home", "local"):
        # 지연 임포트(ollama_backend 가 본 모듈을 import 하므로 순환 회피).
        from .ollama_backend import build_ollama_backend

        return build_ollama_backend()
    return _LiteLLMStub(backend)


class LLMJudge:
    """Gate2. Gate1 의 결과를 받아 LLM 판단을 합성해 Judgment 를 만든다."""

    def __init__(
        self,
        backend: LLMBackend | None = None,
        registry: PromptRegistry | None = None,
    ) -> None:
        self.backend: LLMBackend = backend if backend is not None else build_llm_backend()
        self.registry: PromptRegistry = registry if registry is not None else PromptRegistry()

    def _bundle(self, ruleset_version: str) -> BundleManifest:
        return self.registry.make_bundle(
            ruleset_version,
            policy={"model_id": self.backend.model_id, "gate": "gate1+gate2"},
        )

    def _parse_result(self, raw: object, model_id: str) -> LLMResult:
        """백엔드가 LLMResult 가 아닌 dict/JSON 문자열을 줘도 관용 파싱.

        파싱 실패 시 LLMUnavailable 로 승격(→ degraded fail-safe).
        """
        if isinstance(raw, LLMResult):
            return raw
        data: object = raw
        if isinstance(raw, str):
            try:
                data = json.loads(raw)
            except (ValueError, TypeError) as exc:
                raise LLMUnavailable(f"unparsable LLM output: {exc}") from exc
        if not isinstance(data, dict):
            raise LLMUnavailable("LLM output is not an object")
        try:
            tier = RiskTier.from_str(data["tier"])
            confidence = float(data.get("confidence", 0.5))
            rationale = str(data.get("rationale", ""))
        except (KeyError, ValueError, TypeError) as exc:
            raise LLMUnavailable(f"malformed LLM output: {exc}") from exc
        return LLMResult(
            tier=tier, confidence=confidence, rationale=rationale, model_id=model_id
        )

    def judge(
        self,
        candidate: CandidateAction,
        ctx: ContextSnapshot,
        gate1: Gate1Result,
        *,
        ruleset_version: str,
        trace_id: str | None = None,
        home_id: str | None = None,
    ) -> Judgment:
        """Gate1 결과 + LLM 판단을 합성해 최종 Judgment 를 만든다.

        gate1: Gate1(guardrail) 평가 결과. gate1.floor_tier 가 하한이다.
        최종 risk_tier = escalate_only(gate1.floor_tier, llm_tier) (불변식 2).
        """
        gate1_floor = gate1.floor_tier
        is_safety = gate1.safety_critical or (
            (candidate.category or "").strip().lower() in SAFETY_CRITICAL_CATEGORIES
        )

        bundle = self._bundle(ruleset_version)
        trace = trace_id or ctx.trace_id
        home = home_id or ctx.home_id

        degraded = False
        gate2_verdict: str | None
        model_id = self.backend.model_id

        try:
            raw = self.backend.judge_raw(candidate, ctx, gate1_floor)
            result = self._parse_result(raw, model_id)
            model_id = result.model_id or model_id
        except LLMUnavailable:
            # ── degraded fail-safe 경로 ──────────────────────────────
            degraded = True
            if is_safety:
                # 불변식 6: 안전 카테고리는 Gate1 EMBER 를 그대로 유지. 절대 침묵 아님.
                final_tier = escalate_only(gate1_floor, RiskTier.EMBER)
                rationale = (
                    "LLM 비가용(degraded). 안전 임계 카테고리이므로 Gate1 결정론으로 "
                    f"즉시 {final_tier.label_ko} 제안 유지(Safety-Never-Silent)."
                )
            else:
                # 비안전: 한 단계 승격, 단 최소 DUSK 보장(보수적).
                bumped = RiskTier(min(int(gate1_floor) + 1, int(RiskTier.EMBER)))
                final_tier = escalate_only(gate1_floor, escalate_only(bumped, RiskTier.DUSK))
                rationale = (
                    "LLM 비가용(degraded). 비안전 카테고리를 보수적으로 한 단계 승격하여 "
                    f"{final_tier.label_ko} 로 판정."
                )
            confidence = 0.0
            gate2_verdict = "degraded_fail_safe"
            return self._build(
                candidate=candidate,
                ctx=ctx,
                gate1=gate1,
                final_tier=final_tier,
                confidence=confidence,
                rationale=rationale,
                gate2_verdict=gate2_verdict,
                bundle=bundle,
                model_id=model_id,
                degraded=degraded,
                trace=trace,
                home=home,
            )

        # ── 정상 경로 ────────────────────────────────────────────────
        llm_tier = result.tier
        # 불변식 2: Gate2 는 Gate1 을 무를 수 없다. final = max(floor, llm).
        final_tier = escalate_only(gate1_floor, llm_tier)

        if llm_tier < gate1_floor:
            # LLM 이 하한보다 낮추려 함 → 무시 + injection_suspect.
            gate2_verdict = "injection_suspect"
            rationale = (
                f"LLM 이 {llm_tier.label_ko}({llm_tier.name}) 제안했으나 Gate1 하한 "
                f"{gate1_floor.label_ko}({gate1_floor.name}) 미만이라 무시함. "
                f"최종 {final_tier.label_ko} 유지. LLM 사유: {result.rationale}"
            )
            # 하한을 무르려는 시도이므로 LLM 신뢰도는 채택하지 않는다.
            confidence = 0.0
        elif llm_tier > gate1_floor:
            gate2_verdict = "escalated"
            rationale = (
                f"LLM 이 하한 {gate1_floor.label_ko} 위로 {final_tier.label_ko} 승격. "
                f"사유: {result.rationale}"
            )
            confidence = result.confidence
        else:
            gate2_verdict = "concur"
            rationale = (
                f"LLM 이 Gate1 하한 {final_tier.label_ko} 에 동의. 사유: {result.rationale}"
            )
            confidence = result.confidence

        return self._build(
            candidate=candidate,
            ctx=ctx,
            gate1=gate1,
            final_tier=final_tier,
            confidence=confidence,
            rationale=rationale,
            gate2_verdict=gate2_verdict,
            bundle=bundle,
            model_id=model_id,
            degraded=degraded,
            trace=trace,
            home=home,
        )

    def _build(
        self,
        *,
        candidate: CandidateAction,
        ctx: ContextSnapshot,
        gate1: Gate1Result,
        final_tier: RiskTier,
        confidence: float,
        rationale: str,
        gate2_verdict: str | None,
        bundle: BundleManifest,
        model_id: str,
        degraded: bool,
        trace: str,
        home: str,
    ) -> Judgment:
        return Judgment(
            judgment_id=f"jdg_{uuid.uuid4().hex[:16]}",
            trace_id=trace,
            home_id=home,
            snapshot_hash=ctx.snapshot_hash,
            action=candidate,
            risk_tier=final_tier,
            confidence=confidence,
            rationale=rationale,
            gate1_verdict=gate1.gate1_verdict,
            gate2_verdict=gate2_verdict,
            bundle_id=bundle.bundle_id,
            model_id=model_id,
            degraded=degraded,
        )
