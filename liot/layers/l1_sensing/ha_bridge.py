"""Home Assistant 브리지 — Phase1 스켈레톤(연결하지 않음).

실제 HA WebSocket/REST 연동은 Phase1 범위 밖이다. 이 모듈은 SensorSource
계약의 자리(placeholder)만 잡아 둔다. 어떤 메서드도 네트워크를 열지 않으며,
미구현 지점은 명시적으로 ``NotImplementedError`` + TODO 로 표시한다.

안전 도메인 의도(향후 subscribe_events 대상):
  - door   : 현관/방문 열림·닫힘 (SAFETY_CRITICAL 'door')
  - lock   : 도어락 잠금/해제   (SAFETY_CRITICAL 'lock')
  - motion : 재실/움직임 감지   (맥락 트리거)
  - window : 창문 개폐          (외출/취침 시 안전)
  - leak   : 누수 감지          (SAFETY_CRITICAL 'leak')
이들은 상태 변화 시 즉시 L1->L2 로 흘려야 하며, 안전 카테고리는
should_snapshot 에서 tick/threshold 와 무관하게 항상 스냅샷을 유발한다
(불변식6: Safety-Never-Silent).
"""

from __future__ import annotations

from typing import AsyncIterator

from liot.core.contracts import SensorReading

__all__ = ["HABridge", "SAFETY_EVENT_DOMAINS"]

# 안전상 항상 구독 의도가 있는 HA 도메인(엔티티 접두사).
SAFETY_EVENT_DOMAINS: tuple[str, ...] = (
    "door",
    "lock",
    "motion",
    "window",
    "leak",
)


class HABridge:
    """Home Assistant 브리지 스켈레톤.

    Phase1 에서는 어떤 연결도 수립하지 않는다. 인스턴스 생성과 설정 보관까지만
    하고, I/O 를 요구하는 모든 동작은 NotImplementedError 를 던진다.
    """

    def __init__(
        self,
        base_url: str | None = None,
        token: str | None = None,
        *,
        home_id: str = "singleton",
    ) -> None:
        # 설정만 보관한다. 토큰은 외부로 절대 노출하지 않는다(로깅 금지).
        self._base_url = base_url
        self._token = token
        self.home_id = home_id
        self._connected = False

    async def connect(self) -> None:
        """HA WebSocket 연결 수립.

        TODO(phase2): aiohttp 등으로 /api/websocket 핸드셰이크 + auth.
        Phase1 은 연결하지 않는다.
        """
        raise NotImplementedError(
            "HABridge.connect is a Phase1 skeleton; HA connection is out of scope. "
            "TODO(phase2): establish authenticated HA WebSocket session."
        )

    async def subscribe_events(self, domains: tuple[str, ...] = SAFETY_EVENT_DOMAINS) -> None:
        """state_changed 이벤트 구독(안전 도메인 우선).

        의도: door/lock/motion/window/leak 의 상태 변화를 구독해 즉시 L1->L2
        로 흘린다. 안전 카테고리는 디바운스 없이 통과시켜야 한다.

        TODO(phase2): subscribe_events(event_type='state_changed') 후
        domains 필터링 + normalizer 적용 + SensorReading 방출.
        """
        raise NotImplementedError(
            "HABridge.subscribe_events is a Phase1 skeleton. "
            "TODO(phase2): subscribe HA state_changed for safety domains "
            f"{domains!r} and forward to L1->L2."
        )

    async def stream(self) -> AsyncIterator[SensorReading]:  # type: ignore[empty-body]
        """SensorSource 계약 충족용 스트림(Phase1 미구현).

        TODO(phase2): connect -> subscribe_events 로 수신한 이벤트를
        정규화하여 SensorReading 으로 yield 한다.
        """
        raise NotImplementedError(
            "HABridge.stream is a Phase1 skeleton; use FakeSensorSource for "
            "tests/demo. TODO(phase2): yield normalized readings from HA events."
        )
        # 아래 yield 는 실행되지 않지만, 이 함수가 async-generator 타입으로
        # 인식되도록(SensorSource.stream 시그니처 일치) 보존한다.
        if False:  # pragma: no cover
            yield  # type: ignore[misc]

    async def close(self) -> None:
        """연결 정리(Phase1: no-op)."""
        self._connected = False
