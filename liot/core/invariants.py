"""런타임 불변식 예외와 검증기.

여기 정의한 예외들은 파이프라인 전역에서 8대 불변식을 강제하는 데 쓰인다.

  - TopologyViolation : 허용되지 않은 레이어 간 publish (event_bus).
  - IsolationError    : home_id 누락/공백 등 테넌시 격리 위반 (scoped_repo).
  - EgressViolation   : 외부 송신 시 allowlist/PII 위반 (egress_gate).
  - MonotonicityError : 위험도 강등 시도 (risk 단조 가드).

핵심 검증기:
  - assert_monotonic(old, new) : new < old 면 MonotonicityError.
  - assert_executor_confined() : 구체 Executor 는 전용 모듈
        (l4_execution.executor) 안에만 존재해야 한다. 다른 모듈에 흩어진
        실행기(로그 Executor)가 있으면 InvariantError.
  - assert_execution_gated()   : 불변식 8(Execution-Authorized-Only). 기본
        ExecutionPolicy 는 비활성(옵트인 OFF), 안전 임계는 어떤 티어에서도
        자동 실행되지 않으며, '여는 방향'(unlock/valve open) 동작은 어떤
        실행 화이트리스트에도 없어야 한다.

불변식 3 의 진화: 과거 "실행기 부재(제안-만)" → 현재 "실행은 옵트인·게이트".
조작은 (a) 활성화 + (b) 화이트리스트 + (c) 티어 권한(자동은 비안전 ≤BREATH 만,
그 외 신선한 사람 승인) 을 모두 만족할 때만 일어난다.
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
    "assert_executor_confined",
    "assert_execution_gated",
]


class InvariantError(Exception):
    """8대 불변식 위반의 기반 예외."""


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


def assert_executor_confined(
    package_name: str = "liot.layers.l4_execution",
    *,
    sanctioned_module: str = "liot.layers.l4_execution.executor",
) -> None:
    """구체 Executor 는 *전용 모듈* 안에만 존재해야 한다(아니면 InvariantError).

    조작 능력은 옵트인으로 추가됐지만(불변식 8), 실행기가 여기저기 흩어지면
    감사·게이트가 무력화된다. 그래서 구체 Executor(클래스명에 'Executor' 포함,
    추상/Protocol 제외)는 ``sanctioned_module`` 한 곳에만 두도록 강제한다.
    그 밖의 모듈에서 발견되면 위반이다.

    importlib/pkgutil 로 패키지의 모든 서브모듈을 임포트해 스캔한다. 패키지가
    아직 없으면(스켈레톤) 위반 없음.
    """
    try:
        package = importlib.import_module(package_name)
    except ModuleNotFoundError:
        return

    module_names: list[str] = [package_name]
    pkg_path = getattr(package, "__path__", None)
    if pkg_path is not None:
        for mod in pkgutil.walk_packages(pkg_path, prefix=package_name + "."):
            module_names.append(mod.name)

    offenders: list[str] = []
    for mod_name in module_names:
        if mod_name == sanctioned_module:
            continue  # 전용 모듈의 실행기는 허용
        try:
            module = importlib.import_module(mod_name)
        except ModuleNotFoundError:
            continue
        for attr_name, obj in vars(module).items():
            if not inspect.isclass(obj):
                continue
            if obj.__module__ != mod_name:  # 임포트된 것 제외, 정의된 것만
                continue
            if "Executor" not in obj.__name__:
                continue
            if _is_abstract_executor(obj):
                continue
            offenders.append(f"{mod_name}.{obj.__name__}")

    if offenders:
        raise InvariantError(
            "concrete Executor 는 전용 모듈("
            f"{sanctioned_module}) 밖에 두면 안 된다: {', '.join(sorted(offenders))}"
        )


def assert_execution_gated() -> None:
    """불변식 8(Execution-Authorized-Only)의 정적 속성을 검증한다.

      1) 기본 ExecutionPolicy 는 비활성(옵트인 OFF) — 켜기 전엔 한 줄도 실행 안 함.
      2) 안전 임계 카테고리는 어떤 티어에서도 AUTO 가 아니다(자동 조작 절대 금지).
      3) '여는 방향'(lock.unlock, valve open)은 어떤 실행 화이트리스트에도 없다.
      4) 개입 정책(조건 A/B)은 어떤 티어에서도 기준선보다 약한 확인을 못 낸다
         (개입 하한) — 실험 조건이 안전 확인 강도를 무르게 만들 수 없다.
    """
    from .contracts import CandidateAction
    from .risk import SAFETY_CRITICAL_CATEGORIES

    try:
        from ..layers.l4_execution.executor import (  # 지연 임포트(순환 회피)
            COMFORT_EXECUTABLE,
            SAFETY_EXECUTABLE,
            ExecDecision,
            ExecutionPolicy,
        )
    except ModuleNotFoundError:
        return  # 실행기 모듈이 아직 없으면 검증할 것 없음

    # (1) 기본 비활성
    if ExecutionPolicy().enabled:
        raise InvariantError("기본 ExecutionPolicy 가 활성화돼 있음 — 옵트인 OFF 위반")

    # (2) 안전 카테고리는 자동 실행 금지(활성 정책으로도 AUTO 가 나오면 위반)
    pol = ExecutionPolicy(enabled=True)
    for cat in sorted(SAFETY_CRITICAL_CATEGORIES):
        for cap, act in sorted(SAFETY_EXECUTABLE):
            probe = CandidateAction(
                capability=cap, action=act, category=cat,
                entity_id=f"{cap}.probe", params={},
            )
            for tier in RiskTier:
                if pol.decide(probe, tier) == ExecDecision.AUTO:
                    raise InvariantError(
                        f"안전 카테고리 {cat!r} {cap}.{act} 가 {tier.name} 에서 AUTO 로 "
                        "결정됨 — 안전 자동 실행 금지 위반"
                    )

    # (3) 여는 방향은 어떤 화이트리스트에도 없어야 한다
    forbidden_open = {("lock", "unlock"), ("valve", "open"), ("cover", "open")} & (
        SAFETY_EXECUTABLE
    )
    if forbidden_open:
        raise InvariantError(
            f"여는 방향 동작이 안전 실행 화이트리스트에 있음: {sorted(forbidden_open)}"
        )
    if ("lock", "unlock") in COMFORT_EXECUTABLE or ("valve", "open") in COMFORT_EXECUTABLE:
        raise InvariantError("lock.unlock/valve.open 은 컴포트 화이트리스트에 있으면 안 됨")

    # (4) 개입 하한: 어떤 조건(A/B)도 티어 기준선 아래로 확인을 못 낮춘다
    from ..layers.l4_execution.intervention import (  # 지연 임포트(순환 회피)
        CONFIRMATION_ORDER,
        TieredIntervention,
        UniformIntervention,
    )

    for intervention in (TieredIntervention(), UniformIntervention()):
        for tier in RiskTier:
            conf = intervention.confirmation_for(tier)
            if CONFIRMATION_ORDER[conf] < CONFIRMATION_ORDER[tier.required_confirmation]:
                raise InvariantError(
                    f"개입 정책(조건 {intervention.condition})이 {tier.name} 기준선"
                    f"({tier.required_confirmation})보다 약한 확인({conf})을 냄 — 개입 하한 위반"
                )


# 외부에서 추상 Executor 인터페이스를 정의하고 싶을 때 베이스로 쓸 수 있는
# Protocol. assert_executor_confined 가 이 형태(추상)는 허용한다.
@runtime_checkable
class ExecutorProtocol(Protocol):  # pragma: no cover - 인터페이스 선언
    """액추에이터 실행 인터페이스(Phase1 에서는 구현 금지)."""

    async def execute(self, *args: object, **kwargs: object) -> object: ...
