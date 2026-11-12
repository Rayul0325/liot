"""Style B (AI-lab comparison panels): LIOT next to the alternatives on the same axes, plus the scorecard."""

from __future__ import annotations

import textwrap
from typing import Any

import matplotlib.pyplot as plt
import numpy as np
from matplotlib.patches import Patch, Rectangle

from experiments.kit.figures import effective_verdicts
from experiments.paper_figs import derive
from experiments.paper_figs import paper_style as ps
from experiments.paper_figs.data import D, S, begin, meta, verdict

E1 = "e1_latency"
E3 = "e3_policy"
E6 = "e6_personalization"
E7 = "e7_ladder"
E8 = "e8_mutation"
E10 = "e10_cloud_injection"
E12 = "e12_floor_anchor"
E13 = "e13_adapt_rules"
E14 = "e14_guardian_latency"
E15 = "e15_mutation_recheck"
E16 = "e16_scoring_harm"
E4 = "e4_scoring"
E2 = "e2_invariants"
E5 = "e5_models"
E11 = "e11_wording_fields"

DIRECTION_UP = "↑ 높을수록 좋음"
DIRECTION_DOWN = "↓ 낮을수록 좋음"


def _bars(ax: Any, entries: list[dict[str, Any]], xmax: float, fmt: str = "{:.3f}") -> None:
    """Horizontal bars with 95% interval; LIOT accent, alternatives grey, structural hatched."""
    ys = np.arange(len(entries))[::-1]
    for y, e in zip(ys, entries, strict=True):
        liot = e["kind"] == "liot"
        struct = e.get("struct", False)
        col = ps.ACCENT if liot else ps.G_MID
        if struct:
            ax.barh(y, e["v"], height=0.62, facecolor="white", edgecolor=col, hatch="////", linewidth=0.6)
        else:
            ax.barh(y, e["v"], height=0.62, color=col, linewidth=0)
        if e.get("lo") is not None:
            ax.plot([e["lo"], e["hi"]], [y, y], color=ps.INK, lw=0.9, solid_capstyle="butt")
            ax.plot([e["lo"]] * 2, [y - 0.13, y + 0.13], color=ps.INK, lw=0.9)
            ax.plot([e["hi"]] * 2, [y - 0.13, y + 0.13], color=ps.INK, lw=0.9)
        tx = max(e["v"], e.get("hi") or 0) + xmax * 0.015
        ax.text(tx, y, e["text"] if "text" in e else fmt.format(e["v"]), fontsize=6.6, va="center", color=ps.ACCENT if liot else ps.G_DARK, fontweight="bold" if liot else "normal")
    ax.set_yticks(ys)
    ax.set_yticklabels([e["label"] for e in entries], fontsize=7)
    for tl, e in zip(ax.get_yticklabels(), entries, strict=True):
        if e["kind"] == "liot":
            tl.set_color(ps.ACCENT)
            tl.set_fontweight("bold")
    ax.set_xlim(0, xmax)
    ax.set_ylim(-0.6, len(entries) - 0.4)
    ps.gridx(ax)


