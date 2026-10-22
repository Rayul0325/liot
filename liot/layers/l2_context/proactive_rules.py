"""능동(proactive) 규칙 로더 — Tier A 결정론 생성 규칙.

Gate1 의 rules_loader 와 같은 데이터-주도 철학을 따르되, 효과가 다르다:
Gate1 규칙은 위험도 *하한 승격* 만 하고, 능동 규칙은 *후보 행동 발의* 를
선언한다. 별도 로더인 이유: 능동 규칙은 엔티티 상태 술어(닫힌 비교자)가
필요한데, 이를 rules_loader 의 enum-동등 매칭에 섞으면 그 로더가 지켜온
"스칼라 동등만" 규율이 무너진다. 두 로더는 서로를 오염시키지 않는다.

로드 시 deny-by-default 강제(불변식 7 의 1차 방어선):
  - ``propose.(capability, action)`` 이 카탈로그 파생 허용목록
    (``derive_proactive_allowed``)에 없으면 즉시 거부. lock.unlock / valve.open /
    cover.open / oven.* 같은 금지 방향·안전 액추에이터는 **선언 자체가 불가능**하다
    (rules_loader 가 강등 키를 거부하는 철학과 동일). 허용목록은 출하 카탈로그에서
    구 고정 6튜플 허용목록과 동일하게 파생된다(동치 회귀 테스트로 봉인).
  - 술어 ``op`` 가 닫힌 어휘(``ALLOWED_PREDICATE_OPS``) 밖이면 거부.
  - match/state 값에 dict/list/표현식 불가 — 스칼라만(스크립트 표면 차단).

로더는 rules_loader 처럼 결정론적 ``ruleset_hash`` 를 계산한다(불변식 4:
능동 후보의 출처 ``proactive:rule:<id>@<hash8>`` 가 재현 가능한 식별자가 됨).
"""

from __future__ import annotations

import hashlib
import json
import string
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

import yaml

from liot.core.risk import RiskTier
from liot.layers.l4_execution.device_catalog import load_device_catalog

if TYPE_CHECKING:  # pragma: no cover
    from liot.layers.l4_execution.device_catalog import DeviceCatalog

__all__ = [
    "SAFETY_ACTUATOR_CAPABILITIES",
    "derive_proactive_allowed",
    "ALLOWED_PROACTIVE_MATCH_KEYS",
    "ALLOWED_PREDICATE_OPS",
    "ALLOWED_EXTERNAL_KEYS",
    "ALLOWED_EXTERNAL_OPS",
    "ProactiveRuleError",
    "StatePredicate",
    "ExternalPredicate",
    "REASON_PLACEHOLDERS",
    "render_reason",
    "ProactiveRule",
    "ProactiveLimits",
    "ProactiveRuleSet",
    "parse_rule",
    "build_ruleset",
    "load_proactive_ruleset",
    "load_proactive_rulesets",
    "DEFAULT_PROACTIVE_DIR",
]


class ProactiveRuleError(ValueError):
    """능동 규칙 정의가 deny-by-default/데이터-주도 제약을 어겼을 때."""


# 능동 발의에서 영구 제외할 안전 액추에이터 capability(불변 상수, 코드 수정으로만
# 확장). 카탈로그에 이 capability 의 저위험(breath 이하) action 이 있어도 능동
# 허용목록에서 뺀다 — 안전 액추에이터(잠금/밸브/오븐)는 컴포트 생성기의 손이 닿지
# 않는다. 데이터(카탈로그 등록)로는 절대 넓힐 수 없는 하드 블록리스트다.
SAFETY_ACTUATOR_CAPABILITIES: frozenset[str] = frozenset({"lock", "valve", "oven"})


