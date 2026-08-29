"""결정론적 가짜 센서 소스.

주입된 ``SensorReading`` 리스트(또는 raw 튜플)를 등록 순서대로 yield 한다.
네트워크·시간에 의존하지 않으므로 테스트·데모에서 완전히 재현 가능하다.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Iterable, Sequence

from liot.core.contracts import SensorReading

from .normalizer import normalize

__all__ = ["FakeSensorSource"]


class FakeSensorSource:
    """미리 준비된 읽기들을 순서대로 방출하는 SensorSource 구현.

    두 가지 입력 형태를 받는다:
      - 이미 만들어진 ``SensorReading`` 인스턴스들의 시퀀스, 또는
      - ``from_raw`` 로 (entity_id, raw_value, ...) 원시 입력에서 생성.

    결정론적: 동일 입력 -> 동일 출력 순서. 내부 상태를 바꾸지 않으므로
    여러 번 stream() 해도 같은 시퀀스를 재생한다.
    """

    def __init__(self, readings: Sequence[SensorReading] | None = None) -> None:
        self._readings: list[SensorReading] = list(readings or [])

    @classmethod
    def from_raw(
        cls,
        rows: Iterable[dict],
        *,
        default_source: str = "fake",
        home_id: str = "singleton",
    ) -> FakeSensorSource:
        """원시 dict 행들에서 SensorReading 리스트를 만들어 소스를 구성한다.

        각 행 필수 키: entity_id, raw_value, ts_utc. 선택: source, unit,
        confidence, norm_value(미지정 시 normalize 로 계산), home_id.
        """
        readings: list[SensorReading] = []
        for row in rows:
            raw = row["raw_value"]
            norm = row["norm_value"] if "norm_value" in row else normalize(raw)
            readings.append(
                SensorReading(
                    entity_id=row["entity_id"],
                    source=row.get("source", default_source),
                    raw_value=raw,
                    norm_value=norm,
                    unit=row.get("unit"),
                    confidence=row.get("confidence", 1.0),
                    ts_utc=row["ts_utc"],
                    home_id=row.get("home_id", home_id),
                )
            )
        return cls(readings)

    def add(self, reading: SensorReading) -> None:
        """읽기를 큐 끝에 추가한다."""
        self._readings.append(reading)

    async def stream(self) -> AsyncIterator[SensorReading]:
        """등록된 읽기들을 순서대로 yield 한다(결정론적)."""
        for reading in self._readings:
            yield reading
