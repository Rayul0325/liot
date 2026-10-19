"""기획 4장면 — 실제 규칙(config/proactive/comfort.yaml)·카탈로그·서비스 게이트로.

장면(집 시간대 Asia/Seoul):
  | 장면           | 조건                                   | 행동                          | 단계 → 게이트 |
  | 아침 환기      | morning · 재실 · pm25 ≤ 15             | advice.ventilate(advice.home) | BREATH → 즉시 |
  | 미세먼지 나쁨  | 재실 · pm25 ≥ 36                       | fan.set_speed 강              | BREATH → 즉시 |
  | 취침 준비      | evening 21시대 · 거실 조명 켜짐        | light.set_brightness 30       | AMBIENT → 모아보기 |
  | 흐린 낮        | day · 재실 · cloud_cover ≥ 0.8         | light.desk 켜기               | AMBIENT → 모아보기 |

각 장면은 자기 조건에서만 발의된다. 권유(advice)는 실행 대상이 아니다. 이유 한 줄(reason_ko)은
결정에만 실리고 ntfy 페이로드에는 들어가지 않는다.
"""

from __future__ import annotations

import json
from typing import Any

import pytest

from liot.app import LiotApp
from liot.core.clock import Clock
from liot.core.contracts import CandidateAction, SensorReading
from liot.core.invariants import assert_execution_gated
from liot.core.risk import RiskTier
from liot.db.database import init_db
from liot.layers.l2_context.external_provider import FakeExternalProvider
from liot.layers.l2_context.proactive_rules import (
    ProactiveRuleError,
    load_proactive_ruleset,
    load_proactive_rulesets,
)
from liot.layers.l3_judgment.llm_judge import FakeLLM
from liot.layers.l4_execution.executor import (
    ExecDecision,
    ExecutionDenied,
    ExecutionPolicy,
    FakeExecutor,
    HAExecutor,
    effective_comfort,
)
from liot.layers.l4_execution.notifier import Notifier
from liot.registry import RuntimeRegistry
from liot.service import LiotService

KST = "Asia/Seoul"
MOTION = "binary_sensor.living_motion"
LIVING_LIGHT = "light.living_room"


def _reading(eid: str, value: Any, at: str) -> SensorReading:
    return SensorReading(entity_id=eid, source="ha", raw_value=value, norm_value=value, ts_utc=at)


async def _tick(
    at: str,
    *,
    occupied: bool = True,
    living_light: bool = False,
    weather: dict[str, Any] | None = None,
) -> tuple[Any, LiotApp]:
    """출하 레지스트리·규칙·FakeLLM 으로 한 틱을 서비스 경로(run_tick)로 돈다."""
    reg = RuntimeRegistry()
    clock = Clock(fixed=at, tz=KST)
    app = LiotApp(
        clock=clock,
        catalog=reg.to_catalog(),
        categories=reg.categories(),
        llm_backend=FakeLLM(),
        external_provider=FakeExternalProvider(clock=clock, **(weather or {})),
    )
    app.observe_reading(_reading(MOTION, occupied, at), reason="seed")
    app.observe_reading(_reading(LIVING_LIGHT, living_light, at), reason="seed")
    db = await init_db(":memory:")
    try:
        svc = LiotService(app, db, clock=clock)
        result = await svc.run_tick()
    finally:
        await db.close()
    return result, app


def _decision(app: LiotApp) -> Any:
    assert app.last_trace is not None
    return app.last_trace["proactive"]


# ── 네 장면: 조건이 맞을 때 ───────────────────────────────────────────────────

MORNING = "2026-06-01T22:30:00+00:00"  # KST 06-02 07:30
DAYTIME = "2026-06-02T04:00:00+00:00"  # KST 13:00
BEDTIME = "2026-06-02T12:15:00+00:00"  # KST 21:15
EARLY_EVENING = "2026-06-02T11:15:00+00:00"  # KST 20:15


