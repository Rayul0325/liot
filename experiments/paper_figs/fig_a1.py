"""Style A (paper) figures 1-6: verdict map, E1 latency, E14 kill windows, E3 plane/forest/ambient."""

from __future__ import annotations

from typing import Any

import matplotlib.pyplot as plt
import numpy as np
from matplotlib.lines import Line2D
from matplotlib.patches import Patch, Rectangle

from experiments.kit.figures import effective_verdicts
from experiments.paper_figs import paper_style as ps
from experiments.paper_figs.data import D, S, begin, key_number, meta, verdict

E1 = "e1_latency"
E3 = "e3_policy"
E14 = "e14_guardian_latency"
E14_AFTER = "kit:e14_killsweep_after_u6"


def fmt_ms(x: float) -> str:
    return f"{x:.1f}" if x < 10 else f"{x:.0f}"


# =============================================================================== F01 verdict map
VERDICT_DRAW: dict[str, dict[str, Any]] = {
    "지지": {"fc": ps.ACCENT, "ec": ps.ACCENT, "hatch": None, "ls": "-"},
    "반증": {"fc": ps.INK, "ec": ps.INK, "hatch": None, "ls": "-"},
    "판정 불가": {"fc": "white", "ec": ps.G_DARK, "hatch": None, "ls": (0, (2, 1.2)), "glyph": "?"},
    "구조상 결과": {"fc": "white", "ec": ps.G_DARK, "hatch": "////", "ls": "-"},
    "기술 결과": {"fc": ps.G_PALE, "ec": ps.G_MID, "hatch": None, "ls": "-"},
    "읽기에 따라 갈림": {"fc": "white", "ec": ps.G_DARK, "hatch": "xxxx", "ls": "-"},
    "등록 결론 불충족": {"fc": ps.G_LIGHT, "ec": ps.G_DARK, "hatch": "++++", "ls": "-"},
    "대기": {"fc": "white", "ec": ps.G_LIGHT, "hatch": None, "ls": ":"},
}
CYCLE_NAMES = {1: "사이클 1\n등록 시험", 2: "사이클 2\n원인 가설", 3: "사이클 3\n추가 시험", 4: "사이클 4\n탐색 전용"}


def f01() -> list[Any]:
    begin("F01")
    import yaml

    from experiments.kit.check_numbers import KIT_DIR

    spec = yaml.safe_load((KIT_DIR / "hypotheses.yaml").read_text(encoding="utf-8"))
    rows, _ = effective_verdicts()
    for r in rows:
        verdict(r["id"], r["shown"])
    qids = list(spec["questions"].keys())
    row_h, head_h, top, bottom = 0.145, 0.2, 0.62, 0.62
    n_rows = len(rows)
    height = top + n_rows * row_h + len(qids) * head_h + bottom
    fig = plt.figure(figsize=(ps.W2, height))
    ax = fig.add_axes((0, 0, 1, 1))
    ax.set_xlim(0, ps.W2)
    ax.set_ylim(height, 0)
    ax.axis("off")
    x_id, x_cols, x_name = 0.05, [1.25, 1.85, 2.45, 3.05], 3.55
    for c, xc in enumerate(x_cols, start=1):
        ax.text(xc, 0.12, CYCLE_NAMES[c], ha="center", va="top", fontsize=6.3, color=ps.INK, linespacing=1.15)
    ax.text(x_id, 0.12, "가설", ha="left", va="top", fontsize=7, fontweight="bold")
    ax.text(x_name, 0.12, "내용 (최종 판정은 칸의 무늬·색)", ha="left", va="top", fontsize=7, fontweight="bold")
    y = top
    size = 0.1
    for qid in qids:
        group = [r for r in rows if r["q"] == qid]
        ax.add_patch(Rectangle((0, y), ps.W2, head_h * 0.86, facecolor=ps.G_PALE, edgecolor="none"))
        ax.text(x_id, y + head_h * 0.43, spec["questions"][qid]["short"], fontsize=7.2, fontweight="bold", va="center")
        y += head_h
        for r in group:
            cy = y + row_h / 2
            ax.text(x_id, cy, r["id"], fontsize=6.6, fontweight="bold", va="center", color=ps.INK)
            ax.text(x_name, cy, r["name"], fontsize=6.6, va="center", color=ps.INK)
            st = VERDICT_DRAW[r["shown"]]
            xc = x_cols[r["cycle"] - 1]
            ax.add_patch(
                Rectangle(
                    (xc - size / 2, cy - size / 2),
                    size,
                    size,
                    facecolor=st["fc"],
                    edgecolor=st["ec"],
                    hatch=st["hatch"],
                    linewidth=0.6,
                    linestyle=st["ls"],
                )
            )
            if st.get("glyph"):
                ax.text(xc, cy + 0.003, st["glyph"], ha="center", va="center", fontsize=5.5, color=ps.G_DARK)
            ax.plot([x_cols[0] - 0.1, x_cols[-1] + 0.1], [cy, cy], color=ps.G_GRID, lw=0.3, zorder=0)
            y += row_h
    shown = [v for v in VERDICT_DRAW if any(r["shown"] == v for r in rows)]
    handles = [
        Patch(facecolor=VERDICT_DRAW[v]["fc"], edgecolor=VERDICT_DRAW[v]["ec"], hatch=VERDICT_DRAW[v]["hatch"], linewidth=0.6, label=v)
        for v in shown
    ]
    leg = fig.legend(handles=handles, loc="lower center", ncol=len(handles), fontsize=6.6, handlelength=1.1, columnspacing=1.0, bbox_to_anchor=(0.5, 0.012))
    leg.set_title("판정 말 (최종 판정, 사후 재판정 반영)", prop={"size": 6.8})
    meta(
        "F01",
        "그림 1",
        "가설 판정 지도 (사이클 1~4)",
        "연구 질문(Q1~Q6)별로 모든 가설의 최종 판정을 사이클 열에 한 칸씩 표시했다. 판정 말은 자료집(hypotheses.yaml)의 말을 그대로 쓰며, "
        "'읽기에 따라 갈림'과 '등록 결론 불충족'은 하나로 합치지 않았다. 사이클 4는 전부 탐색이므로 지지된 개수를 세지 않는다.",
        "E1~E16 전체",
        "구조상 결과(빗금)는 비교 결론에 쓰지 않는다. 같은 가설의 사전 등록 형태와 사후 재판정은 별도 칸이다(H2.1 사후, H1.2 전/후).",
    )
    return ps.save(fig, "fig01_verdict_map")


