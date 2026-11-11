"""Regenerate the report kit in one command: figures, experiments/REPORT_KIT.md, claims check.

    PYTHONPATH=$PWD /Users/rayul/liot/.venv/bin/python -m experiments.kit.build

Experiments that have not landed yet (none today: e6/e8/e9 have landed) are tolerated: their
figure panels and claim blocks carry a visible 'pending' marker. When such a summary appears
while the kit still says 대기, the build prints NEW-RESULT and exits 1 so nobody forgets to fill
in claims.yaml / hypotheses.yaml.

Exit code: 0 all checks pass; 1 any failure (number mismatch, stale kit, figure error, NEW-RESULT).
"""

from __future__ import annotations

import sys
from pathlib import Path

from experiments.kit import check_numbers, figures, render
from experiments.kit.check_numbers import EXP_DIR, load_summary

PENDING_EXPERIMENTS: tuple[str, ...] = ()  # e6/e8/e9 have all landed; add a name here for a future experiment


def main() -> int:
    failed = False

    pending = [e for e in PENDING_EXPERIMENTS if load_summary(e) is None]
    arrived = [e for e in PENDING_EXPERIMENTS if load_summary(e) is not None]
    for exp in pending:
        print(f"PENDING  {exp}: summary.json 이 아직 없다 — 그림·자료집에 '대기' 표시")
    for exp in arrived:
        print(f"ARRIVED  {exp}: summary.json 도착 — claims.yaml / hypotheses.yaml / figures.py 를 채워라")

    _, warnings = figures.effective_verdicts()
    for line in warnings:
        print(line)
        failed = True

    for name, fn in (
        ("a 가설 판정 지도", figures.make_verdict_map),
        ("b LIOT 대 대안", figures.make_overview),
        ("c 연구 사이클", figures.make_timeline),
    ):
        try:
            path = Path(fn())
            print(f"FIGURE   {name}: {path.relative_to(EXP_DIR.parent)}")
        except Exception as err:  # report every figure's failure, not just the first
            print(f"FIGURE-FAIL {name}: {err!r}")
            failed = True

    out = render.write()
    print(f"KIT      {out.relative_to(EXP_DIR.parent)}")

    code = check_numbers.main([])
    return 1 if (failed or code) else 0


if __name__ == "__main__":
    sys.exit(main())
