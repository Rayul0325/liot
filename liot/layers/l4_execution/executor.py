"""L4 실행기 — 승인된 동작을 HA로 실제 조작 (불변식 8: Execution-Authorized-Only).

LIOT 의 기본은 제안-만(불변식 3)이다. 이 모듈은 그 위에 *옵트인* 조작 능력을
얹되, 사람을 루프에서 빼지 않는다:

  - **옵트인 기본 OFF**: ``ExecutionPolicy(enabled=False)`` 면 모든 결정이
    NOTIFY_ONLY → 한 줄도 실행하지 않는다(현행 제안-만 보존). env
    ``LIOT_EXECUTION_ENABLED`` 가 참일 때만 켜진다.
  - **티어 = 실행 정책**: 비안전 컴포트는 BREATH 이하 자동, DUSK 단일확인,
    EMBER 이중확인. 안전 임계 카테고리는 *절대 자동 금지* — 사람 승인 후에만.
    안전 조치는 기본이 이중확인이고, 유일한 명시 예외는 ``ONE_TAP_SAFE_ACTIONS``
    (밸브 잠그기·현관 잠그기 — 안전한 방향)의 단일 확인(원탭)이다(T4).
  - **화이트리스트**: 조작 가능한 ``(capability, action)`` 만 실행. 그 외는
    NOTIFY_ONLY. 실행기는 정책이 허용해도 한 번 더 화이트리스트를 재검(방어심층).
  - **안전 조작은 fail-safe 방향만**: 밸브/수도 *잠그기*, 현관 *잠그기* 는
    허용하되 *열기*(unlock/밸브 open)는 화이트리스트에서 제외 — AI 가 집을
    여는 일은 없다.
  - **되돌리기**: 실행 전 이전 상태를 캡처해 ExecutionResult 로 돌려준다 →
    상위가 "되돌리기" 알림/undo 를 제공할 수 있다.

불변식 8 한 줄: *액추에이터 호출은 (a) 활성화됨 + (b) 화이트리스트에 있음 +
(c) 티어에 맞는 권한(자동은 비안전 ≤BREATH 만, 그 외 신선한 사람 승인) 을 모두
만족할 때만 일어난다.* 인젝션·오판은 이 관문들을 통과하지 못한다.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from enum import Enum
from typing import Any, Protocol, runtime_checkable

from liot.core.contracts import CandidateAction
from liot.core.risk import RiskTier, is_safety_category

__all__ = [
    "ExecDecision",
    "ExecutionPolicy",
    "ExecutionResult",
    "Executor",
    "FakeExecutor",
    "HAExecutor",
    "ExecutionDenied",
    "ExecutionError",
    "COMFORT_EXECUTABLE",
    "SAFETY_EXECUTABLE",
    "FORBIDDEN_OPEN",
    "ONE_TAP_SAFE_ACTIONS",
    "NON_EXECUTABLE_CAPABILITIES",
    "effective_comfort",
    "is_non_executable",
    "is_one_tap_safe",
]


class ExecutionDenied(Exception):
    """화이트리스트/정책상 실행이 허용되지 않음(거부)."""


class ExecutionError(Exception):
    """실행 자체(HA 호출)가 실패."""


class ExecDecision(str, Enum):
    """판정 결과가 어떻게 실행되어야 하는가."""

    AUTO = "auto"  # 즉시 자동 실행(비안전 ≤BREATH 만)
    CONFIRM = "confirm"  # 단일 명시 확인(탭 1회) 후 실행 — DUSK
    DOUBLE_CONFIRM = "double_confirm"  # 이중확인 후 실행 — EMBER/안전(원탭 예외 제외)
    NOTIFY_ONLY = "notify_only"  # 실행하지 않음(조작 불가/비활성/비화이트리스트)


# 비안전 컴포트 조작 화이트리스트: (capability, action). 가역적이고 저위험인 것만.
COMFORT_EXECUTABLE: frozenset[tuple[str, str]] = frozenset(
    {
        ("light", "turn_on"),
        ("light", "turn_off"),
        ("light", "set_brightness"),
        ("climate", "set_temperature"),
        ("cover", "open"),
        ("cover", "close"),
        ("cover", "set_position"),
        ("fan", "turn_on"),
        ("fan", "turn_off"),
        ("media", "pause"),
        ("media", "set_volume"),
        ("switch", "turn_on"),
        ("switch", "turn_off"),
    }
)

# 안전 임계 조작 화이트리스트 — '안전한 방향'(차단/잠금)만. 절대 자동 금지(이중확인만).
# 여는 방향(lock.unlock, valve.open)은 의도적으로 제외: AI 가 집을 여는 일은 없다.
SAFETY_EXECUTABLE: frozenset[tuple[str, str]] = frozenset(
    {
        ("valve", "close"),  # 수도/가스 메인 차단
        ("lock", "lock"),  # 현관 잠그기
    }
)

# '여는 방향' 금지 상수(불변) — 카탈로그 파생 확장분(extra_executable)으로도 절대 실행
# 화이트리스트에 들어갈 수 없다. AI 가 집·밸브를 여는 일은 없다(하드 블록리스트).
# cover.open 은 컴포트로 합법 등록돼 있으므로(COMFORT_EXECUTABLE) 여기 넣지 않는다.
FORBIDDEN_OPEN: frozenset[tuple[str, str]] = frozenset(
    {("lock", "unlock"), ("valve", "open")}
)

# 실행 대상이 아닌 capability(불변) — '권유'(advice)는 사람에게 하는 말일 뿐 기기 조작이
# 아니다(창문은 사람이 연다). 정책이 화이트리스트를 보기 **전에** 거부하고, 실행기 두 곳도
# 호출 전에 막는다. 카탈로그 파생 확장분으로도 실행집합에 들어갈 수 없다.
NON_EXECUTABLE_CAPABILITIES: frozenset[str] = frozenset({"advice"})

# 원탭(단일 확인) 안전 조치 — 개입 하한("확인은 티어 기준선보다 약할 수 없다")의
# **유일한 명시 예외**(T4). 닫는·잠그는 안전한 방향 두 쌍만: 위험이 생겼을 때 사람이
# 탭 한 번으로 막을 수 있어야 한다. 위험도는 EMBER 그대로(알림 우회 권한 유지) —
# 확인 강도만 light(라우터)·CONFIRM(실행 정책)으로 풀린다.
#
# SAFETY_EXECUTABLE 을 재사용하지 않는다: '실행해도 되는가'와 '확인을 풀어도 되는가'
# 는 다른 결정이다(``ONE_TAP_SAFE_ACTIONS ⊆ SAFETY_EXECUTABLE`` 만 성립해야 한다 —
# assert_execution_gated 가 검사). frozenset 리터럴이고 등록 경로가 없다: 카탈로그
# 파생 확장분(extra_executable)·런타임 등록·사용자 지시 어느 것도 이 집합을 늘리지 못한다.
ONE_TAP_SAFE_ACTIONS: frozenset[tuple[str, str]] = frozenset(
    {
        ("valve", "close"),  # 가스/수도 밸브 잠그기
        ("lock", "lock"),  # 현관 잠그기
    }
)


def effective_comfort(
    extra: frozenset[tuple[str, str]] | None = None,
) -> frozenset[tuple[str, str]]:
    """실효 컴포트 화이트리스트 = 내장 ``COMFORT_EXECUTABLE`` ∪ 확장분 − 금지 open.

    ``extra`` 미지정/빈값 → 내장 set 그대로(COMFORT_EXECUTABLE 은 금지 open 과
    서로소라 뺄셈이 no-op → 기존 정책·실행기 동작 불변). 데모가 카탈로그 파생분을
    주입해도 ``FORBIDDEN_OPEN`` 은 언제나 제거되어 어떤 실효 실행집합에도 없다.
    확장분은 소문자로 정규화한다(카탈로그 파생분과 _key 정규화 정합).
    """
    if not extra:
        return COMFORT_EXECUTABLE - FORBIDDEN_OPEN
    norm = frozenset(
        (str(cap).strip().lower(), str(act).strip().lower())
        for cap, act in extra
        if str(cap).strip().lower() not in NON_EXECUTABLE_CAPABILITIES
    )
    return (COMFORT_EXECUTABLE | norm) - FORBIDDEN_OPEN


def _key(action: CandidateAction) -> tuple[str, str]:
    return ((action.capability or "").strip().lower(), (action.action or "").strip().lower())


def _is_safety(action: CandidateAction) -> bool:
    return is_safety_category(action.category)


def is_non_executable(action: CandidateAction) -> bool:
    """실행 대상이 아닌 행동(권유 등)인가 — ``NON_EXECUTABLE_CAPABILITIES`` 로만 정한다."""
    return _key(action)[0] in NON_EXECUTABLE_CAPABILITIES


def _deny_non_executable(action: CandidateAction) -> None:
    if is_non_executable(action):
        raise ExecutionDenied(f"not an executable capability: {_key(action)[0]}")


def is_one_tap_safe(action: CandidateAction) -> bool:
    """이 행동이 원탭(단일 확인) 예외 대상인가 — ``ONE_TAP_SAFE_ACTIONS`` 로만 정한다.

    (capability, action) 은 실행 화이트리스트와 같은 규칙(_key: 공백 제거·소문자)
    으로 정규화한다 — 라우터(light)와 실행 정책(CONFIRM)이 같은 쌍을 보게 한다.
    """
    return _key(action) in ONE_TAP_SAFE_ACTIONS


class ExecutionPolicy:
    """판정(action, tier) → ExecDecision. 순수 함수, 부수효과 없음.

    enabled=False(기본) 면 항상 NOTIFY_ONLY → 옵트인 전엔 한 줄도 실행 안 함.
    """

    def __init__(
        self,
        *,
        enabled: bool = False,
        extra_executable: frozenset[tuple[str, str]] | None = None,
    ) -> None:
        self.enabled = enabled
        # 실효 컴포트 = 내장 ∪ 확장분 − 금지 open. 기본(None) → 내장 set 그대로.
        self._comfort = effective_comfort(extra_executable)

    def decide(self, action: CandidateAction, tier: RiskTier | str | int) -> ExecDecision:
        if not self.enabled:
            return ExecDecision.NOTIFY_ONLY
        if is_non_executable(action):
            return ExecDecision.NOTIFY_ONLY  # 권유는 말로만 — 화이트리스트를 보기 전에 끝
        tier_t = RiskTier.from_str(tier)
        key = _key(action)

        if _is_safety(action):
            # 안전 임계: 자동 절대 금지. 화이트리스트(차단/잠금) 밖이면 알림만.
            if key not in SAFETY_EXECUTABLE:
                return ExecDecision.NOTIFY_ONLY
            # 원탭 예외(T4): 밸브 잠그기·현관 잠그기는 단일 확인. 나머지는 이중확인.
            if is_one_tap_safe(action):
                return ExecDecision.CONFIRM
            return ExecDecision.DOUBLE_CONFIRM

        if key not in self._comfort:
            return ExecDecision.NOTIFY_ONLY  # 조작 대상이 아님 → 알림만

        if tier_t <= RiskTier.BREATH:
            return ExecDecision.AUTO
        if tier_t == RiskTier.DUSK:
            return ExecDecision.CONFIRM
        return ExecDecision.DOUBLE_CONFIRM  # 비안전 EMBER(드묾) → 이중확인


@dataclass
class ExecutionResult:
    """실행 결과(관측·되돌리기용)."""

    ok: bool
    service: str = ""
    prior_state: dict[str, Any] | None = None
    error: str | None = None


@runtime_checkable
class Executor(Protocol):
    """승인된 CandidateAction 을 실제 세계로 실행한다(async — HA WS 계층과 정합)."""

    async def execute(self, action: CandidateAction) -> ExecutionResult: ...
    async def undo(
        self, action: CandidateAction, prior_state: dict[str, Any]
    ) -> ExecutionResult: ...


# capability → HA 도메인.
_CAP_DOMAIN: dict[str, str] = {
    "light": "light",
    "climate": "climate",
    "cover": "cover",
    "fan": "fan",
    "media": "media_player",
    "switch": "switch",
    "lock": "lock",
    "valve": "valve",
}

# 되돌리기용 역동작(on/off·open/close·lock). set_* 류는 prior_state 값으로 복원.
_INVERSE_ACTION: dict[str, str] = {
    "turn_on": "turn_off",
    "turn_off": "turn_on",
    "open": "close",
    "close": "open",
    "lock": "unlock",
}


def _deny_opening_undo(action: CandidateAction) -> None:
    """되돌리기의 역동작이 '여는 방향'(FORBIDDEN_OPEN)이면 거부한다.

    밸브 잠그기·도어락 잠그기를 되돌리면 valve.open·lock.unlock 이 된다. 실행
    화이트리스트에서 여는 방향을 뺀 것과 같은 이유로, 되돌리기 경로로도 AI 가 집을
    여는 일은 없어야 한다 — 호출 전에 막는다(외부 호출 0).
    """
    cap, act = _key(action)
    inverse = _INVERSE_ACTION.get(act)
    if inverse is not None and (cap, inverse) in FORBIDDEN_OPEN:
        raise ExecutionDenied(f"undo would open the home: {cap}.{inverse}")


class HAExecutor:
    """HA ``call_service`` 로 실제 조작. 콜러블 주입(웹소켓 비의존 → 테스트 용이).

    call_service(domain, service, entity_id, data) / get_state(entity_id)->dict 를
    주입받는다(서비스 계층이 HABridge 메서드를 연결). 실행기는 화이트리스트를
    스스로 재검하고, 실행 전 이전 상태를 캡처한다.
    """

    def __init__(
        self,
        call_service: Callable[[str, str, str, dict[str, Any]], Any],
        get_state: Callable[[str], Any] | None = None,
        *,
        extra_executable: frozenset[tuple[str, str]] | None = None,
    ) -> None:
        # call_service/get_state 는 async 콜러블(awaitable 반환). HABridge 메서드 연결.
        self._call = call_service
        self._get_state = get_state
        self._comfort = effective_comfort(extra_executable)

    def _allowed(self, action: CandidateAction) -> bool:
        key = _key(action)
        return key in self._comfort or key in SAFETY_EXECUTABLE

    def _service_for(self, action: CandidateAction) -> tuple[str, str]:
        cap, act = _key(action)
        domain = _CAP_DOMAIN.get(cap)
        if domain is None:
            raise ExecutionDenied(f"unmapped capability: {cap!r}")
        return domain, act

    @staticmethod
    def _service_data(action: CandidateAction) -> dict[str, Any]:
        # 수치 파라미터만 전달(자유 문자열 미전달 — 방어적).
        data: dict[str, Any] = {}
        for k in ("brightness", "temperature", "position", "volume_level"):
            v = action.params.get(k)
            if isinstance(v, (int, float)) and not isinstance(v, bool):
                data[k] = v
        return data

    async def execute(self, action: CandidateAction) -> ExecutionResult:
        _deny_non_executable(action)
        if not self._allowed(action):
            # 방어심층: 정책이 잘못 통과시켜도 실행기가 막는다.
            raise ExecutionDenied(f"action not in allowlist: {_key(action)}")
        domain, service = self._service_for(action)
        prior = None
        if self._get_state is not None:
            try:
                prior = await self._get_state(action.entity_id)
            except Exception:  # pragma: no cover - 상태 조회 실패는 치명적 아님
                prior = None
        try:
            await self._call(domain, service, action.entity_id, self._service_data(action))
        except Exception as exc:
            raise ExecutionError(f"HA call_service {domain}.{service} 실패: {exc}") from exc
        return ExecutionResult(ok=True, service=f"{domain}.{service}", prior_state=prior)

    async def undo(
        self, action: CandidateAction, prior_state: dict[str, Any]
    ) -> ExecutionResult:
        """이전 상태로 복원. on/off·open/close 는 역동작, set_* 는 prior 값 재적용."""
        _deny_opening_undo(action)
        domain, service = self._service_for(action)
        _, act = _key(action)
        inverse = _INVERSE_ACTION.get(act)
        data: dict[str, Any] = {}
        if inverse is None:
            # set_brightness/set_temperature 등 → 이전 attributes 로 복원 시도.
            attrs = (prior_state or {}).get("attributes", {}) or {}
            inverse = act
            for k in ("brightness", "temperature", "position", "volume_level"):
                if k in attrs and isinstance(attrs[k], (int, float)):
                    data[k] = attrs[k]
        try:
            await self._call(domain, inverse, action.entity_id, data)
        except Exception as exc:
            raise ExecutionError(f"HA undo {domain}.{inverse} 실패: {exc}") from exc
        return ExecutionResult(ok=True, service=f"{domain}.{inverse}(undo)")


class FakeExecutor:
    """테스트용 실행기. execute/undo 호출을 기록한다. fail=True 면 ExecutionError."""

    def __init__(
        self,
        *,
        fail: bool = False,
        extra_executable: frozenset[tuple[str, str]] | None = None,
    ) -> None:
        self.executed: list[CandidateAction] = []
        self.undone: list[CandidateAction] = []
        self.fail = fail
        self._comfort = effective_comfort(extra_executable)

    async def execute(self, action: CandidateAction) -> ExecutionResult:
        _deny_non_executable(action)
        key = _key(action)
        if key not in self._comfort and key not in SAFETY_EXECUTABLE:
            raise ExecutionDenied(f"action not in allowlist: {key}")
        if self.fail:
            raise ExecutionError("fake executor forced failure")
        self.executed.append(action)
        return ExecutionResult(ok=True, service=f"{key[0]}.{key[1]}", prior_state={"state": "prior"})

    async def undo(
        self, action: CandidateAction, prior_state: dict[str, Any]
    ) -> ExecutionResult:
        _deny_opening_undo(action)
        self.undone.append(action)
        return ExecutionResult(ok=True, service=f"{_key(action)[0]}.undo")


def build_execution_policy() -> ExecutionPolicy:
    """env ``LIOT_EXECUTION_ENABLED`` 로 정책 구성. 미설정/거짓이면 비활성(제안-만)."""
    from liot.core.env import env_flag

    return ExecutionPolicy(enabled=env_flag("LIOT_EXECUTION_ENABLED"))
