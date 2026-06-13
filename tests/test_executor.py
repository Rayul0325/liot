"""L4 실행기 + 실행 정책 테스트 (불변식 8). 순수 동기, 네트워크 없음."""

from __future__ import annotations

import pytest

from liot.core.contracts import CandidateAction
from liot.core.risk import RiskTier
from liot.layers.l4_execution.executor import (
    ExecDecision,
    ExecutionDenied,
    ExecutionError,
    ExecutionPolicy,
    FakeExecutor,
    HAExecutor,
    build_execution_policy,
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


# ── env 빌더 ───────────────────────────────────────────────────────────
def test_build_policy_from_env(monkeypatch):
    monkeypatch.delenv("LIOT_EXECUTION_ENABLED", raising=False)
    assert build_execution_policy().enabled is False
    monkeypatch.setenv("LIOT_EXECUTION_ENABLED", "true")
    assert build_execution_policy().enabled is True
    monkeypatch.setenv("LIOT_EXECUTION_ENABLED", "0")
    assert build_execution_policy().enabled is False
