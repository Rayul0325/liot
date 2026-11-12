"""E15 analysis: recomputes results/summary.json purely from raw/ (+ E8's committed raw/).

Registered form (HYPOTHESES.md, "사이클 4 지표 적대 검토 반영", commit a73414c). Reading choices made
before any outcome was inspected are marked READING; they fill what the registration leaves open.

C4.11(a)  The headline rise (E8 core score -> now) is 구조상 결과: the 15 tests were written against
          known survivors. Registered test = collateral kills, two measures:
          A. paired old/new outcome of E8's own core mutants that are NOT among the 15 targeted gaps
             (identical mutants, ``identity.py``); sensitivities also drop mutants on/near the gap lines.
          B. a fresh sample drawn with a new seed from operators E8 never had (``fresh_ops.py``),
             scored against E8-time tests and against current tests on the same current code.
          Thresholds (registered, unchanged): rise >= 5 pp supported, < 2 pp falsified, between 판정 불가.
          READING: "with its interval" -> per measure, delta = rise in pp with a 95% cluster bootstrap
            (clusters = module+line, 4000 reps) interval:
              지지   if delta >= 5 and the interval's lower bound > 0
              반증   if delta <  2 and the interval's upper bound < 5
              판정 불가 otherwise.
            Measures agree -> that label; disagree -> 읽기에 따라 갈림. The notifier part of the
            prediction (>= 10 pp, measure A restricted to notifier, point estimate): a 지지 without it
            becomes 판정 불가.
C4.11(b)  Mutant level (core mutants of the current run, clustered by module+line). Outcome = killed
          (targeted, as E8's headline). Predictors: line executed (0/1) and the assertion load of the
          line (``density.py``: assertions in the distinct test functions that execute the line).
          Association = AUC for 'killed'. Statistic = AUC(assertion load) - AUC(executed), cluster
          bootstrap 95% interval. Interval includes 0 -> 판정 불가; excludes 0 and > 0 -> 지지;
          excludes 0 and < 0 -> 반증. READING: uncovered mutants have load 0 by construction, so part
          of any advantage is structural; if the covered-only AUC of the load has an interval that
          includes 0.5, the difference comes from the uncovered mutants only -> 구조상 결과.
          Module-level Spearman (the original form) is reported as 기술 결과 only.
C4.11(c)  Gemini as second classifier of the 95 E8 survivors: agreement, Cohen kappa, Gwet AC1 -> 기술 결과.
"""

from __future__ import annotations

import json
import math
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any

import numpy as np
from scipy import stats

from experiments.common.io import read_jsonl

BOOT_REPS = 4000
BOOT_SEED = 15011
THR_SUPPORT_PP = 5.0
THR_FALSIFY_PP = 2.0
NOTIFIER_PP = 10.0
NOTIFIER = "liot/layers/l4_execution/notifier.py"
GAP_WINDOW = 5
BOUNDARY = (
    ("liot/layers/l4_execution/escalation.py", 237),
    ("liot/layers/l3_judgment/llm_judge.py", 291),
    ("liot/layers/l4_execution/notifier.py", 336),
)
CLASSES = ("equivalent", "untested", "safety_gap")


# ── loading ──────────────────────────────────────────────────────────────────
def load_rows(raw_dir: Path) -> dict[str, dict[str, Any]]:
    """mutant_id -> row with ``killed_t`` (E8 'targeted', timeouts rechecked) and ``killed_f``."""
    raw_dir = Path(raw_dir)
    rows = {r["mutant_id"]: dict(r) for r in read_jsonl(raw_dir / "mutants.jsonl")}
    for rc in read_jsonl(raw_dir / "timeout_recheck.jsonl"):
        row = rows.get(rc["mutant_id"])
        if row is not None and row["outcome"] == "timeout":
            row["outcome_120s"] = "timeout"
            row["outcome"] = rc["outcome"]
    full = {r["mutant_id"]: r["outcome"] for r in read_jsonl(raw_dir / "survivors_full.jsonl")}
    for mid, r in rows.items():
        r["killed_t"] = int(r["outcome"] in ("killed", "timeout"))
        r["killed_f"] = int(bool(r["killed_t"]) or full.get(mid) in ("killed", "timeout"))
        r["full_outcome"] = full.get(mid)
    return {m: r for m, r in rows.items() if r["outcome"] != "error"}


