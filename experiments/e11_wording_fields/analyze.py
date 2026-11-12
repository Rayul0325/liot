"""E11 analysis (C4.2 wording vs facts, C4.3 field richness).  Reads only ``raw/``.

``summarize(raw_dir)`` recomputes ``results/summary.json`` from ``raw/answers_local.jsonl``,
``raw/answers_cloud.jsonl``, ``raw/answers_rule.jsonl`` and ``raw/design.jsonl``.

Registered forms (HYPOTHESES.md C4.2/C4.3 + the 'metric review' subsection, commit a73414c) and the analysis
plan fixed before any model run (RESULTS.md, commit of the rule map):
  C4.2  retention  = P(above-floor on the rewrite | above-floor on the original), per judge, divided by the
                     test-retest retention of the unchanged sentence (rep0 vs rep1); judges weighted equally.
        calm gain  = raise rate on the alarming rewrite - raise rate on the original (calm cases), per judge,
                     equal-weight mean.  Bootstrap over cases (2,000).
        order: exclusion share > 20% -> 판정 불가; retention >= .80 and gain >= .10 -> 둘 다;
               retention < .50 or gain >= .10 -> 반증; retention >= .80 and gain < .05 -> 지지; else 판정 불가.
  C4.3  returned share = (raises(arm) - raises(operational)) / (raises(sentence) - raises(operational)), over the
        61 non-injection items, rep0; a judge counts only if its denominator >= 5.  Verdict on the count-pooled
        share of eligible judges (as C2.5/C3.1), equal-weight mean beside it; opposite sides of 0.5 ->
        읽기에 따라 갈림.  Support = monotone 0 <= s(1) <= s(2) <= s(4) and s(4) >= 0.5; falsified = s(4) < 0.5.
Nothing here treats a model answer or the draft labels as truth (ADD-5 is marked draft-label dependent).
"""

from __future__ import annotations

import zlib
from pathlib import Path
from typing import Any

import numpy as np

from experiments.common.io import read_jsonl
from experiments.e5_models.analyze import _add5, _by_item, _cohen_kappa

BOOT = 2000
SEED = 11
LOCAL = ("jevk5-9b-q8", "jevk5-4b-q8", "jevk5-4b-q4", "apus-openjev-4b-q4", "jev-9b-q6")
CLOUD = ("jev-api", "haiku", "gemini")
JEVK5 = ("jevk5-9b-q8", "jevk5-4b-q8", "jevk5-4b-q4")
FIELD_ARMS = ("f_A", "f_AB", "f_ABCD", "f_B", "f_C", "f_D")
RETAIN_SUPPORT, RETAIN_FALSIFY = 0.80, 0.50
GAIN_SUPPORT, GAIN_FALSIFY = 0.05, 0.10
EXCLUDE_MAX = 0.20
MIN_DENOM = 5
Rows = dict[tuple[str, int], dict[str, Any]]
Table = dict[tuple[str, str], Rows]


def _rng(name: str) -> np.random.Generator:
    return np.random.default_rng(zlib.crc32(name.encode()) ^ SEED)


def _load(raw: Path) -> tuple[Table, dict[str, Any]]:
    table: Table = {}
    for fname in ("answers_local.jsonl", "answers_cloud.jsonl", "answers_rule.jsonl"):
        for r in read_jsonl(raw / fname):
            table.setdefault((r["judge"], r["context"]), {})[(r["id"], r["rep"])] = r
    design = next((r for r in read_jsonl(raw / "design.jsonl") if r.get("kind") == "design"), {})
    return table, design


def _items(table: Table, judge: str, ctx: str, rep: int) -> dict[tuple[str, str], tuple[int, int]]:
    rows = table.get((judge, ctx))
    if not rows:
        return {}
    return {k: (v[0], v[1]) for k, v in _by_item(rows, rep).items()}


def _reps(table: Table, judge: str, ctx: str) -> list[int]:
    return sorted({r for (_c, r) in table.get((judge, ctx), {})})


def _weights(judges: list[str], collapse_jevk5: bool) -> dict[str, float]:
    w = {j: 1.0 for j in judges}
    if collapse_jevk5:
        fam = [j for j in judges if j in JEVK5]
        for j in fam:
            w[j] = 1.0 / len(fam)
    return w