# =============================================================================== F02 E1 latency
ARMS = [
    ("liot", "LIOT (표 먼저)"),
    ("wait_for_model", "모델 뒤 발송"),
    ("short_0.25", "짧은 마감 0.25 s"),
    ("short_0.5", "짧은 마감 0.5 s"),
    ("short_1.0", "짧은 마감 1.0 s"),
]
CONDS = [("normal", "모델 정상"), ("slow", "모델 느림"), ("timeout", "마감 초과"), ("raising", "모델 예외")]


def f02() -> list[Any]:
    begin("F02")
    fig = plt.figure(figsize=(ps.W2, 2.75))
    gs = fig.add_gridspec(1, 6, width_ratios=[1, 1, 1, 1, 0.12, 1.55], wspace=0.12, left=0.115, right=0.995, top=0.86, bottom=0.26)
    axes = [fig.add_subplot(gs[0, i]) for i in range(4)]
    ys = np.arange(len(ARMS))[::-1]
    for ax, (cond, cname) in zip(axes, CONDS, strict=True):
        for y, (arm, _lab) in zip(ys, ARMS, strict=True):
            cell = (arm + "/" + cond)
            med = S(E1, "main", "cells", cell, "median", scale=1000)
            mlo = S(E1, "main", "cells", cell, "median_lo", scale=1000)
            mhi = S(E1, "main", "cells", cell, "median_hi", scale=1000)
            p95 = S(E1, "main", "cells", cell, "p95", scale=1000)
            plo = S(E1, "main", "cells", cell, "p95_lo", scale=1000)
            phi = S(E1, "main", "cells", cell, "p95_hi", scale=1000)
            liot = arm == "liot"
            col = ps.ACCENT if liot else ps.G_MID
            fc = col if liot else "white"
            ax.plot([mlo, mhi], [y + 0.12, y + 0.12], color=col, lw=1.2, solid_capstyle="butt")
            ax.plot([med], [y + 0.12], "o", ms=4.2, mfc=fc, mec=col, mew=0.9)
            ax.plot([plo, phi], [y - 0.14, y - 0.14], color=col, lw=1.2, solid_capstyle="butt")
            ax.plot([p95], [y - 0.14], "^", ms=4.2, mfc=fc, mec=col, mew=0.9)
            if cond in ("normal", "slow") or liot:
                ax.text(max(p95, med) * 1.5, y, fmt_ms(p95), fontsize=6, va="center", color=ps.G_DARK)
        ax.set_xscale("log")
        ax.set_xlim(1, 12000)
        ax.set_ylim(-0.7, len(ARMS) - 0.3)
        ax.set_title(cname, fontsize=8, pad=3)
        ax.set_xticks([1, 10, 100, 1000])
        ax.set_xticklabels(["1", "10", "100", "1000"])
        ps.gridx(ax)
        if ax is axes[0]:
            ax.set_yticks(ys)
            ax.set_yticklabels([lab for _a, lab in ARMS])
            ax.get_yticklabels()[0].set_color(ps.ACCENT)
            ax.get_yticklabels()[0].set_fontweight("bold")
        else:
            ax.set_yticks([])
            ax.spines["left"].set_visible(False)
    axes[0].set_xlabel("")
    fig.text(0.115 + (0.995 - 0.115) * 0.27, 0.115, "첫 경보 지연 (ms, 로그 눈금) · ↓ 낮을수록 좋음", ha="center", fontsize=8)
    ps.panel(axes[0], "a", dx=-0.52)
    handles = [
        Line2D([], [], marker="o", ls="", mfc=ps.ACCENT, mec=ps.ACCENT, ms=4.2, label="중앙값 (95% 구간)"),
        Line2D([], [], marker="^", ls="", mfc=ps.ACCENT, mec=ps.ACCENT, ms=4.2, label="p95 (95% 구간)"),
        Line2D([], [], marker="o", ls="", mfc="white", mec=ps.G_MID, ms=4.2, label="속 빈 표시 = 대안 (구조상)"),
    ]
    handles += [
        Line2D([], [], marker="s", ls="", mfc=ps.G_DARK, mec=ps.G_DARK, ms=4.2, label="b: U1 전"),
        Line2D([], [], marker="o", ls="", mfc=ps.ACCENT, mec=ps.ACCENT, ms=4.2, label="b: U1 후"),
    ]
    fig.legend(handles=handles, loc="lower left", ncol=5, bbox_to_anchor=(0.0, -0.05), fontsize=6.6, columnspacing=1.0)

    ax = fig.add_subplot(gs[0, 5])
    delays = ["0.5", "1.5", "3.0"]
    crit = key_number(E1, ["h12", "after", "1.5", "pooled"], "after_criterion_met_p95_diff_le_")
    xs = np.arange(len(delays))
    for off, phase, col, mk, lab in [(-0.16, "before", ps.G_DARK, "s", "U1 전"), (0.16, "after", ps.ACCENT, "o", "U1 후")]:
        for x, d in zip(xs, delays, strict=True):
            base = ("h12", phase, d, "pooled")
            v = S(E1, *base, "p95_minus_control_p95")
            lo = S(E1, *base, "p95_diff_lo")
            hi = S(E1, *base, "p95_diff_hi")
            ax.plot([x + off, x + off], [lo, hi], color=col, lw=1.3)
            ax.plot([x + off], [v], mk, ms=4.5, mfc=col, mec=col)
            ax.text(x + off, hi + (0.15 * abs(hi) + 0.004 if hi > 0.05 else 0.004) * (1.0 if phase == "before" else 1.0), f"{v:.3g}", fontsize=6, ha="center", va="bottom", color=ps.G_DARK)
        ax.plot([], [], mk, ms=4.5, mfc=col, mec=col, label=lab)
    ax.axhline(crit, color=ps.G_MID, lw=0.7, ls="--")
    ax.text(len(delays) - 0.5, crit * 1.25, f"사전 기준 {crit:g} s", fontsize=6.5, ha="right", va="bottom", color=ps.G_DARK)
    ax.set_yscale("symlog", linthresh=0.01, linscale=0.6)
    ax.set_ylim(-0.03, 6)
    ax.set_yticks([0, 0.01, 0.1, 1])
    ax.set_yticklabels(["0", "0.01", "0.1", "1"])
    ax.set_xticks(xs)
    ax.set_xticklabels([f"{d} s" for d in delays])
    ax.set_xlim(-0.6, len(delays) - 0.4)
    ax.set_xlabel("편의 판정 시간 (대조군에 더한 값)")
    ax.set_ylabel("가스 첫 경보 p95 - 대조군 (s) · ↓")
    ps.gridy(ax)
    ps.panel(ax, "b", dx=-0.2)
    liot_p95 = S(E1, "main", "cells", "liot/normal", "p95", scale=1000)
    wait_med = S(E1, "main", "delta_vs_liot", "wait_for_model/timeout", "median", "diff", scale=1000)
    after15 = S(E1, "h12", "after", "1.5", "pooled", "p95_minus_control_p95", scale=1000)
    before15 = S(E1, "h12", "before", "1.5", "pooled", "p95_minus_control_p95")
    meta(
        "F02",
        "그림 2",
        "E1 첫 경보 지연: 구조 × 모델 조건, 그리고 U1 전후",
        f"a: 구조 5종(행) × 모델 조건 4종(열)의 첫 경보 지연. LIOT 는 모델 조건과 무관하게 p95 {liot_p95:.1f} ms(모델 정상)이고, 모델을 기다리는 "
        f"대안과의 차이(마감 초과에서 {wait_med:.0f} ms)는 '대안이 기다리도록 정의됐다'는 데서 나오는 구조상 결과다. "
        f"b: 편의 판정이 이벤트 루프를 막던 U1 전에는 가스 첫 경보 p95 가 대조군보다 {before15:.2f} s(1.5 s 편의 판정) 늦었고, "
        f"U1 후에는 {after15:.1f} ms 차이로 사전 기준 {crit:g} s 안에 들어왔다.",
        "E1",
        "모델 지연·올림 확률·마감은 가정이고 송신기는 가짜다. 부하가 높은 한 대의 기계에서 번갈아 잰 상대 비교이며 폰까지 닿는 시간은 재지 않았다. "
        "U1 전/후는 같은 시간대 대조군 대비 차이다. 구간은 로그 눈금에서 마커보다 좁다.",
    )
    return ps.save(fig, "fig02_e1_latency")


