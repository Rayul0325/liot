"""Self-test of the mutant generator: `pytest experiments/e8_mutation/test_mutate.py`."""

from __future__ import annotations

from experiments.e8_mutation import mutate

SRC = '''"""docstring with 한글 and a < b"""
from typing import Optional


def f(a: int, b: int, flag: bool) -> bool:
    """doc 1 + 2"""
    x = max(a, b)  # 한글 주석
    if a < b and flag:
        return True
    if not flag:
        raise ValueError("no")
    for k in [3, 4]:
        if k == 3:
            continue
    return a >= 5


def g(v: int) -> Optional[int]:
    return v + 1


def h(v: int) -> str:
    return f"{v} -> {v * 2}"
'''


def _mutants() -> dict[tuple[str, str], list[str]]:
    out: dict[tuple[str, str], list[str]] = {}
    src = SRC.encode()
    for m in mutate.generate(src, "toy"):
        out.setdefault((m.operator, m.variant), []).append(mutate.apply(src, m).decode())
    return out


def test_expected_operators_present() -> None:
    got = _mutants()
    assert ("cmp_boundary", "<->=") not in got
    assert ("cmp_boundary", "<-><=") in got
    assert ("cmp_boundary", ">=->>") in got
    assert ("cmp_eq", "==->!=") in got
    assert ("bool_swap", "and->or") in got
    assert ("not_removal", "drop-not") in got
    assert ("max_min", "max->min") in got
    assert ("guard_removal", "body(return)->pass") in got
    assert ("guard_removal", "body(raise)->pass") in got
    assert ("guard_removal", "body(continue)->pass") in got
    assert ("return_const", "return False") in got  # bool function, current value is not False
    assert ("return_const", "return None") in got  # Optional[int]


def test_every_mutant_differs_in_one_span_and_compiles() -> None:
    src = SRC.encode()
    for m in mutate.generate(src, "toy"):
        new = mutate.apply(src, m)
        assert new != src
        compile(new, "toy", "exec")
        assert new[: m.start] == src[: m.start]
        assert new[len(new) - (len(src) - m.end) :] == src[m.end :]


def test_skips_docstrings_annotations_fstrings() -> None:
    src = SRC.encode()
    for m in mutate.generate(src, "toy"):
        assert m.line > 5 or m.operator != "cmp_boundary"  # docstring `a < b` untouched
        assert "->" not in m.original  # annotations never mutated
    f_lines = [ln for ln, text in enumerate(SRC.splitlines(), 1) if 'f"' in text]
    assert all(m.line not in f_lines for m in mutate.generate(src, "toy"))


def test_korean_comment_offsets() -> None:
    got = _mutants()
    for text in got[("max_min", "max->min")]:
        assert "x = min(a, b)  # 한글 주석" in text


def test_no_return_const_when_value_already_that_constant() -> None:
    got = _mutants()
    for text in got[("return_const", "return True")]:
        assert text.count("return True") >= 1
    # `return True` inside f() (already True) must not be re-mutated to True
    src = SRC.encode()
    for m in mutate.generate(src, "toy"):
        if m.operator == "return_const" and m.variant == "return True":
            assert m.original != "True"