def _e8_raw(raw_dir: Path) -> Path:
    return Path(raw_dir).resolve().parents[1] / "e8_mutation" / "raw"


# ── statistics ───────────────────────────────────────────────────────────────
def _pct(x: np.ndarray, q: float) -> float:
    return float(np.percentile(x, q))


def cluster_index(keys: list[Any]) -> list[np.ndarray]:
    groups: dict[Any, list[int]] = defaultdict(list)
    for i, k in enumerate(keys):
        groups[k].append(i)
    return [np.array(v) for v in groups.values()]


def boot_delta(old: np.ndarray, new: np.ndarray, keys: list[Any]) -> dict[str, Any]:
    """Paired rise (pp) of the mean of ``new`` over ``old`` with a cluster bootstrap interval."""
    n = len(old)
    if n == 0:
        return {"n": 0}
    cl = cluster_index(keys)
    c_n = np.array([len(c) for c in cl], dtype=float)
    c_old = np.array([old[c].sum() for c in cl], dtype=float)
    c_new = np.array([new[c].sum() for c in cl], dtype=float)
    rng = np.random.RandomState(BOOT_SEED)
    idx = rng.randint(0, len(cl), size=(BOOT_REPS, len(cl)))
    d = 100 * (c_new[idx].sum(1) - c_old[idx].sum(1)) / c_n[idx].sum(1)
    return {
        "n": n,
        "n_clusters": len(cl),
        "score_old": float(old.mean()),
        "score_new": float(new.mean()),
        "delta_pp": 100 * float(new.mean() - old.mean()),
        "delta_ci95_pp": [_pct(d, 2.5), _pct(d, 97.5)],
        "gained": int(((old == 0) & (new == 1)).sum()),
        "lost": int(((old == 1) & (new == 0)).sum()),
    }


def verdict_rise(delta_pp: float, lo: float, hi: float) -> str:
    if delta_pp >= THR_SUPPORT_PP and lo > 0:
        return "지지"
    if delta_pp < THR_FALSIFY_PP and hi < THR_SUPPORT_PP:
        return "반증"
    return "판정 불가"


def auc(score: np.ndarray, y: np.ndarray) -> float:
    n1 = int(y.sum())
    n0 = len(y) - n1
    if n1 == 0 or n0 == 0:
        return float("nan")
    ranks = stats.rankdata(score)
    return float((ranks[y == 1].sum() - n1 * (n1 + 1) / 2) / (n1 * n0))


def _spearman(x: np.ndarray, y: np.ndarray) -> float:
    if np.ptp(x) == 0 or np.ptp(y) == 0:
        return float("nan")
    return float(np.corrcoef(stats.rankdata(x), stats.rankdata(y))[0, 1])


def boot_stat(fn: Any, arrays: list[np.ndarray], keys: list[Any]) -> dict[str, Any]:
    """point estimate and 95% cluster-bootstrap interval of ``fn(*arrays)``."""
    cl = cluster_index(keys)
    rng = np.random.RandomState(BOOT_SEED)
    vals: list[float] = []
    for _ in range(BOOT_REPS):
        pick = rng.randint(0, len(cl), size=len(cl))
        ix = np.concatenate([cl[i] for i in pick])
        v = fn(*[a[ix] for a in arrays])
        if not math.isnan(v):
            vals.append(v)
    arr = np.array(vals)
    return {
        "point": float(fn(*arrays)),
        "ci95": [_pct(arr, 2.5), _pct(arr, 97.5)] if len(arr) else None,
        "valid_reps": int(len(arr)),
    }


def kappa(a: list[str], b: list[str], labels: tuple[str, ...]) -> dict[str, float]:
    n = len(a)
    po = sum(x == y for x, y in zip(a, b, strict=True)) / n
    pa = {c: sum(x == c for x in a) / n for c in labels}
    pb = {c: sum(y == c for y in b) / n for c in labels}
    pe = sum(pa[c] * pb[c] for c in labels)
    k = (po - pe) / (1 - pe) if pe < 1 else float("nan")
    q = len(labels)
    pi = {c: (pa[c] + pb[c]) / 2 for c in labels}
    pe_g = sum(pi[c] * (1 - pi[c]) for c in labels) / (q - 1)
    ac1 = (po - pe_g) / (1 - pe_g) if pe_g < 1 else float("nan")
    return {"agreement": po, "kappa": float(k), "gwet_ac1": float(ac1)}


