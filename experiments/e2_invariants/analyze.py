"""E2 analysis: reads only raw/, deterministic (verify recomputes it)."""

from __future__ import annotations

import collections
from pathlib import Path
from typing import Any

import numpy as np

from experiments.common.io import read_jsonl
from experiments.e1_latency.stats import SEED

B = 1000
ARM_KEYS = ("liot", "final")


def _summarize_fuzz(rows: list[dict[str, Any]]) -> dict[str, Any]:
    out: dict[str, Any] = {"by_behavior": {}, "total_episodes": len(rows)}
    stress_keys = (
        "pushes", "forwards", "guidance_119", "resolved", "exec_attempts", "exec_denied",
        "exec_executed", "exec_opening_attempts", "model_calls", "judgment_rows",
        "answers_below_floor_discarded", "critical_floor_situations", "new_situations",
        "sensor", "advance", "approve", "attack", "consent", "trait", "comfort",
        "escaped_model_exception", "safety_path_exception", "silent_new_situation",
        "guidance_119_without_consenting_guardian", "first_alert_suppressed_known_finding",
    )
    total_viol: collections.Counter[str] = collections.Counter()
    for b in sorted({r["behavior"] for r in rows}):
        rs = [r for r in rows if r["behavior"] == b]
        viol: collections.Counter[str] = collections.Counter()
        for r in rs:
            for v in r["violations"]:
                viol[v["invariant"]] += 1
        # violations are truncated per episode at 20 — keep the exact per-episode totals too
        entry: dict[str, Any] = {
            "episodes": len(rs),
            "episodes_with_violation": sum(1 for r in rs if r["n_violations"] > 0),
            "violations_total": sum(r["n_violations"] for r in rs),
            "violations_by_invariant": dict(sorted(viol.items())),
            "stress": {k: sum(r.get(k, 0) for r in rs) for k in stress_keys},
            "anomaly_kinds": dict(sorted(collections.Counter(
                f"{a['where']}:{a['exc']}" for r in rs for a in r.get("anomalies", [])).items())),
        }
        out["by_behavior"][b] = entry
        total_viol.update(viol)
    out["violations_total"] = sum(e["violations_total"] for e in out["by_behavior"].values())
    out["violations_by_invariant"] = dict(sorted(total_viol.items()))
    out["episodes_with_opening_attempt"] = sum(1 for r in rows if r["exec_opening_attempts"] > 0)
    out["load1"] = _load(rows)
    out["note_structural"] = (
        "answers_below_floor_discarded counts model answers below the floor that the real "
        "composer discarded; a design without the floor rule would have lowered the alert there "
        "(structural by construction, not a comparison)."
    )
    return out


def _load(rows: list[dict[str, Any]]) -> dict[str, float | None]:
    vals = [r["meta"]["load1"] for r in rows if r.get("meta", {}).get("load1") is not None]
    if not vals:
        return {"min": None, "median": None, "max": None}
    return {"min": float(min(vals)), "median": float(np.median(vals)), "max": float(max(vals))}


def _boot_mean_ci(vals: np.ndarray) -> tuple[float, float, float]:
    rng = np.random.default_rng(SEED)
    idx = rng.integers(0, vals.size, size=(B, vals.size))
    m = vals[idx].mean(axis=1)
    return float(vals.mean()), float(np.percentile(m, 2.5)), float(np.percentile(m, 97.5))


def _arm_block(seeds: list[dict[str, Any]], arm: str) -> dict[str, Any]:
    n = np.array([s["n_events"] for s in seeds], dtype=float)
    crit = np.array([s["n_critical"] for s in seeds], dtype=float)
    fp = np.array([s["arms"][arm][0] for s in seeds], dtype=float)
    ms = np.array([s["arms"][arm][1] for s in seeds], dtype=float)
    pg = np.array([s["arms"][arm][2] for s in seeds], dtype=float)
    fr, fr_lo, fr_hi = _boot_mean_ci(fp / n)
    mr, mr_lo, mr_hi = _boot_mean_ci(ms / n)
    return {
        "false_rate": fr, "false_lo": fr_lo, "false_hi": fr_hi,
        "miss_rate": mr, "miss_lo": mr_lo, "miss_hi": mr_hi,
        "page_rate": float((pg / n).mean()),
        "false_per_noncrit": float((fp / (n - crit)).mean()),
        "miss_per_crit": float((ms / np.maximum(crit, 1)).mean()),
    }


def _cstar(seeds: list[dict[str, Any]]) -> dict[str, Any]:
    """c* = extra false pages of 'final' per miss it removes, in event counts (seed bootstrap)."""
    n = np.array([s["n_events"] for s in seeds], dtype=float)
    d_false = np.array([s["arms"]["final"][0] - s["arms"]["liot"][0] for s in seeds]) / n
    d_miss = np.array([s["arms"]["liot"][1] - s["arms"]["final"][1] for s in seeds]) / n

    def classify(df: float, dm: float) -> tuple[str, float | None]:
        if abs(df) < 1e-12 and abs(dm) < 1e-12:
            return "tie", None
        if dm > 0 and df <= 0:
            return "final_dominates", 0.0
        if dm <= 0 and df >= 0:
            return "liot_dominates", None
        if dm > 0 and df > 0:
            return "tradeoff", df / dm
        return "other", None  # dm < 0 and df < 0: final has fewer false pages but more misses

    cls, point = classify(float(d_false.mean()), float(d_miss.mean()))
    rng = np.random.default_rng(SEED)
    idx = rng.integers(0, len(seeds), size=(B, len(seeds)))
    boots = []
    for k in idx:
        c, p = classify(float(d_false[k].mean()), float(d_miss[k].mean()))
        if c == "tradeoff" and p is not None:
            boots.append(p)
    lo = float(np.percentile(boots, 2.5)) if boots else None
    hi = float(np.percentile(boots, 97.5)) if boots else None
    return {
        "class": cls, "point": point, "lo": lo, "hi": hi,
        "tradeoff_share_in_bootstrap": len(boots) / B,
        "d_false_per_event": float(d_false.mean()), "d_miss_per_event": float(d_miss.mean()),
    }


