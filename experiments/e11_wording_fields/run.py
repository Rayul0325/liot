"""E11 driver.

    # 0. blind inputs / agents (see prep_*.py, checks.py, combine.py): rewrites, field codes, rule map
    # 1. local judges (two lanes):   zsh experiments/e11_wording_fields/run_local_all.sh
    # 2. cloud judges (cap $0.60):   PYTHONPATH=$PWD .venv/bin/python -m experiments.e11_wording_fields.run_cloud
    # 3. collect + analyse:          PYTHONPATH=$PWD .venv/bin/python -m experiments.e11_wording_fields.run
    # smoke (one case prefix, one rep, 1 local model + 3 cloud judges):  ... .run --quick

``--quick`` only exercises the pipeline end to end on the ``sit-gas`` cases into ``raw/local_quick`` and (cloud)
the normal ``raw/answers_cloud.jsonl`` rows for those cases; it never writes results/summary.json.
"""

from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

from experiments.common import io
from experiments.e11_wording_fields import analyze, collect

EXP_DIR = Path(__file__).resolve().parent


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--quick", action="store_true", help="smoke test only; no summary written")
    args = ap.parse_args()
    if args.quick:
        for mod, extra in (("run_local", ["jevk5-4b-q4", "--quick"]), ("run_cloud", ["--quick"])):
            rc = subprocess.call([sys.executable, "-m", f"experiments.e11_wording_fields.{mod}", *extra])
            print(mod, "exit", rc)
        return
    print("collected:", collect.collect_local(), collect.collect_rule(), collect.collect_design())
    summary = analyze.summarize(EXP_DIR / "raw")
    print("summary:", io.write_summary(EXP_DIR, summary))
    from experiments.e11_wording_fields import figures

    for p in figures.make_all(summary, EXP_DIR):
        print("figure:", p)


if __name__ == "__main__":
    main()
