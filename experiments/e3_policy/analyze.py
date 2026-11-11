"""E3 analysis: reads only raw/runs.jsonl, deterministic (verify recomputes it).

Conventions
  * paired difference per parameter set = policy A minus policy B on the SAME seeds (common
    random numbers); its 95% interval is a bootstrap over the seeds of that set (ratio of sums
    inside each resample). Resample indices are fixed (seeded), identical for every comparison.
  * "area" = share of parameter sets (Latin hypercube = uniform sample of the range box) where a
    condition holds; its 95% interval bootstraps over sets. Areas depend on the ranges chosen
    (hence the half/double-range blocks).
  * sig_higher / sig_lower = interval excludes 0 with A above / below B; otherwise "ns".
"""

from __future__ import annotations

import warnings
from pathlib import Path
from typing import Any

import numpy as np

from experiments.common.io import read_jsonl

from .params import M_DEFAULT, M_GRID, PARAMS

B_SEED = 31
NB = 400  # seed-bootstrap resamples per set
NB_SETS = 1000  # set-bootstrap resamples
SHAPES = ("exp", "hyp")
FBINS = (0.0, 0.075, 0.15, 0.225, 0.3000001)
RBINS = (0.0, 0.2, 0.4, 0.6, 0.800001)
PRACTICAL = 0.01  # a harm-rate difference below one percentage point is "no practical difference"
TOL_MONO = 0.005  # harm-rate wobble tolerated when calling a curve monotone
CAP_MIN = 240.0

NAMED = ("auto_all", "flat_explicit", "binary_ext", "binary_opt", "liot_real", "liot_full", "auto_undo")


def _arr(s: str | None) -> np.ndarray:
    return np.array([float(x) for x in s.split(",")]) if s else np.zeros(0)


def key(name: str, shape: str, m: float | None = None, w: int | None = None,
        s: float | None = None, t: float | None = None, gate: bool = False) -> str:
    if name == "auto_all":
        shape = "na"
    parts = [name, shape]
    if m is not None:
        parts.append(f"m{m:.1f}")
    if w is not None:
        parts.append(f"w{w}")
    if t is not None:
        parts += [f"s{s:.1f}", f"t{t:.1f}"]
    if gate:
        parts.append("gate")
    return "|".join(parts)


class Block:
    """All records of one block as aligned numpy arrays (sets x seeds)."""

    def __init__(self, recs: list[dict[str, Any]]) -> None:
        recs = sorted(recs, key=lambda r: r["set_id"])
        self.recs = recs
        self.n_sets = len(recs)
        self.n_seeds = recs[0]["n_seeds"] if recs else 0
        self.days = recs[0]["days"] if recs else 0
        self.p = {k: np.array([r["params"][k] for r in recs]) for k in recs[0]["params"]} if recs else {}
        self.n = {k: np.array([_arr(r["n"][k]) for r in recs]) for k in recs[0]["n"]} if recs else {}
        self._cache: dict[tuple[str, str], np.ndarray] = {}

    def has(self, k: str) -> bool:
        return bool(self.recs) and k in self.recs[0]["res"]

    def seeds(self, k: str, field: str) -> np.ndarray:
        """(sets, seeds) per-seed array, or broadcast sums/n_seeds when only the sum was stored."""
        ck = (k, field)
        if ck in self._cache:
            return self._cache[ck]
        out = []
        for r in self.recs:
            v = r["res"][k]
            if field in v:
                a = _arr(v[field]) if isinstance(v[field], str) else np.full(self.n_seeds, float(v[field]) / self.n_seeds)
            else:
                a = np.full(self.n_seeds, float(v["s_" + field]) / self.n_seeds)
            out.append(a)
        arr = np.array(out)
        self._cache[ck] = arr
        return arr

    def total(self, k: str, field: str) -> np.ndarray:
        return np.array([float(r["res"][k][field]) if field in r["res"][k] else float(r["res"][k]["s_" + field])
                         for r in self.recs])

    def has_seed_detail(self, k: str, field: str) -> bool:
        return isinstance(self.recs[0]["res"][k].get(field), str)


def load(raw_dir: Path) -> dict[str, Block]:
    by: dict[str, list[dict[str, Any]]] = {}
    for r in read_jsonl(Path(raw_dir) / "runs.jsonl"):
        by.setdefault(r["block"], []).append(r)
    return {b: Block(v) for b, v in by.items()}


# ── bootstrap helpers ────────────────────────────────────────────────────────────────────
def _counts(n_seeds: int) -> np.ndarray:
    rng = np.random.default_rng(B_SEED)
    idx = rng.integers(0, n_seeds, (NB, n_seeds))
    c = np.zeros((NB, n_seeds))
    for b in range(NB):
        c[b] = np.bincount(idx[b], minlength=n_seeds)
    return c


def metric_arrays(blk: Block, k: str, metric: str) -> tuple[np.ndarray, np.ndarray]:
    """(numerator, denominator) per set and seed for a ratio metric."""
    n = blk.n
    if metric == "harm":
        return blk.seeds(k, "xh"), n["n_harm"]
    if metric == "help":
        return blk.seeds(k, "xp"), n["n_help"]
    if metric == "burden":
        return blk.seeds(k, "nc"), np.full_like(n["n_help"], blk.days)
    if metric == "hz_ok":
        return blk.seeds(k, "hz_ok"), n["n_haz"]
    if metric == "hz_lat":
        return blk.seeds(k, "hz_lat"), n["n_haz"]
    raise KeyError(metric)


def ratio(num: np.ndarray, den: np.ndarray) -> np.ndarray:
    s, d = num.sum(1), den.sum(1)
    with np.errstate(invalid="ignore", divide="ignore"):
        return np.where(d > 0, s / d, np.nan)


def boot_ratio(num: np.ndarray, den: np.ndarray, c: np.ndarray) -> np.ndarray:
    """(sets, NB) ratio-of-sums per resample."""
    ns, ds = num @ c.T, den @ c.T
    with np.errstate(invalid="ignore", divide="ignore"):
        return np.where(ds > 0, ns / ds, np.nan)


def paired(blk: Block, ka: str, kb: str, metric: str) -> dict[str, np.ndarray]:
    na, da = metric_arrays(blk, ka, metric)
    nb, db = metric_arrays(blk, kb, metric)
    c = _counts(blk.n_seeds)
    est = ratio(na, da) - ratio(nb, db)
    bt = boot_ratio(na, da, c) - boot_ratio(nb, db, c)
    lo = np.nanpercentile(bt, 2.5, axis=1)
    hi = np.nanpercentile(bt, 97.5, axis=1)
    return {"est": est, "lo": lo, "hi": hi, "a": ratio(na, da), "b": ratio(nb, db)}


