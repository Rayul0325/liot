"""Gather everything ``analyze.py`` needs into ``raw/`` (analyze reads only ``raw/``).

    PYTHONPATH=$PWD .venv/bin/python -m experiments.e11_wording_fields.collect

  raw/answers_local.jsonl   local-judge answers: new E11 arms from raw/local/<model>.jsonl + the E5 baselines
                            (sentence, operational; deterministic letter-probability answers; both reps)
  raw/answers_rule.jsonl    rule-lookup answers on the same fields (map committed before any model run)
  raw/design.jsonl          accepted rewrites, exclusions, checker tone ratings, coder A/B field values
All three are rebuilt idempotently (existing rows are skipped by key).
"""

from __future__ import annotations

import json
from typing import Any

from experiments.common import io
from experiments.e5_models.benchkit import LOCAL_MODELS, SEVERITIES, TIERS, run_bench
from experiments.e11_wording_fields import rule, variants
from experiments.e11_wording_fields.prep_inputs import HERE

RAW = HERE / "raw"
E5_RAW = HERE.parent / "e5_models" / "raw"


def _probs(p: dict[str, dict[str, float]] | None, q: str, order: tuple[str, ...]) -> list[float] | None:
    if not p or q not in p:
        return None
    return [round(float(p[q][k]), 4) for k in order]


def collect_local() -> int:
    path = RAW / "answers_local.jsonl"
    done = io.done_keys(path, ("judge", "context", "id", "rep"))
    n = 0
    for m in LOCAL_MODELS:
        for r in io.read_jsonl(RAW / "local" / f"{m}.jsonl"):
            arm, cid = r["id"].split(":", 1)
            if (m, arm, cid, r["rep"]) in done:
                continue
            err = r.get("error")
            io.append_jsonl(path, {
                "judge": m, "context": arm, "id": cid, "rep": r["rep"], "group": r["group"],
                "injection": r.get("injection"), "tier_floor": r["tier_floor"], "severity_floor": r["severity_floor"],
                "tier": r.get("model_tier"), "severity": r.get("model_severity"),
                "p_tier": _probs(r.get("probs"), "tier", TIERS), "p_sev": _probs(r.get("probs"), "severity", SEVERITIES),
                "error": None if err is None else str(err)[:80], "seconds": r.get("seconds"),
                "load1": r.get("load1_before"), "source": "e11",
            })
            n += 1
    for r in io.read_jsonl(E5_RAW / "answers_local.jsonl"):
        if r["judge"] not in LOCAL_MODELS or r["context"] not in variants.BASELINE_ARMS or r.get("injection"):
            continue
        key = (r["judge"], r["context"], r["id"], r["rep"])
        if key in done:
            continue
        keep = {k: r[k] for k in (
            "judge", "context", "id", "rep", "group", "injection", "tier_floor", "severity_floor", "tier",
            "severity", "p_tier", "p_sev", "error", "seconds", "load1")}
        io.append_jsonl(path, {**keep, "source": "e5/raw/answers_local.jsonl"})
        n += 1
    return n


def collect_rule() -> int:
    path = RAW / "answers_rule.jsonl"
    done = io.done_keys(path, ("judge", "context", "id", "rep"))
    m = rule.load_map()
    fields = variants.load_fields()
    built = variants.build_arm("sentence")
    arms: dict[str, tuple[str, ...]] = {"operational": (), **variants.FIELD_ARMS}
    n = 0
    for arm, present in arms.items():
        for b in built:
            key = ("rule-lookup", arm, b.cid, 0)
            if key in done:
                continue
            trig = "sensor_reading" if b.spec.get("situation") else "proactive"
            h = rule.hazard(m, fields[b.cid], present, trig)
            sev_floor = None if b.severity_floor is None else b.severity_floor.name.lower()
            io.append_jsonl(path, {
                "judge": "rule-lookup", "context": arm, "id": b.cid, "rep": 0, "group": b.spec["group"],
                "injection": None, "tier_floor": b.gate1.floor_tier.name.lower(), "severity_floor": sev_floor,
                "h": h, "tier": TIERS[h], "severity": SEVERITIES[h] if sev_floor is not None else None,
                "p_tier": None, "p_sev": None, "error": None, "seconds": None,
            })
            n += 1
    return n


def collect_design() -> int:
    from experiments.e11_wording_fields.combine import failures

    path = RAW / "design.jsonl"
    if io.done_keys(path, ("kind",)):
        return 0
    inp = HERE / "inputs"
    load = lambda name: json.loads((inp / name).read_text(encoding="utf-8"))  # noqa: E731
    mp_a, mp_b = load("anon_codeA.json"), load("anon_codeB.json")
    code_a, code_b = load("codeA_output.json"), load("codeB_output.json")
    tone: dict[str, Any] = {}
    for tag in ("pass1", "pass2"):
        if (inp / f"llm_check_{tag}.json").exists():
            _, t = failures(tag)
            tone[tag] = {arm: sum(v) / len(v) for arm, v in t.items()}
    rb = run_bench()
    cases = {b.cid: b.spec for b in rb.build_all(rb.load_cases(), "sentence")}
    pairs = {cid: {"pair": s.get("pair"), "variant": s.get("variant")} for cid, s in cases.items() if s.get("pair")}
    io.append_jsonl(path, {
        "kind": "design",
        "accepted": load("accepted_rewrites.json"),
        "exclusions": load("exclusions.json"),
        "tone_mean_by_pass": tone,
        "pair_variant": pairs,
        "codes_A": {cid: code_a[a] for a, cid in mp_a.items()},
        "codes_B": {cid: code_b[a] for a, cid in mp_b.items()},
    })
    return 1


if __name__ == "__main__":
    print("local rows", collect_local(), "rule rows", collect_rule(), "design", collect_design())
