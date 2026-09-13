"""공용 pytest 픽스처.

  - tmp_db        : 임시 파일 SQLite(init_db 로 스키마 적용). 함수 스코프.
  - clock         : 고정 시각 Clock(결정론).
  - night_clock   : 야간(UTC 23시) 고정 시각 Clock.
  - fake_source   : FakeSensorSource(결정론적 읽기 시퀀스).
  - app           : 기본 LiotApp(FakeLLM 백엔드).
  - alias         : 빈 AliasMap.
  - mk_reading    : SensorReading 생성 헬퍼.
  - mk_candidate  : CandidateAction 생성 헬퍼.
  - demo_state_reset : 데모 서버 세션 전역(_REGISTRY·_PREF_CELLS·_DIRECTIVES) 초기화.
  - demo_serve    : 루프백 임의 포트에 데모 서버를 띄우는 컨텍스트 매니저 팩토리.

asyncio_mode='auto' 이므로 async 테스트/픽스처에 별도 마커가 필요 없다.
모든 테스트는 외부 네트워크를 호출하지 않는다(데모 서버 테스트는 127.0.0.1 루프백만).
"""

from __future__ import annotations

import threading
from collections.abc import Callable, Iterator
from contextlib import AbstractContextManager, contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
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


# ── 데모 서버(liot.demo_server) 공용 ──────────────────────────────────────
# demo_server 는 모듈 전역 가변 상태와 실 HTTP 서버를 쓴다. 초기화·서버 기동을 테스트
# 모듈마다 복사해 두면 한쪽만 고쳐진다 — 실제로 세 복사본 모두 _DIRECTIVES 초기화가
# 빠져 있었다(2026-09-01 리뷰 R2-30). 여기 한 곳에 둔다.


@pytest.fixture
def demo_state_reset():
    """데모 서버 세션 전역(_REGISTRY·_PREF_CELLS·_DIRECTIVES)을 테스트 전후로 초기화.

    데모 테스트 모듈이 ``pytestmark = pytest.mark.usefixtures("demo_state_reset")``
    로 켠다. autouse 로 두지 않는 건 데모와 무관한 테스트가 demo_server 를 import
    하거나 레지스트리를 매번 다시 만들 이유가 없어서다. (프로세스 전역 안전 확장은
    위 autouse 픽스처가 이미 매 테스트 비운다.)
    """
    from liot import demo_server

    def _reset() -> None:
        demo_server._REGISTRY.reset()
        demo_server._PREF_CELLS.clear()
        demo_server._DIRECTIVES.clear()

    _reset()
    yield
    _reset()


@contextmanager
def _serve_demo(
    timeout: int | None = None, handler: type[BaseHTTPRequestHandler] | None = None
) -> Iterator[int]:
    """루프백 임의 포트(0)에 데모 서버를 띄우고 포트를 넘긴다(고정 포트는 쓰지 않는다).

    ``timeout`` 은 ``demo_server.Handler`` 소켓 타임아웃을 잠시 덮어쓰고(끝나면 복구),
    ``handler`` 는 모듈 사본의 Handler 로 띄울 때만 준다. ``shutdown()`` 은
    serve_forever 의 폴링 간격만큼 기다리므로 간격을 0.05초로 줄인다 — 기본 0.5초면
    서버를 띄울 때마다 그 대기가 쌓인다.
    """
    from liot import demo_server

    original = demo_server.Handler.timeout
    if timeout is not None:
        demo_server.Handler.timeout = timeout
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), handler or demo_server.Handler)
    thread = threading.Thread(
        target=httpd.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True
    )
    thread.start()
    try:
        yield httpd.server_port
    finally:
        httpd.shutdown()
        httpd.server_close()
        thread.join(timeout=5)
        demo_server.Handler.timeout = original


@pytest.fixture(scope="session")
def demo_serve() -> Callable[..., AbstractContextManager[int]]:
    """``with demo_serve() as port:`` — 테스트·모듈 스코프 픽스처가 서버를 띄울 때 쓴다."""
    return _serve_demo
