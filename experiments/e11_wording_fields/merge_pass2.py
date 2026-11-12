"""Merge the repair round into one rewrite file (redo arms from pass 2, untouched arms from pass 1).

    PYTHONPATH=$PWD .venv/bin/python -m experiments.e11_wording_fields.merge_pass2 REPAIR_INPUT PASS2_OUTPUT

Writes ``inputs/rewrite_output_pass2.json`` (ids that had any redo; both arms present) and prints the ids
for ``prep_check --only``.  Pass-2 output for an arm that was not asked for is ignored (and counted).
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

from experiments.e11_wording_fields.prep_inputs import HERE


def main() -> None:
    inp = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
    p2 = json.loads(Path(sys.argv[2]).read_text(encoding="utf-8"))
    p1 = json.loads((HERE / "inputs" / "rewrite_output_pass1.json").read_text(encoding="utf-8"))
    req = {(a, arm) for a, v in inp.items() for arm in v["redo"]}
    got = {(a, arm) for a, v in p2.items() for arm in v}
    print("requested", len(req), "returned", len(got), "missing", sorted(req - got), "ignored extra", len(got - req))
    out = {
        a: {arm: (p2[a][arm] if (a, arm) in req else p1[a][arm]) for arm in ("neutral", "alarming")}
        for a in sorted(inp)
    }
    (HERE / "inputs" / "rewrite_output_pass2.json").write_text(
        json.dumps(out, ensure_ascii=False, indent=1), encoding="utf-8"
    )
    print(",".join(sorted(inp)))


if __name__ == "__main__":
    main()
