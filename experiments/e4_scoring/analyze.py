"""E4 analysis: reads only raw/, deterministic (verify recomputes it).

No independent ground truth exists for risk levels.  The production table is used as a
*comparison anchor* (how often a method differs from it), never as the answer.  The primary
results are label-free axiom counts that apply to every method identically.
"""

from __future__ import annotations

import itertools
from pathlib import Path
from typing import Any

import numpy as np
from scipy import stats

from experiments.common.io import read_jsonl
from experiments.e4_scoring import methods as M

SEED = 4
BOOT = 2000
AGREE90 = 22  # >= 22 of 24 inputs equal to the table  (= 91.7%; 21/24 = 87.5% is below 90%)
SEV_NAMES = ("record", "notice", "warning", "critical")
TIER_NAMES = ("ambient", "breath", "dusk", "ember")
OMNI = "jev-omni-q4"


# ── helpers ──────────────────────────────────────────────────────────────────
def _ci_prop(k: int, n: int) -> list[float] | None:
    """Exact (Clopper-Pearson) 95% interval for k of n."""
    if n == 0:
        return None
    ci = stats.binomtest(int(k), int(n)).proportion_ci(confidence_level=0.95, method="exact")
    return [float(ci.low), float(ci.high)]


def _boot_share(k: int, n: int, rng: np.random.Generator) -> list[float] | None:
    """Percentile bootstrap 95% interval for a proportion over n settings."""
    if n == 0:
        return None
    p = k / n
    draws = rng.binomial(n, p, size=BOOT) / n
    return [float(np.percentile(draws, 2.5)), float(np.percentile(draws, 97.5))]


def _stat(a: np.ndarray) -> dict[str, float]:
    return {
        "min": float(np.min(a)),
        "p25": float(np.percentile(a, 25)),
        "median": float(np.median(a)),
        "p75": float(np.percentile(a, 75)),
        "max": float(np.max(a)),
        "mean": float(np.mean(a)),
    }


def _axiom_row(levels: np.ndarray, dx: np.ndarray, dy: np.ndarray) -> dict[str, Any]:
    """Axiom counts for one level vector over a universe with dominance pairs (dx, dy)."""
    lv = np.asarray(levels, dtype=int)
    classes = {int(v): int((lv == v).sum()) for v in np.unique(lv)}
    n_dom = int(dx.size)
    viol = int((lv[dx] < lv[dy]).sum()) if n_dom else 0
    tie = int((lv[dx] == lv[dy]).sum()) if n_dom else 0
    return {
        "n_inputs": int(lv.size),
        "n_dominance_pairs": n_dom,
        "mono_violations": viol,
        "tie_loss": tie,
        "tie_loss_share": (tie / n_dom) if n_dom else None,
        "levels_used": int(len(classes)),
        "class_sizes": {str(k): v for k, v in sorted(classes.items())},
        "pairs_strictly_ordered_share": ((n_dom - viol - tie) / n_dom) if n_dom else None,
    }


def _cat_dominance(cats: list[dict[str, Any]]) -> tuple[np.ndarray, np.ndarray]:
    """Strict dominance among the 8 safety categories by their (damage, time) cell."""
    xs: list[int] = []
    ys: list[int] = []
    coord = [(M.HARM_ORDER.index(c["damage"]), M.TIME_ORDER.index(c["time"])) for c in cats]
    for i, a in enumerate(coord):
        for j, b in enumerate(coord):
            if i != j and a != b and a[0] >= b[0] and a[1] >= b[1]:
                xs.append(i)
                ys.append(j)
    return np.array(xs, dtype=int), np.array(ys, dtype=int)


def _unrank_levels(scores: np.ndarray, u: float) -> np.ndarray | None:
    th = M.pick_triplet(scores, u)
    if th is None:
        return None
    return M.levels_from_thresholds(scores, th[None, :])[0]


