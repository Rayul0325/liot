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

from collections.abc import Sequence
from dataclasses import dataclass, replace
from datetime import datetime
from typing import Protocol, runtime_checkable

from liot.core.clock import Clock
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
    until: ISO8601 시각(그 시각부터 만료) 또는 "" (세션/무기한). tz 없는 시각은
        UTC 로 본다. 기간 표기("PT2H" 등)는 받지 않는다. source: chat | voice.
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


def _until_dt(until: str) -> datetime:
    """until 문자열 → tz-aware UTC datetime. 형식이 틀리면 ValueError."""
    return Clock(fixed=until).now_dt()


class DirectiveStore:
    """활성 지시 목록(세션 스코프, in-memory). 파싱 결과를 누적/조회/초기화한다.

    ``until`` 이 있는 지시는 그 시각이 되면 만료돼 ``active()`` 에서 빠진다. 빈
    ``until`` 은 세션 동안 유지된다(reset 로 해제). 만료 판정 시각은 호출자가
    ``now`` 로 고를 수 있고, 없으면 주입된 Clock(기본 실시간)을 쓴다 — 데모의
    시뮬레이션 시각과 실시간 중 무엇이 기준인지는 호출자의 사정이기 때문이다.

    ``active()`` 는 만료분을 걸러 보여줄 뿐 목록을 지우지 않는다. 조회가 목록을
    재할당하면 동시에 들어온 ``add_text`` 가 유실될 수 있어서다.
    데모/서비스가 한 인스턴스를 공유해 tick 에 주입한다.
    """

    def __init__(
        self, parser: DirectiveParser | None = None, *, clock: Clock | None = None
    ) -> None:
        self._parser: DirectiveParser = parser or FakeDirectiveParser()
        self._clock = clock or Clock()
        self._active: list[UserDirective] = []

    def add_text(
        self, text: str, *, source: str = "chat", until: str = ""
    ) -> list[UserDirective]:
        """파싱한 지시를 누적한다. until 형식이 틀리면 아무것도 저장하지 않고 ValueError."""
        until = (until or "").strip()
        if until:
            _until_dt(until)  # 저장 전에 형식 검증 — 만료 불가 지시가 끼어들지 않게
        parsed = self._parser.parse(text, source=source)
        if until:
            parsed = [replace(d, until=until) for d in parsed]
        self._active.extend(parsed)
        return parsed

    def active(self, now: datetime | str | None = None) -> list[UserDirective]:
        at = self._clock.now_dt() if now is None else Clock(fixed=now).now_dt()
        return [d for d in self._active if not d.until or at < _until_dt(d.until)]

    def all(self) -> list[UserDirective]:
        """만료 여부를 따지지 않은 저장 목록의 사본(조회 전용).

        어느 시각을 기준으로 만료를 볼지 정할 수 없는 호출자용이다 — 예컨대 데모의
        지시 등록 응답은 시뮬레이션 시각을 모르므로, 실시간으로 거르면 시뮬레이션
        안에서는 아직 유효한 지시까지 만료로 보이게 된다.
        """
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
