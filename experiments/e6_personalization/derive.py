"""Turn one DaySim summary (research mode) into the per-day arrays stored in raw/.

The resident's hidden preference decides what "helpful" means: a scene is helpful on a day when
``resident.helpful(scene, day)``. Nothing here is visible to the system under test.
"""

from __future__ import annotations

from typing import Any

from liot.day_sim import StochasticResident

# A helpful scene that goes unserved for one of these reasons was silenced because of what the
# system learned or decided about preference. Cooldown, budget, tier ceiling and the like are
# baseline throttles that every arm shares, so they are reported apart.
PREFERENCE_BLOCKS = ("rejected", "evaluator_noop", "SUPPRESS")
PHONE_SCENES = ("ventilate", "purifier")
PREF_DAYS = (1, 7, 14, 15, 21, 30, 45, 60)


def _per_day(days: int) -> list[int]:
    return [0] * days


def derive(summary: dict[str, Any], resident: StochasticResident, new_scenes: tuple[str, ...],
           days: int) -> dict[str, Any]:
    keys = ("pushes_imm", "acc_help_imm", "acc_help_tray", "acc_unhelp", "dis", "exp_imm", "exp_dig",
            "offered_help_imm", "offered_unhelp_imm", "new_offer_imm", "new_acc", "ws_help",
            "ws_help_phone", "ws_new", "opp_help", "opp_help_phone", "digest_made")
    out: dict[str, list[int]] = {k: _per_day(days) for k in keys}
    items: list[list[Any]] = []
    delivered: set[tuple[int, str]] = set()
    for p in summary["proposals"]:
        d, scene, dec, state = p["day"], p["scene"], p["decision"], p["state"]
        i = d - 1
        helpful = resident.helpful(scene, d)
        is_new = scene in new_scenes and helpful
        if dec in ("IMMEDIATE", "DIGEST"):
            delivered.add((d, str(scene)))
        if dec == "IMMEDIATE":
            out["pushes_imm"][i] += 1
            out["offered_help_imm" if helpful else "offered_unhelp_imm"][i] += 1
            if is_new:
                out["new_offer_imm"][i] += 1
            items.append([d, p["hhmm"], scene, state[0]])  # a/d/e/p = accepted/dismissed/expired/pending
        elif dec == "DIGEST":
            out["digest_made"][i] += 1
        if state == "accepted":
            if not helpful:
                out["acc_unhelp"][i] += 1
            elif dec == "IMMEDIATE":
                out["acc_help_imm"][i] += 1
            else:
                out["acc_help_tray"][i] += 1
            if is_new:
                out["new_acc"][i] += 1
        elif state == "dismissed":
            out["dis"][i] += 1
        elif state == "expired":
            out["exp_imm" if dec == "IMMEDIATE" else "exp_dig"][i] += 1
    # opportunities: (day, scene) -> set of block reasons seen while the rule matched
    seen: dict[tuple[int, str], set[str]] = {}
    for o in summary["research"]["opportunities"]:
        d = o["day"]
        for scene in o["matched"]:
            seen.setdefault((d, scene), set())
        if o["silence"] is not None:
            for scene in o["matched"]:
                seen[(d, scene)].add(o["silence"])
        if o["winner"] is not None and o["decision"] == "SUPPRESS":
            seen[(d, o["winner"])].add("SUPPRESS")
        for sk in o["skipped"]:
            seen.setdefault((d, sk["scene"]), set()).add(sk["reason"])
    other: dict[str, int] = {}
    for (d, scene), reasons in sorted(seen.items(), key=lambda kv: (kv[0][0], str(kv[0][1]))):
        if not resident.helpful(scene, d):
            continue
        i = d - 1
        phone = scene in PHONE_SCENES
        out["opp_help"][i] += 1
        out["opp_help_phone"][i] += int(phone)
        if (d, scene) in delivered:
            continue
        if reasons & set(PREFERENCE_BLOCKS):
            out["ws_help"][i] += 1
            out["ws_help_phone"][i] += int(phone)
            if scene in new_scenes:
                out["ws_new"][i] += 1
        else:
            for r in reasons:
                other[r] = other.get(r, 0) + 1
    firsts = [s for s in summary["safety"] if s["kind"] == "first_alert"]
    cells = {str(rec["day"]): rec["cells"] for rec in summary["research"]["pref_cells"]
             if rec["day"] in PREF_DAYS}
    return {
        **out,
        "items": items,
        "unserved_other": other,
        "first_alerts": len(firsts),
        "safety_ok": len(firsts) == days and all(s["decision"] == "IMMEDIATE" and s["sent"]
                                                 for s in firsts),
        "first_alert_ms_max": max((s["first_alert_ms"] for s in firsts
                                   if s.get("first_alert_ms") is not None), default=None),
        "pref_cells": cells,
        "explore_days": summary["research"]["explore_days"],
        "n_proposals": len(summary["proposals"]),
    }