# =============================================================================== F03 E14 kill windows
def f03() -> list[Any]:
    begin("F03")
    cats = {"gas": "가스", "door": "문"}
    cls_style = {
        "ok": {"fc": ps.ACCENT_TINT, "ec": ps.ACCENT, "hatch": None, "label": "정상 (첫 알림 + 사다리)"},
        "nothing": {"fc": "white", "ec": ps.G_MID, "hatch": "xxxx", "label": "읽기가 남지 않음 (복구 불가 구간)"},
        "noladder": {"fc": ps.G_MID, "ec": ps.G_MID, "hatch": None, "label": "첫 알림 나감, 사다리 없음"},
        "silent": {"fc": ps.INK, "ec": ps.INK, "hatch": None, "label": "침묵 (첫 알림 없음)"},
    }
    fig = plt.figure(figsize=(ps.W2, 2.7))
    gs = fig.add_gridspec(1, 2, wspace=0.5, left=0.1, right=0.93, top=0.8, bottom=0.27)
    axes = [fig.add_subplot(gs[0, i]) for i in range(2)]
    sets = [("U6 전 (E14 전수 절단 탐색)", E14, ["killsweep", "windows"]), ("U6 후 (같은 탐색 재측정)", E14_AFTER, ["windows"])]
    tallies: dict[str, dict[str, int]] = {}
    for ai, (ax, (title, exp, base)) in enumerate(zip(axes, sets, strict=True)):
        for wi in range(4):
            row = [*base, wi]
            n_st = S(exp, *row, "statements")
            silent = S(exp, *row, "silent_kill_points")
            nol = S(exp, *row, "alerted_but_no_ladder_kill_points")
            noth = S(exp, *row, "nothing_durable_kill_points")
            cat = S(exp, *row, "category")
            vul = S(exp, *row, "vulnerable")
            y = 3 - wi
            for i in range(1, 25):
                if i > n_st:
                    ax.add_patch(Rectangle((i - 0.5, y - 0.38), 1, 0.76, facecolor="white", edgecolor=ps.G_PALE, linewidth=0.4, linestyle=":"))
                    continue
                kind = "silent" if i in silent else "noladder" if i in nol else "nothing" if i in noth else "ok"
                st = cls_style[kind]
                ax.add_patch(
                    Rectangle((i - 0.5, y - 0.38), 1, 0.76, facecolor=st["fc"], edgecolor="white" if kind != "nothing" else ps.G_MID, hatch=st["hatch"], linewidth=0.6 if kind != "nothing" else 0.3)
                )
            label = f"{cats[cat]} · {'취약 거주자' if vul else '일반 가구'}"
            if ai == 0:
                ax.text(0.2, y, label, ha="right", va="center", fontsize=7.2)
            tallies.setdefault(title, {})[label] = len(silent)
            ax.text(24.9, y, f"침묵 {len(silent)}\n사다리 없음 {len(nol)}", ha="left", va="center", fontsize=6.2, linespacing=1.1, color=ps.G_DARK)
        ax.set_xlim(0.3, 24.7)
        ax.set_ylim(-0.6, 3.6)
        ax.set_yticks([])
        ax.set_xticks([1, 4, 8, 12, 16, 20, 24])
        ax.set_xlabel("안전 읽기 하나를 처리하는 중 프로세스를 끊은 DB 문장 번호 (1~24)")
        ax.spines["left"].set_visible(False)
        ax.set_title(title, fontsize=8, pad=4, loc="left")
        ps.panel(ax, "ab"[ai], dx=-0.0 if ai else -0.0, dy=1.1)
        ax.set_xticks([1, 4, 8, 12, 16, 20, 24])
        ax.spines["right"].set_visible(False)
        ax.margins(x=0)
        ax.set_anchor("W")
    handles = [Patch(facecolor=v["fc"], edgecolor=v["ec"], hatch=v["hatch"], linewidth=0.5, label=v["label"]) for v in cls_style.values()]
    fig.legend(handles=handles, loc="lower center", ncol=4, fontsize=6.8, bbox_to_anchor=(0.5, -0.01), columnspacing=1.2, handlelength=1.3)
    mid = S(E14, "fuzz", "by_mode", "midop", "episodes_with_violation", "k")
    mid_n = S(E14, "fuzz", "by_mode", "midop", "episodes")
    mid_rate = S(E14, "fuzz", "by_mode", "midop", "episodes_with_violation", "rate", scale=100)
    b_silent = len(S(E14, "killsweep", "windows", 2, "silent_kill_points"))
    b_nol = len(S(E14, "killsweep", "windows", 2, "alerted_but_no_ladder_kill_points"))
    b_st = S(E14, "killsweep", "windows", 2, "statements")
    a_silent = len(S(E14_AFTER, "windows", 2, "silent_kill_points"))
    a_nol = len(S(E14_AFTER, "windows", 2, "alerted_but_no_ladder_kill_points"))
    a_noth = len(S(E14_AFTER, "windows", 2, "nothing_durable_kill_points"))
    meta(
        "F03",
        "그림 3",
        "E14 + U6: 재시작(프로세스 중단) 시점별 게이트 1 창, U6 전후",
        f"행은 가스·문 × 취약 거주자 유무, 칸은 안전 읽기 하나를 처리하는 도중 끊긴 DB 문장 번호다. U6 이전 코드에서 가스(일반 가구)는 {b_st}문장 중 "
        f"침묵 창 {b_silent}문장, 첫 알림은 나갔지만 사다리가 없는 창 {b_nol}문장이었다(게이트 1 위반). 같은 탐색을 U6 뒤 코드에서 다시 돌리면 침묵 {a_silent}, "
        f"사다리 없음 {a_nol}이지만 읽기 자체가 남지 않는 문장 {a_noth}개는 복구할 수 없는 빈틈으로 남는다.",
        "E14 (killsweep), kit 재측정(U6 후)",
        f"'U6 후' 값은 실험 summary 가 아니라 kit 의 재측정(kit/data/e14_killsweep_after_u6.json)이다. 같은 이상 신호를 재전송해도 흡수되는 경우와 재시작 시점 재동기화 빈틈은 별도로 남는다. "
        f"무작위 중간 절단 퍼징에서 U6 이전 {mid_n}에피소드 중 {mid}건({mid_rate:.1f}%)이 위반이었으나 절단 시점을 균등 추출한 값이라 실제 빈도가 아니다.",
    )
    return ps.save(fig, "fig03_e14_kill_windows")


