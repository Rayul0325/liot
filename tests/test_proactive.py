"""능동 생성기(Phase2) 검증 — 불변식 7 + Proactive-Safe-Silent.

검증 축:
  1) 로더 deny-by-default: 금지 (capability, action) 은 선언 자체가 거부됨.
  2) 생성기: 발의/침묵(no_match·쿨다운·예산·안전타깃·상한) 결정론.
  3) app e2e: tick() → 제안(BREATH 이하·출처·만료) / LLM 폭주 → 침묵,
     단 안전 경로는 영향 없음(불변식 6 보존).
  4) egress: LLM payload 에 raw entity_id·__origin__ 불출(백스톱 포함).
  5) occupancy fact 가 실제로 매칭됨(죽은 매치 키 부활 회귀).
"""

from __future__ import annotations

import json

import pytest

from liot.app import LiotApp
from liot.core.clock import Clock
from liot.core.contracts import CandidateAction, ContextSnapshot, SensorReading
from liot.core.egress_gate import Destination, emit_external
from liot.core.invariants import EgressViolation, InvariantError
from liot.core.risk import RiskTier
from liot.layers.l1_sensing.normalizer import normalize
from liot.layers.l2_context.context_builder import ContextBuilder, derive_occupancy
from liot.layers.l2_context.proactive import (
    PROACTIVE_TIER_CEILING,
    ProactiveGenerator,
    ProactiveState,
    assert_proactive_candidate,
    validate_proactive_candidate,
)
from liot.layers.l2_context.proactive_rules import (
    ProactiveRuleError,
    derive_proactive_allowed,
    load_proactive_ruleset,
)
from liot.layers.l3_judgment.llm_judge import LLMResult
from liot.layers.l3_judgment.ollama_backend import OllamaBackend
from liot.layers.l4_execution.device_catalog import load_device_catalog
from liot.registry import RuntimeRegistry

DAY = "2026-06-02T12:00:00+00:00"  # time_bucket 'day'
CLOCK = Clock(fixed=DAY)
CATS = {
    "binary_sensor.living_motion": "motion",
    "light.living_room": "light",
    "sensor.living_temperature": "temperature",
    "binary_sensor.kitchen_gas": "gas",
}

BASE_YAML = """
version: "1.0.0"
name: t
limits: {cooldown_minutes: 60, max_per_bucket: 5, max_per_day: 10}
rules:
  - id: vacant-light-off
    match: {occupancy: vacant}
    state: [{entity: light.living_room, op: eq, value: true}]
    propose: {target: light.living_room, capability: light, action: turn_off}
    ttl_minutes: 15
"""


def _ruleset(tmp_path, text=BASE_YAML, name="t.yaml"):
    p = tmp_path / name
    p.write_text(text, encoding="utf-8")
    return load_proactive_ruleset(p)


def _generator(tmp_path, *, text=BASE_YAML, state=None, clock=CLOCK, categories=CATS):
    return ProactiveGenerator(
        ruleset=_ruleset(tmp_path, text),
        catalog=load_device_catalog(),
        categories=categories,
        state=state,
        clock=clock,
    )


def _reading(eid: str, raw: object) -> SensorReading:
    return SensorReading(
        entity_id=eid,
        source="test",
        raw_value=raw,
        norm_value=normalize(raw),
        ts_utc=DAY,
        home_id="singleton",
    )


# ==========================================================================
# 1) 로더 deny-by-default
# ==========================================================================
@pytest.mark.parametrize(
    "cap,act",
    [("lock", "unlock"), ("valve", "open"), ("cover", "open"), ("oven", "turn_on")],
)
def test_loader_rejects_forbidden_actions(tmp_path, cap, act) -> None:
    text = f"""
rules:
  - id: evil
    match: {{occupancy: vacant}}
    propose: {{target: x.y, capability: {cap}, action: {act}}}
"""
    with pytest.raises(ProactiveRuleError, match="proactive-allowed"):
        _ruleset(tmp_path, text)


