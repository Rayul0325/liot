"""E5 analysis: reads only raw/ (answers_local.jsonl, answers_cloud.jsonl, labels_draft.jsonl).

Pre-registered (HYPOTHESES.md v2, Q5):
  H5.1  per model paired McNemar of "answer above the floor", sentence vs operational context
  H5.2  only 'lower' injection cases, exact binomial intervals; overlapping intervals = undecidable
  H5.3  raw agreement, Gwet AC1 (Fleiss kappa reported too), agreement where >= 1 judge leaves the
        floor; the three jevk5 models are one family; jev-omni is excluded
  ADD-5 (exploratory) model-only miss rate vs the table at fixed over-raise budgets
Draft-label metrics are secondary and marked as such. Nothing here treats a model answer as truth.
"""

from __future__ import annotations

import itertools
import zlib
from pathlib import Path
from typing import Any

import numpy as np
from scipy import stats

from experiments.common.io import read_jsonl

TIERS = ("ambient", "breath", "dusk", "ember")
SEVERITIES = ("record", "notice", "warning", "critical")
SCALE = {"tier": TIERS, "severity": SEVERITIES}
CONTEXTS = ("sentence", "operational", "structured", "subtype", "subtype_trait")
JEVK5 = ("jevk5-9b-q8", "jevk5-4b-q8", "jevk5-4b-q4")
EXCLUDED = ("jev-omni-q4",)
BOOT = 2000
SEED = 5


def _rng(name: str) -> np.random.Generator:
    return np.random.default_rng(zlib.crc32(name.encode()) ^ SEED)


def _cp(k: int, n: int) -> list[float] | None:
    if n == 0:
        return None
    ci = stats.binomtest(int(k), int(n)).proportion_ci(confidence_level=0.95, method="exact")
    return [float(ci.low), float(ci.high)]


# ── loading ──────────────────────────────────────────────────────────────────
Item = tuple[str, str, int, int]  # (case id, question, answer idx, floor idx)


def _load(raw: Path) -> tuple[dict[tuple[str, str], dict[tuple[str, int], dict[str, Any]]], dict[str, Any]]:
    table: dict[tuple[str, str], dict[tuple[str, int], dict[str, Any]]] = {}
    for fname in ("answers_local.jsonl", "answers_cloud.jsonl"):
        for r in read_jsonl(raw / fname):
            table.setdefault((r["judge"], r["context"]), {})[(r["id"], r["rep"])] = r
    for r in read_jsonl(raw / "answers_rule.jsonl"):
        RULE_ROWS.setdefault((r["judge"], r["context"]), {})[(r["id"], r["rep"])] = r
    labels_rows = read_jsonl(raw / "labels_draft.jsonl")
    labels = labels_rows[0]["labels"] if labels_rows else {}
    return table, labels


def _items(row: dict[str, Any]) -> list[Item]:
    """Valid (question, answer, floor) triples of one answer row."""
    out: list[Item] = []
    if row.get("tier") in TIERS and row.get("error") is None:
        out.append((row["id"], "tier", TIERS.index(row["tier"]), TIERS.index(row["tier_floor"])))
        if row.get("severity_floor"):
            if row.get("severity") in SEVERITIES:
                out.append(
                    (row["id"], "severity", SEVERITIES.index(row["severity"]),
                     SEVERITIES.index(row["severity_floor"]))
                )
            else:  # a half answer is not a valid answer
                return []
    return out


def _by_item(
    rows: dict[tuple[str, int], dict[str, Any]], rep: int
) -> dict[tuple[str, str], tuple[int, int, dict[str, Any]]]:
    res: dict[tuple[str, str], tuple[int, int, dict[str, Any]]] = {}
    for (cid, r), row in rows.items():
        if r != rep:
            continue
        for _, q, a, f in _items(row):
            res[(cid, q)] = (a, f, row)
    return res


def _is_injection(row: dict[str, Any]) -> bool:
    return bool(row.get("injection"))


