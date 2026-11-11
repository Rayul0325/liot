"""E5 figures (made from the summary only)."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import matplotlib.pyplot as plt
import numpy as np

from experiments.common import plotstyle


def _judges_with(s: dict[str, Any], key: str) -> list[str]:
    return [j for j, v in s[key].items() if v]


def fig_h51(s: dict[str, Any], exp_dir: Path) -> Path:
    plotstyle.setup()
    h = s["h51_sentence_vs_operational"]
    judges = [j for j in h if h[j].get("rep0", {}).get("n_items")]
    fig, ax = plt.subplots(figsize=(8.4, 4.2))
    x = np.arange(len(judges))
    w = 0.38
    sent = [h[j]["rep0"]["rate_sentence"] for j in judges]
    oper = [h[j]["rep0"]["rate_operational"] for j in judges]
    ax.bar(x - w / 2, sent, w, color=plotstyle.ACCENT, label="상황 문장 맥락")
    ax.bar(x + w / 2, oper, w, color=plotstyle.GREY, label="운영 집계 요약 맥락")
    for i, j in enumerate(judges):
        d = h[j]["rep0"]
        ax.text(i, max(sent[i], oper[i]) + 0.02,
                f"차 {d['diff_sentence_minus_operational']:+.2f}\n[{d['diff_ci95_case_bootstrap'][0]:+.2f}, {d['diff_ci95_case_bootstrap'][1]:+.2f}]",
                ha="center", fontsize=7, color=plotstyle.GREY_DARK)
    ax.set_xticks(x, judges, rotation=20, ha="right", fontsize=8)
    ax.set_ylabel("하한보다 높게 답한 비율 (주입 제외 61문항)")
    ax.set_ylim(0, max(max(sent), max(oper)) + 0.2)
    ax.legend(fontsize=8)
    ax.set_title("같은 사례를 문장과 집계 요약으로 물었을 때 올림 비율 (막대 위: 차이와 95% 구간)")
    return plotstyle.save(fig, exp_dir, "h51_raise_sentence_vs_operational")


def fig_h52(s: dict[str, Any], exp_dir: Path) -> Path:
    plotstyle.setup()
    h = s["h52_lower_injection"]
    rows: list[tuple[str, float, list[float], str]] = []
    from experiments.e5_models.analyze import CLOUD_NAMES

    for j, v in h.items():
        sent = v.get("sentence")
        if not sent:
            continue
        r = sent[0]
        if not r["cases"]:
            continue
        lowered = r["cases_lowered_any_question"] / r["cases"]
        rows.append((j, lowered, r["cases_lowered_ci95"], "cloud" if j in CLOUD_NAMES else "local"))
    fig, ax = plt.subplots(figsize=(8.0, 4.0))
    y = np.arange(len(rows))
    for i, (_j, p, ci, kind) in enumerate(rows):
        color = plotstyle.ACCENT if kind == "cloud" else plotstyle.GREY_DARK
        ax.plot(ci, [i, i], color=color, lw=2)
        ax.plot([p], [i], "o", color=color)
    ax.set_yticks(y, [r[0] + (" (생성형 JSON)" if r[0] == "haiku" else "") for r in rows], fontsize=8)
    ax.set_xlabel("'낮춰라' 8건 중 하한보다 낮게 답한 사례 비율 (정확 이항 95% 구간)")
    ax.set_xlim(0, 1)
    ax.set_title("주입 '낮춰라' 사례에서 하한 아래로 내려간 비율 — 파랑 = 클라우드, 진회색 = 로컬")
    return plotstyle.save(fig, exp_dir, "h52_lowered_by_injection")


def fig_h53(s: dict[str, Any], exp_dir: Path) -> Path:
    plotstyle.setup()
    labels, vals, los, his = [], [], [], []
    for ctx_key, v in s["h53_agreement"].items():
        if not ctx_key.endswith("rep0"):
            continue
        for subset in ("local_only", "cloud_only", "all"):
            blk = v.get(subset)
            if not blk:
                continue
            for q in ("tier", "severity"):
                st = blk.get(q)
                if not st or "gwet_ac1" not in st:
                    continue
                labels.append(f"{ctx_key.replace('_rep0', '')[:4]}·{subset.replace('_only', '')}·{q}")
                vals.append(st["gwet_ac1"])
                los.append(st["gwet_ac1"] - st["gwet_ac1_ci95"][0])
                his.append(st["gwet_ac1_ci95"][1] - st["gwet_ac1"])
    fig, ax = plt.subplots(figsize=(9, 4.2))
    x = np.arange(len(labels))
    ax.bar(x, vals, color=plotstyle.GREY, yerr=[los, his], capsize=2)
    ax.set_xticks(x, labels, rotation=60, ha="right", fontsize=7)
    ax.axhline(0.6, color=plotstyle.ACCENT, ls="--", lw=1)
    ax.set_ylabel("Gwet AC1 (95% 부트스트랩 구간)")
    ax.set_title("판정자끼리 얼마나 일치하나 (점선 0.6)")
    return plotstyle.save(fig, exp_dir, "h53_agreement_ac1")


def fig_add5(s: dict[str, Any], exp_dir: Path) -> Path:
    plotstyle.setup()
    a = s["add5_miss_vs_table"]
    fig, ax = plt.subplots(figsize=(7.2, 4.2))
    ax.plot([0], [1.0], "s", color=plotstyle.GREY_DARK, label="조회표(올리지 않음)")
    for j, v in a.items():
        sent = v.get("sentence")
        if not sent or "probability_sweep" not in sent:
            continue
        xs = [0.0, 0.05, 0.10]
        ys = [sent["probability_sweep"][f"over_raise_le_{int(b * 100)}pct"]["miss_rate"] for b in xs]
        ax.plot(xs, ys, marker="o", lw=1.2, label=j)
    ax.set_xlabel("과잉 올림 예산 (평온 문장을 올린 비율 상한)")
    ax.set_ylabel("놓침 비율 (위험 문장을 못 올린 비율)")
    ax.set_ylim(0, 1.05)
    ax.legend(fontsize=7, ncol=2)
    ax.set_title("상황 문장 맥락의 모델 단독 vs 조회표 (쌍 설계 기준, 탐색)")
    return plotstyle.save(fig, exp_dir, "add5_miss_vs_overraise")


def fig_c25(s: dict[str, Any], exp_dir: Path) -> Path:
    plotstyle.setup()
    c = s["c25_structured"]["rep0"]["per_judge"]
    judges = sorted(c)
    x = np.arange(len(judges))
    w = 0.26
    fig, axes = plt.subplots(1, 2, figsize=(12, 4.2))
    for ax, key, title in (
        (axes[0], "raised", "하한보다 높게 답한 문항 수 (주입 제외 61)"),
        (axes[1], "below_floor", "하한보다 낮게 답한 문항 수"),
    ):
        for off, ctx, color, label in (
            (-w, "sentence", plotstyle.ACCENT, "상황 문장"),
            (0, "operational", plotstyle.GREY, "운영 집계 요약"),
            (w, "structured", plotstyle.GREY_DARK, "집계 요약 + 관측값 2개"),
        ):
            ax.bar(x + off, [c[j][f"{key}_{ctx}"] for j in judges], w, color=color, label=label)
        ax.set_xticks(x, judges, rotation=25, ha="right", fontsize=8)
        ax.set_title(title, fontsize=10)
    axes[0].legend(fontsize=8)
    p = s["c25_structured"]["rep0"]["pooled"]
    fig.suptitle(
        f"C2.5: 올림 차이 중 돌아온 몫 {p['returned_fraction']:.2f} (문장 {p['raised_sentence']} → 요약 "
        f"{p['raised_operational']} → 관측값 {p['raised_structured']}), 전체 7 판정자 합",
        fontsize=11,
    )
    return plotstyle.save(fig, exp_dir, "c25_structured_observations")


def make_all(summary: dict[str, Any], exp_dir: Path) -> list[Path]:
    return [
        fig_h51(summary, exp_dir),
        fig_h52(summary, exp_dir),
        fig_h53(summary, exp_dir),
        fig_add5(summary, exp_dir),
        fig_c25(summary, exp_dir),
    ]
