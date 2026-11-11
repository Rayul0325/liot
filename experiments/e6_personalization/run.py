"""E6 runner: personalisation arms x condition A/B x persona x seed over 60 simulated days.

    PYTHONPATH=$PWD .venv/bin/python -m experiments.e6_personalization.run --quick
    PYTHONPATH=$PWD .venv/bin/python -m experiments.e6_personalization.run [--workers 3]
    PYTHONPATH=$PWD .venv/bin/python -m experiments.e6_personalization.run --analyze

One raw record per (variant, persona, arm, cond, seed) is appended (fsync) to raw/runs.jsonl by the
parent process only; a killed run resumes from done_keys. 30-day results are the first 30 days of
the same 60-day runs (the script, the resident's draws and the sensors are a deterministic prefix).
Timing is not a metric here, but load1 is recorded in every record.
"""

from __future__ import annotations

import argparse
import asyncio
import multiprocessing as mp
import sys
import time
from pathlib import Path
from typing import Any

from experiments.common import io
from experiments.e6_personalization import analyze, config, derive

EXP_DIR = Path(__file__).resolve().parent
RAW = EXP_DIR / "raw" / "runs.jsonl"
KEY = ("variant", "persona", "arm", "cond", "seed")

Job = tuple[str, str, str, str, int, int]  # variant, persona, arm, cond, seed, days


def jobs(seeds: tuple[int, ...], days: int, personas: tuple[str, ...] | None = None,
         variants: tuple[str, ...] | None = None) -> list[Job]:
    out: list[Job] = []
    for variant, spec in config.VARIANTS.items():
        if variants is not None and variant not in variants:
            continue
        for persona in personas or tuple(config.PERSONAS):
            for arm in spec["arms"]:
                for cond in spec["conds"]:
                    out.extend((variant, persona, arm, cond, seed, days) for seed in seeds)
    return out


def run_one(job: Job) -> dict[str, Any]:
    """One 60-day DaySim run -> one raw record (executed in a worker process)."""
    from liot.day_sim import DaySim

    variant, persona, arm, cond, seed, days = job
    resident = config.make_resident(persona, config.VARIANTS[variant]["resident"])
    sim = DaySim(seed=seed, days=days, condition=cond, resident=resident, research=True,
                 **config.ARMS[arm])
    t0 = time.perf_counter()
    asyncio.run(sim.run(lambda stage, data: None))
    elapsed = time.perf_counter() - t0
    rec: dict[str, Any] = {
        "variant": variant, "persona": persona, "arm": arm, "cond": cond, "seed": seed,
        "days": days,
        **derive.derive(sim.summary(), resident, config.newly_helpful(persona), days),
        "elapsed_s": round(elapsed, 2),
        "meta": io.run_meta(),
    }
    return rec


def collect(workers: int, todo_jobs: list[Job], path: Path) -> int:
    done = io.done_keys(path, KEY)
    todo = [j for j in todo_jobs if tuple(j[:5]) not in done]
    print(f"{len(todo)} runs to do ({len(todo_jobs) - len(todo)} already done)", flush=True)
    written = 0
    bad = 0
    ctx = mp.get_context("spawn")
    with ctx.Pool(workers) as pool:
        for rec in pool.imap_unordered(run_one, todo):
            io.append_jsonl(path, rec)
            written += 1
            if not rec["safety_ok"]:
                bad += 1
                print(f"SAFETY CHECK FAILED in {rec['variant']}/{rec['persona']}/{rec['arm']}/"
                      f"{rec['cond']}/{rec['seed']}", flush=True)
                pool.terminate()
                break
            if written % 20 == 0:
                print(f"  {written}/{len(todo)}  last={rec['elapsed_s']}s load1="
                      f"{rec['meta']['load1']}", flush=True)
    return -bad if bad else written


def make_figures(summary: dict[str, Any]) -> list[Path]:
    from experiments.e6_personalization import figures

    return figures.make_all(EXP_DIR, summary)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--analyze", action="store_true", help="write summary.json and figures")
    parser.add_argument("--quick", action="store_true", help="smoke: 1 seed, 30 days, hater only")
    parser.add_argument("--workers", type=int, default=3)
    args = parser.parse_args()
    if args.analyze:
        summary = analyze.summarize(EXP_DIR / "raw")
        print(f"summary: {io.write_summary(EXP_DIR, summary)}")
        for p in make_figures(summary):
            print(f"figure: {p}")
        return
    if args.quick:
        path = Path("/private/tmp/claude-501/-Users-rayul-liot/b877ae61-64ae-4512-b8b4-d5ca79c8f749/"
                    "scratchpad/b3_e6_quick.jsonl")
        todo = jobs((1,), 30, personas=("hater",), variants=("main",))
        print(f"quick -> {path}")
        n = collect(args.workers, todo, path)
        print(f"wrote {n} quick records")
        return
    n = collect(args.workers, jobs(config.SEEDS, config.DAYS), RAW)
    print(f"wrote {n} new records to {RAW}")
    if n < 0:
        sys.exit(2)


if __name__ == "__main__":
    main()