def cls(d: dict[str, np.ndarray]) -> dict[str, np.ndarray]:
    with np.errstate(invalid="ignore"):
        return {"higher": d["lo"] > 0, "lower": d["hi"] < 0,
                "ns": ~((d["lo"] > 0) | (d["hi"] < 0)) & ~np.isnan(d["est"])}


def pooled(blk: Block, ka: str, kb: str, metric: str, mask: np.ndarray) -> dict[str, float]:
    """Mean over sets of the paired difference; lo/hi = seed noise only, set_* = spread across sets."""
    na, da = metric_arrays(blk, ka, metric)
    nb, db = metric_arrays(blk, kb, metric)
    c = _counts(blk.n_seeds)
    d = boot_ratio(na, da, c) - boot_ratio(nb, db, c)
    est = ratio(na, da) - ratio(nb, db)
    ok = mask & ~np.isnan(est)
    bt = np.nanmean(d[ok], axis=0)
    q = np.percentile(est[ok], [10, 25, 50, 75, 90])
    # lo/hi = seed-noise interval (seeds resampled inside each of the fixed sets); the spread
    # between parameter sets is the set_* quartiles
    return {"mean": float(np.mean(est[ok])), "lo": float(np.percentile(bt, 2.5)),
            "hi": float(np.percentile(bt, 97.5)), "n_sets": int(ok.sum()),
            "set_p10": float(q[0]), "set_q25": float(q[1]), "set_median": float(q[2]),
            "set_q75": float(q[3]), "set_p90": float(q[4])}


def area(x: np.ndarray, mask: np.ndarray) -> dict[str, float]:
    """Share of sets in *mask* where *x* holds, with a bootstrap-over-sets 95% interval."""
    v = x[mask].astype(float)
    if v.size == 0:
        return {"share": float("nan"), "lo": float("nan"), "hi": float("nan"), "n": 0}
    rng = np.random.default_rng(B_SEED + 1)
    idx = rng.integers(0, v.size, (NB_SETS, v.size))
    m = v[idx].mean(1)
    return {"share": float(v.mean()), "lo": float(np.percentile(m, 2.5)),
            "hi": float(np.percentile(m, 97.5)), "n": int(v.size)}


def cls_areas(c: dict[str, np.ndarray], mask: np.ndarray) -> dict[str, Any]:
    return {k: area(v, mask) for k, v in c.items()}


def dist(x: np.ndarray, mask: np.ndarray) -> dict[str, float]:
    v = x[mask & ~np.isnan(x)]
    if v.size == 0:
        return {"n": 0}
    q = np.percentile(v, [10, 25, 50, 75, 90])
    return {"n": int(v.size), "mean": float(v.mean()), "p10": float(q[0]), "p25": float(q[1]),
            "median": float(q[2]), "p75": float(q[3]), "p90": float(q[4])}


# ── per-policy table ─────────────────────────────────────────────────────────────────────
def policy_table(blk: Block, shape: str, m: float, gate: bool = False) -> dict[str, Any]:
    pols = {
        "auto_all": key("auto_all", shape, gate=gate),
        "flat_explicit": key("flat_explicit", shape, m, gate=gate),
        "binary_ext": key("binary_ext", shape, m, gate=gate),
        "binary_opt": key("binary_opt", shape, m, gate=gate),
        "liot_real": key("liot_real", shape, m, gate=gate),
        "liot_full": key("liot_full", shape, m, gate=gate),
    }
    for w in (5, 15, 30):
        pols[f"auto_undo_w{w}"] = key("auto_undo", shape, w=w)
    out: dict[str, Any] = {}
    n = blk.n
    for name, k in pols.items():
        if not blk.has(k):
            continue
        row: dict[str, Any] = {}
        mets = {
            "burden_per_day": ratio(*metric_arrays(blk, k, "burden")),
            "load_units_per_day": blk.total(k, "units") / (blk.n_seeds * blk.days),
            "harm_rate": ratio(*metric_arrays(blk, k, "harm")),
            "helpful_completion": ratio(*metric_arrays(blk, k, "help")),
            "unwanted_executed": blk.total(k, "xu") / np.maximum(1, n["n_unw"].sum(1)),
            "hazard_responded": ratio(*metric_arrays(blk, k, "hz_ok")),
            "hazard_mean_latency_s": ratio(*metric_arrays(blk, k, "hz_lat")),
            "harm_minutes_per_harmful": blk.total(k, "hm") / np.maximum(1, n["n_harm"].sum(1)),
            "silenced_helpful_share": blk.total(k, "bp") / np.maximum(1, n["n_help"].sum(1)),
            "silenced_harm_share": blk.total(k, "bh") / np.maximum(1, n["n_harm"].sum(1)),
        }
        rng = np.random.default_rng(B_SEED + 2)
        for mn, v in mets.items():
            ok = ~np.isnan(v)
            vv = v[ok]
            if vv.size == 0:
                continue
            idx = rng.integers(0, vv.size, (NB_SETS, vv.size))
            bm = vv[idx].mean(1)
            row[mn] = {"mean": float(vv.mean()), "lo": float(np.percentile(bm, 2.5)),
                       "hi": float(np.percentile(bm, 97.5)), "median": float(np.median(vv))}
        out[name] = row
    return out


# ── H2.1 and friends ─────────────────────────────────────────────────────────────────────
def binned_grid(blk: Block, vals: np.ndarray, fm: np.ndarray) -> list[list[float | None]]:
    """Mean of *vals* over an f x r grid (rows f bins, cols r bins); None = empty cell."""
    f, r = blk.p["f"], blk.p["r"]
    grid: list[list[float | None]] = []
    for i in range(len(FBINS) - 1):
        row: list[float | None] = []
        for j in range(len(RBINS) - 1):
            sel = fm & (f >= FBINS[i]) & (f < FBINS[i + 1]) & (r >= RBINS[j]) & (r < RBINS[j + 1]) & ~np.isnan(vals)
            row.append(float(vals[sel].mean()) if sel.any() else None)
        grid.append(row)
    return grid