def _wmean(vals: dict[str, float | None], w: dict[str, float]) -> float | None:
    got = {j: v for j, v in vals.items() if v is not None}
    tot = sum(w[j] for j in got)
    return None if tot == 0 else sum(w[j] * v for j, v in got.items()) / tot


def _ci(xs: list[float]) -> list[float] | None:
    return [float(np.percentile(xs, 2.5)), float(np.percentile(xs, 97.5))] if xs else None


# ── C4.2 ─────────────────────────────────────────────────────────────────────
def _retention_contrib(
    table: Table, judge: str, rw_arm: str, cases: list[str]
) -> dict[str, tuple[int, int, int, int]]:
    """Per case: (raised on rewrite & orig, orig raised, retest hit, retest base) pooled over rep pairs."""
    out: dict[str, list[int]] = {c: [0, 0, 0, 0] for c in cases}
    sent_reps = _reps(table, judge, "sentence")
    rw_reps = _reps(table, judge, rw_arm)
    cs = set(cases)
    for r in rw_reps:
        if r not in sent_reps:
            continue
        o, w = _items(table, judge, "sentence", r), _items(table, judge, rw_arm, r)
        for k, (a, f) in o.items():
            if k[0] in cs and a > f and k in w:
                out[k[0]][1] += 1
                out[k[0]][0] += int(w[k][0] > w[k][1])
    for r1, r2 in ((0, 1), (1, 0)):
        if r1 in sent_reps and r2 in sent_reps:
            o1, o2 = _items(table, judge, "sentence", r1), _items(table, judge, "sentence", r2)
            for k, (a, f) in o1.items():
                if k[0] in cs and a > f and k in o2:
                    out[k[0]][3] += 1
                    out[k[0]][2] += int(o2[k][0] > o2[k][1])
    return {c: (v[0], v[1], v[2], v[3]) for c, v in out.items()}


def _norm_retention(c: dict[str, tuple[int, int, int, int]], pick: list[str]) -> tuple[float | None, float | None, float | None]:
    num = sum(c[x][0] for x in pick)
    den = sum(c[x][1] for x in pick)
    hit = sum(c[x][2] for x in pick)
    base = sum(c[x][3] for x in pick)
    if den == 0 or base == 0 or hit == 0:
        return (num / den if den else None), (hit / base if base else None), None
    return num / den, hit / base, (num / den) / (hit / base)


def _gain_contrib(table: Table, judge: str, rw_arm: str, cases: list[str]) -> dict[str, tuple[int, int, int]]:
    """Per case: (raises on rewrite, raises on original, items), over reps where both exist."""
    out: dict[str, list[int]] = {c: [0, 0, 0] for c in cases}
    cs = set(cases)
    for r in _reps(table, judge, rw_arm):
        if r not in _reps(table, judge, "sentence"):
            continue
        o, w = _items(table, judge, "sentence", r), _items(table, judge, rw_arm, r)
        for k, (a, f) in w.items():
            if k[0] in cs and k in o:
                out[k[0]][0] += int(a > f)
                out[k[0]][1] += int(o[k][0] > o[k][1])
                out[k[0]][2] += 1
    return {c: (v[0], v[1], v[2]) for c, v in out.items()}


def _gain(c: dict[str, tuple[int, int, int]], pick: list[str]) -> float | None:
    n = sum(c[x][2] for x in pick)
    return None if n == 0 else (sum(c[x][0] for x in pick) - sum(c[x][1] for x in pick)) / n


URGENT_WORD = "긴급"


def _prefix_split(
    table: Table, acc: dict[str, dict[str, str]], judges: list[str], danger: list[str], calm: list[str],
    w: dict[str, float],
) -> dict[str, Any]:
    """Raise-rate gain of the alarming rewrites split by whether the accepted text carries '긴급' (descriptive)."""
    out: dict[str, Any] = {"marker": URGENT_WORD}
    for pool_name, pool in (("calm", calm), ("danger", danger)):
        sub: dict[str, Any] = {}
        for name, has in (("with_urgent_marker", True), ("without_urgent_marker", False)):
            pick = [c for c in pool if c in acc.get("alarming", {}) and (URGENT_WORD in acc["alarming"][c]) == has]
            gc = {j: _gain_contrib(table, j, "rw_alarming", pick) for j in judges}
            pt = _wmean({j: _gain(gc[j], pick) for j in judges}, w) if pick else None
            rng = _rng(f"c42-prefix-{pool_name}-{name}")
            bs: list[float] = []
            for _ in range(BOOT if pick else 0):
                s = [pick[i] for i in rng.integers(0, len(pick), size=len(pick))]
                m = _wmean({j: _gain(gc[j], s) for j in judges}, w)
                if m is not None:
                    bs.append(m)
            sub[name] = {"cases": len(pick), "gain_equal_weight": pt, "gain_ci95_case_bootstrap": _ci(bs)}
        out[pool_name] = sub
    return out


