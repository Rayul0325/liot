"""Phase 3 — 선호 임베딩 개인화 루프(유튜브식 온라인 EMA + 시간감쇠).

- apply_signal: 수락↑ / 거부↓ / 무시·만료 약↓.
- apply_decay: 갱신 없는 셀은 중립으로 감쇠.
- EmbedderFeedbackStore: 셀 값을 선호 신호로 공급, 강한 거절은 우선.
- 임베딩은 evaluator PREF 항만 움직이고 safety_risk(catalog)는 불변.
- 서비스 통합: 수락 → 셀↑ → 다음 틱 임베더가 개인화 반영.
"""

from __future__ import annotations

from liot.core.clock import Clock
from liot.core.contracts import (
    ActionCandidate,
    CandidateAction,
    CandidateSet,
    SensorReading,
    compute_snapshot_hash,
    make_noop_candidate,
)
from liot.db.database import init_db
from liot.layers.l1_sensing.normalizer import normalize
from liot.layers.l2_context.evaluator import CandidateEvaluator
from liot.layers.l2_context.feature_vector import (
    NEUTRAL_PREF,
    EmbedderFeedbackStore,
    FakePreferenceEmbedder,
    apply_decay,
    apply_signal,
    cell_key,
    load_cells,
)
from liot.layers.l2_context.feedback_store import REJECTED_SIGNAL, InMemoryFeedbackStore
from liot.layers.l2_context.proactive import ProactiveGenerator
from liot.layers.l2_context.proactive_rules import load_proactive_ruleset
from liot.layers.l4_execution.device_catalog import load_device_catalog
from liot.app import LiotApp
from liot.service import LiotService

DAY = "2026-06-02T12:00:00+00:00"
FACTS = {"time_bucket": "day", "occupancy": "vacant"}
LIGHT = CandidateAction(
    capability="light",
    action="turn_off",
    params={},
    entity_id="light.living_room",
    category="light",
)
KEY = cell_key("day", "vacant", LIGHT)


# ── 순수 함수 ──────────────────────────────────────────────────────────


def test_apply_signal_accept_raises_dismiss_lowers():
    up = apply_signal({}, KEY, "accepted")
    assert up[KEY] > NEUTRAL_PREF
    up2 = apply_signal(up, KEY, "accepted")
    assert up2[KEY] > up[KEY]  # 반복 수락 → 계속 상승
    down = apply_signal({}, KEY, "dismissed")
    assert down[KEY] < NEUTRAL_PREF


def test_apply_signal_expired_is_weak_negative():
    d = apply_signal({KEY: 0.5}, KEY, "expired")
    assert 0.30 <= d[KEY] < 0.5  # 약한 비선호(0.30 쪽으로 소폭)


def test_apply_decay_pulls_toward_neutral():
    high = {KEY: 0.95}
    decayed = apply_decay(high, steps=30)
    assert NEUTRAL_PREF < decayed[KEY] < 0.95  # 중립으로 이동
    # exclude 된 셀은 유지
    kept = apply_decay({KEY: 0.95}, steps=30, exclude=frozenset({KEY}))
    assert kept[KEY] == 0.95


# ── 어댑터 + 안전 불변 ──────────────────────────────────────────────────


def test_embedder_feedback_store_uses_cell_but_rejection_wins():
    emb = FakePreferenceEmbedder({KEY: 0.9})
    fb = EmbedderFeedbackStore(emb, FACTS)
    assert fb.preference_signal(LIGHT) == 0.9  # 셀 값 반영
    assert fb.preference_signal(make_noop_candidate()) == NEUTRAL_PREF
    # base(거절 이력)가 강한 거절이면 임베딩보다 우선
    base = InMemoryFeedbackStore(rejected_keys=frozenset({_fbkey(LIGHT)}))
    fb2 = EmbedderFeedbackStore(emb, FACTS, base=base)
    assert fb2.preference_signal(LIGHT) == REJECTED_SIGNAL


def _fbkey(c):
    return f"{c.capability}:{c.action}:{c.entity_id}:{c.category}"


def test_high_cell_raises_pref_but_not_safety():
    catalog = load_device_catalog()
    cs = CandidateSet(
        trace_id="t",
        snapshot_hash=compute_snapshot_hash({"a": 1}),
        candidates=[
            ActionCandidate(candidate=LIGHT, origin="line_a:ml"),
            ActionCandidate(candidate=make_noop_candidate(), origin="noop"),
        ],
        selected_index=1,
    )
    fb = EmbedderFeedbackStore(FakePreferenceEmbedder({KEY: 0.95}), FACTS)
    ev = CandidateEvaluator(catalog=catalog).evaluate(cs, feedback=fb)
    light = [a for a in ev.scored if a.candidate.capability == "light"][0]
    assert light.user_preference_fit == 0.95  # 임베딩이 선호를 구동
    assert light.safety_risk == 0.0  # 안전은 catalog(ambient) — 개인화 무관


# ── 서비스 통합: 개인화 루프 ─────────────────────────────────────────────

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
CATS = {"binary_sensor.living_motion": "motion", "light.living_room": "light"}


def _reading(eid, raw):
    return SensorReading(
        entity_id=eid, source="t", raw_value=raw, norm_value=normalize(raw), ts_utc=DAY
    )


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
        await app.process_reading(_reading(eid, raw), force_snapshot=True)


async def test_accept_personalizes_next_tick(tmp_path):
    db = await init_db(":memory:")
    try:
        app = _app(tmp_path)
        svc = LiotService(app, db, clock=app.clock)
        await _seed(app)
        res = await svc.run_tick()
        assert res is not None
        pid = res.proposal.proposal_id
        # 수락 → 셀 상승·영속
        await svc._apply_pref_signal(pid, "accepted")
        cells = await load_cells(svc.prefs)
        key = svc._prop_cell[pid]
        assert cells[key] > NEUTRAL_PREF
        # 다음 틱: 임베더가 상승한 셀을 로드 → 그 행동 선호↑
        await svc.run_tick()
        assert app._embedder.preference_for(res.proposal.action, app.last_facts) > NEUTRAL_PREF
    finally:
        await db.close()


async def test_dismiss_lowers_cell(tmp_path):
    db = await init_db(":memory:")
    try:
        app = _app(tmp_path)
        svc = LiotService(app, db, clock=app.clock)
        await _seed(app)
        res = await svc.run_tick()
        assert res is not None
        await svc._apply_pref_signal(res.proposal.proposal_id, "dismissed")
        cells = await load_cells(svc.prefs)
        key = svc._prop_cell[res.proposal.proposal_id]
        assert cells[key] < NEUTRAL_PREF
    finally:
        await db.close()
