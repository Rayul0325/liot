"""E14 analysis: reads only raw/ (plus the C2.9 raw of E2 for a side-by-side), deterministic.

``python -m experiments verify --only e14`` recomputes ``summarize(raw/)`` and compares it with the
committed ``results/summary.json``.
"""

from __future__ import annotations

import collections
import math
from pathlib import Path
from typing import Any

import numpy as np

from experiments.common.io import read_jsonl, repo_root
from experiments.e1_latency.stats import SEED, boot_ci, lat_summary
from experiments.e7_ladder.analyze import _conf, _ratio_ci

B = 400
RETRY_S = 30.0
EPS = 1e-6
Z95 = 1.959964


def wilson(k: int, n: int) -> dict[str, float | int | None]:
    if n == 0:
        return {"n": 0, "k": 0, "rate": None, "lo": None, "hi": None}
    p = k / n
    den = 1 + Z95 ** 2 / n
    centre = (p + Z95 ** 2 / (2 * n)) / den
    half = Z95 * math.sqrt(p * (1 - p) / n + Z95 ** 2 / (4 * n ** 2)) / den
    return {"n": n, "k": k, "rate": p, "lo": max(0.0, centre - half), "hi": min(1.0, centre + half)}


# ── C4.9 (a) regression of the C2.9 path-quota fuzz ──────────────────────────────────────────
def _regress(raw: Path) -> dict[str, Any]:
    rows = read_jsonl(raw / "regress_quota.jsonl")
    if not rows:
        return {}
    out: dict[str, Any] = {"paths": {}}
    for path in sorted({r["path"] for r in rows}):
        rs = [r for r in rows if r["path"] == path]
        entry: dict[str, Any] = {"episodes": len(rs), "hits": sum(1 for r in rs if r.get("hit")),
                                 "immediate": sum(1 for r in rs if r.get("immediate"))}
        if path == "reject_reopen":
            rej = [r for r in rs if r.get("hit")]
            miss = [r for r in rej if not r.get("immediate")]
            entry.update({
                "rejections": len(rej), "suppressed_first_alert": len(miss),
                "miss_classes": dict(sorted(collections.Counter(
                    r.get("miss_class") for r in miss).items())),
                "suppressed_by_floor": dict(sorted(collections.Counter(
                    str(r["floor"]) for r in miss).items())),
                "suppressed_delay_s": sorted({r.get("first_push_delay_s") for r in miss},
                                             key=lambda x: (x is None, x)),
                "unknown": sum(1 for r in miss if r.get("miss_class") == "UNKNOWN"),
            })
        elif path == "first_send_fail":
            delays = [r["first_push_delay_s"] for r in rs]
            fin = [d for d in delays if d is not None]
            entry.update({
                "no_push_within_horizon": sum(1 for d in delays if d is None),
                "delay_min_s": min(fin) if fin else None, "delay_max_s": max(fin) if fin else None,
                "delay_median_s": float(np.median(fin)) if fin else None,
                "conflict_strict_gt_30s_or_none": sum(
                    1 for d in delays if d is None or d > RETRY_S + EPS),
                "conflict_c29_form_ge_60s_or_none": sum(
                    1 for d in delays if d is None or d >= 60.0 - EPS),
                "delay_values_s": dict(sorted(collections.Counter(
                    "none" if d is None else f"{d:g}" for d in delays).items())),
            })
        elif path == "restart_rearm":
            entry["pass"] = sum(1 for r in rs if r.get("pass"))
        elif path == "charter_off":
            entry["resident_pushes_total"] = sum(r.get("resident_pushes", 0) for r in rs)
            entry["guardian_forwards_total"] = sum(r.get("guardian_forwards", 0) for r in rs)
        elif path == "two_entities":
            entry["both_open"] = sum(1 for r in rs if r.get("hit"))
        elif path == "concurrent":
            entry["all_opened"] = sum(1 for r in rs if r.get("all_situations_opened"))
            entry["duplicate_situations"] = sum(r.get("duplicate_situations", 0) for r in rs)
        out["paths"][path] = entry
    out["unknown_misses_other_paths"] = sum(
        1 for r in rows if r["path"] in ("two_entities", "concurrent", "restart_rearm")
        and r.get("miss_class") == "UNKNOWN")
    orows = read_jsonl(raw / "regress_outage.jsonl")
    outage: dict[str, Any] = {}
    for d in sorted({r["outage_s"] for r in orows}):
        rs = [r for r in orows if r["outage_s"] == d]
        delays = [r["first_push_delay_s"] for r in rs]
        late = [None if x is None else x - d for x in delays]
        fin = [x for x in late if x is not None]
        outage[f"outage{d:g}"] = {
            "episodes": len(rs), "no_push": sum(1 for x in late if x is None),
            "conflict_strict_after_recovery_gt_30s": sum(
                1 for x in late if x is None or x > RETRY_S + EPS),
            "delay_after_recovery_max_s": max(fin) if fin else None,
            "delay_after_recovery_median_s": float(np.median(fin)) if fin else None,
        }
    out["outage"] = outage
    old = repo_root() / "experiments" / "e2_invariants" / "raw" / "quota.jsonl"
    c29 = [r for r in read_jsonl(old) if r.get("path") == "first_send_fail"]
    if c29:
        d29 = [r["first_push_delay_s"] for r in c29]
        fin29 = [d for d in d29 if d is not None]
        out["c29_reference_first_send_fail"] = {
            "episodes": len(c29), "no_push": sum(1 for d in d29 if d is None),
            "delay_min_s": min(fin29) if fin29 else None,
            "delay_median_s": float(np.median(fin29)) if fin29 else None,
            "conflict_c29_form_ge_60s_or_none": sum(
                1 for d in d29 if d is None or d >= 60.0 - EPS),
        }
    c29r = [r for r in read_jsonl(old) if r.get("path") == "reject_reopen" and r.get("hit")]
    if c29r:
        out["c29_reference_reject_reopen"] = {
            "rejections": len(c29r),
            "suppressed_first_alert": sum(1 for r in c29r if not r.get("immediate"))}
    return out


