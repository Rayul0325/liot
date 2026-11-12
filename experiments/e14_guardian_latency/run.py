"""E14 runner — guardian paging criteria, restart fuzz, latency tails (cycle 4: C4.9, C4.10).

    PYTHONPATH=$PWD .venv/bin/python -m experiments.e14_guardian_latency.run --part regress
    PYTHONPATH=$PWD .venv/bin/python -m experiments.e14_guardian_latency.run --part hybrid
    PYTHONPATH=$PWD .venv/bin/python -m experiments.e14_guardian_latency.run --part fuzz [--n 2000]
    PYTHONPATH=$PWD .venv/bin/python -m experiments.e14_guardian_latency.run --part killsweep
    PYTHONPATH=$PWD .venv/bin/python -m experiments.e14_guardian_latency.run --part deadline
    PYTHONPATH=$PWD .venv/bin/python -m experiments.e14_guardian_latency.run --part coalesce --shard 0/4
    PYTHONPATH=$PWD .venv/bin/python -m experiments.e14_guardian_latency.run --part tick
    PYTHONPATH=$PWD .venv/bin/python -m experiments.e14_guardian_latency.run --part seq
    PYTHONPATH=$PWD .venv/bin/python -m experiments.e14_guardian_latency.run --quick   # smoke, raw_quick/
    PYTHONPATH=$PWD .venv/bin/python -m experiments.e14_guardian_latency.run --analyze

Parts: see ``PREREG.md`` (readings fixed before the full runs) and RESULTS.md. Every record carries
``run_meta()`` (commit, dirty flag, load1); runs resume through ``io.done_keys``.
"""

from __future__ import annotations

import argparse
import asyncio
import itertools
import random
import time
from pathlib import Path
from typing import Any

from experiments.common import io, plotstyle
from experiments.common.service_kit import (
    TimedTransport,
    build_service,
    proposals,
    reading,
    situations,
)
from experiments.e2_invariants import fuzz as e2fuzz
from experiments.e2_invariants import quota
from experiments.e7_ladder import scenarios
from experiments.e14_guardian_latency import (
    analyze,
    coalesce,
    deadline,
    hybrid,
    killsweep,
    regress,
    restart_fuzz,
    seqpage,
)

EXP_DIR = Path(__file__).resolve().parent
RAW_DIR = EXP_DIR / "raw"
TICK_DRIVERS = ("tick1", "tick1_p0.37", "tick1_p0.71", "tick1_p0.93", "tick5_p3.3")
FUZZ_N = 2000
COAL_N = 48
COAL_K = (1, 2, 3)
COAL_R = (0.3, 1.0)
COAL_CATS = ("gas", "smoke", "leak", "door", "oven", "high_temp")


# ── regress (C4.9 a) ──────────────────────────────────────────────────────────
async def collect_regress(n: int, raw: Path) -> int:
    path = raw / "regress_quota.jsonl"
    done = io.done_keys(path, ("path", "episode"))
    written = 0
    for ep in range(n):
        for p in quota.PATHS:
            if (p, ep) in done:
                continue
            rec = await quota.run_episode(p, ep)
            rec["meta"] = io.run_meta()
            io.append_jsonl(path, rec)
            written += 1
    opath = raw / "regress_outage.jsonl"
    odone = io.done_keys(opath, ("outage_s", "episode"))
    for ep in range(n):
        for d in regress.OUTAGES:
            if (d, ep) in odone:
                continue
            rec = await regress.run_outage_episode(d, ep)
            rec["meta"] = io.run_meta()
            io.append_jsonl(opath, rec)
            written += 1
    return written


# ── hybrid (C4.9 b) ───────────────────────────────────────────────────────────
def hybrid_cells(quick: bool) -> list[dict[str, Any]]:
    cells = []
    for vack, unreg, obs, auc, atk, mode in itertools.product(
        hybrid.VULN_ACK if not quick else (0.5,),
        hybrid.UNREG if not quick else (0.0, 0.1),
        hybrid.OBS if not quick else (0.0, 0.1),
        hybrid.AUC if not quick else (0.5, 0.9),
        hybrid.ATTACK if not quick else (0.0, 0.3),
        hybrid.MODES,
    ):
        cells.append({"vuln_ack": vack, "unreg": unreg, "obs": obs, "auc": auc, "attack": atk,
                      "mode": mode})
    return cells


def hybrid_key(c: dict[str, Any]) -> str:
    return (f"v{c['vuln_ack']}|u{c['unreg']}|obs{c['obs']}|auc{c['auc']}|a{c['attack']}|"
            f"{c['mode']}")


