"""E8 analysis: reads only raw/ (+ the committed survivor_classes.json), deterministic.

Definitions
-----------
* A mutant is **killed** when the selected tests fail (assertion), crash at import/collection
  (``collection_error``), or hang past the 120 s timeout (``timeout``, recorded separately).
  ``error`` (harness problem) is excluded from every denominator and listed.
* ``score`` = killed / (total - error). Three nested views:
  - ``targeted``  tests that import/reference the module (+ tests/test_invariants.py) — the brief
  - ``full``      targeted, then every *covered survivor* re-run against the whole suite
  - ``assertion_only``  targeted, but timeouts and import crashes are NOT counted as killed
* ``core`` = the operators listed in the brief; ``all`` adds the extended operators.
* Uncovered mutants (statement never executed by any test) survive by construction; they stay in
  the denominator (``score_*``) and are removed in ``score_*_covered``.
"""

from __future__ import annotations

import json
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any

import numpy as np

from experiments.common.io import read_jsonl

OPERATORS = (
    "cmp_boundary", "cmp_eq", "bool_swap", "not_removal", "num_const", "return_const",
    "guard_removal", "max_min",
)
EXTENDED = ("cmp_identity", "cond_negate", "arith_swap")
BOOT_REPS = 4000
BOOT_SEED = 80080
H63_COVERAGE = 90.0
H63_SCORE = 0.90


def _ci(flags: list[int]) -> list[float] | None:
    if not flags:
        return None
    arr = np.array(flags, dtype=float)
    rng = np.random.RandomState(BOOT_SEED)
    means = rng.choice(arr, size=(BOOT_REPS, arr.size), replace=True).mean(axis=1)
    return [float(np.percentile(means, 2.5)), float(np.percentile(means, 97.5))]


def _score(flags: list[int]) -> float | None:
    return sum(flags) / len(flags) if flags else None


def _is_killed(row: dict[str, Any]) -> bool:
    return row["outcome"] in ("killed", "timeout")


def _views(rows: list[dict[str, Any]], full: dict[str, str]) -> dict[str, Any]:
    """Score views for one group of mutant rows (already de-duplicated, errors included)."""
    ok = [r for r in rows if r["outcome"] != "error"]
    targeted = [int(_is_killed(r)) for r in ok]
    assertion = [int(r["outcome"] == "killed" and r.get("kind") == "assertion") for r in ok]
    fullv = [
        int(_is_killed(r) or full.get(r["mutant_id"]) in ("killed", "timeout")) for r in ok
    ]
    cov_ok = [r for r in ok if r["covered"]]
    t_cov = [int(_is_killed(r)) for r in cov_ok]
    f_cov = [
        int(_is_killed(r) or full.get(r["mutant_id"]) in ("killed", "timeout")) for r in cov_ok
    ]
    oc = Counter(r["outcome"] for r in rows)
    kinds = Counter(r.get("kind") or "-" for r in rows if r["outcome"] == "killed")
    return {
        "n": len(rows),
        "n_valid": len(ok),
        "n_covered": len(cov_ok),
        "n_uncovered": len(ok) - len(cov_ok),
        "killed": sum(targeted),
        "outcomes": dict(sorted(oc.items())),
        "killed_kinds": dict(sorted(kinds.items())),
        "killed_full": sum(fullv),
        "score_targeted": _score(targeted),
        "score_targeted_ci95": _ci(targeted),
        "score_assertion_only": _score(assertion),
        "score_full": _score(fullv),
        "score_full_ci95": _ci(fullv),
        "score_targeted_covered": _score(t_cov),
        "score_targeted_covered_ci95": _ci(t_cov),
        "score_full_covered": _score(f_cov),
    }


