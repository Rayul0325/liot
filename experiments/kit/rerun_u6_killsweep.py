"""Re-run the E14 kill sweep on the CURRENT code (after U6) and store the windows for the kit.

E14's committed summary.json holds the sweep as measured BEFORE U6 (gate-1 kill-window recovery,
merged at 1643087). To quote the 'after' windows with a number behind them, this script runs the
same sweep (``experiments.e14_guardian_latency.killsweep --category gas door``) on the checked-out
code and writes ``experiments/kit/data/e14_killsweep_after_u6.json``. ``claims.yaml`` reads it as
the pseudo-experiment ``kit:e14_killsweep_after_u6``. It is not an experiment summary: it is the
kit's own re-measurement, labelled that way in REPORT_KIT.md.

    PYTHONPATH=$PWD /Users/rayul/liot/.venv/bin/python -m experiments.kit.rerun_u6_killsweep
"""

from __future__ import annotations

import ast
import json
import subprocess
import sys

from experiments.kit.check_numbers import KIT_DIR

OUT = KIT_DIR / "data" / "e14_killsweep_after_u6.json"


def main() -> int:
    cmd = [sys.executable, "-m", "experiments.e14_guardian_latency.killsweep", "--category", "gas", "door"]
    proc = subprocess.run(cmd, capture_output=True, text=True, check=False)
    windows = []
    records = 0
    for line in proc.stdout.splitlines():
        if line.startswith("WINDOW "):
            windows.append(ast.literal_eval(line[len("WINDOW ") :]))
        elif line.startswith("{'category'"):
            records += 1
    head = subprocess.run(["git", "rev-parse", "--short", "HEAD"], capture_output=True, text=True, check=False).stdout.strip()
    payload = {
        "source": "kit re-run of experiments.e14_guardian_latency.killsweep after U6 (not an experiment summary)",
        "command": "python -m experiments.e14_guardian_latency.killsweep --category gas door",
        "exit_code": proc.returncode,
        "git_head": head,
        "records": records,
        "windows": windows,
    }
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(payload, ensure_ascii=False, indent=1, sort_keys=True) + "\n", encoding="utf-8")
    print(f"wrote {OUT} exit_code={proc.returncode} records={records} windows={len(windows)}")
    return 0 if proc.returncode == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
