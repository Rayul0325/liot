"""런타임 등록 레지스트리 — 데모가 "어떤 센서든, 어떤 행동이든" 을 즉석 등록하도록.

데모 서버(demo_server)가 프로세스 싱글턴으로 하나 들고 있으며(``_REGISTRY``),
브라우저에서 새 센서·새 기기·새 행동을 등록하면 다음 ``/api/simulate`` 요청부터
파이프라인에 그대로 흐른다. 상태는 **메모리 전용**(세션 스코프) — 서버가 요청마다
``LiotApp`` 을 새로 만들기 때문에(demo_server) 등록 직후 자동 반영되고 캐시 무효화가
필요 없다. 프리셋(P7)이 등록 번들을 재적용하는 복구 수단을 겸한다.

시드:
  * 기기·행동·위험도 = ``load_device_catalog()`` 가 파싱한 raw dict(=출하
    ``config/device_catalog.yaml``). YAML 을 다시 파싱하지 않고 로더가 만든 raw
    구조를 그대로 씨앗으로 삼는다(중복 파싱 금지).
  * 센서 메타 = 데모 주택의 센서 목록(이름·카테고리·종류·단위·범위). 기존
    ``demo_server._catalog_view()`` 안에 흩어져 있던 메타를 여기로 모은다.

안전 결합(불가침):
  * **확장 전용(extend-only)** — 같은 (capability, action) 을 다시 선언하면
    위험 등급은 ``escalate_only`` 로 올라가기만 한다. 낮추려는 시도는 구조화된
    거부(``RegistryError``)로, 서버가 HTTP 400 으로 매핑한다. 삭제 API 는 없다.
  * **안전 카테고리·카테고리 하한도 확장만 가능** — ``safety_critical=True`` 로
    등록한 카테고리는 ``register_safety_category`` 로 프로세스 전역 안전 집합에
    **추가**되고(내장 frozenset 불변, 해제 불가), ``floor`` 로 준 카테고리 하한은
    ``register_category_floor`` 가 ``escalate_only`` 로만 반영한다. 현재 유효
    하한보다 낮은 요청은 ``RegistryError`` → HTTP 400 으로 거부한다.
  * 위험도 자체의 권위는 여전히 ``DeviceCatalog.risk_for`` — 레지스트리는 병합된
    raw dict 만 생산하고 ``DeviceCatalog(raw)`` 생성자는 무변경으로 재사용한다.
"""

from __future__ import annotations

import copy
import re
from typing import Any

from liot.core.risk import (
    RiskTier,
    escalate_only,
    is_safety_category,
    register_safety_category,
)
from liot.layers.l2_context.proactive_rules import derive_proactive_allowed
from liot.layers.l3_judgment.guardrail import category_floor, register_category_floor
from liot.layers.l4_execution.device_catalog import DeviceCatalog, load_device_catalog

__all__ = ["RuntimeRegistry", "RegistryError"]


