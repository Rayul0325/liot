"""C4.10 (c): sequential guardian paging with 30 s / 60 s waits — time loss vs people paged.

Same pre-registered assumptions as E7 H6.2 (``experiments/e7_ladder/mc.py``): guardian response
~ Exponential(mean mu = 1/3/10 min), never-respond share q = 0/20/40%, bystander exponent
b = 0/0.3/0.6, N = 2/3/5 guardians (N = 1 is identical for every policy). Time 0 = the moment the
production ladder forwards. Only the wait between pages changes: ``wait = 0`` is the production
simultaneous policy; ``30``, ``60`` and ``90`` (E7's value, re-simulated here as a check against
``mc._seq_closed``) are sequential. Both C2.10 readings of the bystander effect are kept:

  ``N``          p = (1 - q) / N**b for every policy (cycle-1 reading)
  ``concurrent`` simultaneous p = (1 - q) / N**b; sequential p = 1 - q (one person at a time)

A guardian k is paged at ``k * wait`` only if nobody acked before that time; the first ack is
``min_k (k*wait + delay_k)`` over responders (an unpaged guardian's time is later than the ack
that stopped the sequence, so the minimum over all k equals the minimum over paged ones).
Common random numbers: all waits of a batch share the response and delay uniforms.

DESCRIPTIVE (기술 결과): no verdict. Whole result rests on the response model, which is assumed.
"""

from __future__ import annotations

from typing import Any

import numpy as np

from experiments.e7_ladder import mc

WAITS = (0.0, 30.0, 60.0, 90.0)
MEANS_MIN = mc.MEANS_MIN
NONRESP = mc.NONRESP
EXPONENTS = mc.EXPONENTS
GUARDIANS = (2, 3, 5)
N_BATCHES = mc.N_BATCHES
TRIALS_PER_BATCH = mc.TRIALS_PER_BATCH
GUIDANCE_WAIT_S = mc.GUIDANCE_WAIT_S


def _policy(resp_delay: np.ndarray, wait: float) -> tuple[np.ndarray, np.ndarray]:
    """(time of first ack, people paged) per trial; ``wait = 0`` = everyone paged at once."""
    trials, n = resp_delay.shape
    if wait == 0.0:
        return resp_delay.min(axis=1), np.full(trials, float(n))
    ack_t = wait * np.arange(n)[None, :] + resp_delay
    t_first = ack_t.min(axis=1)
    # guardian k is paged iff nobody acked before k*wait: prefix minimum of earlier ack times
    prefix = np.minimum.accumulate(ack_t, axis=1)
    pages = np.ones(trials)
    for k in range(1, n):
        pages += (prefix[:, k - 1] > wait * k).astype(float)
    return t_first, pages


def simulate_batch(seed: int, *, mu_min: float, q: float, b: float, n: int,
                   trials: int = TRIALS_PER_BATCH) -> dict[str, Any]:
    rng = np.random.default_rng(seed)
    u_resp = rng.random((trials, n))
    u_time = rng.random((trials, n))
    mu = mu_min * 60.0
    delay = -mu * np.log1p(-u_time)
    out: dict[str, Any] = {"seed": seed, "trials": trials}
    for reading in ("N", "concurrent"):
        p_sim = mc.p_respond(q, b, n)
        p_seq = p_sim if reading == "N" else 1.0 - q
        t_sim, pg_sim = _policy(np.where(u_resp < p_sim, delay, np.inf), 0.0)
        block: dict[str, Any] = {}
        for wait in WAITS:
            if wait == 0.0:
                t, pages = t_sim, pg_sim
            else:
                t, pages = _policy(np.where(u_resp < p_seq, delay, np.inf), wait)
            acked = np.isfinite(t)
            both = acked & np.isfinite(t_sim)
            block[f"w{int(wait)}"] = {
                "n_ack": int(acked.sum()), "sum_t_ack": float(t[acked].sum()),
                "n_ack_300": int((t <= GUIDANCE_WAIT_S).sum()),
                "sum_pages": float(pages.sum()),
                "n_both": int(both.sum()),
                "sum_diff_vs_sim": float((t[both] - t_sim[both]).sum()),
            }
        out[reading] = block
    return out


def closed_check(mu_min: float, q: float, b: float, n: int) -> dict[str, Any]:
    """E7's closed form for the 90 s sequential policy (reading N), to validate the w90 arm."""
    cf = mc.closed_form(mu_min, q, b, n)
    return {"seq90_e_t_ack": cf["seq"]["e_t_ack"], "seq90_pages": cf["seq"]["pages"],
            "seq90_p_ack_300": cf["seq"]["p_ack_300"], "sim_e_t_ack": cf["sim"]["e_t_ack"]}
