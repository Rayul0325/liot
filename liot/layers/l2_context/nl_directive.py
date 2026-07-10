"""자연어 지시 오버라이드 (대화형).

사용자가 채팅/음성으로 준 지시("오늘 밤은 불 계속 켜놔줘")를 구조화 directive 로
파싱해, 자율 파이프라인의 제안을 억제/제약한다(기존 LLM-IoT 자연어 규칙 개념).

안전 경계: directive 는 **편의(comfort) 후보만** 억제할 수 있다. 안전 임계
카테고리·EMBER 는 어떤 지시로도 억제되지 않는다(사용자 지시가 안전을 못 낮춤).
자율 후보는 이미 비-안전 컴포트로 한정(불변식 7)되지만, 억제 게이트도 안전
카테고리를 명시적으로 건너뛴다(이중 방어).

v1 파서는 결정론 키워드 규칙(네트워크 없음); 실 LLM 파싱은 예정(env 슬롯).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol, Sequence, runtime_checkable

from liot.core.contracts import CandidateAction, CandidateSet, is_noop
from liot.core.risk import is_safety_category

__all__ = [
    "UserDirective",
    "DirectiveParser",
    "FakeDirectiveParser",
    "DirectiveStore",
    "is_suppressed",
    "apply_directives",
]


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


# 키워드(결정론 파싱). 실 LLM 파싱으로 대체 가능(예정).
_LIGHT_WORDS = ("불", "조명", "등", "라이트", "light")
_ON_WORDS = ("켜", "유지", "keep on", "on")
_OFF_WORDS = ("꺼", "끄", "off")


@runtime_checkable
class DirectiveParser(Protocol):
    """자연어 텍스트를 구조화 지시 목록으로 파싱하는 공급자."""

    def parse(self, text: str, *, source: str = "chat") -> list[UserDirective]: ...


class FakeDirectiveParser:
    """결정론 자연어 파서(네트워크 없음). 키워드 규칙 기반.

    예) "오늘 밤은 불 계속 켜놔줘" → keep_on(light). 실 LLM 파싱은 예정.
    """

    def parse(self, text: str, *, source: str = "chat") -> list[UserDirective]:
        if not text or not text.strip():
            return []
        low = text.lower()
        out: list[UserDirective] = []
        if any(w in text or w in low for w in _LIGHT_WORDS):
            has_on = any(w in text or w in low for w in _ON_WORDS)
            has_off = any(w in text or w in low for w in _OFF_WORDS)
            # "꺼" 계열이 있으면 keep_off, 아니면 켜기/유지 계열이면 keep_on.
            if has_off and not has_on:
                out.append(
                    UserDirective(
                        capability="light",
                        constraint="keep_off",
                        source=source,
                        raw_text=text,
                    )
                )
            elif has_on:
                out.append(
                    UserDirective(
                        capability="light",
                        constraint="keep_on",
                        source=source,
                        raw_text=text,
                    )
                )
        return out


class DirectiveStore:
    """활성 지시 목록(세션 스코프, in-memory). 파싱 결과를 누적/조회/초기화한다.

    v1 은 시간 만료를 강제하지 않는다(세션 동안 유지, reset 로 해제). until 기반
    만료는 후속. 데모/서비스가 한 인스턴스를 공유해 tick 에 주입한다.
    """

    def __init__(self, parser: DirectiveParser | None = None) -> None:
        self._parser: DirectiveParser = parser or FakeDirectiveParser()
        self._active: list[UserDirective] = []

    def add_text(self, text: str, *, source: str = "chat") -> list[UserDirective]:
        parsed = self._parser.parse(text, source=source)
        self._active.extend(parsed)
        return parsed

    def active(self) -> list[UserDirective]:
        return list(self._active)

    def clear(self) -> None:
        self._active.clear()


def is_suppressed(
    candidate: CandidateAction, directives: Sequence[UserDirective]
) -> str | None:
    """활성 지시가 이 후보를 억제하면 사유를, 아니면 None.

    안전 임계 카테고리는 **절대 억제하지 않는다**(사용자 지시가 안전을 못 낮춤).
    """
    if is_safety_category(candidate.category):
        return None
    action = (candidate.action or "").strip().lower()
    for d in directives:
        if d.capability and d.capability != candidate.capability:
            continue
        if d.entity_id and d.entity_id != candidate.entity_id:
            continue
        if d.constraint == "keep_on" and action in ("turn_off", "off", "close"):
            return f"directive:keep_on:{d.capability or '*'}"
        if d.constraint == "keep_off" and action in ("turn_on", "on", "open"):
            return f"directive:keep_off:{d.capability or '*'}"
        if d.constraint == "forbid":
            return f"directive:forbid:{d.capability or '*'}"
    return None


def apply_directives(
    candidate_set: CandidateSet, directives: Sequence[UserDirective]
) -> CandidateSet:
    """활성 지시로 억제되는 comfort 후보를 후보풀에서 제거한다(NoOp 유지).

    모든 comfort 후보가 억제되면 NoOp 만 남아 침묵한다("불 켜놔줘" → 자율 소등
    안 함). 안전 후보는 억제 대상이 아니다.
    """
    if not directives:
        return candidate_set
    kept = [
        c
        for c in candidate_set.candidates
        if is_noop(c.candidate) or is_suppressed(c.candidate, directives) is None
    ]
    noop_idx = next(i for i, c in enumerate(kept) if is_noop(c.candidate))
    return CandidateSet(
        trace_id=candidate_set.trace_id,
        home_id=candidate_set.home_id,
        snapshot_hash=candidate_set.snapshot_hash,
        candidates=kept,
        selected_index=noop_idx,
    )
