"""LLM 후보 생성 백엔드 (개념 L4 Candidate Generation, LLM 보조).

LLM 의 역할이 '거부권(Gate2)'에서 '후보 제안·설명'으로 이동한 자리.
단 LLM 후보는 반드시 두 관문을 통과해야 후보 집합에 든다:
  1) pydantic 구조 검증(CandidateAction) — 깨진 출력은 폐기(fail-closed).
  2) validate_proactive_candidate — 안전 카테고리·화이트리스트 밖·금지 행동
     ·BREATH 상한 위반을 거부.
둘 중 하나라도 막히면 그 후보는 사라지고 무행동(NoOp)이 생존한다. 결정론적
가드레일이 최종 권위이며 LLM 은 위험도를 낮추거나 도구를 늘릴 수 없다.

이번 단계는 FakeCandidateLLM(결정론, 네트워크 없음)만 둔다. 실모델 후보생성
품질·프롬프트 튜닝은 후속(예정).
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any, Protocol, runtime_checkable

from pydantic import ValidationError

from liot.core.contracts import CandidateAction

__all__ = [
    "LLMCandidateBackend",
    "FakeCandidateLLM",
    "parse_llm_candidates",
]


@runtime_checkable
class LLMCandidateBackend(Protocol):
    """행동 후보를 제안하고 설명하는 LLM 백엔드의 공통 계약."""

    def propose_candidates(
        self, facts: Mapping[str, Any], cur: Mapping[str, Any]
    ) -> Sequence[Mapping[str, Any]]:
        """후보 행동(직렬화 dict)들을 제안한다. 구조 검증은 호출자가 한다."""
        ...

    def explain(self, candidate: CandidateAction, facts: Mapping[str, Any]) -> str:
        """선택 설명용 한 줄 사유(점수에 가중되지 않음, 외부 송신 금지)."""
        ...


def parse_llm_candidates(raw: Sequence[Mapping[str, Any]]) -> list[CandidateAction]:
    """LLM 출력(dict 들)을 CandidateAction 으로 검증·파싱.

    실패 항목은 조용히 폐기한다(fail-closed) — 깨진/악의적 후보가 통과하느니
    무행동이 생존하는 게 낫다.
    """
    out: list[CandidateAction] = []
    for item in raw:
        try:
            out.append(CandidateAction.model_validate(dict(item)))
        except (ValidationError, TypeError, ValueError):
            continue
    return out


class FakeCandidateLLM:
    """결정론적 LLM 후보 생성기(테스트·오프라인). 네트워크 없음.

    주입된 후보 dict 목록을 그대로 제안한다. 실모델 후보생성은 예정.
    """

    def __init__(
        self,
        candidates: Sequence[Mapping[str, Any]] | None = None,
        *,
        reason: str = "llm 제안",
    ) -> None:
        self._candidates = [dict(c) for c in (candidates or [])]
        self._reason = reason

    def propose_candidates(
        self, facts: Mapping[str, Any], cur: Mapping[str, Any]
    ) -> Sequence[Mapping[str, Any]]:
        return [dict(c) for c in self._candidates]

    def explain(self, candidate: CandidateAction, facts: Mapping[str, Any]) -> str:
        return self._reason
