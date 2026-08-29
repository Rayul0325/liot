"""카테고리 리졸버 — entity_id → category 의 단일 원천(D6).

app·context_builder·proactive 생성기가 각자 같은 우선순위 체인을 중복 구현하던
것을 하나로 모은다. 분류 불일치는 안전 우회 구멍이므로(생성기의 안전 거부가
app/게이트와 다른 분류를 보면 안 됨) 단일 함수로 공유한다.

catalog 는 인자로만 받는다(TYPE_CHECKING 임포트) — 이 모듈은 어떤 레이어도
런타임 임포트하지 않아 순환 의존이 없다(core 하위, 데이터-light).
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING

if TYPE_CHECKING:  # pragma: no cover
    from liot.layers.l4_execution.device_catalog import DeviceCatalog

__all__ = ["resolve_category"]


def resolve_category(
    entity_id: str,
    categories: Mapping[str, str] | None,
    catalog: DeviceCatalog | None,
) -> str:
    """엔티티의 카테고리를 결정한다(우선순위 고정).

    명시 categories 맵 → 카탈로그 메타(``category_for``) → 도메인/언더스코어
    접두사 → ``'unknown'``. app._category_of·proactive 생성기·context_builder 가
    전부 이 체인을 공유해, 안전 분류가 계층마다 갈리지 않게 한다.
    """
    if categories and entity_id in categories:
        return categories[entity_id]
    if catalog is not None:
        cat = catalog.category_for(entity_id)
        if cat:
            return cat
    if "." in entity_id:
        return entity_id.split(".", 1)[0]
    if "_" in entity_id:
        return entity_id.split("_", 1)[0]
    return "unknown"
