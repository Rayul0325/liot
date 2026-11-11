"""E3 parameter space: every swept quantity with range and one-line justification.

All values are ASSUMPTIONS unless noted (registered table: experiments/HYPOTHESES.md Q2).
`FIXED` lists the constants that are not swept (also assumptions). `sample` draws a Latin
hypercube; `scaled` halves/doubles every range around its centre for the sensitivity block.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np

GROUPS = ("light", "climate", "cover", "appliance", "entry")

# Load weights per confirmation strength (none, light, explicit, double); registered default
# first, then the two registered alternatives.
WEIGHT_SETS: tuple[tuple[float, float, float, float], ...] = (
    (0.0, 1.0, 2.0, 3.0),
    (0.0, 1.0, 1.0, 1.0),
    (0.0, 1.0, 3.0, 6.0),
)

M_GRID = (0.0, 0.1, 0.2, 0.3, 0.4)
M_DEFAULT = 0.1  # assumption: headline misclassification (grid 0-0.4 is swept separately)


@dataclass(frozen=True)
class Param:
    name: str
    lo: float
    hi: float
    kind: str  # "lin" | "log"
    dom_lo: float
    dom_hi: float
    why: str


PARAMS: tuple[Param, ...] = (
    Param("lam", 4, 20, "lin", 1, 60, "proposals/day; registered 4-20 (default 10)"),
    Param("harm_frac", 0.01, 0.20, "lin", 0.0, 0.6, "share of harmful proposals; registered 1-20%"),
    Param("unw_frac", 0.10, 0.50, "lin", 0.0, 0.8, "share of unwanted proposals; registered 10-50%"),
    Param("haz_rate", 0.0, 1.0, "lin", 0.0, 4.0, "real hazard events per day; registered 0-1"),
    Param("mult_light", 0.5, 2.0, "log", 0.05, 20, "weight of lighting in the device mix; registered 0.5-2x"),
    Param("mult_climate", 0.5, 2.0, "log", 0.05, 20, "weight of climate/air in the device mix"),
    Param("mult_cover", 0.5, 2.0, "log", 0.05, 20, "weight of window/cover in the device mix"),
    Param("mult_appliance", 0.5, 2.0, "log", 0.05, 20, "weight of oven/washer in the device mix"),
    Param("mult_entry", 0.5, 2.0, "log", 0.05, 20, "weight of lock/garage/valve in the device mix"),
    Param("f", 0.0, 0.3, "lin", 0.0, 2.0, "fatigue coefficient; registered 0-0.3"),
    Param("half_life_h", 2.0, 24.0, "lin", 0.25, 96, "half-life of the confirmation load; registered 2-24 h"),
    Param("a", 0.7, 0.99, "lin", 0.5, 1.0, "accuracy when the request is read; registered 0.7-0.99"),
    Param("r", 0.0, 0.8, "lin", 0.0, 1.0, "rubber-stamp probability when unread; registered 0-0.8"),
    Param("d", 0.2, 1.0, "lin", 0.05, 1.0, "rubber-stamp factor for double confirmation; registered 0.2-1"),
    Param("m0_s", 30.0, 600.0, "log", 5, 3600, "median response latency at zero load (s); ADDED assumption"),
    Param("g", 0.0, 0.3, "lin", 0.0, 2.0, "latency growth per load unit; ADDED assumption"),
)
CAT_DIMS = ("wsel",)  # weight-set index (uniform over WEIGHT_SETS)

FIXED: dict[str, tuple[float | str, str]] = {
    "days": (30, "registered fixed horizon"),
    "latency_sigma": (0.8, "lognormal sigma of response latency; assumption"),
    "proposal_ttl_min": (60, "unanswered/slow proposals expire after 60 min (comfort.yaml ttl)"),
    "hazard_deadline_min": (10, "a hazard response later than this counts as missed; assumption"),
    "harm_exposure_min": (240, "minutes a persisting harmful action is counted as harming; assumption"),
    "undo_windows_min": ("5,15,30", "registered undo windows for auto_undo"),
    "p_vulnerable": (0.5, "share of households with a vulnerable resident; assumption"),
    "p_away_daytime": (0.3, "probability nobody is home 08-19 h; assumption"),
    "p_rain_day": (0.25, "share of rainy days; assumption"),
    "p_pm_bad_day": (0.3, "share of bad-air days; assumption"),
    "mean_temp_range_c": ("-8..22", "per-household mean outdoor temperature drawn uniform; assumption"),
    "theta_score_sigma": ("0,0.5,1", "registered noise of the generic risk score"),
}


def _draw(p: Param, u: np.ndarray) -> np.ndarray:
    if p.kind == "log":
        return np.exp(np.log(p.lo) + u * (np.log(p.hi) - np.log(p.lo)))
    return p.lo + u * (p.hi - p.lo)


def scaled(params: tuple[Param, ...], factor: float) -> tuple[Param, ...]:
    """Range around its centre times *factor* (linear or log space), clipped to the domain."""
    out = []
    for p in params:
        if p.kind == "log":
            c = 0.5 * (np.log(p.lo) + np.log(p.hi))
            h = 0.5 * (np.log(p.hi) - np.log(p.lo)) * factor
            lo, hi = float(np.exp(c - h)), float(np.exp(c + h))
        else:
            c = 0.5 * (p.lo + p.hi)
            h = 0.5 * (p.hi - p.lo) * factor
            lo, hi = c - h, c + h
        lo, hi = max(lo, p.dom_lo), min(hi, p.dom_hi)
        out.append(Param(p.name, lo, hi, p.kind, p.dom_lo, p.dom_hi, p.why))
    return tuple(out)


def sample(n: int, seed: int, params: tuple[Param, ...] = PARAMS) -> list[dict[str, float]]:
    """Latin hypercube of *n* sets over *params* plus the categorical weight set."""
    rng = np.random.default_rng(seed)
    dims = len(params) + len(CAT_DIMS)
    u = np.empty((n, dims))
    for j in range(dims):
        u[:, j] = (rng.permutation(n) + rng.random(n)) / n
    sets: list[dict[str, float]] = []
    cols = {p.name: _draw(p, u[:, j]) for j, p in enumerate(params)}
    wsel = np.minimum((u[:, len(params)] * len(WEIGHT_SETS)).astype(int), len(WEIGHT_SETS) - 1)
    for i in range(n):
        row = {k: float(v[i]) for k, v in cols.items()}
        row["wsel"] = int(wsel[i])
        sets.append(row)
    return sets
