"""Home Assistant 브리지 (L1) — 실제 WebSocket 연동.

HA WebSocket API 흐름:
  1) 서버가 ``{"type":"auth_required"}`` 전송.
  2) 클라이언트가 ``{"type":"auth","access_token":...}`` 전송.
  3) 서버가 ``auth_ok`` (실패 시 ``auth_invalid``).
  4) ``subscribe_events(event_type=state_changed)`` 로 상태 변화 구독.
  5) ``{"type":"event","event":{"event_type":"state_changed","data":{...}}}`` 수신.

설계 핵심:
  - **안전 카테고리는 domain 이 아니라 device_class 에 있다.** HA 의
    ``binary_sensor.kitchen_gas`` 는 domain 이 'binary_sensor' 지만 실제 위험은
    ``attributes.device_class == 'gas'`` 에 있다. 도메인 접두사만 보면 가스
    누출이 안전(EMBER)으로 분류되지 않는다. 그래서 본 브리지가
    (domain, device_class) → LIOT 카테고리 매핑을 제공하고, 이를
    ``LiotApp(categories=...)`` 로 주입해 안전 파이프라인이 실제로 작동하게 한다.
  - **transport 추상화(HAConnection Protocol)**: 실제 websockets 연결과
    테스트용 FakeHAConnection 을 같은 계약으로 교체 가능. 단위 테스트는
    네트워크 없이, 통합 테스트는 로컬 mock HA 서버에 진짜 소켓으로 검증.
  - 토큰은 절대 로깅/노출하지 않는다.
"""

from __future__ import annotations

import json
from typing import Any, AsyncIterator, Protocol, runtime_checkable

from liot.core.contracts import SensorReading

from .normalizer import normalize

__all__ = [
    "HAConnection",
    "FakeHAConnection",
    "WebsocketsHAConnection",
    "HABridge",
    "HAAuthError",
    "HAConnectionError",
    "infer_category",
    "SAFETY_EVENT_DOMAINS",
]

SAFETY_EVENT_DOMAINS: tuple[str, ...] = ("door", "lock", "motion", "window", "leak")


class HAAuthError(Exception):
    """HA 인증 실패(auth_invalid 또는 핸드셰이크 위반)."""


class HAConnectionError(Exception):
    """HA 연결/전송 실패."""


# ---------------------------------------------------------------------------
# device_class → LIOT 카테고리 (안전 신호는 여기서 결정된다)
# ---------------------------------------------------------------------------
# binary_sensor/sensor 의 device_class 를 LIOT 카테고리로. SAFETY_CRITICAL_
# CATEGORIES(gas/smoke/door/leak/high_temp/lock/water_main/oven)와 정렬.
_DEVICE_CLASS_CATEGORY: dict[str, str] = {
    "gas": "gas",
    "carbon_monoxide": "gas",
    "smoke": "smoke",
    "moisture": "leak",  # HA 누수 센서 device_class
    "water": "leak",
    "door": "door",
    "garage_door": "door",
    "window": "window",
    "lock": "lock",
    "motion": "motion",
    "occupancy": "motion",
    "presence": "motion",
    "temperature": "temperature",
    "humidity": "humidity",
}

# domain 자체가 카테고리를 정하는 경우(device_class 무관).
_DOMAIN_CATEGORY: dict[str, str] = {
    "lock": "lock",
    "light": "light",
    "switch": "switch",
    "climate": "climate",
    "media_player": "media",
    "cover": "cover",
    "fan": "fan",
    "vacuum": "appliance",
    "valve": "water_main",
}


def infer_category(entity_id: str, attributes: dict[str, Any] | None = None) -> str:
    """HA 엔티티의 LIOT 카테고리를 (domain, device_class) 로 추론한다.

    우선순위:
      1) domain 이 카테고리를 직접 정하면 그것(lock/light/climate/valve ...).
      2) device_class 가 있으면 매핑(binary_sensor.gas -> 'gas' 안전!).
      3) 그 외 domain 접두사.
    """
    domain = entity_id.split(".", 1)[0] if "." in entity_id else entity_id
    if domain in _DOMAIN_CATEGORY:
        return _DOMAIN_CATEGORY[domain]
    dc = str((attributes or {}).get("device_class") or "").strip().lower()
    if dc in _DEVICE_CLASS_CATEGORY:
        return _DEVICE_CLASS_CATEGORY[dc]
    return domain


# ---------------------------------------------------------------------------
# 연결 추상화
# ---------------------------------------------------------------------------
@runtime_checkable
class HAConnection(Protocol):
    """HA WebSocket 양방향 메시지 채널(JSON dict 단위)."""

    async def send(self, obj: dict[str, Any]) -> None: ...
    async def recv(self) -> dict[str, Any]: ...
    async def close(self) -> None: ...


class FakeHAConnection:
    """테스트용 스크립트 연결. 미리 정한 inbound 를 순서대로 recv 로 내주고,
    send 된 메시지는 ``sent`` 에 기록한다. 네트워크 없음.

    inbound 항목이 callable 이면 '지금까지 send 된 메시지'를 인자로 호출해
    동적으로 응답을 만들 수 있다(예: 요청 id 를 echo).
    """

    def __init__(self, inbound: list[Any]) -> None:
        self._inbound = list(inbound)
        self.sent: list[dict[str, Any]] = []
        self.closed = False

    async def send(self, obj: dict[str, Any]) -> None:
        if self.closed:
            raise HAConnectionError("send on closed connection")
        self.sent.append(obj)

    async def recv(self) -> dict[str, Any]:
        if not self._inbound:
            raise HAConnectionError("no more scripted inbound messages")
        item = self._inbound.pop(0)
        if callable(item):
            return item(self.sent)
        return item

    async def close(self) -> None:
        self.closed = True