# =============================================================================== F13 safety panel
def f13() -> list[Any]:
    begin("F13")
    fig = plt.figure(figsize=(ps.W2, 3.0))
    gs = fig.add_gridspec(1, 3, width_ratios=[1.15, 0.95, 1.0], wspace=1.0, left=0.115, right=0.99, top=0.84, bottom=0.27)
    # (a) first-alert p95 latency, model slow
    ax = fig.add_subplot(gs[0, 0])
    arms = [("liot", "LIOT (표 먼저)", "liot"), ("wait_for_model", "모델 뒤 발송", "alt"), ("short_0.25", "짧은 마감 0.25 s", "alt"), ("short_0.5", "짧은 마감 0.5 s", "alt"), ("short_1.0", "짧은 마감 1.0 s", "alt")]
    ent = []
    for arm, lab, kind in arms:
        base = ["main", "cells", f"{arm}/slow"]
        v = S(E1, *base, "p95", scale=1000)
        ent.append({"label": lab, "kind": kind, "v": v, "lo": S(E1, *base, "p95_lo", scale=1000), "hi": S(E1, *base, "p95_hi", scale=1000), "struct": kind == "alt", "text": f"{v:.0f}" if v > 20 else f"{v:.1f}"})
    _bars(ax, ent, 2200)
    ax.set_xlabel(f"첫 경보 지연 p95 (ms), 모델 느림\n{DIRECTION_DOWN}")
    ps.panel(ax, "a", dx=-0.95)
    ax.text(0.99, 0.5, "빗금 = 대안이 기다리도록\n정의돼 생긴 차이(구조상)", transform=ax.transAxes, fontsize=6.2, ha="right", va="center", color=ps.G_DARK)
    # (b) second push share, warning-floor incidents
    ax = fig.add_subplot(gs[0, 1])
    ent = []
    for arm, lab, kind in [("liot", "LIOT", "liot"), ("floor_aware", "하한을 아는 대안", "alt"), ("wait_for_model", "모델 뒤 발송", "alt")]:
        base = ["c27", "by_floor", "WARNING", "cells", arm, "mismatch_share"]
        v = S(E1, *base, "rate", scale=100)
        ent.append({"label": lab, "kind": kind, "v": v, "lo": S(E1, *base, "lo", scale=100), "hi": S(E1, *base, "hi", scale=100), "text": f"{v:.1f}%"})
    _bars(ax, ent, 45)
    ax.set_xlabel(f"경고 하한 사건 중 두 번째 푸시 (%)\n{DIRECTION_DOWN}")
    ps.panel(ax, "b", dx=-0.8)
    ax.text(0.98, 0.97, "LIOT 가 지는 곳", transform=ax.transAxes, fontsize=6.6, ha="right", va="top", color=ps.INK, fontweight="bold")
    # (c) gate-1 window share on restart cuts: LIOT before / after U6 (alternatives not measured)
    ax = fig.add_subplot(gs[0, 2])
    st = S(E14, "killsweep", "windows", 2, "statements")
    b_bad = len(S(E14, "killsweep", "windows", 2, "silent_kill_points")) + len(S(E14, "killsweep", "windows", 2, "alerted_but_no_ladder_kill_points"))
    a_bad = len(S("kit:e14_killsweep_after_u6", "windows", 2, "silent_kill_points")) + len(S("kit:e14_killsweep_after_u6", "windows", 2, "alerted_but_no_ladder_kill_points"))
    mid_k = S(E14, "fuzz", "by_mode", "midop", "episodes_with_violation", "k")
    mid_n = S(E14, "fuzz", "by_mode", "midop", "episodes")
    ent = [
        {"label": "LIOT, U6 전\n(전수 탐색 문장 비율)", "kind": "liot", "v": D("ratio", b_bad, st) * 100, "text": f"{b_bad:.0f}/{st:.0f}"},
        {"label": "LIOT, U6 전\n(무작위 절단 퍼징†)", "kind": "liot", "v": D("ratio", mid_k, mid_n) * 100, "lo": D("wilson_k", mid_k, mid_n, "lo") * 100, "hi": D("wilson_k", mid_k, mid_n, "hi") * 100, "text": f"{mid_k:.0f}/{mid_n:.0f}"},
        {"label": "LIOT, U6 후\n(같은 전수 탐색)", "kind": "liot", "v": D("ratio", a_bad, st) * 100, "text": f"{a_bad:.0f}/{st:.0f}"},
        {"label": "다른 구조\n(미측정)", "kind": "alt", "v": 0.0, "text": "측정 안 함"},
    ]
    _bars(ax, ent, 100)
    ax.set_xlabel(f"게이트 1 위반(침묵·사다리 없음) 비율 (%)\n{DIRECTION_DOWN}")
    ps.panel(ax, "c", dx=-0.95)
    fig.text(0.5, -0.02, "주의: 모든 위반 칸은 LIOT 자신의 이전 버전과의 비교이며 대안 구조는 재시작 시험을 하지 않았다. † 절단 시점을 균등 추출한 값이라 실제 빈도가 아니다.", ha="center", fontsize=6.4, color=ps.G_DARK)
    liot_p95 = S(E1, "main", "cells", "liot/slow", "p95", scale=1000)
    rate = S(E1, "c27", "by_floor", "WARNING", "cells", "liot", "mismatch_share", "rate", scale=100)
    meta(
        "F13",
        "그림 13 (스타일 B 패널 1)",
        "안전: 구조별 첫 경보 지연·두 번째 푸시·재시작 위반(E1/E2/E14)",
        f"a: 모델이 느릴 때 LIOT 의 첫 경보 p95 는 {liot_p95:.1f} ms이고 모델을 기다리는 대안은 정의상 그만큼 늦다(구조상 결과, LIOT 우위로 읽지 않음). b: 반대로 경고 하한 사건에서 LIOT 는 두 번째 푸시를 {rate:.1f}% 보내 하한을 아는 대안(0%)보다 푸시가 많다 — LIOT 가 지는 곳. "
        f"c: 재시작 중간 절단에서 U6 이전 LIOT 는 게이트 1 위반 창이 {b_bad:.0f}/{st:.0f}문장이었고 U6 이후 {a_bad:.0f}/{st:.0f}이다.",
        "E1, E2, E14",
        "모델 지연·올림 확률 30%·마감은 가정이고 송신기는 가짜이며 부하가 높은 한 대의 기계에서 쟀다. 하드 게이트 위반 0(E2)은 무작위 퍼징 안에서의 0이고 알려진 게이트 1 충돌 두 종류가 있다. c 의 U6 후 값은 kit 재측정이고 읽기 자체가 남지 않는 문장 4개는 복구 불가 빈틈으로 남는다.",
    )
    return ps.save(fig, "fig13_panel_safety")


