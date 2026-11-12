"""E13 analysis: reads only raw/runs.jsonl, deterministic (verify recomputes it).

Definitions are the ones frozen in PREREG.md (committed before the full run). Units are (persona, seed)
pairs; every interval is a 95% percentile bootstrap (2000 resamples, fixed generator) over those units
and paired contrasts resample the pairs jointly.
"""

from __future__ import annotations

from collections import defaultdict
from pathlib import Path
from typing import Any

import numpy as np

from experiments.common.io import read_jsonl
from experiments.e6_personalization.analyze import _accept_rate, _arr, _series, _window_mean

BOOT = 2000
BOOT_SEED = 20261112
ROLL = 7
FRACTION = 0.8
FLIPS = (4, 8, 15, 28, 35)
RESTS = (7, 14, 30)
PERSONAS = ("hater", "ignorer", "swap")
DISMISSERS = ("hater", "swap")  # the personas that press [그만해]
DAYS = 60
USELESS_BOUND_46 = 0.05
USELESS_BOUND_47 = 0.02
SUPPORT_RATIO = 0.90
REFUTE_RATIO = 0.80
WINDOWS = {"d1_14": (1, 14), "d15_30": (15, 30), "d31_60": (31, 60), "d1_60": (1, 60)}


def _load(raw_dir: Path) -> list[dict[str, Any]]:
    rows = read_jsonl(Path(raw_dir) / "runs.jsonl")
    return sorted(rows, key=lambda r: (r["part"], r["variant"], r["persona"], r["arm"], r["cond"],
                                       r["seed"]))


# ── bootstrap helpers ─────────────────────────────────────────────────────────────────────────────
def _ci(rng: np.random.Generator, x: np.ndarray) -> dict[str, Any]:
    n = len(x)
    if n == 0:
        return {"n": 0, "mean": None, "lo": None, "hi": None}
    idx = rng.integers(0, n, size=(BOOT, n))
    b = x[idx].mean(axis=1)
    return {"n": n, "mean": float(x.mean()), "lo": float(np.percentile(b, 2.5)),
            "hi": float(np.percentile(b, 97.5))}


def _ratio(rng: np.random.Generator, num: np.ndarray, den: np.ndarray) -> dict[str, Any]:
    n = len(num)
    if n == 0 or den.sum() <= 0:
        return {"n": n, "ratio": None, "lo": None, "hi": None}
    idx = rng.integers(0, n, size=(BOOT, n))
    d = den[idx].sum(axis=1)
    ok = d > 0
    r = num[idx].sum(axis=1)[ok] / d[ok]
    return {"n": n, "ratio": float(num.sum() / den.sum()), "lo": float(np.percentile(r, 2.5)),
            "hi": float(np.percentile(r, 97.5))}


def _unit(r: dict[str, Any]) -> tuple[str, int]:
    return (r["persona"], r["seed"])


def _aligned(a: list[dict[str, Any]], b: list[dict[str, Any]]) -> list[tuple[dict[str, Any], dict[str, Any]]]:
    bm = {_unit(r): r for r in b}
    return [(r, bm[_unit(r)]) for r in a if _unit(r) in bm]


def _vals(rows: list[dict[str, Any]], metric: str, lo: int, hi: int) -> np.ndarray:
    return np.array([_window_mean(_series(r, metric), lo, hi) for r in rows], dtype=float)


def _paired_diff(rng: np.random.Generator, a: list[dict[str, Any]], b: list[dict[str, Any]],
                 metric: str, lo: int, hi: int) -> dict[str, Any]:
    pairs = _aligned(a, b)
    d = np.array([_window_mean(_series(x, metric), lo, hi) - _window_mean(_series(y, metric), lo, hi)
                  for x, y in pairs], dtype=float)
    return _ci(rng, d)


def _paired_ratio(rng: np.random.Generator, a: list[dict[str, Any]], b: list[dict[str, Any]],
                  metric: str, lo: int, hi: int) -> dict[str, Any]:
    pairs = _aligned(a, b)
    num = np.array([_window_mean(_series(x, metric), lo, hi) for x, _ in pairs], dtype=float)
    den = np.array([_window_mean(_series(y, metric), lo, hi) for _, y in pairs], dtype=float)
    return _ratio(rng, num, den)


# ── C4.6 ──────────────────────────────────────────────────────────────────────────────────────────
def _first_reaching(x: np.ndarray, threshold: float, flip: int) -> int | None:
    for t in range(flip + ROLL - 1, len(x) + 1):
        if float(x[t - ROLL:t].mean()) >= threshold:
            return t - flip
    return None


