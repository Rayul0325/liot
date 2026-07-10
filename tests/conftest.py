"""공용 pytest 픽스처.

  - tmp_db        : 임시 파일 SQLite(init_db 로 스키마 적용). 함수 스코프.
  - clock         : 고정 시각 Clock(결정론).
  - night_clock   : 야간(UTC 23시) 고정 시각 Clock.
  - fake_source   : FakeSensorSource(결정론적 읽기 시퀀스).
  - app           : 기본 LiotApp(FakeLLM 백엔드).
  - alias         : 빈 AliasMap.
  - mk_reading    : SensorReading 생성 헬퍼.
  - mk_candidate  : CandidateAction 생성 헬퍼.

asyncio_mode='auto' 이므로 async 테스트/픽스처에 별도 마커가 필요 없다.
모든 테스트는 네트워크를 호출하지 않는다.
"""

from __future__ import annotations

from typing import Any

import pytest
import pytest_asyncio

from liot.app import LiotApp
from liot.core.clock import Clock
from liot.core.contracts import CandidateAction, SensorReading
from liot.core.egress_gate import AliasMap
from liot.db.database import init_db
from liot.layers.l1_sensing.fake_source import FakeSensorSource
from liot.layers.l1_sensing.normalizer import normalize


@pytest.fixture(autouse=True)
def _reset_process_global_registrations():
    """프로세스 전역 확장 등록(안전 카테고리·카테고리 하한)을 매 테스트 전에 초기화.

    확장 레지스트리는 모듈 전역 상태라, 한 테스트가 등록한 안전 카테고리·하한이
    다음 테스트로 새면 순서 의존 RED 를 만든다(기존 테스트 236개 격리 보장).
    내장 frozenset/dict 는 건드리지 않는다 — 확장분만 비운다.
    """
    from liot.core.risk import _reset_safety_extensions_for_tests
    from liot.layers.l3_judgment.guardrail import _reset_floor_extensions_for_tests

    _reset_safety_extensions_for_tests()
    _reset_floor_extensions_for_tests()
    yield


@pytest.fixture
def clock() -> Clock:
    """고정 시각(낮, UTC 12시) 시계."""
    return Clock(fixed="2026-06-01T12:00:00Z")


@pytest.fixture
def night_clock() -> Clock:
    """야간(UTC 23시) 고정 시각 시계."""
    return Clock(fixed="2026-06-01T23:30:00Z")


@pytest.fixture
def alias() -> AliasMap:
    return AliasMap()


@pytest.fixture
def mk_reading():
    """SensorReading 생성 헬퍼 팩토리."""

    def _make(
        entity_id: str,
        raw_value: Any,
        *,
        ts_utc: str = "2026-06-01T12:00:00+00:00",
        norm_value: Any = ...,
        home_id: str = "singleton",
        source: str = "fake",
    ) -> SensorReading:
        nv = normalize(raw_value) if norm_value is ... else norm_value
        return SensorReading(
            entity_id=entity_id,
            source=source,
            raw_value=raw_value,
            norm_value=nv,
            ts_utc=ts_utc,
            home_id=home_id,
        )

    return _make


@pytest.fixture
def mk_candidate():
    """CandidateAction 생성 헬퍼 팩토리."""

    def _make(
        *,
        capability: str,
        action: str,
        entity_id: str,
        category: str,
        params: dict[str, Any] | None = None,
    ) -> CandidateAction:
        return CandidateAction(
            capability=capability,
            action=action,
            params=params or {},
            entity_id=entity_id,
            category=category,
        )

    return _make


@pytest.fixture
def fake_source(mk_reading) -> FakeSensorSource:
    """결정론적 FakeSensorSource(조명/온도/가스 읽기)."""
    return FakeSensorSource(
        [
            mk_reading("light.living_room", "on", ts_utc="2026-06-01T12:00:00+00:00"),
            mk_reading("climate.living_room", 19.0, ts_utc="2026-06-01T12:01:00+00:00"),
            mk_reading("switch.gas_valve", "detected", ts_utc="2026-06-01T12:02:00+00:00"),
        ]
    )


@pytest.fixture
def app(clock: Clock) -> LiotApp:
    """기본 LiotApp(FakeLLM 백엔드, 낮 고정 시각)."""
    return LiotApp(clock=clock)


@pytest_asyncio.fixture
async def tmp_db(tmp_path):
    """스키마가 적용된 임시 파일 SQLite 연결. 종료 시 close."""
    db_path = str(tmp_path / "liot_test.db")
    db = await init_db(db_path, durable=False)
    try:
        yield db
    finally:
        await db.close()
