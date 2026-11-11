"""Synthesis figures for the final report (hypothesis map, LIOT-vs-alternatives overview, timeline).

All three are drawn at 4.7 in width with >= 7 pt text so they stay legible when printed at half
page width. Style comes from ``experiments.common.plotstyle`` (Korean font, restrained colours).
Experiments that have not landed (no summary.json) are drawn with a visible hatched 'pending'
marker instead of being skipped silently.
"""

from __future__ import annotations

import re
import statistics
import textwrap
from pathlib import Path
from typing import Any

import matplotlib.pyplot as plt
import numpy as np
import yaml
from matplotlib.patches import Patch, Rectangle

from experiments.common import plotstyle
from experiments.kit.check_numbers import EXP_DIR, KIT_DIR, load_summary

__all__ = [
    "VERDICT_ORDER",
    "VERDICT_STYLE",
    "effective_verdicts",
    "make_overview",
    "make_timeline",
    "make_verdict_map",
]

# Verdict colours are categorical and deliberately not the per-policy colours: a verdict is about
# a hypothesis, not about a policy. Hatches keep the categories apart in greyscale print.
VERDICT_ORDER = ["지지", "반증", "판정 불가", "구조상 결과", "기술 결과", "읽기에 따라 갈림", "등록 결론 불충족", "대기"]
VERDICT_STYLE: dict[str, dict[str, Any]] = {
    "지지": {"face": "#1F5FA8", "hatch": "", "text": "white"},
    "반증": {"face": "#B24A3E", "hatch": "xxx", "text": "white"},
    "판정 불가": {"face": "#D9A441", "hatch": "///", "text": "#2B2B2B"},
    "구조상 결과": {"face": "#6B6B6B", "hatch": "...", "text": "white"},
    "기술 결과": {"face": "#DCDCDC", "hatch": "", "text": "#2B2B2B"},
    "읽기에 따라 갈림": {"face": "#8E5A9B", "hatch": "\\\\\\", "text": "white"},
    "등록 결론 불충족": {"face": "#E3B7AF", "hatch": "", "text": "#5A2A24"},
    "대기": {"face": "white", "hatch": "", "text": "#6B6B6B", "dashed": True},
}

LIOT_BETTER = plotstyle.ACCENT
LIOT_WORSE = plotstyle.POLICY_COLORS["auto_all"]
NEUTRAL = plotstyle.GREY

KIT_FIG_DIR = KIT_DIR / "figures"


def _apply_print_style() -> None:
    plotstyle.setup()
    plt.rcParams.update(
        {
            "font.size": 7.5,
            "axes.titlesize": 8.5,
            "axes.labelsize": 7.5,
            "xtick.labelsize": 7,
            "ytick.labelsize": 7,
            "legend.fontsize": 7,
            "axes.grid": True,
            "grid.linewidth": 0.4,
        }
    )


def _load(path: Path) -> Any:
    return yaml.safe_load(path.read_text(encoding="utf-8"))


def effective_verdicts() -> tuple[list[dict[str, Any]], list[str]]:
    """Hypothesis rows with the verdict that is really shown, plus build warnings.

    A row whose experiment has no summary.json is 대기. A row that still says 대기 although its
    summary.json now exists is flagged ``arrived`` (drawn dotted) and reported as a warning.
    """
    spec = _load(KIT_DIR / "hypotheses.yaml")
    rows: list[dict[str, Any]] = []
    warnings: list[str] = []
    for row in spec["hypotheses"]:
        row = dict(row)
        have = load_summary(row["exp"]) is not None
        row["arrived"] = False
        if not have:
            row["shown"] = "대기"
        elif row["verdict"] == "대기":
            row["shown"] = "대기"
            row["arrived"] = True
            warnings.append(f"NEW-RESULT {row['id']}: {row['exp']}/results/summary.json 도착 — hypotheses.yaml 의 verdict 를 입력하라")
        else:
            row["shown"] = row["verdict"]
        rows.append(row)
    return rows, warnings


