"""Raw-record and summary I/O shared by every experiment.

Raw records are appended one JSON object per line with fsync so a long run can be killed
and resumed (`done_keys`). Summaries are canonical JSON (sorted keys, floats rounded to 6
decimals) so `python -m experiments verify` can compare a recomputation byte-for-byte.
"""

from __future__ import annotations

import json
import os
import platform
import subprocess
import sys
from datetime import UTC, datetime
from functools import lru_cache
from pathlib import Path
from typing import Any

__all__ = [
    "FLOAT_DIGITS",
    "append_jsonl",
    "canonical_json",
    "done_keys",
    "normalize",
    "read_jsonl",
    "repo_root",
    "run_meta",
    "write_summary",
]

FLOAT_DIGITS = 6


def repo_root() -> Path:
    """Repository root (the folder that contains the ``experiments`` package)."""
    return Path(__file__).resolve().parents[2]


# ── JSONL ────────────────────────────────────────────────────────────────────
def append_jsonl(path: str | Path, record: dict[str, Any]) -> None:
    """Append one record as one line; create parent dirs; flush + fsync before returning."""
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    line = json.dumps(record, ensure_ascii=False, default=str, allow_nan=True)
    if "\n" in line:  # json.dumps never emits a raw newline; guard the one-record-per-line rule
        raise ValueError("record serialised with a newline")
    with p.open("a", encoding="utf-8") as fh:
        fh.write(line + "\n")
        fh.flush()
        os.fsync(fh.fileno())


def read_jsonl(path: str | Path) -> list[dict[str, Any]]:
    """Read every record. A missing file is an empty list.

    A truncated *final* line (a run killed mid-write) is skipped; a bad line anywhere else
    raises, because that means the file was edited or corrupted.
    """
    p = Path(path)
    if not p.exists():
        return []
    lines = p.read_text(encoding="utf-8").splitlines()
    out: list[dict[str, Any]] = []
    for i, line in enumerate(lines):
        if not line.strip():
            continue
        try:
            out.append(json.loads(line))
        except json.JSONDecodeError:
            if i == len(lines) - 1:
                continue
            raise
    return out


def done_keys(path: str | Path, key_fields: tuple[str, ...] | list[str]) -> set[tuple[Any, ...]]:
    """Set of ``tuple(record[f] for f in key_fields)`` already written — for resume-skipping."""
    keys: set[tuple[Any, ...]] = set()
    for rec in read_jsonl(path):
        keys.add(tuple(_hashable(rec.get(f)) for f in key_fields))
    return keys


def _hashable(value: Any) -> Any:
    if isinstance(value, list):
        return tuple(_hashable(v) for v in value)
    if isinstance(value, dict):
        return tuple(sorted((k, _hashable(v)) for k, v in value.items()))
    return value


# ── canonical summary ────────────────────────────────────────────────────────
def normalize(obj: Any) -> Any:
    """Recursively make *obj* JSON-canonical: floats rounded, numpy scalars/arrays unwrapped."""
    if isinstance(obj, bool) or obj is None or isinstance(obj, str):
        return obj
    if isinstance(obj, int):
        return int(obj)
    if isinstance(obj, float):
        return round(obj, FLOAT_DIGITS)
    if isinstance(obj, dict):
        return {str(k): normalize(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple, set, frozenset)):
        seq = sorted(obj, key=repr) if isinstance(obj, (set, frozenset)) else obj
        return [normalize(v) for v in seq]
    if hasattr(obj, "tolist"):  # numpy array / scalar
        return normalize(obj.tolist())
    if hasattr(obj, "item"):
        return normalize(obj.item())
    if hasattr(obj, "value") and hasattr(obj, "name"):  # enum
        return normalize(obj.value)
    raise TypeError(f"summary value not JSON-serialisable: {type(obj).__name__}")


def canonical_json(obj: Any) -> str:
    """Sorted keys, 2-space indent, UTF-8 text, floats rounded to 6 decimals, trailing newline."""
    return json.dumps(normalize(obj), sort_keys=True, ensure_ascii=False, indent=2) + "\n"


def write_summary(exp_dir: str | Path, summary: dict[str, Any]) -> Path:
    """Write ``<exp_dir>/results/summary.json`` canonically and return its path."""
    out = Path(exp_dir) / "results" / "summary.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(canonical_json(summary), encoding="utf-8")
    return out


# ── run metadata ─────────────────────────────────────────────────────────────
def _git(*args: str) -> str | None:
    try:
        res = subprocess.run(
            ["git", *args], cwd=repo_root(), capture_output=True, text=True, timeout=15, check=False
        )
    except (OSError, subprocess.SubprocessError):
        return None
    return res.stdout.strip() if res.returncode == 0 else None


@lru_cache(maxsize=1)
def _git_state() -> tuple[str | None, bool | None]:
    """(sha, dirty) computed once per process — per-record subprocess calls would be slow.

    `dirty` ignores experiment outputs (raw/, results/, figures/): it answers "was the code
    different from the commit", and a running experiment always changes its own outputs.
    """
    sha = _git("rev-parse", "HEAD")
    status = _git(
        "status", "--porcelain", "--", ".",
        ":(exclude,glob)experiments/**/raw/**",
        ":(exclude,glob)experiments/**/results/**",
        ":(exclude,glob)experiments/**/figures/**",
    )
    return sha, (None if status is None else bool(status))


def run_meta() -> dict[str, Any]:
    """Provenance block for a raw record: commit, dirty flag, python, platform, load, time."""
    sha, dirty = _git_state()
    try:
        load1: float | None = os.getloadavg()[0]
    except OSError:  # pragma: no cover - platform without getloadavg
        load1 = None
    return {
        "git_sha": sha,
        "git_dirty": dirty,
        "python": sys.version.split()[0],
        "platform": platform.platform(),
        "load1": load1,
        "ts": datetime.now(UTC).isoformat(),
    }
