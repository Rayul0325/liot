"""E13 figures (Korean labels), drawn from results/summary.json only."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from experiments.common import plotstyle

FLIPS = (4, 8, 15, 28, 35)
ARM_LABEL = {"explicit_only": "[그만해]만", "lr30": "운영(학습30%)", "r1": "(1) 모아보기 만료 제외",
             "r2": "(2) 두 번째에 차단", "r3": "(3) 수락 시 해제", "r123": "(1)+(2)+(3)"}
PERSONA_LABEL = {"all": "3성향 합침", "hater": "거부형", "ignorer": "무시형", "swap": "뒤바뀜형"}


def _c46(summary: dict[str, Any], exp_dir: Path) -> Path:
    import matplotlib.pyplot as plt

    plotstyle.setup()
    fig, axes = plt.subplots(1, 2, figsize=(11.5, 4.2))
    ax = axes[0]
    for rest, color in ((7, plotstyle.ACCENT), (14, "#C48A2C")):
        xs, ys, lo, hi = [], [], [], []
        for i, f in enumerate(FLIPS):
            c = summary["c46"]["contrast"].get(f"all|flip{f}|rest{rest}-rest30")
            if not c:
                continue
            ci = c["offered_unhelp_imm"]["d1_60"]
            xs.append(i + (0.12 if rest == 14 else -0.12))
            ys.append(ci["mean"])
            lo.append(ci["mean"] - ci["lo"])
            hi.append(ci["hi"] - ci["mean"])
        ax.errorbar(xs, ys, yerr=[lo, hi], fmt="o", color=color, capsize=3, label=f"쉬기 {rest}일 - 30일")
    ax.axhline(0.05, color="#B24A3E", ls="--", lw=1, label="등록 기준 0.05건/일")
    ax.axhline(0, color=plotstyle.GREY, lw=0.8)
    ax.set_xticks(range(len(FLIPS)), [f"{f}일" for f in FLIPS])
    ax.set_xlabel("선호가 바뀐 날")
    ax.set_ylabel("쓸데없는 폰 알림 증가 (건/일, 1~60일, 95% 구간)")
    ax.set_title("C4.6 등록 시험: 쉬는 기간을 줄였을 때의 부작용")
    ax.legend(fontsize=8, frameon=False)
    ax = axes[1]
    for rest, color in ((7, plotstyle.ACCENT), (14, "#C48A2C"), (30, plotstyle.GREY_DARK)):
        ys = []
        for f in FLIPS:
            c = summary["c46"]["cells"].get(f"all|flip{f}|rest{rest}")
            ys.append(None if not c else c["re_accept"]["median_days_censored_as_horizon_plus_1"])
        ax.plot(range(len(FLIPS)), ys, "o-", color=color, label=f"쉬기 {rest}일")
    ax.set_xticks(range(len(FLIPS)), [f"{f}일" for f in FLIPS])
    ax.set_xlabel("선호가 바뀐 날")
    ax.set_ylabel("새 장면 첫 수락까지 중앙 일수(못 한 실행은 기간+1)")
    ax.set_title("다시 수락 일수 (구조상 결과 — 판정에 쓰지 않음)")
    ax.legend(fontsize=8, frameon=False)
    fig.tight_layout()
    return plotstyle.save(fig, exp_dir, "c46_rest_x_change_day")


def _c47(summary: dict[str, Any], exp_dir: Path) -> Path:
    import matplotlib.pyplot as plt

    plotstyle.setup()
    fig, axes = plt.subplots(1, 2, figsize=(11.5, 4.2), sharey=True)
    arms = ("explicit_only", "lr30", "r1", "r2", "r3", "r123")
    for ax, variant, title in ((axes[0], "main", "모아보기 80% 열람"),
                               (axes[1], "tray0", "tray0: 아무도 안 엶(실제 데몬)")):
        for i, arm in enumerate(arms):
            ci = summary["c47"]["vs_none"].get(f"{variant}|all|{arm}", {}).get("d31_60")
            if not ci or ci["ratio"] is None:
                continue
            ax.errorbar([i], [ci["ratio"]], yerr=[[ci["ratio"] - ci["lo"]], [ci["hi"] - ci["ratio"]]],
                        fmt="o", color=plotstyle.ACCENT if arm == "r1" else plotstyle.GREY_DARK,
                        capsize=3)
        ax.axhline(0.9, color="#3E8E7E", ls="--", lw=1, label="지지 하한 0.90")
        ax.axhline(0.8, color="#B24A3E", ls="--", lw=1, label="반증 상한 0.80")
        ax.set_xticks(range(len(arms)), [ARM_LABEL[a] for a in arms], rotation=25, ha="right", fontsize=8)
        ax.set_title(title)
    axes[0].set_ylabel("폰 장면 수락된 도움(31~60일) ÷ 학습 없음")
    axes[0].legend(fontsize=8, frameon=False)
    fig.suptitle("C4.7 대안 규칙: 학습 없음 대비 폰 장면 수락된 도움 (95% 구간)", fontsize=11)
    fig.tight_layout()
    return plotstyle.save(fig, exp_dir, "c47_alternative_rules")


def _c48(summary: dict[str, Any], exp_dir: Path) -> Path:
    import matplotlib.pyplot as plt

    plotstyle.setup()
    fig, ax = plt.subplots(figsize=(6.5, 4.2))
    efforts = summary["c48"]["efforts"]
    for g, color in (("all", plotstyle.ACCENT), ("hater", "#8C8C8C"), ("ignorer", "#C48A2C"),
                     ("swap", "#8E5A9B")):
        pts = [summary["c48"]["points"][f"{g}|{e:.3f}"] for e in efforts]
        ax.errorbar(efforts, [p["mean"] for p in pts],
                    yerr=[[p["mean"] - p["lo"] for p in pts], [p["hi"] - p["mean"] for p in pts]],
                    fmt="o-" if g == "all" else "o--", color=color, capsize=2,
                    lw=2 if g == "all" else 1, label=PERSONA_LABEL[g])
    ax.set_xlabel("수고 계수")
    ax.set_ylabel("수락률 A - B (두 조건이 모두 물은 폰 알림)")
    ax.set_title("C4.8 수고 계수 스윕 (구조상 결과)")
    ax.legend(fontsize=8, frameon=False)
    fig.tight_layout()
    return plotstyle.save(fig, exp_dir, "c48_effort_sweep")


def make_all(exp_dir: Path, summary: dict[str, Any]) -> list[Path]:
    return [_c46(summary, exp_dir), _c47(summary, exp_dir), _c48(summary, exp_dir)]
