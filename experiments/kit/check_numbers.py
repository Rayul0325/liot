"""Verify every number cited in REPORT_KIT.md against the experiments' summary.json files.

Source of truth for the cited numbers is ``experiments/kit/claims.yaml``. Each claim has one or
more *items*; an item names a summary.json value by a key path, the value the report quotes
(``expected``, already in display units) and how many decimals it is rounded to (``round``).

Checks, in order of severity:

1. summary.json value  vs  ``expected``            -> MISMATCH   (exit code 1)
2. REPORT_KIT.md       vs  claims.yaml + template  -> STALE-KIT  (exit code 1)
3. RESULTS.md text     vs  summary.json value      -> RESULTS-DIFF (listed, exit 0; ``--strict`` -> 1)

An experiment whose summary.json does not exist yet (e6/e8/e9) is reported as PENDING and does
not fail the run. If its summary appears while the claims are still placeholders, the run fails
with NEW-RESULT so nobody forgets to fill in the claims.

Usage:  python -m experiments.kit.check_numbers [--strict] [--propose] [--quiet]
"""

from __future__ import annotations

import argparse
import json
import math
import re
import sys
from dataclasses import dataclass, field
from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path
from typing import Any

import yaml

KIT_DIR = Path(__file__).resolve().parent
EXP_DIR = KIT_DIR.parent
CLAIMS_PATH = KIT_DIR / "claims.yaml"

__all__ = [
    "Claim",
    "Item",
    "load_claims",
    "load_summary",
    "resolve",
    "fmt_item",
    "run_check",
    "main",
]


@dataclass
class Item:
    """One quoted number: where it lives, what the report says, how it is shown."""

    label: str
    exp: str
    path: list[Any]
    expected: Any = None
    round: int | None = None
    scale: float = 1.0
    fmt: str = "{}"
    unit: str = ""
    agg: str | None = None
    fn: str | None = None  # name in kit.derived.FUNCS: value computed from summary cells
    args: dict[str, Any] = field(default_factory=dict)
    md: str | None = None  # the text as printed in RESULTS.md (for the text-vs-summary check)
    note: str | None = None


@dataclass
class Claim:
    id: str
    group: str
    text: str
    hyp: str | None = None
    verdict: str | None = None
    fig: str | None = None
    limit: str | None = None
    kind: str = "number"  # number | pending
    pending_exp: str | None = None
    items: list[Item] = field(default_factory=list)


def load_summary(exp: str) -> dict[str, Any] | None:
    """Parsed ``<exp>/results/summary.json`` or ``None`` when the experiment has not landed."""
    path = EXP_DIR / exp / "results" / "summary.json"
    if not path.exists():
        return None
    return json.loads(path.read_text(encoding="utf-8"))


def _step(node: Any, key: Any) -> Any:
    """Follow one path element: dict key, list index, or ``{field: value}`` list selector."""
    if isinstance(key, dict):
        if not isinstance(node, list):
            raise KeyError(f"selector {key} needs a list, got {type(node).__name__}")
        for row in node:
            if isinstance(row, dict) and all(row.get(k) == v for k, v in key.items()):
                return row
        raise KeyError(f"no list element matches {key}")
    if isinstance(node, list):
        return node[int(key)]
    return node[key]


def resolve(summary: dict[str, Any], path: list[Any], agg: str | None = None) -> Any:
    """Value at *path*. A ``"*"`` element fans out over a dict's values; *agg* reduces them."""
    nodes: list[Any] = [summary]
    fanned = False
    for key in path:
        if key == "*":
            fanned = True
            nodes = [v for node in nodes for v in (node.values() if isinstance(node, dict) else node)]
        else:
            nodes = [_step(node, key) for node in nodes]
    if not fanned:
        return len(nodes[0]) if agg == "len" else nodes[0]
    if agg == "sum":
        return sum(nodes)
    if agg == "min":
        return min(nodes)
    if agg == "max":
        return max(nodes)
    if agg == "count":
        return len(nodes)
    if agg == "count_true":
        return sum(1 for n in nodes if n is True)
    if agg == "count_false":
        return sum(1 for n in nodes if n is False)
    raise ValueError("a '*' path needs agg: sum|min|max|count|count_true|count_false")


def _scaled(actual: Any, item: Item) -> Any:
    if isinstance(actual, bool) or not isinstance(actual, (int, float)):
        return actual
    return actual * item.scale


def round_half_up(value: float, decimals: int) -> float:
    """Round like a person (68.5 -> 69), not like Python (68.5 -> 68)."""
    quantum = Decimal(1).scaleb(-decimals)
    return float(Decimal(repr(round(value, 10))).quantize(quantum, rounding=ROUND_HALF_UP))


