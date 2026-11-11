"""Cycle-2 analyses (C2.1-C2.4), registered in experiments/HYPOTHESES.md before the runs.

Reads only raw/. Between-set variation is shown as quartiles; intervals built from seeds alone are
called "seed noise". Verdicts are written in RESULTS.md strictly against the registered conditions.
"""

from __future__ import annotations

from typing import Any

import numpy as np

from . import sim
from .analyze import (
    SHAPES,
    Block,
    area,
    cls,
    dist,
    key,
    paired,
    pooled,
)
from .params import M_GRID

PRACTICAL = 0.02  # C2.2 registered +-2 percentage points


def _lane(blk: Block, k: str) -> dict[str, np.ndarray]:
    nh = blk.n["n_harm"].sum(1)
    npr = blk.n["n_help"].sum(1)
    n_prop = blk.n["n_prop"].sum(1)
    return {
        "harm": blk.seeds(k, "xh").sum(1) / np.maximum(1, nh),
        "help": blk.seeds(k, "xp").sum(1) / np.maximum(1, npr),
        "burden": blk.seeds(k, "nc").sum(1) / (blk.n_seeds * blk.days),
        "silenced": (blk.total(k, "bh") + blk.total(k, "bp") + blk.total(k, "bu")) / np.maximum(1, n_prop),
    }


def _share_below(blk: Block, upto: int = 2) -> tuple[float, np.ndarray]:
    """Share of harmful proposals on LIOT tiers < upto (2: AMBIENT+BREATH, 1: AMBIENT only)."""
    ht = np.array([r["harm_by_tier"] for r in blk.recs], float)
    per_set = (ht[:, :upto].sum(1)) / np.maximum(1, ht.sum(1))
    return float(ht[:, :upto].sum() / ht.sum()), per_set


# ── C2.1 ─────────────────────────────────────────────────────────────────────────────────
def c21(blocks: dict[str, Block], names: tuple[str, ...], upto: int = 2) -> dict[str, Any]:
    """C2.1 (upto=2: share on BREATH-or-below) and C2.11(a) (upto=1: share on AMBIENT)."""
    rows = []
    for bname in names:
        if bname not in blocks:
            continue
        blk = blocks[bname]
        fpos = blk.p["f"] > 0
        share, per_set = _share_below(blk, upto)
        row: dict[str, Any] = {"block": bname, "realized_breath_or_below_share": share,
                               "realized_ambient_share": _share_below(blk, 1)[0],
                               "realized_dial_share": share,
                               "per_set_quartiles": [float(x) for x in np.percentile(per_set, [25, 50, 75])]}
        for m in (0.0, 0.1):
            flat = key("flat_explicit", "exp", m)
            res: dict[str, Any] = {}
            for pol in ("liot_real", "liot_full"):
                kp = key(pol, "exp", m)
                d = paired(blk, kp, flat, "harm")
                c = cls(d)
                dh = paired(blk, kp, flat, "help")
                res[pol] = {
                    "area_harm_lower_seed_noise_sig": area(c["lower"], fpos),
                    "area_harm_higher_seed_noise_sig": area(c["higher"], fpos),
                    "share_sets_harm_lower_point": float(np.mean(d["est"][fpos] < 0)),
                    "harm_diff_pooled": pooled(blk, kp, flat, "harm", fpos),
                    "help_diff_pooled": pooled(blk, kp, flat, "help", fpos),
                    "harm_diff_set_dist": dist(d["est"], fpos),
                    "help_diff_set_dist": dist(dh["est"], fpos),
                }
            row[f"m{m:.1f}"] = res
        rows.append(row)
    out: dict[str, Any] = {"rows": rows}
    for m in ("m0.0", "m0.1"):
        xs = [r["realized_dial_share"] for r in rows]
        ys = [r[m]["liot_real"]["area_harm_lower_seed_noise_sig"]["share"] for r in rows]
        cross = None
        for i in range(1, len(xs)):
            if ys[i - 1] >= 0.5 > ys[i]:
                cross = xs[i - 1] + (xs[i] - xs[i - 1]) * (ys[i - 1] - 0.5) / (ys[i - 1] - ys[i])
                break
        out[f"share_where_area_crosses_50pct_{m}"] = cross
    return out


