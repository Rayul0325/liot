"""D4(결정 0016): 열린 안전 상황 동안의 편의(능동 틱) 제안 보류.

- 열린 안전 상황이 하나라도 있으면 틱 제안은 **자동 실행하지 않는다**(알림 게이트는 그대로).
- 하한 CRITICAL 상황이 열려 있으면 폰 발송도 보류한다: DIGEST, 선호 셀에 넣지 않음(만료가
  비선호로 학습되지 않는다), audit 사유 ``comfort_held_open_situation``.
- 상황이 없을 때, 명시 요청, 상황 알림은 예전 그대로다.

편의 제안 운반체: 비어 있는 거실 + 15도 → 난방 2도 올리기(climate.set_temperature 는
BREATH, 알림 게이트 IMMEDIATE, 정책 AUTO) 라서 '나가고 실행되는' 편의 제안이 실제로 나온다.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from liot.app import LiotApp
from liot.core.clock import Clock
from liot.core.contracts import CandidateAction, SensorReading
from liot.db.database import init_db
from liot.layers.l1_sensing.normalizer import normalize
from liot.layers.l2_context.feature_vector import load_cells
from liot.layers.l2_context.proactive import ProactiveGenerator
from liot.layers.l2_context.proactive_rules import load_proactive_ruleset
from liot.layers.l4_execution.executor import ExecutionPolicy, ExecutionResult, FakeExecutor
from liot.layers.l4_execution.ntfy_transport import FakeNtfyTransport
from liot.service import _COMFORT_TICK, LiotService

T0 = "2026-06-02T12:00:00+00:00"
HOME = "singleton"
MOTION = "binary_sensor.living_motion"
TEMP = "sensor.living_temperature"
CLIMATE = "climate.living_room"
BEDROOM = "climate.bedroom"  # 명시 요청 대상: 틱 제안과 dedup 키를 나누지 않는다
GAS = "binary_sensor.kitchen_gas"
LIGHT = "light.living_room"
DOOR = "binary_sensor.front_door"
CATS = {
    MOTION: "motion",
    TEMP: "temperature",
    CLIMATE: "climate",
    BEDROOM: "climate",
    LIGHT: "light",
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
LIGHT_YAML = """
version: "1.0.0"
name: comfort
limits: {cooldown_minutes: 60, max_per_bucket: 5, max_per_day: 10}
rules:
  - id: vacant-light-off
    match: {occupancy: vacant}
    state: [{entity: light.living_room, op: eq, value: true}]
    propose: {target: light.living_room, capability: light, action: turn_off}
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


class _LogExecutor(FakeExecutor):
    """실행 시점을 사건 기록에 남기는 가짜 실행기."""

    def __init__(self, events: list[tuple[str, str]]) -> None:
        super().__init__()
        self._events = events

    async def execute(self, action: CandidateAction) -> ExecutionResult:
        self._events.append(("execute", action.entity_id))
        return await super().execute(action)


class _LogTransport(FakeNtfyTransport):
    """송신 시점을 사건 기록에 남기는 가짜 transport."""

    def __init__(self, events: list[tuple[str, str]]) -> None:
        super().__init__()
        self._events = events

    def send(self, topic: str, sanitized: dict[str, Any]) -> None:
        self._events.append(("send", str(sanitized.get("proposal_id", ""))))
        super().send(topic, sanitized)


async def _make(
    tmp_path: Path,
    *,
    events: list[tuple[str, str]] | None = None,
    rules_yaml: str = COMFORT_YAML,
    light: bool = False,
):
    events = events if events is not None else []
    clock = Clock(fixed=T0)
    db = await init_db(":memory:")
    rules = tmp_path / "comfort.yaml"
    rules.write_text(rules_yaml, encoding="utf-8")
    gen = ProactiveGenerator(
        ruleset=load_proactive_ruleset(rules), categories=CATS, clock=clock
    )
    app = LiotApp(clock=clock, categories=CATS, proactive=gen)
    executor = _LogExecutor(events)
    transport = _LogTransport(events)
    svc = LiotService(
        app, db, clock=clock, transport=transport, topic="liot-test", secret=b"test-secret-key",
        executor=executor, policy=ExecutionPolicy(enabled=True),
    )
    # 상황이 열리는 시점(DB 커밋 직후)을 사건 기록에 남긴다.
    real_open = svc.safety.open_or_get_situation

    async def _logged_open(*args: Any, **kwargs: Any) -> tuple[str, bool]:
        out = await real_open(*args, **kwargs)
        events.append(("opened", str(args[1])))  # args = (home, category, entity, floor, at)
        return out

    svc.safety.open_or_get_situation = _logged_open  # type: ignore[method-assign]
    # 편의 제안의 맥락: 거실 비어 있음 + (15도 | 조명 켜짐)(관찰은 침묵).
    assert await svc.handle_reading(_reading(MOTION, "off")) is None
    if light:
        assert await svc.handle_reading(_reading(LIGHT, "on")) is None
    else:
        assert await svc.handle_reading(_reading(TEMP, 15)) is None
    events.clear()
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


