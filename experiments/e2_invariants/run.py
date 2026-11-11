"""E2 runner — H1.4 invariant fuzz and H1.5 (P1) guardian-paging trade-off.

    PYTHONPATH=$PWD .venv/bin/python -m experiments.e2_invariants.run --part fuzz
    PYTHONPATH=$PWD .venv/bin/python -m experiments.e2_invariants.run --part h15
    PYTHONPATH=$PWD .venv/bin/python -m experiments.e2_invariants.run --part validate
    PYTHONPATH=$PWD .venv/bin/python -m experiments.e2_invariants.run --quick
    PYTHONPATH=$PWD .venv/bin/python -m experiments.e2_invariants.run --analyze

Parts: fuzz (>=3,000 episodes over 6 model behaviours), h15 (decision-level sweep with common
random numbers), validate (real service: forwards happen iff the production predicate on the
table floor says so; also checks the detectors fire on hand-made violations).
"""

from __future__ import annotations

import argparse
import asyncio
import itertools
import random
from datetime import timedelta
from pathlib import Path
from typing import Any

from experiments.common import invariants, io, plotstyle
from experiments.common.service_kit import (
    HOME,
    HighestBackend,
    LowestBackend,
    TimedTransport,
    build_service,
    guardian_forwards,
    reading,
    situations,
)
from experiments.e2_invariants import analyze, fuzz, h15
from liot.layers.l4_execution.escalation import guardian_path

EXP_DIR = Path(__file__).resolve().parent
RAW_DIR = EXP_DIR / "raw"
EPISODES_PER_BEHAVIOR = 600


# ── fuzz ─────────────────────────────────────────────────────────────────────
async def collect_fuzz(n_per: int, raw: Path) -> int:
    path = raw / "fuzz.jsonl"
    done = io.done_keys(path, ("behavior", "episode"))
    written = 0
    for ep in range(n_per):
        for b in fuzz.BEHAVIORS:  # interleave behaviours
            if (b, ep) in done:
                continue
            rec = await fuzz.run_episode(b, ep)
            rec["meta"] = io.run_meta()
            io.append_jsonl(path, rec)
            written += 1
    return written


# ── h15 ──────────────────────────────────────────────────────────────────────
def h15_cells(quick: bool) -> list[dict[str, Any]]:
    cells: list[dict[str, Any]] = []
    fas = (0.0, 0.02) if quick else h15.FA_LEVELS
    unregs = (0.0, 0.1) if quick else h15.UNREG_LEVELS
    hs = (0.6, 1.0) if quick else h15.HONESTY
    ats = (0.0, 0.2) if quick else h15.ATTACK
    for fa, u, h, a, b in itertools.product(fas, unregs, hs, ats, (0, 1)):
        cells.append({"fa": fa, "unreg": u, "honesty": h, "attack": a, "blind": b, "mix": "base"})
    if not quick:  # sensitivity to the alarm mix (not pre-registered; marked as such)
        for mix, h, a in itertools.product(("alarm_low", "alarm_high"), (0.7, 0.9), (0.0, 0.2)):
            cells.append({"fa": 0.02, "unreg": 0.1, "honesty": h, "attack": a, "blind": 0,
                          "mix": mix})
    return cells


def cell_key(c: dict[str, Any]) -> str:
    return (f"fa{c['fa']}|u{c['unreg']}|h{c['honesty']}|a{c['attack']}|b{c['blind']}|{c['mix']}")


def collect_h15(raw: Path, quick: bool) -> int:
    path = raw / "h15.jsonl"
    done = io.done_keys(path, ("key",))
    n_seeds = 3 if quick else h15.N_SEEDS
    n_events = 2000 if quick else h15.N_EVENTS
    written = 0
    for c in h15_cells(quick):
        key = cell_key(c)
        if (key,) in done:
            continue
        seeds = []
        for s in range(n_seeds):
            seeds.append(h15.run_cell(s, n=n_events, **c))
        io.append_jsonl(path, {"key": key, **c, "n_seeds": n_seeds, "seeds": seeds,
                               "meta": io.run_meta()})
        written += 1
    return written


