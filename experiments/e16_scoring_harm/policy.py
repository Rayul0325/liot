"""C4.13 collection — E3 policy simulator re-runs with per-template harm counts and burden switches.

Everything here wraps `experiments.e3_policy` (generator, sim, liot_map, params); no E3 file is changed.

Blocks (one record per (block, set_id); the same 400 Latin-hypercube sets and stream keys as E3):
  c413a            E3 C2.2 setting (stream key 1000 + set, no generator knob).  Lanes: liot_real, liot_full,
                   flat_explicit and flat_explicit + the same proactive ceiling, m in {0, 0.1}, both fatigue
                   shapes.  Records executed-harm per proposal template (per seed) so any severity weighting
                   can be applied in analysis.
  c413b_<var>      E3 C2.1 setting at 50% of harmful proposals on BREATH-or-lower templates (generator knob
                   p_low = 0.5, same opts as E3 `c21_p050`).  var in base | f0 | g0 | f0g0: the fatigue
                   coefficient f and/or the latency growth per load unit g set to 0 ("burden effects 0").
  c413c_<var>      E3 C2.2 streams with f = 0 and r = 0 (H2.2 sanity condition); var in f0r0 | f0r0g0.

Per-template harm trick (no copy of the simulator): the harmful->unwanted relabel of every harmful event
except one template leaves every resident decision and the load dynamics identical (both labels are
'not good' for the reader and for undo), so `simulate(...)["xh"]` of the relabelled streams is exactly the
executed harm of that template.  The sum over templates is asserted equal to the unrelabelled xh.
"""

from __future__ import annotations

import os
import time
from dataclasses import replace
from multiprocessing import Pool
from pathlib import Path
from typing import Any

import numpy as np

from experiments.common import io
from experiments.e3_policy import generator, liot_map, params, sim
from experiments.e3_policy.run import LHS_SEED

E3_RAW = Path(__file__).resolve().parents[1] / "e3_policy" / "raw" / "runs.jsonl"
VARS_B = {"base": {}, "f0": {"f": 0.0}, "g0": {"g": 0.0}, "f0g0": {"f": 0.0, "g": 0.0}}
VARS_C = {"f0r0": {"f": 0.0, "r": 0.0}, "f0r0g0": {"f": 0.0, "r": 0.0, "g": 0.0}}
BLOCKS = ("c413a", *(f"c413b_{v}" for v in VARS_B), *(f"c413c_{v}" for v in VARS_C))
FULL = {"sets": 400, "seeds": 20, "days": 30}
QUICK = {"sets": 6, "seeds": 2, "days": 7}
KEY0 = 1000  # E3 stream key base for main / c21 / c22 blocks
P_LOW_B = 0.5


def _ints(a: np.ndarray) -> str:
    return ",".join(str(int(x)) for x in a)


def groups_for(block: str) -> list[sim.Group]:
    if block == "c413a":
        gs: list[sim.Group] = []
        for m in (0.0, 0.1):
            for sh in ("exp", "hyp"):
                gs += [sim.Group("liot_real", sh, m=m), sim.Group("liot_full", sh, m=m),
                       sim.Group("flat_explicit", sh, m=m), sim.Group("flat_explicit", sh, m=m, ceil="liot")]
        return gs
    gs = []
    for m in (0.0, 0.1):
        gs += [sim.Group("liot_real", "exp", m=m), sim.Group("liot_full", "exp", m=m),
               sim.Group("flat_explicit", "exp", m=m), sim.Group("flat_explicit", "exp", m=m, ceil="liot")]
    return gs


def make_sets(block: str, cfg: dict[str, int]) -> list[dict[str, float]]:
    sets = params.sample(cfg["sets"], LHS_SEED)
    out = []
    for s in sets:
        s = dict(s)
        s["f_orig"] = s["f"]
        s["g_orig"] = s["g"]
        s["r_orig"] = s["r"]
        if block.startswith("c413b_"):
            s.update(VARS_B[block.split("_", 1)[1]])
        if block.startswith("c413c_"):
            s.update(VARS_C[block.split("_", 1)[1]])
        out.append(s)
    return out


def stream_opts(block: str) -> dict[str, Any] | None:
    if not block.startswith("c413b_"):
        return None
    tier0 = liot_map.tables()["tier0"]
    low_ids = [i for i in range(generator.N_PROP) if int(tier0[i].max()) <= 1]  # LIOT tier <= BREATH
    return {"p_low": P_LOW_B, "low_ids": low_ids, "low_rules": True, "kc": 96}


def _prm(p: dict[str, float]) -> dict[str, float]:
    return {k: v for k, v in p.items() if not k.endswith("_orig")}


