"""테넌시 격리 저장소.

불변식: home_id 는 항상 NOT NULL 이며 기본값은 'singleton'.
ScopedRepo 는 생성 시 home_id 가 None/공백이면 IsolationError 를 던지고,
모든 쿼리에 ``home_id = ?`` 정확 일치 조건을 강제한다.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from .invariants import IsolationError

if TYPE_CHECKING:  # pragma: no cover
    import aiosqlite

__all__ = ["ScopedRepo", "assert_no_null_tenants", "DEFAULT_HOME_ID"]

DEFAULT_HOME_ID = "singleton"

# 테넌시 컬럼(home_id)을 가진 것으로 간주하는 테이블들.
_TENANT_TABLES: tuple[str, ...] = (
    "preferences",
    "sensor_readings",
    "context_snapshots",
    "judgments",
    "proposals",
    "feedback",
    "notifications",
    "audit_log",
    "trust_state",
)


class ScopedRepo:
    """home_id 로 고정 스코프된 저장소 핸들.

    한 인스턴스는 정확히 하나의 home_id 만 다룬다. 모든 쿼리 빌더는 그
    home_id 에 대한 정확 일치 조건을 자동으로 끼워 넣는다.
    """

    def __init__(self, home_id: str | None) -> None:
        if home_id is None or not str(home_id).strip():
            raise IsolationError("home_id must be a non-empty string (got %r)" % (home_id,))
        self._home_id = str(home_id)

    @property
    def home_id(self) -> str:
        return self._home_id

    def scope_where(self, extra: str | None = None) -> str:
        """home_id 정확 일치를 포함한 WHERE 절을 만든다.

        반환 예: ``WHERE home_id = ?`` 또는 ``WHERE home_id = ? AND (<extra>)``.
        바인딩 파라미터의 첫 값은 항상 home_id 여야 한다(scope_params 참고).
        """
        base = "WHERE home_id = ?"
        if extra:
            return f"{base} AND ({extra})"
        return base

    def scope_params(self, *extra: Any) -> tuple[Any, ...]:
        """스코프된 바인딩 파라미터. 첫 값은 항상 home_id."""
        return (self._home_id, *extra)

    async def fetchall(
        self,
        db: "aiosqlite.Connection",
        table: str,
        *,
        columns: str = "*",
        extra_where: str | None = None,
        extra_params: tuple[Any, ...] = (),
        order_by: str | None = None,
        limit: int | None = None,
    ) -> list[Any]:
        """home_id 정확 일치로 스코프된 SELECT.

        table 은 화이트리스트(_TENANT_TABLES)에 있어야 한다(SQL 주입 방지).
        """
        if table not in _TENANT_TABLES:
            raise IsolationError(f"unknown or non-tenant table: {table!r}")
        sql = f"SELECT {columns} FROM {table} {self.scope_where(extra_where)}"
        if order_by:
            sql += f" ORDER BY {order_by}"
        if limit is not None:
            sql += " LIMIT ?"
            params = self.scope_params(*extra_params, limit)
        else:
            params = self.scope_params(*extra_params)
        cur = await db.execute(sql, params)
        try:
            return list(await cur.fetchall())
        finally:
            await cur.close()

    async def insert(
        self,
        db: "aiosqlite.Connection",
        table: str,
        values: dict[str, Any],
    ) -> None:
        """home_id 를 강제로 이 스코프로 덮어쓴 INSERT."""
        if table not in _TENANT_TABLES:
            raise IsolationError(f"unknown or non-tenant table: {table!r}")
        row = dict(values)
        row["home_id"] = self._home_id  # 호출자가 다른 home_id 를 줘도 무시.
        cols = ", ".join(row.keys())
        placeholders = ", ".join("?" for _ in row)
        sql = f"INSERT INTO {table} ({cols}) VALUES ({placeholders})"
        await db.execute(sql, tuple(row.values()))


async def assert_no_null_tenants(db: "aiosqlite.Connection") -> None:
    """모든 테넌트 테이블에 home_id NULL/공백 행이 없음을 검증한다.

    하나라도 발견되면 IsolationError.
    """
    for table in _TENANT_TABLES:
        try:
            cur = await db.execute(
                f"SELECT COUNT(*) FROM {table} "
                "WHERE home_id IS NULL OR TRIM(home_id) = ''"
            )
        except Exception:
            # 해당 테이블이 아직 없으면(부분 마이그레이션) 건너뛴다.
            continue
        row = await cur.fetchone()
        await cur.close()
        count = row[0] if row else 0
        if count:
            raise IsolationError(
                f"table {table!r} has {count} row(s) with NULL/empty home_id"
            )
