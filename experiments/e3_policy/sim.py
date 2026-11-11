"""Policy construction and the resident simulation (vectorised over policies x seeds).

A *lane* is one (policy group, seed). Lanes advance through the event stream together; the
only sequential state is the exponentially decaying confirmation load L (half-life h).

Resident model (all parameters in params.py):
  request with strength s in {1 light,2 explicit,3 double} at load L:
    p_read = exp(-f L)  (shape "exp")   or   1/(1+f L)  (shape "hyp")
    read   -> decides correctly with prob a (approve helpful/hazard, reject unwanted/harmful)
    unread -> approves (rubber stamp) with prob r (x d for double) else ignores
    approved but latency (lognormal, median m0 (1+g L)) > TTL -> expired, not executed
  no confirmation -> executed immediately (unless the proposal is silenced/blocked).
auto_undo: executed immediately + notification (light weight); undone if the notice is read
  (p_read), recognised (a) and the latency fits the window.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np

from .generator import ACT_OF, BASE_SEV, BINARY_KS, EXT_BINARY_LIST, N_PROP, Streams
from .liot_map import tables
from .params import FIXED, WEIGHT_SETS

THETAS = tuple(round(0.1 * i, 1) for i in range(11))
THETA_SIGMAS = (0.0, 0.5, 1.0)
UNDO_WINDOWS = (5, 15, 30)
KAPPAS = (0.0, 0.1, 0.25, 0.5, 1.0, 1.5, 2.0, 3.0, 5.0)  # theta-score noise as a multiple of the within-set SD
GEN_CUTS = tuple(sorted({round(float(x), 2) for x in BASE_SEV[:N_PROP]}))  # generator-side ceilings
P_LOW_GRID = (0.0, 0.25, 0.5, 0.75, 1.0)  # C2.1: share of harmful proposals steered to <=BREATH
P_AMB_GRID = (0.0, 0.25, 0.5, 0.75, 1.0)  # C2.11(a): share steered to the AMBIENT tier
R_LIGHT_MULTS = (1.0, 1.5, 2.0)  # C2.11(b): light-confirmation rubber-stamp as a multiple of explicit


@dataclass(frozen=True)
class Group:
    name: str  # policy family
    shape: str  # "exp" | "hyp" | "na"
    m: float | None = None
    theta: float | None = None
    sigma: float | None = None
    k: int | None = None  # binary list length
    undo_min: int = 0
    gate: bool = False  # system-wide "never open" (lock.unlock, valve.open) applied
    kappa: float | None = None  # theta score noise as a multiple of the within-set score SD
    ceil: str = ""  # "" | "liot" (LIOT tier > BREATH silenced) | "gen" (generator BASE_SEV > cut silenced)
    cut: float | None = None
    notify: bool = False  # silenced events still send a light notice (C2.3 AMBIENT notify-only)

    @property
    def key(self) -> str:
        parts = [self.name, self.shape]
        if self.m is not None:
            parts.append(f"m{self.m:.1f}")
        if self.k is not None:
            parts.append(f"k{self.k}")
        if self.undo_min:
            parts.append(f"w{self.undo_min}")
        if self.theta is not None:
            parts.append(f"s{self.sigma:.1f}" if self.kappa is None else f"k{self.kappa:.2f}")
            parts.append(f"t{self.theta:.1f}")
        if self.ceil:
            parts.append("ceil-liot" if self.ceil == "liot" else f"ceil-gen{self.cut:.2f}")
        if self.notify:
            parts.append("notify")
        if self.gate:
            parts.append("gate")
        return "|".join(parts)


def _cycle2_groups(block: str, m_grid: tuple[float, ...], shapes: tuple[str, ...]) -> list[Group]:
    gs: list[Group] = [Group("auto_all", "na")]
    if block.startswith("c21"):
        for m in (0.0, 0.1):
            for nm in ("flat_explicit", "liot_full", "liot_real"):
                gs.append(Group(nm, "exp", m=m))
    elif block == "c22":
        for m in (0.0, 0.1):
            for sh in shapes:
                for nm in ("liot_real", "liot_full"):
                    gs.append(Group(nm, sh, m=m))
                for base in ("flat_explicit", "binary_ext"):
                    gs.append(Group(base, sh, m=m))
                    gs.append(Group(base, sh, m=m, ceil="liot"))
                    gs += [Group(base, sh, m=m, ceil="gen", cut=c) for c in GEN_CUTS]
            gs.append(Group("auto_all", "na", m=m, ceil="liot"))
            gs += [Group("auto_all", "na", m=m, ceil="gen", cut=c) for c in GEN_CUTS]
    elif block == "c23":
        for m in m_grid:
            for sh in shapes:
                for nm in ("flat_explicit", "liot_full", "liot_real", "binary_ext"):
                    gs.append(Group(nm, sh, m=m))
                gs.append(Group("liot_full", sh, m=m, notify=True))
    elif block == "c24":
        for m in (0.0, 0.1):
            for sh in shapes:
                for nm in ("flat_explicit", "liot_full", "liot_real"):
                    gs.append(Group(nm, sh, m=m))
        for k in KAPPAS:
            for th in THETAS:
                gs.append(Group("theta", "exp", theta=th, kappa=k))
    return gs


def groups_for(block: str, m_grid: tuple[float, ...], shapes: tuple[str, ...]) -> list[Group]:
    if block.startswith("c2"):
        return _cycle2_groups(block, m_grid, shapes)
    if block == "f0_g0":  # lean: the f=0, r=0, g=0 sanity block needs only the compared policies
        gs = [Group("auto_all", "na")]
        for m in m_grid:
            for nm in ("flat_explicit", "liot_full", "liot_real", "binary_ext"):
                gs.append(Group(nm, "na", m=m))
            gs += [Group("binary_k", "na", m=m, k=k) for k in BINARY_KS]
        return gs
    gate = block == "gated"
    gs: list[Group] = [Group("auto_all", "na", gate=gate)]
    for m in m_grid:
        for sh in shapes:
            for nm in ("flat_explicit", "liot_full", "liot_real", "binary_ext"):
                gs.append(Group(nm, sh, m=m, gate=gate))
            for k in BINARY_KS:
                gs.append(Group("binary_k", sh, m=m, k=k, gate=gate))
    if block != "gated":
        for sh in shapes:
            for w in UNDO_WINDOWS:
                gs.append(Group("auto_undo", sh, undo_min=w))
            for s in ((0.5,) if block.startswith("sens") else THETA_SIGMAS):
                for th in THETAS:
                    gs.append(Group("theta", sh, theta=th, sigma=s))
    return gs


def score_sd(st: Streams) -> float:
    """Within-set SD of the noiseless generic score (all valid events, all seeds)."""
    return float(np.std(st.score0[st.valid]))


def _theta_level(st: Streams, sigma: float, theta: float) -> np.ndarray:
    score = st.score0 + sigma * st.z_score
    score = np.where(st.valid, score, -np.inf)
    n = st.valid.sum(1)
    k = np.rint(theta * n).astype(int)
    order = np.argsort(-score, axis=1, kind="stable")
    rank = np.empty_like(order)
    np.put_along_axis(rank, order, np.arange(score.shape[1])[None, :].repeat(score.shape[0], 0), axis=1)
    return np.where(rank < k[:, None], 2, 0).astype(np.int8)


def harmful_kinds(st: Streams) -> np.ndarray:
    """Templates that carry at least one harmful proposal in this set (the 'harmful device kinds')."""
    ok = st.valid & (st.kind == 0) & (st.truth == 2)
    return np.bincount(st.tmpl[ok].astype(int), minlength=N_PROP)[:N_PROP] > 0


def build(st: Streams, g: Group, rank_order: np.ndarray,
          hk: np.ndarray | None = None) -> tuple[np.ndarray, np.ndarray]:
    """(level int8, blocked bool), both (seeds, events), for policy group *g*."""
    tb = tables()
    tmpl, tbk, occ = st.tmpl.astype(int), st.tb.astype(int), st.occ.astype(int)
    shape0 = st.valid.shape
    level = np.zeros(shape0, dtype=np.int8)
    blocked = np.zeros(shape0, dtype=bool)
    prop = st.kind == 0
    if g.m is not None:
        tier0 = tb["tier0"][tmpl, tbk, occ].astype(int)
        mis = (st.truth == 2) & (st.u_mis < g.m)
        target = np.where(st.u_k < 0.5, 0, 1)  # AMBIENT (m/2) or BREATH (m/2)
        tier_m = np.where(mis, np.minimum(tier0, target), tier0)
    if g.name == "flat_explicit":
        level = tb["conf_A"][tmpl, tbk, occ, tier_m]
    elif g.name == "liot_full":
        level = tb["conf_B"][tmpl, tbk, occ, tier_m]
    elif g.name == "liot_real":
        level = tb["conf_B"][tmpl, tbk, occ, tier_m]
        blocked = prop & (tier_m > int(tb["ceiling"]))  # proactive cap: silent
    elif g.name in ("binary_ext", "binary_k"):
        listed = np.zeros(N_PROP, dtype=bool)
        if g.name == "binary_ext":
            listed[list(EXT_BINARY_LIST)] = True
        else:
            listed[rank_order[: g.k]] = True
        act = ACT_OF[tmpl]
        ac = np.clip(act, 0, N_PROP - 1)
        member = np.where(act >= 0, listed[ac], False)
        # registered error model: each harmful device kind on the list is dropped from it with
        # probability m (one draw per seed and kind, shared by every policy -> common random numbers)
        hk_ = hk if hk is not None else np.ones(N_PROP, dtype=bool)
        dropped = (np.take_along_axis(st.u_list, ac, axis=1) < g.m) & hk_[ac] & (act >= 0)
        level = np.where(member & ~dropped, 2, 0)
    elif g.name == "theta":
        sig_abs = g.sigma if g.kappa is None else g.kappa * score_sd(st)
        level = _theta_level(st, sig_abs, g.theta)
    # auto_all / auto_undo: all zero
    if g.ceil == "liot":
        blocked = blocked | (prop & (tier_m > int(tb["ceiling"])))
    elif g.ceil == "gen":
        gen_silent = prop & (BASE_SEV[tmpl] > g.cut + 1e-9)
        if g.m is not None:  # same misclassified harmful proposals escape the generator ceiling too
            gen_silent = gen_silent & ~mis
        blocked = blocked | gen_silent
    if g.notify:
        blocked = blocked | (prop & (level == 0))
    if g.gate:
        blocked = blocked | tb["open_forbidden"][tmpl]
    level = np.where(blocked | ~st.valid, 0, level).astype(np.int8)
    blocked = blocked & st.valid
    return level, blocked


def binary_rank(st: Streams) -> np.ndarray:
    """Templates ordered by pooled harmful-proposal count (post-hoc, oracle ranking)."""
    ok = st.valid & (st.kind == 0) & (st.truth == 2)
    counts = np.bincount(st.tmpl[ok].astype(int), minlength=N_PROP)[:N_PROP]
    return np.argsort(-counts, kind="stable")


COUNTERS = ("xh", "xp", "xu", "nc", "nch", "units", "hm", "bh", "bp", "bu", "mu", "hz_ok", "hz_lat")


def simulate(
    st: Streams, groups: list[Group], levels: np.ndarray, blocked: np.ndarray, prm: dict[str, float]
) -> dict[str, np.ndarray]:
    """Run all lanes. levels/blocked: (groups, seeds, events). Returns counters (groups, seeds)."""
    p_n, s_n, e_n = levels.shape
    w = WEIGHT_SETS[int(prm["wsel"])]
    f, a, r, d, g_ = prm["f"], prm["a"], prm["r"], prm["d"], prm["g"]
    r_light = min(1.0, r * prm.get("r_light_mult", 1.0))  # C2.11(b); 1.0 = same as explicit
    m0 = prm["m0_s"]
    sig = FIXED["latency_sigma"][0]
    ttl_p = FIXED["proposal_ttl_min"][0] * 60.0
    ttl_h = FIXED["hazard_deadline_min"][0] * 60.0
    cap_min = FIXED["harm_exposure_min"][0]
    half = prm["half_life_h"] * 60.0

    lv_t = np.ascontiguousarray(levels.transpose(2, 0, 1))
    bk_t = np.ascontiguousarray(blocked.transpose(2, 0, 1))
    shape_exp = np.array([g.shape != "hyp" for g in groups])[:, None]
    undo_w = np.array([g.undo_min * 60.0 for g in groups])[:, None]
    notify = np.array([g.notify for g in groups])[:, None]
    any_notify = bool(notify.any())
    is_undo = undo_w > 0
    any_undo = bool(is_undo.any())

    valid = st.valid.T
    isprop = (st.kind == 0).T & valid
    ishaz = (st.kind == 1).T & valid
    tr = st.truth.T
    good = (tr == 0) | (tr == 3)
    correct = (st.u_dec < a).T
    ap_read = np.where(correct, good, ~good)
    undo_ok = np.where(~good, correct, ~correct)  # undo decision of a reader (bad -> undo)
    decay = 2.0 ** (-st.dt_min.T / half)
    ur, urs = st.u_read.T, st.u_rs.T
    lat_noise = np.exp(sig * st.z_lat.T)
    ttl = np.where(ishaz, ttl_h, ttl_p)
    m_help, m_unw, m_harm = (tr == 0) & isprop, (tr == 1) & isprop, (tr == 2) & isprop

    L = np.zeros((p_n, s_n))
    c = {k: np.zeros((p_n, s_n)) for k in COUNTERS}
    w1, w2, w3 = w[1], w[2], w[3]
    for e in range(e_n):
        lv = lv_t[e]
        blk = bk_t[e]
        L *= decay[e]
        pr = np.where(shape_exp, np.exp(-f * L), 1.0 / (1.0 + f * L))
        rd = ur[e] < pr
        r_eff = np.where(lv == 3, r * d, np.where(lv == 1, r_light, r))
        ap = np.where(rd, ap_read[e], urs[e] < r_eff)
        lat = m0 * (1.0 + g_ * L) * lat_noise[e]
        req = lv > 0
        ex = np.where(req, ap & (lat <= ttl[e]), ~blk) & valid[e]
        und = np.zeros_like(ex)
        add = (lv == 1) * w1 + (lv == 2) * w2 + (lv == 3) * w3
        if any_undo:
            cand = is_undo & ex & isprop[e]
            und = cand & rd & undo_ok[e] & (lat <= undo_w)
            add = add + (is_undo & isprop[e]) * w1
        if any_notify:
            add = add + (notify & blk & isprop[e]) * w1
        fe = ex & ~und
        c["xh"] += fe & m_harm[e]
        c["xp"] += fe & m_help[e]
        c["xu"] += fe & m_unw[e]
        c["nc"] += req & isprop[e]
        c["nch"] += req & ishaz[e]
        c["units"] += add
        c["hm"] += np.where(m_harm[e] & ex, np.where(und, lat / 60.0, cap_min), 0.0)
        c["bh"] += blk & m_harm[e]
        c["bp"] += blk & m_help[e]
        c["bu"] += blk & m_unw[e]
        c["mu"] += und & m_help[e]
        c["hz_ok"] += fe & ishaz[e]
        c["hz_lat"] += np.where(ishaz[e], np.where(ex, np.where(req, lat, 0.0), ttl_h), 0.0)
        L += add
    return c
