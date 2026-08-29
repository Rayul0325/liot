"""사용자 반응 기록 → 선호 신호 (개념 L3 Context Acquisition 일부).

평가기(CandidateEvaluator)가 후보의 ``user_preference_fit`` 을 산정할 때 쓰는
선호 신호를 공급한다. 0(최근 거절) ~ 1(선호) 범위.

설계: L2 가 L4(proposal_service)를 직접 임포트하지 않도록, 비동기 DB 조회는
바깥(서비스 계층)이 수행해 **스냅샷**(거절 키 집합 + origin별 수용률)을 만들고
그것으로 동기 InMemoryFeedbackStore 를 구성한다. 평가기는 동기·결정론을 유지.

키 형식은 proposal_service.recently_rejected_keys 와 동일:
``"{capability}:{action}:{entity_id}:{category}"``.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Protocol, runtime_checkable

from liot.core.contracts import CandidateAction, is_noop

__all__ = [
    "FeedbackStore",
    "InMemoryFeedbackStore",
    "candidate_key",
    "REJECTED_SIGNAL",
    "NEUTRAL_SIGNAL",
]

# 최근 거절된 행동의 선호 신호(강한 감점). 중립은 이력이 없을 때의 기본값.
REJECTED_SIGNAL = 0.1
NEUTRAL_SIGNAL = 0.6


def candidate_key(candidate: CandidateAction) -> str:
    """후보의 dedup 키. recently_rejected_keys 의 키 형식과 일치."""
    return (
        f"{candidate.capability}:{candidate.action}:"
        f"{candidate.entity_id}:{candidate.category}"
    )


@runtime_checkable
class FeedbackStore(Protocol):
    """후보별 선호 신호(0~1)를 돌려주는 공급자."""

    def preference_signal(self, candidate: CandidateAction) -> float:
        """0(최근 거절) ~ 1(선호). 이력 없으면 중립."""
        ...


class InMemoryFeedbackStore:
    """스냅샷 기반 동기 선호 신호.

    rejected_keys: 최근 거절된 (cap:action:entity:category) 키 집합.
    accept_rate: origin('proactive'/'explicit'/'safety') → 수용률(0~1) — 있으면
                 중립값을 그 수용률 쪽으로 살짝 당긴다(없으면 중립 고정).
    """

    def __init__(
        self,
        *,
        rejected_keys: frozenset[str] | None = None,
        accept_rate: Mapping[str, float] | None = None,
    ) -> None:
        self._rejected = frozenset(rejected_keys or frozenset())
        self._accept_rate = dict(accept_rate or {})

    def preference_signal(self, candidate: CandidateAction) -> float:
        if is_noop(candidate):
            return NEUTRAL_SIGNAL
        if candidate_key(candidate) in self._rejected:
            return REJECTED_SIGNAL
        # origin 별 수용률이 있으면 중립과 절충(가벼운 가중).
        origin_raw = str(candidate.params.get("__origin__", "")).split(":", 1)[0]
        rate = self._accept_rate.get(origin_raw)
        if rate is None:
            return NEUTRAL_SIGNAL
        return max(0.0, min(1.0, 0.5 * NEUTRAL_SIGNAL + 0.5 * rate))
