"""HA 감독(재연결·재동기화·감시 중단/재개) — 결정 기록 0014.

배경(findings): 웹소켓이 닫히면 ``ConnectionClosed`` 가 ``stream()`` 밖으로 올라가 ``run`` 의
gather 가 사다리·틱까지 취소하고 프로세스가 끝났다. 형식 오류로 스트림이 조용히 끝나면 센서를 더
받지 않았다. 시작 때 HA 에 닿지 못하면 프로세스가 멈췄고, 꺼져 있던 동안 이상이 된 안전 센서는
재시작 뒤 침묵했다. 신호 하나의 예외가 스트림 루프를 죽였다.

여기서 고정하는 것:
  1) 브리지: 끊김·형식 오류가 우리 예외가 된다. 요청 중 끼어든 이벤트는 버퍼에 쌓이고 순서대로 나온다.
     get_states 는 현재값(복원 자리표시자 제외)을 돌려준다.
  2) 순수 규칙(OutageTracker·Backoff): 120초 경계·누적·한 번만·재개, 1→60초 대기와 5분 안정 뒤 초기화.
  3) 감독(``run(ha_factory=...)``): 사다리가 HA 와 무관하게 돈다 · 재연결마다 재동기화한다 ·
     세션 순서(구독 → get_states → 재동기화 → 버퍼) · HA 기동 대기 · 카테고리 맵 병합 ·
     신호 하나의 예외 격리와 재동기화 예약 · 감시 중단/재개 알림(거주자만·헌장 OFF 존중·음소거 무시).
  4) 실행기는 호출마다 짧은 별도 연결을 쓰고 HA 가 없으면 fail-closed 다.
  5) serve: SIGTERM·SIGINT 로 정리하고 끝나며 DB 를 닫는다. HA 가 없어도 멈추지 않고 재시도한다.

외부 네트워크 없음: 메모리 가짜 HA(``tests.ha_world.World``)와 루프백 websockets 서버만 쓴다.
"""

from __future__ import annotations

import asyncio
import contextlib
import functools
import json
import logging
import os
import signal
from collections.abc import Awaitable, Callable
from typing import Any

import pytest

from liot import serve
from liot.app import LiotApp
from liot.core.clock import Clock
from liot.core.contracts import CandidateAction, ContextSnapshot, SensorReading
from liot.core.egress_gate import NTFY_ALLOWED_KEYS
from liot.core.risk import RiskTier
from liot.core.severity import SeverityLevel
from liot.db.database import init_db
from liot.ha_supervision import Backoff, OutageTracker
from liot.layers.l1_sensing.ha_bridge import (
    FakeHAConnection,
    HABridge,
    HAConnectionClosed,
    HAConnectionError,
    HAMalformedMessage,
    WebsocketsHAConnection,
    call_service_once,
    connect_ha,
    get_state_once,
)
from liot.layers.l1_sensing.normalizer import normalize
from liot.layers.l3_judgment.llm_judge import LLMResult
from liot.layers.l4_execution import escalation
from liot.layers.l4_execution.executor import ExecutionError, HAExecutor
from liot.layers.l4_execution.ntfy_transport import FakeNtfyTransport
from liot.service import LiotService
from tests.ha_world import T0, FakeTime, LoopbackHA, ScriptedFactory, World

SECRET = b"test-secret-key"
TOPIC = "liot-test"
HOME = "singleton"
GAS = "binary_sensor.kitchen_gas"
SMOKE = "binary_sensor.hall_smoke"
DOOR = "binary_sensor.front_door"
LIGHT = "light.living_room"
STOPPED_LABEL = "안전 센서 전체 · 감시 중단(집 연결 끊김)"
RESUMED_LABEL = "안전 센서 전체 · 감시 재개(집 연결 복구)"


class _Backend:
    """하한 그대로 돌려주는 결정 모델."""

    model_id = "ha-reconnect-test-model"

    def judge_raw(self, candidate: CandidateAction, ctx: ContextSnapshot | None,
                  gate1_floor: RiskTier,
                  severity_floor: SeverityLevel | None = None) -> LLMResult:
        return LLMResult(tier=RiskTier.EMBER, confidence=0.8, rationale="test",
                         model_id=self.model_id, severity=None)


# ── 도우미 ───────────────────────────────────────────────────────────────────
def _world() -> World:
    w = World()
    w.set(GAS, "off", "gas")
    w.set(SMOKE, "off", "smoke")
    w.set(DOOR, "off", "door")
    w.set(LIGHT, "off")
    return w


async def _make(
    *, transport: Any = None, clock: Clock | None = None, fake_time: FakeTime | None = None,
    db_path: str = ":memory:",
):
    clock = clock or Clock(fixed=T0)
    db = await init_db(db_path)
    # categories={} 는 serve 와 같다: HA 에 붙기 전에는 카탈로그 맵으로 시작하고 첫 연결에서 HA 맵으로 바뀐다.
    app = LiotApp(clock=clock, categories={}, llm_backend=_Backend())
    transport = transport if transport is not None else FakeNtfyTransport()
    svc = LiotService(app, db, clock=clock, transport=transport, topic=TOPIC, secret=SECRET)
    svc.ha_ready_poll_s = 0.02
    if fake_time is not None:
        svc._ha_now = fake_time.now
        svc._ha_sleep = fake_time.sleep
    else:
        svc._ha_sleep = _fast_sleep
    return svc, db, transport


async def _fast_sleep(seconds: float) -> None:
    """실제 시간으로 도는 시험에서 백오프를 줄인다(1→60초를 그대로 기다리지 않는다)."""
    await asyncio.sleep(min(seconds, 0.02))


async def _until(pred: Callable[[], bool | Awaitable[bool]], timeout: float = 8.0) -> None:
    end = asyncio.get_running_loop().time() + timeout
    while True:
        res = pred()
        if asyncio.iscoroutine(res) or isinstance(res, asyncio.Future):
            res = await res
        if res:
            return
        if asyncio.get_running_loop().time() > end:
            raise AssertionError("조건이 제한시간 안에 참이 되지 않았다")
        await asyncio.sleep(0.01)


def _start(svc: LiotService, factory: Callable[[], Awaitable[HABridge]], **kw: Any) -> asyncio.Task[None]:
    return asyncio.create_task(
        svc.run(ha_factory=factory, tick_seconds=3600.0, ladder_resolution_s=0.02, **kw)
    )


async def _stop(task: asyncio.Task[None]) -> None:
    task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await task


def _stopped(t: FakeNtfyTransport) -> list[tuple[str, dict[str, Any]]]:
    return [(top, p) for top, p in t.sent if p.get("situation_label") == STOPPED_LABEL]


def _resumed(t: FakeNtfyTransport) -> list[tuple[str, dict[str, Any]]]:
    return [(top, p) for top, p in t.sent if p.get("situation_label") == RESUMED_LABEL]


async def _audit(db: Any, event_type: str) -> list[dict[str, Any]]:
    cur = await db.execute(
        "SELECT ref_id, detail FROM audit_log WHERE event_type = ? ORDER BY rowid", (event_type,)
    )
    try:
        rows = await cur.fetchall()
    finally:
        await cur.close()
    return [{"ref_id": r[0], **json.loads(r[1])} for r in rows]


async def _open_entities(svc: LiotService) -> set[str]:
    return {s.entity_id for s in await svc.safety.list_open_situations(HOME)}


async def _is_open(svc: LiotService, eid: str) -> bool:
    return eid in await _open_entities(svc)


async def _guardian(svc: LiotService) -> str:
    rid = await svc.safety.add_resident(HOME, "엄마")
    gid, topic = await svc.safety.add_guardian(HOME, "딸")
    await svc.safety.grant_consent(HOME, gid, rid)
    return topic


def _reading(eid: str, raw: object) -> SensorReading:
    return SensorReading(
        entity_id=eid, source="test", raw_value=raw, norm_value=normalize(raw),
        ts_utc=T0, home_id=HOME,
    )