def summarize(raw_dir: Path) -> dict[str, Any]:
    raw_dir = Path(raw_dir)
    mut_rows = {r["mutant_id"]: r for r in read_jsonl(raw_dir / "mutants.jsonl")}
    recheck = {r["mutant_id"]: r for r in read_jsonl(raw_dir / "timeout_recheck.jsonl")}
    for mid, rc in recheck.items():  # a 120 s timeout on a loaded machine is re-judged at 900 s
        row = mut_rows.get(mid)
        if row is not None and row["outcome"] == "timeout":
            row["outcome_120s"] = "timeout"
            row["outcome"], row["kind"] = rc["outcome"], (
                "timeout_confirmed" if rc["outcome"] == "timeout" else rc["kind"]
            )
    full = {
        r["mutant_id"]: r["outcome"] for r in read_jsonl(raw_dir / "survivors_full.jsonl")
    }
    cov = {r["module"]: r for r in read_jsonl(raw_dir / "coverage.jsonl")}
    base = {r["module"]: r for r in read_jsonl(raw_dir / "baseline.jsonl")}
    plan = {r["module"]: r for r in read_jsonl(raw_dir / "plan.jsonl")}
    classes_path = Path(__file__).resolve().parent / "survivor_classes.json"
    classes: dict[str, dict[str, str]] = (
        {c["mutant_id"]: c for c in json.loads(classes_path.read_text(encoding="utf-8"))}
        if classes_path.exists()
        else {}
    )

    by_mod: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for r in mut_rows.values():
        by_mod[r["module"]].append(r)

    modules: dict[str, Any] = {}
    for mod in sorted(set(by_mod) | set(base)):
        rows = sorted(by_mod.get(mod, []), key=lambda r: (r["line"], r["col"], r["operator"]))
        core = [r for r in rows if r["tier"] == "core"]
        c = cov.get(mod, {})
        modules[mod] = {
            "baseline_ok": bool(base[mod]["ok"]) if mod in base else None,
            "test_files": base[mod]["tests"] if mod in base else None,
            "coverage_pct": c.get("percent_covered"),
            "num_statements": c.get("num_statements"),
            "planned_core": plan.get(mod, {}).get("core"),
            "planned_all": plan.get(mod, {}).get("all"),
            "core": _views(core, full),
            "all": _views(rows, full),
            "n_timeouts": sum(1 for r in rows if r["outcome"] == "timeout"),
            "n_errors": sum(1 for r in rows if r["outcome"] == "error"),
        }
        # flat convenience keys used by the figure and the verdict
        modules[mod]["score_core"] = modules[mod]["core"]["score_targeted"]
        modules[mod]["score_core_ci95"] = modules[mod]["core"]["score_targeted_ci95"]

    all_rows = list(mut_rows.values())
    overall = {
        "core": _views([r for r in all_rows if r["tier"] == "core"], full),
        "all": _views(all_rows, full),
    }

    by_op: dict[str, Any] = {}
    for op in (*OPERATORS, *EXTENDED):
        rs = [r for r in all_rows if r["operator"] == op and r["outcome"] != "error"]
        k = sum(int(_is_killed(r) or full.get(r["mutant_id"]) in ("killed", "timeout")) for r in rs)
        k_t = sum(int(_is_killed(r)) for r in rs)
        by_op[op] = {
            "n": len(rs), "killed": k_t, "killed_full": k,
            "score": (k_t / len(rs)) if rs else None,
            "score_full": (k / len(rs)) if rs else None,
        }

    survivors = []
    for r in sorted(all_rows, key=lambda r: (r["module"], r["line"], r["col"], r["operator"])):
        if r["outcome"] != "survived":
            continue
        fo = full.get(r["mutant_id"])
        cls = classes.get(r["mutant_id"], {})
        survivors.append({
            "mutant_id": r["mutant_id"], "module": r["module"], "line": r["line"],
            "operator": r["operator"], "tier": r["tier"], "variant": r["variant"],
            "original": r["original"], "mutated": r["mutated"], "code_line": r["code_line"],
            "covered": r["covered"],
            "full_suite": fo if fo else ("not-run" if r["covered"] else "uncovered"),
            "class": cls.get("class"), "note": cls.get("note"),
        })
    still = [s for s in survivors if s["full_suite"] not in ("killed", "timeout")]
    cls_counts = Counter(s["class"] or "unclassified" for s in still)

    # adjusted score: equivalent survivors (reviewed by hand) leave the denominator
    eq_ids = {s_["mutant_id"] for s_ in still if s_["class"] == "equivalent"}
    for mod, v in modules.items():
        for tier in ("core", "all"):
            rows = [
                r for r in by_mod.get(mod, [])
                if r["outcome"] != "error" and (tier == "all" or r["tier"] == "core")
            ]
            n_eq = sum(1 for r in rows if r["mutant_id"] in eq_ids)
            killed = sum(
                int(_is_killed(r) or full.get(r["mutant_id"]) in ("killed", "timeout"))
                for r in rows
            )
            v[tier]["n_equivalent"] = n_eq
            v[tier]["score_adjusted"] = killed / (len(rows) - n_eq) if rows else None
    for tier in ("core", "all"):
        rows = [
            r for r in all_rows
            if r["outcome"] != "error" and (tier == "all" or r["tier"] == "core")
        ]
        n_eq = sum(1 for r in rows if r["mutant_id"] in eq_ids)
        killed = sum(
            int(_is_killed(r) or full.get(r["mutant_id"]) in ("killed", "timeout")) for r in rows
        )
        overall[tier]["n_equivalent"] = n_eq
        overall[tier]["score_adjusted"] = killed / (len(rows) - n_eq) if rows else None

    unc = read_jsonl(raw_dir / "uncovered_check.jsonl")
    canary = read_jsonl(raw_dir / "canary.jsonl")
    integ = read_jsonl(raw_dir / "integrity.jsonl")

    h63 = {
        m: {
            "coverage_pct": v["coverage_pct"],
            "score_core": v["score_core"],
            "score_core_full": v["core"]["score_full"],
        }
        for m, v in modules.items()
        if v["coverage_pct"] is not None and v["coverage_pct"] > H63_COVERAGE
        and v["score_core"] is not None and v["score_core"] < H63_SCORE
    }
    return {
        "modules": modules,
        "overall": overall,
        "by_operator": by_op,
        "survivors_total": len(survivors),
        "survivors_after_full_suite": len(still),
        "survivor_class_counts": dict(sorted(cls_counts.items())),
        "survivors": survivors,
        "h63_modules_low_score_despite_coverage": h63,
        "h63_all_modules_ge_90": bool(modules) and all(
            (v["score_core"] or 0) >= H63_SCORE for v in modules.values() if v["baseline_ok"]
        ),
        "timeouts": {
            "at_120s": sum(1 for r in mut_rows.values() if r.get("outcome_120s") == "timeout")
            + sum(1 for r in mut_rows.values()
                  if r["outcome"] == "timeout" and "outcome_120s" not in r),
            "rechecked_at_900s": len(recheck),
            "recheck_outcomes": dict(sorted(Counter(r["outcome"] for r in recheck.values()).items())),
        },
        "validation": {
            "uncovered_sample_n": len(unc),
            "uncovered_sample_survived": sum(1 for r in unc if r["outcome"] == "survived"),
            "canary_n": len(canary),
            "canary_all_killed": all(r["outcome"] == "killed" for r in canary) if canary else None,
            "integrity_checks": len(integ),
            "integrity_all_equal": all(r["all_equal"] for r in integ) if integ else None,
        },
    }
