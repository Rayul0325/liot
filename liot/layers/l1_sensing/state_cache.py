"""엔티티별 최신 상태 캐시(인메모리).

L1 은 스트림을 흘려보내지만, L2 가 맥락을 구성하려면 "각 엔티티의 현재
값"이라는 스냅샷이 필요하다. StateCache 는 entity_id -> 최신 SensorReading
매핑을 보관하고, ts_utc 기준으로 더 오래된 갱신은 무시한다(역행 방지).

재시작 시 DB의 마지막 스냅샷에서 seed_from_snapshot 으로 시드한다.
home_id 별로 격리된 캐시를 권장한다(인스턴스를 home 단위로 생성).
"""

from __future__ import annotations

from collections.abc import Iterable, Iterator

from liot.core.contracts import SensorReading

__all__ = ["StateCache"]


class StateCache:
    """entity_id -> 최신 SensorReading 인메모리 캐시."""

    def __init__(self) -> None:
        self._latest: dict[str, SensorReading] = {}

    def update(self, reading: SensorReading) -> bool:
        """읽기로 캐시를 갱신한다.

        같은 entity_id 의 기존 값보다 ts_utc 가 더 오래되었으면 무시(False).
        새로 반영했으면 True. ts 비교는 ISO8601 사전식(=시간 순서) 비교.
        """
        prev = self._latest.get(reading.entity_id)
        if prev is not None and reading.ts_utc < prev.ts_utc:
            return False
        self._latest[reading.entity_id] = reading
        return True

    def get(self, entity_id: str) -> SensorReading | None:
        """엔티티의 최신 읽기. 없으면 None."""
        return self._latest.get(entity_id)

    def snapshot(self) -> dict[str, SensorReading]:
        """현재 캐시의 얕은 사본(entity_id -> 최신 읽기)."""
        return dict(self._latest)

    def norm_values(self) -> dict[str, object]:
        """entity_id -> norm_value 만 추린 사전(맥락 구성 입력용)."""
        return {eid: r.norm_value for eid, r in self._latest.items()}

    def seed_from_snapshot(self, readings: Iterable[SensorReading]) -> None:
        """재시작 시 영속 스냅샷(읽기들)으로 캐시를 시드한다.

        update 와 동일한 역행 방지 규칙을 적용한다.
        """
        for reading in readings:
            self.update(reading)

    def __len__(self) -> int:
        return len(self._latest)

    def __iter__(self) -> Iterator[str]:
        return iter(self._latest)

    def __contains__(self, entity_id: object) -> bool:
        return entity_id in self._latest
