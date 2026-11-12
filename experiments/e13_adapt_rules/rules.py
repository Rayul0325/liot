"""E13 experiment-only personalisation rule variants (never production semantics).

``LiotService`` is subclassed here; ``day_sim`` builds its service from the module-level name
``liot.day_sim.LiotService``, so a worker process swaps that name for the subclass for the duration
of one run (``installed``). Nothing under ``liot/`` is edited and with ``RuleSet()`` (all off) the
plain class is used untouched.

Rules (all relative to the shipped [그만해] rest, ``reject_window_days`` = N):
  digest_noexpire  (1) a proposal whose dispatch decision was DIGEST is not turned into the weak
                       negative preference signal when it expires (findings.md approach).
  second_strike    (2) a [그만해] starts the N-day rest only when another [그만해] of the same
                       candidate key happened within the N days before it ("two within N days").
  accept_lifts     (3) a key dismissed earlier is released from rest as soon as a LATER proposal of
                       the same key was accepted.

Safety: rules only ever REMOVE keys from the recently-rejected set, and the notifier ignores that
set for CRITICAL anyway. Rule (1) only skips a preference update. The first safety alert path is not
touched; every run still records ``safety_ok``.
"""

from __future__ import annotations

import contextlib
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from liot.core.contracts import CandidateAction
from liot.service import LiotService

NAMES = {"digest_noexpire": "r1", "second_strike": "r2", "accept_lifts": "r3"}


@dataclass(frozen=True)
class RuleSet:
    digest_noexpire: bool = False
    second_strike: bool = False
    accept_lifts: bool = False

    @property
    def any(self) -> bool:
        return self.digest_noexpire or self.second_strike or self.accept_lifts


def rejected_keys(rows: list[tuple[str, str, str]], now: datetime, days: int,
                  rules: RuleSet) -> frozenset[str]:
    """Pure core: rows are (key, state, created_at ISO) of dismissed/accepted proposals.

    With both gate rules off this returns exactly the shipped set (dismissed with created_at inside
    the window).
    """
    if days <= 0:
        return frozenset()
    cutoff = (now - timedelta(days=days)).replace(microsecond=0)
    span = timedelta(days=days)
    by_key: dict[str, list[tuple[datetime, str]]] = {}
    for key, state, created in rows:
        by_key.setdefault(key, []).append((datetime.fromisoformat(created), state))
    out: set[str] = set()
    for key, evs in by_key.items():
        evs.sort(key=lambda e: e[0])
        dismissed = [t for t, s in evs if s == "dismissed"]
        accepted = [t for t, s in evs if s == "accepted"]
        if rules.accept_lifts and accepted:
            last_acc = accepted[-1]
            dismissed = [t for t in dismissed if t > last_acc]
        if rules.second_strike:
            blocked = False
            for i, t in enumerate(dismissed):
                if t < cutoff:
                    continue
                if i >= 1 and t - dismissed[i - 1] <= span:
                    blocked = True
            if blocked:
                out.add(key)
        elif any(t >= cutoff for t in dismissed):
            out.add(key)
    return frozenset(out)


class RuleService(LiotService):
    """``LiotService`` with E13's research rules. ``RULES`` is set by ``service_class``."""

    RULES: RuleSet = RuleSet()

    def __init__(self, *a: Any, **kw: Any) -> None:
        super().__init__(*a, **kw)
        self.rule_stats: dict[str, int] = {"digest_expiry_skipped": 0, "lift_events": 0,
                                           "first_dismiss_forgiven": 0, "digest_dispatched": 0}
        self._digest_pids: set[str] = set()
        self._seen_lift: set[tuple[str, str]] = set()
        self._seen_forgive: set[tuple[str, str]] = set()
        rules = self.RULES
        if rules.second_strike or rules.accept_lifts:
            self.proposals.recently_rejected_keys = self._rejected_keys  # type: ignore[method-assign]

    async def _rejected_keys(self, *, now: str | None = None, days: int = 30) -> frozenset[str]:
        prop = self.proposals
        rows = await prop._repo.fetchall(  # noqa: SLF001 - experiment wrapper of the same query
            prop._db, "proposals", columns="action_json, state, created_at",  # noqa: SLF001
            extra_where="state IN (?, ?)", extra_params=("dismissed", "accepted"))
        triples: list[tuple[str, str, str]] = []
        for r in rows:
            d = {k: r[k] for k in r.keys()}  # noqa: SIM118 - sqlite3.Row
            a = CandidateAction.model_validate_json(d["action_json"])
            key = f"{a.capability}:{a.action}:{a.entity_id}:{a.category}"
            triples.append((key, d["state"], d["created_at"]))
        keys = rejected_keys(triples, self.clock.now_dt(), days, self.RULES)
        self._count_events(triples, days)
        return keys

    def _count_events(self, triples: list[tuple[str, str, str]], days: int) -> None:
        """Distinct events where a rule changed an outcome (deduped across the many calls)."""
        if days <= 0:
            return
        span = timedelta(days=days)
        by_key: dict[str, list[tuple[datetime, str]]] = {}
        for key, state, created in triples:
            by_key.setdefault(key, []).append((datetime.fromisoformat(created), state))
        for key, evs in by_key.items():
            evs.sort(key=lambda e: e[0])
            dis = [t for t, s in evs if s == "dismissed"]
            acc = [t for t, s in evs if s == "accepted"]
            if self.RULES.accept_lifts:
                for t in dis:
                    if any(t < a <= t + span for a in acc) and (key, t.isoformat()) not in self._seen_lift:
                        self._seen_lift.add((key, t.isoformat()))
                        self.rule_stats["lift_events"] += 1
                dis = [t for t in dis if not any(a > t for a in acc)]
            if self.RULES.second_strike:
                for i, t in enumerate(dis):
                    first = i == 0 or t - dis[i - 1] > span
                    if first and (key, t.isoformat()) not in self._seen_forgive:
                        self._seen_forgive.add((key, t.isoformat()))
                        self.rule_stats["first_dismiss_forgiven"] += 1

    async def dispatch(self, proposal: Any) -> Any:
        result = await super().dispatch(proposal)
        if result.decision == "DIGEST":
            self._digest_pids.add(proposal.proposal_id)
            self.rule_stats["digest_dispatched"] += 1
        return result

    async def _apply_expired_signals(self) -> None:
        if self.RULES.digest_noexpire:
            for pid in list(self._digest_pids):
                if pid in self._pref_signaled:
                    continue
                p = await self.proposals.get(pid)
                if p is not None and (p.state or "").lower() == "expired":
                    self._pref_signaled.add(pid)  # swallow: the expiry teaches nothing
                    self.rule_stats["digest_expiry_skipped"] += 1
        await super()._apply_expired_signals()


def service_class(rules: RuleSet) -> type[LiotService]:
    if not rules.any:
        return LiotService
    return type("RuleServiceX", (RuleService,), {"RULES": rules})


@contextlib.contextmanager
def installed(rules: RuleSet) -> Iterator[None]:
    """Swap ``liot.day_sim.LiotService`` for the rule subclass while a DaySim runs."""
    import liot.day_sim as ds

    orig = ds.LiotService
    ds.LiotService = service_class(rules)  # type: ignore[misc]
    try:
        yield
    finally:
        ds.LiotService = orig  # type: ignore[misc]
