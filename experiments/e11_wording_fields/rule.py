"""C4.3 rule-lookup judge on the structured fields (map written by a blind agent, committed before any model run).

The map ``inputs/field_rule_map.yaml`` was written by an agent that saw neither labels, case sentences nor
model answers.  ``h = clamp(base + deltas, 0, 3)`` and the judge answers tier = TIERS[h], severity = SEVERITIES[h].
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

HERE = Path(__file__).resolve().parent
MAP_PATH = HERE / "inputs" / "field_rule_map.yaml"

KINDS = (
    "door_contact", "gas", "humidity", "lock_battery", "lock_tamper", "motion",
    "smoke", "temperature", "water_flow", "water_leak", "other",
)
RATES = ("rising_fast", "rising", "falling", "steady", "unknown")
CONCURRENT = ("0", "1", "2", "3plus")
DURATIONS = ("lt10m", "10to60m", "gt60m", "unknown")
TRIGGERS = ("sensor_reading", "proactive")
FIELDS = ("sensor_kinds", "rate", "concurrent", "duration")
_TOP = {"version", "kind_h", "base_if_kinds_absent", "rate_delta", "concurrent_delta", "duration_delta"}


class RuleMapError(ValueError):
    pass


def _table(doc: dict[str, Any], key: str, keys: tuple[str, ...], leaf: str, lo: int, hi: int) -> dict[str, int]:
    tab = doc[key]
    if set(tab) != set(keys):
        raise RuleMapError(f"{key}: keys {sorted(map(str, tab))} != {sorted(keys)}")
    out: dict[str, int] = {}
    for k in keys:
        v = tab[k]
        if not isinstance(v, dict) or set(v) != {leaf, "why"}:
            raise RuleMapError(f"{key}.{k}: need exactly {{{leaf}, why}}")
        n = v[leaf]
        if isinstance(n, bool) or not isinstance(n, int) or not lo <= n <= hi:
            raise RuleMapError(f"{key}.{k}.{leaf}={n!r} outside [{lo}, {hi}]")
        out[k] = n
    return out


def load_map(path: Path = MAP_PATH) -> dict[str, dict[str, int]]:
    """Parse and validate (no unknown keys, every value an int in range, so every field value maps to a tier)."""
    doc = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(doc, dict) or set(doc) != _TOP:
        raise RuleMapError(f"top-level keys {sorted(doc) if isinstance(doc, dict) else doc} != {sorted(_TOP)}")
    return {
        "kind_h": _table(doc, "kind_h", KINDS, "h", 0, 3),
        "base_if_kinds_absent": _table(doc, "base_if_kinds_absent", TRIGGERS, "h", 0, 3),
        "rate_delta": _table(doc, "rate_delta", RATES, "d", -1, 1),
        "concurrent_delta": _table(doc, "concurrent_delta", CONCURRENT, "d", -1, 1),
        "duration_delta": _table(doc, "duration_delta", DURATIONS, "d", -1, 1),
    }


def hazard(m: dict[str, dict[str, int]], fields: dict[str, Any], present: tuple[str, ...], trigger: str) -> int:
    """h in 0..3 from the fields named in ``present`` (a field not present contributes nothing)."""
    kinds_known = "sensor_kinds" in present
    kinds = list(fields["sensor_kinds"]) if kinds_known else []
    if kinds_known:
        base = max((m["kind_h"][k] for k in kinds), default=0)
    else:
        base = m["base_if_kinds_absent"][trigger]
    if kinds_known and not kinds and base == 0:
        return 0
    d = 0
    if "rate" in present:
        d += m["rate_delta"][fields["rate"]]
    if "concurrent" in present:
        d += m["concurrent_delta"][fields["concurrent"]]
    if "duration" in present:
        d += m["duration_delta"][fields["duration"]]
    return max(0, min(3, base + d))
