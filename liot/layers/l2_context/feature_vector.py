"""선호 임베딩 (개념 ② 데이터 수집의 일부 — 개인화 축).

맥락×행동 셀맵 ``{(time_bucket, occupancy, capability:action) → 선호 0~1}`` 을
현재 맥락에 대해 조회해, 각 후보의 '선호 차원'을 만든다. 셀맵은 PreferenceStore
에 영속되며(scope="pref_embed"), 알림 응답(수락/거부/무시)으로 계속 갱신된다
(유튜브식 온라인 EMA + 시간감쇠).

Phase 0 은 표현 + 인터페이스 + 조회 스텁(셀맵 없으면 중립 0.5)만 둔다.
갱신 규칙(EMA+감쇠)과 _refresh_feedback 훅은 Phase 3.
"""

from __future__ import annotations

from typing import Any, Mapping, Protocol, runtime_checkable

from liot.core.contracts import CandidateAction

__all__ = [
    "PreferenceEmbedder",
    "FakePreferenceEmbedder",
    "cell_key",
    "NEUTRAL_PREF",
    "PREF_EMBED_SCOPE",
]

# 셀 초기값(이력 없음 = 중립). 영속 스코프.
NEUTRAL_PREF = 0.5
PREF_EMBED_SCOPE = "pref_embed"


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
    Phase 0 스텁: 조회만. 갱신(EMA+감쇠)은 Phase 3 에서 이 모듈에 추가한다.
    """

    def __init__(self, cells: Mapping[str, float] | None = None) -> None:
        self._cells: dict[str, float] = dict(cells or {})

    def preference_for(
        self, candidate: CandidateAction, facts: Mapping[str, Any]
    ) -> float:
        tb = str(facts.get("time_bucket", ""))
        occ = str(facts.get("occupancy", ""))
        return self._cells.get(cell_key(tb, occ, candidate), NEUTRAL_PREF)
