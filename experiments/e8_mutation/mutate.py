"""AST-based mutant generator for E8 (no third-party dependency).

`generate(source: bytes, module: str) -> list[Mutant]` enumerates single-point mutants of one
source file. A mutant is a byte-range replacement on the *original source bytes* (not an
``ast.unparse`` round trip), so everything outside the mutated span — comments, Korean
docstrings, formatting — is byte-identical to git HEAD and the diff is one small span.

Operators (names are the `operator` field of a raw record):

* ``cmp_boundary``  ``<`` ``<=`` ``>`` ``>=`` swapped with their boundary twin
* ``cmp_eq``        ``==`` <-> ``!=``
* ``bool_swap``     ``and`` <-> ``or`` (all operators of one BoolOp at once)
* ``not_removal``   ``not x`` -> ``(x)``
* ``num_const``     int/float literal -> ``+1`` / ``-1`` / ``x2`` (never 0*2, never bool)
* ``return_const``  ``return <expr>`` -> ``True`` / ``False`` / ``None`` only where the
                    function's return annotation makes that type-safe (``bool``, ``X | None``)
* ``guard_removal`` body of an ``if`` that ends in ``raise``/``return``/``continue``/``break``
                    -> ``pass``
* ``max_min``       ``max(...)`` <-> ``min(...)``

Extended (outside the brief; reported separately): ``cmp_identity`` (``in``/``not in``,
``is``/``is not``), ``cond_negate`` (``if c:`` / ``x if c else y`` -> ``not (c)``),
``arith_swap`` (``+`` <-> ``-``).

Skipped on purpose (cannot change behaviour or are not code paths): docstrings, annotations,
f-string internals, ``if TYPE_CHECKING:`` blocks. Mutants that do not compile or that produce
the same bytes as an earlier mutant are dropped.
"""

from __future__ import annotations

import ast
import re
from dataclasses import asdict, dataclass

OPERATORS = (
    "cmp_boundary",
    "cmp_eq",
    "bool_swap",
    "not_removal",
    "num_const",
    "return_const",
    "guard_removal",
    "max_min",
)
# Added beyond the brief's list to widen the mutation space; reported separately so the
# headline score uses only the specified operators.
EXTENDED_OPERATORS = ("cmp_identity", "cond_negate", "arith_swap")

_CMP_SWAP = {
    ast.Lt: ("<", "<=", "cmp_boundary"),
    ast.LtE: ("<=", "<", "cmp_boundary"),
    ast.Gt: (">", ">=", "cmp_boundary"),
    ast.GtE: (">=", ">", "cmp_boundary"),
    ast.Eq: ("==", "!=", "cmp_eq"),
    ast.NotEq: ("!=", "==", "cmp_eq"),
}
# match the operator token exactly (so `<` does not match the head of `<=`)
_CMP_RE = {
    "<": re.compile(rb"<(?!=)"),
    "<=": re.compile(rb"<="),
    ">": re.compile(rb">(?!=)"),
    ">=": re.compile(rb">="),
    "==": re.compile(rb"=="),
    "!=": re.compile(rb"!="),
}
_ID_SWAP = {
    ast.In: (re.compile(rb"\bin\b"), b"not in", "in->not in"),
    ast.NotIn: (re.compile(rb"\bnot\s+in\b"), b"in", "not in->in"),
    ast.Is: (re.compile(rb"\bis\b(?!\s+not\b)"), b"is not", "is->is not"),
    ast.IsNot: (re.compile(rb"\bis\s+not\b"), b"is", "is not->is"),
}
_BOOL_RE = {ast.And: re.compile(rb"\band\b"), ast.Or: re.compile(rb"\bor\b")}


@dataclass(frozen=True)
class Mutant:
    module: str
    line: int  # 1-based line of the mutated span start
    col: int
    operator: str
    variant: str
    original: str  # replaced text (shortened to 160 chars)
    mutated: str  # replacement text (shortened to 160 chars)
    code_line: str  # the full source line (stripped, 200 chars) — what a reader looks at
    cov_line: int  # line a coverage tool attributes the mutated statement to
    start: int  # byte range in the original source
    end: int
    replacement: bytes

    def to_record(self) -> dict[str, object]:
        d = asdict(self)
        d.pop("replacement")
        d.pop("start")
        d.pop("end")
        return d


