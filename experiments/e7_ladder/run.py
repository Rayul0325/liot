"""E7 runner — H6.1 ladder conformance (real service, fake clock) and H6.2 guardian paging MC.

    PYTHONPATH=$PWD .venv/bin/python -m experiments.e7_ladder.run --part conf
    PYTHONPATH=$PWD .venv/bin/python -m experiments.e7_ladder.run --part mc
    PYTHONPATH=$PWD .venv/bin/python -m experiments.e7_ladder.run --quick
    PYTHONPATH=$PWD .venv/bin/python -m experiments.e7_ladder.run --analyze
"""

from __future__ import annotations

import argparse
import asyncio
import itertools
from pathlib import Path

from experiments.common import io, plotstyle
from experiments.e7_ladder import analyze, mc, scenarios

EXP_DIR = Path(__file__).resolve().parent
RAW_DIR = EXP_DIR / "raw"
DRIVERS = ("event", "tick5", "tick5_p1.7", "tick5_p3.3")


async def collect_conf(raw: Path, only: set[str] | None = None) -> int:
    path = raw / "conformance.jsonl"
    done = io.done_keys(path, ("scenario", "driver"))
    written = 0
    for sc in scenarios.build_scenarios():
        if only and sc.sid not in only:
            continue
        for driver in DRIVERS:
            if (sc.sid, driver) in done:
                continue
            rec = await scenarios.run_scenario(sc, driver)
            rec["meta"] = io.run_meta()
            io.append_jsonl(path, rec)
            written += 1
    return written


def collect_mc(raw: Path, quick: bool) -> int:
    path = raw / "mc.jsonl"
    done = io.done_keys(path, ("mu_min", "q", "b", "n_guard"))
    written = 0
    means = (1.0, 10.0) if quick else mc.MEANS_MIN
    nonresp = (0.0, 0.4) if quick else mc.NONRESP
    exps = (0.0, 0.6) if quick else mc.EXPONENTS
    guards = (1, 3) if quick else mc.GUARDIANS
    batches = 2 if quick else mc.N_BATCHES
    for mu, q, b, n in itertools.product(means, nonresp, exps, guards):
        if (mu, q, b, n) in done:
            continue
        recs = [mc.simulate_batch(1000 + s, mu_min=mu, q=q, b=b, n=n) for s in range(batches)]
        io.append_jsonl(path, {
            "mu_min": mu, "q": q, "b": b, "n_guard": n, "batches": recs,
            "closed_form": mc.closed_form(mu, q, b, n), "meta": io.run_meta(),
        })
        written += 1
    return written


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
    p.add_argument("--part", choices=["conf", "mc", "all"], default="all")
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
    if args.part in ("conf", "all"):
        only = {"S01", "S03", "S09"} if args.quick else None
        print("conf wrote", asyncio.run(collect_conf(raw, only)))
    if args.part in ("mc", "all"):
        print("mc wrote", collect_mc(raw, args.quick))


if __name__ == "__main__":
    main()