def _event(eid: str, state: str) -> dict[str, Any]:
    return {
        "type": "event",
        "event": {
            "event_type": "state_changed",
            "data": {"new_state": {"entity_id": eid, "state": state, "last_changed": T0}},
        },
    }


def _echo(extra: dict[str, Any] | None = None) -> Callable[[list[dict[str, Any]]], dict[str, Any]]:
    def _mk(sent: list[dict[str, Any]]) -> dict[str, Any]:
        out: dict[str, Any] = {"id": sent[-1]["id"], "type": "result", "success": True}
        if extra:
            out.update(extra)
        return out

    return _mk


# ==========================================================================
# 1) 브리지
# ==========================================================================
async def test_request_buffers_events_that_arrive_while_waiting_for_the_result() -> None:
    """요청 응답을 기다리는 동안 끼어든 이벤트는 버려지지 않고 스트림이 순서대로 내준다.

    예전에는 ``_request`` 가 이 이벤트를 버렸다(get_states 와 구독 사이에서 안전 신호가 사라진다).
    """
    conn = FakeHAConnection(
        [
            {"type": "auth_required"},
            {"type": "auth_ok"},
            _echo(),  # subscribe
            _event(GAS, "on"),  # get_states 응답 전에 끼어든 이벤트
            _event(DOOR, "on"),
            _echo({"result": []}),  # get_states 결과
            _event(SMOKE, "on"),  # 응답 뒤 연결에서 오는 이벤트
        ]
    )
    bridge = HABridge(conn, "tok")
    await bridge.subscribe()
    await bridge.entity_states()
    got = [(await bridge.next_reading()) for _ in range(3)]
    assert [r.entity_id for r in got if r is not None] == [GAS, DOOR, SMOKE]


async def test_entity_states_returns_current_values_and_skips_restored_placeholders() -> None:
    conn = FakeHAConnection(
        [
            {"type": "auth_required"},
            {"type": "auth_ok"},
            _echo(
                {
                    "result": [
                        {"entity_id": GAS, "state": "on", "attributes": {"device_class": "gas"},
                         "last_changed": T0},
                        {"entity_id": SMOKE, "state": "unavailable",
                         "attributes": {"restored": True}, "last_changed": T0},
                        {"entity_id": LIGHT, "state": "off", "attributes": {}, "last_changed": T0},
                        "garbage-entry",
                        {"state": "on"},  # entity_id 없음
                    ]
                }
            ),
        ]
    )
    snap = await HABridge(conn, "tok").entity_states()
    # 복원 자리표시자는 device_class 가 없어 도메인 접두사로 떨어진다(그래서 병합이 덮어쓰지 못하게 한다).
    assert snap.categories == {GAS: "gas", SMOKE: "binary_sensor", LIGHT: "light"}
    assert [(r.entity_id, r.raw_value) for r in snap.readings] == [(GAS, "on"), (LIGHT, "off")]
    assert snap.restored == frozenset({SMOKE})


async def test_core_state_reads_get_config_and_returns_none_when_absent() -> None:
    with_state = FakeHAConnection(
        [{"type": "auth_required"}, {"type": "auth_ok"}, _echo({"result": {"state": "STARTING"}})]
    )
    assert await HABridge(with_state, "tok").core_state() == "STARTING"
    without = FakeHAConnection(
        [{"type": "auth_required"}, {"type": "auth_ok"}, _echo({"result": {"version": "x"}})]
    )
    assert await HABridge(without, "tok").core_state() is None


async def test_core_state_treats_a_rejected_get_config_as_unknown_not_a_failure() -> None:
    """HA 가 get_config 를 거절해도(구버전 등) 감시가 시작돼야 한다 — None = 기다리지 않는다."""
    rejected = FakeHAConnection(
        [{"type": "auth_required"}, {"type": "auth_ok"},
         lambda sent: {"id": sent[-1]["id"], "type": "result", "success": False,
                       "error": {"code": "unknown_command"}}]
    )
    assert await HABridge(rejected, "tok").core_state() is None


@pytest.mark.parametrize(
    "msg",
    [
        {"type": "event", "event": "not-an-object"},
        {"type": "event", "event": {"event_type": "state_changed", "data": "x"}},
        {"type": "event", "event": {"event_type": "state_changed",
                                    "data": {"new_state": "x"}}},
        {"type": "event", "event": {"event_type": "state_changed",
                                    "data": {"new_state": {"entity_id": ["list"], "state": "on"}}}},
    ],
)
async def test_malformed_event_shape_raises_instead_of_being_skipped(msg: dict[str, Any]) -> None:
    """형식이 틀린 상태 변화 이벤트는 조용히 건너뛰지 않는다 — 건너뛰면 그 안전 신호가 사라진다."""
    conn = FakeHAConnection([{"type": "auth_required"}, {"type": "auth_ok"}, _echo(), msg])
    bridge = HABridge(conn, "tok")
    await bridge.subscribe()
    with pytest.raises(HAMalformedMessage):
        await bridge.next_reading()


async def test_next_reading_timeout_returns_none_without_losing_the_connection() -> None:
    world = _world()
    bridge = await world.factory()
    await bridge.subscribe()
    assert await bridge.next_reading(timeout=0.05) is None
    world.emit(GAS, "on")
    reading = await bridge.next_reading(timeout=1.0)
    assert reading is not None and reading.entity_id == GAS


async def test_request_times_out_instead_of_hanging_on_a_silent_ha() -> None:
    class _Silent:
        async def send(self, obj: dict[str, Any]) -> None:
            return None

        async def recv(self) -> dict[str, Any]:
            await asyncio.sleep(3600)
            return {}

        async def close(self) -> None:
            return None

    bridge = HABridge(_Silent(), "tok", request_timeout_s=0.05)
    bridge._authed = True
    with pytest.raises(HAConnectionError, match="시간 초과"):
        await asyncio.wait_for(bridge.entity_states(), timeout=5)


async def test_stream_keeps_its_legacy_contract_ends_quietly_on_connection_error() -> None:
    """``stream()`` 은 예전처럼 연결이 끝나면 조용히 끝난다(감독은 next_reading 으로 끝난 이유를 안다)."""
    conn = FakeHAConnection(
        [{"type": "auth_required"}, {"type": "auth_ok"}, _echo(), _event(GAS, "on"),
         HAConnectionClosed("closed")]
    )
    got = [r.entity_id async for r in HABridge(conn, "tok").stream()]
    assert got == [GAS]


async def test_real_websocket_close_becomes_ha_connection_closed() -> None:
    """진짜 연결이 닫히면 라이브러리의 ConnectionClosed 가 새지 않고 우리 예외가 된다."""
    websockets = pytest.importorskip("websockets")

    async def handler(ws: Any, *args: Any) -> None:
        await ws.send(json.dumps({"type": "auth_required"}))
        await ws.recv()
        await ws.send(json.dumps({"type": "auth_ok"}))
        await ws.close()

    async with websockets.serve(handler, "127.0.0.1", 0) as server:
        port = server.sockets[0].getsockname()[1]
        conn = WebsocketsHAConnection(f"ws://127.0.0.1:{port}/api/websocket")
        await conn.open()
        try:
            await conn.recv()
            await conn.send({"type": "auth", "access_token": "tok"})
            await conn.recv()
            with pytest.raises(HAConnectionClosed):
                await asyncio.wait_for(conn.recv(), timeout=5)
            with pytest.raises(HAConnectionClosed):  # 닫힌 연결에 쓰기도 우리 예외
                await asyncio.wait_for(conn.send({"type": "ping", "id": 9}), timeout=5)
        finally:
            await conn.close()


