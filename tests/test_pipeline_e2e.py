"""end-to-end 파이프라인 검증.

FakeSensorSource → LiotApp.process_reading 로 ambient/dusk/ember 시나리오가
각각 올바른 위험도 단계의 Proposal 을 만드는지 확인한다. 네트워크 없음.
"""

from __future__ import annotations

from liot.app import LiotApp
from liot.core.clock import Clock
from liot.core.contracts import SensorReading
from liot.core.risk import RiskTier
from liot.layers.l1_sensing.normalizer import normalize


def _reading(entity_id: str, raw: object, *, ts: str) -> SensorReading:
    return SensorReading(
        entity_id=entity_id,
        source="fake",
        raw_value=raw,
        norm_value=normalize(raw),
        ts_utc=ts,
        home_id="singleton",
    )


async def test_ambient_scenario_daytime_light(clock: Clock) -> None:
    # 낮 시간대 + 조명 켜기 → ambient(잔잔).
    app = LiotApp(clock=clock, categories={"light.living_room": "ambient"})
    reading = _reading("light.living_room", "on", ts="2026-06-01T12:00:00+00:00")
    proposal = await app.process_reading(
        reading, action="turn_on", force_snapshot=True
    )
    assert proposal is not None
    assert proposal.risk_tier == RiskTier.AMBIENT
    assert proposal.required_confirmation == "none"
    assert proposal.action.action == "turn_on"


async def test_dusk_scenario_thermostat_lower(clock: Clock) -> None:
    # 비안전 climate 카테고리 + 온도 내리기(lower) → 카탈로그상 DUSK 로 승격.
    app = LiotApp(clock=clock, categories={"climate.living_room": "climate"})
    reading = _reading("climate.living_room", 19.0, ts="2026-06-01T12:01:00+00:00")
    proposal = await app.process_reading(
        reading,
        action="set_temperature",
        params={"temperature": -2},
        force_snapshot=True,
    )
    assert proposal is not None
    assert proposal.risk_tier == RiskTier.DUSK
    assert proposal.required_confirmation == "explicit"


async def test_ember_scenario_gas_leak(clock: Clock) -> None:
    # 가스 누출(안전 임계) → ember(불씨). Safety-Never-Silent.
    app = LiotApp(clock=clock, categories={"switch.gas_valve": "gas"})
    reading = _reading("switch.gas_valve", "detected", ts="2026-06-01T12:02:00+00:00")
    proposal = await app.process_reading(
        reading, action="alert", force_snapshot=True
    )
    assert proposal is not None
    assert proposal.risk_tier == RiskTier.EMBER
    assert proposal.required_confirmation == "double"


async def test_no_snapshot_returns_none(clock: Clock) -> None:
    # 의미 변화가 없고 force_snapshot 도 아니면 스냅샷 미생성 → None.
    app = LiotApp(clock=clock, categories={"light.living_room": "ambient"})
    r1 = _reading("light.living_room", "on", ts="2026-06-01T12:00:00+00:00")
    # 첫 읽기는 강제 스냅샷으로 상태를 채운다.
    await app.process_reading(r1, action="turn_on", force_snapshot=True)
    # 동일 값 재유입 → 변화 없음 → None.
    r2 = _reading("light.living_room", "on", ts="2026-06-01T12:05:00+00:00")
    proposal = await app.process_reading(r2, action="turn_on")
    assert proposal is None


async def test_full_source_sequence_yields_three_proposals(clock: Clock) -> None:
    app = LiotApp(
        clock=clock,
        categories={
            "light.living_room": "ambient",
            "climate.living_room": "climate",
            "switch.gas_valve": "gas",
        },
    )
    tiers: list[RiskTier] = []
    specs = [
        ("light.living_room", "on", "turn_on", None),
        ("climate.living_room", 19.0, "set_temperature", {"temperature": -2}),
        ("switch.gas_valve", "detected", "alert", None),
    ]
    for i, (eid, raw, action, params) in enumerate(specs):
        reading = _reading(eid, raw, ts=f"2026-06-01T12:0{i}:00+00:00")
        p = await app.process_reading(
            reading, action=action, params=params, force_snapshot=True
        )
        assert p is not None
        tiers.append(p.risk_tier)
    assert tiers == [RiskTier.AMBIENT, RiskTier.DUSK, RiskTier.EMBER]
