"""안전 영속 저장소: 거주자·보호자·동의·상황·에스컬레이션·전달 기록·판단.

스키마는 마이그레이션 3(``migrations._add_safety_persistence``)이 만든다.

불변식:
  - 거주자 특성은 이 기기 DB 에만 저장된다. 이 모듈에는 외부 송신·로그 출력
    경로가 없다(logging 도 쓰지 않는다). 특성은 예/아니오와 입력자(entered_by)만
    저장하고, 생년월일·진단명 같은 원자료는 받을 자리 자체가 없다.
  - 특성은 정확히 4가지: 사람 단위 3종(PERSON_TRAITS) + 가구 플래그 lives_alone
    (집 설정, 거주자가 입력). cognitive_decline 은 entered_by='guardian' 만 받는다.
  - 거주자 삭제는 그 사람의 특성 값을 지운다(secure_delete 로 페이지도 0 으로 덮고
    WAL 을 체크포인트한다). 과거 판단 행에는 특성이 애초에 없다 — 남는 건 익명 사실뿐.
  - 동의는 (집, 보호자) 쌍 단위, 이력은 덧붙이기만(트리거가 UPDATE/DELETE 거부).
    '유효 동의' = 그 쌍의 최신 이벤트가 grant. 그 집 거주자라면 누구나 grant/revoke
    할 수 있고, 한 번의 revoke 로 전달이 멈춘다. 보호자가 cognitive_decline 을
    입력한 거주자도 언제나 revoke 할 수 있다(특성으로 철회를 막지 않는다).
  - 열린 상황 에피소드는 (집, 카테고리, 엔티티) 당 하나. 상황 하한은 올라가기만 한다.
  - 보호자 알림 토픽은 비밀이다. DB 에만 두고 repr 에도 나오지 않는다.

예외 규칙: 잘못된 값 → ValueError, 없는 대상 → LookupError,
그 집 거주자가 아닌 사람의 동의 조작 → PermissionError.
"""

from __future__ import annotations

import json
import secrets
import uuid
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Final, NamedTuple

from liot.core.clock import Clock
from liot.core.contracts import CandidateAction, Judgment
from liot.core.risk import RiskTier
from liot.core.scoped_repo import ScopedRepo
from liot.core.severity import SeverityLevel

if TYPE_CHECKING:  # pragma: no cover
    import aiosqlite

__all__ = [
    "PERSON_TRAITS",
    "ENTERED_BY",
    "FORWARD_KINDS",
    "DEFAULT_HOME_ALIAS",
    "TraitValue",
    "Resident",
    "HomeSettings",
    "Guardian",
    "ConsentEvent",
    "Situation",
    "Ladder",
    "Forward",
    "SafetyRepo",
]

# 사람 단위 특성 3종. 가구 플래그(lives_alone)는 set_lives_alone 로만 다룬다.
PERSON_TRAITS: Final[tuple[str, ...]] = (
    "age_75_plus",           # 예/아니오만(생년월일 저장 금지)
    "cannot_self_evacuate",  # 기능 질문의 답만(진단명 저장 금지)
    "cognitive_decline",     # 보호자 입력만 유효
)
ENTERED_BY: Final[tuple[str, ...]] = ("resident", "guardian")
_GUARDIAN_ONLY_TRAITS: Final[frozenset[str]] = frozenset({"cognitive_decline"})
FORWARD_KINDS: Final[tuple[str, ...]] = ("forward", "resolved", "guidance_119")
DEFAULT_HOME_ALIAS: Final[str] = "우리 집"

# update_ladder 가 받는 필드와 그 저장 변환.
_LADDER_BOOL_FIELDS: Final[frozenset[str]] = frozenset(
    {"acked", "guidance_119_sent", "cancelled", "closed"}
)
_LADDER_FIELDS: Final[frozenset[str]] = frozenset(
    {"stage", "next_fire_at", "notified_guardian_ids", "acked_by", "acked_at"}
    | _LADDER_BOOL_FIELDS
)
_LADDER_COLUMNS: Final[str] = (
    "situation_id, home_id, stage, next_fire_at, notified_guardian_ids, acked, acked_by,"
    " acked_at, guidance_119_sent, cancelled, closed, updated_at, anchor_at"
)
_GUARDIAN_COLUMNS: Final[str] = "guardian_id, home_id, alias, topic, created_at, active"
_SITUATION_COLUMNS: Final[str] = (
    "situation_id, home_id, category, entity_id, opened_at, closed_at, severity_floor"
)
_JUDGMENT_COLUMNS: Final[str] = (
    "judgment_id, home_id, trace_id, snapshot_hash, action_json, risk_tier, confidence,"
    " rationale, gate1_verdict, gate2_verdict, bundle_id, model_id, degraded, severity,"
    " severity_floor, severity_sources, situation_id, model_probs"
)