async def test_real_websocket_non_json_becomes_malformed() -> None:
    websockets = pytest.importorskip("websockets")

    async def handler(ws: Any, *args: Any) -> None:
        await ws.send("this is not json")
        await asyncio.sleep(0.3)

    async with websockets.serve(handler, "127.0.0.1", 0) as server:
        port = server.sockets[0].getsockname()[1]
        conn = WebsocketsHAConnection(f"ws://127.0.0.1:{port}/api/websocket")
        await conn.open()
        try:
            with pytest.raises(HAMalformedMessage):
                await asyncio.wait_for(conn.recv(), timeout=5)
        finally:
            await conn.close()


async def test_open_wraps_refused_connection_and_bad_handshake() -> None:
    """연결 거부(OSError)와 핸드셰이크 거부(WebSocketException)가 모두 HAConnectionError 다."""
    pytest.importorskip("websockets")
    with pytest.raises(HAConnectionError):
        await WebsocketsHAConnection("ws://127.0.0.1:1/api/websocket").open()

    async def reply_502(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        await reader.readuntil(b"\r\n\r\n")
        writer.write(b"HTTP/1.1 502 Bad Gateway\r\nContent-Length: 0\r\nConnection: close\r\n\r\n")
        await writer.drain()
        writer.close()

    server = await asyncio.start_server(reply_502, "127.0.0.1", 0)
    try:
        port = server.sockets[0].getsockname()[1]
        with pytest.raises(HAConnectionError):
            await WebsocketsHAConnection(f"ws://127.0.0.1:{port}/api/websocket").open()
    finally:
        server.close()


# ==========================================================================
# 2) 순수 규칙: 대기와 알림 시점
# ==========================================================================
def test_backoff_doubles_from_one_to_sixty_and_stays() -> None:
    b = Backoff()
    assert [b.next_delay() for _ in range(9)] == [1.0, 2.0, 4.0, 8.0, 16.0, 32.0, 60.0, 60.0, 60.0]
    b.reset()
    assert b.next_delay() == 1.0


def test_notice_is_due_at_exactly_120_continuous_seconds_not_before() -> None:
    t = OutageTracker(start=1000.0)
    t.mark_down(1000.0)
    assert t.notice_due(1119.9) is None
    due = t.notice_due(1120.0)
    assert due is not None and due.reason == "startup" and due.down_s == 120.0


def test_notice_reason_is_continuous_after_the_first_connection() -> None:
    t = OutageTracker(start=0.0)
    t.mark_up(0.0)
    t.mark_down(100.0)
    assert t.notice_due(219.9) is None
    due = t.notice_due(220.0)
    assert due is not None and due.reason == "continuous"


def test_notice_is_decided_once_per_outage_and_resume_once() -> None:
    t = OutageTracker(start=0.0)
    t.mark_up(1.0)
    t.mark_down(10.0)
    assert t.notice_due(130.0) is not None
    t.mark_notified()
    assert t.notice_due(500.0) is None  # 같은 끊김에서 다시 내지 않는다
    rec = t.mark_up(600.0)
    assert rec is not None and rec.notified and rec.down_s == 590.0
    assert t.mark_up(601.0) is None  # 이미 올라와 있으면 재개를 또 내지 않는다


def test_recovery_without_a_notice_is_not_notified() -> None:
    t = OutageTracker(start=0.0)
    t.mark_up(1.0)
    t.mark_down(10.0)
    rec = t.mark_up(40.0)
    assert rec is not None and rec.notified is False


def test_flapping_is_counted_by_the_cumulative_rule_within_a_window() -> None:
    t = OutageTracker(start=0.0)
    t.mark_up(0.0)
    for start in (100.0, 200.0):  # 50초씩 두 번 끊겼다 복구(합 100초)
        t.mark_down(start)
        t.mark_up(start + 50.0)
    t.mark_down(300.0)
    assert t.notice_due(319.9) is None  # 100 + 19.9 < 120
    due = t.notice_due(320.0)
    assert due is not None and due.reason == "cumulative" and due.down_s == 20.0


def test_cumulative_window_forgets_outages_older_than_ten_minutes() -> None:
    t = OutageTracker(start=0.0)
    t.mark_up(0.0)
    t.mark_down(10.0)
    t.mark_up(110.0)  # 100초 끊김
    t.mark_down(1000.0)  # 창(600초) 밖이 된 뒤
    assert t.notice_due(1019.0) is None  # 창 밖의 100초는 세지 않는다 → 19초뿐
    assert t.notice_due(1120.0) is not None  # 연속 120초는 그대로 규칙(a)


def test_resume_clears_the_cumulative_window() -> None:
    """알린 끊김이 끝나면 누적을 새로 센다 — 깜빡임이 중단·재개를 연달아 만들지 못한다."""
    t = OutageTracker(start=0.0)
    t.mark_up(0.0)
    t.mark_down(100.0)
    assert t.notice_due(220.0) is not None
    t.mark_notified()
    t.mark_up(221.0)
    t.mark_down(230.0)
    assert t.notice_due(231.0) is None  # 지난 120초 끊김이 다시 세어지지 않는다
    assert t.notice_due(350.0) is not None


def test_next_deadline_is_the_continuous_or_cumulative_boundary() -> None:
    t = OutageTracker(start=0.0)
    assert t.next_deadline(0.0) == 120.0
    t.mark_up(0.0)
    assert t.next_deadline(5.0) is None  # 올라와 있으면 낼 일이 없다
    t.mark_down(10.0)
    t.mark_up(70.0)  # 60초 끊김
    t.mark_down(100.0)
    assert t.next_deadline(100.0) == 160.0  # 누적 60 + 60초 더
    t.mark_notified()
    assert t.next_deadline(100.0) is None


# ==========================================================================
# 3) 감독: 진짜 소켓 — 닫았다 다시 연다
# ==========================================================================
async def test_loopback_close_then_reopen_ladder_keeps_firing_and_resync_sends_gas_alert() -> None:
    """HA 가 닫혔다 다시 열린다.

    - 닫혀 있는 동안에도 사다리는 시각대로 재알림을 낸다(HA 와 독립).
    - 닫혀 있는 동안 켜진 가스는 다시 붙은 뒤 **재동기화**로 첫 알림이 나간다(이벤트는 없었다).
    - 120초 안의 끊김이라 감시 중단 알림은 없다.
    """
    pytest.importorskip("websockets")
    world = _world()
    srv = LoopbackHA(world)
    await srv.start()
    svc, db, transport = await _make()
    task = _start(svc, functools.partial(connect_ha, srv.url, "good-token"))
    try:
        await _until(lambda: world.states_requests >= 1)
        world.emit(SMOKE, "on")  # 위험 상황 → 첫 알림, 사다리 시작(재알림 1분)
        await _until(lambda: len(transport.sent) >= 1)
        first = len(transport.sent)
        port = srv.port
        await srv.stop()  # HA 닫힘(이후 연결은 거부된다)

        # 틈: 시계를 61초 옮기면 사다리가 재알림을 낸다 — HA 는 닿지 않는 채로.
        svc.clock.set_fixed("2026-06-02T12:01:01+00:00")
        await _until(lambda: len(transport.sent) > first)
        assert not task.done(), "HA 가 닫혀도 데몬(run)은 살아 있어야 한다"

        world.set(GAS, "on", "gas")  # 끊긴 동안 가스가 켜졌다 — 이벤트는 아무도 못 받았다
        assert GAS not in await _open_entities(svc)
        sent_before_reopen = len(transport.sent)

        await srv.start(port)  # HA 다시 열림
        await _until(lambda: _is_open(svc, GAS))
        sid = next(
            s.situation_id for s in await svc.safety.list_open_situations(HOME)
            if s.entity_id == GAS
        )
        assert await escalation.situation_notified(db, HOME, sid), "재동기화가 가스 첫 알림을 냈어야 한다"
        assert len(transport.sent) > sent_before_reopen
        assert _stopped(transport) == []  # 끊김이 120초 미만
    finally:
        await _stop(task)
        await srv.stop()
        await db.close()


async def test_loopback_malformed_message_triggers_reconnect_and_resync() -> None:
    """HA 가 JSON 이 아닌 원문을 보내면(예전: 스트림이 조용히 끝나 센서를 더 안 받는다) 다시 붙는다."""
    pytest.importorskip("websockets")
    world = _world()
    srv = LoopbackHA(world)
    srv.malformed_once = True
    await srv.start()
    svc, db, transport = await _make()
    task = _start(svc, functools.partial(connect_ha, srv.url, "good-token"))
    try:
        await _until(lambda: world.connects >= 2 and world.states_requests >= 1)
        world.emit(GAS, "on")
        await _until(lambda: len(transport.sent) >= 1)
        assert not task.done()
    finally:
        await _stop(task)
        await srv.stop()
        await db.close()


# ==========================================================================
# 4) 감독: 감시 중단·재개 알림(가짜 시계)
# ==========================================================================
class _TimedTransport(FakeNtfyTransport):
    """보낸 시각(가짜 시계)을 함께 기록한다."""

    def __init__(self, fake_time: FakeTime) -> None:
        super().__init__()
        self.ft = fake_time
        self.at: list[float] = []

    def send(self, topic: str, sanitized: dict[str, Any]) -> None:
        super().send(topic, sanitized)
        self.at.append(self.ft.t)


async def _supervise(
    svc: LiotService, factory: Callable[[], Awaitable[HABridge]], max_sessions: int
) -> None:
    await asyncio.wait_for(svc._ha_supervisor(factory, max_sessions=max_sessions), timeout=15)


async def test_no_ha_at_startup_notifies_once_at_120s_then_resumes_once_never_guardian() -> None:
    """시작 뒤 첫 연결이 120초 안에 안 되면 '감시 중단' 1회, 붙으면 '감시 재개' 1회(거주자 토픽만)."""
    ft = FakeTime()
    transport = _TimedTransport(ft)
    svc, db, _ = await _make(transport=transport, fake_time=ft)
    world = _world()
    # 시도: 실패 6번(0·1·3·7·15·31초) 뒤 63초에 또 실패, 마지막은 123초에 성공.
    factory = ScriptedFactory(world, ft, ["fail"] * 9 + [("ok", 5.0)], transport)
    try:
        guardian_topic = await _guardian(svc)
        await _supervise(svc, factory, max_sessions=10)
        stopped, resumed = _stopped(transport), _resumed(transport)
        assert len(stopped) == 1 and len(resumed) == 1
        # 120초 경계에서 정확히 한 번: 백오프가 60초여도 경계에서 깨어 알린다.
        assert 120.0 <= transport.at[0] < 120.1, transport.at
        assert transport.at[1] > transport.at[0]
        top, payload = stopped[0]
        assert top == TOPIC and payload["severity_label"] == "경고"
        assert set(payload) == {"channel", "situation_label", "home_alias", "at", "severity_label"}
        assert set(payload) <= NTFY_ALLOWED_KEYS
        assert resumed[0][1]["severity_label"] == "주의"
        assert all(t == TOPIC for t, _ in transport.sent)
        assert all(t != guardian_topic for t, _ in transport.sent)
        lost = await _audit(db, "ha_stream_lost")
        assert [(r["ref_id"], r["notified"], r["charter_enabled"]) for r in lost] == [
            ("startup", True, True)
        ]
        restored = await _audit(db, "ha_stream_restored")
        assert [(r["notified"], r["stop_delivered"]) for r in restored] == [(True, True)]
    finally:
        await db.close()


async def test_outage_that_crosses_120s_while_resyncing_is_still_stopped_then_resumed() -> None:
    """63초에 붙었지만 재동기화가 오래 걸려 복구가 133초에 끝났다: 120초를 넘긴 끊김이므로
    중단 → 재개 순서로 낸다(복구 순간에 한 번 더 판정한다)."""
    ft = FakeTime()
    transport = _TimedTransport(ft)
    svc, db, _ = await _make(transport=transport, fake_time=ft)
    world = _world()
    world.on_get_states = lambda sink: setattr(ft, "t", ft.t + 70.0)  # get_states 가 70초 걸린다
    factory = ScriptedFactory(world, ft, ["fail"] * 6 + [("ok", 5.0)], transport)
    try:
        await _supervise(svc, factory, max_sessions=7)
        assert len(_stopped(transport)) == 1 and len(_resumed(transport)) == 1
        assert transport.at == [133.0, 133.0]
    finally:
        await db.close()


async def test_outage_below_120s_sends_nothing() -> None:
    ft = FakeTime()
    transport = _TimedTransport(ft)
    svc, db, _ = await _make(transport=transport, fake_time=ft)
    # 0·1·3·7·15·31 초에 실패하고 63초에 성공: 63초 끊김.
    factory = ScriptedFactory(_world(), ft, ["fail"] * 6 + [("ok", 5.0)], transport)
    try:
        await _supervise(svc, factory, max_sessions=7)
        assert transport.sent == []
        assert await _audit(db, "ha_stream_lost") == []
    finally:
        await db.close()


async def test_flapping_is_notified_by_the_cumulative_rule_once() -> None:
    """300초 살고 63초 끊기기를 되풀이: 끊김 하나는 120초 미만이지만 10분 안 누적이 120초를 넘는다.

    중단은 한 번(누적), 재개는 한 번. 첫 번째 끊김(63초)만으로는 알리지 않는다.
    """
    ft = FakeTime()
    transport = _TimedTransport(ft)
    svc, db, _ = await _make(transport=transport, fake_time=ft)
    # 1: 300초 생존 → 실패 5번(63초) → 7: 300초 생존 → 실패 5번 → 13: 성공(짧게).
    outcomes: list[Any] = [("ok", 300.0)] + ["fail"] * 5 + [("ok", 300.0)] + ["fail"] * 5 + [("ok", 1.0)]
    factory = ScriptedFactory(_world(), ft, outcomes, transport)
    try:
        await _supervise(svc, factory, max_sessions=13)
        assert factory.sent_at[7] == 0, "첫 끊김(63초)에서는 알리지 않는다"
        assert len(_stopped(transport)) == 1 and len(_resumed(transport)) == 1
        lost = await _audit(db, "ha_stream_lost")
        assert [r["ref_id"] for r in lost] == ["cumulative"]
    finally:
        await db.close()


async def test_charter_off_gives_zero_notices_but_audit_rows_remain() -> None:
    ft = FakeTime()
    transport = _TimedTransport(ft)
    svc, db, _ = await _make(transport=transport, fake_time=ft)
    await svc.prefs.set("charter_enabled", False)
    factory = ScriptedFactory(_world(), ft, ["fail"] * 9 + [("ok", 5.0)], transport)
    try:
        await _supervise(svc, factory, max_sessions=10)
        assert transport.sent == []
        lost = await _audit(db, "ha_stream_lost")
        assert [(r["notified"], r["charter_enabled"]) for r in lost] == [(False, False)]
        restored = await _audit(db, "ha_stream_restored")
        assert [(r["notified"], r["charter_enabled"]) for r in restored] == [(False, False)]
    finally:
        await db.close()


@pytest.mark.parametrize(
    "key,value", [("muted_categories", ["gas", "door", "smoke"]), ("muted_entities", [GAS, SMOKE])]
)
async def test_mute_does_not_hide_the_stopped_or_resumed_notice(key: str, value: list[str]) -> None:
    ft = FakeTime()
    transport = _TimedTransport(ft)
    svc, db, _ = await _make(transport=transport, fake_time=ft)
    await svc.prefs.set(key, value)
    factory = ScriptedFactory(_world(), ft, ["fail"] * 9 + [("ok", 5.0)], transport)
    try:
        await _supervise(svc, factory, max_sessions=10)
        assert len(_stopped(transport)) == 1 and len(_resumed(transport)) == 1
    finally:
        await db.close()


async def test_failed_stop_notice_send_means_no_resume_notice_and_does_not_block() -> None:
    """중단 알림이 안 나갔으면(전송 실패) 재개만 알리지 않는다. 감독은 계속 돈다."""
    ft = FakeTime()
    transport = FakeNtfyTransport(fail=True)
    svc, db, _ = await _make(transport=transport, fake_time=ft)
    factory = ScriptedFactory(_world(), ft, ["fail"] * 9 + [("ok", 5.0)])
    try:
        await _supervise(svc, factory, max_sessions=10)
        lost = await _audit(db, "ha_stream_lost")
        assert [r["notified"] for r in lost] == [False]
        restored = await _audit(db, "ha_stream_restored")
        assert [(r["stop_delivered"], r["notified"]) for r in restored] == [(False, False)]
    finally:
        await db.close()


async def test_auth_rejection_counts_as_an_outage_with_300s_backoff_and_its_own_log(
    caplog: pytest.LogCaptureFixture,
) -> None:
    ft = FakeTime()
    transport = _TimedTransport(ft)
    svc, db, _ = await _make(transport=transport, fake_time=ft)
    world = _world()
    world.reject_auth = True

    async def factory() -> HABridge:
        # 두 번째 시도부터는 토큰이 받아들여진다(운영자가 고쳤다).
        if len(attempts) == 1:
            world.reject_auth = False
        attempts.append(ft.t)
        return HABridge(world.open_conn(lifetime=5.0, fake_time=ft), "tok", request_timeout_s=5.0)

    attempts: list[float] = []
    caplog.set_level(logging.INFO, logger="liot.service")
    try:
        await _supervise(svc, factory, max_sessions=2)
        assert attempts == [0.0, 300.0]  # 인증 거부 뒤 5분 대기
        assert any("authentication rejected" in r.message for r in caplog.records)
        assert not any("connection lost (auth_rejected)" in r.message for r in caplog.records)
        # 120초 경계에서 깨어 알렸다(300초 대기를 통째로 자지 않는다).
        assert 120.0 <= transport.at[0] < 120.1
        assert len(_stopped(transport)) == 1 and len(_resumed(transport)) == 1
    finally:
        await db.close()


async def test_backoff_resets_only_after_five_minutes_of_stable_connection() -> None:
    ft = FakeTime()
    svc, db, _ = await _make(fake_time=ft)
    # 400초 생존(안정) → 1·2·4 대기 → 100초 생존(불안정, 초기화 없음) → 8·16 → 400초 생존 → 1.
    outcomes: list[Any] = [("ok", 400.0), "fail", "fail", ("ok", 100.0), "fail", ("ok", 400.0)]
    factory = ScriptedFactory(_world(), ft, outcomes)
    try:
        await _supervise(svc, factory, max_sessions=7)
        assert ft.sleeps == [1.0, 2.0, 4.0, 8.0, 16.0, 1.0], ft.sleeps
    finally:
        await db.close()


# ==========================================================================
# 5) 감독: 세션 순서·재동기화
# ==========================================================================
async def test_event_between_snapshot_and_subscribe_is_not_lost() -> None:
    """get_states 응답 전에 끼어든 가스 이벤트가 알림을 낸다(구독 → get_states 순서 + 버퍼).

    스냅샷에는 가스가 아직 off 로 찍혀 있다(스냅샷 뒤에 켜졌다). 이벤트가 버려졌다면 재동기화는
    정상만 보고 지나가 침묵한다.
    """
    world = _world()

    def on_get_states(sink: Any) -> None:
        sink.push(_event(GAS, "on"))  # 응답보다 먼저 도착하는 state_changed

    world.on_get_states = on_get_states
    svc, db, transport = await _make()
    task = _start(svc, world.factory)
    try:
        await _until(lambda: _is_open(svc, GAS))
        assert len(transport.sent) == 1
        assert world.log[:3] == ["subscribe", "get_config", "get_states"]  # 구독이 먼저다
    finally:
        await _stop(task)
        await db.close()


async def test_resync_feeds_current_safety_values_abnormal_first() -> None:
    """꺼져 있던 동안 이상이 된 센서는 시작 직후 재동기화로 알림이 나간다. 이상 → 정상 → 감시 불가 순."""
    world = World()
    world.set(SMOKE, "unavailable", "smoke")  # 감시 불가
    world.set(DOOR, "off", "door")  # 정상
    world.set(GAS, "on", "gas")  # 이상
    world.set(LIGHT, "on")  # 비안전은 흘리지 않는다
    svc, db, transport = await _make()
    seen: list[str] = []
    real = svc.handle_reading

    async def spy(reading: SensorReading) -> Any:
        seen.append(reading.entity_id)
        return await real(reading)

    svc.handle_reading = spy
    task = _start(svc, world.factory)
    try:
        await _until(lambda: len(seen) >= 3)
        assert seen == [GAS, DOOR, SMOKE]
        assert await _open_entities(svc) == {GAS}
    finally:
        await _stop(task)
        await db.close()


async def test_resync_skips_entities_whose_state_equals_the_last_value_fed() -> None:
    """재연결마다 같은 상태를 다시 흘리지 않는다. 값이 바뀐 엔티티만 흘린다."""
    world = _world()
    world.set(GAS, "on", "gas")
    svc, db, transport = await _make()
    fed: list[tuple[str, Any]] = []
    real = svc.handle_reading

    async def spy(reading: SensorReading) -> Any:
        fed.append((reading.entity_id, reading.raw_value))
        return await real(reading)

    svc.handle_reading = spy
    task = _start(svc, world.factory)
    try:
        await _until(lambda: _is_open(svc, GAS))
        first = list(fed)
        assert (GAS, "on") in first
        sent = len(transport.sent)
        world.drop_all()  # 재연결 — 상태는 그대로
        await _until(lambda: world.states_requests >= 2)
        await asyncio.sleep(0.1)
        assert fed == first, "같은 값을 다시 흘렸다"
        assert len(transport.sent) == sent  # 알림 중복 없음
        world.set(GAS, "off", "gas")  # 끊긴 동안 정상으로 돌아왔다
        world.drop_all()
        await _until(lambda: world.states_requests >= 3 and (GAS, "off") in fed)
        await _until(lambda: _is_gone(svc, GAS))
    finally:
        await _stop(task)
        await db.close()


async def _is_gone(svc: LiotService, eid: str) -> bool:
    return eid not in await _open_entities(svc)


async def test_hasty_resync_waits_for_ha_running_and_skips_restored_placeholders() -> None:
    """HA 가 막 시작했다: get_config 가 RUNNING 이 될 때까지 기다린 뒤에야 get_states 를 부른다.

    그동안 스트림은 받는다(기동 중 끼어든 문 열림 이벤트가 알림을 낸다). 복원 자리표시자
    (``restored: true``, ``unavailable``)는 재동기화에서 건너뛴다 — 흘렸다면 '감시 불가' 알림이 나간다.
    """
    world = _world()
    world.set(GAS, "unavailable", "gas", restored=True)
    world.core_states = ["NOT_RUNNING", "STARTING", "RUNNING"]
    world.on_get_config = lambda n: world.emit(DOOR, "on") if n == 2 else None
    svc, db, transport = await _make()
    task = _start(svc, world.factory)
    try:
        await _until(lambda: world.states_requests >= 3)
        # RUNNING 확인(세 번째 get_config) 뒤에야 값을 흘리는 get_states(재동기화)가 마지막에 온다.
        # 기다리는 동안의 get_states 는 카테고리 맵만 채운다(값은 흘리지 않는다).
        assert world.log == [
            "subscribe", "get_config", "get_states", "get_config", "get_states",
            "get_config", "get_states",
        ]
        await _until(lambda: _is_open(svc, DOOR))  # 기동 중 스트림으로 받은 이벤트
        assert await _open_entities(svc) == {DOOR}
        unmonitorable = [p for _, p in transport.sent if "감시 불가" in str(p.get("situation_label"))]
        assert unmonitorable == [], "복원 자리표시자를 흘려 감시 불가 알림이 나갔다"
    finally:
        await _stop(task)
        await db.close()


async def test_wait_for_running_is_capped_then_resyncs_anyway() -> None:
    world = _world()
    world.set(GAS, "on", "gas")
    world.core_states = ["STARTING"]  # 영영 RUNNING 이 안 된다
    svc, db, transport = await _make()
    svc.ha_ready_cap_s = 0.1
    task = _start(svc, world.factory)
    try:
        await _until(lambda: _is_open(svc, GAS))  # 상한 뒤 그대로 재동기화해 알림이 나갔다
        assert world.config_polls >= 2
    finally:
        await _stop(task)
        await db.close()


async def test_known_safety_category_survives_restored_placeholder_and_partial_snapshot() -> None:
    """한 번 안전으로 분류한 엔티티는 복원 자리표시자·부분 get_states 가 비안전으로 덮지 못한다."""
    world = _world()
    svc, db, _ = await _make()
    task = _start(svc, world.factory)
    try:
        await _until(lambda: world.states_requests >= 1)
        assert svc.app._category_of(GAS) == "gas"
        world.set(GAS, "unavailable", None, restored=True)  # device_class 없는 자리표시자
        world.remove(SMOKE)  # 부분 스냅샷
        world.drop_all()
        await _until(lambda: world.states_requests >= 2)
        await asyncio.sleep(0.05)
        assert svc.app._category_of(GAS) == "gas"
        assert svc.app._category_of(SMOKE) == "smoke"
    finally:
        await _stop(task)
        await db.close()


async def test_entity_added_at_reconnect_is_classified_and_alerts() -> None:
    """HA 에 센서를 새로 붙이면 재연결 때 맵에 들어가 그 센서의 이상이 알림을 낸다."""
    world = _world()
    svc, db, transport = await _make()
    task = _start(svc, world.factory)
    new = "binary_sensor.garage_smoke"
    try:
        await _until(lambda: world.states_requests >= 1)
        assert svc.app._category_of(new) == "binary_sensor"  # 아직 모른다(비안전 = 침묵)
        world.set(new, "on", "smoke")
        world.drop_all()
        await _until(lambda: len(transport.sent) >= 1)
        assert await _is_open(svc, new)
        assert svc.app._category_of(new) == "smoke"
        assert len(transport.sent) == 1
    finally:
        await _stop(task)
        await db.close()


@pytest.mark.parametrize("holder", ["app", "context_builder", "proactive"])
def test_set_categories_updates_every_copy_of_the_map(holder: str) -> None:
    """앱·ContextBuilder·ProactiveGenerator 는 각자 사본을 쥔다 — 셋 다 새 엔티티를 봐야 한다."""
    app = LiotApp(categories={}, llm_backend=_Backend())
    new = "binary_sensor.new_gas"
    assert new not in app._categories
    app.set_categories({new: "gas"})
    maps = {
        "app": app._categories,
        "context_builder": app.context_builder._categories,
        "proactive": app.proactive.categories,
    }
    assert maps[holder][new] == "gas"
    # 사본이다: 한 맵을 고쳐도 다른 맵이 따라 변하지 않는다(공유 dict 가 아니다).
    assert len({id(m) for m in maps.values()}) == 3


def test_set_categories_empty_falls_back_to_the_catalog_map_like_the_constructor() -> None:
    app = LiotApp(categories={"x.y": "gas"}, llm_backend=_Backend())
    app.set_categories({})
    assert app._categories == LiotApp._categories_from_catalog(app.catalog)
    assert "x.y" not in app._categories


# ==========================================================================
# 6) 감독: 신호 하나의 예외
# ==========================================================================
async def test_one_failing_reading_does_not_stop_the_loop_and_schedules_one_resync() -> None:
    """안전 신호 처리 중 예외: 로그만 남기고 루프는 계속 돈다. 그 신호는 다시 오지 않으므로
    재동기화를 한 번 예약해 현재값을 다시 흘린다(그 가스가 결국 알림을 낸다)."""
    world = _world()
    svc, db, transport = await _make()
    real = svc.handle_reading
    calls = {"gas": 0}

    async def flaky(reading: SensorReading) -> Any:
        if reading.entity_id == GAS and reading.raw_value == "on":
            calls["gas"] += 1
            if calls["gas"] == 1:
                raise RuntimeError("boom")
        return await real(reading)

    svc.handle_reading = flaky
    task = _start(svc, world.factory)
    try:
        await _until(lambda: world.states_requests >= 1)
        world.emit(GAS, "on")  # 첫 처리는 실패한다
        await _until(lambda: world.states_requests >= 2)  # 재동기화가 예약돼 돌았다
        await _until(lambda: _is_open(svc, GAS))  # 재동기화가 다시 흘려 알림이 나갔다
        assert calls["gas"] == 2
        world.emit(DOOR, "on")  # 루프는 살아 있다
        await _until(lambda: _is_open(svc, DOOR))
    finally:
        await _stop(task)
        await db.close()


async def test_failing_non_safety_reading_does_not_schedule_a_resync() -> None:
    world = _world()
    svc, db, transport = await _make()
    real = svc.handle_reading

    async def flaky(reading: SensorReading) -> Any:
        if reading.entity_id == LIGHT:
            raise RuntimeError("boom")
        return await real(reading)

    svc.handle_reading = flaky
    task = _start(svc, world.factory)
    try:
        await _until(lambda: world.states_requests >= 1)
        world.emit(LIGHT, "on")
        world.emit(DOOR, "on")  # 이 신호가 처리되면 위 비안전 신호는 이미 지나갔다
        await _until(lambda: _is_open(svc, DOOR))
        assert world.states_requests == 1
    finally:
        await _stop(task)
        await db.close()


async def test_failure_clears_last_fed_so_resync_refeeds_the_same_value() -> None:
    """처리에 실패한 엔티티는 '마지막으로 흘린 값'에서 지운다 — 같은 값이어도 재동기화가 다시 흘린다."""
    world = _world()
    world.set(GAS, "on", "gas")
    svc, db, transport = await _make()
    real = svc.handle_reading
    fed: list[Any] = []

    async def flaky(reading: SensorReading) -> Any:
        if reading.entity_id == GAS:
            fed.append(reading.raw_value)
            if len(fed) == 2:  # 두 번째(스트림의 같은 값 'on') 처리가 실패한다
                raise RuntimeError("boom")
        return await real(reading)

    svc.handle_reading = flaky
    task = _start(svc, world.factory)
    try:
        await _until(lambda: len(fed) >= 1)  # 재동기화가 'on' 을 흘렸다(성공)
        world.emit(GAS, "on")  # 같은 값의 이벤트(속성만 바뀜) — 처리 실패
        await _until(lambda: len(fed) >= 3)  # 예약된 재동기화가 같은 'on' 을 다시 흘렸다
        assert fed == ["on", "on", "on"]
    finally:
        await _stop(task)
        await db.close()


async def test_failures_during_resync_do_not_rearm_the_resync() -> None:
    """재동기화 도중의 실패는 재동기화를 또 예약하지 않는다(영구 장애에서 쉬지 않고 도는 일을 막는다)."""
    world = _world()
    world.set(GAS, "on", "gas")
    svc, db, transport = await _make()

    async def broken(reading: SensorReading) -> Any:
        raise RuntimeError("db down")

    svc.handle_reading = broken
    task = _start(svc, world.factory)
    try:
        await _until(lambda: world.states_requests >= 1)
        await asyncio.sleep(0.3)
        assert world.states_requests == 1
        world.emit(GAS, "off")  # 스트림 실패 하나 → 재동기화 한 번
        await _until(lambda: world.states_requests >= 2)
        await asyncio.sleep(0.3)
        assert world.states_requests == 2
    finally:
        await _stop(task)
        await db.close()


async def test_single_bridge_run_isolates_reading_exceptions_and_returns_when_stream_ends() -> None:
    """``run(bridge)`` 는 예전처럼 한 번 소비하고 돌아온다(stream() 만 있는 객체도 받는다).
    신호 하나의 예외가 그 뒤 신호를 막지 않는다."""
    svc, db, transport = await _make()
    real = svc.handle_reading

    async def flaky(reading: SensorReading) -> Any:
        if reading.entity_id == DOOR:
            raise RuntimeError("boom")
        return await real(reading)

    svc.handle_reading = flaky

    class _Stream:
        async def stream(self):  # noqa: ANN202
            yield _reading(DOOR, "on")  # 실패
            yield _reading(GAS, "on")

    svc.app.set_categories({GAS: "gas", DOOR: "door"})
    try:
        await asyncio.wait_for(
            svc.run(_Stream(), tick_seconds=3600.0, max_ticks=1, ladder_resolution_s=3600.0),
            timeout=10,
        )
        assert await _open_entities(svc) == {GAS}
    finally:
        await svc.drain_phase2()
        await db.close()


async def test_run_rejects_bridge_and_ha_factory_together() -> None:
    svc, db, _ = await _make()
    try:
        with pytest.raises(ValueError, match="함께"):
            await svc.run(object(), ha_factory=_world().factory)
    finally:
        await db.close()


# ==========================================================================
# 7) 사다리는 HA 와 독립이다
# ==========================================================================
async def test_ladder_keeps_firing_while_ha_is_unreachable_from_the_start() -> None:
    """HA 가 처음부터 닿지 않아도(감독이 재시도 중) 열린 상황의 사다리는 재알림을 낸다."""
    world = _world()
    world.refuse = True
    svc, db, transport = await _make()
    # 이전 프로세스가 남긴 열린 상황: 데몬을 띄우기 전에 가스 상황을 직접 연다.
    svc.app.set_categories({GAS: "gas"})
    res = await svc.handle_reading(_reading(GAS, "on"))
    assert res is not None
    await svc.drain_phase2()
    first = len(transport.sent)
    task = _start(svc, world.factory)
    try:
        await asyncio.sleep(0.1)
        assert not task.done()
        svc.clock.set_fixed("2026-06-02T12:01:01+00:00")  # 61초 뒤: CRITICAL 재알림
        await _until(lambda: len(transport.sent) > first)
        assert world.connects == 0  # 끝까지 HA 에는 붙지 못했다
    finally:
        await _stop(task)
        await db.close()


# ==========================================================================
# 8) 실행기: 호출마다 짧은 별도 연결, HA 가 없으면 fail-closed
# ==========================================================================
def _act(cap: str, action: str, category: str, entity: str) -> CandidateAction:
    return CandidateAction(
        capability=cap, action=action, category=category, entity_id=entity, params={}
    )


async def test_executor_opens_a_short_connection_per_call_and_closes_it() -> None:
    """get_state 와 call_service 가 각자 새 연결을 열고(인증 포함) 끝나면 닫는다 — 스트림 소켓과 무관."""
    world = _world()
    world.set("lock.front_door", "unlocked")
    opened: list[Any] = []

    async def factory() -> HABridge:
        bridge = await world.factory()
        opened.append(bridge._conn)
        return bridge

    ex = HAExecutor(
        functools.partial(call_service_once, factory),
        functools.partial(get_state_once, factory),
    )
    res = await ex.execute(_act("lock", "lock", "lock", "lock.front_door"))
    assert res.ok and res.service == "lock.lock"
    assert res.prior_state is not None and res.prior_state["state"] == "unlocked"
    assert len(opened) == 2 and opened[0] is not opened[1]
    assert all(c.closed for c in opened)
    assert world.calls == [(opened[1].ident, "lock", "lock", "lock.front_door")]
    # 인증을 거쳤다(auth 메시지가 첫 송신).
    assert all(c.sent[0]["type"] == "auth" for c in opened)


async def test_executor_fails_closed_when_ha_is_down() -> None:
    """HA 에 닿지 못하면 조작은 실행 실패(ExecutionError)다 — 서비스는 False 로 받는다(실행 안 함)."""
    world = _world()
    world.refuse = True
    ex = HAExecutor(
        functools.partial(call_service_once, world.factory),
        functools.partial(get_state_once, world.factory),
    )
    action = _act("valve", "close", "water_main", "valve.main")
    with pytest.raises(ExecutionError):
        await ex.execute(action)
    assert world.calls == []

    from liot.layers.l4_execution.executor import ExecutionPolicy
    from liot.service import LiotService as _Svc

    svc, db, _ = await _make()
    try:
        svc2 = _Svc(svc.app, db, clock=svc.clock, transport=FakeNtfyTransport(), topic=TOPIC,
                    secret=SECRET, executor=ex, policy=ExecutionPolicy(enabled=True))

        class _Prop:
            proposal_id = "p-exec"

        _Prop.action = action  # type: ignore[attr-defined]
        assert await svc2._try_execute(_Prop()) is False  # type: ignore[arg-type]
    finally:
        await db.close()


async def test_executor_call_over_a_real_socket_does_not_disturb_the_stream() -> None:
    """스트림 소켓과 호출 소켓이 따로다: 같은 소켓에서 동시에 recv 하면 websockets 가 ConcurrencyError 를 낸다."""
    pytest.importorskip("websockets")
    world = _world()
    world.set("lock.front_door", "unlocked")
    srv = LoopbackHA(world)
    await srv.start()
    factory = functools.partial(connect_ha, srv.url, "good-token")
    svc, db, transport = await _make()
    task = _start(svc, factory)
    try:
        await _until(lambda: world.states_requests >= 1)
        ex = HAExecutor(
            functools.partial(call_service_once, factory),
            functools.partial(get_state_once, factory),
        )
        res = await asyncio.wait_for(
            ex.execute(_act("lock", "lock", "lock", "lock.front_door")), timeout=5
        )
        assert res.ok and [c[1:] for c in world.calls] == [("lock", "lock", "lock.front_door")]
        world.emit(GAS, "on")  # 스트림 연결은 그대로 살아 이벤트를 받는다
        await _until(lambda: len(transport.sent) >= 1)
        assert not task.done()
    finally:
        await _stop(task)
        await srv.stop()
        await db.close()


# ==========================================================================
# 9) serve: 신호·종료·DB 닫기·HA 없이 시작
# ==========================================================================
@pytest.fixture
def _daemon_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for key in (
        "LIOT_HOME_ID", "LIOT_NTFY_TOPIC", "LIOT_NTFY_URL", "LIOT_HA_URL", "LIOT_HA_TOKEN",
        "LIOT_FEEDBACK_ALLOW_DEV_KEY", "LIOT_EXECUTION_ENABLED", "LIOT_LLM_BACKEND",
        "LIOT_HOME_TZ",
    ):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("LIOT_DB_PATH", ":memory:")
    monkeypatch.setenv("LIOT_NOTIFY_SECRET", "a-real-long-signing-secret-value")
    monkeypatch.setenv("LIOT_FEEDBACK_PORT", "0")
    monkeypatch.setenv("LIOT_TICK_SECONDS", "3600")


def _spy_db(monkeypatch: pytest.MonkeyPatch) -> list[Any]:
    """serve 가 연 DB 연결을 잡아 둔다(종료 뒤 닫혔는지 보려고)."""
    dbs: list[Any] = []

    async def spy(path: str, **kwargs: Any) -> Any:
        db = await init_db(path, **kwargs)
        dbs.append(db)
        return db

    monkeypatch.setattr("liot.serve.init_db", spy)
    return dbs


async def _assert_closed(db: Any) -> None:
    with pytest.raises(ValueError, match="no active connection"):
        await db.execute("SELECT 1")


def _park_run(monkeypatch: pytest.MonkeyPatch) -> asyncio.Event:
    """service.run 을 '영원히 기다림'으로 바꾼다. 들어서면 이벤트가 선다."""
    entered = asyncio.Event()

    async def fake_run(self: LiotService, bridge: Any = None, *, ha_factory: Any = None,
                       tick_seconds: float = 0.0) -> None:
        entered.set()
        await asyncio.Event().wait()

    monkeypatch.setattr(LiotService, "run", fake_run)
    return entered


@pytest.mark.parametrize("sig", [signal.SIGTERM, signal.SIGINT])
async def test_amain_stops_cleanly_on_signal_and_closes_db_and_restores_handlers(
    monkeypatch: pytest.MonkeyPatch, _daemon_env: None, sig: signal.Signals,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """SIGTERM(launchd)·SIGINT: 주 태스크를 취소 → finally 가 수신구·DB 를 닫고 종료 코드 0."""
    import socket

    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        free_port = probe.getsockname()[1]
    monkeypatch.setenv("LIOT_FEEDBACK_PORT", str(free_port))
    dbs = _spy_db(monkeypatch)
    entered = _park_run(monkeypatch)
    before = signal.getsignal(sig)
    task = asyncio.create_task(serve._amain())
    await asyncio.wait_for(entered.wait(), timeout=5)
    handler = signal.getsignal(sig)
    assert handler != before, "종료 신호 처리기가 설치되지 않았다 — 신호가 프로세스를 그대로 죽인다"
    os.kill(os.getpid(), sig)
    assert await asyncio.wait_for(task, timeout=5) == 0
    await _assert_closed(dbs[0])
    assert signal.getsignal(sig) == before, "처리기를 되돌리지 않았다"
    with socket.socket() as check:  # 피드백 수신구도 닫혔다
        check.settimeout(1)
        assert check.connect_ex(("127.0.0.1", free_port)) != 0
    assert "종료 신호" in capsys.readouterr().out


async def test_stop_signals_cancel_the_main_task_only_once(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """정리 중(DB 닫는 중)에 신호가 또 와도 정리를 끊지 않는다 — 취소는 한 번만 건다."""
    loop = asyncio.get_running_loop()
    handlers: dict[Any, Callable[[], None]] = {}
    monkeypatch.setattr(loop, "add_signal_handler",
                        lambda sig, cb: handlers.__setitem__(sig, cb), raising=False)
    monkeypatch.setattr(loop, "remove_signal_handler", lambda sig: True, raising=False)

    class _Task:
        cancels = 0

        def cancel(self) -> None:
            self.cancels += 1

    task = _Task()
    stop = serve._StopSignals()
    stop.install(task)  # type: ignore[arg-type]
    assert set(handlers) == {signal.SIGTERM, signal.SIGINT}
    handlers[signal.SIGTERM]()
    handlers[signal.SIGINT]()
    handlers[signal.SIGTERM]()
    assert task.cancels == 1 and stop.requested
    stop.remove()


async def test_amain_external_cancel_propagates_but_still_closes_db(
    monkeypatch: pytest.MonkeyPatch, _daemon_env: None
) -> None:
    """신호가 아닌 취소(호출자·시험)는 삼키지 않고 올린다. DB 는 닫힌다."""
    dbs = _spy_db(monkeypatch)
    entered = _park_run(monkeypatch)
    task = asyncio.create_task(serve._amain())
    await asyncio.wait_for(entered.wait(), timeout=5)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    await _assert_closed(dbs[0])


async def test_amain_closes_db_when_startup_fails_after_the_db_is_open(
    monkeypatch: pytest.MonkeyPatch, _daemon_env: None
) -> None:
    """DB 를 연 뒤 어디서 실패하든 닫는다 — 예전에는 try 밖 실패가 비데몬 스레드를 남겨 프로세스가 멈췄다."""
    dbs = _spy_db(monkeypatch)

    def boom() -> Any:
        raise RuntimeError("transport build failed")

    monkeypatch.setattr(serve, "build_ntfy_transport", boom)
    with pytest.raises(RuntimeError, match="transport build failed"):
        await serve._amain()
    await _assert_closed(dbs[0])


async def test_amain_wires_the_executor_to_a_connection_factory_not_a_startup_bridge(
    monkeypatch: pytest.MonkeyPatch, _daemon_env: None
) -> None:
    """실행기는 시작 때 연 브리지가 아니라 연결 팩토리(호출마다 새 연결)에 묶인다."""
    monkeypatch.setenv("LIOT_EXECUTION_ENABLED", "1")
    monkeypatch.setenv("LIOT_HA_URL", "ws://127.0.0.1:1/api/websocket")
    monkeypatch.setenv("LIOT_HA_TOKEN", "tok")
    seen: dict[str, Any] = {}

    async def fake_run(self: LiotService, bridge: Any = None, *, ha_factory: Any = None,
                       tick_seconds: float = 0.0) -> None:
        seen["executor"] = self._executor
        seen["ha_factory"] = ha_factory

    monkeypatch.setattr(LiotService, "run", fake_run)
    assert await serve._amain() == 0
    assert seen["ha_factory"] is not None
    ex = seen["executor"]
    assert isinstance(ex, HAExecutor)
    call = functools.partial(call_service_once, seen["ha_factory"])
    state = functools.partial(get_state_once, seen["ha_factory"])
    assert (ex._call.func, ex._call.args) == (call.func, call.args)  # type: ignore[attr-defined]
    assert (ex._get_state.func, ex._get_state.args) == (state.func, state.args)  # type: ignore[union-attr]


async def test_daemon_with_no_ha_at_startup_retries_keeps_ladder_alive_and_closes_db_on_term(
    monkeypatch: pytest.MonkeyPatch, _daemon_env: None, caplog: pytest.LogCaptureFixture,
) -> None:
    """HA 가 없는 채 시작: 멈추지 않고 재시도한다(예전: 프로세스가 멈춘 채 남음). 사다리 루프는 계속
    돈다. SIGTERM 으로 DB 를 닫고 끝난다. (``LIOT_HA_URL=ws://127.0.0.1:1`` 재현)"""
    monkeypatch.setenv("LIOT_HA_URL", "ws://127.0.0.1:1/api/websocket")
    monkeypatch.setenv("LIOT_HA_TOKEN", "tok")
    caplog.set_level(logging.INFO, logger="liot.service")
    dbs = _spy_db(monkeypatch)
    ticks = {"n": 0}
    real_tick = LiotService.ladder_tick
    real_run = LiotService.run

    async def counting_tick(self: LiotService, *a: Any, **k: Any) -> Any:
        ticks["n"] += 1
        return await real_tick(self, *a, **k)

    async def fast_ladder_run(self: LiotService, *a: Any, **k: Any) -> None:
        k.setdefault("ladder_resolution_s", 0.05)
        await real_run(self, *a, **k)

    monkeypatch.setattr(LiotService, "ladder_tick", counting_tick)
    monkeypatch.setattr(LiotService, "run", fast_ladder_run)
    before_term = signal.getsignal(signal.SIGTERM)
    task = asyncio.create_task(serve._amain())
    try:
        await _until(
            lambda: sum("connection lost" in r.message for r in caplog.records) >= 2, timeout=8
        )
        assert not task.done(), "HA 가 없어도 프로세스는 끝나지 않고 재시도해야 한다"
        n = ticks["n"]
        await _until(lambda: ticks["n"] >= n + 3, timeout=5)  # 사다리 루프는 재시도 중에도 돈다
        # 처리기가 없으면 SIGTERM 이 시험 프로세스를 죽인다 — 보내기 전에 설치됐는지 확인한다.
        assert signal.getsignal(signal.SIGTERM) != before_term
        os.kill(os.getpid(), signal.SIGTERM)
        assert await asyncio.wait_for(task, timeout=5) == 0
        await _assert_closed(dbs[0])
    finally:
        if not task.done():
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