# ── validate (real service) + detector self-check ───────────────────────────
async def validate_one(i: int) -> dict[str, Any]:
    rng = random.Random(f"e2v/{i}")
    cat = rng.choice(fuzz.SENSORS_LIST)
    eid, abn = fuzz.SENSORS[cat]
    backend = rng.choice([HighestBackend(), LowestBackend()])
    registered = rng.random() < 0.3
    kit = await build_service(
        backend=backend, categories=fuzz.CATEGORIES, transport=TimedTransport(),
    )
    svc = kit.svc
    try:
        rid = await svc.safety.add_resident(HOME, "R0")
        if registered:
            await svc.safety.set_trait(rid, "age_75_plus", True, "resident")
        gid, _topic = await svc.safety.add_guardian(HOME, "G0")
        await svc.safety.grant_consent(HOME, gid, rid)
        await svc.handle_reading(reading(eid, abn))
        await svc.drain_phase2()
        kit.clock.set_fixed(kit.t0 + timedelta(seconds=95))
        await svc.ladder_tick()
        sits = await situations(kit.db)
        floor_int = sits[0]["severity_floor"]
        from liot.core.severity import SeverityLevel

        predicted = guardian_path(SeverityLevel(floor_int))
        sim_floor = int(h15.FLOOR[h15.CATS.index(cat), int(registered)])
        fwds = await guardian_forwards(kit)
        actual = any(f["kind"] == "forward" for f in fwds)
        return {
            "i": i, "category": cat, "registered_vulnerable": registered,
            "backend": backend.model_id, "service_floor": floor_int, "sim_floor": sim_floor,
            "predicted_forward": predicted, "actual_forward": actual,
            "agree": predicted == actual and floor_int == sim_floor,
            "meta": io.run_meta(),
        }
    finally:
        await kit.close()


def selfcheck() -> dict[str, Any]:
    """Each detector must fire on a hand-made violation (so 0 violations is meaningful)."""
    fired = {
        "final_ge_floor": len(invariants.check_final_ge_floor(
            [{"judgment_id": "x", "severity": 1, "severity_floor": 3}])),
        "guardian_gate_floor": len(invariants.check_guardian_gate(
            [{"situation_id": "s", "guardian_id": "g", "kind": "forward"}], {"g": True},
            {"s": 2})),
        "guardian_gate_consent": len(invariants.check_guardian_gate(
            [{"situation_id": "s", "guardian_id": "g", "kind": "forward"}], {"g": False},
            {"s": 3})),
        "no_opening_exec": len(invariants.check_no_opening_exec(
            [{"capability": "lock", "action": "unlock", "kind": "execute",
              "outcome": "executed"}])),
        "no_trait_leak": len(invariants.check_no_trait_leak(
            [("t", {"label": "독거 어르신"})], ["age_75_plus"])),
    }
    return {"fired": fired, "all_fire": all(v >= 1 for v in fired.values()),
            "composer_mismatches": h15.check_composer_equivalence()}


async def collect_validate(n: int, raw: Path) -> int:
    path = raw / "validate.jsonl"
    done = io.done_keys(path, ("i",))
    written = 0
    for i in range(n):
        if (i,) in done:
            continue
        io.append_jsonl(path, await validate_one(i))
        written += 1
    if not io.read_jsonl(raw / "selfcheck.jsonl"):
        io.append_jsonl(raw / "selfcheck.jsonl", {**selfcheck(), "meta": io.run_meta()})
    return written


