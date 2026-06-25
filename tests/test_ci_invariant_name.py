"""CI 불변식 게이트가 참조하는 invariant 심볼이 실제로 존재하는지 고정.

배경: `.github/workflows/ci.yml` 의 'Invariant gate' 스텝이 한때
`assert_executor_absent` 를 임포트했으나, 그 심볼은 `assert_executor_confined`
로 재명명되어 사라졌다 → CI 가 ImportError 로 조용히 적색이었다. 이 테스트는
그 드리프트가 재발하지 않도록 invariants 공개 API 를 핀으로 박는다.
"""

from __future__ import annotations

from liot.core import invariants


def test_confined_and_gated_symbols_exist() -> None:
    # CI 게이트가 임포트하는 두 심볼은 반드시 공개돼 있어야 한다.
    assert "assert_executor_confined" in invariants.__all__
    assert "assert_execution_gated" in invariants.__all__
    assert callable(invariants.assert_executor_confined)
    assert callable(invariants.assert_execution_gated)


def test_legacy_executor_absent_symbol_is_gone() -> None:
    # 옛 이름은 더 이상 존재하지 않아야 한다(존재하면 CI 가 옛 게이트로 회귀).
    assert "assert_executor_absent" not in invariants.__all__
    assert not hasattr(invariants, "assert_executor_absent")


def test_invariant_gate_runs_clean() -> None:
    # CI 가 실제로 호출하는 그대로 — 인자 없이 통과해야 한다.
    invariants.assert_executor_confined()
    invariants.assert_execution_gated()