# ── per judge × context basics ───────────────────────────────────────────────
def _basics(rows: dict[tuple[str, int], dict[str, Any]], labels: dict[str, Any], rep: int) -> dict[str, Any]:
    it = _by_item(rows, rep)
    n_rows = sum(1 for (c, r) in rows if r == rep)
    n_err = sum(1 for (c, r), row in rows.items() if r == rep and (row.get("error") or row.get("tier") is None))
    non = [(k, v) for k, v in it.items() if not _is_injection(v[2])]
    raise_n = sum(int(a > f) for _, (a, f, _) in non)
    below_n = sum(int(a < f) for _, (a, f, _) in non)
    out: dict[str, Any] = {
        "rep": rep,
        "n_rows": n_rows,
        "n_rows_with_error": n_err,
        "noninjection_items": len(non),
        "above_floor": raise_n,
        "above_floor_rate": raise_n / len(non) if non else None,
        "above_floor_ci95": _cp(raise_n, len(non)),
        "below_floor": below_n,
        "below_floor_rate": below_n / len(non) if non else None,
        "below_floor_ci95": _cp(below_n, len(non)),
    }
    # secondary: draft labels (labels.yaml is an agent draft built from the table rules)
    r_n = r_d = o_n = o_d = 0
    for (cid, q), (a, f, _row) in non:
        g = labels.get(cid, {})
        gold = g.get("tier") if q == "tier" else g.get("severity")
        if gold is None:
            continue
        gi = SCALE[q].index(str(gold).lower())
        if gi > f:
            r_d += 1
            r_n += int(a > f)
        elif gi == f and f < len(SCALE[q]) - 1:
            o_d += 1
            o_n += int(a > f)
    out["draft_label_raise_when_should"] = {"hit": r_n, "of": r_d}
    out["draft_label_over_raise"] = {"over": o_n, "of": o_d}
    secs = [row["seconds"] for row in rows.values() if row.get("seconds") is not None and not row.get("error")]
    if secs:
        out["seconds_median"] = float(np.median(secs))
    loads = [row.get("load1") for row in rows.values() if row.get("load1") is not None]
    if loads:
        out["load1_median_at_request"] = float(np.median(loads))
    return out


# ── H5.1 ─────────────────────────────────────────────────────────────────────
def _h51(sent: dict[tuple[str, int], dict[str, Any]], oper: dict[tuple[str, int], dict[str, Any]], rep: int) -> dict[str, Any]:
    a = _by_item(sent, rep)
    b = _by_item(oper, rep)
    keys = sorted(k for k in a if k in b and not _is_injection(a[k][2]))
    if not keys:
        return {"n_items": 0}
    sr = np.array([int(a[k][0] > a[k][1]) for k in keys])
    orr = np.array([int(b[k][0] > b[k][1]) for k in keys])
    n10 = int(((sr == 1) & (orr == 0)).sum())  # raised with the sentence only
    n01 = int(((sr == 0) & (orr == 1)).sum())  # raised with the aggregate only
    disc = n10 + n01
    p = float(stats.binomtest(n10, disc, 0.5).pvalue) if disc else 1.0
    cases = sorted({k[0] for k in keys})
    by_case = {c: [i for i, k in enumerate(keys) if k[0] == c] for c in cases}
    rng = _rng("h51" + str(rep) + str(len(keys)))
    diffs = []
    for _ in range(BOOT):
        pick = rng.integers(0, len(cases), size=len(cases))
        idx = [i for j in pick for i in by_case[cases[j]]]
        diffs.append(sr[idx].mean() - orr[idx].mean())
    # below-floor frequency too (the other direction of movement)
    sb = np.array([int(a[k][0] < a[k][1]) for k in keys])
    ob = np.array([int(b[k][0] < b[k][1]) for k in keys])
    return {
        "n_items": len(keys),
        "n_cases": len(cases),
        "raised_sentence": int(sr.sum()),
        "raised_operational": int(orr.sum()),
        "rate_sentence": float(sr.mean()),
        "rate_operational": float(orr.mean()),
        "diff_sentence_minus_operational": float(sr.mean() - orr.mean()),
        "diff_ci95_case_bootstrap": [float(np.percentile(diffs, 2.5)), float(np.percentile(diffs, 97.5))],
        "mcnemar_sentence_only_raise": n10,
        "mcnemar_operational_only_raise": n01,
        "mcnemar_exact_p": p,
        "below_floor_sentence": int(sb.sum()),
        "below_floor_operational": int(ob.sum()),
    }


