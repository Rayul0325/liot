"""런타임 등록 레지스트리(P2) 검증.

검증 축:
  1) 시드 == 출하 카탈로그(risk_for 동치, categories == CATEGORIES 집합).
  2) add_sensor/add_device 라운드트립: 카탈로그·categories·sensors_meta 에 등장.
  3) 확장 전용: 재선언은 등급을 올리기만 — 낮추기는 RegistryError(양방향).
  4) 강제변환(coerce_value): 미등록 센서 거부 / bool→on-off / number→float.
  5) reset: 세션 등록분 폐기, 출하 상태 복원.
  6) 검증 실패 경로: 알 수 없는 등급·중복 entity_id·수치 범위·자동 생성.
"""

from __future__ import annotations

import pytest

from liot.core.risk import RiskTier
from liot.layers.l4_execution.device_catalog import load_device_catalog
from liot.registry import RegistryError, RuntimeRegistry


@pytest.fixture
def reg() -> RuntimeRegistry:
    return RuntimeRegistry()


# ── 1) 시드 == 출하 카탈로그 ────────────────────────────────────────────────
def test_seed_matches_shipped_catalog(reg: RuntimeRegistry) -> None:
    shipped = load_device_catalog()
    seeded = reg.to_catalog()
    # 방향 비대칭 포함 대표 쌍이 그대로 일치해야 한다.
    for cap, action in [
        ("lock", "lock"), ("lock", "unlock"),
        ("valve", "open"), ("valve", "close"),
        ("cover", "open"), ("cover", "close"),
        ("light", "turn_on"), ("notify", "send"),
    ]:
        assert seeded.risk_for(cap, action) == shipped.risk_for(cap, action)


def test_seed_categories_are_demo_house(reg: RuntimeRegistry) -> None:
    cats = reg.categories()
    assert cats == {
        "sensor.living_temperature": "temperature",
        "sensor.living_humidity": "humidity",
        "binary_sensor.living_motion": "motion",
        "light.living_room": "light",
        "climate.living_room": "climate",
        "cover.living_room": "curtain",
        "binary_sensor.kitchen_gas": "gas",
        "binary_sensor.hall_smoke": "smoke",
        "binary_sensor.bath_leak": "leak",
        "lock.front_door": "lock",
        # 2026-10-19 의도적 갱신: 기획 4장면의 기기(공기청정기·책상 조명).
        "fan.living_purifier": "climate",
        "light.desk": "light",
    }


def test_sensors_meta_kinds(reg: RuntimeRegistry) -> None:
    by_id = {m["entity_id"]: m for m in reg.sensors_meta()}
    temp = by_id["sensor.living_temperature"]
    assert temp["kind"] == "number" and temp["unit"] == "°C"
    assert temp["min"] == -10 and temp["max"] == 40
    assert by_id["binary_sensor.kitchen_gas"]["kind"] == "bool"


# ── 2) add_sensor/add_device 라운드트립 ───────────────────────────────────────
def test_add_sensor_roundtrip(reg: RuntimeRegistry) -> None:
    rec = reg.add_sensor(
        {
            "entity_id": "sensor.soil_moisture", "name": "토양 수분",
            "category": "moisture", "kind": "number", "unit": "%",
            "min": 0, "max": 100,
        }
    )
    assert rec["entity_id"] == "sensor.soil_moisture"
    assert reg.known_sensor("sensor.soil_moisture")
    assert reg.categories()["sensor.soil_moisture"] == "moisture"
    ids = {m["entity_id"] for m in reg.sensors_meta()}
    assert "sensor.soil_moisture" in ids


def test_add_device_roundtrip(reg: RuntimeRegistry) -> None:
    reg.add_device(
        entity_id="vacuum.robot", name="로봇청소기", capability="vacuum",
        category="appliance", actions=[{"action": "start", "tier": "breath"}],
    )
    catalog = reg.to_catalog()
    assert catalog.risk_for("vacuum", "start") == RiskTier.BREATH
    assert reg.categories()["vacuum.robot"] == "appliance"
    listed = [
        a for a in catalog.available_actions()
        if a["entity_id"] == "vacuum.robot" and a["action"] == "start"
    ]
    assert listed and listed[0]["tier"] == "breath"


