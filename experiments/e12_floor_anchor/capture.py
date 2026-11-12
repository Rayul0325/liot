"""E12 capture: re-run the E9 day simulator (real Jev, Haiku, memgineering; NO luna, NO OpenAI) and keep
the per-step evidence string that E9's trace did not store.

Zero production changes: a DaySim subclass snapshots the shadow queue (evidence, Jev probabilities) that the
simulator already builds for the luna comparator, right before the (disabled) luna stage drains it.  The luna
stage is replaced by a LunaShadow in mode "unavailable", which never makes a request, so no OpenAI call exists.

Raw evidence + Jev probabilities go ONLY to raw/cloud_steps.jsonl (git-ignored: evidence is outbound scene
text).  Derived labels (floor, category, Jev's top tier) go to raw/steps.jsonl and the safety/cost numbers of
each run go to raw/runs.jsonl (both committed).
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import replace
from pathlib import Path
from typing import Any

from experiments.common import io
from liot.day_sim import DaySim

EXP_DIR = Path(__file__).resolve().parent
RAW = EXP_DIR / "raw"
STEPS = RAW / "steps.jsonl"
CLOUD_STEPS = RAW / "cloud_steps.jsonl"
RUNS = RAW / "runs.jsonl"
USAGE_PATH = RAW / "cloud_usage_capture.json"  # private day book, git-ignored
SEEDS = tuple(range(1, 11))
DAYS = 3
TIERS = ("ambient", "breath", "dusk", "ember")


def top_tier(probs: dict[str, float] | None) -> str | None:
    return None if not probs else max(probs, key=lambda k: probs[k])


class CaptureDaySim(DaySim):
    """DaySim that also records (day, hhmm, evidence, Jev probs, entity) for every judged step."""

    captured: list[dict[str, Any]]

    async def _luna_shadows(self, kit, step, out) -> None:  # type: ignore[no-untyped-def]
        if not hasattr(self, "captured"):
            self.captured = []
        for evidence, probs, model_id, served, entity_id in list(self._shadow_due):
            self.captured.append({
                "day": step.day, "hhmm": step.at_local.strftime("%H:%M"), "evidence": evidence,
                "jev_probs": probs, "judge": model_id, "served_model": served, "entity_id": entity_id,
            })
        await super()._luna_shadows(kit, step, out)


def one_run(seed: int, days: int, remaining_usd: float) -> dict[str, Any]:
    from liot.day_cloud import build_cloud_kit
    from liot.luna_shadow import LunaConfig, LunaShadow
    from liot.secrets_env import load_secrets

    kit = build_cloud_kit(load_secrets(), usage_path=USAGE_PATH)
    kit.luna = LunaShadow(LunaConfig(mode="unavailable", model="none"))  # never sends; no OpenAI at all
    assert kit.luna.cfg.mode == "unavailable"
    kit.meter.limits = replace(kit.meter.limits,
                               cost_cap_per_run_usd=min(kit.meter.limits.cost_cap_per_run_usd, remaining_usd),
                               cost_cap_per_day_usd=1000.0)
    sim = CaptureDaySim(seed=seed, days=days, models="cloud", cloud=kit, trace=True, research=True,
                        house_id=f"e12-s{seed}")
    asyncio.run(sim.run(lambda stage, data: None))
    summary = sim.summary()
    usage = summary["models"]["usage"]
    firsts = [s for s in summary["safety"] if s["kind"] == "first_alert"]
    steps: list[dict[str, Any]] = []
    counters: dict[tuple[int, str], int] = {}
    for c in getattr(sim, "captured", []):
        n = counters.get((c["day"], c["hhmm"]), 0)
        counters[(c["day"], c["hhmm"])] = n + 1
        ev = json.loads(c["evidence"])
        cand = ev.get("candidate", {})
        steps.append({
            "step_id": f"s{seed}d{c['day']}-{c['hhmm']}-{n}", "seed": seed, "day": c["day"], "hhmm": c["hhmm"],
            "floor": ev.get("gate1_floor"), "category": cand.get("category"), "capability": cand.get("capability"),
            "action": cand.get("action"), "time_bucket": ev.get("time_bucket"),
            "capture_jev_top": top_tier(c["jev_probs"].get("tier")), "served_model": c["served_model"],
            "_evidence": c["evidence"], "_jev_probs": c["jev_probs"],
        })
    return {
        "seed": seed, "days": days, "steps": steps,
        "run": {
            "seed": seed, "days": days, "calls": usage["calls"], "usd": usage["run_usd"],
            "tripped": usage["tripped"], "unknown_price_calls": usage["unknown_price_calls"],
            "by_kind": {k: {"calls": v["calls"], "usd": v["usd"]} for k, v in usage["by_kind"].items()},
            "judged_steps": len(steps), "first_alerts": len(firsts),
            "first_alerts_immediate": sum(1 for s in firsts if s["decision"] == "IMMEDIATE" and s["sent"]),
            "first_alert_ms": [s.get("first_alert_ms") for s in firsts],
            "status": [{k: st[k] for k in ("part", "state", "model")} for st in summary["models"]["status"]],
            "luna_requests_logged": sum(1 for kind, what in sim.log if kind == "ext" and what == "luna"),
        },
    }


def collect(seeds: tuple[int, ...], days: int, runs: Path, steps: Path, cloud_steps: Path,
            budget_usd: float) -> int:
    done = io.done_keys(runs, ("seed",))
    spent = sum(r["usd"] for r in io.read_jsonl(runs))
    written = 0
    for seed in seeds:
        if (seed,) in done:
            continue
        biggest = max((r["usd"] for r in io.read_jsonl(runs)), default=0.0)
        if spent + biggest >= budget_usd:
            print(f"budget stop before seed {seed}: spent={spent:.4f} cap={budget_usd}", flush=True)
            break
        res = one_run(seed, days, budget_usd - spent)
        meta = io.run_meta()
        for s in res["steps"]:
            io.append_jsonl(cloud_steps, {"step_id": s["step_id"], "evidence": s["_evidence"],
                                          "jev_probs": s["_jev_probs"]})
            io.append_jsonl(steps, {k: v for k, v in s.items() if not k.startswith("_")})
        run = res["run"]
        run["meta"] = meta
        io.append_jsonl(runs, run)
        spent += run["usd"]
        written += 1
        print(f"seed {seed}: judged={run['judged_steps']} calls={run['calls']} usd={run['usd']:.4f} "
              f"first_alerts={run['first_alerts_immediate']}/{run['first_alerts']} "
              f"luna_logged={run['luna_requests_logged']} tripped={run['tripped']}", flush=True)
        if run["first_alerts_immediate"] != run["first_alerts"] or run["first_alerts"] != days:
            print("SAFETY CHECK FAILED - stopping", flush=True)
            return -written
    return written
