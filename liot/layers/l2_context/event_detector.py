"""트리거 감지 계층 (개념 ① Trigger Detection).

파이프라인을 시작할지(이벤트가 발생했는지) 결정한다 — 능동(tick) 진입부의
pre-bus 게이트다(새 버스 엣지 없음). 두 트랙:

  - 규칙 기반 감지(상시, 결정론): 관측된 상태가 있으면 파이프라인을 연다.
    실제 행동 게이팅(budget/cooldown/규칙 매칭)은 그 뒤 ``propose()`` 가 한다 —
    감지기는 "볼 가치가 있나"만, propose 는 "무엇을 발의하나"를 나눠 맡는다.
  - LLM 기반 감지(저빈도 옵트인, 기본 off): 환경변수로 켜는 Fake. 실 LLM 감지는
    예정(env 슬롯). 상시 LLM 폴링은 하지 않는다(비용·자율루프 규율).

감지기는 tick 내부 Python 로직이라 ``ALLOWED_EDGES`` 를 건드리지 않는다(불변식 1).
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any, Mapping, Protocol, Sequence, runtime_checkable

__all__ = [
    "TriggerDecision",
    "EventDetector",
    "RuleEventDetector",
    "FakeLLMEventDetector",
    "CompositeEventDetector",
    "build_event_detector",
]


@dataclass(frozen=True)
class TriggerDecision:
    """파이프라인 시작 여부 결정. fire=False 면 이번 틱은 침묵(무진행)."""

    fire: bool
    reason: str = ""
    source: str = "rule"  # "rule" | "llm" | "composite"


@runtime_checkable
class EventDetector(Protocol):
    """이벤트 발생을 감지해 파이프라인 시작 여부를 돌려주는 공급자."""

    source: str

    def detect(
        self, cur: Mapping[str, Any], prev: Mapping[str, Any] | None = None
    ) -> TriggerDecision: ...


class RuleEventDetector:
    """규칙 기반 감지(상시, 결정론).

    관측된 상태가 하나라도 있으면 파이프라인을 연다. 실제 행동 게이팅
    (budget/cooldown/규칙 매칭)은 뒤이은 ``propose()`` 의 몫 — 여기서 회계를
    대신하지 않는다(propose 는 정확히 1회 실행돼야 한다).
    """

    source = "rule"

    def detect(
        self, cur: Mapping[str, Any], prev: Mapping[str, Any] | None = None
    ) -> TriggerDecision:
        if not cur:
            return TriggerDecision(False, "no-observed-state", "rule")
        return TriggerDecision(True, "observed-state", "rule")


class FakeLLMEventDetector:
    """LLM 기반 감지 Fake(결정론, 네트워크 없음). 저빈도 옵트인.

    enabled=False(기본)면 절대 발화하지 않아 규칙 감지에 전적으로 위임한다.
    켜지면 관측 상태가 있을 때 발화하는 Fake — 실 LLM 감지(맥락 해석)는 예정.
    """

    source = "llm"

    def __init__(self, enabled: bool = False) -> None:
        self._enabled = enabled

    def detect(
        self, cur: Mapping[str, Any], prev: Mapping[str, Any] | None = None
    ) -> TriggerDecision:
        if not self._enabled:
            return TriggerDecision(False, "llm-optin-off", "llm")
        if not cur:
            return TriggerDecision(False, "no-observed-state", "llm")
        return TriggerDecision(True, "llm-fake-observed", "llm")


class CompositeEventDetector:
    """여러 감지기를 합친다 — 하나라도 발화하면 파이프라인을 연다(OR)."""

    source = "composite"

    def __init__(self, detectors: Sequence[EventDetector]) -> None:
        self._detectors = list(detectors)

    def detect(
        self, cur: Mapping[str, Any], prev: Mapping[str, Any] | None = None
    ) -> TriggerDecision:
        reasons: list[str] = []
        for d in self._detectors:
            r = d.detect(cur, prev)
            if r.fire:
                return TriggerDecision(True, r.reason, r.source)
            reasons.append(f"{r.source}:{r.reason}")
        return TriggerDecision(False, " · ".join(reasons), "composite")


def build_event_detector() -> EventDetector:
    """env 로 감지기를 조립한다. 규칙 감지는 상시, LLM 감지는 옵트인(기본 off).

    ``LIOT_EVENT_LLM`` 이 truthy 면 FakeLLMEventDetector 를 함께 켠다.
    """
    detectors: list[EventDetector] = [RuleEventDetector()]
    if os.environ.get("LIOT_EVENT_LLM", "").strip().lower() in ("1", "true", "on", "yes"):
        detectors.append(FakeLLMEventDetector(enabled=True))
    return CompositeEventDetector(detectors)