def test_add_device_auto_entity_id(reg: RuntimeRegistry) -> None:
    rec = reg.add_device(
        entity_id="", name="", capability="fan", category="appliance",
        actions=[{"action": "turn_on", "tier": "ambient"}],
    )
    assert rec["entity_id"].startswith("fan.")
    # 이름 미지정 → entity_id 로 폴백.
    assert rec["name"] == rec["entity_id"]


# ── 3) 확장 전용(양방향) ──────────────────────────────────────────────────────
def test_extend_only_lower_rejected_keeps_ember(reg: RuntimeRegistry) -> None:
    # lock.unlock 은 출하에서 ember. 이를 breath 로 낮추려는 시도는 거부되고,
    # 등급은 ember 그대로 유지된다.
    assert reg.to_catalog().risk_for("lock", "unlock") == RiskTier.EMBER
    with pytest.raises(RegistryError) as exc:
        reg.add_action("lock", "unlock", "breath")
    assert exc.value.field == "tier"
    assert reg.to_catalog().risk_for("lock", "unlock") == RiskTier.EMBER


def test_extend_only_raise_applies(reg: RuntimeRegistry) -> None:
    # cover.close 는 출하에서 breath. dusk 로 올리는 재선언은 적용된다.
    assert reg.to_catalog().risk_for("cover", "close") == RiskTier.BREATH
    out = reg.add_action("cover", "close", "dusk")
    assert out["tier"] == "dusk"
    assert reg.to_catalog().risk_for("cover", "close") == RiskTier.DUSK


def test_extend_only_equal_is_noop(reg: RuntimeRegistry) -> None:
    out = reg.add_action("lock", "unlock", "ember")
    assert out["tier"] == "ember"
    assert reg.to_catalog().risk_for("lock", "unlock") == RiskTier.EMBER


def test_add_device_lowering_action_is_atomic(reg: RuntimeRegistry) -> None:
    # 한 행동이라도 확장 전용을 위반하면 기기 전체가 커밋되지 않아야 한다.
    with pytest.raises(RegistryError):
        reg.add_device(
            entity_id="lock.side_door", name="옆문", capability="lock",
            category="lock", actions=[{"action": "unlock", "tier": "breath"}],
        )
    assert reg._device_index("lock.side_door") is None
    assert reg.to_catalog().risk_for("lock", "unlock") == RiskTier.EMBER


# ── 4) 강제변환(coerce_value) ─────────────────────────────────────────────────
def test_coerce_unknown_sensor_raises(reg: RuntimeRegistry) -> None:
    with pytest.raises(RegistryError) as exc:
        reg.coerce_value("sensor.does_not_exist", 5)
    assert exc.value.field == "entity_id"


def test_coerce_number_to_float(reg: RuntimeRegistry) -> None:
    assert reg.coerce_value("sensor.living_temperature", 22) == 22.0
    assert isinstance(reg.coerce_value("sensor.living_temperature", 22), float)


def test_coerce_bool_to_on_off(reg: RuntimeRegistry) -> None:
    assert reg.coerce_value("binary_sensor.kitchen_gas", True) == "on"
    assert reg.coerce_value("binary_sensor.kitchen_gas", False) == "off"
    # 숫자 진리값도 on/off 로.
    assert reg.coerce_value("binary_sensor.kitchen_gas", 1) == "on"
    assert reg.coerce_value("binary_sensor.kitchen_gas", 0) == "off"
    # 등록된 새 number 센서는 float 로.
    reg.add_sensor(
        {
            "entity_id": "sensor.soil_moisture", "name": "토양 수분",
            "category": "moisture", "kind": "number", "unit": "%",
            "min": 0, "max": 100,
        }
    )
    assert reg.coerce_value("sensor.soil_moisture", 30) == 30.0


