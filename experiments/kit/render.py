"""Render experiments/REPORT_KIT.md from kit_template.md + claims.yaml + hypotheses.yaml.

Every number in the output is formatted from the live summary.json value, so the kit cannot drift
from the data. ``check_numbers`` additionally compares the file on disk with a fresh render.

Template placeholders:
  {{claims:GROUP}}  claim blocks of one group (III, IV.Q1 ...)
  {{v:ID:n}}        display text of the n-th item of claim ID
  {{v:count:X}}     number of hypothesis rows whose shown verdict is X (``total_decided`` = not 대기)
  {{verdicts}}      table of hypotheses by research question
  {{limits}}        every limitation sentence, with the claims that use it
  {{pending}}       experiments without a summary.json
  {{inconsistencies}}  RESULTS.md vs summary.json notes from claims.yaml
"""

from __future__ import annotations

import re
from pathlib import Path

import yaml

from experiments.kit import figures
from experiments.kit.check_numbers import (
    EXP_DIR,
    KIT_DIR,
    Claim,
    Item,
    fmt_item,
    load_claims,
    load_summary,
    resolve,
)

__all__ = ["OUT_PATH", "TEMPLATE_PATH", "kit_out_of_sync", "render", "write"]

TEMPLATE_PATH = KIT_DIR / "kit_template.md"
OUT_PATH = EXP_DIR / "REPORT_KIT.md"

_PLACEHOLDER = re.compile(r"\{\{([^{}]+)\}\}")


def _item_value(item: Item) -> str:
    summary = load_summary(item.exp)
    if summary is None:
        return "대기"
    if item.fn:
        from experiments.kit.derived import FUNCS

        actual = FUNCS[item.fn](summary, **item.args)
    else:
        actual = resolve(summary, item.path, item.agg)
    if isinstance(actual, bool):
        return "예" if actual else "아니오"
    if not isinstance(actual, (int, float)):
        return str(actual)
    return fmt_item(actual, item)


def _path_text(item: Item) -> str:
    if item.fn:
        return f"`{item.exp}/results/summary.json` 의 `{' > '.join(map(str, item.path))}` 칸들에서 계산(파생값)"
    parts = []
    for key in item.path:
        parts.append(",".join(f"{k}={v}" for k, v in key.items()) if isinstance(key, dict) else str(key))
    suffix = f" ({item.agg})" if item.agg else ""
    return f"`{item.exp}/results/summary.json` → `{' > '.join(parts)}`{suffix}"


def _claim_block(claim: Claim, limits: dict[str, str]) -> str:
    head = f"#### {claim.id}"
    if claim.hyp:
        head += f" · {claim.hyp}"
    lines = [head, ""]
    if claim.verdict:
        lines.append(f"- **최종 판정**: {claim.verdict}")
    lines.append(f"- **주장**: {claim.text}")
    if claim.kind == "pending":
        lines.append(f"- **상태**: 대기 — `{claim.pending_exp}/results/summary.json` 이 아직 없다(숫자 없음).")
    else:
        lines += ["- **숫자와 출처**:", "", "  | 숫자 | 값 | summary.json 경로 |", "  |---|---|---|"]
        for item in claim.items:
            note = f" — {item.note}" if item.note else ""
            lines.append(f"  | {item.label} | **{_item_value(item)}** | {_path_text(item)}{note} |")
        lines.append("")
    if claim.fig:
        lines.append(f"- **그림**: `experiments/{claim.fig}`")
    if claim.limit:
        lines.append(f"- **반드시 함께 쓸 한계 문장**: {limits[claim.limit]}")
    lines.append("")
    return "\n".join(lines)


def _verdict_table() -> str:
    rows, _ = figures.effective_verdicts()
    spec = yaml.safe_load((KIT_DIR / "hypotheses.yaml").read_text(encoding="utf-8"))
    out = []
    for qid, qinfo in spec["questions"].items():
        out += [f"**{qinfo['short']}**", "", "| 가설 | 이름 | 최종 판정 | 최근 문구 | 근거 청구 |", "|---|---|---|---|---|"]
        for row in (r for r in rows if r["q"] == qid):
            verdict = row["shown"] if not row["arrived"] else "결과 도착(판정 입력 필요)"
            claims = ", ".join(row["claims"])
            out.append(f"| {row['id']} | {row['name']} | **{verdict}** | {row['raw']} | {claims} |")
        out.append("")
    return "\n".join(out)


def _limits_block(claims: list[Claim], limits: dict[str, str]) -> str:
    used: dict[str, list[str]] = {}
    for claim in claims:
        if claim.limit:
            used.setdefault(claim.limit, []).append(claim.id)
    lines = []
    for key, text in limits.items():
        if key in used:
            lines.append(f"- **{key}** ({', '.join(used[key])}): {text}")
    return "\n".join(lines)


def _pending_block(claims: list[Claim]) -> str:
    lines = []
    for claim in claims:
        if claim.kind != "pending":
            continue
        have = load_summary(claim.pending_exp or "") is not None
        state = "결과 도착 — 청구를 채워야 함" if have else "대기"
        lines.append(f"- **{claim.pending_exp}** ({claim.hyp}): {state}. {claim.text}")
    return "\n".join(lines) or "현재 진행 중인 실험은 없다. 모든 실험(E1~E9)의 결과가 들어와 있다."


def render() -> str:
    spec = yaml.safe_load((KIT_DIR / "claims.yaml").read_text(encoding="utf-8"))
    limits: dict[str, str] = {k: " ".join(str(v).split()) for k, v in spec["limits"].items()}
    claims = load_claims()
    by_id = {c.id: c for c in claims}
    rows, _ = figures.effective_verdicts()

    def sub(match: re.Match[str]) -> str:
        body = match.group(1).strip()
        if body.startswith("claims:"):
            group = body.split(":", 1)[1]
            blocks = [_claim_block(c, limits) for c in claims if c.group == group]
            return "\n".join(blocks) if blocks else "(이 묶음의 청구가 없다)"
        if body.startswith("v:count:"):
            key = body.split(":", 2)[2]
            if key == "total_decided":
                return str(sum(1 for r in rows if r["shown"] != "대기"))
            return str(sum(1 for r in rows if r["shown"] == key))
        if body.startswith("v:"):
            _, claim_id, idx = body.split(":")
            return _item_value(by_id[claim_id].items[int(idx)])
        if body == "verdicts":
            return _verdict_table()
        if body == "limits":
            return _limits_block(claims, limits)
        if body == "pending":
            return _pending_block(claims)
        if body == "inconsistencies":
            return "\n".join(f"{n}. **{i['id']}** (`{i['where']}`): {' '.join(i['text'].split())}" for n, i in enumerate(spec["inconsistencies"], 1))
        raise KeyError(f"unknown placeholder {{{{{body}}}}}")

    return _PLACEHOLDER.sub(sub, TEMPLATE_PATH.read_text(encoding="utf-8")).rstrip() + "\n"


def write() -> Path:
    OUT_PATH.write_text(render(), encoding="utf-8")
    return OUT_PATH


def kit_out_of_sync() -> str:
    """Empty string when REPORT_KIT.md equals a fresh render, else a one-line reason."""
    if not OUT_PATH.exists():
        return "experiments/REPORT_KIT.md 가 없다 — python -m experiments.kit.build"
    if OUT_PATH.read_text(encoding="utf-8") != render():
        return "experiments/REPORT_KIT.md 가 낡았다(claims.yaml·template·summary.json 과 다르다) — python -m experiments.kit.build"
    return ""
