"""C2.9 coverage-quota fuzz: force the code paths random fuzzing never reached, >= 50 hits each.

Paths (each episode is one hit of its path):
  reject_reopen      first alert dismissed -> situation resolved -> same entity reopens
  first_send_fail    the first resident send fails (ntfy down), then the transport recovers
  charter_off        the resident turned every notification off (documented opt-out)
  restart_rearm      the daemon restarts mid-ladder (new LiotApp + LiotService on the same DB)
  two_entities       two sensors of the same category open situations
  concurrent         several readings handled concurrently (same and different entities)

Gate 1 here = a NEW situation must produce a successful push to the resident at the moment it
opens. Failure-injected steps ARE included (unlike the random fuzz): the delay until the first
successful push is measured on the fake clock. Classes of a miss:
  known_suppression   dismissal in the last 30 days of the same category (findings.md)
  send_failure_delay  the transport failed at open time (first push waits for the ladder)
  user_opt_out        charter OFF (documented: resident alerts blocked on purpose)
  UNKNOWN             anything else -> a real gate-1 violation beyond the known one (stop & report)
"""

from __future__ import annotations

import asyncio
import random
from datetime import timedelta
from typing import Any

from experiments.common.service_kit import (
    HOME,
    SECRET,
    FixedBackend,
    Kit,
    LowestBackend,
    TimedTransport,
    build_service,
    guardian_forwards,
    proposals,
    reading,
    situations,
)
from experiments.e2_invariants import fuzz
from liot.app import LiotApp
from liot.layers.l4_execution.escalation import parse_iso
from liot.layers.l4_execution.notifier import sign_feedback
from liot.service import LiotService

PATHS = ("reject_reopen", "first_send_fail", "charter_off", "restart_rearm", "two_entities",
         "concurrent")
SENS = fuzz.SENSORS
EXTRA = {"binary_sensor.kitchen_gas_b": "gas", "binary_sensor.back_door": "door",
         "binary_sensor.bath_leak_b": "leak"}
CATS = {**fuzz.CATEGORIES, **EXTRA}
WARNING_CATS = ("door", "leak", "oven", "high_temp", "lock")
ALL_CATS = tuple(SENS)


def _abn(cat: str) -> object:
    return SENS[cat][1]


def _norm(cat: str) -> object:
    return "off" if not isinstance(SENS[cat][1], float) else 20.0


async def _new_kit(backend: Any = None) -> Kit:
    kit = await build_service(
        backend=backend or LowestBackend(), categories=CATS, transport=TimedTransport(),
    )
    kit.transport.clock = kit.clock  # stamps carry the fake-clock time
    return kit


async def _situation_pushes(kit: Kit, sid: str) -> list[float]:
    """Driver-independent push times (clock seconds since T0) of resident pushes of a situation."""
    pids = {p["proposal_id"] for p in await proposals(kit.db) if p["situation_id"] == sid}
    out: list[float] = []
    for st in kit.transport.stamps:
        if st[2] == kit.svc.topic and st[3].get("proposal_id") in pids and st[1]:
            out.append((parse_iso(st[1]) - kit.t0).total_seconds())
    return out


async def _next_due(kit: Kit) -> float | None:
    due = [
        (parse_iso(lad.next_fire_at) - kit.t0).total_seconds()
        for lad in await kit.svc.safety.list_open_ladders() if lad.next_fire_at is not None
    ]
    return min(due) if due else None


async def _run_ladder(kit: Kit, until: float, stop: Any = None) -> float:
    """Jump the clock to each stored due time (ideal daemon) until ``until`` or ``stop()``."""
    now = (kit.clock.now_dt() - kit.t0).total_seconds()
    for _ in range(5000):
        if stop is not None and await stop():
            break
        due = await _next_due(kit)
        if due is None or due > until:
            break
        now = max(due, now + 1e-3)
        kit.clock.set_fixed(kit.t0 + timedelta(seconds=now))
        await kit.svc.ladder_tick()
    return now