def test_coerce_registered_device_state_bool(reg: RuntimeRegistry) -> None:
    # 런타임 등록 기기(센서 아님)의 상태도 on/off 로 주입 가능해야 한다 —
    # 빈집→off류 일반화가 데모/UI 에서 실제로 도달하려면 필수(계약 결함 수정).
    reg.add_device(
        entity_id="vacuum.robot", name="로봇청소기", capability="vacuum",
        category="appliance",
        actions=[{"action": "start", "tier": "breath"}, {"action": "stop", "tier": "ambient"}],
    )
    assert reg.known_device("vacuum.robot")
    assert reg.coerce_value("vacuum.robot", True) == "on"
    assert reg.coerce_value("vacuum.robot", False) == "off"
    assert reg.coerce_value("vacuum.robot", 1) == "on"
    # 센서도 기기도 아니면 여전히 거부.
    with pytest.raises(RegistryError) as exc:
        reg.coerce_value("nope.nothing", True)
    assert exc.value.field == "entity_id"


# ── 5) reset ─────────────────────────────────────────────────────────────────
def test_reset_restores_baked_state(reg: RuntimeRegistry) -> None:
    reg.add_sensor(
        {
            "entity_id": "sensor.soil_moisture", "name": "토양 수분",
            "category": "moisture", "kind": "number", "unit": "%",
            "min": 0, "max": 100,
        }
    )
    reg.add_device(
        entity_id="vacuum.robot", name="로봇청소기", capability="vacuum",
        category="appliance", actions=[{"action": "start", "tier": "breath"}],
    )
    reg.add_action("cover", "close", "dusk")  # 등급도 올려봄
    reg.reset()

    assert not reg.known_sensor("sensor.soil_moisture")
    assert reg._device_index("vacuum.robot") is None
    assert "vacuum.robot" not in reg.categories()
    catalog = reg.to_catalog()
    assert catalog.risk_for("vacuum", "start") == RiskTier.EMBER  # 미지 → default-deny
    assert catalog.risk_for("cover", "close") == RiskTier.BREATH  # 올린 등급도 원복
    assert reg.categories() == RuntimeRegistry().categories()


# ── 6) 검증 실패 경로 ────────────────────────────────────────────────────────
def test_add_action_unknown_tier(reg: RuntimeRegistry) -> None:
    with pytest.raises(RegistryError) as exc:
        reg.add_action("vacuum", "start", "not_a_tier")
    assert exc.value.field == "tier"


def test_add_sensor_duplicate_entity_id(reg: RuntimeRegistry) -> None:
    with pytest.raises(RegistryError) as exc:
        reg.add_sensor(
            {
                "entity_id": "sensor.living_temperature", "name": "중복",
                "category": "temperature", "kind": "bool",
            }
        )
    assert exc.value.field == "entity_id"


def test_add_sensor_number_bad_range(reg: RuntimeRegistry) -> None:
    with pytest.raises(RegistryError) as exc:
        reg.add_sensor(
            {
                "entity_id": "sensor.bad", "name": "잘못된 범위",
                "category": "misc", "kind": "number", "unit": "x",
                "min": 100, "max": 0,
            }
        )
    assert exc.value.field == "min"


@pytest.mark.parametrize(
    "mn,mx",
    [("nan", "nan"), (0, "inf"), ("-inf", 100), (float("nan"), 100), (0, float("inf"))],
)
def test_add_sensor_number_non_finite_range_rejected(reg: RuntimeRegistry, mn, mx) -> None:
    """nan·inf 경계는 거부 — 받아 주면 /api/catalog 가 NaN/Infinity(비표준 JSON)를 낸다.

    `float("nan") >= x` 는 항상 False 라 기존 `mn >= mx` 검사를 그대로 통과했다.
    """
    with pytest.raises(RegistryError) as exc:
        reg.add_sensor(
            {
                "entity_id": "sensor.nonfinite", "name": "비유한 범위",
                "category": "misc", "kind": "number", "min": mn, "max": mx,
            }
        )
    assert exc.value.field == "min"
    assert all(s["entity_id"] != "sensor.nonfinite" for s in reg.sensors_meta())


