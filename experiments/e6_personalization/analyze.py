"""E6 analysis: reads only raw/runs.jsonl, deterministic (verify recomputes it).

Units are seeds (independent 60-day runs). Every interval is a 95% percentile bootstrap over seeds
(pooled rows resample (persona, seed) pairs) with a fixed generator seed. Rates are per simulated day
averaged inside a window.
"""

from __future__ import annotations

from collections import defaultdict
from pathlib import Path
from typing import Any

import numpy as np

from experiments.common.io import read_jsonl

BOOT = 2000
BOOT_SEED = 20261112
FLIP = 15
PRE = (8, 14)
ROLL = 7
FRACTION = 0.8
WINDOWS = {"d1_14": (1, 14), "d15_30": (15, 30), "d31_60": (31, 60), "d1_30": (1, 30), "d1_60": (1, 60)}
RATE_METRICS = ("acc_help", "acc_help_imm", "acc_help_tray", "ws_help", "ws_help_phone", "ws_new",
                "new_acc", "new_offer_imm", "pushes_imm", "offered_help_imm", "offered_unhelp_imm",
                "dis", "exp_imm", "exp_dig", "digest_made", "opp_help")
CONTRAST_METRICS = ("acc_help", "ws_help_phone", "ws_help", "pushes_imm", "offered_unhelp_imm",
                    "accept_rate")
CONTRASTS = (("lr30", "none"), ("lr30", "explicit_only"), ("lr10", "lr30"), ("lr60", "lr30"),
             ("permanent", "lr30"), ("no_gate", "lr30"), ("explore7", "lr30"))
HEADLINE_WINDOWS = ("d1_14", "d15_30", "d31_60", "d1_60")


def _arr(rec: dict[str, Any], key: str) -> np.ndarray:
    return np.asarray(rec[key], dtype=float)


def _series(rec: dict[str, Any], metric: str) -> np.ndarray:
    if metric == "acc_help":
        return _arr(rec, "acc_help_imm") + _arr(rec, "acc_help_tray")
    return _arr(rec, metric)


def _window_mean(x: np.ndarray, lo: int, hi: int) -> float:
    return float(x[lo - 1:hi].mean())


def _accept_rate(rec: dict[str, Any], lo: int, hi: int) -> tuple[int, int]:
    """(accepted, pushed) phone items with day in [lo, hi] — from the item list."""
    its = [i for i in rec["items"] if lo <= i[0] <= hi]
    return sum(1 for i in its if i[3] == "a"), len(its)


def _ci(rng: np.random.Generator, per_unit: np.ndarray) -> dict[str, float | int]:
    """Mean and 95% bootstrap interval of a per-unit statistic."""
    n = len(per_unit)
    if n == 0:
        return {"n": 0, "mean": None, "lo": None, "hi": None}  # type: ignore[dict-item]
    idx = rng.integers(0, n, size=(BOOT, n))
    boots = per_unit[idx].mean(axis=1)
    return {"n": n, "mean": float(per_unit.mean()), "lo": float(np.percentile(boots, 2.5)),
            "hi": float(np.percentile(boots, 97.5))}


def _ratio_ci(rng: np.random.Generator, num: np.ndarray, den: np.ndarray) -> dict[str, Any]:
    n = len(num)
    if n == 0 or den.sum() == 0:
        return {"n": n, "mean": None, "lo": None, "hi": None}
    idx = rng.integers(0, n, size=(BOOT, n))
    dd = den[idx].sum(axis=1)
    ok = dd > 0
    boots = num[idx].sum(axis=1)[ok] / dd[ok]
    return {"n": n, "mean": float(num.sum() / den.sum()), "lo": float(np.percentile(boots, 2.5)),
            "hi": float(np.percentile(boots, 97.5))}


def _first_reaching(x: np.ndarray, threshold: float) -> int | None:
    """Days after the flip (flip = 0) at which the trailing ROLL-day mean first reaches threshold."""
    for t in range(FLIP + ROLL - 1, len(x) + 1):
        if float(x[t - ROLL:t].mean()) >= threshold:
            return t - FLIP
    return None


def _recovery(rec: dict[str, Any], oracle: float) -> dict[str, Any]:
    """Recovery of accepted-helpful after the flip, in three forms (days after the flip, flip = 0).

    literal : the pre-registered form - total accepted-helpful per day back to 80% of the arm's own
              pre-flip level (days 8-14). Only meaningful when the flip REMOVES helpful items (swap
              persona); when it only adds one, the unaffected scenes alone already meet the bar.
    oracle  : accepted-helpful on the newly helpful scenes back to 80% of what the static 'none' arm
              (no learning, no gate) reaches on them over days 22-60 (same persona, condition).
    re_offer / re_accept : first phone offer / first acceptance of a newly helpful scene.
    """
    total = _series(rec, "acc_help")
    pre = float(total[PRE[0] - 1:PRE[1]].mean())
    rec_day = _first_reaching(total, FRACTION * pre) if pre > 0 else None
    oracle_day = _first_reaching(_arr(rec, "new_acc"), FRACTION * oracle) if oracle > 0 else None

    def first(key: str) -> int | None:
        arr = _arr(rec, key)
        hit = np.nonzero(arr[FLIP - 1:] > 0)[0]
        return int(hit[0]) if hit.size else None

    return {"pre": pre, "recovery": rec_day, "recovery_oracle": oracle_day,
            "re_offer": first("new_offer_imm"), "re_accept": first("new_acc")}