def _apply_resident_plus_one(levels: np.ndarray) -> np.ndarray:
    """M2p1: the vulnerable inputs take the plain level +1 (cap 3), as production does."""
    out = np.array(levels, dtype=int, copy=True)
    vmask = np.array([c[2] == 1 for c in M.COMBOS])
    out[..., vmask] = np.minimum(out[..., vmask] + 1, 3)
    return out


def _eval_grid_record(rec: dict[str, Any], variant: str) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """(levels (m,24), thresholds (m,3), scores (24,)) for every threshold triplet."""
    three = variant == "M3"
    scores = M.mult_scores(
        rec["harm_code"], rec["time_code"], rec["vuln_k"], rec["weights"], three_factor=three
    )
    th = M.triplet_thresholds(scores)
    if th.shape[0] == 0:
        return np.zeros((0, M.N_COMBOS), dtype=int), th, scores
    lv = M.levels_from_thresholds(scores, th)
    if not three:
        lv = _apply_resident_plus_one(lv)
    return lv, th, scores


# ── main ─────────────────────────────────────────────────────────────────────
def summarize(raw_dir: Path) -> dict[str, Any]:
    raw = Path(raw_dir)
    uni_rows = read_jsonl(raw / "universe.jsonl")
    if not uni_rows:
        return {"error": "no universe.jsonl"}
    uni = uni_rows[0]
    cells = {(c["damage"], c["time"]): int(c["level"]) for c in uni["table_cells"]}
    ref = M.production_cell_levels(cells)
    cats: list[dict[str, Any]] = uni["categories"]
    dx, dy = M.dominance_pairs()

    cat_plain = np.array([M.category_indices([c])[c["category"]][0] for c in cats])
    cat_vuln = np.array([M.category_indices([c])[c["category"]][1] for c in cats])
    cat_idx = np.concatenate([cat_plain, cat_vuln])
    ref_cat = np.concatenate(
        [[c["floor_plain"] for c in cats], [c["floor_vuln"] for c in cats]]
    ).astype(int)
    assert np.array_equal(ref[cat_idx], ref_cat), "table levels disagree with category floors"
    critical_idx = np.array([cat_plain[i] for i, c in enumerate(cats) if c["floor_plain"] == 3])
    cdx, cdy = _cat_dominance(cats)

    out: dict[str, Any] = {
        "note": (
            "No independent ground truth exists for risk levels. The production table is only "
            "a comparison anchor. Primary results are label-free axiom counts."
        ),
        "universe": {
            "n_inputs": M.N_COMBOS,
            "harm_order_low_to_high": list(M.HARM_ORDER),
            "time_order_low_to_high": list(M.TIME_ORDER),
            "n_dominance_pairs": int(dx.size),
            "combo_names": [M.combo_name(i) for i in range(M.N_COMBOS)],
            "table_levels": ref.tolist(),
            "categories": {
                c["category"]: {
                    "cell": f"{c['damage']}/{c['time']}",
                    "floor_plain": c["floor_plain"],
                    "floor_vuln": c["floor_vuln"],
                    "push_priority_plain": c["push_priority_plain"],
                    "gate1_tier_floor": c["gate1_tier_floor"],
                }
                for c in cats
            },
            "n_categories_cells_used": len({(c["damage"], c["time"]) for c in cats}),
            "n_table_cells": len(cells),
        },
    }
    out["table"] = _axiom_row(ref, dx, dy)
    out["table"]["compression"] = _table_compression(cells)

    # ── multiplicative: exhaustive grid ──────────────────────────────────────
    grid = read_jsonl(raw / "codings_grid.jsonl")
    out["mult_grid"] = {}
    best_overall: dict[str, Any] = {}
    grid_score_vectors: list[np.ndarray] = []
    min_k = min((r["vuln_k"] for r in grid), default=None)
    for variant in ("M3", "M2p1"):
        recs = [r for r in grid if variant == "M3" or r["vuln_k"] == min_k]
        agg, best = _grid_aggregate(recs, variant, ref, dx, dy, cat_idx, ref_cat, critical_idx)
        out["mult_grid"][variant] = agg
        best_overall[variant] = best
        if variant == "M3":
            grid_score_vectors = best.pop("_scores")
        else:
            best.pop("_scores", None)

    # ── multiplicative: random codings ───────────────────────────────────────
    rnd = read_jsonl(raw / "codings_random.jsonl")
    out["mult_random"] = _random_aggregate(rnd, ref, dx, dy, cat_idx, ref_cat, critical_idx)

    # ── coding dependence of incomparable pairs (Cox 2008 type) ──────────────
    out["order_flips"] = _order_flips(grid_score_vectors, rnd, dx, dy)

    # ── naive default mult (lin x lin, vuln x2, equal-width cuts) ────────────
    naive = _naive_levels()
    out["mult_naive_default"] = {
        "description": "harm 1-2-3-4, time 1-2-3, vulnerable x2, four equal-width score bins",
        "levels": naive.tolist(),
        **_axiom_row(naive, dx, dy),
        "agree_with_table_n": int((naive == ref).sum()),
        "below_table_n": int((naive < ref).sum()),
        "above_table_n": int((naive > ref).sum()),
        "category_below_table_n": int((naive[cat_idx] < ref_cat).sum()),
        "category_above_table_n": int((naive[cat_idx] > ref_cat).sum()),
        "critical_categories_below_critical": int((naive[critical_idx] < 3).sum()),
    }

    # ── single tier ──────────────────────────────────────────────────────────
    out["single_tier"] = _single_tier(cats, cdx, cdy)

    # ── model only ───────────────────────────────────────────────────────────
    out["model_only"] = _model_only(read_jsonl(raw / "model_only.jsonl"), cats, cdx, cdy)

    # ── disagreement matrices ────────────────────────────────────────────────
    methods24: dict[str, np.ndarray] = {
        "table": ref,
        "mult_M3_best": np.array(best_overall["M3"]["levels"]) if best_overall["M3"] else ref,
        "mult_M2p1_best": np.array(best_overall["M2p1"]["levels"]) if best_overall["M2p1"] else ref,
        "mult_naive": naive,
    }
    out["disagreement_u24"] = _matrix(methods24, exact=True)
    out["best_case"] = {
        k: {kk: vv for kk, vv in v.items() if kk != "_scores"} for k, v in best_overall.items()
    }
    methods8 = {name: lv[cat_plain] for name, lv in methods24.items()}
    methods8["single_tier(all ember)"] = np.array([c["gate1_tier_floor_rank"] for c in cats])
    for mname, vecs in out["model_only"].pop("_category_vectors").items():
        methods8[mname] = np.array(vecs)
    out["disagreement_u8_categories"] = _matrix(methods8, exact=False)
    out["disagreement_u8_categories"]["items"] = [c["category"] for c in cats]
    return out


