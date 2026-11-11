"""Small deterministic statistics helpers (bootstrap CIs) shared by the B1 experiments."""

from __future__ import annotations

from collections.abc import Callable, Sequence

import numpy as np

B_DEFAULT = 2000
SEED = 20261112


def boot_ci(
    values: Sequence[float | None],
    stat: Callable[[np.ndarray], float] = np.median,  # type: ignore[assignment]
    *,
    b: int = B_DEFAULT,
    alpha: float = 0.05,
    seed: int = SEED,
) -> tuple[float | None, float | None, float | None]:
    """(point, lo, hi): percentile bootstrap CI of ``stat`` over ``values`` (fixed seed)."""
    arr = np.asarray([v for v in values if v is not None], dtype=float)
    if arr.size == 0:
        return None, None, None
    rng = np.random.default_rng(seed)
    idx = rng.integers(0, arr.size, size=(b, arr.size))
    boots = np.array([stat(arr[i]) for i in idx])
    return (
        float(stat(arr)),
        float(np.percentile(boots, 100 * alpha / 2)),
        float(np.percentile(boots, 100 * (1 - alpha / 2))),
    )


def p95(a: np.ndarray) -> float:
    return float(np.percentile(a, 95))


def boot_prop(
    flags: Sequence[bool], *, b: int = B_DEFAULT, seed: int = SEED
) -> dict[str, float | int | None]:
    """Proportion with a percentile bootstrap 95% CI."""
    arr = [float(f) for f in flags]
    if not arr:
        return {"n": 0, "rate": None, "lo": None, "hi": None}
    pt, lo, hi = boot_ci(arr, np.mean, b=b, seed=seed)  # type: ignore[arg-type]
    return {"n": len(arr), "rate": pt, "lo": lo, "hi": hi}


def lat_summary(values: Sequence[float | None]) -> dict[str, float | int | None]:
    """n / median / p95 (with bootstrap CIs) / max of a latency sample."""
    vals = [float(v) for v in values if v is not None]
    if not vals:
        return {"n": 0}
    med, med_lo, med_hi = boot_ci(vals, np.median)  # type: ignore[arg-type]
    q95, q95_lo, q95_hi = boot_ci(vals, p95)
    return {
        "n": len(vals),
        "median": med, "median_lo": med_lo, "median_hi": med_hi,
        "p95": q95, "p95_lo": q95_lo, "p95_hi": q95_hi,
        "max": float(max(vals)),
    }