def _c42(table: Table, design: dict[str, Any], judges: list[str]) -> dict[str, Any]:
    acc = design.get("accepted", {"neutral": {}, "alarming": {}})
    pv = design.get("pair_variant", {})
    danger = sorted(c for c, v in pv.items() if v["variant"] == "danger")
    calm = sorted(c for c, v in pv.items() if v["variant"] == "calm")
    cells = {
        "neutral_on_danger": ("rw_neutral", "neutral", danger),
        "alarming_on_calm": ("rw_alarming", "alarming", calm),
        "alarming_on_danger": ("rw_alarming", "alarming", danger),
        "neutral_on_calm": ("rw_neutral", "neutral", calm),
    }
    w = _weights(judges, False)
    w_fam = _weights(judges, True)
    out: dict[str, Any] = {"judges": judges, "cells": {}}
    n_pairs = len(danger)
    for name, (ctx, key, pool) in cells.items():
        pick = [c for c in pool if c in acc.get(key, {})]
        ret_c = {j: _retention_contrib(table, j, ctx, pick) for j in judges}
        gain_c = {j: _gain_contrib(table, j, ctx, pick) for j in judges}
        per: dict[str, Any] = {}
        for j in judges:
            r, rt, nr = _norm_retention(ret_c[j], pick)
            per[j] = {
                "retention_raw": r, "retest_retention": rt, "retention_normalised": nr,
                "orig_raises_in_set": sum(ret_c[j][x][1] for x in pick),
                "raise_rate_gain": _gain(gain_c[j], pick),
            }
        rng = _rng("c42-" + name)
        boots: dict[str, list[float]] = {"ret": [], "gain": []}
        for _ in range(BOOT):
            if not pick:
                break
            s = [pick[i] for i in rng.integers(0, len(pick), size=len(pick))]
            rr = {j: _norm_retention(ret_c[j], s)[2] for j in judges}
            gg = {j: _gain(gain_c[j], s) for j in judges}
            m1, m2 = _wmean(rr, w), _wmean(gg, w)
            if m1 is not None:
                boots["ret"].append(m1)
            if m2 is not None:
                boots["gain"].append(m2)
        ret_pt = _wmean({j: per[j]["retention_normalised"] for j in judges}, w)
        gain_pt = _wmean({j: per[j]["raise_rate_gain"] for j in judges}, w)
        out["cells"][name] = {
            "rewrite_context": ctx,
            "cases_in_cell": len(pool),
            "cases_accepted": len(pick),
            "per_judge": per,
            "retention_normalised_equal_weight": ret_pt,
            "retention_normalised_ci95_case_bootstrap": _ci(boots["ret"]),
            "retention_normalised_jevk5_as_one": _wmean({j: per[j]["retention_normalised"] for j in judges}, w_fam),
            "retention_raw_equal_weight": _wmean({j: per[j]["retention_raw"] for j in judges}, w),
            "raise_rate_gain_equal_weight": gain_pt,
            "raise_rate_gain_ci95_case_bootstrap": _ci(boots["gain"]),
            "raise_rate_gain_jevk5_as_one": _wmean({j: per[j]["raise_rate_gain"] for j in judges}, w_fam),
        }
    ex = design.get("exclusions", {})
    fin = ex.get("final_excluded", {})
    p1 = ex.get("pass1_failures", {})
    arm_cases = {"neutral": danger, "alarming": calm}
    excl: dict[str, Any] = {}
    for arm, pool in arm_cases.items():
        n_final = sum(1 for c in pool if c in fin.get(arm, {}))
        n_p1 = sum(1 for c in pool if c in p1.get(arm, {}))
        excl[arm] = {
            "cases": len(pool), "pass1_failures": n_p1, "pass1_rate": n_p1 / len(pool) if pool else None,
            "final_excluded": n_final, "final_rate": n_final / len(pool) if pool else None,
        }
    out["exclusions_registered_arms"] = excl
    out["cases_per_register"] = n_pairs
    nd = out["cells"]["neutral_on_danger"]
    ac = out["cells"]["alarming_on_calm"]
    ret, gain = nd["retention_normalised_equal_weight"], ac["raise_rate_gain_equal_weight"]
    excl_over_final = any((e["final_rate"] or 0) > EXCLUDE_MAX for e in excl.values())
    excl_over_pass1 = all((e["pass1_rate"] or 0) > EXCLUDE_MAX for e in excl.values())
    excl_over = excl_over_final
    if ret is None or gain is None:
        verdict_if_final, why_if_final = "판정 불가", "retention or gain undefined"
    elif ret >= RETAIN_SUPPORT and gain >= GAIN_FALSIFY:
        verdict_if_final, why_if_final = "둘 다", "retention >= .80 and calm gain >= .10"
    elif ret < RETAIN_FALSIFY or gain >= GAIN_FALSIFY:
        verdict_if_final, why_if_final = "반증", "retention < .50 or calm gain >= .10"
    elif ret >= RETAIN_SUPPORT and gain < GAIN_SUPPORT:
        verdict_if_final, why_if_final = "지지", "retention >= .80 and calm gain < .05"
    else:
        verdict_if_final, why_if_final = "판정 불가", "between the registered thresholds"
    out["prefix_split"] = _prefix_split(table, acc, judges, danger, calm, w)
    if excl_over_pass1 or excl_over:
        # metric-review reading (validity review): the 20% rule is read on the pass-1 failures
        out["verdict_c42"] = {
            "label": "판정 불가",
            "reason": ("manipulation/exclusion rule failed (pass-1 exclusions exceed 20% in both arms; "
                       "pass-2 checker looser than the pre-run plan; alarming arm became an emergency-assertion prefix)"),
            "retention": ret, "calm_gain": gain,
            "point_estimates_status": "exploratory (not usable for the registered verdict)",
            "label_if_final_exclusions_were_used": verdict_if_final,
            "reason_if_final_exclusions_were_used": why_if_final,
            "pass1_exclusion_rates": {a: e["pass1_rate"] for a, e in excl.items()},
        }
        out["thresholds"] = {"retention_support": RETAIN_SUPPORT, "retention_falsify": RETAIN_FALSIFY,
                             "gain_support": GAIN_SUPPORT, "gain_falsify": GAIN_FALSIFY, "exclusion_max": EXCLUDE_MAX}
        return out
    if excl_over:
        verdict, why = "판정 불가", "exclusion share above 20% in a registered arm"
    elif ret is None or gain is None:
        verdict, why = "판정 불가", "retention or gain undefined (no original raises or no data)"
    elif ret >= RETAIN_SUPPORT and gain >= GAIN_FALSIFY:
        verdict, why = "둘 다", "retention >= .80 and calm gain >= .10"
    elif ret < RETAIN_FALSIFY or gain >= GAIN_FALSIFY:
        verdict, why = "반증", "retention < .50 or calm gain >= .10"
    elif ret >= RETAIN_SUPPORT and gain < GAIN_SUPPORT:
        verdict, why = "지지", "retention >= .80 and calm gain < .05"
    else:
        verdict, why = "판정 불가", "between the registered thresholds"
    out["verdict_c42"] = {"label": verdict, "reason": why, "retention": ret, "calm_gain": gain}
    out["thresholds"] = {"retention_support": RETAIN_SUPPORT, "retention_falsify": RETAIN_FALSIFY,
                         "gain_support": GAIN_SUPPORT, "gain_falsify": GAIN_FALSIFY, "exclusion_max": EXCLUDE_MAX}
    return out