def _short(text: str, limit: int = 160) -> str:
    text = " ".join(text.split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


class _Gen:
    def __init__(self, source: bytes, module: str) -> None:
        self.src = source
        self.module = module
        self.tree = ast.parse(source)
        self.lines = source.split(b"\n")
        self.starts = [0]
        for ln in self.lines[:-1]:
            self.starts.append(self.starts[-1] + len(ln) + 1)
        self.out: list[Mutant] = []
        self.parent: dict[int, ast.AST] = {}
        for node in ast.walk(self.tree):
            for child in ast.iter_child_nodes(node):
                self.parent[id(child)] = node
        self.skip: set[int] = set()
        self._collect_skips()

    # ── skip sets ────────────────────────────────────────────────────────────
    def _mark(self, node: ast.AST | None) -> None:
        if node is not None:
            for sub in ast.walk(node):
                self.skip.add(id(sub))

    def _collect_skips(self) -> None:
        for node in ast.walk(self.tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                self._mark(node.returns)
                args = node.args
                for a in [*args.posonlyargs, *args.args, *args.kwonlyargs, args.vararg, args.kwarg]:
                    if a is not None:
                        self._mark(a.annotation)
            elif isinstance(node, ast.AnnAssign):
                self._mark(node.annotation)
            elif isinstance(node, ast.JoinedStr):
                self._mark(node)
            elif isinstance(node, ast.If) and ast.unparse(node.test) in (
                "TYPE_CHECKING",
                "typing.TYPE_CHECKING",
            ):
                for stmt in node.body:
                    self._mark(stmt)
            if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
                body = node.body
                if (
                    body
                    and isinstance(body[0], ast.Expr)
                    and isinstance(body[0].value, ast.Constant)
                    and isinstance(body[0].value.value, str)
                ):
                    self._mark(body[0])

    # ── offsets ──────────────────────────────────────────────────────────────
    def off(self, line: int, col: int) -> int:
        return self.starts[line - 1] + col  # ast col offsets are UTF-8 byte offsets

    def span(self, node: ast.AST) -> tuple[int, int]:
        return (
            self.off(node.lineno, node.col_offset),  # type: ignore[attr-defined]
            self.off(node.end_lineno, node.end_col_offset),  # type: ignore[attr-defined]
        )

    def stmt_line(self, node: ast.AST) -> int:
        cur: ast.AST | None = node
        while cur is not None and not isinstance(cur, ast.stmt):
            cur = self.parent.get(id(cur))
        return cur.lineno if cur is not None else node.lineno  # type: ignore[attr-defined]

    def enclosing_function(self, node: ast.AST) -> ast.FunctionDef | ast.AsyncFunctionDef | None:
        cur = self.parent.get(id(node))
        while cur is not None:
            if isinstance(cur, (ast.FunctionDef, ast.AsyncFunctionDef)):
                return cur
            if isinstance(cur, ast.Lambda):
                return None
            cur = self.parent.get(id(cur))
        return None

    # ── emit ─────────────────────────────────────────────────────────────────
    def emit(
        self,
        node: ast.AST,
        start: int,
        end: int,
        replacement: bytes,
        operator: str,
        variant: str,
        cov_line: int | None = None,
    ) -> None:
        line = self.src.count(b"\n", 0, start) + 1
        col = start - self.starts[line - 1]
        raw_line = self.lines[line - 1].decode("utf-8", "replace").strip()
        self.out.append(
            Mutant(
                module=self.module,
                line=line,
                col=col,
                operator=operator,
                variant=variant,
                original=_short(self.src[start:end].decode("utf-8", "replace")),
                mutated=_short(replacement.decode("utf-8", "replace")),
                code_line=_short(raw_line, 200),
                cov_line=cov_line if cov_line is not None else self.stmt_line(node),
                start=start,
                end=end,
                replacement=replacement,
            )
        )

    # ── operators ────────────────────────────────────────────────────────────
    def run(self) -> list[Mutant]:
        for node in ast.walk(self.tree):
            if id(node) in self.skip:
                continue
            if isinstance(node, ast.Compare):
                self._compare(node)
            elif isinstance(node, ast.BoolOp):
                self._boolop(node)
            elif isinstance(node, ast.UnaryOp) and isinstance(node.op, ast.Not):
                s, e = self.span(node)
                os_, oe = self.span(node.operand)
                self.emit(node, s, e, b"(" + self.src[os_:oe] + b")", "not_removal", "drop-not")
            elif isinstance(node, ast.Constant):
                self._number(node)
            elif isinstance(node, ast.Return):
                self._return(node)
            elif isinstance(node, ast.If):
                self._guard(node)
                self._negate(node.test, node)
            elif isinstance(node, ast.IfExp):
                self._negate(node.test, node)
            elif isinstance(node, ast.BinOp) and isinstance(node.op, (ast.Add, ast.Sub)):
                self._arith(node)
            elif (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Name)
                and node.func.id in ("max", "min")
            ):
                s, e = self.span(node.func)
                other = "min" if node.func.id == "max" else "max"
                self.emit(node, s, e, other.encode(), "max_min", f"{node.func.id}->{other}")
        return self.out

    def _compare(self, node: ast.Compare) -> None:
        operands = [node.left, *node.comparators]
        for i, op in enumerate(node.ops):
            ident = _ID_SWAP.get(type(op))
            if ident is not None:
                pat, new_b, variant = ident
                g_s = self.span(operands[i])[1]
                g_e = self.span(operands[i + 1])[0]
                m = pat.search(self.src, g_s, g_e)
                if m is not None:
                    self.emit(node, m.start(), m.end(), new_b, "cmp_identity", variant)
                continue
            spec = _CMP_SWAP.get(type(op))
            if spec is None:
                continue
            tok, new, operator = spec
            gap_s = self.span(operands[i])[1]
            gap_e = self.span(operands[i + 1])[0]
            m = _CMP_RE[tok].search(self.src, gap_s, gap_e)
            if m is None:
                continue
            self.emit(node, m.start(), m.end(), new.encode(), operator, f"{tok}->{new}")

    def _boolop(self, node: ast.BoolOp) -> None:
        pat = _BOOL_RE[type(node.op)]
        new = b"or" if isinstance(node.op, ast.And) else b"and"
        edits: list[tuple[int, int]] = []
        for a, b in zip(node.values, node.values[1:], strict=False):
            m = pat.search(self.src, self.span(a)[1], self.span(b)[0])
            if m is None:
                return
            edits.append((m.start(), m.end()))
        # one mutant per BoolOp: splice every operator; encode as a single span replacement
        s, e = self.span(node)
        buf = bytearray(self.src[s:e])
        for ms, me in reversed(edits):
            buf[ms - s : me - s] = new
        old = "and" if isinstance(node.op, ast.And) else "or"
        self.emit(node, s, e, bytes(buf), "bool_swap", f"{old}->{new.decode()}")

    def _number(self, node: ast.Constant) -> None:
        v = node.value
        if isinstance(v, bool) or not isinstance(v, (int, float)):
            return
        if isinstance(v, float) and (v != v or v in (float("inf"), float("-inf"))):
            return
        s, e = self.span(node)
        seen: set[bytes] = set()
        for variant, val in (("+1", v + 1), ("-1", v - 1), ("x2", v * 2)):
            if val == v:
                continue
            text = repr(val)
            if val < 0:
                text = f"({text})"
            rep = text.encode()
            if rep in seen:
                continue
            seen.add(rep)
            self.emit(node, s, e, rep, "num_const", variant)

    def _return(self, node: ast.Return) -> None:
        if node.value is None:
            return
        func = self.enclosing_function(node)
        if func is None or func.returns is None:
            return
        if any(isinstance(n, (ast.Yield, ast.YieldFrom)) for n in ast.walk(func)):
            return
        ann = ast.unparse(func.returns)
        choices: list[str] = []
        if re.search(r"(^|\|\s*)bool(\s*\||$)", ann):
            choices += ["True", "False"]
        if re.search(r"(^|\|\s*)None(\s*\||$)", ann) and ann != "None" or ann.startswith("Optional["):
            choices.append("None")
        cur = ast.unparse(node.value)
        s, e = self.span(node.value)
        for c in choices:
            if cur == c:
                continue
            self.emit(node, s, e, c.encode(), "return_const", f"return {c}")

    def _negate(self, test: ast.expr, node: ast.AST) -> None:
        s, e = self.span(test)
        self.emit(
            node, s, e, b"not (" + self.src[s:e] + b")", "cond_negate", "test->not test",
            cov_line=self.stmt_line(node),
        )

    def _arith(self, node: ast.BinOp) -> None:
        tok, new = (b"+", b"-") if isinstance(node.op, ast.Add) else (b"-", b"+")
        m = re.compile(re.escape(tok)).search(
            self.src, self.span(node.left)[1], self.span(node.right)[0]
        )
        if m is not None:
            self.emit(node, m.start(), m.end(), new, "arith_swap", f"{tok.decode()}->{new.decode()}")

    def _guard(self, node: ast.If) -> None:
        if not node.body or not isinstance(node.body[-1], (ast.Raise, ast.Return, ast.Continue, ast.Break)):
            return
        if len(node.body) == 1 and isinstance(node.body[0], ast.Pass):
            return
        s = self.off(node.body[0].lineno, node.body[0].col_offset)
        e = self.off(node.body[-1].end_lineno, node.body[-1].end_col_offset)  # type: ignore[arg-type]
        kind = type(node.body[-1]).__name__.lower()
        self.emit(
            node, s, e, b"pass", "guard_removal", f"body({kind})->pass",
            cov_line=node.body[0].lineno,
        )


def apply(source: bytes, m: Mutant) -> bytes:
    """Source bytes with the mutant's span replaced."""
    return source[: m.start] + m.replacement + source[m.end :]


def generate(source: bytes, module: str, path: str = "<mutant>") -> list[Mutant]:
    """All valid, de-duplicated mutants of *source*, ordered by (line, col, operator, variant)."""
    raw = _Gen(source, module).run()
    raw.sort(key=lambda m: (m.line, m.col, m.operator, m.variant, m.replacement))
    seen: set[bytes] = set()
    out: list[Mutant] = []
    for m in raw:
        new = apply(source, m)
        if new == source or new in seen:
            continue
        try:
            compile(new, path, "exec")
        except (SyntaxError, ValueError):
            continue
        seen.add(new)
        out.append(m)
    return out
