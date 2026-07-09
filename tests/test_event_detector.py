"""Phase 4 — 트리거 감지 계층(규칙 상시 + LLM 옵트인).

- RuleEventDetector: 관측 상태 있으면 발화, 없으면 침묵.
- FakeLLMEventDetector: 기본 off, 켜면 발화.
- Composite: 하나라도 발화하면 발화.
- app.tick: 트리거 없음 → propose 이전 침묵(예산 미소모). 새 버스 엣지 없음.
"""

from __future__ import annotations

import os

from liot.app import LiotApp
from liot.core.clock import Clock
from liot.core.contracts import SensorReading
from liot.layers.l1_sensing.normalizer import normalize
from liot.layers.l2_context.event_detector import (
    CompositeEventDetector,
    EventDetector,
    FakeLLMEventDetector,
    RuleEventDetector,
    TriggerDecision,
    build_event_detector,
)
from liot.layers.l2_context.proactive import ProactiveGenerator
from liot.layers.l2_context.proactive_rules import load_proactive_ruleset
from liot.layers.l4_execution.device_catalog import load_device_catalog

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


class _NeverFire:
    source = "test"

    def detect(self, cur, prev=None):
        return TriggerDecision(False, "test-never", "test")


# ── 감지기 단위 ─────────────────────────────────────────────────────────


def test_rule_detector_fires_on_observed_state():
    d = RuleEventDetector()
    assert d.detect({"light.living_room": True}).fire is True
    assert d.detect({}).fire is False


def test_llm_detector_optin_off_by_default():
    assert FakeLLMEventDetector().detect({"x": 1}).fire is False
    assert FakeLLMEventDetector(enabled=True).detect({"x": 1}).fire is True


def test_composite_fires_if_any():
    comp = CompositeEventDetector([FakeLLMEventDetector(False), RuleEventDetector()])
    r = comp.detect({"x": 1})
    assert r.fire is True and r.source == "rule"
    assert comp.detect({}).fire is False  # 둘 다 침묵


def test_build_detector_env_optin(monkeypatch=None):
    # 기본: 규칙만
    os.environ.pop("LIOT_EVENT_LLM", None)
    d = build_event_detector()
    assert isinstance(d, (CompositeEventDetector, EventDetector))
    assert d.detect({"x": 1}).fire is True
    # 옵트인: LLM 켜짐 — 켜도 관측 상태 있으면 발화
    os.environ["LIOT_EVENT_LLM"] = "1"
    try:
        d2 = build_event_detector()
        assert d2.detect({"x": 1}).fire is True
    finally:
        os.environ.pop("LIOT_EVENT_LLM", None)


# ── app 통합 ────────────────────────────────────────────────────────────


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


async def test_no_trigger_silences_without_consuming_budget(tmp_path):
    app = _app(tmp_path)
    await _seed(app)
    app._event_detector = _NeverFire()
    assert await app.tick() is None  # 트리거 없음 → 침묵
    # propose 회계 미소모 — 감지기를 규칙으로 되돌리면 정상 발의된다.
    app._event_detector = RuleEventDetector()
    p = await app.tick()
    assert p is not None
    assert p.action.capability == "light"
