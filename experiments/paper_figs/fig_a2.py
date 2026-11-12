"""Style A (paper) figures 7-12: scoring methods, raise recovery, E10 injection, E12 anchoring, adaptation, mutation."""

from __future__ import annotations

from typing import Any

import matplotlib.pyplot as plt
import numpy as np
from matplotlib.lines import Line2D
from matplotlib.patches import Patch

from experiments.paper_figs import paper_style as ps
from experiments.paper_figs.data import D, S, begin, meta

E16 = "e16_scoring_harm"
E4 = "e4_scoring"
E5 = "e5_models"
E6 = "e6_personalization"
E8 = "e8_mutation"
E10 = "e10_cloud_injection"
E11 = "e11_wording_fields"
E12 = "e12_floor_anchor"
E13 = "e13_adapt_rules"
E15 = "e15_mutation_recheck"

JUDGE_NAMES = {
    "jevk5-9b-q8": "Jev K5 9B q8",
    "jevk5-4b-q8": "Jev K5 4B q8",
    "jevk5-4b-q4": "Jev K5 4B q4",
    "apus-openjev-4b-q4": "apus 4B q4",
    "jev-9b-q6": "Jev 9B q6",
    "jev-api": "Jev API",
    "haiku": "Haiku",
    "gemini": "Gemini",
    "jevk5-family": "Jev K5 계열(합)",
}
CLOUD = {"jev-api", "haiku", "gemini"}


# =============================================================================== F07 scoring methods
def f07() -> list[Any]:
    begin("F07")
    ks = ["k1.5", "k2", "k3", "k5", "k7", "k10", "k20"]
    kv = [float(k[1:]) for k in ks]
    table = S(E16, "c412", "headline", "table_tie_loss")
    best = S(E16, "c412", "grid", "groups", "k_ge_5", "restricted", "tie_loss", "min")
    rmed = S(E16, "c412", "headline", "b_restricted_median_tie_loss_k_ge_5")
    fig, axes = plt.subplots(1, 2, figsize=(ps.W2, 2.75), gridspec_kw={"wspace": 0.28, "left": 0.07, "right": 0.99, "top": 0.88, "bottom": 0.21})
    ax = axes[0]
    for off, grp, col, fc, lab in [(-0.17, "restricted", ps.G_DARK, ps.G_PALE, "제한 절단점 설정"), (0.17, "plain", ps.G_MID, "white", "제한 없는 설정")]:
        stats = []
        for _i, k in enumerate(ks):
            base = ["c412", "grid", "by_k", k, grp, "tie_loss"]
            stats.append(
                {
                    "med": S(E16, *base, "median"),
                    "q1": S(E16, *base, "p25"),
                    "q3": S(E16, *base, "p75"),
                    "whislo": S(E16, *base, "min"),
                    "whishi": S(E16, *base, "max"),
                    "fliers": [],
                }
            )
        pos = [i + off for i in range(len(ks))]
        bp = ax.bxp(stats, positions=pos, widths=0.3, showfliers=False, patch_artist=True, manage_ticks=False)
        for patch in bp["boxes"]:
            patch.set(facecolor=fc, edgecolor=col, linewidth=0.7)
        for k_ in ("whiskers", "caps"):
            for ln in bp[k_]:
                ln.set(color=col, linewidth=0.7)
        for ln in bp["medians"]:
            ln.set(color=ps.INK, linewidth=1.1)
        ax.plot([], [], "s", mfc=fc, mec=col, label=lab)
    ax.axhline(table, color=ps.ACCENT, lw=1.0)
    ax.axhline(best, color=ps.G_MID, lw=0.8, ls="--")
    ax.axhline(rmed, color=ps.G_DARK, lw=0.6, ls=":")
    xr = len(ks) - 0.35
    ax.text(xr, table + 1, f"표 {table:.0f}쌍", color=ps.ACCENT, fontsize=6.6, ha="left", va="bottom", fontweight="bold")
    ax.text(xr, best + 1, f"곱셈 최선 {best:.0f}쌍", color=ps.G_DARK, fontsize=6.3, ha="left", va="bottom")
    ax.text(xr, rmed + 1, f"제한 중앙값 {rmed:.0f}쌍", color=ps.G_DARK, fontsize=6.3, ha="left", va="bottom")
    ax.set_xticks(range(len(ks)))
    ax.set_xticklabels([f"{v:g}" for v in kv])
    ax.axvspan(2.5, len(ks) - 0.5, ymin=0, ymax=0.025, color=ps.G_MID)
    ax.set_xlim(-0.6, len(ks) + 1.35)
    ax.text(4.5, 2.5, "등록 예측 구간 k ≥ 5", ha="center", va="bottom", fontsize=6.3, color=ps.G_DARK)
    ax.set_xlabel("취약 거주자 배수 k")
    ax.set_ylabel("동점 손실 (쌍) · ↓ 적을수록 더 잘게 나눔")
    ax.set_ylim(0, 138)
    ps.gridy(ax)
    ax.legend(loc="upper left", fontsize=6.8, ncol=2, columnspacing=0.8, handletextpad=0.3, bbox_to_anchor=(0.0, 1.03))
    ps.panel(ax, "a", dx=-0.13)
    ax = axes[1]
    plain = [S(E16, "c412", "grid", "by_k", k, "plain", "share_gas_below_critical", scale=100) for k in ks]
    restr = [S(E16, "c412", "grid", "by_k", k, "restricted", "share_gas_below_critical", scale=100) for k in ks]
    floor24 = [S(E16, "c412", "grid", "by_k", k, "floor_all24", "share_gas_below_critical", scale=100) for k in ks]
    ax.plot(range(len(ks)), plain, "-o", color=ps.G_DARK, ms=3.6, lw=1.1, label="곱셈 점수, 하한 없음")
    ax.plot(range(len(ks)), restr, "--s", color=ps.G_MID, ms=3.6, mfc="white", lw=0.9, label="제한 절단점 †")
    ax.plot(range(len(ks)), floor24, ":^", color=ps.G_MID, ms=3.4, mfc="white", lw=0.9, label="24칸 전부 하한 †")
    ax.axhline(0, color=ps.ACCENT, lw=1.0)
    ax.text(0, 2.0, "조회표 0 † (표가 칸마다 명시)", color=ps.ACCENT, fontsize=6.6, ha="left", va="bottom")
    ax.set_xticks(range(len(ks)))
    ax.set_xticklabels([f"{v:g}" for v in kv])
    ax.set_xlabel("취약 거주자 배수 k")
    ax.set_ylabel("가스가 위험 등급 아래로 내려간 설정 (%) · ↓")
    ax.set_ylim(-3, 100)
    ps.gridy(ax)
    ax.legend(loc="center left", bbox_to_anchor=(0.0, 0.62), fontsize=6.8)
    ps.panel(ax, "b", dx=-0.13)
    ax.text(len(ks) - 0.6, 16, "† 구조상 결과: max() 로 하한 아래가 될 수 없음", fontsize=6.4, ha="right", color=ps.G_DARK, bbox={"fc": "white", "ec": ps.G_MID, "lw": 0.4, "pad": 2, "hatch": "////"})
    share_below = S(E16, "c412", "headline", "b_restricted_share_below_table_k_ge_5", scale=100)
    unr = S(E16, "c412", "headline", "b_unrestricted_gas_below_critical_share_k_ge_5", scale=100)
    meta(
        "F07",
        "그림 7",
        "E4·E16 곱셈 점수의 동점 손실 분포와 가스 위험 하한",
        f"a: 취약 거주자 배수 k 별 곱셈 설정의 동점 손실 분포(상자=사분위, 수염=최솟값~최댓값). 곱셈의 최선 설정({best:.0f}쌍)은 조회표({table:.0f}쌍)보다 등급을 잘게 나누지만 "
        f"k≥5 제한 설정의 중앙값은 {rmed:.0f}쌍으로 등록 예측(표 아래로 내려감)과 달랐고, 표보다 적은 설정은 {share_below:.1f}%다. "
        f"b: 하한 없는 곱셈은 k≥5 에서 설정의 {unr:.1f}%가 가스를 위험 아래로 매기며, 하한을 붙인 변형의 0은 구조상 결과다.",
        "E16 (c412), E4",
        "동점 손실은 위험 등급에 독립 정답이 없어 표 기준의 순환적 지표이며, 더 잘게 나눈다고 더 옳은 것은 아니다. 설정 수는 우리가 고른 척도·절단점 범위에 따라 달라진다. "
        "분포는 summary 의 사분위·극값 요약으로 그렸다(전체 히스토그램은 summary 에 없음).",
    )
    return ps.save(fig, "fig07_scoring_methods")


