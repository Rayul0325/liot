"""Gate1 — 결정론 가드레일.

규칙셋(rules_loader)으로 후보 행동의 위험도 *하한(floor)* 을 결정한다.
핵심 보장:

  - **deny-by-default**: 카탈로그에 없거나(미지정) 위험도 하한을 못 정한
    action_category 는 즉시 EMBER 로 끌어올린다. 모르면 가장 안전하게.
  - **set_floor 는 escalate_only(max) 로만 적용**된다. 어떤 규칙도 하한을
    낮출 수 없다(불변식 5).
  - **안전 임계 카테고리(SAFETY_CRITICAL_CATEGORIES)는 100% EMBER floor**.
    규칙 유무와 무관하게 코어가 보장한다(불변식 6: Safety-Never-Silent).
  - **block** verdict 지원: 규칙이 block 을 내면 verdict 에 반영되고,
    block 은 항상 EMBER floor 를 동반한다(차단할 만큼 위험하다는 신호).

Gate1 은 LLM 과 완전히 독립적이며 네트워크를 만지지 않는다.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from ...core.contracts import CandidateAction, ContextSnapshot
from ...core.risk import SAFETY_CRITICAL_CATEGORIES, RiskTier, escalate_only
from .rules_loader import Rule, RuleSet, load_rulesets

__all__ = ["Guardrail", "SafetyGate", "Gate1Result"]

# 알려진 비안전 카테고리의 기본 하한. 여기에도 없으면 deny-by-default(EMBER).
# (안전 임계 카테고리는 이 표를 거치지 않고 무조건 EMBER 이다.)
_KNOWN_CATEGORY_FLOOR: dict[str, RiskTier] = {
    "light": RiskTier.AMBIENT,
    "scene": RiskTier.AMBIENT,
    "info": RiskTier.AMBIENT,
    "ambient": RiskTier.AMBIENT,
    "climate": RiskTier.BREATH,
    "media": RiskTier.BREATH,
    "noise": RiskTier.BREATH,
    "blind": RiskTier.BREATH,
    "curtain": RiskTier.BREATH,
    "appliance": RiskTier.DUSK,
    "power": RiskTier.DUSK,
}


@dataclass(frozen=True)
class Gate1Result:
    """Gate1 평가 결과.

    floor_tier: 이 후보에 강제되는 위험도 하한.
    gate1_verdict: 사람이 읽는 결정 요약 문자열(재현/감사용).
    blocked: 규칙이 명시적으로 차단했는지.
    matched_rule_ids: 적용된 규칙 id 들(추적용).
    safety_critical: 안전 임계 카테고리였는지.
    """

    floor_tier: RiskTier
    gate1_verdict: str
    blocked: bool
    matched_rule_ids: tuple[str, ...]
    safety_critical: bool


class Guardrail:
    """Gate1 결정론 가드레일."""

    def __init__(self, ruleset: RuleSet | None = None, rules_dir: str | None = None) -> None:
        self.ruleset: RuleSet = ruleset if ruleset is not None else load_rulesets(rules_dir)

    @property
    def ruleset_version(self) -> str:
        return self.ruleset.ruleset_version

    @property
    def ruleset_hash(self) -> str:
        return self.ruleset.ruleset_hash

    def _facts(self, candidate: CandidateAction, ctx: ContextSnapshot | None) -> dict[str, Any]:
        """매칭에 쓰일 enum 성격 facts 추출(데이터 주도)."""
        facts: dict[str, Any] = {
            "action_category": candidate.category,
            "capability": candidate.capability,
            "action": candidate.action,
        }
        structured = (ctx.structured if ctx is not None else {}) or {}
        for key in ("time_bucket", "occupancy", "source", "trust"):
            if key in structured:
                facts[key] = structured[key]
        # trigger 에 있을 수도 있는 동일 키 보강.
        trigger = (ctx.trigger if ctx is not None else {}) or {}
        for key in ("time_bucket", "occupancy", "source", "trust"):
            if key not in facts and key in trigger:
                facts[key] = trigger[key]
        return facts

    def evaluate(
        self, candidate: CandidateAction, ctx: ContextSnapshot | None = None
    ) -> Gate1Result:
        """후보 행동의 위험도 하한과 Gate1 평결을 결정한다.

        반환 floor_tier 는 다음을 escalate_only(max) 로 합성한 결과:
          1) 카테고리 기준선(deny-by-default: 미지정 → EMBER),
          2) 매칭된 모든 규칙의 set_floor,
          3) 안전 임계 카테고리면 EMBER 강제,
          4) block 규칙이 있으면 EMBER 강제.
        """
        category = (candidate.category or "").strip().lower()
        is_safety = category in SAFETY_CRITICAL_CATEGORIES

        # 1) 카테고리 기준선. deny-by-default.
        if is_safety:
            baseline = RiskTier.EMBER
            base_reason = f"safety-critical category '{category}' → EMBER floor"
        elif category in _KNOWN_CATEGORY_FLOOR:
            baseline = _KNOWN_CATEGORY_FLOOR[category]
            base_reason = f"known category '{category}' baseline {baseline.name}"
        else:
            baseline = RiskTier.EMBER
            base_reason = (
                f"deny-by-default: unspecified category {category!r} → EMBER floor"
            )

        floor = baseline

        # 2) 규칙 매칭(set_floor 는 escalate_only 로만).
        facts = self._facts(candidate, ctx)
        matched: list[Rule] = self.ruleset.matching(facts)
        blocked = False
        rule_notes: list[str] = []
        for rule in matched:
            floor = escalate_only(floor, rule.set_floor)
            note = f"{rule.rule_id}->set_floor={rule.set_floor.name}"
            if rule.verdict == "block":
                blocked = True
                note += "(block)"
            rule_notes.append(note)

        # 3) block 은 항상 EMBER floor 동반.
        if blocked:
            floor = escalate_only(floor, RiskTier.EMBER)

        # 4) 안전 임계 재보장(규칙이 우연히 낮춰도 EMBER 유지 — escalate_only 라 불가하지만 명시적 보강).
        if is_safety:
            floor = escalate_only(floor, RiskTier.EMBER)

        verdict_kind = "block" if blocked else "allow"
        rule_part = ("; rules: " + ", ".join(rule_notes)) if rule_notes else "; rules: none"
        gate1_verdict = (
            f"gate1:{verdict_kind} floor={floor.name} ({base_reason}{rule_part})"
        )

        return Gate1Result(
            floor_tier=floor,
            gate1_verdict=gate1_verdict,
            blocked=blocked,
            matched_rule_ids=tuple(r.rule_id for r in matched),
            safety_critical=is_safety,
        )


# 새 기획(다중 후보 파이프라인)의 개념 L6 'Safety Gate' = 이 Gate1 가드레일.
# 기존 이름·임포트는 그대로 보존하고 별칭만 추가한다. LLM 이 후보를 만들든
# 규칙이 만들든, 선택된 행동은 반드시 이 결정론 게이트를 지나며(출처 무관),
# 게이트는 위험도 하한을 올릴 수만 있다(불변식 2′: LLM 은 floor 를 못 낮춘다).
SafetyGate = Guardrail
