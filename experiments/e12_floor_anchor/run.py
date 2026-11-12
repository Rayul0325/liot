"""E12 runner (C4.5): do Jev, Haiku and Gemini anchor on the Gate1 floor they are shown?

    PYTHONPATH=$PWD .venv/bin/python -m experiments.e12_floor_anchor.run --quick          # smoke, scratch dirs
    PYTHONPATH=$PWD .venv/bin/python -m experiments.e12_floor_anchor.run --phase capture  # E9 re-run, no luna
    PYTHONPATH=$PWD .venv/bin/python -m experiments.e12_floor_anchor.run --phase judge    # floor swap, 3 judges
    PYTHONPATH=$PWD .venv/bin/python -m experiments.e12_floor_anchor.run --analyze

Hard cloud cap for the whole unit: BUDGET_USD (capture + judges).  No OpenAI call anywhere.
"""

from __future__ import annotations

import argparse
import json
import re
import tempfile
import threading
from pathlib import Path
from typing import Any

from experiments.common import io, plotstyle
from experiments.e12_floor_anchor import analyze, capture, judges

EXP_DIR = Path(__file__).resolve().parent
RAW = EXP_DIR / "raw"
BUDGET_USD = 0.50
CAPTURE_CAP_USD = 0.05  # E9 cost $0.0113 for the same 30 days
SAFETY_MARGIN_USD = 0.02
_TIER_WORDS = re.compile(r"ambient|breath|dusk|ember|잔잔|숨결|땅거미|불씨", re.IGNORECASE)


def evidence_checks(steps: list[dict[str, Any]]) -> dict[str, Any]:
    """Round trip, egress idempotence and leak of tier words outside the floor field."""
    roundtrip = sum(1 for s in steps if judges.set_floor(s["evidence"], s["floor"]) == s["evidence"])
    egress_same = 0
    leak = 0
    for s in steps:
        safe = judges._sanitize(s["evidence"], "e12-check")
        egress_same += int(safe == s["evidence"])
        d = json.loads(s["evidence"])
        d.pop("gate1_floor", None)
        leak += int(bool(_TIER_WORDS.search(json.dumps(d, ensure_ascii=False))))
    return {"steps": len(steps), "roundtrip_unchanged": roundtrip, "egress_unchanged": egress_same,
            "tier_word_outside_floor_field": leak}


def phase_judge(steps: list[dict[str, Any]], reps: dict[str, int], budget: float, answers: Path,
                judge_list: tuple[str, ...]) -> dict[str, Any]:
    from liot.secrets_env import load_secrets

    secrets = load_secrets()
    if secrets.status != "ok":
        raise SystemExit(f"secrets not usable: {secrets.status}")
    meter = judges.Meter(budget)
    stop: dict[str, str] = {}
    threads = [threading.Thread(target=judges.run_judge,
                                args=(j, secrets, steps, reps[j], meter, stop, answers), name=j)
               for j in judge_list]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    summ = meter.summary()
    print("stop reasons:", json.dumps(stop, ensure_ascii=False))
    print("cost:", json.dumps({k: summ[k] for k in ("calls", "run_usd", "tripped", "by_kind")},
                              ensure_ascii=False))
    return {"stop": stop, "cost": {k: summ[k] for k in ("calls", "run_usd", "run_cap_usd", "tripped",
                                                         "unknown_price_calls", "estimated_calls")},
            "by_kind": summ["by_kind"], "reps": reps, "judges": list(judge_list), "meta": io.run_meta()}


