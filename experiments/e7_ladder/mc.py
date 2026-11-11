"""H6.2: simultaneous vs sequential guardian paging (pure simulation + closed forms).

Pre-registered (HYPOTHESES.md v2): guardian response ~ Exponential(mean mu = 1/3/10 min); a
guardian never responds with probability q (0/20/40%); bystander effect: with N guardians each
responds with probability p = (1 - q) / N**b (b = 0/0.3/0.6); sequential paging waits 90 s per
guardian before paging the next (``SEQ_WAIT_S``, pre-registered). Time 0 = the moment the
production ladder forwards (situation open + 90 s). 'simultaneous' is the production behaviour
(``_forward_to_guardians`` pages every consenting guardian in one tick); 'sequential' is the
alternative; no production code is changed. All numbers here are model assumptions.

Metrics per (mu, q, b, N, policy): P(ack) (someone acks eventually), E[time to first ack | ack],
P(ack within 300 s) (the production 119-guidance wait), E[people paged]. Closed forms:
  P(no ack) = (1-p)^N for both policies;
  simultaneous: E[T | ack] = sum_k P(K=k) mu/k / (1 - (1-p)^N), K ~ Bin(N, p);
  sequential: P(T > t) = prod_j [1 - p (1 - exp(-(t - 90 j)^+ / mu))],
              E[T ; ack] = integral_0^inf (P(T > t) - (1-p)^N) dt,
              P(page k) = prod_{j<k} [1 - p (1 - exp(-90 (k - j) / mu))].
Common random numbers: both policies of a batch share the response/time uniforms.
"""

from __future__ import annotations

import math
from typing import Any

import numpy as np

SEQ_WAIT_S = 90.0
MEANS_MIN = (1.0, 3.0, 10.0)
NONRESP = (0.0, 0.2, 0.4)
EXPONENTS = (0.0, 0.3, 0.6)
GUARDIANS = (1, 2, 3, 5)
N_BATCHES = 20
TRIALS_PER_BATCH = 500   # 20 x 500 = 10,000 per setting
GUIDANCE_WAIT_S = 300.0


def p_respond(q: float, b: float, n: int) -> float:
    return (1.0 - q) / (n ** b)


def _sequential(resp_delay: np.ndarray, n: int) -> tuple[np.ndarray, np.ndarray]:
    """(time of first ack, people paged) per trial for sequential paging every SEQ_WAIT_S."""
    trials = resp_delay.shape[0]
    ack_t = SEQ_WAIT_S * np.arange(n)[None, :] + resp_delay  # ack time if paged
    pages = np.zeros(trials)
    t_first = np.full(trials, np.inf)
    for t_i in range(trials):
        best = np.inf
        paged = 0
        for k in range(n):
            if best <= SEQ_WAIT_S * k:  # someone already acked before guardian k's turn
                break
            paged += 1
            best = min(best, ack_t[t_i, k])
        pages[t_i] = paged
        t_first[t_i] = best
    return t_first, pages


def simulate_batch(
    seed: int, *, mu_min: float, q: float, b: float, n: int, trials: int = TRIALS_PER_BATCH,
) -> dict[str, Any]:
    """Sufficient statistics of one batch (one 'seed') for both policies, common random numbers."""
    rng = np.random.default_rng(seed)
    u_resp = rng.random((trials, n))
    u_time = rng.random((trials, n))
    mu = mu_min * 60.0
    p = p_respond(q, b, n)
    responds = u_resp < p
    delay = -mu * np.log1p(-u_time)  # Exp(mean mu), seconds after being paged
    inf = np.inf
    resp_delay = np.where(responds, delay, inf)
    # simultaneous: everyone paged at 0
    t_sim = resp_delay.min(axis=1)
    # sequential: guardian k paged at 90 k while nobody has acked before. Two readings of the
    # bystander effect (C2.10): 'N' = p computed from the number of guardians (cycle 1);
    # 'concurrent' = p computed from the number paged AT THE SAME TIME (1 for sequential).
    t_seq, pages_seq = _sequential(resp_delay, n)
    resp_delay_c = np.where(u_resp < (1.0 - q), delay, inf)  # p = (1-q) / 1**b
    t_seqc, pages_seqc = _sequential(resp_delay_c, n)
    out: dict[str, Any] = {"seed": seed, "trials": trials}
    for name, t, pages in (("sim", t_sim, np.full(trials, float(n))), ("seq", t_seq, pages_seq),
                           ("seq_concurrent", t_seqc, pages_seqc)):
        acked = np.isfinite(t)
        out[name] = {
            "n_ack": int(acked.sum()),
            "sum_t_ack": float(t[acked].sum()),
            "n_ack_300": int((t <= GUIDANCE_WAIT_S).sum()),
            "sum_pages": float(pages.sum()),
        }
    both = np.isfinite(t_sim) & np.isfinite(t_seq)
    out["paired"] = {"n_both": int(both.sum()),
                     "sum_diff_seq_minus_sim": float((t_seq[both] - t_sim[both]).sum())}
    both_c = np.isfinite(t_sim) & np.isfinite(t_seqc)
    out["paired_concurrent"] = {"n_both": int(both_c.sum()),
                                "sum_diff_seq_minus_sim": float((t_seqc[both_c] - t_sim[both_c]).sum())}
    return out


def _seq_closed(mu: float, p: float, n: int) -> dict[str, float]:
    """Closed forms of sequential paging with per-guardian response probability ``p``."""
    p_no = (1.0 - p) ** n

    def surv(t: float) -> float:
        out = 1.0
        for j in range(n):
            x = max(t - SEQ_WAIT_S * j, 0.0)
            out *= 1.0 - p * (1.0 - math.exp(-x / mu))
        return out

    upper = 40.0 * mu + SEQ_WAIT_S * n
    grid = np.linspace(0.0, upper, 200001)
    s_grid = np.ones_like(grid)
    for j in range(n):
        x = np.maximum(grid - SEQ_WAIT_S * j, 0.0)
        s_grid *= 1.0 - p * (1.0 - np.exp(-x / mu))
    e_num = float(np.trapezoid(s_grid - p_no, grid))
    e_t = e_num / (1.0 - p_no) if p_no < 1.0 else float("nan")
    pages = 0.0
    for k in range(n):
        pk = 1.0
        for j in range(k):
            pk *= 1.0 - p * (1.0 - math.exp(-SEQ_WAIT_S * (k - j) / mu))
        pages += pk
    return {"e_t_ack": e_t, "p_ack_300": 1.0 - surv(GUIDANCE_WAIT_S), "pages": pages,
            "p_no_ack": p_no}


# ── closed forms ─────────────────────────────────────────────────────────────
def closed_form(mu_min: float, q: float, b: float, n: int) -> dict[str, Any]:
    mu = mu_min * 60.0
    p = p_respond(q, b, n)
    p_no = (1.0 - p) ** n
    # simultaneous
    e_sim = 0.0
    for k in range(1, n + 1):
        pk = math.comb(n, k) * p ** k * (1.0 - p) ** (n - k)
        e_sim += pk * mu / k
    e_sim = e_sim / (1.0 - p_no) if p_no < 1.0 else float("nan")
    p300_sim = 1.0 - (1.0 - p * (1.0 - math.exp(-GUIDANCE_WAIT_S / mu))) ** n

    seq = _seq_closed(mu, p, n)
    seq_c = _seq_closed(mu, 1.0 - q, n)
    return {
        "p": p, "p_no_ack": p_no,
        "sim": {"e_t_ack": e_sim, "p_ack_300": p300_sim, "pages": float(n)},
        "seq": seq, "seq_concurrent": seq_c,
    }
