"""위험도 라우터: Judgment -> Proposal.

Phase1 불변식: **실행 호출 절대 금지**. 이 모듈은 Judgment 를 받아
사용자 대면 Proposal 객체를 만드는 데서 멈춘다(액추에이터 호출 없음).

규칙:
  * Judgment 에 ``gate1_verdict`` 가 없으면(빈 문자열/None) InvariantError.
    (Gate1 결정론 가드레일을 통과하지 않은 판단은 라우팅 불가)
  * Proposal.risk_tier 는 Judgment.risk_tier 를 그대로 따른다
    (= max(gate1, gate2). escalate_only/assert_monotonic 로 단조 보장).
  * required_confirmation = 개입 정책(InterventionPolicy)이 정한다. 기본은
    조건 B(위험도 차등 = risk_tier.required_confirmation)이며, 어떤 정책도
    티어 기준선보다 **약한** 확인을 낼 수 없다(개입 하한 — 위반 시
    InvariantError). 조건 A/B 는 여기(L4)서만 갈리고 L2 선택은 동일하다.
  * 카탈로그 위험도가 Judgment 위험도보다 높으면 **승격만**(escalate_only).
    이는 안전 방향(강등 불가, 불변식 5)으로만 작동한다.
"""

from __future__ import annotations

import uuid
from typing import TYPE_CHECKING

from liot.core.contracts import Judgment, Proposal
from liot.core.invariants import InvariantError, assert_monotonic
from liot.core.risk import escalate_only

from .intervention import CONFIRMATION_ORDER, InterventionPolicy, TieredIntervention

if TYPE_CHECKING:  # pragma: no cover
    from liot.core.clock import Clock

    from .device_catalog import DeviceCatalog

__all__ = ["RiskRouter", "route"]


class RiskRouter:
    """Judgment 를 Proposal 로 변환한다(실행하지 않음).

    선택적으로 DeviceCatalog 를 주면, 카탈로그 위험도가 더 높을 때
    위험도를 승격(escalate_only)한다(절대 강등하지 않음).
    """

    def __init__(
        self,
        catalog: DeviceCatalog | None = None,
        clock: Clock | None = None,
        intervention: InterventionPolicy | None = None,
    ) -> None:
        self._catalog = catalog
        self._clock = clock
        # 개입 정책(조건 A/B). 기본 = 조건 B(위험도 차등, 현행과 동일).
        self._intervention: InterventionPolicy = intervention or TieredIntervention()

    def route(
        self,
        judgment: Judgment,
        *,
        perishable: bool = False,
        expires_at: str | None = None,
        created_at: str | None = None,
    ) -> Proposal:
        """Judgment -> Proposal. 액추에이터 호출 없음.

        gate1_verdict 가 비어 있으면 InvariantError.
        """
        if not getattr(judgment, "gate1_verdict", None) or not str(
            judgment.gate1_verdict
        ).strip():
            raise InvariantError(
                "cannot route judgment without a gate1_verdict "
                f"(judgment_id={judgment.judgment_id!r})"
            )

        risk_tier = judgment.risk_tier

        # 카탈로그가 있으면 행동 위험도를 한 번 더 평가해 '승격만' 적용.
        if self._catalog is not None:
            action = judgment.action
            catalog_tier = self._catalog.risk_for(
                action.capability, action.action, action.params
            )
            # 단조 가드: 카탈로그가 더 높으면 승격, 절대 강등하지 않음.
            risk_tier = escalate_only(risk_tier, catalog_tier)
            assert_monotonic(judgment.risk_tier, risk_tier)

        if created_at is None:
            created_at = self._now()

        # 개입 하한: 정책이 티어 기준선보다 약한 확인을 내면 불변식 위반.
        required = self._intervention.confirmation_for(risk_tier)
        baseline = risk_tier.required_confirmation
        if CONFIRMATION_ORDER.get(required, -1) < CONFIRMATION_ORDER[baseline]:
            raise InvariantError(
                f"intervention policy weakened confirmation below tier baseline: "
                f"{required!r} < {baseline!r} (tier={risk_tier.name})"
            )

        return Proposal(
            proposal_id=str(uuid.uuid4()),
            judgment_id=judgment.judgment_id,
            home_id=judgment.home_id,
            risk_tier=risk_tier,
            action=judgment.action,
            rationale=judgment.rationale,
            required_confirmation=required,
            perishable=perishable,
            expires_at=expires_at,
            state="pending",
            created_at=created_at,
        )

    def _now(self) -> str:
        if self._clock is not None:
            return self._clock.now_utc()
        # Clock 미주입 시 지연 import 로 기본 시계 사용.
        from liot.core.clock import Clock

        return Clock().now_utc()


# 모듈 레벨 편의 함수: 카탈로그 없이 단순 변환.
def route(
    judgment: Judgment,
    *,
    catalog: DeviceCatalog | None = None,
    clock: Clock | None = None,
    perishable: bool = False,
    expires_at: str | None = None,
    created_at: str | None = None,
) -> Proposal:
    """Judgment -> Proposal 단발 변환(RiskRouter 의 얇은 래퍼)."""
    return RiskRouter(catalog=catalog, clock=clock).route(
        judgment,
        perishable=perishable,
        expires_at=expires_at,
        created_at=created_at,
    )
