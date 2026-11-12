"""E13 runner: C4.6 rest x change-day sweep, C4.7 alternative rules, C4.8 effort sweep.

    PYTHONPATH=$PWD /Users/rayul/liot/.venv/bin/python -m experiments.e13_adapt_rules.run --quick
    PYTHONPATH=$PWD /Users/rayul/liot/.venv/bin/python -m experiments.e13_adapt_rules.run [--workers 4]
    PYTHONPATH=$PWD /Users/rayul/liot/.venv/bin/python -m experiments.e13_adapt_rules.run --analyze

One raw record per (part, variant, persona, arm, cond, seed) is appended (fsync) by the parent only;
resumable through done_keys. Everything is a 60-day DaySim run (fake models, StochasticResident); the
script, the resident draws and the sensors follow the seed, so arms share random numbers (CRN).
"""

from __future__ import annotations

import argparse
import asyncio
import multiprocessing as mp
import sys
import tempfile
import time
from pathlib import Path
from typing import Any

from experiments.common import io
from experiments.e6_personalization import derive as e6_derive
from experiments.e13_adapt_rules import analyze, config, rules

EXP_DIR = Path(__file__).resolve().parent
RAW = EXP_DIR / "raw" / "runs.jsonl"

# part, variant, persona, arm, cond, seed, days
Job = tuple[str, str, str, str, str, int, int]


def jobs(seeds: tuple[int, ...], days: int, parts: tuple[str, ...] = ("c46", "c47", "c48"),
         personas: tuple[str, ...] | None = None) -> list[Job]:
    out: list[Job] = []
    pers = personas or tuple(config.e6.PERSONAS)
    if "c46" in parts:
        for persona in pers:
            for flip in config.FLIPS:
                for rest in config.RESTS:
                    out.extend(("c46", "main", persona, config.rest_arm(rest, flip), "B", s, days)
                               for s in seeds)
                out.extend(("c46", "main", persona, f"none_flip{flip}", "B", s, days) for s in seeds)
    if "c47" in parts:
        for variant in config.RULE_VARIANTS:
            for persona in pers:
                for arm in config.RULE_ARMS:
                    out.extend(("c47", variant, persona, arm, "B", s, days) for s in seeds)
    if "c48" in parts:
        for persona in pers:
            for eff in config.EFFORTS:
                for cond in ("A", "B"):
                    out.extend(("c48", "main", persona, config.effort_arm(eff), cond, s, days)
                               for s in seeds)
    return out


def plan(job: Job) -> dict[str, Any]:
    """Job -> DaySim kwargs, resident overrides, flip day, rules (the single place that decodes arms)."""
    part, variant, _persona, arm, _cond, _seed, _days = job
    if part == "c46":
        if arm.startswith("none_flip"):
            flip = int(arm.removeprefix("none_flip"))
            return {"sim": {"learning_rate": 0.0, "reject_window_days": 0}, "res": {}, "flip": flip,
                    "rules": rules.RuleSet(), "rest": 0, "effort": 0.12}
        rest_s, flip_s = arm.removeprefix("rest").split("_flip")
        return {"sim": {"reject_window_days": int(rest_s)} if int(rest_s) != 30 else {}, "res": {},
                "flip": int(flip_s), "rules": rules.RuleSet(), "rest": int(rest_s), "effort": 0.12}
    if part == "c47":
        spec = config.RULE_ARMS[arm]
        return {"sim": dict(spec["sim"]), "res": dict(config.RULE_VARIANTS[variant]),
                "flip": config.e6.FLIP_DAY, "rules": spec["rules"],
                "rest": spec["sim"].get("reject_window_days", 30), "effort": 0.12}
    eff = float(arm.removeprefix("eff"))
    return {"sim": {}, "res": {"effort_cost": eff}, "flip": config.e6.FLIP_DAY,
            "rules": rules.RuleSet(), "rest": 30, "effort": eff}


def run_one(job: Job) -> dict[str, Any]:
    from liot.day_sim import DaySim

    part, variant, persona, arm, cond, seed, days = job
    p = plan(job)
    resident = config.resident(persona, flip_day=p["flip"], **p["res"])
    t0 = time.perf_counter()
    with rules.installed(p["rules"]):
        sim = DaySim(seed=seed, days=days, condition=cond, resident=resident, research=True, **p["sim"])
        asyncio.run(sim.run(lambda stage, data: None))
    elapsed = time.perf_counter() - t0
    svc = sim._svc  # noqa: SLF001 - rule counters live on the experiment subclass
    stats = dict(getattr(svc, "rule_stats", {}) or {})
    return {
        "part": part, "variant": variant, "persona": persona, "arm": arm, "cond": cond, "seed": seed,
        "days": days, "flip": p["flip"], "rest": p["rest"], "effort": p["effort"],
        "rules": [k for k, v in vars(p["rules"]).items() if v],
        **e6_derive.derive(sim.summary(), resident, config.newly_helpful(persona), days),
        "rule_stats": stats,
        "elapsed_s": round(elapsed, 2),
        "meta": io.run_meta(),
    }


def collect(workers: int, todo_jobs: list[Job], path: Path) -> int:
    done = io.done_keys(path, config.KEY)
    todo = [j for j in todo_jobs if tuple(j[:6]) not in done]
    print(f"{len(todo)} runs to do ({len(todo_jobs) - len(todo)} already done)", flush=True)
    written = 0
    ctx = mp.get_context("spawn")
    with ctx.Pool(workers) as pool:
        for rec in pool.imap_unordered(run_one, todo):
            io.append_jsonl(path, rec)
            written += 1
            if not rec["safety_ok"]:
                print(f"SAFETY CHECK FAILED in {rec['part']}/{rec['variant']}/{rec['persona']}/"
                      f"{rec['arm']}/{rec['cond']}/{rec['seed']}", flush=True)
                pool.terminate()
                return -written
            if written % 20 == 0:
                print(f"  {written}/{len(todo)}  last={rec['elapsed_s']}s load1={rec['meta']['load1']}",
                      flush=True)
    return written


def make_figures(summary: dict[str, Any]) -> list[Path]:
    from experiments.e13_adapt_rules import figures

    return figures.make_all(EXP_DIR, summary)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--analyze", action="store_true")
    parser.add_argument("--quick", action="store_true", help="smoke: 1 seed, 20 days, hater only")
    parser.add_argument("--workers", type=int, default=4)
    parser.add_argument("--part", choices=("c46", "c47", "c48"), action="append")
    args = parser.parse_args()
    if args.analyze:
        summary = analyze.summarize(EXP_DIR / "raw")
        print(f"summary: {io.write_summary(EXP_DIR, summary)}")
        for p in make_figures(summary):
            print(f"figure: {p}")
        return
    if args.quick:
        path = Path(tempfile.mkdtemp(prefix="e13-quick-")) / "runs.jsonl"
        keep = ("rest7_flip15", "rest30_flip15", "none_flip15", "lr30", "r1", "r2", "r3", "r123",
                "eff0.000", "eff0.330")
        todo = [j for j in jobs((1,), 20, personas=("hater",)) if j[3] in keep]
        print(f"quick -> {path}")
        n = collect(args.workers, todo, path)
        print(f"wrote {n} quick records")
        return
    parts = tuple(args.part) if args.part else ("c46", "c47", "c48")
    n = collect(args.workers, jobs(config.SEEDS, config.DAYS, parts), RAW)
    print(f"wrote {n} new records to {RAW}")
    if n < 0:
        sys.exit(2)


if __name__ == "__main__":
    main()
