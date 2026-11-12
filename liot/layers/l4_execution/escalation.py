"""상황별 에스컬레이션 사다리 정책(T8) — 순수 계산 + 읽기 전용 조회.

사다리는 열린 상황 하나에 하나다. 이 모듈은 '언제 무엇을 보내야 하는가'만
계산하고, 실제 송신·영속은 서비스(LiotService)가 한다. 네트워크도 쓰기도 없다.

두 값이 서로 다른 것을 움직인다(spec):
  - **최종 파급도**(모델 상향 포함 = 그 상황 제안들의 최대 파급도) → 첫 알림 세기와
    거주자 재알림 주기.
  - **상황 하한**(situations.severity_floor = 표 + 거주자) → 보호자 전달과 119 안내.
    모델만 CRITICAL 로 올린 상황은 1분 재알림을 받지만 보호자·119 는 없다.

사다리(모든 수치는 조정 가능한 기본값 — 아래 모듈 상수):
  - RECORD: 다이제스트, 사다리 없음.   NOTICE: 일반 푸시 1회, 재알림 없음.
  - WARNING: 무응답이면 10분마다 재알림, 최대 3회.
  - 첫 거주자 알림 송신이 실패하면 사다리의 다음 발화를 LADDER_RETRY(30초) 뒤로 당겨
    첫 알림을 다시 보낸다(성공할 때까지). 재알림·보호자 전달·119 시각의 기준은 그대로
    '기준 시각'이라 이 재시도가 그것들을 옮기지 않고, 재알림 횟수도 쓰지 않는다.
  - CRITICAL: 무응답이면 1분마다 재알림.
      하한이 CRITICAL 이면 기준 시각 90초 뒤 동의한 보호자 전원에게 동시 전달,
      그 전달 5분 뒤에도 아무도 확인하지 않으면 거주자·보호자에게 119 전화 **안내**.
      동의한 보호자가 없어도 119 안내는 같은 시각(기준 시각 + 90초 + 5분)에 나간다.

기준 시각(anchor): 사다리 시각(재알림·보호자 전달·119)은 모두 한 시각에서 센다. 보통 그것은
상황이 열린 시각(situations.opened_at)이다. 단 하나의 예외 — 프로세스가 죽어 사다리 없이 남은
오래 묵은 상황을 복구가 지금 **처음으로** 거주자에게 알릴 때, 그 상황이 이미 90초(거주자 우선
창)보다 오래 열려 있었다면 기준 시각은 그 복구 송신 시각이다(escalation_ladders.anchor_at,
결정 0017). 안 그러면 거주자의 첫 알림 직후 보호자 전달이 나가 '거주자 먼저, 90초 뒤 보호자'
순서가 무너진다. 이 모듈의 함수는 기준 시각을 인자(opened)로 받을 뿐 어느 쪽인지 모른다 —
고르는 일은 서비스(LiotService._ladder_anchor)가 한다. 보호자·119 메시지에 싣는 '언제'는 기준이
아니라 사실 그대로 상황이 열린 시각이다.

119 는 안내 문구일 뿐이다. 이 모듈(과 사다리 전체)에는 전화를 걸거나 자동으로
신고하는 경로가 없다.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Final, Literal

from liot.core.severity import SeverityLevel

if TYPE_CHECKING:  # pragma: no cover
    import aiosqlite

    from .notifier import NotificationContext

__all__ = [
    "CRITICAL_REALERT_INTERVAL",
    "GUARDIAN_FORWARD_AFTER",
    "GUIDANCE_119_AFTER_FORWARD",
    "GUIDANCE_119_TEXT",
    "LADDER_RESOLUTION_S",
    "LADDER_RETRY",
    "SENSOR_KIND_LABEL_KO",
    "SITUATION_LABEL_KO",
    "UNMONITORABLE_TEXT",
    "WARNING_REALERT_INTERVAL",
    "WARNING_REALERT_MAX",
    "MessageKind",
    "SituationInfo",
    "final_severity_of",
    "forward_due_at",
    "guardian_path",
    "guidance_due_at",
    "latest_alert_proposal_id",
    "load_situation",
    "situation_notified",
    "message_label",
    "needs_ladder",
    "next_fire_at",
    "next_realert_at",
    "parse_iso",
    "realert_allowed",
    "realert_cap",
    "realert_interval",
    "realerts_due",
    "sensor_kind_label",
    "situation_label",
    "to_iso",
]

# ── 조정 가능한 기본값 ──────────────────────────────────────────────────────
# 사다리 루프의 해상도(초). 300초 틱과 별개로 돈다 — 90초·5분 경계가 틱 주기에
# 묻히지 않게. 다음 발화 시각은 DB(next_fire_at)에서 읽는다.
LADDER_RESOLUTION_S: Final[float] = 5.0
# 송신이 실패한 단계(거주자 첫 알림·보호자 전달·119 안내)를 다시 시도하기까지의 간격.
LADDER_RETRY: Final[timedelta] = timedelta(seconds=30)
CRITICAL_REALERT_INTERVAL: Final[timedelta] = timedelta(minutes=1)
WARNING_REALERT_INTERVAL: Final[timedelta] = timedelta(minutes=10)
WARNING_REALERT_MAX: Final[int] = 3
GUARDIAN_FORWARD_AFTER: Final[timedelta] = timedelta(seconds=90)
GUIDANCE_119_AFTER_FORWARD: Final[timedelta] = timedelta(minutes=5)

# 상황 카테고리 → 보호자·거주자 요약 알림에 싣는 한국어 상황 종류 라벨.
# (demo_server 의 화면 표기 표는 데몬이 import 할 수 없는 모듈에 있어 여기 둔다.)
SITUATION_LABEL_KO: Final[dict[str, str]] = {
    "gas": "가스 누출",
    "smoke": "연기·화재",
    "oven": "오븐 과열",
    "high_temp": "고온",
    "leak": "누수",
    "water_main": "수도 이상",
    "door": "문 열림",
    "lock": "잠금장치 이상",
    "inactivity": "움직임 없음",
}
DEFAULT_SITUATION_LABEL: Final[str] = "안전 상황"
RESOLVED_TEXT: Final[str] = "해소됨"

# 상황 카테고리 → 감시 불가 알림에 싣는 한국어 '센서 종류' 라벨. 상황 종류 라벨
# (SITUATION_LABEL_KO, 예: '가스 누출')과 따로 둔다 — 감시 불가는 위험이 생긴 게 아니라
# 센서를 읽지 못하는 것이라 '가스 누출'이라고 쓰면 거짓 경보가 된다. 기기 이름은 쓰지 않는다.
SENSOR_KIND_LABEL_KO: Final[dict[str, str]] = {
    "gas": "가스",
    "smoke": "연기",
    "oven": "오븐",
    "high_temp": "온도",
    "leak": "누수",
    "water_main": "수도",
    "door": "문",
    "lock": "잠금장치",
    "inactivity": "움직임",
}
DEFAULT_SENSOR_KIND_LABEL: Final[str] = "안전"
UNMONITORABLE_TEXT: Final[str] = "감시 불가(연결 끊김)"
# 119 는 안내다 — 사람이 직접 전화하도록 번호 문구만 싣는다(자동 신고 없음).
GUIDANCE_119_TEXT: Final[str] = "119에 직접 전화해 주세요(자동 신고 아님)"

MessageKind = Literal["forward", "resolved", "guidance_119", "unmonitorable"]


# ── 라벨 ────────────────────────────────────────────────────────────────────
def situation_label(category: str) -> str:
    """카테고리의 한국어 상황 종류 라벨. 모르는 카테고리는 '안전 상황'."""
    return SITUATION_LABEL_KO.get(category, DEFAULT_SITUATION_LABEL)


def sensor_kind_label(category: str) -> str:
    """카테고리의 한국어 센서 종류 라벨. 모르는 카테고리는 '안전'."""
    return SENSOR_KIND_LABEL_KO.get(category, DEFAULT_SENSOR_KIND_LABEL)


def message_label(category: str, kind: MessageKind) -> str:
    """요약 알림 한 건의 상황 라벨(종류 + 메시지 성격). 기기·방·근거는 없다.

    감시 불가(unmonitorable)는 상황 종류가 아니라 센서 종류로 쓴다
    (예: '가스 센서 · 감시 불가(연결 끊김)').
    """
    if kind == "unmonitorable":
        return f"{sensor_kind_label(category)} 센서 · {UNMONITORABLE_TEXT}"
    base = situation_label(category)
    if kind == "resolved":
        return f"{base} · {RESOLVED_TEXT}"
    if kind == "guidance_119":
        return f"{base} · {GUIDANCE_119_TEXT}"
    return base


# ── 사다리 규칙(순수) ───────────────────────────────────────────────────────
def guardian_path(floor: SeverityLevel) -> bool:
    """보호자 전달·119 안내 경로를 타는가. **상황 하한**(표+거주자)만 본다.

    모델 상향은 이 값을 바꾸지 못한다 — 하한은 situations.severity_floor 에서 온다.
    """
    return floor == SeverityLevel.CRITICAL


def realert_interval(final: SeverityLevel) -> timedelta | None:
    """최종 파급도의 재알림 간격. NOTICE·RECORD 는 재알림 없음(None)."""
    if final == SeverityLevel.CRITICAL:
        return CRITICAL_REALERT_INTERVAL
    if final == SeverityLevel.WARNING:
        return WARNING_REALERT_INTERVAL
    return None


def realert_cap(final: SeverityLevel) -> int | None:
    """재알림 최대 횟수. CRITICAL 은 상한 없음(확인·해소까지)."""
    return WARNING_REALERT_MAX if final == SeverityLevel.WARNING else None


def needs_ladder(final: SeverityLevel, floor: SeverityLevel) -> bool:
    """이 상황에 사다리가 필요한가(재알림이 있거나 보호자 경로가 있으면)."""
    return realert_interval(final) is not None or guardian_path(floor)


def realerts_due(opened: datetime, final: SeverityLevel, now: datetime) -> int:
    """지금까지 나갔어야 할 재알림 수(상한 적용). 재시작 뒤 밀린 것은 한 번으로 따라잡는다."""
    interval = realert_interval(final)
    if interval is None or now < opened:
        return 0
    due = int((now - opened) // interval)
    cap = realert_cap(final)
    return min(due, cap) if cap is not None else due


def next_realert_at(
    opened: datetime, final: SeverityLevel, sent: int
) -> datetime | None:
    """sent 번 보낸 뒤 다음 재알림 시각. 상한에 닿았거나 재알림이 없으면 None."""
    interval = realert_interval(final)
    if interval is None:
        return None
    cap = realert_cap(final)
    if cap is not None and sent >= cap:
        return None
    return opened + interval * (sent + 1)


def forward_due_at(opened: datetime) -> datetime:
    """보호자 전달 시각 = 기준 시각 + 90초. 기준 시각은 보통 상황이 열린 시각이다(위 모듈 설명)."""
    return opened + GUARDIAN_FORWARD_AFTER


def guidance_due_at(opened: datetime, first_forward_at: datetime | None) -> datetime:
    """119 안내 시각 = 전달 시각 + 5분. 실제 전달이 늦었으면 그 전달 + 5분.

    보호자가 없어도(전달 없음) 기준 시각 + 90초 + 5분이다. 기준 시각은 보통 상황이 열린 시각이다.
    """
    due = forward_due_at(opened) + GUIDANCE_119_AFTER_FORWARD
    if first_forward_at is not None:
        due = max(due, first_forward_at + GUIDANCE_119_AFTER_FORWARD)
    return due


def next_fire_at(
    *,
    opened: datetime,
    final: SeverityLevel,
    floor: SeverityLevel,
    realerts_sent: int,
    now: datetime,
    guidance_sent: bool,
    first_forward_at: datetime | None,
    retry: bool,
) -> datetime | None:
    """사다리의 다음 발화 시각(없으면 None). 이미 지난 단계는 재시도 간격 뒤로 민다."""
    candidates: list[datetime] = []
    nxt = next_realert_at(opened, final, realerts_sent)
    if nxt is not None:
        candidates.append(nxt)
    if guardian_path(floor):
        forward = forward_due_at(opened)
        if forward > now:
            candidates.append(forward)
        if not guidance_sent:
            guidance = guidance_due_at(opened, first_forward_at)
            candidates.append(guidance if guidance > now else now + LADDER_RETRY)
    if retry:
        candidates.append(now + LADDER_RETRY)
    return min(candidates) if candidates else None


def realert_allowed(
    final: SeverityLevel, ctx: NotificationContext, category: str, entity_id: str
) -> bool:
    """재알림 게이트. CRITICAL 은 헌장 OFF 만, WARNING 은 헌장 OFF·음소거를 존중한다.

    중복(dedup)·거절·야간·예산 게이트는 보지 않는다 — 재알림은 같은 알림의 반복이라
    dedup 에 걸리는 게 당연하고, 그걸로 막히면 사다리가 조용해진다.
    """
    if not ctx.charter_enabled:
        return False
    if final >= SeverityLevel.CRITICAL:
        return True
    muted = category in ctx.muted_categories or entity_id in ctx.muted_entities
    return not muted


# ── 시각 ────────────────────────────────────────────────────────────────────
def parse_iso(text: str) -> datetime:
    """시스템이 만든 ISO 문자열(`...+00:00`)을 tz-aware datetime 으로."""
    raw = text.strip()
    if raw.endswith("Z"):
        raw = raw[:-1] + "+00:00"
    return datetime.fromisoformat(raw)


def to_iso(dt: datetime | None) -> str | None:
    """clock.now_utc 와 같은 형식(마이크로초 없음)으로 — DB 의 문자열 비교가 시각 순서와 같다."""
    return None if dt is None else dt.replace(microsecond=0).isoformat()


# ── 읽기 전용 조회(SafetyRepo 에 없는 것만) ─────────────────────────────────
@dataclass(frozen=True)
class SituationInfo:
    """사다리가 보는 상황 한 건(id 로 읽은 것)."""

    situation_id: str
    home_id: str
    category: str
    entity_id: str
    opened_at: str
    closed_at: str | None
    severity_floor: SeverityLevel


async def load_situation(db: aiosqlite.Connection, situation_id: str) -> SituationInfo | None:
    """situation_id 로 상황을 읽는다(열림·닫힘 무관). 없으면 None."""
    cur = await db.execute(
        "SELECT s.situation_id, s.home_id, s.category, s.entity_id, s.opened_at,"
        " s.closed_at, s.severity_floor FROM situations AS s WHERE s.situation_id = ?",
        (situation_id,),
    )
    try:
        row = await cur.fetchone()
    finally:
        await cur.close()
    if row is None:
        return None
    return SituationInfo(
        situation_id=str(row[0]),
        home_id=str(row[1]),
        category=str(row[2]),
        entity_id=str(row[3]),
        opened_at=str(row[4]),
        closed_at=None if row[5] is None else str(row[5]),
        severity_floor=SeverityLevel.from_str(int(row[6])),
    )


async def situation_notified(db: aiosqlite.Connection, home_id: str, situation_id: str) -> bool:
    """그 상황의 제안 중 하나라도 거주자에게 실제로 기록된(=송신 성공) 알림이 있는가.

    알림 기록(notifications)은 송신이 성공한 뒤에만 쓴다 — 송신 실패·게이트 억제는 남지 않는다.
    """
    cur = await db.execute(
        "SELECT 1 FROM notifications AS n JOIN proposals AS p ON p.proposal_id = n.proposal_id"
        " WHERE p.home_id = ? AND p.situation_id = ? LIMIT 1",
        (home_id, situation_id),
    )
    try:
        return await cur.fetchone() is not None
    finally:
        await cur.close()


async def final_severity_of(
    db: aiosqlite.Connection, home_id: str, situation_id: str
) -> SeverityLevel | None:
    """그 상황 제안들의 최대 파급도(= 모델 상향 포함 최종값). 없으면 None."""
    cur = await db.execute(
        "SELECT MAX(p.severity) FROM proposals AS p"
        " WHERE p.home_id = ? AND p.situation_id = ? AND p.severity IS NOT NULL",
        (home_id, situation_id),
    )
    try:
        row = await cur.fetchone()
    finally:
        await cur.close()
    if row is None or row[0] is None:
        return None
    return SeverityLevel.from_str(int(row[0]))


async def latest_alert_proposal_id(
    db: aiosqlite.Connection, home_id: str, situation_id: str
) -> str | None:
    """재알림에 다시 실을 제안: 그 상황에서 파급도가 가장 높은 최신 제안."""
    cur = await db.execute(
        "SELECT p.proposal_id FROM proposals AS p"
        " WHERE p.home_id = ? AND p.situation_id = ? AND p.severity IS NOT NULL"
        " ORDER BY p.severity DESC, p.created_at DESC, p.rowid DESC LIMIT 1",
        (home_id, situation_id),
    )
    try:
        row = await cur.fetchone()
    finally:
        await cur.close()
    return None if row is None else str(row[0])
