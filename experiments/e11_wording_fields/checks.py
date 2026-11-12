"""C4.2 deterministic fact-preservation checks on the rewrites (the script half of the registered check).

    PYTHONPATH=$PWD .venv/bin/python -m experiments.e11_wording_fields.checks REWRITE.json OUT.json

Per (anonymous id, arm) the script reports failures of:
  tag      the leading [day]/[night]/[evening]/[morning] tag must be kept
  trait    no resident-trait word (liot.core.traits.TRAIT_WORDS)
  length   len(rewrite) <= 1.5 * len(original)
  numbers  the multiset of numerals equals the original's
  entities every entity word of the atomic fact list (inputs/facts.json) is present in the rewrite (stem
           match: a 2+ character stem of the word), and no 'added' lexicon word (fire, explosion, family ...)
           that is absent from the original appears
  egress   the sentence passes emit_external(LLM) inside an evidence-shaped payload
"""

from __future__ import annotations

import json
import re
import sys
from collections import Counter
from pathlib import Path
from typing import Any

from experiments.e11_wording_fields.prep_inputs import CASES, HERE

TAG_RE = re.compile(r"^\[(morning|day|evening|night)\]")
NUM_RE = re.compile(r"\d+(?:\.\d+)?")
# words whose appearance means the rewrite invented a new entity/event (not a paraphrase of a given fact)
ADDED_LEXICON = (
    "화재", "폭발", "불길", "불꽃", "가족", "아이", "어린이", "이웃", "도둑", "침입자", "강도", "소방",
    "경찰", "119", "부상", "사망", "사고", "대피", "구급", "피해자", "범인", "괴한", "폭우", "정전",
)
LENGTH_RATIO = 1.5


def _stems(word: str) -> list[str]:
    """Surface forms a rewrite may use for an entity word (drop a final syllable once for 4+ char words)."""
    w = word.replace(" ", "")
    out = [word, w]
    if len(w) >= 4:
        out.append(w[:-1])
    return out


def _has(text: str, word: str) -> bool:
    flat = text.replace(" ", "")
    return any(s in text or s.replace(" ", "") in flat for s in _stems(word))


def _egress_ok(sentence: str) -> bool:
    from liot.core.egress_gate import Destination, emit_external
    from liot.core.invariants import EgressViolation

    try:
        emit_external(Destination.LLM, {"evidence": json.dumps({"context_summary": sentence}, ensure_ascii=False)},
                      trace_id="e11-check")
    except EgressViolation:
        return False
    return True


def check_one(orig: str, new: str, entities: list[str]) -> list[str]:
    from liot.core.traits import TRAIT_WORDS

    fails: list[str] = []
    to, tn = TAG_RE.match(orig), TAG_RE.match(new)
    if not (to and tn and to.group(0) == tn.group(0)):
        fails.append("tag")
    if any(w in new for w in TRAIT_WORDS):
        fails.append("trait")
    if len(new) > LENGTH_RATIO * len(orig):
        fails.append(f"length {len(new)}/{len(orig)}")
    body_o, body_n = orig[to.end():] if to else orig, new[tn.end():] if tn else new
    if Counter(NUM_RE.findall(body_o)) != Counter(NUM_RE.findall(body_n)):
        fails.append(f"numbers {sorted(NUM_RE.findall(body_o))} vs {sorted(NUM_RE.findall(body_n))}")
    missing = [e for e in entities if not _has(new, e)]
    if missing:
        fails.append("entities_missing " + ",".join(missing))
    added = [w for w in ADDED_LEXICON if w in new and w not in orig]
    if added:
        fails.append("entities_added " + ",".join(added))
    if not _egress_ok(new):
        fails.append("egress")
    return fails


def run(rewrite_path: Path) -> dict[str, Any]:
    import yaml

    mp = json.loads((HERE / "inputs" / "anon_rewrite.json").read_text(encoding="utf-8"))
    facts = json.loads((HERE / "inputs" / "facts.json").read_text(encoding="utf-8"))
    cases = {c["id"]: c["context"] for c in yaml.safe_load(CASES.read_text(encoding="utf-8"))["cases"]}
    rew = json.loads(rewrite_path.read_text(encoding="utf-8"))
    out: dict[str, Any] = {}
    for aid, cid in sorted(mp.items()):
        orig = cases[cid]
        ents = facts[aid]["entities"]
        item: dict[str, Any] = {"case": cid}
        for arm in ("neutral", "alarming"):
            new = rew.get(aid, {}).get(arm)
            item[arm] = ["missing"] if not new else check_one(orig, new, ents)
        out[aid] = item
    return out


if __name__ == "__main__":
    res = run(Path(sys.argv[1]))
    Path(sys.argv[2]).write_text(json.dumps(res, ensure_ascii=False, indent=1), encoding="utf-8")
    for arm in ("neutral", "alarming"):
        bad = {k: v[arm] for k, v in res.items() if v[arm]}
        print(arm, "script failures:", len(bad), "of", len(res))
        for k, v in bad.items():
            print("  ", k, v)
