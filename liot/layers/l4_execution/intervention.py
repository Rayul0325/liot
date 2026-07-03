"""위험도별 개입 정책 (개념 L7 Intervention Policy) — 연구 조건 A/B 분기점.

새 기획의 실험 설계: 후보 생성·평가·선택(L2)은 두 조건에서 완전히 동일하고,
사용자에게 닿는 *개입 방식*(확인 강도)만 여기서 갈린다. L2 는 condition 을
볼 수 없으므로 "동일 선택, 개입만 차등"이 구조적으로 보장된다(실험 오염 방지).

  조건 A — UniformIntervention(단일 승인): 모든 제안이 동일한 명시적 승인
    (explicit)을 요구한다. 단, 티어 기준선보다 약해질 수는 없다 —
    EMBER 는 조건과 무관하게 double 을 유지한다.
  조건 B — TieredIntervention(위험도 차등, 현행 기본): 티어별 확인 강도
    (AMBIENT→none, BREATH→light, DUSK→explicit, EMBER→double).

개입 하한 불변식: 어떤 개입 정책도 티어 기준선(``RiskTier.required_confirmation``)
보다 약한 확인을 낼 수 없다. RiskRouter 가 라우팅 시점에 강제하고(위반 =
InvariantError), ``assert_execution_gated`` 가 두 조건을 정적으로 재검한다.
안전 임계 경로는 조건 A/B 에서 완전히 동일하다 — 실험이 안전을 바꾸지 않는다.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from liot.core.risk import RiskTier

__all__ = [
    "CONFIRMATION_ORDER",
    "InterventionPolicy",
    "TieredIntervention",
    "UniformIntervention",
    "build_intervention",
]

# 확인 강도의 전순서. 개입 정책은 이 순서에서 티어 기준선 아래로 내려갈 수 없다.
CONFIRMATION_ORDER: dict[str, int] = {
    "none": 0,
    "light": 1,
    "explicit": 2,
    "double": 3,
}


@runtime_checkable
class InterventionPolicy(Protocol):
    """위험도 → 확인 강도. 순수 함수, 부수효과 없음."""

    condition: str

    def confirmation_for(self, tier: RiskTier | str | int) -> str:
        """이 위험도의 제안에 요구할 확인 강도('none'|'light'|'explicit'|'double')."""
        ...


class TieredIntervention:
    """조건 B — 위험도 차등 개입(현행 기본). 티어 기준선을 그대로 쓴다."""

    condition = "B"

    def confirmation_for(self, tier: RiskTier | str | int) -> str:
        return RiskTier.from_str(tier).required_confirmation


class UniformIntervention:
    """조건 A — 단일 승인. 모든 제안에 동일한 명시적 승인을 요구한다.

    기준선이 더 강하면(예: EMBER→double) 기준선을 따른다 — 개입 정책은
    확인을 *강화*할 수만 있다(개입 하한 불변식).
    """

    condition = "A"
    baseline = "explicit"

    def confirmation_for(self, tier: RiskTier | str | int) -> str:
        base = RiskTier.from_str(tier).required_confirmation
        if CONFIRMATION_ORDER[base] >= CONFIRMATION_ORDER[self.baseline]:
            return base
        return self.baseline


def build_intervention(condition: str | None) -> InterventionPolicy:
    """실험 조건 문자열('A'|'B', 대소문자 무시) → 개입 정책. 미지값은 거부."""
    c = (condition or "B").strip().upper()
    if c == "A":
        return UniformIntervention()
    if c == "B":
        return TieredIntervention()
    raise ValueError(f"unknown experiment condition: {condition!r} (expected 'A' or 'B')")