def _re_accept(r: dict[str, Any], flip: int) -> int | None:
    hit = np.nonzero(_arr(r, "new_acc")[flip - 1:] > 0)[0]
    return int(hit[0]) if hit.size else None


def _timing(rng: np.random.Generator, vals: list[int | None], horizon: int) -> dict[str, Any]:
    hit = np.array([v is not None for v in vals], dtype=float)
    done = [v for v in vals if v is not None]
    cens = [v if v is not None else horizon + 1 for v in vals]
    out = _ci(rng, hit)
    out["fraction"] = out.pop("mean")
    out["median_days_among_hit"] = None if not done else float(np.median(done))
    med_c = float(np.median(cens)) if cens else None
    out["median_days_censored_as_horizon_plus_1"] = med_c
    out["median_is_beyond_horizon"] = bool(med_c is not None and med_c > horizon)
    out["horizon_days"] = horizon
    out["n"] = len(vals)
    return out


def _c46_cell(rng: np.random.Generator, rows: list[dict[str, Any]], flip: int,
              oracle: dict[str, float]) -> dict[str, Any]:
    horizon = DAYS - flip
    cell: dict[str, Any] = {"n_runs": len(rows)}
    re = [_re_accept(r, flip) for r in rows]
    cell["re_accept"] = _timing(rng, re, horizon)
    lit: list[int | None] = []
    ora: list[int | None] = []
    pre_levels = []
    for r in rows:
        total = _series(r, "acc_help")
        lo = max(1, flip - 7)
        pre = float(total[lo - 1:flip - 1].mean())
        pre_levels.append(pre)
        lit.append(_first_reaching(total, FRACTION * pre, flip) if pre > 0 else None)
        lv = oracle.get(r["persona"], 0.0)
        ora.append(_first_reaching(_arr(r, "new_acc"), FRACTION * lv, flip) if lv > 0 else None)
    cell["recovery_literal"] = _timing(rng, lit, horizon)
    cell["recovery_oracle"] = _timing(rng, ora, horizon)
    cell["pre_level"] = _ci(rng, np.array(pre_levels))
    cell["re_offer"] = _timing(rng, [_first(r, "new_offer_imm", flip) for r in rows], horizon)
    for m in ("offered_unhelp_imm", "pushes_imm", "acc_help", "acc_help_imm", "ws_help_phone"):
        cell[m] = {w: _ci(rng, _vals(rows, m, *WINDOWS[w])) for w in ("d1_60", "d31_60")}
    return cell


def _first(r: dict[str, Any], key: str, flip: int) -> int | None:
    hit = np.nonzero(_arr(r, key)[flip - 1:] > 0)[0]
    return int(hit[0]) if hit.size else None


def _verdict_46(ci: dict[str, Any]) -> str:
    if ci["lo"] is None:
        return "판정 불가"
    if ci["hi"] <= USELESS_BOUND_46:
        return "지지"
    if ci["lo"] > USELESS_BOUND_46:
        return "반증"
    return "판정 불가"


def _c46(rng: np.random.Generator, rows: list[dict[str, Any]]) -> dict[str, Any]:
    rows = [r for r in rows if r["part"] == "c46"]
    by: dict[tuple[str, str, str], list[dict[str, Any]]] = defaultdict(list)  # (persona, arm)
    for r in rows:
        by[(r["persona"], r["arm"])].append(r)
    out: dict[str, Any] = {"cells": {}, "contrast": {}}
    groups = {"all": PERSONAS, "dismissers": DISMISSERS, **{p: (p,) for p in PERSONAS}}
    for flip in FLIPS:
        oracle = {}
        for p in PERSONAS:
            none = by.get((p, f"none_flip{flip}"), [])
            if none:
                oracle[p] = float(np.mean([_arr(r, "new_acc")[flip + ROLL - 1:].mean() for r in none]))
        for rest in RESTS:
            arm = f"rest{rest}_flip{flip}"
            for g, personas in groups.items():
                sel = [r for p in personas for r in by.get((p, arm), [])]
                if sel:
                    out["cells"][f"{g}|flip{flip}|rest{rest}"] = _c46_cell(rng, sel, flip, oracle)
        for g, personas in groups.items():
            ref = [r for p in personas for r in by.get((p, f"rest30_flip{flip}"), [])]
            for rest in (7, 14):
                sel = [r for p in personas for r in by.get((p, f"rest{rest}_flip{flip}"), [])]
                if not sel or not ref:
                    continue
                c: dict[str, Any] = {}
                for m in ("offered_unhelp_imm", "pushes_imm", "acc_help"):
                    c[m] = {w: _paired_diff(rng, sel, ref, m, *WINDOWS[w]) for w in ("d1_60", "d31_60")}
                c["verdict_useless_alerts"] = _verdict_46(c["offered_unhelp_imm"]["d1_60"])
                ra_a = [_re_accept(r, flip) for r in sel]
                ra_b = [_re_accept(r, flip) for r in ref]
                med = lambda v: float(np.median([x for x in v if x is not None])) if any(  # noqa: E731
                    x is not None for x in v) else None
                ma, mb = med(ra_a), med(ra_b)
                c["re_accept_median_days_reduction_vs_rest30"] = (
                    None if ma is None or mb is None else mb - ma)  # 구조상 결과 (not a test)
                out["contrast"][f"{g}|flip{flip}|rest{rest}-rest30"] = c
    return out