# ── (a) ──────────────────────────────────────────────────────────────────────
def part_a(raw_dir: Path) -> dict[str, Any]:
    old = load_rows(_e8_raw(raw_dir))
    new = load_rows(raw_dir)
    ident = read_jsonl(raw_dir / "identity.jsonl")
    classes_path = Path(__file__).resolve().parents[1] / "e8_mutation" / "survivor_classes.json"
    classes = json.loads(classes_path.read_text(encoding="utf-8"))
    gap_ids = [c["mutant_id"] for c in classes if c["class"] == "safety_gap"]

    def score(rows: dict[str, dict[str, Any]], tier: str, key: str, module: str | None = None
              ) -> float | None:
        rs = [r for r in rows.values() if r["tier"] == tier and (module is None or r["module"] == module)]
        return sum(r[key] for r in rs) / len(rs) if rs else None

    mods = sorted({r["module"] for r in old.values()})
    per_module = {}
    for m in mods:
        per_module[m] = {
            "core_old": score(old, "core", "killed_t", m),
            "core_new": score(new, "core", "killed_t", m),
            "core_full_old": score(old, "core", "killed_f", m),
            "core_full_new": score(new, "core", "killed_f", m),
            "n_old": sum(1 for r in old.values() if r["tier"] == "core" and r["module"] == m),
            "n_new": sum(1 for r in new.values() if r["tier"] == "core" and r["module"] == m),
        }
    headline = {
        "core_old": score(old, "core", "killed_t"),
        "core_new": score(new, "core", "killed_t"),
        "core_full_old": score(old, "core", "killed_f"),
        "core_full_new": score(new, "core", "killed_f"),
        "all_old": sum(r["killed_t"] for r in old.values()) / max(len(old), 1),
        "all_new": sum(r["killed_t"] for r in new.values()) / max(len(new), 1),
        "n_old_core": sum(1 for r in old.values() if r["tier"] == "core"),
        "n_new_core": sum(1 for r in new.values() if r["tier"] == "core"),
        "label": "구조상 결과",
    }
    if headline["core_old"] is not None and headline["core_new"] is not None:
        headline["delta_pp"] = 100 * (headline["core_new"] - headline["core_old"])

    by_old = {r["old_id"]: r for r in ident if r["old_id"]}
    status = Counter(r["status"] for r in ident)
    status_by_module = {
        m: dict(Counter(r["status"] for r in ident if r["module"] == m)) for m in mods
    }
    gap_table = []
    gap_old_lines: set[tuple[str, int]] = set()
    for gid in gap_ids:
        o = old[gid]
        gap_old_lines.add((o["module"], o["line"]))
        i = by_old.get(gid)
        n = new.get(i["new_id"]) if i and i["new_id"] else None
        gap_table.append({
            "old_id": gid, "module": o["module"], "old_line": o["line"],
            "code_line": o["code_line"], "variant": o["variant"],
            "identity": i["status"] if i else "missing",
            "new_line": i["new_line"] if i else None,
            "new_outcome": n["outcome"] if n else None,
            "new_full_outcome": (n["full_outcome"] if n else None),
            "new_killed_by": (n.get("killed_by") if n else None),
            "killed_now": bool(n and n["killed_f"]),
            "new_covered": n["covered"] if n else None,
        })
    gap_killed = sum(1 for g in gap_table if g["killed_now"])

    def collateral(exclude: set[str], module: str | None = None) -> dict[str, Any]:
        o_arr, n_arr, keys = [], [], []
        for i in ident:
            if i["status"] not in ("identical_id", "identical_context"):
                continue
            o = old[i["old_id"]]
            n = new.get(i["new_id"])
            if o["tier"] != "core" or n is None or n["tier"] != "core":
                continue
            if i["old_id"] in exclude or (module and o["module"] != module):
                continue
            o_arr.append(o["killed_t"])
            n_arr.append(n["killed_t"])
            keys.append((o["module"], o["line"]))
        res = boot_delta(np.array(o_arr), np.array(n_arr), keys)
        if res.get("n"):
            lo, hi = res["delta_ci95_pp"]
            res["verdict_rule"] = verdict_rise(res["delta_pp"], lo, hi)
        return res

    near = {
        gid for gid, o in old.items()
        if any(o["module"] == m and abs(o["line"] - ln) <= GAP_WINDOW for m, ln in gap_old_lines)
    }
    same_line = {gid for gid, o in old.items() if (o["module"], o["line"]) in gap_old_lines}
    col_a = collateral(set(gap_ids))
    col_a_notifier = collateral(set(gap_ids), NOTIFIER)
    col = {
        "A_excl_15_ids": col_a,
        "A_excl_gap_lines": collateral(same_line),
        f"A_excl_gap_lines_pm{GAP_WINDOW}": collateral(near),
        "A_notifier_excl_15_ids": col_a_notifier,
        "A_all_matched_core_incl_gaps": collateral(set()),
        "A_per_module": {m: collateral(set(gap_ids), m) for m in mods},
        "n_gap_mutants_excluded_same_line": len(same_line),
        "n_gap_mutants_excluded_window": len(near),
    }

    # B: fresh sample, same mutants scored against E8-time tests and current tests
    fr_old = {r["mutant_id"]: r for r in read_jsonl(raw_dir / "fresh_old" / "mutants.jsonl")
              if r["outcome"] != "error"}
    fr_new = {r["mutant_id"]: r for r in read_jsonl(raw_dir / "fresh_new" / "mutants.jsonl")
              if r["outcome"] != "error"}
    both = sorted(set(fr_old) & set(fr_new))
    kt = lambda r: int(r["outcome"] in ("killed", "timeout"))  # noqa: E731
    b_res = boot_delta(
        np.array([kt(fr_old[m]) for m in both]), np.array([kt(fr_new[m]) for m in both]),
        [(fr_new[m]["module"], fr_new[m]["line"]) for m in both],
    )
    if b_res.get("n"):
        lo, hi = b_res["delta_ci95_pp"]
        b_res["verdict_rule"] = verdict_rise(b_res["delta_pp"], lo, hi)
    b_mod = {}
    for m in sorted({fr_new[x]["module"] for x in both}):
        ids = [x for x in both if fr_new[x]["module"] == m]
        b_mod[m] = {
            "n": len(ids),
            "old": sum(kt(fr_old[x]) for x in ids) / len(ids),
            "new": sum(kt(fr_new[x]) for x in ids) / len(ids),
        }
    b_ops = {}
    for op in sorted({fr_new[x]["operator"] for x in both}):
        ids = [x for x in both if fr_new[x]["operator"] == op]
        b_ops[op] = {"n": len(ids), "old": sum(kt(fr_old[x]) for x in ids) / len(ids),
                     "new": sum(kt(fr_new[x]) for x in ids) / len(ids)}
    fresh = {
        "seed": 20261201,
        "planned": len(read_jsonl(raw_dir / "fresh_plan.jsonl")),
        "n_old_run": len(fr_old), "n_new_run": len(fr_new), "n_both": len(both),
        "timeouts_old": sum(1 for r in fr_old.values() if r["outcome"] == "timeout"),
        "timeouts_new": sum(1 for r in fr_new.values() if r["outcome"] == "timeout"),
        "baseline_excluded_old": sorted(
            r["module"] for r in read_jsonl(raw_dir / "fresh_old" / "baseline.jsonl") if not r["ok"]),
        "baseline_excluded_new": sorted(
            r["module"] for r in read_jsonl(raw_dir / "fresh_new" / "baseline.jsonl") if not r["ok"]),
        "paired": b_res, "per_module": b_mod, "per_operator": b_ops,
    }

    verdicts = [col_a.get("verdict_rule"), b_res.get("verdict_rule")]
    overall = verdicts[0] if verdicts[0] == verdicts[1] else "읽기에 따라 갈림"
    notifier_pp = col_a_notifier.get("delta_pp")
    notifier_ok = notifier_pp is not None and notifier_pp >= NOTIFIER_PP
    if overall == "지지" and not notifier_ok:
        overall = "판정 불가"
    return {
        "headline_structural": headline,
        "per_module": per_module,
        "identity_status": dict(status),
        "identity_status_by_module": status_by_module,
        "gap15": {"table": gap_table, "n": len(gap_table), "killed_now": gap_killed},
        "collateral": col,
        "fresh": fresh,
        "notifier_prediction_met": notifier_ok,
        "verdict_measure_A": verdicts[0],
        "verdict_measure_B": verdicts[1],
        "verdict": overall,
    }