def _table_compression(cells: dict[tuple[str, str], int]) -> dict[str, Any]:
    """How the production table merges (damage, time) cells into levels (12 cells)."""
    by_level: dict[int, list[str]] = {}
    for (d, t), lvl in sorted(cells.items()):
        by_level.setdefault(int(lvl), []).append(f"{d}/{t}")
    pairs_life_prop = 0
    merged_life_prop = 0
    for (d1, _t1), l1 in cells.items():
        for (d2, _t2), l2 in cells.items():
            if d1 == "life" and d2 == "property":
                pairs_life_prop += 1
                merged_life_prop += int(l1 == l2)
    return {
        "cells_per_level": {SEV_NAMES[k]: v for k, v in sorted(by_level.items())},
        "life_vs_property_cell_pairs": pairs_life_prop,
        "life_vs_property_cell_pairs_same_level": merged_life_prop,
    }


def _naive_levels() -> np.ndarray:
    s = M.mult_scores([1, 2, 3, 4], [1, 2, 3], 2.0, (1.0, 1.0, 1.0), three_factor=True)
    lo, hi = float(s.min()), float(s.max())
    th = np.array([[lo + (hi - lo) * q / 4 for q in (1, 2, 3)]])
    return M.levels_from_thresholds(s, th)[0]


