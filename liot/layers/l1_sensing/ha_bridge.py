"""Home Assistant 브리지 (L1) — 실제 WebSocket 연동.

HA WebSocket API 흐름:
  1) 서버가 ``{"type":"auth_required"}`` 전송.
  2) 클라이언트가 ``{"type":"auth","access_token":...}`` 전송.
  3) 서버가 ``auth_ok`` (실패 시 ``auth_invalid``).
  4) ``subscribe_events(event_type=state_changed)`` 로 상태 변화 구독.
  5) ``{"type":"event","event":{"event_type":"state_changed","data":{...}}}`` 수신.

감독(재연결)이 쓰는 세션 순서(``LiotService._ha_session``):
  인증 → 구독 → (HA 기동 대기: ``core_state``) → ``entity_states``(get_states) → 재동기화 →
  버퍼 비우기 → 스트림. 요청을 기다리는 동안 끼어든 이벤트는 버리지 않고 버퍼에 쌓는다
  (``_request``) — get_states 스냅샷과 구독 사이의 틈에서 안전 신호가 사라지면 안 된다.

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

import asyncio
import json
import logging
from collections import deque
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass
from typing import Any, Protocol, cast, runtime_checkable

from liot.core.contracts import SensorReading

from .normalizer import normalize

__all__ = [
    "HAConnection",
    "FakeHAConnection",
    "WebsocketsHAConnection",
    "HABridge",
    "HAAuthError",
    "HAConnectionError",
    "HAConnectionClosed",
    "HAMalformedMessage",
    "HARequestFailed",
    "HASnapshot",
    "BridgeFactory",
    "connect_ha",
    "call_service_once",
    "get_state_once",
    "infer_category",
    "SAFETY_EVENT_DOMAINS",
]

logger = logging.getLogger(__name__)

# 요청 하나의 응답을 기다리는 상한(초). 인증·구독·get_states·get_config·call_service 에 건다 —
# 말없이 멈춘 HA 가 감독을 영원히 붙잡지 못하게(연결이 열려 있어도 응답이 안 오는 경우).
DEFAULT_REQUEST_TIMEOUT_S = 30.0

SAFETY_EVENT_DOMAINS: tuple[str, ...] = ("door", "lock", "motion", "window", "leak")


class HAAuthError(Exception):
    """HA 인증 실패(auth_invalid 또는 핸드셰이크 위반)."""


class HAConnectionError(Exception):
    """HA 연결/전송 실패."""


class HAConnectionClosed(HAConnectionError):
    """HA 쪽 WebSocket 이 닫혔다(HA 재시작·네트워크 단절). 라이브러리 예외를 감싼 것."""


class HARequestFailed(HAConnectionError):
    """HA 가 요청을 받았지만 ``success: false`` 로 거절했다(연결 자체는 멀쩡하다)."""


class HAMalformedMessage(HAConnectionError):
    """HA 가 해석할 수 없는 메시지를 보냈다(JSON 객체가 아님·이벤트 형식 위반). 재연결 대상."""


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
    동적으로 응답을 만들 수 있다(예: 요청 id 를 echo). 항목이 예외 인스턴스면 그
    자리에서 raise 한다(연결이 스트림 도중 끊기는 상황을 스크립트로 만든다).
    """

    def __init__(self, inbound: list[Any]) -> None:
        self._inbound: list[Any] = list(inbound)
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
        if isinstance(item, BaseException):
            raise item
        # 스크립트 항목은 dict 이거나 dict 를 만드는 callable 이다(클래스 계약).
        if callable(item):
            return cast("dict[str, Any]", item(self.sent))
        return cast("dict[str, Any]", item)

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
            from websockets.exceptions import WebSocketException
        except ImportError as exc:  # pragma: no cover
            raise HAConnectionError(
                "websockets 미설치. `uv pip install -e '.[ha]'` 로 설치하세요."
            ) from exc
        try:
            self._ws = await websockets.connect(self.url, max_size=8 * 1024 * 1024)
        except (OSError, WebSocketException) as exc:
            # OSError: 연결 거부·시간 초과. WebSocketException: 핸드셰이크 거부(프록시 502 등)·잘못된 URI.
            # URL 에는 토큰이 없지만(토큰은 auth 메시지로만 간다) 예외 본문은 싣지 않는다.
            raise HAConnectionError(
                f"HA 연결 실패: {self.url}: {type(exc).__name__}"
            ) from exc

    async def send(self, obj: dict[str, Any]) -> None:
        if self._ws is None:
            raise HAConnectionError("not opened")
        try:
            await self._ws.send(json.dumps(obj))
        except Exception as exc:
            _raise_if_closed(exc)
            raise

    async def recv(self) -> dict[str, Any]:
        if self._ws is None:
            raise HAConnectionError("not opened")
        try:
            raw = await self._ws.recv()
        except Exception as exc:
            _raise_if_closed(exc)
            raise
        try:
            data = json.loads(raw)
        except (ValueError, TypeError) as exc:
            raise HAMalformedMessage(f"비-JSON HA 메시지: {type(exc).__name__}") from exc
        if not isinstance(data, dict):
            raise HAMalformedMessage("HA 메시지가 객체가 아님")
        return data

    async def close(self) -> None:
        if self._ws is not None:
            ws, self._ws = self._ws, None
            try:
                await ws.close()
            except Exception:  # noqa: BLE001 — 닫는 중의 오류는 호출자가 할 일이 없다
                logger.debug("ha websocket close failed", exc_info=True)