# ── C4.9 (b) hybrid guardian criterion ──────────────────────────────────────────────────────
HYB_WINDOWS = ("hyb90", "hyb180", "hyb300")
PRIMARY = "hyb180"


def _arr(rec: dict[str, Any], arm: str) -> np.ndarray:
    return np.array([s["arms"][arm] for s in rec["seeds"]], dtype=float)


def _evaluate(liot: np.ndarray, final: np.ndarray, hyb: np.ndarray, n: np.ndarray,
              idx: np.ndarray | None = None) -> dict[str, Any]:
    if idx is not None:
        liot, final, hyb, n = liot[idx], final[idx], hyb[idx], n[idx]
    d_miss_final = float(((liot[:, 1] - final[:, 1]) / n).mean())
    d_miss_hyb = float(((liot[:, 1] - hyb[:, 1]) / n).mean())
    inc_final = float(((final[:, 0] - liot[:, 0]) / n).mean())
    inc_hyb = float(((hyb[:, 0] - liot[:, 0]) / n).mean())
    qualifies = d_miss_final >= 0.01
    share = d_miss_hyb / d_miss_final if d_miss_final > 0 else None
    fp_ok = inc_hyb <= 0.5 * max(inc_final, 0.0) + 1e-12
    share_ok = share is not None and share >= 0.5
    return {"qualifies": qualifies, "share": share, "inc_final": inc_final, "inc_hyb": inc_hyb,
            "fp_ratio": (inc_hyb / inc_final) if inc_final > 1e-12 else None,
            "share_ok": share_ok, "fp_ok": fp_ok, "passes": bool(qualifies and share_ok and fp_ok),
            "d_miss_final": d_miss_final, "d_miss_hyb": d_miss_hyb}


