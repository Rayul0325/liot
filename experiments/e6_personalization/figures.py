"""E6 figures (Korean labels). Reads raw/ for the timelines and summary.json for intervals."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import numpy as np

from experiments.common import io, plotstyle
from experiments.e6_personalization import config

ARM_LABEL = {
    "none": "학습·차단 없음", "explicit_only": "[그만해]만(30일 차단)", "lr10": "학습 10%",
    "lr30": "학습 30% (운영)", "lr60": "학습 60%", "permanent": "영구 차단",
    "no_gate": "30일 차단 없음", "explore7": "7일마다 재제안",
}
ARM_COLOR = {
    "none": "#3F3F3F", "explicit_only": "#8C8C8C", "lr10": "#6E9BC5", "lr30": plotstyle.ACCENT,
    "lr60": "#8E5A9B", "permanent": "#B24A3E", "no_gate": "#3E8E7E", "explore7": "#C48A2C",
}
PERSONA_LABEL = {"hater": "거부형(그만해 누름)", "ignorer": "무시형(안 누름)", "swap": "뒤바뀜형"}


def _rolling(x: np.ndarray, w: int = 7) -> np.ndarray:
    pad = np.concatenate([np.full(w - 1, np.nan), x])
    return np.array([np.nanmean(pad[i:i + w]) if i >= w - 1 else np.nan for i in range(len(x))])


def _timeline(rows: list[dict[str, Any]], variant: str, cond: str, key: str, fig_name: str,
              title: str, ylabel: str, exp_dir: Path) -> Path:
    import matplotlib.pyplot as plt

    plotstyle.setup()
    fig, axes = plt.subplots(1, 3, figsize=(13, 3.8), sharey=True)
    for ax, persona in zip(axes, config.PERSONAS, strict=True):
        for arm in config.ARMS:
            sel = [r for r in rows if (r["variant"], r["persona"], r["arm"], r["cond"])
                   == (variant, persona, arm, cond)]
            if not sel:
                continue
            if key == "acc_help":
                mat = np.array([np.asarray(r["acc_help_imm"]) + np.asarray(r["acc_help_tray"])
                                for r in sel], dtype=float)
            else:
                mat = np.array([r[key] for r in sel], dtype=float)
            ax.plot(range(1, mat.shape[1] + 1), _rolling(mat.mean(axis=0)), color=ARM_COLOR[arm],
                    lw=2.2 if arm == "lr30" else 1.4, label=ARM_LABEL[arm])
        ax.axvline(config.FLIP_DAY, color=plotstyle.GREY, ls="--", lw=1)
        ax.set_title(PERSONA_LABEL[persona])
        ax.set_xlabel("날 (7일 이동평균)")
    axes[0].set_ylabel(ylabel)
    handles, labels = axes[0].get_legend_handles_labels()
    fig.legend(handles, labels, loc="lower center", ncol=4, fontsize=8, frameon=False)
    fig.suptitle(title, fontsize=11)
    fig.subplots_adjust(bottom=0.27, top=0.85)
    return plotstyle.save(fig, exp_dir, fig_name)


def _recovery_fig(summary: dict[str, Any], exp_dir: Path) -> Path:
    import matplotlib.pyplot as plt

    plotstyle.setup()
    fig, axes = plt.subplots(1, 3, figsize=(13, 3.6), sharey=True)
    for ax, persona in zip(axes, config.PERSONAS, strict=True):
        xs, ys, lo, hi = [], [], [], []
        for i, arm in enumerate(config.ARMS):
            g = summary["groups"].get(f"main|{persona}|{arm}|B")
            if g is None:
                continue
            c = g["re_accept"]
            xs.append(i)
            ys.append(c["fraction"])
            lo.append(c["fraction"] - c["lo"])
            hi.append(c["hi"] - c["fraction"])
        ax.bar(xs, ys, yerr=[lo, hi], color=[ARM_COLOR[a] for a in config.ARMS][:len(xs)], capsize=2)
        for i, arm in enumerate(config.ARMS):
            g = summary["groups"].get(f"main|{persona}|{arm}|B")
            med = None if g is None else g["re_accept"]["median_days"]
            ax.text(i, 1.01, "-" if med is None else f"{med:.0f}일", ha="center", fontsize=7)
        ax.set_xticks(range(len(config.ARMS)))
        ax.set_xticklabels([ARM_LABEL[a] for a in config.ARMS], rotation=60, ha="right", fontsize=7)
        ax.set_title(PERSONA_LABEL[persona])
        ax.set_ylim(0, 1.12)
    axes[0].set_ylabel("바뀐 뒤 45일 안에 한 번이라도 수락된 씨앗 비율")
    fig.suptitle("선호가 바뀐 장면을 다시 수락하는가 (조건 B, 막대=95% 구간, 위 숫자=처음 수락까지 중앙 일수)",
                 fontsize=10)
    fig.subplots_adjust(bottom=0.35, top=0.82)
    return plotstyle.save(fig, exp_dir, "reaccept_fraction")


def _ab_fig(summary: dict[str, Any], exp_dir: Path) -> Path:
    import matplotlib.pyplot as plt

    plotstyle.setup()
    fig, ax = plt.subplots(figsize=(8, 3.8))
    labels, data = [], []
    for key, g in sorted(summary["h43"].items()):
        variant, persona, arm = key.split("|")
        if persona != "hater" and variant == "main":
            continue
        labels.append(f"{variant}/{arm}" if variant == "main" else f"{variant}/{persona}")
        data.append(g["diff_A_minus_B_per_seed"])
    ys = [d["mean"] if d["mean"] is not None else np.nan for d in data]
    err = [[(d["mean"] - d["lo"]) if d["mean"] is not None else 0 for d in data],
           [(d["hi"] - d["mean"]) if d["mean"] is not None else 0 for d in data]]
    ax.errorbar(range(len(data)), ys, yerr=err, fmt="o", color=plotstyle.ACCENT, capsize=3)
    ax.axhline(0, color=plotstyle.GREY, lw=1)
    ax.set_xticks(range(len(data)))
    ax.set_xticklabels(labels, rotation=60, ha="right", fontsize=7)
    ax.set_ylabel("수락률 차이 (A - B), 두 조건이 모두 물은 항목")
    ax.set_title("확인 방식 A·B 의 수락률 차이 (거주자 모델 의존)")
    return plotstyle.save(fig, exp_dir, "ab_acceptance_diff")


def make_all(exp_dir: Path, summary: dict[str, Any]) -> list[Path]:
    rows = io.read_jsonl(exp_dir / "raw" / "runs.jsonl")
    return [
        _timeline(rows, "main", "B", "acc_help", "accepted_helpful_timeline",
                  "하루 수락된 도움 제안 수 (조건 B, 씨앗 평균)", "하루 수락된 도움 제안", exp_dir),
        _timeline(rows, "main", "B", "ws_help_phone", "wrongly_silenced_timeline",
                  "하루 잘못 꺼진 도움 제안 수 (폰 장면, 조건 B)", "하루 잘못 꺼진 도움 제안", exp_dir),
        _recovery_fig(summary, exp_dir),
        _ab_fig(summary, exp_dir),
    ]