# ── 반환 모델 ────────────────────────────────────────────────────────────
@dataclass(frozen=True)
class TraitValue:
    """특성 하나의 값(예/아니오)과 입력자."""

    value: bool
    entered_by: str
    updated_at: str


@dataclass(frozen=True)
class Resident:
    """거주자 한 사람. traits 에는 입력된 사람 단위 특성만 들어 있다."""

    resident_id: str
    home_id: str
    alias: str
    created_at: str
    updated_at: str
    traits: dict[str, TraitValue] = field(default_factory=dict)


class HomeSettings(NamedTuple):
    """집 설정. 튜플로 풀면 (lives_alone, home_alias)."""

    lives_alone: bool
    home_alias: str


@dataclass(frozen=True)
class Guardian:
    """보호자. topic 은 비밀 알림 토픽 — repr(로그)에 나오지 않는다."""

    guardian_id: str
    home_id: str
    alias: str
    topic: str = field(repr=False)
    created_at: str = ""
    active: bool = True


@dataclass(frozen=True)
class ConsentEvent:
    """동의 이력 한 줄(grant | revoke)."""

    event_id: int
    home_id: str
    guardian_id: str
    event: str
    at: str
    by_resident_id: str


@dataclass(frozen=True)
class Situation:
    """상황 에피소드. closed_at 이 None 이면 열려 있다."""

    situation_id: str
    home_id: str
    category: str
    entity_id: str
    opened_at: str
    closed_at: str | None
    severity_floor: SeverityLevel


@dataclass(frozen=True)
class Ladder:
    """상황 하나의 에스컬레이션 사다리 상태(재시작 뒤 재무장용)."""

    situation_id: str
    home_id: str
    stage: int
    next_fire_at: str | None
    notified_guardian_ids: tuple[str, ...]
    acked: bool
    acked_by: str | None
    acked_at: str | None
    guidance_119_sent: bool
    cancelled: bool
    closed: bool
    updated_at: str
    # 사다리 시각의 기준. None 이면 situations.opened_at(보통). 값이 있으면 그 시각이 기준이다 —
    # 복구가 오래 묵은 고아 상황의 첫 알림을 지금 보낼 때만 채운다(결정 0017).
    anchor_at: str | None = None


@dataclass(frozen=True)
class Forward:
    """보호자 전달 기록 한 줄. guidance_119 는 거주자 전용이라 guardian_id=None."""

    forward_id: int
    home_id: str
    situation_id: str
    guardian_id: str | None
    kind: str
    at: str


def _require_text(value: str, name: str) -> str:
    text = str(value).strip() if value is not None else ""
    if not text:
        raise ValueError(f"{name} must be a non-empty string")
    return text


def _require_bool(value: object, name: str) -> bool:
    # 정수(예: 출생연도)나 문자열이 특성 칸에 들어오지 못하게 bool 만 받는다.
    if not isinstance(value, bool):
        raise ValueError(f"{name} must be a bool (got {type(value).__name__})")
    return value