# ── C4.7 ──────────────────────────────────────────────────────────────────────────────────────────
RULE_ARMS = ("none", "explicit_only", "lr30", "r1", "r2", "r3", "r123")


def _verdict_47(ratio: dict[str, Any], useless: dict[str, Any]) -> str:
    if ratio["lo"] is None or useless["lo"] is None:
        return "판정 불가"
    if ratio["lo"] >= SUPPORT_RATIO and useless["hi"] <= USELESS_BOUND_47:
        return "지지"
    if ratio["hi"] < REFUTE_RATIO or useless["lo"] > USELESS_BOUND_47:
        return "반증"
    return "판정 불가"


def _c47(rng: np.random.Generator, rows: list[dict[str, Any]]) -> dict[str, Any]:
    rows = [r for r in rows if r["part"] == "c47"]
    by: dict[tuple[str, str, str], list[dict[str, Any]]] = defaultdict(list)  # variant, persona, arm
    for r in rows:
        by[(r["variant"], r["persona"], r["arm"])].append(r)
    out: dict[str, Any] = {"arms": {}, "vs_none": {}, "vs_lr30": {}, "rule_stats": {}, "verdict_1": {}}
    for variant in ("main", "tray0"):
        for g, personas in {"all": PERSONAS, **{p: (p,) for p in PERSONAS}}.items():
            get = lambda arm: [r for p in personas for r in by.get((variant, p, arm), [])]  # noqa: E731
            for arm in RULE_ARMS:
                sel = get(arm)
                if not sel:
                    continue
                cell: dict[str, Any] = {"n_runs": len(sel)}
                for m in ("acc_help_imm", "acc_help", "offered_unhelp_imm", "pushes_imm",
                          "ws_help_phone", "digest_made", "exp_dig"):
                    cell[m] = {w: _ci(rng, _vals(sel, m, *WINDOWS[w])) for w in WINDOWS}
                horizon = DAYS - 15
                cell["re_accept"] = _timing(rng, [_re_accept(r, 15) for r in sel], horizon)
                out["arms"][f"{variant}|{g}|{arm}"] = cell
            none, lr30, expl = get("none"), get("lr30"), get("explicit_only")
            for arm in ("explicit_only", "lr30", "r1", "r2", "r3", "r123"):
                sel = get(arm)
                if not sel or not none:
                    continue
                out["vs_none"][f"{variant}|{g}|{arm}"] = {
                    w: _paired_ratio(rng, sel, none, "acc_help_imm", *WINDOWS[w])
                    for w in ("d15_30", "d31_60", "d1_60")}
            for arm in ("explicit_only", "r1", "r2", "r3", "r123"):
                sel = get(arm)
                if not sel or not lr30:
                    continue
                out["vs_lr30"][f"{variant}|{g}|{arm}"] = {
                    "offered_unhelp_imm_d1_60": _paired_diff(rng, sel, lr30, "offered_unhelp_imm", 1, 60),
                    "acc_help_imm_d31_60": _paired_diff(rng, sel, lr30, "acc_help_imm", 31, 60),
                    "acc_help_imm_d31_60_ratio": _paired_ratio(rng, sel, lr30, "acc_help_imm", 31, 60),
                }
            if g == "all" and expl:
                for arm in ("lr30", "r1", "r2", "r3", "r123"):
                    sel = get(arm)
                    if sel:
                        out["vs_none"][f"{variant}|{g}|{arm}|ref=explicit_only"] = {
                            "d31_60": _paired_ratio(rng, sel, expl, "acc_help_imm", 31, 60)}
            for arm in ("r1", "r2", "r3", "r123"):
                agg: dict[str, int] = defaultdict(int)
                for r in get(arm):
                    for k, v in r["rule_stats"].items():
                        agg[k] += v
                out["rule_stats"][f"{variant}|{g}|{arm}"] = dict(agg)
    # registered test: tray0, all personas, r1 vs none on phone-scene accepted help days 31-60
    key = "tray0|all|r1"
    if key in out["vs_none"] and key in out["vs_lr30"]:
        ratio = out["vs_none"][key]["d31_60"]
        useless = out["vs_lr30"][key]["offered_unhelp_imm_d1_60"]
        out["verdict_1"] = {"variant": "tray0", "ratio_r1_over_none_d31_60": ratio,
                            "useless_alert_increase_r1_minus_lr30_d1_60": useless,
                            "verdict": _verdict_47(ratio, useless)}
    return out


