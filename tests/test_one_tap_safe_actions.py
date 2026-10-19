"""안전한 방향 안전 조치의 원탭 확인(T4) — 개입 하한의 **유일한 명시 예외**.

대상은 정확히 두 쌍이다: ``(valve, close)``(가스·수도 밸브 잠그기)와
``(lock, lock)``(현관 잠그기). 이 둘의 제안은

  - 위험도(risk_tier)는 **EMBER 그대로**(강등 없음 — 알림 우회 권한도 그대로),
  - 확인 강도만 ``light``(탭 1회),
  - 조건 A(단일 승인)·B(위험도 차등)에서 **똑같이** ``light``.

그 밖의 안전 행동(상황 알림 ``alert`` 포함)은 EMBER + ``double`` 그대로이고,
여는 방향(``FORBIDDEN_OPEN``)은 여전히 실행 불가다.

쌍마다 테스트를 따로 둔다 — 상수에서 한 쌍을 빼면 **그 쌍의** 테스트만 빨개져야
한다(뮤테이션 점검: 동어반복 금지, 기대값은 리터럴).
"""

from __future__ import annotations

import pytest

from liot.core.contracts import CandidateAction, Judgment
from liot.core.invariants import InvariantError
from liot.core.risk import RiskTier
from liot.layers.l4_execution.device_catalog import load_device_catalog
from liot.layers.l4_execution.executor import (
    FORBIDDEN_OPEN,
    ONE_TAP_SAFE_ACTIONS,
    SAFETY_EXECUTABLE,
    ExecDecision,
    ExecutionPolicy,
)
from liot.layers.l4_execution.intervention import (
    TieredIntervention,
    UniformIntervention,
)
from liot.layers.l4_execution.risk_router import RiskRouter

CREATED = "2026-06-01T12:00:00+00:00"

# 원탭 대상 — 쌍마다 실제 안전 카테고리를 붙인다(리터럴 기대값).
VALVE_CLOSE = ("valve", "close", "switch.gas_valve", "gas")
LOCK_LOCK = ("lock", "lock", "lock.front_door", "lock")
ONE_TAP_CASES = [
    pytest.param(VALVE_CLOSE, id="valve.close"),
    pytest.param(LOCK_LOCK, id="lock.lock"),
]

# 원탭이 아닌 안전 행동 — EMBER + double 그대로여야 한다.
OTHER_SAFETY_CASES = [
    pytest.param(("gas", "alert", "binary_sensor.kitchen_gas", "gas"), id="alert"),
    pytest.param(("valve", "open", "switch.gas_valve", "gas"), id="valve.open"),
    pytest.param(("lock", "unlock", "lock.front_door", "lock"), id="lock.unlock"),
    pytest.param(("oven", "turn_off", "switch.oven", "oven"), id="oven.turn_off"),
    pytest.param(("cover", "close", "cover.garage", "door"), id="cover.close(door)"),
]

CONDITIONS = [
    pytest.param(UniformIntervention, id="A"),
    pytest.param(TieredIntervention, id="B"),
]


def _judgment(case: tuple[str, str, str, str], tier: RiskTier = RiskTier.EMBER) -> Judgment:
    cap, act, entity, category = case
    return Judgment(
        judgment_id="j1",
        trace_id="t1",
        home_id="singleton",
        snapshot_hash="h1",
        action=CandidateAction(
            capability=cap, action=act, params={}, entity_id=entity, category=category
        ),
        risk_tier=tier,
        confidence=1.0,
        rationale="test",
        gate1_verdict="gate1:allow floor=EMBER (test)",
        bundle_id="b1",
        model_id="m1",
    )


def _action(case: tuple[str, str, str, str]) -> CandidateAction:
    cap, act, entity, category = case
    return CandidateAction(
        capability=cap, action=act, params={}, entity_id=entity, category=category
    )


# ── 상수: 리터럴 고정 · 부분집합 · 금지 open 과 서로소 ────────────────────
def test_one_tap_contents_are_pinned_literally() -> None:
    """원탭 상수의 구성원을 리터럴로 고정한다(자기 자신을 기대값으로 쓰지 않는다)."""
    assert frozenset({("valve", "close"), ("lock", "lock")}) == ONE_TAP_SAFE_ACTIONS
    # 런타임에 늘어날 수 없는 불변 집합이어야 한다(등록 경로 없음).
    assert type(ONE_TAP_SAFE_ACTIONS) is frozenset


def test_one_tap_is_subset_of_safety_executable() -> None:
    """확인 완화는 실행 허용의 부분집합이다 — 실행 못 하는 걸 원탭으로 풀 수 없다."""
    assert ONE_TAP_SAFE_ACTIONS <= SAFETY_EXECUTABLE


def test_one_tap_never_contains_an_open_direction() -> None:
    assert not (ONE_TAP_SAFE_ACTIONS & FORBIDDEN_OPEN)
    assert not (ONE_TAP_SAFE_ACTIONS & {("lock", "unlock"), ("valve", "open")})


# ── 라우터: 원탭 두 쌍은 EMBER + light, 조건 A/B 동일 ─────────────────────
@pytest.mark.parametrize("policy", CONDITIONS)
@pytest.mark.parametrize("case", ONE_TAP_CASES)
def test_one_tap_pair_routes_to_ember_light(case, policy) -> None:
    proposal = RiskRouter(intervention=policy()).route(_judgment(case), created_at=CREATED)
    assert proposal.risk_tier == RiskTier.EMBER  # 위험도는 절대 낮추지 않는다
    assert proposal.required_confirmation == "light"