async def test_morning_ventilation_is_breath_advice_sent_immediately() -> None:
    res, app = await _tick(MORNING, weather={"pm25": 8.0})
    assert res is not None
    p = res.proposal
    assert (p.action.capability, p.action.action, p.action.entity_id) == (
        "advice", "ventilate", "advice.home",
    )
    assert p.action.category == "info"
    # Gate1(info) 과 FakeLLM 은 AMBIENT 지만, 라우터가 카탈로그(advice.ventilate = BREATH)로
    # 승격한다 — 최종 단계는 BREATH.
    assert p.risk_tier == RiskTier.BREATH
    assert res.decision == "IMMEDIATE"
    assert _decision(app).reason_ko == (
        "바깥 미세먼지가 8㎍/㎥로 좋아요. 창문을 10분 열어 환기하면 좋겠어요."
    )


async def test_bad_air_purifier_is_breath_sent_immediately() -> None:
    res, app = await _tick(DAYTIME, weather={"pm25": 52.0})
    assert res is not None
    p = res.proposal
    assert (p.action.capability, p.action.action, p.action.entity_id) == (
        "fan", "set_speed", "fan.living_purifier",
    )
    assert p.action.params.get("speed") == "high"
    assert p.action.category == "climate"
    assert p.risk_tier == RiskTier.BREATH
    assert res.decision == "IMMEDIATE"
    assert "52㎍/㎥" in _decision(app).reason_ko


async def test_bedtime_dim_is_ambient_digest() -> None:
    res, app = await _tick(BEDTIME, living_light=True)
    assert res is not None
    p = res.proposal
    assert (p.action.capability, p.action.action, p.action.entity_id) == (
        "light", "set_brightness", LIVING_LIGHT,
    )
    assert p.action.params.get("brightness") == 30
    assert p.risk_tier == RiskTier.AMBIENT
    assert res.decision == "DIGEST"
    assert _decision(app).reason_ko.startswith("잘 시간이 다가와요.")


async def test_cloudy_day_desk_light_is_ambient_digest() -> None:
    res, app = await _tick(DAYTIME, weather={"pm25": 20.0, "cloud_cover": 0.85})
    assert res is not None
    p = res.proposal
    assert (p.action.capability, p.action.action, p.action.entity_id) == (
        "light", "turn_on", "light.desk",
    )
    assert p.risk_tier == RiskTier.AMBIENT
    assert res.decision == "DIGEST"
    assert _decision(app).reason_ko == "하늘이 85% 흐려요. 책상 조명을 켜면 눈이 덜 피로해요."


# ── 네 장면: 조건이 하나라도 빠지면 ───────────────────────────────────────────


@pytest.mark.parametrize(
    ("at", "kwargs"),
    [
        (MORNING, {"weather": {"pm25": 16.0}}),  # 아침인데 바깥 공기 보통
        (MORNING, {"weather": {"pm25": 8.0}, "occupied": False}),  # 아무도 없음
        (MORNING, {}),  # 날씨를 모름
        (MORNING, {"weather": {"pm25": 8.0, "degraded": True}}),  # 날씨 조회 실패
        (DAYTIME, {"weather": {"pm25": 35.0}}),  # 미세먼지 '보통' 끝
        (DAYTIME, {"weather": {"pm25": 52.0}, "occupied": False}),  # 재실 아님
        (EARLY_EVENING, {"living_light": True}),  # 저녁이지만 21시대가 아님
        (BEDTIME, {"living_light": False}),  # 21시대지만 조명 꺼짐
        (DAYTIME, {"weather": {"pm25": 20.0, "cloud_cover": 0.79}}),  # 구름 덜 많음
    ],
)
async def test_scene_does_not_fire_outside_its_conditions(at: str, kwargs: dict[str, Any]) -> None:
    res, _ = await _tick(at, **kwargs)
    assert res is None


async def test_bedtime_reads_kst_hour_not_utc() -> None:
    # 12:15Z 는 UTC 로는 낮(12시)이고 KST 로는 21:15 — 집 시간대로 판정해야 발의된다.
    res, _ = await _tick(BEDTIME, living_light=True)
    assert res is not None and res.proposal.action.action == "set_brightness"


async def test_reason_never_reaches_the_ntfy_payload() -> None:
    res, app = await _tick(MORNING, weather={"pm25": 8.0})
    assert res is not None
    payload = Notifier(secret=b"t" * 32, clock=app.clock).sanitize(res.proposal, "n")
    text = json.dumps(payload, ensure_ascii=False)
    assert "미세먼지" not in text and "환기" not in text
    assert "reason" not in " ".join(payload)


# ── 규칙 파일 ────────────────────────────────────────────────────────────────


