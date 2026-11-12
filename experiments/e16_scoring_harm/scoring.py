"""C4.12 — multiplicative score + a floor that is never lowered; vulnerability multiplier >= 5.

Reuses the E4 universe (24 inputs), dominance pairs and level machinery (experiments.e4_scoring.methods).

Registered form (HYPOTHESES.md "사이클 4 지표 적대 검토 반영", commit a73414c):
  (a) multiplicative + floor.  0 floor violations is a structural result (a floor applied with max()
      cannot end below itself).  Tie loss against the table's 32 is descriptive.
  (b) vulnerability multiplier >= 5, rewritten after the metric review: only cut points that keep every
      non-vulnerable 'life x minutes' cell in the top grade are used ("restricted"), and the MEDIAN
      tie loss is compared with the table's 32.  Under that restriction "gas below CRITICAL" is 0 by
      construction (structural).  The unrestricted counts are reported as the mechanism check.

Two floor shapes (decided before the full run, reported side by side):
  floor_safety8 : floor = the production table level at the 8 distinct U24 inputs that the 8 safety categories
                  occupy (plain and vulnerable residents); every other input keeps its multiplicative level.  This is the shape
                  production has (floors exist only for safety categories).
  floor_all24   : floor = the production table level at all 24 inputs.
final level = max(multiplicative level, floor).
"""

from __future__ import annotations

import math
from pathlib import Path
from typing import Any

import numpy as np

from experiments.common import io
from experiments.e4_scoring import methods as M

HARM_FAMILIES = {
    "lin": [1, 2, 3, 4],
    "geo2": [1, 2, 4, 8],
    "geo3": [1, 3, 9, 27],
    "geo10": [1, 10, 100, 1000],
    "zero_based": [0, 1, 2, 3],
}
TIME_FAMILIES = {
    "lin": [1, 2, 3],
    "geo2": [1, 2, 4],
    "geo3": [1, 3, 9],
    "geo10": [1, 10, 100],
    "zero_based": [0, 1, 2],
}
VULN_K = (1.5, 2.0, 3.0, 5.0, 7.0, 10.0, 20.0)  # <5: comparison with E4; >=5: registered
K_REG = 5.0
WEIGHT_STEPS = (0.5, 1.0, 2.0)
SEED = 20261112 + 412
N_RANDOM = 5000
N_RANDOM_QUICK = 100
E4_RAW = Path(__file__).resolve().parents[1] / "e4_scoring" / "raw"
TABLE_TIE = 32  # registered comparison value (re-computed from the universe in analysis and asserted)


def collect_universe(raw: Path) -> int:
    path = raw / "c412_universe.jsonl"
    if io.done_keys(path, ("kind",)):
        return 0
    src = io.read_jsonl(E4_RAW / "universe.jsonl")
    if not src:
        raise SystemExit("E4 raw/universe.jsonl missing")
    rec = dict(src[0])
    rec["source"] = "experiments/e4_scoring/raw/universe.jsonl (copied: table cells + 8 categories)"
    rec["meta_c412"] = io.run_meta()
    io.append_jsonl(path, rec)
    return 1


def collect_grid(raw: Path) -> int:
    import itertools

    path = raw / "c412_grid.jsonl"
    done = io.done_keys(path, ("key",))
    written = 0
    for (hn, hc), (tn, tc), k, a, b in itertools.product(
        HARM_FAMILIES.items(), TIME_FAMILIES.items(), VULN_K, WEIGHT_STEPS, WEIGHT_STEPS
    ):
        key = f"{hn}|{tn}|k{k}|a{a}|b{b}"
        if (key,) in done:
            continue
        io.append_jsonl(path, {"key": key, "harm_family": hn, "time_family": tn, "harm_code": hc,
                               "time_code": tc, "vuln_k": k, "weights": [a, b, 1.0], "meta": io.run_meta()})
        written += 1
    return written