def derive_proactive_allowed(catalog: DeviceCatalog) -> frozenset[tuple[str, str]]:
    """카탈로그 raw capabilities 맵에서 능동 허용목록을 파생한다(단일 원천).

    (capability, action) 을 포함하는 조건(둘 다 충족):
      * ``catalog.risk_for(cap, action, {}) <= RiskTier.BREATH`` — 컴포트 상한.
        미지·고위험·위험 방향은 카탈로그가 DUSK/EMBER 로 판정해 자동 제외된다.
      * ``cap not in SAFETY_ACTUATOR_CAPABILITIES`` — 안전 액추에이터 제외.

    열거원은 ``capability_actions()``(raw 맵)이지 ``available_actions()`` 가 아니다 —
    후자는 기기 조인이라 ``notify.send`` 처럼 기기 인스턴스 없는 action 이 탈락해
    출하 6튜플 동치가 깨진다. 출하 카탈로그에 적용하면 정확히 다음 6개다:
    (light,turn_on/turn_off/set_brightness)·(climate,set_temperature)·
    (cover,close)·(notify,send) — 구 고정 6튜플 허용목록과 동일(회귀 테스트로 봉인).

    새 등록 행동은 tier ≤ breath 선언만으로 이 집합에 들어온다(안전 액추에이터
    capability 는 제외). 허용목록 통과 후에도 안전 카테고리 타깃 거부·capability
    일치·상한 재검이 별도로 적용된다(다중 deny-by-default).
    """
    return frozenset(
        (cap, action)
        for cap, action in catalog.capability_actions()
        if cap not in SAFETY_ACTUATOR_CAPABILITIES
        and catalog.risk_for(cap, action, {}) <= RiskTier.BREATH
    )

# match 에 허용되는 enum 성격 키(맥락 facts). 그 외 키는 거부.
ALLOWED_PROACTIVE_MATCH_KEYS: frozenset[str] = frozenset({"time_bucket", "occupancy"})

# 상태 술어의 닫힌 비교자 어휘. 표현식/스크립트/임의 연산 불가.
ALLOWED_PREDICATE_OPS: frozenset[str] = frozenset({"eq", "ne", "lt", "le", "gt", "ge"})

# 외부(날씨) 조건이 읽을 수 있는 값과 비교자. 값은 전부 실수라 순서 비교만 둔다
# (실수 같음 비교는 함정이다). 외부 맥락이 없거나 degraded 면 조건은 성립하지 않는다.
ALLOWED_EXTERNAL_KEYS: frozenset[str] = frozenset({"pm25", "cloud_cover", "outdoor_temp_c"})
ALLOWED_EXTERNAL_OPS: frozenset[str] = frozenset({"lt", "le", "gt", "ge"})

# 이유 한 줄(reason_ko) 틀이 쓸 수 있는 자리표시자. 값은 발의 순간의 맥락 사실에서 채운다.
# 틀은 규칙 메타데이터와 시뮬레이션 사건에만 실린다 — ntfy 페이로드에는 들어가지 않는다.
REASON_PLACEHOLDERS: frozenset[str] = frozenset({"pm25", "cloud_pct", "outdoor_temp_c"})
_MISSING = "?"


def _reason_values(facts: Mapping[str, Any]) -> dict[str, str]:
    ext = facts.get("external")
    ext = ext if isinstance(ext, Mapping) and not ext.get("degraded", True) else {}
    pm25, cloud, temp = ext.get("pm25"), ext.get("cloud_cover"), ext.get("outdoor_temp_c")
    return {
        "pm25": _MISSING if pm25 is None else f"{float(pm25):.0f}",
        "cloud_pct": _MISSING if cloud is None else f"{float(cloud) * 100:.0f}",
        "outdoor_temp_c": _MISSING if temp is None else f"{float(temp):.1f}",
    }


def render_reason(template: str, facts: Mapping[str, Any]) -> str:
    """이유 틀을 맥락 값으로 채운다. 값이 없으면 '?' (로더가 자리표시자를 이미 검증)."""
    return template.format_map(_reason_values(facts)) if template else ""

DEFAULT_PROACTIVE_DIR = Path(__file__).resolve().parents[3] / "config" / "proactive"

# limits 기본값(데이터로 덮어쓰기 가능). 능동 제안은 소량이 원칙 —
# L4 알림 예산(6/h)과 별개로 훨씬 보수적이다(알림 피로 = 수용률 붕괴).
_DEFAULT_LIMITS = {"cooldown_minutes": 60, "max_per_bucket": 1, "max_per_day": 3}


def _norm(value: Any) -> str:
    return str(value).strip().lower()


def _is_scalar(value: Any) -> bool:
    return isinstance(value, (str, int, float, bool)) or value is None