@pytest.mark.parametrize("actions", [5, "turn_on", ["x"], [{"action": "start"}, 7], {"a": 1}])
def test_add_device_actions_must_be_list_of_objects(reg: RuntimeRegistry, actions) -> None:
    """행동 목록이 '객체의 리스트'가 아니면 RegistryError(→400).

    예전엔 `for a in 5` 가 TypeError, `"x".get` 이 AttributeError 로 터져
    `except RegistryError` 를 빠져나갔고 /api/registry/device 는 응답 없이 끊겼다.
    """
    with pytest.raises(RegistryError) as exc:
        reg.add_device(
            entity_id="vacuum.robot", name="로봇청소기", capability="vacuum",
            category="appliance", actions=actions,
        )
    assert exc.value.field == "actions"
    assert "vacuum.robot" not in {a["entity_id"] for a in reg.to_catalog().available_actions()}


def test_add_sensor_bad_kind(reg: RuntimeRegistry) -> None:
    with pytest.raises(RegistryError) as exc:
        reg.add_sensor(
            {"entity_id": "sensor.x", "name": "x", "category": "misc", "kind": "enum"}
        )
    assert exc.value.field == "kind"


def test_add_device_requires_action(reg: RuntimeRegistry) -> None:
    with pytest.raises(RegistryError) as exc:
        reg.add_device(
            entity_id="vacuum.robot", name="로봇청소기", capability="vacuum",
            category="appliance", actions=[],
        )
    assert exc.value.field == "actions"


def test_registry_instances_are_isolated() -> None:
    # 시드 raw 캐시가 딥카피되므로 한 인스턴스의 등록이 다른 인스턴스에 새지 않는다.
    a = RuntimeRegistry()
    b = RuntimeRegistry()
    a.add_device(
        entity_id="vacuum.robot", name="로봇청소기", capability="vacuum",
        category="appliance", actions=[{"action": "start", "tier": "breath"}],
    )
    assert b.to_catalog().risk_for("vacuum", "start") == RiskTier.EMBER


# ── 7) 안전 결합: safety_critical + floor 확장 전용(등록이 안전을 못 낮춤) ────────
def test_add_sensor_safety_critical_registers_category(reg: RuntimeRegistry) -> None:
    from liot.core.risk import is_safety_category

    assert not is_safety_category("radon")
    reg.add_sensor(
        {
            "entity_id": "sensor.radon", "name": "라돈", "category": "radon",
            "kind": "number", "unit": "Bq/m³", "min": 0, "max": 1000,
            "safety_critical": True,
        }
    )
    assert is_safety_category("radon")


def test_add_device_floor_lower_than_baked_rejected(reg: RuntimeRegistry) -> None:
    from liot.layers.l3_judgment.guardrail import category_floor

    # climate 는 내장 하한 BREATH. floor=ambient 로 낮추려는 등록은 400(RegistryError).
    assert category_floor("climate") == RiskTier.BREATH
    with pytest.raises(RegistryError) as exc:
        reg.add_device(
            entity_id="climate.x", name="x", capability="climate",
            category="climate", actions=[{"action": "set", "tier": "breath"}],
            floor="ambient",
        )
    assert exc.value.field == "floor"
    assert category_floor("climate") == RiskTier.BREATH  # 내장 하한 불변
    assert reg._device_index("climate.x") is None  # 원자성: 기기도 미커밋


def test_add_device_floor_raise_applies(reg: RuntimeRegistry) -> None:
    from liot.layers.l3_judgment.guardrail import category_floor

    # climate 내장 BREATH → dusk 상향은 적용된다(확장 전용은 올리기만).
    reg.add_device(
        entity_id="climate.upgraded", name="냉난방", capability="climate",
        category="climate", actions=[{"action": "set", "tier": "breath"}],
        floor="dusk",
    )
    assert category_floor("climate") == RiskTier.DUSK


def test_add_sensor_floor_unknown_category_adds_then_lower_rejected(
    reg: RuntimeRegistry,
) -> None:
    from liot.layers.l3_judgment.guardrail import category_floor

    # 미지 카테고리 'aq' 에 dusk 하한을 신규 설정(ADD).
    reg.add_sensor(
        {
            "entity_id": "sensor.aq", "name": "공기질", "category": "aq",
            "kind": "number", "unit": "idx", "min": 0, "max": 500, "floor": "dusk",
        }
    )
    assert category_floor("aq") == RiskTier.DUSK
    # 이후 breath 로 낮추려는 재등록은 거부(확장 전용).
    with pytest.raises(RegistryError) as exc:
        reg.add_device(
            entity_id="fan.aq", name="환기팬", capability="fan",
            category="aq", actions=[{"action": "turn_on", "tier": "ambient"}],
            floor="breath",
        )
    assert exc.value.field == "floor"
    assert category_floor("aq") == RiskTier.DUSK


