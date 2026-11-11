"""E1 analysis: reads only raw/, deterministic (verify recomputes it)."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any

import numpy as np

from experiments.common.io import read_jsonl
from experiments.e1_latency.stats import B_DEFAULT, SEED, boot_prop, lat_summary, p95

ARMS = ("liot", "wait_for_model", "short_0.25", "short_0.5", "short_1.0")
CONDS = ("normal", "slow", "raising", "timeout")


def load_rows(raw_dir: Path, prefix: str) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for f in sorted(Path(raw_dir).glob(f"{prefix}*.jsonl")):
        rows.extend(read_jsonl(f))
    return sorted(
        rows,
        key=lambda r: (
            r.get("incident", 0), r.get("arm", ""), r.get("cond", ""), r.get("model", ""),
            str(r.get("id", "")), r.get("rep", 0), r.get("seconds", 0), r.get("ofrac", 0),
            str(r.get("scenario", "")), r.get("delay_s", 0), r.get("guardians", 0),
        ),
    )


def _paired_diff(
    a: dict[int, float], b: dict[int, float], stat: Callable[[np.ndarray], float]
) -> dict[str, float | int | None]:
    """stat(b) - stat(a) over incidents present in both; paired bootstrap 95% CI."""
    ids = sorted(set(a) & set(b))
    if not ids:
        return {"n": 0}
    va = np.array([a[i] for i in ids])
    vb = np.array([b[i] for i in ids])
    rng = np.random.default_rng(SEED)
    idx = rng.integers(0, len(ids), size=(B_DEFAULT, len(ids)))
    diffs = np.array([stat(vb[k]) - stat(va[k]) for k in idx])
    return {
        "n": len(ids),
        "diff": float(stat(vb) - stat(va)),
        "lo": float(np.percentile(diffs, 2.5)),
        "hi": float(np.percentile(diffs, 97.5)),
    }


def _load(rows: list[dict[str, Any]]) -> dict[str, float | None]:
    vals = [r["meta"]["load1"] for r in rows if r.get("meta", {}).get("load1") is not None]
    if not vals:
        return {"min": None, "median": None, "max": None}
    return {"min": float(min(vals)), "median": float(np.median(vals)), "max": float(max(vals))}


def _summarize_main(rows: list[dict[str, Any]]) -> dict[str, Any]:
    out: dict[str, Any] = {"cells": {}, "delta_vs_liot": {}, "stage2_change": {}}
    by: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for r in rows:
        by.setdefault((r["arm"], r["cond"]), []).append(r)
    for arm in ARMS:
        for cond in CONDS:
            rs = by.get((arm, cond), [])
            if not rs:
                continue
            lat = lat_summary([r["latency_s"] for r in rs])
            lat["missed_first_alert"] = sum(1 for r in rs if r["latency_s"] is None)
            lat["pushes_mean"] = float(np.mean([r["pushes"] for r in rs]))
            lat["pushes_max"] = int(max(r["pushes"] for r in rs))
            lat["share_two_or_more_pushes"] = boot_prop([r["pushes"] >= 2 for r in rs])
            out["cells"][f"{arm}/{cond}"] = lat
    # H1.1: delta of median and p95 vs the production arm, paired by incident
    for arm in ARMS[1:]:
        for cond in CONDS:
            a = {r["incident"]: r["latency_s"] for r in by.get(("liot", cond), [])
                 if r["latency_s"] is not None}
            b = {r["incident"]: r["latency_s"] for r in by.get((arm, cond), [])
                 if r["latency_s"] is not None}
            out["delta_vs_liot"][f"{arm}/{cond}"] = {
                "median": _paired_diff(a, b, np.median),
                "p95": _paired_diff(a, b, p95),
            }
    # representative short-deadline window: lowest p95 averaged over the four conditions
    shorts = [a for a in ARMS if a.startswith("short_")]
    avg_p95 = {}
    for arm in shorts:
        vals = [out["cells"][f"{arm}/{c}"]["p95"] for c in CONDS if f"{arm}/{c}" in out["cells"]]
        if vals:
            avg_p95[arm] = float(np.mean(vals))
    out["short_window_mean_p95"] = avg_p95
    out["short_window_representative"] = min(avg_p95, key=lambda k: avg_p95[k]) if avg_p95 else None
    # stage-2 disagreement: the first pushed severity differs from the final one
    for arm in ARMS:
        for cond in ("normal", "slow"):
            rs = by.get((arm, cond), [])
            if not rs:
                continue
            out["stage2_change"][f"{arm}/{cond}"] = {
                "all": boot_prop([r["changed"] for r in rs]),
                "floor_warning": boot_prop([r["changed"] for r in rs if r["floor"] == "WARNING"]),
                "floor_critical": boot_prop([r["changed"] for r in rs if r["floor"] == "CRITICAL"]),
                "model_would_raise_share": boot_prop([r["would_raise"] for r in rs]),
                "extra_pushes_per_incident": float(np.mean([r["pushes"] for r in rs]) - 1.0),
            }
    out["n_incidents"] = len({r["incident"] for r in rows})
    out["assumed_p_raise"] = 0.3
    out["load1"] = _load(rows)
    return out


def _summarize_replay(rows: list[dict[str, Any]]) -> dict[str, Any]:
    order = {"기록만": 0, "주의": 1, "경고": 2, "위험": 3}
    sev_rank = {"record": 0, "notice": 1, "warning": 2, "critical": 3}
    floor_rank = {"WARNING": 2, "CRITICAL": 3}
    out: dict[str, Any] = {"by_model": {}, "pooled": {}}
    liot = [r for r in rows if r["arm"] == "liot"]
    wfm = [r for r in rows if r["arm"] == "wait_for_model"]

    def block(rs: list[dict[str, Any]], rw: list[dict[str, Any]]) -> dict[str, Any]:
        answered = [r for r in rs if not r["degraded"] and r["model_severity"] is not None]
        raised = [sev_rank[r["model_severity"]] > floor_rank[r["table_floor"]] for r in answered]
        lowered = [sev_rank[r["model_severity"]] < floor_rank[r["table_floor"]] for r in answered]
        return {
            "n_cases": len(rs),
            "degraded_share": float(np.mean([r["degraded"] for r in rs])) if rs else None,
            "mismatch": boot_prop([r["changed"] for r in rs]),
            "pushes_mean_liot": float(np.mean([r["pushes"] for r in rs])) if rs else None,
            "pushes_mean_wait_for_model": (
                float(np.mean([r["pushes"] for r in rw])) if rw else None),
            "model_raised_share": boot_prop(raised),
            "model_lowered_share": boot_prop(lowered),
            "final_label_counts": {
                lab: sum(1 for r in rs if r["final_sev"] == lab) for lab in order
            },
        }

    for m in sorted({r["model"] for r in rows}):
        out["by_model"][m] = block(
            [r for r in liot if r["model"] == m], [r for r in wfm if r["model"] == m]
        )
    out["pooled"] = block(liot, wfm)
    out["note"] = "descriptive; bench situation cases (16 x 2 reps x model); no model is ground truth"
    return out


def _summarize_h12(rows: list[dict[str, Any]]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for tag in sorted({r["tag"] for r in rows}):
        per_t: dict[str, Any] = {}
        for t in sorted({r["seconds"] for r in rows if r["tag"] == tag}):
            rs = [r for r in rows if r["tag"] == tag and r["seconds"] == t]
            ctrl = [r["latency_s"] for r in rs if r["ofrac"] < 0 and r["latency_s"] is not None]
            by_o: dict[str, Any] = {}
            resid: list[float] = []
            faster = 0
            judged_cells = 0
            allv: list[float] = []
            for f in sorted({r["ofrac"] for r in rs if r["ofrac"] >= 0}):
                cell = [r for r in rs if r["ofrac"] == f and r["latency_s"] is not None]
                if not cell:
                    continue
                o = float(cell[0]["o_s"])
                lat = lat_summary([r["latency_s"] for r in cell])
                lat["o_s"] = o
                lat["predicted_T_minus_o"] = t - o
                lat["judged_share"] = float(np.mean([bool(r["judged"]) for r in cell]))
                lat["share_faster_than_pred_minus_50ms"] = float(
                    np.mean([r["latency_s"] < (t - o) - 0.05 for r in cell])
                )
                if ctrl:
                    lat["p95_minus_control_p95"] = float(lat["p95"] - p95(np.array(ctrl)))
                by_o[f"{f}"] = lat
                resid += [r["latency_s"] - (t - o) for r in cell]
                faster += sum(r["latency_s"] < (t - o) - 0.05 for r in cell)
                judged_cells += len(cell)
                allv += [r["latency_s"] for r in cell]
            pooled = {
                "n": judged_cells,
                "share_faster_than_pred_minus_50ms": faster / judged_cells if judged_cells else None,
                "median_residual_vs_T_minus_o": float(np.median(resid)) if resid else None,
                "p95_all_o": float(p95(np.array(allv))) if allv else None,
            }
            if ctrl and allv:
                g, c = np.array(allv), np.array(ctrl)
                pooled["p95_minus_control_p95"] = float(p95(g) - p95(c))
                # bootstrap CI of the difference (gas and control resampled independently)
                rng = np.random.default_rng(SEED)
                ig = rng.integers(0, g.size, size=(B_DEFAULT, g.size))
                ic = rng.integers(0, c.size, size=(B_DEFAULT, c.size))
                diffs = np.array([p95(g[a]) - p95(c[b]) for a, b in zip(ig, ic, strict=True)])
                pooled["p95_diff_lo"] = float(np.percentile(diffs, 2.5))
                pooled["p95_diff_hi"] = float(np.percentile(diffs, 97.5))
                pooled["after_criterion_met_p95_diff_le_0.1s"] = bool(
                    float(p95(g) - p95(c)) <= 0.1)
            per_t[f"{t}"] = {
                "control": lat_summary(ctrl), "by_o": by_o, "pooled": pooled,
                "git_shas": sorted({str(r["meta"]["git_sha"]) for r in rs}),
                "load1": _load(rs),
            }
        out[tag] = per_t
    return out


def _summarize_h13(rows: list[dict[str, Any]]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    keys = sorted({(r["scenario"], r["delay_s"], r["guardians"]) for r in rows})
    for sc, d, g in keys:
        rs = [r for r in rows if (r["scenario"], r["delay_s"], r["guardians"]) == (sc, d, g)]
        lat = lat_summary([r["latency_s"] for r in rs])
        lat.update({
            "scenario": sc, "delay_s": d, "guardians": g,
            "missing": sum(1 for r in rs if r["latency_s"] is None),
            "sends_before_smoke_median": float(np.median([r["sends_before_smoke_done"] for r in rs])),
            "ratio_median_over_delay": (
                None if not d or lat.get("median") is None else lat["median"] / d),
        })
        out[f"{sc}/{d}/{g}"] = lat
    return out


def summarize(raw_dir: Path) -> dict[str, Any]:
    raw_dir = Path(raw_dir)
    out: dict[str, Any] = {}
    main_rows = load_rows(raw_dir, "main_")
    if main_rows:
        out["main"] = _summarize_main(main_rows)
    replay_rows = load_rows(raw_dir, "replay")
    if replay_rows:
        out["replay"] = _summarize_replay(replay_rows)
    h12_rows = load_rows(raw_dir, "h12_")
    if h12_rows:
        out["h12"] = _summarize_h12(h12_rows)
    h13_rows = load_rows(raw_dir, "h13_")
    if h13_rows:
        out["h13"] = _summarize_h13(h13_rows)
    return out