class WebsocketsHAConnection:
    """실제 websockets 기반 연결. ``websockets`` 패키지 필요(extra ``[ha]``).

    지연 임포트하므로 단위 테스트(FakeHAConnection)는 websockets 미설치여도 동작.
    """

    def __init__(self, url: str) -> None:
        self.url = url
        self._ws: Any = None

    async def open(self) -> None:
        try:
            import websockets  # 지연 임포트
        except ImportError as exc:  # pragma: no cover
            raise HAConnectionError(
                "websockets 미설치. `uv pip install -e '.[ha]'` 로 설치하세요."
            ) from exc
        try:
            self._ws = await websockets.connect(self.url, max_size=8 * 1024 * 1024)
        except OSError as exc:
            raise HAConnectionError(f"HA 연결 실패: {self.url}: {exc}") from exc

    async def send(self, obj: dict[str, Any]) -> None:
        if self._ws is None:
            raise HAConnectionError("not opened")
        await self._ws.send(json.dumps(obj))

    async def recv(self) -> dict[str, Any]:
        if self._ws is None:
            raise HAConnectionError("not opened")
        raw = await self._ws.recv()
        try:
            data = json.loads(raw)
        except (ValueError, TypeError) as exc:
            raise HAConnectionError(f"비-JSON HA 메시지: {exc}") from exc
        if not isinstance(data, dict):
            raise HAConnectionError("HA 메시지가 객체가 아님")
        return data

    async def close(self) -> None:
        if self._ws is not None:
            await self._ws.close()
            self._ws = None


# ---------------------------------------------------------------------------
# 브리지
# ---------------------------------------------------------------------------
class HABridge:
    """HA WebSocket → SensorReading 스트림(SensorSource 계약 충족)."""

    def __init__(
        self,
        connection: HAConnection,
        token: str,
        *,
        home_id: str = "singleton",
    ) -> None:
        self._conn = connection
        self._token = token  # 절대 로깅 금지
        self.home_id = home_id
        self._id = 0
        self._authed = False

    def _next_id(self) -> int:
        self._id += 1
        return self._id

    async def authenticate(self) -> None:
        """auth_required → auth → auth_ok 핸드셰이크. 실패 시 HAAuthError."""
        if self._authed:
            return
        first = await self._conn.recv()
        if first.get("type") != "auth_required":
            raise HAAuthError(f"예상치 못한 첫 메시지: {first.get('type')!r}")
        await self._conn.send({"type": "auth", "access_token": self._token})
        resp = await self._conn.recv()
        if resp.get("type") != "auth_ok":
            # 토큰 값은 절대 메시지에 싣지 않는다.
            raise HAAuthError(f"인증 거부: {resp.get('type')!r} ({resp.get('message', '')})")
        self._authed = True

    async def _request(self, payload: dict[str, Any]) -> dict[str, Any]:
        """id 부여 후 전송하고, 같은 id 의 result 를 받을 때까지 수신."""
        req_id = self._next_id()
        await self._conn.send({"id": req_id, **payload})
        while True:
            msg = await self._conn.recv()
            if msg.get("type") == "result" and msg.get("id") == req_id:
                if not msg.get("success", False):
                    raise HAConnectionError(f"HA 요청 실패: {msg.get('error')}")
                return msg
            # 그 사이 끼어든 event 는 무시(구독 전이라면 통상 없음).

    async def entity_categories(self) -> dict[str, str]:
        """get_states 로 모든 엔티티의 (domain, device_class) → LIOT 카테고리 맵.

        이 맵을 LiotApp(categories=...) 로 주입해야 binary_sensor.gas 같은
        엔티티가 안전 카테고리로 정확히 분류된다.
        """
        await self.authenticate()
        result = await self._request({"type": "get_states"})
        cats: dict[str, str] = {}
        for st in result.get("result", []) or []:
            eid = st.get("entity_id")
            if not eid:
                continue
            cats[eid] = infer_category(eid, st.get("attributes") or {})
        return cats

    @staticmethod
    def _reading_from_state(new_state: dict[str, Any], home_id: str) -> SensorReading | None:
        """HA new_state dict → SensorReading. 상태가 없으면 None."""
        eid = new_state.get("entity_id")
        if not eid:
            return None
        raw = new_state.get("state")
        attrs = new_state.get("attributes") or {}
        ts = new_state.get("last_changed") or new_state.get("last_updated") or ""
        return SensorReading(
            entity_id=eid,
            source="ha",
            raw_value=raw,
            norm_value=normalize(raw),
            unit=attrs.get("unit_of_measurement"),
            confidence=1.0,
            ts_utc=str(ts),
            home_id=home_id,
        )

    async def stream(self) -> AsyncIterator[SensorReading]:
        """인증 → state_changed 구독 → 이벤트를 SensorReading 으로 yield."""
        await self.authenticate()
        await self._request({"type": "subscribe_events", "event_type": "state_changed"})
        while True:
            try:
                msg = await self._conn.recv()
            except HAConnectionError:
                # 연결 종료/오류 → 스트림 정상 종료(상위에서 재연결 정책).
                return
            if msg.get("type") != "event":
                continue
            event = msg.get("event") or {}
            if event.get("event_type") != "state_changed":
                continue
            data = event.get("data") or {}
            new_state = data.get("new_state")
            if not new_state:  # 엔티티 제거 등
                continue
            reading = self._reading_from_state(new_state, self.home_id)
            if reading is not None:
                yield reading

    async def close(self) -> None:
        await self._conn.close()
