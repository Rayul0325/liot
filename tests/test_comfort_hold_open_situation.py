"""D4(결정 0016): 열린 안전 상황 동안의 편의(능동 틱) 제안 보류.

- 열린 안전 상황이 하나라도 있으면 틱 제안은 **자동 실행하지 않는다**(알림 게이트는 그대로).
- 하한 CRITICAL 상황이 열려 있으면 폰 발송도 보류한다: DIGEST, 선호 셀에 넣지 않음(만료가
  비선호로 학습되지 않는다), audit 사유 ``comfort_held_open_situation``.
- 상황이 없을 때, 명시 요청, 상황 알림은 예전 그대로다.

편의 제안 운반체: 비어 있는 거실 + 15도 → 난방 2도 올리기(climate.set_temperature 는
BREATH, 알림 게이트 IMMEDIATE, 정책 AUTO) 라서 '나가고 실행되는' 편의 제안이 실제로 나온다.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from pathlib import Path

from liot.app import LiotApp
from liot.core.clock import Clock
from liot.core.contracts import SensorReading
from liot.db.database import init_db
from liot.layers.l1_sensing.normalizer import normalize
from liot.layers.l2_context.feature_vector import load_cells
from liot.layers.l2_context.proactive import ProactiveGenerator
from liot.layers.l2_context.proactive_rules import load_proactive_ruleset
from liot.layers.l4_execution.executor import ExecutionPolicy, FakeExecutor
from liot.layers.l4_execution.ntfy_transport import FakeNtfyTransport
from liot.service import LiotService

T0 = "2026-06-02T12:00:00+00:00"
HOME = "singleton"
MOTION = "binary_sensor.living_motion"
TEMP = "sensor.living_temperature"
CLIMATE = "climate.living_room"
BEDROOM = "climate.bedroom"  # 명시 요청 대상: 틱 제안과 dedup 키를 나누지 않는다
GAS = "binary_sensor.kitchen_gas"
DOOR = "binary_sensor.front_door"
CATS = {
    MOTION: "motion",
    TEMP: "temperature",
    CLIMATE: "climate",
    BEDROOM: "climate",
    GAS: "gas",
    DOOR: "door",
}
COMFORT_YAML = """
version: "1.0.0"
name: comfort
limits: {cooldown_minutes: 60, max_per_bucket: 5, max_per_day: 10}
rules:
  - id: vacant-warm
    match: {occupancy: vacant}
    state: [{entity: sensor.living_temperature, op: lt, value: 18}]
    propose: {target: climate.living_room, capability: climate, action: set_temperature,
              params: {temperature: 2}}
    ttl_minutes: 15