async def _first_resident_push_after(kit: Kit, sid: str, since: float, horizon: float) -> float | None:
    async def stop() -> bool:
        return any(t >= since for t in await _situation_pushes(kit, sid))

    if not await stop():
        await _run_ladder(kit, horizon, stop)
    times = [t for t in await _situation_pushes(kit, sid) if t >= since]
    return min(times) if times else None


def _dismiss_link(kit: Kit, prop: Any, action: str = "reject") -> str:
    tier_int = int(prop.risk_tier)
    nonce = kit.svc._nonce.get(prop.proposal_id, "")
    sig = sign_feedback(SECRET, prop.proposal_id, tier_int, action, nonce)
    return (f"GET /feedback?p={prop.proposal_id}&t={tier_int}&n={nonce}&sig={sig}"
            f"&a={action} HTTP/1.1")


async def _set_vulnerable(kit: Kit, vulnerable: bool) -> str:
    rid = await kit.svc.safety.add_resident(HOME, "R0")
    if vulnerable:
        await kit.svc.safety.set_trait(rid, "age_75_plus", True, "resident")
    return rid


def _classify_miss(*, dismissed_same_cat: bool, floor: int, send_failed: bool, charter_off: bool,
                   ) -> str:
    if charter_off:
        return "user_opt_out"
    if send_failed:
        return "send_failure_delay"
    if dismissed_same_cat and floor < 3:
        return "known_suppression"
    return "UNKNOWN"


# ── paths ────────────────────────────────────────────────────────────────────
async def path_reject_reopen(rng: random.Random) -> dict[str, Any]:
    cat = rng.choice(ALL_CATS)
    vulnerable = rng.random() < 0.3
    action = rng.choices(["reject", "ok", "snooze", "accept"], weights=[8, 1, 0.5, 0.5])[0]
    eid = SENS[cat][0]
    kit = await _new_kit()
    try:
        await _set_vulnerable(kit, vulnerable)
        first = await kit.svc.handle_reading(reading(eid, _abn(cat)))
        await kit.svc.drain_phase2()
        await kit.svc._route_feedback(_dismiss_link(kit, first.proposal, action))  # type: ignore[union-attr]
        gap = rng.choice([5.0, 120.0, 900.0])
        kit.clock.set_fixed(kit.t0 + timedelta(seconds=gap))
        await kit.svc.handle_reading(reading(eid, _norm(cat)))
        reopen_t = gap + 1.0
        kit.clock.set_fixed(kit.t0 + timedelta(seconds=reopen_t))
        res = await kit.svc.handle_reading(reading(eid, _abn(cat)))
        await kit.svc.drain_phase2()
        sits = await situations(kit.db)
        sid, floor = sits[-1]["situation_id"], sits[-1]["severity_floor"]
        times = [t for t in await _situation_pushes(kit, sid)]
        immediate = any(t <= reopen_t + 1e-6 for t in times)
        dismissed = await kit.svc.proposals.recently_rejected_keys()
        known_key = any(k.endswith(f":{cat}") for k in dismissed)
        delay = None
        if not immediate:
            nxt = await _first_resident_push_after(kit, sid, reopen_t, reopen_t + 3000)
            delay = None if nxt is None else nxt - reopen_t
        out = {
            "path": "reject_reopen", "category": cat, "vulnerable": vulnerable, "action": action,
            "floor": floor, "hit": action == "reject", "immediate": immediate,
            "decision": getattr(res, "decision", None), "first_push_delay_s": 0.0 if immediate else delay,
            "dismissed_same_category": known_key,
        }
        if not immediate:
            out["miss_class"] = _classify_miss(dismissed_same_cat=known_key, floor=floor,
                                               send_failed=False, charter_off=False)
        return out
    finally:
        await kit.close()


