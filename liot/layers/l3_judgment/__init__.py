"""L3 Judgment 레이어.

두 관문(gate)으로 위험도를 결정한다:

  - Gate1 (guardrail): 데이터 주도 결정론 가드레일. deny-by-default 이며
    안전 임계 카테고리는 100% EMBER floor 를 보장한다. set_floor 는 항상
    escalate_only(max) 로만 적용되어 강등이 불가능하다(불변식 5).
  - Gate2 (llm_judge): LLM 판단. **Gate1 을 무를 수 없다** — 최종 위험도는
    escalate_only(gate1_floor, llm_tier) = max(...) 이다(불변식 2). LLM 이
    가용하지 않거나 실패하면 degraded fail-safe 로 동작하되, 안전 카테고리는
    절대 침묵하지 않는다(불변식 6).

규칙셋은 prompt_registry 의 BundleManifest 로 prompt_sha + ruleset_version +
policy 를 묶어 bundle_id 를 만들어, 모든 판단이 재현 가능하도록 한다(불변식 4).
"""

from __future__ import annotations

from .guardrail import Gate1Result, Guardrail
from .llm_judge import FakeLLM, LLMJudge, LLMResult, LLMUnavailable
from .prompt_registry import BundleManifest, PromptRegistry
from .rules_loader import Rule, RuleSet, load_ruleset, load_rulesets

__all__ = [
    "Guardrail",
    "Gate1Result",
    "FakeLLM",
    "LLMJudge",
    "LLMResult",
    "LLMUnavailable",
    "BundleManifest",
    "PromptRegistry",
    "Rule",
    "RuleSet",
    "load_ruleset",
    "load_rulesets",
]
