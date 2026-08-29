"""데모 서버 통합 흐름(P5) — 런타임 등록 기기 상태가 simulate 를 통해 흐르는지.

계약 결함 회귀 방지: 런타임 등록 기기(vacuum.robot)의 on/off 상태를
``POST /api/simulate`` 페이로드로 주입할 수 있어야 하고, 빈집이면 제네릭 엔진의
빈집→off류 일반화가 그 기기의 off류 행동(stop)을 후보로 낸다. 이전엔 상태 강제변환이
센서 메타에만 열려 있어 등록 기기 상태가 주입 불가 → 일반화가 데모에서 도달 불가였다.

네트워크 없음(llm_enabled=false → FakeLLM). 모듈 전역 ``_REGISTRY`` 는 픽스처가 격리.
"""

from __future__ import annotations

import asyncio

import pytest

import liot.demo_server as ds
from liot.demo_server import _REGISTRY, _run
from liot.registry import RegistryError


@pytest.fixture(autouse=True)
def _reset_demo_registry():
    """모듈 전역 데모 레지스트리·선호 셀맵을 매 테스트 전후로 초기화(격리)."""
    _REGISTRY.reset()
    ds._PREF_CELLS.clear()
    yield
    _REGISTRY.reset()
    ds._PREF_CELLS.clear()


# 빈집(부재) + 거실 조명 on → vacant-light-off 규칙이 light.turn_off 를 제안한다.
# 선호 임베딩 셀 = "evening|vacant|light:turn_off"(이력 없으면 중립 0.5).
_VACANT_LIGHT_BODY = {
    "mode": "tick",
    "config": {"time": "evening", "llm_enabled": False},
    "sensors": {"binary_sensor.living_motion": "off", "light.living_room": "on"},
}


def _simulate(body: dict) -> dict:
    return asyncio.run(_run(body))


def _keys(result: dict) -> set[tuple[str, str, str]]:
    return {
        (c["capability"], c["action"], c["entity_id"])
        for c in result.get("candidates", [])
    }


def test_registered_device_state_reaches_vacancy_generalization() -> None:
    # 런타임 등록: 로봇청소기(start=breath, stop=ambient).
    _REGISTRY.add_device(
        entity_id="vacuum.robot", name="로봇청소기", capability="vacuum",
        category="appliance",
        actions=[{"action": "start", "tier": "breath"}, {"action": "stop", "tier": "ambient"}],
    )
    # 빈집(재실 off) + 조명 on(config vacant-light-off 가 tick 진입 게이트를 통과) +
    # 로봇청소기 on → 상태 주입이 되고, 제네릭 엔진이 vacuum.stop 을 후보로 낸다.
    body = {
        "mode": "tick",
        "config": {"time": "evening", "llm_enabled": False},
        "sensors": {
            "binary_sensor.living_motion": "off",
            "light.living_room": "on",
            "vacuum.robot": "on",
        },
    }
    result = _simulate(body)
    assert result["ok"] is True, result
    keys = _keys(result)
    assert ("vacuum", "stop", "vacuum.robot") in keys  # 등록 기기 off류 일반화 도달
    assert ("light", "turn_off", "light.living_room") in keys  # 기존 동작 보존


def test_registered_device_state_unknown_entity_still_rejected() -> None:
    # 센서도 기기도 아닌 엔티티 값은 여전히 400(구조화 에러).
    body = {
        "mode": "tick",
        "config": {"time": "evening", "llm_enabled": False},
        "sensors": {"ghost.entity": "on"},
    }
    with pytest.raises(RegistryError):  # _reading → RegistryError (서버가 400 매핑)
        _simulate(body)


# ── P6: 폰 피드백 루프(선호 임베딩) ──────────────────────────────────────


def test_feedback_signal_raises_next_embedding_preference() -> None:
    # 1) 첫 실행: 이력 없음 → embedding.preference == 중립(0.5).
    r1 = _simulate(dict(_VACANT_LIGHT_BODY))
    assert r1["ok"] is True and r1["silence"] is False, r1
    assert r1["embedding"]["preference"] == 0.5, r1["embedding"]
    cell = r1["embedding"]["cell"]

    # 2) 폰 '수락' 피드백 = /api/feedback 엔드포인트 코어(_apply_feedback).
    fb = ds._apply_feedback(cell, "accepted")
    assert fb["ok"] is True, fb
    assert fb["before"] == 0.5
    assert fb["after"] == pytest.approx(0.65, abs=1e-3), fb  # 0.5 + 0.3·(1.0−0.5)

    # 3) 다음 실행이 갱신된 셀을 반영 → embedding.preference 상승.
    r2 = _simulate(dict(_VACANT_LIGHT_BODY))
    assert r2["embedding"]["cell"] == cell
    assert r2["embedding"]["preference"] == pytest.approx(0.65, abs=1e-3)
    assert r2["embedding"]["preference"] > r1["embedding"]["preference"]