# ── figures ──────────────────────────────────────────────────────────────────
def make_figures(summary: dict[str, Any]) -> list[Path]:
    import matplotlib.pyplot as plt
    import numpy as np
    from matplotlib.colors import LogNorm

    plotstyle.setup()
    out: list[Path] = []
    h = summary.get("h15")
    if not h:
        return out
    cells = h["cells"]

    # 1) false-page / miss curves vs attack, honesty fixed, selected (fa, unreg)
    fig, axes = plt.subplots(2, 3, figsize=(13, 6.5), sharex=True)
    for col, (fa, u) in enumerate([(0.02, 0.0), (0.02, 0.1), (0.02, 0.3)]):
        for row, metric in enumerate(("false_rate", "miss_rate")):
            ax = axes[row][col]
            for arm, color, ls in (("liot", plotstyle.policy_color("liot"), "-"),
                                   ("final", plotstyle.policy_color("model_only"), "--")):
                for hon, alpha in ((0.6, 0.45), (1.0, 1.0)):
                    xs, ys = [], []
                    for a in h15.ATTACK:
                        c = cells.get(f"fa{fa}|u{u}|h{hon}|a{a}|b0|base")
                        if c:
                            xs.append(a)
                            ys.append(c[arm][metric])
                    ax.plot(xs, ys, color=color, linestyle=ls, alpha=alpha, marker="o",
                            label=f"{'LIOT 표 하한' if arm == 'liot' else '최종값'} · 정직도 {hon}")
            ax.set_title(f"오경보 {fa:.0%} · 등록 안 된 취약 {u:.0%}", fontsize=9)
            if row == 1:
                ax.set_xlabel("공격 비율(모델이 늘 '위험'이라 답함)")
            ax.set_ylabel("가족 오호출률(사건당)" if metric == "false_rate" else "가족 놓침률(사건당)")
    axes[0][0].legend(fontsize=6)
    fig.suptitle("H1.5 가족 호출 기준 — 오호출(위)과 놓침(아래)", fontsize=12)
    out.append(plotstyle.save(fig, EXP_DIR, "h15_false_and_miss_curves"))

    # 2) c* maps: fa x unreg panels, honesty (x) vs attack (y)
    fig, axes = plt.subplots(len(h15.FA_LEVELS), len(h15.UNREG_LEVELS), figsize=(11, 11),
                             sharex=True, sharey=True)
    for i, fa in enumerate(h15.FA_LEVELS):
        for j, u in enumerate(h15.UNREG_LEVELS):
            ax = axes[i][j]
            grid = np.full((len(h15.ATTACK), len(h15.HONESTY)), np.nan)
            txt = [["" for _ in h15.HONESTY] for _ in h15.ATTACK]
            for yi, a in enumerate(h15.ATTACK):
                for xi, hon in enumerate(h15.HONESTY):
                    c = cells.get(f"fa{fa}|u{u}|h{hon}|a{a}|b0|base")
                    if not c:
                        continue
                    cs = c["c_star"]
                    if cs["class"] == "tradeoff":
                        grid[yi, xi] = max(cs["point"], 1e-3)
                        txt[yi][xi] = f"{cs['point']:.2g}"
                    else:
                        txt[yi][xi] = {"final_dominates": "F", "liot_dominates": "L",
                                       "tie": "="}.get(cs["class"], "?")
            im = ax.imshow(grid, origin="lower", aspect="auto", cmap="Blues",
                           norm=LogNorm(vmin=0.01, vmax=100))
            for yi in range(len(h15.ATTACK)):
                for xi in range(len(h15.HONESTY)):
                    ax.text(xi, yi, txt[yi][xi], ha="center", va="center", fontsize=7)
            ax.set_xticks(range(len(h15.HONESTY)), [f"{x:.1f}" for x in h15.HONESTY], fontsize=7)
            ax.set_yticks(range(len(h15.ATTACK)), [f"{x:.0%}" for x in h15.ATTACK], fontsize=7)
            ax.set_title(f"오경보 {fa:.0%} · 미등록 취약 {u:.0%}", fontsize=8)
            ax.grid(False)
    fig.supxlabel("모델 정직도")
    fig.supylabel("공격 비율")
    fig.suptitle("손익분기 비용비 c* (놓침 1건 비용 / 오호출 1건 비용). F=최종값 우세 · L=LIOT 우세",
                 fontsize=11)
    fig.colorbar(im, ax=axes, shrink=0.5, label="c*")
    out.append(plotstyle.save(fig, EXP_DIR, "h15_cstar_maps"))

    # 3) trait-inference exploratory arm
    inf = h.get("infer", {})
    if inf:
        fig, axes = plt.subplots(1, 2, figsize=(11, 4))
        for ax, metric, title in ((axes[0], "false_rate", "가족 오호출률"),
                                  (axes[1], "miss_rate", "가족 놓침률")):
            labels, vals = [], []
            base = inf.get("fa0.02|u0.1", {})
            for k in ("liot", *[f"infer_{t}_{f}" for t in h15.INFER_TPR for f in h15.INFER_FPR]):
                if k in base:
                    labels.append("LIOT" if k == "liot" else k.replace("infer_", "추론 "))
                    vals.append(base[k][metric])
            ax.bar(range(len(vals)), vals, color=[plotstyle.ACCENT] + [plotstyle.GREY_LIGHT] * (
                len(vals) - 1))
            ax.set_xticks(range(len(vals)), labels, rotation=60, ha="right", fontsize=7)
            ax.set_title(f"{title} (오경보 2% · 미등록 취약 10%)", fontsize=9)
        fig.suptitle("탐색: 거주자 특성 추론을 허용하면 (참양성률_거짓양성률)", fontsize=11)
        out.append(plotstyle.save(fig, EXP_DIR, "h15_trait_inference_exploratory"))
    return out


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
    p.add_argument("--part", choices=["fuzz", "h15", "validate", "all"], default="all")
    p.add_argument("--n", type=int, default=None, help="fuzz episodes per behaviour")
    p.add_argument("--quick", action="store_true")
    p.add_argument("--analyze", action="store_true")
    args = p.parse_args()
    if args.analyze:
        summary = analyze.summarize(RAW_DIR)
        print(f"summary: {io.write_summary(EXP_DIR, summary)}")
        for f in make_figures(summary):
            print(f"figure: {f}")
        return
    raw = EXP_DIR / "raw_quick" if args.quick else RAW_DIR

    async def go() -> None:
        if args.part in ("fuzz", "all"):
            n = args.n or (4 if args.quick else EPISODES_PER_BEHAVIOR)
            print("fuzz wrote", await collect_fuzz(n, raw))
        if args.part in ("validate", "all"):
            print("validate wrote", await collect_validate(10 if args.quick else 200, raw))

    if args.part in ("h15", "all"):
        print("h15 wrote", collect_h15(raw, args.quick))
    asyncio.run(go())


if __name__ == "__main__":
    main()