# ── C4.3 ─────────────────────────────────────────────────────────────────────
def _raise_vec(table: Table, judge: str, ctx: str, rep: int, keys: list[tuple[str, str]]) -> np.ndarray:
    it = _items(table, judge, ctx, rep)
    return np.array([int(it[k][0] > it[k][1]) for k in keys])


def _common_keys(table: Table, judge: str, ctxs: tuple[str, ...], rep: int) -> list[tuple[str, str]]:
    sets = [set(_items(table, judge, c, rep)) for c in ctxs]
    return sorted(set.intersection(*sets)) if all(sets) else []


def _c43(table: Table, design: dict[str, Any], judges: list[str]) -> dict[str, Any]:
    out: dict[str, Any] = {"judges": judges, "min_denominator": MIN_DENOM, "arms": {}}
    # eligibility from sentence vs operational only (independent of the target arm)
    elig: dict[str, dict[str, int]] = {}
    for rep in (0, 1):
        for j in judges:
            ks = _common_keys(table, j, ("sentence", "operational"), rep)
            if not ks:
                continue
            rs = int(_raise_vec(table, j, "sentence", rep, ks).sum())
            ro = int(_raise_vec(table, j, "operational", rep, ks).sum())
            elig.setdefault(f"rep{rep}", {})[j] = rs - ro
    out["denominator_sentence_minus_operational"] = elig
    for rep in (0, 1):
        rk = f"rep{rep}"
        ok = [j for j in judges if elig.get(rk, {}).get(j, 0) >= MIN_DENOM]
        arms_out: dict[str, Any] = {}
        for arm in FIELD_ARMS:
            per: dict[str, Any] = {}
            data: dict[str, tuple[list[tuple[str, str]], np.ndarray, np.ndarray, np.ndarray]] = {}
            for j in ok:
                ks = _common_keys(table, j, ("sentence", "operational", arm), rep)
                if not ks:
                    continue
                s, o, t = (_raise_vec(table, j, c, rep, ks) for c in ("sentence", "operational", arm))
                data[j] = (ks, s, o, t)
                d = int(s.sum() - o.sum())
                per[j] = {
                    "items": len(ks), "raised_sentence": int(s.sum()), "raised_operational": int(o.sum()),
                    "raised_arm": int(t.sum()), "returned": (int(t.sum()) - int(o.sum())) / d if d else None,
                }
            if not data:
                arms_out[arm] = {"per_judge": per, "eligible_judges": [], "pooled_returned": None}
                continue
            rs = sum(int(v[1].sum()) for v in data.values())
            ro = sum(int(v[2].sum()) for v in data.values())
            rt = sum(int(v[3].sum()) for v in data.values())
            pooled = (rt - ro) / (rs - ro) if rs > ro else None
            wmean = float(np.mean([p["returned"] for p in per.values() if p["returned"] is not None]))
            cases = sorted({k[0] for v in data.values() for k in v[0]})
            idx = {j: {c: [i for i, k in enumerate(v[0]) if k[0] == c] for c in cases} for j, v in data.items()}
            rng = _rng(f"c43-{rk}-{arm}")
            bs: list[float] = []
            for _ in range(BOOT):
                pick = [cases[i] for i in rng.integers(0, len(cases), size=len(cases))]
                a = b = c_ = 0
                for j, v in data.items():
                    sel = [i for c in pick for i in idx[j][c]]
                    a += int(v[1][sel].sum())
                    b += int(v[2][sel].sum())
                    c_ += int(v[3][sel].sum())
                if a > b:
                    bs.append((c_ - b) / (a - b))
            arms_out[arm] = {
                "per_judge": per, "eligible_judges": sorted(data),
                "raised_sentence": rs, "raised_operational": ro, "raised_arm": rt,
                "pooled_returned": pooled, "pooled_returned_ci95_case_bootstrap": _ci(bs),
                "equal_weight_mean_returned": wmean,
            }
        out["arms"][rk] = arms_out
    # verdict (rep0)
    a0 = out["arms"].get("rep0", {})
    chain = [("f_A", 1), ("f_AB", 2), ("f_ABCD", 4)]
    shares = {n: a0.get(arm, {}).get("pooled_returned") for arm, n in chain}
    means = {n: a0.get(arm, {}).get("equal_weight_mean_returned") for arm, n in chain}
    elig0 = [j for j in judges if elig.get("rep0", {}).get(j, 0) >= MIN_DENOM]
    if not elig0 or shares[4] is None:
        label, why = "판정 불가", "no judge with a sentence-minus-operational denominator >= 5"
    else:
        seq = [0.0, shares[1], shares[2], shares[4]]
        mono = all(x is not None for x in seq) and all(seq[i] <= seq[i + 1] + 1e-12 for i in range(3))
        hi_pool, hi_mean = shares[4] >= 0.5, (means[4] is not None and means[4] >= 0.5)
        if hi_pool != hi_mean:
            label, why = "읽기에 따라 갈림", "count-pooled and equal-weight readings fall on opposite sides of 0.5 at 4 fields"
        elif not hi_pool:
            label, why = "반증", "returned share < 0.5 at 4 fields"
        elif mono:
            label, why = "지지", "monotone in field count and >= 0.5 at 4 fields"
        else:
            label, why = "판정 불가", ">= 0.5 at 4 fields but not monotone in field count"
    out["verdict_c43"] = {
        "label": label, "reason": why, "eligible_judges_rep0": elig0,
        "pooled_share_by_field_count": {"0": 0.0, **{str(n): shares[n] for n in (1, 2, 4)}},
        "equal_weight_share_by_field_count": {"0": 0.0, **{str(n): means[n] for n in (1, 2, 4)}},
        "single_field_pooled_share": {a: a0.get(a, {}).get("pooled_returned") for a in ("f_A", "f_B", "f_C", "f_D")},
    }
    out["field_information"] = _field_information(design)
    out["informative_only"] = _informative_only(table, design, judges, elig0)
    inf = out["informative_only"]
    out["verdict_c43"]["qualifier"] = (
        f"반증(등록 문구) — 정보 상한 ≈{inf['ceiling_share_reachable']:.2f}; "
        f"정보가 있는 사례만으로는 {inf['label_informative_only_4_fields']}"
    )
    return out


