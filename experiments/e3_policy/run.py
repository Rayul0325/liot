"""E3 policy-level Monte Carlo — collection and analysis entry point.

    PYTHONPATH=$PWD .venv/bin/python -m experiments.e3_policy.run            # full collection
    PYTHONPATH=$PWD .venv/bin/python -m experiments.e3_policy.run --quick    # smoke (raw_quick/)
    PYTHONPATH=$PWD .venv/bin/python -m experiments.e3_policy.run --analyze  # summary + figures

Blocks (raw/runs.jsonl, one record per (block, parameter set), resumable):
  main        400 Latin-hypercube sets x 20 seeds x 30 days, both fatigue shapes, m grid
  f0          same sets and streams with f=0, r=0 (simulator sanity, H2.2)
  gated       main sets, "never open" gate applied to every policy (isolates the structural rule)
  sens_half   150 sets with every range halved around its centre, 8 seeds
  sens_dbl    150 sets with every range doubled, 8 seeds
One record = one parameter set with all policy groups; streams are identical for all groups
(common random numbers), and the f0/gated blocks reuse the main block's stream key.
"""

from __future__ import annotations

import argparse
import os
import time
from dataclasses import replace
from multiprocessing import Pool
from pathlib import Path
from typing import Any

import numpy as np

from experiments.common import io

from . import generator, params, sim

EXP_DIR = Path(__file__).resolve().parent
RAW_DIR = EXP_DIR / "raw"
LHS_SEED = 20261112
STREAM_KEY0 = {"main": 1000, "f0": 1000, "gated": 1000, "sens_half": 5000, "sens_dbl": 9000}

FULL = {"sets": 400, "seeds": 20, "days": 30, "sens_sets": 150, "sens_seeds": 8}
QUICK = {"sets": 20, "seeds": 2, "days": 7, "sens_sets": 8, "sens_seeds": 2}
CORE = ("flat_explicit", "liot_full", "liot_real", "binary_ext", "auto_all", "auto_undo")


def _ints(a: np.ndarray) -> str:
    return ",".join(str(int(x)) for x in a)


def make_sets(block: str, cfg: dict[str, int]) -> list[dict[str, float]]:
    if block in ("main", "f0", "gated"):
        sets = params.sample(cfg["sets"], LHS_SEED)
        if block == "f0":
            sets = [{**s, "f": 0.0, "r": 0.0} for s in sets]
        return sets
    factor = 0.5 if block == "sens_half" else 2.0
    return params.sample(cfg["sens_sets"], LHS_SEED + 1, params.scaled(params.PARAMS, factor))


def block_shape(block: str) -> tuple[tuple[float, ...], tuple[str, ...]]:
    if block == "f0":
        return (0.0, params.M_DEFAULT), ("na",)
    if block == "gated":
        return (params.M_DEFAULT,), ("exp", "hyp")
    return params.M_GRID, ("exp", "hyp")