def h21(blk: Block, shape: str, m: float) -> dict[str, Any]:
    fpos = blk.p["f"] > 0
    flat = key("flat_explicit", shape, m)
    out: dict[str, Any] = {"n_sets_f_gt_0": int(fpos.sum())}
    for pol in ("liot_real", "liot_full"):
        kp = key(pol, shape, m)
        res: dict[str, Any] = {}
        for met in ("harm", "help", "hz_ok", "burden"):
            d = paired(blk, kp, flat, met)
            c = cls(d)
            res[met] = {
                "pooled_diff_vs_flat": pooled(blk, kp, flat, met, fpos),
                "areas": cls_areas(c, fpos),
                "set_dist_diff": dist(d["est"], fpos),
            }
            if met == "harm":
                res[met]["by_f_bin"] = []
                for i in range(len(FBINS) - 1):
                    sel = fpos & (blk.p["f"] >= FBINS[i]) & (blk.p["f"] < FBINS[i + 1])
                    res[met]["by_f_bin"].append({
                        "f_lo": FBINS[i], "f_hi": min(FBINS[i + 1], 0.3),
                        **{k: area(v, sel)["share"] for k, v in c.items()}, "n": int(sel.sum()),
                    })
                res[met]["map_f_by_r_mean_diff"] = binned_grid(blk, d["est"], fpos)
                res[met]["map_f_by_r_share_liot_higher"] = binned_grid(blk, c["higher"].astype(float), fpos)
                res[met]["_est"] = d["est"]  # stripped before output
        out[pol] = res
    # which policy has the lowest harm (ties split) among the named competitors
    names = ["flat_explicit", "binary_ext", "binary_opt", "liot_real", "liot_full", "auto_undo"]
    keys = {n_: (key(n_, shape, w=15) if n_ == "auto_undo" else key(n_, shape, m)) for n_ in names}
    mat = np.array([ratio(*metric_arrays(blk, keys[n_], "harm")) for n_ in names])  # (pol, sets)
    best = np.nanmin(mat, axis=0)
    win = np.isclose(mat, best[None, :], atol=1e-12) & ~np.isnan(mat)
    win = win / np.maximum(1, win.sum(0))[None, :]
    out["lowest_harm_share"] = {n_: area(win[i], fpos) for i, n_ in enumerate(names)}
    grid_best = []
    for i in range(len(FBINS) - 1):
        row = []
        for j in range(len(RBINS) - 1):
            sel = fpos & (blk.p["f"] >= FBINS[i]) & (blk.p["f"] < FBINS[i + 1]) & (blk.p["r"] >= RBINS[j]) & (blk.p["r"] < RBINS[j + 1])
            row.append(names[int(np.argmax(win[:, sel].mean(1)))] if sel.any() else None)
        grid_best.append(row)
    out["map_f_by_r_lowest_harm_policy"] = grid_best
    # same map without the policy that silences proposals (liot_real): the fair confirmation contest
    keep = [i for i, n_ in enumerate(names) if n_ != "liot_real"]
    mat2 = mat[keep]
    best2 = np.nanmin(mat2, axis=0)
    win2 = np.isclose(mat2, best2[None, :], atol=1e-12) & ~np.isnan(mat2)
    win2 = win2 / np.maximum(1, win2.sum(0))[None, :]
    names2 = [names[i] for i in keep]
    out["lowest_harm_share_excluding_liot_real"] = {n_: area(win2[i], fpos) for i, n_ in enumerate(names2)}
    grid2 = []
    for i in range(len(FBINS) - 1):
        row = []
        for j in range(len(RBINS) - 1):
            sel = fpos & (blk.p["f"] >= FBINS[i]) & (blk.p["f"] < FBINS[i + 1]) & (blk.p["r"] >= RBINS[j]) & (blk.p["r"] < RBINS[j + 1])
            row.append(names2[int(np.argmax(win2[:, sel].mean(1)))] if sel.any() else None)
        grid2.append(row)
    out["map_f_by_r_lowest_harm_policy_excluding_liot_real"] = grid2
    out["map_f_bins"] = list(FBINS[:-1]) + [0.3]
    out["map_r_bins"] = list(RBINS[:-1]) + [0.8]
    return out


def strip_private(o: Any) -> Any:
    """Drop private (underscore) keys and turn NaN into None (NaN != NaN breaks verify)."""
    if isinstance(o, float) and o != o:
        return None
    if isinstance(o, dict):
        return {k: strip_private(v) for k, v in o.items() if not k.startswith("_")}
    if isinstance(o, list):
        return [strip_private(v) for v in o]
    return o


def h22(blk: Block) -> dict[str, Any]:
    """f=0, r=0 block: flat vs the tiered variants and the U-shape check."""
    allm = np.ones(blk.n_sets, bool)
    out: dict[str, Any] = {"n_sets": blk.n_sets}
    for m in (0.0, M_DEFAULT):
        flat = key("flat_explicit", "na", m)
        res: dict[str, Any] = {}
        for pol in ("liot_full", "liot_real", "binary_opt", "binary_ext"):
            kp = key(pol, "na", m)
            d = paired(blk, kp, flat, "harm")
            c = cls(d)
            res[pol] = {"pooled_diff_vs_flat": pooled(blk, kp, flat, "harm", allm),
                        "areas": cls_areas(c, allm), "set_dist_diff": dist(d["est"], allm),
                        "share_sets_difference_exactly_zero": float(np.mean(d["est"] == 0)),
                        "share_sets_flat_not_higher": float(np.mean(d["est"] >= 0))}
        out[f"m{m:.1f}"] = res
    out["u_shape"] = {f"s{s:.1f}": u_shape(blk, "na", s, np.ones(blk.n_sets, bool)) for s in (0.0, 0.5, 1.0)}
    out["u_shape"] = {k: v for k, v in out["u_shape"].items() if v}
    ver: dict[str, Any] = {}
    for pol in ("liot_full", "liot_real"):
        pd_ = out["m0.0"][pol]["pooled_diff_vs_flat"]
        ver[pol] = {
            "flat_strictly_lower_pooled_ci_excludes_0": bool(pd_["lo"] > 0),
            "flat_strictly_higher_pooled_ci_excludes_0": bool(pd_["hi"] < 0),
            "share_sets_flat_strictly_lower": out["m0.0"][pol]["areas"]["higher"]["share"],
            "share_sets_flat_strictly_higher": out["m0.0"][pol]["areas"]["lower"]["share"],
        }
    ver["u_shape_vanishes_all_sigma"] = (all(
        out["u_shape"][k]["u_shape_sig"]["share"] == 0.0 for k in out["u_shape"]) if out["u_shape"] else None)
    out["verdict_inputs"] = ver
    return out


# ── H2.3 ─────────────────────────────────────────────────────────────────────────────────
THETAS = tuple(round(0.1 * i, 1) for i in range(11))


