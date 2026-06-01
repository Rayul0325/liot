"""런타임 불변식 예외와 검증기.

여기 정의한 예외들은 파이프라인 전역에서 6대 불변식을 강제하는 데 쓰인다.

  - TopologyViolation : 허용되지 않은 레이어 간 publish (event_bus).
  - IsolationError    : home_id 누락/공백 등 테넌시 격리 위반 (scoped_repo).
  - EgressViolation   : 외부 송신 시 allowlist/PII 위반 (egress_gate).
  - MonotonicityError : 위험도 강등 시도 (risk 단조 가드).

핵심 검증기:
  - assert_monotonic(old, new) : new < old 면 MonotonicityError.
  - assert_executor_absent()   : l4_execution 패키지에 'Executor' 구체 구현이
        있으면 InvariantError. (Phase1 = 제안-만, 액추에이터 실행 코드 부재)
"""

from __future__ import annotations

import importlib
import inspect
import pkgutil
from abc import ABC
from typing import Protocol, runtime_checkable

from .risk import RiskTier

__all__ = [
    "InvariantError",
    "TopologyViolation",
    "IsolationError",
    "EgressViolation",
    "MonotonicityError",
    "assert_monotonic",
    "assert_executor_absent",
]


class InvariantError(Exception):
    """6대 불변식 위반의 기반 예외."""


class TopologyViolation(InvariantError):
    """허용되지 않은 레이어 간 메시지 전달."""


class IsolationError(InvariantError):
    """테넌시(home_id) 격리 위반."""


class EgressViolation(InvariantError):
    """외부 송신 정책(allowlist/PII/whitelist-keys) 위반."""


class MonotonicityError(InvariantError):
    """위험도 강등 시도(단조성 위반)."""


def assert_monotonic(old: RiskTier, new: RiskTier) -> RiskTier:
    """위험도는 강등될 수 없다. new < old 이면 MonotonicityError.

    같거나 승격된 경우 new 를 그대로 돌려준다.
    """
    old_t = RiskTier.from_str(old)
    new_t = RiskTier.from_str(new)
    if new_t < old_t:
        raise MonotonicityError(
            f"risk tier downgrade is forbidden: {old_t.name} -> {new_t.name}"
        )
    return new_t


def _is_abstract_executor(obj: object) -> bool:
    """ABC/Protocol 기반의 추상 Executor 인터페이스인지 판정한다.

    추상(인터페이스)은 허용되고, 구체 구현만 금지된다.
    """
    if not inspect.isclass(obj):
        return False
    # Protocol 정의(typing.Protocol 을 직접 베이스로 갖는 경우)는 추상으로 본다.
    if getattr(obj, "_is_protocol", False):
        return True
    # ABC 를 상속하면서 추상 메서드가 남아 있으면 추상으로 본다.
    if issubclass(obj, ABC) and bool(getattr(obj, "__abstractmethods__", frozenset())):
        return True
    return False


def assert_executor_absent(package_name: str = "liot.layers.l4_execution") -> None:
    """l4_execution 패키지에 'Executor' 구체 구현이 있으면 InvariantError.

    importlib/pkgutil 로 패키지의 모든 서브모듈을 임포트해 클래스 이름에
    'Executor' 가 포함된 정의를 스캔한다. ABC/Protocol 기반의 추상
    인터페이스는 허용한다(구체 구현만 금지).

    패키지가 아직 존재하지 않으면(스켈레톤 단계) 위반 없음으로 간주한다.
    """
    try:
        package = importlib.import_module(package_name)
    except ModuleNotFoundError:
        # l4_execution 자체가 아직 없으면 당연히 Executor 도 없다.
        return

    module_names: list[str] = [package_name]
    pkg_path = getattr(package, "__path__", None)
    if pkg_path is not None:
        for mod in pkgutil.walk_packages(pkg_path, prefix=package_name + "."):
            module_names.append(mod.name)

    offenders: list[str] = []
    for mod_name in module_names:
        try:
            module = importlib.import_module(mod_name)
        except ModuleNotFoundError:
            continue
        for attr_name, obj in vars(module).items():
            if not inspect.isclass(obj):
                continue
            # 이 모듈에서 실제로 정의된 클래스만 본다(임포트된 것은 제외).
            if obj.__module__ != mod_name:
                continue
            if "Executor" not in obj.__name__:
                continue
            if _is_abstract_executor(obj):
                continue
            offenders.append(f"{mod_name}.{obj.__name__}")

    if offenders:
        raise InvariantError(
            "Phase1 forbids concrete Executor implementations in "
            f"{package_name}: {', '.join(sorted(offenders))}"
        )


# 외부에서 추상 Executor 인터페이스를 정의하고 싶을 때 베이스로 쓸 수 있는
# Protocol. assert_executor_absent 가 이 형태(추상)는 허용한다.
@runtime_checkable
class ExecutorProtocol(Protocol):  # pragma: no cover - 인터페이스 선언
    """액추에이터 실행 인터페이스(Phase1 에서는 구현 금지)."""

    async def execute(self, *args: object, **kwargs: object) -> object: ...
