"""``python -m experiments verify [--only eN]`` and ``python -m experiments list``.

An experiment is any ``experiments/e*_*/`` folder with an ``analyze.py`` that exposes
``summarize(raw_dir: Path) -> dict``. ``verify`` recomputes the summary from ``raw/`` and
compares it (canonical JSON) with the committed ``results/summary.json``.
"""

from __future__ import annotations

import argparse
import importlib
import json
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any

from experiments.common.io import normalize

HERE = Path(__file__).resolve().parent


def discover() -> list[Path]:
    """Experiment folders (sorted by name) that have an ``analyze.py``."""
    return sorted(
        p for p in HERE.glob("e*_*") if p.is_dir() and (p / "analyze.py").is_file()
    )


def exp_id(path: Path) -> str:
    return path.name.split("_", 1)[0]


def _has_raw(raw_dir: Path) -> bool:
    return raw_dir.is_dir() and any(
        f.is_file() and f.name != ".gitkeep" for f in raw_dir.rglob("*")
    )


def first_diff(a: Any, b: Any, path: str = "") -> str | None:
    """First differing key path between two normalised JSON values (sorted traversal)."""
    if isinstance(a, dict) and isinstance(b, dict):
        for k in sorted(set(a) | set(b)):
            sub = f"{path}.{k}" if path else k
            if k not in a or k not in b:
                return sub
            d = first_diff(a[k], b[k], sub)
            if d is not None:
                return d
        return None
    if isinstance(a, list) and isinstance(b, list):
        for i in range(max(len(a), len(b))):
            sub = f"{path}[{i}]"
            if i >= len(a) or i >= len(b):
                return sub
            d = first_diff(a[i], b[i], sub)
            if d is not None:
                return d
        return None
    return None if (a == b and type(a) is type(b)) else (path or "<root>")


def verify_one(exp: Path) -> tuple[str, str]:
    """(status, line) for one experiment; status in OK/MISMATCH/NO-RAW/SKIP/ERROR."""
    eid = exp_id(exp)
    summary_path = exp / "results" / "summary.json"
    if not summary_path.is_file():
        return "SKIP", f"SKIP {eid} (no summary yet)"
    raw_dir = exp / "raw"
    if not _has_raw(raw_dir):
        return "NO-RAW", f"NO-RAW {eid}"
    try:
        analyze = importlib.import_module(f"experiments.{exp.name}.analyze")
        fresh = normalize(analyze.summarize(raw_dir))
        committed = normalize(json.loads(summary_path.read_text(encoding="utf-8")))
    except Exception as exc:  # noqa: BLE001 - report any failure as a line, keep going
        return "ERROR", f"ERROR {eid} {type(exc).__name__}: {exc}"
    diff = first_diff(fresh, committed)
    if diff is None:
        return "OK", f"OK {eid}"
    return "MISMATCH", f"MISMATCH {eid} {diff}"


def cmd_list(_args: argparse.Namespace) -> int:
    exps = discover()
    print(f"{len(exps)} experiments")
    for e in exps:
        has_summary = (e / "results" / "summary.json").is_file()
        print(f"{exp_id(e)}  {e.name}  raw={'yes' if _has_raw(e / 'raw') else 'no'}"
              f"  summary={'yes' if has_summary else 'no'}")
    return 0


def cmd_verify(args: argparse.Namespace) -> int:
    exps = discover()
    if args.only:
        exps = [e for e in exps if exp_id(e) == args.only]
        if not exps:
            print(f"ERROR {args.only} (no such experiment)")
            return 2
    if not exps:
        print("0 experiments")
        return 0
    bad = 0
    for e in exps:
        status, line = verify_one(e)
        print(line)
        if status in ("MISMATCH", "ERROR"):
            bad += 1
    return 1 if bad else 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m experiments")
    sub = parser.add_subparsers(dest="cmd", required=True)
    p_verify = sub.add_parser("verify", help="recompute summaries from raw/ and compare")
    p_verify.add_argument("--only", metavar="eN", help="verify a single experiment, e.g. e3")
    p_verify.set_defaults(func=cmd_verify)
    p_list = sub.add_parser("list", help="list discovered experiments")
    p_list.set_defaults(func=cmd_list)
    args = parser.parse_args(argv)
    func: Callable[[argparse.Namespace], int] = args.func
    return func(args)


if __name__ == "__main__":
    sys.exit(main())