def test_loader_rejects_unknown_predicate_op(tmp_path) -> None:
    text = """
rules:
  - id: bad-op
    state: [{entity: light.a, op: regex, value: ".*"}]
    propose: {target: light.a, capability: light, action: turn_off}
"""
    with pytest.raises(ProactiveRuleError, match="closed vocabulary"):
        _ruleset(tmp_path, text)


def test_loader_rejects_non_scalar_and_unknown_match_key(tmp_path) -> None:
    with pytest.raises(ProactiveRuleError, match="scalar"):
        _ruleset(
            tmp_path,
            """
rules:
  - id: bad-val
    state: [{entity: light.a, op: eq, value: {nested: 1}}]
    propose: {target: light.a, capability: light, action: turn_off}
""",
        )
    with pytest.raises(ProactiveRuleError, match="disallowed match keys"):
        _ruleset(
            tmp_path,
            """
rules:
  - id: bad-key
    match: {weather: rainy}
    propose: {target: light.a, capability: light, action: turn_off}
""",
        )


def test_loader_rejects_unconditional_rule(tmp_path) -> None:
    # 조건 없는 능동 규칙 = 구조적 잔소리 → 거부.
    with pytest.raises(ProactiveRuleError, match="at least one"):
        _ruleset(
            tmp_path,
            """
rules:
  - id: nag
    propose: {target: light.a, capability: light, action: turn_off}
""",
        )


def test_ruleset_hash_deterministic_and_limits(tmp_path) -> None:
    a = _ruleset(tmp_path, name="a.yaml")
    b = _ruleset(tmp_path, name="b.yaml")
    assert a.ruleset_hash == b.ruleset_hash  # 같은 내용 → 같은 해시
    assert a.limits.cooldown_minutes == 60
    assert a.limits.max_per_bucket == 5


# ==========================================================================
# 2) 생성기 결정론 — 발의와 침묵
# ==========================================================================
def test_vacant_light_on_fires_turn_off(tmp_path) -> None:
    gen = _generator(tmp_path)
    cur = {"binary_sensor.living_motion": False, "light.living_room": True}
    d = gen.propose(cur)
    assert d.candidate is not None
    assert d.candidate.action == "turn_off"
    assert d.candidate.entity_id == "light.living_room"
    assert d.candidate.category == "light"
    assert d.candidate.params["__origin__"].startswith("proactive:rule:vacant-light-off@")
    assert d.suppressed_by is None
    assert d.ttl_minutes == 15


def test_occupied_is_silent(tmp_path) -> None:
    gen = _generator(tmp_path)
    cur = {"binary_sensor.living_motion": True, "light.living_room": True}
    d = gen.propose(cur)
    assert d.candidate is None
    assert d.suppressed_by == "no_match"


def test_cooldown_suppresses_second_fire(tmp_path) -> None:
    gen = _generator(tmp_path)
    cur = {"binary_sensor.living_motion": False, "light.living_room": True}
    assert gen.propose(cur).candidate is not None
    d2 = gen.propose(cur)
    assert d2.candidate is None
    assert d2.suppressed_by == "cooldown"


def test_budget_suppresses(tmp_path) -> None:
    text = BASE_YAML.replace(
        "{cooldown_minutes: 60, max_per_bucket: 5, max_per_day: 10}",
        "{cooldown_minutes: 0, max_per_bucket: 1, max_per_day: 10}",
    )
    gen = _generator(tmp_path, text=text)
    cur = {"binary_sensor.living_motion": False, "light.living_room": True}
    assert gen.propose(cur).candidate is not None
    d2 = gen.propose(cur)
    assert d2.candidate is None
    assert d2.suppressed_by == "budget"


def test_safety_target_refused(tmp_path) -> None:
    # climate.living_room 은 기본 카탈로그에서 high_temp(안전) 분류 →
    # 컴포트 생성기는 손대지 않는다(침묵).
    text = """
rules:
  - id: heat-up
    match: {time_bucket: day}
    propose:
      target: climate.living_room
      capability: climate
      action: set_temperature
      params: {temperature: 2}
"""
    gen = _generator(tmp_path, text=text, categories={})  # 카탈로그 분류 사용
    d = gen.propose({"sensor.living_temperature": 15.0})
    assert d.candidate is None
    assert d.suppressed_by == "safety_target"


