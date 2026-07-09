"""Phase 5 — 자연어 지시 오버라이드(대화형).

- 파서: "오늘 밤은 불 계속 켜놔줘" → keep_on(light). "불 꺼놔" → keep_off.
- is_suppressed: keep_on 이 light.turn_off 억제 · 안전 카테고리는 억제 불가.
- apply_directives: 억제 comfort 제거·NoOp 유지.
- app.tick: "불 켜놔줘" 활성 시 vacant+light-on 소등 후보 억제 → 침묵.
- 안전 무관: 어떤 지시도 안전 카테고리 후보를 억제하지 못한다.
"""

from __future__ import annotations

from liot.app import LiotApp
from liot.core.clock import Clock
from liot.core.contracts import (
    ActionCandidate,
    CandidateAction,
    CandidateSet,
    SensorReading,
    compute_snapshot_hash,
    is_noop,
    make_noop_candidate,
)
from liot.layers.l1_sensing.normalizer import normalize
from liot.layers.l2_context.nl_directive import (
    DirectiveStore,
    FakeDirectiveParser,
    UserDirective,
    apply_directives,
    is_suppressed,
)
from liot.layers.l2_context.proactive import ProactiveGenerator
from liot.layers.l2_context.proactive_rules import load_proactive_ruleset
from liot.layers.l4_execution.device_catalog import load_device_catalog

LIGHT_OFF = CandidateAction(
    capability="light",
    action="turn_off",
    params={},
    entity_id="light.living_room",
    category="light",
)
GAS = CandidateAction(
    capability="valve",
    action="open",
    params={},
    entity_id="switch.gas_valve",
    category="gas",
)


# ── 파서 ────────────────────────────────────────────────────────────────


def test_parser_keep_on_and_keep_off():
    p = FakeDirectiveParser()
    on = p.parse("오늘 밤은 불 계속 켜놔줘")
    assert len(on) == 1 and on[0].capability == "light" and on[0].constraint == "keep_on"
    off = p.parse("불 꺼놔")
    assert off[0].constraint == "keep_off"
    assert p.parse("") == []
    assert p.parse("날씨 어때") == []  # 불 관련 아님


# ── 억제 게이트 ─────────────────────────────────────────────────────────


def test_keep_on_suppresses_turn_off():
    dirs = [UserDirective(capability="light", constraint="keep_on")]
    assert is_suppressed(LIGHT_OFF, dirs) is not None


def test_safety_never_suppressed():
    # 어떤 지시로도 안전 카테고리 후보는 억제되지 않는다.
    for d in (
        UserDirective(capability="valve", constraint="forbid"),
        UserDirective(capability="", constraint="forbid"),  # 전역 forbid
        UserDirective(capability="valve", constraint="keep_off"),
    ):
        assert is_suppressed(GAS, [d]) is None


def test_apply_directives_keeps_noop_removes_suppressed():
    cs = CandidateSet(
        trace_id="t",
        snapshot_hash=compute_snapshot_hash({"a": 1}),
        candidates=[
            ActionCandidate(candidate=LIGHT_OFF, origin="rule:x"),
            ActionCandidate(candidate=make_noop_candidate(), origin="noop"),
        ],
        selected_index=1,
    )
    out = apply_directives(cs, [UserDirective(capability="light", constraint="keep_on")])
    assert len(out.candidates) == 1
    assert is_noop(out.candidates[0].candidate)  # 소등 억제 → NoOp 만


def test_directive_store_add_active_clear():
    s = DirectiveStore()
    s.add_text("불 켜놔줘")
    assert len(s.active()) == 1
    s.clear()
    assert s.active() == []


# ── app 통합 ────────────────────────────────────────────────────────────

DAY = "2026-06-02T12:00:00+00:00"
CATS = {"binary_sensor.living_motion": "motion", "light.living_room": "light"}
COMFORT_YAML = """
version: "1.0.0"
name: t
limits: {cooldown_minutes: 0, max_per_bucket: 9, max_per_day: 20}
rules:
  - id: vacant-light-off
    match: {occupancy: vacant}
    state: [{entity: light.living_room, op: eq, value: true}]
    propose: {target: light.living_room, capability: light, action: turn_off}
    ttl_minutes: 15
"""


def _app(tmp_path):
    clock = Clock(fixed=DAY)
    p = tmp_path / "t.yaml"
    p.write_text(COMFORT_YAML, encoding="utf-8")
    gen = ProactiveGenerator(
        ruleset=load_proactive_ruleset(p),
        catalog=load_device_catalog(),
        categories=CATS,
        clock=clock,
    )
    return LiotApp(clock=clock, categories=CATS, proactive=gen)


async def _seed(app):
    for eid, raw in [("binary_sensor.living_motion", "off"), ("light.living_room", "on")]:
        await app.process_reading(
            SensorReading(
                entity_id=eid,
                source="t",
                raw_value=raw,
                norm_value=normalize(raw),
                ts_utc=DAY,
            ),
            force_snapshot=True,
        )


async def test_directive_keep_on_silences_autonomous_off(tmp_path):
    app = _app(tmp_path)
    await _seed(app)
    # 지시 없으면 소등 제안이 나온다.
    assert (await app.tick()) is not None
    # "불 켜놔줘" → 자율 소등 억제 → 침묵.
    app.set_directives(FakeDirectiveParser().parse("오늘 밤은 불 계속 켜놔줘"))
    assert (await app.tick()) is None