# ── 게이트가 DIGEST 로 분류하는 편의 제안(잔잔 조명)도 위험 상황 동안은 보류로 센다 ──────
async def test_gas_open_holds_digest_classified_comfort_too(tmp_path: Path) -> None:
    svc, db, executor, transport, clock = await _make(
        tmp_path, rules_yaml=LIGHT_YAML, light=True
    )
    try:
        await _open(svc, GAS)
        res = await svc.run_tick()
        assert res is not None and res.proposal.action.capability == "light"
        pid = res.proposal.proposal_id
        assert res.decision == "DIGEST" and res.held is True
        assert await _audit_rows(svc, HELD) == [(HELD, pid)]
        assert pid not in svc._prop_cell  # 만료가 비선호로 학습되지 않는다
    finally:
        await svc.drain_phase2()
        await db.close()


# ── 경쟁: 틱 제안이 디스패치되는 도중 상황이 열려도 놓치지 않는다 ─────────────────────
async def _race_once(
    tmp_path: Path, entity: str, k: int
) -> tuple[list[tuple[str, str]], Any, set[str]]:
    events: list[tuple[str, str]] = []
    svc, db, executor, transport, clock = await _make(tmp_path, events=events)
    try:
        tick = asyncio.create_task(svc.run_tick())
        for _ in range(k):
            await asyncio.sleep(0)
        other = asyncio.create_task(svc.handle_reading(_reading(entity, "on")))
        tick_res, _ = await asyncio.gather(tick, other)
        await svc.drain_phase2()
        return events, tick_res, set(svc._nonce)
    finally:
        await db.close()


@pytest.mark.parametrize("entity", [GAS, DOOR])
async def test_situation_opening_during_tick_dispatch_is_not_missed(
    tmp_path: Path, entity: str
) -> None:
    """상황이 열린 *뒤*에 편의 제안이 실행(가스·문)되거나 폰으로 나가서(가스)는 안 된다.

    k = 틱 시작 뒤 상황 신호를 시작하기까지 양보한 횟수. 실제 aiosqlite 양보만으로
    두 태스크가 겹친다(인위적 지연 없음). 사건 기록의 ``opened`` 는 상황 행이 커밋된 직후다.
    """
    category = CATS[entity]
    held_seen = unheld_seen = 0
    # 기계마다 한 번의 양보에 진행되는 일이 달라(CI 리눅스는 더 빠르다) 고정 구간 대신
    # 두 순서가 모두 관측될 때까지 k 를 넓힌다. 불변식은 모든 k 에서 확인한다.
    for k in [*range(0, 90), *range(90, 1200, 7)]:
        if held_seen and unheld_seen and k >= 90:
            break
        events, tick_res, nonces = await _race_once(tmp_path, entity, k)
        assert tick_res is not None
        pid = tick_res.proposal.proposal_id
        if tick_res.held:
            assert pid not in nonces, f"k={k}: 보류한 제안에 1회용 링크가 남았다"
        opened_at = events.index(("opened", category))
        late = events[opened_at + 1 :]
        assert ("execute", CLIMATE) not in late, f"k={k}: 상황이 열린 뒤 편의 제안이 실행됐다 {events}"
        if entity == GAS:
            assert ("send", pid) not in late, f"k={k}: 위험 상황이 열린 뒤 편의 제안이 폰으로 나갔다 {events}"
        if ("execute", CLIMATE) in events[:opened_at] or ("send", pid) in events[:opened_at]:
            unheld_seen += 1
        else:
            held_seen += 1
    # 두 경우(상황이 먼저 / 제안이 먼저)가 모두 실제로 일어나야 훑은 구간이 의미가 있다.
    assert held_seen > 0 and unheld_seen > 0, (held_seen, unheld_seen)


# ── 표식은 태스크를 넘지 않는다: 디스패치 중 처리되는 안전 신호는 보류를 받지 않는다 ────────
async def test_tick_marker_does_not_leak_into_other_tasks(tmp_path: Path) -> None:
    for k in range(0, 70, 3):
        events: list[tuple[str, str]] = []
        svc, db, executor, transport, clock = await _make(tmp_path, events=events)
        try:
            await _open(svc, GAS)  # 위험 상황이 열려 있어 틱 제안은 폰 보류 대상이다
            tick = asyncio.create_task(svc.run_tick())
            for _ in range(k):
                await asyncio.sleep(0)
            door = asyncio.create_task(svc.handle_reading(_reading(DOOR, "on")))
            tick_res, door_res = await asyncio.gather(tick, door)
            await svc.drain_phase2()
            assert tick_res is not None and tick_res.held is True
            assert door_res is not None and door_res.proposal.situation_id is not None
            assert door_res.decision == "IMMEDIATE" and door_res.sent is True, f"k={k}"
            assert door_res.held is False
            held_refs = [ref for _, ref in await _audit_rows(svc, HELD)]
            assert door_res.proposal.proposal_id not in held_refs
        finally:
            await db.close()


async def test_tick_marker_is_reset_after_run_tick(tmp_path: Path) -> None:
    svc, db, executor, transport, clock = await _make(tmp_path)
    try:
        assert _COMFORT_TICK.get() is False
        assert await svc.run_tick() is not None
        assert _COMFORT_TICK.get() is False  # 같은 태스크에서도 새지 않는다
    finally:
        await db.close()
