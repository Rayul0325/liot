"""E11 figures (made from the summary only)."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import matplotlib.pyplot as plt

from experiments.common import plotstyle


def fig_c42(s: dict[str, Any], exp_dir: Path) -> Path:
    plotstyle.setup()
    cells = s["c42_wording"]["cells"]
    names = [
        ("neutral_on_danger", "위험 사례 + 중립 어휘\n(등록: 유지율)"),
        ("alarming_on_calm", "평온 사례 + 놀라게 하는 어휘\n(등록: 올림 증가)"),
    ]
    fig, axes = plt.subplots(1, 2, figsize=(8.6, 3.8))
    ret = cells["neutral_on_danger"]
    ci = ret["retention_normalised_ci95_case_bootstrap"] or [0, 0]
    v = ret["retention_normalised_equal_weight"] or 0.0
    axes[0].barh([0], [v], color=plotstyle.ACCENT, height=0.4)
    axes[0].errorbar([v], [0], xerr=[[max(0.0, v - ci[0])], [max(0.0, ci[1] - v)]], color=plotstyle.GREY_DARK, capsize=4)
    for x, lab in ((0.5, "반증선 0.5"), (0.8, "지지선 0.8")):
        axes[0].axvline(x, color=plotstyle.GREY, ls="--", lw=1)
        axes[0].text(x, 0.42, lab, fontsize=7, ha="center", color=plotstyle.GREY_DARK)
    axes[0].set_xlim(0, 1.3)
    axes[0].set_yticks([])
    axes[0].set_ylim(-0.5, 0.5)
    axes[0].set_title("정규화 유지율 (판정자 같은 무게)", fontsize=9)
    gain = cells["alarming_on_calm"]
    gci = gain["raise_rate_gain_ci95_case_bootstrap"] or [0, 0]
    g = gain["raise_rate_gain_equal_weight"] or 0.0
    axes[1].barh([0], [g], color=plotstyle.ACCENT, height=0.4)
    axes[1].errorbar([g], [0], xerr=[[max(0.0, g - gci[0])], [max(0.0, gci[1] - g)]], color=plotstyle.GREY_DARK, capsize=4)
    for x, lab in ((0.05, "지지선 +0.05"), (0.10, "반증선 +0.10")):
        axes[1].axvline(x, color=plotstyle.GREY, ls="--", lw=1)
        axes[1].text(x, 0.42 if x < 0.07 else 0.3, lab, fontsize=7, ha="center", color=plotstyle.GREY_DARK)
    axes[1].set_xlim(-0.1, 0.3)
    axes[1].set_yticks([])
    axes[1].set_ylim(-0.5, 0.5)
    axes[1].set_title("평온 사례 올림 증가 (비율 차이)", fontsize=9)
    fig.suptitle(f"C4.2 판정: {s['c42_wording']['verdict_c42']['label']} (두 점추정은 탐색용)", fontsize=10)
    del names
    return plotstyle.save(fig, exp_dir, "c42_retention_and_calm_gain")


def fig_c43(s: dict[str, Any], exp_dir: Path) -> Path:
    plotstyle.setup()
    v = s["c43_fields"]["verdict_c43"]
    xs, ys, lo, hi = [], [], [], []
    arms0 = s["c43_fields"]["arms"].get("rep0", {})
    for n, arm in ((1, "f_A"), (2, "f_AB"), (4, "f_ABCD")):
        a = arms0.get(arm, {})
        if a.get("pooled_returned") is None:
            continue
        xs.append(n)
        ys.append(a["pooled_returned"])
        c = a.get("pooled_returned_ci95_case_bootstrap") or [a["pooled_returned"]] * 2
        lo.append(a["pooled_returned"] - c[0])
        hi.append(c[1] - a["pooled_returned"])
    fig, ax = plt.subplots(figsize=(7.4, 4.0))
    ax.errorbar([0] + xs, [0.0] + ys, yerr=[[0] + lo, [0] + hi], marker="o", color=plotstyle.ACCENT, capsize=3,
                label="필드를 센서 종류부터 차례로 더함")
    singles = [("B", "f_B"), ("C", "f_C"), ("D", "f_D")]
    for i, (lab, arm) in enumerate(singles):
        a = arms0.get(arm, {})
        if a.get("pooled_returned") is not None:
            ax.scatter([1 + 0.12 * (i + 1)], [a["pooled_returned"]], color=plotstyle.GREY_DARK, marker="s", zorder=3)
            ax.text(1 + 0.12 * (i + 1), a["pooled_returned"] + 0.02, lab + " 단독", fontsize=7, ha="center")
    ax.axhline(0.5, color=plotstyle.GREY, ls="--", lw=1)
    ax.text(3.2, 0.52, "등록 기준 0.5", fontsize=7)
    ax.set_xticks([0, 1, 2, 4], ["0", "1", "2", "4"])
    ax.set_xlabel("더한 필드 수")
    ax.set_ylabel("돌아온 몫 (문장 대 운영 올림 차이 중)")
    ceil = s["c43_fields"]["informative_only"]["ceiling_share_reachable"]
    ax.set_title(f"C4.3 판정: {v['label']}(등록 문구), 정보 상한 약 {ceil:.2f}  (판정자 {len(v['eligible_judges_rep0'])}명, 건수 합산)", fontsize=9)
    ax.legend(fontsize=8, loc="lower right")
    return plotstyle.save(fig, exp_dir, "c43_field_curve")


def make_all(s: dict[str, Any], exp_dir: Path) -> list[Path]:
    return [fig_c42(s, exp_dir), fig_c43(s, exp_dir)]
