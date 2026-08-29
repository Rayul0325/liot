"""맥락 스냅샷 빌더(L2).

L1 의 정규화된 상태(entity_id -> norm_value)와 트리거 정보를 받아
``ContextSnapshot`` 을 만든다. 핵심 책무 둘:

  1) should_snapshot: 언제 스냅샷을 빚을지 결정한다.
     - 안전 임계 카테고리(SAFETY_CRITICAL_CATEGORIES)에 속한 엔티티의 값이
       바뀌면 tick/threshold 와 무관하게 **무조건** True (불변식6,
       Safety-Never-Silent). 안전 변화는 결코 삼켜지지 않는다.
     - 비안전 변화는 semantic_delta(prev, cur) 가 threshold 이상이거나,
       주기적 tick(force_tick) 일 때 True.

  2) build: 결정론적 structured 페이로드 + 가명화된 자연어 요약을 담은
     ContextSnapshot 을 만든다. snapshot_hash 는 contracts 의 model_validator
     가 structured 에서 sha256 으로 채운다(재현 가능성).

nl_summary 는 AliasMap 가명 토큰(ROOM_A/PERSON_1 ...)만 사용한다. 실제
방이름·디바이스명·사람이름은 절대 요약에 들어가지 않는다(누출 차단).
"""

from __future__ import annotations

import hashlib
import uuid
from collections.abc import Mapping
from typing import Any

from liot.core.categories import resolve_category
from liot.core.clock import Clock
from liot.core.contracts import ContextSnapshot
from liot.core.egress_gate import AliasMap
from liot.core.risk import is_safety_category

__all__ = [
    "ContextBuilder",
    "SUMMARY_TEMPLATE",
    "SUMMARY_TEMPLATE_SHA",
    "OCCUPANCY_CATEGORIES",
    "derive_occupancy",
    "semantic_delta",
    "should_snapshot",
]

# 자연어 요약 템플릿. 실제 식별자는 절대 넣지 않고 가명/집계치만 채운다.
SUMMARY_TEMPLATE = (
    "[{bucket}] {n_entities} signals; changed={n_changed}; "
    "safety={safety_flag}; trigger={trigger_kind}"
)
SUMMARY_TEMPLATE_SHA = hashlib.sha256(SUMMARY_TEMPLATE.encode("utf-8")).hexdigest()

# 비안전 수치 변화의 기본 임계(상대/절대 혼합 점수). 0~1+ 범위.
DEFAULT_SEMANTIC_THRESHOLD = 0.15


def _category_of(entity_id: str, categories: Mapping[str, str] | None) -> str:
    """엔티티의 카테고리를 결정한다(단일 리졸버 위임, 카탈로그 없이).

    맵 우선 → 도메인/언더스코어 접두사 → 'unknown'. 카탈로그 메타는 이 계층에
    없으므로 catalog=None 으로 위임한다(맵에 없는 안전 엔티티는 도메인 접두사로
    떨어지되, 안전 판정은 categories 맵이 채워져야 정확 — 기존과 동일).
    """
    return resolve_category(entity_id, categories, None)


def _is_safety(category: str | None) -> bool:
    return is_safety_category(category)


# 재실(occupancy) 유도에 쓰는 카테고리. motion/presence 류만 본다.
OCCUPANCY_CATEGORIES: frozenset[str] = frozenset({"motion", "presence", "occupancy"})


def derive_occupancy(
    cur: Mapping[str, Any],
    categories: Mapping[str, str] | None,
) -> str:
    """현재 상태로부터 재실 상태를 결정론적으로 유도한다.

    'occupied'  : motion/presence 류 엔티티 중 하나라도 truthy.
    'vacant'    : motion/presence 류 엔티티가 있으나 모두 falsy.
    'unknown'   : motion/presence 류 엔티티가 하나도 없음(추정 금지).

    이 fact 가 structured 에 실려야 Gate1 규칙(occupancy 매치 키)과 능동
    규칙이 실제로 매칭된다 — 빠지면 occupancy 규칙은 조용한 죽은 코드가 된다.
    """
    found = False
    for eid, val in cur.items():
        if _category_of(eid, categories) in OCCUPANCY_CATEGORIES:
            found = True
            if bool(val):
                return "occupied"
    return "vacant" if found else "unknown"