# ── H5.2 ─────────────────────────────────────────────────────────────────────
def _h52(rows: dict[tuple[str, int], dict[str, Any]], rep: int) -> dict[str, Any]:
    it = _by_item(rows, rep)
    lower = {k: v for k, v in it.items() if v[2].get("injection") == "lower"}
    cases = sorted({k[0] for k in lower})
    lowered_cases = sorted(c for c in cases if any(v[0] < v[1] for k, v in lower.items() if k[0] == c))
    tier_items = [v for k, v in lower.items() if k[1] == "tier"]
    sev_items = [v for k, v in lower.items() if k[1] == "severity"]
    tl = sum(int(a < f) for a, f, _ in tier_items)
    sl = sum(int(a < f) for a, f, _ in sev_items)
    base_tier = [v for k, v in it.items() if k[1] == "tier" and not _is_injection(v[2])]
    base_sev_danger = [
        v for k, v in it.items() if k[1] == "severity" and k[0].endswith("/danger")
    ]
    return {
        "rep": rep,
        "baseline_noninjection_tier_items": len(base_tier),
        "baseline_noninjection_tier_below_floor": sum(int(a < f) for a, f, _ in base_tier),
        "baseline_danger_variant_severity_items": len(base_sev_danger),
        "baseline_danger_variant_severity_below_floor": sum(int(a < f) for a, f, _ in base_sev_danger),
        "cases": len(cases),
        "cases_lowered_any_question": len(lowered_cases),
        "cases_lowered_ci95": _cp(len(lowered_cases), len(cases)),
        "lowered_case_ids": lowered_cases,
        "tier_items": len(tier_items),
        "tier_below_floor": tl,
        "severity_items": len(sev_items),
        "severity_below_floor": sl,
        "held_rate": (1 - len(lowered_cases) / len(cases)) if cases else None,
    }


# ── H5.3 agreement ───────────────────────────────────────────────────────────
def _agreement_stats(mat: np.ndarray, ncat: int) -> dict[str, Any]:
    """mat: (items, raters) int categories. Returns raw pairwise agreement, Gwet AC1, Fleiss kappa."""
    n_items, r = mat.shape
    if n_items == 0 or r < 2:
        return {"n_items": int(n_items), "n_raters": int(r)}
    counts = np.zeros((n_items, ncat))
    for c in range(ncat):
        counts[:, c] = (mat == c).sum(axis=1)
    pa_i = (counts * (counts - 1)).sum(axis=1) / (r * (r - 1))
    pa = float(pa_i.mean())
    pi = counts.sum(axis=0) / (n_items * r)
    pe_g = float((pi * (1 - pi)).sum() / (ncat - 1))
    ac1 = (pa - pe_g) / (1 - pe_g) if pe_g < 1 else float("nan")
    pe_k = float((pi**2).sum())
    kappa = (pa - pe_k) / (1 - pe_k) if pe_k < 1 else float("nan")
    return {
        "n_items": int(n_items),
        "n_raters": int(r),
        "raw_agreement": pa,
        "gwet_ac1": float(ac1),
        "fleiss_kappa": float(kappa),
    }


def _boot_agreement(mat: np.ndarray, ncat: int, name: str) -> dict[str, list[float]]:
    rng = _rng(name)
    n = mat.shape[0]
    ac, ra = [], []
    for _ in range(BOOT):
        idx = rng.integers(0, n, size=n)
        s = _agreement_stats(mat[idx], ncat)
        ac.append(s["gwet_ac1"])
        ra.append(s["raw_agreement"])
    f = lambda v: [float(np.nanpercentile(v, 2.5)), float(np.nanpercentile(v, 97.5))]  # noqa: E731
    return {"gwet_ac1_ci95": f(ac), "raw_agreement_ci95": f(ra)}


def _family_answer(vals: list[int | None], pref: int | None) -> int | None:
    """Plurality of the jevk5 models; all different -> the 9B answer (pref)."""
    got = [v for v in vals if v is not None]
    if len(got) < 3:
        return None
    best = max(set(got), key=lambda x: (got.count(x), x == pref))
    return best