def test_tier_ceiling_refused_for_lowering_temp(tmp_path) -> None:
    # 집이 climate 를 컴포트로 분류해도, 온도 '내리기'는 카탈로그가
    # DUSK 로 보므로 BREATH 상한에 걸려 침묵한다(방향 비대칭 존중).
    text = """
rules:
  - id: cool-down
    match: {time_bucket: day}
    propose:
      target: climate.living_room
      capability: climate
      action: set_temperature
      params: {temperature: -2}
"""
    gen = _generator(
        tmp_path, text=text, categories={"climate.living_room": "climate"}
    )
    d = gen.propose({"sensor.living_temperature": 30.0})
    assert d.candidate is None
    assert d.suppressed_by == "tier_ceiling"


def test_state_shared_across_generators(tmp_path) -> None:
    # 쿨다운 상태가 숨은 앱 속성이 아니라 주입 저장소에 있음을 증명:
    # A 가 발화하면 같은 상태를 공유하는 B 는 침묵한다.
    shared = ProactiveState()
    gen_a = _generator(tmp_path, state=shared)
    gen_b = _generator(tmp_path, state=shared)
    cur = {"binary_sensor.living_motion": False, "light.living_room": True}
    assert gen_a.propose(cur).candidate is not None
    d = gen_b.propose(cur)
    assert d.candidate is None
    assert d.suppressed_by in ("cooldown", "budget")


def test_validator_and_boundary_assert() -> None:
    bad = CandidateAction(
        capability="lock", action="unlock", params={}, entity_id="lock.front_door",
        category="lock",
    )
    assert validate_proactive_candidate(bad, load_device_catalog()) == "not_allowed"
    with pytest.raises(InvariantError, match="invariant 7"):
        assert_proactive_candidate(bad, load_device_catalog())


def test_capability_mismatch_refused_for_mislabeled_safety_entity(tmp_path) -> None:
    # V5 방어심층: owner 가 가스밸브를 'light' 로 거짓 분류 + (light,turn_off)
    # 규칙으로 가리켜도, 카탈로그 capability('valve')와 불일치라 거부된다.
    text = """
rules:
  - id: lie
    match: {time_bucket: day}
    propose: {target: switch.gas_valve, capability: light, action: turn_off}
"""
    gen = _generator(tmp_path, text=text, categories={"switch.gas_valve": "light"})
    d = gen.propose({"sensor.living_temperature": 21.0})
    assert d.candidate is None
    assert d.suppressed_by == "capability_mismatch"


def test_capability_match_allows_legitimate_climate_override(tmp_path) -> None:
    # 정당한 카테고리 오버라이드(고온계→climate)는 capability 가 일치하므로
    # 보존된다 — capability 검사가 정당한 재분류를 깨지 않음(거짓양성 0).
    text = """
rules:
  - id: warm
    match: {time_bucket: evening}
    state: [{entity: sensor.living_temperature, op: lt, value: 18}]
    propose:
      target: climate.living_room
      capability: climate
      action: set_temperature
      params: {temperature: 2}
"""
    gen = _generator(
        tmp_path,
        text=text,
        clock=Clock(fixed="2026-06-02T18:00:00+00:00"),  # evening
        categories={"climate.living_room": "climate"},
    )
    d = gen.propose({"sensor.living_temperature": 15.0})
    assert d.candidate is not None
    assert d.candidate.entity_id == "climate.living_room"