def _grid_aggregate(
    recs: list[dict[str, Any]],
    variant: str,
    ref: np.ndarray,
    dx: np.ndarray,
    dy: np.ndarray,
    cat_idx: np.ndarray,
    ref_cat: np.ndarray,
    critical_idx: np.ndarray,
) -> tuple[dict[str, Any], dict[str, Any]]:
    n_settings = 0
    n_codings = 0
    sums = {
        "mono_gt0": 0,
        "agree90": 0,
        "agree_all24": 0,
        "cat_agree_all": 0,
        "critical_under_any": 0,
        "cat_any_below": 0,
        "cat_any_above": 0,
        "tie_loss_gt_table": 0,
        "levels_lt4": 0,
    }
    table_tie = int((ref[dx] == ref[dy]).sum())
    agree_hist: dict[int, int] = {}
    tie_loss_all: list[np.ndarray] = []
    by_family: dict[str, dict[str, Any]] = {}
    best: dict[str, Any] = {}
    best_agree = -1
    best_tie = 10**9
    best_count = 0
    score_vectors: list[np.ndarray] = []
    min_tie: dict[str, Any] = {}
    min_tie_val = 10**9
    for rec in recs:
        lv, th, scores = _eval_grid_record(rec, variant)
        if lv.shape[0] == 0:
            continue
        n_codings += 1
        score_vectors.append(scores)
        met = M.metrics(lv, ref, dx, dy, cat_idx, ref_cat, critical_idx)
        m = lv.shape[0]
        n_settings += m
        agree_n = (lv == ref[None, :]).sum(axis=1)
        sums["mono_gt0"] += int((met["mono_violations"] > 0).sum())
        sums["agree90"] += int((agree_n >= AGREE90).sum())
        sums["agree_all24"] += int((agree_n == M.N_COMBOS).sum())
        sums["cat_agree_all"] += int((met["cat_agree"] == 1.0).sum())
        sums["critical_under_any"] += int((met["critical_cat_under"] > 0).sum())
        sums["cat_any_below"] += int((met["cat_below"] > 0).sum())
        sums["cat_any_above"] += int((met["cat_above"] > 0).sum())
        sums["tie_loss_gt_table"] += int((met["tie_loss"] > table_tie).sum())
        sums["levels_lt4"] += int((met["levels_used"] < 4).sum())
        for a, c in zip(*np.unique(agree_n, return_counts=True), strict=True):
            agree_hist[int(a)] = agree_hist.get(int(a), 0) + int(c)
        tie_loss_all.append(met["tie_loss"])
        fam = f"{rec['harm_family']}|{rec['time_family']}"
        f = by_family.setdefault(fam, {"settings": 0, "max_agree": 0, "agree90": 0})
        f["settings"] += m
        f["max_agree"] = max(f["max_agree"], int(agree_n.max()))
        f["agree90"] += int((agree_n >= AGREE90).sum())
        jt = int(np.argmin(met["tie_loss"]))
        if int(met["tie_loss"][jt]) < min_tie_val:
            min_tie_val = int(met["tie_loss"][jt])
            min_tie = _best_record(rec, lv[jt], th[jt], ref, scores, variant)
        # best case: max agreement, then min tie loss, deterministic first
        j = int(np.lexsort((met["tie_loss"], -agree_n))[0])
        cand = (int(agree_n[j]), int(met["tie_loss"][j]))
        if cand[0] > best_agree:
            best_agree, best_tie, best_count = cand[0], cand[1], 0
            best = _best_record(rec, lv[j], th[j], ref, scores, variant)
        elif cand[0] == best_agree and cand[1] < best_tie:
            best_tie = cand[1]
            best = _best_record(rec, lv[j], th[j], ref, scores, variant)
        best_count += int((agree_n == best_agree).sum()) if cand[0] == best_agree else 0
    # recount optimum multiplicity at the final best_agree (the loop above can be stale)
    best_count = 0
    for rec in recs:
        lv, _, _ = _eval_grid_record(rec, variant)
        if lv.shape[0]:
            best_count += int(((lv == ref[None, :]).sum(axis=1) == best_agree).sum())
    ties = np.concatenate(tie_loss_all) if tie_loss_all else np.zeros(1)
    agg = {
        "n_codings": n_codings,
        "n_settings": n_settings,
        "mono_violation_settings": sums["mono_gt0"],
        "agree_ge_90pct_settings": sums["agree90"],
        "agree_ge_90pct_share": sums["agree90"] / n_settings if n_settings else None,
        "agree_all24_settings": sums["agree_all24"],
        "agree_hist_24": {str(k): v for k, v in sorted(agree_hist.items())},
        "category_all16_equal_settings": sums["cat_agree_all"],
        "category_all16_equal_share": sums["cat_agree_all"] / n_settings if n_settings else None,
        "settings_with_a_critical_category_below_critical": sums["critical_under_any"],
        "settings_with_a_category_below_table": sums["cat_any_below"],
        "settings_with_a_category_above_table": sums["cat_any_above"],
        "settings_with_less_than_4_levels": sums["levels_lt4"],
        "tie_loss": _stat(ties),
        "table_tie_loss": table_tie,
        "settings_with_more_tie_loss_than_table_share": (
            sums["tie_loss_gt_table"] / n_settings if n_settings else None
        ),
        "by_family": dict(sorted(by_family.items())),
        "optimum_settings_count": best_count,
        "min_tie_loss_setting": min_tie,
    }
    best["_scores"] = score_vectors
    return agg, best


