"""risk_router(L4) + device_catalog 위험도 비대칭 검증.

  - gate1_verdict 가 없으면 라우팅 거부(InvariantError).
  - required_confirmation 매핑(none/light/explicit/double).
  - lock/unlock · 온도 raise/lower 방향 비대칭.
  - 카탈로그가 더 높으면 승격만(강등 불가).
"""

from __future__ import annotations

import pytest

from liot.core.contracts import CandidateAction, Judgment
from liot.core.invariants import InvariantError
from liot.core.risk import RiskTier
from liot.layers.l4_execution.device_catalog import DeviceCatalog, load_device_catalog
from liot.layers.l4_execution.risk_router import RiskRouter, route


def _judgment(
    *,
    risk_tier: RiskTier,
    action: CandidateAction,
    gate1_verdict: str = "gate1:allow floor=AMBIENT (...)",
) -> Judgment:
    return Judgment(
        judgment_id="jdg_1",
        trace_id="t",
        home_id="singleton",
        snapshot_hash="abc123",
        action=action,
        risk_tier=risk_tier,
        confidence=0.9,
        rationale="test rationale",
        gate1_verdict=gate1_verdict,
        gate2_verdict="concur",
        bundle_id="bundle:deadbeef",
        model_id="fake-llm-v1",
        degraded=False,
    )


def _action(capability: str, action: str, *, category: str, params: dict | None = None) -> CandidateAction:
    return CandidateAction(
        capability=capability,
        action=action,
        params=params or {},
        entity_id=f"{capability}.x",
        category=category,
    )


def test_missing_gate1_verdict_is_rejected() -> None:
    router = RiskRouter()
    act = _action("light", "turn_on", category="ambient")
    bad = _judgment(risk_tier=RiskTier.AMBIENT, action=act, gate1_verdict="")
    with pytest.raises(InvariantError):
        router.route(bad, created_at="2026-06-01T12:00:00+00:00")


@pytest.mark.parametrize(
    "tier,expected",
    [
        (RiskTier.AMBIENT, "none"),
        (RiskTier.BREATH, "light"),
        (RiskTier.DUSK, "explicit"),
        (RiskTier.EMBER, "double"),
    ],
)
def test_required_confirmation_mapping(tier: RiskTier, expected: str) -> None:
    # 모듈 수준 route() 를 검증하는 테스트다(라우터 인스턴스 불필요).
    act = _action("light", "turn_on", category="ambient")
    j = _judgment(risk_tier=tier, action=act)
    proposal = route(j, created_at="2026-06-01T12:00:00+00:00")
    assert proposal.risk_tier == tier
    assert proposal.required_confirmation == expected


def test_catalog_only_escalates_never_downgrades() -> None:
    catalog = load_device_catalog()
    router = RiskRouter(catalog=catalog)

    # unlock 카탈로그 위험도 = EMBER. Judgment 가 AMBIENT 여도 승격.
    unlock = _action("lock", "unlock", category="lock_intent")
    j = _judgment(risk_tier=RiskTier.AMBIENT, action=unlock)
    proposal = router.route(j, created_at="2026-06-01T12:00:00+00:00")
    assert proposal.risk_tier == RiskTier.EMBER

    # 카탈로그가 더 낮아도(lock=BREATH) 이미 높은 Judgment(DUSK)는 강등 안 됨.
    lock = _action("lock", "lock", category="lock_intent")
    j2 = _judgment(risk_tier=RiskTier.DUSK, action=lock)
    proposal2 = router.route(j2, created_at="2026-06-01T12:00:00+00:00")
    assert proposal2.risk_tier == RiskTier.DUSK


def test_lock_unlock_asymmetry() -> None:
    catalog = load_device_catalog()
    assert catalog.risk_for("lock", "lock") == RiskTier.BREATH
    assert catalog.risk_for("lock", "unlock") == RiskTier.EMBER


def test_temperature_direction_asymmetry() -> None:
    catalog = load_device_catalog()
    raise_tier = catalog.risk_for("climate", "set_temperature", {"temperature": +2})
    lower_tier = catalog.risk_for("climate", "set_temperature", {"temperature": -2})
    assert raise_tier == RiskTier.BREATH
    assert lower_tier == RiskTier.DUSK
    assert lower_tier > raise_tier


def test_unknown_action_default_deny_ember() -> None:
    catalog = DeviceCatalog({})
    # 빈 카탈로그: 모든 (capability, action) → default-deny EMBER.
    assert catalog.risk_for("anything", "whatever") == RiskTier.EMBER
