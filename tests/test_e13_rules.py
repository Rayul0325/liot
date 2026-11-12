"""E13 experiment-only rest rules (experiments/e13_adapt_rules/rules.py).

The rules live in the experiment package as a ``LiotService`` subclass; production semantics must be
unchanged. These tests pin: parity with the shipped ``recently_rejected_keys`` when the rules are off,
exactly what each rule changes, that ``installed`` restores the module name, and that a short DaySim
with all rules still sends every first safety alert immediately.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from typing import Any

from experiments.e13_adapt_rules.rules import RuleSet, installed, rejected_keys
from liot.day_sim import DaySim, SceneTaste, StochasticResident

NOW = datetime(2026, 6, 30, 12, 0, tzinfo=UTC)
K = "fan:set_speed:fan.living:comfort"


def _at(days_ago: float) -> str:
    return (NOW - timedelta(days=days_ago)).replace(microsecond=0).isoformat()


async def test_rules_off_equals_the_shipped_query(tmp_db: Any) -> None:
    from liot.core.clock import Clock
    from liot.core.contracts import CandidateAction, Proposal
    from liot.core.risk import RiskTier
    from liot.layers.l4_execution.proposal_service import ProposalService

    svc = ProposalService(tmp_db, clock=Clock(fixed=NOW.isoformat()))
    ages = {"p-old": 40.0, "p-edge": 29.9, "p-new": 1.0}
    for pid, age in ages.items():
        action = CandidateAction(capability="fan", action="set_speed", params={},
                                 entity_id=f"fan.{pid}", category="comfort")
        await svc.save(Proposal.model_validate({
            "proposal_id": pid, "judgment_id": "j", "home_id": "singleton",
            "risk_tier": RiskTier.BREATH, "action": action, "rationale": "r",
            "required_confirmation": "light", "created_at": _at(age)}))
        await svc.set_state(pid, "dismissed")
    rows = [(f"fan:set_speed:fan.{pid}:comfort", "dismissed", _at(age)) for pid, age in ages.items()]
    for days in (0, 7, 14, 30, 60):
        shipped = await svc.recently_rejected_keys(days=days)
        assert rejected_keys(rows, NOW, days, RuleSet()) == shipped, days


def test_second_strike_forgives_one_dismissal_and_blocks_the_second_within_the_window() -> None:
    one = [(K, "dismissed", _at(3))]
    assert rejected_keys(one, NOW, 30, RuleSet()) == {K}
    assert rejected_keys(one, NOW, 30, RuleSet(second_strike=True)) == frozenset()
    two = [(K, "dismissed", _at(10)), (K, "dismissed", _at(3))]
    assert rejected_keys(two, NOW, 30, RuleSet(second_strike=True)) == {K}
    # two far apart (> window) are two separate first strikes
    far = [(K, "dismissed", _at(50)), (K, "dismissed", _at(3))]
    assert rejected_keys(far, NOW, 30, RuleSet(second_strike=True)) == frozenset()
    # the block ends N days after the second dismissal
    ended = [(K, "dismissed", _at(45)), (K, "dismissed", _at(31))]
    assert rejected_keys(ended, NOW, 30, RuleSet(second_strike=True)) == frozenset()


def test_accept_lifts_only_dismissals_older_than_the_accept() -> None:
    rows = [(K, "dismissed", _at(5)), (K, "accepted", _at(2))]
    assert rejected_keys(rows, NOW, 30, RuleSet()) == {K}
    assert rejected_keys(rows, NOW, 30, RuleSet(accept_lifts=True)) == frozenset()
    later = [(K, "accepted", _at(5)), (K, "dismissed", _at(2))]
    assert rejected_keys(later, NOW, 30, RuleSet(accept_lifts=True)) == {K}


def test_a_rule_can_only_remove_keys() -> None:
    rows = [(K, "dismissed", _at(4)), (K, "dismissed", _at(2)), ("a:b:c:d", "dismissed", _at(9)),
            ("a:b:c:d", "accepted", _at(1))]
    base = rejected_keys(rows, NOW, 30, RuleSet())
    for rs in (RuleSet(second_strike=True), RuleSet(accept_lifts=True), RuleSet(True, True, True)):
        assert rejected_keys(rows, NOW, 30, rs) <= base


def test_installed_swaps_and_restores_the_service_name() -> None:
    import importlib

    from liot.service import LiotService

    ds: Any = importlib.import_module("liot.day_sim")

    assert ds.LiotService is LiotService
    with installed(RuleSet()):
        assert ds.LiotService is LiotService  # all off = the untouched class
    with installed(RuleSet(digest_noexpire=True)):
        assert ds.LiotService is not LiotService and issubclass(ds.LiotService, LiotService)
    assert ds.LiotService is LiotService


def _resident() -> StochasticResident:
    return StochasticResident(tastes={"ventilate": SceneTaste(0.8, 0.05),
                                      "purifier": SceneTaste(0.1, 0.6),
                                      "bedtime": SceneTaste(0.8, 0.0),
                                      "desk_light": SceneTaste(0.5, 0.1)}, tray_open=0.0)


def _run(rules: RuleSet) -> tuple[DaySim, dict[str, Any]]:
    with installed(rules):
        sim = DaySim(seed=1, days=8, condition="B", resident=_resident(), research=True)
        asyncio.run(sim.run(lambda stage, data: None))
    return sim, sim.summary()


def test_digest_noexpire_skips_exactly_the_digest_expiries_and_safety_stays_immediate() -> None:
    sim, summary = _run(RuleSet(digest_noexpire=True))
    stats = sim._svc.rule_stats  # type: ignore[union-attr]  # noqa: SLF001
    digests = [p for p in summary["proposals"] if p["decision"] == "DIGEST"]
    expired_digests = [p for p in digests if p["state"] == "expired"]
    assert stats["digest_dispatched"] == len(digests) > 0
    assert 0 < stats["digest_expiry_skipped"] <= len(expired_digests)
    firsts = [s for s in summary["safety"] if s["kind"] == "first_alert"]
    assert len(firsts) == 8 and all(s["decision"] == "IMMEDIATE" and s["sent"] for s in firsts)


def test_all_rules_keep_every_first_safety_alert_immediate() -> None:
    _, summary = _run(RuleSet(True, True, True))
    firsts = [s for s in summary["safety"] if s["kind"] == "first_alert"]
    assert len(firsts) == 8 and all(s["decision"] == "IMMEDIATE" and s["sent"] for s in firsts)


def test_rules_off_summary_is_identical_to_a_plain_run() -> None:
    _, summary = _run(RuleSet())
    plain = DaySim(seed=1, days=8, condition="B", resident=_resident(), research=True)
    asyncio.run(plain.run(lambda stage, data: None))
    assert summary["proposals"] == plain.summary()["proposals"]
    assert len(summary["proposals"]) > 0