NULL_FIELDS = ((), "unknown", "0", "unknown")


def _tuple(codes: dict[str, Any]) -> tuple[Any, ...]:
    return (tuple(codes["sensor_kinds"]), codes["rate"], codes["concurrent"], codes["duration"])


def _field_information(design: dict[str, Any]) -> dict[str, Any]:
    """Which field differs between the calm and the danger variant of a pair (coder-A values)."""
    codes = design.get("codes_A", {})
    pv = design.get("pair_variant", {})
    names = sorted({v["pair"] for v in pv.values() if v["pair"]})
    cnt = {"sensor_kinds_set": 0, "sensor_kinds_emptiness": 0, "rate": 0, "concurrent": 0, "duration": 0,
           "all_four_identical": 0}
    for n in names:
        a, b = codes[f"{n}/calm"], codes[f"{n}/danger"]
        cnt["sensor_kinds_set"] += int(sorted(a["sensor_kinds"]) != sorted(b["sensor_kinds"]))
        cnt["sensor_kinds_emptiness"] += int(bool(a["sensor_kinds"]) != bool(b["sensor_kinds"]))
        for f in ("rate", "concurrent", "duration"):
            cnt[f] += int(a[f] != b[f])
        cnt["all_four_identical"] += int(_tuple(a) == _tuple(b))
    return {"pairs": len(names), "pairs_where_field_differs": cnt,
            "cases_with_any_non_default_field": sum(1 for c in codes.values() if _tuple(c) != NULL_FIELDS),
            "cases": len(codes)}