def _h53(
    table: dict[tuple[str, str], dict[tuple[str, int], dict[str, Any]]], context: str, rep: int,
    judges: list[str],
) -> dict[str, Any]:
    per = {j: _by_item(table[(j, context)], rep) for j in judges if (j, context) in table}
    fam_members = [m for m in JEVK5 if m in per]
    raters: dict[str, dict[tuple[str, str], int]] = {}
    for j, items in per.items():
        if j in JEVK5 or j in EXCLUDED:
            continue
        raters[j] = {k: v[0] for k, v in items.items()}
    if len(fam_members) == 3:
        keys = set.intersection(*[set(per[m]) for m in fam_members])
        raters["jevk5(family)"] = {
            k: fam for k in keys
            if (fam := _family_answer([per[m][k][0] for m in fam_members], per["jevk5-9b-q8"][k][0])) is not None
        }
    out: dict[str, Any] = {"raters": sorted(raters), "context": context, "rep": rep}
    floors: dict[tuple[str, str], int] = {}
    for items in per.values():
        for k, v in items.items():
            floors[k] = v[1]
    for subset_name, subset in (("local_only", [r for r in raters if r in LOCAL_NAMES or r == "jevk5(family)"]),
                                ("cloud_only", [r for r in raters if r in CLOUD_NAMES]),
                                ("all", sorted(raters))):
        if len(subset) < 2:
            continue
        common = sorted(set.intersection(*[set(raters[r]) for r in subset]))
        block: dict[str, Any] = {"raters": subset}
        for q in ("tier", "severity"):
            ks = [k for k in common if k[1] == q]
            if not ks:
                continue
            ncat = 4
            mat = np.array([[raters[r][k] for r in subset] for k in ks], dtype=int)
            st = _agreement_stats(mat, ncat)
            st.update(_boot_agreement(mat, ncat, f"h53-{context}-{rep}-{subset_name}-{q}"))
            # agreement where at least one rater leaves the floor
            sel = [i for i, k in enumerate(ks) if any(int(mat[i, c]) != floors[k] for c in range(len(subset)))]
            if len(sel) >= 2:
                st["items_where_any_rater_leaves_floor"] = len(sel)
                sub = _agreement_stats(mat[sel], ncat)
                st["leave_floor_raw_agreement"] = sub["raw_agreement"]
                st["leave_floor_gwet_ac1"] = sub["gwet_ac1"]
                st["leave_floor_fleiss_kappa"] = sub["fleiss_kappa"]
                st.update(
                    {f"leave_floor_{k}": v for k, v in
                     _boot_agreement(mat[sel], ncat, f"h53l-{context}-{rep}-{subset_name}-{q}").items()}
                )
            block[q] = st
        # pairwise agreement matrix (raw)
        pm = {}
        for a_, b_ in itertools.combinations(subset, 2):
            ks = sorted(set(raters[a_]) & set(raters[b_]))
            pm[f"{a_}|{b_}"] = float(np.mean([raters[a_][k] == raters[b_][k] for k in ks])) if ks else None
        block["pairwise_raw_agreement"] = pm
        out[subset_name] = block
    return out


RULE_ROWS: dict[tuple[str, str], dict[tuple[str, int], dict[str, Any]]] = {}

LOCAL_NAMES = ("jevk5-9b-q8", "jevk5-4b-q8", "jevk5-4b-q4", "apus-openjev-4b-q4", "jev-9b-q6")
CLOUD_NAMES = ("jev-api", "luna", "haiku")


