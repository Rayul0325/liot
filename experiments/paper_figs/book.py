"""MANIFEST.md (Korean) and figure_book.pdf (one figure per page with caption) from the figure metadata."""

from __future__ import annotations

import textwrap
from pathlib import Path

import matplotlib.pyplot as plt
from matplotlib.backends.backend_pdf import PdfPages

from experiments.paper_figs import data
from experiments.paper_figs import paper_style as ps

HERE = Path(__file__).resolve().parent
FILES = {
    "F01": "fig01_verdict_map",
    "F02": "fig02_e1_latency",
    "F03": "fig03_e14_kill_windows",
    "F04": "fig04_e3_plane",
    "F05": "fig05_e3_forest",
    "F06": "fig06_e3_ambient_share",
    "F07": "fig07_scoring_methods",
    "F08": "fig08_raise_recovery",
    "F09": "fig09_e10_injection",
    "F10": "fig10_e12_anchoring",
    "F11": "fig11_adaptation",
    "F12": "fig12_mutation",
    "F13": "fig13_panel_safety",
    "F14": "fig14_panel_burden_help",
    "F15": "fig15_scorecard",
}

NOTES = """
## 만들지 않았거나 일부만 만든 것

- 건너뛴 그림은 없다. 다만 다음 둘은 자료의 한계로 요청한 형태에서 조금 바꿨다.
  - 그림 7a '동점 손실 분포': summary.json 에는 곱셈 설정 90만 개의 전체 히스토그램이 없고 취약 배수별 최솟값·사분위·중앙값·최댓값만 있다. 전체를 다시 계산하는 대신 이 요약으로 상자 그림을 그렸다(`c412 > grid > by_k`).
  - 그림 13c '다른 구조와의 재시작 위반 비교': E14 의 재시작 시험은 LIOT 만 대상으로 했다. 대안 구조는 '측정 안 함'으로 표시했고 LIOT 의 U6 전/후만 비교한다.
- 그림 4 의 θ 정책 점은 summary.json 에 부담·피해 쌍이 없어 원자료 `e3_policy/raw/runs.jsonl` 에서 `derive.e3_policy_stat` 로 계산했다(세트 평균, 세트 부트스트랩 95% 구간). 그림 5 의 같은 상한 효과의 구간도 같은 방식으로 원자료(`c22` 블록)에서 계산했고 평균은 summary.json 의 값과 일치함을 `check.py` 가 확인한다.
- 점수표(그림 15)의 승·패·동률은 칸 효과의 95% 구간과 자료집의 판정 말에서 정했다(동률은 E3 의 실질 차이 기준 1%p 안). '판정 불가'에는 '읽기에 따라 갈림'(동시 대 순차 호출)을 함께 넣었고, 판정이 없는 값은 '기술'이라는 여섯 번째 칸으로 따로 두었다.

## 숫자 검증

모든 그림의 숫자는 `data.S`(summary.json 읽기) 또는 `data.D`(원자료·요약값에서 계산하는 함수)를 거쳐 `annotations.json` 에 기록되고, `check.py` 가 같은 경로를 다시 읽어 값이 같은지 단언한다.
"""


def write_manifest() -> Path:
    lines = [
        "# 그림 목록 (MANIFEST) — LIOT R&E 최종 보고서 그림 세트",
        "",
        "> `experiments/paper_figs/make_figs.py` 가 만든다. 직접 고치지 않는다. 그림 파일은 `figs/*.pdf`(벡터)·`figs/*.png`(300 dpi), 모아 보기는 `figure_book.pdf`.",
        "> 모든 수치는 `experiments/e*/results/summary.json` 에서 읽거나 원자료에서 계산한 값이며 `check.py` 가 대조한다. 스타일 A = 논문(흰 바탕, 회색 + 강조색 1개), 스타일 B = 경쟁 대안과 나란히 보는 비교 패널.",
        "",
        "| 번호 | 제목 | 캡션 | 실험 | summary.json 키 | 주의(한계) | 파일 |",
        "|---|---|---|---|---|---|---|",
    ]
    for fid, stem in FILES.items():
        m = data.META[fid]
        keys = sorted(data.USED[fid])
        shown = "; ".join(f"`{k}`" for k in keys[:12]) + (f" 외 {len(keys) - 12}개" if len(keys) > 12 else "")
        cell = lambda t: t.replace("|", "/").replace("\n", " ")  # noqa: E731
        lines.append(
            f"| {m['number']} | {cell(m['title'])} | {cell(m['caption'])} | {m['exps']} | {cell(shown)} | {cell(m['caveats'])} | `figs/{stem}.pdf` `figs/{stem}.png` |"
        )
    lines.append(NOTES)
    path = HERE / "MANIFEST.md"
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


def write_book() -> Path:
    path = HERE / "figure_book.pdf"
    pw, ph = 8.27, 11.69
    with PdfPages(path) as pdf:
        for n, (fid, stem) in enumerate(FILES.items(), start=1):
            m = data.META[fid]
            fig = plt.figure(figsize=(pw, ph))
            fig.text(0.07, 0.965, f"{m['number']}. {m['title']}", fontsize=10.5, fontweight="bold", va="top", ha="left", wrap=True)
            cap = "\n".join(textwrap.wrap(m["caption"], 62))
            cav = "\n".join(textwrap.wrap("주의: " + m["caveats"], 66))
            fig.text(0.07, 0.935, cap, fontsize=7.8, va="top", ha="left", linespacing=1.35)
            ncap = cap.count("\n") + 1
            ncav = cav.count("\n") + 1
            cap_h = 0.0145 * ncap
            fig.text(0.07, 0.935 - cap_h - 0.008, cav, fontsize=7.0, va="top", ha="left", linespacing=1.3, color=ps.G_DARK)
            fig.text(0.07, 0.935 - cap_h - 0.008 - 0.0135 * ncav - 0.006, f"실험: {m['exps']}", fontsize=7.0, va="top", ha="left", color=ps.G_DARK)
            top = 0.935 - cap_h - 0.0135 * ncav - 0.04
            img = plt.imread(HERE / "figs" / f"{stem}.png")
            h, w = img.shape[0], img.shape[1]
            avail_w, avail_h = 0.86 * pw, (top - 0.04) * ph
            scale = min(avail_w / w, avail_h / h)
            iw, ih = w * scale / pw, h * scale / ph
            ax = fig.add_axes((0.07, top - ih, iw, ih))
            ax.imshow(img)
            ax.axis("off")
            fig.text(0.5, 0.02, f"{n} / {len(FILES)}", fontsize=7, ha="center", color=ps.G_MID)
            pdf.savefig(fig, dpi=300)
            plt.close(fig)
    return path