def run_set(task: tuple[str, int, dict[str, float], int, int]) -> dict[str, Any]:
    os.nice(10)
    block, set_id, prm_full, n_seeds, days = task
    prm = _prm(prm_full)
    st = generator.make_streams(prm, KEY0 + set_id, n_seeds, days, stream_opts(block))
    groups = groups_for(block)
    rank = sim.binary_rank(st)
    hk = sim.harmful_kinds(st)
    lv = np.empty((len(groups), *st.valid.shape), dtype=np.int8)
    bk = np.empty((len(groups), *st.valid.shape), dtype=bool)
    for i, g in enumerate(groups):
        lv[i], bk[i] = sim.build(st, g, rank, hk)
    c = sim.simulate(st, groups, lv, bk, prm)

    ok = st.valid & (st.kind == 0)
    harm = ok & (st.truth == 2)
    tm = st.tmpl.astype(int)
    h_t = np.stack([((harm & (tm == t)).sum(1)) for t in range(generator.N_PROP)])  # (15, seeds)
    tier0 = liot_map.tables()["tier0"][tm, st.tb.astype(int), st.occ.astype(int)]
    tier_hist = [[int((harm & (tm == t) & (tier0 == k)).sum()) for k in range(4)] for t in range(generator.N_PROP)]
    res: dict[str, Any] = {}
    for i, g in enumerate(groups):
        res[g.key] = {"xh": _ints(c["xh"][i]), "xp": _ints(c["xp"][i]), "nc": int(c["nc"][i].sum()),
                      "units": round(float(c["units"][i].sum()), 3), "bh": int(c["bh"][i].sum()),
                      "bp": int(c["bp"][i].sum()), "bu": int(c["bu"][i].sum())}
    if block == "c413a":
        harm_t = [t for t in range(generator.N_PROP) if int(h_t[t].sum()) > 0]
        per_t: dict[int, np.ndarray] = {}
        for t in harm_t:
            truth_t = np.where(harm & (tm != t), 1, st.truth).astype(st.truth.dtype)
            per_t[t] = sim.simulate(replace(st, truth=truth_t), groups, lv, bk, prm)["xh"]
        tot = np.sum([per_t[t] for t in harm_t], axis=0) if harm_t else np.zeros_like(c["xh"])
        if not np.array_equal(tot, c["xh"]):
            raise RuntimeError(f"set {set_id}: per-template harm does not add up to xh")
        for i, g in enumerate(groups):
            res[g.key]["xh_t"] = {str(t): _ints(per_t[t][i]) for t in harm_t}
    return {
        "block": block, "set_id": set_id, "params": prm_full, "n_seeds": n_seeds, "days": days,
        "stream_key": KEY0 + set_id,
        "n": {"n_harm": _ints(harm.sum(1)), "n_help": _ints(((st.truth == 0) & ok).sum(1)),
              "n_prop": _ints(ok.sum(1))},
        "harm_by_template": {str(t): _ints(h_t[t]) for t in range(generator.N_PROP) if int(h_t[t].sum()) > 0},
        "harm_tier_hist_by_template": tier_hist,
        "res": res, "meta": io.run_meta(),
    }


def collect(raw: Path, cfg: dict[str, int], blocks: tuple[str, ...], workers: int) -> int:
    path = raw / "c413_runs.jsonl"
    done = io.done_keys(path, ("block", "set_id"))
    written = 0
    for block in blocks:
        sets = make_sets(block, cfg)
        tasks = [(block, i, s, cfg["seeds"], cfg["days"]) for i, s in enumerate(sets) if (block, i) not in done]
        if not tasks:
            continue
        t0 = time.time()
        with Pool(workers) as pool:
            for k, rec in enumerate(pool.imap_unordered(run_set, tasks, chunksize=1), 1):
                rec["load1"] = round(os.getloadavg()[0], 2)
                io.append_jsonl(path, rec)
                written += 1
                if k % 25 == 0 or k == len(tasks):
                    print(f"  {block}: {k}/{len(tasks)} sets  {time.time() - t0:.0f}s  load1={os.getloadavg()[0]:.1f}",
                          flush=True)
    return written


def collect_severity(raw: Path) -> int:
    """Copy the two label-blind coders' ratings (+ id maps) into raw/ so analysis reads raw/ only."""
    import json

    path = raw / "c413_severity_coders.jsonl"
    sev = Path(__file__).resolve().parent / "severity"
    done = io.done_keys(path, ("coder", "template"))
    written = 0
    for coder in ("coderA", "coderB"):
        ratings = json.loads((sev / f"output_{coder}.json").read_text(encoding="utf-8"))
        idmap = json.loads((sev / f"idmap_{coder}.json").read_text(encoding="utf-8"))
        for aid, tmpl in idmap.items():
            if (coder, int(tmpl)) in done:
                continue
            r = ratings[aid]
            io.append_jsonl(path, {"coder": coder, "id": aid, "template": int(tmpl),
                                   "template_name": generator.NAME[int(tmpl)], "injury": int(r["injury"]),
                                   "reversibility": int(r["reversibility"]), "duration": int(r["duration"]),
                                   "note": r["note"]})
            written += 1
    return written


def collect_repro(raw: Path, n_sets: int) -> int:
    """Re-run the first n_sets of E3's c22 / c21_p050 blocks through this module and compare with E3's raw."""
    path = raw / "c413_repro.jsonl"
    done = io.done_keys(path, ("block", "set_id"))
    want: dict[tuple[str, int], dict[str, Any]] = {}
    for r in io.read_jsonl(E3_RAW):
        if r["block"] in ("c22", "c21_p050") and r["set_id"] < n_sets:
            want[(r["block"], r["set_id"])] = r
    written = 0
    mine = {"c22": "c413a", "c21_p050": "c413b_base"}
    for (eb, sid), e3 in sorted(want.items()):
        if (eb, sid) in done:
            continue
        b = mine[eb]
        sets = make_sets(b, {"sets": 400})
        rec = run_set((b, sid, sets[sid], e3["n_seeds"], e3["days"]))
        cmp: dict[str, Any] = {}
        for key, v in rec["res"].items():
            if key not in e3["res"]:
                continue
            e = e3["res"][key]
            if "xh" in e:
                cmp[key] = {"xh_equal": e["xh"] == v["xh"]}
            elif "s_xh" in e:
                cmp[key] = {"xh_equal": int(e["s_xh"]) == sum(int(x) for x in v["xh"].split(","))}
        io.append_jsonl(path, {"block": eb, "set_id": sid, "compared": cmp,
                               "all_equal": bool(cmp) and all(x["xh_equal"] for x in cmp.values()),
                               "n_lanes": len(cmp), "meta": io.run_meta()})
        written += 1
    return written