@dataclass(frozen=True)
class StatePredicate:
    """엔티티 상태에 대한 단일 술어: entities[entity_id] <op> value.

    닫힌 비교자만 허용. 수치 비교(lt/le/gt/ge)는 양쪽 모두 수치일 때만
    참이 될 수 있다(타입 불일치 → False, 예외 없음 — 결정론).
    """

    entity_id: str
    op: str
    value: Any

    def holds(self, entities: Mapping[str, Any]) -> bool:
        if self.entity_id not in entities:
            return False
        have = entities[self.entity_id]
        want = self.value
        # have/want 는 Any(YAML·런타임 등록에서 온다) — 비교 결과를 bool 로 확정한다.
        if self.op == "eq":
            return bool(have == want)
        if self.op == "ne":
            return bool(have != want)
        # 순서 비교는 수치만(불리언 제외 — True<2 같은 함정 차단).
        if isinstance(have, bool) or isinstance(want, bool):
            return False
        if not isinstance(have, (int, float)) or not isinstance(want, (int, float)):
            return False
        if self.op == "lt":
            return have < want
        if self.op == "le":
            return have <= want
        if self.op == "gt":
            return have > want
        if self.op == "ge":
            return have >= want
        return False  # pragma: no cover - 로더가 op 를 이미 검증


@dataclass(frozen=True)
class ExternalPredicate:
    """외부 맥락 값에 대한 단일 술어: external[key] <op> value (순서 비교만).

    외부 맥락이 없거나(None) degraded 이거나 그 값이 비어 있으면 거짓 — 모르는 날씨로
    발의하지 않는다(Proactive-Safe-Silent).
    """

    key: str
    op: str
    value: float

    def holds(self, external: Mapping[str, Any] | None) -> bool:
        if not isinstance(external, Mapping) or external.get("degraded", True):
            return False
        have = external.get(self.key)
        if isinstance(have, bool) or not isinstance(have, (int, float)):
            return False
        if self.op == "lt":
            return have < self.value
        if self.op == "le":
            return have <= self.value
        if self.op == "gt":
            return have > self.value
        if self.op == "ge":
            return have >= self.value
        return False  # pragma: no cover - 로더가 op 를 이미 검증


@dataclass(frozen=True)
class ProactiveRule:
    """단일 능동 규칙: match(enum facts) + state(술어들) 가 모두 성립하면
    (capability, action, target_entity, params) 후보를 발의한다."""

    rule_id: str
    match: dict[str, str]
    state: tuple[StatePredicate, ...]
    capability: str
    action: str
    target_entity: str
    params: dict[str, Any]
    ttl_minutes: int
    description: str = ""
    external: tuple[ExternalPredicate, ...] = ()
    # 규칙이 선언한 기대 효과(0~1, 선택). 평가기가 이 후보의 효과 점수로 쓴다.
    benefit: float | None = None
    # 발의할 수 있는 집 지역 시(hour) 목록(비면 제한 없음). 시간대 버킷보다 좁게 잡을 때.
    hours: tuple[int, ...] = ()
    # 이유 한 줄 틀(한국어). 자리표시자는 REASON_PLACEHOLDERS 만.
    reason_ko: str = ""
    # 사람이 읽는 행동 제목(한국어, 자리표시자 없음). 화면·시뮬레이션 사건용 — ntfy 에는 없다.
    title_ko: str = ""

    def matches(self, facts: Mapping[str, Any], entities: Mapping[str, Any]) -> bool:
        """match 키 전부 일치(정규화 비교) AND 모든 상태 술어 AND 모든 외부 술어 성립.

        외부 술어는 ``facts["external"]``(ExternalContext.rule_facts)을 읽는다.
        """
        for key, want in self.match.items():
            have = facts.get(key)
            if have is None or _norm(have) != _norm(want):
                return False
        if self.hours and facts.get("local_hour") not in self.hours:
            return False
        if not all(p.holds(entities) for p in self.state):
            return False
        ext = facts.get("external")
        return all(p.holds(ext) for p in self.external)


@dataclass(frozen=True)
class ProactiveLimits:
    """능동 제안 빈도 제한(생성 시 게이팅). L4 알림 예산과 별개·더 보수적."""

    cooldown_minutes: int = _DEFAULT_LIMITS["cooldown_minutes"]
    max_per_bucket: int = _DEFAULT_LIMITS["max_per_bucket"]
    max_per_day: int = _DEFAULT_LIMITS["max_per_day"]