# ------------------------------------------------------------------ (a) hypothesis verdict map
def make_verdict_map() -> Path:
    _apply_print_style()
    spec = _load(KIT_DIR / "hypotheses.yaml")
    rows, _ = effective_verdicts()
    questions = spec["questions"]

    row_h = 0.205
    head_h = 0.28
    n_rows = len(rows)
    n_heads = len(questions)
    height = n_rows * row_h + n_heads * head_h + 0.95
    fig = plt.figure(figsize=(4.7, height))
    ax = fig.add_axes((0.0, 0.0, 1.0, 1.0))
    ax.set_xlim(0, 1)
    ax.set_ylim(height, 0)
    ax.axis("off")

    x_id, x_name, x_chip, chip_w = 0.015, 0.205, 0.755, 0.23
    y = 0.12
    ax.text(0.015, y + 0.05, "가설 · 사이클별 최종 판정", fontsize=9, fontweight="bold", va="center", color=plotstyle.GREY_DARK)
    y += 0.3
    for qid, qinfo in questions.items():
        group = [r for r in rows if r["q"] == qid]
        ax.add_patch(Rectangle((0, y), 1, head_h * 0.82, facecolor="#EFEFEF", edgecolor="none"))
        ax.text(x_id, y + head_h * 0.41, qinfo["short"], fontsize=7.8, fontweight="bold", va="center", color=plotstyle.GREY_DARK)
        y += head_h
        for row in group:
            style = VERDICT_STYLE[row["shown"]]
            cy = y + row_h / 2
            ax.text(x_id, cy, row["id"], fontsize=7, fontweight="bold", va="center", color=plotstyle.GREY_DARK)
            ax.text(x_name, cy, row["name"], fontsize=7, va="center", color="#2B2B2B")
            label = "결과 도착 · 입력 필요" if row["arrived"] else row["shown"]
            chip = Rectangle(
                (x_chip, y + 0.02),
                chip_w,
                row_h - 0.04,
                facecolor=style["face"],
                edgecolor=style["face"] if not style.get("dashed") else "#6B6B6B",
                hatch=style["hatch"],
                linewidth=0.6 if not style.get("dashed") else 0.8,
                linestyle="--" if style.get("dashed") else "-",
            )
            if style["hatch"]:
                chip.set_edgecolor("white" if style["text"] == "white" else "#8A6A1F")
            ax.add_patch(chip)
            ax.text(
                x_chip + chip_w / 2,
                cy,
                label,
                fontsize=6.8,
                va="center",
                ha="center",
                color=style["text"],
                fontweight="bold",
                bbox={"boxstyle": "square,pad=0.08", "facecolor": style["face"], "edgecolor": "none", "alpha": 1.0 if style["hatch"] else 0.0},
            )
            y += row_h
    y += 0.18
    counts = {v: sum(1 for r in rows if r["shown"] == v) for v in VERDICT_ORDER}
    handles = [
        Patch(
            facecolor=VERDICT_STYLE[v]["face"],
            edgecolor="#6B6B6B" if VERDICT_STYLE[v].get("dashed") else VERDICT_STYLE[v]["face"],
            hatch=VERDICT_STYLE[v]["hatch"],
            linestyle="--" if VERDICT_STYLE[v].get("dashed") else "-",
            label=f"{v} {counts[v]}",
        )
        for v in VERDICT_ORDER
    ]
    ax.legend(handles=handles, loc="upper left", bbox_to_anchor=(0.01, 1 - (y / height) + 0.0), ncol=4, fontsize=6.6, handlelength=1.4, columnspacing=0.8, frameon=False)
    ax.text(
        0.015,
        y + 0.62,
        "정답이 우리 표·규칙에서 나오는 결과는 '구조상 결과'로 따로 표시했고 비교 결론에 쓰지 않았다.\n"
        "'판정 불가'는 구간이 0 을 넘거나 시뮬레이터 점검을 통과하지 못한 경우다.",
        fontsize=6.4,
        va="top",
        color=plotstyle.GREY_DARK,
        linespacing=1.35,
    )
    return plotstyle.save(fig, KIT_DIR, "a_verdict_map")


