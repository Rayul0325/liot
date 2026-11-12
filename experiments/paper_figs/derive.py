"""Numbers that are not stored in a summary.json but computed from raw files or from summary values.

Every function here is pure and JSON-argument driven, so ``check.py`` can call it again with the
arguments recorded by ``make_figs.py`` and assert the same value comes out.
"""

from __future__ import annotations

import importlib
import math
from functools import lru_cache
from typing import Any

import numpy as np

from experiments.kit.check_numbers import EXP_DIR, load_summary, resolve

B_SEED = 31  # same seed family as experiments/e3_policy/analyze.py
NB_SETS = 1000


def _summary(exp: str) -> dict[str, Any]:
    s = load_summary(exp)
    if s is None:
        raise FileNotFoundError(exp)
    return s


# --------------------------------------------------------------------------- registered constants
def code_const(module: str, name: str) -> float:
    """A pre-registered threshold that lives in analysis code, e.g. USELESS_BOUND_46."""
    return float(getattr(importlib.import_module(module), name))


def key_number(exp: str, path: list[Any], prefix: str) -> float:
    """Number embedded in a summary key that starts with *prefix* (``..._le_0.1s`` -> 0.1)."""
    import re

    node = resolve(_summary(exp), path)
    for k in node:
        if str(k).startswith(prefix):
            found = re.findall(r"[0-9]+\.[0-9]+|[0-9]+", str(k)[len(prefix) :])
            if found:
                return float(found[-1])
    raise KeyError(prefix)


def half(exp: str, path: list[Any], key: str) -> float:
    """The 'half' of a summary key named ``..._ge_half`` (registered threshold 0.5)."""
    node = resolve(_summary(exp), path)
    if key not in node:
        raise KeyError(key)
    return 0.5


def loss_items() -> list[str]:
    """Titles of the numbered 'LIOT 가 지는 곳' items of REPORT_KIT.md (section Ⅴ-1), as written there."""
    import re

    text = (EXP_DIR / "REPORT_KIT.md").read_text(encoding="utf-8")
    start = text.index("### Ⅴ-1. LIOT 가 지는 곳")
    end = text.index("### Ⅴ-2.")
    out = []
    for line in text[start:end].splitlines():
        m = re.match(r"^(\d+)\. \*\*(.+?)\*\*", line.strip())
        if m:
            out.append(m.group(2))
    return out


def loss_count() -> float:
    return float(len(loss_items()))


# --------------------------------------------------------------------------- proportions
def wilson(exp: str, path: list[Any], n: int, which: str) -> float:
    """Wilson 95% interval bound ('lo'|'hi') of a share stored at *path* over *n* units."""
    p = float(resolve(_summary(exp), path))
    z = 1.959964
    den = 1 + z * z / n
    centre = (p + z * z / (2 * n)) / den
    half = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / den
    return centre - half if which == "lo" else centre + half


def wilson_k(k: int, n: int, which: str) -> float:
    p = k / n
    z = 1.959964
    den = 1 + z * z / n
    centre = (p + z * z / (2 * n)) / den
    half = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / den
    return centre - half if which == "lo" else centre + half


def ratio(num: float, den: float) -> float:
    return float(num) / float(den)


def sub(a: float, b: float) -> float:
    return float(a) - float(b)


def e12_cell(judge: str, scene: str, what: str) -> float:
    """E12: for one judge and one 'scene | true=.. shown=..' row, the answers equal to the shown floor.

    what = 'k' (answers equal to the shown floor), 'n' (all answers), 'share' (k / n).
    """
    row = _summary("e12_floor_anchor")["answers_by_scene_and_shown_floor"][judge][scene]
    shown = scene.rsplit("shown=", 1)[1]
    k = float(row.get(shown, 0))
    n = float(sum(row.values()))
    return {"k": k, "n": n, "share": k / n}[what]


# --------------------------------------------------------------------------- E3 raw
@lru_cache(maxsize=1)
def _e3_blocks() -> dict[str, Any]:
    from experiments.e3_policy import analyze

    return analyze.load(EXP_DIR / "e3_policy" / "raw")  # type: ignore[no-any-return]


def _boot_mean(v: np.ndarray, which: str) -> float:
    rng = np.random.default_rng(B_SEED + 2)
    idx = rng.integers(0, v.size, (NB_SETS, v.size))
    bm = v[idx].mean(1)
    return float(np.percentile(bm, 2.5 if which == "lo" else 97.5))


def e3_policy_stat(block: str, key: str, metric: str, stat: str) -> float:
    """Mean over parameter sets of a per-set ratio for one raw policy lane (+ set-bootstrap 95% bounds).

    metric: 'burden' (confirmations per day) or 'harm' (harmful-execution rate). Same per-set
    definitions as experiments/e3_policy/analyze.py ``policy_table``.
    """
    blk = _e3_blocks()[block]
    if metric == "burden":
        v = blk.grand(key, "nc") / (blk.n_seeds * blk.days)
    elif metric == "harm":
        v = blk.grand(key, "xh") / np.maximum(1, blk.n["n_harm"].sum(1))
    else:
        raise KeyError(metric)
    v = v[~np.isnan(v)]
    if stat == "mean":
        return float(v.mean())
    return _boot_mean(v, stat)


def e3_c22_effect(metric: str, m: float, stat: str) -> float:
    """liot_real minus all-explicit under the SAME proactive ceiling (E3 block c22, f > 0 sets).

    metric 'help' | 'harm' | 'burden'; stat 'mean' | 'lo' | 'hi' (bootstrap over sets, 95%).
    """
    from experiments.e3_policy import sim
    from experiments.e3_policy.analyze import key
    from experiments.e3_policy.analyze_c2 import _lane

    blk = _e3_blocks()["c22"]
    fpos = blk.p["f"] > 0
    lr = _lane(blk, key("liot_real", "exp", m))
    fc = _lane(blk, sim.Group("flat_explicit", "exp", m=m, ceil="liot").key)
    v = (lr[metric] - fc[metric])[fpos]
    v = v[~np.isnan(v)]
    if stat == "mean":
        return float(v.mean())
    return _boot_mean(v, stat)


FUNCS: dict[str, Any] = {
    "code_const": code_const,
    "key_number": key_number,
    "half": half,
    "loss_count": loss_count,
    "wilson": wilson,
    "wilson_k": wilson_k,
    "ratio": ratio,
    "sub": sub,
    "e12_cell": e12_cell,
    "e3_policy_stat": e3_policy_stat,
    "e3_c22_effect": e3_c22_effect,
}