# =============================================================================== F08 raise recovery
def f08() -> list[Any]:
    begin("F08")
    judges = ["jevk5-9b-q8", "jevk5-4b-q8", "jevk5-4b-q4", "apus-openjev-4b-q4", "jev-9b-q6", "jev-api", "haiku"]
    rows = []
    for j in judges:
        base = ["h51_sentence_vs_operational", j, "rep0"]
        rows.append(
            (
                j,
                S(E5, *base, "rate_sentence"),
                S(E5, *base, "rate_operational"),
                S(E5, *base, "diff_sentence_minus_operational"),
                S(E5, *base, "diff_ci95_case_bootstrap", 0),
                S(E5, *base, "diff_ci95_case_bootstrap", 1),
            )
        )
    rows.sort(key=lambda r: r[3])
    fig = plt.figure(figsize=(ps.W2, 3.0))
    gs = fig.add_gridspec(1, 3, width_ratios=[1.2, 0.8, 1.35], wspace=0.95, left=0.1, right=0.99, top=0.88, bottom=0.2)
    ax = fig.add_subplot(gs[0, 0])
    ys = np.arange(len(rows))
    for y, (_j, rs, ro, _d, _lo, _hi) in zip(ys, rows, strict=True):
        ax.plot([ro, rs], [y, y], color=ps.G_LIGHT, lw=1.6, zorder=1)
        ax.plot([ro], [y], "o", ms=4.6, mfc="white", mec=ps.G_DARK, mew=1.0, zorder=3)
        ax.plot([rs], [y], "o", ms=4.6, mfc=ps.INK, mec=ps.INK, zorder=3)
    ax.set_yticks(ys)
    ax.set_yticklabels([JUDGE_NAMES[r[0]] + (" (클라우드)" if r[0] in CLOUD else "") for r in rows])
    ax.set_xlim(-0.03, 0.3)
    ax.set_xlabel("하한 위로 올린 비율 (61문항)")
    ps.gridx(ax)
    ax.plot([], [], "o", mfc=ps.INK, mec=ps.INK, label="상황 문장 맥락")
    ax.plot([], [], "o", mfc="white", mec=ps.G_DARK, label="운영 집계 요약 맥락 †")
    ax.legend(loc="upper center", bbox_to_anchor=(0.5, -0.2), ncol=2, fontsize=6.6, handletextpad=0.2, columnspacing=0.8)
    ps.panel(ax, "a", dx=-0.62)
    ax = fig.add_subplot(gs[0, 1])
    for y, (_j, _rs, _ro, d, lo, hi) in zip(ys, rows, strict=True):
        ax.plot([lo, hi], [y, y], color=ps.G_DARK, lw=1.3, solid_capstyle="butt")
        ax.plot([d], [y], "o", ms=4.4, mfc=ps.G_DARK, mec=ps.G_DARK)
    ax.axvline(0, color=ps.G_DARK, lw=0.6, ls="--")
    ax.set_yticks(ys)
    ax.set_yticklabels([])
    ax.set_xlim(-0.02, 0.4)
    ax.set_xlabel("올림 비율 차이 (문장 - 운영)\n(사례 부트스트랩 95%)")
    ps.gridx(ax)
    ps.panel(ax, "b", dx=-0.07)
    # c: recovered share
    ax = fig.add_subplot(gs[0, 2])
    half = D("half", E5, ["c25_structured", "rep0"], "returned_ge_half")
    ceiling = S(E11, "c43_fields", "informative_only", "ceiling_share_reachable")
    items = [
        ("관측값 2개 (E5)", S(E5, "c25_structured", "rep0", "pooled", "returned_fraction"), S(E5, "c25_structured", "rep0", "pooled", "returned_fraction_ci95_case_bootstrap", 0), S(E5, "c25_structured", "rep0", "pooled", "returned_fraction_ci95_case_bootstrap", 1), False),
        ("상황 종류 코드 (E5)", S(E5, "c31_subtype", "rep0", "pooled", "returned_fraction"), S(E5, "c31_subtype", "rep0", "pooled", "returned_fraction_ci95_case_bootstrap", 0), S(E5, "c31_subtype", "rep0", "pooled", "returned_fraction_ci95_case_bootstrap", 1), False),
        ("필드 1개 (E11)", S(E11, "c43_fields", "arms", "rep0", "f_A", "pooled_returned"), S(E11, "c43_fields", "arms", "rep0", "f_A", "pooled_returned_ci95_case_bootstrap", 0), S(E11, "c43_fields", "arms", "rep0", "f_A", "pooled_returned_ci95_case_bootstrap", 1), False),
        ("필드 2개 (E11)", S(E11, "c43_fields", "arms", "rep0", "f_AB", "pooled_returned"), S(E11, "c43_fields", "arms", "rep0", "f_AB", "pooled_returned_ci95_case_bootstrap", 0), S(E11, "c43_fields", "arms", "rep0", "f_AB", "pooled_returned_ci95_case_bootstrap", 1), False),
        ("필드 4개 (E11)", S(E11, "c43_fields", "arms", "rep0", "f_ABCD", "pooled_returned"), S(E11, "c43_fields", "arms", "rep0", "f_ABCD", "pooled_returned_ci95_case_bootstrap", 0), S(E11, "c43_fields", "arms", "rep0", "f_ABCD", "pooled_returned_ci95_case_bootstrap", 1), False),
        ("필드 4개, 정보 있는 사례만", S(E11, "c43_fields", "informative_only", "arms", "f_ABCD", "pooled_returned"), S(E11, "c43_fields", "informative_only", "arms", "f_ABCD", "pooled_returned_ci95_case_bootstrap", 0), S(E11, "c43_fields", "informative_only", "arms", "f_ABCD", "pooled_returned_ci95_case_bootstrap", 1), True),
    ]
    ys = np.arange(len(items))[::-1]
    ax.axvspan(ceiling, 1.0, facecolor="white", edgecolor=ps.G_MID, hatch="////", linewidth=0)
    for y, (_lab, v, lo, hi, post) in zip(ys, items, strict=True):
        col = ps.G_MID if post else ps.INK
        ax.plot([lo, hi], [y, y], color=col, lw=1.4, solid_capstyle="butt")
        ax.plot([v], [y], "o", ms=4.6, mfc="white" if post else col, mec=col, mew=1.0)
        ax.text(v if post else max(hi, v) + 0.015, y + (0.3 if post else 0), f"{v:.3f}", fontsize=6.4, va="bottom" if post else "center", ha="center" if post else "left", color=ps.G_DARK)
    ax.axvline(half, color=ps.G_DARK, lw=0.8, ls="--")
    ax.axvline(ceiling, color=ps.G_DARK, lw=0.8)
    ax.text(half - 0.01, len(items) - 0.35, f"등록 예측 ≥ {half:g}", fontsize=6.4, ha="right", va="bottom", color=ps.G_DARK)
    ax.text(ceiling + 0.01, len(items) - 0.35, f"정보 상한 ≈ {ceiling:.2f}\n(도달 불가 구간)", fontsize=6.2, ha="left", va="bottom", color=ps.G_DARK)
    ax.set_yticks(ys)
    ax.set_yticklabels([r[0] for r in items], fontsize=6.6)
    ax.set_xlim(-0.2, 1.0)
    ax.set_ylim(-0.6, len(items) + 0.55)
    ax.set_xlabel("문장 대 운영 올림 차이 중 돌아온 몫 · ↑\n(사례 부트스트랩 95%)")
    ps.gridx(ax)
    ps.panel(ax, "c", dx=-1.02)
    ro = rows[0]
    raise_sum_s = S(E5, "c25_structured", "rep0", "pooled", "raised_sentence")
    raise_sum_o = S(E5, "c25_structured", "rep0", "pooled", "raised_operational")
    del ro
    meta(
        "F08",
        "그림 8",
        "E5·E11 올림 회복: 문장 대 운영, 필드를 더하면 돌아오나",
        f"a, b: 판정자마다 상황 문장 맥락에서는 하한 위로 올리지만(7명 합 {raise_sum_s:.0f}건) 운영 집계 요약에서는 거의 사라진다({raise_sum_o:.0f}건). 이 소멸은 요약에 상황 내용이 없어 평온·위험 문장이 같은 입력이 되는 구조상 결과(†)다. "
        f"c: 요약에 관측값·상황 종류 코드·필드 1/2/4개를 더해도 돌아온 몫은 등록 예측 {half:g}에 크게 못 미치며, 이 시험이 도달할 수 있는 상한 자체가 약 {ceiling:.2f}다.",
        "E5 (h51, c25, c31), E11 (c43)",
        "문항이 작고(주입 제외 61문항) 판정자는 같은 문항을 공유해 독립이 아니다. 로컬 모델은 글자 확률, 클라우드는 다른 인터페이스라 같은 측정이 아니다. 정보 있는 사례만 보면 4개 필드의 구간이 0.5 를 포함해 판정 불가이며(사후 읽기), 필드 4개 반증은 등록 문구대로다.",
    )
    return ps.save(fig, "fig08_raise_recovery")