def _raise_if_closed(exc: Exception) -> None:
    """websockets 의 ConnectionClosed(정상·비정상 종료 모두)를 우리 예외로 바꿔 던진다.

    라이브러리 예외는 브리지·서비스가 모른다 — 감싸지 않으면 감독이 '연결이 끊겼다'를 알아보지
    못하고 예외가 밖으로 새어 데몬을 끝낸다(예전 결함). 연결이 열려 있었다면 websockets 는
    이미 설치돼 있으므로 지연 임포트가 실패하지 않는다.
    """
    from websockets.exceptions import ConnectionClosed

    if isinstance(exc, ConnectionClosed):
        raise HAConnectionClosed(f"HA 연결 종료: {type(exc).__name__}") from exc


@dataclass(frozen=True)
class HASnapshot:
    """``get_states`` 한 번의 결과.

    categories : 모든 엔티티(복원 자리표시자 포함)의 엔티티 → LIOT 카테고리.
    readings   : 재동기화용 현재값. ``attributes.restored`` 인 자리표시자는 **빠져 있다** —
                 HA 가 재시작 직후 디스크에서 되살린 가짜 상태라 실제 센서 값이 아니다.
    restored   : 복원 자리표시자 엔티티 id(카테고리 맵 병합이 이미 아는 카테고리를 덮어쓰지 않게).
    """

    categories: dict[str, str]
    readings: list[SensorReading]
    restored: frozenset[str]


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
        request_timeout_s: float = DEFAULT_REQUEST_TIMEOUT_S,
    ) -> None:
        self._conn = connection
        self._token = token  # 절대 로깅 금지
        self.home_id = home_id
        self._request_timeout_s = request_timeout_s
        self._id = 0
        self._authed = False
        self._subscribed = False
        # 요청 응답을 기다리는 동안 끼어든 이벤트. 버리지 않고 쌓아 두었다가 스트림이 먼저 내준다.
        self._pending: deque[dict[str, Any]] = deque()

    def _next_id(self) -> int:
        self._id += 1
        return self._id

    async def authenticate(self) -> None:
        """auth_required → auth → auth_ok 핸드셰이크. 실패 시 HAAuthError."""
        if self._authed:
            return
        try:
            async with asyncio.timeout(self._request_timeout_s):
                first = await self._conn.recv()
                if first.get("type") != "auth_required":
                    raise HAAuthError(f"예상치 못한 첫 메시지: {first.get('type')!r}")
                await self._conn.send({"type": "auth", "access_token": self._token})
                resp = await self._conn.recv()
        except TimeoutError as exc:
            raise HAConnectionError("HA 인증 응답 시간 초과") from exc
        if resp.get("type") != "auth_ok":
            # 토큰 값은 절대 메시지에 싣지 않는다.
            raise HAAuthError(f"인증 거부: {resp.get('type')!r} ({resp.get('message', '')})")
        self._authed = True

    async def _request(self, payload: dict[str, Any]) -> dict[str, Any]:
        """id 부여 후 전송하고, 같은 id 의 result 를 받을 때까지 수신.

        기다리는 동안 도착한 이벤트(``type == "event"``)는 ``_pending`` 에 쌓는다. 버리면
        get_states 응답과 구독 응답 사이에 일어난 상태 변화(예: 가스 on)가 영영 사라진다.
        """
        req_id = self._next_id()
        try:
            async with asyncio.timeout(self._request_timeout_s):
                await self._conn.send({"id": req_id, **payload})
                while True:
                    msg = await self._conn.recv()
                    if msg.get("type") == "result" and msg.get("id") == req_id:
                        if not msg.get("success", False):
                            raise HARequestFailed(f"HA 요청 실패: {msg.get('error')}")
                        return msg
                    if msg.get("type") == "event":
                        self._pending.append(msg)
        except TimeoutError as exc:
            raise HAConnectionError(f"HA 응답 시간 초과: {payload.get('type')}") from exc

    async def subscribe(self) -> None:
        """state_changed 구독(멱등). 구독 뒤의 이벤트는 ``next_reading`` 이 내준다."""
        await self.authenticate()
        if self._subscribed:
            return
        await self._request({"type": "subscribe_events", "event_type": "state_changed"})
        self._subscribed = True

    async def core_state(self) -> str | None:
        """``get_config`` 의 HA 코어 상태(``RUNNING``·``NOT_RUNNING``·``STARTING`` …).

        필드가 없거나 HA 가 이 요청을 거절하면 None — 상태를 알려 주지 않는 HA 를 기다리느라 감시가
        시작되지 못하면 안 되므로 호출자는 None 을 '준비됨'으로 본다. 연결 오류는 그대로 올라간다.
        """
        await self.authenticate()
        try:
            result = await self._request({"type": "get_config"})
        except HARequestFailed:
            return None
        body = result.get("result")
        state = body.get("state") if isinstance(body, dict) else None
        return state if isinstance(state, str) else None

    async def _states(self) -> list[dict[str, Any]]:
        await self.authenticate()
        result = await self._request({"type": "get_states"})
        raw = result.get("result")
        if raw is None:
            return []
        if not isinstance(raw, list):
            raise HAMalformedMessage("get_states 결과가 목록이 아님")
        return [st for st in raw if isinstance(st, dict)]

    async def entity_categories(self) -> dict[str, str]:
        """get_states 로 모든 엔티티의 (domain, device_class) → LIOT 카테고리 맵.

        이 맵을 LiotApp(categories=...) 로 주입해야 binary_sensor.gas 같은
        엔티티가 안전 카테고리로 정확히 분류된다.
        """
        return (await self.entity_states()).categories

    async def entity_states(self) -> HASnapshot:
        """get_states 한 번으로 카테고리 맵과 재동기화용 현재값을 함께 돌려준다.

        현재값을 버리지 않는 것이 요점이다: 데몬이 꺼져 있던(또는 연결이 끊겼던) 동안 이상이 된
        안전 센서는 상태 변화 이벤트가 오지 않아, 이 값을 흘리지 않으면 침묵한다.
        """
        cats: dict[str, str] = {}
        readings: list[SensorReading] = []
        restored: set[str] = set()
        for st in await self._states():
            eid = st.get("entity_id")
            if not isinstance(eid, str) or not eid:
                continue
            attrs = st.get("attributes")
            attrs = attrs if isinstance(attrs, dict) else {}
            cats[eid] = infer_category(eid, attrs)
            if attrs.get("restored") is True:
                restored.add(eid)
                continue
            try:
                reading = self._reading_from_state(st, self.home_id)
            except Exception:  # noqa: BLE001 — 이 엔티티 하나가 재동기화 전체를 막지 않는다
                logger.warning("ha state skipped (unparseable): %s", type(st.get("state")).__name__)
                continue
            if reading is not None:
                readings.append(reading)
        return HASnapshot(categories=cats, readings=readings, restored=frozenset(restored))

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

    async def get_state(self, entity_id: str) -> dict[str, Any]:
        """단일 엔티티의 현재 상태 dict 를 돌려준다(되돌리기용 이전 상태 캡처).

        HA WS 는 단건 조회가 없어 get_states 후 필터링한다. 없으면 빈 dict.
        """
        for st in await self._states():
            if st.get("entity_id") == entity_id:
                return st
        return {}

    async def call_service(
        self, domain: str, service: str, entity_id: str, data: dict[str, Any] | None = None
    ) -> None:
        """HA 서비스 호출(쓰기). **오직 L4 Executor 만 호출해야 한다.**

        이 메서드는 LIOT 에서 실제 세계를 바꾸는 유일한 경로다. 호출 가능 여부는
        Executor 의 화이트리스트 + ExecutionPolicy 가 이미 통제한다(불변식 8).
        브리지 자체는 정책을 모르며, 센서 스트림/맥락/판단 경로는 이 메서드를
        절대 호출하지 않는다(읽기 전용 유지).
        """
        await self.authenticate()
        payload: dict[str, Any] = {
            "type": "call_service",
            "domain": domain,
            "service": service,
            "target": {"entity_id": entity_id},
        }
        if data:
            payload["service_data"] = dict(data)
        await self._request(payload)

    def _reading_from_message(self, msg: dict[str, Any]) -> SensorReading | None:
        """수신 메시지 하나 → 읽기(해당 없으면 None). 형식이 틀린 이벤트는 HAMalformedMessage.

        조용히 건너뛰면 그 안전 신호가 영영 사라진다. 던지면 감독이 재연결 + 재동기화로 현재값을
        다시 받아 온다.
        """
        if msg.get("type") != "event":
            return None
        event = msg.get("event")
        if event is None:
            return None
        if not isinstance(event, dict):
            raise HAMalformedMessage("event 가 객체가 아님")
        if event.get("event_type") != "state_changed":
            return None
        data = event.get("data")
        if not isinstance(data, dict):
            raise HAMalformedMessage("state_changed data 가 객체가 아님")
        new_state = data.get("new_state")
        if not new_state:  # 엔티티 제거 등
            return None
        if not isinstance(new_state, dict):
            raise HAMalformedMessage("new_state 가 객체가 아님")
        try:
            return self._reading_from_state(new_state, self.home_id)
        except Exception as exc:  # noqa: BLE001 — 계약 검증 실패도 형식 오류다
            raise HAMalformedMessage(f"읽기로 바꿀 수 없는 상태: {type(exc).__name__}") from exc

    async def next_reading(self, timeout: float | None = None) -> SensorReading | None:
        """다음 읽기. 쌓여 있는 이벤트(요청 중 끼어든 것)를 먼저 내주고, 없으면 연결에서 받는다.

        ``timeout`` 안에 읽기가 없으면 None. 연결이 끝나면 ``HAConnectionClosed``·
        ``HAMalformedMessage``·``HAConnectionError`` 가 그대로 올라간다(스트림과 달리 삼키지 않는다 —
        감독이 끝난 이유를 알아야 한다).
        """
        loop = asyncio.get_running_loop()
        deadline = None if timeout is None else loop.time() + timeout
        while True:
            if self._pending:
                msg = self._pending.popleft()
            elif deadline is None:
                msg = await self._conn.recv()
            else:
                remaining = deadline - loop.time()
                if remaining <= 0:
                    return None
                try:
                    msg = await asyncio.wait_for(self._conn.recv(), timeout=remaining)
                except TimeoutError:
                    return None
            reading = self._reading_from_message(msg)
            if reading is not None:
                return reading

    async def stream(self) -> AsyncIterator[SensorReading]:
        """인증 → state_changed 구독 → 이벤트를 SensorReading 으로 yield.

        연결이 끝나면 조용히 끝난다(예전 계약). 재연결·알림은 하지 않는다 — 그건 감독
        (``LiotService.run(ha_factory=...)``)이 ``next_reading`` 으로 한다.
        """
        await self.subscribe()
        while True:
            try:
                reading = await self.next_reading()
            except HAConnectionError as exc:
                logger.warning("HA stream ended: %s", type(exc).__name__)
                return
            if reading is not None:
                yield reading

    async def close(self) -> None:
        await self._conn.close()


