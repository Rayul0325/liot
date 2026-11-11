"""C2.8: guardian paging by table floor vs final value when the table's miss is observable.

Cycle-1 H1.5 had two causes of a wrong table (sensor false alarms, unregistered vulnerability).
C2.8 adds a THIRD cause the model can see without any resident trait: a warning-category alarm
that is truly CRITICAL because of trait-free structured facts (how long the anomaly lasts, whether
someone is home, ...). Share ``obs`` of warning-category events in plain homes are like this.

The model is a binormal signal detector with a fixed operating point: it sees a 'critical-looking'
signal for observable-escalation events (and, in ``honest`` mode, for hidden-vulnerable homes;
in ``blind`` mode it never sees vulnerability, hard gate 3), scores ~ N(mu * signal, 1) with
AUC = Phi(mu / sqrt 2), and raises to CRITICAL when the score passes the threshold that gives a
false-raise rate ``fpr`` on plain events. ``attack`` = share of events where it always answers
CRITICAL. The E5 operating point (operational raises 0-2 of 61 questions) is the cell AUC = 0.5,
fpr = 0.03: the model carries no information and raises about as often as it is wrong.

Arms: ``liot`` (page iff table floor == CRITICAL) and ``final`` (floor or model raise), using the
production table (``SeverityTable.compute``) and predicate (``guardian_path``) exactly as h15.py.
Common random numbers per seed. Structural by construction: unreg = 0 with obs = 0 cells (the table
cannot be wrong except for sensor false alarms) and ``blind`` mode for the hidden-vulnerability part.
"""

from __future__ import annotations

from typing import Any

import numpy as np
from scipy.stats import norm

from experiments.e2_invariants.h15 import CATS, FLOOR, GAS_LIKE, MIX_BASE, V_REG

FA = 0.02
UNREG = (0.0, 0.10, 0.30)
OBS = (0.0, 0.05, 0.10)
AUC = (0.5, 0.6, 0.7, 0.8, 0.9, 0.95, 1.0)
ATTACK = (0.0, 0.1, 0.3)
FPR = 0.05
OPERATIONAL_FPR = 0.03   # E5: 0-2 raises in 61 questions
N_EVENTS = 20000
N_SEEDS = 10
CRIT = 3


def tpr_of(auc: float, fpr: float) -> float:
    """True-raise rate of the binormal detector with the given AUC at the given false-raise rate."""
    if auc >= 1.0:
        return 1.0
    mu = np.sqrt(2.0) * norm.ppf(auc)
    return float(norm.cdf(mu - norm.ppf(1.0 - fpr)))


def run_cell(seed: int, *, unreg: float, obs: float, auc: float, attack: float, mode: str,
             fpr: float = FPR, n: int = N_EVENTS) -> dict[str, Any]:
    rng = np.random.default_rng(seed)
    u_cat, u_fa, u_home, u_obs, u_att, u_score = (rng.random(n) for _ in range(6))
    probs = np.array([MIX_BASE[c] for c in CATS])
    cat = np.searchsorted(np.cumsum(probs / probs.sum()), u_cat, side="right").clip(0, len(CATS) - 1)
    gas_like = np.isin(cat, [CATS.index(c) for c in GAS_LIKE])
    registered = u_home < V_REG
    hidden = (u_home >= V_REG) & (u_home < V_REG + unreg)
    truly_vuln = registered | hidden
    obs_crit = ~gas_like & ~truly_vuln & (u_obs < obs)          # the third cause
    truth_crit = np.where(gas_like, u_fa >= FA, truly_vuln | obs_crit)
    floor = FLOOR[cat, registered.astype(np.int64)]
    # model: sees observable escalation always; hidden vulnerability only in 'honest' mode
    signal = obs_crit | (hidden & ~gas_like & (mode == "honest"))
    mu = np.sqrt(2.0) * norm.ppf(min(auc, 1 - 1e-9)) if auc < 1.0 else 12.0
    z = norm.ppf(u_score.clip(1e-12, 1 - 1e-12))
    score = z + mu * signal
    raised = (score > norm.ppf(1.0 - fpr)) | (u_att < attack)
    final_crit = (floor == CRIT) | raised
    liot_crit = floor == CRIT

    def counts(page: np.ndarray) -> list[int]:
        return [int((page & ~truth_crit).sum()), int((~page & truth_crit).sum()), int(page.sum())]

    return {
        "n_events": n, "n_critical": int(truth_crit.sum()),
        "arms": {"liot": counts(liot_crit), "final": counts(final_crit)},
        "miss_by_cause_liot": {
            "unregistered_vulnerability": int((~liot_crit & hidden & ~gas_like).sum()),
            "observable_escalation": int((~liot_crit & obs_crit).sum()),
        },
        "model_tpr_realised": float(raised[signal].mean()) if signal.any() else None,
        "model_fpr_realised": float(raised[~truth_crit & (floor < CRIT)].mean()),
    }
