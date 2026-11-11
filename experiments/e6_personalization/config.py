"""E6 design constants: resident personas, learning arms, run variants.

Everything the raw records are keyed by lives here so run.py and analyze.py agree.
"""

from __future__ import annotations

from typing import Any

from liot.day_sim import SceneTaste, StochasticResident

DAYS = 60
FLIP_DAY = 15
SEEDS = tuple(range(1, 11))  # 10 seeds; the script, the resident draws and the sensors all follow the seed
PRE_WINDOW = (8, 14)  # days used as the "pre-flip level" (days 1-7 are warm-up)
ROLL = 7  # trailing window (days) for the recovery test
RECOVERY_FRACTION = 0.8

LIKE = SceneTaste(0.8, 0.05)  # accepts 80% when it reads and acts; dismisses 5%
HATE = SceneTaste(0.1, 0.6)  # dismisses 60% -> the production 30-day rejection gate trips
IGNORE = SceneTaste(0.1, 0.05)  # dislikes quietly: mostly no tap -> only expiry teaches the system

# Scenes that go to the phone (IMMEDIATE) are ventilate and purifier; bedtime and desk_light are
# AMBIENT and go to the digest. The digest scenes never flip.
_DIGEST_TASTES = {"bedtime": SceneTaste(0.8, 0.0), "desk_light": SceneTaste(0.5, 0.1)}

# persona -> (tastes before the flip, scenes that change on FLIP_DAY)
PERSONAS: dict[str, tuple[dict[str, SceneTaste], dict[str, SceneTaste]]] = {
    # P1 refuses the purifier loudly ([그만해]) until day 15, then wants it.
    "hater": ({"ventilate": LIKE, "purifier": HATE, **_DIGEST_TASTES}, {"purifier": LIKE}),
    # P2 ignores the purifier (no [그만해]) until day 15, then wants it.
    "ignorer": ({"ventilate": LIKE, "purifier": IGNORE, **_DIGEST_TASTES}, {"purifier": LIKE}),
    # P3 swaps: loved purifier, hated ventilation -> hated purifier, loved ventilation.
    "swap": (
        {"ventilate": HATE, "purifier": LIKE, **_DIGEST_TASTES},
        {"ventilate": LIKE, "purifier": HATE},
    ),
}

# arm -> DaySim keyword arguments (all other options stay at their defaults)
ARMS: dict[str, dict[str, Any]] = {
    # learning off AND no 30-day gate: a static system (the real "no personalisation" baseline)
    "none": {"learning_rate": 0.0, "reject_window_days": 0},
    # ADD-4: only the explicit [그만해] button (30-day gate), no implicit learning at all
    "explicit_only": {"learning_rate": 0.0},
    "lr10": {"learning_rate": 0.1},
    "lr30": {},  # production
    "lr60": {"learning_rate": 0.6},
    "permanent": {"permanent_block_on_dismiss": True},
    "no_gate": {"reject_window_days": 0},  # production learning, no 30-day gate
    "explore7": {"explore_every_days": 7},  # production + a re-offer day every 7 days
}

# variant -> (resident overrides, arms run, conditions run)
#   main    : the design above (digest opened at 22:00 with probability 0.8 - a simulator assumption)
#   tray0   : the real daemon: the digest is never delivered (findings.md), nobody opens it
#   zerofat : H4.3 control - fatigue and effort cost both zero, production arm only
#   fatonly / effonly : only one of the two mechanisms on (production arm)
#   likepure: liked scenes are never dismissed by mistake (sensitivity of the 30-day gate)
VARIANTS: dict[str, dict[str, Any]] = {
    "zerofat": {"resident": {"fatigue": 0.0, "effort_cost": 0.0}, "arms": ("lr30",),
                "conds": ("A", "B")},
    "main": {"resident": {}, "arms": tuple(ARMS), "conds": ("A", "B")},
    "tray0": {"resident": {"tray_open": 0.0}, "arms": tuple(ARMS), "conds": ("B",)},
    # H4.3 decomposition: which resident mechanism carries the A-B difference (production arm only)
    "fatonly": {"resident": {"effort_cost": 0.0}, "arms": ("lr30",), "conds": ("A", "B")},
    "effonly": {"resident": {"fatigue": 0.0}, "arms": ("lr30",), "conds": ("A", "B")},
    # likepure: the same personas, but a liked scene is never dismissed by mistake (dismiss = 0).
    # In `main` a liked scene is dismissed 5% of the time it is acted on, which trips the 30-day gate.
    "likepure": {"resident": {"_like_dismiss": 0.0},
                 "arms": ("none", "explicit_only", "lr30", "permanent", "no_gate", "explore7"),
                 "conds": ("B",)},
}


def newly_helpful(persona: str) -> tuple[str, ...]:
    """Scenes that are unhelpful before the flip and helpful after it."""
    before, change = PERSONAS[persona]
    return tuple(sorted(s for s, t in change.items() if t.helpful and not before[s].helpful))


def make_resident(persona: str, overrides: dict[str, Any]) -> StochasticResident:
    before, change = PERSONAS[persona]
    kw = dict(overrides)
    like_dismiss = kw.pop("_like_dismiss", None)
    if like_dismiss is not None:  # liked scenes (accept 0.8) get this dismiss probability
        def fix(t: SceneTaste) -> SceneTaste:
            return SceneTaste(t.accept, like_dismiss) if t.accept == LIKE.accept else t

        before = {k: fix(v) for k, v in before.items()}
        change = {k: fix(v) for k, v in change.items()}
    return StochasticResident(tastes=before, flip_day=FLIP_DAY, tastes_after=change, **kw)
