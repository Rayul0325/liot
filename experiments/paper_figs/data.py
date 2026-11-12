"""Number registry: every number a figure draws or prints is read through ``S``/``D`` and recorded.

``make_figs.py`` writes the records to ``annotations.json``; ``check.py`` reads the same
summary.json paths (or re-runs the same derived function) and asserts equality. Nothing in the
figure code types a result by hand.
"""

from __future__ import annotations

import json
from collections import defaultdict
from pathlib import Path
from typing import Any

from experiments.kit.check_numbers import load_summary, resolve
from experiments.paper_figs import derive

HERE = Path(__file__).resolve().parent
ANNOTATIONS = HERE / "annotations.json"

REG: list[dict[str, Any]] = []
META: dict[str, dict[str, Any]] = {}
USED: dict[str, set[str]] = defaultdict(set)
_cur: dict[str, str] = {"fig": ""}


def begin(fig: str) -> None:
    _cur["fig"] = fig


def summary(exp: str) -> dict[str, Any]:
    s = load_summary(exp)
    if s is None:
        raise FileNotFoundError(f"summary for {exp} missing")
    return s


def _short(exp: str, path: tuple[Any, ...]) -> str:
    tail = ">".join(str(p) for p in path[:3])
    return f"{exp}: {tail}"


def S(exp: str, *path: Any, scale: float = 1.0, agg: str | None = None) -> Any:
    """Read a value from ``<exp>/results/summary.json`` (or ``kit:<name>``) and record it."""
    raw = resolve(summary(exp), list(path), agg)
    if isinstance(raw, bool) or not isinstance(raw, (int, float)):
        value = raw
    else:
        value = raw * scale
    REG.append(
        {"fig": _cur["fig"], "kind": "summary", "exp": exp, "path": list(path), "agg": agg, "scale": scale, "value": value}
    )
    USED[_cur["fig"]].add(_short(exp, path))
    return value


def D(fn: str, *args: Any) -> Any:
    """Compute a derived number (raw files / summary values) and record the recipe."""
    value = derive.FUNCS[fn](*args)
    REG.append({"fig": _cur["fig"], "kind": "derived", "fn": fn, "args": list(args), "value": value})
    USED[_cur["fig"]].add(f"derived:{fn}")
    return value


def key_number(exp: str, path: list[Any], prefix: str) -> float:
    """Number embedded in a summary key (``after_criterion_met_p95_diff_le_0.1s`` -> 0.1)."""
    return float(D("key_number", exp, path, prefix))


def verdict(hyp_id: str, shown: str) -> str:
    REG.append({"fig": _cur["fig"], "kind": "verdict", "id": hyp_id, "value": shown})
    return shown


def meta(fig: str, number: str, title: str, caption: str, exps: str, caveats: str, skipped: str = "") -> None:
    META[fig] = {
        "number": number,
        "title": title,
        "caption": caption,
        "exps": exps,
        "caveats": caveats,
        "skipped": skipped,
    }


def dump() -> None:
    ANNOTATIONS.write_text(json.dumps(REG, ensure_ascii=False, indent=0, default=float), encoding="utf-8")