def test_feedback_reset_returns_embedding_to_neutral() -> None:
    r1 = _simulate(dict(_VACANT_LIGHT_BODY))
    ds._apply_feedback(r1["embedding"]["cell"], "accepted")
    assert ds._PREF_CELLS  # 셀맵 채워짐

    # 리셋(엔드포인트 reset 이 하는 관찰 효과와 동일: 세션 셀맵 비움).
    ds._PREF_CELLS.clear()
    r2 = _simulate(dict(_VACANT_LIGHT_BODY))
    assert r2["embedding"]["preference"] == 0.5  # 중립 복귀


def test_feedback_rejects_unknown_signal() -> None:
    out = ds._apply_feedback("evening|vacant|light:turn_off", "bogus")
    assert out["ok"] is False and out["field"] == "signal", out


def test_feedback_requires_cell() -> None:
    out = ds._apply_feedback("", "accepted")
    assert out["ok"] is False and out["field"] == "cell", out


# ── P7: 프리셋(등록 번들) 원자 적용 ──────────────────────────────────────

# 프리셋 ③ 「라이브 등록 시연」 = 센서 + 기기 + 규칙을 한 번에 등록하는 번들.
_LIVE_PRESET = {
    "reset": True,
    "sensors": [
        {"entity_id": "sensor.soil_moisture", "name": "토양 수분",
         "category": "moisture", "kind": "number", "unit": "%", "min": 0, "max": 100}
    ],
    "devices": [
        {"entity_id": "vacuum.robot", "name": "로봇청소기", "capability": "vacuum",
         "category": "appliance",
         "actions": [{"action": "start", "tier": "breath"},
                     {"action": "stop", "tier": "ambient"}]}
    ],
    "rules": [
        {"name": "soil-dry-clean", "match": {},
         "state": [{"entity": "sensor.soil_moisture", "op": "lt", "value": 30}],
         "propose": {"entity_id": "vacuum.robot", "capability": "vacuum",
                     "action": "start"}}
    ],
    "values": {"sensor.soil_moisture": 22, "binary_sensor.living_motion": True},
}


def _catalog_ids() -> tuple[set[str], set[str]]:
    """(센서 entity_id 집합, 기기 entity_id 집합) — 원자성 대조용 스냅샷."""
    view = ds._catalog_view()
    return (
        {s["entity_id"] for s in view["sensors"]},
        {d["entity_id"] for d in view["devices"]},
    )


def _seed_device() -> None:
    """비-seed 마커 기기 하나를 등록(롤백/리셋이 실제로 이걸 다루는지 확인용)."""
    _REGISTRY.add_device(
        entity_id="fan.study", name="서재 선풍기", capability="fan",
        category="appliance", actions=[{"action": "turn_on", "tier": "breath"}],
    )


def test_preset_registers_all_three_kinds() -> None:
    result, code = ds._apply_preset(_LIVE_PRESET)
    assert code == 200 and result["ok"] is True, result
    assert result["applied"] == {"sensors": 1, "devices": 1, "rules": 1}, result
    assert result["values"] == _LIVE_PRESET["values"], result
    sensors, devices = _catalog_ids()
    assert "sensor.soil_moisture" in sensors  # 센서 등록됨
    assert "vacuum.robot" in devices  # 기기 등록됨
    assert any(  # 규칙 등록됨(대상 = vacuum.robot)
        r["target"] == "vacuum.robot" for r in _REGISTRY.rules_meta()
    ), _REGISTRY.rules_meta()


def test_preset_apply_then_simulate_surfaces_registered_candidate() -> None:
    # 프리셋 적용 → 등록한 행동(vacuum.start)이 다음 simulate 후보로 등장(일반화 도달).
    result, code = ds._apply_preset(_LIVE_PRESET)
    assert code == 200 and result["ok"] is True, result
    body = {
        "mode": "tick",
        "config": {"time": "day", "llm_enabled": False},
        "sensors": {"sensor.soil_moisture": 22, "binary_sensor.living_motion": "on"},
    }
    r = _simulate(body)
    assert r["ok"] is True, r
    assert ("vacuum", "start", "vacuum.robot") in _keys(r), _keys(r)


