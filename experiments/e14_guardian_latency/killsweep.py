"""Statement-granular kill sweep (C4.9 (c) companion; regression check for a U6-style fix).

Kill the daemon at its n-th DB statement while it handles ONE abnormal reading, restart it (new
``LiotApp`` + ``LiotService`` on the same file DB, ``start()``), drive the ladder for 35 s, and ask:
is there a resident push? If not, does the sensor repeating the same abnormal value after the
restart produce one, or is it swallowed by the open situation (and does 700 s of ladder time change
that)?

Reusable as a regression check:

    PYTHONPATH=$PWD .venv/bin/python -m experiments.e14_guardian_latency.killsweep

prints one line per (category, vulnerable, kill point) and a summary; exit code 1 while any kill
point leaves a committed situation with no resident alert after recovery (the gate-1 window found
by C4.9 (c)) or a kill point that alerts but never creates the ladder. From Python: ``await killsweep.sweep(("gas", "door"))`` returns the records.
"""

from __future__ import annotations

import argparse
import asyncio
import random
import sys
import tempfile
from pathlib import Path
from typing import Any

from experiments.common.service_kit import HOME, proposals, reading, situations
from experiments.e2_invariants import fuzz
from experiments.e14_guardian_latency.restart_fuzz import (
    RECOVERY_DRIVE_S,
    SENS,
    SimulatedKill,
    kill,
    new_incarnation,
    recovery_drive,
)
from liot.core.clock import Clock

CATEGORIES = ("gas", "door")


async def trial(cat: str, n: int | None, *, vulnerable: bool = False) -> dict[str, Any]:
    """One kill point. ``n=None`` = uninterrupted run (counts the statements)."""
    clock = Clock(fixed="2026-06-02T12:00:00+00:00")
    tr = fuzz.ConsentWatchTransport()
    tr.clock = clock
    backend, deadline = fuzz.make_backend("normal", random.Random(1))
    with tempfile.TemporaryDirectory(prefix="e14sweep") as tmp:
        path = str(Path(tmp) / "liot.db")
        inc = await new_incarnation(path, clock, backend, tr, deadline)
        if vulnerable:
            rid = await inc.svc.safety.add_resident(HOME, "R0")
            await inc.svc.safety.set_trait(rid, "age_75_plus", True, "resident")
        eid, abn = SENS[cat]
        if n is not None:
            inc.db.arm(n)
        calls0 = inc.db.calls
        killed = False
        try:
            await inc.svc.handle_reading(reading(eid, abn))
        except SimulatedKill:
            killed = True
        used = inc.db.calls - calls0
        inc.db.budget = None
        if killed:
            await kill(inc)
            inc = await new_incarnation(path, clock, backend, tr, deadline)
        pushes_before = len(tr.stamps)
        await recovery_drive(inc, clock, RECOVERY_DRIVE_S)
        sits = await situations(inc.db)
        props = await proposals(inc.db)
        pushes_after = len(tr.stamps)
        swallowed: bool | None = None
        if killed and sits and pushes_after == 0:
            again = await inc.svc.handle_reading(reading(eid, abn))
            await inc.svc.drain_phase2()
            swallowed = again is None and len(tr.stamps) == 0
            await recovery_drive(inc, clock, 700.0)
        final_pushes = len(tr.stamps)
        ladders = len(await inc.svc.safety.list_open_ladders())
        await inc.svc.drain_phase2()
        await kill(inc)
        return {
            "category": cat, "vulnerable": vulnerable, "kill_at": n, "killed": killed,
            "statements_used": used, "situations": len(sits), "proposals": len(props),
            "pushes_before_restart": pushes_before, "pushes_after_recovery": pushes_after,
            "ok": pushes_after > 0, "repeat_swallowed": swallowed,
            "pushes_after_700s": final_pushes, "open_ladders": ladders,
        }


async def sweep(categories: tuple[str, ...] = CATEGORIES,
                vulnerable_flags: tuple[bool, ...] = (False, True)) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for cat in categories:
        for vul in vulnerable_flags:
            base = await trial(cat, None, vulnerable=vul)
            out.append({**base, "kill_at": None})
            for n in range(1, base["statements_used"] + 2):
                out.append(await trial(cat, n, vulnerable=vul))
    return out


def window(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Per (category, vulnerable): the kill points whose situation was committed but never alerted."""
    out = []
    for cat in sorted({r["category"] for r in records}):
        for vul in (False, True):
            rs = [r for r in records if r["category"] == cat and r["vulnerable"] == vul]
            if not rs:
                continue
            total = max(r["statements_used"] for r in rs if r["kill_at"] is None)
            bad = [r["kill_at"] for r in rs if r["killed"] and r["situations"] > 0 and not r["ok"]]
            lost = [r["kill_at"] for r in rs if r["killed"] and r["situations"] == 0]
            # first alert went out but the ladder row was never created (kill between the
            # first push and ladder creation): re-alerts, guardian forward and 119 guidance are lost
            no_ladder = [r["kill_at"] for r in rs
                         if r["killed"] and r["ok"] and r["open_ladders"] == 0]
            out.append({"category": cat, "vulnerable": vul, "statements": total,
                        "silent_kill_points": bad, "nothing_durable_kill_points": lost,
                        "alerted_but_no_ladder_kill_points": no_ladder})
    return out


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
    p.add_argument("--category", nargs="*", default=list(CATEGORIES))
    args = p.parse_args()
    recs = asyncio.run(sweep(tuple(args.category)))
    for r in recs:
        print(r)
    wins = window(recs)
    for w in wins:
        print("WINDOW", w)
    return 1 if any(w["silent_kill_points"] or w["alerted_but_no_ladder_kill_points"]
                    for w in wins) else 0


if __name__ == "__main__":
    sys.exit(main())
