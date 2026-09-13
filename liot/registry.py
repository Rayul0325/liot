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
import math
import re
from typing import Any

from liot.core.risk import (
    RiskTier,
    escalate_only,
    is_safety_category,
    register_safety_category,
)
from liot.layers.l2_context.proactive_rules import (
    ALLOWED_PREDICATE_OPS,
    ProactiveRule,
    ProactiveRuleError,
    ProactiveRuleSet,
    build_ruleset,
    derive_proactive_allowed,
    load_proactive_rulesets,
    parse_rule,
)
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

# capability·action 이름에 쓸 수 없는 문자: 공백과 구분자 `|`·`:`.
# 이 두 이름은 나중에 두 곳에서 **구분자로 조립**된다:
#   - 선호 셀 키 `time_bucket|occupancy|capability:action` (feature_vector.cell_key)
#   - 데모 UI 의 행동 선택값 `entity_id|capability|action` (web/assets/app.js)
# 그래서 "turn on" 처럼 공백이 든 이름은 등록은 되고 나중에 그 셀의 피드백이
# 400 으로 막히는(=등록 시점엔 안 보이는) 어긋남을 만든다. 조립 지점에서 막지 않고
# 입력 지점에서 거부한다 — 사용자가 고칠 수 있는 자리가 여기다.
_NAME_FORBIDDEN_RE = re.compile(r"[\s|:]")


# 이름 길이 상한. 셀 키(`time_bucket|occupancy|capability:action`)에는 별도 상한
# (demo_server.MAX_CELL_KEY_LEN=200)이 있어, 120자 + 120자 같은 이름은 등록은 되고
# 그 셀의 피드백만 400 이 됐다 — 같은 '나중에 드러나는 어긋남'이다(리뷰 R-1 잔여).
_NAME_MAX_LEN = 64


