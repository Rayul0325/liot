"""Gate1(guardrail) 결정론 가드레일 검증.

  - deny-by-default: 미지 카테고리 → EMBER floor.
  - set_floor 는 escalate_only(max) 로만 클램프(강등 불가).
  - 안전 임계 카테고리 → EMBER floor(규칙 유무 무관).
  - block verdict 는 EMBER floor 를 동반.
"""

from __future__ import annotations

import pytest

from liot.core.contracts import CandidateAction, ContextSnapshot
from liot.core.risk import SAFETY_CRITICAL_CATEGORIES, RiskTier
from liot.layers.l3_judgment.guardrail import Guardrail


def _ctx(structured: dict[str, object] | None = None) -> ContextSnapshot:
    return ContextSnapshot(
        snapshot_id="s",
        trace_id="t",
        home_id="singleton",
        trigger={"kind": "test"},
        structured=structured or {"time_bucket": "day"},
        nl_summary="x",
        summary_template_sha="deadbeef",
        created_at="2026-06-01T12:00:00+00:00",
    )


def _cand(category: str, *, action: str = "suggest", capability: str | None = None) -> CandidateAction:
    return CandidateAction(
        capability=capability or category,
        action=action,
        params={},
        entity_id=f"{category}.x",
        category=category,
    )


def test_deny_by_default_unknown_category_is_ember() -> None:
    g = Guardrail()
    res = g.evaluate(_cand("totally_unknown_category"))
    assert res.floor_tier == RiskTier.EMBER
    assert "deny-by-default" in res.gate1_verdict


def test_known_low_category_baseline() -> None:
    g = Guardrail()
    res = g.evaluate(_cand("light", action="turn_on"))
    # light baseline = AMBIENT (낮 시간대, 야간 규칙 미발동).
    assert res.floor_tier == RiskTier.AMBIENT
    assert res.safety_critical is False


@pytest.mark.parametrize("category", sorted(SAFETY_CRITICAL_CATEGORIES))
def test_safety_categories_always_ember(category: str) -> None:
    g = Guardrail()
    res = g.evaluate(_cand(category, action="alert"))
    assert res.floor_tier == RiskTier.EMBER
    assert res.safety_critical is True


def test_set_floor_escalates_via_night_rule() -> None:
    g = Guardrail()
    # 야간 + light 카테고리 → night_quiet 규칙이 BREATH 하한으로 승격.
    night_ctx = _ctx({"time_bucket": "night"})
    res = g.evaluate(_cand("light", action="turn_on"), night_ctx)
    # baseline AMBIENT < 규칙 set_floor BREATH → max = BREATH.
    assert res.floor_tier == RiskTier.BREATH
    assert any("night-light" in rid for rid in res.matched_rule_ids)


def test_set_floor_max_clamp_does_not_downgrade() -> None:
    g = Guardrail()
    # noise 카테고리 baseline = BREATH. 야간 noise 규칙 set_floor = DUSK.
    night_ctx = _ctx({"time_bucket": "night"})
    res = g.evaluate(_cand("noise", action="play"), night_ctx)
    # max(BREATH, DUSK) = DUSK. 절대 baseline 아래로 내려가지 않음.
    assert res.floor_tier == RiskTier.DUSK
    assert res.floor_tier >= RiskTier.BREATH


def test_block_rule_forces_ember() -> None:
    g = Guardrail()
    # emergency.yaml 의 vacant-door-open 규칙: door + occupancy=vacant → block + ember.
    ctx = _ctx({"time_bucket": "day", "occupancy": "vacant"})
    res = g.evaluate(_cand("door", action="open"), ctx)
    assert res.blocked is True
    assert res.floor_tier == RiskTier.EMBER


# ── 뮤테이션 스윕이 찾은 미고정 가드(2026-09-16 야간) ────────────────────
def test_gate1_result_is_immutable() -> None:
    """Gate1Result 는 얼려 둔다 — 하류에서 안전 하한을 덮어쓸 수 없어야 한다.

    스윕에서 `@dataclass(frozen=True)` 를 False 로 바꿔도 아무 테스트가 울지 않았다.
    풀리면 floor_tier 를 조용히 낮추는 코드가 타입검사도 통과한 채 들어올 수 있다.
    """
    import dataclasses

    g = Guardrail()
    res = g.evaluate(
        CandidateAction(capability="valve", action="open", params={},
                        entity_id="switch.gas_valve", category="gas")
    )
    assert res.floor_tier is RiskTier.EMBER
    with pytest.raises(dataclasses.FrozenInstanceError):
        res.floor_tier = RiskTier.AMBIENT  # type: ignore[misc]


def test_gate1_facts_merge_trigger_but_structured_wins() -> None:
    """맥락 사실은 structured 를 우선하고, 빠진 것만 trigger 로 보강한다.

    스윕 두 건이 여기서 살아남았다: trigger 병합을 통째로 없애도(`or`→`and`),
    우선순위를 뒤집어도(`not in`→`in`) 아무도 몰랐다.
    """
    g = Guardrail()
    candidate = CandidateAction(capability="light", action="turn_on", params={},
                                entity_id="light.living_room", category="light")
    ctx = ContextSnapshot(
        snapshot_id="s", trace_id="t", home_id="singleton",
        trigger={"time_bucket": "day", "occupancy": "vacant"},   # 여기에만 occupancy
        structured={"time_bucket": "night"},                     # 겹치는 키는 이쪽이 이긴다
        nl_summary="", summary_template_sha="x",
        created_at="2026-06-02T22:00:00+00:00",
    )
    facts = g._facts(candidate, ctx)
    assert facts["time_bucket"] == "night", "structured 가 trigger 를 이겨야 한다"
    assert facts["occupancy"] == "vacant", "structured 에 없는 키는 trigger 에서 보강"