def _informative_only(table: Table, design: dict[str, Any], judges: list[str], elig0: list[str]) -> dict[str, Any]:
    """Ceiling of the returned share and the share restricted to cases whose 4 fields are not all default.

    A case whose four field values are all default (no anomalous sensor, rate unknown, concurrent 0, duration
    unknown) gives the model exactly the operational summary plus constant text, so a rise there cannot come back
    through the fields.  Ceiling = 1 - (denominator items on such cases) / (all denominator items); a denominator
    item is a (judge, case, question) the judge raised with the sentence and not with the operational summary
    (rep0, eligible judges).  Post-hoc reading added after the validity review (HYPOTHESES.md, 사후 변경 기록).
    """
    codes = design.get("codes_A", {})
    informative = {c for c, v in codes.items() if _tuple(v) != NULL_FIELDS}
    items = []
    for j in elig0:
        s_, o_ = _items(table, j, "sentence", 0), _items(table, j, "operational", 0)
        items += [(j, k) for k, (a, f) in s_.items() if k in o_ and a > f and not o_[k][0] > o_[k][1]]
    uninf = sum(1 for _, k in items if k[0] not in informative)
    out: dict[str, Any] = {
        "informative_cases": len(informative), "cases": len(codes),
        "denominator_items": len(items), "denominator_items_on_uninformative_cases": uninf,
        "ceiling_share_reachable": 1 - uninf / len(items) if items else None,
        "arms": {},
    }
    for arm in FIELD_ARMS:
        data: dict[str, tuple[list[tuple[str, str]], np.ndarray, np.ndarray, np.ndarray]] = {}
        for j in elig0:
            ks = [k for k in _common_keys(table, j, ("sentence", "operational", arm), 0) if k[0] in informative]
            if ks:
                data[j] = (ks, *(_raise_vec(table, j, c, 0, ks) for c in ("sentence", "operational", arm)))
        if not data:
            continue
        rs, ro, rt = (sum(int(v[i].sum()) for v in data.values()) for i in (1, 2, 3))
        cases = sorted({k[0] for v in data.values() for k in v[0]})
        idx = {j: {c: [i for i, k in enumerate(v[0]) if k[0] == c] for c in cases} for j, v in data.items()}
        rng = _rng(f"c43-informative-{arm}")
        bs: list[float] = []
        for _ in range(BOOT):
            pick = [cases[i] for i in rng.integers(0, len(cases), size=len(cases))]
            a = b = c_ = 0
            for j, v in data.items():
                sel = [i for c in pick for i in idx[j][c]]
                a += int(v[1][sel].sum())
                b += int(v[2][sel].sum())
                c_ += int(v[3][sel].sum())
            if a > b:
                bs.append((c_ - b) / (a - b))
        out["arms"][arm] = {
            "raised_sentence": rs, "raised_operational": ro, "raised_arm": rt,
            "pooled_returned": (rt - ro) / (rs - ro) if rs > ro else None,
            "pooled_returned_ci95_case_bootstrap": _ci(bs),
        }
    a4 = out["arms"].get("f_ABCD", {})
    ci = a4.get("pooled_returned_ci95_case_bootstrap")
    if a4.get("pooled_returned") is None or ci is None:
        lab = "판정 불가"
    elif ci[1] < 0.5:
        lab = "반증"
    elif ci[0] >= 0.5:
        lab = "지지"
    else:
        lab = "판정 불가"
    out["label_informative_only_4_fields"] = lab
    out["label_rule"] = "post-hoc: interval entirely below 0.5 -> 반증; entirely at/above 0.5 -> 지지; else 판정 불가"
    return out


