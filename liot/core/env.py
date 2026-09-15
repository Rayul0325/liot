"""환경변수 파싱 공용 헬퍼.

불리언 플래그 해석이 모듈마다 복제돼 있었다(serve·executor·event_detector, 각각
튜플 순서만 다른 같은 코드). 한 곳이 바뀌면 나머지가 조용히 갈라지므로 여기로 모은다.
"""

from __future__ import annotations

import os

__all__ = ["TRUTHY", "env_flag"]

# 참으로 보는 값(소문자 비교). 그 밖의 모든 값(빈 문자열·"0"·"off"·오타)은 거짓 —
# 옵트인 플래그라 '모르는 값'은 켜지 않는 쪽이 안전하다.
TRUTHY: frozenset[str] = frozenset({"1", "true", "yes", "on"})


def env_flag(name: str, *, default: bool = False) -> bool:
    """환경변수를 불리언으로 읽는다. 미설정이면 default."""
    raw = os.environ.get(name)
    if raw is None or not raw.strip():
        return default
    return raw.strip().lower() in TRUTHY