# ── C2.2 ─────────────────────────────────────────────────────────────────────────────────
def c22(blk: Block) -> dict[str, Any]:
    out: dict[str, Any] = {}
    fpos = blk.p["f"] > 0
    for sh in SHAPES:
        out[sh] = {}
        for m in (0.0, 0.1):
            res: dict[str, Any] = {}
            for base in ("flat_explicit", "binary_ext", "auto_all"):
                bsh = "na" if base == "auto_all" else sh
                kl = sim.Group(base, bsh, m=m, ceil="liot").key
                k0 = key(base, sh, m) if base != "auto_all" else key("auto_all", "na")
                lane_l = _lane(blk, kl)
                lane_0 = _lane(blk, k0)
                cuts = []
                for cut in sim.GEN_CUTS:
                    g = _lane(blk, sim.Group(base, bsh, m=m, ceil="gen", cut=cut).key)
                    cuts.append(g)
                s_c = np.array([g["silenced"] for g in cuts])  # (cuts, sets)
                h_c = np.array([g["harm"] for g in cuts])
                p_c = np.array([g["help"] for g in cuts])
                dh = np.full(blk.n_sets, np.nan)
                dp = np.full(blk.n_sets, np.nan)
                for i in range(blk.n_sets):
                    order = np.argsort(s_c[:, i], kind="stable")
                    xs = s_c[order, i]
                    dh[i] = lane_l["harm"][i] - np.interp(lane_l["silenced"][i], xs, h_c[order, i])
                    dp[i] = lane_l["help"][i] - np.interp(lane_l["silenced"][i], xs, p_c[order, i])
                within = (np.abs(dh) <= PRACTICAL) & (np.abs(dp) <= PRACTICAL)
                fals0 = (dh < -PRACTICAL) & (dp >= 0)
                fals1 = (dh < -PRACTICAL) & (dp >= -0.01)
                res[base] = {
                    "silenced_share_with_liot_ceiling": dist(lane_l["silenced"], fpos),
                    "harm_noceiling": dist(lane_0["harm"], fpos), "harm_liot_ceiling": dist(lane_l["harm"], fpos),
                    "help_noceiling": dist(lane_0["help"], fpos), "help_liot_ceiling": dist(lane_l["help"], fpos),
                    "harm_reduction_by_liot_ceiling": dist(lane_0["harm"] - lane_l["harm"], fpos),
                    "liot_minus_generator_ceiling_at_matched_silence": {
                        "harm_diff": dist(dh, fpos), "help_diff": dist(dp, fpos),
                        "share_within_2pp_both": area(within, fpos),
                        "share_falsifier_harm_lower_2pp_and_help_not_lower": area(fals0, fpos),
                        "share_falsifier_with_help_tolerance_1pp": area(fals1, fpos),
                    },
                }
            kr, kf = key("liot_real", sh, m), key("liot_full", sh, m)
            lr = _lane(blk, kr)
            fc = _lane(blk, sim.Group("flat_explicit", sh, m=m, ceil="liot").key)
            res["liot_real_vs_flat_same_liot_ceiling"] = {
                "harm_diff": dist(lr["harm"] - fc["harm"], fpos), "help_diff": dist(lr["help"] - fc["help"], fpos),
                "burden_diff": dist(lr["burden"] - fc["burden"], fpos),
                "share_sets_liot_real_harm_higher_point": float(np.mean((lr["harm"] - fc["harm"])[fpos] > 0)),
                "share_sets_liot_real_help_higher_point": float(np.mean((lr["help"] - fc["help"])[fpos] > 0)),
                "means": {"harm_liot_real": float(lr["harm"][fpos].mean()), "harm_flat_ceiling": float(fc["harm"][fpos].mean()),
                          "help_liot_real": float(lr["help"][fpos].mean()), "help_flat_ceiling": float(fc["help"][fpos].mean()),
                          "burden_liot_real": float(lr["burden"][fpos].mean()),
                          "burden_flat_ceiling": float(fc["burden"][fpos].mean())},
            }
            res["liot_real_reference"] = {k: dist(v, fpos) for k, v in _lane(blk, kr).items()}
            res["liot_full_reference"] = {k: dist(v, fpos) for k, v in _lane(blk, kf).items()}
            out[sh][f"m{m:.1f}"] = res
    out["generator_cuts"] = list(sim.GEN_CUTS)
    return out