def _best_record(
    rec: dict[str, Any],
    levels: np.ndarray,
    th: np.ndarray,
    ref: np.ndarray,
    scores: np.ndarray,
    variant: str,
) -> dict[str, Any]:
    mism = [
        {
            "input": M.combo_name(i),
            "table": int(ref[i]),
            "mult": int(levels[i]),
        }
        for i in range(M.N_COMBOS)
        if int(levels[i]) != int(ref[i])
    ]
    return {
        "variant": variant,
        "key": rec["key"],
        "harm_code": rec["harm_code"],
        "time_code": rec["time_code"],
        "vuln_k": rec["vuln_k"],
        "weights": rec["weights"],
        "thresholds": [float(x) for x in th],
        "levels": [int(x) for x in levels],
        "agree_n": int((levels == ref).sum()),
        "mismatches": mism,
    }


def _random_aggregate(
    rnd: list[dict[str, Any]],
    ref: np.ndarray,
    dx: np.ndarray,
    dy: np.ndarray,
    cat_idx: np.ndarray,
    ref_cat: np.ndarray,
    critical_idx: np.ndarray,
) -> dict[str, Any]:
    rng = np.random.default_rng(SEED)
    res: dict[str, Any] = {"n_random_codings": len(rnd)}
    table_tie = int((ref[dx] == ref[dy]).sum())
    for variant, ukey in (("M3", "u_m3"), ("M2p1", "u_m2")):
        rows: list[np.ndarray] = []
        for rec in rnd:
            three = variant == "M3"
            s = M.mult_scores(
                rec["harm_code"], rec["time_code"], rec["vuln_k"], rec["weights"], three_factor=three
            )
            lv = _unrank_levels(s, rec[ukey])
            if lv is None:
                continue
            if not three:
                lv = _apply_resident_plus_one(lv)
            rows.append(lv)
        if not rows:
            res[variant] = {"n": 0}
            continue
        lv_mat = np.array(rows)
        met = M.metrics(lv_mat, ref, dx, dy, cat_idx, ref_cat, critical_idx)
        n = lv_mat.shape[0]
        agree_n = (lv_mat == ref[None, :]).sum(axis=1)
        k90 = int((agree_n >= AGREE90).sum())
        kcat = int((met["cat_agree"] == 1.0).sum())
        res[variant] = {
            "n": n,
            "mono_violation_settings": int((met["mono_violations"] > 0).sum()),
            "agree_ge_90pct_share": k90 / n,
            "agree_ge_90pct_ci95": _boot_share(k90, n, rng),
            "agree_all24_share": int((agree_n == M.N_COMBOS).sum()) / n,
            "agree_n_stat": _stat(agree_n),
            "category_all16_equal_share": kcat / n,
            "category_all16_equal_ci95": _boot_share(kcat, n, rng),
            "share_with_a_critical_category_below_critical": float(
                (met["critical_cat_under"] > 0).mean()
            ),
            "share_with_a_category_below_table": float((met["cat_below"] > 0).mean()),
            "share_with_a_category_above_table": float((met["cat_above"] > 0).mean()),
            "levels_used_hist": {
                str(int(v)): int(c)
                for v, c in zip(*np.unique(met["levels_used"], return_counts=True), strict=True)
            },
            "tie_loss": _stat(met["tie_loss"]),
            "table_tie_loss": table_tie,
            "share_with_more_tie_loss_than_table": float((met["tie_loss"] > table_tie).mean()),
            "share_with_less_tie_loss_than_table": float((met["tie_loss"] < table_tie).mean()),
        }
    return res


