"""LIOT 영속 계층.

forward-only 마이그레이션(migrations.py)과 aiosqlite 연결 헬퍼(database.py),
그리고 스키마 정의(schema.sql)를 제공한다.

모든 테넌트 테이블의 home_id 는 TEXT NOT NULL DEFAULT 'singleton'.
"""

__all__ = ["migrations", "database"]