# =============================================================================== F09 E10 injection
def f09() -> list[Any]:
    begin("F09")
    order = ["jevk5-9b-q8", "jevk5-4b-q8", "jevk5-4b-q4", "apus-openjev-4b-q4", "jev-9b-q6", "jev-api", "haiku", "gemini"]
    rows = []
    for j in order:
        base = ["c41", "per_judge", j]
        rows.append((j, S(E10, *base, "flip_rate"), S(E10, *base, "flip_ci95", 0), S(E10, *base, "flip_ci95", 1)))
    rows.sort(key=lambda r: (r[0] in CLOUD, -r[1]))
    grp = [
        ("로컬 5종 (Jev K5 는 1로 합산)", S(E10, "c41", "groups", "local", "flip_rate"), S(E10, "c41", "groups", "local", "flip_ci95", 0), S(E10, "c41", "groups", "local", "flip_ci95", 1), "local"),
        ("클라우드 3종", S(E10, "c41", "groups", "cloud", "flip_rate"), S(E10, "c41", "groups", "cloud", "flip_ci95", 0), S(E10, "c41", "groups", "cloud", "flip_ci95", 1), "cloud"),
    ]
    fig = plt.figure(figsize=(ps.W2, 3.0))
    gs = fig.add_gridspec(1, 2, width_ratios=[1.45, 1], wspace=0.55, left=0.2, right=0.985, top=0.88, bottom=0.22)
    ax = fig.add_subplot(gs[0, 0])
    labels, vals = [], []
    ytop = 0
    entries = [(JUDGE_NAMES[j] + (" (클라우드)" if j in CLOUD else " (로컬)"), v, lo, hi, "cloud" if j in CLOUD else "local") for j, v, lo, hi in rows]
    entries = [(g[0], g[1], g[2], g[3], g[4]) for g in grp] + entries
    ys = np.arange(len(entries))[::-1].astype(float)
    ys = np.where(np.arange(len(entries)) >= 2, ys - 0.6, ys)
    for y, (lab, v, lo, hi, kind) in zip(ys, entries, strict=True):
        col = ps.ACCENT if kind == "local" else ps.G_MID
        grp_row = lab.startswith("로컬 5") or lab.startswith("클라우드 3")
        ax.barh(y, v, height=0.62 if not grp_row else 0.72, color=col if grp_row else "white", edgecolor=col, linewidth=0.9, alpha=1.0 if grp_row else 1.0)
        if not grp_row:
            ax.barh(y, v, height=0.62, color=col, alpha=0.35, linewidth=0)
        ax.plot([lo, hi], [y, y], color=ps.INK, lw=1.0, solid_capstyle="butt")
        ax.text(max(hi, v) + 0.012, y, f"{v:.3f}", fontsize=6.6, va="center", color=ps.G_DARK)
        labels.append(lab)
        vals.append(v)
        ytop = max(ytop, y)
    ax.set_yticks(ys)
    ax.set_yticklabels(labels)
    for tl, e in zip(ax.get_yticklabels(), entries, strict=True):
        if e[0].startswith("로컬 5") or e[0].startswith("클라우드 3"):
            tl.set_fontweight("bold")
    ax.set_xlim(0, 0.62)
    ax.set_xlabel("주입 변형이 붙어 하한 아래로 뒤집힌 비율 · ↓ 낮을수록 좋음\n(짝지은 뒤집힘, 35개 기본 상황, 95% 구간)")
    ps.gridx(ax)
    ax.axhline(ys[1] - 0.5 - 0.0, color=ps.G_LIGHT, lw=0.5)
    ps.panel(ax, "a", dx=-0.9)
    handles = [Patch(facecolor=ps.ACCENT, label="로컬 판정자 (LIOT 는 로컬 우선 설계)"), Patch(facecolor=ps.G_MID, label="클라우드 판정자 (비교 대안)")]
    ax.legend(handles=handles, loc="lower right", fontsize=6.6)
    ax = fig.add_subplot(gs[0, 1])
    diff = S(E10, "c41", "difference_local_minus_cloud", "point")
    dlo = S(E10, "c41", "difference_local_minus_cloud", "ci95", 0)
    dhi = S(E10, "c41", "difference_local_minus_cloud", "ci95", 1)
    nb = ["c41", "robustness", "net_effect"]
    n1 = S(E10, *nb, "case_then_slot_bootstrap", "difference_local_minus_cloud")
    n1lo = S(E10, *nb, "case_then_slot_bootstrap", "difference_ci95", 0)
    n1hi = S(E10, *nb, "case_then_slot_bootstrap", "difference_ci95", 1)
    n2lo = S(E10, *nb, "crossed_bootstrap", "difference_ci95", 0)
    n2hi = S(E10, *nb, "crossed_bootstrap", "difference_ci95", 1)
    thr = D("code_const", "experiments.e10_cloud_injection.analyze", "EFFECT_MIN")
    rows2 = [
        ("뒤집힘 비율 차이\n(등록 지표)", diff, dlo, dhi, ps.ACCENT),
        ("순효과 차이\n(사례→변형 부트스트랩)", n1, n1lo, n1hi, ps.G_DARK),
        ("순효과 차이\n(교차 부트스트랩)", n1, n2lo, n2hi, ps.G_DARK),
    ]
    ys2 = [2, 1, 0]
    for y, (_lab, v, lo, hi, col) in zip(ys2, rows2, strict=True):
        ax.plot([lo, hi], [y, y], color=col, lw=1.6, solid_capstyle="butt")
        ax.plot([v], [y], "o", ms=4.8, mfc=col, mec=col)
        ax.text(0.01, y + 0.22, f"{v:.3f} [{lo:.3f}, {hi:.3f}]", fontsize=6.2, va="bottom", ha="left", color=ps.G_DARK)
    ax.axvline(0, color=ps.G_DARK, lw=0.6)
    ax.axvline(thr, color=ps.G_MID, lw=0.9, ls="--")
    ax.text(thr + 0.005, -0.62, f"문턱 {thr:.2f}", fontsize=6.4, color=ps.G_DARK, va="bottom")
    ax.set_yticks(ys2)
    ax.set_yticklabels([r[0] for r in rows2], fontsize=6.6)
    ax.set_xlim(-0.02, 0.62)
    ax.set_ylim(-0.7, 2.7)
    ax.set_xlabel("로컬 - 클라우드 차이")
    ps.gridx(ax)
    ps.panel(ax, "b", dx=-0.62)
    cl = S(E10, "c41", "groups", "cloud", "flip_rate")
    lc = S(E10, "c41", "groups", "local", "flip_rate")
    meta(
        "F09",
        "그림 9",
        "E10 주입(낮춰라) 짝지은 뒤집힘 비율: 클라우드 3종 대 로컬",
        f"주입 변형이 붙어 하한 아래로 뒤집힌 비율은 클라우드 {cl:.3f}, 로컬 {lc:.3f}으로 로컬이 훨씬 잘 흔들린다(차이 {diff:.3f}, 95% 구간 [{dlo:.3f}, {dhi:.3f}]). 가장 낮은 로컬 판정자도 가장 높은 클라우드 판정자보다 높다. "
        f"그러나 이것은 이 3개 대 이 5개의 비교라 원인이 클라우드 여부인지는 분리하지 못했고, 순효과로 읽으면 차이의 구간 하한({n1lo:.3f})이 문턱 {thr:.2f} 아래다. LIOT 는 로컬 우선 설계이므로 이 결과는 LIOT 쪽의 약점이다.",
        "E10 (c41)",
        "원인(클라우드 여부)은 분리되지 않았다. 판정자 3 대 5, 초안 라벨, 어휘가 문장을 보고 만들어졌다는 한계가 있다. 로컬은 글자 확률, 클라우드는 텍스트 답이라 인터페이스가 다르다. Jev K5 계열 3개는 하나의 판정자로 합산해 로컬 합계를 만든다.",
    )
    return ps.save(fig, "fig09_e10_injection")


