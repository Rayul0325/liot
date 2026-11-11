"""e0_smoke runner — the template other experiments copy.

    PYTHONPATH=$PWD .venv/bin/python -m experiments.e0_smoke.run            # collect raw
    PYTHONPATH=$PWD .venv/bin/python -m experiments.e0_smoke.run --analyze  # summary + figure

Pattern: one raw record per run appended (fsync) to raw/runs.jsonl, keyed so a killed run
resumes where it stopped; analysis reads only raw/ and is deterministic.
"""

from __future__ import annotations

import argparse
import asyncio
import time
from pathlib import Path

from experiments.common import io, plotstyle
from experiments.common.service_kit import (
    GAS,
    FixedBackend,
    TimedTransport,
    build_service,
    reading,
)
from experiments.e0_smoke import analyze

EXP_DIR = Path(__file__).resolve().parent
RAW = EXP_DIR / "raw" / "runs.jsonl"
N_RUNS = 20


async def one_run(run_id: int) -> dict[str, object]:
    """Feed one abnormal gas reading; return first-alert latency (ms) at the transport."""
    transport = TimedTransport()
    kit = await build_service(backend=FixedBackend(), transport=transport)
    try:
        t0 = time.perf_counter()
        await kit.svc.handle_reading(reading(GAS, "on"))
        stamps = list(transport.stamps)
        latency_ms = (stamps[0][0] - t0) * 1000.0 if stamps else None
        await kit.svc.drain_phase2()
        return {
            "run_id": run_id,
            "latency_ms": latency_ms,
            "pushes_phase1": len(stamps),
            "meta": io.run_meta(),
        }
    finally:
        await kit.close()


async def collect() -> int:
    done = io.done_keys(RAW, ("run_id",))
    written = 0
    for run_id in range(N_RUNS):
        if (run_id,) in done:
            continue
        io.append_jsonl(RAW, await one_run(run_id))
        written += 1
    return written


def make_figure(summary: dict[str, object]) -> Path:
    import matplotlib.pyplot as plt

    plotstyle.setup()
    rows = io.read_jsonl(RAW)
    values = [r["latency_ms"] for r in rows if r.get("latency_ms") is not None]
    fig, ax = plt.subplots(figsize=(6, 3.2))
    ax.hist(values, bins=10, color=plotstyle.GREY_LIGHT, edgecolor=plotstyle.GREY)
    ax.axvline(float(summary["median_ms"]), color=plotstyle.ACCENT, label="중앙값")  # type: ignore[arg-type]
    ax.axvline(float(summary["p95_ms"]), color=plotstyle.GREY_DARK, linestyle="--", label="p95")  # type: ignore[arg-type]
    ax.set_xlabel("첫 알림까지 걸린 시간 (ms)")
    ax.set_ylabel("실행 횟수")
    ax.set_title("가스 이상 신호 → 첫 알림 지연 (e0 스모크)")
    ax.legend()
    return plotstyle.save(fig, EXP_DIR, "first_alert_latency")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--analyze", action="store_true", help="write summary.json and figure")
    args = parser.parse_args()
    if args.analyze:
        summary = analyze.summarize(EXP_DIR / "raw")
        print(f"summary: {io.write_summary(EXP_DIR, summary)}")
        print(f"figure: {make_figure(summary)}")
        return
    written = asyncio.run(collect())
    print(f"wrote {written} new records to {RAW}")


if __name__ == "__main__":
    main()
