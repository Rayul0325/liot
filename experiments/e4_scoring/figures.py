"""E4 figures (made from the summary only)."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import matplotlib.pyplot as plt
import numpy as np

from experiments.common import plotstyle

FAM = ["lin", "geo2", "geo3", "geo10", "zero_based"]
FAM_KO = {"lin": "1-2-3(-4)", "geo2": "1-2-4(-8)", "geo3": "1-3-9(-27)", "geo10": "1-10-100(-1000)",
          "zero_based": "0부터"}


def fig_family_heatmap(s: dict[str, Any], exp_dir: Path) -> Path:
    plotstyle.setup()
    by = s["mult_grid"]["M3"]["by_family"]
    mat = np.zeros((len(FAM), len(FAM)))
    for i, h in enumerate(FAM):
        for j, t in enumerate(FAM):
            mat[i, j] = by[f"{h}|{t}"]["max_agree"]
    fig, ax = plt.subplots(figsize=(6.2, 4.4))
    im = ax.imshow(mat, cmap="Blues", vmin=8, vmax=24)
    ax.set_xticks(range(len(FAM)), [FAM_KO[f] for f in FAM], rotation=30, ha="right")
    ax.set_yticks(range(len(FAM)), [FAM_KO[f] for f in FAM])
    ax.set_xlabel("속도 점수 척도")
    ax.set_ylabel("피해 점수 척도")
    ax.grid(False)
    for i in range(len(FAM)):
        for j in range(len(FAM)):
            ax.text(j, i, int(mat[i, j]), ha="center", va="center",
                    color="white" if mat[i, j] > 18 else plotstyle.GREY_DARK, fontsize=9)
    ax.set_title("곱셈 점수가 조회표와 같은 등급을 낼 수 있는 최대 칸 수 (24칸 중)")
    fig.colorbar(im, ax=ax, shrink=0.8)
    return plotstyle.save(fig, exp_dir, "mult_best_agreement_by_coding")


def fig_matrices(s: dict[str, Any], exp_dir: Path) -> Path:
    plotstyle.setup()
    fig, axes = plt.subplots(1, 2, figsize=(12.5, 5.2), gridspec_kw={"width_ratios": [4, 8]})
    for ax, key, title in (
        (axes[0], "disagreement_u24", "24가지 입력 — 방식 간 순서 불일치"),
        (axes[1], "disagreement_u8_categories", "안전 카테고리 8종 — 방식 간 순서 불일치"),
    ):
        d = s[key]
        names = d["methods"]
        mat = np.array(d["pair_relation_disagreement"], dtype=float)
        ax.imshow(mat, cmap="Greys", vmin=0, vmax=1)
        ax.set_xticks(range(len(names)), names, rotation=60, ha="right", fontsize=7)
        ax.set_yticks(range(len(names)), names, fontsize=7)
        ax.grid(False)
        for i in range(len(names)):
            for j in range(len(names)):
                ax.text(j, i, f"{mat[i, j]:.2f}", ha="center", va="center", fontsize=6,
                        color="white" if mat[i, j] > 0.5 else plotstyle.GREY_DARK)
        ax.set_title(title, fontsize=10)
    fig.suptitle("두 입력의 대소 관계(<, =, >)가 다르게 매겨진 쌍의 비율", fontsize=11)
    return plotstyle.save(fig, exp_dir, "inter_method_disagreement")


def fig_model_lowering(s: dict[str, Any], exp_dir: Path) -> Path:
    plotstyle.setup()
    models = [m for m, e in s["model_only"]["models"].items() if e.get("used_in_inference") and "severity_injection_lower" in e]
    labels, sev, tier, base = [], [], [], []
    for m in models:
        e = s["model_only"]["models"][m]
        labels.append(m)
        sev.append(e["severity_injection_lower"]["below_floor"] / e["severity_injection_lower"]["n"])
        tier.append(e["tier_injection_lower"]["below_floor"] / e["tier_injection_lower"]["n"])
        base.append(e["tier_noninjection_all"]["below_floor"] / e["tier_noninjection_all"]["n"])
    x = np.arange(len(models))
    fig, ax = plt.subplots(figsize=(8.2, 4.0))
    w = 0.27
    ax.bar(x - w, sev, w, label="파급도 질문 · '낮춰라' 4건", color=plotstyle.ACCENT)
    ax.bar(x, tier, w, label="확인 강도 질문 · '낮춰라' 8건", color=plotstyle.GREY)
    ax.bar(x + w, base, w, label="확인 강도 질문 · 주입 없는 45건(기준선)", color=plotstyle.GREY_LIGHT)
    ax.set_xticks(x, labels, rotation=20, ha="right", fontsize=8)
    ax.set_ylabel("하한보다 낮게 답한 비율")
    ax.set_ylim(0, 1.05)
    ax.legend(fontsize=8)
    ax.set_title("모델 단독 판정이 하한 아래로 내려간 비율 (최종값은 하한이 지킨다)")
    return plotstyle.save(fig, exp_dir, "model_only_lowering")


def make_all(summary: dict[str, Any], exp_dir: Path) -> list[Path]:
    return [
        fig_family_heatmap(summary, exp_dir),
        fig_matrices(summary, exp_dir),
        fig_model_lowering(summary, exp_dir),
    ]
