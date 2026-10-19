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
from typing import Any, Protocol, runtime_checkable

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

    주입된 값을 그대로 돌려준다. ``degraded=True`` 면 조회 실패 상황을
    시뮬레이션한다(수치는 비우고 보수적 선택을 유도). ``set_values`` 로 값을 바꾸면
    다음 ``fetch`` 부터 반영된다(하루 시뮬레이터가 단계마다 날씨를 갈아 끼운다).
    """

    _FIELDS = frozenset({"outdoor_temp_c", "outdoor_aqi", "pm25", "cloud_cover", "degraded"})

    def __init__(
        self,
        *,
        clock: Clock | None = None,
        outdoor_temp_c: float | None = None,
        outdoor_aqi: int | None = None,
        pm25: float | None = None,
        cloud_cover: float | None = None,
        degraded: bool = False,
        source: str = "fake",
    ) -> None:
        self._clock = clock or Clock()
        self._values: dict[str, Any] = {
            "outdoor_temp_c": outdoor_temp_c,
            "outdoor_aqi": outdoor_aqi,
            "pm25": pm25,
            "cloud_cover": cloud_cover,
            "degraded": degraded,
        }
        self._source = source

    def set_values(self, **values: Any) -> None:
        """주어진 항목만 바꾼다(나머지는 그대로). 모르는 항목은 ``ValueError``."""
        unknown = set(values) - self._FIELDS
        if unknown:
            raise ValueError(f"unknown external fields: {sorted(unknown)}")
        self._values.update(values)

    async def fetch(self) -> ExternalContext:
        if self._values["degraded"]:
            return ExternalContext(
                source=self._source,
                fetched_at=self._clock.now_utc(),
                degraded=True,
            )
        return ExternalContext(
            outdoor_temp_c=self._values["outdoor_temp_c"],
            outdoor_aqi=self._values["outdoor_aqi"],
            pm25=self._values["pm25"],
            cloud_cover=self._values["cloud_cover"],
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
