"""C4.10 (a): the real model-latency distribution from the bench raw data (descriptive).

C4.10 (a) was dropped as a hypothesis by the metric review (the bench CDF already showed the
answer before registration), so this module only reads ``seconds`` out of ``bench/risk/raw*/`` and
writes one raw record per bench row; ``analyze.py`` turns them into the cumulative share of model
answers that come back within 0.25 / 0.5 / 1.0 s, which is what a short-deadline arm of E1
(``arms.short_<w>``) would catch. The arm itself is the production-derived wrapper in
``experiments/e1_latency/arms.py``; replaying these latencies through it would reproduce this CDF
by construction (the arm waits ``min(model latency, w)``), so it is not re-run here.

Caveats carried into the report: the bench ran on the Mac Studio with load around 10 on
2026-10-19 and the bench notes call the latency verdicts unreliable (bimodal prompt-eval
throughput); ``questions == 2`` rows are the safety-situation judgments (tier + severity) the
first alert would wait for, ``questions == 1`` are comfort-style single-question judgments.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from experiments.common import io

BENCH = io.repo_root() / "bench" / "risk"
DIRS = ("raw", "raw_operational", "raw_structured", "raw_subtype", "raw_subtype_trait")
WINDOWS = (0.25, 0.5, 1.0)


def load_rows() -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for d in DIRS:
        for f in sorted((BENCH / d).glob("*.jsonl")):
            if f.name.endswith(".sessions.jsonl"):
                continue
            for line in f.read_text(encoding="utf-8").splitlines():
                if not line.strip():
                    continue
                r = json.loads(line)
                rows.append({
                    "dir": d, "model": r["model"], "id": r["id"], "rep": r["rep"],
                    "group": r["group"], "questions": r["questions"], "seconds": r["seconds"],
                    "degraded": r.get("degraded"),
                })
    return rows


def session_info() -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for d in DIRS:
        for f in sorted((BENCH / d).glob("*.sessions.jsonl")):
            for line in f.read_text(encoding="utf-8").splitlines():
                if line.strip():
                    s = json.loads(line)
                    out.append({"dir": d, "model": s.get("model"), "started_at": s.get("started_at"),
                                "status": s.get("status")})
    return out


def operational_two_question_pool() -> list[float]:
    """Latencies (s) of the 2-question rows of the production-like context (raw_operational)."""
    return sorted(r["seconds"] for r in load_rows()
                  if r["dir"] == "raw_operational" and r["questions"] == 2)
