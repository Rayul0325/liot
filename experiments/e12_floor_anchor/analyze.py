"""E12 analysis (C4.5): recomputed purely from the committed derived files in raw/.

  raw/steps.jsonl     judged steps of the E9 re-run (floor, scene labels, Jev's capture answer)
  raw/answers.jsonl   every judge answer per floor condition (true / up / down) and rep
  raw/runs.jsonl      per DaySim run: safety first-alert check, cost, calls
  raw/checks.jsonl    evidence round trip / egress / floor-leak checks
  raw/budget.jsonl    judge-phase cost and stop reasons

Intervals are 95% percentile bootstraps over SCENE TYPES (clusters; the 79 comfort steps are ~6 repeated scene
types) with a fixed generator seed.  With so few clusters the intervals are wide; that is the honest width.
"""

from __future__ import annotations

from collections import Counter
from pathlib import Path
from typing import Any

import numpy as np

from experiments.common.io import read_jsonl

TIERS = ("ambient", "breath", "dusk", "ember")
ORD = {t: i for i, t in enumerate(TIERS)}
JUDGES = ("jev", "haiku", "gemini")
BOOT = 4000
BOOT_SEED = 20261112


def scene_of(step: dict[str, Any]) -> str:
    return f"{step.get('action')}|{step.get('category')}"


def cluster_ratio(rng: np.random.Generator, items: list[tuple[str, int, int]]) -> dict[str, Any]:
    """items = [(cluster, numerator 0/1, denominator 0/1)] per step -> ratio with a cluster bootstrap CI."""
    clusters = sorted({c for c, _, _ in items})
    num_c = np.array([sum(n for c, n, _ in items if c == k) for k in clusters], dtype=float)
    den_c = np.array([sum(d for c, _, d in items if c == k) for k in clusters], dtype=float)
    k = len(clusters)
    if k == 0 or den_c.sum() == 0:
        return {"clusters": k, "num": 0, "den": 0, "rate": None, "lo": None, "hi": None}
    idx = rng.integers(0, k, size=(BOOT, k))
    dd = den_c[idx].sum(axis=1)
    ok = dd > 0
    boots = num_c[idx].sum(axis=1)[ok] / dd[ok]
    return {"clusters": k, "num": int(num_c.sum()), "den": int(den_c.sum()),
            "rate": float(num_c.sum() / den_c.sum()),
            "lo": float(np.percentile(boots, 2.5)), "hi": float(np.percentile(boots, 97.5))}


def wilson(num: int, den: int) -> dict[str, Any]:
    """Step-level Wilson interval (ignores clustering; shown only next to the cluster interval)."""
    if den == 0:
        return {"num": 0, "den": 0, "rate": None, "lo": None, "hi": None}
    z = 1.959964
    p = num / den
    denom = 1 + z * z / den
    centre = (p + z * z / (2 * den)) / denom
    half = z * ((p * (1 - p) / den + z * z / (4 * den * den)) ** 0.5) / denom
    return {"num": num, "den": den, "rate": p, "lo": centre - half, "hi": centre + half}


def _answers(rows: list[dict[str, Any]], rep: int) -> dict[tuple[str, str, str], str]:
    """(judge, cond, step_id) -> tier for answered requests of one rep."""
    return {(r["judge"], r["cond"], r["step_id"]): r["tier"]
            for r in rows if r["rep"] == rep and r.get("tier") in ORD and not r.get("error")}


