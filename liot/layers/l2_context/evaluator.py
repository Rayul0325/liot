"""후보 평가·선택 (개념 L5 Candidate Evaluation & Selection).

후보 집합(CandidateSet)을 받아 각 후보를 점수화하고 가장 적절한 후보(또는
무행동)를 고른다. 점수는 두 갈래로 합성된다:

  결정론(권위):
    - safety_risk = catalog.risk_for(...) 정규화. **LLM 이 제출한 값은 무시하고
      카탈로그 값으로 덮어쓴다** — 안전은 결정론적 가드레일/카탈로그가 권위.
    - energy_cost = capability 고정표.
    - user_preference_fit = FeedbackStore(최근 거절 → 감점).
    - 외기 반응 = ExternalContext(외기 나쁨 → 환기 감점·청정기 가점 등).
    - final_score = Σ(가중치·항목).
  LLM(설명):
    - reason 텍스트만(점수 가중 0). 외부 송신 금지.

보수성: ExternalContext.degraded(외부 조회 실패/불확실)면 무행동을 고른다
(Safety-Never-Silent 와 정합 — 모르면 행동하지 않는다).
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from liot.core.contracts import (
    ActionCandidate,
    CandidateSet,
    EvaluationResult,
    ExternalContext,
    is_noop,
)

from .candidate_llm import LLMCandidateBackend
from .feedback_store import NEUTRAL_SIGNAL, FeedbackStore

if TYPE_CHECKING:  # pragma: no cover
    from liot.layers.l4_execution.device_catalog import DeviceCatalog

__all__ = ["CandidateEvaluator", "EvalWeights"]


class EvalWeights:
    """final_score 가중치(고정 상수). 데이터 학습은 후속(예정)."""

    EFFECT = 0.40
    PREF = 0.30
    ENERGY = 0.10
    DISTURB = 0.10
    SAFETY = 0.50
    # 무행동 기준선 — 행동 후보가 이보다 낮으면 침묵을 고른다.
    NOOP_SCORE = 0.35


# capability → 에너지 비용(0~1, 높을수록 나쁨).
_ENERGY_COST: dict[str, float] = {
    "climate": 0.6,
    "heater": 0.6,
    "ac": 0.6,
    "fan": 0.2,
    "purifier": 0.2,
    "air_purifier": 0.2,
    "light": 0.1,
    "cover": 0.05,
    "window": 0.0,
    "notify": 0.0,
}

# 외기 임계.
_AQI_BAD = 100
_AQI_GOOD = 50
_COLD_C = 5.0

_DEFAULT_EFFECT = 0.70
_DEFAULT_DISTURB = 0.10


def _clamp(x: float) -> float:
    return max(0.0, min(1.0, x))


def _is_ventilation(cap: str, action: str) -> bool:
    """바깥 공기를 들이는 행동(환기)인가."""
    return cap == "ventilation" or (cap in ("cover", "window") and action == "open")


def _is_purifier(cap: str) -> bool:
    """실내 공기를 거르는 행동(공기청정)인가."""
    return cap in ("purifier", "air_purifier", "fan")


def _is_heating(cap: str, action: str) -> bool:
    return cap in ("climate", "heater") and action in ("turn_on", "set_temperature")


class CandidateEvaluator:
    """후보 집합을 점수화하고 선택한다(결정론 + LLM 설명)."""

    def __init__(
        self,
        *,
        catalog: "DeviceCatalog | None" = None,
        llm: LLMCandidateBackend | None = None,
        weights: type[EvalWeights] = EvalWeights,
    ) -> None:
        self.catalog = catalog
        self.llm = llm
        self.w = weights

    # ------------------------------------------------------------------
    def _safety_risk(self, ac: ActionCandidate) -> float:
        """결정론적 안전 위험도(0~1). 카탈로그가 권위 — LLM 값 무시."""
        if is_noop(ac.candidate):
            return 0.0
        if self.catalog is None:
            return 0.0
        tier = self.catalog.risk_for(
            ac.candidate.capability, ac.candidate.action, ac.candidate.params
        )
        return int(tier) / 3.0  # AMBIENT0..EMBER3 → 0..1

    def _score_one(
        self, ac: ActionCandidate, external: ExternalContext | None
    ) -> ActionCandidate:
        cand = ac.candidate
        if is_noop(cand):
            return ac.model_copy(
                update={
                    "expected_effect": 0.0,
                    "user_preference_fit": NEUTRAL_SIGNAL,
                    "energy_cost": 0.0,
                    "disturbance": 0.0,
                    "safety_risk": 0.0,
                    "final_score": self.w.NOOP_SCORE,
                }
            )

        cap, action = cand.capability, cand.action
        effect = _DEFAULT_EFFECT
        disturb = _DEFAULT_DISTURB
        energy = _ENERGY_COST.get(cap, 0.3)
        safety = self._safety_risk(ac)
        pref = ac.user_preference_fit  # 평가 전 FeedbackStore 가 미리 채움

        # ── 외기 반응(결정론) ─────────────────────────────────────────
        if external is not None and not external.degraded:
            aqi = external.outdoor_aqi
            temp = external.outdoor_temp_c
            if aqi is not None:
                if _is_ventilation(cap, action):
                    if aqi >= _AQI_BAD:
                        effect -= 0.30  # 바깥이 나쁘면 환기는 역효과
                        disturb += 0.30
                    elif aqi < _AQI_GOOD:
                        effect += 0.30  # 바깥이 맑으면 환기가 최선
                if _is_purifier(cap) and aqi >= _AQI_BAD:
                    effect += 0.20  # 바깥이 나쁘면 청정기가 낫다
            if temp is not None and temp <= _COLD_C and _is_heating(cap, action):
                effect += 0.20

        effect = _clamp(effect)
        disturb = _clamp(disturb)
        final = (
            self.w.EFFECT * effect
            + self.w.PREF * pref
            - self.w.ENERGY * energy
            - self.w.DISTURB * disturb
            - self.w.SAFETY * safety
        )
        reason = ac.reason
        if not reason and self.llm is not None:
            reason = self.llm.explain(cand, {})
        return ac.model_copy(
            update={
                "expected_effect": effect,
                "user_preference_fit": pref,
                "energy_cost": energy,
                "disturbance": disturb,
                "safety_risk": safety,
                "final_score": round(final, 4),
                "reason": reason,
            }
        )

    # ------------------------------------------------------------------
    def evaluate(
        self,
        candidate_set: CandidateSet,
        *,
        external: ExternalContext | None = None,
        feedback: FeedbackStore | None = None,
    ) -> EvaluationResult:
        """후보들을 점수화하고 선택한다.

        feedback 이 있으면 점수화 전에 각 후보의 user_preference_fit 을 채운다.
        external.degraded 면 무행동을 고른다(보수적). 그 외엔 final_score 최대.
        """
        # 1) 선호 신호 주입(결정론 입력). 피드백이 없으면 중립값.
        primed: list[ActionCandidate] = []
        for ac in candidate_set.candidates:
            pref = (
                feedback.preference_signal(ac.candidate)
                if feedback is not None
                else NEUTRAL_SIGNAL
            )
            primed.append(ac.model_copy(update={"user_preference_fit": pref}))

        # 2) 점수화.
        scored = [self._score_one(ac, external) for ac in primed]

        # 3) 선택.
        noop_idx = next(
            (i for i, ac in enumerate(scored) if is_noop(ac.candidate)), len(scored) - 1
        )
        if external is not None and external.degraded:
            return EvaluationResult(
                scored=scored,
                selected_index=noop_idx,
                rationale="외부 데이터 불확실(degraded) → 보수적으로 무행동.",
            )

        best_idx = max(range(len(scored)), key=lambda i: scored[i].final_score)
        sel = scored[best_idx]
        if is_noop(sel.candidate):
            rationale = "어떤 행동도 무행동 기준선을 넘지 못함 → 침묵."
        else:
            rationale = (
                f"최고 점수 후보 선택: {sel.candidate.capability}."
                f"{sel.candidate.action} (score={sel.final_score})."
            )
        return EvaluationResult(scored=scored, selected_index=best_idx, rationale=rationale)
