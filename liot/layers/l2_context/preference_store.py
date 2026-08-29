"""사용자 선호 저장소(L2).

key/scope/value 형태의 선호를 저장·조회한다. 영속 백엔드는 core 의
ScopedRepo(테넌시 격리)를 통해 ``preferences`` 테이블에 접근하고, DB 없이
동작하는 인메모리 백엔드도 지원한다(테스트·데모).

스키마 주의: preferences 테이블은 (home_id, key) PK + value(JSON) 컬럼이다.
별도 scope 컬럼이 없으므로 scope 는 key 네임스페이스로 인코딩한다:
    저장 키 = f"{scope}:{key}"  (scope 기본값 'default')
value 는 JSON 직렬화하여 보관한다(임의 구조 허용).
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

from liot.core.scoped_repo import ScopedRepo

if TYPE_CHECKING:  # pragma: no cover
    import aiosqlite

__all__ = ["PreferenceStore"]

_DEFAULT_SCOPE = "default"


def _compose_key(scope: str, key: str) -> str:
    return f"{scope}:{key}"


class PreferenceStore:
    """선호 저장소. DB(ScopedRepo) 또는 인메모리 백엔드.

    db 가 None 이면 인메모리 dict 백엔드를 사용한다. 어느 쪽이든 home_id
    격리는 ScopedRepo 가 보증한다(None/공백 home_id -> IsolationError).
    """

    def __init__(self, home_id: str | None, db: aiosqlite.Connection | None = None) -> None:
        # ScopedRepo 가 home_id None/공백을 IsolationError 로 거른다.
        self._repo = ScopedRepo(home_id)
        self._db = db
        # 인메모리 백엔드: composed_key -> value(파이썬 객체).
        self._mem: dict[str, Any] = {}

    @property
    def home_id(self) -> str:
        return self._repo.home_id

    @property
    def is_persistent(self) -> bool:
        return self._db is not None

    async def set(
        self,
        key: str,
        value: Any,
        *,
        scope: str = _DEFAULT_SCOPE,
        updated_at: str = "",
    ) -> None:
        """선호 값을 저장(upsert)한다. value 는 JSON 직렬화 가능해야 한다."""
        composed = _compose_key(scope, key)
        if self._db is None:
            self._mem[composed] = value
            return
        encoded = json.dumps(value, ensure_ascii=False, sort_keys=True)
        # (home_id, key) PK upsert. ScopedRepo 가 home_id 를 강제하지만
        # upsert 구문이 필요해 직접 실행한다(테이블은 테넌트 화이트리스트).
        await self._db.execute(
            "INSERT INTO preferences (home_id, key, value, updated_at) "
            "VALUES (?, ?, ?, ?) "
            "ON CONFLICT(home_id, key) DO UPDATE SET "
            "value = excluded.value, updated_at = excluded.updated_at",
            (self.home_id, composed, encoded, updated_at),
        )

    async def get(
        self,
        key: str,
        default: Any = None,
        *,
        scope: str = _DEFAULT_SCOPE,
    ) -> Any:
        """선호 값을 조회한다. 없으면 default."""
        composed = _compose_key(scope, key)
        if self._db is None:
            return self._mem.get(composed, default)
        rows = await self._repo.fetchall(
            self._db,
            "preferences",
            columns="value",
            extra_where="key = ?",
            extra_params=(composed,),
            limit=1,
        )
        if not rows:
            return default
        raw = rows[0][0]
        if raw is None:
            return default
        try:
            return json.loads(raw)
        except (TypeError, json.JSONDecodeError):  # pragma: no cover - 방어적
            return raw

    async def all_in_scope(self, scope: str = _DEFAULT_SCOPE) -> dict[str, Any]:
        """해당 scope 의 모든 (key -> value) 를 반환한다."""
        prefix = f"{scope}:"
        if self._db is None:
            return {
                k[len(prefix):]: v
                for k, v in self._mem.items()
                if k.startswith(prefix)
            }
        rows = await self._repo.fetchall(
            self._db,
            "preferences",
            columns="key, value",
            extra_where="key LIKE ?",
            extra_params=(prefix + "%",),
        )
        out: dict[str, Any] = {}
        for row in rows:
            composed, raw = row[0], row[1]
            short = composed[len(prefix):]
            try:
                out[short] = json.loads(raw) if raw is not None else None
            except (TypeError, json.JSONDecodeError):  # pragma: no cover
                out[short] = raw
        return out