# =============================================================================== F04 E3 plane
THETAS = [f"{i / 10:.1f}" for i in range(11)]
PLANE_POLICIES = [
    # key, label, kind, marker
    ("flat_explicit", "모두 명시 확인", "alt", "s"),
    ("binary_ext", "이진 승인 (외부 목록)", "alt", "^"),
    ("binary_opt", "이진 승인 (사후 최적 목록)", "alt", "v"),
    ("auto_all", "모두 자동", "alt", "X"),
    ("auto_undo_w15", "자동+되돌리기 15분", "alt", "P"),
    ("liot_full", "LIOT 4단계(가상)", "liot", "o"),
    ("liot_real", "LIOT 실제(능동 상한)†", "liot_struct", "D"),
]


def _pareto_mask(pts: np.ndarray) -> np.ndarray:
    ok = np.ones(len(pts), bool)
    for i in range(len(pts)):
        dom = (pts[:, 0] <= pts[i, 0]) & (pts[:, 1] <= pts[i, 1]) & ((pts[:, 0] < pts[i, 0]) | (pts[:, 1] < pts[i, 1]))
        if dom.any():
            ok[i] = False
    return ok


LABEL_POS: dict[str, tuple[float, float, str]] = {
    # policy -> (x, y, ha) of the label text in data coordinates (arrow to the point)
    "flat_explicit": (13.2, 0.13, "right"),
    "binary_ext": (2.0, 0.93, "left"),
    "binary_opt": (7.6, 0.13, "center"),
    "auto_all": (0.5, 1.0, "left"),
    "auto_undo_w15": (0.05, 0.3, "left"),
    "liot_full": (9.9, 0.05, "center"),
    "liot_real": (3.9, 0.04, "left"),
}