# ── C4.8 ──────────────────────────────────────────────────────────────────────────────────────────
def _common_diffs(rows: list[dict[str, Any]]) -> dict[tuple[str, int], float]:
    """(persona, seed) -> (accA - accB) / n_common over phone items both conditions put to the resident."""
    by: dict[tuple[str, int], dict[str, dict[tuple[Any, ...], str]]] = defaultdict(dict)
    for r in rows:
        by[_unit(r)][r["cond"]] = {(i[0], i[1], i[2]): i[3] for i in r["items"]}
    out: dict[tuple[str, int], float] = {}
    for u, conds in by.items():
        if "A" not in conds or "B" not in conds:
            continue
        a, b = conds["A"], conds["B"]
        common = sorted(set(a) & set(b))
        out[u] = 0.0 if not common else (sum(a[k] == "a" for k in common)
                                         - sum(b[k] == "a" for k in common)) / len(common)
    return out


def _c48(rng: np.random.Generator, rows: list[dict[str, Any]]) -> dict[str, Any]:
    rows = [r for r in rows if r["part"] == "c48"]
    efforts = sorted({r["effort"] for r in rows})
    per: dict[float, dict[tuple[str, int], float]] = {}
    for e in efforts:
        per[e] = _common_diffs([r for r in rows if r["effort"] == e])
    out: dict[str, Any] = {"efforts": efforts, "points": {}, "steps": {}, "monotone": {}}
    for g, personas in {"all": PERSONAS, **{p: (p,) for p in PERSONAS}}.items():
        means = []
        for e in efforts:
            v = np.array([d for (p, _s), d in sorted(per[e].items()) if p in personas], dtype=float)
            ci = _ci(rng, v)
            out["points"][f"{g}|{e:.3f}"] = ci
            means.append(ci["mean"])
        if all(m is not None for m in means):
            steps = [means[i + 1] - means[i] for i in range(len(means) - 1)]
            rev = [i for i, s in enumerate(steps) if s > 0]
            reversal_ci = {}
            for i in rev:
                u = [k for k in per[efforts[i]] if k in per[efforts[i + 1]] and k[0] in personas]
                d = np.array([per[efforts[i + 1]][k] - per[efforts[i]][k] for k in sorted(u)])
                reversal_ci[f"{efforts[i]:.3f}->{efforts[i + 1]:.3f}"] = _ci(rng, d)
            out["monotone"][g] = {"non_increasing": not rev, "steps": steps,
                                  "reversals": [f"{efforts[i]:.3f}->{efforts[i + 1]:.3f}" for i in rev],
                                  "reversal_intervals": reversal_ci}
    return out


# ── assembly ──────────────────────────────────────────────────────────────────────────────────────
def summarize(raw_dir: Path) -> dict[str, Any]:
    rows = _load(raw_dir)
    if not rows:
        return {"runs": 0}
    rng = np.random.default_rng(BOOT_SEED)
    parts: dict[str, int] = defaultdict(int)
    for r in rows:
        parts[r["part"]] += 1
    return {
        "runs": len(rows),
        "runs_by_part": dict(sorted(parts.items())),
        "design": {"days": DAYS, "seeds": sorted({r["seed"] for r in rows}), "bootstrap": BOOT,
                   "safety_ok_all": all(r["safety_ok"] for r in rows),
                   "first_alerts_total": int(sum(r["first_alerts"] for r in rows)),
                   "runs_with_failed_safety": int(sum(not r["safety_ok"] for r in rows)),
                   "max_first_alert_ms": max((r["first_alert_ms_max"] for r in rows
                                              if r["first_alert_ms_max"] is not None), default=None)},
        "c46": _c46(rng, rows),
        "c47": _c47(rng, rows),
        "c48": _c48(rng, rows),
    }