def _rule_vs_models(table: Table, judges: list[str]) -> dict[str, Any]:
    out: dict[str, Any] = {"note": "ADD-5 uses author-built pairs and the draft pair design: marked draft-label dependent",
                           "arms": {}}
    for arm in ("operational", *FIELD_ARMS):
        rule_rows = table.get(("rule-lookup", arm))
        if not rule_rows:
            continue
        ri = _by_item(rule_rows, 0)
        r_raise = sum(int(a > f) for a, f, _ in ri.values())
        r_below = sum(int(a < f) for a, f, _ in ri.values())
        r_add5 = _add5(rule_rows, 0, f"rule-{arm}")
        models: dict[str, Any] = {}
        for j in judges:
            rows = table.get((j, arm))
            if not rows:
                continue
            mi = _by_item(rows, 0)
            common = sorted(set(ri) & set(mi))
            ma = _add5(rows, 0, f"add5-{j}-{arm}")
            models[j] = {
                "above_floor": sum(int(a > f) for a, f, _ in mi.values()),
                "below_floor": sum(int(a < f) for a, f, _ in mi.values()),
                "add5": None if ma is None else ma["answer_level"],
                "answer_equal_to_rule": sum(int(ri[k][0] == mi[k][0]) for k in common),
                "items_compared": len(common),
            }
        miss = [m["add5"]["miss_rate"] for m in models.values() if m["add5"]]
        over = [m["add5"]["over_raise_rate"] for m in models.values() if m["add5"]]
        ra = None if r_add5 is None else r_add5["answer_level"]
        match = None
        if ra is not None and miss and r_add5 is not None:
            match = bool(ra["miss_rate"] <= float(np.median(miss)) + 1e-9
                         and ra["over_raise_rate"] <= float(np.median(over)) + 1.0 / max(1, r_add5["pairs"]) + 1e-9)
        out["arms"][arm] = {
            "rule_above_floor": r_raise, "rule_below_floor": r_below, "items": len(ri),
            "rule_add5": ra, "rule_add5_pairs": None if r_add5 is None else r_add5["pairs"],
            "models": models,
            "models_median_above_floor": float(np.median([m["above_floor"] for m in models.values()])) if models else None,
            "models_median_miss": float(np.median(miss)) if miss else None,
            "models_median_over_raise": float(np.median(over)) if over else None,
            "rule_matches_or_beats_models_draft_label_dependent": match,
        }
    return out


