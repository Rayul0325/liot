"""AUC of a risk score for 'this proposal is harmful' (ties count one half)."""

from __future__ import annotations

import numpy as np
from scipy.stats import rankdata


def auc(score: np.ndarray, positive: np.ndarray) -> float:
    """Mann-Whitney AUC of *score* for *positive* (1-D arrays); nan if one class is empty."""
    n1 = int(positive.sum())
    n0 = positive.size - n1
    if n1 == 0 or n0 == 0:
        return float("nan")
    ranks = rankdata(score)
    return float((ranks[positive].sum() - n1 * (n1 + 1) / 2.0) / (n1 * n0))