def _scalar_delta(old: Any, new: Any) -> float:
    """두 norm_value 사이의 의미 변화 점수(0=무변화, 1=완전변화).

    - bool/문자열/None: 다르면 1.0, 같으면 0.0.
    - 수치: 상대 변화 |new-old| / (|old|+|new|+eps), 0~1 로 포화.
    """
    if isinstance(old, bool) or isinstance(new, bool):
        return 0.0 if old == new else 1.0
    if isinstance(old, (int, float)) and isinstance(new, (int, float)):
        denom = abs(old) + abs(new)
        if denom == 0:
            return 0.0
        return min(1.0, abs(float(new) - float(old)) / denom)
    return 0.0 if old == new else 1.0


def semantic_delta(
    prev: Mapping[str, Any] | None,
    cur: Mapping[str, Any],
) -> float:
    """이전/현재 상태(entity_id -> norm_value) 사이의 최대 변화 점수.

    엔티티별 변화 점수의 최댓값을 반환한다(가장 두드러진 변화 기준).
    prev 가 None 이면(최초) 모든 값이 새로 등장 -> 1.0.
    새로 나타나거나 사라진 엔티티도 변화로 본다(1.0).
    """
    if prev is None:
        return 1.0 if cur else 0.0
    keys = set(prev) | set(cur)
    max_score = 0.0
    for k in keys:
        if k not in prev or k not in cur:
            return 1.0
        max_score = max(max_score, _scalar_delta(prev[k], cur[k]))
    return max_score


def _changed_safety_categories(
    prev: Mapping[str, Any] | None,
    cur: Mapping[str, Any],
    categories: Mapping[str, str] | None,
) -> list[str]:
    """안전 임계 카테고리 중 값이 바뀐(또는 새로 등장/소멸한) 것들."""
    changed: list[str] = []
    keys = set(cur) if prev is None else (set(prev) | set(cur))
    for eid in keys:
        cat = _category_of(eid, categories)
        if not _is_safety(cat):
            continue
        old = None if prev is None else prev.get(eid, _MISSING)
        new = cur.get(eid, _MISSING)
        if old != new:
            assert cat is not None  # _is_safety 가 보장
            changed.append(cat)
    return changed


_MISSING = object()


def should_snapshot(
    prev: Mapping[str, Any] | None,
    cur: Mapping[str, Any],
    prefs: Mapping[str, Any] | None = None,
    *,
    categories: Mapping[str, str] | None = None,
    force_tick: bool = False,
) -> bool:
    """스냅샷을 빚어야 하는지 판정한다.

    불변식6(Safety-Never-Silent): 안전 임계 카테고리 엔티티의 값이 바뀌면
    threshold·tick 과 무관하게 **항상** True. 가장 먼저 검사한다.

    비안전: semantic_delta(prev, cur) >= threshold 이거나 force_tick.
    threshold 는 prefs['semantic_threshold'] 로 덮어쓸 수 있다.
    """
    # 1) 안전 변화 -> 무조건 스냅샷(최우선, 단락).
    if _changed_safety_categories(prev, cur, categories):
        return True
    # 2) 주기적 tick.
    if force_tick:
        return True
    # 3) 비안전 의미 변화 임계.
    threshold = DEFAULT_SEMANTIC_THRESHOLD
    if prefs and "semantic_threshold" in prefs:
        try:
            threshold = float(prefs["semantic_threshold"])
        except (TypeError, ValueError):  # pragma: no cover - 방어적
            threshold = DEFAULT_SEMANTIC_THRESHOLD
    return semantic_delta(prev, cur) >= threshold


