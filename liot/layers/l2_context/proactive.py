"""능동 생성기(Proactive Generator) — Phase2 핵심: "AI 가 먼저 말을 건다".

맥락(현재 상태 + 시간대 + 재실)을 읽고, 도움이 될 액추에이터 행동을
*후보로 발의* 한다. 발의된 후보는 기존 Gate1+Gate2 판단과 RiskRouter 를
그대로 통과한다 — 생성기는 "무엇을 고려할지"만 고르고, 위험도/확인 강도는
게이트가 정한다. **실행하지 않는다**(불변식 3: 제안-만 모델 유지).

비대칭 원칙(불변식 6 과 쌍):
  - 안전: 침묵이 위험 → Safety-Never-Silent (LLM 죽어도 EMBER 강제).
  - 능동 컴포트: 침묵이 안전 → **Proactive-Safe-Silent** (불확실/쿨다운/
    예산초과/검증실패 → 제안 없음). 같은 fail-closed, 반대 방향.

불변식 7 (능동 제안 = 컴포트 전용 + BREATH 상한) 의 3중 강제 지점:
  1) 로드: proactive_rules 로더가 PROACTIVE_ALLOWED 밖 선언을 거부.
  2) 생성: validate_proactive_candidate — 허용 목록 + 안전 카테고리 타깃
     거부 + catalog.risk_for <= BREATH (카탈로그가 단일 위험 원천).
  3) 판단 후: app 이 게이트/LLM 이 BREATH 위로 승격시킨 능동 제안을
     *억제*(침묵)한다. 강등이 아니라 미표출 — 불변식 5 무관.

재현성(불변식 4): 결정의 출처 ``proactive:rule:<id>@<ruleset_hash 8자리>`` 가
후보 params["__origin__"] 에 실리고, app.tick 이 결정 전체를 스냅샷
trigger 로 삼아 snapshot_hash 가 판단된 후보를 고정한다.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Any, Mapping

from liot.core.clock import Clock
from liot.core.contracts import CandidateAction
from liot.core.invariants import InvariantError
from liot.core.risk import SAFETY_CRITICAL_CATEGORIES, RiskTier

from .context_builder import derive_occupancy
from .proactive_rules import (
    PROACTIVE_ALLOWED,
    ProactiveLimits,
    ProactiveRule,
    ProactiveRuleSet,
    load_proactive_rulesets,
)

if TYPE_CHECKING:  # pragma: no cover
    from liot.layers.l4_execution.device_catalog import DeviceCatalog

__all__ = [
    "PROACTIVE_TIER_CEILING",
    "ProactiveDecision",
    "ProactiveState",
    "ProactiveGenerator",
    "validate_proactive_candidate",
    "assert_proactive_candidate",
    "resolve_category",
]

# 능동 제안의 위험도 상한. 컴포트 제안이 explicit/double 확인을 요구하면
# 그것은 컴포트가 아니라 잔소리다(수용률 붕괴) — DUSK 이상은 침묵.
PROACTIVE_TIER_CEILING = RiskTier.BREATH


def resolve_category(
    entity_id: str,
    categories: Mapping[str, str] | None,
    catalog: "DeviceCatalog | None",
) -> str:
    """타깃 엔티티의 카테고리를 결정한다 — app._category_of 와 동일 우선순위.

    명시 categories 맵 → 카탈로그 메타 → 도메인 접두사. 생성기의 안전 거부가
    app/게이트와 같은 분류를 보도록 단일 로직을 공유한다(분류 불일치 =
    안전 우회 구멍).
    """
    if categories and entity_id in categories:
        return categories[entity_id]
    if catalog is not None:
        cat = catalog.category_for(entity_id)
        if cat:
            return cat
    if "." in entity_id:
        return entity_id.split(".", 1)[0]
    if "_" in entity_id:
        return entity_id.split("_", 1)[0]
    return "unknown"


def validate_proactive_candidate(
    candidate: CandidateAction, catalog: "DeviceCatalog | None"
) -> str | None:
    """능동 후보의 금지 사유를 반환한다(None = 통과). deny-by-default.

    검사 순서(모두 통과해야 발의 가능):
      1) (capability, action) ∈ PROACTIVE_ALLOWED — 허용 목록.
      2) category ∉ SAFETY_CRITICAL_CATEGORIES — 컴포트 생성기는 안전
         액추에이터(가스밸브/잠금/오븐/고온계...)를 건드릴 자격이 없다.
         안전 이벤트는 안전 경로(Safety-Never-Silent)가 소유한다.
      3) catalog.risk_for(...) <= BREATH — 카탈로그가 단일 위험 원천.
         미지 (capability, action) 은 catalog 가 EMBER(default-deny) 를
         반환하므로 자동 거부된다. climate 내리기(dusk) 등 방향 비대칭도
         여기서 걸린다.
    """
    if (candidate.capability, candidate.action) not in PROACTIVE_ALLOWED:
        return "not_allowed"
    if (candidate.category or "").strip().lower() in SAFETY_CRITICAL_CATEGORIES:
        return "safety_target"
    if catalog is not None:
        tier = catalog.risk_for(candidate.capability, candidate.action, candidate.params)
        if tier > PROACTIVE_TIER_CEILING:
            return "tier_ceiling"
    return None


def assert_proactive_candidate(
    candidate: CandidateAction, catalog: "DeviceCatalog | None"
) -> None:
    """app 경계의 방어 심층: 금지 능동 후보는 InvariantError (불변식 7)."""
    reason = validate_proactive_candidate(candidate, catalog)
    if reason is not None:
        raise InvariantError(
            f"proactive candidate violates invariant 7 ({reason}): "
            f"{candidate.capability}.{candidate.action} -> {candidate.entity_id} "
            f"[{candidate.category}]"
        )


@dataclass(frozen=True)
class ProactiveDecision:
    """생성기의 단일 결정(불변). candidate 가 None 이면 침묵 + 사유.

    suppressed_by: None | 'no_match' | 'cooldown' | 'budget' |
                   'safety_target' | 'tier_ceiling' | 'not_allowed'
    """

    candidate: CandidateAction | None
    origin: str
    ttl_minutes: int
    matched_rule_ids: tuple[str, ...]
    suppressed_by: str | None


class ProactiveState:
    """쿨다운/예산 상태(주입식, in-memory).

    시각은 항상 주입된 Clock 에서 온다(벽시계 금지 — 테스트 결정론).
    두 LiotApp 이 같은 인스턴스를 공유하면 쿨다운도 공유된다(상태가 앱의
    숨은 속성이 아니라 주입 저장소에 산다는 증명 가능 속성).

    주의: L4 notifier 의 억제 스택(dedup/quiet_hours/예산)과 별개의,
    *생성 시* 게이팅 전용 상태다. 표출 억제는 L4 의 소관이며 여기서
    재구현하지 않는다(이중 억제 시계 금지).
    """

    def __init__(self) -> None:
        # (home_id, entity_id, action) -> 마지막 발화 시각
        self._last_fired: dict[tuple[str, str, str], datetime] = {}
        # home_id -> [(시각, 버킷)] 발화 로그(예산 계산용)
        self._fired: dict[str, list[tuple[datetime, str]]] = {}

    def cooldown_active(
        self, home_id: str, entity_id: str, action: str, now: datetime, minutes: int
    ) -> bool:
        last = self._last_fired.get((home_id, entity_id, action))
        if last is None:
            return False
        return (now - last) < timedelta(minutes=minutes)

    def fired_in_bucket(self, home_id: str, now: datetime, bucket: str) -> int:
        return sum(
            1
            for ts, b in self._fired.get(home_id, [])
            if b == bucket and ts.date() == now.date()
        )

    def fired_today(self, home_id: str, now: datetime) -> int:
        return sum(1 for ts, _ in self._fired.get(home_id, []) if ts.date() == now.date())

    def record(
        self, home_id: str, entity_id: str, action: str, now: datetime, bucket: str
    ) -> None:
        self._last_fired[(home_id, entity_id, action)] = now
        self._fired.setdefault(home_id, []).append((now, bucket))


class ProactiveGenerator:
    """Tier A 결정론 능동 생성기.

    rules(데이터) + 현재 상태(cur) + Clock 으로 결정한다. 네트워크/실행
    표면 없음 — LLM 의 역할은 발의가 아니라 기존 Gate2 의 거부권/승격뿐
    (승격되면 app 의 BREATH 상한이 침묵시키므로, 인젝션된 LLM 의 최악
    결과는 '침묵'이다 — fail-closed).
    """

    def __init__(
        self,
        ruleset: ProactiveRuleSet | None = None,
        rules_dir: str | None = None,
        *,
        catalog: "DeviceCatalog | None" = None,
        categories: Mapping[str, str] | None = None,
        state: ProactiveState | None = None,
        clock: Clock | None = None,
        limits: ProactiveLimits | None = None,
        home_id: str = "singleton",
    ) -> None:
        self.ruleset: ProactiveRuleSet = (
            ruleset if ruleset is not None else load_proactive_rulesets(rules_dir)
        )
        self.catalog = catalog
        self.categories = dict(categories) if categories else {}
        self.state = state if state is not None else ProactiveState()
        self.clock = clock or Clock()
        self.limits = limits if limits is not None else self.ruleset.limits
        self.home_id = home_id

    # ------------------------------------------------------------------
    def _origin_for(self, rule: ProactiveRule) -> str:
        """재현 가능한 출처 토큰: proactive:rule:<id>@<ruleset_hash 8자리>."""
        return f"proactive:rule:{rule.rule_id}@{self.ruleset.ruleset_hash[:8]}"

    def _build_candidate(self, rule: ProactiveRule) -> CandidateAction:
        """규칙 → 후보. category 는 항상 분류 체계(맵→카탈로그→도메인)가
        정한다 — 규칙이 양성 라벨을 자칭해 안전 분류를 우회할 수 없다."""
        category = resolve_category(rule.target_entity, self.categories, self.catalog)
        params: dict[str, Any] = dict(rule.params)
        params["__origin__"] = self._origin_for(rule)
        return CandidateAction(
            capability=rule.capability,
            action=rule.action,
            params=params,
            entity_id=rule.target_entity,
            category=category,
        )

    # ------------------------------------------------------------------
    def propose(self, cur: Mapping[str, Any]) -> ProactiveDecision:
        """현재 상태에서 능동 결정을 내린다. 후보 없음 = 침묵(안전 기본값).

        첫 번째로 (match+state 성립 ∧ 검증 통과 ∧ 쿨다운 비활성) 인 규칙이
        이긴다(선언 순서 우선). 예산 초과면 규칙을 보지도 않는다.
        """
        now = self.clock.now_dt()
        bucket = self.clock.time_bucket()

        # ── 예산(전역) — 알림 피로의 1차 빗장 ─────────────────────────
        if (
            self.state.fired_in_bucket(self.home_id, now, bucket)
            >= self.limits.max_per_bucket
            or self.state.fired_today(self.home_id, now) >= self.limits.max_per_day
        ):
            return ProactiveDecision(
                candidate=None,
                origin="proactive:none",
                ttl_minutes=0,
                matched_rule_ids=(),
                suppressed_by="budget",
            )

        facts = {
            "time_bucket": bucket,
            "occupancy": derive_occupancy(cur, self.categories),
        }
        matched = self.ruleset.matching(facts, cur)
        if not matched:
            return ProactiveDecision(
                candidate=None,
                origin="proactive:none",
                ttl_minutes=0,
                matched_rule_ids=(),
                suppressed_by="no_match",
            )

        last_reason = "no_match"
        for rule in matched:
            candidate = self._build_candidate(rule)
            reason = validate_proactive_candidate(candidate, self.catalog)
            if reason is not None:
                last_reason = reason
                continue
            if self.state.cooldown_active(
                self.home_id,
                rule.target_entity,
                rule.action,
                now,
                self.limits.cooldown_minutes,
            ):
                last_reason = "cooldown"
                continue
            # 발의 확정 — 예산/쿨다운에 즉시 기록(시도 기준 차감: 하류에서
            # 상한 억제로 떨어져도 같은 틱마다 재시도하는 잔소리 루프 방지).
            self.state.record(
                self.home_id, rule.target_entity, rule.action, now, bucket
            )
            return ProactiveDecision(
                candidate=candidate,
                origin=self._origin_for(rule),
                ttl_minutes=rule.ttl_minutes,
                matched_rule_ids=tuple(r.rule_id for r in matched),
                suppressed_by=None,
            )

        return ProactiveDecision(
            candidate=None,
            origin="proactive:none",
            ttl_minutes=0,
            matched_rule_ids=tuple(r.rule_id for r in matched),
            suppressed_by=last_reason,
        )