def u_shape(blk: Block, shape: str, sigma: float, mask: np.ndarray) -> dict[str, Any]:
    ks = [key("theta", shape, s=sigma, t=t) for t in THETAS]
    if not all(blk.has(k) for k in ks):
        return {}
    arr = np.stack([blk.seeds(k, "xh") for k in ks], axis=-1)  # (sets, seeds, 11)
    nh = blk.n["n_harm"]
    c = _counts(blk.n_seeds)
    num = np.einsum("bs,nst->nbt", c, arr)
    den = (c @ nh.T).T  # (sets, B)
    with np.errstate(invalid="ignore", divide="ignore"):
        curve_b = np.where(den[..., None] > 0, num / den[..., None], np.nan)
    pt = arr.sum(1) / np.maximum(1, nh.sum(1))[:, None]  # (sets, 11)
    inner = curve_b[..., 1:10]
    mn = np.nanmin(inner, axis=-1)
    d0 = curve_b[..., 0] - mn
    d1 = curve_b[..., 10] - mn
    lo0, lo1 = np.nanpercentile(d0, 2.5, axis=1), np.nanpercentile(d1, 2.5, axis=1)
    u_sig = (lo0 > 0) & (lo1 > 0)
    u_prac = (lo0 > PRACTICAL) & (lo1 > PRACTICAL)
    dec = np.all(np.diff(pt, axis=1) <= TOL_MONO, axis=1)
    inv_u = np.argmax(pt[:, 1:10], axis=1)  # interior max (literal inverted U of harm)
    inv = (pt[:, 1:10].max(1) > np.maximum(pt[:, 0], pt[:, 10]) + TOL_MONO)
    argmin = np.argmin(pt, axis=1)
    out = {
        "u_shape_sig": area(u_sig, mask),
        "u_shape_sig_depth_gt_0.01": area(u_prac, mask),
        "u_depth_median": float(np.nanmedian(np.where(mask, np.nanmedian(d1, axis=1), np.nan))),
        "monotone_nonincreasing": area(dec, mask),
        "interior_max_harm_inverted_u": area(inv, mask),
        "argmin_theta_dist": {f"{t:.1f}": float(np.mean(argmin[mask] == i)) for i, t in enumerate(THETAS)},
        "median_curve": [float(np.nanmedian(pt[mask][:, i])) for i in range(11)],
        "q25_curve": [float(np.nanpercentile(pt[mask][:, i], 25)) for i in range(11)],
        "q75_curve": [float(np.nanpercentile(pt[mask][:, i], 75)) for i in range(11)],
        "_u_sig": u_sig, "_inv_unused": inv_u,
    }
    f = blk.p["f"]
    out["by_f_bin"] = []
    for i in range(len(FBINS) - 1):
        sel = mask & (f >= FBINS[i]) & (f < FBINS[i + 1])
        out["by_f_bin"].append({"f_lo": FBINS[i], "u_sig": area(u_sig, sel)["share"],
                                "u_depth_gt_0.01": area(u_prac, sel)["share"],
                                "monotone": area(dec, sel)["share"], "n": int(sel.sum())})
    out["map_f_by_r_u_share"] = binned_grid(blk, u_sig.astype(float), mask)
    # f terciles of the median curve (for the figure)
    qs = np.quantile(f[mask], [1 / 3, 2 / 3]) if mask.any() else [0, 0]
    terc = [mask & (f <= qs[0]), mask & (f > qs[0]) & (f <= qs[1]), mask & (f > qs[1])]
    out["median_curve_by_f_tercile"] = [[float(np.nanmedian(pt[t][:, i])) if t.any() else None for i in range(11)] for t in terc]
    return out


def h23(blk: Block) -> dict[str, Any]:
    fpos = blk.p["f"] > 0
    return {sh: {f"s{s:.1f}": u_shape(blk, sh, s, fpos) for s in (0.0, 0.5, 1.0)} for sh in SHAPES}


# ── H2.4 ─────────────────────────────────────────────────────────────────────────────────
def h24(blk: Block) -> dict[str, Any]:
    fpos = blk.p["f"] > 0
    out: dict[str, Any] = {"m_grid": list(M_GRID)}
    for sh in SHAPES:
        rows = []
        for m in M_GRID:
            flat, opt, ext = key("flat_explicit", sh, m), key("binary_opt", sh, m), key("binary_ext", sh, m)
            cl = {}
            for pol in ("liot_real", "liot_full"):
                kp = key(pol, sh, m)
                cl[f"{pol}_vs_flat"] = cls(paired(blk, kp, flat, "harm"))["lower"]
                cl[f"{pol}_vs_binary_opt"] = cls(paired(blk, kp, opt, "harm"))["lower"]
                cl[f"{pol}_vs_binary_ext"] = cls(paired(blk, kp, ext, "harm"))["lower"]
            both = (cl["liot_real_vs_flat"] & cl["liot_full_vs_flat"] & cl["liot_real_vs_binary_opt"]
                    & cl["liot_full_vs_binary_opt"])
            full_only = cl["liot_full_vs_flat"] & cl["liot_full_vs_binary_opt"]
            both_ext = (cl["liot_real_vs_flat"] & cl["liot_full_vs_flat"] & cl["liot_real_vs_binary_ext"]
                        & cl["liot_full_vs_binary_ext"])
            ratios = {}
            for pol in ("liot_real", "liot_full"):
                h = ratio(*metric_arrays(blk, key(pol, sh, m), "harm"))
                hf = ratio(*metric_arrays(blk, flat, "harm"))
                with np.errstate(invalid="ignore", divide="ignore"):
                    ratios[pol] = dist(h / hf, fpos)
            rows.append({
                "m": m,
                "area_both_lower_than_flat_and_binary_opt": area(both, fpos),
                "area_liot_full_only_lower_than_flat_and_binary_opt": area(full_only, fpos),
                "area_both_lower_than_flat_and_binary_ext": area(both_ext, fpos),
                **{f"area_{k}": area(v, fpos) for k, v in cl.items()},
                "harm_ratio_vs_flat": ratios,
                "pooled_harm_diff_full_vs_flat": pooled(blk, key("liot_full", sh, m), flat, "harm", fpos),
                "pooled_harm_diff_real_vs_flat": pooled(blk, key("liot_real", sh, m), flat, "harm", fpos),
            })
        a0 = rows[0]["area_both_lower_than_flat_and_binary_opt"]["share"]
        bound = None
        for r_ in rows:
            if r_["area_both_lower_than_flat_and_binary_opt"]["share"] < 0.5 * a0:
                bound = r_["m"]
                break
        a_end = rows[-1]["area_both_lower_than_flat_and_binary_opt"]["share"]
        out[sh] = {"rows": rows, "m_star_area_halves": bound, "area_m0": a0, "area_m40": a_end,
                   "area_ratio_m40_over_m0": (a_end / a0 if a0 else None)}
    return out