# =============================================================================== F10 E12 anchoring
def f10() -> list[Any]:
    begin("F10")
    judges = ["jev", "haiku", "gemini"]
    jn = {"jev": "Jev", "haiku": "Haiku", "gemini": "Gemini"}
    from experiments.paper_figs.data import summary

    scenes = sorted(summary(E12)["answers_by_scene_and_shown_floor"]["jev"].keys())
    fig = plt.figure(figsize=(ps.W2, 3.35))
    gs = fig.add_gridspec(1, 3, width_ratios=[1.35, 1.1, 0.9], wspace=0.5, left=0.215, right=0.99, top=0.9, bottom=0.17)
    ax = fig.add_subplot(gs[0, 0])
    mat = np.zeros((len(scenes), len(judges)))
    for i, sc in enumerate(scenes):
        for j, jd in enumerate(judges):
            k = D("e12_cell", jd, sc, "k")
            n = D("e12_cell", jd, sc, "n")
            sh = D("e12_cell", jd, sc, "share")
            mat[i, j] = sh
            ax.add_patch(plt.Rectangle((j - 0.5, i - 0.5), 1, 1, facecolor=_blend(sh), edgecolor="white", lw=1))
            ax.text(j, i, f"{k:.0f}/{n:.0f}", ha="center", va="center", fontsize=6.4, color="white" if sh > 0.55 else ps.INK)
    ax.set_xlim(-0.5, len(judges) - 0.5)
    ax.set_ylim(len(scenes) - 0.5, -0.5)
    ax.set_xticks(range(len(judges)))
    ax.set_xticklabels([jn[j] for j in judges])
    ax.xaxis.tick_top()
    ax.set_yticks(range(len(scenes)))
    ax.set_yticklabels([_scene_label(s) for s in scenes], fontsize=6.2)
    for sp in ("left", "bottom", "top"):
        ax.spines[sp].set_visible(False)
    ax.tick_params(length=0)
    ax.set_xlabel("칸 = 답이 '보여 준 하한'과 같은 단계 수 / 전체\n(색이 짙을수록 보여 준 하한을 따라 씀)", fontsize=6.8)
    ps.panel(ax, "a", dx=-1.55, dy=1.08)
    ax = fig.add_subplot(gs[0, 1])
    ys = np.arange(len(judges))[::-1]
    for y, jd in zip(ys, judges, strict=True):
        base = ["departure_true_floor", jd]
        at = S(E12, *base, "at_floor")
        below = S(E12, *base, "below")
        above = S(E12, *base, "above")
        tot = S(E12, *base, "answered")
        ax.barh(y, at, color=ps.G_PALE, edgecolor=ps.G_MID, linewidth=0.5, height=0.55)
        ax.barh(y, below, left=at, color=ps.INK, height=0.55)
        ax.barh(y, above, left=at + below, color=ps.ACCENT, height=0.55)
        ax.text(at / 2, y, f"{at:.0f}", fontsize=6.6, ha="center", va="center", color=ps.INK)
        if below > 0:
            ax.text(at + below / 2, y + 0.4, f"아래 {below:.0f}", fontsize=6.4, ha="center", va="bottom")
        ax.text(tot + 2, y, f"위 {above:.0f}", fontsize=6.6, ha="left", va="center", color=ps.ACCENT, fontweight="bold")
    ax.set_yticks(ys)
    ax.set_yticklabels([jn[j] for j in judges])
    ax.set_xlim(0, 135)
    ax.set_ylim(-0.6, 2.7)
    up_steps = S(E12, "steps", "can_depart_upward")
    tot_steps = S(E12, "steps", "total")
    ax.set_xlabel(f"참 하한 대비 답의 위치 (단계 수, 전체 {tot_steps:.0f})")
    ps.gridx(ax)
    ax.legend(handles=[Patch(facecolor=ps.G_PALE, edgecolor=ps.G_MID, label="하한과 같음"), Patch(facecolor=ps.INK, label="하한 아래"), Patch(facecolor=ps.ACCENT, label="하한 위(올림)")], loc="upper center", bbox_to_anchor=(0.5, -0.2), fontsize=6.4, ncol=1)
    ps.panel(ax, "b", dx=-0.25, dy=1.0)
    ax = fig.add_subplot(gs[0, 2])
    for y, jd in zip(ys, judges, strict=True):
        base = ["anchoring", jd, "pooled_moved_with"]
        v = S(E12, *base, "rate")
        lo = S(E12, *base, "lo")
        hi = S(E12, *base, "hi")
        ax.plot([lo, hi], [y, y], color=ps.G_DARK, lw=1.4, solid_capstyle="butt")
        ax.plot([v], [y], "o", ms=4.8, mfc=ps.INK, mec=ps.INK)
        ax.text(0.5, y + 0.28, f"{v:.2f}", fontsize=6.6, ha="center", va="bottom", color=ps.G_DARK)
    ax.set_yticks(ys)
    ax.set_yticklabels([])
    ax.set_xlim(-0.03, 1.05)
    ax.set_ylim(-0.6, 2.7)
    ax.set_xlabel("보여 준 하한을 따라간 몫\n(장면 종류 5개 군집 구간)")
    ps.gridx(ax)
    ps.panel(ax, "c", dx=-0.1, dy=1.0)
    j_up = S(E12, "departure_true_floor", "jev", "above")
    h_dn = S(E12, "departure_true_floor", "haiku", "below")
    meta(
        "F10",
        "그림 10",
        "E12 하한 고정(anchoring): 판정자 × 보여 준 하한, 이탈 개수",
        f"a: 장면(행)마다 같은 단계에 참 하한·한 칸 위·한 칸 아래를 증거로 바꿔 보여 줬을 때 답이 보여 준 하한과 같아진 단계의 수. 세 판정자 모두 필드의 하한 값을 따라 쓰는 경향이 있어 '하한을 이해하고 존중'이 아니라 선택지와 같은 낱말을 따라 쓴 echo 로 읽는다. "
        f"b: 위로 벗어날 수 있었던 {up_steps:.0f}단계에서 세 판정자의 올림은 {j_up:.0f}건(Jev)·0건·0건이고, Haiku 의 아래 이탈은 {h_dn:.0f}단계다.",
        "E12",
        "장면 종류는 5개뿐이고 군집 구간이 넓다(군집 3~5개). Jev 가 낀 쌍의 일치는 Jev 가 하한을 거의 벗어나지 않아 구조상 결과이며, 장면이 실제로 그 하한을 요구하는 것일 수 있다. 이 시험만으로 모델 상향 경로가 쓸모없다고 단정하지 않는다.",
    )
    return ps.save(fig, "fig10_e12_anchoring")


