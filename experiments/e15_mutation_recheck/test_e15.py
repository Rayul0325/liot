"""Self-tests of the E15 helpers: `pytest experiments/e15_mutation_recheck/test_e15.py`."""

from __future__ import annotations

import numpy as np

from experiments.e15_mutation_recheck import analyze, density, fresh_ops, identity
from experiments.e15_mutation_recheck.gemini import render_code


def _row(mid: str, line: int, col: int = 4, op: str = "cmp_eq", cl: str = "if a == b:") -> dict:
    return {"module": "m.py", "mutant_id": mid, "line": line, "col": col, "operator": op,
            "variant": "==->!=", "original": "==", "mutated": "!=", "code_line": cl}


def test_line_map_shifts_equal_blocks_only() -> None:
    old = b"a\nb\nc\nd\n"
    new = b"a\nX\nY\nb\nc\nd\n"
    lm = identity.line_map(old, new)
    assert lm == {1: 1, 2: 4, 3: 5, 4: 6}
    changed = identity.line_map(b"a\nb\nc\n", b"a\nB\nc\n")
    assert changed == {1: 1, 3: 3}  # the edited line has no counterpart


def test_match_module_identity_classes() -> None:
    old = [_row("o1", 2), _row("o2", 3, cl="if a == c:"), _row("o3", 9)]
    new = [_row("n1", 4), _row("n2", 3, cl="if a == CHANGED:"), _row("n4", 20)]
    lm = {2: 4, 9: 11}  # line 3 edited, 9 maps to a line with no new mutant
    recs = identity.match_module(old, new, lm)
    by_old = {r["old_id"]: r for r in recs if r["old_id"]}
    assert by_old["o1"]["status"] == "identical_context" and by_old["o1"]["new_id"] == "n1"
    assert by_old["o2"]["status"] == "old_only"
    assert by_old["o3"]["status"] == "old_only"
    assert sorted(r["new_id"] for r in recs if r["status"] == "new_only") == ["n2", "n4"]


def test_match_module_unchanged_file_matches_by_id_only() -> None:
    old = [_row("same", 5)]
    recs = identity.match_module(old, [_row("same", 5)], None)
    assert recs[0]["status"] == "identical_id"
    moved = identity.match_module(old, [_row("other", 5)], None)
    assert moved[0]["status"] == "old_only"


def test_density_counts_asserts_and_raises_per_test() -> None:
    src = (
        "import pytest\n"
        "def helper():\n    assert True\n"
        "def test_a():\n    assert 1\n    assert 2\n"
        "class TestC:\n"
        "    def test_b(self):\n        with pytest.raises(ValueError):\n            pass\n"
        "        assert 3\n"
    )
    c = density.function_counts(src, "tests/x.py")
    assert c == {
        "tests/x.py::test_a": {"asserts": 2, "raises": 0},
        "tests/x.py::TestC::test_b": {"asserts": 1, "raises": 1},
    }  # the helper's assert is not counted
    assert density.context_to_test("tests/x.py::TestC::test_b[p1-p2]|run") == "tests/x.py::TestC::test_b"
    assert density.context_to_test("") is None


def test_fresh_operators_never_reuse_e8_operator_names() -> None:
    from experiments.e8_mutation import mutate

    src = (
        b"def f(a, b, d):\n"
        b"    log.info('m.x')\n    d.update()\n"
        b"    if a < b and d:\n        return 'k'\n    raise ValueError('boom')\n"
    )
    ms = fresh_ops.generate_fresh(src, "toy")
    ops = {m.operator for m in ms}
    assert ops <= set(fresh_ops.FRESH_OPERATORS)
    assert not ops & set(mutate.OPERATORS + mutate.EXTENDED_OPERATORS)
    assert "fr_str" in ops and "fr_call_drop" in ops and "fr_cmp_rev" in ops
    # string inside raise / logging is not mutated
    assert not any(m.original in ("'boom'", "'m.x'") for m in ms)
    assert all(compile(mutate.apply(src, m), "x", "exec") for m in ms)


def test_render_code_rewrites_only_attribute_dots() -> None:
    assert render_code("self.x.y(2.5, v2.0)") == "self::x::y(2.5, v2::0)".replace("v2::0", "v2.0")


def test_verdict_rule_thresholds() -> None:
    assert analyze.verdict_rise(6.0, 2.0, 10.0) == "지지"
    assert analyze.verdict_rise(6.0, -1.0, 12.0) == "판정 불가"  # interval reaches 0
    assert analyze.verdict_rise(1.0, -1.0, 3.0) == "반증"
    assert analyze.verdict_rise(1.0, -1.0, 7.0) == "판정 불가"
    assert analyze.verdict_rise(3.5, 1.0, 6.0) == "판정 불가"


def test_auc_and_kappa() -> None:
    y = np.array([1, 1, 0, 0])
    assert analyze.auc(np.array([3.0, 2.0, 1.0, 0.0]), y) == 1.0
    assert analyze.auc(np.array([1.0, 1.0, 1.0, 1.0]), y) == 0.5
    k = analyze.kappa(["a", "b", "a", "b"], ["a", "b", "a", "b"], ("a", "b"))
    assert k["agreement"] == 1.0 and k["kappa"] == 1.0
    k2 = analyze.kappa(["a", "a", "b", "b"], ["a", "b", "a", "b"], ("a", "b"))
    assert k2["agreement"] == 0.5 and abs(k2["kappa"]) < 1e-9


def test_boot_delta_paired_counts() -> None:
    old = np.array([0, 0, 1, 1])
    new = np.array([1, 0, 1, 0])
    r = analyze.boot_delta(old, new, [("m", 1), ("m", 2), ("m", 3), ("m", 4)])
    assert r["gained"] == 1 and r["lost"] == 1 and r["delta_pp"] == 0.0
