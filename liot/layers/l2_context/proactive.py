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
  1) 로드: proactive_rules 로더가 카탈로그 파생 허용목록 밖 선언을 거부.
  2) 생성: validate_proactive_candidate — 허용 목록 + 안전 카테고리 타깃
     거부 + catalog.risk_for <= BREATH (카탈로그가 단일 위험 원천).
  3) 판단 후: app 이 게이트/LLM 이 BREATH 위로 승격시킨 능동 제안을
     *억제*(침묵)한다. 강등이 아니라 미표출 — 불변식 5 무관.

재현성(불변식 4): 결정의 출처 ``proactive:rule:<id>@<ruleset_hash 8자리>`` 가
후보 params["__origin__"] 에 실리고, app.tick 이 결정 전체를 스냅샷
trigger 로 삼아 snapshot_hash 가 판단된 후보를 고정한다.
"""

from __future__ import annotations

import weakref
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Any

from liot.core.categories import resolve_category
from liot.core.clock import Clock
from liot.core.contracts import (
    ActionCandidate,
    CandidateAction,
    CandidateSet,
    ExternalContext,
    compute_snapshot_hash,
    make_noop_candidate,
)
from liot.core.invariants import InvariantError
from liot.core.risk import RiskTier, is_safety_category

from .candidate_llm import LLMCandidateBackend, parse_llm_candidates
from .context_builder import derive_occupancy
from .feedback_store import candidate_key
from .proactive_rules import (
    ProactiveLimits,
    ProactiveRule,
    ProactiveRuleSet,
    derive_proactive_allowed,
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


# 카탈로그 인스턴스별 파생 허용목록 메모(약한 참조 키 — 카탈로그가 GC 되면 자동 제거).
# 데모는 요청마다 새 DeviceCatalog(registry.to_catalog())를 만들지만, 한 생성기의
# 수명 안에선 같은 catalog 로 후보 검증이 반복되므로 재파생을 피한다.
_ALLOWED_MEMO: weakref.WeakKeyDictionary[DeviceCatalog, frozenset[tuple[str, str]]] = (
    weakref.WeakKeyDictionary()
)
# catalog=None 경로(카탈로그 미주입 생성기)의 출하 기본 허용목록(첫 사용 시 1회 파생).
_DEFAULT_ALLOWED: frozenset[tuple[str, str]] | None = None


def _proactive_allowed(catalog: DeviceCatalog | None) -> frozenset[tuple[str, str]]:
    """catalog 에서 파생한 능동 허용목록(인스턴스별 메모). catalog=None → 출하 기본.

    catalog 가 없으면 출하 ``load_device_catalog()`` 파생분(=구 고정 6튜플 허용목록)
    을 쓴다 — 구 상수 기반 검증과 동치를 보존한다.
    """
    global _DEFAULT_ALLOWED
    if catalog is None:
        if _DEFAULT_ALLOWED is None:
            from liot.layers.l4_execution.device_catalog import load_device_catalog

            _DEFAULT_ALLOWED = derive_proactive_allowed(load_device_catalog())
        return _DEFAULT_ALLOWED
    cached = _ALLOWED_MEMO.get(catalog)
    if cached is None:
        cached = derive_proactive_allowed(catalog)
        _ALLOWED_MEMO[catalog] = cached
    return cached


def validate_proactive_candidate(
    candidate: CandidateAction, catalog: DeviceCatalog | None
) -> str | None:
    """능동 후보의 금지 사유를 반환한다(None = 통과). deny-by-default.

    검사 순서(모두 통과해야 발의 가능):
      1) (capability, action) ∈ 카탈로그 파생 허용목록(derive_proactive_allowed) —
         catalog 미주입 시 출하 기본 파생분(구 고정 허용목록과 동치).
      2) category ∉ SAFETY_CRITICAL_CATEGORIES — 컴포트 생성기는 안전
         액추에이터(가스밸브/잠금/오븐/고온계...)를 건드릴 자격이 없다.
         안전 이벤트는 안전 경로(Safety-Never-Silent)가 소유한다.
      3) capability 가 카탈로그의 실제 capability 와 일치 — owner 가
         ``categories`` 오버라이드로 가스밸브를 'light' 라 거짓 분류해도,
         capability 는 물리적 사실(밸브는 밸브)이라 거짓말이 안 통한다.
         규칙이 엔티티의 정체(catalog capability)와 다른 capability 를
         자칭하면 거부('capability_mismatch'). 카탈로그가 모르는 엔티티는
         제약 없음(양성 도메인 추정만 — V2 정상 경로 보존). 카테고리는
         owner 가 정당히 재분류할 수 있어도(고온계→climate), capability 는
         재분류 대상이 아니라는 비대칭이 안전 우회를 외과적으로 막는다.
      4) catalog.risk_for(...) <= BREATH — 카탈로그가 단일 위험 원천.
         미지 (capability, action) 은 catalog 가 EMBER(default-deny) 를
         반환하므로 자동 거부된다. climate 내리기(dusk) 등 방향 비대칭도
         여기서 걸린다.
    """
    if (candidate.capability, candidate.action) not in _proactive_allowed(catalog):
        return "not_allowed"
    if is_safety_category(candidate.category):
        return "safety_target"
    if catalog is not None:
        true_cap = catalog.capability_for(candidate.entity_id)
        if true_cap is not None and true_cap != candidate.capability:
            return "capability_mismatch"
        tier = catalog.risk_for(candidate.capability, candidate.action, candidate.params)
        if tier > PROACTIVE_TIER_CEILING:
            return "tier_ceiling"
    return None


def assert_proactive_candidate(
    candidate: CandidateAction, catalog: DeviceCatalog | None
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

    suppressed_by: None | 'no_match' | 'cooldown' | 'budget' | 'rejected' |
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

    @staticmethod
    def _same_day(ts: datetime, now: datetime) -> bool:
        """같은 날인가 — now 의 시간대(집 지역 시각)로 센 날짜끼리 비교한다."""
        return ts.astimezone(now.tzinfo).date() == now.date()

    def fired_in_bucket(self, home_id: str, now: datetime, bucket: str) -> int:
        return sum(
            1
            for ts, b in self._fired.get(home_id, [])
            if b == bucket and self._same_day(ts, now)
        )

    def fired_today(self, home_id: str, now: datetime) -> int:
        return sum(1 for ts, _ in self._fired.get(home_id, []) if self._same_day(ts, now))

    def record(
        self, home_id: str, entity_id: str, action: str, now: datetime, bucket: str
    ) -> None:
        self._last_fired[(home_id, entity_id, action)] = now
        self._fired.setdefault(home_id, []).append((now, bucket))


class ProactiveGenerator:
    """Tier A 결정론 능동 생성기 + 다중 후보 집합 빌더.

    rules(데이터) + 현재 상태(cur) + Clock 으로 결정한다. 네트워크/실행
    표면 없음. LLM 은 (옵션) 후보를 *제안*할 수 있으나 두 관문(pydantic
    구조 검증 + validate_proactive_candidate)을 통과해야 하고, 위험도는
    기존 Gate2 가 올릴 수만 있다(승격되면 app 의 BREATH 상한이 침묵시키므로,
    인젝션된 LLM 의 최악 결과는 '침묵'이다 — fail-closed, 불변식 2′).
    """

    def __init__(
        self,
        ruleset: ProactiveRuleSet | None = None,
        rules_dir: str | None = None,
        *,
        catalog: DeviceCatalog | None = None,
        categories: Mapping[str, str] | None = None,
        state: ProactiveState | None = None,
        clock: Clock | None = None,
        limits: ProactiveLimits | None = None,
        home_id: str = "singleton",
        llm_candidate_backend: LLMCandidateBackend | None = None,
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
        # LLM 후보 보조 생성기(옵션). None 이면 규칙만으로 후보를 만든다 —
        # 기본 파이프라인 동작을 바꾸지 않기 위해 기본값은 None.
        self.llm_candidate_backend = llm_candidate_backend

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
    def propose(
        self,
        cur: Mapping[str, Any],
        *,
        external: ExternalContext | None = None,
        rejected_keys: frozenset[str] = frozenset(),
    ) -> ProactiveDecision:
        """현재 상태에서 능동 결정을 내린다. 후보 없음 = 침묵(안전 기본값).

        첫 번째로 (match+state+external 성립 ∧ 검증 통과 ∧ 최근 거절 아님 ∧ 쿨다운
        비활성) 인 규칙이 이긴다(선언 순서 우선). 예산 초과면 규칙을 보지도 않는다.

        ``rejected_keys``: 최근(30일) 거절된 후보 키(`cap:action:entity:category`). 그 키에
        걸린 규칙은 쿨다운·예산을 쓰기 **전에** 건너뛴다 — 거절된 행동은 어차피 고르기와
        알림 게이트에서 막히므로, 그 자리를 차지하면 같은 시간대의 다른 제안만 밀려난다.
        """
        # 집 지역 시각: 예산의 '하루'·시간대 묶음이 그 집의 날짜로 갈린다(UTC 자정이 아니라).
        # 쿨다운은 시각 차이라 시간대와 무관하다. tz 미지정 Clock 이면 UTC 그대로다.
        now = self.clock.local_dt()
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

        facts = self._gen_facts(cur, external)
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
            if candidate_key(candidate) in rejected_keys:
                last_reason = "rejected"
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

    # ------------------------------------------------------------------
    # 다중 후보 (새 기획: 생성 → 평가 → 선택). propose() 는 보존하고 그 위에
    # 후보 집합(다중 + 무행동)을 *추가*한다. 선택 구동은 Phase3 평가기로.
    # ------------------------------------------------------------------
    def _gen_facts(
        self, cur: Mapping[str, Any], external: ExternalContext | None = None
    ) -> dict[str, Any]:
        """규칙이 읽는 맥락 사실. 외부 맥락이 있으면 ``external`` 로 싣는다(해시에도 들어간다).

        외부 맥락이 없으면 키 자체를 넣지 않는다 — 외부 없는 경로의 후보 해시는 예전 그대로.
        """
        facts: dict[str, Any] = {
            "time_bucket": self.clock.time_bucket(),
            "occupancy": derive_occupancy(cur, self.categories),
        }
        if external is not None:
            facts["external"] = external.rule_facts()
        return facts

    def candidate_set_for(
        self,
        cur: Mapping[str, Any],
        decision: ProactiveDecision,
        *,
        trace_id: str = "",
        external: ExternalContext | None = None,
    ) -> CandidateSet:
        """이미 내려진 decision 을 선택으로 삼아 후보 집합을 빚는다(읽기 전용).

        후보 풀 = 검증 통과 규칙 후보 + (있으면) 검증 통과 LLM 후보 + 무행동.
        선택(selected_index)은 decision.candidate(=propose 승자) 또는 무행동.
        상태(쿨다운/예산) 변경은 하지 않는다 — 그건 propose() 의 몫이다.
        """
        facts = self._gen_facts(cur, external)
        gen_hash = compute_snapshot_hash({"facts": dict(facts), "cur": dict(cur)})

        cands: list[ActionCandidate] = []
        seen: set[tuple[str, str, str]] = set()

        def _add(c: CandidateAction, origin: str, benefit: float | None = None) -> None:
            key = (c.capability, c.action, c.entity_id)
            if key in seen:
                return
            seen.add(key)
            cands.append(ActionCandidate(candidate=c, origin=origin, declared_benefit=benefit))

        # 규칙 후보(검증 통과만). 규칙이 선언한 기대 효과(benefit)를 후보에 싣는다.
        for rule in self.ruleset.matching(facts, cur):
            c = self._build_candidate(rule)
            if validate_proactive_candidate(c, self.catalog) is None:
                _add(c, f"rule:{rule.rule_id}", rule.benefit)

        # LLM 후보(검증 통과만 — 금지/화이트리스트 밖은 폐기, fail-closed).
        if self.llm_candidate_backend is not None:
            raw = self.llm_candidate_backend.propose_candidates(facts, cur)
            for c in parse_llm_candidates(raw):
                if validate_proactive_candidate(c, self.catalog) is not None:
                    continue
                if "__origin__" not in c.params:
                    c = c.model_copy(
                        update={"params": {**c.params, "__origin__": "proactive:llm"}}
                    )
                _add(c, "llm")

        # 무행동 후보(항상 포함, 마지막).
        noop_idx = len(cands)
        cands.append(ActionCandidate(candidate=make_noop_candidate(), origin="noop"))

        # 선택 = propose 승자(있으면 그 후보) 또는 무행동.
        selected = noop_idx
        if decision.candidate is not None:
            wkey = (
                decision.candidate.capability,
                decision.candidate.action,
                decision.candidate.entity_id,
            )
            found = next(
                (
                    i
                    for i, ac in enumerate(cands)
                    if (
                        ac.candidate.capability,
                        ac.candidate.action,
                        ac.candidate.entity_id,
                    )
                    == wkey
                ),
                None,
            )
            if found is None:
                # 승자가 풀에 없으면(이론상 드묾) 무행동 앞에 끼워 넣는다.
                cands.insert(
                    noop_idx,
                    ActionCandidate(candidate=decision.candidate, origin="rule"),
                )
                selected = noop_idx
            else:
                selected = found

        return CandidateSet(
            trace_id=trace_id,
            home_id=self.home_id,
            snapshot_hash=gen_hash,
            candidates=cands,
            selected_index=selected,
        )

    def generate(
        self,
        cur: Mapping[str, Any],
        *,
        trace_id: str = "",
        external: ExternalContext | None = None,
    ) -> CandidateSet:
        """현재 상태에서 후보 집합을 만든다 = propose(선택+상태) + 후보 데코.

        Phase2 임시 선택은 propose() 승자(최우선 유효 규칙) 또는 무행동이다.
        Phase3 평가기가 이 선택을 점수 기반으로 대체한다.
        """
        decision = self.propose(cur, external=external)
        return self.candidate_set_for(cur, decision, trace_id=trace_id, external=external)