@dataclass(frozen=True)
class ProactiveRuleSet:
    """로드된 능동 규칙셋. 결정론적 무결성 해시 동반(출처 재현성)."""

    name: str
    ruleset_version: str
    rules: tuple[ProactiveRule, ...]
    ruleset_hash: str
    limits: ProactiveLimits = field(default_factory=ProactiveLimits)
    sources: tuple[str, ...] = field(default_factory=tuple)

    @property
    def uses_hours(self) -> bool:
        """시(hour) 제한 규칙이 있는가 — 있을 때만 생성기가 local_hour 를 사실에 싣는다."""
        return any(r.hours for r in self.rules)

    def matching(
        self, facts: Mapping[str, Any], entities: Mapping[str, Any]
    ) -> list[ProactiveRule]:
        """성립하는 규칙들을 선언 순서대로 반환(첫 규칙 우선 정책은 생성기)."""
        return [r for r in self.rules if r.matches(facts, entities)]


def _parse_predicate(raw: Any, rule_id: str, idx: int) -> StatePredicate:
    if not isinstance(raw, dict):
        raise ProactiveRuleError(
            f"rule {rule_id!r}: state[{idx}] must be a mapping {{entity, op, value}}"
        )
    unknown = set(raw.keys()) - {"entity", "op", "value"}
    if unknown:
        raise ProactiveRuleError(
            f"rule {rule_id!r}: state[{idx}] unknown keys {sorted(unknown)}"
        )
    entity = raw.get("entity")
    op = _norm(raw.get("op", ""))
    value = raw.get("value")
    if not entity or not isinstance(entity, str):
        raise ProactiveRuleError(f"rule {rule_id!r}: state[{idx}] missing 'entity'")
    if op not in ALLOWED_PREDICATE_OPS:
        raise ProactiveRuleError(
            f"rule {rule_id!r}: state[{idx}] op {op!r} not in closed vocabulary "
            f"{sorted(ALLOWED_PREDICATE_OPS)} (no scripts/expressions)"
        )
    if not _is_scalar(value):
        raise ProactiveRuleError(
            f"rule {rule_id!r}: state[{idx}] value must be a scalar, "
            f"got {type(value).__name__}"
        )
    return StatePredicate(entity_id=entity, op=op, value=value)


def _parse_external(raw: Any, rule_id: str, idx: int) -> ExternalPredicate:
    if not isinstance(raw, dict):
        raise ProactiveRuleError(
            f"rule {rule_id!r}: external[{idx}] must be a mapping {{key, op, value}}"
        )
    unknown = set(raw.keys()) - {"key", "op", "value"}
    if unknown:
        raise ProactiveRuleError(
            f"rule {rule_id!r}: external[{idx}] unknown keys {sorted(unknown)}"
        )
    key = _norm(raw.get("key", ""))
    op = _norm(raw.get("op", ""))
    value = raw.get("value")
    if key not in ALLOWED_EXTERNAL_KEYS:
        raise ProactiveRuleError(
            f"rule {rule_id!r}: external[{idx}] key {key!r} not in "
            f"{sorted(ALLOWED_EXTERNAL_KEYS)}"
        )
    if op not in ALLOWED_EXTERNAL_OPS:
        raise ProactiveRuleError(
            f"rule {rule_id!r}: external[{idx}] op {op!r} not in closed vocabulary "
            f"{sorted(ALLOWED_EXTERNAL_OPS)}"
        )
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ProactiveRuleError(
            f"rule {rule_id!r}: external[{idx}] value must be a number"
        )
    return ExternalPredicate(key=key, op=op, value=float(value))


def _parse_hours(raw: Any, rule_id: str) -> tuple[int, ...]:
    if raw is None:
        return ()
    if not isinstance(raw, list) or not raw:
        raise ProactiveRuleError(f"rule {rule_id!r}: hours must be a non-empty list of 0..23")
    out: list[int] = []
    for h in raw:
        if isinstance(h, bool) or not isinstance(h, int) or not 0 <= h <= 23:
            raise ProactiveRuleError(f"rule {rule_id!r}: hours must be integers 0..23")
        out.append(h)
    return tuple(sorted(set(out)))


def _parse_reason(raw: Any, rule_id: str) -> str:
    if raw is None:
        return ""
    if not isinstance(raw, str):
        raise ProactiveRuleError(f"rule {rule_id!r}: reason_ko must be a string")
    try:
        names = {name for _, name, _, _ in string.Formatter().parse(raw) if name is not None}
    except ValueError as exc:
        raise ProactiveRuleError(f"rule {rule_id!r}: reason_ko is malformed: {exc}") from exc
    bad = names - REASON_PLACEHOLDERS
    if bad:
        raise ProactiveRuleError(
            f"rule {rule_id!r}: reason_ko placeholders {sorted(bad)} not in "
            f"{sorted(REASON_PLACEHOLDERS)}"
        )
    return raw.strip()