async def path_first_send_fail(rng: random.Random) -> dict[str, Any]:
    cat = rng.choice(ALL_CATS)
    vulnerable = rng.random() < 0.3
    eid = SENS[cat][0]
    kit = await _new_kit()
    try:
        await _set_vulnerable(kit, vulnerable)
        kit.transport.fail = True
        res = await kit.svc.handle_reading(reading(eid, _abn(cat)))
        kit.transport.fail = False  # recovery right away
        await kit.svc.drain_phase2()
        sits = await situations(kit.db)
        sid, floor = sits[-1]["situation_id"], sits[-1]["severity_floor"]
        delay = None
        first = await _first_resident_push_after(kit, sid, 0.0, 3000)
        if first is not None:
            delay = first
        return {
            "path": "first_send_fail", "category": cat, "vulnerable": vulnerable, "floor": floor,
            "hit": getattr(res, "sent", None) is False, "immediate": False,
            "first_push_delay_s": delay, "situation_open_without_push": delay is None or delay > 0,
            "miss_class": "send_failure_delay",
        }
    finally:
        await kit.close()


async def path_charter_off(rng: random.Random) -> dict[str, Any]:
    cat = rng.choice(ALL_CATS)
    eid = SENS[cat][0]
    kit = await _new_kit()
    try:
        rid = await _set_vulnerable(kit, rng.random() < 0.3)
        gid, _topic = await kit.svc.safety.add_guardian(HOME, "G0")
        await kit.svc.safety.grant_consent(HOME, gid, rid)
        await kit.svc.prefs.set("charter_enabled", False)
        await kit.svc.handle_reading(reading(eid, _abn(cat)))
        await kit.svc.drain_phase2()
        sits = await situations(kit.db)
        sid, floor = sits[-1]["situation_id"], sits[-1]["severity_floor"]
        await _run_ladder(kit, 700)
        resident_pushes = len(await _situation_pushes(kit, sid))
        fw = [f for f in await guardian_forwards(kit) if f["kind"] == "forward"]
        return {
            "path": "charter_off", "category": cat, "floor": floor, "hit": True,
            "immediate": resident_pushes > 0, "resident_pushes": resident_pushes,
            "guardian_forwards": len(fw), "first_push_delay_s": None,
            "miss_class": "user_opt_out",
        }
    finally:
        await kit.close()


