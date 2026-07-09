"""트리거 감지 계층 (개념 ① Trigger Detection).

파이프라인을 시작할지(이벤트가 발생했는지) 결정한다. 규칙 기반 감지가 상시
돌고, LLM 기반 감지는 저빈도 옵트인이다.

Phase 0 은 인터페이스 + 결정론 스텁만 둔다 — 동작을 바꾸지 않는다(규칙 감지는
항상 fire, LLM 감지는 기본 off). 실제 게이팅 배선(should_snapshot/propose
재사용, tick 진입부 pre-bus 게이트)은 Phase 4.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping, Protocol, runtime_checkable

__all__ = [
    "TriggerDecision",
    "EventDetector",
    "RuleEventDetector",
    "FakeLLMEventDetector",
    "build_event_detector",
]


@dataclass(frozen=True)
class TriggerDecision:
    """파이프라인 시작 여부 결정. fire=False 면 이번 틱은 침묵(무진행)."""

    fire: bool
    reason: str = ""
    source: str = "rule"  # "rule" | "llm"


@runtime_checkable
class EventDetector(Protocol):
    """이벤트 발생을 감지해 파이프라인 시작 여부를 돌려주는 공급자."""

    source: str

    def detect(
        self, cur: Mapping[str, Any], prev: Mapping[str, Any] | None = None
    ) -> TriggerDecision: ...


class RuleEventDetector:
    """규칙 기반 감지(상시). Phase 4 에서 should_snapshot/propose 게이팅에 위임한다.

    Phase 0 스텁: 항상 fire → 기존 동작(모든 틱 진행)을 그대로 보존.
    """

    source = "rule"

    def detect(
        self, cur: Mapping[str, Any], prev: Mapping[str, Any] | None = None
    ) -> TriggerDecision:
        return TriggerDecision(fire=True, reason="stub:always-fire", source="rule")


class FakeLLMEventDetector:
    """LLM 기반 감지 Fake(결정론, 네트워크 없음). 저빈도 옵트인.

    Phase 0 스텁: 기본 비활성(fire=False) → 규칙 감지에 위임. 실 LLM 감지는 예정.
    """

    source = "llm"

    def __init__(self, enabled: bool = False) -> None:
        self._enabled = enabled

    def detect(
        self, cur: Mapping[str, Any], prev: Mapping[str, Any] | None = None
    ) -> TriggerDecision:
        return TriggerDecision(fire=False, reason="stub:llm-optin-off", source="llm")


def build_event_detector() -> EventDetector:
    """env 게이트로 감지기를 고른다. 기본은 규칙 감지(상시). 실 LLM 감지는 예정."""
    return RuleEventDetector()
