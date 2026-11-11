"""E1 runner — first-alert latency of dispatch architectures + H1.1c replay + H1.2 + H1.3.

    PYTHONPATH=$PWD .venv/bin/python -m experiments.e1_latency.run --part main --shard 0/10
    PYTHONPATH=$PWD .venv/bin/python -m experiments.e1_latency.run --part replay
    PYTHONPATH=$PWD .venv/bin/python -m experiments.e1_latency.run --part h12 --tag before
    PYTHONPATH=$PWD .venv/bin/python -m experiments.e1_latency.run --part h13 --shard 0/6
    PYTHONPATH=$PWD .venv/bin/python -m experiments.e1_latency.run --quick       # smoke, raw_quick/
    PYTHONPATH=$PWD .venv/bin/python -m experiments.e1_latency.run --analyze     # summary+figures

Parts (pre-registration v2, experiments/HYPOTHESES.md)
  main    H1.1: 5 architectures (liot, wait_for_model, short_0.25/0.5/1.0) x 4 model conditions.
          Paired incidents (same category and same 'model would raise' flag in every cell), cells
          interleaved round-robin per incident. Alternatives are at their best setting: when the
          model fails or the deadline passes they send immediately (nothing waits further).
  replay  H1.1c: bench/risk/raw situation answers replayed through the real service (no timing).
  h12     H1.2: slow comfort judgment (T) inside ``run_tick``; a gas reading arrives o seconds after
          the judgment started (o on a fixed grid of fractions of T); no-comfort control per T.
          ``--tag before|after`` is stored in every record (the U1 fix is applied between).
  h13     H1.3: ntfy send delay x guardian count; a smoke reading arrives while sends are blocked.

Every record carries ``run_meta()`` (git sha, dirty flag, load1). Records are keyed so a killed
run resumes. Timing is only interpreted as relative comparisons inside the same time window.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import random
import threading
import time
from datetime import timedelta
from pathlib import Path
from typing import Any

from experiments.common import io, plotstyle
from experiments.common.service_kit import (
    CATS,
    COMFORT_YAML,
    GAS,
    RaisingBackend,
    RecordingBackend,
    SlowBackend,
    TimedTransport,
    build_service,
    proposals,
    reading,
    situations,
)
from experiments.e1_latency import analyze
from experiments.e1_latency.arms import ARMS, C27_ARMS, make_arm
from liot.core.risk import RiskTier
from liot.core.severity import SeverityLevel
from liot.layers.l3_judgment.llm_judge import LLMResult, LLMUnavailable

EXP_DIR = Path(__file__).resolve().parent
RAW_DIR = EXP_DIR / "raw"
BENCH_RAW = EXP_DIR.parents[1] / "bench" / "risk" / "raw"

DEADLINE_S = 2.0          # service phase-2 deadline used in every arm
NORMAL_S, SLOW_S, TIMEOUT_S = 0.2, 1.5, 4.0
CONDS = ("normal", "slow", "raising", "timeout")
P_RAISE = 0.30            # assumed share of incidents where the model raises a WARNING to CRITICAL
N_INCIDENTS = 300

SMOKE = "binary_sensor.kitchen_smoke"
ENTITIES: dict[str, tuple[str, object, SeverityLevel]] = {
    # category: (entity_id, abnormal raw value, table floor)
    "gas": (GAS, "on", SeverityLevel.CRITICAL),
    "smoke": (SMOKE, "on", SeverityLevel.CRITICAL),
    "leak": ("binary_sensor.bath_leak", "on", SeverityLevel.WARNING),
    "door": ("binary_sensor.front_door", "on", SeverityLevel.WARNING),
    "oven": ("binary_sensor.oven_on", "on", SeverityLevel.WARNING),
    "high_temp": ("sensor.kitchen_temp", 70.0, SeverityLevel.WARNING),
    "water_main": ("binary_sensor.water_main", "on", SeverityLevel.WARNING),
    "lock": ("binary_sensor.front_lock", "on", SeverityLevel.WARNING),
}
CATEGORY_MAP = {**CATS, **{eid: cat for cat, (eid, _, _) in ENTITIES.items()}}
MAIN_CATEGORIES = ("gas", "smoke", "leak", "door", "oven", "high_temp")
_RANK = {"기록만": 0, "주의": 1, "경고": 2, "위험": 3}


def _sev(payload: dict[str, Any]) -> str | None:
    return payload.get("severity_label")


def _final(sevs: list[str | None]) -> str | None:
    return max(sevs, key=lambda s: _RANK.get(s or "", -1)) if sevs else None


# ── main: H1.1 ───────────────────────────────────────────────────────────────
def incident_spec(i: int) -> dict[str, Any]:
    rng = random.Random(1000 + i)
    cat = MAIN_CATEGORIES[rng.randrange(len(MAIN_CATEGORIES))]
    return {"category": cat, "would_raise": rng.random() < P_RAISE}


def make_backend(cond: str, spec: dict[str, Any]) -> Any:
    floor = ENTITIES[spec["category"]][2]
    answer = SeverityLevel.CRITICAL if spec["would_raise"] else floor
    if cond == "raising":
        return RaisingBackend()
    seconds = {"normal": NORMAL_S, "slow": SLOW_S, "timeout": TIMEOUT_S}[cond]
    return SlowBackend(seconds, tier=RiskTier.EMBER, severity=answer)


async def one_incident(i: int, arm: str, cond: str) -> dict[str, Any]:
    spec = incident_spec(i)
    eid, raw, floor = ENTITIES[spec["category"]]
    kit = await build_service(
        backend=make_backend(cond, spec), categories=CATEGORY_MAP,
        phase2_deadline_s=DEADLINE_S, transport=TimedTransport(),
    )
    kit = await make_arm(kit, arm, DEADLINE_S)
    tr = kit.transport
    try:
        t0 = time.perf_counter()
        await kit.svc.handle_reading(reading(eid, raw))
        latency = (tr.stamps[0][0] - t0) if tr.stamps else None
        await kit.svc.drain_phase2()
        sevs = [_sev(s[3]) for s in tr.stamps]
        return {
            "incident": i, "arm": arm, "cond": cond,
            "category": spec["category"], "floor": floor.name,
            "would_raise": spec["would_raise"],
            "latency_s": latency, "pushes": len(sevs), "sev_labels": sevs,
            "first_sev": sevs[0] if sevs else None, "final_sev": _final(sevs),
            "changed": bool(sevs) and sevs[0] != _final(sevs),
            "meta": io.run_meta(),
        }
    finally:
        shutdown = getattr(kit.svc, "shutdown", None)
        await kit.close()
        if shutdown is not None:
            shutdown()


def cells() -> list[tuple[str, str]]:
    return [(a, c) for a in ARMS for c in CONDS]


async def collect_main(shard: int, nshards: int, n: int, raw: Path) -> int:
    path = raw / f"main_s{shard}.jsonl"
    done = io.done_keys(path, ("incident", "arm", "cond"))
    allc = cells()
    written = 0
    for i in range(shard, n, nshards):
        rot = i % len(allc)
        for arm, cond in allc[rot:] + allc[:rot]:  # round-robin start per incident
            if (i, arm, cond) in done:
                continue
            io.append_jsonl(path, await one_incident(i, arm, cond))
            written += 1
    return written


# ── C2.7: floor-aware model-first arm with the bench's real model latencies ──
def bench_latencies() -> list[float]:
    """Measured seconds per judgment (two questions) of the bench situation cases, answered
    models only (jev-omni's readout failures are excluded). Sorted for a stable index."""
    vals = [r["seconds"] for r in bench_situation_records()
            if not r["degraded"] and r.get("seconds") is not None]
    return sorted(float(v) for v in vals)


async def one_c27(i: int, arm: str, lats: list[float]) -> dict[str, Any]:
    spec = incident_spec(i)
    eid, raw, floor = ENTITIES[spec["category"]]
    model_s = lats[random.Random(5000 + i).randrange(len(lats))]  # paired across arms
    answer = SeverityLevel.CRITICAL if spec["would_raise"] else floor
    kit = await build_service(
        backend=SlowBackend(model_s, tier=RiskTier.EMBER, severity=answer),
        categories=CATEGORY_MAP, phase2_deadline_s=DEADLINE_S, transport=TimedTransport(),
    )
    kit = await make_arm(kit, arm, DEADLINE_S)
    tr = kit.transport
    try:
        t0 = time.perf_counter()
        await kit.svc.handle_reading(reading(eid, raw))
        latency = (tr.stamps[0][0] - t0) if tr.stamps else None
        await kit.svc.drain_phase2()
        sevs = [_sev(s[3]) for s in tr.stamps]
        return {
            "incident": i, "arm": arm, "category": spec["category"], "floor": floor.name,
            "would_raise": spec["would_raise"], "model_s": model_s,
            "latency_s": latency, "pushes": len(sevs), "sev_labels": sevs,
            "first_sev": sevs[0] if sevs else None, "final_sev": _final(sevs),
            "changed": bool(sevs) and sevs[0] != _final(sevs), "meta": io.run_meta(),
        }
    finally:
        shutdown = getattr(kit.svc, "shutdown", None)
        await kit.close()
        if shutdown is not None:
            shutdown()


async def collect_c27(shard: int, nshards: int, n: int, raw: Path) -> int:
    path = raw / f"c27_s{shard}.jsonl"
    done = io.done_keys(path, ("incident", "arm"))
    lats = bench_latencies()
    written = 0
    for i in range(shard, n, nshards):
        rot = i % len(C27_ARMS)
        for arm in C27_ARMS[rot:] + C27_ARMS[:rot]:
            if (i, arm) in done:
                continue
            io.append_jsonl(path, await one_c27(i, arm, lats))
            written += 1
    return written


# ── replay: H1.1c on bench raw answers ───────────────────────────────────────
class ReplayBackend(RecordingBackend):
    """Returns a recorded bench answer (tier, severity); ``fail`` replays a degraded record."""

    model_id = "replay-bench"

    def __init__(self, tier: str | None, severity: str | None, fail: bool) -> None:
        super().__init__()
        self._tier, self._sev, self._fail = tier, severity, fail

    def _answer(self, candidate, ctx, gate1_floor, severity_floor):  # type: ignore[no-untyped-def]
        if self._fail or self._tier is None:
            raise LLMUnavailable("replayed degraded record")
        asked = severity_floor is not None and self._sev is not None
        return LLMResult(
            tier=RiskTier.from_str(self._tier), confidence=0.8, rationale="replay",
            model_id=self.model_id,
            severity=SeverityLevel.from_str(self._sev) if asked else None,
            probs={"severity": {"A": 0.1, "B": 0.9}} if asked else None,
        )


def bench_situation_records() -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for f in sorted(BENCH_RAW.glob("*.jsonl")):
        if f.name.endswith(".sessions.jsonl"):
            continue
        for line in f.read_text(encoding="utf-8").splitlines():
            r = json.loads(line)
            if r.get("group") == "situation":
                out.append(r)
    return out


async def one_replay(rec: dict[str, Any], arm: str) -> dict[str, Any]:
    cat = rec["id"].split("/")[0].removeprefix("sit-")
    eid, raw, floor = ENTITIES[cat]
    backend = ReplayBackend(rec["model_tier"], rec["model_severity"], bool(rec["degraded"]))
    kit = await build_service(
        backend=backend, categories=CATEGORY_MAP, phase2_deadline_s=DEADLINE_S,
        transport=TimedTransport(),
    )
    kit = await make_arm(kit, arm, DEADLINE_S)
    tr = kit.transport
    try:
        await kit.svc.handle_reading(reading(eid, raw))
        await kit.svc.drain_phase2()
        sevs = [_sev(s[3]) for s in tr.stamps]
        return {
            "model": rec["model"], "id": rec["id"], "rep": rec["rep"], "arm": arm,
            "category": cat, "variant": rec["id"].split("/")[1],
            "table_floor": floor.name, "bench_floor": rec["severity_floor"],
            "model_severity": rec["model_severity"], "model_tier": rec["model_tier"],
            "degraded": bool(rec["degraded"]),
            "pushes": len(sevs), "sev_labels": sevs,
            "first_sev": sevs[0] if sevs else None, "final_sev": _final(sevs),
            "changed": bool(sevs) and sevs[0] != _final(sevs),
            "meta": io.run_meta(),
        }
    finally:
        shutdown = getattr(kit.svc, "shutdown", None)
        await kit.close()
        if shutdown is not None:
            shutdown()


async def collect_replay(raw: Path, limit: int | None) -> int:
    path = raw / "replay.jsonl"
    done = io.done_keys(path, ("model", "id", "rep", "arm"))
    recs = bench_situation_records()
    if limit:
        recs = recs[:limit]
    written = 0
    for rec in recs:
        for arm in ("liot", "wait_for_model"):
            if (rec["model"], rec["id"], rec["rep"], arm) in done:
                continue
            io.append_jsonl(path, await one_replay(rec, arm))
            written += 1
    return written


# ── H1.2: slow comfort judgment inside run_tick while gas arrives ────────────
O_FRACS = (0.05, 0.25, 0.5, 0.75, 0.95)
H12_LEVELS = (0.5, 1.5, 3.0)


async def one_h12(seconds: float, ofrac: float, rep: int, tag: str) -> dict[str, Any]:
    """ofrac >= 0: gas arrives ofrac*T after the comfort judgment starts; ofrac < 0: control."""
    backend = SlowBackend(seconds, comfort_only=True, tier=RiskTier.AMBIENT)
    kit = await build_service(
        backend=backend, comfort_yaml=COMFORT_YAML, transport=TimedTransport(),
        phase2_deadline_s=DEADLINE_S,
    )
    svc, tr = kit.svc, kit.transport
    loop = asyncio.get_running_loop()
    arrival: dict[str, float] = {}
    gas_tasks: list[asyncio.Future[Any]] = []
    judged = threading.Event()
    o = max(ofrac, 0.0) * seconds

    def inject() -> None:
        # Stamp the arrival from a plain thread (the loop may be blocked) o seconds into the
        # comfort judgment; the reading is handed to the loop as soon as it is free.
        if not backend.started.wait(timeout=8.0):
            return
        judged.set()
        time.sleep(o)
        arrival["t"] = time.perf_counter()
        loop.call_soon_threadsafe(
            lambda: gas_tasks.append(asyncio.ensure_future(svc.handle_reading(reading(GAS, "on"))))
        )

    try:
        await svc.handle_reading(reading("binary_sensor.living_motion", "off"))
        await svc.handle_reading(reading("light.living_room", "on"))
        tick_s: float | None = None
        tick_res = None
        if ofrac < 0:  # control: same kit, same window, no comfort judgment
            arrival["t"] = time.perf_counter()
            await svc.handle_reading(reading(GAS, "on"))
        else:
            th = threading.Thread(target=inject, daemon=True)
            th.start()
            t_tick = time.perf_counter()
            tick_res = await svc.run_tick()
            tick_s = time.perf_counter() - t_tick
            for _ in range(4000):
                if gas_tasks:
                    break
                if not th.is_alive():  # the thread's callback may not have run yet
                    await asyncio.sleep(0.02)
                    break
                await asyncio.sleep(0.005)
            if gas_tasks:
                await gas_tasks[0]
            th.join(timeout=10.0)
        gas_stamps = [s for s in tr.stamps if s[3].get("severity_label") == "위험"]
        latency = None
        if gas_stamps and "t" in arrival:
            latency = gas_stamps[0][0] - arrival["t"]
        await svc.drain_phase2()
        return {
            "part": "h12", "tag": tag, "seconds": seconds, "ofrac": ofrac, "rep": rep,
            "o_s": o, "latency_s": latency, "tick_s": tick_s,
            "judged": judged.is_set() if ofrac >= 0 else None,
            "tick_proposed": tick_res is not None, "meta": io.run_meta(),
        }
    finally:
        await kit.close()


async def collect_h12(tag: str, n: int, shard: int, nshards: int, raw: Path) -> int:
    path = raw / f"h12_{tag}_s{shard}.jsonl"
    done = io.done_keys(path, ("seconds", "ofrac", "rep"))
    grid = [(s, f) for s in H12_LEVELS for f in (*O_FRACS, -1.0)]
    written = 0
    for rep in range(shard, n, nshards):
        rot = rep % len(grid)
        for s, f in grid[rot:] + grid[:rot]:  # interleave cells
            if (s, f, rep) in done:
                continue
            io.append_jsonl(path, await one_h12(s, f, rep, tag))
            written += 1
    return written


# ── H1.3: slow ntfy send, then a second safety signal ────────────────────────
async def one_h13(scenario: str, delay_s: float, n_guard: int, rep: int) -> dict[str, Any]:
    """scenario 'first_send': smoke arrives while the first (gas) alert is being sent.
    scenario 'ladder': 91 s later the ladder fires (resident re-alert + one send per guardian,
    serial) and smoke arrives right after the ladder started."""
    tr = TimedTransport(delay_s=delay_s)
    kit = await build_service(
        backend=SlowBackend(0.0), categories=CATEGORY_MAP, transport=tr,
        phase2_deadline_s=DEADLINE_S,
    )
    svc = kit.svc
    loop = asyncio.get_running_loop()
    arrival: dict[str, float] = {}
    smoke_tasks: list[asyncio.Future[Any]] = []

    def inject() -> None:
        # TimedTransport only records a stamp after its delay, so the blocking send's arrival
        # cannot be polled; the send starts within a few ms, so 0.1 s later is inside it.
        time.sleep(0.1)
        arrival["t"] = time.perf_counter()
        loop.call_soon_threadsafe(
            lambda: smoke_tasks.append(
                asyncio.ensure_future(svc.handle_reading(reading(SMOKE, "on")))
            )
        )

    try:
        if n_guard:
            rid = await svc.safety.add_resident(kit.home_id, "엄마")
            for k in range(n_guard):
                gid, _topic = await svc.safety.add_guardian(kit.home_id, f"가족{k}")
                await svc.safety.grant_consent(kit.home_id, gid, rid)
        th = threading.Thread(target=inject, daemon=True)
        t_start = time.perf_counter()
        if scenario == "first_send":
            th.start()
            await svc.handle_reading(reading(GAS, "on"))
        else:
            await svc.handle_reading(reading(GAS, "on"))
            kit.clock.set_fixed(kit.t0 + timedelta(seconds=91))
            th.start()
            await svc.ladder_tick()
        n_before = len(tr.stamps)
        for _ in range(60000):
            if smoke_tasks:
                break
            if not th.is_alive():  # the thread's callback may not have run yet
                await asyncio.sleep(0.02)
                break
            await asyncio.sleep(0.005)
        if smoke_tasks:
            await smoke_tasks[0]
        th.join(timeout=15.0)
        sits = await situations(kit.db)
        props = await proposals(kit.db)
        smoke_sids = {s["situation_id"] for s in sits if s["category"] == "smoke"}
        smoke_pids = {p["proposal_id"] for p in props if p["situation_id"] in smoke_sids}
        latency = None
        for st in tr.stamps:
            if st[3].get("proposal_id") in smoke_pids and "t" in arrival:
                latency = st[0] - arrival["t"]
                break
        await svc.drain_phase2()
        return {
            "part": "h13", "scenario": scenario, "delay_s": delay_s, "guardians": n_guard,
            "rep": rep, "latency_s": latency, "pushes": len(tr.stamps),
            "sends_before_smoke_done": n_before,
            "block_s": time.perf_counter() - t_start, "meta": io.run_meta(),
        }
    finally:
        await kit.close()


H13_DELAYS = (0.0, 2.0, 5.0, 10.0)
H13_GUARDS = (0, 1, 3)


def h13_grid(delays: tuple[float, ...]) -> list[tuple[str, float, int]]:
    g: list[tuple[str, float, int]] = [("first_send", d, 0) for d in delays]
    g += [("ladder", d, n) for d in delays for n in H13_GUARDS]
    return g


async def collect_h13(delays: tuple[float, ...], n: int, shard: int, nshards: int,
                      raw: Path) -> int:
    path = raw / f"h13_s{shard}.jsonl"
    done = io.done_keys(path, ("scenario", "delay_s", "guardians", "rep"))
    grid = h13_grid(delays)
    written = 0
    for rep in range(shard, n, nshards):
        rot = rep % len(grid)
        for sc, d, g in grid[rot:] + grid[:rot]:
            if (sc, d, g, rep) in done:
                continue
            io.append_jsonl(path, await one_h13(sc, d, g, rep))
            written += 1
    return written


# ── figures ──────────────────────────────────────────────────────────────────
ARM_LABEL = {
    "liot": "LIOT 표 우선", "wait_for_model": "모델 뒤 발송",
    "short_0.25": "0.25초 대기", "short_0.5": "0.5초 대기", "short_1.0": "1.0초 대기",
}
ARM_COLOR = {
    "liot": plotstyle.policy_color("liot"), "wait_for_model": plotstyle.policy_color("wait_for_model"),
    "short_0.25": "#9DBAD8", "short_0.5": plotstyle.policy_color("short_deadline"),
    "short_1.0": "#3E6FA0",
}


def make_figures(summary: dict[str, Any]) -> list[Path]:
    import matplotlib.pyplot as plt
    import numpy as np

    plotstyle.setup()
    out: list[Path] = []
    rows = analyze.load_rows(RAW_DIR, "main_")
    if rows:
        fig, axes = plt.subplots(1, 4, figsize=(15, 4.2), sharey=True)
        for ax, cond in zip(axes, CONDS, strict=True):
            data, labels, colors = [], [], []
            for arm in ARMS:
                v = [max(r["latency_s"], 1e-4) for r in rows
                     if r["arm"] == arm and r["cond"] == cond and r["latency_s"] is not None]
                if v:
                    data.append(v)
                    labels.append(ARM_LABEL[arm].replace(" ", "\n", 1))
                    colors.append(ARM_COLOR[arm])
            if not data:
                continue
            bp = ax.boxplot(data, patch_artist=True, widths=0.6,
                            flierprops={"marker": ".", "markersize": 3})
            for patch, c in zip(bp["boxes"], colors, strict=True):
                patch.set_facecolor(c)
                patch.set_alpha(0.6)
            ax.set_xticks(range(1, len(labels) + 1), labels, fontsize=7)
            ax.set_yscale("log")
            ax.yaxis.set_major_formatter(plt.FuncFormatter(lambda v, _: f"{v:g}"))
            ax.yaxis.set_minor_formatter(plt.NullFormatter())
            ax.set_title({"normal": "모델 정상 0.2초", "slow": "모델 느림 1.5초",
                          "raising": "모델 예외", "timeout": "마감 초과 4초"}[cond], fontsize=10)
        axes[0].set_ylabel("첫 알림까지 걸린 시간 (초, 로그)")
        fig.suptitle("E1 첫 알림 지연 — 구조별 · 모델 조건별", fontsize=12, y=1.02)
        out.append(plotstyle.save(fig, EXP_DIR, "latency_by_arm_cond"))

    h12 = summary.get("h12", {})
    if h12:
        fig, axes = plt.subplots(1, 3, figsize=(13, 3.9))
        for ax, t in zip(axes, H12_LEVELS, strict=True):
            ax.plot([0, t], [t, 0], color=plotstyle.GREY, linestyle=":", label="예측 T-o")
            for tag, color, marker in (("before", plotstyle.ACCENT, "o"), ("after", "#B24A3E", "s")):
                cells_ = h12.get(tag, {}).get(f"{t}")
                if not cells_:
                    continue
                pts = [(c["o_s"], c["median"], c["median_lo"], c["median_hi"])
                       for c in cells_["by_o"].values()]
                pts.sort()
                xs = [p[0] for p in pts]
                ys = np.array([p[1] for p in pts])
                lo = np.array([p[2] for p in pts])
                hi = np.array([p[3] for p in pts])
                ax.errorbar(xs, ys, yerr=[ys - lo, hi - ys], color=color, marker=marker,
                            capsize=3, label=f"{tag} 중앙값")
            ax.set_xlabel("가스 신호가 판정 시작 뒤 도착한 시각 o (초)")
            ax.set_title(f"편의 판정 T = {t}초")
        axes[0].set_ylabel("가스 첫 알림 지연 (초)")
        axes[0].legend()
        fig.suptitle("H1.2 느린 편의 판정 중 가스 첫 알림 (U1 전 · 후)", fontsize=12, y=1.04)
        out.append(plotstyle.save(fig, EXP_DIR, "h12_comfort_blocks_gas"))

    h13 = summary.get("h13", {})
    if h13:
        fig, ax = plt.subplots(figsize=(6.8, 4.0))
        for cell in h13.values():
            sc = cell["scenario"]
            if sc == "first_send":
                label, color, ls = "첫 송신 중(보호자 무관)", plotstyle.ACCENT, "-"
            else:
                label = f"사다리 발화 중 · 보호자 {cell['guardians']}명"
                color = {0: "#8C8C8C", 1: "#C48A2C", 3: "#B24A3E"}[cell["guardians"]]
                ls = "--"
            cell["_label"] = (label, color, ls)
        series: dict[str, list[tuple[float, float]]] = {}
        meta: dict[str, tuple[str, str, str]] = {}
        for cell in h13.values():
            lab, col, ls = cell["_label"]
            series.setdefault(lab, []).append((cell["delay_s"], cell["median"]))
            meta[lab] = (lab, col, ls)
        for lab, pts in series.items():
            pts.sort()
            _, col, ls = meta[lab]
            ax.plot([p[0] for p in pts], [p[1] for p in pts], color=col, linestyle=ls,
                    marker="o", label=lab)
        ax.set_xlabel("ntfy 송신 지연 (초)")
        ax.set_ylabel("두 번째 안전 신호의 첫 알림 지연 중앙값 (초)")
        ax.set_title("H1.3 송신이 느리면 다음 안전 알림도 늦다")
        ax.legend(fontsize=7)
        out.append(plotstyle.save(fig, EXP_DIR, "h13_send_delay"))

    rep = summary.get("replay", {})
    if rep:
        fig, ax = plt.subplots(figsize=(7, 3.8))
        names = sorted(rep["by_model"])
        vals = [rep["by_model"][m]["mismatch"]["rate"] for m in names]
        lo = [rep["by_model"][m]["mismatch"]["lo"] for m in names]
        hi = [rep["by_model"][m]["mismatch"]["hi"] for m in names]
        v = np.array(vals)
        ax.bar(names, v, color=plotstyle.GREY_LIGHT, edgecolor=plotstyle.GREY,
               yerr=[v - np.array(lo), np.array(hi) - v], capsize=3)
        ax.set_ylabel("2단계 불일치율")
        ax.set_title("H1.1c 벤치 원시 답을 재생했을 때 첫 경보가 바뀐 비율")
        plt.setp(ax.get_xticklabels(), rotation=25, ha="right", fontsize=8)
        out.append(plotstyle.save(fig, EXP_DIR, "h11c_replay_mismatch"))
    return out


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
    p.add_argument("--part", choices=["main", "replay", "h12", "h13", "c27", "all"], default="all")
    p.add_argument("--tag", default="before", help="H1.2 tag: before|after")
    p.add_argument("--shard", default="0/1", help="i/N sharding over incidents / reps")
    p.add_argument("--n", type=int, default=None, help="override sample size")
    p.add_argument("--quick", action="store_true", help="tiny smoke run into raw_quick/")
    p.add_argument("--analyze", action="store_true")
    args = p.parse_args()
    if args.analyze:
        summary = analyze.summarize(RAW_DIR)
        print(f"summary: {io.write_summary(EXP_DIR, summary)}")
        for f in make_figures(summary):
            print(f"figure: {f}")
        return
    raw = EXP_DIR / "raw_quick" if args.quick else RAW_DIR
    shard, nshards = (int(x) for x in args.shard.split("/"))

    async def go() -> None:
        if args.part in ("main", "all"):
            n = args.n or (3 if args.quick else N_INCIDENTS)
            print("main wrote", await collect_main(shard, nshards, n, raw))
        if args.part in ("c27", "all"):
            n = args.n or (6 if args.quick else 600)
            print("c27 wrote", await collect_c27(shard, nshards, n, raw))
        if args.part in ("replay", "all"):
            print("replay wrote", await collect_replay(raw, 8 if args.quick else None))
        if args.part in ("h12", "all"):
            n = args.n or (1 if args.quick else 40)
            print("h12 wrote", await collect_h12(args.tag, n, shard, nshards, raw))
        if args.part in ("h13", "all"):
            n = args.n or (1 if args.quick else 30)
            delays = (0.0, 0.5) if args.quick else H13_DELAYS
            print("h13 wrote", await collect_h13(delays, n, shard, nshards, raw))

    asyncio.run(go())


if __name__ == "__main__":
    main()