def test_add_device_bad_floor_tier_rejected(reg: RuntimeRegistry) -> None:
    with pytest.raises(RegistryError) as exc:
        reg.add_device(
            entity_id="fan.x", name="팬", capability="fan", category="appliance",
            actions=[{"action": "turn_on", "tier": "ambient"}], floor="not_a_tier",
        )
    assert exc.value.field == "floor"


# ── 8) 런타임 임계 규칙(P5): 등록·병합·안전 거부 ──────────────────────────────
def _register_vacuum(reg: RuntimeRegistry) -> None:
    reg.add_sensor(
        {
            "entity_id": "sensor.soil_moisture", "name": "토양 수분",
            "category": "moisture", "kind": "number", "unit": "%",
            "min": 0, "max": 100,
        }
    )
    reg.add_device(
        entity_id="vacuum.robot", name="로봇청소기", capability="vacuum",
        category="appliance",
        actions=[{"action": "start", "tier": "breath"}, {"action": "stop", "tier": "ambient"}],
    )


def test_add_rule_roundtrip(reg: RuntimeRegistry) -> None:
    _register_vacuum(reg)
    rec = reg.add_rule(
        {
            "name": "토양 건조 청소",
            "state": [{"entity": "sensor.soil_moisture", "op": "lt", "value": 30}],
            "propose": {"entity_id": "vacuum.robot", "capability": "vacuum", "action": "start"},
        }
    )
    assert rec["capability"] == "vacuum" and rec["action"] == "start"
    assert rec["target"] == "vacuum.robot"
    # rules_meta 에 요약이 등장하고, 병합 룰셋에 규칙이 흐른다.
    ids = {r["id"] for r in reg.rules_meta()}
    assert "토양 건조 청소" in ids
    rs = reg.proactive_ruleset()
    assert any(
        r.capability == "vacuum" and r.action == "start" and r.target_entity == "vacuum.robot"
        for r in rs.rules
    )


def test_add_rule_maps_target_alias(reg: RuntimeRegistry) -> None:
    # propose.target 로 줘도 동작(entity_id 별칭과 등가).
    _register_vacuum(reg)
    rec = reg.add_rule(
        {
            "state": [{"entity": "sensor.soil_moisture", "op": "le", "value": 20}],
            "propose": {"target": "vacuum.robot", "capability": "vacuum", "action": "stop"},
        }
    )
    assert rec["action"] == "stop" and rec["target"] == "vacuum.robot"


def test_add_rule_ember_lock_action_rejected(reg: RuntimeRegistry) -> None:
    # 안전/여는 방향(lock.unlock)은 파생 허용목록 밖 → 등록 자체가 400(RegistryError).
    with pytest.raises(RegistryError) as exc:
        reg.add_rule(
            {
                "match": {"occupancy": "vacant"},
                "propose": {"entity_id": "lock.front_door", "capability": "lock", "action": "unlock"},
            }
        )
    assert exc.value.field == "propose"
    assert "허용되지 않은 행동" in exc.value.message  # 한글 사유(영어 원문 아님)
    assert not reg.rules_meta()  # 미커밋


def test_add_rule_ember_registered_action_rejected(reg: RuntimeRegistry) -> None:
    # ember 로 등록한 새 행동은 상한(breath)을 넘어 허용목록 밖 → 규칙 거부.
    reg.add_device(
        entity_id="siren.attic", name="다락 사이렌", capability="siren",
        category="appliance", actions=[{"action": "blast", "tier": "ember"}],
    )
    with pytest.raises(RegistryError) as exc:
        reg.add_rule(
            {
                "match": {"occupancy": "vacant"},
                "propose": {"entity_id": "siren.attic", "capability": "siren", "action": "blast"},
            }
        )
    assert exc.value.field == "propose"


