"""Matplotlib style shared by every experiment figure.

Rules: Korean-capable font, restrained palette (one accent + greys + a few muted hues),
one fixed colour per compared policy across all figures, no emoji, no gradients.
"""

from __future__ import annotations

import warnings
from pathlib import Path
from typing import Any

import matplotlib

matplotlib.use("Agg")

import matplotlib.pyplot as plt  # noqa: E402
from matplotlib import font_manager  # noqa: E402

__all__ = [
    "ACCENT",
    "GREY",
    "GREY_DARK",
    "GREY_LIGHT",
    "POLICY_COLORS",
    "POLICY_MARKERS",
    "FONT_PRIORITY",
    "policy_color",
    "save",
    "setup",
]

ACCENT = "#1F5FA8"
GREY_DARK = "#3F3F3F"
GREY = "#8C8C8C"
GREY_LIGHT = "#D4D4D4"

FONT_PRIORITY = ("Apple SD Gothic Neo", "Malgun Gothic", "NanumSquare")

# One fixed colour per compared policy. `liot` owns the accent; the rest are greys and muted
# hues, kept distinguishable on white at line width >= 1.5.
POLICY_COLORS: dict[str, str] = {
    "liot": ACCENT,
    "flat_explicit": "#7F7F7F",
    "binary_approval": "#B3B3B3",
    "auto_all": "#B24A3E",
    "rule_only": "#3F3F3F",
    "model_only": "#C48A2C",
    "model_can_lower": "#8E5A9B",
    "wait_for_model": "#3E8E7E",
    "short_deadline": "#6E9BC5",
    "multiplicative": "#8A7F3A",
    "single_tier": "#D08C8C",
}

# Marker per policy so figures stay readable in greyscale print.
POLICY_MARKERS: dict[str, str] = {
    "liot": "o",
    "flat_explicit": "s",
    "binary_approval": "^",
    "auto_all": "X",
    "rule_only": "D",
    "model_only": "v",
    "model_can_lower": "P",
    "wait_for_model": "<",
    "short_deadline": ">",
    "multiplicative": "h",
    "single_tier": "*",
}

_chosen_font: str | None = None


def policy_color(policy: str) -> str:
    """Colour for *policy*; unknown names fall back to mid grey instead of raising."""
    return POLICY_COLORS.get(policy, GREY)


def _pick_font() -> str | None:
    available = {f.name for f in font_manager.fontManager.ttflist}
    for name in FONT_PRIORITY:
        if name in available:
            return name
    return None


def setup() -> str | None:
    """Configure matplotlib; returns the Korean font in use (None if none was found)."""
    global _chosen_font
    font = _pick_font()
    families = [font] if font else []
    families.append("DejaVu Sans")
    if font is None and _chosen_font is None:
        warnings.warn(
            "한글 글꼴(Apple SD Gothic Neo/Malgun Gothic/NanumSquare)을 찾지 못해 "
            "DejaVu Sans 로 대신합니다 — 그림의 한글이 깨질 수 있습니다.",
            stacklevel=2,
        )
    _chosen_font = font or "DejaVu Sans"
    plt.rcParams.update(
        {
            "font.family": "sans-serif",
            "font.sans-serif": families,
            "axes.unicode_minus": False,
            "figure.facecolor": "white",
            "axes.facecolor": "white",
            "axes.edgecolor": GREY,
            "axes.labelcolor": GREY_DARK,
            "axes.titlecolor": GREY_DARK,
            "axes.titleweight": "bold",
            "axes.titlesize": 12,
            "axes.labelsize": 10,
            "axes.spines.top": False,
            "axes.spines.right": False,
            "axes.grid": True,
            "grid.color": GREY_LIGHT,
            "grid.linewidth": 0.6,
            "xtick.color": GREY_DARK,
            "ytick.color": GREY_DARK,
            "legend.frameon": False,
            "lines.linewidth": 1.8,
            "axes.prop_cycle": matplotlib.cycler(color=[ACCENT, GREY, GREY_DARK, GREY_LIGHT]),
            "savefig.facecolor": "white",
        }
    )
    return font


def save(fig: Any, exp_dir: str | Path, name: str) -> Path:
    """Save *fig* as ``<exp_dir>/figures/<name>.png`` (200 dpi, tight bbox) and close it."""
    stem = name[:-4] if name.lower().endswith(".png") else name
    out = Path(exp_dir) / "figures" / f"{stem}.png"
    out.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(out, dpi=200, bbox_inches="tight")
    plt.close(fig)
    return out