def _same(actual: Any, expected: Any, decimals: int | None) -> bool:
    if isinstance(expected, bool) or isinstance(actual, bool):
        return actual is expected
    if isinstance(expected, (int, float)) and isinstance(actual, (int, float)):
        if decimals is None:
            return math.isclose(actual, expected, rel_tol=0, abs_tol=1e-9)
        return math.isclose(round_half_up(actual, decimals), round_half_up(expected, decimals), abs_tol=1e-9)
    return actual == expected


_FIXED = re.compile(r"^\{:(\+?)(,?)\.(\d+)f\}$")


def _format(value: Any, spec: str) -> str:
    """``str.format`` but fixed-point specs round half up, so 71.25 reads 71.3 as in a table."""
    match = _FIXED.match(spec)
    if match and isinstance(value, (int, float)) and not isinstance(value, bool):
        sign, comma, digits = match.groups()
        value = round_half_up(float(value), int(digits))
        return ("{:" + sign + comma + "." + digits + "f}").format(value)
    return spec.format(value)


def norm(text: str) -> str:
    """Make typographic minus signs comparable (RESULTS.md writes U+2212)."""
    return text.replace("\u2212", "-").replace("\u2013", "-")


def fmt_item(actual: Any, item: Item, with_unit: bool = True) -> str:
    """Display text of *actual* the way the report quotes it."""
    shown = _scaled(actual, item)
    text = _format(shown, item.fmt)
    return f"{text}{item.unit}" if with_unit else text


def load_claims(path: Path = CLAIMS_PATH) -> list[Claim]:
    raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    claims: list[Claim] = []
    for row in raw["claims"]:
        default_exp = row.get("exp")
        items = []
        for it in row.get("items", []):
            items.append(
                Item(
                    label=it["label"],
                    exp=it.get("exp", default_exp),
                    path=list(it["path"]),
                    expected=it.get("expected"),
                    round=it.get("round"),
                    scale=float(it.get("scale", 1.0)),
                    fmt=it.get("fmt", "{}"),
                    unit=it.get("unit", ""),
                    agg=it.get("agg"),
                    fn=it.get("fn"),
                    args=dict(it.get("args", {})),
                    md=it.get("md"),
                    note=it.get("note"),
                )
            )
        claims.append(
            Claim(
                id=row["id"],
                group=row["group"],
                text=row["text"],
                hyp=row.get("hyp"),
                verdict=row.get("verdict"),
                fig=row.get("fig"),
                limit=row.get("limit"),
                kind=row.get("kind", "number"),
                pending_exp=row.get("pending_exp"),
                items=items,
            )
        )
    return claims


def _results_text(exp: str, cache: dict[str, str]) -> str:
    if exp not in cache:
        path = EXP_DIR / exp / "RESULTS.md"
        cache[exp] = path.read_text(encoding="utf-8") if path.exists() else ""
    return cache[exp]


@dataclass
class Report:
    ok: int = 0
    mismatches: list[str] = field(default_factory=list)
    pending: list[str] = field(default_factory=list)
    new_results: list[str] = field(default_factory=list)
    results_diff: list[str] = field(default_factory=list)
    results_rounding: list[str] = field(default_factory=list)
    proposals: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)


