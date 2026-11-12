"""HA 감독의 순수 규칙 — 재연결 대기(백오프)와 '감시 중단/재개' 알림 시점.

시간과 I/O 를 모른다: 모든 메서드가 ``now``(단조 시계 초)를 인자로 받는다. 그래서 120초
경계·깜빡임 누적 같은 규칙을 가짜 시계 없이 값으로 고정해 시험할 수 있다. 실제로 기다리고 보내는 것은
``LiotService._ha_supervisor`` 몫이다.

정책(소유자 결정 2026-11-12, 결정 기록 0014):
  - 대기: 1초에서 시작해 두 배씩 60초까지. **연결이 5분 넘게 안정된 뒤에만** 1초로 되돌린다
    (연결됐다 곧 끊기는 깜빡임이 1초 간격 재시도로 HA 를 두드리지 못하게).
  - 인증 거부(HAAuthError): HA 의 IP 차단을 피하려고 대기를 5분으로 둔다.
  - '감시 중단' 알림은 한 번의 끊김(=알림 없이 이어지는 연속 구간)당 한 번, 아래 중 먼저 오는 쪽:
      (a) 연속 120초 끊김,
      (b) 최근 10분 안 끊김 누적이 120초,
      (c) 시작 뒤 첫 연결이 120초 안에 성공하지 못함 — 시작 시각부터 이미 끊긴 것으로 세므로
          (a) 의 특수한 경우다(reason 만 'startup' 으로 구분한다).
    경계는 포함이다: 120초에 이르면 알린다.
  - '감시 재개'는 알림이 나간 끊김이 끝날 때 한 번. 재개하면 누적 창을 비워, 깜빡임이 알림을 연달아
    만들지 못하게 한다(다음 중단은 새로 120초가 쌓여야 한다).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Final

__all__ = [
    "AUTH_BACKOFF_S",
    "BACKOFF_INITIAL_S",
    "BACKOFF_MAX_S",
    "BACKOFF_STABLE_RESET_S",
    "NOTICE_CONTINUOUS_S",
    "NOTICE_CUMULATIVE_S",
    "NOTICE_WINDOW_S",
    "Backoff",
    "NoticeDue",
    "OutageTracker",
    "Recovery",
]

BACKOFF_INITIAL_S: Final[float] = 1.0
BACKOFF_MAX_S: Final[float] = 60.0
# 이만큼 연속으로 연결을 유지해야 대기를 처음(1초)으로 되돌린다.
BACKOFF_STABLE_RESET_S: Final[float] = 300.0
# 인증 거부 뒤 대기. 잘못된 토큰으로 두드리면 HA 가 IP 를 막는다.
AUTH_BACKOFF_S: Final[float] = 300.0

NOTICE_CONTINUOUS_S: Final[float] = 120.0
NOTICE_CUMULATIVE_S: Final[float] = 120.0
NOTICE_WINDOW_S: Final[float] = 600.0


class Backoff:
    """지수 대기: ``next_delay()`` 가 1, 2, 4 … 60, 60 … 을 돌려준다."""

    def __init__(
        self,
        initial_s: float = BACKOFF_INITIAL_S,
        max_s: float = BACKOFF_MAX_S,
    ) -> None:
        self._initial = initial_s
        self._max = max_s
        self._current = initial_s

    def next_delay(self) -> float:
        delay = self._current
        self._current = min(self._current * 2, self._max)
        return delay

    def reset(self) -> None:
        self._current = self._initial


@dataclass(frozen=True)
class NoticeDue:
    """'감시 중단' 알림을 낼 때가 됐다. ``reason``: startup · continuous · cumulative."""

    reason: str
    down_s: float


@dataclass(frozen=True)
class Recovery:
    """끊김이 끝났다. ``notified``: 그 끊김에서 '감시 중단' 알림 결정이 있었는가."""

    down_s: float
    notified: bool


class OutageTracker:
    """끊김 구간을 세고 '감시 중단' 알림 시점을 정한다. 시작 시각부터 '끊김' 상태다.

    시작을 끊김으로 두는 이유: 데몬이 뜬 직후에는 아직 HA 에 붙은 적이 없어 센서를 하나도 못
    읽는다. 첫 연결이 120초 안에 안 되면 그것도 감시 중단이다(HA 가 꺼진 채 데몬만 시작된 경우).
    """

    def __init__(
        self,
        start: float,
        *,
        continuous_s: float = NOTICE_CONTINUOUS_S,
        cumulative_s: float = NOTICE_CUMULATIVE_S,
        window_s: float = NOTICE_WINDOW_S,
    ) -> None:
        self._continuous_s = continuous_s
        self._cumulative_s = cumulative_s
        self._window_s = window_s
        self._down_since: float | None = start
        self._ever_up = False
        self._notified = False
        # 끝난 끊김 구간(시작, 끝). 누적 창 밖은 버린다.
        self._history: list[tuple[float, float]] = []
        # 마지막으로 '올라온'(재동기화까지 마친) 시각. 안정 판정에 쓴다.
        self.up_since: float | None = None

    # ── 상태 ────────────────────────────────────────────────────────────
    @property
    def is_down(self) -> bool:
        return self._down_since is not None

    @property
    def notified(self) -> bool:
        return self._notified

    def mark_down(self, now: float) -> None:
        """연결이 끊겼다(또는 여전히 끊겨 있다). 이미 끊겨 있으면 시작 시각을 바꾸지 않는다."""
        self.up_since = None
        if self._down_since is None:
            self._down_since = now

    def mark_up(self, now: float) -> Recovery | None:
        """연결이 복구됐다(재동기화까지 끝났다). 끊겨 있었으면 ``Recovery`` 를 돌려준다."""
        self.up_since = now
        since = self._down_since
        if since is None:
            return None
        self._down_since = None
        self._ever_up = True
        was_notified = self._notified
        self._notified = False
        if was_notified:
            self._history = []  # 재개 뒤에는 누적을 새로 센다
        else:
            self._history.append((since, now))
            self._prune(now)
        return Recovery(down_s=now - since, notified=was_notified)

    def mark_notified(self) -> None:
        """이 끊김의 '감시 중단' 결정을 냈다 — 같은 끊김에서 다시 내지 않는다."""
        self._notified = True

    # ── 판정 ────────────────────────────────────────────────────────────
    def _prune(self, now: float) -> None:
        cutoff = now - self._window_s
        self._history = [(a, b) for a, b in self._history if b > cutoff]

    def _cumulative(self, now: float) -> float:
        """최근 ``window_s`` 안의 끊김 누적(끝난 구간 + 지금 진행 중인 구간)."""
        cutoff = now - self._window_s
        total = 0.0
        for a, b in self._history:
            total += max(0.0, b - max(a, cutoff))
        if self._down_since is not None:
            total += max(0.0, now - max(self._down_since, cutoff))
        return total

    def notice_due(self, now: float) -> NoticeDue | None:
        """지금 '감시 중단'을 내야 하는가. 이미 냈거나 끊겨 있지 않으면 None."""
        if self._down_since is None or self._notified:
            return None
        down_s = now - self._down_since
        if down_s >= self._continuous_s:
            return NoticeDue("continuous" if self._ever_up else "startup", down_s)
        if self._cumulative(now) >= self._cumulative_s:
            return NoticeDue("cumulative", down_s)
        return None

    def next_deadline(self, now: float) -> float | None:
        """알림이 나가야 하는 가장 이른 시각(단조 시계). 낼 일이 없으면 None.

        누적 쪽은 '지금부터 끊김이 계속된다'는 가정의 예측이다(창이 밀려 값이 줄 수 있어 도착
        뒤 다시 판정한다).
        """
        if self._down_since is None or self._notified:
            return None
        continuous = self._down_since + self._continuous_s
        cumulative = now + max(0.0, self._cumulative_s - self._cumulative(now))
        return min(continuous, cumulative)
