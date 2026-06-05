"""파이프라인 글루(LiotApp).

4개 계층을 ``InProcessBus`` 로 배선하고, 단일 센서 읽기를 받아
L1 정규화 → L2 스냅샷 → L3 Gate1+Gate2 → L4 Proposal 까지 end-to-end 로
흘린다. 레이어 간 이동은 모두 허용된 토폴로지 엣지
(L1→L2, L2→L3, L3→L4)로 EventMessage 를 publish 해 단방향성을
런타임에 강제한다(불변식 1).

핵심 책무:
  - process_reading(reading) -> Proposal | None
      하나의 SensorReading 을 받아 (스냅샷이 필요한 경우) 하나의 Proposal 을
      만든다. 스냅샷이 불필요하면(의미 변화 미달) None.
  - 각 단계 진입 시 EventBus 로 (layer_from → layer_to) 메시지를 publish 한다.
    핸들러는 동일 trace 의 단계 산출물을 stash 에 보관해 동기적으로 이어 붙인다.

Phase1: 실행 코드 없음. L4 는 Proposal 생성에서 멈춘다.
"""

from __future__ import annotations

import uuid
from datetime import timedelta
from typing import Any

from liot.core.clock import Clock
from liot.core.contracts import (
    CandidateAction,
    ContextSnapshot,
    EventMessage,
    Judgment,
    Layer,
    Proposal,
    SensorReading,
)
from liot.core.egress_gate import AliasMap
from liot.core.event_bus import InProcessBus
from liot.core.risk import SAFETY_CRITICAL_CATEGORIES
from liot.layers.l1_sensing.normalizer import normalize
from liot.layers.l1_sensing.state_cache import StateCache
from liot.layers.l2_context.context_builder import ContextBuilder
from liot.layers.l2_context.proactive import (
    PROACTIVE_TIER_CEILING,
    ProactiveDecision,
    ProactiveGenerator,
    assert_proactive_candidate,
)
from liot.layers.l3_judgment.guardrail import Guardrail
from liot.layers.l3_judgment.llm_judge import LLMBackend, LLMJudge
from liot.layers.l4_execution.device_catalog import DeviceCatalog, load_device_catalog
from liot.layers.l4_execution.risk_router import RiskRouter

__all__ = ["LiotApp"]