def _hybrid(raw: Path) -> dict[str, Any]:
    recs = [r for r in read_jsonl(raw / "hybrid.jsonl") if r["key"] != "selfcheck"]
    sc = [r for r in read_jsonl(raw / "hybrid.jsonl") if r["key"] == "selfcheck"]
    if not recs:
        return {}
    out: dict[str, Any] = {"cells": len(recs), "equivalence_with_h15b": sc[0]["equivalence"] if sc else None}
    n_seeds = recs[0]["n_seeds"]
    rng = np.random.default_rng(SEED)
    idx = rng.integers(0, n_seeds, size=(B, n_seeds))
    levels: dict[str, dict[str, Any]] = {}
    time_stats: dict[str, Any] = {}
    cell_flags: dict[str, Any] = {}
    for win in HYB_WINDOWS:
        per_level: dict[tuple[float, float], list[dict[str, Any]]] = collections.defaultdict(list)
        boot_level: dict[tuple[float, float], list[list[bool | None]]] = collections.defaultdict(list)
        for rec in recs:
            n = np.array([s["n_events"] for s in rec["seeds"]], dtype=float)
            liot, final, hyb = _arr(rec, "liot"), _arr(rec, "final"), _arr(rec, win)
            ev = _evaluate(liot, final, hyb, n)
            key = (rec["vuln_ack"], rec["auc"])
            per_level[key].append(ev)
            flags: list[bool | None] = []
            for k in idx:
                e = _evaluate(liot, final, hyb, n, k)
                flags.append(e["passes"] if e["qualifies"] else None)
            boot_level[key].append(flags)
            if win == PRIMARY:
                cell_flags[rec["key"]] = [int(ev["qualifies"]), ev["share"], ev["fp_ratio"],
                                          int(ev["passes"])]
        lv_out: dict[str, Any] = {}
        for key in sorted(per_level):
            evs = per_level[key]
            qual = [e for e in evs if e["qualifies"]]
            n_pass = sum(1 for e in qual if e["passes"])
            maj = bool(qual) and n_pass * 2 > len(qual)
            # bootstrap over seeds: share of resamples in which the majority holds
            holds = 0
            for b in range(B):
                q = [fl[b] for fl in boot_level[key] if fl[b] is not None]
                if q and sum(1 for x in q if x) * 2 > len(q):
                    holds += 1
            lv_out[f"v{key[0]}|auc{key[1]}"] = {
                "cells": len(evs), "qualifying": len(qual), "passing": n_pass,
                "majority_pass": maj if qual else None,
                "majority_pass_bootstrap_share": holds / B if qual else None,
                "share_median": float(np.median([e["share"] for e in qual])) if qual else None,
                "fp_ratio_median": float(np.median(
                    [e["fp_ratio"] for e in qual if e["fp_ratio"] is not None]))
                if any(e["fp_ratio"] is not None for e in qual) else None,
                "miss_ok": sum(1 for e in qual if e["share_ok"]),
                "fp_ok": sum(1 for e in qual if e["fp_ok"]),
            }
        evaluable = [v for v in lv_out.values() if v["qualifying"] > 0]
        by_vack: dict[str, Any] = {}
        for vack in sorted({k[0] for k in per_level}):
            vs = [v for kk, v in lv_out.items() if kk.startswith(f"v{vack}|") and v["qualifying"]]
            by_vack[f"v{vack}"] = {"levels_evaluable": len(vs),
                                   "levels_majority_pass": sum(1 for v in vs if v["majority_pass"])}
        levels[win] = {
            "levels": lv_out, "levels_evaluable": len(evaluable),
            "levels_majority_pass": sum(1 for v in evaluable if v["majority_pass"]),
            "by_vuln_ack": by_vack,
        }
        # time to family page and miss rates (cells where final cuts misses by >= 1 pp)
        agg: dict[str, list[float]] = collections.defaultdict(list)
        for rec in recs:
            n = np.array([s["n_events"] for s in rec["seeds"]], dtype=float)
            liot, final, hyb = _arr(rec, "liot"), _arr(rec, "final"), _arr(rec, win)
            if not _evaluate(liot, final, hyb, n)["qualifies"]:
                continue
            for name, a in (("liot", liot), ("final", final), (win, hyb)):
                paged = a[:, 3].sum()
                agg[f"{name}_mean_time_paged_true_s"].append(
                    float(a[:, 4].sum() / paged) if paged > 0 else float("nan"))
                crit = sum(s["n_critical"] for s in rec["seeds"])
                agg[f"{name}_share_true_paged_by_300s"].append(float(a[:, 5].sum() / crit))
                agg[f"{name}_miss_rate"].append(float((a[:, 1] / n).mean()))
                agg[f"{name}_false_rate"].append(float((a[:, 0] / n).mean()))
        time_stats[win] = {k: float(np.nanmedian(v)) for k, v in sorted(agg.items())}
        time_stats[win]["qualifying_cells"] = len(agg[f"liot_miss_rate"])
    out["by_window"] = levels
    out["median_over_qualifying_cells"] = time_stats
    out["primary_window"] = PRIMARY
    prim = levels[PRIMARY]
    out["verdict_rule_evaluable_levels"] = prim["levels_evaluable"]
    out["verdict_rule_majority_pass_levels"] = prim["levels_majority_pass"]
    out["all_evaluable_levels_pass"] = bool(
        prim["levels_evaluable"] and prim["levels_evaluable"] == prim["levels_majority_pass"])
    out["cell_flags_primary"] = cell_flags
    out["model_only_raise"] = {
        "events_total": int(sum(sum(s["n_model_only_raise"] for s in r["seeds"]) for r in recs)),
        "events_false": int(sum(sum(s["n_model_only_raise_false"] for s in r["seeds"])
                                for r in recs)),
    }
    return out