def _validate_name(value: str, field: str, label: str) -> str:
    """capability·action 이름 검사. 공백·`|`·`:` 나 과도한 길이면 RegistryError."""
    if len(value) > _NAME_MAX_LEN:
        raise RegistryError(
            f"{label}이 너무 깁니다({len(value)}자) — {_NAME_MAX_LEN}자 이내로 쓰세요.",
            field=field,
        )
    if _NAME_FORBIDDEN_RE.search(value):
        raise RegistryError(
            f"{label}에는 공백과 구분자(| :)를 쓸 수 없습니다"
            f"(밑줄을 쓰세요, 예: turn_on): {value!r}",
            field=field,
        )
    return value

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
        # 런타임 등록 임계 규칙(파싱·검증 완료본). config 베이스라인과 병합해
        # proactive_ruleset() 이 생성기에 공급한다. reset 에서 폐기(세션 스코프).
        self._rules: list[ProactiveRule] = []

    # ------------------------------------------------------------------
    # 조회 (서버가 소비)
    # ------------------------------------------------------------------
    def to_catalog(self) -> DeviceCatalog:
        """병합된 raw 로 ``DeviceCatalog`` 를 만든다(생성자 무변경 재사용)."""
        return DeviceCatalog(copy.deepcopy(self._raw))

    def snapshot(self) -> dict[str, Any]:
        """현재 등록 상태의 격리된 스냅샷을 돌려준다(프리셋 원자 적용의 롤백 지점).

        ``restore`` 로 정확히 이 상태로 되돌릴 수 있다 — 프리셋(P7)이 센서·기기·
        규칙 번들을 순차 등록하다 한 항목이라도 거부되면 이 스냅샷으로 복원해
        "실패하면 아무것도 안 남는다"(원자성)를 보장한다. 프로세스 전역 안전 확장
        (``register_safety_category``/``register_category_floor``)은 확장 전용이라
        스냅샷에 담지 않는다 — 되돌리는 것 자체가 안전 하한을 낮추는 셈이 되고,
        확장은 절대 안전을 낮추지 않으므로(escalate만) 남겨도 무해하다.
        """
        return {
            "raw": copy.deepcopy(self._raw),
            "sensors": {k: dict(v) for k, v in self._sensors.items()},
            "device_names": dict(self._device_names),
            "device_categories": dict(self._device_categories),
            "rules": list(self._rules),
        }

    def restore(self, snap: dict[str, Any]) -> None:
        """``snapshot`` 이 만든 상태로 되돌린다(프리셋 적용 실패 시 원자 롤백)."""
        self._raw = copy.deepcopy(snap["raw"])
        self._sensors = {k: dict(v) for k, v in snap["sensors"].items()}
        self._device_names = dict(snap["device_names"])
        self._device_categories = dict(snap["device_categories"])
        self._rules = list(snap["rules"])

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

    def proactive_ruleset(self) -> ProactiveRuleSet:
        """config 베이스라인 규칙 + 런타임 등록 규칙을 하나의 병합 룰셋으로.

        베이스라인(``config/proactive/*.yaml``)을 반드시 포함한다 — 출하 홈의
        vacant-light-off 가 tick 의 ``propose()`` 에서 계속 매칭돼 능동 진입
        게이트를 통과해야 하기 때문이다(빠지면 능동성이 침묵으로 퇴행). 파생
        허용목록을 한 번만 계산해 로더/런타임 규칙 검증에 공유한다. limits 는
        베이스라인 것을 승계한다(런타임 규칙이 throttle 을 못 느슨하게).
        """
        allowed = self.proactive_allowed()
        base = load_proactive_rulesets(allowed=allowed)
        return build_ruleset(
            tuple(base.rules) + tuple(self._rules),
            name="__demo__",
            version=base.ruleset_version,
            limits=base.limits,
            sources=base.sources,
        )

    def rules_meta(self) -> list[dict[str, Any]]:
        """등록된 런타임 규칙 요약 목록(UI 규칙 패널용, 등록 순서 보존)."""
        return [self._rule_view(r) for r in self._rules]

    @staticmethod
    def _rule_view(rule: ProactiveRule) -> dict[str, Any]:
        """ProactiveRule → UI/응답용 평면 dict(한글 요약은 프런트가 조립)."""
        return {
            "id": rule.rule_id,
            "match": dict(rule.match),
            "state": [
                {"entity": p.entity_id, "op": p.op, "value": p.value} for p in rule.state
            ],
            "capability": rule.capability,
            "action": rule.action,
            "target": rule.target_entity,
        }

    def device_name(self, entity_id: str) -> str:
        """기기 표시 이름(없으면 entity_id)."""
        return self._device_names.get(entity_id, entity_id)

    def known_sensor(self, entity_id: str) -> bool:
        return entity_id in self._sensors

    def known_device(self, entity_id: str) -> bool:
        """등록된 기기(카탈로그 devices)에 이 entity_id 가 있는가."""
        return self._device_index(entity_id) is not None

    @staticmethod
    def _coerce_bool(value: Any) -> Any:
        """bool 상태 강제변환: python bool / 숫자 진리값 → on/off, 문자열은 그대로."""
        if isinstance(value, bool):
            return "on" if value else "off"
        if isinstance(value, (int, float)):
            return "on" if value else "off"
        return value

    def coerce_value(self, entity_id: str, value: Any) -> Any:
        """UI 값 -> ``SensorReading.raw_value``. 센서/기기 상태를 강제변환한다.

        센서면 메타 kind(number→float, bool→on/off)로, 등록된 **기기**면 상태
        토글(on/off bool)로 강제한다 — 런타임 등록 기기의 상태를 simulate 로 주입해
        빈집→off류 일반화가 실제로 도달하게 한다(baked 기기는 센서로도 시드돼
        센서 경로가 상태를 소유). 둘 다 아니면 ``RegistryError``(서버가 HTTP 400).
        """
        meta = self._sensors.get(entity_id)
        if meta is None:
            if self.known_device(entity_id):
                return self._coerce_bool(value)  # 기기 상태 = on/off
            raise RegistryError(
                f"등록되지 않은 센서·기기입니다: {entity_id}", field="entity_id"
            )
        if meta["kind"] == "number":
            try:
                return float(value)
            except (TypeError, ValueError):
                raise RegistryError(
                    f"수치 센서 값이 숫자가 아닙니다: {entity_id}={value!r}",
                    field="value",
                ) from None
        return self._coerce_bool(value)

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
            # None 이면 float() 이 TypeError 를 던져 아래에서 잡힌다(기존 동작).
            raw_min: Any = meta.get("min")
            raw_max: Any = meta.get("max")
            try:
                mn = float(raw_min)
                mx = float(raw_max)
            except (TypeError, ValueError):
                raise RegistryError(
                    "수치 센서는 최소·최대값(숫자)이 필요합니다.", field="min"
                ) from None
            # float("nan")·float("inf") 는 위 변환을 통과하고, NaN 은 아래 `>=` 비교도
            # 항상 False 라 빠져나간다. 받아 두면 /api/catalog 가 NaN/Infinity 를 내보내
            # (표준 JSON 아님) 보드가 카탈로그를 못 읽는다 — 유한값만 받는다.
            if not (math.isfinite(mn) and math.isfinite(mx)):
                raise RegistryError(
                    "최소·최대값은 유한한 숫자여야 합니다(nan·inf 불가).", field="min"
                )
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
        # JSON 에서 그대로 온 값이라 선언 타입을 믿지 않는다. 예전엔 `for a in 5` 가
        # TypeError, `"x".get` 이 AttributeError 로 터져 RegistryError 가 아니었고,
        # /api/registry/device 는 응답 없이 연결이 끊겼다.
        if not isinstance(actions, list) or not all(isinstance(a, dict) for a in actions):
            raise RegistryError(
                "행동 목록은 객체({action, tier})의 배열이어야 합니다.", field="actions"
            )

        if not capability:
            raise RegistryError("capability 를 입력하세요.", field="capability")
        _validate_name(capability, "capability", "capability")
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
            _validate_name(action, "action", "행동 이름")
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
        _validate_name(capability, "capability", "capability")
        if not action:
            raise RegistryError("행동 이름을 입력하세요.", field="action")
        _validate_name(action, "action", "행동 이름")
        new_tier = self._resolve_action_tier(capability, action, tier)
        cap_map = self._raw.setdefault("capabilities", {}).setdefault(capability, {})
        spec = cap_map.setdefault(action, {})
        spec["tier"] = new_tier.name.lower()
        if isinstance(directional, dict):
            spec["directional"] = directional
        return {
            "capability": capability, "action": action, "tier": new_tier.name.lower()
        }

    def add_rule(self, spec: dict[str, Any]) -> dict[str, Any]:
        """런타임 임계 규칙 1개를 등록한다("센서 X가 V를 넘으면 행동 Y 제안").

        payload: ``{name?, match?, state?, propose:{entity_id|target, capability,
        action, tier?}}``. 검증(전부 실패 시 **한글** 사유 → 서버가 HTTP 400):
          * 대상 기기(propose.entity_id)는 등록된 기기여야 하고, 선언 capability 가
            그 기기의 실제 capability 와 일치해야 한다(미등록/불일치 = 거부).
          * 상태 술어 op 는 닫힌 어휘(eq/ne/lt/le/gt/ge)여야 하고, 수치 센서 임계값은
            float 로 강제한다(문자열 임계 = 조용히 죽는 규칙 방지).
          * (capability, action)은 파생 허용목록 안이어야 한다(ember·안전 액추에이터·
            여는 방향 거부 — ``parse_rule`` 강제, 영어 사유는 한글로 매핑).
          * 규칙 이름(rule_id)은 중복 불가(origin 토큰 충돌 방지).
        ``propose.tier`` 는 UI 표시용이며 검증엔 쓰지 않는다.
        """
        if not isinstance(spec, dict):
            raise RegistryError("규칙 정의가 올바르지 않습니다.")
        if spec.get("propose") is not None and not isinstance(spec["propose"], dict):
            raise RegistryError(
                "행동(propose) 형식이 올바르지 않습니다 (객체여야 합니다)", field="propose"
            )
        if spec.get("state") is not None and not isinstance(spec["state"], list):
            raise RegistryError(
                "조건(state) 형식이 올바르지 않습니다 (목록이어야 합니다)", field="state"
            )

        # ── propose 정규화 + 대상 기기 검증(등록·capability 일치) ──────────
        prop = dict(spec.get("propose") or {})
        if not prop.get("target") and not prop.get("target_entity") and prop.get("entity_id"):
            prop["target"] = prop.get("entity_id")
        target = str(prop.get("target") or prop.get("target_entity") or "").strip()
        capability = str(prop.get("capability") or "").strip().lower()
        action = str(prop.get("action") or "").strip().lower()
        if not capability or not action or not target:
            raise RegistryError(
                "행동 정의에 capability·action·대상 기기가 모두 필요합니다.",
                field="propose",
            )
        if not self.known_device(target):
            raise RegistryError(f"등록되지 않은 기기입니다: {target}", field="propose")
        dev_cap = (self._device_capability(target) or "").strip().lower()
        if dev_cap and dev_cap != capability:
            raise RegistryError(
                f"기기 capability 불일치: '{target}' 는 '{dev_cap}' 인데 "
                f"규칙은 '{capability}' 를 선언했습니다.",
                field="propose",
            )

        # ── 상태 술어: op 어휘 검사 + 수치 센서 임계 float 강제 ────────────
        coerced_state: list[dict[str, Any]] = []
        for p in spec.get("state") or []:
            if not isinstance(p, dict):
                raise RegistryError("조건(state) 형식이 올바르지 않습니다.", field="state")
            p = dict(p)
            op = str(p.get("op") or "").strip().lower()
            if op and op not in ALLOWED_PREDICATE_OPS:
                raise RegistryError(
                    f"지원하지 않는 연산자입니다: '{op}' (가능: eq/ne/lt/le/gt/ge)",
                    field="state",
                )
            meta = self._sensors.get(str(p.get("entity") or ""))
            if meta is not None and meta.get("kind") == "number" and "value" in p:
                try:
                    p["value"] = float(p["value"])
                except (TypeError, ValueError):
                    raise RegistryError(
                        f"수치 조건 값이 숫자가 아닙니다: {p.get('value')!r}",
                        field="state",
                    ) from None
            coerced_state.append(p)

        raw = {
            "id": spec.get("name") or spec.get("id"),
            "match": spec.get("match") or {},
            "state": coerced_state,
            "propose": prop,
            "ttl_minutes": spec.get("ttl_minutes", 15),
            "description": spec.get("description", ""),
        }
        try:
            rule = parse_rule(raw, len(self._rules), allowed=self.proactive_allowed())
        except ProactiveRuleError as exc:
            raise RegistryError(
                self._korean_rule_error(str(exc), capability, action), field="propose"
            ) from None

        # ── 이름(rule_id) 중복 거부(origin 토큰 충돌 방지) ────────────────
        if any(r.rule_id == rule.rule_id for r in self._rules):
            raise RegistryError(
                f"이미 존재하는 규칙 이름입니다: {rule.rule_id}", field="name"
            )
        self._rules.append(rule)
        return self._rule_view(rule)

    # ------------------------------------------------------------------
    # 내부
    # ------------------------------------------------------------------
    def _device_capability(self, entity_id: str) -> str | None:
        """등록된 기기의 실제 capability(없으면 None)."""
        idx = self._device_index(entity_id)
        if idx is None:
            return None
        cap = (self._raw.get("devices", []) or [])[idx].get("capability")
        return None if cap is None else str(cap)

    @staticmethod
    def _korean_rule_error(msg: str, capability: str, action: str) -> str:
        """``parse_rule`` 의 영어 ``ProactiveRuleError`` 를 한글 사유로 매핑한다.

        데모/UI 노출 에러는 전량 한글이어야 한다(리뷰 지적). 실패 종류를 영어 메시지
        토큰으로 식별해 한글로 바꾸되, 식별 안 되는 잔여 케이스는 한글 접두사 +
        원문(기술 토큰)으로 폴백한다.
        """
        if "proactive-allowed" in msg:
            return (
                f"허용되지 않은 행동입니다(능동 제안 불가): ('{capability}', '{action}') — "
                "등급이 숨결 이하인 비-안전 행동만 규칙에 쓸 수 있습니다"
            )
        if "closed vocabulary" in msg:
            return "지원하지 않는 연산자입니다 (가능: eq/ne/lt/le/gt/ge)"
        if "disallowed match keys" in msg:
            return "규칙 조건 키가 올바르지 않습니다 (가능: 시간대·재실)"
        if "scalar" in msg or "mapping" in msg:
            return "규칙 값 형식이 올바르지 않습니다 (단일 스칼라만 허용)"
        if "at least one" in msg:
            return "규칙에는 조건이 최소 하나 필요합니다 (시간대·재실 또는 센서 상태)"
        if "missing 'entity'" in msg:
            return "조건에 센서(entity)가 필요합니다"
        if "ttl" in msg.lower():
            return "TTL(분)은 양의 정수여야 합니다"
        if "unknown keys" in msg:
            return "규칙 조건에 알 수 없는 키가 있습니다 (가능: entity/op/value)"
        if "must be a list" in msg:
            return "조건(state) 형식이 올바르지 않습니다 (목록이어야 합니다)"
        return "규칙을 등록할 수 없습니다 (정의 형식을 확인해 주세요)"

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