# =============================================================================== F14 burden / help panel
POLICIES = [
    ("liot_full", "LIOT 4단계(가상)", "liot", False),
    ("liot_real", "LIOT 실제(능동 상한)†", "liot", True),
    ("flat_explicit", "모두 명시 확인", "alt", False),
    ("binary_ext", "이진 승인(외부 목록)", "alt", False),
    ("binary_opt", "이진 승인(사후 최적)", "alt", False),
    ("auto_all", "모두 자동", "alt", False),
    ("auto_undo_w15", "자동+되돌리기 15분", "alt", False),
]


def f14() -> list[Any]:
    begin("F14")
    fig = plt.figure(figsize=(ps.W2, 3.15))
    gs = fig.add_gridspec(1, 5, width_ratios=[1.15, 0.8, 0.8, 0.5, 1.0], wspace=0.35, left=0.125, right=0.99, top=0.84, bottom=0.22)
    specs = [
        ("burden_per_day", "하루 확인 부담 (건/일)", DIRECTION_DOWN, 1.0, 17.5, "{:.1f}"),
        ("harm_rate", "해로운 실행률 (%)", DIRECTION_DOWN, 100.0, 135, "{:.1f}"),
        ("helpful_completion", "도움 완료율 (%)", DIRECTION_UP, 100.0, 125, "{:.1f}"),
    ]
    axes = []
    for ai, (metric, xl, dtxt, sc, xmax, fmt) in enumerate(specs):
        ax = fig.add_subplot(gs[0, ai])
        axes.append(ax)
        ent = []
        for key, lab, kind, struct in POLICIES:
            base = ["policy_table", "exp", "m0.1", key, metric]
            v = S(E3, *base, "mean", scale=sc)
            lo = S(E3, *base, "lo", scale=sc)
            hi = S(E3, *base, "hi", scale=sc)
            ent.append({"label": lab, "kind": kind, "v": v, "lo": lo, "hi": hi, "struct": struct and metric != "helpful_completion", "text": fmt.format(v)})
        _bars(ax, ent, xmax)
        ax.set_xlabel(f"{xl}\n{dtxt}")
        if ai > 0:
            ax.set_yticklabels([])
        ps.panel(ax, "abc"[ai], dx=-0.0 if ai else -0.95)
    ax = fig.add_subplot(gs[0, 4])
    arms = [("lr30", "LIOT 운영 학습 (30%)", "liot", False), ("none", "학습 없음", "alt", False), ("explicit_only", "버튼만 ([그만해])", "alt", False), ("lr10", "학습률 10%", "alt", False), ("lr60", "학습률 60%", "alt", False), ("explore7", "7일 탐색", "alt", False), ("no_gate", "게이트 없음", "alt", False), ("permanent", "영구 차단†", "alt", True)]
    ent = []
    for arm, lab, kind, struct in arms:
        base = ["pooled", f"main|{arm}|B", "d1_60", "acc_help"]
        v = S(E6, *base, "mean")
        ent.append({"label": lab, "kind": kind, "v": v, "lo": S(E6, *base, "lo"), "hi": S(E6, *base, "hi"), "struct": struct, "text": f"{v:.2f}"})
    _bars(ax, ent, 1.6)
    ax.set_xlabel(f"하루 수락된 도움 (건/일, E6)\n{DIRECTION_UP}")
    ps.panel(ax, "d", dx=-0.95)
    fig.text(0.5, -0.01, "m = 10%, 지수 피로, 400세트 평균과 95% 구간(a–c, E3) · 씨앗 10개 × 성향 3종 60일(d, E6). † 빗금 = 구조상 결과(LIOT 실제의 낮은 피해·확인은 땅거미 이상 침묵에서 옴).", ha="center", fontsize=6.4, color=ps.G_DARK)
    lr_b = S(E3, "policy_table", "exp", "m0.1", "liot_real", "burden_per_day", "mean")
    fl_b = S(E3, "policy_table", "exp", "m0.1", "flat_explicit", "burden_per_day", "mean")
    lf_h = S(E3, "policy_table", "exp", "m0.1", "liot_full", "harm_rate", "mean", scale=100)
    fl_h = S(E3, "policy_table", "exp", "m0.1", "flat_explicit", "harm_rate", "mean", scale=100)
    acc_l = S(E6, "pooled", "main|lr30|B", "d1_60", "acc_help", "mean")
    acc_e = S(E6, "pooled", "main|explicit_only|B", "d1_60", "acc_help", "mean")
    meta(
        "F14",
        "그림 14 (스타일 B 패널 2)",
        "부담과 도움: 정책별 확인 부담·해로운 실행률·도움 완료율(E3)과 학습 방식별 수락된 도움(E6)",
        f"a–c: 오분류 10%에서 LIOT 가상은 확인 부담을 {fl_b:.1f}건/일에서 {S(E3, 'policy_table', 'exp', 'm0.1', 'liot_full', 'burden_per_day', 'mean'):.1f}건/일로 줄이지만 해로운 실행률({lf_h:.1f}%)은 모두 명시 확인({fl_h:.1f}%)보다 낮지 않다. "
        f"LIOT 실제의 부담 {lr_b:.1f}건/일과 낮은 해로운 실행률은 땅거미 이상을 침묵시켜 생긴 구조상 결과다. d: 운영 학습의 하루 수락된 도움({acc_l:.2f})은 버튼만 있는 대안({acc_e:.2f})보다 낮다.",
        "E3 (policy_table), E6 (pooled)",
        "거주자 반응은 전부 가정이고 구간은 세트·씨앗 잡음이며 가정의 불확실성은 들어 있지 않다. '이진 승인(사후 최적)'은 이진에게 가장 유리하게 고른 목록이다. 영구 차단은 정의에서 나오는 구조상 결과다. d 의 모아보기 열람 80%는 가정이다.",
    )
    return ps.save(fig, "fig14_panel_burden_help")


