"""시계(Clock).

UTC ISO8601 타임스탬프와 하루의 시간대 버킷(morning/day/evening/night)을
제공한다. 테스트 재현성을 위해 고정 시각을 주입할 수 있다.

trust_ok: 시각 신뢰 여부 플래그. NTP 신뢰 불가 등으로 시각을 믿을 수 없으면
False 가 되며, 시각에 의존하는 판단을 보수적으로(승격 쪽으로) 다루게 한다.
"""

from __future__ import annotations

from datetime import datetime, timezone

__all__ = ["Clock", "TimeBucket"]

# 시간대 버킷 경계(로컬 시간 기준 시(hour)). [start, end) 반열린 구간.
#   night   : 22:00 ~ 06:00
#   morning : 06:00 ~ 11:00
#   day     : 11:00 ~ 17:00
#   evening : 17:00 ~ 22:00
TimeBucket = str  # 'morning' | 'day' | 'evening' | 'night'


class Clock:
    """시각 공급자. 고정 시각을 주입하면 결정론적으로 동작한다."""

    def __init__(
        self,
        fixed: datetime | str | None = None,
        trust_ok: bool = True,
    ) -> None:
        """fixed: 고정할 시각(datetime 또는 ISO8601 문자열). None 이면 실시간.

        고정 시각이 tz-naive 면 UTC 로 간주한다.
        """
        self._fixed: datetime | None = None
        if fixed is not None:
            self._fixed = self._coerce_dt(fixed)
        self.trust_ok = trust_ok

    @staticmethod
    def _coerce_dt(value: datetime | str) -> datetime:
        if isinstance(value, datetime):
            dt = value
        else:
            text = value.strip()
            if text.endswith("Z"):
                text = text[:-1] + "+00:00"
            dt = datetime.fromisoformat(text)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)

    def _raw_now(self) -> datetime:
        if self._fixed is not None:
            return self._fixed
        return datetime.now(timezone.utc)

    def now_utc(self) -> str:
        """현재(또는 고정된) 시각의 UTC ISO8601 문자열.

        형식: 'YYYY-MM-DDTHH:MM:SS+00:00' (마이크로초는 잘라 안정화).
        """
        return self._raw_now().replace(microsecond=0).isoformat()

    def now_dt(self) -> datetime:
        """현재(또는 고정된) tz-aware UTC datetime."""
        return self._raw_now()

    def time_bucket(self, when: datetime | str | None = None) -> TimeBucket:
        """시각을 'morning'/'day'/'evening'/'night' 버킷으로 분류한다.

        when 미지정 시 현재 시각을 사용한다. (UTC 시 기준)
        """
        if when is None:
            dt = self._raw_now()
        else:
            dt = self._coerce_dt(when)
        hour = dt.hour
        if 6 <= hour < 11:
            return "morning"
        if 11 <= hour < 17:
            return "day"
        if 17 <= hour < 22:
            return "evening"
        return "night"

    def set_fixed(self, fixed: datetime | str | None) -> None:
        """고정 시각을 교체/해제한다(테스트 편의)."""
        self._fixed = self._coerce_dt(fixed) if fixed is not None else None