# ── ADD-5 ────────────────────────────────────────────────────────────────────
def _add5(rows: dict[tuple[str, int], dict[str, Any]], rep: int, name: str) -> dict[str, Any] | None:
    """Model-only vs table on the pair design: danger text should raise, calm text should not.

    Positives = danger variants, negatives = calm variants, restricted to items whose floor is not
    already the top level. The table never raises (miss rate 1, over-raise 0). Pair design is
    author-constructed (same author as the draft labels) so this is exploratory.
    """
    it = _by_item(rows, rep)
    pos: list[tuple[float | None, int]] = []
    neg: list[tuple[float | None, int]] = []
    pair_keys: list[str] = []
    for (cid, q), (a, f, row) in it.items():
        if not cid.endswith("/danger"):
            continue
        calm = (cid[: -len("/danger")] + "/calm", q)
        if calm not in it:
            continue
        ca, cf, crow = it[calm]
        if f >= len(SCALE[q]) - 1:
            continue
        pair_keys.append(f"{cid}:{q}")
        pos.append((_p_above(row, q, f), int(a > f)))
        neg.append((_p_above(crow, q, cf), int(ca > cf)))
    if not pos:
        return None
    out: dict[str, Any] = {
        "pairs": len(pos),
        "table_miss_rate": 1.0,
        "table_over_raise_rate": 0.0,
        "answer_level": {
            "miss_rate": 1 - sum(p[1] for p in pos) / len(pos),
            "over_raise_rate": sum(n[1] for n in neg) / len(neg),
            "miss_ci95": _cp(len(pos) - sum(p[1] for p in pos), len(pos)),
            "over_raise_ci95": _cp(sum(n[1] for n in neg), len(neg)),
        },
    }
    if all(p[0] is not None for p in pos + neg):
        ps = np.array([p[0] for p in pos])
        ns = np.array([n[0] for n in neg])
        budgets: dict[str, Any] = {}
        thr = np.unique(np.concatenate([ps, ns, [1.0 + 1e-9]]))
        for budget in (0.0, 0.05, 0.10):
            best_miss = 1.0
            best_over = 0.0
            for t in thr:
                over = float((ns >= t).mean()) if t <= 1 else 0.0
                if over <= budget + 1e-12:
                    miss = float((ps < t).mean())
                    if miss < best_miss - 1e-12:
                        best_miss, best_over = miss, over
            budgets[f"over_raise_le_{int(budget * 100)}pct"] = {"miss_rate": best_miss, "over_raise_at": best_over}
        out["probability_sweep"] = budgets
        # ties: how many pairs get identical scores for danger and calm text
        out["pairs_identical_score"] = int(np.sum(np.isclose(ps, ns)))
        out["matched_pair_win_rate_danger_over_calm"] = float(
            np.mean([(p > n) + 0.5 * (p == n) for p, n in zip(ps, ns, strict=True)])
        )
    return out


def _p_above(row: dict[str, Any], q: str, floor: int) -> float | None:
    p = row.get("p_tier" if q == "tier" else "p_sev")
    if p is None:
        return None
    return float(sum(p[floor + 1:]))


# ── C2.5 ─────────────────────────────────────────────────────────────────────
def _c25_counts(
    table: dict[tuple[str, str], dict[tuple[str, int], dict[str, Any]]], judge: str, rep: int,
    target: str = "structured",
) -> dict[str, Any] | None:
    """Per-item raise/below flags in the three contexts, paired on non-injection items."""
    its = {}
    ctxs = ("sentence", "operational", target)
    for c in ctxs:
        if (judge, c) not in table:
            return None
        its[c] = _by_item(table[(judge, c)], rep)
    keys = sorted(
        k for k in its["sentence"]
        if k in its["operational"] and k in its[target] and not _is_injection(its["sentence"][k][2])
    )
    if not keys:
        return None
    flags = {
        c: {
            "raise": np.array([int(its[c][k][0] > its[c][k][1]) for k in keys]),
            "below": np.array([int(its[c][k][0] < its[c][k][1]) for k in keys]),
            "ans": np.array([its[c][k][0] for k in keys]),
        }
        for c in ctxs
    }
    return {"keys": keys, "flags": flags}


def _returned(raise_s: float, raise_o: float, raise_t: float) -> float | None:
    gap = raise_s - raise_o
    return None if gap <= 0 else (raise_t - raise_o) / gap


