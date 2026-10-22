"""능동 규칙의 날씨 조건(external:)·후보 해시·거절 키 사전 차단·발의 앞 날씨 조회.

핵심 검증:
  - external: 조건은 닫힌 순서 비교(lt·le·gt·ge)만, 키는 pm25·cloud_cover·outdoor_temp_c 만.
  - 외부 맥락이 없거나 degraded 거나 값이 비면 성립하지 않는다(모르는 날씨로 발의 금지).
  - 후보 집합 해시가 날씨 값을 반영한다. 외부 맥락이 없는 경로의 해시·규칙셋 해시는 예전 그대로.
  - 최근 거절 키에 걸린 규칙은 쿨다운·예산을 쓰기 전에 건너뛴다 — 같은 시간대의 다음 규칙이 발의된다.
  - app.tick 은 날씨를 발의 앞에서 가져오고, 서비스의 거절 스냅샷을 발의에 넘긴다.
"""

from __future__ import annotations

import pytest

from liot.app import LiotApp
from liot.core.clock import Clock
from liot.core.contracts import ExternalContext, SensorReading, compute_snapshot_hash
from liot.layers.l2_context.external_provider import FakeExternalProvider
from liot.layers.l2_context.feedback_store import InMemoryFeedbackStore, candidate_key
from liot.layers.l2_context.proactive import ProactiveGenerator, ProactiveState
from liot.layers.l2_context.proactive_rules import (
    ExternalPredicate,
    ProactiveRuleError,
    load_proactive_ruleset,
)
from liot.layers.l3_judgment.llm_judge import FakeLLM
from liot.layers.l4_execution.device_catalog import load_device_catalog

DAY = "2026-06-02T12:00:00+00:00"  # UTC 시계 — day
TS = DAY
CATS = {"light.living_room": "light", "binary_sensor.living_motion": "motion"}
CUR = {"light.living_room": True, "binary_sensor.living_motion": True}

PM25_RULE = """
version: "1.0.0"
name: ext
limits: {cooldown_minutes: 0, max_per_bucket: 5, max_per_day: 10}
rules:
  - id: clean-air
    match: {time_bucket: day}
    external:
      - {key: pm25, op: le, value: 15}
    propose: {target: light.living_room, capability: light, action: turn_off}
"""


def _load(tmp_path, text: str):
    path = tmp_path / "ext.yaml"
    path.write_text(text, encoding="utf-8")
    return load_proactive_ruleset(path)


def _gen(tmp_path, text: str = PM25_RULE, clock: Clock | None = None) -> ProactiveGenerator:
    return ProactiveGenerator(
        ruleset=_load(tmp_path, text),
        catalog=load_device_catalog(),
        categories=CATS,
        state=ProactiveState(),
        clock=clock or Clock(fixed=DAY),
    )


def _ext(**values: float | bool | None) -> ExternalContext:
    return ExternalContext(fetched_at=TS, **values)  # type: ignore[arg-type]


# ── external: 조건 ───────────────────────────────────────────────────────────


@pytest.mark.parametrize(("pm25", "fires"), [(15.0, True), (3.0, True), (15.5, False)])
def test_external_condition_matches_only_inside_the_band(
    tmp_path, pm25: float, fires: bool
) -> None:
    d = _gen(tmp_path).propose(CUR, external=_ext(pm25=pm25))
    assert (d.candidate is not None) is fires
    if not fires:
        assert d.suppressed_by == "no_match"


@pytest.mark.parametrize(
    "external",
    [None, _ext(degraded=True), _ext(cloud_cover=0.9)],  # 없음 · 조회 실패 · pm25 값 없음
)
def test_unknown_weather_never_matches(tmp_path, external: ExternalContext | None) -> None:
    d = _gen(tmp_path).propose(CUR, external=external)
    assert d.candidate is None
    assert d.suppressed_by == "no_match"


def test_degraded_external_hides_values_from_rules() -> None:
    ctx = _ext(pm25=5.0, cloud_cover=0.9, degraded=True)
    assert ctx.rule_facts() == {"degraded": True}
    assert _ext(pm25=5.0).rule_facts() == {
        "degraded": False, "pm25": 5.0, "cloud_cover": None, "outdoor_temp_c": None,
    }