def run_set(task: tuple[str, int, dict[str, float], int, int]) -> dict[str, Any]:
    """One parameter set -> one raw record (runs in a worker process)."""
    os.nice(10)
    block, set_id, prm, n_seeds, days = task
    key = STREAM_KEY0[block] + set_id
    st = generator.make_streams(prm, key, n_seeds, days)
    ms, shapes = block_shape(block)
    groups = sim.groups_for(block, ms, shapes)
    rank = sim.binary_rank(st)
    lv = np.empty((len(groups), *st.valid.shape), dtype=np.int8)
    bk = np.empty((len(groups), *st.valid.shape), dtype=bool)
    for i, g in enumerate(groups):
        lv[i], bk[i] = sim.build(st, g, rank)
    c = sim.simulate(st, groups, lv, bk, prm)

    ok = st.valid & (st.kind == 0)
    n = {
        "n_help": _ints(((st.truth == 0) & ok).sum(1)),
        "n_unw": _ints(((st.truth == 1) & ok).sum(1)),
        "n_harm": _ints(((st.truth == 2) & ok).sum(1)),
        "n_haz": _ints((st.valid & (st.kind == 1)).sum(1)),
        "n_prop": _ints(ok.sum(1)),
    }
    res: dict[str, Any] = {}
    curve: dict[str, dict[str, list[float]]] = {}
    best: dict[str, tuple[float, float, int]] = {}

    def pack(i: int, detail: bool, theta: bool = False) -> dict[str, Any]:
        """Per-seed arrays where a paired CI needs them, integer sums elsewhere (raw size)."""
        rec: dict[str, Any] = {"xh": _ints(c["xh"][i])}
        if theta:  # per-seed harm for the U-shape test; burden/help/load as sums
            for k in ("xp", "nc", "units"):
                rec[f"s_{k}"] = round(float(c[k][i].sum()))
            return rec
        for k in ("xu", "nch", "units", "hm", "bh", "bp", "mu"):
            rec[k] = round(float(c[k][i].sum()))
        if detail:
            for k in ("xp", "nc", "hz_ok"):
                rec[k] = _ints(c[k][i])
            rec["hz_lat"] = ",".join(f"{x:.0f}" for x in c["hz_lat"][i])
            rec["bps"] = _ints(c["bp"][i])
            rec["bhs"] = _ints(c["bh"][i])
        else:
            for k in ("xp", "nc", "hz_ok", "hz_lat"):
                rec[f"s_{k}"] = round(float(c[k][i].sum()))
        return rec

    for i, g in enumerate(groups):
        detail = g.m is None or abs(g.m - params.M_DEFAULT) < 1e-9
        if g.name == "binary_k":
            ck = f"{g.shape}|m{g.m:.1f}"
            curve.setdefault(ck, {})[str(g.k)] = [float(c["xh"][i].sum()), float(c["nc"][i].sum()),
                                                  float(c["xp"][i].sum())]
            cand = (float(c["xh"][i].sum()), float(c["nc"][i].sum()), i)
            if ck not in best or cand[:2] < best[ck][:2]:
                best[ck] = cand
            continue
        res[g.key] = pack(i, detail, theta=g.name == "theta")
    for _, _, i in best.values():
        g = groups[i]
        rec = pack(i, g.m is None or abs(g.m - params.M_DEFAULT) < 1e-9)
        rec["k"] = g.k
        res[replace(g, name="binary_opt", k=None).key] = rec
    return {
        "block": block, "set_id": set_id, "params": prm, "n_seeds": n_seeds, "days": days,
        "stream_key": key, "n": n, "realized": st.realized,
        "binary_rank": [int(x) for x in rank], "binary_curve": curve, "res": res,
        "meta": io.run_meta(),
    }


def collect(raw_path: Path, cfg: dict[str, int], blocks: tuple[str, ...], workers: int) -> int:
    done = io.done_keys(raw_path, ("block", "set_id"))
    written = 0
    for block in blocks:
        sets = make_sets(block, cfg)
        n_seeds = cfg["sens_seeds"] if block.startswith("sens") else cfg["seeds"]
        tasks = [(block, i, s, n_seeds, cfg["days"]) for i, s in enumerate(sets) if (block, i) not in done]
        if not tasks:
            continue
        t0 = time.time()
        with Pool(workers) as pool:
            for k, rec in enumerate(pool.imap_unordered(run_set, tasks, chunksize=1), 1):
                io.append_jsonl(raw_path, rec)
                written += 1
                if k % 20 == 0 or k == len(tasks):
                    print(f"  {block}: {k}/{len(tasks)} sets  {time.time() - t0:.0f}s", flush=True)
    return written


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--analyze", action="store_true", help="write summary.json and figures")
    ap.add_argument("--quick", action="store_true", help="20 sets x 2 seeds x 7 days into raw_quick/")
    ap.add_argument("--workers", type=int, default=8)
    ap.add_argument("--blocks", default="main,f0,gated,sens_half,sens_dbl")
    args = ap.parse_args()
    raw_dir = EXP_DIR / ("raw_quick" if args.quick else "raw")
    cfg = QUICK if args.quick else FULL
    if args.analyze:
        from . import analyze, figures

        summary = analyze.summarize(raw_dir)
        out_dir = EXP_DIR / "quick_out" if args.quick else EXP_DIR  # quick_out/ is never committed
        print(f"summary: {io.write_summary(out_dir, summary)}")
        for p in figures.make_all(raw_dir, summary, out_dir):
            print(f"figure: {p}")
        return
    n = collect(raw_dir / "runs.jsonl", cfg, tuple(args.blocks.split(",")), min(args.workers, 8))
    print(f"wrote {n} new records to {raw_dir / 'runs.jsonl'}")


if __name__ == "__main__":
    main()
