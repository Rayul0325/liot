"""L1 Sensing 레이어.

센서 소스(SensorSource)로부터 원시 값을 받아 정규화(normalizer)하여
``SensorReading`` 을 방출한다. 엔티티별 최신값은 state_cache 가 보관한다.

Phase1: 실제 Home Assistant 연결은 ha_bridge 스켈레톤(미구현)이며,
테스트·데모는 결정론적 FakeSensorSource 를 사용한다.
"""

from __future__ import annotations

from .base import SensorSource
from .fake_source import FakeSensorSource
from .normalizer import Normalizer, normalize
from .state_cache import StateCache

__all__ = [
    "SensorSource",
    "FakeSensorSource",
    "Normalizer",
    "normalize",
    "StateCache",
]
