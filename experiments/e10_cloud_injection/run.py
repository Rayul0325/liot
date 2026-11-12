"""E10 runner (cloud injection robustness, C4.1; third coder, C4.4).

    PYTHONPATH=$PWD .venv/bin/python -m experiments.e10_cloud_injection.run --quick      # offline smoke
    # data collection (each resumable, budget-capped; see the modules' docstrings)
    PYTHONPATH=$PWD .venv/bin/python -m experiments.e10_cloud_injection.coders           # C4.4, 61 Gemini calls
    PYTHONPATH=$PWD .venv/bin/python -m experiments.e10_cloud_injection.cloud --judges jev-api,haiku
    PYTHONPATH=$PWD .venv/bin/python -m experiments.e10_cloud_injection.cloud --judges gemini --k 6
    PYTHONPATH=$PWD zsh experiments/e10_cloud_injection/run_local.sh                     # five local models
    # summary + figures
    PYTHONPATH=$PWD .venv/bin/python -m experiments.e10_cloud_injection.run --analyze

--quick makes no network call and starts no server: it builds the 576 items through the production
path, checks every outbound evidence string against emit_external, checks the design (cases, variants,
length matching) and recomputes the summary into a temporary folder.
"""

from __future__ import annotations

import argparse
import tempfile
from pathlib import Path
from typing import Any

from experiments.common import io, plotstyle
from experiments.e10_cloud_injection import analyze
from experiments.e10_cloud_injection import common as C

EXP_DIR = Path(__file__).resolve().parent


def quick() -> None:
    from liot.core.egress_gate import Destination, emit_external
    from liot.layers.l3_judgment.decision_backend import EvidenceBuilder

    built, dsg = C.build_items(C.K_MAX)
    ev = EvidenceBuilder("quick")
    for b in built.values():
        emit_external(Destination.LLM, {"evidence": ev.build(b.candidate, b.ctx, b.gate1.floor_tier)},
                      trace_id=b.ctx.trace_id)
    variants, neutral = C.load_variants(), C.load_neutral()
    assert len(variants) >= 40 and set(neutral) == {v["id"] for v in variants}
    assert len({k[0] for k in built}) >= 30
    diffs = [len(neutral[v["id"]]) - len(v["text"]) for v in variants]
    assert max(abs(d) for d in diffs) <= 2
    print(f"quick: items={len(built)} cases={len({k[0] for k in built})} variants={len(variants)} "
          f"max|len diff|={max(abs(d) for d in diffs)} egress OK")
    with tempfile.TemporaryDirectory() as td:
        s = analyze.summarize(EXP_DIR / "raw")
        print("quick: analyze ok, groups:", list(s.get("c41", {}).get("groups", {})), "->", td)


def figures(s: dict[str, Any]) -> None:
    import matplotlib.pyplot as plt
    import numpy as np

    plotstyle.setup()
    c = s.get("c41", {})
    per = c.get("per_judge", {})
    if per:
        order = [j for j in (*C.CLOUD_JUDGES, *C.LOCAL_MODELS) if j in per]
        fig, ax = plt.subplots(figsize=(8.6, 4.4))
        y = np.arange(len(order))
        for i, j in enumerate(order):
            p = per[j]
            ci = p["flip_ci95"] or [p["flip_rate"], p["flip_rate"]]
            color = plotstyle.ACCENT if j in C.CLOUD_JUDGES else plotstyle.GREY_DARK
            ax.errorbar(p["flip_rate"], i, xerr=[[p["flip_rate"] - ci[0]], [ci[1] - p["flip_rate"]]],
                        fmt="o", color=color, capsize=3)
        ax.set_yticks(y, order, fontsize=8)
        ax.invert_yaxis()
        ax.set_xlabel("짝지은 뒤집힘 비율 (대조에서 하한 아래가 아니던 항목 중 주입으로 하한 아래가 된 몫, 95% 구간)")
        ax.set_title("판정자별 주입 효과 (파랑 = 클라우드, 회색 = 로컬)")
        plotstyle.save(fig, EXP_DIR, "c41_flip_rate_per_judge")
    d = c.get("difference_local_minus_cloud")
    g = c.get("groups", {})
    if d and len(g) == 2:
        fig, ax = plt.subplots(figsize=(7.0, 3.2))
        for i, name in enumerate(("cloud", "local")):
            v = g[name]
            ci = v["flip_ci95"]
            ax.errorbar(v["flip_rate"], i, xerr=[[v["flip_rate"] - ci[0]], [ci[1] - v["flip_rate"]]], fmt="o",
                        color=plotstyle.ACCENT if name == "cloud" else plotstyle.GREY_DARK, capsize=3)
        ax.set_yticks([0, 1], ["클라우드(3)", "로컬(3: jevk5 계열 1)"])
        ax.set_xlabel("판정자 같은 무게 평균 뒤집힘 비율")
        ax.set_title(f"로컬-클라우드 차이 {d['point']:+.3f} [{d['ci95'][0]:+.3f}, {d['ci95'][1]:+.3f}] → {d['verdict']}")
        plotstyle.save(fig, EXP_DIR, "c41_groups")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--quick", action="store_true")
    ap.add_argument("--analyze", action="store_true")
    args = ap.parse_args()
    if args.quick:
        quick()
        return
    if args.analyze:
        s = analyze.summarize(EXP_DIR / "raw")
        path = io.write_summary(EXP_DIR, s)
        figures(s)
        print("summary ->", path)
        return
    ap.print_help()


if __name__ == "__main__":
    main()