def summarize(raw_dir: Path) -> dict[str, Any]:
    raw_dir = Path(raw_dir)
    steps = read_jsonl(raw_dir / "steps.jsonl")
    answers = read_jsonl(raw_dir / "answers.jsonl")
    runs = read_jsonl(raw_dir / "runs.jsonl")
    if not steps:
        return {"steps": 0}
    rng = np.random.default_rng(BOOT_SEED)
    by_id = {s["step_id"]: s for s in steps}
    out: dict[str, Any] = {}

    # ── capture run: E9 re-run, safety check, cost ──
    out["capture"] = {
        "runs": len(runs), "seeds": sorted(r["seed"] for r in runs), "days_per_run": runs[0]["days"] if runs else 0,
        "judged_steps": len(steps),
        "usd": sum(r["usd"] for r in runs), "calls": sum(r["calls"] for r in runs),
        "tripped_runs": [r["seed"] for r in runs if r["tripped"] is not None],
        "unknown_price_calls": sum(r["unknown_price_calls"] for r in runs),
        "luna_requests_logged": sum(r["luna_requests_logged"] for r in runs),
        "kit_status": runs[0]["status"] if runs else [],
        "first_alerts": sum(r["first_alerts"] for r in runs),
        "first_alerts_immediate": sum(r["first_alerts_immediate"] for r in runs),
        "days_total": sum(r["days"] for r in runs),
        "first_alert_ms_max": max((x for r in runs for x in r["first_alert_ms"] if x is not None), default=None),
    }
    floors = Counter(s["floor"] for s in steps)
    out["steps"] = {
        "total": len(steps), "floor_counts": dict(sorted(floors.items(), key=lambda kv: ORD[kv[0]])),
        "can_depart_upward": sum(c for f, c in floors.items() if f != "ember"),
        "can_depart_downward": sum(c for f, c in floors.items() if f != "ambient"),
        "scene_types": dict(sorted(Counter(scene_of(s) for s in steps).items())),
        "scene_types_by_floor": {f: dict(sorted(Counter(scene_of(s) for s in steps if s["floor"] == f).items()))
                                 for f in sorted(floors, key=lambda t: ORD[t])},
    }
    cap = [(s["floor"], s["capture_jev_top"]) for s in steps if s["capture_jev_top"] in ORD]
    out["capture_jev_vs_floor"] = {
        "judged": len(cap), "at_floor": sum(1 for f, t in cap if f == t),
        "above": sum(1 for f, t in cap if ORD[t] > ORD[f]), "below": sum(1 for f, t in cap if ORD[t] < ORD[f]),
    }

    ans0 = _answers(answers, 0)
    n_err = Counter((r["judge"], r["cond"]) for r in answers if r["rep"] == 0 and (r.get("error") or r.get("tier") not in ORD))
    n_req = Counter((r["judge"], r["cond"]) for r in answers if r["rep"] == 0)
    out["requests_rep0"] = {f"{j}|{c}": {"requests": n_req[(j, c)], "failed": n_err[(j, c)]}
                            for j, c in sorted(n_req)}
    present = [j for j in JUDGES if any(k[0] == j for k in ans0)]

    # ── (a) departures from the TRUE floor, per judge, up and down separately ──
    dep: dict[str, Any] = {}
    for j in present:
        rows = [(s, ans0[(j, "true", s["step_id"])]) for s in steps if (j, "true", s["step_id"]) in ans0]
        up_items = [(scene_of(s), int(ORD[t] > ORD[s["floor"]]), 1) for s, t in rows if s["floor"] != "ember"]
        dn_items = [(scene_of(s), int(ORD[t] < ORD[s["floor"]]), 1) for s, t in rows if s["floor"] != "ambient"]
        dep[j] = {
            "answered": len(rows),
            "at_floor": sum(1 for s, t in rows if t == s["floor"]),
            "above": sum(1 for s, t in rows if ORD[t] > ORD[s["floor"]]),
            "below": sum(1 for s, t in rows if ORD[t] < ORD[s["floor"]]),
            "upward_capable_steps": len(up_items), "downward_capable_steps": len(dn_items),
            "upward_rate": cluster_ratio(rng, up_items), "downward_rate": cluster_ratio(rng, dn_items),
            "upward_rate_step_wilson": wilson(sum(n for _, n, _ in up_items), len(up_items)),
            "answer_counts": dict(sorted(Counter(t for _, t in rows).items(), key=lambda kv: ORD[kv[0]])),
        }
    out["departure_true_floor"] = dep

    # ── (a) agreement where at least one judge departs from the floor ──
    def agreement(a: str, b: str) -> dict[str, Any]:
        both = [s for s in steps if (a, "true", s["step_id"]) in ans0 and (b, "true", s["step_id"]) in ans0]
        departs = [s for s in both
                   if ans0[(a, "true", s["step_id"])] != s["floor"] or ans0[(b, "true", s["step_id"])] != s["floor"]]

        def rate(sel: list[dict[str, Any]]) -> dict[str, Any]:
            return cluster_ratio(rng, [(scene_of(s), int(ans0[(a, "true", s["step_id"])] == ans0[(b, "true", s["step_id"])]), 1)
                                       for s in sel])
        jev_departs = dep.get("jev", {}).get("above", 0) + dep.get("jev", {}).get("below", 0)
        structural = "jev" in (a, b)  # registered (metric review): every pair involving Jev is 구조상 결과
        return {"both_answered": len(both), "steps_where_one_departs": len(departs),
                "agree_where_one_departs": rate(departs), "agree_all_steps": rate(both),
                "label": "구조상 결과" if structural else "기술 결과",
                "jev_departures_from_true_floor": jev_departs if structural else None,
                "why": ("Jev left the floor in almost no step, so agreement with Jev mostly equals "
                        "'the other judge stayed at the floor'") if structural else None}

    out["pair_agreement_true_floor"] = {f"{a}|{b}": agreement(a, b) for i, a in enumerate(present)
                                        for b in present[i + 1:]}

    # ── (b) floor swap: anchoring per judge and direction ──
    anch: dict[str, Any] = {}
    for j in present:
        per_dir: dict[str, Any] = {}
        pooled_move: list[tuple[str, int, int]] = []
        pooled_any: list[tuple[str, int, int]] = []
        for d in ("up", "down"):
            moved_with, moved_any, moved_dir, equals_shown = [], [], [], []
            above_given = below_given = at_given = 0
            n = 0
            for s in steps:
                sid = s["step_id"]
                if (j, d, sid) not in ans0 or (j, "true", sid) not in ans0:
                    continue
                shown = TIERS[ORD[s["floor"]] + (1 if d == "up" else -1)]
                t_true, t_sw = ans0[(j, "true", sid)], ans0[(j, d, sid)]
                sc = scene_of(s)
                n += 1
                mw = int(t_sw == shown and t_sw != t_true)
                moved_with.append((sc, mw, 1))
                moved_any.append((sc, int(t_sw != t_true), 1))
                direction = 1 if d == "up" else -1
                moved_dir.append((sc, int((ORD[t_sw] - ORD[t_true]) * direction > 0), 1))
                equals_shown.append((sc, int(t_sw == shown), 1))
                if ORD[t_sw] > ORD[shown]:
                    above_given += 1
                elif ORD[t_sw] < ORD[shown]:
                    below_given += 1
                else:
                    at_given += 1
                pooled_move.append((sc, mw, 1))
                pooled_any.append((sc, int(t_sw != t_true), 1))
            per_dir[d] = {
                "steps": n,
                "moved_with": cluster_ratio(rng, moved_with),
                "moved_with_step_wilson": wilson(sum(x[1] for x in moved_with), n),
                "moved_any": cluster_ratio(rng, moved_any),
                "moved_in_direction": cluster_ratio(rng, moved_dir),
                "answer_equals_shown_floor": cluster_ratio(rng, equals_shown),
                "vs_given_floor": {"above": above_given, "at": at_given, "below": below_given},
            }
        anch[j] = {"by_direction": per_dir, "pooled_moved_with": cluster_ratio(rng, pooled_move),
                   "pooled_moved_any": cluster_ratio(rng, pooled_any)}
    out["anchoring"] = anch
    out["anchoring_definition"] = (
        "moved_with = answer under the swapped floor equals the shown floor AND differs from the answer under "
        "the true floor; moved_any = answer changed at all; per direction (up = floor one tier higher shown, "
        "down = one tier lower); bootstrap clustered by scene type (action|category)")

    # ── answer counts for every (true floor -> shown floor) cell, rep 0 ──
    trans: dict[str, Any] = {}
    for j in present:
        cells: dict[str, Counter[str]] = {}
        for (jj, cond, sid), t in ans0.items():
            if jj != j:
                continue
            s = by_id[sid]
            shown = {"true": s["floor"], "up": TIERS[ORD[s["floor"]] + 1] if s["floor"] != "ember" else None,
                     "down": TIERS[ORD[s["floor"]] - 1] if s["floor"] != "ambient" else None}[cond]
            cells.setdefault(f"{scene_of(s)} | true={s['floor']} shown={shown}", Counter())[t] += 1
        trans[j] = {k: dict(sorted(v.items(), key=lambda kv: ORD[kv[0]])) for k, v in sorted(cells.items())}
    out["answers_by_scene_and_shown_floor"] = trans

    # ── noise baseline: the same request repeated (rep 0 vs rep 1, true floor) ──
    ans1 = _answers(answers, 1)
    noise: dict[str, Any] = {}
    for j in present:
        pairs = [(by_id[sid], t) for (jj, c, sid), t in ans1.items() if jj == j and c == "true"
                 if (j, "true", sid) in ans0]
        if pairs:
            noise[j] = cluster_ratio(rng, [(scene_of(s), int(t != ans0[(j, "true", s["step_id"])]), 1) for s, t in pairs])
    out["repeat_flip_true_floor"] = noise

    # ── Jev capture answer vs rerun answer (same call path vs experiment-side body) ──
    cmp_rows = [(s["capture_jev_top"], ans0[("jev", "true", s["step_id"])]) for s in steps
                if ("jev", "true", s["step_id"]) in ans0 and s["capture_jev_top"] in ORD]
    out["jev_capture_vs_rerun_true_floor"] = {"n": len(cmp_rows), "same": sum(1 for a, b in cmp_rows if a == b)}

    # ── checks and cost ──
    checks = read_jsonl(raw_dir / "checks.jsonl")
    out["evidence_checks"] = {k: checks[-1][k] for k in ("steps", "roundtrip_unchanged", "egress_unchanged",
                                                         "tier_word_outside_floor_field")} if checks else None
    budget = read_jsonl(raw_dir / "budget.jsonl")
    out["judge_cost"] = {
        "usd": sum(b["cost"]["run_usd"] for b in budget), "calls": sum(b["cost"]["calls"] for b in budget),
        "stop_reasons": [b["stop"] for b in budget],
        "by_kind": {k: sum(b["by_kind"].get(k, {}).get("usd", 0.0) for b in budget)
                    for k in sorted({k for b in budget for k in b["by_kind"]})},
        "tripped": [b["cost"]["tripped"] for b in budget],
        "unknown_price_calls": sum(b["cost"]["unknown_price_calls"] for b in budget),
    }
    out["usd_total_estimated"] = out["capture"]["usd"] + out["judge_cost"]["usd"]
    return out
