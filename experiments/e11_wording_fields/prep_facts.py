"""Prepare the blind input for the atomic-fact extractor (C4.2, metric-review fix 1).

    PYTHONPATH=$PWD .venv/bin/python -m experiments.e11_wording_fields.prep_facts OUT_FILE

Per anonymised rewrite id (r01..r36): the original sentence plus the definition sentence of the
blind coder's subtype code (``bench/risk/observations_blind.yaml``, written by an agent that saw no
labels).  The code NAME is withheld (it carries a calm/danger hint); only the definition text goes
to the extractor.  Occupied/abnormal_for values from the same file are included as given facts.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import yaml

HERE = Path(__file__).resolve().parent
BENCH = HERE.parents[1] / "bench" / "risk"


def main(out: Path) -> None:
    cases = yaml.safe_load((BENCH / "cases.yaml").read_text(encoding="utf-8"))["cases"]
    by_id = {c["id"]: c for c in cases}
    blind = yaml.safe_load((BENCH / "observations_blind.yaml").read_text(encoding="utf-8"))
    vocab = blind["vocabulary"]
    mp = json.loads((HERE / "inputs" / "anon_rewrite.json").read_text(encoding="utf-8"))
    res = {}
    for aid, cid in sorted(mp.items()):
        b = blind["cases"][cid]
        res[aid] = {
            "sentence": by_id[cid]["context"],
            "coder_definition": vocab[b["subtype"]],
            "coder_occupied": str(b["occupied"]),
            "coder_abnormal_for": str(b["abnormal_for"]),
        }
    out.write_text(json.dumps(res, ensure_ascii=False, indent=1), encoding="utf-8")
    print(len(res))


if __name__ == "__main__":
    main(Path(sys.argv[1]))