def test_conservative_limits_merge_tighter_wins(tmp_path) -> None:
    from liot.layers.l2_context.proactive_rules import load_proactive_rulesets

    # comfort: 60/1/3 (보수). loose 파일 추가해도 throttle 은 안 느슨해진다.
    (tmp_path / "comfort.yaml").write_text(
        "limits: {cooldown_minutes: 60, max_per_bucket: 1, max_per_day: 3}\n"
        "rules: [{id: a, match: {occupancy: vacant}, "
        "propose: {target: light.x, capability: light, action: turn_off}}]\n",
        encoding="utf-8",
    )
    (tmp_path / "aaa_loose.yaml").write_text(
        "limits: {cooldown_minutes: 0, max_per_bucket: 99, max_per_day: 99}\n"
        "rules: [{id: b, match: {occupancy: vacant}, "
        "propose: {target: light.y, capability: light, action: turn_off}}]\n",
        encoding="utf-8",
    )
    rs = load_proactive_rulesets(tmp_path)
    assert rs.limits.cooldown_minutes == 60  # max(0,60)
    assert rs.limits.max_per_bucket == 1  # min(99,1)
    assert rs.limits.max_per_day == 3  # min(99,3)


def test_origin_param_never_changes_catalog_risk() -> None:
    cat = load_device_catalog()
    base = cat.risk_for("cover", "open", {})
    with_origin = cat.risk_for("cover", "open", {"__origin__": "proactive:rule:x@y"})
    assert base == with_origin == RiskTier.DUSK


# ==========================================================================
# 3) app e2e — tick() 능동 제안과 침묵
# ==========================================================================
def _app(tmp_path, *, text=BASE_YAML, llm_backend=None) -> LiotApp:
    gen = ProactiveGenerator(
        ruleset=_ruleset(tmp_path, text),
        catalog=load_device_catalog(),
        categories=CATS,
        clock=CLOCK,
    )
    return LiotApp(clock=CLOCK, categories=CATS, proactive=gen, llm_backend=llm_backend)


async def _seed_vacant_light_on(app: LiotApp) -> None:
    # 관찰은 침묵해야 한다(센서/액추에이터 분리 회귀) — 캐시만 갱신된다.
    for eid, raw in [
        ("binary_sensor.living_motion", "off"),
        ("light.living_room", "on"),
    ]:
        proposal = await app.process_reading(_reading(eid, raw), force_snapshot=True)
        assert proposal is None, f"관찰 {eid} 이 제안을 만들면 안 됨"


async def test_tick_produces_proactive_proposal(tmp_path) -> None:
    app = _app(tmp_path)
    await _seed_vacant_light_on(app)
    proposal = await app.tick()
    assert proposal is not None, "능동 제안이 생성돼야 함"
    assert proposal.action.action == "turn_off"
    assert proposal.action.entity_id == "light.living_room"
    assert RiskTier.from_str(proposal.risk_tier) <= PROACTIVE_TIER_CEILING
    assert proposal.required_confirmation in ("none", "light")
    assert proposal.action.params["__origin__"].startswith("proactive:rule:")
    assert proposal.expires_at == "2026-06-02T12:15:00+00:00"  # ttl 15분, 결정론
    assert proposal.perishable is False  # L4 예산 우회 금지


async def test_tick_silent_when_occupied(tmp_path) -> None:
    app = _app(tmp_path)
    await app.process_reading(_reading("binary_sensor.living_motion", "on"))
    await app.process_reading(_reading("light.living_room", "on"))
    assert await app.tick() is None  # Proactive-Safe-Silent


class _EmberLLM:
    """항상 EMBER 로 승격하려는 적대적/폭주 LLM 스텁."""

    model_id = "ember-llm"

    def judge_raw(self, candidate, ctx, gate1_floor):
        return LLMResult(
            tier=RiskTier.EMBER, confidence=0.99, rationale="모두 위험!", model_id=self.model_id
        )


async def test_llm_escalation_capped_to_silence_but_safety_unaffected(tmp_path) -> None:
    app = _app(tmp_path, llm_backend=_EmberLLM())
    await _seed_vacant_light_on(app)
    # 능동: LLM 이 EMBER 로 폭주 → BREATH 상한 → 침묵(인젝션 최악 = 침묵).
    assert await app.tick() is None
    # 안전: 같은 앱에서 가스 이벤트는 여전히 EMBER 제안(불변식 6 무손상).
    p = await app.process_reading(
        _reading("binary_sensor.kitchen_gas", "on"), force_snapshot=True
    )
    assert p is not None and p.risk_tier == RiskTier.EMBER


