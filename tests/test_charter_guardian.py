"""D3(결정 0013): 헌장 전면 OFF 는 거주자 알림만 끄고, 동의한 보호자 전달은 막지 않는다.

헌장은 거주자 알림, 동의는 가족 호출을 다룬다. 거주자가 자기 알림을 끈 집에서는 동의한
가족 전달이 유일한 안전망이다. 이 파일이 그 현재 동작을 고정한다(코드 변경 없음).

시각은 고정 Clock 을 옮겨서 잰다.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

from liot.app import LiotApp
from liot.core.clock import Clock
from liot.core.contracts import SensorReading
from liot.db.database import init_db
from liot.layers.l1_sensing.normalizer import normalize
from liot.layers.l4_execution.ntfy_transport import FakeNtfyTransport
from liot.service import LiotService

T0 = "2026-06-02T12:00:00+00:00"
SECRET = b"test-secret-key"
TOPIC = "liot-test"
GAS = "binary_sensor.kitchen_gas"
CATS = {GAS: "gas"}
HOME = "singleton"


def _at(seconds: float) -> str:
    return (datetime.fromisoformat(T0) + timedelta(seconds=seconds)).isoformat()


def _to(transport: FakeNtfyTransport, topic: str) -> list[dict[str, Any]]:
    return [p for t, p in transport.sent if t == topic]


async def _make(*, charter_enabled: bool, consent: bool):
    clock = Clock(fixed=T0)
    db = await init_db(":memory:")
    app = LiotApp(clock=clock, categories=CATS)
    transport = FakeNtfyTransport()
    svc = LiotService(app, db, clock=clock, transport=transport, topic=TOPIC, secret=SECRET)
    rid = await svc.safety.add_resident(HOME, "엄마")
    gid, gtopic = await svc.safety.add_guardian(HOME, "딸")
    if consent:
        await svc.safety.grant_consent(HOME, gid, rid)
    await svc.prefs.set("charter_enabled", charter_enabled)
    return svc, db, transport, clock, gid, gtopic


async def _open_gas(svc: LiotService):
    reading = SensorReading(
        entity_id=GAS, source="test", raw_value="on", norm_value=normalize("on"),
        ts_utc=T0, home_id=HOME,
    )
    res = await svc.handle_reading(reading)
    assert res is not None and res.proposal.situation_id is not None
    await svc.drain_phase2()
    return res


async def _advance(svc: LiotService, clock: Clock, seconds: float) -> None:
    clock.set_fixed(_at(seconds))
    await svc.ladder_tick()


async def _forwards(svc: LiotService) -> list[tuple[str | None, str]]:
    return [(f.guardian_id, f.kind) for f in await svc.safety.list_forwards(HOME)]


async def test_charter_off_silences_resident_but_guardian_forward_and_119_still_happen() -> None:
    svc, db, transport, clock, gid, gtopic = await _make(charter_enabled=False, consent=True)
    try:
        res = await _open_gas(svc)
        # 거주자: 첫 알림이 헌장에 막혀 0건.
        assert res.decision == "SUPPRESS" and res.sent is False
        assert _to(transport, TOPIC) == []
        assert _to(transport, gtopic) == []

        # 89초: 아직 보호자 전달 없음. 90초: 동의한 보호자에게 한 번.
        await _advance(svc, clock, 89)
        assert _to(transport, gtopic) == []
        await _advance(svc, clock, 90)
        assert len(_to(transport, gtopic)) == 1
        assert await _forwards(svc) == [(gid, "forward")]

        # 그 뒤 5분 무응답이면 119 안내가 보호자에게 한 번(자동 신고 없음).
        await _advance(svc, clock, 90 + 300)
        assert await _forwards(svc) == [(gid, "forward"), (None, "guidance_119")]
        guardian_msgs = _to(transport, gtopic)
        assert len(guardian_msgs) == 2
        assert "119" in guardian_msgs[1]["situation_label"]

        # 더 기다려도 되풀이하지 않고, 거주자 토픽은 끝까지 0건이다.
        await _advance(svc, clock, 2000)
        assert len(_to(transport, gtopic)) == 2
        assert [k for _, k in await _forwards(svc)].count("guidance_119") == 1
        assert _to(transport, TOPIC) == []
        assert {t for t, _ in transport.sent} == {gtopic}
    finally:
        await db.close()


async def test_charter_off_without_consent_pages_nobody() -> None:
    """동의가 가족 호출의 열쇠다: 헌장 OFF + 동의 없음이면 아무에게도 가지 않는다."""
    svc, db, transport, clock, gid, gtopic = await _make(charter_enabled=False, consent=False)
    try:
        await _open_gas(svc)
        for t in (90, 400, 2000):
            await _advance(svc, clock, t)
        assert transport.sent == []
        assert await _forwards(svc) == []
    finally:
        await db.close()


async def test_charter_on_control_resident_is_alerted_first() -> None:
    """대조군: 헌장 ON 이면 거주자가 먼저 알림을 받고(1건), 보호자는 90초에 한 번."""
    svc, db, transport, clock, gid, gtopic = await _make(charter_enabled=True, consent=True)
    try:
        res = await _open_gas(svc)
        assert res.decision == "IMMEDIATE" and res.sent is True
        assert len(_to(transport, TOPIC)) == 1
        await _advance(svc, clock, 90)
        assert len(_to(transport, gtopic)) == 1
    finally:
        await db.close()