# ── (b) ──────────────────────────────────────────────────────────────────────
def part_b(raw_dir: Path) -> dict[str, Any]:
    new = load_rows(raw_dir)
    load = {r["module"]: r["lines"] for r in read_jsonl(raw_dir / "line_load.jsonl")}
    core = sorted((r for r in new.values() if r["tier"] == "core"),
                  key=lambda r: (r["module"], r["line"], r["col"], r["operator"], r["mutant_id"]))
    y = np.array([r["killed_t"] for r in core])
    ex = np.array([int(r["covered"]) for r in core])
    def get(r: dict[str, Any], i: int) -> int:
        v = load.get(r["module"], {}).get(str(r["cov_line"]))
        return int(v[i]) if v else 0
    a_sum = np.array([get(r, 1) if r["covered"] else 0 for r in core], dtype=float)
    a_only = np.array([get(r, 2) if r["covered"] else 0 for r in core], dtype=float)
    n_tests = np.array([get(r, 0) if r["covered"] else 0 for r in core], dtype=float)
    keys = [(r["module"], r["line"]) for r in core]
    mods = np.array([r["module"] for r in core])
    # assertion load ranked within its module (removes between-module scale)
    a_rank = np.zeros(len(core))
    for m in set(mods):
        ix = np.where(mods == m)[0]
        a_rank[ix] = stats.rankdata(a_sum[ix]) / len(ix)

    def d_auc(yy: np.ndarray, e: np.ndarray, a: np.ndarray) -> float:
        return auc(a, yy) - auc(e, yy)

    def d_rho(yy: np.ndarray, e: np.ndarray, a: np.ndarray) -> float:
        return _spearman(a, yy) - _spearman(e, yy)

    main = boot_stat(d_auc, [y, ex, a_sum], keys)
    lo, hi = main["ci95"] if main["ci95"] else (float("nan"), float("nan"))
    if lo <= 0 <= hi:
        verdict = "판정 불가"
    else:
        verdict = "지지" if main["point"] > 0 else "반증"
    cov_ix = np.where(ex == 1)[0]
    ck = [keys[i] for i in cov_ix]
    cov_only = boot_stat(lambda yy, a: auc(a, yy), [y[cov_ix], a_sum[cov_ix]], ck)
    cov_only_ntests = boot_stat(lambda yy, a: auc(a, yy), [y[cov_ix], n_tests[cov_ix]], ck)
    cov_only_rank = boot_stat(lambda yy, a: auc(a, yy), [y[cov_ix], a_rank[cov_ix]], ck)
    structural = bool(cov_only["ci95"] and cov_only["ci95"][0] <= 0.5 <= cov_only["ci95"][1])
    label = "구조상 결과" if (verdict == "지지" and structural) else verdict

    # module-level (original form; 기술 결과 only)
    cov = {r["module"]: r for r in read_jsonl(raw_dir / "coverage.jsonl")}
    dens = {(r["module"], r["tests_version"]): r for r in read_jsonl(raw_dir / "module_density.jsonl")}
    mods_sorted = sorted(cov)
    score_m = np.array([float(np.mean([r["killed_t"] for r in core if r["module"] == m]))
                        for m in mods_sorted])
    cov_m = np.array([cov[m]["percent_covered"] for m in mods_sorted])
    den_m = np.array([dens[(m, "current")]["density"] for m in mods_sorted])
    rho_c = _spearman(cov_m, score_m)
    rho_d = _spearman(den_m, score_m)
    rng = np.random.RandomState(BOOT_SEED)
    diffs = []
    for _ in range(BOOT_REPS):
        ix = rng.randint(0, len(mods_sorted), size=len(mods_sorted))
        a, b = _spearman(den_m[ix], score_m[ix]), _spearman(cov_m[ix], score_m[ix])
        if not (math.isnan(a) or math.isnan(b)):
            diffs.append(a - b)
    module_level = {
        "n_modules": len(mods_sorted), "rho_coverage": rho_c, "rho_density": rho_d,
        "diff": rho_d - rho_c,
        "diff_ci95": [_pct(np.array(diffs), 2.5), _pct(np.array(diffs), 97.5)] if diffs else None,
        "valid_reps": len(diffs),
        "coverage_range_pct": [float(cov_m.min()), float(cov_m.max())],
        "label": "기술 결과",
        "per_module": {m: {"coverage": float(c), "density": float(d), "score": float(s)}
                       for m, c, d, s in zip(mods_sorted, cov_m, den_m, score_m, strict=True)},
    }
    return {
        "n_mutants": len(core), "n_killed": int(y.sum()), "n_executed": int(ex.sum()),
        "n_clusters": len(cluster_index(keys)),
        "n_load_positive": int((a_sum > 0).sum()),
        "auc_executed": auc(ex.astype(float), y),
        "auc_assertion_load": auc(a_sum, y),
        "auc_assertions_only": auc(a_only, y),
        "auc_n_tests": auc(n_tests, y),
        "auc_load_rank_within_module": auc(a_rank, y),
        "diff_auc": main,
        "diff_spearman": boot_stat(d_rho, [y, ex, a_sum], keys),
        "covered_only": {
            "n": int(len(cov_ix)), "auc_assertion_load": cov_only,
            "auc_n_tests": cov_only_ntests, "auc_load_rank_within_module": cov_only_rank,
        },
        "uncovered_driven": structural,
        "verdict_rule": verdict,
        "verdict": label,
        "module_level_descriptive": module_level,
    }