def _timing(rng: np.random.Generator, values: list[int | None], horizon: int) -> dict[str, Any]:
    """Fraction that happened within the horizon (+CI) and the median days among those that did."""
    n = len(values)
    hit = np.array([v is not None for v in values], dtype=float)
    done = [v for v in values if v is not None]
    out = _ci(rng, hit)
    out["fraction"] = out.pop("mean")
    out["median_days"] = None if not done else float(np.median(done))
    out["horizon_days"] = horizon
    out["n"] = n
    return out


def _load(raw_dir: Path) -> list[dict[str, Any]]:
    rows = read_jsonl(Path(raw_dir) / "runs.jsonl")
    return sorted(rows, key=lambda r: (r["variant"], r["persona"], r["arm"], r["cond"], r["seed"]))


def _group_stats(rng: np.random.Generator, rows: list[dict[str, Any]],
                 oracles: dict[tuple[str, str, str], float]) -> dict[str, Any]:
    out: dict[str, Any] = {"n_runs": len(rows)}
    for w, (lo, hi) in WINDOWS.items():
        cell: dict[str, Any] = {}
        for m in RATE_METRICS:
            cell[m] = _ci(rng, np.array([_window_mean(_series(r, m), lo, hi) for r in rows]))
        acc = np.array([_accept_rate(r, lo, hi)[0] for r in rows], dtype=float)
        pushed = np.array([_accept_rate(r, lo, hi)[1] for r in rows], dtype=float)
        cell["accept_rate"] = _ratio_ci(rng, acc, pushed)
        out[w] = cell
    rec = [_recovery(r, oracles.get((r["variant"], r["persona"], r["cond"]), 0.0)) for r in rows]
    horizon = rows[0]["days"] - FLIP
    out["recovery"] = _timing(rng, [x["recovery"] for x in rec if x["pre"] > 0], horizon)
    out["recovery_oracle"] = _timing(
        rng, [x["recovery_oracle"] for r, x in zip(rows, rec, strict=True)
              if oracles.get((r["variant"], r["persona"], r["cond"]), 0.0) > 0], horizon)
    out["re_offer"] = _timing(rng, [x["re_offer"] for x in rec], horizon)
    out["re_accept"] = _timing(rng, [x["re_accept"] for x in rec], horizon)
    out["pre_level"] = _ci(rng, np.array([x["pre"] for x in rec]))
    return out


def _contrast(rng: np.random.Generator, a: list[dict[str, Any]], b: list[dict[str, Any]],
              metric: str, lo: int, hi: int) -> dict[str, Any]:
    """Paired difference (arm a - arm b) per (persona, seed) unit."""
    bm = {(r["persona"], r["seed"]): r for r in b}
    diffs: list[float] = []
    for r in a:
        o = bm.get((r["persona"], r["seed"]))
        if o is None:
            continue
        if metric == "accept_rate":
            xa, na = _accept_rate(r, lo, hi)
            xb, nb = _accept_rate(o, lo, hi)
            if na == 0 or nb == 0:
                continue
            diffs.append(xa / na - xb / nb)
        else:
            diffs.append(_window_mean(_series(r, metric), lo, hi) - _window_mean(_series(o, metric), lo, hi))
    return _ci(rng, np.array(diffs))


