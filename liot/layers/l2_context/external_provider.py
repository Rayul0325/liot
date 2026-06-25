"""외부 데이터 조회 인터페이스 (개념 L3 Context Acquisition).

내부 센서만으로 판단이 부족할 때 외부 날씨·외기 공기질 등을 가져온다.
하드웨어/키 없이도 테스트되도록 Protocol + Fake 로 분리한다(라즈베리파이 비종속) —
센서·제어 계층과 AI 판단 계층 사이의 경계를 Protocol 로 끊는다.

실제 API(기상청 등)는 env 게이트 뒤의 옵트인이며, 이번 단계에선 실 HTTP 미연결.
외부가 미상이면 침묵하지 않고 ``degraded=True`` 로 보수적 ExternalContext 를
돌려준다(Safety-Never-Silent 와 정합 — 모르면 보수적으로).
"""

from __future__ import annotations

import os
from typing import Protocol, runtime_checkable

from liot.core.clock import Clock
from liot.core.contracts import ExternalContext

__all__ = [
    "ExternalDataProvider",
    "FakeExternalProvider",
    "KmaExternalProvider",
    "build_external_provider",
]


def _external_enabled() -> bool:
    """LIOT_EXTERNAL_ENABLED 옵트인 플래그(build_execution_policy 와 동일 관례)."""
    return os.environ.get("LIOT_EXTERNAL_ENABLED", "").strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }


@runtime_checkable
class ExternalDataProvider(Protocol):
    """외부 맥락(날씨·외기 공기질)을 비동기로 가져오는 소스의 공통 계약."""

    async def fetch(self) -> ExternalContext:
        """현재 외부 맥락을 반환한다. 실패 시에도 예외가 아니라 degraded 로."""
        ...


class FakeExternalProvider:
    """결정론적 외부 데이터. 테스트·오프라인 기본값(네트워크 없음).

    주입된 고정값을 그대로 돌려준다. ``degraded=True`` 면 조회 실패 상황을
    시뮬레이션한다(수치는 비우고 보수적 선택을 유도).
    """

    def __init__(
        self,
        *,
        clock: Clock | None = None,
        outdoor_temp_c: float | None = None,
        outdoor_aqi: int | None = None,
        degraded: bool = False,
        source: str = "fake",
    ) -> None:
        self._clock = clock or Clock()
        self._temp = outdoor_temp_c
        self._aqi = outdoor_aqi
        self._degraded = degraded
        self._source = source

    async def fetch(self) -> ExternalContext:
        if self._degraded:
            return ExternalContext(
                outdoor_temp_c=None,
                outdoor_aqi=None,
                source=self._source,
                fetched_at=self._clock.now_utc(),
                degraded=True,
            )
        return ExternalContext(
            outdoor_temp_c=self._temp,
            outdoor_aqi=self._aqi,
            source=self._source,
            fetched_at=self._clock.now_utc(),
            degraded=False,
        )


class KmaExternalProvider:
    """기상청/공기질 실연동 스텁(옵트인). 이번 단계에선 실 HTTP 미연결(예정).

    env ``LIOT_EXTERNAL_ENABLED`` 이 꺼져 있으면 Fake 로 폴백한다. 켜져 있어도
    실제 호출부가 아직 없으므로 ``degraded=True`` 인 보수적 ExternalContext 를
    돌려준다(절대 침묵하지 않음). 실 HTTP·egress(KMA) 연동은 후속 작업.
    """

    def __init__(
        self,
        *,
        clock: Clock | None = None,
        fallback: ExternalDataProvider | None = None,
    ) -> None:
        self._clock = clock or Clock()
        self._fallback: ExternalDataProvider = fallback or FakeExternalProvider(
            clock=self._clock
        )

    async def fetch(self) -> ExternalContext:
        if not _external_enabled():
            return await self._fallback.fetch()
        # 실 HTTP 연동 미구현(예정) → 보수적 degraded 반환.
        return ExternalContext(
            outdoor_temp_c=None,
            outdoor_aqi=None,
            source="kma",
            fetched_at=self._clock.now_utc(),
            degraded=True,
        )


def build_external_provider(*, clock: Clock | None = None) -> ExternalDataProvider:
    """env 기반 provider 구성. 기본은 Fake(빈 외부맥락), 옵트인 시 KMA 스텁."""
    if _external_enabled():
        return KmaExternalProvider(clock=clock)
    return FakeExternalProvider(clock=clock)
