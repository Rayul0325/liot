"""Reduce one real-model DaySim summary (research mode) to derived numbers (no raw model text).

H5.4 needs, per judged step, the Gate1 floor and the top tier of Jev and of luna ("raw answers").
Those three labels are derived values, not model text, so they may be committed.
"""

from __future__ import annotations

from typing import Any

TIERS = ("ambient", "breath", "dusk", "ember")
SEVERITIES = ("record", "notice", "warning", "critical")


def _tier_name(raw: Any) -> str | None:
    """'0'..'3' / 0..3 / 'breath' -> lowercase tier name (None when it is not a tier)."""
    s = str(raw).strip().lower()
    if s.isdigit() and int(s) < len(TIERS):
        return TIERS[int(s)]
    return s if s in TIERS else None


def _top(probs: dict[str, float] | None) -> str | None:
    return None if not probs else max(probs, key=lambda k: probs[k])


def derive(summary: dict[str, Any], seed: int, days: int) -> dict[str, Any]:
    models = summary["models"]
    usage = models["usage"]
    by_kind = {k: {"calls": v["calls"], "seconds": v["seconds"], "usd": v["usd"],
                   "avg_s": (v["seconds"] / v["calls"]) if v["calls"] else None}
               for k, v in sorted(usage["by_kind"].items())}
    pairs: list[list[Any]] = []  # [floor, jev_top, luna_top] for the tier question
    sev_pairs: list[list[Any]] = []
    jev_floor: list[list[Any]] = []  # [floor, jev_top] for every judged step, luna or not
    unavailable = 0
    judge_models: dict[str, int] = {}
    for r in summary["luna"]:
        judge_models[r["judge"]] = judge_models.get(r["judge"], 0) + 1
        jev_floor.append([_tier_name(r.get("floor")), _tier_name(_top(r["jev"].get("tier")))])
        if r["status"] != "ok":
            unavailable += 1
            continue
        jev_t, luna_t = _tier_name(_top(r["jev"].get("tier"))), _tier_name(_top(r["luna"].get("tier")))
        if jev_t is not None and luna_t is not None:
            pairs.append([_tier_name(r.get("floor")), jev_t, luna_t])
        if "severity" in r["jev"] and "severity" in r["luna"]:
            sev_pairs.append([str(_top(r["jev"]["severity"])), str(_top(r["luna"]["severity"]))])
    props = summary["proposals"]
    reasons = {"n": len(props), "model": 0, "fallback": 0, "causes": {}}
    resident: dict[str, Any] = {"n_decisions": 0, "model": 0, "persona_table": 0, "causes": {}}
    for p in props:
        if p.get("reason_source") == "model":
            reasons["model"] += 1
        elif "reason_fallback" in p:
            reasons["fallback"] += 1
            reasons["causes"][p["reason_fallback"]] = reasons["causes"].get(p["reason_fallback"], 0) + 1
    for p in props:  # the cloud resident decider's source per decision (phone push or digest tap)
        who = p.get("decided_by")
        if who:
            resident["n_decisions"] += 1
            resident[who] = resident.get(who, 0) + 1
            if "reaction_fallback" in p:
                resident["causes"][p["reaction_fallback"]] = (
                    resident["causes"].get(p["reaction_fallback"], 0) + 1)
    firsts = [s for s in summary["safety"] if s["kind"] == "first_alert"]
    return {
        "seed": seed, "days": days,
        "status": [{k: st[k] for k in ("part", "state", "model")} for st in models["status"]],
        "calls": usage["calls"], "usd": usage["run_usd"], "tripped": usage["tripped"],
        "unknown_price_calls": usage["unknown_price_calls"],
        "estimated_calls": usage["estimated_calls"], "by_kind": by_kind,
        "per_day": [{k: d[k] for k in ("day", "calls", "avg_seconds", "usd")} for d in models["days"]],
        "luna_results": len(summary["luna"]), "luna_unavailable": unavailable,
        "judge_models": judge_models,
        "tier_pairs": pairs, "severity_pairs": sev_pairs, "jev_floor": jev_floor,
        "reasons": reasons, "resident": resident,
        "first_alerts": len(firsts),
        "first_alerts_immediate": sum(1 for s in firsts if s["decision"] == "IMMEDIATE" and s["sent"]),
        "first_alert_ms": [s.get("first_alert_ms") for s in firsts],
        "comfort_proposals": len(props),
        "pushes": sum(m["pushes"] for m in summary["metrics"]),
        "accepted": sum(m["accepted"] for m in summary["metrics"]),
        "dismissed": sum(m["dismissed"] for m in summary["metrics"]),
    }
