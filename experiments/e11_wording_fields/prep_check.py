"""Build the blind input of the fact-checker agent (C4.2).

    PYTHONPATH=$PWD .venv/bin/python -m experiments.e11_wording_fields.prep_check REWRITE.json OUT_DIR TAG [--only ID,ID]

Per anonymous id: original sentence, atomic fact list, and two versions v1/v2 whose assignment to
neutral/alarming is random (seeded) and hidden from the checker.  The assignment is written next to
the committed inputs as ``check_assign_<TAG>.json``.
"""

from __future__ import annotations

import json
import random
import sys
from pathlib import Path

import yaml

from experiments.e11_wording_fields.prep_inputs import CASES, HERE


def main() -> None:
    rewrite, out_dir, tag = Path(sys.argv[1]), Path(sys.argv[2]), sys.argv[3]
    only = set(sys.argv[5].split(",")) if len(sys.argv) > 5 and sys.argv[4] == "--only" else None
    mp = json.loads((HERE / "inputs" / "anon_rewrite.json").read_text(encoding="utf-8"))
    facts = json.loads((HERE / "inputs" / "facts.json").read_text(encoding="utf-8"))
    cases = {c["id"]: c["context"] for c in yaml.safe_load(CASES.read_text(encoding="utf-8"))["cases"]}
    rew = json.loads(rewrite.read_text(encoding="utf-8"))
    rnd = random.Random(777 if tag == "pass1" else 778)
    inp, assign = {}, {}
    for aid in sorted(rew):
        if only and aid not in only:
            continue
        order = ["neutral", "alarming"]
        rnd.shuffle(order)
        assign[aid] = {"v1": order[0], "v2": order[1]}
        inp[aid] = {
            "original": cases[mp[aid]],
            "atomic_facts": facts[aid]["facts"],
            "v1": rew[aid][order[0]],
            "v2": rew[aid][order[1]],
        }
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / f"e11_check_{tag}_input.json").write_text(json.dumps(inp, ensure_ascii=False, indent=1), encoding="utf-8")
    (HERE / "inputs" / f"check_assign_{tag}.json").write_text(json.dumps(assign, indent=1), encoding="utf-8")
    print(len(inp))


if __name__ == "__main__":
    main()
