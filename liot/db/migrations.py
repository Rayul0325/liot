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


# (version, async fn(db)) 순서대로 누적 적용한다. version 은 1부터 단조 증가.
_MIGRATIONS: list[tuple[int, str, Callable[[aiosqlite.Connection], object]]] = [
    (1, "baseline schema (schema.sql)", _apply_baseline),
    (2, "candidate_evaluations table", _add_candidate_evaluations),
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