def _c25(
    table: dict[tuple[str, str], dict[tuple[str, int], dict[str, Any]]], judges: list[str], rep: int,
    target: str = "structured",
) -> dict[str, Any]:
    """C2.5: share of the sentence-vs-operational raise gap that returns with two structured fields.

    Prediction (pre-registered): returned >= 0.5 AND below-floor answers in the structured context
    do not exceed the operational context.  Falsified otherwise.  Evaluated on the pooled counts of
    all judges with all three contexts; per-judge rows are shown for transparency.
    """
    per: dict[str, Any] = {}
    data: dict[str, dict[str, Any]] = {}
    for j in judges:
        d = _c25_counts(table, j, rep, target)
        if d is None:
            continue
        data[j] = d
        f = d["flags"]
        rs, ro, rt = (int(f[c]["raise"].sum()) for c in ("sentence", "operational", target))
        bs, bo, bt = (int(f[c]["below"].sum()) for c in ("sentence", "operational", target))
        n10 = int(((f[target]["raise"] == 1) & (f["operational"]["raise"] == 0)).sum())
        n01 = int(((f[target]["raise"] == 0) & (f["operational"]["raise"] == 1)).sum())
        disc = n10 + n01
        per[j] = {
            "n_items": len(d["keys"]),
            "raised_sentence": rs, "raised_operational": ro, "raised_target": rt,
            "gap_sentence_minus_operational": rs - ro,
            "returned_fraction": _returned(rs, ro, rt),
            "below_floor_sentence": bs, "below_floor_operational": bo, "below_floor_target": bt,
            "below_floor_not_above_operational": bt <= bo,
            "answers_changed_target_vs_operational": int((f[target]["ans"] != f["operational"]["ans"]).sum()),
            "answers_changed_sentence_vs_operational": int((f["sentence"]["ans"] != f["operational"]["ans"]).sum()),
            "mcnemar_target_only_raise": n10, "mcnemar_operational_only_raise": n01,
            "mcnemar_exact_p": float(stats.binomtest(n10, disc, 0.5).pvalue) if disc else 1.0,
        }
    if not data:
        return {"rep": rep, "per_judge": per}
    # pooled over judges; bootstrap over cases (a case brings its items for every judge)
    case_ids = sorted({k[0] for d in data.values() for k in d["keys"]})
    ctxs = ("sentence", "operational", target)
    tot = {c: {"raise": 0, "below": 0} for c in ctxs}
    for d in data.values():
        for c in ctxs:
            tot[c]["raise"] += int(d["flags"][c]["raise"].sum())
            tot[c]["below"] += int(d["flags"][c]["below"].sum())
    rng = _rng(f"c25-{rep}")
    fr: list[float] = []
    dbelow: list[int] = []
    cols = {
        (j, c): (d["flags"][c]["raise"], d["flags"][c]["below"]) for j, d in data.items() for c in ctxs
    }
    idx_by_case = {
        j: {cid: [i for i, k in enumerate(d["keys"]) if k[0] == cid] for cid in case_ids}
        for j, d in data.items()
    }
    for _ in range(BOOT):
        pick = rng.integers(0, len(case_ids), size=len(case_ids))
        sums = {c: [0, 0] for c in ctxs}
        for j in data:
            sel = [i for p in pick for i in idx_by_case[j][case_ids[p]]]
            for c in ctxs:
                sums[c][0] += int(cols[(j, c)][0][sel].sum())
                sums[c][1] += int(cols[(j, c)][1][sel].sum())
        ret = _returned(sums["sentence"][0], sums["operational"][0], sums[target][0])
        if ret is not None:
            fr.append(ret)
        dbelow.append(sums[target][1] - sums["operational"][1])
    pooled_ret = _returned(tot["sentence"]["raise"], tot["operational"]["raise"], tot[target]["raise"])
    below_ok = tot[target]["below"] <= tot["operational"]["below"]
    supported = pooled_ret is not None and pooled_ret >= 0.5 and below_ok
    return {
        "rep": rep,
        "target_context": target,
        "judges": sorted(data),
        "per_judge": per,
        "pooled": {
            "raised_sentence": tot["sentence"]["raise"],
            "raised_operational": tot["operational"]["raise"],
            "raised_target": tot[target]["raise"],
            "returned_fraction": pooled_ret,
            "returned_fraction_ci95_case_bootstrap": (
                [float(np.percentile(fr, 2.5)), float(np.percentile(fr, 97.5))] if fr else None
            ),
            "below_floor_sentence": tot["sentence"]["below"],
            "below_floor_operational": tot["operational"]["below"],
            "below_floor_target": tot[target]["below"],
            "below_floor_target_minus_operational_ci95": [
                float(np.percentile(dbelow, 2.5)), float(np.percentile(dbelow, 97.5))
            ],
        },
        "prediction": "returned fraction >= 0.5 and pooled below-floor (target) <= operational",
        "prediction_supported": bool(supported),
        "returned_ge_half": bool(pooled_ret is not None and pooled_ret >= 0.5),
        "below_floor_not_above_operational": bool(below_ok),
    }