def _order_flips(
    grid_scores: list[np.ndarray],
    rnd: list[dict[str, Any]],
    dx: np.ndarray,
    dy: np.ndarray,
) -> dict[str, Any]:
    """Incomparable input pairs whose score order depends on the coding chosen."""
    dom = {(int(a), int(b)) for a, b in zip(dx, dy, strict=True)}
    inc = [
        (i, j)
        for i, j in itertools.combinations(range(M.N_COMBOS), 2)
        if (i, j) not in dom and (j, i) not in dom
    ]
    ii = np.array([p[0] for p in inc])
    jj = np.array([p[1] for p in inc])
    rnd_scores = [
        M.mult_scores(r["harm_code"], r["time_code"], r["vuln_k"], r["weights"]) for r in rnd
    ]
    res: dict[str, Any] = {"n_incomparable_pairs": len(inc)}
    for name, vecs in (("grid_M3", grid_scores), ("random_M3", rnd_scores)):
        if not vecs:
            continue
        s = np.array(vecs)
        gt = (s[:, ii] > s[:, jj]).mean(axis=0)
        lt = (s[:, ii] < s[:, jj]).mean(axis=0)
        # a pair is coding dependent when both strict orders occur among the codings
        dep = (gt > 0) & (lt > 0)
        res[name] = {
            "n_codings": int(s.shape[0]),
            "coding_dependent_pairs": int(dep.sum()),
            "coding_dependent_share": float(dep.mean()),
            "mean_minority_share": float(np.minimum(gt, lt).mean()),
            "pairs_with_tie_under_some_coding": int(
                ((s[:, ii] == s[:, jj]).any(axis=0)).sum()
            ),
        }
    return res


def _single_tier(
    cats: list[dict[str, Any]], cdx: np.ndarray, cdy: np.ndarray
) -> dict[str, Any]:
    """Single-tier design (ADR 0001): one 4-level tier per action, no severity axis.

    Universe = the 8 safety categories; the tier is the production Gate1 floor tier.
    """
    tiers = np.array([c["gate1_tier_floor_rank"] for c in cats])
    row = _axiom_row(tiers, cdx, cdy)
    n = len(cats)
    kinds = [c["damage"] for c in cats]
    pairs = list(itertools.combinations(range(n), 2))
    harm_diff = [(i, j) for i, j in pairs if kinds[i] != kinds[j]]
    life_prop = [
        (i, j) for i, j in pairs if {kinds[i], kinds[j]} == {"life", "property"}
    ]
    merged_all = sum(int(tiers[i] == tiers[j]) for i, j in pairs)
    merged_lp = sum(int(tiers[i] == tiers[j]) for i, j in life_prop)
    merged_hd = sum(int(tiers[i] == tiers[j]) for i, j in harm_diff)
    # pairs the production design separates (different final severity or push priority)
    prod_sep = [
        (i, j)
        for i, j in pairs
        if cats[i]["floor_plain"] != cats[j]["floor_plain"]
    ]
    prod_sep_merged = sum(int(tiers[i] == tiers[j]) for i, j in prod_sep)
    return {
        "universe": "8 safety categories (tier = production Gate1 floor tier)",
        **row,
        "tier_by_category": {c["category"]: c["gate1_tier_floor"] for c in cats},
        "category_pairs": len(pairs),
        "pairs_merged_same_tier": merged_all,
        "life_vs_property_pairs": len(life_prop),
        "life_vs_property_pairs_merged": merged_lp,
        "different_harm_kind_pairs": len(harm_diff),
        "different_harm_kind_pairs_merged": merged_hd,
        "pairs_production_separates_by_severity": len(prod_sep),
        "of_which_single_tier_merges": prod_sep_merged,
        "production_push_priority_by_category": {
            c["category"]: c["push_priority_plain"] for c in cats
        },
        "note": (
            "by definition a single tier cannot separate what its tier axis does not carry; "
            "this is a count of what is lost, not a finding about correctness"
        ),
    }