def f04() -> list[Any]:
    begin("F04")
    fig, axes = plt.subplots(1, 2, figsize=(ps.W2, 3.25), sharey=True, gridspec_kw={"wspace": 0.06, "left": 0.065, "right": 0.995, "top": 0.88, "bottom": 0.22})
    pareto_share = {}
    for ai, (ax, m, mtxt) in enumerate(zip(axes, ["m0.0", "m0.1"], ["오분류 m = 0", "오분류 m = 10%"], strict=True)):
        pts: list[tuple[float, float]] = []
        names: list[str] = []
        pt_base = ("policy_table", "exp", m)
        for key, label, kind, mk in PLANE_POLICIES:
            bx = S(E3, *pt_base, key, "burden_per_day", "mean")
            bxl = S(E3, *pt_base, key, "burden_per_day", "lo")
            bxh = S(E3, *pt_base, key, "burden_per_day", "hi")
            hy = S(E3, *pt_base, key, "harm_rate", "mean")
            hyl = S(E3, *pt_base, key, "harm_rate", "lo")
            hyh = S(E3, *pt_base, key, "harm_rate", "hi")
            pts.append((bx, hy))
            names.append(key)
            if kind == "liot":
                col, mfc, ms = ps.ACCENT, ps.ACCENT, 5.2
            elif kind == "liot_struct":
                col, mfc, ms = ps.ACCENT, "white", 5.2
            else:
                col, mfc, ms = ps.G_MID, ps.G_MID if key not in ("binary_opt",) else "white", 4.6
            ax.plot([bxl, bxh], [hy, hy], color=col, lw=0.8, zorder=3)
            ax.plot([bx, bx], [hyl, hyh], color=col, lw=0.8, zorder=3)
            ax.plot([bx], [hy], mk, ms=ms, mfc=mfc, mec=col, mew=1.0, zorder=4)
            lx, ly, ha = LABEL_POS[key]
            ax.annotate(
                label,
                (bx, hy),
                xytext=(lx, ly),
                textcoords="data",
                fontsize=6.6,
                ha=ha,
                va="center",
                color=ps.ACCENT if kind != "alt" else ps.G_DARK,
                fontweight="bold" if kind != "alt" else "normal",
                arrowprops={"arrowstyle": "-", "lw": 0.4, "color": ps.G_LIGHT, "shrinkA": 1, "shrinkB": 3},
                zorder=5,
            )
        # theta sweeps (score-noise 0 and 0.5), computed from raw
        for sig, ls, lab in [("0.0", "-", "θ 정책 (점수 잡음 0)"), ("0.5", ":", "θ 정책 (점수 잡음 0.5)")]:
            tx, ty, txl, txh, tyl, tyh = [], [], [], [], [], []
            for t in THETAS:
                k = f"theta|exp|s{sig}|t{t}"
                tx.append(D("e3_policy_stat", "main", k, "burden", "mean"))
                ty.append(D("e3_policy_stat", "main", k, "harm", "mean"))
                txl.append(D("e3_policy_stat", "main", k, "burden", "lo"))
                txh.append(D("e3_policy_stat", "main", k, "burden", "hi"))
                tyl.append(D("e3_policy_stat", "main", k, "harm", "lo"))
                tyh.append(D("e3_policy_stat", "main", k, "harm", "hi"))
            ax.errorbar(tx, ty, xerr=[np.subtract(tx, txl), np.subtract(txh, tx)], yerr=[np.subtract(ty, tyl), np.subtract(tyh, ty)], fmt="none", ecolor=ps.G_LIGHT, elinewidth=0.5, zorder=1)
            ax.plot(tx, ty, ls, color=ps.G_DARK, lw=0.7, marker="o", ms=2.2, mfc="white", mec=ps.G_DARK, mew=0.5, zorder=2)
            for p in zip(tx, ty, strict=True):
                pts.append(p)
                names.append(f"theta{sig}")
            lx, ly, ia = (3.7, 0.24, 4) if sig == "0.0" else (8.4, 0.72, 6)
            ax.annotate(
                lab,
                (tx[ia], ty[ia]),
                xytext=(lx, ly),
                textcoords="data",
                fontsize=6.3,
                color=ps.G_DARK,
                ha="left",
                va="center",
                arrowprops={"arrowstyle": "-", "lw": 0.4, "color": ps.G_LIGHT, "shrinkA": 1, "shrinkB": 2},
            )
        arr = np.array(pts)
        front = _pareto_mask(arr)
        fp = arr[front]
        fp = fp[np.argsort(fp[:, 0])]
        ax.step(fp[:, 0], fp[:, 1], where="post", color=ps.INK, lw=0.9, alpha=0.9, zorder=2)
        ax.set_xlim(-0.4, 13.3)
        ax.set_ylim(-0.04, 1.08)
        ax.set_xlabel("하루 확인 부담 (건/일) · ↓ 낮을수록 좋음")
        if ai == 0:
            ax.set_ylabel("해로운 실행률 · ↓ 낮을수록 좋음")
        ps.gridx(ax)
        ps.gridy(ax)
        ax.set_title(mtxt, fontsize=8, pad=3)
        ps.panel(ax, "ab"[ai], dx=-0.0, dy=1.04) if ai else ps.panel(ax, "a", dx=-0.09, dy=1.04)
        pareto_share[m] = {k: S(E3, "pareto", "exp", "2d_burden_harm", k, "share") for k in ("flat_explicit", "liot_full", "liot_real", "binary_opt")}
    handles = [
        Line2D([], [], color=ps.INK, lw=0.9, label="평균점의 파레토 경계(모든 정책 + θ 정책)"),
        Line2D([], [], marker="D", ls="", mfc="white", mec=ps.ACCENT, ms=5, label="† 구조상: LIOT 실제는 땅거미 이상을 통째로 침묵시킴"),
        Line2D([], [], marker="o", ls="-", color=ps.G_DARK, mfc="white", ms=2.5, label="θ 정책 11점 (θ=0…1)"),
    ]
    fig.legend(handles=handles, loc="lower center", ncol=3, fontsize=6.8, bbox_to_anchor=(0.5, -0.005), columnspacing=1.3)
    real_p = pareto_share["m0.1"]["liot_real"]
    full_p = pareto_share["m0.1"]["liot_full"]
    flat_p = pareto_share["m0.1"]["flat_explicit"]
    meta(
        "F04",
        "그림 4",
        "E3 부담 대 피해 평면(오분류 m=0, m=10%)",
        f"정책마다 하루 확인 부담(x)과 해로운 실행률(y)의 400세트 평균과 95% 구간(세트 부트스트랩)이다. 4단계 전체(LIOT 가상)는 확인 부담을 줄이지만 해로운 실행률은 모두 명시 확인과 비슷하거나 높고, "
        f"LIOT 실제의 낮은 피해는 땅거미 이상을 침묵시키는 데서 오는 구조상 결과(속 빈 마름모)다. 세트마다 따로 파레토 최적인 비율은 m=10%에서 LIOT 실제 {real_p * 100:.0f}%, LIOT 가상 {full_p * 100:.2f}%, 모두 명시 확인 {flat_p * 100:.2f}%다.",
        "E3 (summary 의 policy_table, pareto) + 원자료 runs.jsonl(θ 정책)",
        "거주자 반응(피로·무심코 승인·정확도·지연)은 전부 가정이며 구간은 씨앗·세트 잡음일 뿐 가정의 불확실성은 들어 있지 않다. θ 정책(점수 상위 일부에만 확인)은 오분류 m 과 무관하게 점수 잡음으로 정해져 두 패널에서 같다. "
        "'이진 승인(사후 최적 목록)'은 이진에게 가장 유리하게 고른 목록이다. 파선·실선은 평균점 기준이며 세트별 경계가 아니다.",
    )
    return ps.save(fig, "fig04_e3_plane")


