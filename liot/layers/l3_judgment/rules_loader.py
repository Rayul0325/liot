"""Gate1 규칙셋 로더.

규칙은 ``config/rules/*.yaml`` 에 선언되며 **데이터 주도 매칭만** 한다:

  - ``match`` 는 enum 성격의 키만 허용한다(action_category / time_bucket /
    occupancy / source / trust). 자유 표현/스크립트는 없다.
  - 효과는 ``set_floor`` 뿐이다. 즉 위험도 *하한(floor)* 만 끌어올린다.
    적용은 항상 ``escalate_only(max)`` 라서 강등이 구조적으로 불가능하다(불변식 5).
  - 강등 의도(``demote`` / ``to_tier`` / ``downgrade`` / ``set_ceiling`` 등)는
    로더가 **즉시 거부**한다. 규칙이 위험도를 낮출 길이 없다.

로더는 ``ruleset_version`` 과 결정론적 무결성 해시(``ruleset_hash``)를 계산해
번들 매니페스트가 재현 가능한 식별자를 갖게 한다(불변식 4).
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from ...core.risk import RiskTier

__all__ = [
    "Rule",
    "RuleSet",
    "RuleLoadError",
    "ALLOWED_MATCH_KEYS",
    "FORBIDDEN_EFFECT_KEYS",
    "load_ruleset",
    "load_rulesets",
    "DEFAULT_RULES_DIR",
]


class RuleLoadError(ValueError):
    """규칙 정의가 데이터 주도/승격-전용 제약을 어겼을 때."""


# match 에 허용되는 enum 성격 키. 그 외 키는 거부된다(데이터 주도 강제).
ALLOWED_MATCH_KEYS: frozenset[str] = frozenset(
    {
        "action_category",
        "time_bucket",
        "occupancy",
        "source",
        "trust",
        "capability",
        "action",
    }
)

# 위험도를 낮추려는 효과 키. 발견 즉시 로드 거부(불변식 5: 강등 불가).
FORBIDDEN_EFFECT_KEYS: frozenset[str] = frozenset(
    {
        "demote",
        "downgrade",
        "to_tier",
        "set_tier",
        "set_ceiling",
        "ceiling",
        "cap",
        "max_tier",
        "lower_to",
        "reduce_to",
    }
)

DEFAULT_RULES_DIR = Path(__file__).resolve().parents[3] / "config" / "rules"


@dataclass(frozen=True)
class Rule:
    """단일 규칙: match 가 들어맞으면 set_floor 로 하한을 끌어올린다."""

    rule_id: str
    match: dict[str, str]
    set_floor: RiskTier
    verdict: str  # 'allow' | 'block'
    description: str = ""

    def matches(self, facts: dict[str, Any]) -> bool:
        """모든 match 키가 facts 와 일치하면 True(부분집합 매칭).

        값 비교는 대소문자/공백 무시(문자열 정규화)로 한다.
        """
        for key, want in self.match.items():
            have = facts.get(key)
            if have is None:
                return False
            if _norm(have) != _norm(want):
                return False
        return True


@dataclass(frozen=True)
class RuleSet:
    """로드된 규칙셋. 결정론적 무결성 해시를 동반한다."""

    name: str
    ruleset_version: str
    rules: tuple[Rule, ...]
    ruleset_hash: str
    sources: tuple[str, ...] = field(default_factory=tuple)

    def matching(self, facts: dict[str, Any]) -> list[Rule]:
        """facts 에 들어맞는 규칙들을 선언 순서대로 반환."""
        return [r for r in self.rules if r.matches(facts)]


def _norm(value: Any) -> str:
    return str(value).strip().lower()


def _coerce_floor(value: Any, rule_id: str) -> RiskTier:
    try:
        return RiskTier.from_str(value)
    except (ValueError, KeyError) as exc:
        raise RuleLoadError(
            f"rule {rule_id!r}: invalid set_floor {value!r}"
        ) from exc


def _parse_rule(raw: dict[str, Any], idx: int) -> Rule:
    if not isinstance(raw, dict):
        raise RuleLoadError(f"rule #{idx} is not a mapping")

    rule_id = str(raw.get("id") or raw.get("rule_id") or f"rule_{idx}")

    # 강등 의도 키 탐지 → 즉시 거부.
    offending = FORBIDDEN_EFFECT_KEYS & set(raw.keys())
    if offending:
        raise RuleLoadError(
            f"rule {rule_id!r}: forbidden demotion keys present "
            f"{sorted(offending)} (rules may only escalate via set_floor)"
        )

    if "set_floor" not in raw:
        raise RuleLoadError(
            f"rule {rule_id!r}: missing required 'set_floor' (only escalation effect allowed)"
        )
    set_floor = _coerce_floor(raw["set_floor"], rule_id)

    match_raw = raw.get("match", {})
    if not isinstance(match_raw, dict) or not match_raw:
        raise RuleLoadError(f"rule {rule_id!r}: 'match' must be a non-empty mapping")
    bad_keys = set(match_raw.keys()) - ALLOWED_MATCH_KEYS
    if bad_keys:
        raise RuleLoadError(
            f"rule {rule_id!r}: disallowed match keys {sorted(bad_keys)}; "
            f"only enum keys {sorted(ALLOWED_MATCH_KEYS)} are data-driven matchable"
        )
    # match 값은 스칼라(문자열/숫자)만 — 자유 구조/스크립트 차단.
    match: dict[str, str] = {}
    for k, v in match_raw.items():
        if isinstance(v, (dict, list)):
            raise RuleLoadError(
                f"rule {rule_id!r}: match[{k!r}] must be a scalar enum value, got {type(v).__name__}"
            )
        match[k] = _norm(v)

    verdict = _norm(raw.get("verdict", "allow"))
    if verdict not in ("allow", "block"):
        raise RuleLoadError(
            f"rule {rule_id!r}: verdict must be 'allow' or 'block', got {verdict!r}"
        )

    description = str(raw.get("description", ""))
    return Rule(
        rule_id=rule_id,
        match=match,
        set_floor=set_floor,
        verdict=verdict,
        description=description,
    )


def _compute_hash(name: str, version: str, rules: tuple[Rule, ...]) -> str:
    """규칙셋의 결정론적 무결성 해시(sha256 hex)."""
    canonical = {
        "name": name,
        "ruleset_version": version,
        "rules": [
            {
                "id": r.rule_id,
                "match": dict(sorted(r.match.items())),
                "set_floor": int(r.set_floor),
                "verdict": r.verdict,
            }
            for r in rules
        ],
    }
    blob = json.dumps(
        canonical, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    )
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def load_ruleset(path: str | Path) -> RuleSet:
    """단일 YAML 규칙셋을 로드·검증한다."""
    p = Path(path)
    with p.open("r", encoding="utf-8") as fh:
        doc = yaml.safe_load(fh) or {}
    if not isinstance(doc, dict):
        raise RuleLoadError(f"{p}: top-level document must be a mapping")

    name = str(doc.get("name") or p.stem)
    version = str(doc.get("version") or doc.get("ruleset_version") or "0.0.0")
    raw_rules = doc.get("rules", [])
    if not isinstance(raw_rules, list):
        raise RuleLoadError(f"{p}: 'rules' must be a list")

    rules = tuple(_parse_rule(r, i) for i, r in enumerate(raw_rules))
    ruleset_hash = _compute_hash(name, version, rules)
    return RuleSet(
        name=name,
        ruleset_version=version,
        rules=rules,
        ruleset_hash=ruleset_hash,
        sources=(str(p),),
    )


def load_rulesets(rules_dir: str | Path | None = None) -> RuleSet:
    """디렉터리의 모든 ``*.yaml`` 규칙셋을 파일명 순으로 합쳐 하나의 RuleSet 으로.

    합쳐진 규칙셋의 ruleset_version 은 구성 파일들의 ``name:version`` 을
    정렬·결합한 문자열이며, 무결성 해시는 전체 규칙 집합에서 결정론적으로 계산된다.
    """
    base = Path(rules_dir) if rules_dir is not None else DEFAULT_RULES_DIR
    files = sorted(base.glob("*.yaml")) + sorted(base.glob("*.yml"))
    # 중복 제거(정렬 유지).
    seen: set[Path] = set()
    ordered: list[Path] = []
    for f in files:
        if f not in seen:
            seen.add(f)
            ordered.append(f)

    sub: list[RuleSet] = [load_ruleset(f) for f in ordered]
    all_rules: tuple[Rule, ...] = tuple(r for rs in sub for r in rs.rules)
    version = "+".join(f"{rs.name}:{rs.ruleset_version}" for rs in sub) or "empty:0.0.0"
    ruleset_hash = _compute_hash("__merged__", version, all_rules)
    sources = tuple(s for rs in sub for s in rs.sources)
    return RuleSet(
        name="__merged__",
        ruleset_version=version,
        rules=all_rules,
        ruleset_hash=ruleset_hash,
        sources=sources,
    )
