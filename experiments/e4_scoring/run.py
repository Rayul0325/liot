"""E4 runner: collect the enumeration inputs as raw records, then (--analyze) summarise.

    PYTHONPATH=$PWD .venv/bin/python -m experiments.e4_scoring.run            # collect raw
    PYTHONPATH=$PWD .venv/bin/python -m experiments.e4_scoring.run --analyze  # summary + figures
    PYTHONPATH=$PWD .venv/bin/python -m experiments.e4_scoring.run --quick    # tiny smoke run

Nothing here calls a model or a network.  The raw files are:
  universe.jsonl       production table cells, the 8 safety categories (floors, push priority,
                       Gate1 tier floor), run_meta
  codings_grid.jsonl   named ordinal codings x weights (scores are recomputed in analysis)
  codings_random.jsonl log-uniform random codings with a seeded cut-point draw
  model_only.jsonl     the bench raw answers (bench/risk/raw/*.jsonl) reduced to what E4 needs
All raw files are resumable (done_keys) and every record carries run_meta().
"""

from __future__ import annotations

import argparse
import itertools
import math
from pathlib import Path
from typing import Any

import numpy as np

from experiments.common import io
from experiments.e4_scoring import analyze

EXP_DIR = Path(__file__).resolve().parent
RAW = EXP_DIR / "raw"
BENCH_RAW = Path(__file__).resolve().parents[2] / "bench" / "risk" / "raw"

SEED = 20261112
N_RANDOM = 5000
N_RANDOM_QUICK = 200

# ordinal codings, low -> high (inconvenience..life ; later..minutes)
HARM_FAMILIES: dict[str, list[float]] = {
    "lin": [1, 2, 3, 4],
    "geo2": [1, 2, 4, 8],
    "geo3": [1, 3, 9, 27],
    "geo10": [1, 10, 100, 1000],
    "zero_based": [0, 1, 2, 3],
}
TIME_FAMILIES: dict[str, list[float]] = {
    "lin": [1, 2, 3],
    "geo2": [1, 2, 4],
    "geo3": [1, 3, 9],
    "geo10": [1, 10, 100],
    "zero_based": [0, 1, 2],
}
VULN_K = (1.5, 2.0, 3.0, 5.0)
WEIGHT_STEPS = (0.5, 1.0, 2.0)


def collect_universe() -> int:
    """Write universe.jsonl (one record). Uses the real severity table and Guardrail."""
    path = RAW / "universe.jsonl"
    if io.done_keys(path, ("kind",)):
        return 0
    from liot.core.severity import load_severity_table
    from liot.layers.l3_judgment.guardrail import Guardrail
    from liot.live_graph import build_one, load_bench_specs

    table = load_severity_table()
    if table.load_failed:
        raise SystemExit(f"severity table failed to load: {table.load_error}")
    guard = Guardrail()
    specs = {s["id"]: s for s in load_bench_specs()}
    cats: list[dict[str, Any]] = []
    for cat in ("gas", "smoke", "oven", "high_temp", "leak", "water_main", "door", "lock"):
        plain = table.compute(cat, vulnerable_resident=False)
        vuln = table.compute(cat, vulnerable_resident=True)
        assert plain is not None and vuln is not None
        built = build_one(specs[f"sit-{cat}/danger"], guard, table)
        cats.append(
            {
                "category": cat,
                "damage": plain.damage,
                "time": plain.time_to_harm,
                "floor_plain": int(plain.floor),
                "floor_vuln": int(vuln.floor),
                "push_priority_plain": plain.floor.ntfy_priority,
                "push_priority_vuln": vuln.floor.ntfy_priority,
                "gate1_tier_floor": built.gate1.floor_tier.name.lower(),
                "gate1_tier_floor_rank": int(built.gate1.floor_tier),
            }
        )
    rec = {
        "kind": "universe",
        "table_cells": [
            {"damage": d, "time": t, "level": int(v)} for (d, t), v in sorted(table.cells().items())
        ],
        "categories": cats,
        "meta": io.run_meta(),
    }
    io.append_jsonl(path, rec)
    return 1