class RegistryError(ValueError):
    """등록 검증 실패(구조화된 한글 사유). 서버가 HTTP 400 으로 매핑한다.

    ``field`` 는 폼에서 오류를 인라인 표시할 대상 필드명(없으면 None).
    """

    def __init__(self, message: str, *, field: str | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.field = field


# ── 시드 데이터 ────────────────────────────────────────────────────────────
# 센서 메타(데모 주택). 수치 센서만 kind="number"(+단위/범위), 나머지는 "bool".
# categories() 가 이 목록에서 파생되므로 여기의 category 값이 곧 LiotApp 의
# entity_id -> category 매핑이다(출하 데모의 CATEGORIES 와 동일 집합).
_SEED_SENSORS: list[dict[str, Any]] = [
    {"entity_id": "sensor.living_temperature", "name": "거실 온도",
     "category": "temperature", "kind": "number", "unit": "°C", "min": -10, "max": 40},
    {"entity_id": "sensor.living_humidity", "name": "거실 습도",
     "category": "humidity", "kind": "number", "unit": "%", "min": 0, "max": 100},
    {"entity_id": "binary_sensor.living_motion", "name": "거실 움직임",
     "category": "motion", "kind": "bool", "unit": None, "min": None, "max": None},
    {"entity_id": "light.living_room", "name": "거실 조명",
     "category": "light", "kind": "bool", "unit": None, "min": None, "max": None},
    {"entity_id": "climate.living_room", "name": "거실 냉난방",
     "category": "climate", "kind": "bool", "unit": None, "min": None, "max": None},
    {"entity_id": "cover.living_room", "name": "거실 커튼",
     "category": "curtain", "kind": "bool", "unit": None, "min": None, "max": None},
    {"entity_id": "binary_sensor.kitchen_gas", "name": "주방 가스",
     "category": "gas", "kind": "bool", "unit": None, "min": None, "max": None},
    {"entity_id": "binary_sensor.hall_smoke", "name": "복도 연기",
     "category": "smoke", "kind": "bool", "unit": None, "min": None, "max": None},
    {"entity_id": "binary_sensor.bath_leak", "name": "욕실 누수",
     "category": "leak", "kind": "bool", "unit": None, "min": None, "max": None},
    {"entity_id": "lock.front_door", "name": "현관 잠금",
     "category": "lock", "kind": "bool", "unit": None, "min": None, "max": None},
]

# 카탈로그 devices 의 한글 표시 이름(카탈로그 YAML 엔 이름이 없다). 신규 등록
# 기기는 add_device 의 name 으로 채운다. 없으면 entity_id 를 그대로 노출.
_SEED_DEVICE_NAMES: dict[str, str] = {
    "lock.front_door": "현관 잠금",
    "climate.living_room": "거실 냉난방",
    "switch.gas_valve": "가스 밸브",
    "light.living_room": "거실 조명",
    "cover.garage": "차고 문",
}

# entity_id 형식(HA 스타일): <domain>.<object_id>. 자동 생성분도 이 형식을 지킨다.
_ENTITY_ID_RE = re.compile(r"^[a-z][a-z0-9_]*\.[a-z0-9_]+$")

# 출하 카탈로그 raw 캐시(한 번만 로드). reset 마다 deepcopy 해 격리한다.
_SEED_RAW_CACHE: dict[str, Any] | None = None


def _seed_raw() -> dict[str, Any]:
    """출하 카탈로그 raw dict 의 격리된 사본을 돌려준다.

    ``load_device_catalog()`` 가 파싱한 raw 구조를 그대로 씨앗으로 삼는다(YAML
    재파싱 금지). ``DeviceCatalog`` 는 입력 raw 를 ``_raw`` 로 보관하므로 이를
    딥카피해 레지스트리 변형이 시드/생성자에 새지 않게 한다.
    """
    global _SEED_RAW_CACHE
    if _SEED_RAW_CACHE is None:
        _SEED_RAW_CACHE = load_device_catalog()._raw
    return copy.deepcopy(_SEED_RAW_CACHE)


class RuntimeRegistry:
    """센서·기기·행동의 런타임 등록 상태(메모리 전용, 확장 전용).

    ``to_catalog()`` 로 병합된 ``DeviceCatalog`` 를, ``categories()`` 로 entity->
    category 매핑을, ``sensors_meta()`` 로 센서 보드용 메타를 공급한다.
    """

    def __init__(self) -> None:
        self.reset()

    # ------------------------------------------------------------------
    # 시드/초기화
    # ------------------------------------------------------------------
    def reset(self) -> None:
        """등록 상태를 출하(baked) 상태로 되돌린다(세션 등록분 폐기)."""
        self._raw: dict[str, Any] = _seed_raw()
        self._sensors: dict[str, dict[str, Any]] = {
            m["entity_id"]: dict(m) for m in _SEED_SENSORS
        }
        self._device_names: dict[str, str] = dict(_SEED_DEVICE_NAMES)
        # add_device 로 새로 등록된 기기의 entity->category(=categories() 확장분).
        # 출하 카탈로그 기기(가스밸브·차고문)는 CATEGORIES 에 없었으므로 여기 넣지
        # 않는다 — categories() 가 출하 데모와 동일 집합을 유지하게.
        self._device_categories: dict[str, str] = {}

    # ------------------------------------------------------------------
    # 조회 (서버가 소비)
    # ------------------------------------------------------------------
    def to_catalog(self) -> DeviceCatalog:
        """병합된 raw 로 ``DeviceCatalog`` 를 만든다(생성자 무변경 재사용)."""
        return DeviceCatalog(copy.deepcopy(self._raw))

    def categories(self) -> dict[str, str]:
        """entity_id -> category. 센서 시드 + add_device 등록분."""
        cats = {eid: meta["category"] for eid, meta in self._sensors.items()}
        cats.update(self._device_categories)
        return cats

    def sensors_meta(self) -> list[dict[str, Any]]:
        """센서 보드용 메타 목록(등록 순서 보존)."""
        return [dict(m) for m in self._sensors.values()]

    def proactive_allowed(self) -> frozenset[tuple[str, str]]:
        """레지스트리 병합 카탈로그에서 파생한 능동 허용목록(단일 원천).

        tier ≤ breath 로 등록된 비-안전-액추에이터 (capability, action) 만 담긴다.
        새 등록 행동은 이 집합에 들어오면 능동 제안 대상이 된다.
        """
        return derive_proactive_allowed(self.to_catalog())

    def executable_extras(self) -> frozenset[tuple[str, str]]:
        """실행기(executor)에 주입할 카탈로그 파생 실행 확장분.

        능동 허용목록과 동일 집합 — breath 이하·비안전 액추에이터. executor 가
        ``FORBIDDEN_OPEN`` 을 다시 빼므로 여는 방향은 여기 있어도 실행 불가.
        """
        return self.proactive_allowed()

    def device_name(self, entity_id: str) -> str:
        """기기 표시 이름(없으면 entity_id)."""
        return self._device_names.get(entity_id, entity_id)

    def known_sensor(self, entity_id: str) -> bool:
        return entity_id in self._sensors

    def coerce_value(self, entity_id: str, value: Any) -> Any:
        """UI 값 -> ``SensorReading.raw_value``. 센서 메타 kind 로 강제변환한다.

        미등록 센서는 ``RegistryError``(서버가 HTTP 400 으로 매핑). bool 은 on/off
        문자열로, number 는 float 로 강제한다(기존 ``_BOOL_SENSORS``/``_raw_for``
        특수케이스를 kind 주도로 일반화).
        """
        meta = self._sensors.get(entity_id)
        if meta is None:
            raise RegistryError(
                f"등록되지 않은 센서입니다: {entity_id}", field="entity_id"
            )
        if meta["kind"] == "number":
            try:
                return float(value)
            except (TypeError, ValueError):
                raise RegistryError(
                    f"수치 센서 값이 숫자가 아닙니다: {entity_id}={value!r}",
                    field="value",
                ) from None
        # bool: python bool / 숫자 진리값 -> on/off, 이미 문자열이면 그대로.
        if isinstance(value, bool):
            return "on" if value else "off"
        if isinstance(value, (int, float)):
            return "on" if value else "off"
        return value

    # ------------------------------------------------------------------
    # 등록 (확장 전용)
    # ------------------------------------------------------------------
    def add_sensor(self, meta: dict[str, Any]) -> dict[str, Any]:
        """센서 1개 등록. 반환 = 저장된 메타. 검증 실패는 ``RegistryError``."""
        name = str(meta.get("name") or "").strip()
        category = str(meta.get("category") or "").strip()
        kind = str(meta.get("kind") or "").strip().lower()
        entity_id = str(meta.get("entity_id") or "").strip()

        if not name:
            raise RegistryError("센서 이름을 입력하세요.", field="name")
        if not category:
            raise RegistryError("카테고리를 입력하세요.", field="category")
        if kind not in ("bool", "number"):
            raise RegistryError(
                f"센서 종류는 bool 또는 number 여야 합니다: {kind or '(빈값)'}",
                field="kind",
            )

        if not entity_id:
            entity_id = self._gen_entity_id("sensor", category)
        elif not _ENTITY_ID_RE.match(entity_id):
            raise RegistryError(
                f"entity_id 형식이 올바르지 않습니다(예: sensor.soil_moisture): {entity_id}",
                field="entity_id",
            )
        if entity_id in self._sensors:
            raise RegistryError(
                f"이미 등록된 센서입니다: {entity_id}", field="entity_id"
            )

        # 안전 결합 검증(확장 전용) — 로컬 커밋 전. 낮추기 요청은 여기서 400.
        floor_tier = self._validate_floor(category, meta.get("floor"))

        rec: dict[str, Any] = {
            "entity_id": entity_id, "name": name, "category": category,
            "kind": kind, "unit": None, "min": None, "max": None,
        }
        if kind == "number":
            try:
                mn = float(meta.get("min"))
                mx = float(meta.get("max"))
            except (TypeError, ValueError):
                raise RegistryError(
                    "수치 센서는 최소·최대값(숫자)이 필요합니다.", field="min"
                ) from None
            if mn >= mx:
                raise RegistryError("최소값은 최대값보다 작아야 합니다.", field="min")
            unit = meta.get("unit")
            rec.update(unit=(str(unit).strip() or None) if unit else None, min=mn, max=mx)

        self._sensors[entity_id] = rec
        # 검증 통과 후 프로세스 전역 안전 등록 반영(확장 전용, 되돌릴 수 없음).
        self._apply_safety(category, meta.get("safety_critical"), floor_tier)
        return dict(rec)

    def add_device(
        self,
        entity_id: str,
        name: str,
        capability: str,
        category: str,
        actions: list[dict[str, Any]] | None = None,
        *,
        safety_critical: Any = False,
        floor: Any = None,
    ) -> dict[str, Any]:
        """기기 1개(+행동 목록) 등록. 원자적 — 한 행동이라도 거부되면 무커밋.

        ``safety_critical`` 이 참이면 카테고리를 안전 집합에 추가하고, ``floor`` 가
        주어지면 카테고리 하한을 상향한다(둘 다 확장 전용, 낮추기는 400).
        """
        entity_id = str(entity_id or "").strip()
        name = str(name or "").strip()
        capability = str(capability or "").strip()
        category = str(category or "").strip()
        actions = actions or []

        if not capability:
            raise RegistryError("capability 를 입력하세요.", field="capability")
        if not category:
            raise RegistryError("카테고리를 입력하세요.", field="category")
        if not actions:
            raise RegistryError("행동을 하나 이상 추가하세요.", field="actions")

        if not entity_id:
            domain = capability if capability.isascii() else "device"
            entity_id = self._gen_entity_id(domain, capability)
        elif not _ENTITY_ID_RE.match(entity_id):
            raise RegistryError(
                f"entity_id 형식이 올바르지 않습니다(예: vacuum.robot): {entity_id}",
                field="entity_id",
            )
        if self._device_index(entity_id) is not None:
            raise RegistryError(
                f"이미 등록된 기기입니다: {entity_id}", field="entity_id"
            )
        if not name:
            name = entity_id

        # pass 1: 모든 행동 + 안전 하한을 먼저 검증(확장 전용 위반은 여기서 거부) —
        # 원자성. 낮추기 요청은 커밋 전에 400 으로 튕긴다.
        floor_tier = self._validate_floor(category, floor)
        resolved: list[tuple[str, RiskTier, Any]] = []
        for a in actions:
            action = str(a.get("action") or "").strip()
            if not action:
                raise RegistryError("행동 이름을 입력하세요.", field="action")
            tier = self._resolve_action_tier(capability, action, a.get("tier"))
            resolved.append((action, tier, a.get("directional")))

        # pass 2: 커밋.
        cap_map = self._raw.setdefault("capabilities", {}).setdefault(capability, {})
        for action, tier, directional in resolved:
            spec = cap_map.setdefault(action, {})
            spec["tier"] = tier.name.lower()
            if isinstance(directional, dict):
                spec["directional"] = directional
        self._raw.setdefault("devices", []).append(
            {"entity_id": entity_id, "capability": capability, "category": category}
        )
        self._device_names[entity_id] = name
        self._device_categories[entity_id] = category
        # 검증 통과·커밋 후 프로세스 전역 안전 등록 반영(확장 전용).
        self._apply_safety(category, safety_critical, floor_tier)
        return {
            "entity_id": entity_id, "name": name, "capability": capability,
            "category": category,
            "actions": [{"action": a, "tier": t.name.lower()} for a, t, _ in resolved],
        }

    def add_action(
        self,
        capability: str,
        action: str,
        tier: Any,
        directional: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """(capability, action) 하나를 등록/갱신. 확장 전용(낮추기 거부)."""
        capability = str(capability or "").strip()
        action = str(action or "").strip()
        if not capability:
            raise RegistryError("capability 를 입력하세요.", field="capability")
        if not action:
            raise RegistryError("행동 이름을 입력하세요.", field="action")
        new_tier = self._resolve_action_tier(capability, action, tier)
        cap_map = self._raw.setdefault("capabilities", {}).setdefault(capability, {})
        spec = cap_map.setdefault(action, {})
        spec["tier"] = new_tier.name.lower()
        if isinstance(directional, dict):
            spec["directional"] = directional
        return {
            "capability": capability, "action": action, "tier": new_tier.name.lower()
        }

    # ------------------------------------------------------------------
    # 내부
    # ------------------------------------------------------------------
    def _validate_floor(self, category: str, floor: Any) -> RiskTier | None:
        """카테고리 하한 요청을 검증한다(낮추기 거부). 미지정이면 None.

        현재 유효 하한(안전 카테고리 → EMBER, 그 외 → ``category_floor``)보다 낮은
        요청은 ``RegistryError``(확장 전용). 알 수 없는 등급도 ``RegistryError``.
        실제 반영은 검증 통과 후 ``_apply_safety`` 가 한다.
        """
        if floor is None or (isinstance(floor, str) and not floor.strip()):
            return None
        try:
            requested = RiskTier.from_str(floor)
        except ValueError:
            raise RegistryError(
                f"알 수 없는 위험 등급입니다: {floor!r} "
                "(ambient/breath/dusk/ember 중 하나)",
                field="floor",
            ) from None
        current = (
            RiskTier.EMBER if is_safety_category(category) else category_floor(category)
        )
        if current is not None and requested < current:
            raise RegistryError(
                f"안전 하한은 낮출 수 없습니다(확장 전용): "
                f"기존 '{current.label_ko}' → 요청 '{requested.label_ko}' 거부",
                field="floor",
            )
        return requested

    def _apply_safety(
        self, category: str, safety_critical: Any, floor_tier: RiskTier | None
    ) -> None:
        """검증 통과 후 프로세스 전역 안전 등록을 반영한다(확장 전용).

        ``safety_critical`` 이 참이면 카테고리를 안전 집합에 추가하고, ``floor_tier``
        가 있으면 카테고리 하한을 ``escalate_only`` 로 상향한다. 둘 다 되돌릴 수 없다
        (제거 API 없음). 데모 ``reset`` 은 별도로 확장 set 전체를 비운다.
        """
        if safety_critical:
            register_safety_category(category)
        if floor_tier is not None:
            register_category_floor(category, floor_tier)

    def _resolve_action_tier(
        self, capability: str, action: str, tier_value: Any
    ) -> RiskTier:
        """요청 등급을 검증하고 확장 전용을 강제한다.

        기존 (capability, action) 이 있으면 요청 등급이 더 낮은 경우 거부하고,
        같거나 높으면 ``escalate_only`` 로 병합(=요청 등급)한다.
        """
        try:
            requested = RiskTier.from_str(tier_value)
        except ValueError:
            raise RegistryError(
                f"알 수 없는 위험 등급입니다: {tier_value!r} "
                "(ambient/breath/dusk/ember 중 하나)",
                field="tier",
            ) from None
        existing_spec = (
            self._raw.get("capabilities", {}).get(capability, {}).get(action)
        )
        if isinstance(existing_spec, dict) and existing_spec.get("tier") is not None:
            existing = RiskTier.from_str(existing_spec["tier"])
            if requested < existing:
                raise RegistryError(
                    f"위험 등급은 낮출 수 없습니다(확장 전용): "
                    f"기존 '{existing.label_ko}' → 요청 '{requested.label_ko}' 거부",
                    field="tier",
                )
            return escalate_only(existing, requested)
        return requested

    def _device_index(self, entity_id: str) -> int | None:
        for i, dev in enumerate(self._raw.get("devices", []) or []):
            if str(dev.get("entity_id")) == entity_id:
                return i
        return None

    def _gen_entity_id(self, domain: str, base: str) -> str:
        """비어있는 entity_id 를 카테고리 슬러그로 자동 생성한다(충돌 회피)."""
        slug = re.sub(r"[^a-z0-9]+", "_", str(base).lower()).strip("_") or "custom"
        candidate = f"{domain}.{slug}"
        n = 1
        taken = set(self._sensors) | {
            str(d.get("entity_id")) for d in self._raw.get("devices", []) or []
        }
        while candidate in taken:
            n += 1
            candidate = f"{domain}.{slug}_{n}"
        return candidate
