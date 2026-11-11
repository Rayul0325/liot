"""E4 pure functions: the input universe, scoring methods, axioms and set metrics.

Universe U24 = harm kind (4) x time-to-harm (3) x vulnerable resident (2) = 24 inputs.
Ordinal axes (low -> high): harm inconvenience < property < injury < life; urgency later <
hour < minutes; vulnerability no < yes.  "X dominates Y" = X is at least as bad on every axis
and strictly worse on one.

Axioms applied identically to every method (HYPOTHESES.md v2, H3.1):
  - monotone: if X dominates Y, level(X) must not be below level(Y)  -> violation count
  - resolution: how many distinct levels a method uses on the universe
  - tie loss: dominance pairs that end at the same level (the order is lost)

Nothing here decides which method is "right" — no independent ground truth exists.
"""

from __future__ import annotations

import itertools
from collections.abc import Sequence
from typing import Any

import numpy as np

from liot.core.severity import DAMAGE_TYPES, TIMES_TO_HARM, SeverityLevel, SeverityTable

__all__ = [
    "HARM_ORDER",
    "TIME_ORDER",
    "COMBOS",
    "N_COMBOS",
    "dominance_pairs",
    "table_levels",
    "production_cell_levels",
    "mult_scores",
    "triplet_thresholds",
    "levels_from_thresholds",
    "metrics",
    "pair_relation_disagreement",
    "category_indices",
]

# low -> high
HARM_ORDER: tuple[str, ...] = tuple(reversed(DAMAGE_TYPES))  # inconvenience, property, injury, life
TIME_ORDER: tuple[str, ...] = tuple(reversed(TIMES_TO_HARM))  # later, hour, minutes
COMBOS: tuple[tuple[int, int, int], ...] = tuple(
    itertools.product(range(len(HARM_ORDER)), range(len(TIME_ORDER)), (0, 1))
)
N_COMBOS = len(COMBOS)


def combo_name(i: int) -> str:
    h, t, v = COMBOS[i]
    return f"{HARM_ORDER[h]}/{TIME_ORDER[t]}/{'vuln' if v else 'plain'}"


def dominance_pairs() -> tuple[np.ndarray, np.ndarray]:
    """Index arrays (X, Y) for every ordered pair where X strictly dominates Y."""
    xs: list[int] = []
    ys: list[int] = []
    for i, a in enumerate(COMBOS):
        for j, b in enumerate(COMBOS):
            if i != j and all(x >= y for x, y in zip(a, b, strict=True)) and a != b:
                xs.append(i)
                ys.append(j)
    return np.array(xs, dtype=int), np.array(ys, dtype=int)


def table_levels(table: SeverityTable) -> np.ndarray:
    """Production lookup table + the production resident rule (+1, stops at CRITICAL)."""
    return production_cell_levels({k: int(v) for k, v in table.cells().items()})


def production_cell_levels(cells: dict[tuple[str, str], int]) -> np.ndarray:
    """Levels for U24 from 12 cell values (keys (damage, time)); resident = +1 capped at 3."""
    out = np.zeros(N_COMBOS, dtype=int)
    for i, (h, t, v) in enumerate(COMBOS):
        base = int(cells[(HARM_ORDER[h], TIME_ORDER[t])])
        out[i] = min(base + 1, int(SeverityLevel.CRITICAL)) if v else base
    return out


def mult_scores(
    harm_code: Sequence[float],
    time_code: Sequence[float],
    vuln_k: float,
    weights: Sequence[float] = (1.0, 1.0, 1.0),
    *,
    three_factor: bool = True,
) -> np.ndarray:
    """Product score per U24 input: harm^a * time^b * vuln^c (three_factor) or harm^a * time^b.

    Codes must be positive and strictly increasing (an ordinal coding); the vulnerable factor
    is 1 for 'plain' and vuln_k (>= 1) for 'vuln'.  With three_factor=False the vulnerability
    does not enter the score (the production +1 rule is applied afterwards by the caller).
    """
    a, b, c = weights
    out = np.zeros(N_COMBOS, dtype=float)
    for i, (h, t, v) in enumerate(COMBOS):
        s = float(harm_code[h]) ** a * float(time_code[t]) ** b
        if three_factor and v:
            s *= float(vuln_k) ** c
        out[i] = s
    return out


