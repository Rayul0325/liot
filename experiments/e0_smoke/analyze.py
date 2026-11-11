"""e0_smoke analysis: reads only raw/, deterministic (verify recomputes it)."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import numpy as np

from experiments.common.io import read_jsonl


def summarize(raw_dir: Path) -> dict[str, Any]:
    """count / median / p95 of first-alert latency (ms) over the recorded runs."""
    rows = read_jsonl(Path(raw_dir) / "runs.jsonl")
    values = np.array([r["latency_ms"] for r in rows if r.get("latency_ms") is not None])
    if values.size == 0:
        return {"count": 0, "median_ms": None, "p95_ms": None}
    return {
        "count": int(values.size),
        "median_ms": float(np.median(values)),
        "p95_ms": float(np.percentile(values, 95)),
    }
