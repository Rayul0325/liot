"""Paper style (Nature/IEEE-like) for the final-report figure set.

White background, neutral greys plus ONE accent colour (LIOT), thin axes without top/right spines,
8 pt text, single column (89 mm) or double column (183 mm). Korean text is set in Apple SD Gothic
Neo (Pretendard as fallback). Results that hold by construction (구조상 결과) are always drawn with
the same hatched, greyed style so they can never be read as LIOT wins.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import matplotlib

matplotlib.use("Agg")

import matplotlib.pyplot as plt  # noqa: E402
from matplotlib import font_manager  # noqa: E402
from matplotlib.patches import Patch  # noqa: E402

MM = 1 / 25.4
W1 = 89 * MM  # single column, inches
W2 = 183 * MM  # double column, inches

# one accent colour (LIOT), neutral greys for everything else
ACCENT = "#1F5FA8"
ACCENT_TINT = "#C9D8EC"
INK = "#2B2B2B"
G_DARK = "#4A4A4A"
G_MID = "#8A8A8A"  # the alternatives (validated against the accent: normal-vision dE 19.9)
G_LIGHT = "#BDBDBD"
G_PALE = "#E6E6E6"
G_GRID = "#EDEDED"

FIG_DIR = Path(__file__).resolve().parent / "figs"

FONT_CHAIN = ("Apple SD Gothic Neo", "Pretendard", "Malgun Gothic", "NanumSquare")

STRUCT = {"facecolor": "white", "edgecolor": G_DARK, "hatch": "////", "linewidth": 0.5}
STRUCT_NOTE = "빗금·속 빈 표시 = 구조상 결과(LIOT 우위로 읽지 않음)"


def setup() -> str:
    available = {f.name for f in font_manager.fontManager.ttflist}
    chain = [n for n in FONT_CHAIN if n in available]
    if not chain:
        raise RuntimeError("no Korean-capable font found (Apple SD Gothic Neo / Pretendard)")
    plt.rcParams.update(
        {
            "font.family": "sans-serif",
            "font.sans-serif": [*chain, "DejaVu Sans"],
            "axes.unicode_minus": False,
            "pdf.fonttype": 42,
            "ps.fonttype": 42,
            "svg.fonttype": "none",
            "font.size": 8,
            "axes.labelsize": 8,
            "axes.titlesize": 8,
            "xtick.labelsize": 7.5,
            "ytick.labelsize": 7.5,
            "legend.fontsize": 7.5,
            "legend.frameon": False,
            "axes.linewidth": 0.5,
            "xtick.major.width": 0.5,
            "ytick.major.width": 0.5,
            "xtick.minor.width": 0.4,
            "ytick.minor.width": 0.4,
            "xtick.major.size": 2.5,
            "ytick.major.size": 2.5,
            "xtick.minor.size": 1.5,
            "ytick.minor.size": 1.5,
            "axes.spines.top": False,
            "axes.spines.right": False,
            "axes.edgecolor": G_DARK,
            "axes.labelcolor": INK,
            "text.color": INK,
            "xtick.color": INK,
            "ytick.color": INK,
            "axes.grid": False,
            "grid.color": G_GRID,
            "grid.linewidth": 0.4,
            "lines.linewidth": 1.0,
            "lines.markersize": 4,
            "hatch.linewidth": 0.4,
            "figure.facecolor": "white",
            "axes.facecolor": "white",
            "savefig.facecolor": "white",
            "figure.dpi": 100,
        }
    )
    return chain[0]


def panel(ax: Any, letter: str, dx: float = -0.02, dy: float = 1.04) -> None:
    """Bold lower-case panel letter at the top-left corner of *ax* (axes coordinates)."""
    ax.text(dx, dy, letter, transform=ax.transAxes, fontsize=9, fontweight="bold", ha="right", va="bottom")


def gridx(ax: Any) -> None:
    ax.grid(axis="x", color=G_GRID, linewidth=0.4)
    ax.set_axisbelow(True)


def gridy(ax: Any) -> None:
    ax.grid(axis="y", color=G_GRID, linewidth=0.4)
    ax.set_axisbelow(True)


def struct_patch(label: str = "구조상 결과") -> Patch:
    return Patch(facecolor="white", edgecolor=G_DARK, hatch="////", linewidth=0.5, label=label)


def direction(ax: Any, text: str, loc: str = "xlabel") -> None:
    """Mark the metric direction (↑ better / ↓ better) in the axis label."""
    if loc == "xlabel":
        cur = ax.get_xlabel()
        ax.set_xlabel(f"{cur}  ({text})" if cur else text)
    else:
        cur = ax.get_ylabel()
        ax.set_ylabel(f"{cur}  ({text})" if cur else text)


def save(fig: Any, stem: str) -> list[Path]:
    FIG_DIR.mkdir(parents=True, exist_ok=True)
    out = []
    pdf = FIG_DIR / f"{stem}.pdf"
    png = FIG_DIR / f"{stem}.png"
    fig.savefig(pdf, bbox_inches="tight", pad_inches=0.03)
    fig.savefig(png, dpi=300, bbox_inches="tight", pad_inches=0.03)
    plt.close(fig)
    out += [pdf, png]
    return out