def collect_random(raw: Path, n: int) -> int:
    """Log-uniform random codings with vulnerability multiplier in [5, 20] (registered range >= 5)."""
    path = raw / "c412_random.jsonl"
    done = io.done_keys(path, ("idx",))
    rng = np.random.default_rng(SEED)
    written = 0
    for idx in range(n):
        h = np.sort(np.exp(rng.uniform(math.log(1.0), math.log(1000.0), 4)))
        t = np.sort(np.exp(rng.uniform(math.log(1.0), math.log(100.0), 3)))
        k = float(np.exp(rng.uniform(math.log(5.0), math.log(20.0))))
        u3 = float(rng.uniform())
        if (idx,) in done:
            continue
        h, t = h / h[0], t / t[0]
        if not (np.all(np.diff(h) > 1e-9) and np.all(np.diff(t) > 1e-9)):
            continue
        io.append_jsonl(path, {"idx": idx, "harm_code": [round(float(x), 6) for x in h],
                               "time_code": [round(float(x), 6) for x in t], "vuln_k": round(k, 6),
                               "weights": [1.0, 1.0, 1.0], "u": round(u3, 9), "meta": io.run_meta()})
        written += 1
    return written


# ── analysis ────────────────────────────────────────────────────────────────
class _Acc:
    """Accumulates per-setting metrics for one design cell (a vulnerability multiplier, or a group)."""

    FIELDS = ("n", "gas_below", "mono", "floor_viol", "any_cat_below_table", "any_cat_above_table")

    def __init__(self) -> None:
        self.n = 0
        self.ties: list[np.ndarray] = []
        self.c = dict.fromkeys(self.FIELDS[1:], 0)
        self.has_floor = False

    def add(self, lv: np.ndarray, ctx: dict[str, Any], floor_mask: np.ndarray | None = None,
            floor_vals: np.ndarray | None = None) -> None:
        m = lv.shape[0]
        self.n += m
        dx, dy = ctx["dx"], ctx["dy"]
        self.ties.append((lv[:, dx] == lv[:, dy]).sum(axis=1).astype(np.int16))
        self.c["gas_below"] += int((lv[:, ctx["gas"]] < 3).any(axis=1).sum())
        self.c["mono"] += int(((lv[:, dx] < lv[:, dy]).sum(axis=1) > 0).sum())
        lc = lv[:, ctx["cat_idx"]]
        self.c["any_cat_below_table"] += int((lc < ctx["ref_cat"][None, :]).any(axis=1).sum())
        self.c["any_cat_above_table"] += int((lc > ctx["ref_cat"][None, :]).any(axis=1).sum())
        if floor_mask is not None and floor_vals is not None:
            self.has_floor = True
            self.c["floor_viol"] += int((lv[:, floor_mask] < floor_vals[None, :]).any(axis=1).sum())

    def row(self) -> dict[str, Any]:
        if self.n == 0:
            return {"n_settings": 0}
        t = np.concatenate(self.ties)
        out: dict[str, Any] = {
            "n_settings": self.n,
            "tie_loss": {"min": int(t.min()), "p25": float(np.percentile(t, 25)),
                         "median": float(np.median(t)), "p75": float(np.percentile(t, 75)),
                         "max": int(t.max()), "mean": float(t.mean())},
            "share_tie_loss_below_table": float((t < TABLE_TIE).mean()),
            "share_tie_loss_at_most_table": float((t <= TABLE_TIE).mean()),
            "settings_gas_below_critical": self.c["gas_below"],
            "share_gas_below_critical": self.c["gas_below"] / self.n,
            "settings_with_monotone_violation": self.c["mono"],
            "settings_with_a_safety_category_below_table": self.c["any_cat_below_table"],
            "settings_with_a_safety_category_above_table": self.c["any_cat_above_table"],
        }
        if self.has_floor:
            out["settings_with_floor_violation"] = self.c["floor_viol"]
        return out