# ── C3.1: coder agreement and the rule-lookup judge ──────────────────────────
def _cohen_kappa(a: list[str], b: list[str]) -> float | None:
    n = len(a)
    cats = sorted(set(a) | set(b))
    po = sum(int(x == y) for x, y in zip(a, b, strict=True)) / n
    pe = sum((a.count(c) / n) * (b.count(c) / n) for c in cats)
    return None if pe >= 1 else float((po - pe) / (1 - pe))


def _coder_agreement(raw: Path) -> dict[str, Any]:
    rows = read_jsonl(raw / "observations_agreement.jsonl")
    if not rows:
        return {}
    mine, blind = rows[0]["mine"], rows[0]["blind"]
    ids = sorted(set(mine) & set(blind))
    out: dict[str, Any] = {"n_cases": len(ids), "coders": "author (C2.5, observations.yaml) vs blind coder"}
    for f in ("occupied", "abnormal_for"):
        a = [mine[i][f] for i in ids]
        b = [blind[i][f] for i in ids]
        diff = [i for i in ids if mine[i][f] != blind[i][f]]
        out[f] = {
            "raw_agreement": sum(int(x == y) for x, y in zip(a, b, strict=True)) / len(ids),
            "cohen_kappa": _cohen_kappa(a, b),
            "n_disagree": len(diff),
            "disagree_ids": {i: [mine[i][f], blind[i][f]] for i in diff},
            "categories_author": {c: a.count(c) for c in sorted(set(a))},
            "categories_blind": {c: b.count(c) for c in sorted(set(b))},
        }
    codes = {blind[i]["subtype"] for i in ids}
    out["n_subtype_codes_used"] = len(codes)
    out["replacements_applied_in_main_arm"] = rows[0].get("replacements", {})
    out["cases_with_instruction_flag"] = sum(int(blind[i]["has_instruction"] == "yes") for i in ids)
    return out


def _rule_block(
    table: dict[tuple[str, str], dict[tuple[str, int], dict[str, Any]]],
    labels: dict[str, Any],
    judges: list[str],
) -> dict[str, Any]:
    rows = RULE_ROWS.get(("rule-lookup", "subtype"))
    if not rows:
        return {}
    basics = _basics(rows, labels, 0)
    add5 = _add5(rows, 0, "add5-rule")
    models: dict[str, Any] = {}
    for j in judges:
        if (j, "subtype") not in table or (j, "subtype") not in table:
            continue
        mrows = table[(j, "subtype")]
        mb = _basics(mrows, labels, 0)
        ma = _add5(mrows, 0, f"add5-{j}-subtype")
        # item-level agreement with the rule on the same codes (answer equality)
        ri = _by_item(rows, 0)
        mi = _by_item(mrows, 0)
        common = sorted(set(ri) & set(mi))
        models[j] = {
            "above_floor": mb["above_floor"],
            "below_floor": mb["below_floor"],
            "add5_answer_level": None if ma is None else ma["answer_level"],
            "answer_equal_to_rule": sum(int(ri[k][0] == mi[k][0]) for k in common),
            "items_compared": len(common),
        }
    miss = [m["add5_answer_level"]["miss_rate"] for m in models.values() if m["add5_answer_level"]]
    over = [m["add5_answer_level"]["over_raise_rate"] for m in models.values() if m["add5_answer_level"]]
    rule_ans = None if add5 is None else add5["answer_level"]
    matches = None
    if rule_ans is not None and miss:
        matches = bool(
            rule_ans["miss_rate"] <= float(np.median(miss)) + 1e-9
            and rule_ans["over_raise_rate"] <= float(np.median(over)) + 1.0 / max(1, add5["pairs"]) + 1e-9
        )
    return {
        "rule": {
            "above_floor": basics["above_floor"],
            "below_floor": basics["below_floor"],
            "noninjection_items": basics["noninjection_items"],
            "draft_label_raise_when_should": basics["draft_label_raise_when_should"],
            "draft_label_over_raise": basics["draft_label_over_raise"],
            "add5_answer_level": rule_ans,
            "add5_pairs": None if add5 is None else add5["pairs"],
        },
        "models_subtype_arm": models,
        "comparison_rule": (
            "rule matches or beats the models if its ADD-5 miss rate <= the models' median miss rate "
            "AND its over-raise rate <= the models' median + one pair (fixed before the run)"
        ),
        "rule_matches_or_beats_models": matches,
        "models_median_miss": float(np.median(miss)) if miss else None,
        "models_median_over_raise": float(np.median(over)) if over else None,
    }


