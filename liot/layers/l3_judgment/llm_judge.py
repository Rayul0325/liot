"""Gate2 — LLM 판단(LLMJudge) + 결정론 FakeLLM.

불변식 강제:

  - **불변식 2 (Gate2 는 Gate1 을 무를 수 없다)**: 최종 위험도는
    ``escalate_only(gate1_floor, llm_tier) == max(...)``. LLM 이 더 낮은 단계를
    내밀면 그 의도는 *무시*되고, gate2_verdict 에 ``injection_suspect`` 가 기록된다.
  - **불변식 6 (Safety-Never-Silent)**: 안전 임계 카테고리는 LLM 가용성과
    무관하게 Gate1 의 EMBER 하한이 그대로 유지된다. LLM 예외/타임아웃/파싱
    실패가 나도 안전 카테고리는 절대 침묵하지 않는다.
  - **파급도 합성**: 최종 파급도 = ``escalate_severity(severity_floor, model)``.
    모델이 하한보다 낮은 파급도를 내면 무시하고 tier 의 낮은 답과 똑같이
    ``injection_suspect`` 로 기록한다. 실제로 올렸을 때만 출처에 'model' 이 붙는다.
  - **불변식 4 (재현 가능)**: Judgment 에 snapshot_hash + bundle_id +
    rationale + model_id + degraded 를 함께 채운다.

LLM 백엔드:
  - 기본은 결정론적 ``FakeLLM`` (네트워크 없음, category 기반 tier·rationale).
  - ``LIOT_LLM_BACKEND`` 가 ollama|gemma|home|local 이면 ``OllamaBackend``
    (로컬 ollama 실연동, egress 게이트 통과).
  - ``LIOT_LLM_BACKEND=decision`` 이면 ``DecisionBackend``(로컬 llama.cpp 결정 모델,
    선택지 글자 확률 판독. tier 와 파급도 두 질문).
  - 그 밖의 값(예: litellm)은 미연결 스텁 ``_LiteLLMStub`` — 항상 LLMUnavailable
    이라 degraded fail-safe 로 떨어진다. 자세한 선택 규칙은 ``build_llm_backend``.
"""

from __future__ import annotations

import json
import os
import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Protocol, runtime_checkable

from ...core.contracts import CandidateAction, ContextSnapshot, Judgment
from ...core.risk import RiskTier, escalate_only, is_safety_category
from ...core.severity import (
    SEVERITY_SOURCE_ORDER,
    SeverityLevel,
    SeveritySource,
    escalate_severity,
)
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
    """LLM 백엔드의 원시 판단 결과(아직 Gate1 과 합성 전).

    severity: 모델이 낸 상황 파급도. 파급도 질문을 하지 않았거나(하한 없음)
        파급도를 내지 않는 백엔드(Fake·Ollama)면 None — None 은 '올리지 않음'이다.
    probs: 질문 id("tier", "severity") → {선택지 id: 확률}. 확률을 내는
        결정 모델만 채운다.
    """

    tier: RiskTier
    confidence: float
    rationale: str
    model_id: str
    severity: SeverityLevel | None = None
    probs: dict[str, dict[str, float]] | None = None


@runtime_checkable
class LLMBackend(Protocol):
    """동기 판단 백엔드 인터페이스(Phase1 단일 프로세스용)."""

    model_id: str

    def judge_raw(
        self,
        candidate: CandidateAction,
        ctx: ContextSnapshot | None,
        gate1_floor: RiskTier,
        severity_floor: SeverityLevel | None = None,
    ) -> LLMResult: ...  # severity_floor 가 None 이면 파급도는 묻지 않는다.


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
        self,
        candidate: CandidateAction,
        ctx: ContextSnapshot | None,
        gate1_floor: RiskTier,
        severity_floor: SeverityLevel | None = None,
    ) -> LLMResult:
        category = (candidate.category or "").strip().lower()
        if is_safety_category(category):
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
    """미연결 백엔드 스텁 — ollama 계열이 아닌 LIOT_LLM_BACKEND 값에 선택된다.

    네트워크 연결 대신 항상 LLMUnavailable 을 던져, LLMJudge 의 degraded
    fail-safe 경로(불변식 6 포함)가 동작함을 보장한다. 실연동은 OllamaBackend 뿐.
    """

    def __init__(self, backend_name: str) -> None:
        self.model_id = f"litellm-stub:{backend_name}"

    def judge_raw(
        self,
        candidate: CandidateAction,
        ctx: ContextSnapshot | None,
        gate1_floor: RiskTier,
        severity_floor: SeverityLevel | None = None,
    ) -> LLMResult:
        raise LLMUnavailable(
            f"real LLM backend {self.model_id!r} not wired in Phase1 (no network)"
        )


