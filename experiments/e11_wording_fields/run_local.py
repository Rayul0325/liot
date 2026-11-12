"""E11 local judges: one llama-server session per model over every E11 arm (bench runner reused, not copied).

    PYTHONPATH=$PWD .venv/bin/python -m experiments.e11_wording_fields.run_local MODEL [--quick] [--port N]

The bench runner's ``cmd_run`` does the serving, the evidence-key check, the timing and the resumable raw
writes.  Two module attributes are replaced for this process only: ``build_all`` (our arms, ids
``<arm>:<case>``) and ``validate_labels`` (the draft labels do not cover rewritten arms).  Records land in
``raw/local/<model>.jsonl``.  Local answers are deterministic (letter probabilities), so one rep is run; the
first-repetition determinism of the E5 baselines is the stated basis (verified there for one model only).
"""

from __future__ import annotations

import argparse
from argparse import Namespace
from pathlib import Path

from experiments.e11_wording_fields import variants
from experiments.e5_models.benchkit import LOCAL_MODELS, run_bench

EXP_DIR = Path(__file__).resolve().parent
ARMS = (*variants.FIELD_ARMS, *variants.REWRITE_ARMS)  # baselines come from the E5 raw files


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("model", choices=LOCAL_MODELS)
    ap.add_argument("--quick", action="store_true", help="smoke: one arm prefix, first cases only")
    ap.add_argument("--port", type=int, default=18770)
    args = ap.parse_args()
    rb = run_bench()
    arms = ARMS
    variants.build_arm("operational")  # primes the original build_all before it is replaced
    rb.build_all = lambda _cases, _mode="sentence": variants.build_local_all(arms)
    rb.validate_labels = lambda _built, _labels: []
    ns = Namespace(
        model=args.model, port=args.port, context="e11", latency_only=False, reps=1,
        only=("f_A:sit-gas" if args.quick else None),
        raw_dir=str(EXP_DIR / ("raw/local_quick" if args.quick else "raw/local")),
    )
    return int(rb.cmd_run(ns))


if __name__ == "__main__":
    raise SystemExit(main())
