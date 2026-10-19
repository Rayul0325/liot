"""상황 파급도(Severity) 값 모델.

행동 확인 강도(``RiskTier``)와 별개의 축이다. ``RiskTier`` 는 "이 행동을 실행하기
전에 얼마나 세게 확인받나"이고, 파급도는 "이 상황을 아무도 손대지 않으면 얼마나
큰 피해가 나나"다. 둘을 한 값으로 섞지 않는다.

파급도 4단계(오름차순):
    RECORD(0, 기록만) < NOTICE(1, 주의) < WARNING(2, 경고) < CRITICAL(3, 위험)

핵심 불변식:
  - 파급도도 자동 승격만 허용: ``escalate_severity(cur, target) == max(cur, target)``.
  - 표+거주자 값(severity_floor) 아래로는 어떤 모델·지시도 내리지 못한다.

표·매핑·계산기는 이 모듈에 이어서 붙는다(파급도 표는 설정 파일).
"""

from __future__ import annotations

from enum import IntEnum
from typing import Literal

__all__ = [
    "SeverityLevel",
    "SeveritySource",
    "SEVERITY_SOURCE_ORDER",
    "escalate_severity",
]

# 파급도가 어디서 왔는지. 기록 순서는 SEVERITY_SOURCE_ORDER 를 따른다.
SeveritySource = Literal["table", "resident", "model"]
SEVERITY_SOURCE_ORDER: tuple[SeveritySource, ...] = ("table", "resident", "model")


class SeverityLevel(IntEnum):
    """상황 파급도. IntEnum 이므로 정수 비교로 대소를 비교한다."""

    RECORD = 0
    NOTICE = 1
    WARNING = 2
    CRITICAL = 3

    @classmethod
    def from_str(cls, value: str | SeverityLevel | int) -> SeverityLevel:
        """문자열/정수/SeverityLevel 을 SeverityLevel 로 정규화한다.

        대소문자와 양옆 공백을 무시한다. 알 수 없는 값은 ``ValueError``.
        """
        if isinstance(value, SeverityLevel):
            return value
        if isinstance(value, int):
            return cls(value)
        key = str(value).strip().upper()
        try:
            return cls[key]
        except KeyError as exc:
            raise ValueError(f"unknown severity level: {value!r}") from exc

    @property
    def label_ko(self) -> str:
        """한국어 라벨."""
        return _LABEL_KO[self]

    @property
    def ntfy_priority(self) -> str:
        """이 파급도의 ntfy 푸시 세기(문자열 '2'..'5')."""
        return _NTFY_PRIORITY[self]


_LABEL_KO: dict[SeverityLevel, str] = {
    SeverityLevel.RECORD: "기록만",
    SeverityLevel.NOTICE: "주의",
    SeverityLevel.WARNING: "경고",
    SeverityLevel.CRITICAL: "위험",
}

_NTFY_PRIORITY: dict[SeverityLevel, str] = {
    SeverityLevel.RECORD: "2",
    SeverityLevel.NOTICE: "3",
    SeverityLevel.WARNING: "4",
    SeverityLevel.CRITICAL: "5",
}


def escalate_severity(
    cur: SeverityLevel | str | int, target: SeverityLevel | str | int
) -> SeverityLevel:
    """파급도는 자동 승격만 가능하다. 결과 = max(cur, target)."""
    cur_s = SeverityLevel.from_str(cur)
    target_s = SeverityLevel.from_str(target)
    return cur_s if cur_s >= target_s else target_s
