"""C4.13 analysis — reads raw/c413_*.jsonl only.

Metric (as in E3 `_lane`): harm rate of a policy in a set = executed harmful proposals / harmful proposals,
pooled over the set's seeds.  Differences are policy minus comparator, in percentage points when printed.
Weighted harm rate = sum_t w_t * executed_t / sum_t w_t * harmful_t  (same denominator for every policy).

Interval naming (registered, HYPOTHESES.md a73414c):
  * 씨앗 잡음     : resampling the 20 seeds inside every parameter set (the parameter sets stay fixed).
  * 세트 간 분포  : quartiles / bootstrap over the 400 parameter sets (assumption uncertainty is in neither).
The verdict rules below are fixed in the code before the full run.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import numpy as np
from scipy import stats

from experiments.common.io import read_jsonl
from experiments.e3_policy import generator

BOOT_SETS = 2000
BOOT_SEEDS = 300
SEED = 413
HARM_T = (0, 1, 2, 3, 4, 5, 6, 7, 8, 10, 12, 13)  # templates the coders rated
SHAPES = ("exp", "hyp")
MS = (0.0, 0.1)


def _arr(s: str) -> np.ndarray:
    return np.array([int(x) for x in s.split(",")], dtype=float)


class Blk:
    """One block as aligned arrays: sets x seeds (x templates)."""

    def __init__(self, recs: list[dict[str, Any]]) -> None:
        recs = sorted(recs, key=lambda r: r["set_id"])
        self.recs = recs
        self.n_sets = len(recs)
        self.n_seeds = recs[0]["n_seeds"]
        self.days = recs[0]["days"]
        self.p = {k: np.array([r["params"][k] for r in recs]) for k in recs[0]["params"]}
        self.n_harm = np.array([_arr(r["n"]["n_harm"]) for r in recs])
        self.n_help = np.array([_arr(r["n"]["n_help"]) for r in recs])
        self.mask = self.p["f_orig"] > 0  # E3's 'f > 0 region'
        self._c: dict[tuple[str, str], np.ndarray] = {}

    def lane(self, key: str, field: str) -> np.ndarray:
        ck = (key, field)
        if ck not in self._c:
            self._c[ck] = np.array([_arr(r["res"][key][field]) for r in self.recs])
        return self._c[ck]

    def total(self, key: str, field: str) -> np.ndarray:
        return np.array([float(r["res"][key][field]) for r in self.recs])

    def xh_t(self, key: str) -> np.ndarray:
        """(sets, seeds, 15) executed harm per template."""
        ck = (key, "xh_t")
        if ck not in self._c:
            out = np.zeros((self.n_sets, self.n_seeds, generator.N_PROP))
            for i, r in enumerate(self.recs):
                for t, s in r["res"][key]["xh_t"].items():
                    out[i, :, int(t)] = _arr(s)
            self._c[ck] = out
        return self._c[ck]

    def h_t(self) -> np.ndarray:
        ck = ("_", "h_t")
        if ck not in self._c:
            out = np.zeros((self.n_sets, self.n_seeds, generator.N_PROP))
            for i, r in enumerate(self.recs):
                for t, s in r["harm_by_template"].items():
                    out[i, :, int(t)] = _arr(s)
            self._c[ck] = out
        return self._c[ck]


def key(name: str, shape: str, m: float, ceil: bool = False) -> str:
    return "|".join([name, shape, f"m{m:.1f}"] + (["ceil-liot"] if ceil else []))


# ── intervals ───────────────────────────────────────────────────────────────
def _ci(x: np.ndarray, rng: np.random.Generator) -> list[float]:
    idx = rng.integers(0, x.size, size=(BOOT_SETS, x.size))
    means = x[idx].mean(axis=1)
    return [float(np.percentile(means, 2.5)), float(np.percentile(means, 97.5))]


def _dist(x: np.ndarray) -> dict[str, float]:
    return {"mean": float(x.mean()), "median": float(np.median(x)), "q25": float(np.percentile(x, 25)),
            "q75": float(np.percentile(x, 75)), "min": float(x.min()), "max": float(x.max()), "n": int(x.size)}


def _seed_noise_mean(num: np.ndarray, den: np.ndarray, mask: np.ndarray, rng: np.random.Generator) -> list[float]:
    """씨앗 잡음: mean over masked sets of sum(num)/sum(den), seeds resampled inside each set.

    num, den: (sets, seeds) or (sets, seeds, T) already contracted over T by the caller.
    """
    num, den = num[mask], den[mask]
    s = num.shape[1]
    idx = rng.integers(0, s, size=(BOOT_SEEDS, s))
    est = np.empty(BOOT_SEEDS)
    for b in range(BOOT_SEEDS):
        n_ = num[:, idx[b]].sum(axis=1)
        d_ = np.maximum(den[:, idx[b]].sum(axis=1), 1e-12)
        est[b] = float((n_ / d_).mean())
    return [float(np.percentile(est, 2.5)), float(np.percentile(est, 97.5))]


def _per_set_sig(num: np.ndarray, den: np.ndarray, rng: np.random.Generator) -> tuple[np.ndarray, np.ndarray]:
    """Per set: (lo, hi) of the seed-noise 95% interval of sum(num)/sum(den)."""
    s = num.shape[1]
    idx = rng.integers(0, s, size=(BOOT_SEEDS, s))
    est = np.stack([num[:, idx[b]].sum(axis=1) / np.maximum(den[:, idx[b]].sum(axis=1), 1e-12)
                    for b in range(BOOT_SEEDS)], axis=1)
    return np.percentile(est, 2.5, axis=1), np.percentile(est, 97.5, axis=1)


def _kappa_quadratic(a: np.ndarray, b: np.ndarray, cats: int) -> float | None:
    a, b = a.astype(int) - 1, b.astype(int) - 1
    o = np.zeros((cats, cats))
    for x, y in zip(a, b, strict=True):
        o[x, y] += 1
    o /= o.sum()
    e = np.outer(o.sum(1), o.sum(0))
    w = np.array([[(i - j) ** 2 for j in range(cats)] for i in range(cats)], float) / (cats - 1) ** 2
    den = float((w * e).sum())
    return None if den == 0 else float(1.0 - (w * o).sum() / den)


# ── severity ────────────────────────────────────────────────────────────────
def _coder_tables(rows: list[dict[str, Any]]) -> dict[str, Any]:
    by: dict[str, dict[int, dict[str, Any]]] = {}
    for r in rows:
        by.setdefault(r["coder"], {})[int(r["template"])] = r
    out: dict[str, Any] = {"coders": sorted(by)}
    comp: dict[str, np.ndarray] = {}
    for c, d in by.items():
        v = np.array([d[t]["injury"] * d[t]["reversibility"] * d[t]["duration"] for t in HARM_T], float)
        comp[c] = v
    out["composite_by_coder"] = {c: {generator.NAME[t]: float(comp[c][i]) for i, t in enumerate(HARM_T)}
                                 for c in comp}
    if len(by) == 2:
        ca, cb = sorted(by)
        agree: dict[str, Any] = {}
        for dim, cats in (("injury", 5), ("reversibility", 4), ("duration", 4)):
            x = np.array([by[ca][t][dim] for t in HARM_T])
            y = np.array([by[cb][t][dim] for t in HARM_T])
            agree[dim] = {"quadratic_weighted_kappa": _kappa_quadratic(x, y, cats),
                          "exact_agreement": float((x == y).mean()),
                          "spearman": float(stats.spearmanr(x, y).statistic) if x.std() and y.std() else None}
        agree["composite_spearman"] = float(stats.spearmanr(comp[ca], comp[cb]).statistic)
        out["agreement"] = agree
    norm = {c: v / v.mean() for c, v in comp.items()}
    consensus = np.mean([norm[c] for c in sorted(norm)], axis=0)
    consensus = consensus / consensus.mean()
    out["consensus_weight"] = {generator.NAME[t]: float(consensus[i]) for i, t in enumerate(HARM_T)}
    out["_weights"] = {
        "uniform": np.ones(len(HARM_T)),
        "coder": consensus,
        "coder_shrunk_half": 0.5 * consensus + 0.5,
        **{f"{c}_only": norm[c] for c in sorted(norm)},
        **{f"{c}_only_shrunk_half": 0.5 * norm[c] + 0.5 for c in sorted(norm)},
    }
    out["_consensus"] = consensus
    return out


def _tier_measure(blks: list[Blk]) -> dict[int, float]:
    """Mean LIOT tier of each template's harmful proposals, pooled over the given blocks (c413a + c413b_base:
    c413b adds the three templates that are harmful only in the C2.1 setting)."""
    hist = np.sum([np.array(r["harm_tier_hist_by_template"], float) for b in blks for r in b.recs], axis=0)
    return {t: float((hist[t] * np.arange(4)).sum() / hist[t].sum()) for t in HARM_T if hist[t].sum() > 0}


# ── (a) severity-weighted deficit ──────────────────────────────────────────
def _weighted_rows(blk: Blk, sev: dict[str, Any], rng: np.random.Generator) -> dict[str, Any]:
    h_t = blk.h_t()[:, :, list(HARM_T)]
    other = np.delete(np.arange(generator.N_PROP), list(HARM_T))
    assert blk.h_t()[:, :, other].sum() == 0, "harmful proposals exist on a template the coders did not rate"
    out: dict[str, Any] = {}
    for sh in SHAPES:
        for m in MS:
            kr, kc = key("liot_real", sh, m), key("flat_explicit", sh, m, True)
            xr, xc = blk.xh_t(kr)[:, :, list(HARM_T)], blk.xh_t(kc)[:, :, list(HARM_T)]
            cell: dict[str, Any] = {}
            d_count = None
            for wname, w in sev["_weights"].items():
                num = ((xr - xc) * w).sum(axis=2)
                den = (h_t * w).sum(axis=2)
                per_set = num.sum(axis=1) / np.maximum(den.sum(axis=1), 1e-12)
                pm = per_set[blk.mask]
                row = {"mean_diff": float(pm.mean()), "set_dist": _dist(pm),
                       "ci95_across_sets": _ci(pm, rng),
                       "ci95_seed_noise": _seed_noise_mean(num, den, blk.mask, rng),
                       "share_sets_liot_real_higher": float((pm > 0).mean())}
                if wname == "uniform":
                    d_count = per_set
                    # E3 identity: uniform-weight rate equals executed/harmful
                    row["equals_count_rate_check"] = float(np.abs(
                        per_set - (blk.lane(kr, "xh").sum(1) - blk.lane(kc, "xh").sum(1)) / blk.n_harm.sum(1)
                    ).max())
                else:
                    assert d_count is not None
                    half = per_set - 0.5 * d_count  # <0: deficit shrank to less than half
                    hm = half[blk.mask]
                    lo, hi = _ci(hm, rng)
                    row["ratio_of_means_to_count_deficit"] = float(pm.mean() / (d_count[blk.mask].mean()))
                    row["minus_half_count_deficit"] = {"mean": float(hm.mean()), "ci95_across_sets": [lo, hi]}
                    row["verdict_rule_result"] = ("지지(절반 이하)" if hi < 0 else
                                                  "반증(절반 넘게 남음)" if lo > 0 else "판정 불가(구간이 절반선을 걸침)")
                cell[wname] = row
            out[f"{sh}|m{m:.1f}"] = cell
    return out


def _context_numbers(blk: Blk) -> dict[str, Any]:
    """Help and burden of the matched-ceiling pair (E3 C2.2 N1 comparison), per shape and m."""
    out: dict[str, Any] = {}
    for sh in SHAPES:
        for m in MS:
            kr, kc = key("liot_real", sh, m), key("flat_explicit", sh, m, True)
            mk = blk.mask
            hr = blk.lane(kr, "xp").sum(1) / np.maximum(blk.n_help.sum(1), 1)
            hc = blk.lane(kc, "xp").sum(1) / np.maximum(blk.n_help.sum(1), 1)
            br = blk.total(kr, "nc") / (blk.n_seeds * blk.days)
            bc = blk.total(kc, "nc") / (blk.n_seeds * blk.days)
            xr = blk.lane(kr, "xh").sum(1) / np.maximum(blk.n_harm.sum(1), 1)
            xc = blk.lane(kc, "xh").sum(1) / np.maximum(blk.n_harm.sum(1), 1)
            out[f"{sh}|m{m:.1f}"] = {
                "harm_liot_real": float(xr[mk].mean()), "harm_flat_ceiling": float(xc[mk].mean()),
                "help_liot_real": float(hr[mk].mean()), "help_flat_ceiling": float(hc[mk].mean()),
                "help_diff": float((hr - hc)[mk].mean()),
                "confirmations_per_day_liot_real": float(br[mk].mean()),
                "confirmations_per_day_flat_ceiling": float(bc[mk].mean()),
            }
    return out


def _severity_units(blk: Blk, sev: dict[str, Any]) -> dict[str, Any]:
    """Severity-units per day (coder weights, unnormalised) for the compared arms, exp, m=0.1."""
    w = sev["_weights"]["coder"]
    out = {}
    for name, ceil in (("liot_real", False), ("flat_explicit_ceil", True), ("liot_full", False), ("flat_explicit", False)):
        base = "flat_explicit" if name.startswith("flat") else name
        k = key(base, "exp", 0.1, ceil)
        x = (blk.xh_t(k)[:, :, list(HARM_T)] * w).sum(axis=2)
        out[name] = float((x.sum(1) / (blk.n_seeds * blk.days))[blk.mask].mean())
    return out


# ── (b) burden off ─────────────────────────────────────────────────────────
def _burden_rows(blocks: dict[str, Blk], rng: np.random.Generator) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for var in ("base", "f0", "g0", "f0g0"):
        blk = blocks.get(f"c413b_{var}")
        if blk is None:
            continue
        for m in MS:
            kr, ka, kac = key("liot_real", "exp", m), key("flat_explicit", "exp", m), key("flat_explicit", "exp", m, True)
            kf = key("liot_full", "exp", m)
            nh = blk.n_harm
            cell: dict[str, Any] = {}
            for rname, kb in (("R1_vs_flat_explicit", ka), ("R2_vs_flat_explicit_same_ceiling", kac)):
                for pname, kp in (("liot_real", kr), ("liot_full", kf)):
                    num = blk.lane(kp, "xh") - blk.lane(kb, "xh")
                    per_set = num.sum(1) / np.maximum(nh.sum(1), 1)
                    pm = per_set[blk.mask]
                    adv = -pm
                    lo, hi = _ci(adv, rng)
                    cell[f"{pname}|{rname}"] = {
                        "advantage_mean": float(adv.mean()), "advantage_set_dist": _dist(adv),
                        "advantage_ci95_across_sets": [lo, hi],
                        "advantage_ci95_seed_noise": [-x for x in _seed_noise_mean(num, nh, blk.mask, rng)][::-1],
                        "share_sets_policy_lower_harm": float((pm < 0).mean()),
                        "rule": ("지지(이점 ≤1%p)" if hi <= 0.01 else "반증(이점 ≥2%p)" if lo >= 0.02
                                 else "판정 불가(1~2%p 구간 또는 구간이 걸침)")
                        + ("" if rname.startswith("R1") else " [사후 기준: R2 는 결과를 본 뒤 더한 읽기]"),
                    }
            helpd = (blk.lane(kr, "xp").sum(1) / np.maximum(blk.n_help.sum(1), 1)
                     - blk.lane(ka, "xp").sum(1) / np.maximum(blk.n_help.sum(1), 1))[blk.mask]
            cell["liot_real_help_diff_vs_flat_explicit_mean"] = float(helpd.mean())
            cell["confirmations_per_day"] = {
                nm: float((blk.total(k, "nc") / (blk.n_seeds * blk.days))[blk.mask].mean())
                for nm, k in (("liot_real", kr), ("flat_explicit", ka), ("flat_explicit_ceil", kac))}
            cell["silenced_harm_share_liot_real"] = float((blk.total(kr, "bh") / np.maximum(nh.sum(1), 1))[blk.mask].mean())
            out[f"{var}|m{m:.1f}"] = cell
    return out


# ── (c) H2.2 diagnosis ─────────────────────────────────────────────────────
def _sig_row(blk: Blk, kp: str, kb: str, rng: np.random.Generator) -> dict[str, Any]:
    num = blk.lane(kp, "xh") - blk.lane(kb, "xh")
    den = blk.n_harm
    est = num.sum(1) / np.maximum(den.sum(1), 1)
    lo, hi = _per_set_sig(num, den, rng)
    mk = blk.mask
    ex0 = (blk.lane(kp, "xh").sum(1) == blk.lane(kb, "xh").sum(1))
    return {"mean_diff": float(est[mk].mean()), "set_dist": _dist(est[mk]),
            "share_exactly_zero": float(ex0[mk].mean()),
            "share_policy_sig_higher": float((lo > 0)[mk].mean()),  # comparator sig lower
            "share_policy_sig_lower": float((hi < 0)[mk].mean())}


def _h22_rows(blocks: dict[str, Blk], rng: np.random.Generator) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for var in ("f0r0", "f0r0g0"):
        blk = blocks.get(f"c413c_{var}")
        if blk is None:
            continue
        # f = 0 and r = 0 for every set: E3's 'f > 0' mask would be empty, use all sets
        blk.mask = np.ones(blk.n_sets, dtype=bool)
        for m in MS:
            ka, kac = key("flat_explicit", "exp", m), key("flat_explicit", "exp", m, True)
            cell: dict[str, Any] = {}
            for pname in ("liot_full", "liot_real"):
                kp = key(pname, "exp", m)
                cell[f"{pname}_vs_flat_explicit(registered)"] = _sig_row(blk, kp, ka, rng)
                cell[f"{pname}_vs_flat_explicit_same_ceiling(사후)"] = _sig_row(blk, kp, kac, rng)
            sil = _sig_row(blk, kac, ka, rng)
            cell["decomposition"] = {
                "silence_effect_(flat_ceiling_minus_flat)": sil["mean_diff"],
                "structure_effect_(liot_real_minus_flat_ceiling)": cell["liot_real_vs_flat_explicit_same_ceiling(사후)"]["mean_diff"],
                "total_(liot_real_minus_flat)": cell["liot_real_vs_flat_explicit(registered)"]["mean_diff"],
                "share_of_harmful_proposals_silenced_by_liot_real": float(
                    (blk.total(key("liot_real", "exp", m), "bh") / np.maximum(blk.n_harm.sum(1), 1)).mean()),
            }
            out[f"{var}|m{m:.1f}"] = cell
    return out


def _h21_matched(blk: Blk, rng: np.random.Generator) -> dict[str, Any]:
    """H2.1 re-judged with the same proactive ceiling on the comparator (사후 기준)."""
    out: dict[str, Any] = {}
    for sh in SHAPES:
        for m in MS:
            kr = key("liot_real", sh, m)
            row = {"vs_flat_explicit(registered form, unmatched)": _sig_row(blk, kr, key("flat_explicit", sh, m), rng),
                   "vs_flat_explicit_same_ceiling(사후, matched)": _sig_row(blk, kr, key("flat_explicit", sh, m, True), rng)}
            for v in row.values():
                v["h2_1_falsifier_met_(policy_sig_higher_share_gt_0.5)"] = bool(v["share_policy_sig_higher"] > 0.5)
            out[f"{sh}|m{m:.1f}"] = row
    return out


def _repro(raw: Path) -> dict[str, Any]:
    rows = read_jsonl(raw / "c413_repro.jsonl")
    return {"n_sets_compared": len(rows), "n_all_equal": int(sum(bool(r["all_equal"]) for r in rows)),
            "n_lanes_compared": int(sum(r["n_lanes"] for r in rows)),
            "by_block": {b: {"sets": sum(1 for r in rows if r["block"] == b),
                             "all_equal": sum(1 for r in rows if r["block"] == b and r["all_equal"])}
                         for b in sorted({r["block"] for r in rows})}}


def summarize(raw: Path) -> dict[str, Any]:
    raw = Path(raw)
    by: dict[str, list[dict[str, Any]]] = {}
    for r in read_jsonl(raw / "c413_runs.jsonl"):
        by.setdefault(r["block"], []).append(r)
    blocks = {b: Blk(v) for b, v in by.items()}
    rng = np.random.default_rng(SEED)
    out: dict[str, Any] = {"blocks": {b: {"n_sets": k.n_sets, "n_seeds": k.n_seeds, "days": k.days,
                                         "n_sets_f_gt_0": int(k.mask.sum())} for b, k in blocks.items()},
                           "reproduction_vs_E3_raw": _repro(raw)}
    sev_rows = read_jsonl(raw / "c413_severity_coders.jsonl")
    if "c413a" in blocks and sev_rows:
        a = blocks["c413a"]
        sev = _coder_tables(sev_rows)
        tier = _tier_measure([a] + ([blocks["c413b_base"]] if "c413b_base" in blocks else []))
        ts = [t for t in HARM_T if t in tier]
        rho = stats.spearmanr(sev["_consensus"][[HARM_T.index(t) for t in ts]], [tier[t] for t in ts])
        circular = bool(rho.statistic > 0.8)
        out["c413a"] = {
            "coders": {k: v for k, v in sev.items() if not k.startswith("_")},
            "severity_vs_liot_tier_spearman": {"rho": float(rho.statistic), "p": float(rho.pvalue), "n_templates": len(ts),
                                               "tier_measure": "mean LIOT tier of the template's harmful proposals (0 ambient .. 3 ember), pooled over sets of c413a and c413b_base",
                                               "per_template_mean_tier": {generator.NAME[t]: tier[t] for t in ts},
                                               "circular_if_gt_0.8": circular},
            "weighted_deficit": _weighted_rows(a, sev, rng),
            "matched_ceiling_context": _context_numbers(a),
            "severity_units_per_day_coder_weights_exp_m0.1": _severity_units(a, sev),
            "h2_1_matched_posthoc": _h21_matched(a, rng),
            "harmful_proposal_share_by_template": {
                generator.NAME[t]: float(a.h_t()[:, :, t].sum() / a.h_t().sum()) for t in HARM_T},
        }
    out["c413b"] = _burden_rows(blocks, rng)
    out["c413c_h2_2"] = _h22_rows(blocks, rng)
    return out