def test_predicate_refuses_degraded_or_unflagged_values() -> None:
    # 방어 심층: 값이 실려 와도 degraded 표시가 있거나 표시 자체가 없으면 모르는 날씨다.
    p = ExternalPredicate(key="pm25", op="le", value=15.0)
    assert p.holds({"degraded": False, "pm25": 5.0}) is True
    assert p.holds({"degraded": True, "pm25": 5.0}) is False
    assert p.holds({"pm25": 5.0}) is False


def test_external_only_rule_is_allowed(tmp_path) -> None:
    text = """
rules:
  - id: cloudy
    external: [{key: cloud_cover, op: ge, value: 0.8}]
    propose: {target: light.living_room, capability: light, action: turn_on}
"""
    gen = _gen(tmp_path, text)
    assert gen.propose(CUR, external=_ext(cloud_cover=0.8)).candidate is not None
    assert gen.propose(CUR, external=_ext(cloud_cover=0.79)).candidate is None


@pytest.mark.parametrize(
    ("cond", "message"),
    [
        ("{key: humidity, op: le, value: 15}", "not in"),
        ("{key: pm25, op: eq, value: 15}", "closed vocabulary"),
        ("{key: pm25, op: le, value: '15'}", "must be a number"),
        ("{key: pm25, op: le, value: true}", "must be a number"),
        ("{key: pm25, op: le, value: 15, expr: x}", "unknown keys"),
    ],
)
def test_external_condition_loader_rejects(tmp_path, cond: str, message: str) -> None:
    text = f"""
rules:
  - id: bad
    match: {{time_bucket: day}}
    external: [{cond}]
    propose: {{target: light.living_room, capability: light, action: turn_off}}
"""
    with pytest.raises(ProactiveRuleError, match=message):
        _load(tmp_path, text)


@pytest.mark.parametrize("benefit", ["1.5", "-0.1", "true", "'0.5'"])
def test_benefit_must_be_a_unit_number(tmp_path, benefit: str) -> None:
    text = f"""
rules:
  - id: b
    match: {{time_bucket: day}}
    benefit: {benefit}
    propose: {{target: light.living_room, capability: light, action: turn_off}}
"""
    with pytest.raises(ProactiveRuleError, match="benefit"):
        _load(tmp_path, text)


def test_rule_benefit_reaches_the_candidate(tmp_path) -> None:
    text = PM25_RULE.replace("    external:", "    benefit: 0.7\n    external:")
    gen = _gen(tmp_path, text)
    ext = _ext(pm25=5.0)
    decision = gen.propose(CUR, external=ext)
    cset = gen.candidate_set_for(CUR, decision, external=ext)
    rule_cands = [ac for ac in cset.candidates if ac.origin == "rule:clean-air"]
    assert [ac.declared_benefit for ac in rule_cands] == [0.7]


# ── 해시 ─────────────────────────────────────────────────────────────────────


def test_candidate_set_hash_follows_weather(tmp_path) -> None:
    gen = _gen(tmp_path)

    def h(pm25: float) -> str:
        ext = _ext(pm25=pm25)
        return gen.candidate_set_for(CUR, gen.propose(CUR, external=ext), external=ext).snapshot_hash

    assert h(5.0) == h(5.0)
    assert h(5.0) != h(6.0)


def test_hash_without_external_is_unchanged(tmp_path) -> None:
    # 외부 맥락이 없으면 facts 에 external 키가 없다 → 예전 해시 형태 그대로.
    gen = _gen(tmp_path)
    cset = gen.candidate_set_for(CUR, gen.propose(CUR))
    legacy = compute_snapshot_hash(
        {"facts": {"time_bucket": "day", "occupancy": "occupied"}, "cur": dict(CUR)}
    )
    assert cset.snapshot_hash == legacy


def test_ruleset_hash_of_rules_without_new_fields_is_unchanged(tmp_path) -> None:
    # 2026-10-19 이전 코드로 잰 값(출처 식별자 proactive:rule:<id>@<hash8> 가 그대로여야 한다).
    text = """version: "1.0.0"
name: legacy
rules:
  - id: vacant-light-off
    match: {occupancy: vacant}
    state: [{entity: light.living_room, op: eq, value: true}]
    propose: {target: light.living_room, capability: light, action: turn_off}
    ttl_minutes: 15
"""
    assert _load(tmp_path, text).ruleset_hash == (
        "e156f15c4432e78da42bcf4e3c2eada8d51add3a257a71e64d492c6bc7a8e400"
    )


