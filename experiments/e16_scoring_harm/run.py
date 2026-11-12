"""E16 runner — C4.12 (scoring + floor / vulnerability multiplier) and C4.13 (severity-weighted harm, burden).

    PYTHONPATH=$PWD .venv/bin/python -m experiments.e16_scoring_harm.run --part scoring
    PYTHONPATH=$PWD .venv/bin/python -m experiments.e16_scoring_harm.run --part policy [--blocks c413a,...]
    PYTHONPATH=$PWD .venv/bin/python -m experiments.e16_scoring_harm.run --part repro --repro-sets 30
    PYTHONPATH=$PWD .venv/bin/python -m experiments.e16_scoring_harm.run --analyze
    PYTHONPATH=$PWD .venv/bin/python -m experiments.e16_scoring_harm.run --quick     # smoke into a scratch dir

No model or network call. Resumable (done_keys); every record carries run_meta() and the policy records the
1-minute load average.
"""

from __future__ import annotations

import argparse
import tempfile
from pathlib import Path

from experiments.common import io

EXP_DIR = Path(__file__).resolve().parent
RAW = EXP_DIR / "raw"


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--part", default="all", choices=("all", "scoring", "policy", "repro", "severity"))
    ap.add_argument("--blocks", default="all")
    ap.add_argument("--workers", type=int, default=4)
    ap.add_argument("--repro-sets", type=int, default=30)
    ap.add_argument("--analyze", action="store_true")
    ap.add_argument("--quick", action="store_true")
    args = ap.parse_args()

    from experiments.e16_scoring_harm import analyze, policy, scoring

    raw = RAW
    if args.quick:
        raw = Path(tempfile.mkdtemp(prefix="e16_quick_")) / "raw"
        raw.mkdir(parents=True)
        print(f"[quick] scratch raw dir: {raw}")
    if args.analyze and not args.quick:
        summary = analyze.summarize(raw)
        print(f"summary: {io.write_summary(EXP_DIR, summary)}")
        from experiments.e16_scoring_harm import figures

        for p in figures.make_all(summary, EXP_DIR):
            print(f"figure: {p}")
        return
    workers = min(args.workers, 4)
    if args.part in ("all", "scoring"):
        n_u = scoring.collect_universe(raw)
        n_g = scoring.collect_grid(raw)
        n_r = scoring.collect_random(raw, scoring.N_RANDOM_QUICK if args.quick else scoring.N_RANDOM)
        print(f"scoring: universe={n_u} grid={n_g} random={n_r}")
    if args.part in ("all", "severity"):
        print(f"severity coder rows: {policy.collect_severity(raw)}")
    if args.part in ("all", "policy"):
        cfg = policy.QUICK if args.quick else policy.FULL
        blocks = policy.BLOCKS if args.blocks == "all" else tuple(args.blocks.split(","))
        print(f"policy records written: {policy.collect(raw, cfg, blocks, workers)}")
    if args.part == "repro":
        print(f"repro records written: {policy.collect_repro(raw, args.repro_sets)}")
    if args.quick:
        summary = analyze.summarize(raw)
        print("[quick] summary keys:", sorted(summary))


if __name__ == "__main__":
    main()
