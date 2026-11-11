"""Event generator for E3 — independent of LIOT (no Gate1, no catalog, no tiers).

Ground truth of a proposal (0 helpful, 1 unwanted, 2 harmful) is decided from context
features only (hour, occupancy, outdoor temperature, rain, air quality, vulnerable
resident) by `truth_codes`. Real hazards (truth 3) are separate events whose protective
action is proposed to the resident.

Common random numbers: `make_streams(params, key, n_seeds, days)` is a pure function of its
arguments; every policy is evaluated on the same arrays.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np

from .params import FIXED

# ── device/action templates ──────────────────────────────────────────────────────────────
# (name, group index, capability, action, params, entity_id, category)
# Categories follow production resolution: known categories keep their Gate1 floor; an
# entity absent from the catalog's capability map is default-deny EMBER in the catalog.
# group: 0 light, 1 climate/air, 2 cover, 3 appliance, 4 entry
TEMPLATES: tuple[tuple[str, int, str, str, dict, str, str], ...] = (
    ("light_on", 0, "light", "turn_on", {}, "light.living_room", "light"),
    ("light_off", 0, "light", "turn_off", {}, "light.living_room", "light"),
    ("purifier_boost", 1, "fan", "set_speed", {"speed": "high"}, "fan.living_purifier", "climate"),
    ("heat_up", 1, "climate", "set_temperature", {"temperature": "+2"}, "climate.living_room", "climate"),
    ("heat_down", 1, "climate", "set_temperature", {"temperature": "-2"}, "climate.living_room", "climate"),
    ("window_open", 2, "cover", "open", {}, "cover.window", "blind"),
    ("window_close", 2, "cover", "close", {}, "cover.window", "blind"),
    ("washer_start", 3, "switch", "turn_on", {}, "switch.washer", "appliance"),
    ("oven_on", 3, "switch", "turn_on", {}, "switch.oven", "oven"),
    ("lock_lock", 4, "lock", "lock", {}, "lock.front_door", "lock"),
    ("lock_unlock", 4, "lock", "unlock", {}, "lock.front_door", "lock"),
    ("valve_close", 4, "valve", "close", {}, "switch.gas_valve", "gas"),
    ("valve_open", 4, "valve", "open", {}, "switch.gas_valve", "gas"),
    ("garage_open", 4, "cover", "open", {}, "cover.garage", "door"),
    ("garage_close", 4, "cover", "close", {}, "cover.garage", "door"),
    # hazard response actions (never proposed as ordinary proposals)
    ("haz_gas_valve_close", 4, "valve", "close", {}, "switch.gas_valve", "gas"),
    ("haz_leak_valve_close", 4, "valve", "close", {}, "switch.water_valve", "leak"),
    ("haz_door_lock", 4, "lock", "lock", {}, "lock.front_door", "lock"),
    ("haz_oven_off", 3, "switch", "turn_off", {}, "switch.oven", "oven"),
)
N_PROP = 15  # templates 0..14 are ordinary proposals
N_TMPL = len(TEMPLATES)
HAZARD_TMPLS = (15, 16, 17, 18)
NAME = [t[0] for t in TEMPLATES]
GROUP_OF = np.array([t[1] for t in TEMPLATES])
# list-membership index for binary approval: a hazard action is "the same device action"
ACT_OF = np.array([*range(N_PROP), 11, 11, 9, -1])
# generic (generator-side) inherent severity used only by the theta risk score
BASE_SEV = np.array(
    [0.05, 0.10, 0.10, 0.15, 0.35, 0.40, 0.10, 0.15, 0.80, 0.15, 0.90, 0.20, 0.90, 0.60, 0.30,
     0.90, 0.90, 0.90, 0.90]
)
EXT_BINARY_LIST = (10, 13)  # HGA public docs: unlock, garage open
BINARY_KS = (1, 2, 3, 4, 5, 6, 8, 10, 15)

HOUR_PROFILE = np.array(
    [0.3] * 6 + [1.5, 2.0, 1.5, 1.0, 1.0, 1.0, 1.2, 1.0, 0.8, 0.8, 1.0, 1.5, 2.0, 2.0, 1.8, 1.5, 0.8, 0.5]
)
HOUR_PROFILE = HOUR_PROFILE / HOUR_PROFILE.sum()


def time_bucket_code(hr: np.ndarray) -> np.ndarray:
    """0 morning 06-11, 1 day 11-17, 2 evening 17-22, 3 night (production Clock buckets)."""
    return np.where((hr >= 22) | (hr < 6), 3, np.where(hr < 11, 0, np.where(hr < 17, 1, 2)))


def truth_codes(
    tmpl: np.ndarray,
    hr: np.ndarray,
    away: np.ndarray,
    temp: np.ndarray,
    rain: np.ndarray,
    vuln: np.ndarray,
    pm_bad: np.ndarray,
    low_rules: bool = False,
    amb_rules: bool = False,
) -> np.ndarray:
    """0 helpful / 1 unwanted / 2 harmful for each (template, context) element.

    Pure function of context features; shapes broadcast. Harm is checked first.
    """
    night = (hr >= 22) | (hr < 6)
    home = ~away
    evening = (hr >= 17) & (hr < 22)
    out = np.ones(np.broadcast(tmpl, hr).shape, dtype=np.int8)
    harm = np.zeros(out.shape, dtype=bool)
    help_ = np.zeros(out.shape, dtype=bool)

    def put(t: int, h: np.ndarray | bool, g: np.ndarray | bool) -> None:
        m = tmpl == t
        harm[m] = np.broadcast_to(h, out.shape)[m]
        help_[m] = np.broadcast_to(g, out.shape)[m]

    put(0, (home & night) if low_rules else False, home & (night | evening))
    harm_lo = home & night & vuln
    if amb_rules:  # C2.11(a): evening light-off with someone home is harmful (an AMBIENT-tier context)
        harm_lo = harm_lo | (home & evening)
    put(1, harm_lo, away | (home & (hr >= 6) & (hr < 17)) | (home & night & ~vuln))
    put(2, (home & night) if low_rules else False, home & pm_bad)
    put(3, home & vuln & (temp > 26), home & (temp < 14))
    harm_hd = (temp < -5) | (home & (temp < 5) & (vuln | night))
    put(4, harm_hd, (temp > 18) | (away & (temp >= 5)))
    harm_wo = rain | away | night | (temp < 0)
    put(5, harm_wo, home & (hr >= 6) & (hr < 11) & (temp >= 8) & (temp <= 28))
    put(6, (home & (temp > 26)) if low_rules else False, rain | away | night)
    put(7, away, home & (hr >= 8) & (hr < 21))
    put(8, away | night, home & np.isin(hr, (7, 8, 12, 17, 18, 19)))
    put(9, False, away | (home & night))
    put(10, night | away, home & (hr >= 16) & (hr < 22))
    put(11, False, away)
    put(12, away | night, home & (((hr >= 6) & (hr < 9)) | ((hr >= 16) & (hr < 20))))
    put(13, night | away, home & (hr >= 7) & (hr < 21))
    put(14, False, night | away)
    out[help_] = 0
    out[harm] = 2
    return out


@dataclass
class Streams:
    """Padded (seeds, events) arrays; every policy reads the same ones."""

    valid: np.ndarray
    kind: np.ndarray  # 0 proposal, 1 hazard
    tmpl: np.ndarray
    truth: np.ndarray  # 0 help, 1 unwanted, 2 harm, 3 hazard
    tb: np.ndarray
    occ: np.ndarray  # 0 occupied, 1 vacant
    t_min: np.ndarray
    dt_min: np.ndarray
    score0: np.ndarray  # generic score before noise, in [0, 1]
    z_score: np.ndarray
    u_read: np.ndarray
    u_dec: np.ndarray
    u_rs: np.ndarray
    z_lat: np.ndarray
    u_mis: np.ndarray
    u_k: np.ndarray
    u_list: np.ndarray  # (seeds, N_PROP) draws for dropping device kinds from a binary list
    realized: dict[str, float]


def _one_seed(prm: dict[str, float], key: int, seed: int, days: int,
              opts: dict | None = None) -> dict[str, np.ndarray]:
    rng = np.random.default_rng([key, seed])
    vuln = rng.random() < FIXED["p_vulnerable"][0]
    mu = rng.uniform(-8, 22)
    tday = mu + 4.0 * rng.standard_normal(days)
    rain_day = rng.random(days) < FIXED["p_rain_day"][0]
    pm_day = rng.random(days) < FIXED["p_pm_bad_day"][0]
    p_away = FIXED["p_away_daytime"][0]

    n_prop_day = rng.poisson(prm["lam"], days)
    day_p = np.repeat(np.arange(days), n_prop_day)
    n = day_p.size
    gm = np.array([prm["mult_light"], prm["mult_climate"], prm["mult_cover"],
                   prm["mult_appliance"], prm["mult_entry"]])
    cnt = np.bincount(GROUP_OF[:N_PROP], minlength=5)
    w = gm[GROUP_OF[:N_PROP]] / cnt[GROUP_OF[:N_PROP]]
    w = w / w.sum()
    opts = opts or {}
    kc = int(opts.get("kc", 64))
    cand_t = rng.choice(N_PROP, size=(n, kc), p=w)
    cand_h = rng.choice(24, size=(n, kc), p=HOUR_PROFILE)
    p_low = opts.get("p_low")
    if p_low is not None:
        # sensitivity block (C2.1): a share p_low of the harmful proposals is drawn from a template pool
        # (and mostly night hours) given by the caller; ids come from outside, this module reads no tiers
        u = rng.random(n)
        want = np.where(u < prm["harm_frac"], 2, np.where(u < prm["harm_frac"] + prm["unw_frac"], 1, 0))
        pool_ids = np.array(list(opts["low_ids"]))
        pool_low = np.zeros(N_PROP, dtype=bool)
        pool_low[list(opts.get("avoid_ids", opts["low_ids"]))] = True  # non-steered harm keeps out of these
        steer = (want == 2) & (rng.random(n) < p_low)
        wl = gm[GROUP_OF[pool_ids]] / cnt[GROUP_OF[pool_ids]]
        wl = wl / wl.sum()
        ct_low = rng.choice(pool_ids, size=(n, kc), p=wl)
        ch_low = np.where(rng.random((n, kc)) < opts.get("pool_hour_p", 0.6),
                          rng.choice(opts.get("pool_hours", [22, 23, 0, 1, 2, 3, 4, 5]), size=(n, kc)), cand_h)
        cand_t = np.where(steer[:, None], ct_low, cand_t)
        cand_h = np.where(steer[:, None], ch_low, cand_h)
    daytime = (cand_h >= 8) & (cand_h < 19)
    cand_away = rng.random((n, kc)) < np.where(daytime, p_away, 0.03)
    temp = tday[day_p][:, None] - np.where((cand_h >= 22) | (cand_h < 6), 5.0, 0.0)
    rain = rain_day[day_p][:, None]
    pm = pm_day[day_p][:, None]
    codes = truth_codes(cand_t, cand_h, cand_away, temp, rain, vuln, pm, bool(opts.get("low_rules", False)),
                        bool(opts.get("amb_rules", False)))
    if p_low is None:
        u = rng.random(n)
        want = np.where(u < prm["harm_frac"], 2, np.where(u < prm["harm_frac"] + prm["unw_frac"], 1, 0))
    match = codes == want[:, None]
    if p_low is not None:
        # harmful proposals that are not steered keep to the other pool (so the share is a clean dial)
        keep_high = (want == 2) & ~steer
        steered_match = match & (~pool_low[cand_t] | ~keep_high[:, None])
        match = np.where(steered_match.any(1)[:, None], steered_match, match)
    first = match.argmax(1)
    found = match.any(1)
    pick = np.where(found, first, 0)
    ar = np.arange(n)
    p_tmpl = cand_t[ar, pick]
    p_hr = cand_h[ar, pick]
    p_away_sel = cand_away[ar, pick]
    p_truth = codes[ar, pick].astype(np.int8)
    p_temp = temp[ar, pick]
    p_rain = rain[ar, 0]
    p_min = day_p * 1440 + p_hr * 60 + rng.random(n) * 60

    n_haz_day = rng.poisson(prm["haz_rate"], days)
    day_h = np.repeat(np.arange(days), n_haz_day)
    nh = day_h.size
    h_hr = rng.integers(0, 24, nh)
    h_away = rng.random(nh) < np.where((h_hr >= 8) & (h_hr < 19), p_away, 0.03)
    h_tmpl = np.array(HAZARD_TMPLS)[rng.integers(0, 4, nh)] if nh else np.zeros(0, int)
    h_temp = tday[day_h] - np.where((h_hr >= 22) | (h_hr < 6), 5.0, 0.0)
    h_rain = rain_day[day_h]
    h_min = day_h * 1440 + h_hr * 60 + rng.random(nh) * 60

    tmpl = np.concatenate([p_tmpl, h_tmpl]).astype(int)
    hr = np.concatenate([p_hr, h_hr]).astype(int)
    away = np.concatenate([p_away_sel, h_away])
    tmp = np.concatenate([p_temp, h_temp])
    rn = np.concatenate([p_rain, h_rain])
    truth = np.concatenate([p_truth, np.full(nh, 3, np.int8)])
    kind = np.concatenate([np.zeros(n, int), np.ones(nh, int)])
    t = np.concatenate([p_min, h_min])
    order = np.argsort(t, kind="stable")
    tmpl, hr, away, tmp, rn, truth, kind, t = (x[order] for x in (tmpl, hr, away, tmp, rn, truth, kind, t))
    ne = tmpl.size
    night = (hr >= 22) | (hr < 6)
    k = night.astype(int) + away + ((~away) & vuln) + rn + (tmp < 5)
    score0 = BASE_SEV[tmpl] * (1 + 0.4 * k) / 3.0
    return {
        "tmpl": tmpl, "truth": truth, "kind": kind, "t": t,
        "tb": time_bucket_code(hr), "occ": away.astype(int), "score0": score0,
        "z_score": rng.standard_normal(ne), "u_read": rng.random(ne), "u_dec": rng.random(ne),
        "u_rs": rng.random(ne), "z_lat": rng.standard_normal(ne), "u_mis": rng.random(ne),
        "u_k": rng.random(ne), "u_list": rng.random(N_PROP),
    }


def make_streams(prm: dict[str, float], key: int, n_seeds: int, days: int,
                 opts: dict | None = None) -> Streams:
    per = [_one_seed(prm, key, s, days, opts) for s in range(n_seeds)]
    e = max(1, max(p["tmpl"].size for p in per))

    def stack(name: str, fill: float, dtype: type) -> np.ndarray:
        out = np.full((n_seeds, e), fill, dtype=dtype)
        for i, p in enumerate(per):
            out[i, : p[name].size] = p[name]
        return out

    valid = np.zeros((n_seeds, e), dtype=bool)
    for i, p in enumerate(per):
        valid[i, : p["tmpl"].size] = True
    t = stack("t", 0.0, float)
    for i, p in enumerate(per):
        t[i, p["tmpl"].size :] = p["t"][-1] if p["t"].size else 0.0
    dt = np.diff(t, axis=1, prepend=t[:, :1])
    truth = stack("truth", 0, np.int8)
    kind = stack("kind", 0, np.int8)
    ok = valid & (kind == 0)
    realized = {
        "help": float(((truth == 0) & ok).sum() / max(1, ok.sum())),
        "unw": float(((truth == 1) & ok).sum() / max(1, ok.sum())),
        "harm": float(((truth == 2) & ok).sum() / max(1, ok.sum())),
        "events_per_seed": float(valid.sum() / n_seeds),
    }
    return Streams(
        valid=valid, kind=kind, tmpl=stack("tmpl", 0, np.int16), truth=truth,
        tb=stack("tb", 0, np.int8), occ=stack("occ", 0, np.int8), t_min=t, dt_min=dt,
        score0=stack("score0", 0.0, float), z_score=stack("z_score", 0.0, float),
        u_read=stack("u_read", 0.5, float), u_dec=stack("u_dec", 0.5, float),
        u_rs=stack("u_rs", 0.5, float), z_lat=stack("z_lat", 0.0, float),
        u_mis=stack("u_mis", 1.0, float), u_k=stack("u_k", 0.0, float),
        u_list=np.array([p["u_list"] for p in per]), realized=realized,
    )
