"""Access to bench/risk/run_bench.py (a script, not a package) and the shared E5 constants."""

from __future__ import annotations

import importlib.util
import sys
from functools import lru_cache
from pathlib import Path
from types import ModuleType

REPO = Path(__file__).resolve().parents[2]
BENCH_DIR = REPO / "bench" / "risk"

CONTEXTS = ("sentence", "operational", "structured", "subtype", "subtype_trait")
CLOUD_CONTEXTS = ("sentence", "operational", "structured", "subtype")  # trait-coded arm: local only
LOCAL_MODELS = (
    "jevk5-9b-q8",
    "jevk5-4b-q8",
    "jevk5-4b-q4",
    "apus-openjev-4b-q4",
    "jev-9b-q6",
)
EXCLUDED_MODEL = "jev-omni-q4"  # 98/122 read errors in the bench; not a model judgement
JEVK5_FAMILY = ("jevk5-9b-q8", "jevk5-4b-q8", "jevk5-4b-q4")
CLOUD_JUDGES = ("jev-api", "luna", "haiku")
TIERS = ("ambient", "breath", "dusk", "ember")
SEVERITIES = ("record", "notice", "warning", "critical")

JUDGE_META: dict[str, dict[str, str]] = {
    "jevk5-9b-q8": {"kind": "local", "family": "jevk5", "interface": "letter-probability"},
    "jevk5-4b-q8": {"kind": "local", "family": "jevk5", "interface": "letter-probability"},
    "jevk5-4b-q4": {"kind": "local", "family": "jevk5", "interface": "letter-probability"},
    "apus-openjev-4b-q4": {"kind": "local", "family": "apus", "interface": "letter-probability"},
    "jev-9b-q6": {"kind": "local", "family": "jev9b", "interface": "letter-probability"},
    "jev-api": {"kind": "cloud", "family": "jev-api", "interface": "score-probability"},
    "luna": {"kind": "cloud", "family": "luna", "interface": "logprob-letter"},
    "haiku": {"kind": "cloud", "family": "haiku", "interface": "generative-json"},
}


@lru_cache(maxsize=1)
def run_bench() -> ModuleType:
    """Import bench/risk/run_bench.py by path (it inserts the repo root on sys.path itself)."""
    spec = importlib.util.spec_from_file_location("liot_bench_run_bench", BENCH_DIR / "run_bench.py")
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules["liot_bench_run_bench"] = mod
    spec.loader.exec_module(mod)
    return mod
