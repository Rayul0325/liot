"""L4 실행기 + 실행 정책 테스트 (불변식 8). 순수 동기, 네트워크 없음."""

from __future__ import annotations

import pytest

from liot.core.contracts import CandidateAction
from liot.core.risk import RiskTier
from liot.layers.l4_execution.executor import (
    COMFORT_EXECUTABLE,
    FORBIDDEN_OPEN,
    ExecDecision,
    ExecutionDenied,
    ExecutionError,
    ExecutionPolicy,
    FakeExecutor,
    HAExecutor,
    build_execution_policy,
    effective_comfort,
)


def _act(cap: str, action: str, category: str, **params) -> CandidateAction:
    return CandidateAction(
        capability=cap, action=action, category=category,
        entity_id=f"{cap}.test_entity", params=dict(params),
    )


# ── 정책: 옵트인 기본 OFF ───────────────────────────────────────────────
def test_disabled_policy_never_executes():
    pol = ExecutionPolicy(enabled=False)
    for tier in RiskTier:
        assert pol.decide(_act("light", "turn_off", "light"), tier) == ExecDecision.NOTIFY_ONLY
    # 안전 장치도 당연히 NOTIFY_ONLY
    assert pol.decide(_act("lock", "lock", "lock"), RiskTier.EMBER) == ExecDecision.NOTIFY_ONLY


# ── 정책: 비안전 컴포트 티어 매핑 ──────────────────────────────────────
def test_comfort_tier_policy():
    pol = ExecutionPolicy(enabled=True)
    light = _act("light", "turn_off", "light")
    assert pol.decide(light, RiskTier.AMBIENT) == ExecDecision.AUTO
    assert pol.decide(light, RiskTier.BREATH) == ExecDecision.AUTO
    assert pol.decide(light, RiskTier.DUSK) == ExecDecision.CONFIRM
    assert pol.decide(light, RiskTier.EMBER) == ExecDecision.DOUBLE_CONFIRM


def test_non_allowlisted_comfort_is_notify_only():
    pol = ExecutionPolicy(enabled=True)
    # vacuum.start 는 화이트리스트에 없음 → 조작 불가, 알림만
    assert pol.decide(_act("vacuum", "start", "appliance"), RiskTier.BREATH) == ExecDecision.NOTIFY_ONLY


# ── 정책: 안전 임계는 절대 자동 금지, fail-safe 방향만 ─────────────────
def test_safety_never_auto_even_at_low_tier():
    pol = ExecutionPolicy(enabled=True)
    # 안전 카테고리는 BREATH 라도 자동 금지 → 이중확인
    assert pol.decide(_act("lock", "lock", "lock"), RiskTier.BREATH) == ExecDecision.DOUBLE_CONFIRM
    assert pol.decide(_act("valve", "close", "water_main"), RiskTier.AMBIENT) == ExecDecision.DOUBLE_CONFIRM


def test_safety_open_direction_excluded():
    pol = ExecutionPolicy(enabled=True)
    # 여는 방향은 화이트리스트 제외 → 알림만 (AI 가 집을 열지 않음)
    assert pol.decide(_act("lock", "unlock", "lock"), RiskTier.EMBER) == ExecDecision.NOTIFY_ONLY
    assert pol.decide(_act("valve", "open", "water_main"), RiskTier.EMBER) == ExecDecision.NOTIFY_ONLY


def test_safety_non_actuatable_is_notify_only():
    pol = ExecutionPolicy(enabled=True)
    # 가스/연기 감지기 알림은 조작 대상이 아님 → 알림만
    assert pol.decide(_act("notify", "alert", "gas"), RiskTier.EMBER) == ExecDecision.NOTIFY_ONLY


# ── HAExecutor: 실제 호출 매핑 (async) ─────────────────────────────────
async def test_haexecutor_calls_correct_service():
    calls = []

    async def call(d, s, e, data):
        calls.append((d, s, e, data))

    async def get_state(e):
        return {"state": "on", "attributes": {"brightness": 200}}

    ex = HAExecutor(call_service=call, get_state=get_state)
    res = await ex.execute(_act("light", "set_brightness", "light", brightness=120))
    assert res.ok and res.service == "light.set_brightness"
    assert calls == [("light", "set_brightness", "light.test_entity", {"brightness": 120})]
    assert res.prior_state == {"state": "on", "attributes": {"brightness": 200}}


async def test_haexecutor_media_capability_maps_to_media_player():
    calls = []

    async def call(d, s, e, data):
        calls.append((d, s, e, data))

    ex = HAExecutor(call_service=call)
    await ex.execute(_act("media", "pause", "media"))
    assert calls[0][0] == "media_player" and calls[0][1] == "pause"


async def test_haexecutor_drops_non_numeric_params():
    calls = []

    async def call(d, s, e, data):
        calls.append((d, s, e, data))

    ex = HAExecutor(call_service=call)
    # 자유 문자열 파라미터는 전달되지 않아야 한다
    await ex.execute(_act("climate", "set_temperature", "climate", temperature=22, note="해킹시도"))
    assert calls[0][3] == {"temperature": 22}


async def test_haexecutor_denies_non_allowlisted():
    async def call(*a):
        return None

    ex = HAExecutor(call_service=call)
    # 방어심층: unlock 은 화이트리스트 밖 → 정책을 우회해 직접 불러도 거부
    with pytest.raises(ExecutionDenied):
        await ex.execute(_act("lock", "unlock", "lock"))


async def test_haexecutor_wraps_call_failure():
    async def boom(*a):
        raise RuntimeError("ws down")

    ex = HAExecutor(call_service=boom)
    with pytest.raises(ExecutionError):
        await ex.execute(_act("light", "turn_off", "light"))


