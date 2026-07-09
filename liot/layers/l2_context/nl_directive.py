"""자연어 지시 오버라이드 (대화형).

사용자가 채팅/음성으로 준 지시("오늘 밤은 불 계속 켜놔줘")를 구조화 directive 로
파싱해, 자율 파이프라인의 제안을 억제/제약한다(기존 LLM-IoT 자연어 규칙 개념).

안전 경계: directive 는 **편의(comfort) 후보만** 억제할 수 있다. 안전 임계
카테고리·EMBER 는 어떤 지시로도 억제되지 않는다(사용자 지시가 안전을 못 낮춤).

Phase 0 은 타입 + Protocol + Fake 스텁(빈 목록)만 둔다. 결정론 키워드 파싱·
억제 게이트·/api/directive 엔드포인트·음성(STT/TTS)은 Phase 5.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol, runtime_checkable

__all__ = ["UserDirective", "DirectiveParser", "FakeDirectiveParser"]


@dataclass(frozen=True)
class UserDirective:
    """구조화된 사용자 지시.

    constraint: keep_on(계속 켜) | keep_off(계속 꺼) | forbid(하지마) | prefer(선호).
    until: ISO 시각/기간 또는 "" (세션/무기한). source: chat | voice.
    """

    capability: str
    entity_id: str = ""
    constraint: str = "prefer"
    until: str = ""
    source: str = "chat"
    raw_text: str = ""


@runtime_checkable
class DirectiveParser(Protocol):
    """자연어 텍스트를 구조화 지시 목록으로 파싱하는 공급자."""

    def parse(self, text: str, *, source: str = "chat") -> list[UserDirective]: ...


class FakeDirectiveParser:
    """결정론 자연어 파서(네트워크 없음).

    Phase 0 스텁: 빈 목록. Phase 5 에서 간단한 키워드 규칙(불/켜/꺼/밤 …)으로
    채우고, 실 LLM 파싱 슬롯을 둔다.
    """

    def parse(self, text: str, *, source: str = "chat") -> list[UserDirective]:
        return []
