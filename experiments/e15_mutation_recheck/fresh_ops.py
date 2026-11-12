"""Fresh mutant operators for E15 (C4.11(a), second measure of collateral kills).

The 15 gap tests were written against E8's surviving mutants, so any score on E8's own mutant set
can rise for that reason. These operators are NOT in E8's operator list, so none of their mutants
was ever enumerated, shown or targeted. They reuse E8's generator machinery (skip sets, byte-range
splicing, compile + de-dup check) by subclassing ``mutate._Gen``; only the operators differ.

* ``fr_str``       non-message string literal ``"abc"`` -> ``"abc_x"`` (``""`` -> ``"x"``); skipped
                   inside ``raise`` statements and logging calls (message text is rarely tested)
* ``fr_call_drop`` expression statement that is a call (not a docstring) -> ``pass``
* ``fr_ret_none``  ``return <expr>`` -> ``return None`` (no annotation filter, unlike E8's
                   ``return_const``)
* ``fr_bool_drop`` ``a and b`` / ``a or b`` (two operands) -> only the first / only the last operand
* ``fr_cmp_rev``   ``<``<->``>``, ``<=``<->``>=`` (reversal, not E8's boundary twin)
"""

from __future__ import annotations

import ast
import re

from experiments.e8_mutation import mutate

FRESH_OPERATORS = ("fr_str", "fr_call_drop", "fr_ret_none", "fr_bool_drop", "fr_cmp_rev")
_LOG_NAMES = {"debug", "info", "warning", "warn", "error", "exception", "critical", "log"}
_REV = {ast.Lt: ("<", ">"), ast.LtE: ("<=", ">="), ast.Gt: (">", "<"), ast.GtE: (">=", "<=")}


class _FreshGen(mutate._Gen):  # noqa: SLF001 - deliberate reuse of E8's span/skip machinery
    def _in_message(self, node: ast.AST) -> bool:
        cur = self.parent.get(id(node))
        while cur is not None:
            if isinstance(cur, ast.Raise):
                return True
            if (
                isinstance(cur, ast.Call)
                and isinstance(cur.func, ast.Attribute)
                and cur.func.attr in _LOG_NAMES
            ):
                return True
            if isinstance(cur, ast.stmt):
                return False
            cur = self.parent.get(id(cur))
        return False

    def run(self) -> list[mutate.Mutant]:  # type: ignore[override]
        for node in ast.walk(self.tree):
            if id(node) in self.skip:
                continue
            if isinstance(node, ast.Constant) and isinstance(node.value, str):
                if not self._in_message(node):
                    s, e = self.span(node)
                    new = repr((node.value + "_x") if node.value else "x")
                    self.emit(node, s, e, new.encode(), "fr_str", "str+_x")
            elif isinstance(node, ast.Expr) and isinstance(node.value, ast.Call):
                s, e = self.span(node)
                self.emit(node, s, e, b"pass", "fr_call_drop", "call->pass")
            elif isinstance(node, ast.Return) and node.value is not None:
                func = self.enclosing_function(node)
                if func is None or any(
                    isinstance(n, (ast.Yield, ast.YieldFrom)) for n in ast.walk(func)
                ):
                    continue
                if isinstance(node.value, ast.Constant) and node.value.value is None:
                    continue
                s, e = self.span(node.value)
                self.emit(node, s, e, b"None", "fr_ret_none", "return None")
            elif isinstance(node, ast.BoolOp) and len(node.values) == 2:
                s, e = self.span(node)
                for tag, operand in (("first", node.values[0]), ("last", node.values[1])):
                    os_, oe = self.span(operand)
                    self.emit(
                        node, s, e, b"(" + self.src[os_:oe] + b")", "fr_bool_drop", f"keep-{tag}"
                    )
            elif isinstance(node, ast.Compare) and len(node.ops) == 1 and type(node.ops[0]) in _REV:
                tok, new = _REV[type(node.ops[0])]
                gap_s = self.span(node.left)[1]
                gap_e = self.span(node.comparators[0])[0]
                pat = re.compile(rb"<=|>=|<|>")
                m = pat.search(self.src, gap_s, gap_e)
                if m is not None and m.group(0) == tok.encode():
                    self.emit(node, m.start(), m.end(), new.encode(), "fr_cmp_rev", f"{tok}->{new}")
        return self.out


def generate_fresh(source: bytes, module: str, path: str = "<mutant>") -> list[mutate.Mutant]:
    """Valid, de-duplicated fresh mutants of *source*, ordered by (line, col, operator, variant)."""
    raw = _FreshGen(source, module).run()
    raw.sort(key=lambda m: (m.line, m.col, m.operator, m.variant, m.replacement))
    seen: set[bytes] = set()
    out: list[mutate.Mutant] = []
    for m in raw:
        new = mutate.apply(source, m)
        if new == source or new in seen:
            continue
        try:
            compile(new, path, "exec")
        except (SyntaxError, ValueError):
            continue
        seen.add(new)
        out.append(m)
    return out
