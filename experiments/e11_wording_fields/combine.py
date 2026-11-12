"""Combine the LLM checker and the script checks into per-arm exclusions (C4.2).

    PYTHONPATH=$PWD .venv/bin/python -m experiments.e11_wording_fields.combine TAG [--write-accepted]

TAG = pass1 (first rewrite) or pass2 (repaired items).  A rewrite fails if the LLM checker says any atomic
fact is missing/changed or lists any added claim, or if any script check fails.  ``--write-accepted`` merges
pass1 and pass2 into ``inputs/accepted_rewrites.json`` (case id -> sentence) and ``inputs/exclusions.json``.
"""

from __future__ import annotations

import json
import sys
from collections import defaultdict
from typing import Any

from experiments.e11_wording_fields.prep_inputs import HERE

INP = HERE / "inputs"


def _load(name: str) -> Any:
    return json.loads((INP / name).read_text(encoding="utf-8"))


def failures(tag: str) -> tuple[dict[str, dict[str, list[str]]], dict[str, list[int]]]:
    """({arm: {anon id: reasons}}, {arm: tone ratings}) for one pass."""
    chk, asg = _load(f"llm_check_{tag}.json"), _load(f"check_assign_{tag}.json")
    scr = _load(f"script_check_{tag}.json")
    bad: dict[str, dict[str, list[str]]] = defaultdict(dict)
    tone: dict[str, list[int]] = defaultdict(list)
    for aid, a in asg.items():
        for v in ("v1", "v2"):
            arm, c = a[v], chk[aid][v]
            fl: list[str] = []
            if not all(c["facts_present"]):
                fl.append("llm_fact_missing: " + c.get("note", ""))
            if c["added"]:
                fl.append("llm_added: " + "; ".join(c["added"]))
            if scr.get(aid, {}).get(arm):
                fl.append("script: " + "; ".join(scr[aid][arm]))
            tone[arm].append(int(c["tone"]))
            if fl:
                bad[arm][aid] = fl
    return bad, tone


def main() -> None:
    tag = sys.argv[1]
    bad, tone = failures(tag)
    mp = _load("anon_rewrite.json")
    for arm in ("neutral", "alarming"):
        print(f"{arm}: tone mean {sum(tone[arm]) / len(tone[arm]):.2f}  failures {len(bad[arm])}")
        for aid, fl in sorted(bad[arm].items()):
            print("  ", aid, mp[aid], fl)
    if "--write-accepted" in sys.argv:
        import yaml

        from experiments.e11_wording_fields.prep_inputs import CASES

        cases = {c["id"]: c["context"] for c in yaml.safe_load(CASES.read_text(encoding="utf-8"))["cases"]}
        final_bad: dict[str, dict[str, list[str]]] = {"neutral": {}, "alarming": {}}
        accepted: dict[str, dict[str, str]] = {"neutral": {}, "alarming": {}}
        p1, _ = failures("pass1")
        p1_rw = _load("rewrite_output_pass1.json")
        p2_rw: dict[str, Any] = {}
        p2_bad: dict[str, dict[str, list[str]]] = {"neutral": {}, "alarming": {}}
        if (INP / "llm_check_pass2.json").exists():
            p2_bad_raw, _ = failures("pass2")
            p2_bad = {a: dict(p2_bad_raw.get(a, {})) for a in ("neutral", "alarming")}
            p2_rw = _load("rewrite_output_pass2.json")
        for arm in ("neutral", "alarming"):
            for aid, cid in mp.items():
                if aid in p2_bad[arm]:  # failed the re-check in pass 2 (redone or merely re-checked): excluded
                    final_bad[arm][cid] = p2_bad[arm][aid]
                elif aid in p1.get(arm, {}):  # failed pass 1, repaired and passed pass 2
                    accepted[arm][cid] = p2_rw[aid][arm]
                else:
                    accepted[arm][cid] = p1_rw[aid][arm]
        (INP / "accepted_rewrites.json").write_text(json.dumps(accepted, ensure_ascii=False, indent=1), encoding="utf-8")
        excl = {
            "pass1_failures": {a: {mp[k]: v for k, v in p1.get(a, {}).items()} for a in ("neutral", "alarming")},
            "final_excluded": final_bad,
            "cases_per_arm": 18 * 2 // 2,
        }
        (INP / "exclusions.json").write_text(json.dumps(excl, ensure_ascii=False, indent=1), encoding="utf-8")
        print({a: len(v) for a, v in final_bad.items()}, "excluded in the final pass (of 36 per register)")


if __name__ == "__main__":
    main()