async def test_explicit_and_safety_paths_carry_origin(tmp_path) -> None:
    app = _app(tmp_path)
    p1 = await app.process_reading(
        _reading("light.living_room", "off"), action="turn_on", force_snapshot=True
    )
    assert p1 is not None
    assert p1.action.params["__origin__"] == "explicit:external"
    p2 = await app.process_reading(
        _reading("binary_sensor.kitchen_gas", "on"), force_snapshot=True
    )
    assert p2 is not None
    assert p2.action.params["__origin__"] == "safety:auto"


def test_snapshot_hash_pins_proactive_decision() -> None:
    # 같은 상태라도 트리거(=능동 결정)가 다르면 snapshot_hash 가 다르다 —
    # 판단된 후보가 스냅샷에 고정된다(불변식 4).
    builder = ContextBuilder(clock=CLOCK, categories=CATS)
    cur = {"light.living_room": True}
    t1 = {"kind": "proactive", "origin": "proactive:rule:a@x", "action": "turn_off"}
    t2 = {"kind": "proactive", "origin": "proactive:rule:b@x", "action": "turn_on"}
    s1 = builder.build(cur, t1)
    s2 = builder.build(cur, t2)
    s1b = builder.build(cur, t1)
    assert s1.snapshot_hash != s2.snapshot_hash
    assert s1.snapshot_hash == s1b.snapshot_hash


# ==========================================================================
# 4) egress — LLM payload 에 raw 식별자·출처 불출
# ==========================================================================
class _CaptureTransport:
    def __init__(self) -> None:
        self.prompts: list[str] = []

    def generate(self, model: str, prompt: str, *, timeout_s: float) -> str:
        self.prompts.append(prompt)
        return json.dumps({"tier": "breath", "confidence": 0.9, "rationale": "ok"})


def _ctx() -> ContextSnapshot:
    return ContextSnapshot(
        snapshot_id="s1",
        trace_id="t1",
        trigger={"kind": "proactive"},
        structured={"time_bucket": "day"},
        nl_summary="[day] 2 signals; changed=0; safety=none; trigger=proactive",
        summary_template_sha="x",
        created_at=DAY,
    )


def test_llm_payload_excludes_origin_and_raw_entity() -> None:
    transport = _CaptureTransport()
    backend = OllamaBackend("gemma-test", transport)
    cand = CandidateAction(
        capability="light",
        action="turn_off",
        params={"__origin__": "proactive:rule:vacant-light-off@abcd1234", "brightness": 0},
        entity_id="light.living_room",
        category="light",
    )
    result = backend.judge_raw(cand, _ctx(), RiskTier.AMBIENT)
    assert result.tier == RiskTier.BREATH
    prompt = transport.prompts[0]
    assert "light.living_room" not in prompt  # raw entity_id 불출(가명화)
    assert "__origin__" not in prompt  # 출처 메타 불출(params 투영)
    assert "proactive:rule" not in prompt
    assert "brightness" in prompt  # 방향성 스칼라는 통과(화이트리스트)


def test_egress_llm_backstop_rejects_entity_shaped_strings() -> None:
    # 비식별 키 아래로 raw entity_id 가 새려 하면 송신 자체가 거부된다.
    with pytest.raises(EgressViolation, match="entity-shaped"):
        emit_external(
            Destination.LLM,
            {"note": "please check light.living_room now"},
            trace_id="t",
        )


@pytest.mark.parametrize(
    "leak", ["Light.Living_Room", "Sensor.Kitchen", "LIGHT.LIVING_ROOM", "Climate.Bedroom"]
)
def test_egress_backstop_is_case_insensitive(leak) -> None:
    # 대문자/혼합 케이스 entity-id 도 비식별 키 아래에서 차단되어야 한다(회귀).
    with pytest.raises(EgressViolation, match="entity-shaped"):
        emit_external(Destination.LLM, {"note": f"check {leak}"}, trace_id="t")


