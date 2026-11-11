"""Derived statistics that are computed from summary.json cells, not stored in them.

Every function takes the parsed summary dict and returns a plain number. They exist so a figure
or a claim can quote a per-mode median without hand-copying it from RESULTS.md; the claim is
flagged as derived in REPORT_KIT.md.
"""

from __future__ import annotations

import statistics
from collections.abc import Callable
from typing import Any

__all__ = ["FUNCS", "h15_cstar_values", "h15b_operating_values"]


def h15_cstar_values(summary: dict[str, Any], blind: int) -> list[float]:
    """c* points of the E2 H1.5 cells (base alarm mix) where the policies trade off, for one mode.

    RESULTS.md counts 184 trade-off cells per mode; the 8 alarm-mix sensitivity cells are not part
    of that count, so they are excluded here too.
    """
    values: list[float] = []
    for cell in summary["h15"]["cells"].values():
        if cell["params"]["blind"] != blind or cell["params"]["mix"] != "base":
            continue
        cstar = cell["c_star"]
        if cstar["class"] == "tradeoff" and cstar["point"] is not None:
            values.append(float(cstar["point"]))
    return values


def h15b_operating_values(summary: dict[str, Any], mode: str) -> list[float | None]:
    """c* points at the E5 operating point (AUC 0.5, attack 0) for one mode; None = LIOT dominates."""
    values: list[float | None] = []
    for key, cell in summary["h15b"]["cells"].items():
        parts = key.split("|")
        if parts[-1] != "e5_operational" or mode not in parts or "a0.0" not in parts:
            continue
        cstar = cell["c_star"]
        values.append(float(cstar["point"]) if cstar["class"] == "tradeoff" and cstar["point"] is not None else None)
    return values


def _stat(values: list[float], stat: str) -> float:
    if stat == "median":
        return statistics.median(values)
    if stat == "min":
        return min(values)
    if stat == "max":
        return max(values)
    if stat == "count":
        return float(len(values))
    raise ValueError(stat)


def h15_cstar(summary: dict[str, Any], blind: int, stat: str) -> float:
    return _stat(h15_cstar_values(summary, blind), stat)


def h15b_operating_cstar(summary: dict[str, Any], mode: str, stat: str) -> float:
    return _stat([v for v in h15b_operating_values(summary, mode) if v is not None], stat)


def h15b_auc08_over2_share(summary: dict[str, Any], mode: str, attack0_only: bool = False) -> float:
    """Share of non-structural sweep cells with AUC >= 0.8 whose c* exceeds 2 (E2 C2.8).

    summary.json stores this only for the ``honest`` mode (``blind`` is stored as empty), so the
    blind figure quoted in RESULTS.md is recomputed here from ``h15b.cells``.
    A cell is structural when both the unregistered-vulnerability and observable-risk shares are 0.
    """
    total = 0
    over = 0
    for key, cell in summary["h15b"]["cells"].items():
        parts = key.split("|")
        if parts[-1] != "sweep" or mode not in parts:
            continue
        unreg = float(parts[0][1:])
        obs = float(parts[1][3:])
        auc = float(parts[2][3:])
        attack = float(parts[3][1:])
        if auc < 0.8 or (unreg == 0 and obs == 0) or (attack0_only and attack != 0):
            continue
        total += 1
        cstar = cell["c_star"]
        if cstar["class"] == "tradeoff" and cstar["point"] is not None and cstar["point"] > 2:
            over += 1
    return over / total


def h15b_auc08_cells(summary: dict[str, Any], mode: str) -> float:
    return float(
        sum(
            1
            for key in summary["h15b"]["cells"]
            if key.split("|")[-1] == "sweep"
            and mode in key.split("|")
            and float(key.split("|")[2][3:]) >= 0.8
            and not (float(key.split("|")[0][1:]) == 0 and float(key.split("|")[1][3:]) == 0)
        )
    )


def e8_modules_below(summary: dict[str, Any], block: str, key: str, threshold: float = 0.9) -> float:
    """Number of E8 modules whose ``modules.<m>.<block>.<key>`` score is below *threshold*."""
    return float(sum(1 for module in summary["modules"].values() if module[block][key] < threshold))


def e9_upward_possible(summary: dict[str, Any]) -> float:
    """E9 judge steps whose floor leaves room above (ambient + breath); ember cannot go up."""
    by_floor = summary["jev_vs_floor"]["by_floor"]
    return float(by_floor["ambient"]["n"] + by_floor["breath"]["n"])


FUNCS: dict[str, Callable[..., float]] = {
    "e9_upward_possible": e9_upward_possible,
    "e8_modules_below": e8_modules_below,
    "h15b_auc08_over2_share": h15b_auc08_over2_share,
    "h15b_auc08_cells": h15b_auc08_cells,
    "h15_cstar": h15_cstar,
    "h15b_operating_cstar": h15b_operating_cstar,
}
