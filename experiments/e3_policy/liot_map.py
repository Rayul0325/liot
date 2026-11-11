"""Production tier / confirmation lookup tables, built by calling the REAL LIOT code.

For every (template, time bucket, occupancy):
  tier0     = escalate_only(Gate1 floor, catalog risk)            (guardrail + device catalog)
  CONF_B/A  = RiskRouter(Tiered/Uniform intervention).route(...).required_confirmation, for each
              possible final tier (needed to apply misclassification) — condition B and A.
Nothing here re-implements a production rule; the tables only cache production answers.
Levels: 0 none, 1 light, 2 explicit, 3 double.
"""

from __future__ import annotations

from functools import lru_cache

import numpy as np

from liot.core.contracts import CandidateAction, ContextSnapshot, Judgment
from liot.core.risk import RiskTier, escalate_only
from liot.layers.l2_context.proactive import PROACTIVE_TIER_CEILING
from liot.layers.l3_judgment.guardrail import Guardrail
from liot.layers.l4_execution.device_catalog import load_device_catalog
from liot.layers.l4_execution.executor import FORBIDDEN_OPEN
from liot.layers.l4_execution.intervention import (
    CONFIRMATION_ORDER,
    TieredIntervention,
    UniformIntervention,
)
from liot.layers.l4_execution.risk_router import RiskRouter

from .generator import N_TMPL, TEMPLATES

TB_NAMES = ("morning", "day", "evening", "night")
OCC_NAMES = ("occupied", "vacant")


def _candidate(i: int) -> CandidateAction:
    _, _, cap, act, par, ent, cat = TEMPLATES[i]
    return CandidateAction(capability=cap, action=act, params=par, entity_id=ent, category=cat)


def _judgment(cand: CandidateAction, tier: RiskTier) -> Judgment:
    return Judgment(
        judgment_id="j", trace_id="t", home_id="singleton", snapshot_hash="h", action=cand,
        risk_tier=tier, confidence=1.0, rationale="e3", gate1_verdict="gate1:allow e3",
        bundle_id="b", model_id="none",
    )


@lru_cache(maxsize=1)
def tables() -> dict[str, np.ndarray]:
    guard = Guardrail()
    catalog = load_device_catalog()
    routers = {
        "B": RiskRouter(catalog=None, intervention=TieredIntervention()),
        "A": RiskRouter(catalog=None, intervention=UniformIntervention()),
    }
    tier0 = np.zeros((N_TMPL, 4, 2), dtype=np.int8)
    conf = {c: np.zeros((N_TMPL, 4, 2, 4), dtype=np.int8) for c in "AB"}
    for i in range(N_TMPL):
        cand = _candidate(i)
        cat_tier = catalog.risk_for(cand.capability, cand.action, cand.params)
        for tb in range(4):
            for oc in range(2):
                ctx = ContextSnapshot(
                    snapshot_id="s", trace_id="t",
                    trigger={}, structured={"time_bucket": TB_NAMES[tb], "occupancy": OCC_NAMES[oc]},
                    nl_summary="e3", summary_template_sha="x", created_at="2026-01-01T00:00:00+00:00",
                )
                floor = guard.evaluate(cand, ctx).floor_tier
                tier0[i, tb, oc] = int(escalate_only(floor, cat_tier))
                for t in range(4):
                    for c in "AB":
                        p = routers[c].route(_judgment(cand, RiskTier(t)), created_at="2026-01-01T00:00:00+00:00")
                        conf[c][i, tb, oc, t] = CONFIRMATION_ORDER[p.required_confirmation]
    open_forbidden = np.array(
        [(TEMPLATES[i][2], TEMPLATES[i][3]) in FORBIDDEN_OPEN for i in range(N_TMPL)]
    )
    return {
        "tier0": tier0,
        "conf_A": conf["A"],
        "conf_B": conf["B"],
        "open_forbidden": open_forbidden,
        "ceiling": np.int8(int(PROACTIVE_TIER_CEILING)),
    }


def describe() -> list[dict[str, object]]:
    """Per-template production classification (for RESULTS.md): tier by time bucket (occupied)."""
    t = tables()
    rows = []
    for i in range(N_TMPL):
        rows.append({
            "template": TEMPLATES[i][0],
            "tier_by_bucket(morning,day,evening,night)": [RiskTier(int(t["tier0"][i, b, 0])).name for b in range(4)],
            "conf_B_at_tier0_day": int(t["conf_B"][i, 1, 0, t["tier0"][i, 1, 0]]),
            "conf_A_at_tier0_day": int(t["conf_A"][i, 1, 0, t["tier0"][i, 1, 0]]),
            "open_forbidden": bool(t["open_forbidden"][i]),
        })
    return rows