def triplet_thresholds(scores: np.ndarray) -> np.ndarray:
    """All (t1<t2<t3) choices among the observed distinct scores excluding the minimum.

    A threshold at value x means 'level >= k when score >= x'.  Excluding the minimum keeps
    level 0 non-empty; each threshold is an observed value so every level is non-empty.
    Returns an (m, 3) array (m = C(n-1, 3), n = #distinct scores); empty if n < 4.
    """
    d = np.unique(np.round(scores, 9))[1:]
    if d.size < 3:
        return np.zeros((0, 3))
    return np.array(list(itertools.combinations(d.tolist(), 3)), dtype=float)


def levels_from_thresholds(scores: np.ndarray, thresholds: np.ndarray) -> np.ndarray:
    """(m, n_inputs) integer levels for each threshold triplet."""
    s = np.round(scores, 9)[None, :]
    th = thresholds[:, :, None]
    return (s[:, None, :] >= th).sum(axis=1).astype(int)


def category_indices(category_rows: Sequence[dict[str, Any]]) -> dict[str, tuple[int, int]]:
    """category -> (index of plain input, index of vulnerable input) in U24."""
    out: dict[str, tuple[int, int]] = {}
    for row in category_rows:
        h = HARM_ORDER.index(row["damage"])
        t = TIME_ORDER.index(row["time"])
        out[row["category"]] = (COMBOS.index((h, t, 0)), COMBOS.index((h, t, 1)))
    return out


def metrics(
    levels: np.ndarray,
    ref: np.ndarray,
    dom_x: np.ndarray,
    dom_y: np.ndarray,
    cat_idx: np.ndarray | None = None,
    ref_cat: np.ndarray | None = None,
    critical_idx: np.ndarray | None = None,
) -> dict[str, np.ndarray]:
    """Vectorised axiom and difference metrics for an (m, 24) level matrix.

    ref is the production table (a *comparison anchor*, not ground truth).
    """
    lv = np.atleast_2d(levels)
    out: dict[str, np.ndarray] = {
        "mono_violations": (lv[:, dom_x] < lv[:, dom_y]).sum(axis=1),
        "tie_loss": (lv[:, dom_x] == lv[:, dom_y]).sum(axis=1),
        "levels_used": np.array([len(np.unique(r)) for r in lv]),
        "agree": (lv == ref[None, :]).mean(axis=1),
        "n_below_table": (lv < ref[None, :]).sum(axis=1),
        "n_above_table": (lv > ref[None, :]).sum(axis=1),
    }
    if cat_idx is not None and ref_cat is not None:
        lc = lv[:, cat_idx]
        out["cat_agree"] = (lc == ref_cat[None, :]).mean(axis=1)
        out["cat_below"] = (lc < ref_cat[None, :]).sum(axis=1)
        out["cat_above"] = (lc > ref_cat[None, :]).sum(axis=1)
        if critical_idx is not None:
            # a life-threat (table CRITICAL, plain) category rated below CRITICAL
            out["critical_cat_under"] = (lv[:, critical_idx] < 3).sum(axis=1)
    return out


def pair_relation_disagreement(a: Sequence[float], b: Sequence[float]) -> float:
    """Fraction of item pairs whose relation (<, =, >) differs between two level vectors.

    Works across different scales (tier vs severity): only the induced order matters.
    """
    n = len(a)
    if n < 2:
        return 0.0
    diff = 0
    total = 0
    for i, j in itertools.combinations(range(n), 2):
        ra = np.sign(a[i] - a[j])
        rb = np.sign(b[i] - b[j])
        diff += int(ra != rb)
        total += 1
    return diff / total


def _comb(n: int, k: int) -> int:
    if k < 0 or n < k:
        return 0
    if k == 0:
        return 1
    out = 1
    for i in range(k):
        out = out * (n - i) // (i + 1)
    return out


def n_triplets(n_distinct_scores: int) -> int:
    """Number of threshold triplets for n distinct scores (the minimum is excluded)."""
    return _comb(n_distinct_scores - 1, 3)


def pick_triplet(scores: np.ndarray, u: float) -> np.ndarray | None:
    """The threshold triplet with lexicographic rank floor(u * C(n-1, 3)); None if impossible.

    Equals ``triplet_thresholds(scores)[int(u * m)]`` without materialising all triplets.
    """
    d = np.unique(np.round(scores, 9))[1:]
    m = int(d.size)
    total = _comb(m, 3)
    if total == 0:
        return None
    rank = min(int(u * total), total - 1)
    chosen: list[float] = []
    lo = 0
    for slot in range(3):
        remaining = 3 - slot - 1
        for c in range(lo, m):
            block = _comb(m - c - 1, remaining)
            if rank < block:
                chosen.append(float(d[c]))
                lo = c + 1
                break
            rank -= block
    return np.array(chosen, dtype=float)