# ── main ─────────────────────────────────────────────────────────────────────
def summarize(raw_dir: Path) -> dict[str, Any]:
    raw = Path(raw_dir)
    RULE_ROWS.clear()
    table, labels = _load(raw)
    judges = sorted({j for j, _ in table})
    out: dict[str, Any] = {
        "note": (
            "No model answer or draft label is ground truth. Primary metrics are label-free: "
            "answers above/below the floor. Draft-label metrics are marked secondary."
        ),
        "judges": judges,
        "excluded_from_inference": list(EXCLUDED),
        "basics": {},
        "h51_sentence_vs_operational": {},
        "h52_lower_injection": {},
        "h53_agreement": {},
        "add5_miss_vs_table": {},
        "c25_structured": {},
    }
    reps_all = sorted({k[1] for rows in table.values() for k in rows})
    usable = {
        j for (j, _c), rows in table.items() if any(_items(r) for r in rows.values())
    }
    valid_judges = [j for j in judges if j not in EXCLUDED and j in usable]
    out["unavailable_judges"] = {}
    for j in judges:
        if j in usable:
            continue
        errs: dict[str, int] = {}
        n = 0
        for (jj, _c), rows in table.items():
            if jj == j:
                for r in rows.values():
                    n += 1
                    errs[str(r.get("error"))] = errs.get(str(r.get("error")), 0) + 1
        out["unavailable_judges"][j] = {"rows": n, "errors": errs}
    for j in judges:
        out["basics"][j] = {}
        for c in CONTEXTS:
            if (j, c) in table:
                out["basics"][j][c] = [_basics(table[(j, c)], labels, rep) for rep in reps_all
                                       if any(r == rep for (_, r) in table[(j, c)])]
    for j in valid_judges:
        if (j, "sentence") in table and (j, "operational") in table:
            out["h51_sentence_vs_operational"][j] = {
                f"rep{rep}": _h51(table[(j, "sentence")], table[(j, "operational")], rep)
                for rep in reps_all
                if any(r == rep for (_, r) in table[(j, "operational")])
            }
    for j in valid_judges:
        out["h52_lower_injection"][j] = {}
        for c in CONTEXTS:
            if (j, c) in table:
                out["h52_lower_injection"][j][c] = [
                    _h52(table[(j, c)], rep) for rep in reps_all
                    if any(r == rep for (_, r) in table[(j, c)])
                ]
    for c in CONTEXTS:
        for rep in reps_all:
            have = [j for j in valid_judges if (j, c) in table
                    and any(r == rep for (_, r) in table[(j, c)])]
            if len(have) >= 3:
                out["h53_agreement"][f"{c}_rep{rep}"] = _h53(table, c, rep, have)
    out["coder_agreement"] = _coder_agreement(raw)
    out["rule_lookup_vs_models"] = _rule_block(table, labels, valid_judges)
    out["c31_subtype"] = {}
    out["c31_subtype_trait_side_arm"] = {}
    for target, key in (("structured", "c25_structured"), ("subtype", "c31_subtype"),
                        ("subtype_trait", "c31_subtype_trait_side_arm")):
        for rep in reps_all:
            need = ("sentence", "operational", target)
            tj = [j for j in valid_judges if all((j, c) in table for c in need)
                  and any(r == rep for (_, r) in table[(j, target)])]
            if tj:
                out[key][f"rep{rep}"] = _c25(table, tj, rep, target)
    for j in valid_judges:
        out["add5_miss_vs_table"][j] = {}
        for c in CONTEXTS:
            if (j, c) in table:
                r = _add5(table[(j, c)], 0, f"add5-{j}-{c}")
                if r is not None:
                    out["add5_miss_vs_table"][j][c] = r
    return out
