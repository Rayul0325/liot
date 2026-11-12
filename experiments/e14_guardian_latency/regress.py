"""C4.9 (a): re-run the C2.9 path-quota fuzz on the current code (U5 included).

Reuses ``experiments.e2_invariants.quota`` unchanged (same seeds ``quota/<path>/<ep>``, so every
episode of the six paths is the same episode C2.9 ran). Two things differ from C2.9's reading:

* ``quota.path_first_send_fail`` hard-codes its miss class as ``send_failure_delay`` (the C2.9
  report says so itself), so a class label cannot count as evidence here. The conflict is decided
  from the measured delay only.
* an outage variant (``first_send_fail_outage``): the transport stays down for D seconds, then
  recovers; U5 promises a retry every LADDER_RETRY (30 s) until the first send succeeds.

Pre-fixed definitions (before the run):
  first-send-failure conflict (strict)  = no resident push within ``outage + LADDER_RETRY`` seconds
                                          of the situation opening (ideal daemon, fake clock)
  C2.9 form of the same conflict        = first push delay >= 60 s, or none within the horizon
The known suppression (reject -> reopen, WARNING floor, plain home) is reported separately and
unchanged: its C2.9 value was 38 suppressed of 85 rejections.
"""

from __future__ import annotations

import random
from datetime import timedelta
from typing import Any

from experiments.e2_invariants import quota
from experiments.common.service_kit import reading
from liot.layers.l4_execution.escalation import LADDER_RETRY

RETRY_S = LADDER_RETRY.total_seconds()
OUTAGES = (0.0, 20.0, 45.0, 100.0)
HORIZON_S = 3000.0


async def path_first_send_fail_outage(rng: random.Random, outage_s: float) -> dict[str, Any]:
    cat = rng.choice(quota.ALL_CATS)
    vulnerable = rng.random() < 0.3
    eid = quota.SENS[cat][0]
    kit = await quota._new_kit()
    try:
        await quota._set_vulnerable(kit, vulnerable)
        kit.transport.fail = True
        res = await kit.svc.handle_reading(reading(eid, quota._abn(cat)))
        await kit.svc.drain_phase2()
        sits = await quota.situations(kit.db)
        sid, floor = sits[-1]["situation_id"], sits[-1]["severity_floor"]
        # ideal daemon: jump to each stored due time; the transport recovers at ``outage_s``
        now = 0.0
        recovered = outage_s <= 0.0
        if recovered:
            kit.transport.fail = False
        first: float | None = None
        for _ in range(5000):
            pushes = await quota._situation_pushes(kit, sid)
            if pushes:
                first = min(pushes)
                break
            due = await quota._next_due(kit)
            nxt_candidates = [c for c in (due, None if recovered else outage_s) if c is not None]
            if not nxt_candidates:
                break
            nxt = min(nxt_candidates)
            if nxt > HORIZON_S:
                break
            now = max(nxt, now + 1e-3)
            kit.clock.set_fixed(kit.t0 + timedelta(seconds=now))
            if not recovered and now >= outage_s:
                kit.transport.fail = False
                recovered = True
            await kit.svc.ladder_tick()
        return {
            "path": "first_send_fail_outage", "category": cat, "vulnerable": vulnerable,
            "floor": floor, "outage_s": outage_s, "hit": getattr(res, "sent", None) is False,
            "first_push_delay_s": first,
        }
    finally:
        await kit.close()


async def run_outage_episode(outage_s: float, ep: int) -> dict[str, Any]:
    rng = random.Random(f"e14/outage/{outage_s}/{ep}")
    rec = await path_first_send_fail_outage(rng, outage_s)
    rec["episode"] = ep
    return rec