# ------------------------------------------------------------------ (b) LIOT vs alternatives
def _pending_panel(ax: Any, title: str, note: str) -> None:
    ax.set_title(title, loc="left", fontweight="bold")
    ax.set_xticks([])
    ax.set_yticks([])
    for spine in ax.spines.values():
        spine.set_visible(True)
        spine.set_linestyle("--")
        spine.set_color("#8C8C8C")
    ax.add_patch(Rectangle((0, 0), 1, 1, transform=ax.transAxes, facecolor="white", hatch="//", edgecolor="#D4D4D4", linewidth=0))
    ax.text(0.5, 0.5, note, transform=ax.transAxes, ha="center", va="center", fontsize=8, color="#555555", fontweight="bold", bbox={"facecolor": "white", "edgecolor": "#8C8C8C", "boxstyle": "round,pad=0.5"})
    ax.grid(False)


def _panel_e1(ax: Any) -> None:
    summary = load_summary("e1_latency")
    if summary is None:
        _pending_panel(ax, "Q1 첫 경보 지연 (E1)", "대기")
        return
    by_floor = summary["c27"]["by_floor"]
    labels, diffs, los, his, pushes = [], [], [], [], []
    for floor, label in (("CRITICAL", "위험 하한\n(가스·연기)"), ("WARNING", "경고 하한\n(그 외 6종)")):
        cell = by_floor[floor]["delta_vs_liot"]["floor_aware"]
        labels.append(label)
        diffs.append(cell["median"]["diff"] * 1000)
        los.append(cell["median"]["lo"] * 1000)
        his.append(cell["median"]["hi"] * 1000)
        pushes.append(cell.get("pushes_mean_diff", 0.0))
    x = np.arange(2)
    ax.bar(x, diffs, color=LIOT_BETTER, width=0.7)
    ax.errorbar(x, diffs, yerr=[np.array(diffs) - np.array(los), np.array(his) - np.array(diffs)], fmt="none", ecolor="#2B2B2B", capsize=3, linewidth=0.9)
    ax.set_xticks(x, labels)
    ax.set_ylabel("지연 차이 (ms)")
    ax.set_ylim(-80, 1250)
    ax.set_title("Q1 첫 경보: 하한 아는 대안 빼기 LIOT", loc="left", fontweight="bold")
    ax.text(0, diffs[0] + 60, f"{diffs[0]:+.1f} ms\n차이 없음", ha="center", fontsize=7)
    ax.text(1, diffs[1] + 60, f"{diffs[1]:+.0f} ms", ha="center", fontsize=7)
    ax.text(1, 420, f"대신 푸시\n{pushes[1]:+.2f}회/건", ha="center", va="center", fontsize=6.8, color="white", fontweight="bold")
    ax.axhline(0, color="#3F3F3F", linewidth=0.7)


def _c_values(summary: dict[str, Any], mode: str, auc: float) -> tuple[list[float], int]:
    """c* points (trade-off cells) and the number of LIOT-dominates cells for one mode / AUC."""
    values: list[float] = []
    dominated = 0
    for key, cell in summary["h15b"]["cells"].items():
        parts = key.split("|")
        if mode not in parts or "a0.0" not in parts:
            continue
        if auc == 0.5:
            if parts[-1] != "e5_operational":
                continue
        else:
            if parts[-1] != "sweep" or parts[2] != f"auc{auc}":
                continue
            if float(parts[0][1:]) == 0 and float(parts[1][3:]) == 0:
                continue
        cstar = cell["c_star"]
        if cstar["class"] == "tradeoff" and cstar["point"] is not None:
            values.append(float(cstar["point"]))
        else:
            dominated += 1
    return values, dominated