"""
HELD = "comfort_held_open_situation"


def _reading(eid: str, raw: object) -> SensorReading:
    return SensorReading(
        entity_id=eid, source="test", raw_value=raw, norm_value=normalize(raw),
        ts_utc=T0, home_id=HOME,
    )


def _at(seconds: float) -> str:
    return (datetime.fromisoformat(T0) + timedelta(seconds=seconds)).isoformat()


async def _make(tmp_path: Path):
    clock = Clock(fixed=T0)
    db = await init_db(":memory:")
    rules = tmp_path / "comfort.yaml"
    rules.write_text(COMFORT_YAML, encoding="utf-8")
    gen = ProactiveGenerator(
        ruleset=load_proactive_ruleset(rules), categories=CATS, clock=clock
    )
    app = LiotApp(clock=clock, categories=CATS, proactive=gen)
    executor = FakeExecutor()
    transport = FakeNtfyTransport()
    svc = LiotService(
        app, db, clock=clock, transport=transport, topic="liot-test", secret=b"test-secret-key",
        executor=executor, policy=ExecutionPolicy(enabled=True),
    )
    # 편의 제안의 맥락: 거실 비어 있음 + 15도(관찰은 침묵).
    assert await svc.handle_reading(_reading(MOTION, "off")) is None
    assert await svc.handle_reading(_reading(TEMP, 15)) is None
    return svc, db, executor, transport, clock


async def _audit_rows(svc: LiotService, event_type: str) -> list[tuple[str, str]]:
    cur = await svc._db.execute(
        "SELECT event_type, ref_id FROM audit_log WHERE event_type = ?", (event_type,)
    )
    return [(r[0], r[1]) for r in await cur.fetchall()]


async def _open(svc: LiotService, eid: str):
    res = await svc.handle_reading(_reading(eid, "on"))
    assert res is not None and res.proposal.situation_id is not None
    await svc.drain_phase2()
    return res


# ── 상황 없음: 예전 그대로 ────────────────────────────────────────────────
async def test_no_situation_comfort_is_sent_executed_and_learned(tmp_path: Path) -> None:
    svc, db, executor, transport, clock = await _make(tmp_path)
    try:
        res = await svc.run_tick()
        assert res is not None and res.proposal.action.capability == "climate"
        assert res.decision == "IMMEDIATE" and res.sent is True and res.executed is True
        assert len(executor.executed) == 1
        assert await _audit_rows(svc, HELD) == []
        pid = res.proposal.proposal_id
        assert pid in svc._prop_cell

        # 만료는 약한 비선호로 학습된다(보류 시험의 대조군: 이 신호가 보류분에는 없어야 한다).
        clock.set_fixed(_at(20 * 60))
        await svc.run_tick()
        assert pid in svc._pref_signaled
    finally:
        await svc.drain_phase2()
        await db.close()


# ── 가스(CRITICAL 하한) 열림: 발송·실행·학습 모두 보류 ────────────────────────
async def test_gas_open_holds_comfort_phone_execution_and_learning(tmp_path: Path) -> None:
    svc, db, executor, transport, clock = await _make(tmp_path)
    try:
        await _open(svc, GAS)
        sent_before = len(transport.sent)
        cells_before = await load_cells(svc.prefs)

        res = await svc.run_tick()
        assert res is not None and res.proposal.action.capability == "climate"
        pid = res.proposal.proposal_id
        assert res.decision == "DIGEST" and res.sent is False and res.executed is False
        assert len(transport.sent) == sent_before  # 폰 발송 없음
        assert executor.executed == []
        assert pid not in svc._nonce  # 어디로도 안 나가는 제안에 1회용 링크를 남기지 않는다
        assert await _audit_rows(svc, HELD) == [(HELD, pid)]
        stored = await svc.proposals.get(pid)
        assert stored is not None and stored.state == "pending"  # 영속은 된다

        # 학습 제외: 선호 셀 추적에 없고, 만료돼도 비선호 신호가 반영되지 않는다.
        assert pid not in svc._prop_cell
        clock.set_fixed(_at(20 * 60))
        await svc.run_tick()
        stored = await svc.proposals.get(pid)
        assert stored is not None and stored.state == "expired"
        assert pid not in svc._pref_signaled
        assert await load_cells(svc.prefs) == cells_before
    finally:
        await svc.drain_phase2()
        await db.close()


# ── 경고(문) 열림: 알림은 나가되 자동 실행만 막는다 ───────────────────────────
async def test_warning_door_open_still_sends_but_blocks_auto_execution(tmp_path: Path) -> None:
    svc, db, executor, transport, clock = await _make(tmp_path)
    try:
        await _open(svc, DOOR)
        sent_before = len(transport.sent)

        res = await svc.run_tick()
        assert res is not None and res.proposal.action.capability == "climate"
        assert res.decision == "IMMEDIATE" and res.sent is True
        assert len(transport.sent) == sent_before + 1
        assert res.executed is False and executor.executed == []
        assert await _audit_rows(svc, HELD) == []
        # 폰으로 나간 제안이라 선호 학습은 평소대로 추적한다.
        assert res.proposal.proposal_id in svc._prop_cell
    finally:
        await svc.drain_phase2()
        await db.close()


# ── 상황이 닫히면 다시 예전 그대로 ───────────────────────────────────────────
async def test_comfort_resumes_after_situation_closes(tmp_path: Path) -> None:
    svc, db, executor, transport, clock = await _make(tmp_path)
    try:
        await _open(svc, GAS)
        clock.set_fixed(_at(60))
        await svc.handle_reading(_reading(GAS, "off"))
        res = await svc.run_tick()
        assert res is not None
        assert res.decision == "IMMEDIATE" and res.sent is True and res.executed is True
    finally:
        await svc.drain_phase2()
        await db.close()


# ── 명시 요청은 상황 중에도 그대로(run_tick 만 보류한다) ──────────────────────
async def test_explicit_request_during_gas_is_unchanged(tmp_path: Path) -> None:
    svc, db, executor, transport, clock = await _make(tmp_path)
    try:
        await _open(svc, GAS)
        # 같은 태스크에서 보류된 틱이 먼저 지나간 뒤에도(보류가 새지 않아야 한다) 명시 요청은 그대로다.
        held = await svc.run_tick()
        assert held is not None
        proposal = await svc.app.process_reading(
            _reading(BEDROOM, 20), action="set_temperature", params={"temperature": 2}
        )
        assert proposal is not None
        assert proposal.action.params["__origin__"] == "explicit:external"
        res = await svc.dispatch(proposal)
        assert res.decision == "IMMEDIATE" and res.sent is True and res.executed is True
        assert len(executor.executed) == 1
        # 명시 요청에는 보류 감사 행이 없다.
        assert proposal.proposal_id not in [ref for _, ref in await _audit_rows(svc, HELD)]
    finally:
        await svc.drain_phase2()
        await db.close()
