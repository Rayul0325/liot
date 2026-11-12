"""C4.9 (c): restart and concurrency fuzz on the real LIOT service; check hard gates 1 and 4.

Each episode runs on a FILE database (a scratch directory per episode) so a "daemon restart" is a
real one: the old service object is abandoned, its connection is closed (uncommitted work rolls
back, like a killed process), and a brand-new ``LiotApp`` + ``LiotService`` opens the same file and
``start()``s (ladder re-arm). Two restart kinds:

  boundary  restart between operations; in-flight phase-2 judgments are cancelled
  midop     a statement-granular kill: after the n-th later DB statement every DB call and every
            transport send of the old incarnation raises ``SimulatedKill`` (a ``BaseException``,
            so the service's own cleanup cannot write either, as in ``kill -9``); the operation
            that was running is cut at that point, then the service restarts.

Concurrency: operations also include ``asyncio.gather`` bursts (several readings of different and
of the same entity, abnormal and normal mixed, a ladder tick or a feedback tap in the same
gather), plus phase-2 judgments left in flight across steps.

Gate checks (pre-registered; only ``UNKNOWN`` is a violation):
  gate 1  every NEW situation must have a resident push, immediately when nothing was faulty, and
          within LADDER_RETRY (30 s) of recovery after a send failure or a kill (U5).
          Misses are classed BEFORE counting:
            known_suppression      same-category dismissal in the last 30 days, floor < CRITICAL
            user_opt_out           charter OFF, or the category/entity muted by the resident
            u5_retry_recovered     send failure or kill; the first push came <= 30 s after recovery
            UNKNOWN                anything else (a real violation: stop and report)
          Reading lost before its situation row committed (kill) is counted separately as
          ``reading_lost_in_kill`` (nothing durable existed; not a gate-1 class, see RESULTS.md).
  gate 4  ``invariants.check_guardian_gate`` on every new forward row (consent + CRITICAL floor), a
          send-time consent watch on guardian topics, no 'resolved' to a never-forwarded guardian.
          Duplicate forwards/guidance of one situation are counted as ``duplicate_family_page``
          (an anomaly, not a gate-4 violation: gate 4 is about WHO may be paged).
  also    gate 2 (final >= floor) and gate 3 (no trait word in sends / model input) in a final sweep.
"""

from __future__ import annotations

import asyncio
import logging
import random
import tempfile
from datetime import timedelta
from pathlib import Path
from typing import Any

from experiments.common import invariants
from experiments.common.service_kit import (
    HOME,
    SECRET,
    TOPIC,
    guardian_forwards,
    judgments,
    proposals,
    reading,
    situations,
)
from experiments.e2_invariants import fuzz, quota
from liot.app import LiotApp
from liot.core.clock import Clock
from liot.db.database import init_db
from liot.layers.l4_execution.escalation import LADDER_RETRY, parse_iso
from liot.service import LiotService

logging.getLogger("liot").setLevel(logging.CRITICAL)

RETRY_S = LADDER_RETRY.total_seconds()
RECOVERY_DRIVE_S = RETRY_S + 5.0
BEHAVIORS = ("normal", "highest", "lowest", "raising")
CATS = quota.CATS
SENS = fuzz.SENSORS
ALL_ENT = [(SENS[c][0], c) for c in SENS] + [
    ("binary_sensor.kitchen_gas_b", "gas"), ("binary_sensor.back_door", "door"),
    ("binary_sensor.bath_leak_b", "leak"),
]
MODES = ("boundary", "midop")


class SimulatedKill(BaseException):
    """The process died here (kill -9). BaseException: service cleanup must not catch it."""


class CrashDB:
    """Proxy for the aiosqlite connection that can be killed at a statement boundary."""

    def __init__(self, db: Any) -> None:
        self._db = db
        self.dead = False
        self.budget: int | None = None
        self.calls = 0

    def arm(self, n: int) -> None:
        self.budget = n

    def _tick(self) -> None:
        if self.dead:
            raise SimulatedKill()
        self.calls += 1
        if self.budget is not None:
            self.budget -= 1
            if self.budget <= 0:
                self.dead = True
                raise SimulatedKill()

    def execute(self, *a: Any, **k: Any) -> Any:
        self._tick()
        return self._db.execute(*a, **k)

    def executemany(self, *a: Any, **k: Any) -> Any:
        self._tick()
        return self._db.executemany(*a, **k)

    async def commit(self) -> None:
        self._tick()
        await self._db.commit()

    def __getattr__(self, name: str) -> Any:
        return getattr(self._db, name)