def _panel_e2(ax: Any) -> None:
    summary = load_summary("e2_invariants")
    if summary is None:
        _pending_panel(ax, "Q1 보호자 호출 기준 (E2)", "대기")
        return
    groups = [
        ("진실을 보는 모드", "honest", 0.5, True),
        ("취약성을 못 보는 모드", "blind", 0.5, False),
        ("진실을 보는 모드", "honest", 0.8, True),
        ("취약성을 못 보는 모드", "blind", 0.8, False),
    ]
    rng = np.random.default_rng(7)
    dominated_notes: list[str] = []
    for i, (_, mode, auc, filled) in enumerate(groups):
        vals, dominated = _c_values(summary, mode, auc)
        xs = i + (rng.random(len(vals)) - 0.5) * 0.4
        ax.scatter(xs, vals, s=14, facecolor=plotstyle.GREY_DARK if filled else "white", edgecolor=plotstyle.GREY_DARK, linewidth=0.8, zorder=3)
        med = statistics.median(vals)
        ax.hlines(med, i - 0.3, i + 0.3, color=LIOT_BETTER, linewidth=1.8, zorder=4)
        ax.text(i + 0.33, med, f"{med:.1f}", fontsize=7, va="center", color=LIOT_BETTER, fontweight="bold")
        if dominated:
            dominated_notes.append(f"{dominated}칸 LIOT 우세")
        else:
            dominated_notes.append("")
    ax.set_yscale("log")
    ax.set_ylim(0.3, 30)
    ax.set_yticks([0.5, 1, 2, 5, 10, 20], ["0.5", "1", "2", "5", "10", "20"])
    ax.axhline(1, color="#8C8C8C", linewidth=0.8, linestyle="--")
    names = ["진실\n(운영점)", "못 봄\n(운영점)", "진실\n(AUC 0.8)", "못 봄\n(AUC 0.8)"]
    ax.set_xticks(range(4), names, fontsize=6.4)
    n_dom = sum(int(d.split("칸")[0]) for d in dominated_notes if d)
    ax.text(3.45, 28, f"LIOT 우세 {n_dom}칸은 점에서 제외", fontsize=6, color="#555555", ha="right", va="top")
    ax.set_ylabel("c* (높을수록 LIOT 유리)")
    ax.set_title("Q1 보호자 호출: 표 하한 대 최종값", loc="left", fontweight="bold")
    ax.text(-0.45, 0.88, "점선: 놓침 1건 = 오호출 1건", fontsize=6, color="#555555", ha="left", va="top")


def _panel_e3(fig: Any, spec: Any) -> None:
    summary = load_summary("e3_policy")
    sub = spec.subgridspec(1, 3, wspace=0.85)
    if summary is None:
        ax = fig.add_subplot(spec)
        _pending_panel(ax, "Q2 같은 상한을 씌운 비교 (E3)", "대기")
        return
    cmp = summary["c2_2"]["exp"]["m0.1"]["liot_real_vs_flat_same_liot_ceiling"]
    metrics = [
        ("해로운 실행률", "harm_diff", 100, "%p", LIOT_WORSE),
        ("도움 완료율", "help_diff", 100, "%p", LIOT_BETTER),
        ("하루 확인 수", "burden_diff", 1, "건", LIOT_BETTER),
    ]
    axes = []
    for i, (title, key, scale, unit, color) in enumerate(metrics):
        ax = fig.add_subplot(sub[i])
        axes.append(ax)
        d = cmp[key]
        mean, q25, q75 = d["mean"] * scale, d["p25"] * scale, d["p75"] * scale
        ax.errorbar([0], [mean], yerr=[[mean - q25], [q75 - mean]], fmt="o", color=color, ecolor=color, capsize=3, markersize=5, linewidth=1.2)
        ax.axhline(0, color="#3F3F3F", linewidth=0.7)
        ax.set_xlim(-1, 1)
        ax.set_xticks([])
        ax.set_title(title, fontsize=7, pad=3)
        ax.set_ylabel(unit)
        lo, hi = min(q25, 0), max(q75, 0)
        pad = (hi - lo) * 0.25
        ax.set_ylim(lo - pad, hi + pad)
        ax.text(0.5, mean, f" {mean:+.1f}" if key != "burden_diff" else f" {mean:+.2f}", fontsize=7, va="center", color=color, fontweight="bold")
    axes[0].set_title("해로운 실행률", fontsize=7, pad=3)
    axes[0].text(-0.5, 1.30, "Q2 같은 상한 비교(LIOT 실제 빼기 모두 명시 확인+상한)", transform=axes[0].transAxes, fontsize=8.5, fontweight="bold", color=plotstyle.GREY_DARK)
    axes[1].text(0.5, -0.12, "점=세트 평균, 막대=세트 사분위", transform=axes[1].transAxes, fontsize=6.2, ha="center", va="top", color="#555555")


