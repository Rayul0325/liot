"""E7 analysis: reads only raw/, deterministic (verify recomputes it)."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import numpy as np

from experiments.common.io import read_jsonl
from experiments.e1_latency.stats import SEED

B = 500


def _conf(rows: list[dict[str, Any]]) -> dict[str, Any]:
    out: dict[str, Any] = {"by_scenario": {}, "by_driver": {}}
    for r in rows:
        key = f"{r['scenario']}/{r['driver']}"
        m = r["match"]
        out["by_scenario"][key] = {
            "title": r["title"], "conformant": r["conformant"],
            "n_expected": len(r["expected"]), "n_matched": len(m["pairs"]),
            "n_missing": r["n_missing"], "n_extra": r["n_extra"],
            "max_abs_error_s": r["max_abs_error_s"],
            "missing": m["missing"], "extra": m["extra"], "note": r["note"],
            "failed_attempts_s": [a[2] for a in r["failed_attempts"]],
        }
    for d in sorted({r["driver"] for r in rows}):
        rs = [r for r in rows if r["driver"] == d]
        errs = [abs(p["error_s"]) for r in rs for p in r["match"]["pairs"]]
        out["by_driver"][d] = {
            "scenarios": len(rs),
            "conformant": sum(1 for r in rs if r["conformant"]),
            "events_matched": len(errs),
            "events_over_1s": sum(1 for e in errs if e >= 1.0),
            "max_abs_error_s": float(max(errs)) if errs else 0.0,
            "median_abs_error_s": float(np.median(errs)) if errs else 0.0,
            "missing_total": sum(r["n_missing"] for r in rs),
            "extra_total": sum(r["n_extra"] for r in rs),
            "nonconformant": sorted(r["scenario"] for r in rs if not r["conformant"]),
        }
    return out


def _ratio_ci(num: np.ndarray, den: np.ndarray) -> tuple[float, float, float]:
    rng = np.random.default_rng(SEED)
    idx = rng.integers(0, len(num), size=(B, len(num)))
    point = float(num.sum() / den.sum()) if den.sum() > 0 else float("nan")
    boots = []
    for k in idx:
        d = den[k].sum()
        boots.append(num[k].sum() / d if d > 0 else np.nan)
    arr = np.array(boots)
    return point, float(np.nanpercentile(arr, 2.5)), float(np.nanpercentile(arr, 97.5))


def _mc(rows: list[dict[str, Any]]) -> dict[str, Any]:
    cells: dict[str, Any] = {}
    in_ci = {"e_t_ack": [0, 0], "pages": [0, 0], "p_ack_300": [0, 0]}
    worst: dict[str, float] = {"e_t_ack": 0.0, "pages": 0.0, "p_ack_300": 0.0}
    for r in rows:
        bs = r["batches"]
        trials = np.array([b["trials"] for b in bs], dtype=float)
        cf = r["closed_form"]
        entry: dict[str, Any] = {"mu_min": r["mu_min"], "q": r["q"], "b": r["b"],
                                 "n_guard": r["n_guard"], "p_respond": cf["p"],
                                 "p_no_ack_closed": cf["p_no_ack"]}
        for pol in ("sim", "seq"):
            n_ack = np.array([b[pol]["n_ack"] for b in bs], dtype=float)
            s_t = np.array([b[pol]["sum_t_ack"] for b in bs], dtype=float)
            n300 = np.array([b[pol]["n_ack_300"] for b in bs], dtype=float)
            pages = np.array([b[pol]["sum_pages"] for b in bs], dtype=float)
            e_t, e_lo, e_hi = _ratio_ci(s_t, n_ack)
            p_ack, pa_lo, pa_hi = _ratio_ci(n_ack, trials)
            p300, p3_lo, p3_hi = _ratio_ci(n300, trials)
            pg, pg_lo, pg_hi = _ratio_ci(pages, trials)
            entry[pol] = {
                "e_t_ack_s": e_t, "e_t_ack_lo": e_lo, "e_t_ack_hi": e_hi,
                "p_ack": p_ack, "p_ack_lo": pa_lo, "p_ack_hi": pa_hi,
                "p_ack_300": p300, "p_ack_300_lo": p3_lo, "p_ack_300_hi": p3_hi,
                "pages": pg, "pages_lo": pg_lo, "pages_hi": pg_hi,
            }
            for metric, val, lo, hi, closed in (
                ("e_t_ack", e_t, e_lo, e_hi, cf[pol]["e_t_ack"]),
                ("pages", pg, pg_lo, pg_hi, cf[pol]["pages"]),
                ("p_ack_300", p300, p3_lo, p3_hi, cf[pol]["p_ack_300"]),
            ):
                if closed != closed or val != val:  # NaN
                    continue
                in_ci[metric][1] += 1
                in_ci[metric][0] += int(lo <= closed <= hi)
                worst[metric] = max(worst[metric], abs(val - closed) / max(abs(closed), 1e-9))
            entry[f"closed_{pol}"] = cf[pol]
        n_both = np.array([b["paired"]["n_both"] for b in bs], dtype=float)
        s_diff = np.array([b["paired"]["sum_diff_seq_minus_sim"] for b in bs], dtype=float)
        d, d_lo, d_hi = _ratio_ci(s_diff, n_both)
        entry["delta_seq_minus_sim_s"] = {"point": d, "lo": d_lo, "hi": d_hi,
                                          "ci_excludes_zero": bool(d_lo > 0 or d_hi < 0)}
        entry["extra_people_paged_by_sim"] = entry["sim"]["pages"] - entry["seq"]["pages"]
        cells[f"mu{r['mu_min']}|q{r['q']}|b{r['b']}|N{r['n_guard']}"] = entry
    return {
        "cells": cells,
        "closed_form_check": {
            "share_closed_inside_sim_ci": {k: (v[0] / v[1] if v[1] else None)
                                           for k, v in in_ci.items()},
            "max_relative_deviation": worst,
        },
        "trials_per_setting": int(sum(b["trials"] for b in rows[0]["batches"])) if rows else 0,
        "seq_wait_s": 90.0,
    }


def summarize(raw_dir: Path) -> dict[str, Any]:
    raw_dir = Path(raw_dir)
    out: dict[str, Any] = {}
    conf = read_jsonl(raw_dir / "conformance.jsonl")
    if conf:
        out["conformance"] = _conf(sorted(conf, key=lambda r: (r["scenario"], r["driver"])))
    mc = read_jsonl(raw_dir / "mc.jsonl")
    if mc:
        out["mc"] = _mc(sorted(mc, key=lambda r: (r["mu_min"], r["q"], r["b"], r["n_guard"])))
    return out


def make_figures(summary: dict[str, Any], exp_dir: Path, plotstyle: Any) -> list[Path]:
    import matplotlib.pyplot as plt

    plotstyle.setup()
    out: list[Path] = []
    conf = summary.get("conformance")
    if conf:
        fig, axes = plt.subplots(1, 2, figsize=(11, 5), sharey=True)
        for ax, driver, ttl in ((axes[0], "event", "이상적 데몬(다음 발화 시각으로 점프)"),
                                (axes[1], "tick5_p3.3", "실제 데몬 해상도(5초 간격, 위상 3.3초)")):
            keys = sorted(k for k in conf["by_scenario"] if k.endswith(f"/{driver}"))
            vals = [conf["by_scenario"][k]["max_abs_error_s"] for k in keys]
            labels = [k.split("/")[0] for k in keys]
            colors = [plotstyle.ACCENT if conf["by_scenario"][k]["conformant"] else "#B24A3E"
                      for k in keys]
            ax.barh(range(len(keys)), vals, color=colors)
            ax.set_yticks(range(len(keys)), labels, fontsize=8)
            ax.axvline(1.0, color=plotstyle.GREY_DARK, linestyle="--", label="기준 1초")
            ax.set_title(ttl, fontsize=9)
            ax.set_xlabel("명세 시각과의 최대 오차 (초)")
        axes[0].legend()
        fig.suptitle("H6.1 사다리 명세 적합 — 시나리오별 최대 오차 (붉은색 = 부적합)", fontsize=11)
        out.append(plotstyle.save(fig, exp_dir, "h61_conformance"))
    m = summary.get("mc")
    if m:
        cells = m["cells"]
        fig, axes = plt.subplots(2, 3, figsize=(13, 7))
        for col, mu in enumerate((1.0, 3.0, 10.0)):
            for row, (metric, ylab) in enumerate((("e_t_ack_s", "첫 확인까지 시간(초, 확인된 사건)"),
                                                  ("pages", "사건당 호출받는 사람 수"))):
                ax = axes[row][col]
                for pol, color, ls in (("sim", plotstyle.policy_color("liot"), "-"),
                                       ("seq", plotstyle.policy_color("model_only"), "--")):
                    xs, ys = [], []
                    for n in (1, 2, 3, 5):
                        c = cells.get(f"mu{mu}|q0.2|b0.3|N{n}")
                        if c:
                            xs.append(n)
                            ys.append(c[pol][metric])
                    ax.plot(xs, ys, color=color, linestyle=ls, marker="o",
                            label="동시 호출(LIOT)" if pol == "sim" else "순차 호출(90초 간격)")
                ax.set_title(f"평균 응답 {mu:g}분 · 무응답 20% · 방관 b=0.3", fontsize=9)
                ax.set_xlabel("동의한 보호자 수")
                ax.set_ylabel(ylab)
        axes[0][0].legend(fontsize=7)
        fig.suptitle("H6.2 동시 호출 대 순차 호출 (시뮬레이션, 가정 기반)", fontsize=12)
        out.append(plotstyle.save(fig, exp_dir, "h62_sim_vs_seq"))
    return out