@pytest.mark.parametrize("case", ONE_TAP_CASES)
def test_one_tap_pair_is_identical_across_conditions(case) -> None:
    a = RiskRouter(intervention=UniformIntervention()).route(_judgment(case), created_at=CREATED)
    b = RiskRouter(intervention=TieredIntervention()).route(_judgment(case), created_at=CREATED)
    assert (a.risk_tier, a.required_confirmation) == (b.risk_tier, b.required_confirmation)
    assert (a.risk_tier, a.required_confirmation) == (RiskTier.EMBER, "light")


@pytest.mark.parametrize("case", ONE_TAP_CASES)
def test_one_tap_pair_with_catalog_keeps_ember(case) -> None:
    """카탈로그는 이 쌍을 BREATH 로 보지만 EMBER 판단을 강등하지 않는다(승격만)."""
    router = RiskRouter(catalog=load_device_catalog(), intervention=TieredIntervention())
    proposal = router.route(_judgment(case), created_at=CREATED)
    assert proposal.risk_tier == RiskTier.EMBER
    assert proposal.required_confirmation == "light"


@pytest.mark.parametrize("case", ONE_TAP_CASES)
def test_one_tap_floor_still_rejects_a_policy_weaker_than_light(case) -> None:
    """예외의 하한은 light 다 — 정책이 none 을 내면 여전히 불변식 위반."""

    class _WeakPolicy:
        condition = "X"

        def confirmation_for(self, tier):  # noqa: ANN001
            return "none"

    with pytest.raises(InvariantError):
        RiskRouter(intervention=_WeakPolicy()).route(_judgment(case), created_at=CREATED)


def test_one_tap_exception_does_not_weaken_lower_tiers() -> None:
    """예외는 EMBER 의 double → light 하나뿐 — DUSK 의 explicit 을 무르게 하지 않는다."""
    proposal = RiskRouter(intervention=TieredIntervention()).route(
        _judgment(LOCK_LOCK, tier=RiskTier.DUSK), created_at=CREATED
    )
    assert proposal.risk_tier == RiskTier.DUSK
    assert proposal.required_confirmation == "explicit"


# ── 라우터: 그 밖의 안전 행동은 EMBER + double 그대로 ───────────────────
@pytest.mark.parametrize("policy", CONDITIONS)
@pytest.mark.parametrize("case", OTHER_SAFETY_CASES)
def test_other_safety_actions_stay_ember_double(case, policy) -> None:
    proposal = RiskRouter(intervention=policy()).route(_judgment(case), created_at=CREATED)
    assert proposal.risk_tier == RiskTier.EMBER
    assert proposal.required_confirmation == "double"


# ── 실행 정책: 원탭 두 쌍은 단일 확인(CONFIRM), 절대 AUTO 아님 ─────────────
@pytest.mark.parametrize("case", ONE_TAP_CASES)
def test_one_tap_pair_is_single_confirm_at_every_tier(case) -> None:
    pol = ExecutionPolicy(enabled=True)
    for tier in RiskTier:
        assert pol.decide(_action(case), tier) == ExecDecision.CONFIRM


@pytest.mark.parametrize("case", OTHER_SAFETY_CASES)
def test_other_safety_actions_are_never_single_confirm(case) -> None:
    """원탭 밖의 안전 행동은 실행 화이트리스트 밖이라 알림만(단일 확인 실행 없음)."""
    pol = ExecutionPolicy(enabled=True)
    for tier in RiskTier:
        assert pol.decide(_action(case), tier) == ExecDecision.NOTIFY_ONLY


def test_extension_cannot_widen_one_tap() -> None:
    """확장 허용목록(카탈로그 파생분)을 주입해도 원탭은 넓어지지 않는다.

    oven.turn_off 를 확장분으로 넣어도 안전 카테고리면 원탭(CONFIRM)이 아니다 —
    원탭은 상수 리터럴로만 정해지고 등록·지시로 늘어날 길이 없다.
    """
    before = ONE_TAP_SAFE_ACTIONS
    pol = ExecutionPolicy(
        enabled=True,
        extra_executable=frozenset({("oven", "turn_off"), ("lock", "unlock"), ("valve", "open")}),
    )
    for case in (
        ("oven", "turn_off", "switch.oven", "oven"),
        ("lock", "unlock", "lock.front_door", "lock"),
        ("valve", "open", "switch.gas_valve", "gas"),
    ):
        for tier in RiskTier:
            assert pol.decide(_action(case), tier) == ExecDecision.NOTIFY_ONLY
    assert before is ONE_TAP_SAFE_ACTIONS  # 정책 생성이 상수를 바꾸지 않는다


def test_disabled_policy_keeps_one_tap_notify_only() -> None:
    """옵트인 OFF 면 원탭 쌍도 실행하지 않는다(완화가 옵트인을 우회하지 않는다)."""
    pol = ExecutionPolicy(enabled=False)
    for case in (VALVE_CLOSE, LOCK_LOCK):
        assert pol.decide(_action(case), RiskTier.EMBER) == ExecDecision.NOTIFY_ONLY


def test_new_safety_executable_defaults_to_double_confirm(monkeypatch: pytest.MonkeyPatch) -> None:
    """실행 허용에 새 안전 조치를 넣어도 원탭이 되지 않는다 — 기본은 이중확인.

    실행 허용(SAFETY_EXECUTABLE)과 확인 완화(ONE_TAP_SAFE_ACTIONS)는 다른 결정이다.
    """
    from liot.layers.l4_execution import executor as ex

    monkeypatch.setattr(
        ex, "SAFETY_EXECUTABLE", ex.SAFETY_EXECUTABLE | {("oven", "turn_off")}
    )
    pol = ExecutionPolicy(enabled=True)
    oven = _action(("oven", "turn_off", "switch.oven", "oven"))
    for tier in RiskTier:
        assert pol.decide(oven, tier) == ExecDecision.DOUBLE_CONFIRM
