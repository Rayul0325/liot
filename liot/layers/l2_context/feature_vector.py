"""선호 임베딩 (개념 ② 데이터 수집의 일부 — 개인화 축).

맥락×행동 셀맵 ``{(time_bucket, occupancy, capability:action) → 선호 0~1}`` 을
현재 맥락에 대해 조회해, 각 후보의 '선호 차원'을 만든다. 셀맵은 PreferenceStore
에 영속되며(scope="pref_embed"), 알림 응답(수락/거부/무시)으로 계속 갱신된다
(유튜브식 온라인 EMA + 시간감쇠).

조회(``FakePreferenceEmbedder``, 셀 없으면 중립 0.5)와 갱신 규칙(``apply_signal``
EMA · ``apply_decay`` 감쇠, 순수 함수)을 둔다. 갱신 호출은 서비스가 제안 해소 시
``LiotService._apply_pref_signal`` 에서 한다.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING, Any, Protocol, runtime_checkable

from liot.core.contracts import CandidateAction, is_noop

from .feedback_store import REJECTED_SIGNAL

if TYPE_CHECKING:  # pragma: no cover
    from .feedback_store import FeedbackStore
    from .preference_store import PreferenceStore

__all__ = [
    "PreferenceEmbedder",
    "FakePreferenceEmbedder",
    "EmbedderFeedbackStore",
    "cell_key",
    "apply_signal",
    "apply_decay",
    "load_cells",
    "save_cells",
    "NEUTRAL_PREF",
    "PREF_EMBED_SCOPE",
    "SIGNAL_TARGET",
    "EMA_ALPHA",
    "DECAY_RATE",
]

# 셀 초기값(이력 없음 = 중립). 영속 스코프.
NEUTRAL_PREF = 0.5
PREF_EMBED_SCOPE = "pref_embed"

# 알림 응답 신호 → 목표 선호도(EMA 가 이쪽으로 당긴다).
#   수락=강한 선호(1.0) · 거부=강한 비선호(0.0) · 무시/만료=약한 비선호(0.30).
SIGNAL_TARGET: dict[str, float] = {
    "accepted": 1.0,
    "dismissed": 0.0,
    "expired": 0.30,
    "ignored": 0.30,
}
EMA_ALPHA = 0.3  # 온라인 EMA 학습률
DECAY_RATE = 0.05  # 시간감쇠(스텝당 중립으로 당기는 비율)


def cell_key(time_bucket: str, occupancy: str, candidate: CandidateAction) -> str:
    """맥락×행동 셀 키. 같은 (시간대, 재실, 행동)이면 같은 셀을 가리킨다."""
    return f"{time_bucket}|{occupancy}|{candidate.capability}:{candidate.action}"


@runtime_checkable
class PreferenceEmbedder(Protocol):
    """후보의 맥락별 선호도(0~1)를 돌려주는 공급자."""

    def preference_for(
        self, candidate: CandidateAction, facts: Mapping[str, Any]
    ) -> float: ...


class FakePreferenceEmbedder:
    """결정론 선호 임베더. 영속 셀맵을 읽어 후보의 선호도를 돌려준다.

    cells: cell_key → 선호도(0~1). 없으면 중립(NEUTRAL_PREF).
    조회 전용. 갱신은 아래 ``apply_signal``/``apply_decay`` 가 셀맵을 새로 만들어 맡는다.
    """

    def __init__(self, cells: Mapping[str, float] | None = None) -> None:
        self._cells: dict[str, float] = dict(cells or {})

    def preference_for(
        self, candidate: CandidateAction, facts: Mapping[str, Any]
    ) -> float:
        tb = str(facts.get("time_bucket", ""))
        occ = str(facts.get("occupancy", ""))
        return self._cells.get(cell_key(tb, occ, candidate), NEUTRAL_PREF)


# ── 개인화 갱신(유튜브식 온라인 EMA + 시간감쇠) ────────────────────────


def apply_signal(
    cells: Mapping[str, float], key: str, signal: str, *, alpha: float | None = None
) -> dict[str, float]:
    """한 셀에 알림 응답 신호를 온라인 EMA 로 반영한다(수락↑/거부↓/무시 약↓).

    ``cell += α·(target − cell)``. 미지 신호는 무시(원본 유지). 순수 함수.
    ``alpha`` 는 연구용 재정의(None = ``EMA_ALPHA``, 출하값). 운영 경로는 넘기지 않는다.
    """
    target = SIGNAL_TARGET.get(signal)
    out = dict(cells)
    if target is None:
        return out
    cur = out.get(key, NEUTRAL_PREF)
    rate = EMA_ALPHA if alpha is None else alpha
    out[key] = round(cur + rate * (target - cur), 4)
    return out


def apply_decay(
    cells: Mapping[str, float],
    *,
    steps: int = 1,
    exclude: frozenset[str] = frozenset(),
    rate: float | None = None,
) -> dict[str, float]:
    """갱신 없는 셀을 중립(0.5)으로 시간감쇠한다(steps 스텝).

    ``cell = 0.5 + (cell−0.5)·(1−λ)^steps``. exclude 의 셀은 그대로 둔다
    (이번에 방금 갱신된 셀). 최근 취향이 우세, 옛 취향은 서서히 망각.
    ``rate`` 는 연구용 재정의(None = ``DECAY_RATE``, 출하값). 운영 경로는 넘기지 않는다.
    """
    if steps <= 0:
        return dict(cells)
    keep = (1.0 - (DECAY_RATE if rate is None else rate)) ** steps
    out: dict[str, float] = {}
    for k, v in cells.items():
        if k in exclude:
            out[k] = v
        else:
            out[k] = round(NEUTRAL_PREF + (v - NEUTRAL_PREF) * keep, 4)
    return out


async def load_cells(store: PreferenceStore) -> dict[str, float]:
    """PreferenceStore(scope=pref_embed)에서 셀맵을 로드한다(없으면 빈)."""
    data = await store.get("cells", {}, scope=PREF_EMBED_SCOPE)
    if not isinstance(data, dict):
        return {}
    out: dict[str, float] = {}
    for k, v in data.items():
        try:
            out[str(k)] = float(v)
        except (TypeError, ValueError):
            continue
    return out


async def save_cells(
    store: PreferenceStore, cells: Mapping[str, float], *, updated_at: str = ""
) -> None:
    """셀맵을 PreferenceStore(scope=pref_embed)에 영속한다."""
    await store.set(
        "cells", {str(k): float(v) for k, v in cells.items()},
        scope=PREF_EMBED_SCOPE,
        updated_at=updated_at,
    )


class EmbedderFeedbackStore:
    """임베딩 셀을 evaluator 의 선호 신호(user_preference_fit)로 공급하는 어댑터.

    현재 맥락(facts)의 셀 값을 돌려줘 **선택을 개인화**한다. 단 base(거절 이력)
    가 강한 거절(<=REJECTED_SIGNAL)이면 그걸 우선한다 — 방금 거절한 류를
    임베딩이 되살리지 못하게(안전한 보수). NoOp 는 항상 중립.

    안전: 이 신호는 evaluator 의 PREF 항(가중 0.30)만 움직이며, safety_risk 는
    여전히 카탈로그 결정론이라 개인화가 안전 하한을 못 낮춘다(불변식 2′).
    """

    def __init__(
        self,
        embedder: PreferenceEmbedder,
        facts: Mapping[str, Any],
        *,
        base: FeedbackStore | None = None,
    ) -> None:
        self._embedder = embedder
        self._facts = dict(facts)
        self._base = base

    def preference_signal(self, candidate: CandidateAction) -> float:
        if is_noop(candidate):
            return NEUTRAL_PREF
        if self._base is not None:
            b = self._base.preference_signal(candidate)
            if b <= REJECTED_SIGNAL:  # 최근 거절 → 강한 감점 우선(임베딩보다)
                return b
        return self._embedder.preference_for(candidate, self._facts)