# =============================================================================== F05 forest
def f05() -> list[Any]:
    begin("F05")
    fig, axes = plt.subplots(1, 4, figsize=(ps.W2, 2.5), gridspec_kw={"width_ratios": [1, 1, 1, 1.5], "wspace": 0.62, "left": 0.07, "right": 0.985, "top": 0.82, "bottom": 0.26})
    specs = [
        ("help", 100.0, "도움 완료율 차이 (%p)", "↑ 높을수록 좋음"),
        ("harm", 100.0, "해로운 실행률 차이 (%p)", "↓ 낮을수록 좋음"),
        ("burden", 1.0, "하루 확인 부담 차이 (건/일)", "↓ 낮을수록 좋음"),
    ]
    ms = [(0.0, "m = 0"), (0.1, "m = 10%")]
    for ai, (ax, (metric, sc, xl, dtxt)) in enumerate(zip(axes[:3], specs, strict=True)):
        for yi, (m, _mlab) in enumerate(ms):
            y = 1 - yi
            v = D("e3_c22_effect", metric, m, "mean") * sc
            lo = D("e3_c22_effect", metric, m, "lo") * sc
            hi = D("e3_c22_effect", metric, m, "hi") * sc
            # classify: win / loss / tie relative to zero (direction-aware)
            better_low = metric != "help"
            excl = lo > 0 or hi < 0
            win = excl and ((hi < 0) if better_low else (lo > 0))
            loss = excl and not win
            col = ps.ACCENT if win else ps.INK if loss else ps.G_MID
            ax.plot([lo, hi], [y, y], color=col, lw=1.5, solid_capstyle="butt")
            ax.plot([v], [y], "o", ms=4.6, mfc=col, mec=col)
            ax.text(v, y + 0.2, f"{v:+.2f}", fontsize=6.6, ha="center", va="bottom", color=ps.G_DARK)
        ax.axvline(0, color=ps.G_DARK, lw=0.6, ls="--")
        ax.set_ylim(-0.6, 1.7)
        ax.set_yticks([1, 0])
        ax.set_yticklabels([m for _m, m in ms] if ai == 0 else ["", ""])
        ax.set_xlim(*[(-2, 17), (-2.5, 6), (-3.4, 0.9)][ai])
        ax.set_xlabel(f"{xl}\n{dtxt}")
        ps.gridx(ax)
        ps.panel(ax, "abc"[ai], dx=-0.0 if ai else -0.5, dy=1.06)
    # d: post-hoc H2.1 matched comparator (share of sets where liot_real is significantly more harmful)
    ax = axes[3]
    h = ("h2_1", "exp")
    rows = []
    for m, mlab in ms:
        k = f"exp|m{m:.1f}"
        base = ["c413", "c413a", "h2_1_matched_posthoc", k]
        matched = S("e16_scoring_harm", *base, "vs_flat_explicit_same_ceiling(사후, matched)", "share_policy_sig_higher")
        unmatched = S("e16_scoring_harm", *base, "vs_flat_explicit(registered form, unmatched)", "share_policy_sig_lower")
        rows.append((m, mlab, matched, unmatched))
    del h
    y = 3.0
    for m, _mlab, matched, unmatched in rows:
        # matched (post hoc): LIOT worse
        path = f"exp|m{m:.1f}"
        lo = D("wilson", "e16_scoring_harm", ["c413", "c413a", "h2_1_matched_posthoc", path, "vs_flat_explicit_same_ceiling(사후, matched)", "share_policy_sig_higher"], 400, "lo")
        hi = D("wilson", "e16_scoring_harm", ["c413", "c413a", "h2_1_matched_posthoc", path, "vs_flat_explicit_same_ceiling(사후, matched)", "share_policy_sig_higher"], 400, "hi")
        ax.plot([lo * 100, hi * 100], [y, y], color=ps.INK, lw=1.5, solid_capstyle="butt")
        ax.plot([matched * 100], [y], "o", ms=4.6, mfc=ps.INK, mec=ps.INK)
        ax.text(max(matched * 100 + 5, 12) if matched < 0.5 else matched * 100 - 6, y + 0.0, f"{matched * 100:.1f}%", fontsize=6.6, va="center", ha="left" if matched < 0.5 else "right", color=ps.G_DARK)
        # unmatched registered form (structural): LIOT lower
        ulo = D("wilson", "e16_scoring_harm", ["c413", "c413a", "h2_1_matched_posthoc", path, "vs_flat_explicit(registered form, unmatched)", "share_policy_sig_lower"], 400, "lo")
        uhi = D("wilson", "e16_scoring_harm", ["c413", "c413a", "h2_1_matched_posthoc", path, "vs_flat_explicit(registered form, unmatched)", "share_policy_sig_lower"], 400, "hi")
        ax.plot([ulo * 100, uhi * 100], [y - 0.55, y - 0.55], color=ps.G_DARK, lw=1.5, solid_capstyle="butt")
        ax.plot([unmatched * 100], [y - 0.55], "D", ms=4.4, mfc="white", mec=ps.G_DARK, mew=0.9)
        ax.text(unmatched * 100 - 6, y - 0.55, f"{unmatched * 100:.1f}%", fontsize=6.6, va="center", ha="right", color=ps.G_DARK)
        y -= 1.7
    ax.set_ylim(-0.7, 3.8)
    ax.set_yticks([3.0, 2.45, 1.3, 0.75])
    ax.set_yticklabels(["m=0 사후·같은 상한", "m=0 등록형(†)", "m=10% 사후·같은 상한", "m=10% 등록형(†)"], fontsize=6.4)
    ax.set_xlim(-2, 105)
    ax.set_xlabel("유의하게 다른 세트 비율 (%)\n사후 = LIOT 가 더 해로움 · 등록형(†) = 덜 해로움")
    ps.gridx(ax)
    ps.panel(ax, "d", dx=-0.62, dy=1.0)
    ax.text(0.5, 1.02, "사후 재판정 (H2.1)", transform=ax.transAxes, fontsize=7.6, fontweight="bold", ha="center", va="bottom", color=ps.INK)
    for ax, t in zip(axes[:3], ["도움", "피해", "부담"], strict=True):
        ax.set_title(f"{t}", fontsize=7.6, pad=6)
    m10_harm = D("e3_c22_effect", "harm", 0.1, "mean") * 100
    m10_help = D("e3_c22_effect", "help", 0.1, "mean") * 100
    m10_bur = D("e3_c22_effect", "burden", 0.1, "mean")
    post = S("e16_scoring_harm", "c413", "c413a", "h2_1_matched_posthoc", "exp|m0.1", "vs_flat_explicit_same_ceiling(사후, matched)", "share_policy_sig_higher", scale=100)
    meta(
        "F05",
        "그림 5",
        "E3·E16 같은 상한 비교의 효과(liot_real - 모두 명시 확인)와 H2.1 사후 재판정",
        f"a~c: 모두 명시 확인에도 같은 능동 상한을 씌운 일치 비교에서 오분류 10%일 때 LIOT 실제는 도움 완료 {m10_help:+.1f}%p, 확인 부담 {m10_bur:+.2f}건/일로 이기고 해로운 실행률은 {m10_harm:+.1f}%p로 진다(점과 95% 구간, 세트 부트스트랩). "
        f"오분류가 없으면 해로운 실행률은 사실상 동률이다. d: 이 같은 상한 비교로 H2.1 을 다시 판정하면 m=10%에서 세트의 {post:.1f}%에서 LIOT 실제가 유의하게 더 해롭다(사후). 등록한 형태(상한 비일치)의 낮은 피해는 침묵에서 온 구조상 결과(†)다.",
        "E3 (c2_2, 원자료 c22 블록), E16 (c413a)",
        "사후 재판정은 H2.2 점검 실패의 원인(비교군 제안 집합 차이)을 푼 뒤 만든 사후 기준이다. 등록 형태의 '판정 불가·구조상 결과'는 그대로 남는다. 구간은 세트 부트스트랩이고 거주자 반응은 가정이다. d 의 구간은 400세트 기준 윌슨 구간이다.",
    )
    return ps.save(fig, "fig05_e3_forest")