def _h43(rng: np.random.Generator, rows: list[dict[str, Any]]) -> dict[str, Any]:
    """Acceptance over the phone items BOTH conditions put to the resident (same day, time, scene)."""
    by: dict[tuple[str, str, str, int], dict[str, dict[tuple[Any, ...], str]]] = defaultdict(dict)
    for r in rows:
        if r["cond"] in ("A", "B"):
            by[(r["variant"], r["persona"], r["arm"], r["seed"])][r["cond"]] = {
                (i[0], i[1], i[2]): i[3] for i in r["items"]}
    groups: dict[tuple[str, str, str], list[tuple[int, ...]]] = defaultdict(list)
    for (variant, persona, arm, _seed), conds in sorted(by.items()):
        if "A" not in conds or "B" not in conds:
            continue
        a, b = conds["A"], conds["B"]
        common = sorted(set(a) & set(b))
        groups[(variant, persona, arm)].append((
            len(common), sum(a[k] == "a" for k in common), sum(b[k] == "a" for k in common),
            len(a), sum(v == "a" for v in a.values()), len(b), sum(v == "a" for v in b.values())))
    out: dict[str, Any] = {}
    for (variant, persona, arm), recs in groups.items():
        m = np.array(recs, dtype=float)
        n_common, acc_a, acc_b = m[:, 0], m[:, 1], m[:, 2]
        diff_units = np.where(n_common > 0, (acc_a - acc_b) / np.maximum(n_common, 1), 0.0)
        out[f"{variant}|{persona}|{arm}"] = {
            "seeds": len(recs), "common_items": int(n_common.sum()),
            "items_A": int(m[:, 3].sum()), "items_B": int(m[:, 5].sum()),
            "accept_A_on_common": _ratio_ci(rng, acc_a, n_common),
            "accept_B_on_common": _ratio_ci(rng, acc_b, n_common),
            "diff_A_minus_B_per_seed": _ci(rng, diff_units),
            "accept_A_all_items": _ratio_ci(rng, m[:, 4], m[:, 3]),
            "accept_B_all_items": _ratio_ci(rng, m[:, 6], m[:, 5]),
        }
    return out


def summarize(raw_dir: Path) -> dict[str, Any]:
    rows = _load(raw_dir)
    rng = np.random.default_rng(BOOT_SEED)
    if not rows:
        return {"runs": 0}
    groups: dict[tuple[str, str, str, str], list[dict[str, Any]]] = defaultdict(list)
    pooled: dict[tuple[str, str, str], list[dict[str, Any]]] = defaultdict(list)
    for r in rows:
        groups[(r["variant"], r["persona"], r["arm"], r["cond"])].append(r)
        pooled[(r["variant"], r["arm"], r["cond"])].append(r)
    # oracle level: accepted-helpful per day on the newly helpful scenes in the static 'none' arm
    oracles: dict[tuple[str, str, str], float] = {}
    for (variant, persona, arm, cond), v in groups.items():
        if arm == "none":
            oracles[(variant, persona, cond)] = float(np.mean([_arr(r, "new_acc")[21:].mean()
                                                               for r in v]))
    out: dict[str, Any] = {
        "runs": len(rows),
        "oracle_new_acc_per_day": {"|".join(k): v for k, v in sorted(oracles.items())},
        "design": {"days": rows[0]["days"], "flip_day": FLIP, "pre_window": list(PRE),
                   "recovery_window_days": ROLL, "recovery_fraction": FRACTION,
                   "bootstrap": BOOT, "seeds": sorted({r["seed"] for r in rows}),
                   "safety_ok_all": all(r["safety_ok"] for r in rows),
                   "first_alerts_total": int(sum(r["first_alerts"] for r in rows)),
                   "max_first_alert_ms": max((r["first_alert_ms_max"] for r in rows
                                              if r["first_alert_ms_max"] is not None), default=None)},
        "groups": {"|".join(k): _group_stats(rng, v, oracles) for k, v in sorted(groups.items())},
        "pooled": {"|".join(k): _group_stats(rng, v, oracles) for k, v in sorted(pooled.items())},
    }
    # reachability: can the pre-registered 80% reference be met at all? (no_gate arm, days 41-60)
    reach: dict[str, Any] = {}
    for (variant, persona, arm, cond), v in sorted(groups.items()):
        if arm != "no_gate":
            continue
        pre = float(np.mean([_series(r, "acc_help")[PRE[0] - 1:PRE[1]].mean() for r in v]))
        late = float(np.mean([_series(r, "acc_help")[40:60].mean() for r in v]))
        reach[f"{variant}|{persona}|{cond}"] = {"pre_level": pre, "no_gate_days41_60": late,
                                                "reference_reachable": late >= FRACTION * pre}
    out["reachability"] = reach
    # H4.1 / contrasts: paired by (persona, seed), per persona and pooled
    contrasts: dict[str, Any] = {}
    for variant, cond in sorted({(k[0], k[3]) for k in groups}):
        for persona in [*sorted({k[1] for k in groups}), "pooled"]:
            for a_arm, b_arm in CONTRASTS:
                if persona == "pooled":
                    a, b = pooled.get((variant, a_arm, cond), []), pooled.get((variant, b_arm, cond), [])
                else:
                    a, b = groups.get((variant, persona, a_arm, cond), []), groups.get((variant, persona, b_arm, cond), [])
                if not a or not b:
                    continue
                key = f"{variant}|{persona}|{cond}|{a_arm}-{b_arm}"
                contrasts[key] = {
                    w: {m: _contrast(rng, a, b, m, *WINDOWS[w]) for m in CONTRAST_METRICS}
                    for w in HEADLINE_WINDOWS}
    out["contrasts"] = contrasts
    out["h43"] = _h43(rng, rows)
    return out
