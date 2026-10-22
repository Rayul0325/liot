"""시계(Clock).

UTC ISO8601 타임스탬프와 하루의 시간대 버킷(morning/day/evening/night)을
제공한다. 테스트 재현성을 위해 고정 시각을 주입할 수 있다.

집 시간대(tz): 타임스탬프·시각 계산은 늘 UTC 다. 시간대 버킷과 '하루'(능동 예산의 날짜
묶음)만 집의 지역 시각으로 센다. tz 를 주지 않으면 UTC 그대로다(예전 동작). 데몬 진입점은
``LIOT_HOME_TZ``(기본 Asia/Seoul)를 넘긴다.

trust_ok: 시각 신뢰 여부 플래그. NTP 신뢰 불가 등으로 시각을 믿을 수 없으면
False 가 되며, 시각에 의존하는 판단을 보수적으로(승격 쪽으로) 다루게 한다.
"""

from __future__ import annotations

from datetime import UTC, datetime, tzinfo
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

__all__ = ["Clock", "TimeBucket", "resolve_tz"]

# 시간대 버킷 경계(집 지역 시각의 시(hour), tz 미지정이면 UTC). [start, end) 반열린 구간.
#   night   : 22:00 ~ 06:00
#   morning : 06:00 ~ 11:00
#   day     : 11:00 ~ 17:00
#   evening : 17:00 ~ 22:00
TimeBucket = str  # 'morning' | 'day' | 'evening' | 'night'


def resolve_tz(tz: str | tzinfo | None) -> tzinfo:
    """시간대 이름(IANA, 예 'Asia/Seoul') 또는 tzinfo → tzinfo. None 이면 UTC.

    모르는 이름은 ``ValueError``(한국어 한 줄) — 조용히 UTC 로 떨어지지 않는다.
    """
    if tz is None:
        return UTC
    if isinstance(tz, tzinfo):
        return tz
    name = tz.strip()
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError, OSError):
        raise ValueError(f"알 수 없는 시간대입니다: {tz!r} (예: Asia/Seoul)") from None


class Clock:
    """시각 공급자. 고정 시각을 주입하면 결정론적으로 동작한다."""

    def __init__(
        self,
        fixed: datetime | str | None = None,
        trust_ok: bool = True,
        *,
        tz: str | tzinfo | None = None,
    ) -> None:
        """fixed: 고정할 시각(datetime 또는 ISO8601 문자열). None 이면 실시간.

        고정 시각이 tz-naive 면 UTC 로 간주한다.
        tz: 집 시간대(시간대 버킷·'하루'를 셀 지역 시각). None 이면 UTC(예전 동작).
        """
        self._fixed: datetime | None = None
        if fixed is not None:
            self._fixed = self._coerce_dt(fixed)
        self.trust_ok = trust_ok
        self.tz: tzinfo = resolve_tz(tz)

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
            dt = dt.replace(tzinfo=UTC)
        return dt.astimezone(UTC)

    def _raw_now(self) -> datetime:
        if self._fixed is not None:
            return self._fixed
        return datetime.now(UTC)

    def now_utc(self) -> str:
        """현재(또는 고정된) 시각의 UTC ISO8601 문자열.

        형식: 'YYYY-MM-DDTHH:MM:SS+00:00' (마이크로초는 잘라 안정화).
        """
        return self._raw_now().replace(microsecond=0).isoformat()

    def now_dt(self) -> datetime:
        """현재(또는 고정된) tz-aware UTC datetime."""
        return self._raw_now()

    def local_dt(self, when: datetime | str | None = None) -> datetime:
        """집 지역 시각(tz-aware). tz 미지정이면 UTC — ``now_dt()`` 와 같다.

        '하루'(날짜)나 시(hour)를 셀 때만 쓴다. 저장·비교용 타임스탬프는 UTC 를 쓴다.
        """
        dt = self._raw_now() if when is None else self._coerce_dt(when)
        return dt.astimezone(self.tz)

    def time_bucket(self, when: datetime | str | None = None) -> TimeBucket:
        """시각을 'morning'/'day'/'evening'/'night' 버킷으로 분류한다.

        when 미지정 시 현재 시각을 사용한다. 집 지역 시각의 시(hour) 기준(tz 미지정이면 UTC).
        """
        hour = self.local_dt(when).hour
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