def _parse_title(raw: Any, rule_id: str) -> str:
    if raw is None:
        return ""
    if not isinstance(raw, str) or "{" in raw or "}" in raw:
        raise ProactiveRuleError(f"rule {rule_id!r}: title_ko must be plain text")
    return raw.strip()


def _parse_benefit(raw: Any, rule_id: str) -> float | None:
    if raw is None:
        return None
    if isinstance(raw, bool) or not isinstance(raw, (int, float)) or not 0.0 <= raw <= 1.0:
        raise ProactiveRuleError(f"rule {rule_id!r}: benefit must be a number in 0..1")
    return float(raw)


def _parse_rule(
    raw: dict[str, Any], idx: int, *, allowed: frozenset[tuple[str, str]] | None = None
) -> ProactiveRule:
    if allowed is None:
        allowed = derive_proactive_allowed(load_device_catalog())
    if not isinstance(raw, dict):
        raise ProactiveRuleError(f"rule #{idx} is not a mapping")
    rule_id = str(raw.get("id") or raw.get("rule_id") or f"proactive_{idx}")

    # ── match: enum facts 만, 스칼라만 ─────────────────────────────────
    match_raw = raw.get("match", {}) or {}
    if not isinstance(match_raw, dict):
        raise ProactiveRuleError(f"rule {rule_id!r}: 'match' must be a mapping")
    bad = set(match_raw.keys()) - ALLOWED_PROACTIVE_MATCH_KEYS
    if bad:
        raise ProactiveRuleError(
            f"rule {rule_id!r}: disallowed match keys {sorted(bad)}; "
            f"only {sorted(ALLOWED_PROACTIVE_MATCH_KEYS)} are matchable"
        )
    match: dict[str, str] = {}
    for k, v in match_raw.items():
        if not _is_scalar(v):
            raise ProactiveRuleError(
                f"rule {rule_id!r}: match[{k!r}] must be a scalar"
            )
        match[k] = _norm(v)

    # ── state: 닫힌 술어들 ─────────────────────────────────────────────
    state_raw = raw.get("state", []) or []
    if not isinstance(state_raw, list):
        raise ProactiveRuleError(f"rule {rule_id!r}: 'state' must be a list")
    state = tuple(_parse_predicate(p, rule_id, i) for i, p in enumerate(state_raw))

    # ── external: 날씨 값의 닫힌 순서 비교 ─────────────────────────────
    ext_raw = raw.get("external", []) or []
    if not isinstance(ext_raw, list):
        raise ProactiveRuleError(f"rule {rule_id!r}: 'external' must be a list")
    external = tuple(_parse_external(p, rule_id, i) for i, p in enumerate(ext_raw))

    if not match and not state and not external:
        raise ProactiveRuleError(
            f"rule {rule_id!r}: requires at least one match key or state predicate "
            "(unconditional proactive rules are nag-by-construction)"
        )

    # ── propose: 허용 목록 deny-by-default ─────────────────────────────
    prop = raw.get("propose")
    if not isinstance(prop, dict):
        raise ProactiveRuleError(f"rule {rule_id!r}: missing 'propose' mapping")
    capability = _norm(prop.get("capability", ""))
    action = _norm(prop.get("action", ""))
    target = str(prop.get("target") or prop.get("target_entity") or "").strip()
    if not capability or not action or not target:
        raise ProactiveRuleError(
            f"rule {rule_id!r}: propose requires capability/action/target"
        )
    if (capability, action) not in allowed:
        raise ProactiveRuleError(
            f"rule {rule_id!r}: ({capability!r}, {action!r}) is not in the "
            "catalog-derived proactive-allowed set — proactive rules may only "
            "declare comfort actions (deny-by-default; unsafe directions and "
            "safety actuators are undeclarable)"
        )
    params_raw = prop.get("params", {}) or {}
    if not isinstance(params_raw, dict):
        raise ProactiveRuleError(f"rule {rule_id!r}: propose.params must be a mapping")
    for pk, pv in params_raw.items():
        if not _is_scalar(pv):
            raise ProactiveRuleError(
                f"rule {rule_id!r}: propose.params[{pk!r}] must be a scalar"
            )

    ttl_raw = raw.get("ttl_minutes", 15)
    try:
        ttl = int(ttl_raw)
    except (TypeError, ValueError) as exc:
        raise ProactiveRuleError(f"rule {rule_id!r}: bad ttl_minutes {ttl_raw!r}") from exc
    if ttl <= 0:
        raise ProactiveRuleError(f"rule {rule_id!r}: ttl_minutes must be positive")

    return ProactiveRule(
        rule_id=rule_id,
        match=match,
        state=state,
        capability=capability,
        action=action,
        target_entity=target,
        params=dict(params_raw),
        ttl_minutes=ttl,
        description=str(raw.get("description", "")),
        external=external,
        benefit=_parse_benefit(raw.get("benefit"), rule_id),
        hours=_parse_hours(raw.get("hours"), rule_id),
        reason_ko=_parse_reason(raw.get("reason_ko"), rule_id),
        title_ko=_parse_title(raw.get("title_ko"), rule_id),
    )


