"""E16 figures (made from the summary only)."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import matplotlib.pyplot as plt
import numpy as np

from experiments.common import plotstyle


def fig_multiplier(s: dict[str, Any], exp_dir: Path) -> Path:
    plotstyle.setup()
    by = s["c412"]["grid"]["by_k"]
    ks = list(by)
    x = np.arange(len(ks))
    fig, ax = plt.subplots(1, 2, figsize=(10.5, 4.2))
    for name, col, lab in (("plain", plotstyle.GREY, "절단점 제한 없음"),
                           ("restricted", plotstyle.ACCENT, "비취약 '생명·몇 분' 칸을 최상 등급에 두는 절단점만"),
                           ("floor_safety8", "#C0504D", "하한(안전 카테고리 칸만)")):
        med = [by[k][name]["tie_loss"]["median"] for k in ks]
        q25 = [by[k][name]["tie_loss"]["p25"] for k in ks]
        q75 = [by[k][name]["tie_loss"]["p75"] for k in ks]
        ax[0].plot(x, med, "o-", color=col, label=lab)
        ax[0].fill_between(x, q25, q75, color=col, alpha=0.12)
    ax[0].axhline(s["c412"]["table_tie_loss"], color="black", ls="--", lw=1)
    ax[0].text(0.02, s["c412"]["table_tie_loss"] + 1, "운영 조회표 32쌍", fontsize=9)
    ax[0].set_ylim(30, 54)
    ax[0].set_xticks(x, [k[1:] for k in ks])
    ax[0].set_xlabel("취약 배수")
    ax[0].set_ylabel("동점 손실 (우세 쌍 156 중), 중앙값과 사분위")
    ax[0].legend(fontsize=7.5, loc="upper right")
    ax[0].set_title("동점 손실은 배수를 키워도 32 아래로 내려가지 않는다")
    share = [by[k]["plain"]["share_gas_below_critical"] for k in ks]
    ax[1].bar(x, share, color=plotstyle.GREY)
    ax[1].axvline(2.5, color="black", ls=":", lw=1)
    ax[1].set_xticks(x, [k[1:] for k in ks])
    ax[1].set_xlabel("취약 배수 (점선 오른쪽이 등록 범위 ≥ 5)")
    ax[1].set_ylabel("가스(취약 아님)가 위험 아래인 설정의 비율")
    ax[1].set_ylim(0, 1)
    ax[1].set_title("배수가 클수록 가스가 최상 점수에서 밀려난다")
    return plotstyle.save(fig, exp_dir, "c412_multiplier_tie_loss_and_gas")


def fig_weighted(s: dict[str, Any], exp_dir: Path) -> Path | None:
    c = s["c413"].get("c413a")
    if not c:
        return None
    plotstyle.setup()
    cell = c["weighted_deficit"]["exp|m0.1"]
    names = ["uniform", "coder", "coder_shrunk_half"]
    lab = ["균등(건수)", "코더 가중", "반쯤 균등 쪽"]
    fig, ax = plt.subplots(figsize=(6.6, 4.2))
    for i, n in enumerate(names):
        r = cell[n]
        m = 100 * r["mean_diff"]
        lo, hi = (100 * v for v in r["ci95_across_sets"])
        ax.errorbar(i, m, yerr=[[m - lo], [hi - m]], fmt="o", color=plotstyle.ACCENT, capsize=4)
        q25, q75 = 100 * r["set_dist"]["q25"], 100 * r["set_dist"]["q75"]
        ax.plot([i + 0.12] * 2, [q25, q75], color=plotstyle.GREY, lw=3, alpha=0.6)
    ax.axhline(0.5 * 100 * cell["uniform"]["mean_diff"], color="black", ls="--", lw=1)
    ax.text(-0.35, 0.5 * 100 * cell["uniform"]["mean_diff"] + 0.05, "건수 열세의 절반", fontsize=9)
    ax.axhline(0, color=plotstyle.GREY_LIGHT)
    ax.set_xticks(range(3), lab)
    ax.set_ylabel("liot_real - 같은 상한 명시 확인, 해로운 실행률 차이 (%p)")
    ax.set_title("m=10%, 지수 피로: 평균 (파랑=세트 간 부트스트랩 95%, 회색=사분위)")
    return plotstyle.save(fig, exp_dir, "c413a_weighted_deficit")


def fig_burden(s: dict[str, Any], exp_dir: Path) -> Path | None:
    b = s["c413"].get("c413b")
    if not b:
        return None
    plotstyle.setup()
    vars_ = [v for v in ("base", "f0", "g0", "f0g0") if f"{v}|m0.1" in b]
    fig, ax = plt.subplots(figsize=(7.2, 4.2))
    w = 0.38
    for j, (rk, lab, col) in enumerate((("R1_vs_flat_explicit", "R1 대 모두 명시 확인", plotstyle.ACCENT),
                                        ("R2_vs_flat_explicit_same_ceiling", "R2 대 같은 상한 명시 확인", "#C0504D"))):
        ys = [100 * b[f"{v}|m0.1"][f"liot_real|{rk}"]["advantage_mean"] for v in vars_]
        lo = [100 * b[f"{v}|m0.1"][f"liot_real|{rk}"]["advantage_ci95_across_sets"][0] for v in vars_]
        hi = [100 * b[f"{v}|m0.1"][f"liot_real|{rk}"]["advantage_ci95_across_sets"][1] for v in vars_]
        xs = np.arange(len(vars_)) + (j - 0.5) * w
        ax.bar(xs, ys, w, color=col, label=lab, alpha=0.85)
        ax.errorbar(xs, ys, yerr=[np.array(ys) - lo, np.array(hi) - ys], fmt="none", ecolor="black", capsize=3)
    ax.axhspan(1, 2, color=plotstyle.GREY_LIGHT, alpha=0.5)
    ax.text(len(vars_) - 0.6, 1.1, "1~2%p: 판정 불가 구간", fontsize=8)
    ax.set_xticks(range(len(vars_)), ["원래", "f=0", "g=0", "f=0, g=0"])
    ax.set_ylabel("liot_real 의 피해 이점 (%p, 양수 = 낮은 피해)")
    ax.set_title("C2.1 숨결 이하 50% 설정, m=10%: 부담을 끄면 이점이 어떻게 되나")
    ax.legend(fontsize=8)
    return plotstyle.save(fig, exp_dir, "c413b_burden_off_advantage")


def make_all(summary: dict[str, Any], exp_dir: Path) -> list[Path]:
    out = [fig_multiplier(summary, exp_dir)]
    for f in (fig_weighted, fig_burden):
        p = f(summary, exp_dir)
        if p is not None:
            out.append(p)
    return out
