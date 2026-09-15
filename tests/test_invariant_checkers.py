"""불변식 **검사기 자체**가 작동하는지 — 초록이 '위반 없음'인지 '검사 안 함'인지.

2026-09-16 야간 뮤테이션 스윕에서 `assert_executor_confined`·`assert_execution_gated`
내부 가드를 뒤집어도 전체 스위트가 초록이었다. 기존 테스트는 '현재 코드에서 통과한다'만
보고 있어서, 검사기가 조용히 아무것도 안 보게 되는 변경을 잡지 못했다.
여기서는 **일부러 위반을 심어** 검사기가 실제로 잡는지 본다.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from liot.core.invariants import (
    InvariantError,
    assert_execution_gated,
    assert_executor_confined,
)


def _make_pkg(tmp_path: Path, name: str, body: str) -> None:
    """서브패키지 안에 클래스를 심은 임시 패키지를 만든다(스캔이 재귀하는지 확인용)."""
    pkg = tmp_path / name / "deep"
    pkg.mkdir(parents=True)
    (tmp_path / name / "__init__.py").write_text("", encoding="utf-8")
    (pkg / "__init__.py").write_text("", encoding="utf-8")
    (pkg / "mod.py").write_text(body, encoding="utf-8")


CONCRETE = """
class PlantedExecutor:
    async def execute(self, *args, **kwargs):
        return None
"""

ABSTRACT = """
from abc import ABC, abstractmethod


class PlantedExecutor(ABC):
    @abstractmethod
    async def execute(self, *args, **kwargs): ...
"""

IMPORTED_ELSEWHERE = """
from liot.layers.l4_execution.executor import HAExecutor  # 임포트일 뿐 정의가 아니다

__all__ = ["HAExecutor"]
"""


def test_scan_catches_concrete_executor_planted_in_a_subpackage(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """서브패키지에 심은 구체 실행기를 잡는다 — 스캔이 정말 재귀한다."""
    _make_pkg(tmp_path, "probe_concrete", CONCRETE)
    monkeypatch.syspath_prepend(str(tmp_path))
    with pytest.raises(InvariantError, match="PlantedExecutor"):
        assert_executor_confined("probe_concrete",
                                 sanctioned_module="probe_concrete.sanctioned")


def test_scan_allows_abstract_executor_interface(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """추상 인터페이스는 허용 — 구체 구현만 금지다."""
    _make_pkg(tmp_path, "probe_abstract", ABSTRACT)
    monkeypatch.syspath_prepend(str(tmp_path))
    assert_executor_confined("probe_abstract",
                             sanctioned_module="probe_abstract.sanctioned")


def test_scan_ignores_classes_merely_imported(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """임포트해 온 실행기는 그 모듈이 '정의'한 게 아니라 위반이 아니다."""
    _make_pkg(tmp_path, "probe_imported", IMPORTED_ELSEWHERE)
    monkeypatch.syspath_prepend(str(tmp_path))
    assert_executor_confined("probe_imported",
                             sanctioned_module="probe_imported.sanctioned")


def test_execution_gate_catches_a_policy_that_would_auto_run_safety(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """안전 카테고리가 AUTO 로 결정되는 정책이면 잡아야 한다(불변식 8의 핵심).

    검사기가 '활성 정책'으로 조회하지 않게 되면(예: enabled=False) 이 검사는
    언제나 통과한다 — 그 무력화를 여기서 잡는다.
    """
    from liot.layers.l4_execution import executor as ex

    class _StubPolicy:
        def __init__(self, *, enabled: bool = False, extra_executable: object = None):
            self.enabled = enabled

        def decide(self, action: object, tier: object) -> object:
            # 활성일 때만 AUTO — 검사기가 비활성으로 물으면 위반이 안 보인다.
            return ex.ExecDecision.AUTO if self.enabled else ex.ExecDecision.NOTIFY_ONLY

    monkeypatch.setattr(ex, "ExecutionPolicy", _StubPolicy)
    # 메시지까지 특정한다: 'AUTO' 만 보면 뒤쪽 검사(확장 주입)의 위반과 구분되지 않아
    # 이 검사가 무력화돼도 테스트가 초록으로 남는다(실제로 그렇게 새어 나갔다).
    with pytest.raises(InvariantError, match="안전 자동 실행 금지 위반"):
        assert_execution_gated()


def test_execution_gate_catches_one_forbidden_comfort_pair(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """'여는 방향' 한 쌍만 컴포트 화이트리스트에 있어도 위반이다(둘 다일 필요 없다)."""
    from liot.layers.l4_execution import executor as ex

    monkeypatch.setattr(ex, "COMFORT_EXECUTABLE", frozenset({("lock", "unlock")}))
    with pytest.raises(InvariantError, match="컴포트"):
        assert_execution_gated()

    monkeypatch.setattr(ex, "COMFORT_EXECUTABLE", frozenset({("valve", "open")}))
    with pytest.raises(InvariantError, match="컴포트"):
        assert_execution_gated()


def test_register_safety_category_ignores_empty_input() -> None:
    """빈 카테고리는 확장 집합에 들어가지 않는다(빈 문자열이 '안전'이 되면 안 된다)."""
    from liot.core.risk import (
        _reset_safety_extensions_for_tests,
        is_safety_category,
        register_safety_category,
    )

    try:
        assert register_safety_category("   ") == ""
        assert is_safety_category("") is False
        assert is_safety_category("   ") is False
    finally:
        _reset_safety_extensions_for_tests()


def test_execution_gate_catches_auto_under_injected_extras(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """확장 허용목록을 주입한 상태에서도 AUTO 가 나오면 잡아야 한다(방어심층 5a).

    이 검사는 '활성 + 확장 주입' 정책으로 물어야 의미가 있다. 비활성으로 물으면
    (스윕 뮤턴트: enabled=False) 언제나 통과해 검사가 사라진다.
    """
    from liot.layers.l4_execution import executor as ex

    class _StubPolicy:
        def __init__(
            self,
            *,
            enabled: bool = False,
            extra_executable: frozenset[tuple[str, str]] | None = None,
        ):
            self.enabled = enabled
            self.extra = frozenset(extra_executable or frozenset())

        def decide(self, action: object, tier: object) -> object:
            cap = getattr(action, "capability", "")
            act = getattr(action, "action", "")
            # 주입된 확장분에 대해서만, 그리고 활성일 때만 AUTO 를 낸다.
            if self.enabled and (cap, act) in self.extra:
                return ex.ExecDecision.AUTO
            return ex.ExecDecision.NOTIFY_ONLY

    monkeypatch.setattr(ex, "ExecutionPolicy", _StubPolicy)
    with pytest.raises(InvariantError, match="확장 주입 상태"):
        assert_execution_gated()