def _blend(share: float) -> Any:
    base = np.array([1.0, 1.0, 1.0])
    acc = np.array([0x1F, 0x5F, 0xA8]) / 255
    return tuple(base * (1 - share) + acc * share)


_SCENE_KO = {
    "alert": "경보",
    "set_brightness": "밝기 조절",
    "set_speed": "풍속 조절",
    "ventilate": "환기",
    "gas": "가스",
    "leak": "누수",
    "light": "조명",
    "climate": "냉난방",
    "info": "정보",
    "ambient": "잔잔",
    "breath": "숨결",
    "dusk": "땅거미",
    "ember": "불씨",
}


def _scene_label(s: str) -> str:
    head, tail = s.split(" | ")
    act, cat = head.split("|")
    true = tail.split("true=")[1].split(" ")[0]
    shown = tail.split("shown=")[1]
    return f"{_SCENE_KO[act]}·{_SCENE_KO[cat]}  참 {_SCENE_KO[true]} → 보여줌 {_SCENE_KO[shown]}"


# =============================================================================== F11 adaptation
def f11() -> list[Any]:
    begin("F11")
    fig = plt.figure(figsize=(ps.W2, 3.15))
    gs = fig.add_gridspec(1, 3, width_ratios=[1.0, 1.05, 1.1], wspace=0.55, left=0.105, right=0.99, top=0.88, bottom=0.2)
    arms = [
        ("none", "학습 없음"),
        ("explicit_only", "버튼만 ([그만해])"),
        ("lr10", "학습률 10%"),
        ("lr30", "운영 학습 (30%)"),
        ("lr60", "학습률 60%"),
        ("explore7", "7일 탐색"),
        ("permanent", "영구 차단 †"),
        ("no_gate", "게이트 없음"),
    ]
    ax = fig.add_subplot(gs[0, 0])
    ys = np.arange(len(arms))[::-1]
    for y, (arm, _lab) in zip(ys, arms, strict=True):
        base = ["pooled", f"main|{arm}|B", "d1_60", "acc_help"]
        v = S(E6, *base, "mean")
        lo = S(E6, *base, "lo")
        hi = S(E6, *base, "hi")
        liot = arm == "lr30"
        struct = arm == "permanent"
        col = ps.ACCENT if liot else ps.G_MID
        ax.plot([lo, hi], [y, y], color=col, lw=1.4, solid_capstyle="butt")
        ax.plot([v], [y], "o", ms=4.8, mfc="white" if struct else col, mec=col, mew=1.0)
        ax.text(hi + 0.03, y, f"{v:.2f}", fontsize=6.4, va="center", color=ps.G_DARK)
    ax.set_yticks(ys)
    ax.set_yticklabels([a[1] for a in arms], fontsize=7)
    ax.get_yticklabels()[3].set_color(ps.ACCENT)
    ax.get_yticklabels()[3].set_fontweight("bold")
    ax.set_xlim(0, 1.45)
    ax.set_xlabel("하루 수락된 도움 (건/일) · ↑\n(60일 평균, 95% 구간)")
    ps.gridx(ax)
    ps.panel(ax, "a", dx=-0.62)
    # b rest-period sweep
    ax = fig.add_subplot(gs[0, 1])
    flips = [4, 8, 15, 28, 35]
    bound = D("code_const", "experiments.e13_adapt_rules.analyze", "USELESS_BOUND_46")
    for off, rest, col, mk, lab in [(-0.9, "rest7", ps.ACCENT, "o", "쉬는 기간 7일 - 30일"), (0.9, "rest14", ps.G_MID, "s", "쉬는 기간 14일 - 30일")]:
        xs, ys_, lo, hi = [], [], [], []
        for d in flips:
            base = ["c46", "contrast", f"all|flip{d}|{rest}-rest30", "offered_unhelp_imm", "d1_60"]
            xs.append(d + off)
            ys_.append(S(E13, *base, "mean"))
            lo.append(S(E13, *base, "lo"))
            hi.append(S(E13, *base, "hi"))
        ax.errorbar(xs, ys_, yerr=[np.subtract(ys_, lo), np.subtract(hi, ys_)], fmt="none", ecolor=col, elinewidth=1.2, capsize=2)
        ax.plot(xs, ys_, mk, color=col, ms=4.2, mfc=col if rest == "rest7" else "white", label=lab)
    ax.axhline(bound, color=ps.INK, lw=0.8, ls="--")
    ax.text(0.5, bound + 0.003, f"등록 기준 {bound:g}", fontsize=6.4, ha="left", va="bottom")
    ax.axhline(0, color=ps.G_DARK, lw=0.5)
    ax.set_xticks(flips)
    ax.set_xlim(0, 40)
    ax.set_xlabel("선호가 바뀌는 날")
    ax.set_ylabel("쓸데없는 알림 차이 (건/일) · ↓")
    ps.gridy(ax)
    ax.legend(loc="lower left", bbox_to_anchor=(-0.02, 0.99), fontsize=6.3, handletextpad=0.2, ncol=1, labelspacing=0.2)
    ax.plot([15 - 0.9], [S(E13, "c46", "contrast", "all|flip15|rest7-rest30", "offered_unhelp_imm", "d1_60", "mean")], "o", ms=9, mfc="none", mec=ps.INK, mew=0.7)
    ax.set_ylim(0, 0.095)
    ps.panel(ax, "b", dx=-0.2)
    # c tray0 ratio forest
    ax = fig.add_subplot(gs[0, 2])
    sup = D("code_const", "experiments.e13_adapt_rules.analyze", "SUPPORT_RATIO")
    ref = D("code_const", "experiments.e13_adapt_rules.analyze", "REFUTE_RATIO")
    rows = [
        ("(1) · 열람 80%", "main|all|r1", False),
        ("(1) · tray0", "tray0|all|r1", False),
        ("(2) · tray0", "tray0|all|r2", False),
        ("(3) · tray0 †", "tray0|all|r3", True),
        ("(1)(2)(3) · tray0", "tray0|all|r123", False),
    ]
    ys = np.arange(len(rows))[::-1]
    for y, (_lab, key, struct) in zip(ys, rows, strict=True):
        base = ["c47", "vs_none", key, "d31_60"]
        v = S(E13, *base, "ratio")
        lo = S(E13, *base, "lo")
        hi = S(E13, *base, "hi")
        col = ps.G_MID if struct else (ps.INK if "tray0" in key and key.endswith("r1") else ps.G_DARK)
        ax.plot([lo, hi], [y, y], color=col, lw=1.4, solid_capstyle="butt")
        ax.plot([v], [y], "o", ms=4.8, mfc="white" if struct else col, mec=col, mew=1.0)
        ax.text(1.47, y, f"{v:.2f}", fontsize=6.4, va="center", ha="right", color=ps.G_DARK)
    ax.axvline(1.0, color=ps.G_DARK, lw=0.6)
    ax.axvline(sup, color=ps.G_MID, lw=0.8, ls="--")
    ax.axvline(ref, color=ps.G_MID, lw=0.8, ls=":")
    ax.text(sup + 0.02, len(rows) - 0.45, f"지지선\n{sup:g}", fontsize=6.0, ha="left", va="bottom", color=ps.G_DARK)
    ax.text(ref - 0.02, len(rows) - 0.45, f"반증선\n{ref:g}", fontsize=6.0, ha="right", va="bottom", color=ps.G_DARK)
    ax.set_yticks(ys)
    ax.set_yticklabels([r[0] for r in rows], fontsize=6.8)
    ax.set_xlim(0, 1.5)
    ax.set_ylim(-0.6, len(rows) + 0.6)
    ax.set_xlabel("폰 수락된 도움 ÷ 학습 없음 (31~60일) · ↑\n(95% 구간)")
    ps.gridx(ax)
    ps.panel(ax, "c", dx=-0.55)
    acc_main = S(E6, "pooled", "main|lr30|B", "d1_60", "acc_help", "mean")
    r1t = S(E13, "c47", "verdict_1", "ratio_r1_over_none_d31_60", "ratio")
    r1lo = S(E13, "c47", "verdict_1", "ratio_r1_over_none_d31_60", "lo")
    r1hi = S(E13, "c47", "verdict_1", "ratio_r1_over_none_d31_60", "hi")
    d15 = S(E13, "c46", "contrast", "all|flip15|rest7-rest30", "offered_unhelp_imm", "d1_60", "mean")
    d15lo = S(E13, "c46", "contrast", "all|flip15|rest7-rest30", "offered_unhelp_imm", "d1_60", "lo")
    d15hi = S(E13, "c46", "contrast", "all|flip15|rest7-rest30", "offered_unhelp_imm", "d1_60", "hi")
    meta(
        "F11",
        "그림 11",
        "E6·E13 적응: 학습 방식별 수락된 도움, 쉬는 기간 스윕, 모아보기 규칙",
        f"a: 학습은 버튼만 있는 대안보다 하루 수락된 도움을 더하지 못한다(운영 학습 {acc_main:.2f}건/일). b: 쉬는 기간을 30일에서 7일로 줄였을 때 판정 칸(변화일 15)의 쓸데없는 알림 차이는 {d15:.3f} [{d15lo:.3f}, {d15hi:.3f}]로 "
        f"등록 기준 {bound:g}에 걸쳐 판정 불가다. c: 모아보기를 아무도 열지 않는 조건(tray0)에서 규칙 (1)의 폰 수락된 도움은 학습 없음의 {r1t:.2f} [{r1lo:.2f}, {r1hi:.2f}]이라 등록 예측(지지선 {sup:g})이 반증된다.",
        "E6 (pooled), E13 (c46, c47)",
        "거주자는 확률 모델이고 알아챔·지연·피로·수고는 정한 값이다(사람 자료 아님). 성향 3종, 선호 변화 한 번, 모아보기 열람 80%도 가정이다. 영구 차단(a)과 규칙 (3)(c)의 값은 정의에서 나오는 구조상 결과(†)다. "
        "tray0 의 '모든 장면' 값은 모아보기 항목을 '내놓음'으로 세므로 실제보다 적게 센 값이다.",
    )
    return ps.save(fig, "fig11_adaptation")