async def test_haexecutor_undo_inverse_and_restore():
    calls = []

    async def call(d, s, e, data):
        calls.append((d, s, e, data))

    ex = HAExecutor(call_service=call)
    # on/off 역동작
    await ex.undo(_act("light", "turn_off", "light"), {"state": "on"})
    assert calls[-1][:2] == ("light", "turn_on")
    # set_temperature 는 이전 값 복원
    await ex.undo(_act("climate", "set_temperature", "climate"), {"attributes": {"temperature": 21}})
    assert calls[-1] == ("climate", "set_temperature", "climate.test_entity", {"temperature": 21})


# ── FakeExecutor ───────────────────────────────────────────────────────
async def test_fake_executor_records_and_guards():
    fx = FakeExecutor()
    await fx.execute(_act("light", "turn_off", "light"))
    assert len(fx.executed) == 1
    with pytest.raises(ExecutionDenied):
        await fx.execute(_act("lock", "unlock", "lock"))


async def test_fake_executor_fail_mode():
    fx = FakeExecutor(fail=True)
    with pytest.raises(ExecutionError):
        await fx.execute(_act("light", "turn_off", "light"))


# ── 확장 실행 허용목록(extra_executable) — P4 ──────────────────────────
def test_effective_comfort_default_equals_baked():
    # 기본(None)/빈값 → 내장 그대로(COMFORT 은 금지 open 과 서로소라 뺄셈이 no-op).
    assert effective_comfort() == COMFORT_EXECUTABLE
    assert effective_comfort(None) == COMFORT_EXECUTABLE
    assert effective_comfort(frozenset()) == COMFORT_EXECUTABLE


def test_extra_executable_makes_new_action_auto():
    # 카탈로그 파생 확장분 주입 → 새 (cap,action) 이 컴포트 티어 매핑을 탄다.
    pol = ExecutionPolicy(enabled=True, extra_executable=frozenset({("vacuum", "start")}))
    assert pol.decide(_act("vacuum", "start", "appliance"), RiskTier.BREATH) == ExecDecision.AUTO
    assert pol.decide(_act("vacuum", "start", "appliance"), RiskTier.DUSK) == ExecDecision.CONFIRM
    # 확장 없이 기본이면 여전히 NOTIFY_ONLY(기존 동작 불변).
    base = ExecutionPolicy(enabled=True)
    assert base.decide(_act("vacuum", "start", "appliance"), RiskTier.BREATH) == ExecDecision.NOTIFY_ONLY


def test_forbidden_open_never_executable_even_if_injected():
    # 금지 open 을 확장분으로 억지 주입 + 비안전 카테고리로 위장해도 실행 불가.
    pol = ExecutionPolicy(enabled=True, extra_executable=FORBIDDEN_OPEN)
    for cap, act in FORBIDDEN_OPEN:
        for tier in RiskTier:
            assert pol.decide(_act(cap, act, "light"), tier) == ExecDecision.NOTIFY_ONLY
    # effective_comfort 도 금지 open 을 항상 제거한다.
    assert not (effective_comfort(FORBIDDEN_OPEN) & FORBIDDEN_OPEN)


async def test_fake_executor_respects_extra_but_blocks_forbidden_open():
    fx = FakeExecutor(extra_executable=frozenset({("vacuum", "start"), ("lock", "unlock")}))
    await fx.execute(_act("vacuum", "start", "appliance"))
    assert len(fx.executed) == 1
    with pytest.raises(ExecutionDenied):
        await fx.execute(_act("lock", "unlock", "lock"))  # 금지 open → 확장분에서 제거됨


async def test_haexecutor_extra_executable_allows_new_action():
    calls = []

    async def call(d, s, e, data):
        calls.append((d, s, e, data))

    ex = HAExecutor(call_service=call, extra_executable=frozenset({("switch", "toggle")}))
    await ex.execute(_act("switch", "toggle", "appliance"))
    assert calls[0][0] == "switch" and calls[0][1] == "toggle"
    # 확장 없이 기본 실행기는 switch.toggle 을 화이트리스트 밖으로 거부.
    with pytest.raises(ExecutionDenied):
        await HAExecutor(call_service=call).execute(_act("switch", "toggle", "appliance"))


# ── env 빌더 ───────────────────────────────────────────────────────────
def test_build_policy_from_env(monkeypatch):
    monkeypatch.delenv("LIOT_EXECUTION_ENABLED", raising=False)
    assert build_execution_policy().enabled is False
    monkeypatch.setenv("LIOT_EXECUTION_ENABLED", "true")
    assert build_execution_policy().enabled is True
    monkeypatch.setenv("LIOT_EXECUTION_ENABLED", "0")
    assert build_execution_policy().enabled is False


def test_env_flag_reads_truthy_values_only(monkeypatch) -> None:
    """불리언 env 해석은 한 곳(core.env.env_flag)에만 있다 — 모듈마다 복제되던 것.

    옵트인 플래그라 '모르는 값'은 켜지 않는 쪽이 안전하다.
    """
    from liot.core.env import env_flag

    for truthy in ("1", "true", "TRUE", " on ", "Yes"):
        monkeypatch.setenv("LIOT_TEST_FLAG", truthy)
        assert env_flag("LIOT_TEST_FLAG") is True, truthy
    for falsy in ("", "   ", "0", "off", "no", "nope"):
        monkeypatch.setenv("LIOT_TEST_FLAG", falsy)
        assert env_flag("LIOT_TEST_FLAG") is False, falsy
    monkeypatch.delenv("LIOT_TEST_FLAG", raising=False)
    assert env_flag("LIOT_TEST_FLAG") is False
    assert env_flag("LIOT_TEST_FLAG", default=True) is True
