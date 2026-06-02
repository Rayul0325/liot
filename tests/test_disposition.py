"""센서/액추에이터 분리(관찰 vs 제안) 검증 — 알림 피로 방지.

규칙:
  - 제안은 (a) 명시적 action 이 주어졌거나, (b) 안전 임계 카테고리일 때만 생성.
  - 그 외 읽기전용 센서(온도/습도/움직임)·액추에이터 상태변화는 맥락만 갱신,
    제안 없음(None).

이 규칙으로, 실제 HA의 수많은 양성 센서 이벤트가 ember 로 과승격되던 문제를 막는다.
"""

from __future__ import annotations

from liot.app import LiotApp
from liot.core.clock import Clock
from liot.core.contracts import SensorReading
from liot.core.risk import RiskTier
from liot.layers.l1_sensing.normalizer import normalize

CLOCK = Clock(fixed="2026-06-02T12:00:00+00:00")
CATS = {
    "sensor.living_temperature": "temperature",
    "sensor.living_humidity": "humidity",
    "binary_sensor.living_motion": "motion",
    "light.living": "light",  # 액추에이터(관찰)
    "binary_sensor.kitchen_gas": "gas",  # 안전
}


def _r(eid: str, raw: object) -> SensorReading:
    return SensorReading(
        entity_id=eid,
        source="ha",
        raw_value=raw,
        norm_value=normalize(raw),
        ts_utc="2026-06-02T12:00:00+00:00",
        home_id="singleton",
    )


def _app() -> LiotApp:
    return LiotApp(clock=CLOCK, categories=CATS)


async def test_benign_sensors_produce_no_proposal() -> None:
    # force_snapshot=True 로 L2 스냅샷까지 강제 → None 은 disposition 때문임이 분명.
    for eid, raw in [
        ("sensor.living_temperature", "22.5"),
        ("sensor.living_humidity", "55"),
        ("binary_sensor.living_motion", "on"),
    ]:
        app = _app()
        proposal = await app.process_reading(_r(eid, raw), force_snapshot=True)
        assert proposal is None, f"{eid} 가 제안을 만들면 안 됨(맥락만)"


async def test_actuator_observation_without_action_is_silent() -> None:
    # 조명이 스스로 켜진 '관찰'은 제안이 아니다(명시적 action 없음).
    app = _app()
    proposal = await app.process_reading(_r("light.living", "on"), force_snapshot=True)
    assert proposal is None


async def test_safety_sensor_proposes_even_without_action() -> None:
    # 가스(안전)는 명시적 action 없이도 자동 알림 제안(ember).
    app = _app()
    proposal = await app.process_reading(_r("binary_sensor.kitchen_gas", "on"), force_snapshot=True)
    assert proposal is not None
    assert proposal.risk_tier == RiskTier.EMBER


async def test_explicit_action_still_proposes_proactively() -> None:
    # 프로액티브 생성기가 명시적 action 을 주면 양성 카테고리도 제안된다(회귀).
    app = _app()
    proposal = await app.process_reading(
        _r("light.living", "off"), action="turn_on", force_snapshot=True
    )
    assert proposal is not None
    assert proposal.action.action == "turn_on"


async def test_benign_stream_no_alert_fatigue() -> None:
    # 양성 센서 20건 연속 유입 → 제안 0건. 안전 1건만 ember.
    app = _app()
    proposals = []
    for i in range(20):
        r = _r("sensor.living_temperature", f"{20 + i * 0.3:.1f}")
        p = await app.process_reading(r, force_snapshot=True)
        if p is not None:
            proposals.append(p)
    assert proposals == [], "양성 센서 스트림이 제안을 만들면 알림 피로"

    # 그 와중에 가스 1건 → ember 제안.
    p = await app.process_reading(_r("binary_sensor.kitchen_gas", "on"), force_snapshot=True)
    assert p is not None and p.risk_tier == RiskTier.EMBER
