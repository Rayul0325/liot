"""센서 소스 추상 인터페이스.

SensorSource 는 ``SensorReading`` 을 비동기로 스트리밍하는 모든 소스의
공통 계약이다(Protocol). FakeSensorSource·HABridge 가 이를 만족한다.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Protocol, runtime_checkable

from liot.core.contracts import SensorReading

__all__ = ["SensorSource"]


@runtime_checkable
class SensorSource(Protocol):
    """센서 읽기를 비동기로 yield 하는 소스.

    구현체는 ``stream()`` 을 ``async def`` + ``yield`` 형태(async generator)로
    제공해 ``AsyncIterator[SensorReading]`` 를 반환한다.
    """

    def stream(self) -> AsyncIterator[SensorReading]:
        """정규화된 SensorReading 을 순서대로 방출하는 비동기 이터레이터."""
        ...
