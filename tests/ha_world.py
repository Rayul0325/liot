"""HA 재연결 시험용 가짜 HA 세계 — 전송과 무관한 HA 쪽 규칙 한 벌.

``World`` 는 HA 가 하는 일만 안다(인증·구독·get_config·get_states·call_service·이벤트 팬아웃).
전송은 둘이다.
  - ``MemConn``     : 메모리 큐 위의 ``HAConnection``(빠르고 결정적 — 대부분의 시험).
  - ``LoopbackHA``  : 진짜 websockets 서버(루프백, 포트 0). 닫았다 같은 포트로 다시 연다.

외부 네트워크는 쓰지 않는다. 시각은 ``FakeTime`` 으로 감독의 120초 경계를 가짜로 흘린다.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
from collections.abc import Callable
from typing import Any

from liot.layers.l1_sensing.ha_bridge import (
    HABridge,
    HAConnectionClosed,
    HAConnectionError,
)

T0 = "2026-06-02T12:00:00+00:00"


class FakeTime:
    """감독의 시계·잠자기를 대신한다: 잠자기가 시계를 그만큼 앞으로 옮기고 즉시 돌아온다."""

    def __init__(self) -> None:
        self.t = 0.0
        self.sleeps: list[float] = []

    def now(self) -> float:
        return self.t

    async def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.t += seconds
        await asyncio.sleep(0)


class _Raw:
    """JSON 이 아닌 원문을 그대로 보내라는 표식(형식 오류 시험)."""

    def __init__(self, text: str) -> None:
        self.text = text


def state_dict(
    eid: str, state: str, device_class: str | None = None, *, restored: bool = False
) -> dict[str, Any]:
    attrs: dict[str, Any] = {}
    if device_class:
        attrs["device_class"] = device_class
    if restored:
        attrs["restored"] = True
    return {"entity_id": eid, "state": state, "attributes": attrs, "last_changed": T0}


class World:
    """HA 한 대의 상태와 응답 규칙."""

    def __init__(self) -> None:
        self.states: dict[str, dict[str, Any]] = {}
        self.sinks: set[Any] = set()
        self.log: list[str] = []  # 받은 요청 종류를 순서대로(subscribe·get_config·get_states …)
        self.core_states: list[str] = ["RUNNING"]
        self.config_polls = 0
        self.states_requests = 0
        self.calls: list[tuple[int, str, str, str]] = []  # (연결 번호, 도메인, 서비스, 엔티티)
        self.connects = 0
        self.refuse = False
        self.reject_auth = False
        # get_states 응답 직전에 불리는 훅(그 연결에 이벤트를 먼저 흘리는 시험용).
        self.on_get_states: Callable[[Any], None] | None = None
        # get_config 응답 직전 훅(폴링 횟수 n).
        self.on_get_config: Callable[[int], None] | None = None

    def set(
        self, eid: str, state: str, device_class: str | None = None, *, restored: bool = False
    ) -> None:
        self.states[eid] = state_dict(eid, state, device_class, restored=restored)

    def remove(self, eid: str) -> None:
        self.states.pop(eid, None)

    def emit(self, eid: str, state: str) -> None:
        """상태를 바꾸고 구독한 모든 연결에 state_changed 이벤트를 보낸다."""
        old = self.states.get(eid) or state_dict(eid, state)
        new = {**old, "state": state}
        self.states[eid] = new
        event = {
            "type": "event",
            "event": {
                "event_type": "state_changed",
                "data": {"entity_id": eid, "new_state": new},
            },
        }
        for sink in list(self.sinks):
            if sink.subscribed:
                sink.push(event)

    def _result(self, obj: dict[str, Any], result: Any = None) -> dict[str, Any]:
        return {"id": obj["id"], "type": "result", "success": True, "result": result}

    def handle(self, sink: Any, obj: dict[str, Any]) -> None:
        kind = obj.get("type")
        if kind == "auth":
            if self.reject_auth:
                sink.push({"type": "auth_invalid", "message": "bad token"})
            else:
                sink.push({"type": "auth_ok"})
        elif kind == "subscribe_events":
            self.log.append("subscribe")
            sink.subscribed = True
            sink.push(self._result(obj))
        elif kind == "get_config":
            self.log.append("get_config")
            self.config_polls += 1
            if self.on_get_config is not None:
                self.on_get_config(self.config_polls)
            idx = min(self.config_polls - 1, len(self.core_states) - 1)
            sink.push(self._result(obj, {"state": self.core_states[idx]}))
        elif kind == "get_states":
            self.log.append("get_states")
            self.states_requests += 1
            snapshot = [dict(v) for v in self.states.values()]
            if self.on_get_states is not None:
                self.on_get_states(sink)
            sink.push(self._result(obj, snapshot))
        elif kind == "call_service":
            self.log.append("call_service")
            self.calls.append(
                (sink.ident, obj["domain"], obj["service"], obj["target"]["entity_id"])
            )
            sink.push(self._result(obj))
        else:
            sink.push({"id": obj.get("id"), "type": "result", "success": False,
                       "error": {"code": "unknown_command"}})

    # ── 메모리 전송 ──────────────────────────────────────────────────────
    async def factory(self) -> HABridge:
        """새 메모리 연결을 열어 브리지로 돌려준다(``refuse`` 면 열지 못한다)."""
        return HABridge(self.open_conn(), "tok", request_timeout_s=5.0)

    def open_conn(self, **kw: Any) -> MemConn:
        if self.refuse:
            raise HAConnectionError("connection refused")
        self.connects += 1
        conn = MemConn(self, self.connects, **kw)
        self.sinks.add(conn)
        return conn

    def drop_all(self, exc: BaseException | None = None) -> None:
        """모든 연결을 끊는다(다음 recv 가 예외)."""
        for sink in list(self.sinks):
            sink.push(exc if exc is not None else HAConnectionClosed("ws closed"))


class MemConn:
    """메모리 큐 위의 HAConnection. 큐가 비면 recv 는 기다린다(스트림이 조용한 상태)."""

    def __init__(
        self, world: World, ident: int, *, lifetime: float | None = None,
        fake_time: FakeTime | None = None,
    ) -> None:
        self.world = world
        self.ident = ident
        self.subscribed = False
        self.closed = False
        self.sent: list[dict[str, Any]] = []
        self._inbox: asyncio.Queue[Any] = asyncio.Queue()
        self._inbox.put_nowait({"type": "auth_required"})
        # lifetime 이 있으면 큐가 비는 순간(= 스트림 단계) 가짜 시계를 그만큼 옮기고 연결이 끊긴다.
        self._lifetime = lifetime
        self._ft = fake_time

    def push(self, msg: Any) -> None:
        self._inbox.put_nowait(msg)

    async def send(self, obj: dict[str, Any]) -> None:
        if self.closed:
            raise HAConnectionClosed("send on closed connection")
        self.sent.append(obj)
        self.world.handle(self, obj)

    async def recv(self) -> dict[str, Any]:
        if self._lifetime is not None and self._inbox.empty():
            assert self._ft is not None
            self._ft.t += self._lifetime
            raise HAConnectionClosed("scripted end of life")
        item = await self._inbox.get()
        if isinstance(item, BaseException):
            raise item
        return item  # type: ignore[no-any-return]

    async def close(self) -> None:
        self.closed = True
        self.world.sinks.discard(self)


class ScriptedFactory:
    """연결 시도마다 결과를 정한다. 'fail' 이면 HAConnectionError, ('ok', 수명) 이면 그 수명만큼 산다.

    스크립트를 다 쓰면 이후는 'fail'. 시도 시각(가짜 시계)과 그때까지 나간 알림 수를 기록한다.
    """

    def __init__(
        self, world: World, fake_time: FakeTime, outcomes: list[Any], transport: Any = None
    ) -> None:
        self.world = world
        self.ft = fake_time
        self.outcomes = outcomes
        self.transport = transport
        self.times: list[float] = []
        self.sent_at: list[int] = []

    async def __call__(self) -> HABridge:
        i = len(self.times)
        self.times.append(self.ft.t)
        self.sent_at.append(len(self.transport.sent) if self.transport is not None else 0)
        outcome = self.outcomes[i] if i < len(self.outcomes) else "fail"
        if outcome == "fail":
            raise HAConnectionError("scripted failure")
        conn = self.world.open_conn(lifetime=outcome[1], fake_time=self.ft)
        return HABridge(conn, "tok", request_timeout_s=5.0)


# ---------------------------------------------------------------------------
# 진짜 websockets 루프백 서버
# ---------------------------------------------------------------------------
class _WsSink:
    def __init__(self, ws: Any, ident: int) -> None:
        self.ws = ws
        self.ident = ident
        self.subscribed = False
        self.out: asyncio.Queue[Any] = asyncio.Queue()

    def push(self, msg: Any) -> None:
        self.out.put_nowait(msg)


class LoopbackHA:
    """``World`` 를 진짜 WebSocket 서버로 노출한다. ``stop()`` 후 같은 포트로 ``start()`` 하면 다시 열린다."""

    def __init__(self, world: World) -> None:
        self.world = world
        self._server: Any = None
        self.port = 0
        # True 면 다음 연결은 구독 응답 직후 JSON 이 아닌 원문을 한 번 보낸다(형식 오류).
        self.malformed_once = False
        self._count = 0

    @property
    def url(self) -> str:
        return f"ws://127.0.0.1:{self.port}/api/websocket"

    async def start(self, port: int = 0) -> None:
        import websockets

        self._server = await websockets.serve(self._handler, "127.0.0.1", port)
        self.port = self._server.sockets[0].getsockname()[1]

    async def stop(self) -> None:
        if self._server is not None:
            self._server.close()
            await self._server.wait_closed()
            self._server = None

    async def _handler(self, ws: Any) -> None:
        self._count += 1
        sink = _WsSink(ws, self._count)
        self.world.connects += 1
        self.world.sinks.add(sink)

        async def writer() -> None:
            while True:
                msg = await sink.out.get()
                if isinstance(msg, _Raw):
                    await ws.send(msg.text)
                else:
                    await ws.send(json.dumps(msg))

        wtask = asyncio.create_task(writer())
        try:
            await ws.send(json.dumps({"type": "auth_required"}))
            async for raw in ws:
                obj = json.loads(raw)
                self.world.handle(sink, obj)
                if obj.get("type") == "subscribe_events" and self.malformed_once:
                    self.malformed_once = False
                    sink.push(_Raw("this is not json"))
        except Exception:  # noqa: BLE001, S110 — 클라이언트가 먼저 끊은 경우
            pass
        finally:
            self.world.sinks.discard(sink)
            wtask.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await wtask
