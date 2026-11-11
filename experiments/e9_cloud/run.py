"""E9 runner: DaySim with models="cloud" (real Jev, luna, cheap model, memgineering), trace on.

    PYTHONPATH=$PWD .venv/bin/python -m experiments.e9_cloud.run --quick     # 1 seed, 1 day
    PYTHONPATH=$PWD .venv/bin/python -m experiments.e9_cloud.run             # 10 seeds x 3 days
    PYTHONPATH=$PWD .venv/bin/python -m experiments.e9_cloud.run --analyze

Keys are loaded by the existing code from ~/.config/liot/secrets.env and never read or printed here.
Raw responses (summary + trace events) go ONLY to raw/cloud_runs.jsonl (git-ignored). Derived numbers
go to raw/derived.jsonl (committed). Budget: the run stops before a seed when the estimated cumulative
cost plus the largest run so far would pass BUDGET_USD; each run's own cost cap is the smaller of the
shipped cap and the remaining budget. The estimate uses the shipped price table (models without a
price count as $0 and are reported as unknown_price_calls).
"""

from __future__ import annotations

import argparse
import asyncio
from dataclasses import replace
from pathlib import Path
from typing import Any

from experiments.common import io, plotstyle
from experiments.e9_cloud import analyze, derive

EXP_DIR = Path(__file__).resolve().parent
DERIVED = EXP_DIR / "raw" / "derived.jsonl"
CLOUD_RAW = EXP_DIR / "raw" / "cloud_runs.jsonl"
SEEDS = tuple(range(1, 11))
DAYS = 3
BUDGET_USD = 1.00


def one_run(seed: int, days: int, remaining_usd: float) -> tuple[dict[str, Any], dict[str, Any], list[Any]]:
    from liot.day_cloud import build_cloud_kit
    from liot.day_sim import DaySim
    from liot.secrets_env import load_secrets

    kit = build_cloud_kit(load_secrets())
    kit.meter.limits = replace(kit.meter.limits,
                               cost_cap_per_run_usd=min(kit.meter.limits.cost_cap_per_run_usd, remaining_usd))
    sim = DaySim(seed=seed, days=days, models="cloud", cloud=kit, trace=True, research=True,
                 house_id=f"e9-s{seed}")
    events: list[Any] = []
    asyncio.run(sim.run(lambda stage, data: events.append([stage, data])))
    summary = sim.summary()
    d = derive.derive(summary, seed, days)
    d["meta"] = io.run_meta()
    return d, summary, events


def collect(seeds: tuple[int, ...], days: int, derived: Path, cloud_raw: Path) -> int:
    done = io.done_keys(derived, ("seed",))
    spent = sum(r["usd"] for r in io.read_jsonl(derived))
    biggest = max((r["usd"] for r in io.read_jsonl(derived)), default=0.0)
    written = 0
    for seed in seeds:
        if (seed,) in done:
            continue
        if spent >= BUDGET_USD or spent + biggest >= BUDGET_USD:
            print(f"budget stop before seed {seed}: spent={spent:.4f} biggest={biggest:.4f} "
                  f"cap={BUDGET_USD}", flush=True)
            break
        d, summary, events = one_run(seed, days, BUDGET_USD - spent)
        io.append_jsonl(cloud_raw, {"seed": seed, "days": days, "summary": summary, "events": events,
                                    "meta": d["meta"]})
        io.append_jsonl(derived, d)
        spent += d["usd"]
        biggest = max(biggest, d["usd"])
        written += 1
        print(f"seed {seed}: calls={d['calls']} usd={d['usd']:.4f} cumulative={spent:.4f} "
              f"tripped={d['tripped']} luna_unavailable={d['luna_unavailable']} "
              f"first_alerts={d['first_alerts_immediate']}/{d['first_alerts']}", flush=True)
        if d["first_alerts_immediate"] != d["first_alerts"] or d["first_alerts"] != days:
            print("SAFETY CHECK FAILED - stopping", flush=True)
            return -written
    return written


def make_figure(summary: dict[str, Any]) -> Path:
    import matplotlib.pyplot as plt

    plotstyle.setup()
    fig, axes = plt.subplots(1, 2, figsize=(11, 3.8))
    stage = summary["stage_latency"]
    names = list(stage)
    axes[0].bar(range(len(names)), [stage[k]["mean_s_per_call"] or 0 for k in names],
                color=plotstyle.ACCENT)
    axes[0].set_xticks(range(len(names)))
    axes[0].set_xticklabels(names, rotation=30, ha="right")
    axes[0].set_ylabel("호출 한 번당 평균 시간 (초)")
    axes[0].set_title("단계별 실제 모델 지연 (부하가 높은 기계, 상대 비교만)")
    h = summary["h54"]
    labels = ["둘 다 하한", "Jev 가 하한을 벗어남", "luna 가 하한을 벗어남", "벗어난 단계 중 같은 답"]
    both = sum(c for k, c in h["depart_confusion_jev_luna"].items() if k.split("|")[0] == k.split("|")[1])
    values = [h["both_at_floor"], h["jev_departs"], h["luna_departs"], both]
    axes[1].bar(range(4), values, color=[plotstyle.GREY_LIGHT, plotstyle.GREY, plotstyle.GREY_DARK,
                                         plotstyle.ACCENT])
    axes[1].set_xticks(range(4))
    axes[1].set_xticklabels(labels, rotation=30, ha="right")
    axes[1].set_ylabel("판정 단계 수")
    axes[1].set_title("Jev·luna 가 하한을 벗어나는 단계 (일치는 정확도가 아님)")
    return plotstyle.save(fig, EXP_DIR, "stage_latency_and_departures")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--analyze", action="store_true")
    parser.add_argument("--quick", action="store_true", help="1 seed, 1 day, scratch outputs")
    parser.add_argument("--rederive", action="store_true",
                        help="rebuild raw/derived.jsonl from raw/cloud_runs.jsonl (no API calls)")
    args = parser.parse_args()
    if args.rederive:
        rows = io.read_jsonl(CLOUD_RAW)
        DERIVED.write_text("", encoding="utf-8")
        for r in sorted(rows, key=lambda x: x["seed"]):
            d = derive.derive(r["summary"], r["seed"], r["days"])
            d["meta"] = r["meta"]
            io.append_jsonl(DERIVED, d)
        print(f"rederived {len(rows)} runs from {CLOUD_RAW.name}")
        return
    if args.analyze:
        summary = analyze.summarize(EXP_DIR / "raw")
        print(f"summary: {io.write_summary(EXP_DIR, summary)}")
        print(f"figure: {make_figure(summary)}")
        return
    if args.quick:
        scratch = Path("/private/tmp/claude-501/-Users-rayul-liot/b877ae61-64ae-4512-b8b4-d5ca79c8f749/"
                       "scratchpad")
        n = collect((1,), 1, scratch / "b3_e9_quick_derived.jsonl", scratch / "b3_e9_quick_cloud.jsonl")
        print(f"wrote {n} quick records")
        return
    n = collect(SEEDS, DAYS, DERIVED, CLOUD_RAW)
    print(f"wrote {n} new records to {DERIVED}")


if __name__ == "__main__":
    main()