# =============================================================================== F15 scorecard
COLS = ["모두 명시\n확인", "이진 승인", "모델 뒤 발송\n·짧은 마감", "곱셈·단일\n점수", "모델 판정자\n(클라우드·상향)", "학습 없음\n·버튼만", "호출 방식\n(최종값·순차)", "명세·사전\n예측"]
CAT = {
    "win": ("승", ps.ACCENT, "white", None),
    "loss": ("패", ps.INK, "white", None),
    "tie": ("동률", "white", ps.INK, None),
    "struct": ("구조상", "white", ps.G_DARK, "////"),
    "undec": ("판정 불가", "white", ps.G_DARK, None),
    "desc": ("기술", ps.G_PALE, ps.INK, None),
}


def _classify(effect: float, lo: float, hi: float, better_low: bool, practical: float = 0.0) -> str:
    """win/loss/tie of an effect from its 95% interval; inside +-practical it is a tie."""
    if practical and max(abs(lo), abs(hi)) < practical:
        return "tie"
    if lo > 0 or hi < 0:
        good = hi < 0 if better_low else lo > 0
        return "win" if good else "loss"
    return "tie"


def f15() -> list[Any]:
    begin("F15")
    hv = {r["id"]: r["shown"] for r in effective_verdicts()[0]}

    def hyp(i: str) -> str:
        return verdict(i, hv[i])

    rows: list[tuple[str, int, str, str]] = []  # (axis label, col, category, text)
    group_of: list[str] = []

    def add(group: str, label: str, col: int, cat: str, text: str) -> None:
        rows.append((label, col, cat, text))
        group_of.append(group)

    # ---- Q1 safety
    assert hyp("H1.1") == "구조상 결과"
    add("Q1 안전", "첫 경보 지연 (E1)", 2, "struct", f"+{S(E1, 'main', 'delta_vs_liot', 'wait_for_model/normal', 'median', 'diff', scale=1000):.0f} ms")
    liot_r = S(E1, "c27", "by_floor", "WARNING", "cells", "liot", "mismatch_share", "lo", scale=100)
    alt_r = S(E1, "c27", "by_floor", "WARNING", "cells", "floor_aware", "mismatch_share", "hi", scale=100)
    add("Q1 안전", "경고 하한 사건의 두 번째 푸시 (E1)", 2, "loss" if liot_r > alt_r else "tie", f"{S(E1, 'c27', 'by_floor', 'WARNING', 'cells', 'liot', 'mismatch_share', 'rate', scale=100):.1f}% 대 0")
    assert hyp("H1.4") == "구조상 결과"
    add("Q1 안전", "하드 게이트 위반, 무작위 퍼징 (E2)", 7, "struct", f"{S(E2, 'fuzz', 'violations_total'):.0f}건")
    assert hyp("C4.9 (c)") == "반증"
    add("Q1 안전", "재시작 중간 절단의 게이트 1, U6 전 (E14)", 7, "loss", f"{S(E14, 'fuzz', 'by_mode', 'midop', 'episodes_with_violation', 'k'):.0f}/{S(E14, 'fuzz', 'by_mode', 'midop', 'episodes'):.0f}")
    add("Q1 안전", "같은 절단 전수 탐색, U6 후 (E14)", 7, "desc", f"침묵 {len(S('kit:e14_killsweep_after_u6', 'windows', 2, 'silent_kill_points')):.0f}/{S('kit:e14_killsweep_after_u6', 'windows', 2, 'statements'):.0f}")
    assert hyp("H6.1") == "반증"
    add("Q1 안전", "사다리 명세 적합 (E7·E14)", 7, "loss", f"{S(E14, 'tick', 'by_driver', 'tick5_p3.3', 'events_over_1s'):.0f}건 >1초")
    assert hyp("H1.5 (P1)") == "기술 결과"
    add("Q1 안전", "보호자 호출 기준 (E2)", 6, "desc", f"맞교환 {S(E2, 'h15', 'class_counts_model_sees_truth', 'tradeoff'):.0f}칸")
    assert hyp("C4.9 (b)") == "지지"
    add("Q1 안전", "혼합 보호자 기준 (E14)", 6, "win", f"응답 60초 가정만 {S(E14, 'hybrid_sens', 'by_ack_mean', 'mean60', 'passing_primary_window'):.0f}칸")
    assert hyp("C2.10") == "읽기에 따라 갈림"
    add("Q1 안전", "동시 대 순차 호출 (E7)", 6, "undec", f"{S(E7, 'mc', 'c2_10', 'concurrent_scaled_seq', 'b0.6', 'seq_higher_ci_separated'):.0f}/27 · {S(E7, 'mc', 'c2_10', 'N_scaled_both', 'b0.6', 'sim_higher_ci_separated'):.0f}/27")
    # ---- Q2 burden vs harm
    for metric, label, better_low, fmt, sc in [("burden", "확인 부담, 같은 능동 상한 (E3)", True, "{:+.2f}건", 1.0), ("help", "도움 완료, 같은 능동 상한 (E3)", False, "{:+.1f}%p", 100.0)]:
        v = D("e3_c22_effect", metric, 0.1, "mean")
        lo = D("e3_c22_effect", metric, 0.1, "lo")
        hi = D("e3_c22_effect", metric, 0.1, "hi")
        prac = D("code_const", "experiments.e3_policy.analyze", "PRACTICAL") if metric == "help" else 0.0
        add("Q2 부담 대 피해", label, 0, _classify(v, lo, hi, better_low, prac), fmt.format(v * sc))
    for m, label in [(0.1, "해로운 실행, 같은 상한, m=10% (E3·E16)"), (0.0, "해로운 실행, 같은 상한, m=0 (E3)")]:
        v = D("e3_c22_effect", "harm", m, "mean")
        lo = D("e3_c22_effect", "harm", m, "lo")
        hi = D("e3_c22_effect", "harm", m, "hi")
        prac = D("code_const", "experiments.e3_policy.analyze", "PRACTICAL")
        add("Q2 부담 대 피해", label, 0, _classify(v, lo, hi, True, prac), f"{v * 100:+.1f}%p")
    assert hyp("H2.1 (P2)") == "판정 불가"
    add("Q2 부담 대 피해", "해로운 실행, 상한 비일치 등록형 (E3)", 0, "struct", f"{S(E16, 'c413', 'c413a', 'h2_1_matched_posthoc', 'exp|m0.1', 'vs_flat_explicit(registered form, unmatched)', 'mean_diff', scale=100):+.1f}%p")
    add("Q2 부담 대 피해", "LIOT 가상의 해로운 실행, m=10% (E3)", 0, "loss", f"더 해로운 세트 {S(E3, 'h2_1', 'exp', 'm0.1', 'liot_full', 'harm', 'areas', 'higher', 'share', scale=100):.0f}%")
    add("Q2 부담 대 피해", "능동 상한이 잃는 도움 (E3)", 0, "loss", f"도움 제안 {S(E3, 'h2_6', 'exp', 'pooled_silenced_helpful_share', scale=100):.0f}% 소실")
    lf = ["policy_table", "exp", "m0.1", "liot_full", "harm_rate"]
    for alt, label, col in [("binary_ext", "해로운 실행 대 이진(외부 목록) (E3)", 1), ("binary_opt", "해로운 실행 대 이진(사후 최적) (E3)", 1)]:
        ab = ["policy_table", "exp", "m0.1", alt, "harm_rate"]
        l_hi, l_lo = S(E3, *lf, "hi"), S(E3, *lf, "lo")
        a_hi, a_lo = S(E3, *ab, "hi"), S(E3, *ab, "lo")
        cat = "win" if l_hi < a_lo else "loss" if l_lo > a_hi else "tie"
        add("Q2 부담 대 피해", label, col, cat, f"{S(E3, *lf, 'mean', scale=100):.0f}% 대 {S(E3, *ab, 'mean', scale=100):.0f}%")
    # ---- Q3
    t, b = S(E16, "c412", "headline", "table_tie_loss"), S(E16, "c412", "grid", "groups", "k_ge_5", "restricted", "tie_loss", "min")
    add("Q3 위험 판정", "동점 손실, 표 대 곱셈 최선 (E4·E16)", 3, "loss" if t > b else "win", f"{t:.0f}쌍 대 {b:.0f}쌍")
    assert hyp("C4.12 (b)") == "반증"
    add("Q3 위험 판정", "가스가 위험 아래인 설정 (E4·E16)", 3, "struct", f"표 0 · 곱셈 {S(E16, 'c412', 'headline', 'b_unrestricted_gas_below_critical_share_k_ge_5', scale=100):.0f}%")
    # ---- Q4
    base = ["contrasts", "main|pooled|B|lr30-explicit_only", "d1_60", "acc_help"]
    v, lo, hi = S(E6, *base, "mean"), S(E6, *base, "lo"), S(E6, *base, "hi")
    add("Q4 적응", "학습이 더하는 수락된 도움 (E6)", 5, _classify(v, lo, hi, False), f"{v:+.2f}건/일")
    ref = D("code_const", "experiments.e13_adapt_rules.analyze", "REFUTE_RATIO")
    r1 = ["c47", "verdict_1", "ratio_r1_over_none_d31_60"]
    add("Q4 적응", "모아보기 만료 규칙 (1), tray0 (E13)", 5, "loss" if S(E13, *r1, "hi") < ref else "tie", f"{S(E13, *r1, 'ratio'):.2f} [{S(E13, *r1, 'lo'):.2f}, {S(E13, *r1, 'hi'):.2f}]")
    bound = D("code_const", "experiments.e13_adapt_rules.analyze", "USELESS_BOUND_46")
    rs = ["c46", "contrast", "all|flip15|rest7-rest30", "offered_unhelp_imm", "d1_60"]
    undec = S(E13, *rs, "lo") < bound < S(E13, *rs, "hi")
    assert hyp("C4.6") == "판정 불가" and undec
    add("Q4 적응", "쉬는 기간 7일 대 30일 (E13)", 5, "undec", f"{S(E13, *rs, 'mean'):.3f} [{S(E13, *rs, 'lo'):.3f}, {S(E13, *rs, 'hi'):.3f}]")
    # ---- Q5
    dl = S(E10, "c41", "difference_local_minus_cloud", "ci95", 0)
    assert hyp("C4.1") == "지지" and dl > 0
    add("Q5 결정 모델", "주입 내성, 로컬 대 클라우드 (E10)", 4, "loss", f"{S(E10, 'c41', 'groups', 'local', 'flip_rate'):.2f} 대 {S(E10, 'c41', 'groups', 'cloud', 'flip_rate'):.2f}")
    up = S(E12, "departure_true_floor", "jev", "above") + S(E12, "departure_true_floor", "haiku", "above") + S(E12, "departure_true_floor", "gemini", "above")
    add("Q5 결정 모델", "모델이 하한 위로 올린 일 (E12)", 4, "loss" if up == 0 else "tie", f"{up:.0f}/{S(E12, 'steps', 'can_depart_upward'):.0f}")
    assert hyp("H5.1") == "구조상 결과"
    add("Q5 결정 모델", "운영 요약에서 올림 감소 (E5)", 4, "struct", f"{S(E5, 'c25_structured', 'rep0', 'pooled', 'raised_sentence'):.0f}→{S(E5, 'c25_structured', 'rep0', 'pooled', 'raised_operational'):.0f}건")
    # ---- Q6 tests
    n_low = S(E8, "h63_modules_low_score_despite_coverage", agg="len")
    assert hyp("H6.3") == "지지"
    add("Q6 테스트", "안전 모듈 뮤테이션 점수, 보강 전 (E8)", 7, "loss", f"{n_low:.0f}/10 모듈 <90%")
    assert hyp("C4.11 (a) 부수 처치") == "반증"
    add("Q6 테스트", "빈틈 시험의 15곳 밖 효과 (E15)", 7, "loss", f"+{S(E15, 'a', 'collateral', 'A_excl_15_ids', 'delta_pp'):.2f}%p")
    assert hyp("C4.11 (a) 헤드라인") == "구조상 결과"
    add("Q6 테스트", "15곳 시험 뒤 핵심 점수 (E15)", 7, "struct", f"+{S(E15, 'a', 'headline_structural', 'delta_pp'):.2f}%p")

    n_rows = len(rows)
    n_loss = derive.loss_items()
    n_loss_n = D("loss_count")
    row_h, label_w, cell_w = 0.168, 2.3, 0.45
    top = 0.62
    mat_h = top + n_rows * row_h + len(set(group_of)) * 0.07 + 0.4
    list_h = 0.35 + len(n_loss) * 0.185 + 0.3 + 0.6
    fig = plt.figure(figsize=(ps.W2, mat_h + list_h))
    ax = fig.add_axes((0, list_h / (mat_h + list_h), 1, mat_h / (mat_h + list_h)))
    ax.set_xlim(0, ps.W2)
    ax.set_ylim(mat_h, 0)
    ax.axis("off")
    ax.text(0.05, 0.1, "a", fontsize=9, fontweight="bold", va="top")
    for ci, c in enumerate(COLS):
        ax.text(label_w + (ci + 0.5) * cell_w, top - 0.04, c, ha="center", va="bottom", fontsize=5.6, linespacing=1.1)
    ax.text(label_w + len(COLS) * cell_w + 0.06, top - 0.04, "칸의 근거 수치", ha="left", va="bottom", fontsize=5.8, fontweight="bold")
    y = top
    last = ""
    for (label, col, cat, text), grp in zip(rows, group_of, strict=True):
        if grp != last:
            y += 0.07
            ax.text(0.05, y + row_h * 0.5 - 0.005, grp, fontsize=6.4, fontweight="bold", va="center", color=ps.G_DARK)
            ax.plot([0.04, label_w + len(COLS) * cell_w], [y - 0.005, y - 0.005], color=ps.G_LIGHT, lw=0.4)
            last = grp
        ax.text(label_w - 0.06, y + row_h / 2, label, ha="right", va="center", fontsize=6.5)
        for ci in range(len(COLS)):
            x0 = label_w + ci * cell_w
            if ci != col:
                ax.add_patch(Rectangle((x0 + 0.01, y + 0.01), cell_w - 0.02, row_h - 0.02, facecolor="none", edgecolor=ps.G_PALE, linewidth=0.3))
                continue
            name, fc, tc, hatch = CAT[cat]
            ec = ps.G_DARK if fc == "white" else fc
            ax.add_patch(Rectangle((x0 + 0.01, y + 0.01), cell_w - 0.02, row_h - 0.02, facecolor=fc, edgecolor=ec, hatch=hatch, linewidth=0.5, linestyle=(0, (2, 1.2)) if cat == "undec" else "-"))
            ax.text(x0 + cell_w / 2, y + row_h / 2, name, ha="center", va="center", fontsize=5.8, color=tc, fontweight="bold")
        ax.text(label_w + len(COLS) * cell_w + 0.06, y + row_h / 2, text, fontsize=6.0, va="center", color=ps.G_DARK)
        y += row_h
    handles = [Patch(facecolor=fc, edgecolor=(ps.G_DARK if fc == "white" else fc), hatch=h, linewidth=0.5, label=n) for n, fc, _tc, h in CAT.values()]
    ax.legend(handles=handles, loc="lower center", ncol=6, fontsize=6.4, bbox_to_anchor=(0.45, -0.005), handlelength=1.2, columnspacing=1.0)
    # panel b: the 28 losing items, all visible
    ax2 = fig.add_axes((0, 0, 1, list_h / (mat_h + list_h)))
    ax2.set_xlim(0, ps.W2)
    ax2.set_ylim(list_h, 0)
    ax2.axis("off")
    ax2.text(0.05, 0.05, "b", fontsize=9, fontweight="bold", va="top")
    ax2.text(0.3, 0.07, f"LIOT 가 지는 곳 {n_loss_n:.0f}항목 (자료집 Ⅴ-1, 전부 표시)", fontsize=7.4, fontweight="bold", va="top")
    yy = 0.4
    for i, title in enumerate(n_loss, start=1):
        lines = textwrap.wrap(title, 95)
        ax2.text(0.08, yy + 0.04, f"{i}", fontsize=6.0, fontweight="bold", ha="left", va="top", color=ps.INK)
        ax2.text(0.3, yy + 0.04, "\n".join(lines[:2]), fontsize=6.0, ha="left", va="top", linespacing=1.1, color=ps.INK)
        yy += 0.185 + 0.1 * (min(len(lines), 2) - 1)
    yy += 0.12
    ax2.text(
        0.05,
        yy,
        "주의: ① 구조상 결과(빗금)는 비교 결론에 쓰지 않는다. ② 승 칸도 거주자·모델 지연 등 가정 범위 안의 결과다. ③ 같은 상한 비교와 사후 재판정은 사후 기준이며 등록 형태는 판정 불가다. "
        "④ '기술'은 판정이 없는 값이다. ⑤ 칸의 승·패·동률은 그 칸 효과의 95% 구간이 0(또는 해당 기준)과 겹치는지로 정했다.",
        fontsize=6.0,
        va="top",
        ha="left",
        wrap=True,
        color=ps.G_DARK,
    )
    n_cat = {k: sum(1 for r in rows if r[2] == k) for k in CAT}
    meta(
        "F15",
        "그림 15 (스타일 B 점수표)",
        "LIOT 대 대안 점수표: 비교 축별 승·패·동률·구조상·판정 불가와 'LIOT 가 지는 곳' 전부",
        f"a: 연구 질문별 비교 축(행)과 비교 대상(열)마다 칸을 승·패·동률·구조상·판정 불가·기술로 표시했다(승 {n_cat['win']}, 패 {n_cat['loss']}, 동률 {n_cat['tie']}, 구조상 {n_cat['struct']}, 판정 불가 {n_cat['undec']}, 기술 {n_cat['desc']}). "
        f"{'패 칸이 승 칸보다 많고, ' if n_cat['loss'] > n_cat['win'] else ''}구조상 결과는 승으로 세지 않았다. b: 자료집 Ⅴ-1 의 'LIOT 가 지는 곳' {n_loss_n:.0f}항목을 줄임 없이 모두 적었다.",
        "E1~E16 (표의 각 칸 수치는 칸 안에 표기)",
        "승·패는 가정한 거주자·모델·지연 범위 안의 결과이고, 한 칸의 승이 다른 칸의 패를 상쇄하지 않는다. 대안이 구조상 정의된 칸은 구조상으로만 둔다. 칸 분류는 효과 구간과 자료집의 판정 말을 그대로 따랐고 임의로 올리지 않았다. 표에 없는 비교 축은 측정하지 않은 것이다.",
    )
    return ps.save(fig, "fig15_scorecard")