# ── (c) ──────────────────────────────────────────────────────────────────────
def part_c(raw_dir: Path) -> dict[str, Any]:
    rows = read_jsonl(raw_dir / "gemini_answers.jsonl")
    if not rows:
        return {"n": 0, "label": "미수행"}
    ok = [r for r in rows if r.get("gemini_class") in CLASSES]
    a = [r["e8_class"] for r in ok]
    b = [r["gemini_class"] for r in ok]
    out: dict[str, Any] = {
        "label": "기술 결과",
        "n_requested": len(rows), "n_valid": len(ok),
        "n_error": sum(1 for r in rows if r.get("error")),
        "errors": dict(Counter(r.get("error") for r in rows if r.get("error"))),
        "usd_estimate": sum(float(r.get("usd") or 0) for r in rows),
    }
    if len(ok) >= 2:
        out["three_class"] = kappa(a, b, CLASSES)
        out["confusion"] = {
            f"{x}|{y}": int(sum(1 for p, q in zip(a, b, strict=True) if p == x and q == y))
            for x in CLASSES for y in CLASSES
        }
        a2 = ["gap" if x == "safety_gap" else "other" for x in a]
        b2 = ["gap" if x == "safety_gap" else "other" for x in b]
        out["binary_safety_gap"] = kappa(a2, b2, ("gap", "other"))
        out["prevalence_e8_gap"] = a2.count("gap") / len(a2)
        out["prevalence_gemini_gap"] = b2.count("gap") / len(b2)
        rng = np.random.RandomState(BOOT_SEED)
        ks = []
        for _ in range(BOOT_REPS):
            ix = rng.randint(0, len(ok), size=len(ok))
            ks.append(kappa([a[i] for i in ix], [b[i] for i in ix], CLASSES)["kappa"])
        ks_a = np.array([k for k in ks if not math.isnan(k)])
        out["kappa_ci95"] = [_pct(ks_a, 2.5), _pct(ks_a, 97.5)]
        bnd = [r for r in ok if (r["module"], r["line"]) in BOUNDARY]
        out["boundary"] = [
            {"module": r["module"], "line": r["line"], "mutant_id": r["mutant_id"],
             "e8_class": r["e8_class"], "gemini_class": r["gemini_class"]} for r in bnd
        ]
        out["boundary_agreement"] = (
            sum(1 for r in bnd if r["e8_class"] == r["gemini_class"]) / len(bnd) if bnd else None
        )
        out["disagreements"] = [
            {"mutant_id": r["mutant_id"], "e8_class": r["e8_class"],
             "gemini_class": r["gemini_class"]}
            for r in ok if r["e8_class"] != r["gemini_class"]
        ]
        by_mod: dict[str, list[int]] = defaultdict(list)
        for r in ok:
            by_mod[r["module"]].append(int(r["e8_class"] == r["gemini_class"]))
        out["agreement_by_module"] = {m: [sum(v), len(v)] for m, v in sorted(by_mod.items())}
    return out


