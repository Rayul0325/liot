"""Phase 3 — candidate_evaluations forward-only 마이그레이션.

버전 2 마이그레이션이 멱등하게 적용되고 테이블이 생기는지, 테넌시 화이트리스트
(_TENANT_TABLES)에 동기화됐는지를 고정한다.
"""

from __future__ import annotations

import aiosqlite

from liot.core.scoped_repo import _TENANT_TABLES
from liot.db.migrations import LATEST_VERSION, current_version, run_migrations


def test_candidate_evaluations_in_tenant_tables():
    # ScopedRepo 가 home_id 스코프를 강제하려면 화이트리스트에 있어야 한다.
    assert "candidate_evaluations" in _TENANT_TABLES


async def test_migration_creates_table_idempotently(tmp_path):
    db_path = tmp_path / "t.db"
    async with aiosqlite.connect(db_path) as db:
        v1 = await run_migrations(db)
        assert v1 == LATEST_VERSION
        assert LATEST_VERSION >= 2

        cur = await db.execute(
            "SELECT name FROM sqlite_master "
            "WHERE type='table' AND name='candidate_evaluations'"
        )
        row = await cur.fetchone()
        await cur.close()
        assert row is not None, "candidate_evaluations 테이블이 생성돼야 한다"

        # 두 번째 실행은 멱등 — 버전 그대로, 오류 없음.
        v2 = await run_migrations(db)
        assert v2 == v1
        assert await current_version(db) == LATEST_VERSION


async def test_candidate_evaluations_insert_roundtrip(tmp_path):
    db_path = tmp_path / "t.db"
    async with aiosqlite.connect(db_path) as db:
        await run_migrations(db)
        await db.execute(
            "INSERT INTO candidate_evaluations "
            "(home_id, trace_id, snapshot_hash, scored_json, selected_index, "
            " rationale, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
            ("singleton", "tr1", "h1", "[]", 0, "테스트", "2026-01-01T00:00:00+00:00"),
        )
        await db.commit()
        cur = await db.execute(
            "SELECT selected_index FROM candidate_evaluations WHERE trace_id = ?",
            ("tr1",),
        )
        row = await cur.fetchone()
        await cur.close()
        assert row is not None and row[0] == 0