# ── C4.9 (c) restart + concurrency fuzz ─────────────────────────────────────────────────────
def _fuzz(raw: Path) -> dict[str, Any]:
    rows = read_jsonl(raw / "restart_fuzz.jsonl")
    if not rows:
        return {}
    out: dict[str, Any] = {"by_mode": {}}
    for mode in sorted({r["mode"] for r in rows}):
        rs = [r for r in rows if r["mode"] == mode]
        viol = [v for r in rs for v in r["violations"]]
        by_inv = collections.Counter(v["invariant"] for v in viol)
        by_detail = collections.Counter(v.get("detail", "") for v in viol if v["invariant"] ==
                                        "safety_never_silent")
        cls: collections.Counter[str] = collections.Counter()
        for r in rs:
            cls.update(r["classes"])
        with_viol = sum(1 for r in rs if r["n_violations"] > 0)
        killed_eps = [r for r in rs if r["kills_midop"] > 0]
        out["by_mode"][mode] = {
            "episodes": len(rs),
            "episodes_with_violation": wilson(with_viol, len(rs)),
            "episodes_with_midop_kill": len(killed_eps),
            "episodes_with_violation_given_midop_kill": wilson(
                sum(1 for r in killed_eps if r["n_violations"] > 0), len(killed_eps)),
            "violations_total_exact": sum(r["n_violations"] for r in rs),
            "violations_recorded": len(viol),
            "violations_by_invariant": dict(sorted(by_inv.items())),
            "gate1_unknown_by_detail": dict(sorted(by_detail.items())),
            "gate1_unknown_with_ladder": sum(1 for v in viol if v.get("has_ladder")),
            "gate1_unknown_in_op_without_kill": sum(
                1 for v in viol if v["invariant"] == "safety_never_silent" and
                v.get("op_killed") is False),
            "gate4_violations": by_inv.get("guardian_gate", 0),
            "gate2_violations": by_inv.get("final_ge_floor", 0),
            "gate3_violations": by_inv.get("no_trait_leak", 0),
            "classes": dict(sorted(cls.items())),
            "stress": {k: sum(r.get(k, 0) for r in rs) for k in (
                "ops", "restarts", "kills_midop", "kills_boundary", "bursts", "new_situations",
                "immediate_ok", "duplicate_family_page", "phase2_cut", "send_failure_ops",
                "redeliveries", "pushes", "forwards", "guidance_119", "resolved", "model_calls")},
            "u5_delay_after_recovery_s": lat_summary(
                [d for r in rs for d in r["u5_delays_s"]]),
            "anomaly_kinds": dict(sorted(collections.Counter(
                f"{a['where']}:{a.get('exc', '')}" for r in rs for a in r["anomalies"]).items())),
            "load1_median": float(np.median([r["meta"]["load1"] for r in rs
                                             if r["meta"].get("load1") is not None]))
            if any(r["meta"].get("load1") is not None for r in rs) else None,
        }
    return out