def collect_hybrid(raw: Path, quick: bool) -> int:
    path = raw / "hybrid.jsonl"
    done = io.done_keys(path, ("key",))
    if ("selfcheck",) not in done:
        io.append_jsonl(path, {"key": "selfcheck", "equivalence": hybrid.check_equivalence(),
                               "meta": io.run_meta()})
    written = 0
    n_seeds = 2 if quick else hybrid.N_SEEDS
    for c in hybrid_cells(quick):
        key = hybrid_key(c)
        if (key,) in done:
            continue
        seeds = [hybrid.run_cell(s, **c) for s in range(n_seeds)]
        io.append_jsonl(path, {"key": key, **c, "n_seeds": n_seeds, "seeds": seeds,
                               "meta": io.run_meta()})
        written += 1
    return written


ACK_MEANS = (30.0, 60.0, 120.0, 240.0)


def collect_hybrid_sens(raw: Path) -> int:
    """Post-hoc sensitivity (added after the main run): the resident answer-time mean was fixed at
    60 s as an assumption; this shows how the prediction depends on it. Descriptive only."""
    path = raw / "hybrid_sens.jsonl"
    done = io.done_keys(path, ("key",))
    written = 0
    for vack, atk, am in itertools.product(hybrid.VULN_ACK, (0.0, 0.1), ACK_MEANS):
        c = {"vuln_ack": vack, "unreg": 0.1, "obs": 0.05, "auc": 0.8, "attack": atk,
             "mode": "honest", "ack_mean_s": am}
        key = f"v{vack}|a{atk}|mean{am:g}"
        if (key,) in done:
            continue
        seeds = [hybrid.run_cell(s, **c) for s in range(hybrid.N_SEEDS)]
        io.append_jsonl(path, {"key": key, **c, "n_seeds": hybrid.N_SEEDS, "seeds": seeds,
                               "meta": io.run_meta()})
        written += 1
    return written


# ── restart fuzz (C4.9 c) ─────────────────────────────────────────────────────
async def collect_fuzz(n_per: int, raw: Path) -> int:
    path = raw / "restart_fuzz.jsonl"
    done = io.done_keys(path, ("mode", "episode"))
    written = 0
    for ep in range(n_per):
        for mode in restart_fuzz.MODES:  # interleave
            if (mode, ep) in done:
                continue
            rec = await restart_fuzz.run_episode(mode, ep)
            rec["meta"] = io.run_meta()
            io.append_jsonl(path, rec)
            written += 1
    return written


async def collect_killsweep(raw: Path) -> int:
    path = raw / "killsweep.jsonl"
    if io.read_jsonl(path):
        return 0
    recs = await killsweep.sweep()
    meta = io.run_meta()
    for r in recs:
        io.append_jsonl(path, {**r, "meta": meta})
    return len(recs)


# ── deadline CDF (C4.10 a) ────────────────────────────────────────────────────
def collect_deadline(raw: Path) -> int:
    path = raw / "bench_latency.jsonl"
    if io.read_jsonl(path):
        return 0
    meta = io.run_meta()
    rows = deadline.load_rows()
    for r in rows:
        io.append_jsonl(path, {**r, "meta": meta})
    for s in deadline.session_info():
        io.append_jsonl(raw / "bench_sessions.jsonl", s)
    return len(rows)


# ── coalesce (C4.10 b) ────────────────────────────────────────────────────────
async def run_incident(variant: str, k: int, r: float, inc: int,
                       pool: list[float]) -> dict[str, Any]:
    rng = random.Random(f"e14/coal/{k}/{r}/{inc}")
    cats = rng.sample(COAL_CATS, k)
    plan = {e2fuzz.SENSORS[c][0]: (rng.choice(pool), rng.random() < r) for c in cats}
    backend = coalesce.ReplayBackend(plan)
    kit = await build_service(
        backend=backend, categories=e2fuzz.CATEGORIES, phase2_deadline_s=6.0,
        transport=TimedTransport(),
    )
    try:
        await coalesce.make_variant(kit, variant, 6.0)
        t0 = time.perf_counter()
        await asyncio.gather(*[
            kit.svc.handle_reading(reading(e2fuzz.SENSORS[c][0], e2fuzz.SENSORS[c][1]))
            for c in cats
        ])
        await kit.svc.drain_phase2()
        props = await proposals(kit.db)
        sit_of = {p["proposal_id"]: p["situation_id"] for p in props}
        sits = {s["situation_id"]: s for s in await situations(kit.db)}
        pushes = []
        for st in kit.transport.stamps:
            if st[2] != kit.svc.topic:
                continue
            sid = sit_of.get(st[3].get("proposal_id"))
            pushes.append({"t_s": st[0] - t0, "category": sits[sid]["category"] if sid else None,
                           "floor": sits[sid]["severity_floor"] if sid else None,
                           "severity_label": st[3].get("severity_label")})
        return {
            "variant": variant, "k": k, "raise_p": r, "inc": inc, "categories": cats,
            "plan": {sits_c: [plan[e2fuzz.SENSORS[sits_c][0]][0],
                              plan[e2fuzz.SENSORS[sits_c][0]][1]] for sits_c in cats},
            "pushes": pushes, "n_pushes": len(pushes),
            "hold_log": getattr(kit.svc, "hold_log", []),
        }
    finally:
        await kit.close()


