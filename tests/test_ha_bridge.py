"""HA 브리지(L1) 검증 — 네트워크 없는 단위 + 로컬 mock HA 서버 실소켓.

핵심 검증:
  - infer_category: 안전 신호가 domain 이 아닌 device_class 에 있음을 정확히
    매핑(binary_sensor+gas → 'gas' 안전).
  - auth 핸드셰이크 성공/실패(auth_invalid → HAAuthError).
  - get_states → entity→카테고리 맵(가스 센서가 'gas').
  - state_changed 이벤트 → SensorReading.
  - **end-to-end 안전 흐름**: HA 가스 이벤트 → 브리지 카테고리 맵 → LiotApp →
    Proposal EMBER (device_class 갭이 실제로 닫혔는지).
  - **실소켓 왕복**: 로컬 mock HA 서버에 진짜 websockets 클라이언트로 인증+이벤트.
"""

from __future__ import annotations

import asyncio
import json

import pytest

from liot.app import LiotApp
from liot.core.risk import RiskTier
from liot.layers.l1_sensing.ha_bridge import (
    FakeHAConnection,
    HAAuthError,
    HABridge,
    WebsocketsHAConnection,
    infer_category,
)


# --------------------------------------------------------------------------
# infer_category — 안전 신호는 device_class 에 있다
# --------------------------------------------------------------------------
def test_infer_category_safety_lives_in_device_class() -> None:
    # domain 만 보면 'binary_sensor'(비안전)지만 device_class 로 'gas'(안전).
    assert infer_category("binary_sensor.kitchen_gas", {"device_class": "gas"}) == "gas"
    assert infer_category("binary_sensor.hall_smoke", {"device_class": "smoke"}) == "smoke"
    assert infer_category("binary_sensor.bath_leak", {"device_class": "moisture"}) == "leak"
    assert infer_category("binary_sensor.front", {"device_class": "door"}) == "door"
    # domain 이 직접 카테고리를 정하는 경우.
    assert infer_category("lock.front_door", {}) == "lock"
    assert infer_category("light.living_room", {}) == "light"
    assert infer_category("climate.bedroom", {}) == "climate"
    # 미상 → domain 접두사 폴백.
    assert infer_category("sensor.unknownthing", {}) == "sensor"


def test_infer_category_gas_is_safety_critical() -> None:
    from liot.core.risk import SAFETY_CRITICAL_CATEGORIES

    assert infer_category("binary_sensor.kitchen_gas", {"device_class": "gas"}) in SAFETY_CRITICAL_CATEGORIES


# --------------------------------------------------------------------------
# 인증 핸드셰이크
# --------------------------------------------------------------------------
def _echo_result(extra: dict | None = None):
    def _mk(sent: list[dict]) -> dict:
        out = {"id": sent[-1]["id"], "type": "result", "success": True}
        if extra:
            out.update(extra)
        return out

    return _mk


async def test_authenticate_success() -> None:
    conn = FakeHAConnection([{"type": "auth_required"}, {"type": "auth_ok"}])
    bridge = HABridge(conn, "tok")
    await bridge.authenticate()
    # 두 번째 send 가 auth 메시지여야 하고 토큰을 담는다.
    assert conn.sent[0]["type"] == "auth"
    assert conn.sent[0]["access_token"] == "tok"


async def test_authenticate_invalid_raises() -> None:
    conn = FakeHAConnection([{"type": "auth_required"}, {"type": "auth_invalid", "message": "bad"}])
    bridge = HABridge(conn, "bad")
    with pytest.raises(HAAuthError):
        await bridge.authenticate()


async def test_authenticate_unexpected_first_message() -> None:
    conn = FakeHAConnection([{"type": "result"}])
    bridge = HABridge(conn, "tok")
    with pytest.raises(HAAuthError):
        await bridge.authenticate()


# --------------------------------------------------------------------------
# get_states → 카테고리 맵
# --------------------------------------------------------------------------
GAS_STATE = {
    "entity_id": "binary_sensor.kitchen_gas",
    "state": "on",
    "attributes": {"device_class": "gas"},
    "last_changed": "2026-06-02T01:00:00+00:00",
}
LIGHT_STATE = {
    "entity_id": "light.living_room",
    "state": "off",
    "attributes": {},
    "last_changed": "2026-06-02T01:00:00+00:00",
}


async def test_entity_categories_maps_device_class() -> None:
    conn = FakeHAConnection(
        [
            {"type": "auth_required"},
            {"type": "auth_ok"},
            _echo_result({"result": [GAS_STATE, LIGHT_STATE]}),
        ]
    )
    bridge = HABridge(conn, "tok")
    cats = await bridge.entity_categories()
    assert cats["binary_sensor.kitchen_gas"] == "gas"  # 안전!
    assert cats["light.living_room"] == "light"


