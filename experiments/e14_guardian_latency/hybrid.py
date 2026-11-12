"""C4.9 (b): a hybrid guardian-paging criterion next to the table floor (LIOT) and the final value.

GATE-4-BREAKING EXPLORATORY VARIANT. Production pages the family only when the table floor is
CRITICAL (``escalation.guardian_path(floor)``) and a model-only raise never reaches family
(hard gate 4). The hybrid below pages the family on a model-only raise (after the resident did
not confirm in time), so it cannot exist without breaking that rule. It is modelled as the
counterfactual policy it is, exactly like the trait-inference arm of H1.5, and lives in
``experiments/`` only. A result that favours it is not a recommendation to change gate 4.

Generator: the h15b (C2.8) event generator, same draws (``u_cat, u_fa, u_home, u_obs, u_att,
u_score`` from ``default_rng(seed)``), same table floors (``FLOOR`` from the production
``SeverityTable``) and the same binormal model. ``check_equivalence`` re-derives the h15b
``liot`` / ``final`` counts from this module with the resident-ack model switched off.

ONE shared resident-ack model for ALL arms (metric review): a resident answers an alert with
probability ``p`` (vulnerable homes 0.2/0.5/0.8, plain homes 0.9; swept) and the answer time is
Exp(mean 60 s) after the alert. The same two uniforms per event feed every arm, so arms differ
only through their paging rule. In production a resident ack before the 90 s forward stops the
family page (``business-rules.md`` ladder), so every arm pages at 90 s unless acked before that:

  liot      page iff floor == CRITICAL; family page at 90 s unless acked by 90 s
  final     page iff floor == CRITICAL or model raise; family page at 90 s unless acked by 90 s
  hyb<W>    floor == CRITICAL: as liot. Model-only raise: the resident is re-alerted at once,
            the family is paged at W seconds unless the resident acked within W seconds
            (W = 90 / 180 / 300; 180 is the primary window, fixed before the run)

Metrics per arm: false pages (page without true danger), misses (true danger, no page), pages,
and time-to-family-page (over true-danger events that were paged; misses are censored, so the
count of paged ones is stored next to the sum of times).
"""

from __future__ import annotations

from typing import Any

import numpy as np
from scipy.stats import norm

from experiments.e2_invariants import h15b
from experiments.e2_invariants.h15 import CATS, FLOOR, GAS_LIKE, MIX_BASE, V_REG

PLAIN_ACK = 0.9
VULN_ACK = (0.2, 0.5, 0.8)
ACK_MEAN_S = 60.0
FORWARD_S = 90.0
WINDOWS = (90.0, 180.0, 300.0)
PRIMARY_WINDOW = 180.0
UNREG = h15b.UNREG
OBS = h15b.OBS
AUC = h15b.AUC
ATTACK = h15b.ATTACK
MODES = ("honest", "blind")
N_EVENTS = h15b.N_EVENTS
N_SEEDS = h15b.N_SEEDS
CRIT = 3


def arm_names() -> list[str]:
    return ["liot", "final", *[f"hyb{int(w)}" for w in WINDOWS]]


def run_cell(seed: int, *, unreg: float, obs: float, auc: float, attack: float, mode: str,
             vuln_ack: float, plain_ack: float = PLAIN_ACK, fpr: float = h15b.FPR,
             n: int = N_EVENTS, ack_on: bool = True) -> dict[str, Any]:
    rng = np.random.default_rng(seed)
    u_cat, u_fa, u_home, u_obs, u_att, u_score = (rng.random(n) for _ in range(6))
    ack_rng = np.random.default_rng(seed + 10_000)  # separate stream: h15b draws stay identical
    u_resp, u_time = ack_rng.random(n), ack_rng.random(n)
    probs = np.array([MIX_BASE[c] for c in CATS])
    cat = np.searchsorted(np.cumsum(probs / probs.sum()), u_cat, side="right").clip(0, len(CATS) - 1)
    gas_like = np.isin(cat, [CATS.index(c) for c in GAS_LIKE])
    registered = u_home < V_REG
    hidden = (u_home >= V_REG) & (u_home < V_REG + unreg)
    truly_vuln = registered | hidden
    obs_crit = ~gas_like & ~truly_vuln & (u_obs < obs)
    truth_crit = np.where(gas_like, u_fa >= h15b.FA, truly_vuln | obs_crit)
    floor = FLOOR[cat, registered.astype(np.int64)]
    signal = obs_crit | (hidden & ~gas_like & (mode == "honest"))
    mu = np.sqrt(2.0) * norm.ppf(min(auc, 1 - 1e-9)) if auc < 1.0 else 12.0
    z = norm.ppf(u_score.clip(1e-12, 1 - 1e-12))
    score = z + mu * signal
    raised = (score > norm.ppf(1.0 - fpr)) | (u_att < attack)
    floor_crit = floor == CRIT
    model_only = raised & ~floor_crit

    # ── shared resident-ack model ──
    p_ack = np.where(truly_vuln, vuln_ack, plain_ack)
    resp = (u_resp < p_ack) if ack_on else np.zeros(n, dtype=bool)
    t_ack = -ACK_MEAN_S * np.log1p(-u_time)

    def acked_within(w: float) -> np.ndarray:
        return resp & (t_ack <= w)

    not_acked_90 = ~acked_within(FORWARD_S)
    arms: dict[str, tuple[np.ndarray, np.ndarray]] = {}  # name -> (page, page time seconds)
    arms["liot"] = (floor_crit & not_acked_90, np.full(n, FORWARD_S))
    arms["final"] = ((floor_crit | raised) & not_acked_90, np.full(n, FORWARD_S))
    for w in WINDOWS:
        page = (floor_crit & not_acked_90) | (model_only & ~acked_within(w))
        arms[f"hyb{int(w)}"] = (page, np.where(floor_crit, FORWARD_S, w))

    out_arms: dict[str, list[float]] = {}
    for name, (page, ptime) in arms.items():
        paged_true = page & truth_crit
        out_arms[name] = [
            int((page & ~truth_crit).sum()),          # 0 false pages
            int((~page & truth_crit).sum()),          # 1 misses
            int(page.sum()),                          # 2 pages
            int(paged_true.sum()),                    # 3 paged true-danger events
            float(ptime[paged_true].sum()),           # 4 sum of time-to-page of those
            int((paged_true & (ptime <= 300.0)).sum()),  # 5 paged within 300 s
        ]
    return {
        "n_events": n, "n_critical": int(truth_crit.sum()),
        "n_model_only_raise": int(model_only.sum()),
        "n_model_only_raise_false": int((model_only & ~truth_crit).sum()),
        "arms": out_arms,
    }


def check_equivalence(seeds: int = 3) -> dict[str, Any]:
    """With the ack model off, ``liot`` and ``final`` equal h15b.run_cell's counts exactly."""
    bad = 0
    checked = 0
    for seed in range(seeds):
        for unreg, obs, auc, attack, mode in (
            (0.1, 0.1, 0.8, 0.0, "honest"), (0.3, 0.05, 0.6, 0.3, "blind"),
            (0.0, 0.0, 0.5, 0.1, "honest"),
        ):
            mine = run_cell(seed, unreg=unreg, obs=obs, auc=auc, attack=attack, mode=mode,
                            vuln_ack=0.0, ack_on=False)
            ref = h15b.run_cell(seed, unreg=unreg, obs=obs, auc=auc, attack=attack, mode=mode)
            for arm in ("liot", "final"):
                checked += 1
                if [int(x) for x in mine["arms"][arm][:3]] != ref["arms"][arm]:
                    bad += 1
    return {"checked": checked, "mismatch": bad}