def collect_grid() -> int:
    path = RAW / "codings_grid.jsonl"
    done = io.done_keys(path, ("key",))
    written = 0
    for (hn, hc), (tn, tc), k, a, b in itertools.product(
        HARM_FAMILIES.items(),
        TIME_FAMILIES.items(),
        VULN_K,
        WEIGHT_STEPS,
        WEIGHT_STEPS,
    ):
        key = f"{hn}|{tn}|k{k}|a{a}|b{b}"
        if (key,) in done:
            continue
        io.append_jsonl(
            path,
            {
                "key": key,
                "harm_family": hn,
                "time_family": tn,
                "harm_code": hc,
                "time_code": tc,
                "vuln_k": k,
                "weights": [a, b, 1.0],
                "meta": io.run_meta(),
            },
        )
        written += 1
    return written


def _logu(rng: np.random.Generator, lo: float, hi: float, n: int) -> np.ndarray:
    return np.exp(rng.uniform(math.log(lo), math.log(hi), size=n))


def collect_random(n: int) -> int:
    path = RAW / "codings_random.jsonl"
    done = io.done_keys(path, ("idx",))
    rng = np.random.default_rng(SEED)
    written = 0
    for idx in range(n):
        # draw for every idx (even if done) so resuming keeps the same stream
        h = np.sort(_logu(rng, 1.0, 1000.0, 4))
        t = np.sort(_logu(rng, 1.0, 100.0, 3))
        k = float(_logu(rng, 1.2, 10.0, 1)[0])
        u3 = float(rng.uniform())
        u2 = float(rng.uniform())
        if (idx,) in done:
            continue
        h = h / h[0]
        t = t / t[0]
        # strictly increasing is required for an ordinal coding; a tie after rounding is skipped
        if not (np.all(np.diff(h) > 1e-9) and np.all(np.diff(t) > 1e-9)):
            continue
        io.append_jsonl(
            path,
            {
                "idx": idx,
                "harm_code": [round(float(x), 6) for x in h],
                "time_code": [round(float(x), 6) for x in t],
                "vuln_k": round(k, 6),
                "weights": [1.0, 1.0, 1.0],
                "u_m3": round(u3, 9),
                "u_m2": round(u2, 9),
                "meta": io.run_meta(),
            },
        )
        written += 1
    return written


def collect_model_only() -> int:
    """Reduce bench/risk/raw/*.jsonl to the fields E4 needs (answers, floors, group)."""
    path = RAW / "model_only.jsonl"
    done = io.done_keys(path, ("model", "id", "rep"))
    written = 0
    for f in sorted(BENCH_RAW.glob("*.jsonl")):
        if f.name.endswith(".sessions.jsonl"):
            continue
        for r in io.read_jsonl(f):
            key = (r["model"], r["id"], r["rep"])
            if key in done:
                continue
            io.append_jsonl(
                path,
                {
                    "model": r["model"],
                    "id": r["id"],
                    "rep": r["rep"],
                    "group": r["group"],
                    "injection": r.get("injection"),
                    "tier_floor": r["tier_floor"],
                    "severity_floor": r["severity_floor"],
                    "model_tier": r.get("model_tier"),
                    "model_severity": r.get("model_severity"),
                    "error": r.get("error"),
                    "source": f"bench/risk/raw/{f.name}",
                    "meta": io.run_meta(),
                },
            )
            written += 1
    return written


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--analyze", action="store_true", help="write summary.json and figures")
    parser.add_argument(
        "--quick", action="store_true", help="smoke run into a scratch dir; does not touch raw/"
    )
    args = parser.parse_args()
    global RAW
    if args.quick:
        import tempfile

        RAW = Path(tempfile.mkdtemp(prefix="e4_quick_")) / "raw"
        RAW.mkdir(parents=True)
        print(f"[quick] scratch raw dir: {RAW}")
    if args.analyze and not args.quick:
        summary = analyze.summarize(RAW)
        print(f"summary: {io.write_summary(EXP_DIR, summary)}")
        from experiments.e4_scoring import figures

        for p in figures.make_all(summary, EXP_DIR):
            print(f"figure: {p}")
        return
    n_u = collect_universe()
    n_g = collect_grid()
    n_r = collect_random(N_RANDOM_QUICK if args.quick else N_RANDOM)
    n_m = collect_model_only()
    print(f"wrote universe={n_u} grid={n_g} random={n_r} model_only={n_m} -> {RAW}")
    if args.quick:
        summary = analyze.summarize(RAW)
        print("[quick] summary keys:", sorted(summary))
        print("[quick] table axioms:", summary["table"])


if __name__ == "__main__":
    main()