def parse_rule(
    raw: dict[str, Any],
    idx: int = 0,
    *,
    allowed: frozenset[tuple[str, str]] | None = None,
) -> ProactiveRule:
    """단일 규칙 dict 를 검증·파싱한다(레지스트리 런타임 규칙 등록용 공개 진입).

    ``_parse_rule`` 의 얇은 공개 래퍼 — deny-by-default 검증(카탈로그 파생
    허용목록·닫힌 op 어휘·스칼라만)을 그대로 적용한다. ``allowed`` 미지정 →
    출하 카탈로그 파생 허용목록.
    """
    return _parse_rule(raw, idx, allowed=allowed)


def build_ruleset(
    rules: Sequence[ProactiveRule],
    *,
    name: str = "__runtime__",
    version: str = "runtime:0.0.0",
    limits: ProactiveLimits | None = None,
    sources: tuple[str, ...] = (),
) -> ProactiveRuleSet:
    """이미 파싱된 ``ProactiveRule`` 들로 ``ProactiveRuleSet`` 을 조립한다.

    레지스트리가 config 베이스라인 규칙 + 런타임 등록 규칙을 하나의 병합 룰셋으로
    묶을 때 쓴다(로더를 거치지 않고 조립하되 ``_compute_hash`` 로 결정론적 무결성
    해시를 유지 — 출처 재현성 불변식 4 보존).
    """
    rules_t = tuple(rules)
    return ProactiveRuleSet(
        name=name,
        ruleset_version=version,
        rules=rules_t,
        ruleset_hash=_compute_hash(name, version, rules_t),
        limits=limits if limits is not None else ProactiveLimits(),
        sources=sources,
    )


def _parse_limits(raw: Any) -> ProactiveLimits:
    if raw is None:
        return ProactiveLimits()
    if not isinstance(raw, dict):
        raise ProactiveRuleError("'limits' must be a mapping")
    vals = dict(_DEFAULT_LIMITS)
    for key in vals:
        if key in raw:
            try:
                vals[key] = int(raw[key])
            except (TypeError, ValueError) as exc:
                raise ProactiveRuleError(f"limits.{key} must be an int") from exc
            if vals[key] < 0:
                raise ProactiveRuleError(f"limits.{key} must be >= 0")
    return ProactiveLimits(**vals)


