"""Simulator self-checks (run: python -m experiments.e3_policy.selfcheck).

Each check is an identity that must hold by construction; a failure is a simulator defect.
"""

from __future__ import annotations

from pathlib import Path

import numpy as np

from . import generator, params, sim


def _run(prm: dict[str, float], block: str = "main", seeds: int = 4, days: int = 10):
    st = generator.make_streams(prm, 4242, seeds, days)
    ms, shapes = (0.0, 0.1), ("exp", "hyp")
    groups = sim.groups_for(block, ms, shapes)
    rank = sim.binary_rank(st)
    lv = np.empty((len(groups), *st.valid.shape), dtype=np.int8)
    bk = np.empty((len(groups), *st.valid.shape), dtype=bool)
    for i, g in enumerate(groups):
        lv[i], bk[i] = sim.build(st, g, rank)
    c = sim.simulate(st, groups, lv, bk, prm)
    return st, groups, lv, bk, c, {g.key: i for i, g in enumerate(groups)}


def main() -> None:
    prm = params.sample(1, 7)[0]
    prm.update(f=0.15, r=0.3, haz_rate=0.8, harm_frac=0.12)
    st, groups, lv, bk, c, ix = _run(prm)
    ok = []

    def check(name: str, cond: bool) -> None:
        ok.append(cond)
        print(("PASS " if cond else "FAIL ") + name)

    # 1. auto_all executes every proposal: harm executed == harmful proposals
    i = ix["auto_all|na"]
    n_harm = ((st.truth == 2) & st.valid & (st.kind == 0)).sum(1)
    check("auto_all executes all harmful proposals", np.array_equal(c["xh"][i], n_harm))
    # 2. theta=0 == auto_all and theta=1 == explicit for everything (levels), any sigma
    for s in sim.THETA_SIGMAS:
        z = sim.Group("theta", "exp", theta=0.0, sigma=s)
        o = sim.Group("theta", "exp", theta=1.0, sigma=s)
        lz, _ = sim.build(st, z, sim.binary_rank(st))
        lo, _ = sim.build(st, o, sim.binary_rank(st))
        check(f"theta=0 -> no confirmation (sigma={s})", int(lz.sum()) == 0)
        check(f"theta=1 -> explicit for every valid event (sigma={s})", bool(np.all(lo[st.valid] == 2)))
    # 3. binary list of all 15 actions confirms every proposal at m=0
    b15 = sim.Group("binary_k", "exp", m=0.0, k=15)
    lb, _ = sim.build(st, b15, sim.binary_rank(st))
    prop = st.valid & (st.kind == 0)
    check("binary_k15 confirms every proposal at m=0", bool(np.all(lb[prop] == 2)))
    # 4. common random numbers: same streams -> flat lanes of two m values differ only through tiers
    i0, i1 = ix["liot_full|exp|m0.0"], ix["liot_full|exp|m0.1"]
    check("liot_full: misclassification only lowers confirmations (nc m=0.1 <= m=0)",
          bool(np.all(c["nc"][i1] <= c["nc"][i0])))
    # 5. liot_real blocked proposals never executed, never confirmed
    ir = ix["liot_real|exp|m0.0"]
    check("liot_real: blocked events have level 0", bool(np.all(lv[ir][bk[ir]] == 0)))
    n_prop = (st.valid & (st.kind == 0)).sum(1)
    n_blk = (bk[ir] & (st.kind == 0)).sum(1)
    check("liot_real: executed proposals <= unblocked proposals",
          bool(np.all(c["xh"][ir] + c["xp"][ir] + c["xu"][ir] <= n_prop - n_blk)))
    # 6. f=0 and r irrelevant: with f=0 the two shapes coincide exactly
    p0 = {**prm, "f": 0.0}
    st0, g0, lv0, bk0, c0, ix0 = _run(p0)
    a, b = ix0["flat_explicit|exp|m0.1"], ix0["flat_explicit|hyp|m0.1"]
    check("f=0: exp and hyp shapes identical", np.array_equal(c0["xh"][a], c0["xh"][b]))
    # 7. nc (requests) independent of fatigue/resident parameters
    st2, g2, lv2, bk2, c2, ix2 = _run({**prm, "f": 0.3, "a": 0.7, "r": 0.9})
    a, b = ix["flat_explicit|exp|m0.1"], ix2["flat_explicit|exp|m0.1"]
    check("requests per day independent of resident parameters", np.array_equal(c["nc"][a], c2["nc"][b]))
    # 8. generator independence: truth decided before any LIOT table is consulted
    src = Path(generator.__file__).read_text(encoding="utf-8")
    check("generator.py imports nothing from liot", "import liot" not in src and "from liot" not in src)
    # 9. analytic check at f=0, g=0: harm_flat = (1-a) * P(latency <= TTL); every proposal is
    #    confirmed and read, so only the decision error and expiry remain.
    from math import erf, log, sqrt

    pa = {**prm, "f": 0.0, "g": 0.0, "a": 0.8, "m0_s": 900.0, "harm_frac": 0.15, "haz_rate": 0.0}
    st3, g3, lv3, bk3, c3, ix3 = _run(pa, seeds=40, days=30)
    nh = ((st3.truth == 2) & st3.valid & (st3.kind == 0)).sum()
    meas = c3["xh"][ix3["flat_explicit|exp|m0.0"]].sum() / nh
    z = log(3600.0 / pa["m0_s"]) / 0.8
    pred = (1 - pa["a"]) * 0.5 * (1 + erf(z / sqrt(2)))
    se = sqrt(pred * (1 - pred) / nh)
    print(f"  analytic harm_flat(f=0,g=0) predicted {pred:.4f} measured {meas:.4f} (se {se:.4f}, n={nh})")
    check("f=0,g=0: flat harm matches (1-a)*P(latency<=TTL) within 3 se", abs(meas - pred) < 3 * se)
    # 10. auto_all hazard response is instantaneous; flat's is slower (latency and errors)
    print("ALL PASS" if all(ok) else "SOME FAILED")
    raise SystemExit(0 if all(ok) else 1)


if __name__ == "__main__":
    main()