def test_preset_invalid_rule_rolls_back_atomically() -> None:
    # 비-seed 기기를 먼저 등록해 둔다 — reset:True 프리셋이 실패하면 이 상태로
    # 완전 복원돼야 한다(reset 조차 롤백 = 진짜 원자성).
    _seed_device()
    before_sensors, before_devices = _catalog_ids()
    before_rules = len(_REGISTRY.rules_meta())

    broken = {
        "reset": True,
        "sensors": [{"entity_id": "sensor.x", "name": "X", "category": "moisture",
                     "kind": "number", "min": 0, "max": 100}],
        "devices": [{"entity_id": "vacuum.robot", "name": "청소기",
                     "capability": "vacuum", "category": "appliance",
                     "actions": [{"action": "start", "tier": "breath"}]}],
        # lock.unlock 은 파생 허용목록 밖(여는 방향·안전 액추에이터) → 규칙 거부.
        "rules": [{"name": "bad", "match": {},
                   "state": [{"entity": "sensor.x", "op": "lt", "value": 5}],
                   "propose": {"entity_id": "lock.front_door", "capability": "lock",
                               "action": "unlock"}}],
        "values": {},
    }
    result, code = ds._apply_preset(broken)
    assert code == 400 and result["ok"] is False, result
    assert "규칙" in result["error"], result  # 어느 단계에서 막혔는지 한글 사유

    # 원자성: 센서·기기·규칙 전부 프리셋 이전과 동일(부분 등록 잔재도, reset 흔적도 없음).
    after_sensors, after_devices = _catalog_ids()
    assert after_sensors == before_sensors, (after_sensors, before_sensors)
    assert after_devices == before_devices, (after_devices, before_devices)
    assert "fan.study" in after_devices  # reset 도 롤백됨(비-seed 기기 생존)
    assert "sensor.x" not in after_sensors  # 부분 등록 잔재 없음
    assert len(_REGISTRY.rules_meta()) == before_rules


def test_preset_reset_flag_clears_then_preserves() -> None:
    # reset:True → 이전 등록 폐기(seed 복귀).
    _seed_device()
    assert "fan.study" in _catalog_ids()[1]
    result, code = ds._apply_preset(
        {"reset": True, "sensors": [], "devices": [], "rules": [], "values": {}}
    )
    assert code == 200 and result["ok"] is True, result
    assert "fan.study" not in _catalog_ids()[1]  # reset 이 이전 등록을 지움

    # reset:False → 이전 등록 보존(추가만).
    _seed_device()
    result2, code2 = ds._apply_preset(
        {"reset": False, "sensors": [], "devices": [], "rules": [], "values": {}}
    )
    assert code2 == 200 and result2["ok"] is True, result2
    assert "fan.study" in _catalog_ids()[1]  # reset:False 는 보존


# ── 프리셋: RegistryError 가 아닌 실패에도 상태가 살아남는다 ──────────────
def test_preset_non_registry_error_still_rolls_back() -> None:
    """항목이 dict 가 아니면 아무것도 건드리기 전에 400 — 상태 파괴 없음.

    2026-08-29 코드리뷰가 찾은 결함: `_apply_preset` 은 `except RegistryError`
    만 두고 있었다. `{"reset": true, "sensors": [123]}` 을 보내면 reset 이 먼저
    실행된 뒤 `dict(123)` 에서 TypeError 가 났고, 그 예외는 except 를 그대로
    빠져나가 롤백도 응답도 없이 연결이 끊겼다 — 레지스트리와 선호 셀맵이 통째로
    날아간 채로. (실측: 센서 11 → 10, 선호맵 {'...': 0.65} → {})
    """
    _seed_device()
    ds._PREF_CELLS["evening|vacant|light:turn_off"] = 0.65
    before_sensors, before_devices = _catalog_ids()
    before_rules = len(_REGISTRY.rules_meta())

    for bad in ([123], ["문자열"], [None]):
        result, code = ds._apply_preset({"reset": True, "sensors": bad})
        assert code == 400 and result["ok"] is False, (bad, result)
        assert "객체여야" in result["error"], result

        after_sensors, after_devices = _catalog_ids()
        assert after_sensors == before_sensors, (bad, after_sensors)
        assert after_devices == before_devices, (bad, after_devices)
        assert "fan.study" in after_devices, f"{bad}: reset 이 롤백되지 않았다"
        assert len(_REGISTRY.rules_meta()) == before_rules
        assert ds._PREF_CELLS == {"evening|vacant|light:turn_off": 0.65}, (
            f"{bad}: 선호 셀맵이 복원되지 않았다"
        )


def test_preset_unexpected_exception_rolls_back(monkeypatch: pytest.MonkeyPatch) -> None:
    """검증을 통과한 뒤 터지는 예상 밖 예외도 반드시 롤백된다."""
    _seed_device()
    ds._PREF_CELLS["evening|vacant|light:turn_off"] = 0.65
    before_sensors, before_devices = _catalog_ids()

    def boom(**_kw):
        raise TypeError("레지스트리 내부에서 예상 못 한 실패")

    monkeypatch.setattr(_REGISTRY, "add_device", boom)
    result, code = ds._apply_preset(
        {
            "reset": True,
            "devices": [{"entity_id": "fan.x", "name": "팬", "capability": "fan",
                         "category": "appliance",
                         "actions": [{"action": "turn_on", "tier": "ambient"}]}],
        }
    )
    assert code == 400 and result["ok"] is False, result
    assert "TypeError" in result["error"], result  # 데모: 실패를 화면에 그대로 노출

    assert _catalog_ids() == (before_sensors, before_devices)
    assert "fan.study" in _catalog_ids()[1]  # reset 롤백됨
    assert ds._PREF_CELLS == {"evening|vacant|light:turn_off": 0.65}