def make_figure(summary: dict[str, Any]) -> Path:
    import matplotlib.pyplot as plt

    plotstyle.setup()
    fig, axes = plt.subplots(1, 2, figsize=(11, 3.8))
    names = [j for j in judges.JUDGES if j in summary["anchoring"]]
    width = 0.38
    for k, (direction, color) in enumerate((("up", plotstyle.ACCENT), ("down", plotstyle.GREY))):
        vals, los, his = [], [], []
        for j in names:
            r = summary["anchoring"][j]["by_direction"].get(direction, {}).get("moved_with")
            vals.append(r["rate"] or 0.0 if r else 0.0)
            los.append((r["rate"] or 0.0) - (r["lo"] or 0.0) if r and r["rate"] is not None else 0.0)
            his.append((r["hi"] or 0.0) - (r["rate"] or 0.0) if r and r["rate"] is not None else 0.0)
        xs = [i + (k - 0.5) * width for i in range(len(names))]
        axes[0].bar(xs, vals, width, color=color, label="하한 한 단계 " + ("위" if direction == "up" else "아래"),
                    yerr=[los, his], capsize=3)
    axes[0].set_xticks(range(len(names)))
    axes[0].set_xticklabels(names)
    axes[0].set_ylim(0, 1.25)
    axes[0].set_ylabel("답이 주어진 하한을 따라간 단계의 몫")
    axes[0].set_title("하한 바꿔치기 (막대 = 장면 종류로 묶은 95% 구간)")
    axes[0].legend(frameon=False, loc="upper left", ncol=2)
    dep = summary["departure_true_floor"]
    above = [dep[j]["above"] for j in names]
    below = [dep[j]["below"] for j in names]
    axes[1].bar(range(len(names)), above, color=plotstyle.ACCENT, label="하한보다 위")
    axes[1].bar(range(len(names)), below, bottom=above, color=plotstyle.GREY, label="하한보다 아래")
    axes[1].set_xticks(range(len(names)))
    axes[1].set_xticklabels(names)
    axes[1].set_ylabel("참 하한을 줬을 때 벗어난 단계 수")
    axes[1].set_title("판정자별 하한 이탈")
    axes[1].legend(frameon=False)
    return plotstyle.save(fig, EXP_DIR, "floor_swap_anchoring")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--phase", choices=("capture", "judge"), default=None)
    parser.add_argument("--analyze", action="store_true")
    parser.add_argument("--quick", action="store_true", help="1 seed x 1 day, first 4 steps, scratch outputs")
    parser.add_argument("--seeds", default=None, help="comma list for capture (default 1..10)")
    parser.add_argument("--jev-reps", type=int, default=2)
    parser.add_argument("--haiku-reps", type=int, default=1)
    parser.add_argument("--gemini-reps", type=int, default=1)
    parser.add_argument("--judges", default=",".join(judges.JUDGES))
    args = parser.parse_args()

    if args.analyze:
        summary = analyze.summarize(RAW)
        print(f"summary: {io.write_summary(EXP_DIR, summary)}")
        print(f"figure: {make_figure(summary)}")
        return

    reps = {"jev": args.jev_reps, "haiku": args.haiku_reps, "gemini": args.gemini_reps}
    if args.quick:
        scratch = Path(tempfile.mkdtemp(prefix="e12-quick-"))  # scratch, never committed
        capture.USAGE_PATH = scratch / "cloud_usage_capture.json"
        n = capture.collect((1,), 1, scratch / "runs.jsonl", scratch / "steps.jsonl",
                            scratch / "cloud_steps.jsonl", CAPTURE_CAP_USD)
        print(f"quick capture wrote {n} runs to {scratch}")
        judges.RAW = scratch
        judges.STEPS, judges.CLOUD_STEPS = scratch / "steps.jsonl", scratch / "cloud_steps.jsonl"
        judges.USAGE_PATH = scratch / "cloud_usage_judges.json"
        steps = judges.load_steps(4)
        print("evidence checks:", evidence_checks(steps))
        out = phase_judge(steps, dict.fromkeys(judges.JUDGES, 1), 0.05, scratch / "answers.jsonl",
                          tuple(args.judges.split(",")))
        print("quick judge done:", json.dumps(out["stop"]))
        return

    if args.phase == "capture":
        seeds = tuple(int(x) for x in args.seeds.split(",")) if args.seeds else capture.SEEDS
        n = capture.collect(seeds, capture.DAYS, capture.RUNS, capture.STEPS, capture.CLOUD_STEPS,
                            CAPTURE_CAP_USD)
        print(f"capture: wrote {n} runs")
        return
    if args.phase == "judge":
        steps = judges.load_steps()
        spent_capture = sum(r["usd"] for r in io.read_jsonl(capture.RUNS))
        checks = evidence_checks(steps)
        print("evidence checks:", checks)
        io.append_jsonl(RAW / "checks.jsonl", {**checks, "meta": io.run_meta()})
        budget = BUDGET_USD - spent_capture - SAFETY_MARGIN_USD
        out = phase_judge(steps, reps, budget, judges.ANSWERS, tuple(args.judges.split(",")))
        io.append_jsonl(RAW / "budget.jsonl", {"capture_usd": spent_capture, **out})
        return
    parser.error("choose --phase, --analyze or --quick")


if __name__ == "__main__":
    main()