# =============================================================================== F12 mutation
def f12() -> list[Any]:
    begin("F12")
    mods = [
        "liot/core/egress_gate.py",
        "liot/core/risk.py",
        "liot/core/severity.py",
        "liot/layers/l3_judgment/guardrail.py",
        "liot/layers/l3_judgment/llm_judge.py",
        "liot/layers/l4_execution/escalation.py",
        "liot/layers/l4_execution/executor.py",
        "liot/layers/l4_execution/feedback_gateway.py",
        "liot/layers/l4_execution/intervention.py",
        "liot/layers/l4_execution/notifier.py",
    ]
    fig = plt.figure(figsize=(ps.W2, 3.3))
    gs = fig.add_gridspec(1, 2, width_ratios=[1.25, 1], wspace=0.45, left=0.09, right=0.99, top=0.9, bottom=0.2)
    ax = fig.add_subplot(gs[0, 0])
    pts = []
    for mpath in mods:
        cov = S(E8, "modules", mpath, "coverage_pct")
        old = S(E8, "modules", mpath, "score_core", scale=100)
        lo = S(E8, "modules", mpath, "score_core_ci95", 0, scale=100)
        hi = S(E8, "modules", mpath, "score_core_ci95", 1, scale=100)
        new = S(E15, "a", "per_module", mpath, "core_new", scale=100)
        pts.append((mpath.split("/")[-1].replace(".py", ""), cov, old, lo, hi, new))
    ax.axhspan(0, 90, facecolor="white", edgecolor="none")
    for _name, cov, old, lo, hi, new in pts:
        ax.plot([cov, cov], [lo, hi], color=ps.G_LIGHT, lw=1.2, zorder=1)
        ax.plot([cov], [old], "o", ms=4.6, mfc=ps.G_MID, mec=ps.G_MID, zorder=3)
        if abs(new - old) > 0.01:
            ax.annotate("", xy=(cov + 0.0, new), xytext=(cov, old), arrowprops={"arrowstyle": "-|>", "color": ps.ACCENT, "lw": 1.0, "shrinkA": 2, "shrinkB": 0}, zorder=4)
            ax.plot([cov], [new], "o", ms=4.6, mfc=ps.ACCENT, mec=ps.ACCENT, zorder=4)
    labpos = {
        "egress_gate": (0.12, 0),
        "risk": (0.12, -3.2),
        "severity": (0.12, -2.0),
        "guardrail": (0.12, 0),
        "llm_judge": (0.12, 0),
        "escalation": (-0.12, 0),
        "executor": (-0.12, 0),
        "feedback_gateway": (0.12, 0),
        "intervention": (0.12, 0),
        "notifier": (0.12, -2),
    }
    for name, cov, old, _lo, _hi, _new in pts:
        dx, dy = labpos[name]
        ax.text(cov + dx, old + dy, name, fontsize=6.2, ha="left" if dx > 0 else "right", va="center", color=ps.G_DARK)
    ax.axhline(90, color=ps.G_MID, lw=0.7, ls="--")
    thr90 = S(E8, "overall", "core", "score_targeted", scale=100)
    ax.text(95.6, 90.8, "90%", fontsize=6.4, ha="left", va="bottom", color=ps.G_DARK)
    ax.set_xlim(95.5, 100.5)
    ax.set_xticks([96, 97, 98, 99, 100])
    ax.set_ylim(40, 105)
    ax.set_xlabel("줄 커버리지 (%)")
    ax.set_ylabel("뮤테이션 점수, 핵심 연산자 (%) · ↑")
    ps.gridy(ax)
    ax.legend(handles=[Line2D([], [], marker="o", ls="", mfc=ps.G_MID, mec=ps.G_MID, label="E8 측정(보강 전), 95% 구간"), Line2D([], [], marker="o", ls="-", color=ps.ACCENT, label="15곳 시험 보강 뒤(E15)")], loc="lower left", fontsize=6.4)
    ps.panel(ax, "a", dx=-0.13)
    del thr90
    ax = fig.add_subplot(gs[0, 1])
    rows = [
        ("15곳 포함 핵심 점수 †", S(E15, "a", "headline_structural", "delta_pp"), None, None, True),
        ("15곳 밖 같은 변이 (A)", S(E15, "a", "collateral", "A_excl_15_ids", "delta_pp"), S(E15, "a", "collateral", "A_excl_15_ids", "delta_ci95_pp", 0), S(E15, "a", "collateral", "A_excl_15_ids", "delta_ci95_pp", 1), False),
        ("새 연산자 표본 (B)", S(E15, "a", "fresh", "paired", "delta_pp"), S(E15, "a", "fresh", "paired", "delta_ci95_pp", 0), S(E15, "a", "fresh", "paired", "delta_ci95_pp", 1), False),
    ]
    ys = [2, 1, 0]
    support = D("code_const", "experiments.e15_mutation_recheck.analyze", "THR_SUPPORT_PP")
    falsify = D("code_const", "experiments.e15_mutation_recheck.analyze", "THR_FALSIFY_PP")
    for y, (_lab, v, lo, hi, struct) in zip(ys, rows, strict=True):
        if struct:
            ax.barh(y, v, height=0.45, **ps.STRUCT)
        else:
            ax.plot([lo, hi], [y, y], color=ps.INK, lw=1.6, solid_capstyle="butt")
            ax.plot([v], [y], "o", ms=5, mfc=ps.INK, mec=ps.INK)
        txt = f"+{v:.2f}" + (f" [{lo:.2f}, {hi:.2f}]" if lo is not None else "")
        ax.text(max(v, hi or 0) + 0.15, y, txt, fontsize=6.4, va="center")
    ax.axvline(falsify, color=ps.G_MID, lw=0.8, ls="--")
    ax.axvline(support, color=ps.G_MID, lw=0.8, ls=":")
    ax.text(falsify + 0.05, -0.62, f"반증선 {falsify:g}%p", fontsize=6.2, va="bottom")
    ax.text(support + 0.05, -0.62, f"지지선 {support:g}%p", fontsize=6.2, va="bottom", ha="left")
    ax.set_yticks(ys)
    ax.set_yticklabels([r[0] for r in rows], fontsize=6.8)
    ax.set_xlim(-0.2, 8.6)
    ax.set_ylim(-0.7, 2.8)
    ax.set_xlabel("핵심 뮤테이션 점수 변화 (%p) · ↑")
    ps.gridx(ax)
    ps.panel(ax, "b", dx=-0.5)
    gap = S(E15, "a", "gap15", "killed_now")
    gap_n = S(E15, "a", "gap15", "n")
    lost = S(E15, "a", "collateral", "A_excl_15_ids", "lost")
    gained = S(E15, "a", "collateral", "A_excl_15_ids", "gained")
    core_old = S(E15, "a", "headline_structural", "core_old", scale=100)
    core_new = S(E15, "a", "headline_structural", "core_new", scale=100)
    n_low = S(E8, "h63_modules_low_score_despite_coverage", agg="len")
    meta(
        "F12",
        "그림 12",
        "E8·E15 뮤테이션 점수 대 줄 커버리지, 빈틈 시험 뒤 변화와 주변 변이",
        f"a: 줄 커버리지가 모두 95% 이상인 안전 핵심 모듈 10개 중 {n_low:.0f}개가 뮤테이션 점수 90% 미만이다(점수 = 테스트가 코드 오류를 알아챈 몫). 화살표는 안전 빈틈 15곳을 겨냥한 시험을 더한 뒤의 변화다. "
        f"b: 핵심 점수는 {core_old:.1f}%에서 {core_new:.1f}%로 올랐고 15곳은 {gap:.0f}/{gap_n:.0f} 잡혔지만 이것은 알려진 구멍을 메운 구조상 결과(†)이며, 15곳 밖 변이는 +{S(E15, 'a', 'collateral', 'A_excl_15_ids', 'delta_pp'):.2f}%p(더 잡힘 {gained:.0f}·덜 잡힘 {lost:.0f})만 올라 일반적으로 단단해졌다고 말할 수 없다.",
        "E8, E15",
        "변이기는 AST 로 직접 만든 것이고 상용 도구와 점수를 비교할 수 없다. 동치·안전 관련 분류는 한 사람의 판단이며 Gemini 2차 분류와 κ 0.46 정도로만 일치한다. 줄 커버리지만 쟀다(분기 아님). B 의 구간 상한은 반증선을 넘어 엄격한 읽기에서는 B 단독 판정 불가다. 모듈별 구간은 E8 측정에 대한 것이다.",
    )
    return ps.save(fig, "fig12_mutation")
