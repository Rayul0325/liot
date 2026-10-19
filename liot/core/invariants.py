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
        실행 화이트리스트에도 없어야 한다. 원탭 예외(ONE_TAP_SAFE_ACTIONS)는
        실행 허용의 부분집합이고 그 밖으로 새지 않아야 한다.

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
    return issubclass(obj, ABC) and bool(getattr(obj, "__abstractmethods__", frozenset()))


def assert_executor_confined(
    package_name: str = "liot",
    *,
    sanctioned_module: str = "liot.layers.l4_execution.executor",
) -> None:
    """구체 Executor 는 *전용 모듈* 안에만 존재해야 한다(아니면 InvariantError).

    조작 능력은 옵트인으로 추가됐지만(불변식 8), 실행기가 여기저기 흩어지면
    감사·게이트가 무력화된다. 그래서 구체 Executor(클래스명에 'Executor' 포함,
    추상/Protocol 제외)는 ``sanctioned_module`` 한 곳에만 두도록 강제한다.
    그 밖의 모듈에서 발견되면 위반이다.

    importlib/pkgutil 로 패키지의 모든 서브모듈을 임포트해 스캔한다.

    스캔 범위는 **`liot` 전체**다. 예전 기본값은 `liot.layers.l4_execution` 이어서
    docstring 이 말하는 '다른 모듈에 흩어진 실행기'(service.py·demo_server.py 등)를
    정작 못 봤다(2026-09-20 감사).

    패키지를 못 찾으면: 이름이 `liot` 으로 시작하면 **InvariantError** 다. 검사기가
    조용히 0개 모듈을 스캔하고 통과하는 게 가장 위험한 실패 방식이기 때문이다 —
    기본값에 오타 한 글자만 나도 CI 가 "OK" 를 찍었다(같은 감사에서 재현). 우리
    패키지가 아닌 이름(테스트용 임시 패키지 등)은 예전처럼 조용히 통과한다.
    """
    try:
        package = importlib.import_module(package_name)
    except ModuleNotFoundError:
        if package_name.startswith("liot"):  # 오타 한 글자도 여기 걸린다
            raise InvariantError(
                f"실행기 스캔 대상 패키지를 임포트할 수 없습니다: {package_name!r} — "
                "검사기가 아무것도 보지 않는 상태(무력화)."
            ) from None
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
        for obj in vars(module).values():
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
      5) 카탈로그 파생 확장분(extra_executable)을 주입해도 FORBIDDEN_OPEN 은 어떤
         실효 실행집합에도 없고, 안전 카테고리는 확장 주입 상태에서도 AUTO 가 아니다.
      6) 원탭 예외(T4) — 개입 하한의 **유일한** 명시 예외가 상수 밖으로 새지 않는다:
         a) ONE_TAP_SAFE_ACTIONS 는 frozenset 이고 SAFETY_EXECUTABLE 의 부분집합.
         b) 원탭 밖의 안전 행동(확장 주입·여는 방향 포함)은 단일 확인(CONFIRM)도
            AUTO 도 아니다.
         c) 라우터: 조건 A/B 모두 원탭 쌍은 EMBER + light, 상황 알림(alert)은
            EMBER + double — 예외가 다른 안전 경로를 조용하게 만들지 않는다.
    """
    from .contracts import CandidateAction, Judgment
    from .risk import safety_categories

    try:
        from ..layers.l4_execution.executor import (  # 지연 임포트(순환 회피)
            COMFORT_EXECUTABLE,
            FORBIDDEN_OPEN,
            ONE_TAP_SAFE_ACTIONS,
            SAFETY_EXECUTABLE,
            ExecDecision,
            ExecutionPolicy,
            effective_comfort,
        )
    except ModuleNotFoundError:
        return  # 실행기 모듈이 아직 없으면 검증할 것 없음

    # (1) 기본 비활성
    if ExecutionPolicy().enabled:
        raise InvariantError("기본 ExecutionPolicy 가 활성화돼 있음 — 옵트인 OFF 위반")

    # (2) 안전 카테고리는 자동 실행 금지(활성 정책으로도 AUTO 가 나오면 위반)
    pol = ExecutionPolicy(enabled=True)
    for cat in sorted(safety_categories()):
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

    # (5) 확장 허용목록을 주입해도 금지 open 은 어떤 실효 실행집합에도 없고,
    #     안전 카테고리는 확장 주입 상태에서도 AUTO 가 아니다(데모 파생분 방어심층).
    probe_extra = FORBIDDEN_OPEN | {("light", "turn_on"), ("valve", "close"), ("lock", "lock")}
    eff = effective_comfort(probe_extra)
    leaked = eff & FORBIDDEN_OPEN
    if leaked:
        raise InvariantError(
            f"금지 open 동작이 확장 주입된 실효 컴포트 집합에 있음: {sorted(leaked)}"
        )
    pol_extra = ExecutionPolicy(enabled=True, extra_executable=probe_extra)
    # 5a) 금지 open 을 비안전 카테고리로 위장해 주입해도 절대 AUTO 가 아니다(제거됨).
    for cap, act in sorted(FORBIDDEN_OPEN):
        probe = CandidateAction(
            capability=cap, action=act, category="light",  # 비안전 위장
            entity_id=f"{cap}.probe", params={},
        )
        for tier in RiskTier:
            if pol_extra.decide(probe, tier) == ExecDecision.AUTO:
                raise InvariantError(
                    f"금지 open {cap}.{act} 가 확장 주입 상태 {tier.name} 에서 AUTO — 위반"
                )
    # 5b) 안전 카테고리는 확장 주입 상태에서도 어떤 티어에서도 AUTO 가 아니다.
    for cat in sorted(safety_categories()):
        safe_probe = CandidateAction(
            capability="valve", action="close", category=cat,
            entity_id="valve.probe", params={},
        )
        for tier in RiskTier:
            if pol_extra.decide(safe_probe, tier) == ExecDecision.AUTO:
                raise InvariantError(
                    f"안전 카테고리 {cat!r} 가 확장 주입 상태 {tier.name} 에서 AUTO — 위반"
                )

    # (6) 원탭 예외는 상수에 갇혀 있다.
    # 6a) 불변 집합 + 실행 허용의 부분집합(확인 완화 ⊆ 실행 허용).
    if not isinstance(ONE_TAP_SAFE_ACTIONS, frozenset) or not (
        ONE_TAP_SAFE_ACTIONS <= SAFETY_EXECUTABLE
    ):
        raise InvariantError(
            "원탭 상수(ONE_TAP_SAFE_ACTIONS)가 SAFETY_EXECUTABLE 의 부분집합(frozenset)이 "
            f"아님: {sorted(ONE_TAP_SAFE_ACTIONS - SAFETY_EXECUTABLE)}"
        )
    # 6b) 원탭 밖의 안전 행동은 확장 주입 상태에서도 단일 확인·자동이 아니다.
    lenient = (ExecDecision.AUTO, ExecDecision.CONFIRM)
    outside = (SAFETY_EXECUTABLE - ONE_TAP_SAFE_ACTIONS) | FORBIDDEN_OPEN | probe_extra
    for cat in sorted(safety_categories()):
        for cap, act in sorted(outside - ONE_TAP_SAFE_ACTIONS):
            probe = CandidateAction(
                capability=cap, action=act, category=cat,
                entity_id=f"{cap}.probe", params={},
            )
            for tier in RiskTier:
                if pol_extra.decide(probe, tier) in lenient:
                    raise InvariantError(
                        f"원탭 밖의 안전 행동 {cat!r} {cap}.{act} 가 {tier.name} 에서 "
                        "단일 확인/자동으로 결정됨 — 원탭 예외 누수"
                    )
    # 6c) 라우터: 원탭 쌍만 EMBER + light, 상황 알림은 EMBER + double(A/B 동일).
    from ..layers.l4_execution.risk_router import ONE_TAP_CONFIRMATION, RiskRouter

    def _probe_judgment(cap: str, act: str, cat: str) -> Judgment:
        return Judgment(
            judgment_id="probe", trace_id="probe", home_id="probe", snapshot_hash="probe",
            action=CandidateAction(
                capability=cap, action=act, category=cat,
                entity_id=f"{cap}.probe", params={},
            ),
            risk_tier=RiskTier.EMBER, confidence=1.0, rationale="probe",
            gate1_verdict="gate1:probe", bundle_id="probe", model_id="probe",
        )

    probes = [(cap, act, "gas", ONE_TAP_CONFIRMATION) for cap, act in sorted(ONE_TAP_SAFE_ACTIONS)]
    probes.append(("gas", "alert", "gas", RiskTier.EMBER.required_confirmation))
    for intervention in (TieredIntervention(), UniformIntervention()):
        router = RiskRouter(intervention=intervention)
        for cap, act, cat, expected in probes:
            prop = router.route(
                _probe_judgment(cap, act, cat), created_at="1970-01-01T00:00:00+00:00"
            )
            if prop.risk_tier != RiskTier.EMBER or prop.required_confirmation != expected:
                raise InvariantError(
                    f"원탭 예외 위반(조건 {intervention.condition}): {cap}.{act} → "
                    f"{prop.risk_tier.name}+{prop.required_confirmation} "
                    f"(기대 EMBER+{expected})"
                )


# 외부에서 추상 Executor 인터페이스를 정의하고 싶을 때 베이스로 쓸 수 있는
# Protocol. assert_executor_confined 가 이 형태(추상)는 허용한다.
@runtime_checkable
class ExecutorProtocol(Protocol):  # pragma: no cover - 인터페이스 선언
    """액추에이터 실행 인터페이스(Phase1 에서는 구현 금지)."""

    async def execute(self, *args: object, **kwargs: object) -> object: ...
