"""aiosqlite 연결 헬퍼.

단일 프로세스 asyncio 전제. WAL 저널 모드를 쓰고, durable 여부에 따라
synchronous 수준을 조정한다.

  - durable=True  -> PRAGMA synchronous=FULL   (내구성 우선; 안전 임계 경로)
  - durable=False -> PRAGMA synchronous=NORMAL  (성능 우선; 일반 경로)
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import aiosqlite

from .migrations import run_migrations

if TYPE_CHECKING:  # pragma: no cover
    pass

__all__ = ["connect", "init_db"]


async def connect(path: str, durable: bool = False) -> aiosqlite.Connection:
    """주어진 경로로 aiosqlite 연결을 열고 PRAGMA 를 설정한다.

    journal_mode=WAL, foreign_keys=ON 을 항상 적용하고,
    synchronous 는 durable 에 따라 FULL/NORMAL 로 둔다.

    호출자가 연결 수명을 소유한다(close 책임). row_factory 는 aiosqlite.Row.
    """
    db = await aiosqlite.connect(path)
    db.row_factory = aiosqlite.Row
    await db.execute("PRAGMA journal_mode=WAL")
    await db.execute("PRAGMA foreign_keys=ON")
    await db.execute(
        "PRAGMA synchronous=FULL" if durable else "PRAGMA synchronous=NORMAL"
    )
    await db.commit()
    return db


async def init_db(path: str, durable: bool = False) -> aiosqlite.Connection:
    """연결을 열고 forward-only 마이그레이션을 끝까지 적용한 뒤 반환한다.

    반환된 연결은 최신 스키마 버전이 적용된 상태다. 호출자가 close 한다.
    """
    db = await connect(path, durable=durable)
    await run_migrations(db)
    return db