# ── H2.5 ─────────────────────────────────────────────────────────────────────────────────
def h25(blk: Block, m: float = M_DEFAULT) -> dict[str, Any]:
    fpos = blk.p["f"] > 0
    out: dict[str, Any] = {}
    for sh in SHAPES:
        res: dict[str, Any] = {}
        for bname in ("binary_ext", "binary_opt"):
            kb = key(bname, sh, m)
            for lname in ("liot_real", "liot_full"):
                kl = key(lname, sh, m)
                row: dict[str, Any] = {}
                for met in ("harm", "help", "burden"):
                    d = paired(blk, kb, kl, met)
                    row[met] = {"pooled_binary_minus_liot": pooled(blk, kb, kl, met, fpos),
                                "areas": cls_areas(cls(d), fpos)}
                res[f"{bname}_vs_{lname}"] = row
        ks = np.array([r["res"][key("binary_opt", sh, m)]["k"] for r in blk.recs])
        res["binary_opt_chosen_k"] = {str(k): float(np.mean(ks == k)) for k in sorted(set(ks.tolist()))}
        out[sh] = res
    return out


# ── H2.6 across the registered device-weight sweep ───────────────────────────────────────
CLASSES = ("light", "climate", "cover", "appliance", "entry")
MULT_EDGES = (0.5, 0.5 * 2**0.5, 1.0, 2**0.5, 2.0000001)


def _rank(x: np.ndarray) -> np.ndarray:
    return np.argsort(np.argsort(x, kind="stable"), kind="stable").astype(float)


def first_order_sub(blk: Block, y: np.ndarray, names: list[str], nbin: int = 6) -> dict[str, float]:
    var = y.var()
    out = {}
    for nm in names:
        x = blk.p[nm]
        edges = np.quantile(x, np.linspace(0, 1, nbin + 1))
        b = np.clip(np.searchsorted(edges, x, side="right") - 1, 0, nbin - 1)
        groups = [np.where(b == i)[0] for i in range(nbin)]
        means = np.array([y[g].mean() for g in groups if g.size])
        w = np.array([g.size for g in groups if g.size], float)
        mu = (means * w).sum() / w.sum()
        out[nm] = float(((w * (means - mu) ** 2).sum() / w.sum()) / var) if var > 0 else 0.0
    return out


def silenced_sweep(blk: Block) -> dict[str, Any]:
    """Share of helpful proposals the proactive cap silences, over the device-weight sweep.

    The cap acts per action (the action's tier), so the share is a property of the helpful-proposal
    mix: which classes drive it, and how it moves with each class weight (0.5-2x).
    """
    helped = np.array([r["help_by_group"] for r in blk.recs], float)  # (sets, 5)
    silent = np.array([r["silenced_help_by_group"] for r in blk.recs], float)
    share = silent.sum(1) / np.maximum(1, helped.sum(1))
    out: dict[str, Any] = {
        "share_distribution_over_sets": {
            "min": float(share.min()), "p10": float(np.percentile(share, 10)),
            "p25": float(np.percentile(share, 25)), "median": float(np.median(share)),
            "p75": float(np.percentile(share, 75)), "p90": float(np.percentile(share, 90)),
            "max": float(share.max()), "pooled": float(silent.sum() / helped.sum()),
        },
        "classes": list(CLASSES),
        "helpful_mix_pooled": [float(x) for x in helped.sum(0) / helped.sum()],
        "silenced_fraction_within_class": [
            float(a / b) if b else None for a, b in zip(silent.sum(0), helped.sum(0), strict=True)],
        "contribution_to_total_silenced": [float(x) for x in silent.sum(0) / silent.sum()],
    }
    contrib = silent / np.maximum(1, silent.sum(1))[:, None]
    out["contribution_range_over_sets"] = {
        c: [float(contrib[:, i].min()), float(contrib[:, i].max())] for i, c in enumerate(CLASSES)}
    drivers: dict[str, Any] = {}
    for c in CLASSES:
        x = blk.p[f"mult_{c}"]
        lo_q, hi_q = np.quantile(x, [1 / 3, 2 / 3])
        drivers[c] = {
            "spearman_with_weight": float(np.corrcoef(_rank(x), _rank(share))[0, 1]),
            "mean_share_weight_low_third": float(share[x <= lo_q].mean()),
            "mean_share_weight_high_third": float(share[x > hi_q].mean()),
        }
    out["drivers"] = drivers
    out["first_order_index"] = first_order_sub(blk, share, [f"mult_{c}" for c in CLASSES])

    def grid(xn: str, yn: str) -> dict[str, Any]:
        x, y = blk.p[xn], blk.p[yn]
        g: list[list[float | None]] = []
        for j in range(len(MULT_EDGES) - 1):
            row: list[float | None] = []
            for i in range(len(MULT_EDGES) - 1):
                sel = ((x >= MULT_EDGES[i]) & (x < MULT_EDGES[i + 1])
                       & (y >= MULT_EDGES[j]) & (y < MULT_EDGES[j + 1]))
                row.append(float(share[sel].mean()) if sel.any() else None)
            g.append(row)
        return {"x": xn, "y": yn, "edges": [float(e) for e in MULT_EDGES[:-1]] + [2.0], "mean_share": g}

    out["region_map_entry_by_light"] = grid("mult_entry", "mult_light")
    out["region_map_entry_by_appliance"] = grid("mult_entry", "mult_appliance")
    return out


def p2_p3_separate(blk: Block) -> dict[str, Any]:
    """Harm, helpful completion and hazard response side by side (pooled differences vs flat)."""
    fpos = blk.p["f"] > 0
    out: dict[str, Any] = {}
    for sh in SHAPES:
        rows = []
        for m in M_GRID:
            flat = key("flat_explicit", sh, m)
            row: dict[str, Any] = {"m": m}
            for pol in ("liot_real", "liot_full"):
                d = {}
                for met in ("harm", "help", "hz_ok", "burden"):
                    pd_ = pooled(blk, key(pol, sh, m), flat, met, fpos)
                    d[met] = pd_
                row[pol] = d
            d = {}
            for met in ("harm", "help", "hz_ok"):
                pd_ = pooled(blk, key("liot_real", sh, m), key("liot_full", sh, m), met, fpos)
                d[met] = pd_
            row["liot_real_minus_liot_full"] = d
            rows.append(row)
        out[sh] = rows
    return out