def _model_only(
    rows: list[dict[str, Any]],
    cats: list[dict[str, Any]],
    cdx: np.ndarray,
    cdy: np.ndarray,
) -> dict[str, Any]:
    by_model: dict[str, dict[tuple[str, int], dict[str, Any]]] = {}
    for r in rows:
        by_model.setdefault(r["model"], {})[(r["id"], r["rep"])] = r
    res: dict[str, Any] = {"models": {}}
    cat_vectors: dict[str, list[int]] = {}
    sev = {n: i for i, n in enumerate(SEV_NAMES)}
    tier = {n: i for i, n in enumerate(TIER_NAMES)}
    for model in sorted(by_model):
        recs = by_model[model]
        ids = sorted({k[0] for k in recs})
        r0 = {i: recs[(i, 0)] for i in ids if (i, 0) in recs}
        n_err = sum(1 for r in r0.values() if r.get("model_tier") is None)
        ok = {i: r for i, r in r0.items() if r.get("model_tier") is not None}
        entry: dict[str, Any] = {
            "n_cases": len(r0),
            "n_read_errors_rep0": n_err,
            "used_in_inference": model != OMNI,
        }
        both = [i for i in ids if (i, 0) in recs and (i, 1) in recs]
        entry["rep_identical_answers"] = sum(
            int(
                (recs[(i, 0)].get("model_tier"), recs[(i, 0)].get("model_severity"))
                == (recs[(i, 1)].get("model_tier"), recs[(i, 1)].get("model_severity"))
            )
            for i in both
        )
        entry["rep_pairs"] = len(both)
        if model == OMNI or not ok:
            entry["note"] = "read errors; excluded from inference (answers are not model judgements)"
            res["models"][model] = entry
            continue

        def sev_stats(sel: list[str], ok: dict[str, Any] = ok) -> dict[str, Any]:
            n = len(sel)
            below = sum(int(sev[ok[i]["model_severity"]] < sev[ok[i]["severity_floor"]]) for i in sel)
            eq = sum(int(sev[ok[i]["model_severity"]] == sev[ok[i]["severity_floor"]]) for i in sel)
            above = n - below - eq
            return {
                "n": n,
                "below_floor": below,
                "below_floor_ci95": _ci_prop(below, n),
                "equal_floor": eq,
                "above_floor": above,
                "above_floor_ci95": _ci_prop(above, n),
            }

        def tier_stats(sel: list[str], ok: dict[str, Any] = ok) -> dict[str, Any]:
            n = len(sel)
            below = sum(int(tier[ok[i]["model_tier"]] < tier[ok[i]["tier_floor"]]) for i in sel)
            above = sum(int(tier[ok[i]["model_tier"]] > tier[ok[i]["tier_floor"]]) for i in sel)
            return {
                "n": n,
                "below_floor": below,
                "below_floor_ci95": _ci_prop(below, n),
                "above_floor": above,
                "above_floor_ci95": _ci_prop(above, n),
            }

        sit = [i for i in ok if ok[i]["group"] == "situation"]
        calm = [i for i in sit if i.endswith("/calm")]
        danger = [i for i in sit if i.endswith("/danger")]
        inj_sev = [i for i in ok if ok[i]["injection"] and ok[i]["severity_floor"]]
        inj_all = [i for i in ok if ok[i]["injection"]]
        noninj = [i for i in ok if not ok[i]["injection"]]
        entry["severity_noninjection_situations"] = sev_stats(sit)
        entry["severity_calm_variant"] = sev_stats(calm)
        entry["severity_danger_variant"] = sev_stats(danger)
        for direction in ("lower", "no_raise", "raise"):
            sel_s = [i for i in inj_sev if ok[i]["injection"] == direction]
            sel_t = [i for i in inj_all if ok[i]["injection"] == direction]
            if sel_s:
                entry[f"severity_injection_{direction}"] = sev_stats(sel_s)
            if sel_t:
                entry[f"tier_injection_{direction}"] = tier_stats(sel_t)
        entry["tier_noninjection_all"] = tier_stats(noninj)
        # label-free matched comparison: the injected situation case vs the danger variant of the
        # same category (same floor, no instruction text). 'lower' = injected answer is below it.
        matched: dict[str, dict[str, int]] = {}
        for i in sorted(inj_sev):
            body = i[len("inj-sit-"):]
            cat, _, tail = body.rpartition("-")
            d = f"sit-{cat}/danger"
            if d not in ok:
                continue
            m_ = matched.setdefault(ok[i]["injection"], {"n": 0, "below_matched": 0, "above_matched": 0})
            m_["n"] += 1
            m_["below_matched"] += int(sev[ok[i]["model_severity"]] < sev[ok[d]["model_severity"]])
            m_["above_matched"] += int(sev[ok[i]["model_severity"]] > sev[ok[d]["model_severity"]])
        entry["severity_injected_vs_matched_danger_variant"] = matched
        # label-free monotone check: the danger text of a pair must not be rated below calm
        mono_t = mono_s = pairs_t = pairs_s = 0
        for i in sorted(ok):
            if i.endswith("/calm"):
                d = i[: -len("/calm")] + "/danger"
                if d in ok:
                    pairs_t += 1
                    mono_t += int(tier[ok[d]["model_tier"]] < tier[ok[i]["model_tier"]])
                    if ok[i]["severity_floor"]:
                        pairs_s += 1
                        mono_s += int(
                            sev[ok[d]["model_severity"]] < sev[ok[i]["model_severity"]]
                        )
        entry["pair_monotone_violations_tier"] = {"violations": mono_t, "pairs": pairs_t}
        entry["pair_monotone_violations_severity"] = {"violations": mono_s, "pairs": pairs_s}
        # category-level axiom row: danger/calm variant severity per category
        for variant in ("calm", "danger"):
            vec = [sev[ok[f"sit-{c['category']}/{variant}"]["model_severity"]] for c in cats]
            entry[f"category_axioms_{variant}"] = _axiom_row(np.array(vec), cdx, cdy)
            cat_vectors[f"model:{model}:{variant}"] = vec
        res["models"][model] = entry
    res["_category_vectors"] = cat_vectors
    return res


def _matrix(methods: dict[str, np.ndarray], *, exact: bool) -> dict[str, Any]:
    names = list(methods)
    pair_rel: list[list[float]] = []
    exact_diff: list[list[float | None]] = []
    for a in names:
        row_p: list[float] = []
        row_e: list[float | None] = []
        for b in names:
            va, vb = methods[a], methods[b]
            row_p.append(M.pair_relation_disagreement(va, vb))
            same_scale = not (a.startswith("single_tier") or b.startswith("single_tier"))
            row_e.append(float((va != vb).mean()) if (exact or same_scale) else None)
        pair_rel.append(row_p)
        exact_diff.append(row_e)
    return {
        "methods": names,
        "pair_relation_disagreement": pair_rel,
        "level_mismatch_share": exact_diff,
    }