def _killsweep(raw: Path) -> dict[str, Any]:
    from experiments.e14_guardian_latency import killsweep

    rows = read_jsonl(raw / "killsweep.jsonl")
    if not rows:
        return {}
    wins = killsweep.window(rows)
    sw = [r for r in rows if r.get("repeat_swallowed") is not None]
    return {
        "windows": wins,
        "records": len(rows),
        "repeat_after_restart_swallowed": sum(1 for r in sw if r["repeat_swallowed"]),
        "repeat_after_restart_checked": len(sw),
        "silent_after_700s_total_pushes": sorted({r["pushes_after_700s"] for r in sw}),
        "silent_open_ladders": sorted({r["open_ladders"] for r in sw}),
    }


# ── C4.10 (a) deadline CDF ──────────────────────────────────────────────────────────────────
def _cdf(vals: list[float]) -> dict[str, Any]:
    out: dict[str, Any] = {"n": len(vals)}
    for w in (0.25, 0.5, 1.0):
        k = sum(1 for v in vals if v <= w)
        out[f"le_{w:g}s"] = wilson(k, len(vals))
    if vals:
        out["median_s"] = float(np.median(vals))
        out["p95_s"] = float(np.percentile(vals, 95))
    return out


def _deadline(raw: Path) -> dict[str, Any]:
    rows = read_jsonl(raw / "bench_latency.jsonl")
    if not rows:
        return {}
    out: dict[str, Any] = {"rows": len(rows)}
    out["all"] = _cdf([r["seconds"] for r in rows])
    for q in (1, 2):
        out[f"questions{q}"] = _cdf([r["seconds"] for r in rows if r["questions"] == q])
    out["by_dir_questions2"] = {
        d: _cdf([r["seconds"] for r in rows if r["dir"] == d and r["questions"] == 2])
        for d in sorted({r["dir"] for r in rows})}
    out["by_model_questions2"] = {
        m: _cdf([r["seconds"] for r in rows if r["model"] == m and r["questions"] == 2])
        for m in sorted({r["model"] for r in rows})}
    out["by_group"] = {
        g: _cdf([r["seconds"] for r in rows if r["group"] == g])
        for g in sorted({r["group"] for r in rows})}
    out["situation_raw_excl_omni"] = _cdf([
        r["seconds"] for r in rows if r["dir"] == "raw" and r["group"] == "situation"
        and r["model"] != "jev-omni-q4"])
    out["operational_questions2"] = _cdf([
        r["seconds"] for r in rows if r["dir"] == "raw_operational" and r["questions"] == 2])
    sess = read_jsonl(raw / "bench_sessions.jsonl")
    out["bench_started_at"] = sorted({str(s["started_at"])[:10] for s in sess})
    return out