def test_ruleset_hash_changes_with_external_condition(tmp_path) -> None:
    a = _load(tmp_path, PM25_RULE).ruleset_hash
    b = _load(tmp_path, PM25_RULE.replace("value: 15}", "value: 16}")).ruleset_hash
    assert a != b


# ── 거절 키: 예산을 쓰기 전에 건너뛴다 ───────────────────────────────────────

TWO_RULES = """
version: "1.0.0"
name: two
limits: {cooldown_minutes: 60, max_per_bucket: 1, max_per_day: 10}
rules:
  - id: first-light-off
    match: {time_bucket: day}
    propose: {target: light.living_room, capability: light, action: turn_off}
  - id: second-dim
    match: {time_bucket: day}
    propose: {target: light.living_room, capability: light, action: set_brightness,
              params: {brightness: 30}}
"""


def _first_key(gen: ProactiveGenerator) -> str:
    rule = gen.ruleset.rules[0]
    return candidate_key(gen._build_candidate(rule))


def test_rejected_rule_does_not_consume_the_bucket(tmp_path) -> None:
    gen = _gen(tmp_path, TWO_RULES)
    d = gen.propose(CUR, rejected_keys=frozenset({_first_key(gen)}))
    # 거절된 첫 규칙은 건너뛰고, 시간대 한 자리는 다음 규칙이 쓴다.
    assert d.candidate is not None
    assert d.candidate.action == "set_brightness"
    assert gen.state.fired_in_bucket(gen.home_id, gen.clock.local_dt(), "day") == 1


def test_only_rejected_rules_leave_budget_untouched(tmp_path) -> None:
    text = TWO_RULES.split("  - id: second-dim")[0]
    gen = _gen(tmp_path, text)
    d = gen.propose(CUR, rejected_keys=frozenset({_first_key(gen)}))
    assert d.candidate is None
    assert d.suppressed_by == "rejected"
    now = gen.clock.local_dt()
    assert gen.state.fired_today(gen.home_id, now) == 0
    assert not gen.state.cooldown_active(
        gen.home_id, "light.living_room", "turn_off", now, 60
    )


def test_without_rejection_the_first_rule_wins(tmp_path) -> None:
    gen = _gen(tmp_path, TWO_RULES)
    d = gen.propose(CUR)
    assert d.candidate is not None and d.candidate.action == "turn_off"


# ── app.tick 배선 ────────────────────────────────────────────────────────────


def _app(tmp_path, provider: FakeExternalProvider, text: str = PM25_RULE) -> LiotApp:
    clock = Clock(fixed=DAY)
    gen = ProactiveGenerator(
        ruleset=_load(tmp_path, text),
        catalog=load_device_catalog(),
        categories=CATS,
        clock=clock,
    )
    app = LiotApp(
        clock=clock, categories=CATS, llm_backend=FakeLLM(), proactive=gen,
        external_provider=provider,
    )
    for eid, val in CUR.items():
        app.observe_reading(
            SensorReading(entity_id=eid, source="ha", raw_value=val, norm_value=val, ts_utc=DAY),
            reason="seed",
        )
    return app


async def test_tick_reads_weather_before_proposing(tmp_path) -> None:
    provider = FakeExternalProvider(clock=Clock(fixed=DAY), pm25=40.0)
    app = _app(tmp_path, provider)
    assert await app.tick() is None  # 나쁜 공기 → 맑은 공기 규칙 불성립
    provider.set_values(pm25=8.0)
    proposal = await app.tick()
    assert proposal is not None
    snap = app.last_trace["snapshot"] if app.last_trace else None
    assert snap is not None
    assert snap.structured["trigger"]["pm25"] == 8.0


async def test_tick_passes_rejected_snapshot_to_propose(tmp_path) -> None:
    provider = FakeExternalProvider(clock=Clock(fixed=DAY), pm25=8.0)
    app = _app(tmp_path, provider)
    key = _first_key(app.proactive)
    app.feedback_store = InMemoryFeedbackStore(rejected_keys=frozenset({key}))
    assert await app.tick() is None
    assert app.last_candidate_evaluation is None  # 평가까지 가지 않았다
    assert app.proactive.state.fired_today(app.home_id, app.clock.local_dt()) == 0


def test_fake_provider_rejects_unknown_fields() -> None:
    with pytest.raises(ValueError, match="unknown external fields"):
        FakeExternalProvider().set_values(humidity=40)
