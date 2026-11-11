"""H1.5 (= P1): who should page the family — the table floor (LIOT) or the final value?

A decision-level simulation that uses the PRODUCTION decision objects:
  * the floor is ``SeverityTable.compute(category, vulnerable_resident=registered)`` (real table),
  * the final value is ``escalate_severity(floor, model_answer)`` (real composer),
  * the paging predicate is ``escalation.guardian_path(sev)`` (real predicate), applied to
    the floor (LIOT) or to the final value (alternative).
``validate_liot_arm`` additionally runs the real service on random homes and checks that actual
guardian forwards happen exactly when the production predicate on the table floor says so, so the
simulated LIOT arm is the real thing.

Hidden truth (independent of the table; errors only from the two registered causes):
  * sensor false alarm (gas/smoke): with prob ``fa`` the alarm has no real danger (truth 0),
  * unregistered vulnerability: a share ``unreg`` of all homes hold a vulnerable resident nobody
    registered; in any truly vulnerable home every warning-category alarm is truly CRITICAL while
    the table (not knowing) says WARNING. Registered-vulnerable homes (share ``V_REG``) are
    handled correctly by the table.
Model: with prob ``attack`` it answers CRITICAL regardless; otherwise with prob ``honesty`` it
answers the true severity, else a uniformly random other level. ``blind=1``: the model never
sees any vulnerability (hard gate 3), so its honest answer is the no-vulnerability truth.
Common random numbers: every cell of a seed reuses the same uniforms, so arms and cells differ
only through their thresholds.
"""

from __future__ import annotations

from typing import Any

import numpy as np

from liot.core.severity import SeverityLevel, escalate_severity, load_severity_table
from liot.layers.l4_execution.escalation import guardian_path

V_REG = 0.10      # assumption: share of homes with a REGISTERED vulnerable resident
MIX_BASE = {"gas": .15, "smoke": .15, "door": .25, "leak": .15, "oven": .10,
            "high_temp": .05, "lock": .10, "water_main": .05}
MIXES = {
    "base": MIX_BASE,
    "alarm_low": {"gas": .05, "smoke": .05, "door": .30, "leak": .20, "oven": .12,
                  "high_temp": .08, "lock": .12, "water_main": .08},
    "alarm_high": {"gas": .30, "smoke": .30, "door": .15, "leak": .08, "oven": .07,
                   "high_temp": .03, "lock": .05, "water_main": .02},
}
CATS = tuple(MIX_BASE)
GAS_LIKE = ("gas", "smoke")
FA_LEVELS = (0.0, 0.01, 0.02, 0.05)
UNREG_LEVELS = (0.0, 0.10, 0.30)
HONESTY = (0.5, 0.6, 0.7, 0.8, 0.9, 1.0)
ATTACK = (0.0, 0.1, 0.2, 0.3)
INFER_TPR = (0.5, 0.8, 0.95)
INFER_FPR = (0.0, 0.05, 0.2)
N_EVENTS = 20000
N_SEEDS = 10
CRIT = int(SeverityLevel.CRITICAL)


def _floor_lookup() -> np.ndarray:
    """floor[cat_index, registered_vulnerable(0/1)] from the production severity table."""
    table = load_severity_table()
    out = np.zeros((len(CATS), 2), dtype=np.int64)
    for i, cat in enumerate(CATS):
        for v in (0, 1):
            res = table.compute(cat, vulnerable_resident=bool(v))
            assert res is not None
            out[i, v] = int(res.floor)
    return out


FLOOR = _floor_lookup()


def _arm_counts(page: np.ndarray, crit: np.ndarray) -> list[int]:
    return [int((page & ~crit).sum()), int((~page & crit).sum()), int(page.sum())]


def run_cell(
    seed: int, *, fa: float, unreg: float, honesty: float, attack: float, blind: int,
    mix: str = "base", n: int = N_EVENTS,
) -> dict[str, Any]:
    """Counts [false_pages, misses, pages] per arm for one (cell, seed)."""
    rng = np.random.default_rng(seed)
    u_cat, u_fa, u_home, u_att, u_h, u_w, u_inf = (rng.random(n) for _ in range(7))
    probs = np.array([MIXES[mix][c] for c in CATS])
    cat = np.searchsorted(np.cumsum(probs / probs.sum()), u_cat, side="right").clip(0, len(CATS) - 1)
    gas_like = np.isin(cat, [CATS.index(c) for c in GAS_LIKE])
    registered = u_home < V_REG
    hidden = (u_home >= V_REG) & (u_home < V_REG + unreg)
    truly_vuln = registered | hidden
    # hidden truth
    truth = np.where(gas_like, np.where(u_fa < fa, 0, CRIT), np.where(truly_vuln, CRIT, 2))
    crit = truth == CRIT
    # production floor with the REGISTERED vulnerability only
    floor = FLOOR[cat, registered.astype(np.int64)]
    # model answer
    observed = truth if not blind else np.where(
        gas_like, truth, 2)  # blind: no vulnerability information at all
    wrong_idx = np.floor(u_w * 3).astype(np.int64)
    others = np.stack([(observed + 1 + k) % 4 for k in range(3)], axis=1)
    wrong = others[np.arange(n), wrong_idx]
    answer = np.where(u_h < honesty, observed, wrong)
    answer = np.where(u_att < attack, CRIT, answer)
    final = np.maximum(floor, answer)  # == escalate_severity elementwise (checked in validate)
    arms: dict[str, list[int]] = {
        "liot": _arm_counts(floor == CRIT, crit),
        "final": _arm_counts(final == CRIT, crit),
    }
    for tpr in INFER_TPR:
        for fpr in INFER_FPR:
            inferred = (hidden & (u_inf < tpr)) | (~truly_vuln & (u_inf < fpr))
            f_inf = FLOOR[cat, (registered | inferred).astype(np.int64)]
            arms[f"infer_{tpr}_{fpr}"] = _arm_counts(f_inf == CRIT, crit)
    return {
        "n_events": n, "n_critical": int(crit.sum()), "arms": arms,
        "share_hidden_home": float(hidden.mean()),
    }


def check_composer_equivalence() -> int:
    """The vectorised ``np.maximum`` equals production ``escalate_severity`` on all 16 pairs."""
    bad = 0
    for f in range(4):
        for m in range(4):
            if int(escalate_severity(SeverityLevel(f), SeverityLevel(m))) != max(f, m):
                bad += 1
    for lv in range(4):
        if guardian_path(SeverityLevel(lv)) != (lv == CRIT):
            bad += 1
    return bad
