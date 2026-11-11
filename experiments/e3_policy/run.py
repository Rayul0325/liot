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

from . import auc as auc_mod
from . import generator, liot_map, params, sim

EXP_DIR = Path(__file__).resolve().parent
RAW_DIR = EXP_DIR / "raw"
LHS_SEED = 20261112
C21_BLOCKS = tuple(f"c21_p{round(p * 100):03d}" for p in sim.P_LOW_GRID)
CYCLE1 = ("main", "f0", "gated", "sens_half", "sens_dbl", "f0_g0")
C211A_BLOCKS = tuple(f"c211a_p{round(p * 100):03d}" for p in sim.P_AMB_GRID)
C211B_BLOCKS = tuple(f"c211b_x{round(x * 100):03d}" for x in sim.R_LIGHT_MULTS)
CYCLE2 = (*C21_BLOCKS, "c22", "c23", "c24")
CYCLE211 = (*C211A_BLOCKS, *C211B_BLOCKS)
STREAM_KEY0 = {"main": 1000, "f0": 1000, "f0_g0": 1000, "gated": 1000, "sens_half": 5000, "sens_dbl": 9000,
               "c22": 1000, "c23": 1000, "c24": 1000, **dict.fromkeys(C21_BLOCKS, 1000), **dict.fromkeys(CYCLE211, 1000)}

FULL = {"sets": 400, "seeds": 20, "days": 30, "sens_sets": 150, "sens_seeds": 8}
QUICK = {"sets": 20, "seeds": 2, "days": 7, "sens_sets": 8, "sens_seeds": 2}
CORE = ("flat_explicit", "liot_full", "liot_real", "binary_ext", "auto_all", "auto_undo")


def _ints(a: np.ndarray) -> str:
    return ",".join(str(int(x)) for x in a)


def make_sets(block: str, cfg: dict[str, int]) -> list[dict[str, float]]:
    if block in ("main", "f0", "f0_g0", "gated", "c22", "c23", "c24", *C21_BLOCKS, *CYCLE211):
        sets = params.sample(cfg["sets"], LHS_SEED)
        if block == "f0":
            sets = [{**s, "f": 0.0, "r": 0.0} for s in sets]
        if block == "f0_g0":
            sets = [{**s, "f": 0.0, "r": 0.0, "g": 0.0} for s in sets]
        if block == "c23":
            sets = [{**s, "g": 0.0} for s in sets]
        if block in C211B_BLOCKS:
            mult = sim.R_LIGHT_MULTS[C211B_BLOCKS.index(block)]
            sets = [{**s, "r_light_mult": mult} for s in sets]
        return sets
    factor = 0.5 if block == "sens_half" else 2.0
    return params.sample(cfg["sens_sets"], LHS_SEED + 1, params.scaled(params.PARAMS, factor))


def block_shape(block: str) -> tuple[tuple[float, ...], tuple[str, ...]]:
    if block in ("f0", "f0_g0"):
        return (0.0, params.M_DEFAULT), ("na",)
    if block == "gated":
        return (params.M_DEFAULT,), ("exp", "hyp")
    return params.M_GRID, ("exp", "hyp")


