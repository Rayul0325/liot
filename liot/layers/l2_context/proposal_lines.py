"""2갈래 제안 라인 (개념 ③ Proposal Lines).

한 맥락에서 두 독립 엔진이 각각 행동 후보 + 자신감(가중치)을 낸다.

  Line A (ML)  : 선호 임베딩 + 전체 값 → 규칙 후보 + EvalWeights 휴리스틱(재사용).
  Line B (LLM) : 선호 임베딩 LLM 친화 변환 + 센서값 파싱 + available_actions →
                 LLM 후보(무엇을 조작할 수 있는지 목록을 받아 제안).

두 라인의 모든 후보는 반드시 ``validate_proactive_candidate`` 를 통과해야 병합
후보풀에 든다(비선택 후보도 이 게이트에만 의존 — 안전 경계). 라인 자신감은
``ActionCandidate.origin`` + ``line_confidence`` 에 실려 텔레메트리/타이브레이크로
만 쓰인다(final_score 미개입 → 결정론 아비터 = evaluator 무변경).

Phase 0 은 Protocol + Fake 스텁(빈 목록)만 둔다. 실제 후보 생성·검증·병합은
Phase 1.
"""

from __future__ import annotations

from typing import Any, Mapping, Protocol, Sequence, runtime_checkable

from liot.core.contracts import ActionCandidate

from .feature_vector import PreferenceEmbedder

__all__ = ["ProposalLine", "MLProposalLine", "LLMProposalLine"]


@runtime_checkable
class ProposalLine(Protocol):
    """행동 후보 + 자신감을 내는 제안 엔진의 공통 계약."""

    name: str

    def propose(
        self,
        facts: Mapping[str, Any],
        cur: Mapping[str, Any],
        *,
        embedder: PreferenceEmbedder | None = None,
        available_actions: Sequence[Mapping[str, Any]] | None = None,
    ) -> list[ActionCandidate]: ...


class MLProposalLine:
    """Line A · ML(결정론 휴리스틱) 제안.

    Phase 0 스텁: 빈 목록. Phase 1 에서 기존 규칙 생성(ProactiveGenerator) +
    EvalWeights 휴리스틱으로 후보를 채우고, 각 후보에 origin="line_a:ml" 를 단다.
    """

    name = "line_a:ml"

    def propose(
        self,
        facts: Mapping[str, Any],
        cur: Mapping[str, Any],
        *,
        embedder: PreferenceEmbedder | None = None,
        available_actions: Sequence[Mapping[str, Any]] | None = None,
    ) -> list[ActionCandidate]:
        return []


class LLMProposalLine:
    """Line B · LLM 제안(Fake, 네트워크 없음).

    Phase 0 스텁: 빈 목록. Phase 1 에서 available_actions 를 소비한 결정론 후보로
    채우고, 각 후보에 origin="line_b:llm" 를 단다. 실모델 LLM 슬롯은 예정.
    """

    name = "line_b:llm"

    def propose(
        self,
        facts: Mapping[str, Any],
        cur: Mapping[str, Any],
        *,
        embedder: PreferenceEmbedder | None = None,
        available_actions: Sequence[Mapping[str, Any]] | None = None,
    ) -> list[ActionCandidate]:
        return []