def test_proactive_ruleset_includes_config_baseline(reg: RuntimeRegistry) -> None:
    # 병합 룰셋은 config 베이스라인(vacant-light-off)을 반드시 포함한다 —
    # 출하 홈 능동성이 tick propose() 게이트를 계속 통과하도록.
    rs = reg.proactive_ruleset()
    assert any(r.rule_id == "vacant-light-off" for r in rs.rules)


def test_add_rule_reset_clears(reg: RuntimeRegistry) -> None:
    _register_vacuum(reg)
    reg.add_rule(
        {
            "state": [{"entity": "sensor.soil_moisture", "op": "lt", "value": 30}],
            "propose": {"entity_id": "vacuum.robot", "capability": "vacuum", "action": "start"},
        }
    )
    assert reg.rules_meta()
    reg.reset()
    assert not reg.rules_meta()


# ── 8b) 규칙 등록 강건화(리뷰 지적): 한글 사유 · 대상검증 · 수치강제 · 이름중복 ──
def test_add_rule_unregistered_target_rejected(reg: RuntimeRegistry) -> None:
    # BLOCKER 2: propose.entity_id 가 미등록 기기면 한글 400(허용목록 통과 무관).
    with pytest.raises(RegistryError) as exc:
        reg.add_rule(
            {
                "match": {"occupancy": "vacant"},
                "propose": {"entity_id": "ghost.unknown", "capability": "ghost", "action": "poke"},
            }
        )
    assert exc.value.field == "propose"
    assert "등록되지 않은 기기입니다" in exc.value.message
    assert "ghost.unknown" in exc.value.message


def test_add_rule_capability_mismatch_rejected(reg: RuntimeRegistry) -> None:
    # BLOCKER 2: 대상 기기의 실제 capability 와 규칙 선언 capability 가 다르면 거부.
    _register_vacuum(reg)  # vacuum.robot 의 실제 capability = vacuum
    with pytest.raises(RegistryError) as exc:
        reg.add_rule(
            {
                "state": [{"entity": "sensor.soil_moisture", "op": "lt", "value": 30}],
                "propose": {"entity_id": "vacuum.robot", "capability": "light", "action": "turn_off"},
            }
        )
    assert exc.value.field == "propose"
    assert "capability 불일치" in exc.value.message


def test_add_rule_unsupported_op_rejected(reg: RuntimeRegistry) -> None:
    # BLOCKER 1: 닫힌 어휘 밖 연산자는 한글 사유(영어 아님) + 해당 op 노출.
    _register_vacuum(reg)
    with pytest.raises(RegistryError) as exc:
        reg.add_rule(
            {
                "state": [{"entity": "sensor.soil_moisture", "op": "regex", "value": 30}],
                "propose": {"entity_id": "vacuum.robot", "capability": "vacuum", "action": "start"},
            }
        )
    assert exc.value.field == "state"
    assert "지원하지 않는 연산자입니다" in exc.value.message
    assert "regex" in exc.value.message


def test_add_rule_numeric_threshold_string_coerced(reg: RuntimeRegistry) -> None:
    # HARDENING 3: 수치 센서 임계 "30"(문자열)은 float 로 강제 — 조용히 죽는 규칙 방지.
    _register_vacuum(reg)
    rec = reg.add_rule(
        {
            "name": "문자열 임계",
            "state": [{"entity": "sensor.soil_moisture", "op": "lt", "value": "30"}],
            "propose": {"entity_id": "vacuum.robot", "capability": "vacuum", "action": "start"},
        }
    )
    val = rec["state"][0]["value"]
    assert val == 30.0 and isinstance(val, float)
    rule = [r for r in reg.proactive_ruleset().rules if r.rule_id == "문자열 임계"][0]
    assert rule.state[0].value == 30.0  # 병합 룰셋에도 float 로 흐른다