def _panel_e4(ax: Any) -> None:
    summary = load_summary("e4_scoring")
    if summary is None:
        _pending_panel(ax, "Q3 위험 판정 방식 (E4)", "대기")
        return
    n_pairs = summary["table"]["n_dominance_pairs"]
    mult = summary["mult_grid"]["M3"]["tie_loss"]
    rows = [
        ("조회표(LIOT)", summary["table"]["tie_loss"] / n_pairs * 100, None, None, LIOT_BETTER),
        ("곱셈 점수\n(설정 79만 개)", mult["median"] / n_pairs * 100, mult["min"] / n_pairs * 100, mult["max"] / n_pairs * 100, NEUTRAL),
        ("단순 기본값\n곱셈", summary["mult_naive_default"]["tie_loss"] / n_pairs * 100 if "tie_loss" in summary["mult_naive_default"] else None, None, None, NEUTRAL),
        ("단일 등급", summary["single_tier"]["tie_loss_share"] * 100, None, None, LIOT_WORSE),
    ]
    ypos = np.arange(len(rows))[::-1]
    for y, (_label, value, lo, hi, color) in zip(ypos, rows, strict=True):
        if value is None:
            continue
        if lo is not None:
            ax.hlines(y, lo, hi, color=color, linewidth=5, alpha=0.55)
            ax.scatter([value], [y], s=26, color=plotstyle.GREY_DARK, zorder=3)
            ax.text(hi + 2, y, f"{lo:.0f}–{hi:.0f}% (중앙 {value:.0f}%)", va="center", fontsize=6.6)
        else:
            ax.scatter([value], [y], s=36, color=color, zorder=3)
            if value > 90:
                ax.text(value - 3, y, f"{value:.0f}%", va="center", ha="right", fontsize=7, fontweight="bold", color=color)
            else:
                ax.text(value + 4, y, f"{value:.0f}%", va="center", fontsize=7, fontweight="bold", color=color)
    ax.set_yticks(ypos, [r[0] for r in rows])
    ax.set_xlim(0, 135)
    ax.set_xlabel("동점 손실 (분명히 더 나쁜 입력이 같은 등급이 된 쌍, %)")
    ax.set_title("Q3 등급을 얼마나 뭉개나 (낮을수록 잘게)", loc="left", fontweight="bold")
    ax.grid(axis="y", visible=False)


