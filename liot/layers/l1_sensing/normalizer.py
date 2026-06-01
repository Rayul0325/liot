"""원시 센서 값 정규화.

Home Assistant 등에서 들어오는 raw_value 는 타입·표기가 제각각이다.
normalizer 는 이를 결정론적으로 norm_value 로 변환한다:

  - 온/습도 등 수치: float 로 강제(문자 단위 'C','%' 등 제거).
  - 불리언/상태 문자열: 'on'/'off', 'open'/'closed', 'true'/'false',
    'detected'/'clear', 'locked'/'unlocked' 등을 표준 bool 로.
  - 그 외 문자열: strip + 소문자 정규화(식별자 안정화).

raw 값 그 자체는 SensorReading.raw_value 로 보존하고, 정규화 결과만
norm_value 에 담는다(재현·감사 가능성).
"""

from __future__ import annotations

import re
from typing import Any

__all__ = ["Normalizer", "normalize"]

# 참(True) 으로 간주하는 상태 토큰.
_TRUE_TOKENS: frozenset[str] = frozenset(
    {
        "on",
        "true",
        "1",
        "yes",
        "open",
        "opened",
        "detected",
        "motion",
        "present",
        "home",
        "wet",
        "unlocked",
        "active",
    }
)
# 거짓(False) 으로 간주하는 상태 토큰.
_FALSE_TOKENS: frozenset[str] = frozenset(
    {
        "off",
        "false",
        "0",
        "no",
        "closed",
        "close",
        "clear",
        "no_motion",
        "absent",
        "away",
        "dry",
        "locked",
        "idle",
        "inactive",
    }
)

# 수치 뒤에 흔히 붙는 단위/기호(제거 대상).
_NUMERIC_RE = re.compile(r"^\s*([-+]?\d+(?:\.\d+)?)\s*[a-zA-Z%°℃℉/]*\s*$")


def _to_number(text: str) -> float | None:
    """문자열에서 선두 수치를 추출한다. 수치가 아니면 None."""
    m = _NUMERIC_RE.match(text)
    if not m:
        return None
    try:
        return float(m.group(1))
    except ValueError:  # pragma: no cover - 방어적
        return None


def normalize(raw_value: Any) -> Any:
    """raw_value 를 결정론적 norm_value 로 변환한다.

    규칙(우선순위):
      1) 이미 bool 이면 그대로.
      2) int/float 이면 float 로(불리언 제외).
      3) 문자열:
         - 참/거짓 상태 토큰 -> bool
         - 선두가 수치면 -> float
         - 그 외 -> strip + 소문자.
      4) None/기타 -> 그대로.
    """
    if isinstance(raw_value, bool):
        return raw_value
    if isinstance(raw_value, (int, float)):
        return float(raw_value)
    if isinstance(raw_value, str):
        token = raw_value.strip().lower()
        if token in _TRUE_TOKENS:
            return True
        if token in _FALSE_TOKENS:
            return False
        num = _to_number(raw_value)
        if num is not None:
            return num
        return token
    return raw_value


class Normalizer:
    """정규화기. 현재는 무상태이며 ``normalize`` 의 객체 래퍼다.

    엔티티별 보정(스케일·오프셋)이 필요해지면 여기에 상태를 둔다.
    """

    def normalize(self, raw_value: Any) -> Any:
        """raw_value -> norm_value (모듈 함수 normalize 위임)."""
        return normalize(raw_value)