def _variants(lv_all: np.ndarray, scores: np.ndarray, th: np.ndarray, ctx: dict[str, Any]) -> dict[str, tuple]:
    """name -> (levels, floor_mask, floor_vals) for every registered variant of one coding."""
    ref = ctx["ref"]
    s_gas = float(scores[ctx["gas"][0]])
    keep = th[:, 2] <= s_gas + 1e-9  # top cut must not exceed the non-vulnerable life x minutes score
    out: dict[str, tuple] = {"plain": (lv_all, None, None)}
    out["restricted"] = (lv_all[keep], None, None)
    cat_idx = ctx["cat_idx"]
    out["floor_safety8"] = (np.maximum(lv_all, _spread(ref, cat_idx, ref.size)[None, :]), cat_idx, ref[cat_idx])
    out["floor_all24"] = (np.maximum(lv_all, ref[None, :]), np.arange(ref.size), ref)
    return out


def _spread(ref: np.ndarray, idx: np.ndarray, n: int) -> np.ndarray:
    f = np.zeros(n, dtype=int)
    f[idx] = ref[idx]
    return f


def _context(universe: dict[str, Any]) -> dict[str, Any]:
    cells = {(c["damage"], c["time"]): int(c["level"]) for c in universe["table_cells"]}
    ref = M.production_cell_levels(cells)
    cats = universe["categories"]
    dx, dy = M.dominance_pairs()
    plain = np.array([M.category_indices([c])[c["category"]][0] for c in cats])
    vuln = np.array([M.category_indices([c])[c["category"]][1] for c in cats])
    cat_idx = np.unique(np.concatenate([plain, vuln]))
    ref_cat = ref[cat_idx]
    gas = np.array(sorted({int(plain[i]) for i, c in enumerate(cats) if c["category"] == "gas"}))
    assert ref[gas][0] == 3, "gas plain input is expected at CRITICAL in the table"
    tie = int((ref[dx] == ref[dy]).sum())
    assert tie == TABLE_TIE, f"table tie loss {tie} != registered {TABLE_TIE}"
    return {"ref": ref, "dx": dx, "dy": dy, "cat_idx": cat_idx, "ref_cat": ref_cat, "gas": gas,
            "table_tie": tie, "n_cat_inputs": int(cat_idx.size)}


def _levels(rec: dict[str, Any]) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    scores = M.mult_scores(rec["harm_code"], rec["time_code"], rec["vuln_k"], rec["weights"], three_factor=True)
    th = M.triplet_thresholds(scores)
    if th.shape[0] == 0:
        return np.zeros((0, M.N_COMBOS), dtype=int), th, scores
    return M.levels_from_thresholds(scores, th), th, scores


def _grid_summary(grid: list[dict[str, Any]], ctx: dict[str, Any]) -> dict[str, Any]:
    accs: dict[str, dict[str, _Acc]] = {}
    for rec in grid:
        lv, th, scores = _levels(rec)
        if lv.shape[0] == 0:
            continue
        kk = f"k{rec['vuln_k']:g}"
        for name, (lvv, fm, fv) in _variants(lv, scores, th, ctx).items():
            if lvv.shape[0]:
                accs.setdefault(kk, {}).setdefault(name, _Acc()).add(lvv, ctx, fm, fv)
    out: dict[str, Any] = {"by_k": {}, "groups": {}}
    for kk, d in sorted(accs.items(), key=lambda kv: float(kv[0][1:])):
        out["by_k"][kk] = {name: acc.row() for name, acc in d.items()}
    # pooled groups: k < 5 (E4 comparison) and k >= 5 (registered)
    for gname, pred in (("k_lt_5", lambda k: k < K_REG), ("k_ge_5", lambda k: k >= K_REG)):
        pool: dict[str, _Acc] = {}
        for rec in grid:
            if not pred(rec["vuln_k"]):
                continue
            lv, th, scores = _levels(rec)
            if lv.shape[0] == 0:
                continue
            for name, (lvv, fm, fv) in _variants(lv, scores, th, ctx).items():
                if lvv.shape[0]:
                    pool.setdefault(name, _Acc()).add(lvv, ctx, fm, fv)
        out["groups"][gname] = {name: acc.row() for name, acc in pool.items()}
    return out