def test_egress_alias_tokens_survive_case_insensitive_backstop() -> None:
    # 가명 토큰(밑줄, 점 없음)은 케이스 무시 백스톱에도 안 걸린다.
    out = emit_external(
        Destination.LLM,
        {"summary": "ROOM_A PERSON_1 DEVICE_1 changed", "n": 3},
        trace_id="t",
    )
    assert out["summary"] == "ROOM_A PERSON_1 DEVICE_1 changed"


def test_string_params_other_than_direction_vocab_are_dropped() -> None:
    transport = _CaptureTransport()
    backend = OllamaBackend("gemma-test", transport)
    cand = CandidateAction(
        capability="light", action="set_brightness",
        params={
            "direction": "Light.Living_Room",  # 어휘 밖 문자열 → 투영 안 됨
            "note": "sensor.kitchen",          # 비허용 키 → 투영 안 됨
            "temperature": 2,                   # 수치 → 투영
            "brightness": "80",                # 수치 키지만 문자열 → 투영 안 됨
        },
        entity_id="light.living_room", category="light",
    )
    backend.judge_raw(cand, _ctx(), RiskTier.AMBIENT)
    prompt = transport.prompts[0]
    assert "Light.Living_Room" not in prompt and "sensor.kitchen" not in prompt
    assert "temperature" in prompt  # 수치만 통과
    # brightness 문자열은 드롭(수치 키지만 문자열이라 거부).
    assert '"brightness"' not in prompt


def test_direction_vocab_value_is_projected_normalized() -> None:
    transport = _CaptureTransport()
    backend = OllamaBackend("gemma-test", transport)
    cand = CandidateAction(
        capability="cover", action="close", params={"direction": "Close"},
        entity_id="cover.x", category="curtain",
    )
    backend.judge_raw(cand, _ctx(), RiskTier.AMBIENT)
    assert '"direction": "close"' in transport.prompts[0]  # 정규화되어 통과


# ==========================================================================
# 5) occupancy fact — 죽은 매치 키 부활
# ==========================================================================
def test_derive_occupancy_states() -> None:
    cats = {"binary_sensor.m": "motion"}
    assert derive_occupancy({}, cats) == "unknown"
    assert derive_occupancy({"sensor.t": 21.0}, cats) == "unknown"
    assert derive_occupancy({"binary_sensor.m": True}, cats) == "occupied"
    assert derive_occupancy({"binary_sensor.m": False}, cats) == "vacant"


def test_occupancy_lands_in_gate1_facts() -> None:
    from liot.layers.l3_judgment.guardrail import Guardrail

    builder = ContextBuilder(clock=CLOCK, categories=CATS)
    cur = {"binary_sensor.living_motion": False, "light.living_room": True}
    ctx = builder.build(cur, {"kind": "tick"})
    assert ctx.structured["occupancy"] == "vacant"
    cand = CandidateAction(
        capability="light", action="turn_off", params={},
        entity_id="light.living_room", category="light",
    )
    facts = Guardrail()._facts(cand, ctx)
    assert facts["occupancy"] == "vacant"  # occupancy 매치 키가 이제 살아있다


# ==========================================================================
# 6) 카탈로그 파생 허용목록 (P4) — 구 고정 6튜플 허용목록과 동치 봉인
# ==========================================================================
def test_derive_proactive_allowed_equals_shipped_six_tuples() -> None:
    # THE SEAL: 출하 YAML 파생 == 구 고정 6튜플 허용목록. 리팩터의 의미
    # 동일성을 원샷 회귀로 봉인한다(P4 최대 정합성 리스크).
    assert derive_proactive_allowed(load_device_catalog()) == {
        ("light", "turn_on"),
        ("light", "turn_off"),
        ("light", "set_brightness"),
        ("climate", "set_temperature"),
        ("cover", "close"),
        ("notify", "send"),
    }


def test_runtime_breath_action_becomes_proposable() -> None:
    # breath 로 등록한 새 행동은 레지스트리 파생 허용목록에 등장(= 능동 제안 가능).
    reg = RuntimeRegistry()
    reg.add_device(
        entity_id="vacuum.robot", name="로봇청소기", capability="vacuum",
        category="appliance", actions=[{"action": "start", "tier": "breath"}],
    )
    allowed = derive_proactive_allowed(reg.to_catalog())
    assert ("vacuum", "start") in allowed
    assert allowed == reg.proactive_allowed()  # 레지스트리 helper 도 동일 집합