# ── H2.6 / H2.7 ──────────────────────────────────────────────────────────────────────────
def h26(blk: Block, m: float = M_DEFAULT) -> dict[str, Any]:
    out: dict[str, Any] = {}
    n = blk.n
    for sh in SHAPES:
        kr, kf = key("liot_real", sh, m), key("liot_full", sh, m)
        lost = blk.total(kr, "bp") / np.maximum(1, n["n_help"].sum(1))
        blocked_harm = blk.total(kr, "bh") / np.maximum(1, n["n_harm"].sum(1))
        lost_n = blk.total(kr, "bp") / (blk.n_seeds * blk.days)
        blk_h_n = blk.total(kr, "bh") / (blk.n_seeds * blk.days)
        with np.errstate(invalid="ignore", divide="ignore"):
            exchange = blk_h_n / lost_n
        all_ = np.ones(blk.n_sets, bool)
        d_help = paired(blk, kr, kf, "help")
        d_harm = paired(blk, kr, kf, "harm")
        out[sh] = {
            "silenced_helpful_share": dist(lost, all_),
            "silenced_harmful_share": dist(blocked_harm, all_),
            "silenced_helpful_per_day": dist(lost_n, all_),
            "silenced_harmful_per_day": dist(blk_h_n, all_),
            "harmful_blocked_per_helpful_lost": dist(exchange, all_),
            "pooled_silenced_helpful_share": float(blk.total(kr, "bp").sum() / n["n_help"].sum()),
            "pooled_silenced_harm_share": float(blk.total(kr, "bh").sum() / n["n_harm"].sum()),
            "real_minus_full_help_completion": pooled(blk, kr, kf, "help", all_),
            "real_minus_full_harm": pooled(blk, kr, kf, "harm", all_),
            "areas_help_real_vs_full": cls_areas(cls(d_help), all_),
            "areas_harm_real_vs_full": cls_areas(cls(d_harm), all_),
        }
    return out


def h27(blk: Block, m: float = M_DEFAULT) -> dict[str, Any]:
    out: dict[str, Any] = {}
    n = blk.n
    fpos = blk.p["f"] > 0
    for sh in SHAPES:
        res: dict[str, Any] = {}
        for ref in ("flat_explicit", "liot_full", "liot_real"):
            kr = key(ref, sh, m)
            for w in (5, 15, 30):
                ku = key("auto_undo", sh, w=w)
                row: dict[str, Any] = {}
                for met in ("harm", "help"):
                    d = paired(blk, ku, kr, met)
                    row[met] = {"pooled_undo_minus_ref": pooled(blk, ku, kr, met, fpos),
                                "areas": cls_areas(cls(d), fpos)}
                hm_u = blk.total(ku, "hm") / np.maximum(1, n["n_harm"].sum(1))
                hm_r = blk.total(kr, "hm") / np.maximum(1, n["n_harm"].sum(1))
                row["harm_minutes_per_harmful"] = {
                    "undo": dist(hm_u, fpos), "ref": dist(hm_r, fpos),
                    "share_sets_undo_lower": area(hm_u < hm_r, fpos),
                }
                res[f"w{w}_vs_{ref}"] = row
        out[sh] = res
    return out


# ── Pareto ───────────────────────────────────────────────────────────────────────────────
def _pareto(points: np.ndarray) -> np.ndarray:
    """points (policies, objectives) minimised; True = not dominated."""
    n = points.shape[0]
    ok = np.ones(n, bool)
    for i in range(n):
        le = np.all(points <= points[i], axis=1)
        lt = np.any(points < points[i], axis=1)
        ok[i] = not np.any(le & lt)
    return ok


def policy_points(blk: Block, sh: str, m: float, three: bool = False,
                  gate: bool = False, units: bool = False) -> tuple[list[str], np.ndarray]:
    ks = {n_: key(n_, sh, m, gate=gate) for n_ in ("flat_explicit", "binary_ext", "binary_opt", "liot_real", "liot_full")}
    ks["auto_all"] = key("auto_all", sh, gate=gate)
    if not gate:
        ks["auto_undo"] = key("auto_undo", sh, w=15)
    names = list(ks)
    cols = []
    for n_ in names:
        b = (blk.total(ks[n_], "units") / (blk.n_seeds * blk.days) if units
             else ratio(*metric_arrays(blk, ks[n_], "burden")))
        h = ratio(*metric_arrays(blk, ks[n_], "harm"))
        cols.append([b, h] + ([1 - ratio(*metric_arrays(blk, ks[n_], "help"))] if three else []))
    return names, np.array(cols)  # (pol, obj, sets)


def pareto_block(blk: Block, sh: str, m: float, mask: np.ndarray, gate: bool = False) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for tag, three in (("2d_burden_harm", False), ("3d_burden_harm_helploss", True)):
        names, pts = policy_points(blk, sh, m, three, gate)
        flag = np.zeros((len(names), blk.n_sets), bool)
        for s in range(blk.n_sets):
            p = pts[:, :, s]
            if np.isnan(p).any():
                continue
            flag[:, s] = _pareto(p)
        out[tag] = {n_: area(flag[i], mask) for i, n_ in enumerate(names)}
        if not three:
            h = pts[:, 1, :]
            best = np.nanmin(h, axis=0)
            win = np.isclose(h, best[None, :], atol=1e-12)
            win = win / np.maximum(1, win.sum(0))[None, :]
            out["lowest_harm"] = {n_: area(win[i], mask) for i, n_ in enumerate(names)}
    names_u, pts_u = policy_points(blk, sh, m, False, gate, True)
    flag_u = np.zeros((len(names_u), blk.n_sets), bool)
    for s_i in range(blk.n_sets):
        p = pts_u[:, :, s_i]
        if not np.isnan(p).any():
            flag_u[:, s_i] = _pareto(p)
    out["2d_load_units_harm"] = {n_: area(flag_u[i], mask) for i, n_ in enumerate(names_u)}
    # with the theta sweeps as extra competitors (sigma 0 and 0.5)
    names, pts = policy_points(blk, sh, m, False, gate)
    for s_ in (0.0, 0.5):
        tk = [key("theta", sh, s=s_, t=t) for t in THETAS]
        if not all(blk.has(k) for k in tk):
            continue
        th = np.array([blk.seeds(k, "xh").sum(1) / np.maximum(1, blk.n["n_harm"].sum(1)) for k in tk])
        tb = np.array([[blk.seeds(k, "nc").sum(1) / (blk.n_seeds * blk.days), th[i]] for i, k in enumerate(tk)])
        dom_out: dict[str, Any] = {}
        for n_ in ("flat_explicit", "binary_opt", "liot_full"):
            i_ = names.index(n_)
            b_, h_ = pts[i_, 0], pts[i_, 1]
            dom = ((tb[:, 0] <= b_[None]) & (tb[:, 1] <= h_[None]) & ((tb[:, 0] < b_[None]) | (tb[:, 1] < h_[None]))).any(0)
            dom_out[n_] = area(dom, mask)
        out[f"dominated_by_a_theta_policy_sigma{s_:.1f}"] = dom_out
        flags = {n_: np.zeros(blk.n_sets, bool) for n_ in ("liot_real", "liot_full")}
        for s in range(blk.n_sets):
            allp = np.vstack([pts[:, :, s], tb[:, :, s]])
            if np.isnan(allp).any():
                continue
            ok = _pareto(allp)
            for n_ in flags:
                flags[n_][s] = ok[names.index(n_)]
        out[f"pareto_optimal_with_theta_sigma{s_:.1f}"] = {n_: area(v, mask) for n_, v in flags.items()}
    return out


