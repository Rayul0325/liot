"""E5 runner.

    # 1. local models, operational context (bench runner, one llama-server at a time)
    PYTHONPATH=$PWD zsh experiments/e5_models/run_local_operational.sh
    # 2. cloud judges (budget-capped)
    PYTHONPATH=$PWD .venv/bin/python -m experiments.e5_models.cloud
    # 3. reduce the bench raw files into raw/answers_local.jsonl + labels_draft.jsonl
    PYTHONPATH=$PWD .venv/bin/python -m experiments.e5_models.run
    # 4. summary + figures
    PYTHONPATH=$PWD .venv/bin/python -m experiments.e5_models.run --analyze

The latency re-measurement is NOT run here: use the bench runner's --latency-only mode in a quiet
window (records load1 per request):
    PYTHONPATH=$PWD .venv/bin/python bench/risk/run_bench.py run --model KEY --latency-only
"""

from __future__ import annotations

import argparse
from pathlib import Path
from typing import Any

import yaml

from experiments.common import io
from experiments.e5_models import analyze
from experiments.e5_models.benchkit import (
    BENCH_DIR,
    CONTEXTS,
    EXCLUDED_MODEL,
    LOCAL_MODELS,
    SEVERITIES,
    TIERS,
)

EXP_DIR = Path(__file__).resolve().parent
RAW = EXP_DIR / "raw"
SOURCES = {
    "sentence": BENCH_DIR / "raw",
    "operational": BENCH_DIR / "raw_operational",
    "structured": BENCH_DIR / "raw_structured",
    "subtype": BENCH_DIR / "raw_subtype",
    "subtype_trait": BENCH_DIR / "raw_subtype_trait",
}


def _probs(p: dict[str, dict[str, float]] | None, q: str, order: tuple[str, ...]) -> list[float] | None:
    if not p or q not in p:
        return None
    return [round(float(p[q][k]), 4) for k in order]


def collect_local() -> int:
    path = RAW / "answers_local.jsonl"
    done = io.done_keys(path, ("judge", "context", "id", "rep"))
    written = 0
    for context in CONTEXTS:
        for model in (*LOCAL_MODELS, EXCLUDED_MODEL):
            src = SOURCES[context] / f"{model}.jsonl"
            for r in io.read_jsonl(src):
                key = (model, context, r["id"], r["rep"])
                if key in done:
                    continue
                err = r.get("error")
                io.append_jsonl(
                    path,
                    {
                        "judge": model,
                        "context": context,
                        "id": r["id"],
                        "rep": r["rep"],
                        "group": r["group"],
                        "injection": r.get("injection"),
                        "tier_floor": r["tier_floor"],
                        "severity_floor": r["severity_floor"],
                        "tier": r.get("model_tier"),
                        "severity": r.get("model_severity"),
                        "p_tier": _probs(r.get("probs"), "tier", TIERS),
                        "p_sev": _probs(r.get("probs"), "severity", SEVERITIES),
                        "error": None if err is None else str(err)[:80],
                        "seconds": r.get("seconds"),
                        "load1": r.get("load1_before"),
                        "source": str(src.relative_to(BENCH_DIR.parents[1])),
                        "meta": io.run_meta(),
                    },
                )
                written += 1
    return written


def collect_observations() -> int:
    """Both observation tables (mine = C2.5, blind coder = C3.1) for the inter-coder agreement."""
    path = RAW / "observations_agreement.jsonl"
    if io.done_keys(path, ("kind",)):
        return 0
    mine: dict[str, Any] = yaml.safe_load((BENCH_DIR / "observations.yaml").read_text(encoding="utf-8"))
    blind: dict[str, Any] = yaml.safe_load((BENCH_DIR / "observations_blind.yaml").read_text(encoding="utf-8"))
    repl: dict[str, Any] = yaml.safe_load((BENCH_DIR / "subtype_replacements.yaml").read_text(encoding="utf-8"))

    def norm(x: Any) -> str:
        return "yes" if x is True else "no" if x is False else str(x)

    io.append_jsonl(
        path,
        {
            "kind": "tables",
            "mine": {
                k: {f: norm(v[f]) for f in ("occupied", "abnormal_for")}
                for k, v in mine["cases"].items()
            },
            "blind": {
                k: {f: norm(v[f]) for f in ("subtype", "occupied", "abnormal_for", "has_instruction")}
                for k, v in blind["cases"].items()
            },
            "replacements": repl["replace"],
            "meta": io.run_meta(),
        },
    )
    return 1