# ── whole summary ────────────────────────────────────────────────────────────
def validity(raw_dir: Path) -> dict[str, Any]:
    canary = read_jsonl(raw_dir / "canary.jsonl")
    integ = read_jsonl(raw_dir / "integrity.jsonl")
    base = read_jsonl(raw_dir / "baseline.jsonl")
    unmatched_files = {}
    for sub in ("fresh_new", "fresh_old"):
        for r in read_jsonl(raw_dir / sub / "integrity.jsonl"):
            unmatched_files[sub] = unmatched_files.get(sub, True) and r["all_equal"]
    meta = [r.get("meta", {}) for r in read_jsonl(raw_dir / "mutants.jsonl")]
    loads = [m.get("load1") for m in meta if m.get("load1") is not None]
    return {
        "baseline_ok": sum(1 for r in base if r["ok"]), "baseline_n": len(base),
        "canary_n": len(canary), "canary_all_killed": all(r["outcome"] == "killed" for r in canary),
        "integrity_checks_main": len(integ), "integrity_all_equal_main": all(
            r["all_equal"] for r in integ) if integ else None,
        "integrity_all_equal_fresh": unmatched_files,
        "load1_mean_during_sweep": float(np.mean(loads)) if loads else None,
        "load1_max_during_sweep": float(np.max(loads)) if loads else None,
        "git_shas": sorted({m.get("git_sha") for m in meta if m.get("git_sha")}),
        "n_timeouts_120s_rechecked": len(read_jsonl(raw_dir / "timeout_recheck.jsonl")),
    }