# ---------------------------------------------------------------------------
# 연결 팩토리 — 감독(스트림)과 실행기(호출마다 짧은 연결)가 같은 방식으로 새 연결을 만든다
# ---------------------------------------------------------------------------
# 새 연결을 열어 브리지를 돌려주는 비동기 함수. 열지 못하면 HAConnectionError.
BridgeFactory = Callable[[], Awaitable[HABridge]]


async def connect_ha(url: str, token: str, *, home_id: str = "singleton") -> HABridge:
    """HA 에 새 WebSocket 연결을 열어 브리지로 감싼다(인증은 첫 사용 때)."""
    conn = WebsocketsHAConnection(url)
    await conn.open()
    return HABridge(conn, token, home_id=home_id)


async def call_service_once(
    factory: BridgeFactory,
    domain: str,
    service: str,
    entity_id: str,
    data: dict[str, Any] | None = None,
) -> None:
    """호출 한 번을 위한 짧은 연결: 열기 → 인증 → 호출 → 닫기.

    스트림 소켓과 공유하지 않는다 — 같은 소켓에서 동시에 recv() 하면 websockets 가
    ConcurrencyError 를 낸다. HA 에 닿지 못하면 예외가 올라가고, 실행기는 그것을 실행 실패
    (fail-closed: 조작 안 함)로 다룬다.
    """
    bridge = await factory()
    try:
        await bridge.call_service(domain, service, entity_id, data)
    finally:
        await bridge.close()


async def get_state_once(factory: BridgeFactory, entity_id: str) -> dict[str, Any]:
    """단일 엔티티 상태 조회 한 번을 위한 짧은 연결(되돌리기용 이전 상태 캡처)."""
    bridge = await factory()
    try:
        return await bridge.get_state(entity_id)
    finally:
        await bridge.close()
