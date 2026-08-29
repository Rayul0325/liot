"""L2 Context 레이어.

L1 의 정규화된 상태(StateCache)와 사용자 선호(PreferenceStore)를 결합해
재현 가능한 ``ContextSnapshot`` 을 빚는다. 자연어 요약(nl_summary)은
AliasMap 가명 토큰만으로 구성해 식별 정보 누출을 구조적으로 차단한다.
"""

from __future__ import annotations

from .context_builder import (
    SUMMARY_TEMPLATE,
    SUMMARY_TEMPLATE_SHA,
    ContextBuilder,
    semantic_delta,
    should_snapshot,
)
from .preference_store import PreferenceStore

__all__ = [
    "ContextBuilder",
    "PreferenceStore",
    "SUMMARY_TEMPLATE",
    "SUMMARY_TEMPLATE_SHA",
    "semantic_delta",
    "should_snapshot",
]