# ── C4.10 (b) coalesced follow-ups ──────────────────────────────────────────────────────────
def _coalesce(raw: Path) -> dict[str, Any]:
    rows: list[dict[str, Any]] = []
    for f in sorted(raw.glob("coalesce_shard*.jsonl")):
        rows += read_jsonl(f)
    if not rows:
        return {}
    out: dict[str, Any] = {"records": len(rows), "cells": {}}
    liot_first: dict[tuple[Any, ...], float] = {}
    for r in rows:
        if r["variant"] == "liot":
            for cat in r["categories"]:
                ts = [p["t_s"] for p in r["pushes"] if p["category"] == cat]
                if ts:
                    liot_first[(r["k"], r["raise_p"], r["inc"], cat)] = min(ts)
    for k in sorted({r["k"] for r in rows}):
        for rp in sorted({r["raise_p"] for r in rows}):
            for v in coalesce_variants(rows):
                rs = [r for r in rows if r["k"] == k and r["raise_p"] == rp and r["variant"] == v]
                if not rs:
                    continue
                pushes = [r["n_pushes"] for r in rs]
                pt, lo, hi = boot_ci(pushes, np.mean, b=1000)  # type: ignore[arg-type]
                lat: list[float] = []
                diffs: list[float] = []
                for r in rs:
                    for cat in r["categories"]:
                        ts = [p["t_s"] for p in r["pushes"] if p["category"] == cat]
                        if ts:
                            lat.append(min(ts))
                            base = liot_first.get((k, rp, r["inc"], cat))
                            if base is not None:
                                diffs.append(min(ts) - base)
                holds = [h["held_s"] for r in rs for h in r["hold_log"]]
                merged = [h["merged"] for r in rs for h in r["hold_log"]]
                dpt = boot_ci(diffs, np.median, b=1000) if diffs else (None, None, None)
                out["cells"][f"k{k}|r{rp}|{v}"] = {
                    "incidents": len(rs),
                    "pushes_per_incident": pt, "pushes_lo": lo, "pushes_hi": hi,
                    "followup_pushes_per_incident": pt - k if pt is not None else None,
                    "target_le_1_05": bool(hi is not None and hi <= 1.05) if k == 1 else None,
                    "first_alert_latency_s": lat_summary(lat),
                    "first_alert_diff_vs_liot_median_s": {"point": dpt[0], "lo": dpt[1], "hi": dpt[2],
                                                          "n": len(diffs)},
                    "hold_mean_s": float(np.mean(holds)) if holds else None,
                    "merged_per_flush_mean": float(np.mean(merged)) if merged else None,
                    "flushes": len(holds),
                    "load1_median": float(np.median([r["meta"]["load1"] for r in rs
                                                     if r["meta"].get("load1") is not None]))
                    if any(r["meta"].get("load1") is not None for r in rs) else None,
                }
    return out


def coalesce_variants(rows: list[dict[str, Any]]) -> list[str]:
    order = ["liot", "situation_1.0", "incident_0.5", "incident_1.0", "incident_2.0"]
    present = {r["variant"] for r in rows}
    return [v for v in order if v in present]


# ── C4.10 (c) 1 s tick ──────────────────────────────────────────────────────────────────────
def _tick(raw: Path) -> dict[str, Any]:
    rows = read_jsonl(raw / "tick.jsonl")
    if not rows:
        return {}
    conf = _conf(rows)
    return {
        "by_driver": conf["by_driver"],
        "nonconformant": {k: v["missing"] or v["extra"] or v["max_abs_error_s"]
                          for k, v in conf["by_scenario"].items() if not v["conformant"]
                          and k.split("/")[1].startswith("tick1")},
        "ladder_reads_per_hour": {"tick5": 3600 / 5, "tick1": 3600 / 1},
    }