def summarize(raw_dir: Path) -> dict[str, Any]:
    raw_dir = Path(raw_dir)
    return {
        "a": part_a(raw_dir),
        "b": part_b(raw_dir),
        "c": part_c(raw_dir),
        "validity": validity(raw_dir),
    }


def make_figure(summary: dict[str, Any], exp_dir: Path) -> Path:
    import matplotlib.pyplot as plt

    from experiments.common import plotstyle

    plotstyle.setup()
    fig, (ax1, ax2) = plt.subplots(1, 2, figsize=(11.5, 4.4))
    pm = summary["a"]["per_module"]
    names = [m.split("/")[-1][:-3] for m in pm]
    x = np.arange(len(pm))
    ax1.bar(x - 0.2, [100 * (v["core_old"] or 0) for v in pm.values()], 0.4,
            color=plotstyle.GREY, label="E8 (빈틈 시험 전)")
    ax1.bar(x + 0.2, [100 * (v["core_new"] or 0) for v in pm.values()], 0.4,
            color=plotstyle.ACCENT, label="지금")
    ax1.set_xticks(x)
    ax1.set_xticklabels(names, rotation=40, ha="right", fontsize=8)
    ax1.axhline(90, color=plotstyle.GREY_DARK, linestyle="--", linewidth=1)
    ax1.set_ylabel("뮤테이션 점수 (%, 지정 연산자)")
    ax1.set_title("모듈별 점수: 이전 대 지금 (오름은 구조상 결과)")
    ax1.legend(fontsize=8)
    b = summary["b"]
    labels = ["실행 여부", "단언 부하", "(실행된 줄만)\n단언 부하"]
    vals = [b["auc_executed"], b["auc_assertion_load"],
            b["covered_only"]["auc_assertion_load"]["point"]]
    ax2.bar(range(3), [100 * v for v in vals], color=[plotstyle.GREY, plotstyle.ACCENT,
                                                       plotstyle.GREY_DARK])
    cov_ci = b["covered_only"]["auc_assertion_load"]["ci95"]
    if cov_ci:
        ax2.vlines(2, 100 * cov_ci[0], 100 * cov_ci[1], color="black", linewidth=1.2)
    ax2.axhline(50, color=plotstyle.GREY_DARK, linestyle=":", linewidth=1)
    ax2.set_xticks(range(3))
    ax2.set_xticklabels(labels, fontsize=9)
    ax2.set_ylim(40, 100)
    ax2.set_ylabel("'잡힘'을 가르는 AUC (%)")
    ax2.set_title("변이 단위: 무엇이 잡힘과 관련되나")
    fig.tight_layout()
    return plotstyle.save(fig, exp_dir, "collateral_and_association")
