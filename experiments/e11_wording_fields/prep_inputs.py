"""Prepare the blind inputs for the rewriter (C4.2) and the two field coders (C4.3).

    PYTHONPATH=$PWD .venv/bin/python -m experiments.e11_wording_fields.prep_inputs OUT_DIR

Each agent receives only anonymised ids and the context sentence: no variant name, no case id,
no floor, no label, no model answer.  The anonymisation maps are committed under ``inputs/``.
Rewriter: the 36 pair sentences (18 calm, 18 danger).  Coders: the 45 non-injection sentences
(injection cases are excluded from every metric of this unit).
"""

from __future__ import annotations

import json
import random
import sys
from pathlib import Path

import yaml

HERE = Path(__file__).resolve().parent
CASES = HERE.parents[1] / "bench" / "risk" / "cases.yaml"


def anon(case_ids: list[str], prefix: str, seed: int) -> dict[str, str]:
    ids = list(case_ids)
    random.Random(seed).shuffle(ids)
    return {f"{prefix}{i + 1:02d}": cid for i, cid in enumerate(ids)}


def main(out_dir: Path) -> None:
    cases = yaml.safe_load(CASES.read_text(encoding="utf-8"))["cases"]
    by_id = {c["id"]: c for c in cases}
    pair = [c["id"] for c in cases if c["group"] in ("comfort_pair", "situation")]
    non = [c["id"] for c in cases if not c.get("injection")]
    out_dir.mkdir(parents=True, exist_ok=True)
    for name, ids, prefix, seed in (
        ("rewrite", pair, "r", 411),
        ("codeA", non, "k", 412),
        ("codeB", non, "m", 413),
    ):
        mp = anon(ids, prefix, seed)
        (HERE / "inputs" / f"anon_{name}.json").write_text(
            json.dumps(mp, ensure_ascii=False, indent=1), encoding="utf-8"
        )
        (out_dir / f"e11_{name}_input.json").write_text(
            json.dumps({a: by_id[c]["context"] for a, c in mp.items()}, ensure_ascii=False, indent=1),
            encoding="utf-8",
        )
        print(name, len(mp))


if __name__ == "__main__":
    main(Path(sys.argv[1]))