def build_llm_backend() -> LLMBackend:
    """환경에 따라 백엔드를 고른다.

    LIOT_LLM_BACKEND 미설정/'fake' → FakeLLM(결정론).
    'ollama'|'gemma'|'home'|'local' → OllamaBackend(실연동, egress 통과).
    'decision' → DecisionBackend(로컬 llama.cpp 결정 모델, egress 통과).
    그 밖의 값 → _LiteLLMStub(미연결. 항상 LLMUnavailable → degraded 경로).
    """
    backend = os.environ.get("LIOT_LLM_BACKEND", "").strip()
    if not backend or backend.lower() in ("fake", "fakellm", "none"):
        return FakeLLM()
    if backend.lower() in ("ollama", "gemma", "home", "local"):
        # 지연 임포트(ollama_backend 가 본 모듈을 import 하므로 순환 회피).
        from .ollama_backend import build_ollama_backend

        return build_ollama_backend()
    if backend.lower() == "decision":
        # 지연 임포트(decision_backend 도 본 모듈을 import 하므로 순환 회피).
        from .decision_backend import build_decision_backend

        return build_decision_backend()
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
        severity_floor: SeverityLevel | None = None,
        severity_sources: Sequence[SeveritySource] = (),
        situation_id: str | None = None,
    ) -> Judgment:
        """Gate1 결과 + LLM 판단을 합성해 최종 Judgment 를 만든다.

        gate1: Gate1(guardrail) 평가 결과. gate1.floor_tier 가 하한이다.
        최종 risk_tier = escalate_only(gate1.floor_tier, llm_tier) (불변식 2).

        severity_floor: 표+거주자로 정해진 파급도 하한. None 이면 파급도 질문을
            하지 않고 Judgment 의 파급도 필드도 비운다.
        severity_sources: 하한의 출처(예 table/resident). 그대로 보존되고, 모델이
            하한 위로 실제로 올렸을 때만 'model' 이 SEVERITY_SOURCE_ORDER 순서로 붙는다.
            알 수 없는 출처는 ``ValueError``.
        situation_id: 상황 식별자. 주어지면 Judgment 에 그대로 실린다.
        최종 severity = escalate_severity(severity_floor, model.severity).
        """
        gate1_floor = gate1.floor_tier
        is_safety = gate1.safety_critical or is_safety_category(candidate.category)
        floor_sources = _ordered_sources(severity_sources) if severity_floor is not None else []

        bundle = self._bundle(ruleset_version)
        trace = trace_id or ctx.trace_id
        home = home_id or ctx.home_id

        degraded = False
        gate2_verdict: str | None
        model_id = self.backend.model_id

        try:
            if severity_floor is None:
                # 하한이 없으면 파급도를 묻지 않는다. 인자를 아예 넘기지 않아
                # 파급도 인자를 모르는 기존 백엔드와도 호환된다.
                raw = self.backend.judge_raw(candidate, ctx, gate1_floor)
            else:
                raw = self.backend.judge_raw(
                    candidate, ctx, gate1_floor, severity_floor=severity_floor
                )
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
            # 파급도는 하한 그대로, 출처도 하한의 출처 그대로.
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
                severity=severity_floor,
                severity_floor=severity_floor,
                severity_sources=floor_sources,
                situation_id=situation_id,
                model_probs=None,
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

        # ── 파급도 합성: 최종 = escalate_severity(하한, 모델) ─────────────
        severity: SeverityLevel | None = severity_floor
        sources = floor_sources
        if severity_floor is not None and result.severity is not None:
            model_sev = result.severity
            severity = escalate_severity(severity_floor, model_sev)
            if model_sev > severity_floor:
                sources = _ordered_sources([*floor_sources, "model"])
                rationale += (
                    f" 파급도: 모델이 하한 {severity_floor.label_ko}({severity_floor.name}) "
                    f"위로 {severity.label_ko}({severity.name}) 승격."
                )
            elif model_sev < severity_floor:
                # tier 의 낮은 답과 같은 방식으로 기록 — 무시 + injection_suspect.
                gate2_verdict = "injection_suspect"
                confidence = 0.0
                rationale += (
                    f" 파급도: 모델이 {model_sev.label_ko}({model_sev.name}) 제안했으나 "
                    f"하한 {severity_floor.label_ko}({severity_floor.name}) 미만이라 무시함. "
                    f"최종 {severity.label_ko} 유지."
                )

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
            severity=severity,
            severity_floor=severity_floor,
            severity_sources=sources,
            situation_id=situation_id,
            model_probs=_copy_probs(result.probs),
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
        severity: SeverityLevel | None,
        severity_floor: SeverityLevel | None,
        severity_sources: list[SeveritySource],
        situation_id: str | None,
        model_probs: dict[str, dict[str, float]] | None,
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
            severity=severity,
            severity_floor=severity_floor,
            severity_sources=severity_sources,
            situation_id=situation_id,
            model_probs=model_probs,
        )


def _ordered_sources(sources: Sequence[str]) -> list[SeveritySource]:
    """파급도 출처를 중복 없이 SEVERITY_SOURCE_ORDER 순서로. 알 수 없는 출처는 ValueError."""
    unknown = [s for s in sources if s not in SEVERITY_SOURCE_ORDER]
    if unknown:
        raise ValueError(f"unknown severity source(s): {unknown!r}")
    given = set(sources)
    return [s for s in SEVERITY_SOURCE_ORDER if s in given]


def _copy_probs(
    probs: dict[str, dict[str, float]] | None,
) -> dict[str, dict[str, float]] | None:
    """백엔드 확률표를 얕게 복사(Judgment 가 백엔드 객체와 dict 를 공유하지 않게)."""
    if probs is None:
        return None
    return {q: dict(opts) for q, opts in probs.items()}