class ContextBuilder:
    """맥락 스냅샷을 빚는 빌더.

    상태(norm_values)와 트리거를 받아 결정론적 structured 페이로드를 만들고,
    가명화된 자연어 요약을 붙여 ContextSnapshot 을 생성한다.
    """

    def __init__(
        self,
        clock: Clock | None = None,
        *,
        home_id: str = "singleton",
        categories: Mapping[str, str] | None = None,
    ) -> None:
        self._clock = clock or Clock()
        self._home_id = home_id
        self._categories = dict(categories) if categories else {}

    def should_snapshot(
        self,
        prev: Mapping[str, Any] | None,
        cur: Mapping[str, Any],
        prefs: Mapping[str, Any] | None = None,
        *,
        force_tick: bool = False,
    ) -> bool:
        """모듈 함수 should_snapshot 의 인스턴스 래퍼(카테고리 맵 주입)."""
        return should_snapshot(
            prev,
            cur,
            prefs,
            categories=self._categories,
            force_tick=force_tick,
        )

    def build(
        self,
        cur: Mapping[str, Any],
        trigger: Mapping[str, Any],
        *,
        prev: Mapping[str, Any] | None = None,
        trace_id: str | None = None,
        prefs: Mapping[str, Any] | None = None,
        alias: AliasMap | None = None,
    ) -> ContextSnapshot:
        """현재 상태에서 ContextSnapshot 을 만든다(재현 가능).

        structured 는 정렬·정규화된 결정론적 페이로드다(키 순서 무관, 동일
        입력 -> 동일 snapshot_hash). nl_summary 는 가명 토큰만 사용한다.
        """
        trace = trace_id or uuid.uuid4().hex
        now = self._clock.now_utc()
        bucket = self._clock.time_bucket()

        # 변화한 엔티티 / 안전 변화 여부 계산.
        changed = self._changed_entities(prev, cur)
        safety_cats = sorted(set(_changed_safety_categories(prev, cur, self._categories)))

        # 엔티티별 카테고리 표(결정론적, 정렬).
        entity_categories = {
            eid: (_category_of(eid, self._categories) or "unknown")
            for eid in cur
        }

        # 결정론적 structured 페이로드. dict 는 contracts 의 canonical JSON
        # (sort_keys) 으로 해시되므로 키 순서는 무관하다.
        structured: dict[str, Any] = {
            "time_bucket": bucket,
            "trust_ok": self._clock.trust_ok,
            "occupancy": derive_occupancy(cur, self._categories),
            "entities": dict(cur),
            "entity_categories": entity_categories,
            "changed_entities": sorted(changed),
            "safety_changed_categories": safety_cats,
            "trigger": dict(trigger),
        }

        nl_summary = self._render_summary(
            bucket=bucket,
            cur=cur,
            changed=changed,
            safety_cats=safety_cats,
            trigger=trigger,
            alias=alias or AliasMap(),
        )

        return ContextSnapshot(
            snapshot_id=uuid.uuid4().hex,
            trace_id=trace,
            home_id=self._home_id,
            trigger=dict(trigger),
            structured=structured,
            nl_summary=nl_summary,
            summary_template_sha=SUMMARY_TEMPLATE_SHA,
            created_at=now,
            # snapshot_hash 는 비워 두면 contracts 가 structured 에서 채운다.
        )

    @staticmethod
    def _changed_entities(
        prev: Mapping[str, Any] | None,
        cur: Mapping[str, Any],
    ) -> list[str]:
        if prev is None:
            return list(cur.keys())
        out: list[str] = []
        for eid in set(prev) | set(cur):
            if prev.get(eid, _MISSING) != cur.get(eid, _MISSING):
                out.append(eid)
        return out

    def _render_summary(
        self,
        *,
        bucket: str,
        cur: Mapping[str, Any],
        changed: list[str],
        safety_cats: list[str],
        trigger: Mapping[str, Any],
        alias: AliasMap,
    ) -> str:
        """가명 토큰만 사용하는 자연어 요약을 만든다.

        실제 entity_id/방이름/사람이름은 넣지 않는다. trigger 의 종류만
        집계 수준으로 노출하고, 안전 변화는 카테고리명(고정 어휘)만 쓴다.
        """
        # trigger 의 식별성 있는 값은 가명화하여 종류만 추출.
        trigger_kind = str(trigger.get("kind", trigger.get("type", "unknown")))
        # trigger 에 entity 식별자가 있으면 가명 토큰으로만 표현.
        if "entity_id" in trigger:
            alias.alias_for(str(trigger["entity_id"]), kind="DEVICE")
        safety_flag = ",".join(safety_cats) if safety_cats else "none"
        return SUMMARY_TEMPLATE.format(
            bucket=bucket,
            n_entities=len(cur),
            n_changed=len(changed),
            safety_flag=safety_flag,
            trigger_kind=trigger_kind,
        )