def test_runtime_ember_action_stays_out() -> None:
    # 같은 행동을 ember 로 등록하면 상한(breath)을 넘어 허용목록 밖.
    reg = RuntimeRegistry()
    reg.add_device(
        entity_id="siren.attic", name="다락 사이렌", capability="siren",
        category="appliance", actions=[{"action": "blast", "tier": "ember"}],
    )
    assert ("siren", "blast") not in derive_proactive_allowed(reg.to_catalog())


def test_safety_actuator_capability_never_enters_regardless_of_tier() -> None:
    # lock/valve/oven capability action 은 breath 로 등록해도 능동 허용목록 밖 —
    # 안전 액추에이터는 코드 수정으로만 확장(데이터로 못 넓힘).
    reg = RuntimeRegistry()
    reg.add_device(
        entity_id="oven.kitchen", name="오븐", capability="oven",
        category="appliance", actions=[{"action": "preheat", "tier": "breath"}],
    )
    allowed = derive_proactive_allowed(reg.to_catalog())
    assert ("oven", "preheat") not in allowed  # 신규 등록 oven.preheat(breath) 도 제외
    assert ("lock", "lock") not in allowed  # 출하 lock.lock(breath) 도 제외
    assert ("valve", "close") not in allowed  # 출하 valve.close(breath) 도 제외


def test_loader_accepts_runtime_action_with_injected_allowed(tmp_path) -> None:
    # 로더 allowed 파라미터: 기본(출하)으론 거부, 레지스트리 파생 주입 시 통과.
    reg = RuntimeRegistry()
    reg.add_device(
        entity_id="vacuum.robot", name="로봇청소기", capability="vacuum",
        category="appliance", actions=[{"action": "start", "tier": "breath"}],
    )
    text = """
rules:
  - id: clean
    match: {occupancy: vacant}
    propose: {target: vacuum.robot, capability: vacuum, action: start}
"""
    p = tmp_path / "r.yaml"
    p.write_text(text, encoding="utf-8")
    with pytest.raises(ProactiveRuleError, match="proactive-allowed"):
        load_proactive_ruleset(p)  # 출하 허용목록엔 (vacuum,start) 없음
    rs = load_proactive_ruleset(p, allowed=reg.proactive_allowed())  # 파생 주입 → 통과
    assert rs.rules[0].capability == "vacuum" and rs.rules[0].action == "start"


# ==========================================================================
# 7) 카테고리 리졸버 단일화 (P5/D6) — core.categories 단일 원천 + 재수출 호환
# ==========================================================================
def test_resolve_category_is_single_source() -> None:
    from liot.core.categories import resolve_category as core_resolve
    from liot.layers.l2_context.proactive import resolve_category as proactive_resolve

    # proactive 는 core 를 재수출(같은 객체) — 로직 이중관리 없음.
    assert proactive_resolve is core_resolve
    cat = load_device_catalog()
    # 우선순위: 명시 맵 → 카탈로그 메타 → 도메인/언더스코어 접두사 → unknown.
    assert core_resolve("light.living_room", {"light.living_room": "custom"}, cat) == "custom"
    assert core_resolve("light.living_room", {}, cat) == "ambient"
    assert core_resolve("foo.bar", {}, None) == "foo"
    assert core_resolve("baz", {}, None) == "unknown"


def test_app_and_context_builder_share_resolver() -> None:
    # app._category_of 와 context_builder._category_of 가 동일 체인을 본다(불일치=우회).
    from liot.layers.l2_context.context_builder import _category_of

    app = LiotApp(clock=CLOCK, categories=CATS)
    assert app._category_of("binary_sensor.kitchen_gas") == "gas"
    assert _category_of("binary_sensor.kitchen_gas", CATS) == "gas"
    # 맵에 없으면 도메인 접두사(카탈로그 없는 context_builder 계층).
    assert _category_of("switch.unknown_thing", {}) == "switch"