def run_set(task: tuple[str, int, dict[str, float], int, int]) -> dict[str, Any]:
    """One parameter set -> one raw record (runs in a worker process)."""
    os.nice(10)
    block, set_id, prm, n_seeds, days = task
    key = STREAM_KEY0[block] + set_id
    opts: dict[str, Any] | None = None
    if block in C21_BLOCKS:
        tier0 = liot_map.tables()["tier0"]
        low_ids = [i for i in range(generator.N_PROP) if int(tier0[i].max()) <= 1]  # LIOT tier <= BREATH
        opts = {"p_low": sim.P_LOW_GRID[C21_BLOCKS.index(block)], "low_ids": low_ids,
                "low_rules": True, "kc": 96}
    if block in C211A_BLOCKS:
        opts = {"p_low": sim.P_AMB_GRID[C211A_BLOCKS.index(block)], "low_ids": [1],
                "avoid_ids": [i for i in range(generator.N_PROP) if int(liot_map.tables()["tier0"][i].max()) <= 1],
                "amb_rules": True, "pool_hours": [17, 18, 19, 20, 21], "pool_hour_p": 0.9, "kc": 96}
    st = generator.make_streams(prm, key, n_seeds, days, opts)
    ms, shapes = block_shape(block)
    groups = sim.groups_for(block, ms, shapes)
    rank = sim.binary_rank(st)
    hk = sim.harmful_kinds(st)
    lv = np.empty((len(groups), *st.valid.shape), dtype=np.int8)
    bk = np.empty((len(groups), *st.valid.shape), dtype=bool)
    for i, g in enumerate(groups):
        lv[i], bk[i] = sim.build(st, g, rank, hk)
    c = sim.simulate(st, groups, lv, bk, prm)

    ok = st.valid & (st.kind == 0)
    n = {
        "n_help": _ints(((st.truth == 0) & ok).sum(1)),
        "n_unw": _ints(((st.truth == 1) & ok).sum(1)),
        "n_harm": _ints(((st.truth == 2) & ok).sum(1)),
        "n_haz": _ints((st.valid & (st.kind == 1)).sum(1)),
        "n_prop": _ints(ok.sum(1)),
    }
    # helpful proposals and the ones silenced by the proactive cap, per device class (liot_real, m=0.1)
    helpful = (st.truth == 0) & ok
    grp = generator.GROUP_OF[st.tmpl.astype(int)]
    by_group: dict[str, Any] = {"help_by_group": [int((helpful & (grp == k)).sum()) for k in range(5)]}
    irs = [i for i, g in enumerate(groups) if g.name == "liot_real" and g.shape != "hyp"
           and abs((g.m or 0.0) - params.M_DEFAULT) < 1e-9]
    if irs:
        by_group["silenced_help_by_group"] = [int((helpful & bk[irs[0]] & (grp == k)).sum()) for k in range(5)]
    # where the generator's harmful proposals land on LIOT's own tiers (no misclassification)
    tier0 = liot_map.tables()["tier0"][st.tmpl.astype(int), st.tb.astype(int), st.occ.astype(int)]
    harmful = (st.truth == 2) & ok
    by_group["harm_by_tier"] = [int((harmful & (tier0 == t)).sum()) for t in range(4)]
    by_group["help_by_tier"] = [int((helpful & (tier0 == t)).sum()) for t in range(4)]
    if block in ("main", "c24"):
        pos = harmful[ok]
        sd = sim.score_sd(st)
        aucs: dict[str, float] = {"score_sd": sd}
        for k in sim.KAPPAS:
            aucs[f"kappa{k:.2f}"] = auc_mod.auc((st.score0 + k * sd * st.z_score)[ok], pos)
        for sg in sim.THETA_SIGMAS:
            aucs[f"sigma{sg:.1f}"] = auc_mod.auc((st.score0 + sg * st.z_score)[ok], pos)
            aucs[f"sigma{sg:.1f}_over_sd"] = sg / sd
        aucs["tier0"] = auc_mod.auc(tier0[ok].astype(float), pos)
        for m in params.M_GRID:
            mis = (st.truth == 2) & (st.u_mis < m)
            target = np.where(st.u_k < 0.5, 0, 1)
            tm = np.where(mis, np.minimum(tier0, target), tier0)
            aucs[f"tier_m{m:.1f}"] = auc_mod.auc(tm[ok].astype(float), pos)
        aucs["base_sev"] = auc_mod.auc(generator.BASE_SEV[st.tmpl.astype(int)][ok], pos)
        by_group["auc"] = {k: (round(v, 4) if v == v else None) for k, v in aucs.items()}
    res: dict[str, Any] = {}
    curve: dict[str, dict[str, list[float]]] = {}
    best: dict[str, tuple[float, float, int]] = {}

    cycle2 = block.startswith("c2")

    def pack(i: int, detail: bool, theta: bool = False, mode: str = "") -> dict[str, Any]:
        """Per-seed arrays where a paired CI needs them, integer sums elsewhere (raw size)."""
        if mode == "sums3":  # theta lanes of C2.4: pooled harm, help, burden only
            return {f"s_{k}": round(float(c[k][i].sum())) for k in ("xh", "xp", "nc")}
        if mode == "sums":  # cycle-2 lanes that only need pooled totals
            return {f"s_{k}": round(float(c[k][i].sum())) for k in ("xh", "xp", "nc", "bh", "bp", "bu")} | {
                "units": round(float(c["units"][i].sum()))}
        rec: dict[str, Any] = {"xh": _ints(c["xh"][i])}
        if mode == "seedhh":
            rec["xp"] = _ints(c["xp"][i])
            for k in ("nc", "bh", "bp", "bu", "xu", "units", "hz_ok", "hz_lat"):
                rec[f"s_{k}" if k in ("nc", "hz_ok", "hz_lat") else k] = round(float(c[k][i].sum()))
            return rec
        if theta:  # per-seed harm for the U-shape test; burden/help/load as sums
            for k in ("xp", "nc", "units"):
                rec[f"s_{k}"] = round(float(c[k][i].sum()))
            return rec
        for k in ("xu", "nch", "units", "hm", "bh", "bp", "bu", "mu"):
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

    def c2_mode(g: sim.Group) -> str:
        if g.name == "theta":
            return "sums3"
        lean = g.name in ("flat_explicit", "liot_full", "liot_real", "binary_ext") and not g.ceil
        return "seedhh" if lean else "sums"

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
        res[g.key] = pack(i, detail, theta=g.name == "theta", mode=c2_mode(g) if cycle2 else "")
    for _, _, i in best.values():
        g = groups[i]
        rec = pack(i, g.m is None or abs(g.m - params.M_DEFAULT) < 1e-9)
        rec["k"] = g.k
        res[replace(g, name="binary_opt", k=None).key] = rec
    return {
        "block": block, "set_id": set_id, "params": prm, "n_seeds": n_seeds, "days": days,
        "stream_key": key, "n": n, **by_group, "realized": st.realized,
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
    ap.add_argument("--blocks", default="cycle1", help="cycle1 | cycle2 | comma list")
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
    named = {"cycle1": CYCLE1, "cycle2": CYCLE2, "c211": CYCLE211}
    blocks = named.get(args.blocks) or tuple(args.blocks.split(","))
    n = collect(raw_dir / "runs.jsonl", cfg, blocks, min(args.workers, 8))
    print(f"wrote {n} new records to {raw_dir / 'runs.jsonl'}")


if __name__ == "__main__":
    main()
