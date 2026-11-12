"""Assertion counts per test function and per module (E15, hypothesis C4.11(b) / H6.3b).

Definitions are fixed before any result is looked at.

**Test** = a ``def test_*`` function at module level or inside a class, found by AST. Its key is
``<relative path>::[Class::]name`` (the same shape pytest/coverage use for a test context, with the
``[param]`` suffix and the ``|run``/``|setup`` phase suffix stripped). A parametrised function is one
test (static count; parametrisation is not expanded).

**Assertions of a test** = ``assert`` statements anywhere inside the test function (nested closures
included) + each ``pytest.raises(...)`` / ``pytest.warns(...)`` with-item. Asserts that live in helper
functions outside any ``test_*`` function are NOT counted (stated limitation).

**Module density** (descriptive only, module-level view the metric review rejected as the verdict
basis) = pooled assertions / pooled tests over the test files E8 selected for the module
(``experiments.e8_mutation.run.select_tests``: files that import or name the module, plus
``tests/test_invariants.py``).

**Line assertion load** (verdict basis, mutant level) = sum over the *distinct test functions that
execute the mutated line* (coverage dynamic contexts) of the assertions of each function.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path
from typing import Any

_RAISES = {("pytest", "raises"), ("pytest", "warns")}
_CTX_SUFFIX = re.compile(r"(\[.*\])?(\|(run|setup|teardown))?$")


def _is_raises(item: ast.withitem) -> bool:
    call = item.context_expr
    if not isinstance(call, ast.Call):
        return False
    f = call.func
    return (
        isinstance(f, ast.Attribute)
        and isinstance(f.value, ast.Name)
        and (f.value.id, f.attr) in _RAISES
    )


def _count_in(node: ast.AST) -> tuple[int, int]:
    asserts = raises = 0
    for sub in ast.walk(node):
        if isinstance(sub, ast.Assert):
            asserts += 1
        elif isinstance(sub, (ast.With, ast.AsyncWith)):
            raises += sum(1 for it in sub.items if _is_raises(it))
    return asserts, raises


def function_counts(source: str, rel: str) -> dict[str, dict[str, int]]:
    """{test key: {"asserts": n, "raises": n}} for every ``test_*`` function in *source*."""
    tree = ast.parse(source)
    out: dict[str, dict[str, int]] = {}

    def visit(body: list[ast.stmt], prefix: str) -> None:
        for node in body:
            if isinstance(node, ast.ClassDef):
                visit(node.body, f"{prefix}{node.name}::")
            elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name.startswith(
                "test_"
            ):
                a, r = _count_in(node)
                out[f"{rel}::{prefix}{node.name}"] = {"asserts": a, "raises": r}

    visit(tree.body, "")
    return out


def context_to_test(ctx: str) -> str | None:
    """Coverage context ``tests/x.py::Cls::test_a[p]|run`` -> ``tests/x.py::Cls::test_a``; '' -> None."""
    if not ctx:
        return None
    return _CTX_SUFFIX.sub("", ctx)


def module_density(files: list[Path], rel_of: dict[Path, str] | None = None) -> dict[str, Any]:
    """Pooled counts over *files* (descriptive module-level density)."""
    tot = {"tests": 0, "asserts": 0, "raises": 0}
    for p in files:
        rel = (rel_of or {}).get(p, f"tests/{p.name}")
        for c in function_counts(p.read_text(encoding="utf-8"), rel).values():
            tot["tests"] += 1
            tot["asserts"] += c["asserts"]
            tot["raises"] += c["raises"]
    n = tot["tests"]
    return {
        "n_files": len(files),
        **tot,
        "density": (tot["asserts"] + tot["raises"]) / n if n else None,
        "density_assert_only": tot["asserts"] / n if n else None,
    }
