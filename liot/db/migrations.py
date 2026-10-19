"""Forward-only 마이그레이션.

마이그레이션은 오직 앞으로만 간다(롤백/다운그레이드 없음). 각 단계는
멱등하게 적용 가능해야 하며, schema_version 테이블에 진행 상태를 남긴다.

단계 1 = schema.sql 의 기준 스키마. 이후 변경은 _MIGRATIONS 에 (version, sql)
를 추가만 한다(기존 컬럼 삭제/이름변경 금지).
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:  # pragma: no cover
    import aiosqlite

__all__ = ["run_migrations", "current_version", "LATEST_VERSION", "SCHEMA_SQL_PATH"]

SCHEMA_SQL_PATH = Path(__file__).with_name("schema.sql")


def _now() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat()


async def _ensure_version_table(db: aiosqlite.Connection) -> None:
    await db.execute(
        """
        CREATE TABLE IF NOT EXISTS schema_version (
            version    INTEGER NOT NULL,
            applied_at TEXT    NOT NULL,
            note       TEXT
        )
        """
    )
    await db.commit()


async def current_version(db: aiosqlite.Connection) -> int:
    """현재 적용된 최대 스키마 버전(없으면 0)."""
    await _ensure_version_table(db)
    cur = await db.execute("SELECT MAX(version) FROM schema_version")
    row = await cur.fetchone()
    await cur.close()
    if row is None or row[0] is None:
        return 0
    return int(row[0])


async def _apply_baseline(db: aiosqlite.Connection) -> None:
    """schema.sql 의 기준 스키마(버전 1)를 적용한다."""
    sql = SCHEMA_SQL_PATH.read_text(encoding="utf-8")
    await db.executescript(sql)


async def _add_candidate_evaluations(db: aiosqlite.Connection) -> None:
    """버전 2: 후보 평가 결과 보관(감사/데모/연구 로그).

    L5 평가기의 CandidateSet/EvaluationResult 를 trace 단위로 남긴다 — 어떤
    후보들이 생성·점수화됐고 무엇이 선택됐는지(무행동 포함). 멱등(CREATE IF NOT
    EXISTS). 컬럼 삭제/이름변경 금지 규칙은 schema.sql 과 동일하게 적용된다.
    """
    await db.executescript(
        """
        CREATE TABLE IF NOT EXISTS candidate_evaluations (
            home_id        TEXT    NOT NULL DEFAULT 'singleton',
            trace_id       TEXT    NOT NULL,
            snapshot_hash  TEXT    NOT NULL,   -- 후보집합 재현 키
            scored_json    TEXT    NOT NULL,   -- list[ActionCandidate] 직렬화
            selected_index INTEGER NOT NULL,   -- 선택된 후보(무행동 가능)
            rationale      TEXT    NOT NULL,
            created_at     TEXT    NOT NULL,
            PRIMARY KEY (home_id, trace_id)
        );
        CREATE INDEX IF NOT EXISTS idx_candidate_eval_snapshot
            ON candidate_evaluations (home_id, snapshot_hash);
        """
    )


async def _column_names(db: aiosqlite.Connection, table: str) -> set[str]:
    """테이블의 현재 컬럼 이름 집합(row_factory 와 무관하게 위치 1 = name)."""
    cur = await db.execute(f"PRAGMA table_info({table})")
    rows = await cur.fetchall()
    await cur.close()
    return {str(r[1]) for r in rows}


async def _add_columns_if_missing(
    db: aiosqlite.Connection, table: str, columns: tuple[tuple[str, str], ...]
) -> None:
    """없는 컬럼만 ALTER TABLE ADD COLUMN 으로 덧붙인다(멱등, 기존 행은 NULL)."""
    existing = await _column_names(db, table)
    for name, decl in columns:
        if name not in existing:
            await db.execute(f"ALTER TABLE {table} ADD COLUMN {name} {decl}")


# 판단/제안에 덧붙이는 파급도·상황 컬럼. 전부 NULL 허용(기존 행은 NULL 유지).
_JUDGMENT_V3_COLUMNS: tuple[tuple[str, str], ...] = (
    ("severity", "INTEGER"),           # SeverityLevel 정수값(0..3)
    ("severity_floor", "INTEGER"),     # 표+거주자 하한
    ("severity_sources", "TEXT"),      # JSON list[SeveritySource]
    ("situation_id", "TEXT"),
    ("model_probs", "TEXT"),           # JSON dict[str, dict[str, float]]
)
_PROPOSAL_V3_COLUMNS: tuple[tuple[str, str], ...] = (
    ("severity", "INTEGER"),
    ("situation_id", "TEXT"),
)


async def _add_safety_persistence(db: aiosqlite.Connection) -> None:
    """버전 3: 거주자·보호자·동의·상황·에스컬레이션·전달 기록 + 판단/제안 새 컬럼.

    - 거주자 특성은 이 기기 DB 에만 둔다(외부 송신 경로 없음). 생년월일·진단명
      같은 원자료 컬럼은 만들지 않는다 — 예/아니오와 입력자만.
    - 동의 기록(consent_events)은 덧붙이기만 된다: UPDATE/DELETE 는 트리거가 거부.
    - 열린 상황 에피소드는 (home, category, entity) 당 하나 — 부분 유일 인덱스.
    - 멱등: CREATE ... IF NOT EXISTS, 컬럼은 없을 때만 추가.
    """
    await db.executescript(
        """
        CREATE TABLE IF NOT EXISTS residents (
            resident_id TEXT NOT NULL PRIMARY KEY,
            home_id     TEXT NOT NULL DEFAULT 'singleton' CHECK (TRIM(home_id) <> ''),
            alias       TEXT NOT NULL,          -- 표시용 별칭(실명 불필요)
            created_at  TEXT NOT NULL,
            updated_at  TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_residents_home ON residents (home_id);

        -- 사람 단위 특성 3종(예/아니오 + 입력자). 가구 플래그 lives_alone 은 home_settings.
        CREATE TABLE IF NOT EXISTS resident_traits (
            resident_id TEXT    NOT NULL,
            home_id     TEXT    NOT NULL DEFAULT 'singleton' CHECK (TRIM(home_id) <> ''),
            trait       TEXT    NOT NULL CHECK (trait IN
                            ('age_75_plus', 'cannot_self_evacuate', 'cognitive_decline')),
            value       INTEGER NOT NULL CHECK (value IN (0, 1)),
            entered_by  TEXT    NOT NULL CHECK (entered_by IN ('resident', 'guardian')),
            updated_at  TEXT    NOT NULL,
            PRIMARY KEY (resident_id, trait),
            CHECK (trait <> 'cognitive_decline' OR entered_by = 'guardian')
        );

        CREATE TABLE IF NOT EXISTS home_settings (
            home_id                TEXT    NOT NULL PRIMARY KEY CHECK (TRIM(home_id) <> ''),
            lives_alone            INTEGER NOT NULL DEFAULT 0 CHECK (lives_alone IN (0, 1)),
            lives_alone_entered_by TEXT    NOT NULL DEFAULT 'resident'
                                   CHECK (lives_alone_entered_by = 'resident'),
            home_alias             TEXT    NOT NULL DEFAULT '우리 집',
            updated_at             TEXT    NOT NULL
        );

        -- 보호자. topic 은 추측 불가 비밀 알림 토픽(DB 에만, 로그 금지).
        CREATE TABLE IF NOT EXISTS guardians (
            guardian_id TEXT    NOT NULL PRIMARY KEY,
            home_id     TEXT    NOT NULL DEFAULT 'singleton' CHECK (TRIM(home_id) <> ''),
            alias       TEXT    NOT NULL,
            topic       TEXT    NOT NULL UNIQUE,
            created_at  TEXT    NOT NULL,
            active      INTEGER NOT NULL DEFAULT 1 CHECK (active IN (0, 1))
        );
        CREATE INDEX IF NOT EXISTS idx_guardians_home ON guardians (home_id, active);

        -- 집 단위 동의 이력(append-only). 최신 이벤트가 grant 면 '유효 동의'.
        CREATE TABLE IF NOT EXISTS consent_events (
            id             INTEGER PRIMARY KEY AUTOINCREMENT,
            home_id        TEXT NOT NULL DEFAULT 'singleton' CHECK (TRIM(home_id) <> ''),
            guardian_id    TEXT NOT NULL,
            event          TEXT NOT NULL CHECK (event IN ('grant', 'revoke')),
            at             TEXT NOT NULL,
            by_resident_id TEXT NOT NULL      -- 익명 id(거주자 삭제 뒤에도 남는다)
        );
        CREATE INDEX IF NOT EXISTS idx_consent_pair
            ON consent_events (home_id, guardian_id, id);
        CREATE TRIGGER IF NOT EXISTS trg_consent_events_no_update
            BEFORE UPDATE ON consent_events
            BEGIN SELECT RAISE(ABORT, 'consent_events is append-only'); END;
        CREATE TRIGGER IF NOT EXISTS trg_consent_events_no_delete
            BEFORE DELETE ON consent_events
            BEGIN SELECT RAISE(ABORT, 'consent_events is append-only'); END;

        CREATE TABLE IF NOT EXISTS situations (
            situation_id   TEXT    NOT NULL PRIMARY KEY,
            home_id        TEXT    NOT NULL DEFAULT 'singleton' CHECK (TRIM(home_id) <> ''),
            category       TEXT    NOT NULL,
            entity_id      TEXT    NOT NULL,
            opened_at      TEXT    NOT NULL,
            closed_at      TEXT,                -- 열려 있는 동안 NULL
            severity_floor INTEGER NOT NULL
        );
        CREATE UNIQUE INDEX IF NOT EXISTS ux_situations_one_open
            ON situations (home_id, category, entity_id) WHERE closed_at IS NULL;

        -- 상황당 하나. 데몬 재시작 뒤 다시 무장(re-arm)할 수 있게 영속한다.
        CREATE TABLE IF NOT EXISTS escalation_ladders (
            situation_id          TEXT    NOT NULL PRIMARY KEY,
            home_id               TEXT    NOT NULL DEFAULT 'singleton'
                                  CHECK (TRIM(home_id) <> ''),
            stage                 INTEGER NOT NULL,
            next_fire_at          TEXT,
            notified_guardian_ids TEXT    NOT NULL DEFAULT '[]',  -- JSON list[str]
            acked                 INTEGER NOT NULL DEFAULT 0 CHECK (acked IN (0, 1)),
            acked_by              TEXT,
            acked_at              TEXT,
            guidance_119_sent     INTEGER NOT NULL DEFAULT 0 CHECK (guidance_119_sent IN (0, 1)),
            cancelled             INTEGER NOT NULL DEFAULT 0 CHECK (cancelled IN (0, 1)),
            closed                INTEGER NOT NULL DEFAULT 0 CHECK (closed IN (0, 1)),
            updated_at            TEXT    NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_ladders_open
            ON escalation_ladders (acked, closed, cancelled, next_fire_at);

        -- 보호자 전달 기록. 119 안내는 거주자 전용이라 guardian_id 가 NULL.
        CREATE TABLE IF NOT EXISTS guardian_forwards (
            id           INTEGER PRIMARY KEY AUTOINCREMENT,
            home_id      TEXT NOT NULL DEFAULT 'singleton' CHECK (TRIM(home_id) <> ''),
            situation_id TEXT NOT NULL,
            guardian_id  TEXT,
            kind         TEXT NOT NULL CHECK (kind IN ('forward', 'resolved', 'guidance_119')),
            at           TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_forwards_home ON guardian_forwards (home_id, id);
        """
    )
    await _add_columns_if_missing(db, "judgments", _JUDGMENT_V3_COLUMNS)
    await _add_columns_if_missing(db, "proposals", _PROPOSAL_V3_COLUMNS)


# (version, async fn(db)) 순서대로 누적 적용한다. version 은 1부터 단조 증가.
_MIGRATIONS: list[tuple[int, str, Callable[[aiosqlite.Connection], object]]] = [
    (1, "baseline schema (schema.sql)", _apply_baseline),
    (2, "candidate_evaluations table", _add_candidate_evaluations),
    (3, "residents/guardians/consent/situations/ladders + severity columns",
     _add_safety_persistence),
]

LATEST_VERSION = max((v for v, _, _ in _MIGRATIONS), default=0)


async def run_migrations(db: aiosqlite.Connection) -> int:
    """현재 버전보다 높은 마이그레이션을 순서대로 적용한다.

    반환: 적용 완료 후의 최신 버전. 이미 최신이면 그대로 반환(멱등).
    """
    await _ensure_version_table(db)
    version = await current_version(db)
    for target, note, fn in sorted(_MIGRATIONS, key=lambda m: m[0]):
        if target <= version:
            continue
        result = fn(db)
        # 마이그레이션 함수는 코루틴을 반환한다(async).
        if hasattr(result, "__await__"):
            await result
        await db.execute(
            "INSERT INTO schema_version (version, applied_at, note) VALUES (?, ?, ?)",
            (target, _now(), note),
        )
        await db.commit()
        version = target
    return version