# ── C4.10 (c) sequential paging waits ───────────────────────────────────────────────────────
def _seq(raw: Path) -> dict[str, Any]:
    rows = read_jsonl(raw / "seq.jsonl")
    if not rows:
        return {}
    cells: dict[str, Any] = {}
    in_ci = {"e_t_ack": [0, 0], "pages": [0, 0]}
    worst = {"e_t_ack": 0.0, "pages": 0.0}
    waits = ("w0", "w30", "w60", "w90")
    for r in rows:
        bs = r["batches"]
        trials = np.array([b["trials"] for b in bs], dtype=float)
        entry: dict[str, Any] = {"mu_min": r["mu_min"], "q": r["q"], "b": r["b"],
                                 "n_guard": r["n_guard"]}
        for reading_name in ("N", "concurrent"):
            blk: dict[str, Any] = {}
            for w in waits:
                st = [b[reading_name][w] for b in bs]
                n_ack = np.array([s["n_ack"] for s in st], dtype=float)
                s_t = np.array([s["sum_t_ack"] for s in st], dtype=float)
                n300 = np.array([s["n_ack_300"] for s in st], dtype=float)
                pages = np.array([s["sum_pages"] for s in st], dtype=float)
                n_both = np.array([s["n_both"] for s in st], dtype=float)
                s_diff = np.array([s["sum_diff_vs_sim"] for s in st], dtype=float)
                e_t = _ratio_ci(s_t, n_ack)
                pg = _ratio_ci(pages, trials)
                p3 = _ratio_ci(n300, trials)
                dd = _ratio_ci(s_diff, n_both)
                blk[w] = {
                    "e_t_ack_s": e_t[0], "e_t_lo": e_t[1], "e_t_hi": e_t[2],
                    "pages": pg[0], "pages_lo": pg[1], "pages_hi": pg[2],
                    "p_ack_300": p3[0], "p_ack_300_lo": p3[1], "p_ack_300_hi": p3[2],
                    "time_loss_vs_sim_s": dd[0], "loss_lo": dd[1], "loss_hi": dd[2],
                }
                if reading_name == "N" and w == "w90":
                    cc = r["closed_check"]
                    for metric, val, lo, hi, closed in (
                        ("e_t_ack", e_t[0], e_t[1], e_t[2], cc["seq90_e_t_ack"]),
                        ("pages", pg[0], pg[1], pg[2], cc["seq90_pages"]),
                    ):
                        if closed == closed:
                            in_ci[metric][1] += 1
                            in_ci[metric][0] += int(lo <= closed <= hi)
                            worst[metric] = max(worst[metric],
                                                abs(val - closed) / max(abs(closed), 1e-9))
            entry[reading_name] = blk
        cells[f"mu{r['mu_min']}|q{r['q']}|b{r['b']}|N{r['n_guard']}"] = entry
    agg: dict[str, Any] = {}
    for reading_name in ("N", "concurrent"):
        for w in waits[1:]:
            loss = [c[reading_name][w]["time_loss_vs_sim_s"] for c in cells.values()]
            pg = [c[reading_name][w]["pages"] for c in cells.values()]
            n = [c["n_guard"] for c in cells.values()]
            saved = [1 - p / m for p, m in zip(pg, n, strict=True)]
            sep = sum(1 for c in cells.values()
                      if c[reading_name][w]["loss_lo"] > 0 or c[reading_name][w]["loss_hi"] < 0)
            p300 = [c[reading_name][w]["p_ack_300"] - c[reading_name]["w0"]["p_ack_300"]
                    for c in cells.values()]
            agg[f"{reading_name}|{w}"] = {
                "cells": len(cells),
                "time_loss_median_s": float(np.median(loss)),
                "time_loss_q25_s": float(np.percentile(loss, 25)),
                "time_loss_q75_s": float(np.percentile(loss, 75)),
                "pages_saved_fraction_median": float(np.median(saved)),
                "p_ack_300_diff_median": float(np.median(p300)),
                "cells_ci_excludes_zero": sep,
                "cells_seq_faster": sum(1 for x in loss if x < 0),
            }
    return {"cells": cells, "aggregate": agg,
            "closed_form_w90_in_ci": {k: {"inside": v[0], "of": v[1]} for k, v in in_ci.items()},
            "closed_form_w90_worst_rel_err": worst}


def summarize(raw_dir: Path) -> dict[str, Any]:
    raw = Path(raw_dir)
    return {
        "regress": _regress(raw), "hybrid": _hybrid(raw), "fuzz": _fuzz(raw),
        "killsweep": _killsweep(raw), "deadline": _deadline(raw), "coalesce": _coalesce(raw),
        "tick": _tick(raw), "seq": _seq(raw),
    }


