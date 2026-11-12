"""마이그레이션 3 — 거주자·보호자·동의·상황·에스컬레이션·전달 기록 + 판단/제안 새 컬럼.

고정하는 것:
  - 빈 DB → 3 까지 한 번에 올라간다.
  - 2 에서 멈춘 DB 에 3 을 두 번 적용해도 오류 없이 같은 결과(멱등).
  - 기존 판단/제안 행은 새 컬럼이 NULL 로 남는다.
  - 버전은 단조 증가한다(schema_version 에 3 이 한 번만).
"""

from __future__ import annotations

import aiosqlite

from liot.db import migrations
from liot.db.migrations import LATEST_VERSION, current_version, run_migrations

_NEW_TABLES = (
    "residents",
    "resident_traits",
    "home_settings",
    "guardians",
    "consent_events",
    "situations",
    "escalation_ladders",
    "guardian_forwards",
)


async def _tables(db: aiosqlite.Connection) -> set[str]:
    cur = await db.execute("SELECT name FROM sqlite_master WHERE type='table'")
    rows = await cur.fetchall()
    await cur.close()
    return {str(r[0]) for r in rows}


async def _columns(db: aiosqlite.Connection, table: str) -> set[str]:
    cur = await db.execute(f"PRAGMA table_info({table})")
    rows = await cur.fetchall()
    await cur.close()
    return {str(r[1]) for r in rows}


async def _migrate_to_v2(db: aiosqlite.Connection) -> None:
    """버전 2 까지만 적용된 기존 설치를 흉내 낸다."""
    for version, note, fn in migrations._MIGRATIONS:
        if version > 2:
            continue
        result = fn(db)
        if hasattr(result, "__await__"):
            await result
        await db.execute(
            "INSERT INTO schema_version (version, applied_at, note) VALUES (?, ?, ?)",
            (version, "2026-01-01T00:00:00+00:00", note),
        )
        await db.commit()


def test_latest_version_is_four() -> None:
    # v4(결정 0017)가 escalation_ladders.anchor_at 을 더했다 — 의도된 고정값 수정.
    assert LATEST_VERSION == 4


async def test_empty_to_latest_creates_tables_and_columns(tmp_path) -> None:
    async with aiosqlite.connect(tmp_path / "e.db") as db:
        assert await run_migrations(db) == 4
        assert set(_NEW_TABLES) <= await _tables(db)
        assert {
            "severity",
            "severity_floor",
            "severity_sources",
            "situation_id",
            "model_probs",
        } <= await _columns(db, "judgments")
        assert {"severity", "situation_id"} <= await _columns(db, "proposals")


async def test_v2_to_v3_twice_is_idempotent_and_keeps_old_rows_null(tmp_path) -> None:
    async with aiosqlite.connect(tmp_path / "v2.db") as db:
        await migrations._ensure_version_table(db)
        await _migrate_to_v2(db)
        assert await current_version(db) == 2
        await db.execute(
            "INSERT INTO proposals (proposal_id, home_id, judgment_id, risk_tier, action_json,"
            " rationale, required_confirmation, created_at) VALUES (?,?,?,?,?,?,?,?)",
            ("p-old", "singleton", "j-old", 1, "{}", "r", "light", "2026-01-01T00:00:00+00:00"),
        )
        await db.execute(
            "INSERT INTO judgments (judgment_id, home_id, trace_id, snapshot_hash, action_json,"
            " risk_tier, confidence, rationale, gate1_verdict, bundle_id, model_id, created_at)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            ("j-old", "singleton", "t", "h", "{}", 1, 0.5, "r", "ok", "b", "m", "2026-01-01"),
        )
        await db.commit()

        assert await run_migrations(db) == 4
        # 같은 마이그레이션 함수를 한 번 더 직접 돌려도 오류 없음(멱등).
        await migrations._add_safety_persistence(db)
        await db.commit()
        assert await run_migrations(db) == 4

        cur = await db.execute("SELECT COUNT(*) FROM schema_version WHERE version = 3")
        row = await cur.fetchone()
        await cur.close()
        assert row is not None and row[0] == 1

        cur = await db.execute(
            "SELECT severity, situation_id FROM proposals WHERE proposal_id = 'p-old'"
        )
        prow = await cur.fetchone()
        await cur.close()
        assert prow is not None and tuple(prow) == (None, None)

        cur = await db.execute(
            "SELECT severity, severity_floor, severity_sources, situation_id, model_probs"
            " FROM judgments WHERE judgment_id = 'j-old'"
        )
        jrow = await cur.fetchone()
        await cur.close()
        assert jrow is not None and tuple(jrow) == (None, None, None, None, None)


async def test_versions_are_monotonic(tmp_path) -> None:
    versions = [v for v, _, _ in migrations._MIGRATIONS]
    assert versions == sorted(versions)
    assert len(versions) == len(set(versions))
    async with aiosqlite.connect(tmp_path / "m.db") as db:
        await run_migrations(db)
        cur = await db.execute("SELECT version FROM schema_version ORDER BY rowid")
        rows = [int(r[0]) for r in await cur.fetchall()]
        await cur.close()
        assert rows == sorted(rows) == [1, 2, 3, 4]