def test_shipped_rules_hold_the_four_scenes_and_day_cap_four() -> None:
    rs = load_proactive_rulesets()
    ids = [r.rule_id for r in rs.rules]
    assert ids[:4] == ["morning-ventilate", "bad-air-purifier", "bedtime-dim", "cloudy-desk-light"]
    assert rs.limits.max_per_day == 4
    assert rs.limits.max_per_bucket == 1
    assert all(r.reason_ko for r in rs.rules[:4])


@pytest.mark.parametrize(
    ("cap", "action", "target"),
    [("lock", "unlock", "lock.front_door"), ("valve", "open", "switch.gas_valve"),
     ("cover", "open", "cover.living_room")],
)
def test_opening_actions_are_still_undeclarable(tmp_path, cap: str, action: str, target: str) -> None:
    path = tmp_path / "bad.yaml"
    path.write_text(
        f"rules:\n  - id: bad\n    match: {{time_bucket: day}}\n"
        f"    propose: {{target: {target}, capability: {cap}, action: {action}}}\n",
        encoding="utf-8",
    )
    with pytest.raises(ProactiveRuleError, match="proactive-allowed"):
        load_proactive_ruleset(path)


@pytest.mark.parametrize(
    ("extra", "message"),
    [
        ("hours: [24]", "hours"),
        ("hours: []", "hours"),
        ("hours: [true]", "hours"),
        ("reason_ko: '{device} 를 켜요'", "placeholders"),
        ("reason_ko: '{pm25'", "malformed"),
        ("reason_ko: 3", "string"),
    ],
)
def test_hours_and_reason_are_validated_at_load(tmp_path, extra: str, message: str) -> None:
    path = tmp_path / "bad.yaml"
    path.write_text(
        "rules:\n  - id: bad\n    match: {time_bucket: day}\n"
        f"    {extra}\n"
        "    propose: {target: light.living_room, capability: light, action: turn_off}\n",
        encoding="utf-8",
    )
    with pytest.raises(ProactiveRuleError, match=message):
        load_proactive_ruleset(path)


# ── 권유는 실행 대상이 아니다 ────────────────────────────────────────────────

ADVICE = CandidateAction(
    capability="advice", action="ventilate", params={}, entity_id="advice.home", category="info"
)


def test_policy_never_executes_advice_even_if_whitelisted() -> None:
    for extra in (None, frozenset({("advice", "ventilate")})):
        pol = ExecutionPolicy(enabled=True, extra_executable=extra)
        for tier in RiskTier:
            assert pol.decide(ADVICE, tier) is ExecDecision.NOTIFY_ONLY


def test_policy_refuses_advice_before_reading_the_whitelist() -> None:
    # 방어 심층: 실효 화이트리스트에 권유가 (어떤 경로로든) 섞여도 정책은 먼저 거부한다.
    pol = ExecutionPolicy(enabled=True)
    pol._comfort = pol._comfort | {("advice", "ventilate")}
    assert pol.decide(ADVICE, RiskTier.BREATH) is ExecDecision.NOTIFY_ONLY


def test_effective_comfort_drops_advice_from_extensions() -> None:
    assert ("advice", "ventilate") not in effective_comfort(frozenset({("advice", "ventilate")}))
    assert ("fan", "set_speed") in effective_comfort(frozenset({("fan", "set_speed")}))


async def test_executors_refuse_advice_before_any_call() -> None:
    calls: list[tuple[str, str]] = []

    async def call(domain: str, service: str, entity_id: str, data: dict[str, Any]) -> None:
        calls.append((domain, service))

    extra = frozenset({("advice", "ventilate")})
    for executor in (FakeExecutor(extra_executable=extra), HAExecutor(call, extra_executable=extra)):
        with pytest.raises(ExecutionDenied, match="not an executable capability"):
            await executor.execute(ADVICE)
    assert calls == []


def test_registry_keeps_advice_proposable_but_not_executable() -> None:
    reg = RuntimeRegistry()
    assert ("advice", "ventilate") in reg.proactive_allowed()
    extras = reg.executable_extras()
    assert not any(cap == "advice" for cap, _ in extras)
    assert ("fan", "set_speed") in extras  # 공기청정기 세기는 (옵트인 시) 실행 가능한 컴포트


def test_execution_invariants_still_hold() -> None:
    assert_execution_gated()