# ── figures ──────────────────────────────────────────────────────────────────────────────────
def make_figures(summary: dict[str, Any], exp_dir: Path, plotstyle: Any) -> list[Path]:
    import matplotlib.pyplot as plt

    plotstyle.setup()
    out: list[Path] = []
    hy = summary.get("hybrid")
    if hy:
        lv = hy["by_window"][PRIMARY]["levels"]
        fig, ax = plt.subplots(figsize=(7, 4))
        aucs = sorted({float(k.split("auc")[1]) for k in lv})
        for vack, color in ((0.2, plotstyle.ACCENT), (0.5, plotstyle.GREY_DARK),
                            (0.8, plotstyle.GREY)):
            xs, ys = [], []
            for a in aucs:
                e = lv.get(f"v{vack}|auc{a}")
                if e and e["qualifying"]:
                    xs.append(a)
                    ys.append(e["passing"] / e["qualifying"])
            ax.plot(xs, ys, marker="o", color=color, label=f"취약 가구 응답 확률 {vack}")
        ax.axhline(0.5, color=plotstyle.GREY, linestyle=":")
        ax.set_xlabel("모델 신호 세기 AUC")
        ax.set_ylabel("혼합 기준 예측을 만족한 칸의 몫")
        ax.set_ylim(0, 1.05)
        ax.legend(fontsize=7)
        ax.set_title("C4.9 (b) 혼합 보호자 기준(게이트 4 를 깨는 탐색 변형, 창 180초)", fontsize=9)
        out.append(plotstyle.save(fig, exp_dir, "c49b_hybrid_pass_share"))
    dl = summary.get("deadline")
    if dl:
        rows = read_jsonl(exp_dir / "raw" / "bench_latency.jsonl")
        fig, ax = plt.subplots(figsize=(7, 4))
        for q, color, label in ((1, plotstyle.GREY, "질문 1개(편의형)"),
                                (2, plotstyle.ACCENT, "질문 2개(안전 상황)")):
            v = np.sort([r["seconds"] for r in rows if r["questions"] == q])
            ax.step(v, np.arange(1, len(v) + 1) / len(v), color=color, label=label)
        for w in (0.25, 0.5, 1.0):
            ax.axvline(w, color=plotstyle.GREY_LIGHT, linestyle="--")
        ax.set_xlim(0, 2.0)
        ax.set_xlabel("모델 답이 오는 데 걸린 시간(초, 벤치 원자료)")
        ax.set_ylabel("그 시간 안에 온 답의 누적 비율")
        ax.legend(fontsize=8)
        ax.set_title("C4.10 (a) 짧은 마감 대기가 잡는 모델 답의 비율(기술)", fontsize=9)
        out.append(plotstyle.save(fig, exp_dir, "c410a_latency_cdf"))
    sq = summary.get("seq")
    if sq:
        fig, axes = plt.subplots(1, 2, figsize=(11, 4), sharey=True)
        cols = {"w30": plotstyle.ACCENT, "w60": plotstyle.GREY_DARK, "w90": plotstyle.GREY}
        for ax, rd in zip(axes, ("N", "concurrent"), strict=True):
            for w, color in cols.items():
                xs = [c[rd][w]["pages"] for c in sq["cells"].values() if c["n_guard"] == 3]
                ys = [c[rd][w]["time_loss_vs_sim_s"] for c in sq["cells"].values()
                      if c["n_guard"] == 3]
                ax.scatter(xs, ys, s=12, color=color, label=f"순차 대기 {w[1:]}초")
            ax.axhline(0, color=plotstyle.GREY_LIGHT)
            ax.set_xlabel("사건당 호출 인원(보호자 3명, 동시 호출은 3.0)")
            ax.set_title("방관 효과: " + ("보호자 수 N" if rd == "N" else "동시 호출 수"), fontsize=9)
        axes[0].set_ylabel("첫 확인까지 시간 손실(순차 − 동시, 초)")
        axes[0].legend(fontsize=7)
        out.append(plotstyle.save(fig, exp_dir, "c410c_seq_time_loss_vs_pages"))
    return out