# ── coder agreement ──────────────────────────────────────────────────────────
def _ac1(a: list[str], b: list[str]) -> float | None:
    cats = sorted(set(a) | set(b))
    n = len(a)
    if n == 0 or len(cats) < 2:
        return None
    po = sum(x == y for x, y in zip(a, b, strict=True)) / n
    pe = 0.0
    for c in cats:
        pi = (a.count(c) + b.count(c)) / (2 * n)
        pe += pi * (1 - pi)
    pe /= len(cats) - 1
    return (po - pe) / (1 - pe) if pe < 1 else None


def _coder_agreement(design: dict[str, Any]) -> dict[str, Any]:
    ca, cb = design.get("codes_A", {}), design.get("codes_B", {})
    ids = sorted(set(ca) & set(cb))
    if not ids:
        return {}
    out: dict[str, Any] = {"cases": len(ids)}
    for f in ("rate", "concurrent", "duration"):
        a, b = [str(ca[i][f]) for i in ids], [str(cb[i][f]) for i in ids]
        out[f] = {
            "raw_agreement": sum(x == y for x, y in zip(a, b, strict=True)) / len(ids),
            "cohen_kappa": _cohen_kappa(a, b), "gwet_ac1": _ac1(a, b),
            "prevalence_unknown_or_zero_A": sum(x in ("unknown", "0") for x in a) / len(ids),
            "disagreements": [i for i, x, y in zip(ids, a, b, strict=True) if x != y],
        }
    a_set = ["+".join(ca[i]["sensor_kinds"]) or "none" for i in ids]
    b_set = ["+".join(cb[i]["sensor_kinds"]) or "none" for i in ids]
    out["sensor_kinds_exact_set"] = {
        "raw_agreement": sum(x == y for x, y in zip(a_set, b_set, strict=True)) / len(ids),
        "cohen_kappa": _cohen_kappa(a_set, b_set), "gwet_ac1": _ac1(a_set, b_set),
        "disagreements": {i: [x, y] for i, x, y in zip(ids, a_set, b_set, strict=True) if x != y},
    }
    a_any = ["any" if ca[i]["sensor_kinds"] else "none" for i in ids]
    b_any = ["any" if cb[i]["sensor_kinds"] else "none" for i in ids]
    out["sensor_kinds_any_anomaly"] = {
        "raw_agreement": sum(x == y for x, y in zip(a_any, b_any, strict=True)) / len(ids),
        "cohen_kappa": _cohen_kappa(a_any, b_any), "gwet_ac1": _ac1(a_any, b_any),
    }
    return out


def _basic_counts(table: Table, judges: list[str]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for j in judges:
        out[j] = {}
        for (jj, ctx), rows in sorted(table.items()):
            if jj != j:
                continue
            reps = _reps(table, j, ctx)
            out[j][ctx] = {
                f"rep{r}": {
                    "items": len(_items(table, j, ctx, r)),
                    "above_floor": sum(int(a > f) for a, f in _items(table, j, ctx, r).values()),
                    "below_floor": sum(int(a < f) for a, f in _items(table, j, ctx, r).values()),
                    "rows_with_error": sum(1 for (c, rr), row in rows.items() if rr == r and row.get("error")),
                }
                for r in reps
            }
    return out


def summarize(raw_dir: Path) -> dict[str, Any]:
    raw = Path(raw_dir)
    table, design = _load(raw)
    judges = [j for j in (*LOCAL, *CLOUD) if any(jj == j for jj, _ in table)]
    return {
        "note": ("Exploratory cycle-4 unit. No model answer or draft label is ground truth; primary metrics are "
                 "label-free (answer above/below the floor)."),
        "judges": judges,
        "counts": _basic_counts(table, judges),
        "c42_wording": _c42(table, design, judges),
        "c43_fields": _c43(table, design, judges),
        "rule_vs_models": _rule_vs_models(table, judges),
        "coder_agreement_A_vs_B": _coder_agreement(design),
        "design": {k: design.get(k) for k in ("exclusions", "tone_mean_by_pass")},
    }
