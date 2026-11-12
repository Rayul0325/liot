"""마이그레이션 v4: escalation_ladders.anchor_at (nullable, 결정 0017).

고정하는 것:
  - 새 DB 에 anchor_at 컬럼이 있다.
  - 버전 3 까지만 적용된 기존 설치의 사다리 행은 올린 뒤 anchor_at 이 NULL 이다(= 열린 시각 기준,
    지금까지와 똑같이 동작).
  - 다시 돌려도(함수 직접 호출 포함) 오류 없이 같은 결과, schema_version 에 4 가 한 번만.
  - SafetyRepo 는 anchor_at 을 만들 때 저장하고 읽으며, 주지 않으면 None 이다.
"""

from __future__ import annotations

import aiosqlite

from liot.core.clock import Clock
from liot.db import migrations
from liot.db.migrations import LATEST_VERSION, current_version, run_migrations
from liot.db.safety_repo import SafetyRepo

T0 = "2026-06-02T12:00:00+00:00"


async def _columns(db: aiosqlite.Connection, table: str) -> list[str]:
    cur = await db.execute(f"PRAGMA table_info({table})")
    rows = await cur.fetchall()
    await cur.close()
    return [str(r[1]) for r in rows]


async def _migrate_to_v3(db: aiosqlite.Connection) -> None:
    """버전 3 까지만 적용된 기존 설치를 흉내 낸다."""
    await migrations._ensure_version_table(db)
    for version, note, fn in migrations._MIGRATIONS:
        if version > 3:
            continue
        result = fn(db)
        if hasattr(result, "__await__"):
            await result
        await db.execute(
            "INSERT INTO schema_version (version, applied_at, note) VALUES (?, ?, ?)",
            (version, "2026-01-01T00:00:00+00:00", note),
        )
        await db.commit()


async def test_new_db_has_the_anchor_column(tmp_path) -> None:
    async with aiosqlite.connect(tmp_path / "n.db") as db:
        assert await run_migrations(db) == LATEST_VERSION
        assert "anchor_at" in await _columns(db, "escalation_ladders")


async def test_v3_ladder_rows_get_null_anchor_and_rerun_is_idempotent(tmp_path) -> None:
    async with aiosqlite.connect(tmp_path / "v3.db") as db:
        await _migrate_to_v3(db)
        assert "anchor_at" not in await _columns(db, "escalation_ladders")
        await db.execute(
            "INSERT INTO situations (situation_id, home_id, category, entity_id, opened_at,"
            " severity_floor) VALUES ('s-old', 'singleton', 'gas', 'e', ?, 3)",
            (T0,),
        )
        await db.execute(
            "INSERT INTO escalation_ladders (situation_id, home_id, stage, next_fire_at,"
            " updated_at) VALUES ('s-old', 'singleton', 0, ?, ?)",
            (T0, T0),
        )
        await db.commit()

        assert await run_migrations(db) == 4
        # 같은 마이그레이션 함수를 한 번 더 직접 돌려도 오류 없음(멱등).
        await migrations._add_ladder_anchor(db)
        await db.commit()
        assert await run_migrations(db) == 4
        assert await current_version(db) == 4

        cols = await _columns(db, "escalation_ladders")
        assert cols.count("anchor_at") == 1
        cur = await db.execute("SELECT COUNT(*) FROM schema_version WHERE version = 4")
        row = await cur.fetchone()
        await cur.close()
        assert row is not None and row[0] == 1

        cur = await db.execute(
            "SELECT anchor_at FROM escalation_ladders WHERE situation_id = 's-old'"
        )
        old = await cur.fetchone()
        await cur.close()
        assert old is not None and old[0] is None

        # 올린 뒤 SafetyRepo 가 옛 행을 anchor_at=None 으로 읽는다.
        repo = SafetyRepo(db, clock=Clock(fixed=T0))
        ladder = await repo.get_ladder("s-old")
        assert ladder is not None and ladder.anchor_at is None


async def test_repo_stores_and_reads_the_anchor(tmp_path) -> None:
    async with aiosqlite.connect(tmp_path / "r.db") as db:
        await run_migrations(db)
        repo = SafetyRepo(db, clock=Clock(fixed=T0))
        sid1, _ = await repo.open_or_get_situation("singleton", "gas", "e1", 3, T0)
        sid2, _ = await repo.open_or_get_situation("singleton", "gas", "e2", 3, T0)
        await repo.create_ladder(sid1, 0, "2026-06-02T12:01:00+00:00")
        await repo.create_ladder(sid2, 0, "2026-06-02T12:01:00+00:00", T0)
        one = await repo.get_ladder(sid1)
        two = await repo.get_ladder(sid2)
        assert one is not None and one.anchor_at is None
        assert two is not None and two.anchor_at == T0
        listed = {x.situation_id: x.anchor_at for x in await repo.list_open_ladders()}
        assert listed == {sid1: None, sid2: T0}