def test_add_rule_numeric_threshold_uncoercible_rejected(reg: RuntimeRegistry) -> None:
    # HARDENING 3: 수치 센서 임계가 숫자로 강제 불가면 한글 400(조용히 삼키지 않음).
    _register_vacuum(reg)
    with pytest.raises(RegistryError) as exc:
        reg.add_rule(
            {
                "state": [{"entity": "sensor.soil_moisture", "op": "lt", "value": "축축함"}],
                "propose": {"entity_id": "vacuum.robot", "capability": "vacuum", "action": "start"},
            }
        )
    assert exc.value.field == "state"
    assert "숫자가 아닙니다" in exc.value.message


def test_add_rule_duplicate_name_rejected(reg: RuntimeRegistry) -> None:
    # HARDENING 4: 같은 규칙 이름 재등록은 한글 400(origin 토큰/rule_id 충돌 방지).
    _register_vacuum(reg)
    spec = {
        "name": "청소 규칙",
        "state": [{"entity": "sensor.soil_moisture", "op": "lt", "value": 30}],
        "propose": {"entity_id": "vacuum.robot", "capability": "vacuum", "action": "start"},
    }
    reg.add_rule(spec)
    with pytest.raises(RegistryError) as exc:
        reg.add_rule(dict(spec))
    assert exc.value.field == "name"
    assert "이미 존재하는 규칙 이름입니다" in exc.value.message


# ── capability·action 이름은 셀 키·UI 선택값의 구분자를 담을 수 없다 ─────
def test_action_name_with_separator_or_space_is_rejected() -> None:
    """"turn on" 같은 이름은 등록 시점에 거부된다.

    2026-09-13 리뷰 RISK: 등록은 공백을 받아 주는데, 그 행동으로 만들어지는 선호
    셀 키(`time_bucket|occupancy|capability:action`)는 공백을 허용하지 않아서,
    한참 뒤 그 셀의 피드백이 400 으로 막혔다(등록 시점엔 보이지 않는 어긋남).
    UI 의 행동 선택값도 `entity_id|capability|action` 으로 조립된다.
    """
    reg = RuntimeRegistry()
    for bad in ("turn on", "turn|on", "turn:on", "turn\ton"):
        with pytest.raises(RegistryError) as exc:
            reg.add_device(
                entity_id="vacuum.robot", name="로봇", capability="vacuum",
                category="vacuum", actions=[{"action": bad, "tier": "breath"}],
            )
        assert exc.value.field == "action", bad
    with pytest.raises(RegistryError) as exc:
        reg.add_device(
            entity_id="vacuum.robot", name="로봇", capability="vac uum",
            category="vacuum", actions=[{"action": "start", "tier": "breath"}],
        )
    assert exc.value.field == "capability"
    # add_action 경로도 같은 규칙.
    with pytest.raises(RegistryError) as exc:
        reg.add_action("vacuum", "deep clean", "breath")
    assert exc.value.field == "action"


def test_registered_action_produces_a_feedback_cell_the_server_accepts() -> None:
    """등록한 행동으로 만들어진 셀 키는 /api/feedback 검사를 통과한다(왕복 일치)."""
    from liot.core.contracts import CandidateAction
    from liot.layers.l2_context.feature_vector import cell_key

    reg = RuntimeRegistry()
    rec = reg.add_device(
        entity_id="vacuum.robot", name="로봇", capability="vacuum",
        category="vacuum", actions=[{"action": "start_clean", "tier": "breath"}],
    )
    candidate = CandidateAction(
        capability=rec["capability"], action=rec["actions"][0]["action"],
        params={}, entity_id=rec["entity_id"], category=rec["category"],
    )
    cell = cell_key("night", "vacant", candidate)
    from liot import demo_server

    assert demo_server._CELL_KEY_RE.fullmatch(cell), cell


def test_overlong_action_name_is_rejected() -> None:
    """이름 길이 상한 — 셀 키 상한(200자)과 어긋나지 않게 등록에서 막는다(리뷰 R-1 잔여)."""
    reg = RuntimeRegistry()
    with pytest.raises(RegistryError) as exc:
        reg.add_device(
            entity_id="vacuum.robot", name="로봇", capability="vacuum",
            category="vacuum", actions=[{"action": "a" * 120, "tier": "breath"}],
        )
    assert exc.value.field == "action"
    assert "64" in exc.value.message