# ── C2.3 ─────────────────────────────────────────────────────────────────────────────────
def c23(blk: Block) -> dict[str, Any]:
    fpos = blk.p["f"] > 0
    out: dict[str, Any] = {"note": "latency growth g = 0 for every policy in this block"}
    for sh in SHAPES:
        rows = []
        for m in M_GRID:
            flat = key("flat_explicit", sh, m)
            row: dict[str, Any] = {"m": m}
            for tag, kp in (("liot_full", key("liot_full", sh, m)),
                            ("liot_full_notify_only", sim.Group("liot_full", sh, m=m, notify=True).key),
                            ("liot_real", key("liot_real", sh, m))):
                d = paired(blk, kp, flat, "harm")
                c = cls(d)
                row[tag] = {
                    "area_harm_higher_seed_noise_sig": area(c["higher"], fpos),
                    "area_harm_lower_seed_noise_sig": area(c["lower"], fpos),
                    "share_sets_harm_higher_point": float(np.mean(d["est"][fpos] > 0)),
                    "harm_diff_pooled": pooled(blk, kp, flat, "harm", fpos),
                    "help_diff_pooled": pooled(blk, kp, flat, "help", fpos),
                    "harm_diff_set_dist": dist(d["est"], fpos),
                }
            rows.append(row)
        out[sh] = rows
    return out