def _panel_e5(ax: Any) -> None:
    summary = load_summary("e5_models")
    if summary is None:
        _pending_panel(ax, "Q5 결정 모델 (E5)", "대기")
        return
    judges = list(summary["c31_subtype"]["rep0"]["per_judge"].keys())
    n_items = summary["h51_sentence_vs_operational"][judges[0]]["rep0"]["n_items"]
    per_ctx: dict[str, list[float]] = {
        "상황 문장": [summary["h51_sentence_vs_operational"][j]["rep0"]["raised_sentence"] / n_items * 100 for j in judges],
        "운영 요약": [summary["h51_sentence_vs_operational"][j]["rep0"]["raised_operational"] / n_items * 100 for j in judges],
        "+관측값 2개": [summary["c25_structured"]["rep0"]["per_judge"][j]["raised_target"] / n_items * 100 for j in judges],
        "+종류 코드": [summary["c31_subtype"]["rep0"]["per_judge"][j]["raised_target"] / n_items * 100 for j in judges],
    }
    colors = [NEUTRAL, LIOT_BETTER, NEUTRAL, NEUTRAL]
    for i, ((_name, vals), color) in enumerate(zip(per_ctx.items(), colors, strict=True)):
        mean = statistics.mean(vals)
        ax.bar(i, mean, color=color, width=0.6)
        ax.scatter([i] * len(vals), vals, s=9, color="#2B2B2B", zorder=3, linewidths=0)
        ax.text(i, max(vals) + 1.2, f"{mean:.0f}%", ha="center", fontsize=7, fontweight="bold")
    rule = summary["rule_lookup_vs_models"]["rule"]["above_floor"] / summary["rule_lookup_vs_models"]["rule"]["noninjection_items"] * 100
    ax.bar(4, rule, color=plotstyle.POLICY_COLORS["rule_only"], width=0.6, hatch="//", edgecolor="white")
    ax.text(4, rule + 1.2, f"{rule:.0f}%", ha="center", fontsize=7, fontweight="bold")
    ax.set_xticks(range(5), ["상황\n문장", "운영\n요약", "+관측\n값 2개", "+종류\n코드", "규칙\n조회"])
    ax.set_ylabel("하한 위로 올린 비율 (%)")
    ax.set_ylim(0, 33)
    ax.set_title("Q5 모델 상향이 돌아오나 (판정자 7명 평균)", loc="left", fontweight="bold")
    ax.text(0.99, 0.97, "점=판정자 1명", transform=ax.transAxes, ha="right", va="top", fontsize=6.2, color="#555555")


def _panel_e6(ax: Any) -> None:
    summary = load_summary("e6_personalization")
    if summary is None:
        _pending_panel(ax, "Q4 적응 (E6)", "대기\n(e6 결과 도착 후\n다시 만들면 채워짐)")
        return
    pooled = summary["pooled"]
    groups = [("폰 장면", "acc_help_imm"), ("모아보기 장면", "acc_help_tray")]
    width = 0.34
    for i, (arm, color, label) in enumerate(
        [("main|explicit_only|B", NEUTRAL, "버튼만 있는 대안"), ("main|lr30|B", LIOT_BETTER, "LIOT 운영(학습 30%)")]
    ):
        vals = [pooled[arm]["d1_60"][key]["mean"] for _, key in groups]
        xs = np.arange(len(groups)) + (i - 0.5) * width
        ax.bar(xs, vals, width=width, color=color, label=label)
        for x, v in zip(xs, vals, strict=True):
            ax.text(x, v + 0.012, f"{v:.2f}", ha="center", fontsize=6.8, fontweight="bold")
    ax.set_xticks(np.arange(len(groups)), [g[0] for g in groups])
    ax.set_ylim(0, 0.86)
    ax.set_ylabel("하루 수락된 도움 (건)")
    ax.set_title("Q4 학습이 버튼에 더하는 것", loc="left", fontweight="bold")
    ax.legend(loc="upper left", fontsize=6, handlelength=1.2)
    ax.text(0.99, 0.98, "쓸데없는 알림 차이 0", transform=ax.transAxes, ha="right", va="top", fontsize=6.2, color="#555555")


def _panel_e7(ax: Any) -> None:
    summary = load_summary("e7_ladder")
    if summary is None:
        _pending_panel(ax, "Q6 보호자 호출 (E7)", "대기")
        return
    c210 = summary["mc"]["c2_10"]
    bs = ["b0.0", "b0.3", "b0.6"]
    xs = np.array([0.0, 0.3, 0.6])
    series = [
        ("해석 N (보호자 수로 방관)", "N_scaled_both", plotstyle.GREY_DARK, "--", "o"),
        ("해석 동시 호출 수", "concurrent_scaled_seq", "#3E8E7E", "-", "s"),
    ]
    for label, key, color, ls, marker in series:
        med = np.array([c210[key][b]["median_diff_p300_seq_minus_sim"] * 100 for b in bs])
        lo = np.array([c210[key][b]["min_diff"] * 100 for b in bs])
        hi = np.array([c210[key][b]["max_diff"] * 100 for b in bs])
        ax.fill_between(xs, lo, hi, color=color, alpha=0.15, linewidth=0)
        ax.plot(xs, med, color=color, linestyle=ls, marker=marker, markersize=4, label=label)
    ax.axhline(0, color=LIOT_BETTER, linewidth=1.3, label="동시 호출(현재 LIOT) = 0")
    ax.set_xticks(xs, ["0", "0.3", "0.6"])
    ax.set_xlabel("방관 효과 b")
    ax.set_ylabel("300초 안 확인 확률 차이\n(순차 빼기 동시, %p)")
    ax.set_title("Q6 순차 대 동시: 해석에 달림", loc="left", fontweight="bold")
    ax.set_ylim(-32, 52)
    ax.legend(loc="upper left", fontsize=6, handlelength=1.6)