class GatedTransport:
    """Per-incarnation view of the shared transport; a dead incarnation sends nothing."""

    def __init__(self, inner: Any, db: CrashDB) -> None:
        self.inner = inner
        self.db = db

    def send(self, topic: str, sanitized: dict[str, Any]) -> None:
        if self.db.dead:
            raise SimulatedKill()
        self.inner.send(topic, sanitized)


class Inc:
    """One incarnation of the daemon (service + app + its own DB connection)."""

    def __init__(self, svc: LiotService, db: CrashDB, backend: Any) -> None:
        self.svc, self.db, self.backend = svc, db, backend


async def new_incarnation(path: str, clock: Clock, backend: Any, inner: Any,
                          deadline: float | None) -> Inc:
    raw = await init_db(path)
    db = CrashDB(raw)
    app = LiotApp(clock=clock, categories=CATS, llm_backend=backend)
    gate = GatedTransport(inner, db)
    svc = LiotService(app, db, clock=clock, transport=gate, topic=TOPIC, secret=SECRET,  # type: ignore[arg-type]
                      phase2_deadline_s=deadline)
    await svc.start()
    return Inc(svc, db, backend)


async def kill(inc: Inc) -> None:
    """Abandon the incarnation: dead flags, cancel phase-2 tasks, roll back uncommitted work."""
    inc.db.dead = True
    tasks = list(inc.svc._phase2_tasks)  # noqa: SLF001
    for t in tasks:
        t.cancel()
    for t in tasks:
        try:
            await t
        except BaseException:  # noqa: BLE001, S110 - a killed process leaves no result
            pass
    release = getattr(inc.backend, "release_all", None)
    if release is not None:
        release()
    try:
        await inc.db._db.close()  # noqa: SLF001 - rolls back whatever was not committed
    except Exception:  # noqa: BLE001, S110
        pass


def _clock_s(clock: Clock, t0: Any) -> float:
    return (clock.now_dt() - t0).total_seconds()


async def _pushes_by_situation(inc: Inc, transport: Any) -> dict[str, list[float]]:
    """Resident push times (seconds since T0) per situation, from the shared transport stamps."""
    try:
        props = await proposals(inc.db)
    except SimulatedKill:  # pragma: no cover - only on a dead incarnation
        return {}
    sit_of = {p["proposal_id"]: p["situation_id"] for p in props}
    out: dict[str, list[float]] = {}
    for st in transport.stamps:
        if st[2] != TOPIC:
            continue
        sid = sit_of.get(st[3].get("proposal_id"))
        if sid is not None and st[1]:
            out.setdefault(sid, []).append((parse_iso(st[1]) - T0_DT).total_seconds())
    return out


T0_DT = Clock(fixed="2026-06-02T12:00:00+00:00").now_dt()


async def _next_due(inc: Inc) -> float | None:
    due = [
        (parse_iso(lad.next_fire_at) - T0_DT).total_seconds()
        for lad in await inc.svc.safety.list_open_ladders() if lad.next_fire_at is not None
    ]
    return min(due) if due else None


async def recovery_drive(inc: Inc, clock: Clock, seconds: float) -> None:
    """Ideal daemon: jump the clock to each stored due time within ``seconds`` from now."""
    start = _clock_s(clock, T0_DT)
    now = start
    for _ in range(400):
        due = await _next_due(inc)
        if due is None or due > start + seconds:
            break
        now = max(due, now + 1e-3)
        clock.set_fixed(T0_DT + timedelta(seconds=now))
        await inc.svc.ladder_tick()
    clock.set_fixed(T0_DT + timedelta(seconds=start + seconds))


def _link(svc: LiotService, prop: Any, action: str) -> str:
    return fuzz._link(svc, prop, action)  # noqa: SLF001


