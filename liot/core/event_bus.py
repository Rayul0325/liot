"""단방향 파이프라인을 강제하는 인-프로세스 이벤트 버스.

불변식 1 (단방향 파이프라인): 오직 다음 엣지만 허용된다.
    (L1→L2, L2→L3, L3→L4, L4→L2)
그 외의 publish 는 TopologyViolation 을 raise 한다.

단일 프로세스 asyncio 전제. 네트워크 호출 없음.
"""

from __future__ import annotations

import asyncio
from collections import defaultdict
from collections.abc import Awaitable, Callable

from .contracts import EventMessage, Layer
from .invariants import TopologyViolation

__all__ = ["ALLOWED_EDGES", "InProcessBus", "Handler"]

# 허용된 단방향 엣지. L4→L2 피드백 루프만 역방향처럼 보이지만 허용된다.
ALLOWED_EDGES: frozenset[tuple[Layer, Layer]] = frozenset(
    {
        (Layer.L1, Layer.L2),
        (Layer.L2, Layer.L3),
        (Layer.L3, Layer.L4),
        (Layer.L4, Layer.L2),
    }
)

Handler = Callable[[EventMessage], Awaitable[None]]


class InProcessBus:
    """비동기 인-프로세스 버스. 토폴로지를 런타임에 강제한다."""

    def __init__(self) -> None:
        self._subscribers: dict[Layer, list[Handler]] = defaultdict(list)

    def subscribe(self, layer_to: Layer, handler: Handler) -> None:
        """layer_to 로 향하는 메시지를 받을 핸들러를 등록한다."""
        self._subscribers[layer_to].append(handler)

    async def publish(self, msg: EventMessage) -> None:
        """메시지를 게시한다. 비허용 엣지면 TopologyViolation.

        허용 엣지면 layer_to 구독자들을 순차적으로 await 한다(결정론적 순서).
        """
        edge = (msg.layer_from, msg.layer_to)
        if edge not in ALLOWED_EDGES:
            raise TopologyViolation(
                "illegal pipeline edge: "
                f"{msg.layer_from.value} -> {msg.layer_to.value} "
                f"(type={msg.type!r}, trace={msg.trace_id})"
            )
        handlers = list(self._subscribers.get(msg.layer_to, ()))
        for handler in handlers:
            await handler(msg)

    async def publish_many(self, messages: list[EventMessage]) -> None:
        """여러 메시지를 순서대로 게시한다(편의 메서드)."""
        for msg in messages:
            await self.publish(msg)

    def subscriber_count(self, layer_to: Layer) -> int:
        """layer_to 구독자 수(테스트/진단용)."""
        return len(self._subscribers.get(layer_to, ()))


# 단일 프로세스 내 공유가 필요할 때 쓸 수 있는 기본 락(현재는 미사용 자리표시).
_BUS_LOCK = asyncio.Lock()
