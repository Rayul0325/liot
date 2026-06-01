"""위험도(Risk) 모델.

위험도 4단계(심각도 오름차순):
    AMBIENT(0) < BREATH(1) < DUSK(2) < EMBER(3)

핵심 불변식:
  - 위험도 자동 승격만 허용, 강등 불가: ``escalate_only(cur, target) == max(cur, target)``.
  - 안전 임계 카테고리(SAFETY_CRITICAL_CATEGORIES)는 LLM 가용성과 무관하게
    Gate1 결정론만으로 즉시 EMBER 제안을 만들어야 한다(Safety-Never-Silent).
"""

from __future__ import annotations

from enum import IntEnum

__all__ = [
    "RiskTier",
    "escalate_only",
    "SAFETY_CRITICAL_CATEGORIES",
]


class RiskTier(IntEnum):
    """위험도 단계. IntEnum 이므로 정수 비교로 심각도를 직접 비교할 수 있다.

    값이 클수록 심각하다: AMBIENT(0) < BREATH(1) < DUSK(2) < EMBER(3).
    """

    AMBIENT = 0
    BREATH = 1
    DUSK = 2
    EMBER = 3

    @classmethod
    def from_str(cls, value: str | "RiskTier" | int) -> "RiskTier":
        """문자열/정수/RiskTier 를 RiskTier 로 정규화한다.

        대소문자와 양옆 공백을 무시한다. 알 수 없는 값은 ``ValueError``.
        """
        if isinstance(value, RiskTier):
            return value
        if isinstance(value, int):
            return cls(value)
        key = str(value).strip().lower()
        try:
            return cls[key.upper()]
        except KeyError as exc:  # pragma: no cover - 방어적
            raise ValueError(f"unknown risk tier: {value!r}") from exc

    @property
    def label_ko(self) -> str:
        """한국어 라벨."""
        return _LABEL_KO[self]

    @property
    def glyph(self) -> str:
        """단계별 글리프. AMBIENT='●', BREATH='✓', DUSK='?', EMBER='🛡'."""
        return _GLYPH[self]

    @property
    def required_confirmation(self) -> str:
        """이 위험도에서 필요한 사용자 확인 강도.

        'none' < 'light' < 'explicit' < 'double'.
        """
        return _CONFIRMATION[self]


_LABEL_KO: dict[RiskTier, str] = {
    RiskTier.AMBIENT: "잔잔",
    RiskTier.BREATH: "숨결",
    RiskTier.DUSK: "땅거미",
    RiskTier.EMBER: "불씨",
}

_GLYPH: dict[RiskTier, str] = {
    RiskTier.AMBIENT: "●",
    RiskTier.BREATH: "✓",
    RiskTier.DUSK: "?",
    RiskTier.EMBER: "🛡",
}

_CONFIRMATION: dict[RiskTier, str] = {
    RiskTier.AMBIENT: "none",
    RiskTier.BREATH: "light",
    RiskTier.DUSK: "explicit",
    RiskTier.EMBER: "double",
}


def escalate_only(cur: RiskTier, target: RiskTier) -> RiskTier:
    """위험도는 자동 승격만 가능하다. 결과 = max(cur, target).

    cur/target 가 문자열이나 정수여도 RiskTier 로 정규화한다.
    """
    cur_t = RiskTier.from_str(cur)
    target_t = RiskTier.from_str(target)
    return cur_t if cur_t >= target_t else target_t


# 안전 임계 카테고리. 이 카테고리에 속한 트리거/액션은 LLM 없이도
# Gate1 결정론만으로 즉시 EMBER 제안을 생성해야 한다.
SAFETY_CRITICAL_CATEGORIES: frozenset[str] = frozenset(
    {
        "gas",
        "smoke",
        "door",
        "leak",
        "high_temp",
        "lock",
        "water_main",
        "oven",
    }
)