async def run_episode(mode: str, ep: int) -> dict[str, Any]:
    rng = random.Random(f"e14/restart/{mode}/{ep}")
    behavior = rng.choice(BEHAVIORS)
    backend, deadline = fuzz.make_backend(behavior, rng)
    transport = fuzz.ConsentWatchTransport()
    clock = Clock(fixed="2026-06-02T12:00:00+00:00")
    transport.clock = clock  # stamps carry the fake-clock time
    violations: list[dict[str, Any]] = []
    anomalies: list[dict[str, Any]] = []
    classes: dict[str, int] = {}
    delays: list[float] = []
    counts = {"ops": 0, "restarts": 0, "kills_midop": 0, "kills_boundary": 0, "bursts": 0,
              "new_situations": 0, "evaluated_situations": 0, "immediate_ok": 0,
              "reading_lost_in_kill": 0, "duplicate_family_page": 0, "phase2_cut": 0,
              "send_failure_ops": 0, "redeliveries": 0}

    def bump(cls: str) -> None:
        classes[cls] = classes.get(cls, 0) + 1

    with tempfile.TemporaryDirectory(prefix="e14fuzz") as tmp:
        path = str(Path(tmp) / "liot.db")
        inc = await new_incarnation(path, clock, backend, transport, deadline)
        try:
            svc = inc.svc
            residents: list[str] = []
            for k in range(rng.randint(1, 2)):
                rid = await svc.safety.add_resident(HOME, f"R{k}")
                residents.append(rid)
                if rng.random() < 0.3:
                    trait = rng.choice(fuzz.TRAITS)
                    by = "guardian" if trait == "cognitive_decline" else "resident"
                    await svc.safety.set_trait(rid, trait, True, by)
            guardians: list[str] = []
            consent = transport.consent
            for k in range(rng.randint(0, 3)):
                gid, topic = await svc.safety.add_guardian(HOME, f"G{k}")
                guardians.append(gid)
                transport.topic_guardian[topic] = gid
                consent[gid] = False
                if rng.random() < 0.75:
                    await svc.safety.grant_consent(HOME, gid, residents[0])
                    consent[gid] = True
            last_abn: dict[str, object] = {}
            seen_sits: set[str] = set()
            seen_fwd = 0
            charter_off = False
            dismissed_cats: set[str] = set()

            async def check_forwards() -> None:
                nonlocal seen_fwd
                fwds = await guardian_forwards_inc()
                new = fwds[seen_fwd:]
                seen_fwd = len(fwds)
                if not new:
                    return
                sits = await situations(inc.db)
                floors = {s["situation_id"]: s["severity_floor"] for s in sits}
                gate_rows = [r for r in new if r["kind"] in ("forward", "guidance_119")]
                violations.extend(invariants.check_guardian_gate(gate_rows, consent, floors))
                prior = {(r["situation_id"], r["guardian_id"], r["kind"])
                         for r in fwds[: len(fwds) - len(new)]
                         if r["kind"] in ("forward", "guidance_119")}
                for r in gate_rows:
                    key = (r["situation_id"], r["guardian_id"], r["kind"])
                    if key in prior:
                        counts["duplicate_family_page"] += 1
                        anomalies.append({"where": "duplicate_family_page", "kind": r["kind"]})
                    prior.add(key)
                for r in new:
                    if r["kind"] == "resolved":
                        was = {(x["situation_id"], x["guardian_id"]) for x in fwds
                               if x["kind"] == "forward"}
                        if (r["situation_id"], r["guardian_id"]) not in was:
                            violations.append({"invariant": "guardian_gate",
                                               "detail": "resolved notice without prior forward"})
                        if not consent.get(str(r["guardian_id"]), False):
                            violations.append({"invariant": "guardian_gate",
                                               "detail": "resolved notice after consent revoked"})

            async def guardian_forwards_inc() -> list[dict[str, Any]]:
                fwds = await inc.svc.safety.list_forwards(HOME)
                return [{"forward_id": f.forward_id, "home_id": f.home_id,
                         "situation_id": f.situation_id, "guardian_id": f.guardian_id,
                         "kind": f.kind, "at": f.at} for f in fwds]

            async def gate_state(cat: str, entity: str, floor: int) -> str | None:
                """Pre-registered reasons a first alert may legitimately be silent."""
                ctx = await inc.svc._build_context(clock.now_dt())  # noqa: SLF001
                if not ctx.charter_enabled:
                    return "user_opt_out"
                if cat in ctx.muted_categories or entity in ctx.muted_entities:
                    return "user_opt_out"
                rej = await inc.svc.proposals.recently_rejected_keys()
                if floor < 3 and any(k.endswith(f":{cat}") for k in rej):
                    return "known_suppression"
                return None

            async def evaluate_new(fault: bool, op: str, recovery_t: float, killed: bool) -> None:
                """Gate 1 for situations that appeared during the last operation."""
                nonlocal seen_sits
                sits = await situations(inc.db)
                new = [s for s in sits if s["situation_id"] not in seen_sits]
                seen_sits |= {s["situation_id"] for s in sits}
                if not new:
                    return
                pushes = await _pushes_by_situation(inc, transport)
                props = await proposals(inc.db)
                has_prop = {p["situation_id"] for p in props}
                for s in new:
                    counts["new_situations"] += 1
                    counts["evaluated_situations"] += 1
                    sid, floor = s["situation_id"], s["severity_floor"]
                    times = pushes.get(sid, [])
                    opened = (parse_iso(s["opened_at"]) - T0_DT).total_seconds()
                    if times and killed and s["closed_at"] is None and (
                            await inc.svc.safety.get_ladder(sid)) is None:
                        # alerted, but the kill landed before the ladder row: no re-alert, no
                        # guardian forward, no 119 guidance (a ladder-conformance gap, reported
                        # on its own; the first alert itself was not silent)
                        bump("ladder_lost_after_kill")
                    if times:
                        first = min(times)
                        delay = first - opened
                        if not fault:
                            counts["immediate_ok"] += 1
                            continue
                        late_after_recovery = first - recovery_t
                        if late_after_recovery <= RETRY_S + 1e-6:
                            bump("u5_retry_recovered")
                            delays.append(max(late_after_recovery, 0.0))
                        else:
                            violations.append({
                                "invariant": "safety_never_silent", "cls": "UNKNOWN",
                                "detail": f"first push {late_after_recovery:.1f}s after recovery",
                                "op": op, "category": s["category"], "delay_s": delay})
                        continue
                    # no resident push at all
                    why = await gate_state(s["category"], s["entity_id"], floor)
                    if why is not None:
                        bump(why)
                        continue
                    ladder = await inc.svc.safety.get_ladder(sid)
                    detail = ("situation open, no proposal persisted" if sid not in has_prop
                              else "situation open, proposal persisted but never sent")
                    violations.append({
                        "invariant": "safety_never_silent", "cls": "UNKNOWN", "detail": detail,
                        "op": op, "category": s["category"], "floor": floor,
                        "closed": s["closed_at"] is not None, "has_ladder": ladder is not None,
                        "op_killed": killed})

            async def restart(kind: str, redeliver: bool) -> None:
                nonlocal inc
                await kill(inc)
                counts["restarts"] += 1
                counts["kills_" + kind] += 1
                inc = await new_incarnation(path, clock, backend, transport, deadline)
                if redeliver:
                    counts["redeliveries"] += 1
                    for eid, val in list(last_abn.items()):
                        try:
                            await inc.svc.handle_reading(reading(eid, val))
                        except Exception as exc:  # noqa: BLE001
                            anomalies.append({"where": "redelivery", "exc": type(exc).__name__})

            async def one_op(kind: str) -> list[Any]:
                """The body of an operation (may be cut by a kill)."""
                s = inc.svc
                if kind == "sensor":
                    eid, cat = rng.choice(ALL_ENT)
                    r = rng.random()
                    if r < 0.65:
                        val: object = SENS[cat][1]
                        last_abn[eid] = val
                    elif r < 0.9:
                        val = "off" if not isinstance(SENS[cat][1], float) else 20.0
                        last_abn.pop(eid, None)
                    else:
                        val = rng.choice(["unavailable", "unknown"])
                    return [await s.handle_reading(reading(eid, val))]
                if kind == "burst":
                    counts["bursts"] += 1
                    coros: list[Any] = []
                    for _ in range(rng.randint(2, 6)):
                        eid, cat = rng.choice(ALL_ENT)
                        val = SENS[cat][1] if rng.random() < 0.7 else (
                            "off" if not isinstance(SENS[cat][1], float) else 20.0)
                        if val == SENS[cat][1]:
                            last_abn[eid] = val
                        else:
                            last_abn.pop(eid, None)
                        coros.append(s.handle_reading(reading(eid, val)))
                        if rng.random() < 0.3:  # the same entity again in the same gather
                            coros.append(s.handle_reading(reading(eid, val)))
                    if rng.random() < 0.5:
                        coros.append(s.ladder_tick())
                    if rng.random() < 0.3:
                        coros.append(tap())
                    return list(await asyncio.gather(*coros, return_exceptions=True))
                if kind == "ladder":
                    dt = rng.choice([1, 30, 61, 95, 200, 310, 700])
                    clock.set_fixed(clock.now_dt() + timedelta(seconds=dt))
                    return [await s.ladder_tick()]
                if kind == "tap":
                    return [await tap()]
                raise ValueError(kind)

            async def tap() -> None:
                nonlocal charter_off
                cur = await inc.db.execute("SELECT p.proposal_id FROM proposals AS p")
                ids = [x[0] for x in await cur.fetchall()]
                await cur.close()
                if not ids:
                    return
                prop = await inc.svc.proposals.get(rng.choice(ids))
                if prop is None:
                    return
                action = rng.choice(["ok", "ok", "reject", "snooze", "accept"])
                if action == "reject":
                    dismissed_cats.add(str(prop.action.category))
                await inc.svc._route_feedback(_link(inc.svc, prop, action))  # noqa: SLF001

            steps = rng.randint(14, 30)
            for _ in range(steps):
                counts["ops"] += 1
                r = rng.random()
                try:
                    await check_forwards()
                except SimulatedKill:  # pragma: no cover
                    pass
                if r < 0.07:  # household change
                    if guardians:
                        gid = rng.choice(guardians)
                        if rng.random() < 0.5:
                            await inc.svc.safety.grant_consent(HOME, gid, residents[0])
                            consent[gid] = True
                        else:
                            await inc.svc.safety.revoke_consent(HOME, gid, residents[0])
                            consent[gid] = False
                    continue
                if r < 0.09:
                    charter_off = not charter_off
                    await inc.svc.prefs.set("charter_enabled", not charter_off)
                    continue
                if r < 0.17 and mode in MODES:  # restart at an operation boundary
                    if rng.random() < 0.5:
                        await asyncio.sleep(0)  # let phase 2 start before the kill
                    pending = len(inc.svc._phase2_tasks)  # noqa: SLF001
                    counts["phase2_cut"] += pending
                    await restart("boundary", rng.random() < 0.5)
                    continue
                kind = rng.choices(["sensor", "burst", "ladder", "tap"], [0.5, 0.2, 0.18, 0.12])[0]
                fault = False
                armed = mode == "midop" and rng.random() < 0.3
                if armed:
                    inc.db.arm(rng.randint(1, 70))
                send_fail = rng.random() < 0.12
                if send_fail:
                    transport.fail = True
                    counts["send_failure_ops"] += 1
                    fault = True
                before_stamps = len(transport.stamps)
                killed = False
                results: list[Any] = []
                try:
                    results = await one_op(kind)
                except SimulatedKill:
                    killed = True
                except Exception as exc:  # noqa: BLE001 - an exception on the safety path is recorded
                    anomalies.append({"where": kind, "exc": type(exc).__name__})
                killed = killed or any(isinstance(x, SimulatedKill) for x in results)
                inc.db.budget = None
                transport.fail = False
                if killed:
                    counts["phase2_cut"] += len(inc.svc._phase2_tasks)  # noqa: SLF001
                    fault = True
                    await restart("midop", rng.random() < 0.4)
                recovery_t = _clock_s(clock, T0_DT)
                if fault:
                    await recovery_drive(inc, clock, RECOVERY_DRIVE_S)
                elif rng.random() < 0.5:
                    await inc.svc.drain_phase2()
                # a phase-2 follow-up is not part of the first-alert check; only new situations are
                await evaluate_new(fault, kind, recovery_t, killed)
                if len(transport.stamps) < before_stamps:  # pragma: no cover
                    anomalies.append({"where": "stamps_shrank", "exc": "-"})
            await inc.svc.drain_phase2()
            await check_forwards()
            # readings that died before any situation row existed (nothing durable) — descriptive
            sits = await situations(inc.db)
            open_entities = {s["entity_id"] for s in sits if s["closed_at"] is None}
            counts["reading_lost_in_kill"] = sum(
                1 for eid in last_abn if eid not in open_entities
                and not any(s["entity_id"] == eid for s in sits))
            # final sweep: gate 2 and gate 3
            judg = await judgments(inc.db)
            violations.extend(invariants.check_final_ge_floor(judg))
            violations.extend(invariants.check_no_trait_leak(transport.sent, backend.model_inputs()))
            for u in transport.unconsented_sends:
                violations.append({"invariant": "guardian_gate",
                                   "detail": "send to a guardian topic without valid consent", **u})
            fwds = await guardian_forwards_inc()
            return {
                "mode": mode, "episode": ep, "behavior": behavior,
                "n_violations": len(violations), "violations": violations[:12],
                "anomalies": anomalies[:8], "classes": classes, "u5_delays_s": delays[:40],
                "pushes": len(transport.stamps),
                "forwards": sum(1 for f in fwds if f["kind"] == "forward"),
                "guidance_119": sum(1 for f in fwds if f["kind"] == "guidance_119"),
                "resolved": sum(1 for f in fwds if f["kind"] == "resolved"),
                "model_calls": len(backend.calls),
                "n_guardians": len(guardians), **counts,
            }
        finally:
            try:
                await kill(inc)
            except Exception:  # noqa: BLE001, S110
                pass