async def collect_coalesce(raw: Path, quick: bool, shard: tuple[int, int]) -> int:
    pool = deadline.operational_two_question_pool()
    path = raw / f"coalesce_shard{shard[0]}of{shard[1]}.jsonl"
    done = io.done_keys(path, ("variant", "k", "raise_p", "inc"))
    n_inc = 3 if quick else COAL_N
    written = 0
    ks = (1, 2) if quick else COAL_K
    for inc in range(n_inc):
        for k, r in itertools.product(ks, COAL_R):
            if hash_shard(k, r, inc) % shard[1] != shard[0]:
                continue
            order = list(coalesce.VARIANTS)
            random.Random(f"e14/coalorder/{k}/{r}/{inc}").shuffle(order)  # interleave conditions
            for v in order:
                if (v, k, r, inc) in done:
                    continue
                rec = await run_incident(v, k, r, inc, pool)
                rec["meta"] = io.run_meta()
                io.append_jsonl(path, rec)
                written += 1
    return written


def hash_shard(k: int, r: float, inc: int) -> int:
    return k * 1000 + int(r * 10) * 100 + inc


# ── tick (C4.10 c) ────────────────────────────────────────────────────────────
async def collect_tick(raw: Path, only: set[str] | None = None) -> int:
    path = raw / "tick.jsonl"
    done = io.done_keys(path, ("scenario", "driver"))
    written = 0
    for sc in scenarios.build_scenarios():
        if only and sc.sid not in only:
            continue
        for driver in TICK_DRIVERS:
            if (sc.sid, driver) in done:
                continue
            rec = await scenarios.run_scenario(sc, driver)
            rec["meta"] = io.run_meta()
            io.append_jsonl(path, rec)
            written += 1
    return written


# ── sequential paging (C4.10 c) ───────────────────────────────────────────────
def collect_seq(raw: Path, quick: bool) -> int:
    path = raw / "seq.jsonl"
    done = io.done_keys(path, ("mu_min", "q", "b", "n_guard"))
    written = 0
    means = (1.0, 10.0) if quick else seqpage.MEANS_MIN
    nonresp = (0.0, 0.4) if quick else seqpage.NONRESP
    exps = (0.0, 0.6) if quick else seqpage.EXPONENTS
    guards = (2, 3) if quick else seqpage.GUARDIANS
    batches = 2 if quick else seqpage.N_BATCHES
    for mu, q, b, n in itertools.product(means, nonresp, exps, guards):
        if (mu, q, b, n) in done:
            continue
        recs = [seqpage.simulate_batch(2000 + s, mu_min=mu, q=q, b=b, n=n) for s in range(batches)]
        io.append_jsonl(path, {"mu_min": mu, "q": q, "b": b, "n_guard": n, "batches": recs,
                               "closed_check": seqpage.closed_check(mu, q, b, n),
                               "meta": io.run_meta()})
        written += 1
    return written


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
    p.add_argument("--part", default="all", choices=[
        "regress", "hybrid", "hybrid_sens", "fuzz", "killsweep", "deadline", "coalesce", "tick", "seq", "all"])
    p.add_argument("--n", type=int, default=None)
    p.add_argument("--shard", default="0/1")
    p.add_argument("--quick", action="store_true")
    p.add_argument("--analyze", action="store_true")
    args = p.parse_args()
    if args.analyze:
        summary = analyze.summarize(RAW_DIR)
        print(f"summary: {io.write_summary(EXP_DIR, summary)}")
        for f in analyze.make_figures(summary, EXP_DIR, plotstyle):
            print(f"figure: {f}")
        return
    raw = EXP_DIR / "raw_quick" if args.quick else RAW_DIR
    si, sn = (int(x) for x in args.shard.split("/"))

    async def go() -> None:
        if args.part in ("regress", "all"):
            print("regress wrote", await collect_regress(args.n or (3 if args.quick else 100), raw))
        if args.part in ("fuzz", "all"):
            print("fuzz wrote", await collect_fuzz(args.n or (6 if args.quick else FUZZ_N), raw))
        if args.part in ("killsweep", "all"):
            print("killsweep wrote", await collect_killsweep(raw))
        if args.part in ("coalesce", "all"):
            print("coalesce wrote", await collect_coalesce(raw, args.quick, (si, sn)))
        if args.part in ("tick", "all"):
            print("tick wrote", await collect_tick(raw, {"S01", "S03", "S14"} if args.quick else None))

    if args.part in ("hybrid", "all"):
        print("hybrid wrote", collect_hybrid(raw, args.quick))
    if args.part in ("hybrid_sens", "all"):
        print("hybrid_sens wrote", collect_hybrid_sens(raw))
    if args.part in ("deadline", "all"):
        print("deadline wrote", collect_deadline(raw))
    if args.part in ("seq", "all"):
        print("seq wrote", collect_seq(raw, args.quick))
    asyncio.run(go())


if __name__ == "__main__":
    main()
