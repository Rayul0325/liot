"""Input of the one repair round: the failed rewrites of pass 1 with the checker's reasons.

    PYTHONPATH=$PWD .venv/bin/python -m experiments.e11_wording_fields.prep_repair OUT_FILE
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import yaml

from experiments.e11_wording_fields.combine import failures
from experiments.e11_wording_fields.prep_inputs import CASES, HERE


def main(out: Path) -> None:
    mp = json.loads((HERE / "inputs" / "anon_rewrite.json").read_text(encoding="utf-8"))
    cases = {c["id"]: c["context"] for c in yaml.safe_load(CASES.read_text(encoding="utf-8"))["cases"]}
    rw = json.loads((HERE / "inputs" / "rewrite_output_pass1.json").read_text(encoding="utf-8"))
    facts = json.loads((HERE / "inputs" / "facts.json").read_text(encoding="utf-8"))
    bad, _ = failures("pass1")
    items: dict[str, dict[str, object]] = {}
    for arm in ("neutral", "alarming"):
        for aid, why in sorted(bad[arm].items()):
            items.setdefault(aid, {"original": cases[mp[aid]], "atomic_facts": facts[aid]["facts"], "redo": {}})
            items[aid]["redo"][arm] = {"previous": rw[aid][arm], "problems": why}  # type: ignore[index]
    out.write_text(json.dumps(items, ensure_ascii=False, indent=1), encoding="utf-8")
    print(len(items), "ids;", sum(len(v["redo"]) for v in items.values()), "rewrites to redo")  # type: ignore[arg-type]


if __name__ == "__main__":
    main(Path(sys.argv[1]))
