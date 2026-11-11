"""E9 analysis: reads only raw/derived.jsonl (committed derived numbers), deterministic.

Raw model responses live in raw/cloud_*.jsonl (git-ignored) and are never needed here.
Intervals are 95% percentile bootstraps over seeds (clusters) with a fixed generator seed.
"""

from __future__ import annotations

from collections import Counter
from pathlib import Path
from typing import Any

import numpy as np

from experiments.common.io import read_jsonl

BOOT = 4000
BOOT_SEED = 20261112


def _cluster_ratio(rng: np.random.Generator, num: np.ndarray, den: np.ndarray) -> dict[str, Any]:
    n = len(num)
    if n == 0 or den.sum() == 0:
        return {"seeds": n, "num": 0, "den": 0, "rate": None, "lo": None, "hi": None}
    idx = rng.integers(0, n, size=(BOOT, n))
    dd = den[idx].sum(axis=1)
    ok = dd > 0
    boots = num[idx].sum(axis=1)[ok] / dd[ok]
    return {"seeds": n, "num": int(num.sum()), "den": int(den.sum()), "rate": float(num.sum() / den.sum()),
            "lo": float(np.percentile(boots, 2.5)), "hi": float(np.percentile(boots, 97.5))}


def summarize(raw_dir: Path) -> dict[str, Any]:
    rows = sorted(read_jsonl(Path(raw_dir) / "derived.jsonl"), key=lambda r: r["seed"])
    if not rows:
        return {"runs": 0}
    rng = np.random.default_rng(BOOT_SEED)
    out: dict[str, Any] = {
        "runs": len(rows), "seeds": [r["seed"] for r in rows], "days_per_run": rows[0]["days"],
        "usd_total_estimated": sum(r["usd"] for r in rows),
        "calls_total": sum(r["calls"] for r in rows),
        "usd_per_run": [r["usd"] for r in rows], "calls_per_run": [r["calls"] for r in rows],
        "tripped_runs": [r["seed"] for r in rows if r["tripped"] is not None],
        "unknown_price_calls": sum(r["unknown_price_calls"] for r in rows),
        "estimated_calls": sum(r["estimated_calls"] for r in rows),
        "kit_status": rows[0]["status"],
    }
    # per-stage latency: pooled seconds per call, and the spread of the per-run means
    kinds = sorted({k for r in rows for k in r["by_kind"]})
    stage: dict[str, Any] = {}
    for k in kinds:
        sec = np.array([r["by_kind"].get(k, {}).get("seconds", 0.0) for r in rows])
        cal = np.array([r["by_kind"].get(k, {}).get("calls", 0) for r in rows], dtype=float)
        per_run = np.array([s / c for s, c in zip(sec, cal, strict=True) if c > 0])
        stage[k] = {
            "calls": int(cal.sum()), "usd": float(sum(r["by_kind"].get(k, {}).get("usd", 0.0) for r in rows)),
            "mean_s_per_call": float(sec.sum() / cal.sum()) if cal.sum() else None,
            "per_run_mean_s_median": float(np.median(per_run)) if per_run.size else None,
            "per_run_mean_s_min": float(per_run.min()) if per_run.size else None,
            "per_run_mean_s_max": float(per_run.max()) if per_run.size else None,
        }
    out["stage_latency"] = stage
    # reason writing and resident decisions
    n = np.array([r["reasons"]["n"] for r in rows], dtype=float)
    m = np.array([r["reasons"]["model"] for r in rows], dtype=float)
    out["reason_writing_model_rate"] = _cluster_ratio(rng, m, n)
    out["reason_fallback_causes"] = dict(sorted(sum((Counter(r["reasons"]["causes"]) for r in rows),
                                                    Counter()).items()))
    rn = np.array([r["resident"]["n_decisions"] for r in rows], dtype=float)
    rm = np.array([r["resident"].get("model", 0) for r in rows], dtype=float)
    out["resident_model_rate"] = _cluster_ratio(rng, rm, rn)
    out["resident_fallback_causes"] = dict(sorted(sum((Counter(r["resident"]["causes"]) for r in rows),
                                                      Counter()).items()))
    # safety path
    out["safety"] = {
        "first_alerts": int(sum(r["first_alerts"] for r in rows)),
        "first_alerts_immediate": int(sum(r["first_alerts_immediate"] for r in rows)),
        "days_total": int(sum(r["days"] for r in rows)),
        "first_alert_ms_max": max((x for r in rows for x in r["first_alert_ms"] if x is not None),
                                  default=None),
    }
    # H5.4: raw-answer agreement of Jev and luna, only where at least one departs from the Gate1 floor
    pairs = [[(s, p[0], p[1], p[2]) for p in r["tier_pairs"]] for r, s in
             zip(rows, [r["seed"] for r in rows], strict=True)]
    flat = [x for seed_pairs in pairs for x in seed_pairs]
    with_floor = [x for x in flat if x[1] is not None]

    def count(sel: list[tuple[Any, ...]]) -> tuple[int, int]:
        return sum(1 for x in sel if x[2] == x[3]), len(sel)

    per_seed_depart_agree, per_seed_depart_n, per_seed_all_agree, per_seed_all_n = [], [], [], []
    for seed_pairs in pairs:
        wf = [x for x in seed_pairs if x[1] is not None]
        dep = [x for x in wf if x[2] != x[1] or x[3] != x[1]]
        a, nn = count(dep)
        a2, n2 = count(seed_pairs)
        per_seed_depart_agree.append(a)
        per_seed_depart_n.append(nn)
        per_seed_all_agree.append(a2)
        per_seed_all_n.append(n2)
    depart = [x for x in with_floor if x[2] != x[1] or x[3] != x[1]]
    conf = Counter((x[2], x[3]) for x in depart)
    out["h54"] = {
        "tier_pairs_total": len(flat), "pairs_with_floor": len(with_floor),
        "depart_steps": len(depart),
        "agree_where_one_departs": _cluster_ratio(
            rng, np.array(per_seed_depart_agree, dtype=float), np.array(per_seed_depart_n, dtype=float)),
        "agree_all_steps": _cluster_ratio(
            rng, np.array(per_seed_all_agree, dtype=float), np.array(per_seed_all_n, dtype=float)),
        "both_at_floor": sum(1 for x in with_floor if x[2] == x[1] and x[3] == x[1]),
        "jev_departs": sum(1 for x in with_floor if x[2] != x[1]),
        "luna_departs": sum(1 for x in with_floor if x[3] != x[1]),
        "floor_counts": dict(sorted(Counter(x[1] for x in with_floor).items())),
        "depart_confusion_jev_luna": {f"{a}|{b}": c for (a, b), c in sorted(conf.items())},
        "luna_unavailable": sum(r["luna_unavailable"] for r in rows),
    }
    order = {"ambient": 0, "breath": 1, "dusk": 2, "ember": 3}
    jf = [x for r in rows for x in r["jev_floor"] if x[0] is not None and x[1] is not None]
    above = [x for x in jf if order[x[1]] > order[x[0]]]
    below = [x for x in jf if order[x[1]] < order[x[0]]]
    out["jev_vs_floor"] = {
        "judged_steps": len(jf),
        "at_floor": len(jf) - len(above) - len(below),
        "above_floor": len(above), "below_floor": len(below),
        "by_floor": {f: {"n": sum(1 for x in jf if x[0] == f),
                         "above": sum(1 for x in above if x[0] == f),
                         "below": sum(1 for x in below if x[0] == f)}
                     for f in sorted({x[0] for x in jf}, key=lambda t: order[t])},
    }
    out["h54_verdict"] = (
        "undecidable: luna answered 0 of {n} requests (OpenAI credit_balance_exhausted, HTTP 429)"
        .format(n=out["h54"]["luna_unavailable"]) if not out["h54"]["tier_pairs_total"]
        else "see h54")
    sev = [p for r in rows for p in r["severity_pairs"]]
    out["severity_pairs"] = {"n": len(sev), "agree": sum(1 for a, b in sev if a == b)}
    out["comfort"] = {"proposals": sum(r["comfort_proposals"] for r in rows),
                      "pushes": sum(r["pushes"] for r in rows),
                      "accepted": sum(r["accepted"] for r in rows),
                      "dismissed": sum(r["dismissed"] for r in rows)}
    return out