def _panel_e8(ax: Any) -> None:
    summary = load_summary("e8_mutation")
    if summary is None:
        _pending_panel(ax, "Q6 안전 모듈의 뮤테이션 점수 (E8)", "대기")
        return
    rows = []
    for path, module in summary["modules"].items():
        rows.append((path.rsplit("/", 1)[-1].removesuffix(".py"), module["score_core"] * 100, module["coverage_pct"]))
    rows.sort(key=lambda r: r[1])
    ypos = np.arange(len(rows))
    for y, (_name, score, cov) in zip(ypos, rows, strict=True):
        color = LIOT_BETTER if score >= 90 else LIOT_WORSE
        ax.barh(y, score, color=color, height=0.62)
        ax.text(103, y, f"{score:.0f}%  (커버리지 {cov:.0f}%)", va="center", fontsize=6.4)
    ax.axvline(90, color="#3F3F3F", linestyle="--", linewidth=0.8)
    ax.text(90, -0.95, "90%", fontsize=6.2, color="#3F3F3F", va="top", ha="center")
    ax.set_yticks(ypos, [r[0] for r in rows])
    ax.set_xlim(0, 160)
    ax.set_ylim(-1.0, len(rows) - 0.4)
    ax.set_xticks([0, 25, 50, 75, 100])
    ax.set_xlabel("뮤테이션 점수 (%, 핵심 연산자 · 높을수록 테스트가 오류를 잡음)")
    ax.set_title("Q6 안전 모듈 10개: 커버리지는 높은데 점수는 모듈마다 다르다", loc="left", fontweight="bold")
    ax.grid(axis="y", visible=False)


def make_overview() -> Path:
    _apply_print_style()
    fig = plt.figure(figsize=(4.7, 10.1))
    gs = fig.add_gridspec(5, 2, hspace=0.82, wspace=0.52, left=0.12, right=0.985, top=0.97, bottom=0.07, height_ratios=[1, 1.05, 1, 1, 1.15])
    _panel_e1(fig.add_subplot(gs[0, 0]))
    _panel_e2(fig.add_subplot(gs[0, 1]))
    _panel_e3(fig, gs[1, :])
    _panel_e4(fig.add_subplot(gs[2, 0]))
    _panel_e5(fig.add_subplot(gs[2, 1]))
    _panel_e6(fig.add_subplot(gs[3, 0]))
    _panel_e7(fig.add_subplot(gs[3, 1]))
    _panel_e8(fig.add_subplot(gs[4, :]))
    fig.text(
        0.12,
        0.012,
        "파랑=LIOT 쪽 값 또는 LIOT 에 유리 · 붉은색=LIOT 에 불리 · 회색=대안·참고. 숫자 출처는 REPORT_KIT.md 의 청구 번호.",
        fontsize=6.2,
        color="#555555",
    )
    return plotstyle.save(fig, KIT_DIR, "b_liot_vs_alternatives")


