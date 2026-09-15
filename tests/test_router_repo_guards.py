"""라우터·저장소·개입 정책의 미고정 가드(2026-09-16 야간 스윕 2라운드).

전부 '뒤집어도 전체 스위트가 초록'이던 자리다. 공통점은 **기본값과 경계**다:
perishable 기본값, created_at/now 인자 존중, 개입 확인 강도의 '같을 때'.
"""

from __future__ import annotations

import pytest

from liot.core.clock import Clock
from liot.core.contracts import CandidateAction, Judgment
from liot.core.invariants import InvariantError
from liot.core.risk import RiskTier
from liot.db.database import init_db
from liot.layers.l4_execution.intervention import (
    UniformIntervention,
    build_intervention,
)
from liot.layers.l4_execution.proposal_service import ProposalService
from liot.layers.l4_execution.risk_router import RiskRouter

DAY = "2026-06-02T12:00:00+00:00"


def _judgment(verdict: str = "gate1:ok", tier: RiskTier = RiskTier.BREATH) -> Judgment:
    return Judgment(
        judgment_id="j-1", trace_id="t", home_id="singleton", snapshot_hash="h" * 8,
        action=CandidateAction(capability="light", action="turn_on", params={},
                               entity_id="light.living_room", category="light"),
        risk_tier=tier, confidence=0.5, rationale="r", gate1_verdict=verdict,
        bundle_id="b", model_id="m", degraded=False,
    )


# ── perishable 기본값 ───────────────────────────────────────────────────
def test_proposals_are_not_perishable_by_default() -> None:
    """기본은 시의성 없음 — 기본이 True 로 뒤집히면 모든 알림이 발화 게이트를 우회한다.

    perishable 은 dedup·정숙시간·예산을 **우회**하는 표식이다. 기본값이 바뀌면
    잔소리 방지 장치가 통째로 무력화되는데, 스윕에서 세 곳(라우터·서비스 두 진입점)을
    True 로 바꿔도 아무 테스트가 울지 않았다.
    """
    proposal = RiskRouter(clock=Clock(fixed=DAY)).route(_judgment())
    assert proposal.perishable is False


async def test_service_build_and_create_keep_perishable_false(tmp_path) -> None:
    db = await init_db(":memory:")
    try:
        svc = ProposalService(db, clock=Clock(fixed=DAY))
        built = svc.build(_judgment())
        created = await svc.create(_judgment())
        assert built.perishable is False and created.perishable is False
    finally:
        await db.close()


# ── 라우터의 입력 검증·시각 처리 ────────────────────────────────────────
@pytest.mark.parametrize("verdict", ["", "   "])
def test_router_refuses_judgment_without_gate1_verdict(verdict: str) -> None:
    """gate1_verdict 가 비었으면 라우팅 거부 — 빈 문자열도 '없음'이다."""
    with pytest.raises(InvariantError, match="gate1_verdict"):
        RiskRouter(clock=Clock(fixed=DAY)).route(_judgment(verdict=verdict))


def test_router_honours_explicit_created_at_and_falls_back_to_clock() -> None:
    """created_at 을 주면 그대로 쓰고, 없으면 시계에서 받는다."""
    router = RiskRouter(clock=Clock(fixed=DAY))
    explicit = router.route(_judgment(), created_at="2020-01-01T00:00:00+00:00")
    assert explicit.created_at == "2020-01-01T00:00:00+00:00"
    assert router.route(_judgment()).created_at == DAY


# ── 저장소의 now 인자 ───────────────────────────────────────────────────
async def test_expire_due_uses_the_given_now(tmp_path) -> None:
    """expire_due(now=...) 가 실제로 기준 시각으로 쓰인다(시계 값에 먹히지 않는다)."""
    db = await init_db(":memory:")
    try:
        svc = ProposalService(db, clock=Clock(fixed=DAY))
        proposal = await svc.create(_judgment(), expires_at="2026-06-02T12:30:00+00:00")
        # 만료 전 시각으로 물으면 아무것도 만료되지 않는다.
        assert await svc.expire_due(now="2026-06-02T12:00:00+00:00") == 0
        # 만료 뒤 시각으로 물으면 만료된다 — 시계(DAY=12:00)가 아니라 인자가 기준이다.
        assert await svc.expire_due(now="2026-06-02T13:00:00+00:00") == 1
        got = await svc.get(proposal.proposal_id)
        assert got is not None and got.state == "expired"
    finally:
        await db.close()


# ── 개입 정책의 '같을 때' ───────────────────────────────────────────────
def test_uniform_intervention_keeps_stronger_or_equal_baseline() -> None:
    """조건 A: 기준선이 같거나 더 강하면 기준선을 따르고, 약하면 정책으로 강화한다.

    스윕의 `>=`→`>` 뮤턴트는 **등가**다: 같을 때 base 를 돌려주나 baseline 을
    돌려주나 문자열이 같다. 그래도 이 표는 남긴다 — 네 티어의 확인 강도가
    '정책은 강화만 한다'는 개입 하한을 만족하는지가 여기서 읽힌다.
    """
    pol = UniformIntervention()
    assert pol.confirmation_for(RiskTier.DUSK) == "explicit"   # 같음 → 기준선 유지
    assert pol.confirmation_for(RiskTier.EMBER) == "double"    # 더 강함 → 그대로
    assert pol.confirmation_for(RiskTier.AMBIENT) == "explicit"  # 더 약함 → 정책으로 강화
    assert pol.confirmation_for(RiskTier.BREATH) == "explicit"


def test_build_intervention_rejects_unknown_condition() -> None:
    assert build_intervention("A").condition == "A"
    assert build_intervention(None).condition == "B"
    with pytest.raises(ValueError, match="condition"):
        build_intervention("C")