def run_check(claims: list[Claim], propose: bool = False) -> Report:
    """Check every claim item against the live summary files."""
    rep = Report()
    summaries: dict[str, dict[str, Any] | None] = {}
    texts: dict[str, str] = {}
    for claim in claims:
        if claim.kind == "pending":
            exp = claim.pending_exp or ""
            if load_summary(exp) is None:
                rep.pending.append(f"{claim.id} ({exp})")
            else:
                rep.new_results.append(f"{claim.id}: {exp}/results/summary.json 가 도착했다 — claims.yaml 의 자리표시를 숫자 항목으로 바꿔라")
            continue
        for item in claim.items:
            tag = f"{claim.id} · {item.label}"
            if item.exp not in summaries:
                summaries[item.exp] = load_summary(item.exp)
            summary = summaries[item.exp]
            if summary is None:
                rep.pending.append(f"{tag} ({item.exp})")
                continue
            try:
                if item.fn:
                    from experiments.kit.derived import FUNCS

                    actual = FUNCS[item.fn](summary, **item.args)
                else:
                    actual = resolve(summary, item.path, item.agg)
            except (KeyError, IndexError, TypeError, ValueError) as err:
                rep.errors.append(f"{tag}: 경로를 따라갈 수 없다 {item.path} ({err!r})")
                continue
            shown = _scaled(actual, item)
            if propose:
                rep.proposals.append(f"{tag}: {_format(shown, item.fmt)}{item.unit}  (raw {actual!r})")
                continue
            if item.expected is None:
                rep.errors.append(f"{tag}: expected 가 비어 있다")
                continue
            if _same(shown, item.expected, item.round):
                rep.ok += 1
            else:
                rep.mismatches.append(
                    f"{tag}: summary.json → {str(shown)[:80]} (경로 {' > '.join(map(str, item.path))}), 보고서 기대값 {item.expected!r}"
                )
                continue
            if item.md is not None:
                text = norm(_results_text(item.exp, texts))
                if norm(item.md) not in text:
                    rep.results_diff.append(f"{tag}: RESULTS.md 에 '{item.md}' 가 없다")
                else:
                    verdict = _md_vs_actual(item.md, actual)
                    if verdict == "rounding":
                        rep.results_rounding.append(f"{tag}: RESULTS.md '{item.md}' 와 summary.json 값 {actual!r} 은 마지막 자리 반올림만 다르다")
                    elif verdict == "diff":
                        rep.results_diff.append(f"{tag}: RESULTS.md 는 '{item.md}', summary.json 값은 {actual!r}")
    return rep


def _md_vs_actual(md: str, actual: Any) -> str:
    """Compare a number as printed in RESULTS.md with the summary value: ok | rounding | diff."""
    if isinstance(actual, bool) or not isinstance(actual, (int, float)):
        return "ok" if norm(md) == str(actual) else "diff"
    cleaned = norm(md).replace("%", "").replace("ms", "").replace(",", "").strip()
    try:
        printed = float(cleaned)
    except ValueError:
        return "ok"  # not a plain number (e.g. a ratio like 5/8): nothing to compare
    digits = len(cleaned.split(".")[1]) if "." in cleaned else 0
    best = "diff"
    for scale in (1.0, 100.0, 1000.0):
        shown = actual * scale
        if math.isclose(round_half_up(shown, digits), printed, abs_tol=1e-9):
            return "ok"
        if abs(shown - printed) <= 1.0 * 10 ** (-digits) + 1e-9:
            best = "rounding"
    return best


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--strict", action="store_true", help="RESULTS.md 불일치도 실패로 센다")
    ap.add_argument("--propose", action="store_true", help="expected 대신 현재 값을 출력한다(처음 채울 때)")
    ap.add_argument("--quiet", action="store_true", help="통과한 줄은 출력하지 않는다")
    ap.add_argument("--no-kit-sync", action="store_true", help="REPORT_KIT.md 동기 검사를 건너뛴다")
    args = ap.parse_args(argv)

    claims = load_claims()
    rep = run_check(claims, propose=args.propose)
    if args.propose:
        print("\n".join(rep.proposals))
        for line in rep.errors:
            print("ERROR", line)
        return 0

    n_items = sum(len(c.items) for c in claims)
    n_claims = sum(1 for c in claims if c.kind == "number")
    failed = bool(rep.mismatches or rep.errors or rep.new_results)

    if not args.no_kit_sync:
        from experiments.kit import render

        stale = render.kit_out_of_sync()
        if stale:
            print(f"STALE-KIT  {stale}")
            failed = True
        else:
            print("OK  REPORT_KIT.md 가 claims.yaml·template·summary.json 에서 다시 만든 것과 같다")

    for line in rep.mismatches:
        print("MISMATCH  ", line)
    for line in rep.errors:
        print("ERROR     ", line)
    for line in rep.new_results:
        print("NEW-RESULT", line)
    for line in rep.results_diff:
        print("RESULTS-DIFF", line)
    for line in rep.results_rounding:
        print("RESULTS-ROUNDING", line)
    if rep.pending and not args.quiet:
        names = sorted({p.split(" (")[-1].rstrip(")") for p in rep.pending})
        print(f"PENDING   {len(rep.pending)}건 — 아직 summary.json 이 없는 실험: {', '.join(names)}")
    print(
        f"checked {rep.ok}/{n_items - sum(1 for p in rep.pending if '·' in p)} items in {n_claims} claims: "
        f"{len(rep.mismatches)} mismatch, {len(rep.errors)} error, {len(rep.results_diff)} RESULTS-DIFF, {len(rep.results_rounding)} RESULTS-ROUNDING, "
        f"{len(rep.pending)} pending"
    )
    if args.strict and rep.results_diff:
        failed = True
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
