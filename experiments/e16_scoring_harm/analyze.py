"""E16 analysis entry point: recomputes results/summary.json purely from raw/ (see scoring.py, policy_analyze.py)."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from experiments.e16_scoring_harm import policy_analyze, scoring


def summarize(raw_dir: Path) -> dict[str, Any]:
    raw = Path(raw_dir)
    return {
        "note": ("E16 = C4.12 (E4 follow-up: floor, vulnerability multiplier >= 5) and C4.13 (E3 follow-up: "
                 "severity-weighted harm, burden off, H2.2 diagnosis). Registered form: HYPOTHESES.md a73414c."),
        "c412": scoring.summarize(raw),
        "c413": policy_analyze.summarize(raw),
    }