# =============================================================================== F06 ambient share
def f06() -> list[Any]:
    begin("F06")
    fig, axes = plt.subplots(1, 2, figsize=(ps.W2, 2.7), gridspec_kw={"wspace": 0.28, "left": 0.075, "right": 0.99, "top": 0.86, "bottom": 0.2})
    blocks = ["c211a_p000", "c211a_p025", "c211a_p050", "c211a_p075", "c211a_p100"]
    c = "c2_11a"
    xs = [S(E3, c, "rows", i, "realized_ambient_share", scale=100) for i in range(5)]
    cross10 = S(E3, c, "share_where_area_crosses_50pct_m0.1", scale=100)
    cross0 = S(E3, c, "share_where_area_crosses_50pct_m0.0", scale=100)
    struct_from = S(E3, c, "rows", 2, "realized_ambient_share", scale=100)
    gen_amb = S(E3, "harm_alignment", "harmful_share_by_tier_pooled", "AMBIENT", scale=100)
    del blocks
    for _ai, ax in enumerate(axes):
        ax.axvspan(struct_from, 104, facecolor="white", edgecolor=ps.G_MID, hatch="////", linewidth=0, alpha=1.0, zorder=0)
        ax.axvspan(min(cross10, cross0), max(cross10, cross0), color=ps.ACCENT_TINT, zorder=0.5)
        ax.set_xlim(-2, 104)
        ax.set_xlabel("해로운 제안 중 잔잔(자동 실행) 단계에 놓인 몫 (%)")
        ps.gridy(ax)
    ax = axes[0]
    for m, col, ls, mk, lab in [("m0.0", ps.G_DARK, "--", "s", "m = 0"), ("m0.1", ps.ACCENT, "-", "o", "m = 10%")]:
        ys, lo, hi = [], [], []
        for i in range(5):
            base = [c, "rows", i, m, "liot_real", "area_harm_lower_seed_noise_sig"]
            ys.append(S(E3, *base, "share", scale=100))
            lo.append(S(E3, *base, "lo", scale=100))
            hi.append(S(E3, *base, "hi", scale=100))
        ax.fill_between(xs, lo, hi, color=col, alpha=0.15, linewidth=0, zorder=2)
        ax.plot(xs, ys, ls, color=col, marker=mk, ms=3.8, lw=1.1, zorder=3, label=lab)
    ax.set_ylim(-3, 104)
    ax.set_ylabel("피해 이점 면적 (%) · ↑ 높을수록 이점\n(유의하게 덜 해로운 세트 비율)")
    ax.axhline(50, color=ps.G_MID, lw=0.5, ls=":")
    ax.legend(loc="upper left", bbox_to_anchor=(0.3, 1.0), fontsize=7, title="오분류", title_fontsize=7)
    ps.panel(ax, "a", dx=-0.13)
    ax.text((min(cross10, cross0) + max(cross10, cross0)) / 2, 100, f"교차 구간\n{min(cross10, cross0):.0f}~{max(cross10, cross0):.0f}%", fontsize=6.6, ha="center", va="top", color=ps.ACCENT)
    ax.text((struct_from + 104) / 2, 62, "구조상 결과 구간\n(잔잔은 확인 없이\n자동 실행되므로\n정의에 가까움)", fontsize=6.4, ha="center", va="center", color=ps.G_DARK, bbox={"fc": "white", "ec": "none", "pad": 1.5})
    ax.annotate(f"생성기 기본\n(잔잔 {gen_amb:.0f}%)", (xs[0], 0), xytext=(xs[0] + 3, 22), fontsize=6.4, color=ps.G_DARK, arrowprops={"arrowstyle": "-", "lw": 0.5, "color": ps.G_MID})
    ax = axes[1]
    for m, col, ls, mk, lab, off in [("m0.0", ps.G_DARK, "--", "s", "m = 0", -0.8), ("m0.1", ps.ACCENT, "-", "o", "m = 10%", 0.8)]:
        ys, lo, hi = [], [], []
        for i in range(5):
            base = [c, "rows", i, m, "liot_real", "harm_diff_pooled"]
            ys.append(S(E3, *base, "mean", scale=100))
            lo.append(S(E3, *base, "lo", scale=100))
            hi.append(S(E3, *base, "hi", scale=100))
        xo = [x + off for x in xs]
        ax.errorbar(xo, ys, yerr=[np.subtract(ys, lo), np.subtract(hi, ys)], fmt=ls, color=col, marker=mk, ms=3.8, lw=1.0, elinewidth=0.8, capsize=1.8, label=lab)
    ax.axhline(0, color=ps.G_DARK, lw=0.6)
    ax.set_ylabel("피해 차이 (%p) · ↓ 낮을수록 좋음\n(LIOT 실제 - 모두 명시 확인)")
    ps.panel(ax, "b", dx=-0.13)
    ax.text(56, 5, "LIOT 가 더 해로움 ↑", fontsize=6.4, color=ps.G_DARK, va="bottom")
    ax.text(56, -5, "LIOT 가 덜 해로움 ↓", fontsize=6.4, color=ps.G_DARK, va="top")
    a50_0 = S(E3, c, "rows", 2, "m0.0", "liot_real", "area_harm_lower_seed_noise_sig", "share", scale=100)
    a50_1 = S(E3, c, "rows", 2, "m0.1", "liot_real", "area_harm_lower_seed_noise_sig", "share", scale=100)
    hi50 = S(E3, c, "rows", 2, "m0.1", "liot_real", "area_harm_higher_seed_noise_sig", "share", scale=100)
    meta(
        "F06",
        "그림 6",
        "E3 C2.11(a): 해로운 제안이 잔잔 단계에 놓이는 몫과 LIOT 실제의 피해 이점",
        f"해로운 제안이 자동 실행되는 잔잔 단계에 놓이는 몫을 0에서 100%까지 옮기면 LIOT 실제의 피해 이점 면적이 줄어 {min(cross10, cross0):.0f}~{max(cross10, cross0):.0f}%에서 과반 아래로 내려간다(음영 띠). "
        f"잔잔 {struct_from:.0f}%에서는 이점 면적이 m=0 {a50_0:.1f}%, m=10% {a50_1:.1f}%이고 모두 명시 확인보다 유의하게 더 해로운 세트가 {hi50:.1f}%다.",
        "E3 (c2_11a)",
        "잔잔 비율은 생성기의 노브이며 현실의 잔잔 비율은 모른다. 빗금 구간의 큰 격차는 잔잔이 확인 없이 자동 실행된다는 정의에서 나오는 구조상 결과라 발견으로 쓰지 않고, 정보가 되는 것은 교차 구간뿐이다. 잔잔 쪽 해로움은 한 견본(조명 끄기)에 몰려 있다.",
    )
    return ps.save(fig, "fig06_e3_ambient_share")