def pareto_vs_m(blk: Block) -> dict[str, Any]:
    fpos = blk.p["f"] > 0
    out: dict[str, Any] = {}
    for sh in SHAPES:
        out[sh] = {}
        for m in M_GRID:
            r = pareto_block(blk, sh, m, fpos)
            out[sh][f"m{m:.1f}"] = {"pareto_2d": {k: v["share"] for k, v in r["2d_burden_harm"].items()},
                                    "lowest_harm": {k: v["share"] for k, v in r["lowest_harm"].items()}}
    return out


# ── sensitivity indices (binned first-order, Sobol first-order approximation) ───────────
def first_order(blk: Block, y: np.ndarray, nbin: int = 8) -> dict[str, float]:
    ok = ~np.isnan(y)
    var = y[ok].var()
    out = {}
    names = [p.name for p in PARAMS] + ["wsel"]
    for nm in names:
        x = blk.p[nm]
        if nm == "wsel":
            bins = x.astype(int)
            groups = [np.where(ok & (bins == b))[0] for b in range(3)]
        else:
            edges = np.quantile(x[ok], np.linspace(0, 1, nbin + 1))
            b = np.clip(np.searchsorted(edges, x, side="right") - 1, 0, nbin - 1)
            groups = [np.where(ok & (b == i))[0] for i in range(nbin)]
        means = np.array([y[g].mean() for g in groups if g.size])
        w = np.array([g.size for g in groups if g.size], float)
        mu = (means * w).sum() / w.sum()
        out[nm] = float(((w * (means - mu) ** 2).sum() / w.sum()) / var) if var > 0 else 0.0
    return out


def sensitivity(blk: Block, sh: str = "exp", m: float = M_DEFAULT) -> dict[str, Any]:
    flat = key("flat_explicit", sh, m)
    outs = {
        "harm_real_minus_flat": paired(blk, key("liot_real", sh, m), flat, "harm")["est"],
        "harm_full_minus_flat": paired(blk, key("liot_full", sh, m), flat, "harm")["est"],
        "help_full_minus_flat": paired(blk, key("liot_full", sh, m), flat, "help")["est"],
        "help_real_minus_flat": paired(blk, key("liot_real", sh, m), flat, "help")["est"],
        "harm_binary_opt_minus_full": paired(blk, key("binary_opt", sh, m), key("liot_full", sh, m), "harm")["est"],
    }
    res = {k: first_order(blk, v) for k, v in outs.items()}
    return {"note": "binned first-order index S_i = Var(E[Y|X_i])/Var(Y), 8 quantile bins, n=400 Latin hypercube sets; "
                    "an approximation of the Sobol first-order index, not a Saltelli estimate",
            "shape": sh, "m": m, "indices": res,
            "sum_first_order": {k: float(sum(v.values())) for k, v in res.items()}}


# ── headline for any block (main / half / double / gated) ────────────────────────────────
def headline(blk: Block, m: float = M_DEFAULT, gate: bool = False) -> dict[str, Any]:
    fpos = blk.p["f"] > 0
    out: dict[str, Any] = {"n_sets": blk.n_sets, "n_seeds": blk.n_seeds}
    for sh in SHAPES:
        flat = key("flat_explicit", sh, m, gate=gate)
        row: dict[str, Any] = {}
        for pol in ("liot_real", "liot_full", "binary_opt", "binary_ext"):
            c = cls(paired(blk, key(pol, sh, m, gate=gate), flat, "harm"))
            row[f"{pol}_vs_flat_harm_higher"] = area(c["higher"], fpos)["share"]
            row[f"{pol}_vs_flat_harm_lower"] = area(c["lower"], fpos)["share"]
        for pol in ("liot_real", "liot_full"):
            c = cls(paired(blk, key(pol, sh, m, gate=gate), key("binary_opt", sh, m, gate=gate), "harm"))
            row[f"{pol}_vs_binary_opt_harm_higher"] = area(c["higher"], fpos)["share"]
            row[f"{pol}_vs_binary_opt_harm_lower"] = area(c["lower"], fpos)["share"]
            ch = cls(paired(blk, key(pol, sh, m, gate=gate), flat, "help"))
            row[f"{pol}_vs_flat_help_higher"] = area(ch["higher"], fpos)["share"]
            row[f"{pol}_vs_flat_help_lower"] = area(ch["lower"], fpos)["share"]
            cz = cls(paired(blk, key(pol, sh, m, gate=gate), flat, "hz_ok"))
            row[f"{pol}_vs_flat_hazard_responded_higher"] = area(cz["higher"], fpos)["share"]
            row[f"{pol}_vs_flat_hazard_responded_lower"] = area(cz["lower"], fpos)["share"]
        if not gate and blk.has(key("theta", sh, s=0.5, t=0.5)):
            us = u_shape(blk, sh, 0.5, fpos)
            row["theta_u_shape_share_sigma0.5"] = us["u_shape_sig"]["share"]
            row["theta_monotone_share_sigma0.5"] = us["monotone_nonincreasing"]["share"]
        if not gate and blk.has(key("liot_real", sh, 0.4)):
            row["h24_area_m0"] = h24_one(blk, sh, 0.0, fpos)
            row["h24_area_m40"] = h24_one(blk, sh, 0.4, fpos)
        pt = pareto_block(blk, sh, m, fpos, gate)
        row["pareto_2d_share"] = {k: v["share"] for k, v in pt["2d_burden_harm"].items()}
        row["lowest_harm_share"] = {k: v["share"] for k, v in pt["lowest_harm"].items()}
        out[sh] = row
    n = blk.n
    kr = key("liot_real", "exp", m, gate=gate)
    out["silenced_helpful_share_real"] = float(blk.total(kr, "bp").sum() / n["n_help"].sum())
    out["silenced_harm_share_real"] = float(blk.total(kr, "bh").sum() / n["n_harm"].sum())
    return out