class LiotApp:
    """LIOT Phase1 파이프라인 글루.

    레이어를 InProcessBus 에 배선하고, process_reading 으로 단일 읽기를
    Proposal 까지 변환한다. 모든 레이어 전이는 허용 토폴로지 엣지로만 흐른다.
    """

    def __init__(
        self,
        *,
        home_id: str = "singleton",
        clock: Clock | None = None,
        catalog: DeviceCatalog | None = None,
        guardrail: Guardrail | None = None,
        llm_backend: LLMBackend | None = None,
        categories: dict[str, str] | None = None,
        proactive: ProactiveGenerator | None = None,
    ) -> None:
        self.home_id = home_id
        self.clock = clock or Clock()
        self.bus = InProcessBus()

        # entity_id -> category 매핑. 카탈로그가 있으면 그 메타에서 보강한다.
        self._categories: dict[str, str] = dict(categories or {})

        # ── 레이어 컴포넌트 ──────────────────────────────────────────────
        self.catalog = catalog if catalog is not None else load_device_catalog()
        if not self._categories:
            self._categories = self._categories_from_catalog(self.catalog)

        self.state_cache = StateCache()
        self.context_builder = ContextBuilder(
            clock=self.clock, home_id=home_id, categories=self._categories
        )
        self.guardrail = guardrail if guardrail is not None else Guardrail()
        self.judge = LLMJudge(backend=llm_backend)
        self.router = RiskRouter(catalog=self.catalog, clock=self.clock)
        # 능동 생성기(Phase2). 기본은 config/proactive/*.yaml 의 규칙셋 —
        # 디렉터리가 없으면 빈 규칙셋(능동 제안 없음, 안전한 기본값).
        self.proactive = (
            proactive
            if proactive is not None
            else ProactiveGenerator(
                catalog=self.catalog,
                categories=self._categories,
                clock=self.clock,
                home_id=home_id,
            )
        )

        # 한 번의 process_reading 동안 단계 산출물을 잇기 위한 trace-stash.
        self._stash: dict[str, dict[str, Any]] = {}

        self._wire()

    # ------------------------------------------------------------------
    # 배선
    # ------------------------------------------------------------------
    def _wire(self) -> None:
        """허용 토폴로지(L1→L2, L2→L3, L3→L4)에 핸들러를 등록한다."""
        self.bus.subscribe(Layer.L2, self._on_l1_to_l2)
        self.bus.subscribe(Layer.L3, self._on_l2_to_l3)
        self.bus.subscribe(Layer.L4, self._on_l3_to_l4)

    @staticmethod
    def _categories_from_catalog(catalog: DeviceCatalog) -> dict[str, str]:
        cats: dict[str, str] = {}
        for eid, dev in getattr(catalog, "_devices", {}).items():
            cat = dev.get("category")
            if cat:
                cats[str(eid)] = str(cat)
        return cats

    # ------------------------------------------------------------------
    # 카테고리 추론
    # ------------------------------------------------------------------
    def _category_of(self, entity_id: str) -> str:
        if entity_id in self._categories:
            return self._categories[entity_id]
        cat = self.catalog.category_for(entity_id)
        if cat:
            return cat
        # 도메인 접두사 추정(예: 'light.living_room' -> 'light').
        if "." in entity_id:
            return entity_id.split(".", 1)[0]
        if "_" in entity_id:
            return entity_id.split("_", 1)[0]
        return "unknown"

    def _capability_of(self, entity_id: str, category: str) -> str:
        cap = self.catalog.capability_for(entity_id)
        if cap:
            return cap
        if "." in entity_id:
            return entity_id.split(".", 1)[0]
        return category

    # ------------------------------------------------------------------
    # 단계 핸들러 (EventBus 구독)
    # ------------------------------------------------------------------
    async def _on_l1_to_l2(self, msg: EventMessage) -> None:
        """L1→L2: 캐시 상태로 ContextSnapshot 을 빚는다(필요 시)."""
        stash = self._stash[msg.trace_id]
        reading: SensorReading = stash["reading"]

        prev = stash.get("prev_norm")
        cur = self.state_cache.norm_values()

        category = self._category_of(reading.entity_id)
        # 트리거 정보: 어떤 엔티티/카테고리가 흐름을 촉발했는가.
        trigger: dict[str, Any] = {
            "kind": "sensor_reading",
            "entity_id": reading.entity_id,
            "category": category,
            "time_bucket": self.clock.time_bucket(),
        }

        force = bool(stash.get("force_snapshot"))
        if not self.context_builder.should_snapshot(prev, cur, force_tick=force):
            stash["snapshot"] = None
            return

        snapshot = self.context_builder.build(
            cur,
            trigger,
            prev=prev,
            trace_id=msg.trace_id,
            alias=AliasMap(),
        )
        stash["snapshot"] = snapshot

        await self.bus.publish(
            EventMessage(
                trace_id=msg.trace_id,
                home_id=self.home_id,
                layer_from=Layer.L2,
                layer_to=Layer.L3,
                type="context_snapshot",
                ref_id=snapshot.snapshot_id,
                partition_key=self.home_id,
                ts=self.clock.now_utc(),
            )
        )

    async def _on_l2_to_l3(self, msg: EventMessage) -> None:
        """L2→L3: Gate1(guardrail) + Gate2(LLM) 으로 Judgment 를 만든다.

        세 분기(능동/명시/안전)가 각각 정확히 하나의 CandidateAction 을
        내고, 단일 gate1/judge 흐름이 그 후보를 소비한다.
        """
        stash = self._stash[msg.trace_id]
        snapshot: ContextSnapshot = stash["snapshot"]
        decision: ProactiveDecision | None = stash.get("proactive")

        if decision is not None and decision.candidate is not None:
            # ── 능동 분기(tick 진입) ──────────────────────────────────
            # 후보는 생성기가 이미 검증했지만, 경계에서 재확인한다
            # (불변식 7 방어 심층: 금지 후보는 InvariantError).
            candidate = decision.candidate
            assert_proactive_candidate(candidate, self.catalog)
        else:
            reading: SensorReading = stash["reading"]
            category = self._category_of(reading.entity_id)

            # ── 센서/액추에이터 분리(알림 피로 방지) ──────────────────
            # 관찰(observation)은 맥락만 갱신하고 제안을 만들지 않는다.
            # 제안은 (a) 명시적 action 이 주어졌거나(요청), 또는 (b) 안전
            # 임계 카테고리(가스/연기/누수/문/잠금 ...)일 때만 생성한다.
            # 그 외 읽기전용 센서·액추에이터 상태변화는 침묵.
            # (능동 제안은 센서 경로가 아니라 tick() 경로로만 들어온다.)
            explicit_action = stash.get("action_name")
            is_safety = category in SAFETY_CRITICAL_CATEGORIES
            if explicit_action is None and not is_safety:
                stash["proposal"] = None  # 관찰 → 맥락만, 제안 없음
                return

            capability = self._capability_of(reading.entity_id, category)
            action_name = explicit_action or self._default_action(category, reading)
            params: dict[str, Any] = dict(stash.get("params") or {})
            # 출처 통일(수용률 측정의 분모 무결성): 세 경로 모두 __origin__
            # 을 갖는다. 호출자가 명시한 __origin__ 은 보존한다.
            params.setdefault(
                "__origin__",
                "explicit:external" if explicit_action else "safety:auto",
            )

            candidate = CandidateAction(
                capability=capability,
                action=action_name,
                params=params,
                entity_id=reading.entity_id,
                category=category,
            )
        stash["candidate"] = candidate

        gate1 = self.guardrail.evaluate(candidate, snapshot)
        # ── 불변식 7: 능동 제안의 BREATH 상한(판단 전 차단) ───────────────
        # 게이트가 능동 후보를 DUSK 이상으로 보거나 차단하면 그 제안은
        # '컴포트'가 아니다 → 표출하지 않는다(억제 = 침묵. 위험도 강등이
        # 아니므로 불변식 5와 무관). 안전 이벤트는 이 분기에 오지 않는다.
        if decision is not None and (
            gate1.blocked or gate1.floor_tier > PROACTIVE_TIER_CEILING
        ):
            stash["proposal"] = None
            return

        judgment = self.judge.judge(
            candidate,
            snapshot,
            gate1,
            ruleset_version=self.guardrail.ruleset_version,
            trace_id=msg.trace_id,
            home_id=self.home_id,
        )
        # 불변식 7 후단: LLM(Gate2)이 BREATH 위로 승격시킨 능동 제안도 억제.
        # 인젝션된 LLM 의 최악 결과 = 침묵(fail-closed).
        if decision is not None and judgment.risk_tier > PROACTIVE_TIER_CEILING:
            stash["proposal"] = None
            return
        stash["judgment"] = judgment

        await self.bus.publish(
            EventMessage(
                trace_id=msg.trace_id,
                home_id=self.home_id,
                layer_from=Layer.L3,
                layer_to=Layer.L4,
                type="judgment",
                ref_id=judgment.judgment_id,
                partition_key=self.home_id,
                ts=self.clock.now_utc(),
            )
        )

    async def _on_l3_to_l4(self, msg: EventMessage) -> None:
        """L3→L4: Judgment 를 Proposal 로 라우팅한다(실행 없음).

        능동 제안은 expires_at(신선도)을 갖는다. perishable 은 False 로
        둔다 — L4 notifier 에서 perishable=True 는 예산/정숙시간 *우회*
        의미라서 컴포트에 쓰면 오히려 잔소리가 된다.
        """
        stash = self._stash[msg.trace_id]
        judgment: Judgment = stash["judgment"]
        proposal = self.router.route(
            judgment,
            expires_at=stash.get("expires_at"),
            created_at=self.clock.now_utc(),
        )
        stash["proposal"] = proposal

    # ------------------------------------------------------------------
    # 후보 행동 기본값
    # ------------------------------------------------------------------
    def _default_action(self, category: str, reading: SensorReading) -> str:
        """카테고리/정규값으로 합리적 기본 action 이름을 고른다."""
        if category in SAFETY_CRITICAL_CATEGORIES:
            return "alert"
        norm = reading.norm_value
        if category in ("light", "ambient"):
            return "turn_on" if norm else "turn_off"
        return "suggest"

    # ------------------------------------------------------------------
    # 공개 API
    # ------------------------------------------------------------------
    async def tick(self) -> Proposal | None:
        """주기적 컴포트 틱(Phase2 능동성) — "AI 가 먼저 말을 거는" 진입점.

        센서 cadence 와 분리된 주기 호출용이다. should_snapshot 의 양성
        변화 억제는 그대로 두고(알림 피로 방지선 불변), 틱이 자체적으로
        맥락을 평가한다:

          1) 생성기(ProactiveGenerator)가 현재 상태에서 결정을 내린다.
             결정 없음(no_match/쿨다운/예산/검증실패) → None (침묵 =
             Proactive-Safe-Silent, 안전한 기본값).
          2) 결정이 있으면 그 결정 자체를 trigger 로 삼아 스냅샷을 빚는다
             (L2 소관 작업) → snapshot_hash 가 판단된 후보를 고정한다
             (불변식 4: 같은 스냅샷 = 같은 후보, 재현 가능).
          3) L2→L3 허용 엣지로 publish — 기존 Gate1+Gate2+Router 를
             그대로 통과한다. BREATH 상한 초과는 침묵(불변식 7).
        """
        cur = self.state_cache.norm_values()
        decision = self.proactive.propose(cur)
        if decision.candidate is None:
            return None

        cand = decision.candidate
        trace_id = uuid.uuid4().hex
        expires_at = (
            (self.clock.now_dt() + timedelta(minutes=decision.ttl_minutes))
            .replace(microsecond=0)
            .isoformat()
        )

        # 결정이 곧 트리거다: 무엇이(규칙 출처) 무엇을(타깃/행동) 발의했는지가
        # structured 에 박혀 snapshot_hash 로 고정된다.
        trigger: dict[str, Any] = {
            "kind": "proactive",
            "origin": decision.origin,
            "entity_id": cand.entity_id,
            "capability": cand.capability,
            "action": cand.action,
            "category": cand.category,
            "time_bucket": self.clock.time_bucket(),
        }
        snapshot = self.context_builder.build(
            cur,
            trigger,
            prev=cur or None,  # 틱은 상태 변화가 아니라 기회 평가
            trace_id=trace_id,
            alias=AliasMap(),
        )

        self._stash[trace_id] = {
            "reading": None,  # 틱은 센서 읽기 없이 진입한다(합성 읽기 금지)
            "prev_norm": cur or None,
            "action_name": None,
            "params": None,
            "force_snapshot": True,
            "proactive": decision,
            "expires_at": expires_at,
            "snapshot": snapshot,
            "judgment": None,
            "proposal": None,
        }
        try:
            await self.bus.publish(
                EventMessage(
                    trace_id=trace_id,
                    home_id=self.home_id,
                    layer_from=Layer.L2,
                    layer_to=Layer.L3,
                    type="context_snapshot",
                    ref_id=snapshot.snapshot_id,
                    partition_key=self.home_id,
                    ts=self.clock.now_utc(),
                )
            )
            return self._stash[trace_id].get("proposal")
        finally:
            self._stash.pop(trace_id, None)

    async def process_reading(
        self,
        reading: SensorReading,
        *,
        action: str | None = None,
        params: dict[str, Any] | None = None,
        force_snapshot: bool = False,
    ) -> Proposal | None:
        """단일 SensorReading 을 end-to-end 로 흘려 Proposal 을 만든다.

        L1 정규화(필요 시 norm_value 보정) → L2 스냅샷(Gate B: should_snapshot)
        → L3 Gate1+Gate2 → L4 Proposal. 스냅샷이 불필요하면 None.

        action/params 를 주면 후보 행동을 명시할 수 있다(미지정 시 카테고리
        기반 기본값). 모든 레이어 이동은 EventBus 허용 엣지로만 흐른다.
        """
        # L1 정규화: norm_value 가 비어 있으면 raw 로부터 보정.
        if reading.norm_value is None and reading.raw_value is not None:
            reading = reading.model_copy(
                update={"norm_value": normalize(reading.raw_value)}
            )

        trace_id = uuid.uuid4().hex

        # L1 캐시 갱신(역행 방지). 변화 전/후 비교용 prev 보관.
        prev_norm = self.state_cache.norm_values()
        self.state_cache.update(reading)

        self._stash[trace_id] = {
            "reading": reading,
            "prev_norm": prev_norm or None,
            "action_name": action,
            "params": params,
            "force_snapshot": force_snapshot,
            "snapshot": None,
            "judgment": None,
            "proposal": None,
        }
        try:
            # L1→L2 진입(허용 엣지). 이후 단계는 핸들러가 동기 연쇄로 잇는다.
            await self.bus.publish(
                EventMessage(
                    trace_id=trace_id,
                    home_id=self.home_id,
                    layer_from=Layer.L1,
                    layer_to=Layer.L2,
                    type="sensor_reading",
                    ref_id=reading.entity_id,
                    partition_key=self.home_id,
                    ts=self.clock.now_utc(),
                )
            )
            return self._stash[trace_id].get("proposal")
        finally:
            self._stash.pop(trace_id, None)
