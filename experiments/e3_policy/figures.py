"""E3 figures (Korean labels). Reads raw/ through analyze.load and the computed summary."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import matplotlib.pyplot as plt
import numpy as np

from experiments.common import plotstyle

from . import analyze
from .params import M_DEFAULT, M_GRID

C = plotstyle.POLICY_COLORS
MK = plotstyle.POLICY_MARKERS
COLORS = {
    "liot_real": C["liot"], "liot_full": C["short_deadline"], "flat_explicit": C["flat_explicit"],
    "binary_ext": C["binary_approval"], "binary_opt": C["rule_only"], "auto_all": C["auto_all"],
    "auto_undo": C["model_only"],
}
MARKS = {
    "liot_real": MK["liot"], "liot_full": "o", "flat_explicit": MK["flat_explicit"],
    "binary_ext": MK["binary_approval"], "binary_opt": "D", "auto_all": MK["auto_all"], "auto_undo": "P",
}
LABEL = {
    "liot_real": "LIOT 실제(능동 상한)", "liot_full": "LIOT 가상(4단계 전부)", "flat_explicit": "모두 명시 확인(A)",
    "binary_ext": "이진 승인(외부 목록)", "binary_opt": "이진 승인(사후 최적)", "auto_all": "모두 자동",
    "auto_undo": "자동+되돌리기(15분)",
}
SHAPE_KO = {"exp": "지수 피로", "hyp": "포화 피로"}


def pareto(blocks: dict[str, analyze.Block], exp_dir: Path) -> Path:
    blk = blocks["main"]
    fig, axes = plt.subplots(1, 2, figsize=(11, 4.6), sharey=True)
    for ax, sh in zip(axes, analyze.SHAPES, strict=True):
        names, pts = analyze.policy_points(blk, sh, M_DEFAULT)
        for i, n in enumerate(names):
            ax.scatter(pts[i, 0], pts[i, 1], s=9, alpha=0.22, color=COLORS[n], marker=MARKS[n], linewidths=0)
        for i, n in enumerate(names):
            ax.scatter(np.nanmedian(pts[i, 0]), np.nanmedian(pts[i, 1]), s=120, color=COLORS[n], marker=MARKS[n],
                       edgecolor="white", linewidth=1.2, label=LABEL[n], zorder=5)
        ax.set_title(f"{SHAPE_KO[sh]} (m=10%)")
        ax.set_xlabel("하루 확인 요청 수 (부담)")
        ax.set_ylim(-0.02, 1.04)
    axes[0].set_ylabel("해로운 제안 실행률 (피해)")
    axes[1].legend(loc="lower right", fontsize=8)
    fig.suptitle("부담 대 피해: 점 하나 = 매개변수 한 세트(400), 큰 점 = 중앙값. 왼쪽 아래가 좋다", fontsize=10)
    return plotstyle.save(fig, exp_dir, "pareto_burden_vs_harm")


def harm_vs_theta(summary: dict[str, Any], exp_dir: Path) -> Path:
    fig, axes = plt.subplots(2, 3, figsize=(12, 6.2), sharey=True, sharex=True)
    th = [round(0.1 * i, 1) for i in range(11)]
    cols = [plotstyle.ACCENT, plotstyle.GREY_DARK, plotstyle.GREY]
    labs = ["피로 계수 f 하위 1/3", "f 중간 1/3", "f 상위 1/3"]
    for r, sh in enumerate(analyze.SHAPES):
        for c, sg in enumerate(("s0.0", "s0.5", "s1.0")):
            ax = axes[r, c]
            u = summary["h2_3"][sh][sg]
            for k, cv in enumerate(u["median_curve_by_f_tercile"]):
                ax.plot(th, cv, color=cols[k], marker="o", ms=3, label=labs[k])
            ax.set_title(f"{SHAPE_KO[sh]} · 점수 잡음 σ={sg[1:]}", fontsize=10)
            ax.text(0.03, 0.05, f"U자(구간>0) {u['u_shape_sig']['share']*100:.0f}% · 1%p 이상 {u['u_shape_sig_depth_gt_0.01']['share']*100:.0f}%",
                    transform=ax.transAxes, fontsize=8)
            if r == 1:
                ax.set_xlabel("확인을 요구하는 제안 비율 θ")
            if c == 0:
                ax.set_ylabel("해로운 제안 실행률(중앙값)")
    axes[0, 0].legend(fontsize=8)
    fig.suptitle("확인 비율 θ 에 따른 해로운 실행률(U자 점검). 가운데가 가장 낮으면 U자", fontsize=10)
    return plotstyle.save(fig, exp_dir, "harm_vs_theta")


def winfrac_vs_m(summary: dict[str, Any], exp_dir: Path) -> Path:
    fig, axes = plt.subplots(1, 3, figsize=(14, 4.4))
    ms = [100 * m for m in M_GRID]
    for sh, ls in zip(analyze.SHAPES, ("-", "--"), strict=True):
        rows = summary["h2_4"][sh]["rows"]
        ax = axes[0]
        for pol in ("liot_real", "liot_full"):
            ax.plot(ms, [r[f"area_{pol}_vs_flat"]["share"] for r in rows], ls, color=COLORS[pol], marker=MARKS[pol],
                    label=f"{LABEL[pol]} · {SHAPE_KO[sh]}")
        ax.plot(ms, [r["area_both_lower_than_flat_and_binary_opt"]["share"] for r in rows], ls, color=plotstyle.GREY_DARK,
                label=f"둘 다 확인·이진(최적)보다 낮음 · {SHAPE_KO[sh]}")
        ax = axes[1]
        pv = summary["pareto_vs_m"][sh]
        for pol in ("flat_explicit", "binary_opt", "liot_real", "liot_full"):
            ax.plot(ms, [pv[f"m{m:.1f}"]["lowest_harm"][pol] for m in M_GRID], ls, color=COLORS[pol], marker=MARKS[pol],
                    label=f"{LABEL[pol]} · {SHAPE_KO[sh]}")
        ax = axes[2]
        for pol in ("flat_explicit", "binary_opt", "liot_real", "liot_full"):
            ax.plot(ms, [pv[f"m{m:.1f}"]["pareto_2d"][pol] for m in M_GRID], ls, color=COLORS[pol], marker=MARKS[pol])
    axes[0].set_title("피해가 확인(A)보다 유의하게 낮은 세트 비율")
    axes[1].set_title("해로운 실행률이 가장 낮은 정책(세트 비율)")
    axes[2].set_title("파레토 최적(부담·피해)인 세트 비율")
    for ax in axes:
        ax.set_xlabel("오분류율 m (%)")
        ax.set_ylim(-0.02, 1.02)
    axes[0].set_ylabel("매개변수 세트 비율")
    axes[0].legend(fontsize=6.5, loc="upper right")
    axes[1].legend(fontsize=6.5, loc="upper right")
    return plotstyle.save(fig, exp_dir, "winfraction_vs_m")


def burden_bars(summary: dict[str, Any], exp_dir: Path) -> Path:
    t = summary["policy_table"]["exp"][f"m{M_DEFAULT:.1f}"]
    th = summary["policy_table"]["hyp"][f"m{M_DEFAULT:.1f}"]
    order = [("auto_all", "auto_all"), ("flat_explicit", "flat_explicit"), ("binary_ext", "binary_ext"),
             ("binary_opt", "binary_opt"), ("liot_full", "liot_full"), ("liot_real", "liot_real"),
             ("auto_undo", "auto_undo_w15")]
    fig, axes = plt.subplots(1, 3, figsize=(14, 4.4))
    x = np.arange(len(order))
    for ax, met, title in ((axes[0], "burden_per_day", "하루 확인 요청 수"),
                           (axes[1], "load_units_per_day", "하루 부담 단위(알림 포함)")):
        vals = [t[k][met] for _, k in order]
        ax.bar(x, [v["mean"] for v in vals], color=[COLORS[n] for n, _ in order],
               yerr=[[v["mean"] - v["lo"] for v in vals], [v["hi"] - v["mean"] for v in vals]], capsize=3)
        ax.set_title(title)
    ax = axes[2]
    w = 0.38
    for off, tab, lab in ((-w / 2, t, "지수 피로"), (w / 2, th, "포화 피로")):
        vals = [tab[k]["harm_rate"] for _, k in order]
        ax.bar(x + off, [v["mean"] for v in vals], w, color=[COLORS[n] for n, _ in order], alpha=1.0 if off < 0 else 0.55,
               yerr=[[v["mean"] - v["lo"] for v in vals], [v["hi"] - v["mean"] for v in vals]], capsize=2, label=lab)
    ax.set_title("해로운 제안 실행률 (진한 막대 지수·옅은 막대 포화)")
    for ax in axes:
        ax.set_xticks(x)
        ax.set_xticklabels([LABEL[n].replace("(", "\n(") for n, _ in order], rotation=40, ha="right", fontsize=7)
    fig.suptitle("정책별 평균(세트 400개, 막대 끝 = 세트 부트스트랩 95% 구간, m=10%)", fontsize=10)
    return plotstyle.save(fig, exp_dir, "burden_bars")


def f0_sanity(summary: dict[str, Any], exp_dir: Path) -> Path:
    fig, ax = plt.subplots(figsize=(7.5, 4))
    t = summary["policy_table_f0"][f"m{0.0:.1f}"]
    order = [("flat_explicit", "flat_explicit"), ("liot_full", "liot_full"), ("liot_real", "liot_real"),
             ("binary_opt", "binary_opt"), ("auto_undo", "auto_undo_w15"), ("auto_all", "auto_all")]
    vals = [t[k]["harm_rate"] for _, k in order]
    x = np.arange(len(order))
    ax.bar(x, [v["mean"] for v in vals], color=[COLORS[n] for n, _ in order],
           yerr=[[v["mean"] - v["lo"] for v in vals], [v["hi"] - v["mean"] for v in vals]], capsize=3)
    ax.set_xticks(x)
    ax.set_xticklabels([LABEL[n].replace("(", "\n(") for n, _ in order], fontsize=7)
    ax.set_ylabel("해로운 제안 실행률")
    ax.set_title("시뮬레이터 점검: f=0, r=0 (피로 없음), m=0")
    return plotstyle.save(fig, exp_dir, "f0_sanity_harm")


def region_maps(summary: dict[str, Any], exp_dir: Path) -> Path:
    h = summary["h2_1"]["exp"][f"m{M_DEFAULT:.1f}"]
    fb, rb = h["map_f_bins"], h["map_r_bins"]
    fig, axes = plt.subplots(1, 4, figsize=(19, 4.4))
    for ax, pol in zip(axes[:2], ("liot_real", "liot_full"), strict=True):
        g = np.array([[np.nan if v is None else v for v in row] for row in h[pol]["harm"]["map_f_by_r_mean_diff"]])
        im = ax.imshow(g, origin="lower", cmap="RdBu_r", vmin=-0.4, vmax=0.4, aspect="auto")
        for i in range(g.shape[0]):
            for j in range(g.shape[1]):
                if not np.isnan(g[i, j]):
                    ax.text(j, i, f"{g[i, j]:+.2f}", ha="center", va="center", fontsize=8)
        ax.set_title(f"{LABEL[pol]} - 모두 명시 확인\n해로운 실행률 차이(파랑 = LIOT 가 낮음)", fontsize=9)
        fig.colorbar(im, ax=ax, shrink=0.8, pad=0.2)
    for ax, key_, ttl in ((axes[2], "map_f_by_r_lowest_harm_policy", "셀마다 해로운 실행률이 가장 낮은 정책(전체)"),
                          (axes[3], "map_f_by_r_lowest_harm_policy_excluding_liot_real",
                           "같은 지도, '침묵'하는 LIOT 실제를 뺀 확인 대결")):
        names = ["flat_explicit", "binary_ext", "binary_opt", "liot_real", "liot_full", "auto_undo"]
        code = np.array([[names.index(v) if v in names else np.nan for v in row] for row in h[key_]])
        for i in range(code.shape[0]):
            for j in range(code.shape[1]):
                if not np.isnan(code[i, j]):
                    n = names[int(code[i, j])]
                    ax.add_patch(plt.Rectangle((j - 0.5, i - 0.5), 1, 1, color=COLORS[n]))
                    ax.text(j, i, LABEL[n].replace("(", "\n("), ha="center", va="center", fontsize=6.5, color="white")
        ax.set_xlim(-0.5, code.shape[1] - 0.5)
        ax.set_ylim(-0.5, code.shape[0] - 0.5)
        ax.set_title(ttl, fontsize=9)
    for ax in axes:
        ax.set_xticks(range(len(rb) - 1))
        ax.set_xticklabels([f"{rb[j]:.1f}–{rb[j + 1]:.1f}" for j in range(len(rb) - 1)], fontsize=8)
        ax.set_yticks(range(len(fb) - 1))
        ax.set_yticklabels([f"{fb[i]:.3f}–{fb[i + 1]:.3f}" for i in range(len(fb) - 1)], fontsize=8)
        ax.set_xlabel("무심코 승인 확률 r")
        ax.grid(False)
    axes[0].set_ylabel("피로 계수 f")
    return plotstyle.save(fig, exp_dir, "region_map_f_by_r")


def m_ratio(summary: dict[str, Any], exp_dir: Path) -> Path:
    fig, axes = plt.subplots(1, 2, figsize=(11, 4.2), sharey=True)
    ms = [100 * m for m in M_GRID]
    for ax, sh in zip(axes, analyze.SHAPES, strict=True):
        rows = summary["h2_4"][sh]["rows"]
        for pol in ("liot_real", "liot_full"):
            med = [r["harm_ratio_vs_flat"][pol]["median"] for r in rows]
            lo = [r["harm_ratio_vs_flat"][pol]["p25"] for r in rows]
            hi = [r["harm_ratio_vs_flat"][pol]["p75"] for r in rows]
            ax.plot(ms, med, color=COLORS[pol], marker=MARKS[pol], label=LABEL[pol])
            ax.fill_between(ms, lo, hi, color=COLORS[pol], alpha=0.18)
        ax.axhline(1.0, color=plotstyle.GREY_DARK, lw=1, ls=":")
        ax.set_title(f"{SHAPE_KO[sh]}")
        ax.set_xlabel("오분류율 m (%)")
    axes[0].set_ylabel("LIOT 해로운 실행률 ÷ 모두 명시 확인 (중앙값, 25–75%)")
    axes[0].legend(fontsize=8)
    fig.suptitle("오분류가 늘면 4단계의 피해 이점이 사라진다(1 위 = 확인보다 나쁨)", fontsize=10)
    return plotstyle.save(fig, exp_dir, "m_by_harm_ratio_map")


def sensitivity(summary: dict[str, Any], exp_dir: Path) -> Path:
    heads = summary["headline_by_block"]
    blocks = [("sens_half", "범위 절반"), ("main", "기본"), ("sens_dbl", "범위 두 배")]
    items = [("liot_real_vs_flat_harm_higher", "LIOT 실제 > A (피해)", COLORS["liot_real"]),
             ("liot_full_vs_flat_harm_higher", "LIOT 가상 > A (피해)", COLORS["liot_full"]),
             ("binary_opt_vs_flat_harm_higher", "이진(최적) > A (피해)", COLORS["binary_opt"])]
    fig, ax = plt.subplots(figsize=(8, 4))
    w = 0.25
    x = np.arange(len(blocks))
    for k, (kk, lab, col) in enumerate(items):
        ax.bar(x + (k - 1) * w, [heads[b]["exp"][kk] for b, _ in blocks if b in heads], w, color=col, label=lab)
    ax.set_xticks(x)
    ax.set_xticklabels([n for _, n in blocks])
    ax.set_ylabel("세트 비율(구간이 0 을 넘는 경우)")
    ax.set_title("민감도: 매개변수 범위를 절반·두 배로 했을 때(지수 피로, m=10%)")
    ax.legend(fontsize=8)
    return plotstyle.save(fig, exp_dir, "sensitivity_ranges")


def silenced_by_device(summary: dict[str, Any], exp_dir: Path) -> Path:
    d = summary["h2_6_device_sweep"]
    ko = {"light": "조명", "climate": "냉난방·공기", "cover": "창문·커버", "appliance": "가전(오븐·세탁기)",
          "entry": "출입(잠금·차고·밸브)"}
    fig, axes = plt.subplots(1, 3, figsize=(15, 4.4))
    for ax, key_ in zip(axes[:2], ("region_map_entry_by_light", "region_map_entry_by_appliance"), strict=True):
        g = d[key_]
        arr = np.array([[np.nan if v is None else v for v in row] for row in g["mean_share"]])
        im = ax.imshow(arr, origin="lower", cmap="Blues", vmin=0.2, vmax=0.8, aspect="auto")
        for j in range(arr.shape[0]):
            for i in range(arr.shape[1]):
                if not np.isnan(arr[j, i]):
                    ax.text(i, j, f"{arr[j, i] * 100:.0f}%", ha="center", va="center", fontsize=9)
        e = g["edges"]
        lab = [f"{e[i]:.2f}-{e[i + 1]:.2f}" for i in range(len(e) - 1)]
        ax.set_xticks(range(len(lab)))
        ax.set_xticklabels(lab, fontsize=8)
        ax.set_yticks(range(len(lab)))
        ax.set_yticklabels(lab, fontsize=8)
        ax.set_xlabel(f"{ko[g['x'][5:]]} 비중 배수")
        ax.set_ylabel(f"{ko[g['y'][5:]]} 비중 배수")
        ax.set_title("능동 상한에 막힌 도움 제안 비율(세트 평균)", fontsize=9)
        ax.grid(False)
        fig.colorbar(im, ax=ax, shrink=0.8, pad=0.03)
    ax = axes[2]
    x = np.arange(5)
    w = 0.38
    ax.bar(x - w / 2, [100 * v for v in d["silenced_fraction_within_class"]], w, color=plotstyle.GREY, label="그 종류 도움 제안 중 막힌 비율")
    ax.bar(x + w / 2, [100 * v for v in d["contribution_to_total_silenced"]], w, color=plotstyle.ACCENT, label="막힌 전체 중 그 종류가 차지한 몫")
    ax.set_xticks(x)
    ax.set_xticklabels([ko[c] for c in d["classes"]], rotation=25, ha="right", fontsize=8)
    ax.set_ylabel("%")
    ax.legend(fontsize=7)
    ax.set_title("기기 종류별", fontsize=9)
    fig.subplots_adjust(wspace=0.55)
    return plotstyle.save(fig, exp_dir, "silenced_helpful_by_device")


def c21_fig(summary: dict[str, Any], exp_dir: Path) -> Path:
    rows = summary["c2_1"]["rows"]
    x = [100 * r["realized_breath_or_below_share"] for r in rows]
    fig, axes = plt.subplots(1, 3, figsize=(14.5, 4.3))
    for m, ls in (("m0.0", "-"), ("m0.1", "--")):
        axes[0].plot(x, [100 * r[m]["liot_real"]["area_harm_lower_seed_noise_sig"]["share"] for r in rows], ls,
                     color=COLORS["liot_real"], marker="o", label=f"{LABEL['liot_real']} 낮음 · m={m[1:]}")
        axes[0].plot(x, [100 * r[m]["liot_real"]["area_harm_higher_seed_noise_sig"]["share"] for r in rows], ls,
                     color=plotstyle.GREY_DARK, marker="s", label=f"{LABEL['liot_real']} 높음 · m={m[1:]}")
        axes[1].plot(x, [100 * r[m]["liot_full"]["area_harm_lower_seed_noise_sig"]["share"] for r in rows], ls,
                     color=COLORS["liot_full"], marker="o", label=f"{LABEL['liot_full']} 낮음 · m={m[1:]}")
        axes[1].plot(x, [100 * r[m]["liot_full"]["area_harm_higher_seed_noise_sig"]["share"] for r in rows], ls,
                     color=plotstyle.GREY_DARK, marker="s", label=f"{LABEL['liot_full']} 높음 · m={m[1:]}")
        d = [r[m]["liot_real"]["harm_diff_pooled"] for r in rows]
        axes[2].plot(x, [v["set_median"] for v in d], ls, color=COLORS["liot_real"], marker="o", label=f"중앙값 · m={m[1:]}")
        if m == "m0.1":
            axes[2].fill_between(x, [v["set_q25"] for v in d], [v["set_q75"] for v in d], color=COLORS["liot_real"], alpha=0.18)
    for ax in axes[:2]:
        ax.axhline(50, color=plotstyle.GREY, lw=1, ls=":")
        ax.set_ylim(-2, 102)
        ax.set_ylabel("세트 비율 (%, 씨앗 잡음 구간이 0 을 넘는 경우)")
        ax.legend(fontsize=6.5)
    axes[2].axhline(0, color=plotstyle.GREY, lw=1, ls=":")
    axes[2].set_ylabel("해로운 실행률 차이(LIOT 실제 - 모두 명시 확인), 세트 중앙값과 사분위")
    axes[2].legend(fontsize=7)
    for ax, t in zip(axes, ("LIOT 실제 대 모두 명시 확인(피해)", "LIOT 가상 대 모두 명시 확인(피해)", "피해 차이의 세트 간 분포"), strict=True):
        ax.set_xlabel("해로운 제안 중 LIOT 단계가 숨결 이하인 비율 (%)")
        ax.set_title(t, fontsize=9)
    return plotstyle.save(fig, exp_dir, "c21_harm_template_shift")


def c22_fig(summary: dict[str, Any], exp_dir: Path) -> Path:
    fig, axes = plt.subplots(1, 2, figsize=(12, 4.2), sharey=True)
    bases = [("flat_explicit", "모두 명시 확인 + 상한"), ("binary_ext", "이진(외부) + 상한"), ("auto_all", "모두 자동 + 상한")]
    for ax, m in zip(axes, ("m0.0", "m0.1"), strict=True):
        for i, (b, _lab) in enumerate(bases):
            r = summary["c2_2"]["exp"][m][b]["liot_minus_generator_ceiling_at_matched_silence"]["harm_diff"]
            ax.plot([r["p10"], r["p90"]], [i, i], color=plotstyle.GREY, lw=1.5)
            ax.plot([r["p25"], r["p75"]], [i, i], color=COLORS["liot_real"], lw=5)
            ax.plot([r["median"]], [i], "o", color="white", mec=plotstyle.GREY_DARK)
        ax.axvline(0, color=plotstyle.GREY_DARK, lw=1)
        ax.axvspan(-0.02, 0.02, color=plotstyle.GREY_LIGHT, alpha=0.5)
        ax.set_yticks(range(len(bases)))
        ax.set_yticklabels([lab for _, lab in bases], fontsize=8)
        ax.set_xlabel("해로운 실행률 차이: LIOT 단계 상한 - 생성기 상한 (같은 침묵 비율)")
        ax.set_title(f"오분류 {m[1:]} (회색 띠 = 등록한 ±2%p)", fontsize=9)
    return plotstyle.save(fig, exp_dir, "c22_ceiling_matched_silence")


def c23_fig(summary: dict[str, Any], exp_dir: Path) -> Path:
    fig, axes = plt.subplots(1, 2, figsize=(11, 4.2))
    ms = [100 * m for m in M_GRID]
    rows = summary["c2_3"]["exp"]
    for tag, col, lab in (("liot_full", COLORS["liot_full"], "잔잔 자동 실행(LIOT 가상)"),
                          ("liot_full_notify_only", COLORS["liot_real"], "잔잔 알림만"),
                          ("liot_real", plotstyle.GREY_DARK, "LIOT 실제")):
        axes[0].plot(ms, [100 * r[tag]["share_sets_harm_higher_point"] for r in rows], color=col, marker="o", label=lab)
        axes[1].plot(ms, [r[tag]["harm_diff_pooled"]["set_median"] for r in rows], color=col, marker="o", label=lab)
        axes[1].fill_between(ms, [r[tag]["harm_diff_pooled"]["set_q25"] for r in rows],
                             [r[tag]["harm_diff_pooled"]["set_q75"] for r in rows], color=col, alpha=0.12)
    axes[0].axhline(50, color=plotstyle.GREY, lw=1, ls=":")
    axes[0].set_ylabel("모두 명시 확인보다 해로운 실행률이 높은 세트 (%, 점추정)")
    axes[1].axhline(0, color=plotstyle.GREY, lw=1, ls=":")
    axes[1].set_ylabel("해로운 실행률 차이의 세트 중앙값과 사분위")
    for ax in axes:
        ax.set_xlabel("오분류율 m (%) · 지연 증가 g=0")
        ax.legend(fontsize=8)
    return plotstyle.save(fig, exp_dir, "c23_ambient_notify_only")


def c24_fig(summary: dict[str, Any], exp_dir: Path) -> Path:
    c = summary["c2_4"]
    fig, ax = plt.subplots(figsize=(7.5, 4.4))
    for m, col in (("0.0", plotstyle.GREY_DARK), ("0.1", COLORS["liot_real"])):
        rs = sorted([r for r in c["rows"] if f"{r['m']:.1f}" == m], key=lambda r: r["auc_median"])
        ax.plot([r["auc_median"] for r in rs], [100 * r["share_sets_dominating_liot_full"]["share"] for r in rs],
                marker="o", color=col, label=f"θ 정책이 LIOT 가상을 지배하는 세트 · m={m}")
        ax.axvline(c["liot_tier_auc"]["tier0_median"] if m == "0.0" else c["liot_tier_auc"]["tier_m0.1_median"],
                   color=col, ls="--", lw=1)
    ax.axhline(50, color=plotstyle.GREY, lw=1, ls=":")
    ax.set_xlabel("θ 점수의 AUC (점선 = LIOT 단계 자체의 AUC, m=0 / m=10%)")
    ax.set_ylabel("세트 비율 (%)")
    ax.legend(fontsize=8)
    return plotstyle.save(fig, exp_dir, "c24_theta_auc")


def make_all(raw_dir: Path, summary: dict[str, Any], exp_dir: Path) -> list[Path]:
    plotstyle.setup()
    blocks = analyze.load(raw_dir)
    out = [pareto(blocks, exp_dir), harm_vs_theta(summary, exp_dir), winfrac_vs_m(summary, exp_dir),
           burden_bars(summary, exp_dir), region_maps(summary, exp_dir), m_ratio(summary, exp_dir),
           sensitivity(summary, exp_dir), silenced_by_device(summary, exp_dir)]
    if "policy_table_f0" in summary:
        out.append(f0_sanity(summary, exp_dir))
    for key_, fn_ in (("c2_1", c21_fig), ("c2_2", c22_fig), ("c2_3", c23_fig), ("c2_4", c24_fig)):
        if key_ in summary:
            out.append(fn_(summary, exp_dir))
    return out
