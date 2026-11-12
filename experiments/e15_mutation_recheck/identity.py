"""Mutant identity across a production-code change (E15).

E8 measured the code at commit ``1e83525``. Two of the ten target files changed afterwards
(``severity.py``: duplicate-YAML-key rejection, commit 9e38185; ``escalation.py``: first-alert
resend, commit 8e3b2e3); the other eight are byte-identical. A mutant id embeds ``line:col``, so
ids of unchanged mutants in a changed file drift. Identity rule (strict, no fuzzy matching):

* **identical_id** - same ``mutant_id`` in both runs (file unchanged, or the id agrees and so does
  the mutant's operator/variant/code line).
* **identical_context** - the file changed, but the mutant's source line lies in a block that is
  *equal* in a ``difflib`` line alignment of old and new source (so the code line is byte-identical
  and moved by whole lines only), and the new run has a mutant on the mapped line with the same
  column, operator, variant, replaced text, replacement text and code line.
* **old_only** - the old mutant has no counterpart (its line was edited or deleted).
* **new_only** - a new mutant no old mutant maps to (new or edited code).

Only the first two classes enter the paired comparison.
"""

from __future__ import annotations

import difflib
from typing import Any

CONTEXT_FIELDS = ("col", "operator", "variant", "original", "mutated", "code_line")


def line_map(old_src: bytes, new_src: bytes) -> dict[int, int]:
    """1-based old line -> 1-based new line, for lines inside *equal* alignment blocks only."""
    a = old_src.decode("utf-8").splitlines()
    b = new_src.decode("utf-8").splitlines()
    sm = difflib.SequenceMatcher(None, a, b, autojunk=False)
    out: dict[int, int] = {}
    for tag, i1, i2, j1, _j2 in sm.get_opcodes():
        if tag == "equal":
            for k in range(i2 - i1):
                out[i1 + k + 1] = j1 + k + 1
    return out


def match_module(
    old_rows: list[dict[str, Any]],
    new_rows: list[dict[str, Any]],
    lmap: dict[int, int] | None,
) -> list[dict[str, Any]]:
    """One record per old mutant and per unmatched new mutant (see module docstring).

    ``lmap`` is None when the file is byte-identical; then only ids are compared.
    """
    new_by_id = {r["mutant_id"]: r for r in new_rows}
    new_by_ctx: dict[tuple[Any, ...], list[dict[str, Any]]] = {}
    for r in new_rows:
        new_by_ctx.setdefault((r["line"], *(r[f] for f in CONTEXT_FIELDS)), []).append(r)
    used: set[str] = set()
    out: list[dict[str, Any]] = []
    for o in sorted(old_rows, key=lambda r: (r["line"], r["col"], r["operator"], r["mutant_id"])):
        hit: dict[str, Any] | None = None
        how = "old_only"
        cand = new_by_id.get(o["mutant_id"])
        if cand is not None and all(cand[f] == o[f] for f in CONTEXT_FIELDS):
            hit, how = cand, "identical_id"
        if hit is None and lmap is not None and o["line"] in lmap:
            key = (lmap[o["line"]], *(o[f] for f in CONTEXT_FIELDS))
            for cand in new_by_ctx.get(key, []):
                if cand["mutant_id"] not in used:
                    hit, how = cand, "identical_context"
                    break
        if hit is not None:
            used.add(hit["mutant_id"])
        out.append({
            "module": o["module"], "old_id": o["mutant_id"],
            "new_id": hit["mutant_id"] if hit else None, "status": how,
            "old_line": o["line"], "new_line": hit["line"] if hit else None,
        })
    for r in new_rows:
        if r["mutant_id"] not in used:
            out.append({
                "module": r["module"], "old_id": None, "new_id": r["mutant_id"],
                "status": "new_only", "old_line": None, "new_line": r["line"],
            })
    return out