def collect_rule() -> int:
    """Rule-lookup judge: each blind subtype code -> hazard score h -> tier/severity (map committed first)."""
    from experiments.e5_models.benchkit import run_bench

    path = RAW / "answers_rule.jsonl"
    done = io.done_keys(path, ("judge", "context", "id", "rep"))
    rmap: dict[str, Any] = yaml.safe_load((BENCH_DIR / "subtype_rule_map.yaml").read_text(encoding="utf-8"))
    blind: dict[str, Any] = yaml.safe_load((BENCH_DIR / "observations_blind.yaml").read_text(encoding="utf-8"))
    repl: dict[str, str] = yaml.safe_load(
        (BENCH_DIR / "subtype_replacements.yaml").read_text(encoding="utf-8")
    )["replace"]
    rb = run_bench()
    built = rb.build_all(rb.load_cases(), "sentence")
    written = 0
    for b in built:
        code = blind["cases"][b.cid]["subtype"]
        code = repl.get(code, code)
        h = int(rmap["codes"][code]["h"])
        for rep in (0, 1):
            key = ("rule-lookup", "subtype", b.cid, rep)
            if key in done:
                continue
            sev_floor = None if b.severity_floor is None else b.severity_floor.name.lower()
            io.append_jsonl(
                path,
                {
                    "judge": "rule-lookup",
                    "context": "subtype",
                    "id": b.cid,
                    "rep": rep,
                    "group": b.spec["group"],
                    "injection": b.spec.get("injection"),
                    "tier_floor": b.gate1.floor_tier.name.lower(),
                    "severity_floor": sev_floor,
                    "subtype": code,
                    "h": h,
                    "tier": TIERS[h],
                    "severity": SEVERITIES[h] if sev_floor is not None else None,
                    "p_tier": None,
                    "p_sev": None,
                    "error": None,
                    "seconds": None,
                    "meta": io.run_meta(),
                },
            )
            written += 1
    return written


def collect_labels() -> int:
    path = RAW / "labels_draft.jsonl"
    if io.done_keys(path, ("kind",)):
        return 0
    doc: dict[str, Any] = yaml.safe_load((BENCH_DIR / "labels.yaml").read_text(encoding="utf-8"))
    io.append_jsonl(
        path,
        {
            "kind": "labels",
            "status": doc.get("status"),
            "note": "agent-made draft from table rules, not reviewed by a human; secondary use only",
            "labels": {
                str(k): {"tier": v.get("tier"), "severity": v.get("severity")}
                for k, v in doc["labels"].items()
            },
            "meta": io.run_meta(),
        },
    )
    return 1


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--analyze", action="store_true", help="write summary.json and figures")
    args = parser.parse_args()
    if args.analyze:
        summary = analyze.summarize(RAW)
        print(f"summary: {io.write_summary(EXP_DIR, summary)}")
        from experiments.e5_models import figures

        for p in figures.make_all(summary, EXP_DIR):
            print(f"figure: {p}")
        return
    n_l = collect_local()
    n_b = collect_labels()
    n_o = collect_observations()
    n_r = collect_rule()
    print(f"wrote local_answers={n_l} labels={n_b} observations={n_o} rule={n_r} -> {RAW}")


if __name__ == "__main__":
    main()