def h24_one(blk: Block, sh: str, m: float, mask: np.ndarray) -> float:
    flat, opt = key("flat_explicit", sh, m), key("binary_opt", sh, m)
    ok = np.ones(blk.n_sets, bool)
    for pol in ("liot_real", "liot_full"):
        kp = key(pol, sh, m)
        ok &= cls(paired(blk, kp, flat, "harm"))["lower"] & cls(paired(blk, kp, opt, "harm"))["lower"]
    return area(ok, mask)["share"]


def harm_alignment(blk: Block) -> dict[str, Any]:
    """Where the generator's harmful proposals land on LIOT's own tiers (no misclassification)."""
    ht = np.array([r["harm_by_tier"] for r in blk.recs], float)  # (sets, 4)
    hp = np.array([r["help_by_tier"] for r in blk.recs], float)
    pooled_ = ht.sum(0) / ht.sum()
    per_set = ht / np.maximum(1, ht.sum(1))[:, None]
    names = ("AMBIENT", "BREATH", "DUSK", "EMBER")
    return {
        "harmful_share_by_tier_pooled": dict(zip(names, (float(x) for x in pooled_), strict=True)),
        "harmful_share_by_tier_mean_over_sets": dict(zip(names, (float(x) for x in per_set.mean(0)), strict=True)),
        "harmful_share_dusk_or_ember_pooled": float(pooled_[2:].sum()),
        "harmful_share_dusk_or_ember_mean_over_sets": float(per_set[:, 2:].sum(1).mean()),
        "harmful_share_dusk_or_ember_quartiles_over_sets": [float(x) for x in np.percentile(per_set[:, 2:].sum(1), [25, 50, 75])],
        "helpful_share_by_tier_pooled": dict(zip(names, (float(x) for x in hp.sum(0) / hp.sum()), strict=True)),
        "note": "alignment is authored by generator.py (harm rules and templates), not measured from the world",
    }


def auc_summary(blk: Block) -> dict[str, Any]:
    keys = [k for k in blk.recs[0]["auc"] if k != "score_sd"]
    out: dict[str, Any] = {}
    for k in keys:
        v = np.array([r["auc"][k] if r["auc"][k] is not None else np.nan for r in blk.recs], float)
        v = v[~np.isnan(v)]
        out[k] = {"median": float(np.median(v)), "q25": float(np.percentile(v, 25)),
                  "q75": float(np.percentile(v, 75))} if v.size else None
    out["score_sd_median"] = float(np.median([r["auc"]["score_sd"] for r in blk.recs]))
    return out


def _realized(blk: Block) -> dict[str, float]:
    keys = ("help", "unw", "harm", "events_per_seed")
    return {k: float(np.mean([r["realized"][k] for r in blk.recs])) for k in keys}


def summarize(raw_dir: Path) -> dict[str, Any]:
    """Deterministic summary of raw/ (all-NaN warnings from sets with ~0 harmful proposals muted)."""
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", RuntimeWarning)
        return _summarize(raw_dir)


def _summarize(raw_dir: Path) -> dict[str, Any]:
    blocks = load(Path(raw_dir))
    if "main" not in blocks:
        return {"blocks": {}}
    main = blocks["main"]
    out: dict[str, Any] = {
        "blocks": {b: {"sets": v.n_sets, "seeds": v.n_seeds, "days": v.days} for b, v in sorted(blocks.items())},
        "default_m": M_DEFAULT,
        "realized_mix_main": _realized(main),
        "param_ranges": {p.name: [p.lo, p.hi] for p in PARAMS},
    }
    out["policy_table"] = {sh: {f"m{m:.1f}": policy_table(main, sh, m) for m in (0.0, M_DEFAULT, 0.4)} for sh in SHAPES}
    out["h2_1"] = {sh: {f"m{m:.1f}": strip_private(h21(main, sh, m)) for m in (0.0, M_DEFAULT, 0.4)} for sh in SHAPES}
    if "f0_g0" in blocks:
        out["h2_2_g0"] = strip_private(h22(blocks["f0_g0"]))
    if "f0" in blocks:
        out["h2_2"] = strip_private(h22(blocks["f0"]))
        out["policy_table_f0"] = {f"m{m:.1f}": policy_table(blocks["f0"], "na", m) for m in (0.0, M_DEFAULT)}
    out["h2_3"] = strip_private(h23(main))
    out["h2_4"] = h24(main)
    out["h2_5"] = {"m0.0": h25(main, 0.0), "m0.1": h25(main, M_DEFAULT)}
    out["h2_6"] = h26(main)
    out["harm_alignment"] = harm_alignment(main)
    out["auc_theta_and_tiers"] = auc_summary(main)
    out["h2_6_device_sweep"] = silenced_sweep(main)
    out["p2_p3_separate"] = p2_p3_separate(main)
    out["h2_7"] = h27(main)
    out["pareto"] = {sh: strip_private(pareto_block(main, sh, M_DEFAULT, main.p["f"] > 0)) for sh in SHAPES}
    out["pareto_vs_m"] = pareto_vs_m(main)
    out["sensitivity_indices"] = sensitivity(main)
    heads = {"main": headline(main)}
    for b in ("sens_half", "sens_dbl"):
        if b in blocks:
            heads[b] = headline(blocks[b])
    if "gated" in blocks:
        heads["gated_never_open_for_all"] = headline(blocks["gated"], gate=True)
    out["headline_by_block"] = heads
    from . import analyze_c2 as c2
    from . import sim as sim_mod

    c21_names = tuple(f"c21_p{round(p_ * 100):03d}" for p_ in sim_mod.P_LOW_GRID)
    if any(b in blocks for b in c21_names):
        out["c2_1"] = c2.c21(blocks, c21_names)
    amb_names = tuple(f"c211a_p{round(p_ * 100):03d}" for p_ in sim_mod.P_AMB_GRID)
    if any(b in blocks for b in amb_names):
        out["c2_11a"] = c2.c21(blocks, amb_names, upto=1)
    mult_names = tuple(f"c211b_x{round(x * 100):03d}" for x in sim_mod.R_LIGHT_MULTS)
    if any(b in blocks for b in mult_names):
        out["c2_11b"] = c2.c211b(blocks, mult_names, sim_mod.R_LIGHT_MULTS)
    if "c22" in blocks:
        out["c2_2"] = c2.c22(blocks["c22"])
    if "c23" in blocks:
        out["c2_3"] = c2.c23(blocks["c23"])
    if "c24" in blocks:
        out["c2_4"] = c2.c24(blocks["c24"])
    return strip_private(out)