# ── C2.4 ─────────────────────────────────────────────────────────────────────────────────
def c24(blk: Block) -> dict[str, Any]:
    fpos = blk.p["f"] > 0
    thetas = tuple(round(0.1 * i, 1) for i in range(11))
    nh = blk.n["n_harm"].sum(1)
    npr = blk.n["n_help"].sum(1)
    out: dict[str, Any] = {}
    sh = "exp"
    rows = []
    for m in (0.0, 0.1):
        kf = key("liot_full", sh, m)
        lf = _lane(blk, kf)
        lt = {"harm": lf["harm"], "burden": lf["burden"]}
        for kappa in sim.KAPPAS:
            ks = [sim.Group("theta", sh, theta=t, kappa=kappa).key for t in thetas]
            th = np.array([blk.seeds(k, "xh").sum(1) / np.maximum(1, nh) for k in ks])  # (theta, sets)
            tb = np.array([blk.seeds(k, "nc").sum(1) / (blk.n_seeds * blk.days) for k in ks])
            tp = np.array([blk.seeds(k, "xp").sum(1) / np.maximum(1, npr) for k in ks])
            dom = ((tb <= lt["burden"][None]) & (th <= lt["harm"][None])
                   & ((tb < lt["burden"][None]) | (th < lt["harm"][None]))).any(0)
            dom_margin = ((tb <= lt["burden"][None]) & (th <= lt["harm"][None] - 0.01)).any(0)
            dom_help = ((tb <= lt["burden"][None]) & (th <= lt["harm"][None]) & (tp >= lf["help"][None])
                        & ((tb < lt["burden"][None]) | (th < lt["harm"][None]) | (tp > lf["help"][None]))).any(0)
            aucs = np.array([r["auc"][f"kappa{kappa:.2f}"] for r in blk.recs], float)
            rows.append({
                "m": m, "kappa": kappa, "auc_median": float(np.nanmedian(aucs)),
                "auc_q25": float(np.nanpercentile(aucs, 25)), "auc_q75": float(np.nanpercentile(aucs, 75)),
                "share_sets_dominating_liot_full": area(dom, fpos),
                "share_sets_dominating_liot_full_by_1pp_harm": area(dom_margin, fpos),
                "share_sets_dominating_incl_helpful_completion": area(dom_help, fpos),
            })
    out["rows"] = rows

    def crossing(m: float, field: str) -> float | None:
        rs = [r for r in rows if r["m"] == m]
        rs = sorted(rs, key=lambda r: r["auc_median"])
        for i in range(1, len(rs)):
            a0, a1 = rs[i - 1][field]["share"], rs[i][field]["share"]
            if a0 < 0.5 <= a1:
                x0, x1 = rs[i - 1]["auc_median"], rs[i]["auc_median"]
                return x0 + (x1 - x0) * (0.5 - a0) / (a1 - a0)
        return None

    out["threshold_auc_where_dominating_share_reaches_50pct"] = {
        f"m{m:.1f}": {f: crossing(m, f) for f in ("share_sets_dominating_liot_full",
                                                  "share_sets_dominating_liot_full_by_1pp_harm",
                                                  "share_sets_dominating_incl_helpful_completion")}
        for m in (0.0, 0.1)}
    auc_t0 = np.array([r["auc"]["tier0"] for r in blk.recs], float)
    out["liot_tier_auc"] = {"tier0_median": float(np.nanmedian(auc_t0)),
                            "tier0_q25": float(np.nanpercentile(auc_t0, 25)),
                            "tier0_q75": float(np.nanpercentile(auc_t0, 75))}
    for m in M_GRID:
        v = np.array([r["auc"][f"tier_m{m:.1f}"] for r in blk.recs], float)
        out["liot_tier_auc"][f"tier_m{m:.1f}_median"] = float(np.nanmedian(v))
    for nm in ("base_sev",):
        v = np.array([r["auc"][nm] for r in blk.recs], float)
        out["liot_tier_auc"][f"{nm}_median"] = float(np.nanmedian(v))
    # reference lanes at the same sets
    out["reference_policy_means"] = {
        f"{p}_m{m:.1f}": {k: float(np.mean(v[fpos])) for k, v in _lane(blk, key(p, "exp", m)).items() if k != "silenced"}
        for p in ("flat_explicit", "liot_full", "liot_real") for m in (0.0, 0.1)}
    return out


# ── C2.11(b): light-confirmation rubber-stamp as a multiple of explicit ──────────────────
def c211b(blocks: dict[str, Block], names: tuple[str, ...], mults: tuple[float, ...]) -> dict[str, Any]:
    rows = []
    for bname, mult in zip(names, mults, strict=True):
        if bname not in blocks:
            continue
        blk = blocks[bname]
        fpos = blk.p["f"] > 0
        row: dict[str, Any] = {"block": bname, "light_rubber_stamp_multiple": mult}
        for m in (0.0, 0.1):
            flat = key("flat_explicit", "exp", m)
            res: dict[str, Any] = {}
            for pol in ("liot_real", "liot_full"):
                kp = key(pol, "exp", m)
                d = paired(blk, kp, flat, "harm")
                c = cls(d)
                res[pol] = {
                    "area_harm_higher_seed_noise_sig": area(c["higher"], fpos),
                    "area_harm_lower_seed_noise_sig": area(c["lower"], fpos),
                    "share_sets_harm_higher_point": float(np.mean(d["est"][fpos] > 0)),
                    "harm_diff_pooled": pooled(blk, kp, flat, "harm", fpos),
                    "help_diff_pooled": pooled(blk, kp, flat, "help", fpos),
                }
            row[f"m{m:.1f}"] = res
        rows.append(row)
    return {"rows": rows}