# ------------------------------------------------------------------ (c) research-cycle timeline
def _log_rows() -> set[tuple[str, int]]:
    """(time, cycle) pairs of the RESEARCH_LOG.md table (``YYYY-MM-DD HH:MM | cycle | …``)."""
    text = (EXP_DIR / "RESEARCH_LOG.md").read_text(encoding="utf-8")
    rows = set()
    for match in re.finditer(r"^\|\s*\d{4}-\d{2}-\d{2} (\d{2}:\d{2})\s*\|\s*(\d+)\s*\|", text, flags=re.M):
        rows.add((match.group(1), int(match.group(2))))
    return rows


def make_timeline() -> Path:
    _apply_print_style()
    events = _load(KIT_DIR / "timeline.yaml")["events"]
    log = _log_rows()
    missing = [f"{e['time']} 사이클 {e['cycle']}" for e in events if (e["time"], e["cycle"]) not in log]
    if missing:
        raise ValueError(f"timeline.yaml 의 항목이 RESEARCH_LOG.md 에 없다: {', '.join(missing)}")

    lane_x = {"H": 0.215, "R": 0.505, "V": 0.795}
    lane_name = {"H": "가설 등록", "R": "결과", "V": "검토·판정"}
    lane_face = {"H": "#E3EEF8", "R": "#EFEFEF", "V": "#F7E8E5"}
    lane_edge = {"H": plotstyle.ACCENT, "R": plotstyle.GREY, "V": LIOT_WORSE}
    row_h = 0.52
    n = len(events)
    height = n * row_h + 1.15
    fig = plt.figure(figsize=(4.7, height))
    ax = fig.add_axes((0.0, 0.0, 1.0, 1.0))
    ax.set_xlim(0, 1)
    ax.set_ylim(height, 0)
    ax.axis("off")

    top = 0.62
    ax.text(0.015, 0.2, "연구 사이클: 가설 → 결과 → 판정 → 다음 가설", fontsize=9, fontweight="bold", color=plotstyle.GREY_DARK, va="center")
    for lane, x in lane_x.items():
        ax.text(x, top - 0.12, lane_name[lane], fontsize=7.5, fontweight="bold", ha="center", color=lane_edge[lane])

    centers: list[tuple[float, float]] = []
    band_cols = {0: "#FAFAFA", 1: "#F1F5FA", 2: "#F6F3EA", 3: "#EFF6F1"}
    for i, ev in enumerate(events):
        y0 = top + i * row_h
        ax.add_patch(Rectangle((0, y0), 1, row_h, facecolor=band_cols[ev["cycle"]], edgecolor="none", zorder=0))
        ax.text(0.012, y0 + row_h / 2, f"사이클 {ev['cycle']}", fontsize=6.4, fontweight="bold", color=plotstyle.GREY_DARK, va="center")

    for i, ev in enumerate(events):
        y = top + i * row_h + row_h / 2
        x = lane_x[ev["lane"]]
        wrapped = "\n".join(textwrap.wrap(ev["text"], 14))
        label = f"{ev['time']}  {wrapped}"
        if ev.get("mark"):
            label += f"\n→ {ev['mark']}"
        ax.text(
            x,
            y,
            label,
            fontsize=6.5,
            ha="center",
            va="center",
            color="#2B2B2B",
            linespacing=1.25,
            bbox={"boxstyle": "round,pad=0.28", "facecolor": lane_face[ev["lane"]], "edgecolor": lane_edge[ev["lane"]], "linewidth": 0.9},
            zorder=3,
        )
        centers.append((x, y))
    for (x0, y0), (x1, y1) in zip(centers, centers[1:], strict=False):
        ax.annotate("", xy=(x1, y1 - row_h * 0.30), xytext=(x0, y0 + row_h * 0.30), arrowprops={"arrowstyle": "-|>", "color": "#9A9A9A", "linewidth": 0.7, "shrinkA": 0, "shrinkB": 0, "connectionstyle": "arc3,rad=0.0"}, zorder=2)
    ax.text(0.015, height - 0.28, "시각은 KST(2026-11-12). 출처: experiments/RESEARCH_LOG.md — 같은 시각·사이클의 행과 일치함을 build 가 검사한다.", fontsize=6.0, color="#555555", va="center")
    return plotstyle.save(fig, KIT_DIR, "c_research_timeline")