# --------------------------------------------------------------------------
# state_changed → SensorReading
# --------------------------------------------------------------------------
async def test_stream_yields_reading_from_event() -> None:
    conn = FakeHAConnection(
        [
            {"type": "auth_required"},
            {"type": "auth_ok"},
            _echo_result(),  # subscribe result
            {"type": "event", "event": {"event_type": "state_changed", "data": {"new_state": GAS_STATE}}},
        ]
    )
    bridge = HABridge(conn, "tok")
    got = []
    async for reading in bridge.stream():
        got.append(reading)
        break
    assert len(got) == 1
    r = got[0]
    assert r.entity_id == "binary_sensor.kitchen_gas"
    assert r.source == "ha"
    assert r.norm_value is True  # 'on' → True
    assert r.ts_utc == "2026-06-02T01:00:00+00:00"


# --------------------------------------------------------------------------
# end-to-end: HA 가스 이벤트 → LiotApp → Proposal EMBER (갭이 닫혔는지)
# --------------------------------------------------------------------------
async def test_e2e_ha_gas_event_yields_ember_proposal() -> None:
    conn = FakeHAConnection(
        [
            {"type": "auth_required"},
            {"type": "auth_ok"},
            _echo_result({"result": [GAS_STATE, LIGHT_STATE]}),  # get_states
            _echo_result(),  # subscribe
            {"type": "event", "event": {"event_type": "state_changed", "data": {"new_state": GAS_STATE}}},
        ]
    )
    bridge = HABridge(conn, "tok")
    # 1) HA 메타에서 카테고리 맵을 받아 LiotApp 에 주입(device_class 반영).
    cats = await bridge.entity_categories()
    app = LiotApp(categories=cats)  # 기본 FakeLLM(네트워크 없음)

    # 2) 브리지 스트림의 가스 이벤트를 파이프라인에 흘린다.
    proposal = None
    async for reading in bridge.stream():
        proposal = await app.process_reading(reading)
        break

    assert proposal is not None, "가스 이벤트가 제안을 만들지 못함"
    assert proposal.risk_tier == RiskTier.EMBER  # device_class 'gas' → 안전 EMBER
    assert proposal.required_confirmation == "double"


# --------------------------------------------------------------------------
# 실소켓 왕복: 로컬 mock HA 서버 + 진짜 websockets 클라이언트
# --------------------------------------------------------------------------
async def test_real_websocket_roundtrip_against_mock_ha() -> None:
    websockets = pytest.importorskip("websockets")

    LOCK_STATE = {
        "entity_id": "lock.front_door",
        "state": "unlocked",
        "attributes": {},
        "last_changed": "2026-06-02T01:00:00+00:00",
    }

    async def handler(ws, *args):
        await ws.send(json.dumps({"type": "auth_required"}))
        auth = json.loads(await ws.recv())
        if auth.get("access_token") != "good-token":
            await ws.send(json.dumps({"type": "auth_invalid"}))
            return
        await ws.send(json.dumps({"type": "auth_ok"}))
        sub = json.loads(await ws.recv())
        await ws.send(json.dumps({"id": sub["id"], "type": "result", "success": True}))
        await ws.send(
            json.dumps(
                {
                    "type": "event",
                    "event": {"event_type": "state_changed", "data": {"new_state": LOCK_STATE}},
                }
            )
        )
        await asyncio.sleep(0.3)

    async with websockets.serve(handler, "127.0.0.1", 0) as server:
        port = server.sockets[0].getsockname()[1]
        conn = WebsocketsHAConnection(f"ws://127.0.0.1:{port}/api/websocket")
        await conn.open()
        bridge = HABridge(conn, "good-token")
        try:
            got = None
            async for reading in bridge.stream():
                got = reading
                break
        finally:
            await conn.close()

    assert got is not None
    assert got.entity_id == "lock.front_door"
    # normalizer 규약: True = 주목할/활성 상태(open/detected/unlocked),
    # False = 기본/안전 상태(closed/dry/locked). 'unlocked' → True.
    assert got.norm_value is True
    assert got.source == "ha"


async def test_real_websocket_auth_invalid() -> None:
    websockets = pytest.importorskip("websockets")

    async def handler(ws, *args):
        await ws.send(json.dumps({"type": "auth_required"}))
        await ws.recv()
        await ws.send(json.dumps({"type": "auth_invalid", "message": "nope"}))

    async with websockets.serve(handler, "127.0.0.1", 0) as server:
        port = server.sockets[0].getsockname()[1]
        conn = WebsocketsHAConnection(f"ws://127.0.0.1:{port}/api/websocket")
        await conn.open()
        bridge = HABridge(conn, "wrong-token")
        with pytest.raises(HAAuthError):
            await bridge.authenticate()
        await conn.close()