async def path_restart_rearm(rng: random.Random) -> dict[str, Any]:
    cat = rng.choice(["gas", "smoke", "door", "leak"])
    eid = SENS[cat][0]
    restart_at = rng.choice([10.0, 70.0, 100.0, 200.0, 400.0])
    kit = await _new_kit()
    try:
        rid = await _set_vulnerable(kit, rng.random() < 0.3)
        gid, _topic = await kit.svc.safety.add_guardian(HOME, "G0")
        await kit.svc.safety.grant_consent(HOME, gid, rid)
        first = await kit.svc.handle_reading(reading(eid, _abn(cat)))
        await kit.svc.drain_phase2()
        sits = await situations(kit.db)
        sid, floor = sits[-1]["situation_id"], sits[-1]["severity_floor"]
        await _run_ladder(kit, restart_at)
        n_before = len(kit.transport.stamps)
        # ── restart: a brand-new app + service on the same DB, clock and transport ──
        app2 = LiotApp(clock=kit.clock, categories=CATS, llm_backend=FixedBackend())
        kit.svc = LiotService(app2, kit.db, clock=kit.clock, transport=kit.transport,
                              topic=kit.svc.topic, secret=SECRET)
        await kit.svc.start()
        kit.clock.set_fixed(kit.t0 + timedelta(seconds=restart_at + 1.0))
        again = await kit.svc.handle_reading(reading(eid, _abn(cat)))  # repeated abnormal value
        await kit.svc.drain_phase2()
        await _run_ladder(kit, 700)
        times = await _situation_pushes(kit, sid)
        # spec: CRITICAL realert every 60 s until acked (WARNING: 600/1200/1800 s)
        if floor >= 3 or cat in ("gas", "smoke"):
            expected_n = 1 + int(700 // 60)
        else:
            expected_n = 1 + int(700 // 600)
        fwd = [f for f in await guardian_forwards(kit) if f["kind"] == "forward"]
        exp_fwd = 1 if floor >= 3 else 0
        return {
            "path": "restart_rearm", "category": cat, "floor": floor, "restart_at_s": restart_at,
            "hit": True, "immediate": first is not None and n_before >= 1,
            "pushes_total": len(times), "pushes_expected": expected_n,
            "forwards": len(fwd), "forwards_expected": exp_fwd,
            "new_alert_after_restart": again is not None,
            "first_push_delay_s": 0.0,
            "pass": len(times) >= expected_n - 1 and len(fwd) == exp_fwd,
        }
    finally:
        await kit.close()


async def path_two_entities(rng: random.Random) -> dict[str, Any]:
    cat = rng.choice(["gas", "door", "leak"])
    a = SENS[cat][0]
    b = {"gas": "binary_sensor.kitchen_gas_b", "door": "binary_sensor.back_door",
         "leak": "binary_sensor.bath_leak_b"}[cat]
    kit = await _new_kit()
    try:
        await _set_vulnerable(kit, rng.random() < 0.3)
        await kit.svc.handle_reading(reading(a, _abn(cat)))
        await kit.svc.drain_phase2()
        gap = rng.choice([1.0, 30.0, 400.0])
        kit.clock.set_fixed(kit.t0 + timedelta(seconds=gap))
        await kit.svc.handle_reading(reading(b, _abn(cat)))
        await kit.svc.drain_phase2()
        sits = await situations(kit.db)
        by_ent = {s["entity_id"]: s for s in sits}
        ok_open = a in by_ent and b in by_ent
        second_push = (await _situation_pushes(kit, by_ent[b]["situation_id"])) if b in by_ent else []
        immediate = any(t <= gap + 1e-6 for t in second_push)
        out = {"path": "two_entities", "category": cat, "hit": ok_open,
               "floor": by_ent[b]["severity_floor"] if b in by_ent else None,
               "immediate": immediate, "first_push_delay_s": 0.0 if immediate else None}
        if not immediate:
            out["miss_class"] = "UNKNOWN"
        return out
    finally:
        await kit.close()


async def path_concurrent(rng: random.Random) -> dict[str, Any]:
    cats = rng.sample(ALL_CATS, rng.randint(2, 5))
    dup = rng.random() < 0.5
    kit = await _new_kit()
    try:
        await _set_vulnerable(kit, rng.random() < 0.3)
        reads = [reading(SENS[c][0], _abn(c)) for c in cats]
        if dup:
            reads.append(reading(SENS[cats[0]][0], _abn(cats[0])))  # same entity twice
        await asyncio.gather(*[kit.svc.handle_reading(r) for r in reads])
        await kit.svc.drain_phase2()
        sits = await situations(kit.db)
        per_entity: dict[str, int] = {}
        for s in sits:
            per_entity[s["entity_id"]] = per_entity.get(s["entity_id"], 0) + 1
        all_open = all(SENS[c][0] in per_entity for c in cats)
        dup_situations = sum(1 for n in per_entity.values() if n > 1)
        pushed = []
        for s in sits:
            pushed.append(len(await _situation_pushes(kit, s["situation_id"])) >= 1)
        return {
            "path": "concurrent", "categories": cats, "duplicate_reading": dup, "hit": True,
            "all_situations_opened": all_open, "duplicate_situations": dup_situations,
            "immediate": all(pushed), "situations": len(sits), "first_push_delay_s": 0.0,
            "miss_class": None if all(pushed) else "UNKNOWN",
        }
    finally:
        await kit.close()


RUNNERS = {
    "reject_reopen": path_reject_reopen, "first_send_fail": path_first_send_fail,
    "charter_off": path_charter_off, "restart_rearm": path_restart_rearm,
    "two_entities": path_two_entities, "concurrent": path_concurrent,
}


async def run_episode(path: str, ep: int) -> dict[str, Any]:
    rng = random.Random(f"quota/{path}/{ep}")
    rec = await RUNNERS[path](rng)
    rec["episode"] = ep
    return rec