def _random_summary(rnd: list[dict[str, Any]], ctx: dict[str, Any]) -> dict[str, Any]:
    """One random threshold triplet per coding (plain and restricted draws), k in [5, 20]."""
    rng = np.random.default_rng(SEED + 1)
    ties_plain: list[int] = []
    ties_restr: list[int] = []
    gas_plain = 0
    n_plain = 0
    n_restr = 0
    for rec in rnd:
        lv, th, scores = _levels(rec)
        if lv.shape[0] == 0:
            continue
        j = min(int(rec["u"] * lv.shape[0]), lv.shape[0] - 1)
        ties_plain.append(int((lv[j, ctx["dx"]] == lv[j, ctx["dy"]]).sum()))
        gas_plain += int(lv[j, ctx["gas"]].min() < 3)
        n_plain += 1
        keep = np.flatnonzero(th[:, 2] <= float(scores[ctx["gas"][0]]) + 1e-9)
        if keep.size:
            jr = int(keep[min(int(rec["u"] * keep.size), keep.size - 1)])
            ties_restr.append(int((lv[jr, ctx["dx"]] == lv[jr, ctx["dy"]]).sum()))
            n_restr += 1

    def boot(a: list[int]) -> dict[str, Any]:
        arr = np.array(a)
        meds = [float(np.median(arr[rng.integers(0, arr.size, arr.size)])) for _ in range(1000)]
        shares = [float((arr[rng.integers(0, arr.size, arr.size)] < TABLE_TIE).mean()) for _ in range(1000)]
        return {"n": int(arr.size), "median": float(np.median(arr)),
                "median_ci95": [float(np.percentile(meds, 2.5)), float(np.percentile(meds, 97.5))],
                "share_below_table": float((arr < TABLE_TIE).mean()),
                "share_below_table_ci95": [float(np.percentile(shares, 2.5)), float(np.percentile(shares, 97.5))],
                "min": int(arr.min()), "max": int(arr.max())}

    return {"n_codings": len(rnd), "plain": boot(ties_plain) | {"gas_below_critical_share": gas_plain / max(1, n_plain)},
            "restricted": boot(ties_restr) if ties_restr else {"n": 0}}


def summarize(raw: Path, ctx_only: bool = False) -> dict[str, Any]:
    uni = io.read_jsonl(raw / "c412_universe.jsonl")
    if not uni:
        return {"error": "no c412_universe.jsonl"}
    ctx = _context(uni[0])
    grid = io.read_jsonl(raw / "c412_grid.jsonl")
    rnd = io.read_jsonl(raw / "c412_random.jsonl")
    grid_s = _grid_summary(grid, ctx)
    ge5 = grid_s["groups"]["k_ge_5"]
    restr = ge5.get("restricted", {})
    plain = ge5.get("plain", {})
    verdict: dict[str, Any] = {
        "table_tie_loss": ctx["table_tie"],
        "a_floor_violations_floor_safety8": ge5.get("floor_safety8", {}).get("settings_with_floor_violation"),
        "a_floor_violations_floor_all24": ge5.get("floor_all24", {}).get("settings_with_floor_violation"),
        "b_restricted_median_tie_loss_k_ge_5": restr.get("tie_loss", {}).get("median"),
        "b_restricted_share_below_table_k_ge_5": restr.get("share_tie_loss_below_table"),
        "b_restricted_gas_below_critical_settings": restr.get("settings_gas_below_critical"),
        "b_unrestricted_gas_below_critical_settings_k_ge_5": plain.get("settings_gas_below_critical"),
        "b_unrestricted_gas_below_critical_share_k_ge_5": plain.get("share_gas_below_critical"),
        "b_unrestricted_median_tie_loss_k_ge_5": plain.get("tie_loss", {}).get("median"),
    }
    return {"table_tie_loss": ctx["table_tie"], "n_inputs": int(M.N_COMBOS),
            "n_dominance_pairs": int(ctx["dx"].size), "n_safety_category_inputs": ctx["n_cat_inputs"],
            "grid": grid_s, "random_k5_20": _random_summary(rnd, ctx), "headline": verdict}
