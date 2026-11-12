"""E13 design: rest-length x change-day sweep (C4.6), alternative rules (C4.7), effort sweep (C4.8).

Personas and tastes are E6's (`experiments.e6_personalization.config.PERSONAS`); only the flip day,
the rest length, the rule set and the effort coefficient vary. The key of a raw record is ``KEY``;
the varied fields are stored in every record so analyze.py needs nothing from this file.
"""

from __future__ import annotations

from typing import Any

from experiments.e13_adapt_rules.rules import RuleSet
from experiments.e6_personalization import config as e6
from liot.day_sim import StochasticResident

DAYS = 60
SEEDS = tuple(range(1, 11))
KEY = ("part", "variant", "persona", "arm", "cond", "seed")

# C4.6 --------------------------------------------------------------------------------------------
RESTS = (7, 14, 30)
FLIPS = (4, 8, 15, 28, 35)
# `none_flip<F>` (no learning, no rest) is the static reference for the oracle recovery form.

# C4.7 --------------------------------------------------------------------------------------------
RULE_ARMS: dict[str, dict[str, Any]] = {
    "none": {"sim": {"learning_rate": 0.0, "reject_window_days": 0}, "rules": RuleSet()},
    "explicit_only": {"sim": {"learning_rate": 0.0}, "rules": RuleSet()},
    "lr30": {"sim": {}, "rules": RuleSet()},  # production
    "r1": {"sim": {}, "rules": RuleSet(digest_noexpire=True)},
    "r2": {"sim": {}, "rules": RuleSet(second_strike=True)},
    "r3": {"sim": {}, "rules": RuleSet(accept_lifts=True)},
    "r123": {"sim": {}, "rules": RuleSet(True, True, True)},
}
RULE_VARIANTS: dict[str, dict[str, Any]] = {"main": {}, "tray0": {"tray_open": 0.0}}

# C4.8 --------------------------------------------------------------------------------------------
EFFORTS = (0.0, 0.066, 0.132, 0.198, 0.264, 0.33)  # six equally spaced points over 0-0.33


def resident(persona: str, *, flip_day: int = e6.FLIP_DAY, **overrides: Any) -> StochasticResident:
    before, change = e6.PERSONAS[persona]
    return StochasticResident(tastes=before, flip_day=flip_day, tastes_after=change, **overrides)


def newly_helpful(persona: str) -> tuple[str, ...]:
    return e6.newly_helpful(persona)


def rest_arm(rest: int, flip: int) -> str:
    return f"rest{rest}_flip{flip}"


def effort_arm(effort: float) -> str:
    return f"eff{effort:.3f}"