class SafetyRepo:
    """거주자·보호자·동의·상황·사다리·전달 기록·판단 저장소(aiosqlite)."""

    def __init__(self, db: aiosqlite.Connection, *, clock: Clock | None = None) -> None:
        self._db = db
        self._clock = clock or Clock()

    # ------------------------------------------------------------------
    # 내부 도우미
    # ------------------------------------------------------------------
    async def _one(self, sql: str, params: tuple[Any, ...] = ()) -> Any:
        cur = await self._db.execute(sql, params)
        try:
            return await cur.fetchone()
        finally:
            await cur.close()

    async def _all(self, sql: str, params: tuple[Any, ...] = ()) -> list[Any]:
        cur = await self._db.execute(sql, params)
        try:
            return list(await cur.fetchall())
        finally:
            await cur.close()

    async def _exec(self, sql: str, params: tuple[Any, ...] = ()) -> int:
        cur = await self._db.execute(sql, params)
        try:
            return int(cur.rowcount)
        finally:
            await cur.close()

    async def _home_of_situation(self, situation_id: str) -> str:
        row = await self._one(
            "SELECT home_id FROM situations WHERE situation_id = ?", (situation_id,)
        )
        if row is None:
            raise LookupError(f"unknown situation: {situation_id!r}")
        return str(row[0])

    async def _require_resident_of(self, home_id: str, resident_id: str) -> None:
        row = await self._one(
            "SELECT 1 FROM residents WHERE home_id = ? AND resident_id = ?",
            (home_id, resident_id),
        )
        if row is None:
            raise PermissionError("by_resident_id is not a registered resident of this home")

    async def _guardian_active(self, home_id: str, guardian_id: str) -> bool:
        """그 집 보호자인지 확인하고 활성 여부를 돌려준다(아니면 LookupError)."""
        row = await self._one(
            "SELECT active FROM guardians WHERE home_id = ? AND guardian_id = ?",
            (home_id, guardian_id),
        )
        if row is None:
            raise LookupError(f"unknown guardian for this home: {guardian_id!r}")
        return bool(row[0])

    # ------------------------------------------------------------------
    # 거주자
    # ------------------------------------------------------------------
    async def add_resident(self, home_id: str, alias: str) -> str:
        """거주자를 등록하고 id 를 돌려준다. 별칭만 받는다(실명 불필요)."""
        home = _require_text(home_id, "home_id")
        name = _require_text(alias, "alias")
        rid = str(uuid.uuid4())
        now = self._clock.now_utc()
        await self._db.execute(
            "INSERT INTO residents (resident_id, home_id, alias, created_at, updated_at)"
            " VALUES (?, ?, ?, ?, ?)",
            (rid, home, name, now, now),
        )
        await self._db.commit()
        return rid

    async def set_trait(
        self, resident_id: str, trait: str, value: bool, entered_by: str
    ) -> None:
        """사람 단위 특성 하나를 저장(갱신)한다.

        trait 은 PERSON_TRAITS 중 하나, entered_by 는 'resident' | 'guardian'.
        cognitive_decline 은 'guardian' 만 받는다. lives_alone 은 가구 플래그라
        여기서 받지 않는다(set_lives_alone 사용).
        """
        if trait not in PERSON_TRAITS:
            hint = " (use set_lives_alone)" if trait == "lives_alone" else ""
            raise ValueError(f"unknown resident trait: {trait!r}{hint}")
        if entered_by not in ENTERED_BY:
            raise ValueError(f"entered_by must be one of {ENTERED_BY} (got {entered_by!r})")
        if trait in _GUARDIAN_ONLY_TRAITS and entered_by != "guardian":
            raise ValueError(f"{trait} may only be entered by a guardian")
        flag = _require_bool(value, "value")
        row = await self._one(
            "SELECT home_id FROM residents WHERE resident_id = ?", (resident_id,)
        )
        if row is None:
            raise LookupError(f"unknown resident: {resident_id!r}")
        now = self._clock.now_utc()
        await self._db.execute(
            "INSERT INTO resident_traits"
            " (resident_id, home_id, trait, value, entered_by, updated_at)"
            " VALUES (?, ?, ?, ?, ?, ?)"
            " ON CONFLICT (resident_id, trait) DO UPDATE SET"
            " value = excluded.value, entered_by = excluded.entered_by,"
            " updated_at = excluded.updated_at",
            (resident_id, str(row[0]), trait, 1 if flag else 0, entered_by, now),
        )
        await self._db.execute(
            "UPDATE residents SET updated_at = ? WHERE resident_id = ?", (now, resident_id)
        )
        await self._db.commit()

    async def _traits_of(self, resident_id: str) -> dict[str, TraitValue]:
        rows = await self._all(
            "SELECT trait, value, entered_by, updated_at FROM resident_traits"
            " WHERE resident_id = ? ORDER BY trait",
            (resident_id,),
        )
        return {
            str(r[0]): TraitValue(value=bool(r[1]), entered_by=str(r[2]), updated_at=str(r[3]))
            for r in rows
        }

    async def get_resident(self, resident_id: str) -> Resident | None:
        row = await self._one(
            "SELECT resident_id, home_id, alias, created_at, updated_at FROM residents"
            " WHERE resident_id = ?",
            (resident_id,),
        )
        if row is None:
            return None
        return Resident(
            resident_id=str(row[0]),
            home_id=str(row[1]),
            alias=str(row[2]),
            created_at=str(row[3]),
            updated_at=str(row[4]),
            traits=await self._traits_of(str(row[0])),
        )

    async def list_residents(self, home_id: str) -> list[Resident]:
        rows = await self._all(
            "SELECT resident_id FROM residents WHERE home_id = ? ORDER BY created_at, rowid",
            (home_id,),
        )
        out: list[Resident] = []
        for r in rows:
            res = await self.get_resident(str(r[0]))
            if res is not None:
                out.append(res)
        return out

    async def delete_resident(self, resident_id: str) -> bool:
        """거주자와 그 특성 값을 지운다(purge). 지웠으면 True.

        secure_delete 를 켠 채 지워 해제된 페이지를 0 으로 덮고, 커밋 뒤 WAL 을
        체크포인트해 옛 페이지 사본이 WAL 에 남지 않게 한다. 동의 이력의
        by_resident_id(익명 id)는 append-only 라 그대로 남는다.
        """
        prev = await self._one("PRAGMA secure_delete")
        await self._db.execute("PRAGMA secure_delete = ON")
        try:
            await self._exec("DELETE FROM resident_traits WHERE resident_id = ?", (resident_id,))
            deleted = await self._exec(
                "DELETE FROM residents WHERE resident_id = ?", (resident_id,)
            )
            await self._db.commit()
        finally:
            await self._db.execute(
                f"PRAGMA secure_delete = {int(prev[0]) if prev is not None else 0}"
            )
        await self._db.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        return deleted > 0

    # ------------------------------------------------------------------
    # 집 설정
    # ------------------------------------------------------------------
    async def set_lives_alone(self, home_id: str, value: bool) -> None:
        """가구 플래그 lives_alone 을 저장한다(거주자 입력)."""
        home = _require_text(home_id, "home_id")
        flag = _require_bool(value, "lives_alone")
        await self._db.execute(
            "INSERT INTO home_settings (home_id, lives_alone, lives_alone_entered_by, updated_at)"
            " VALUES (?, ?, 'resident', ?)"
            " ON CONFLICT (home_id) DO UPDATE SET lives_alone = excluded.lives_alone,"
            " lives_alone_entered_by = 'resident', updated_at = excluded.updated_at",
            (home, 1 if flag else 0, self._clock.now_utc()),
        )
        await self._db.commit()

    async def set_home_alias(self, home_id: str, alias: str) -> None:
        home = _require_text(home_id, "home_id")
        name = _require_text(alias, "home_alias")
        await self._db.execute(
            "INSERT INTO home_settings (home_id, home_alias, updated_at) VALUES (?, ?, ?)"
            " ON CONFLICT (home_id) DO UPDATE SET home_alias = excluded.home_alias,"
            " updated_at = excluded.updated_at",
            (home, name, self._clock.now_utc()),
        )
        await self._db.commit()

    async def get_home_settings(self, home_id: str) -> HomeSettings:
        """(lives_alone, home_alias). 설정이 없으면 (False, '우리 집')."""
        row = await self._one(
            "SELECT lives_alone, home_alias FROM home_settings WHERE home_id = ?", (home_id,)
        )
        if row is None:
            return HomeSettings(lives_alone=False, home_alias=DEFAULT_HOME_ALIAS)
        return HomeSettings(lives_alone=bool(row[0]), home_alias=str(row[1]))

    async def has_vulnerable_resident(self, home_id: str) -> bool:
        """사람 단위 특성 3종 중 하나라도 참인 거주자가 있거나 lives_alone 이면 True."""
        row = await self._one(
            "SELECT EXISTS ("
            "  SELECT 1 FROM resident_traits t"
            "  JOIN residents r ON r.resident_id = t.resident_id"
            "  WHERE r.home_id = ? AND t.value = 1"
            ")",
            (home_id,),
        )
        if row is not None and bool(row[0]):
            return True
        return (await self.get_home_settings(home_id)).lives_alone

    # ------------------------------------------------------------------
    # 보호자
    # ------------------------------------------------------------------
    async def add_guardian(self, home_id: str, alias: str) -> tuple[str, str]:
        """보호자를 등록하고 (guardian_id, 비밀 토픽)을 돌려준다."""
        home = _require_text(home_id, "home_id")
        name = _require_text(alias, "alias")
        gid = str(uuid.uuid4())
        topic = secrets.token_urlsafe(32)
        await self._db.execute(
            f"INSERT INTO guardians ({_GUARDIAN_COLUMNS}) VALUES (?, ?, ?, ?, ?, 1)",
            (gid, home, name, topic, self._clock.now_utc()),
        )
        await self._db.commit()
        return gid, topic

    @staticmethod
    def _row_to_guardian(row: Any) -> Guardian:
        return Guardian(
            guardian_id=str(row[0]),
            home_id=str(row[1]),
            alias=str(row[2]),
            topic=str(row[3]),
            created_at=str(row[4]),
            active=bool(row[5]),
        )

    async def list_guardians(self, home_id: str, active_only: bool = True) -> list[Guardian]:
        where = "home_id = ? AND active = 1" if active_only else "home_id = ?"
        rows = await self._all(
            f"SELECT {_GUARDIAN_COLUMNS} FROM guardians WHERE {where}"
            " ORDER BY created_at, rowid",
            (home_id,),
        )
        return [self._row_to_guardian(r) for r in rows]

    async def deactivate_guardian(self, home_id: str, guardian_id: str) -> bool:
        """보호자를 지우지 않고 비활성화한다. 바뀌었으면 True."""
        changed = await self._exec(
            "UPDATE guardians SET active = 0 WHERE home_id = ? AND guardian_id = ? AND active = 1",
            (home_id, guardian_id),
        )
        await self._db.commit()
        return changed > 0

    # ------------------------------------------------------------------
    # 동의(집 단위, append-only)
    # ------------------------------------------------------------------
    async def _append_consent(
        self, home_id: str, guardian_id: str, event: str, by_resident_id: str, at: str | None
    ) -> None:
        await self._db.execute(
            "INSERT INTO consent_events (home_id, guardian_id, event, at, by_resident_id)"
            " VALUES (?, ?, ?, ?, ?)",
            (home_id, guardian_id, event, at or self._clock.now_utc(), by_resident_id),
        )
        await self._db.commit()

    async def grant_consent(
        self, home_id: str, guardian_id: str, by_resident_id: str, *, at: str | None = None
    ) -> None:
        """그 집 거주자 누구나 그 집 활성 보호자에게 전달 동의를 줄 수 있다."""
        await self._require_resident_of(home_id, by_resident_id)
        if not await self._guardian_active(home_id, guardian_id):
            raise ValueError("cannot grant consent to an inactive guardian")
        await self._append_consent(home_id, guardian_id, "grant", by_resident_id, at)

    async def revoke_consent(
        self, home_id: str, guardian_id: str, by_resident_id: str, *, at: str | None = None
    ) -> None:
        """그 집 거주자 누구나 철회할 수 있다(비활성 보호자·특성과 무관하게 항상)."""
        await self._require_resident_of(home_id, by_resident_id)
        await self._guardian_active(home_id, guardian_id)
        await self._append_consent(home_id, guardian_id, "revoke", by_resident_id, at)

    async def consented_guardians(self, home_id: str) -> list[Guardian]:
        """활성이면서 (집, 보호자) 쌍의 최신 동의 이벤트가 grant 인 보호자."""
        rows = await self._all(
            "SELECT g.guardian_id, g.home_id, g.alias, g.topic, g.created_at, g.active"
            " FROM guardians g"
            " WHERE g.home_id = ? AND g.active = 1 AND ("
            "   SELECT e.event FROM consent_events e"
            "   WHERE e.home_id = g.home_id AND e.guardian_id = g.guardian_id"
            "   ORDER BY e.id DESC LIMIT 1"
            " ) = 'grant'"
            " ORDER BY g.created_at, g.rowid",
            (home_id,),
        )
        return [self._row_to_guardian(r) for r in rows]

    async def consent_history(self, home_id: str) -> list[ConsentEvent]:
        rows = await self._all(
            "SELECT id, home_id, guardian_id, event, at, by_resident_id FROM consent_events"
            " WHERE home_id = ? ORDER BY id",
            (home_id,),
        )
        return [
            ConsentEvent(
                event_id=int(r[0]),
                home_id=str(r[1]),
                guardian_id=str(r[2]),
                event=str(r[3]),
                at=str(r[4]),
                by_resident_id=str(r[5]),
            )
            for r in rows
        ]

    # ------------------------------------------------------------------
    # 상황 에피소드
    # ------------------------------------------------------------------
    @staticmethod
    def _row_to_situation(row: Any) -> Situation:
        return Situation(
            situation_id=str(row[0]),
            home_id=str(row[1]),
            category=str(row[2]),
            entity_id=str(row[3]),
            opened_at=str(row[4]),
            closed_at=None if row[5] is None else str(row[5]),
            severity_floor=SeverityLevel.from_str(int(row[6])),
        )

    async def get_open_situation(
        self, home_id: str, category: str, entity_id: str
    ) -> Situation | None:
        row = await self._one(
            f"SELECT {_SITUATION_COLUMNS} FROM situations"
            " WHERE home_id = ? AND category = ? AND entity_id = ? AND closed_at IS NULL",
            (home_id, category, entity_id),
        )
        return None if row is None else self._row_to_situation(row)

    async def list_open_situations(self, home_id: str) -> list[Situation]:
        """그 집의 열린 상황 전부(시작 시 복구용). 오래된 것부터."""
        rows = await self._all(
            f"SELECT {_SITUATION_COLUMNS} FROM situations"
            " WHERE home_id = ? AND closed_at IS NULL ORDER BY opened_at, situation_id",
            (_require_text(home_id, "home_id"),),
        )
        return [self._row_to_situation(r) for r in rows]

    async def open_or_get_situation(
        self,
        home_id: str,
        category: str,
        entity_id: str,
        severity_floor: SeverityLevel | int | str,
        at: str,
    ) -> tuple[str, bool]:
        """열린 에피소드가 있으면 그 id 를, 없으면 새로 열어 그 id 를 돌려준다.

        반환: (situation_id, opened_new). 이미 열려 있으면 하한은 올라가기만 한다
        (max(기존, 새 값)) — 파급도 하한은 내리지 못한다.
        """
        home = _require_text(home_id, "home_id")
        floor = int(SeverityLevel.from_str(severity_floor))
        sid = str(uuid.uuid4())
        inserted = await self._exec(
            f"INSERT INTO situations ({_SITUATION_COLUMNS}) VALUES (?, ?, ?, ?, ?, NULL, ?)"
            " ON CONFLICT (home_id, category, entity_id) WHERE closed_at IS NULL DO NOTHING",
            (sid, home, category, entity_id, at, floor),
        )
        if inserted:
            await self._db.commit()
            return sid, True
        await self._exec(
            "UPDATE situations SET severity_floor = MAX(severity_floor, ?)"
            " WHERE home_id = ? AND category = ? AND entity_id = ? AND closed_at IS NULL",
            (floor, home, category, entity_id),
        )
        await self._db.commit()
        existing = await self.get_open_situation(home, category, entity_id)
        if existing is None:  # pragma: no cover - 단일 연결에선 도달 불가(방어)
            raise LookupError("open situation vanished during open_or_get_situation")
        return existing.situation_id, False

    async def close_situation(self, situation_id: str, at: str) -> bool:
        """열린 에피소드를 닫는다. 닫았으면 True(이미 닫혔거나 없으면 False)."""
        changed = await self._exec(
            "UPDATE situations SET closed_at = ? WHERE situation_id = ? AND closed_at IS NULL",
            (at, situation_id),
        )
        await self._db.commit()
        return changed > 0

    # ------------------------------------------------------------------
    # 에스컬레이션 사다리
    # ------------------------------------------------------------------
    @staticmethod
    def _row_to_ladder(row: Any) -> Ladder:
        ids = json.loads(row[4]) if row[4] else []
        return Ladder(
            situation_id=str(row[0]),
            home_id=str(row[1]),
            stage=int(row[2]),
            next_fire_at=None if row[3] is None else str(row[3]),
            notified_guardian_ids=tuple(str(i) for i in ids),
            acked=bool(row[5]),
            acked_by=None if row[6] is None else str(row[6]),
            acked_at=None if row[7] is None else str(row[7]),
            guidance_119_sent=bool(row[8]),
            cancelled=bool(row[9]),
            closed=bool(row[10]),
            updated_at=str(row[11]),
            anchor_at=None if row[12] is None else str(row[12]),
        )

    async def create_ladder(
        self,
        situation_id: str,
        stage: int,
        next_fire_at: str | None,
        anchor_at: str | None = None,
    ) -> None:
        """상황 하나에 사다리 하나를 만든다(이미 있으면 ValueError).

        anchor_at 은 사다리 시각의 기준을 열린 시각이 아닌 다른 시각으로 옮길 때만 준다(None =
        열린 시각 기준). 만든 뒤에는 바꾸지 않는다(update_ladder 가 받지 않는다).
        """
        home = await self._home_of_situation(situation_id)
        existing = await self._one(
            "SELECT 1 FROM escalation_ladders WHERE situation_id = ?", (situation_id,)
        )
        if existing is not None:
            raise ValueError(f"ladder already exists for situation {situation_id!r}")
        await self._db.execute(
            "INSERT INTO escalation_ladders"
            " (situation_id, home_id, stage, next_fire_at, updated_at, anchor_at)"
            " VALUES (?, ?, ?, ?, ?, ?)",
            (situation_id, home, int(stage), next_fire_at, self._clock.now_utc(), anchor_at),
        )
        await self._db.commit()

    async def update_ladder(self, situation_id: str, **fields: Any) -> None:
        """사다리 필드를 갱신한다. 허용 필드 밖이면 ValueError, 없는 사다리면 LookupError.

        허용: stage, next_fire_at, notified_guardian_ids(문자열 목록), acked, acked_by,
        acked_at, guidance_119_sent, cancelled, closed. updated_at 은 자동 갱신.
        """
        unknown = set(fields) - _LADDER_FIELDS
        if unknown:
            raise ValueError(f"unknown ladder field(s): {sorted(unknown)}")
        sets: list[str] = []
        params: list[Any] = []
        for name in sorted(fields):
            value = fields[name]
            if name in _LADDER_BOOL_FIELDS:
                value = 1 if _require_bool(value, name) else 0
            elif name == "notified_guardian_ids":
                if isinstance(value, str) or not isinstance(value, Sequence):
                    raise ValueError("notified_guardian_ids must be a sequence of ids")
                value = json.dumps([str(v) for v in value])
            elif name == "stage":
                value = int(value)
            sets.append(f"{name} = ?")  # name 은 _LADDER_FIELDS 화이트리스트
            params.append(value)
        sets.append("updated_at = ?")
        params.append(self._clock.now_utc())
        changed = await self._exec(
            f"UPDATE escalation_ladders SET {', '.join(sets)} WHERE situation_id = ?",
            (*params, situation_id),
        )
        if not changed:
            raise LookupError(f"unknown ladder: {situation_id!r}")
        await self._db.commit()

    async def get_ladder(self, situation_id: str) -> Ladder | None:
        row = await self._one(
            f"SELECT {_LADDER_COLUMNS} FROM escalation_ladders WHERE situation_id = ?",
            (situation_id,),
        )
        return None if row is None else self._row_to_ladder(row)

    async def list_open_ladders(self) -> list[Ladder]:
        """재시작 때 다시 무장할 사다리: 확인(ack)·종료·취소되지 않은 것(모든 집)."""
        rows = await self._all(
            f"SELECT {_LADDER_COLUMNS} FROM escalation_ladders"
            " WHERE acked = 0 AND closed = 0 AND cancelled = 0"
            " ORDER BY next_fire_at IS NULL, next_fire_at, situation_id"
        )
        return [self._row_to_ladder(r) for r in rows]

    # ------------------------------------------------------------------
    # 보호자 전달 기록
    # ------------------------------------------------------------------
    async def record_forward(
        self, situation_id: str, guardian_id: str | None, kind: str, at: str
    ) -> None:
        """전달 기록을 남긴다. guidance_119 는 guardian_id=None, 나머지는 보호자 필수."""
        if kind not in FORWARD_KINDS:
            raise ValueError(f"kind must be one of {FORWARD_KINDS} (got {kind!r})")
        if (kind == "guidance_119") != (guardian_id is None):
            raise ValueError("guidance_119 has no guardian; forward/resolved need one")
        home = await self._home_of_situation(situation_id)
        if guardian_id is not None:
            await self._guardian_active(home, guardian_id)
        await self._db.execute(
            "INSERT INTO guardian_forwards (home_id, situation_id, guardian_id, kind, at)"
            " VALUES (?, ?, ?, ?, ?)",
            (home, situation_id, guardian_id, kind, at),
        )
        await self._db.commit()

    async def list_forwards(self, home_id: str) -> list[Forward]:
        rows = await self._all(
            "SELECT id, home_id, situation_id, guardian_id, kind, at FROM guardian_forwards"
            " WHERE home_id = ? ORDER BY id",
            (home_id,),
        )
        return [
            Forward(
                forward_id=int(r[0]),
                home_id=str(r[1]),
                situation_id=str(r[2]),
                guardian_id=None if r[3] is None else str(r[3]),
                kind=str(r[4]),
                at=str(r[5]),
            )
            for r in rows
        ]

    # ------------------------------------------------------------------
    # 판단 저장
    # ------------------------------------------------------------------
    async def save_judgment(self, judgment: Judgment) -> None:
        """판단 한 건을 judgments 에 한 행으로 남긴다(목록·사전은 JSON).

        거주자 특성은 Judgment 계약에 없으므로 이 행에도 없다.
        """
        await ScopedRepo(judgment.home_id).insert(
            self._db,
            "judgments",
            {
                "judgment_id": judgment.judgment_id,
                "trace_id": judgment.trace_id,
                "snapshot_hash": judgment.snapshot_hash,
                "action_json": judgment.action.model_dump_json(),
                "risk_tier": int(judgment.risk_tier),
                "confidence": judgment.confidence,
                "rationale": judgment.rationale,
                "gate1_verdict": judgment.gate1_verdict,
                "gate2_verdict": judgment.gate2_verdict,
                "bundle_id": judgment.bundle_id,
                "model_id": judgment.model_id,
                "degraded": 1 if judgment.degraded else 0,
                "created_at": self._clock.now_utc(),
                "severity": None if judgment.severity is None else int(judgment.severity),
                "severity_floor": (
                    None if judgment.severity_floor is None else int(judgment.severity_floor)
                ),
                "severity_sources": json.dumps(list(judgment.severity_sources)),
                "situation_id": judgment.situation_id,
                "model_probs": (
                    None if judgment.model_probs is None else json.dumps(judgment.model_probs)
                ),
            },
        )
        await self._db.commit()

    async def get_judgment(self, home_id: str, judgment_id: str) -> Judgment | None:
        """저장된 판단을 다시 Judgment 로 읽는다(home_id 정확 일치)."""
        rows = await ScopedRepo(home_id).fetchall(
            self._db,
            "judgments",
            columns=_JUDGMENT_COLUMNS,
            extra_where="judgment_id = ?",
            extra_params=(judgment_id,),
            limit=1,
        )
        if not rows:
            return None
        r = rows[0]
        return Judgment(
            judgment_id=str(r[0]),
            home_id=str(r[1]),
            trace_id=str(r[2]),
            snapshot_hash=str(r[3]),
            action=CandidateAction.model_validate_json(r[4]),
            risk_tier=RiskTier.from_str(int(r[5])),
            confidence=float(r[6]),
            rationale=str(r[7]),
            gate1_verdict=str(r[8]),
            gate2_verdict=None if r[9] is None else str(r[9]),
            bundle_id=str(r[10]),
            model_id=str(r[11]),
            degraded=bool(r[12]),
            severity=None if r[13] is None else SeverityLevel.from_str(int(r[13])),
            severity_floor=None if r[14] is None else SeverityLevel.from_str(int(r[14])),
            severity_sources=json.loads(r[15]) if r[15] else [],
            situation_id=None if r[16] is None else str(r[16]),
            model_probs=None if r[17] is None else json.loads(r[17]),
        )