def _compute_hash(name: str, version: str, rules: tuple[ProactiveRule, ...]) -> str:
    """규칙셋의 결정론적 무결성 해시(sha256 hex) — rules_loader 와 동형."""
    canonical = {
        "name": name,
        "ruleset_version": version,
        "rules": [
            {
                "id": r.rule_id,
                "match": dict(sorted(r.match.items())),
                "state": [
                    {"entity": p.entity_id, "op": p.op, "value": p.value}
                    for p in r.state
                ],
                "capability": r.capability,
                "action": r.action,
                "target": r.target_entity,
                "params": dict(sorted(r.params.items())),
                "ttl_minutes": r.ttl_minutes,
                # 새 항목은 있을 때만 싣는다 — 없는 규칙의 해시(출처 식별자)는 예전 그대로.
                **(
                    {"external": [{"key": p.key, "op": p.op, "value": p.value}
                                  for p in r.external]}
                    if r.external else {}
                ),
                **({"benefit": r.benefit} if r.benefit is not None else {}),
                **({"hours": list(r.hours)} if r.hours else {}),
                **({"reason_ko": r.reason_ko} if r.reason_ko else {}),
                **({"title_ko": r.title_ko} if r.title_ko else {}),
            }
            for r in rules
        ],
    }
    blob = json.dumps(canonical, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def load_proactive_ruleset(
    path: str | Path, *, allowed: frozenset[tuple[str, str]] | None = None
) -> ProactiveRuleSet:
    """단일 YAML 능동 규칙셋을 로드·검증한다(deny-by-default).

    ``allowed`` 미지정 → 출하 카탈로그 파생 허용목록으로 검증(기존 호출부는
    인자 없이 그대로 유효). 데모는 레지스트리 파생 허용목록을 주입할 수 있다.
    """
    if allowed is None:
        allowed = derive_proactive_allowed(load_device_catalog())
    p = Path(path)
    with p.open("r", encoding="utf-8") as fh:
        doc = yaml.safe_load(fh) or {}
    if not isinstance(doc, dict):
        raise ProactiveRuleError(f"{p}: top-level document must be a mapping")
    name = str(doc.get("name") or p.stem)
    version = str(doc.get("version") or doc.get("ruleset_version") or "0.0.0")
    raw_rules = doc.get("rules", [])
    if not isinstance(raw_rules, list):
        raise ProactiveRuleError(f"{p}: 'rules' must be a list")
    rules = tuple(_parse_rule(r, i, allowed=allowed) for i, r in enumerate(raw_rules))
    limits = _parse_limits(doc.get("limits"))
    return ProactiveRuleSet(
        name=name,
        ruleset_version=version,
        rules=rules,
        ruleset_hash=_compute_hash(name, version, rules),
        limits=limits,
        sources=(str(p),),
    )


def load_proactive_rulesets(
    rules_dir: str | Path | None = None,
    *,
    allowed: frozenset[tuple[str, str]] | None = None,
) -> ProactiveRuleSet:
    """디렉터리의 모든 ``*.yaml`` 을 파일명 순으로 합쳐 하나의 규칙셋으로.

    디렉터리가 없으면 빈 규칙셋(능동 제안 없음 — 안전한 기본값)을 반환한다.
    limits 는 첫 번째(파일명 순) 명시 limits 를 채택한다. ``allowed`` 는 파생
    허용목록을 한 번만 계산해 하위 로드에 넘긴다(파일마다 재파생 방지).
    """
    if allowed is None:
        allowed = derive_proactive_allowed(load_device_catalog())
    base = Path(rules_dir) if rules_dir is not None else DEFAULT_PROACTIVE_DIR
    if not base.exists():
        return ProactiveRuleSet(
            name="__empty__",
            ruleset_version="empty:0.0.0",
            rules=(),
            ruleset_hash=_compute_hash("__empty__", "empty:0.0.0", ()),
        )
    files = sorted(base.glob("*.yaml")) + sorted(base.glob("*.yml"))
    seen: set[Path] = set()
    ordered: list[Path] = []
    for f in files:
        if f not in seen:
            seen.add(f)
            ordered.append(f)

    sub = [load_proactive_ruleset(f, allowed=allowed) for f in ordered]
    all_rules: tuple[ProactiveRule, ...] = tuple(r for rs in sub for r in rs.rules)
    version = "+".join(f"{rs.name}:{rs.ruleset_version}" for rs in sub) or "empty:0.0.0"
    # tighter-wins 병합: 파일을 추가해도 알림 throttle 은 절대 느슨해질 수
    # 없다(deny-by-default 정신). 쿨다운은 큰 값이, 예산은 작은 값이 보수적.
    if sub:
        limits = ProactiveLimits(
            cooldown_minutes=max(rs.limits.cooldown_minutes for rs in sub),
            max_per_bucket=min(rs.limits.max_per_bucket for rs in sub),
            max_per_day=min(rs.limits.max_per_day for rs in sub),
        )
    else:
        limits = ProactiveLimits()
    return ProactiveRuleSet(
        name="__merged__",
        ruleset_version=version,
        rules=all_rules,
        ruleset_hash=_compute_hash("__merged__", version, all_rules),
        limits=limits,
        sources=tuple(s for rs in sub for s in rs.sources),
    )