def _summarize_h15(rows: list[dict[str, Any]]) -> dict[str, Any]:
    cells: dict[str, Any] = {}
    infer: dict[str, Any] = {}
    for r in rows:
        seeds = r["seeds"]
        entry = {k: _arm_block(seeds, k) for k in ARM_KEYS}
        entry["c_star"] = _cstar(seeds)
        entry["params"] = {k: r[k] for k in ("fa", "unreg", "honesty", "attack", "blind", "mix")}
        entry["n_seeds"] = r["n_seeds"]
        entry["share_hidden_home"] = float(np.mean([s["share_hidden_home"] for s in seeds]))
        cells[r["key"]] = entry
        # inference arms do not depend on the model: take the (honesty 1, attack 0, blind 0) cell
        if (r["honesty"], r["attack"], r["blind"], r["mix"]) == (1.0, 0.0, 0, "base"):
            tag = f"fa{r['fa']}|u{r['unreg']}"
            infer[tag] = {
                a: _arm_block(seeds, a) for a in seeds[0]["arms"] if a == "liot" or
                a.startswith("infer_")
            }
    classes = collections.Counter(c["c_star"]["class"] for c in cells.values()
                                  if c["params"]["mix"] == "base" and c["params"]["blind"] == 0)
    classes_blind = collections.Counter(c["c_star"]["class"] for c in cells.values()
                                        if c["params"]["mix"] == "base"
                                        and c["params"]["blind"] == 1)
    trade = [c["c_star"]["point"] for c in cells.values()
             if c["c_star"]["class"] == "tradeoff" and c["params"]["mix"] == "base"]
    out: dict[str, Any] = {
        "cells": cells, "infer": infer,
        "class_counts_model_sees_truth": dict(sorted(classes.items())),
        "class_counts_model_blind": dict(sorted(classes_blind.items())),
        "cstar_range_tradeoff_cells": (
            {"min": float(min(trade)), "median": float(np.median(trade)), "max": float(max(trade))}
            if trade else None),
        "assumptions": {
            "registered_vulnerable_home_share": 0.10, "events_per_seed": rows[0]["seeds"][0]["n_events"],
            "seeds": rows[0]["n_seeds"],
            "alarm_mix_base": {"gas": .15, "smoke": .15, "door": .25, "leak": .15, "oven": .10,
                               "high_temp": .05, "lock": .10, "water_main": .05},
            "wrong_answer": "uniform over the three other severity levels",
            "model_honesty_is_assumed": True,
        },
    }
    return out


def summarize(raw_dir: Path) -> dict[str, Any]:
    raw_dir = Path(raw_dir)
    out: dict[str, Any] = {}
    fz = read_jsonl(raw_dir / "fuzz.jsonl")
    if fz:
        out["fuzz"] = _summarize_fuzz(sorted(fz, key=lambda r: (r["behavior"], r["episode"])))
    h = read_jsonl(raw_dir / "h15.jsonl")
    if h:
        out["h15"] = _summarize_h15(sorted(h, key=lambda r: r["key"]))
    v = read_jsonl(raw_dir / "validate.jsonl")
    if v:
        out["validate"] = {
            "n": len(v), "agree": sum(1 for r in v if r["agree"]),
            "forwards_predicted": sum(1 for r in v if r["predicted_forward"]),
            "forwards_actual": sum(1 for r in v if r["actual_forward"]),
            "disagreements": [r["i"] for r in v if not r["agree"]][:10],
        }
    pr = read_jsonl(raw_dir / "probe.jsonl")
    if pr:
        out["probe_dismissal"] = {
            f"{r['category']}/{'registered_vulnerable' if r['registered_vulnerable'] else 'plain'}": {
                "floor": r["floor"], "second_first_alert_pushed": r["second_first_alert_pushed"]}
            for r in sorted(pr, key=lambda r: (r["category"], r["registered_vulnerable"]))
        }
    ll = read_jsonl(raw_dir / "leak_label.jsonl")
    if ll:
        out["inference_leak_door_label"] = {
            ("registered_vulnerable" if r["registered_vulnerable"] else "plain"): {
                "resident_labels": r["resident_labels"], "guardian_labels": r["guardian_labels"]}
            for r in sorted(ll, key=lambda r: r["registered_vulnerable"])
        }
    sc = read_jsonl(raw_dir / "selfcheck.jsonl")
    if sc:
        out["selfcheck"] = {k: sc[0][k] for k in ("fired", "all_fire", "composer_mismatches")}
    return out
