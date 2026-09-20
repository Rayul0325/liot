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


# ── 검사기의 '기본값'과 '범위' 자체를 고정 (2026-09-20 검증 무결성 감사) ──
def test_default_scan_target_exists_and_covers_the_sanctioned_module() -> None:
    """기본 스캔 대상이 실제로 존재하고, 전용 모듈을 품고 있어야 한다.

    감사 [2]: 기본 `package_name` 에 오타 한 글자만 나도 임포트 실패 → 조용히 통과라
    전체 스위트도 CI 게이트도 "OK" 를 찍었다(재현함). 기본값을 이름으로 고정한다.
    """
    import importlib
    import inspect

    sig = inspect.signature(assert_executor_confined)
    package_name = sig.parameters["package_name"].default
    sanctioned = sig.parameters["sanctioned_module"].default
    assert package_name == "liot", package_name
    assert sanctioned == "liot.layers.l4_execution.executor", sanctioned
    assert sanctioned.startswith(package_name + "."), "전용 모듈이 스캔 범위 밖이다"
    importlib.import_module(package_name)  # 없으면 ModuleNotFoundError 로 즉시 빨강
    importlib.import_module(sanctioned)


def test_missing_liot_package_is_an_error_not_a_silent_pass() -> None:
    """우리 패키지를 못 찾으면 통과가 아니라 InvariantError (fail-closed).

    '0개 모듈을 스캔하고 통과' 가 이 검사기의 가장 위험한 실패 방식이다.
    """
    with pytest.raises(InvariantError, match="무력화"):
        assert_executor_confined("liot.layers.l4_execution_absent")
    with pytest.raises(InvariantError, match="무력화"):
        assert_executor_confined("liot_typo")
    # 우리 패키지가 아닌 이름(테스트용 임시 패키지)은 예전처럼 조용히 통과한다.
    assert_executor_confined("some_external_package_that_does_not_exist")


def test_scan_reaches_modules_outside_the_execution_layer(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """스캔 범위가 실행 계층 밖까지 닿는다 — docstring 이 말하는 '흩어진 실행기'.

    감사 [2] 부수 지적: 예전 기본값(`liot.layers.l4_execution`)으로는 service.py·
    demo_server.py 에 구체 실행기를 둬도 못 잡았다. 임시 패키지의 **실행 계층이 아닌**
    하위 모듈에 심어서 잡히는지 본다.
    """
    pkg = tmp_path / "probe_scope" / "service_like"
    pkg.mkdir(parents=True)
    (tmp_path / "probe_scope" / "__init__.py").write_text("", encoding="utf-8")
    (pkg / "__init__.py").write_text("", encoding="utf-8")
    (pkg / "daemon.py").write_text(CONCRETE, encoding="utf-8")
    monkeypatch.syspath_prepend(str(tmp_path))
    with pytest.raises(InvariantError, match="PlantedExecutor"):
        assert_executor_confined("probe_scope",
                                 sanctioned_module="probe_scope.layers.executor")
