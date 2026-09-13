"""데모 서버 통합 흐름(P5) — 런타임 등록 기기 상태가 simulate 를 통해 흐르는지.

계약 결함 회귀 방지: 런타임 등록 기기(vacuum.robot)의 on/off 상태를
``POST /api/simulate`` 페이로드로 주입할 수 있어야 하고, 빈집이면 제네릭 엔진의
빈집→off류 일반화가 그 기기의 off류 행동(stop)을 후보로 낸다. 이전엔 상태 강제변환이
센서 메타에만 열려 있어 등록 기기 상태가 주입 불가 → 일반화가 데모에서 도달 불가였다.

네트워크 없음(llm_enabled=false → FakeLLM). 데모 모듈 전역(``_REGISTRY``·선호 셀맵·
활성 지시)은 conftest 의 ``demo_state_reset`` 픽스처가 매 테스트 전후로 격리한다.
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

import liot.demo_server as ds
from liot.demo_server import _REGISTRY, _run
from liot.registry import RegistryError

pytestmark = pytest.mark.usefixtures("demo_state_reset")


# 빈집(부재) + 거실 조명 on → vacant-light-off 규칙이 light.turn_off 를 제안한다.
# 선호 임베딩 셀 = "evening|vacant|light:turn_off"(이력 없으면 중립 0.5).
_VACANT_LIGHT_BODY = {
    "mode": "tick",
    "config": {"time": "evening", "llm_enabled": False},
    "sensors": {"binary_sensor.living_motion": "off", "light.living_room": "on"},
}


def _simulate(body: dict[str, Any]) -> dict[str, Any]:
    return asyncio.run(_run(body))


def _keys(result: dict[str, Any]) -> set[tuple[str, str, str]]:
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


# ── 프리셋: 최상위 필드 타입이 틀려도 구조화 400 ─────────────────────────
@pytest.mark.parametrize(
    "bad,field",
    [
        ({"sensors": 123}, "sensors"),
        ({"devices": 5}, "devices"),
        ({"rules": 7}, "rules"),
        ({"values": [1, 2]}, "values"),
        ({"reset": True, "sensors": "abc"}, "sensors"),
        ({"reset": True, "values": "x"}, "values"),
    ],
)
def test_preset_top_level_wrong_type_is_400_without_touching_state(
    bad: dict[str, Any], field: str
) -> None:
    """최상위 sensors/devices/rules/values 의 타입이 틀리면 어느 필드인지 밝혀 400.

    예전엔 `list(123)`·`dict([1, 2])` 가 try 밖에서 터져 엔드포인트가 응답 없이
    끊겼다(2026-09-01 리뷰 R2-03). 문자열은 list("abc") 로 글자 배열이 돼 엉뚱한
    '항목' 사유를 냈다.
    """
    _seed_device()
    ds._PREF_CELLS["evening|vacant|light:turn_off"] = 0.65
    before = _catalog_ids()

    result, code = ds._apply_preset(bad)

    assert code == 400 and result["ok"] is False, result
    assert result["field"] == field, result
    assert field in result["error"], result
    assert _catalog_ids() == before
    assert ds._PREF_CELLS == {"evening|vacant|light:turn_off": 0.65}


# ── 피드백 셀 키: 서버가 만드는 형식만, 개수는 상한까지 ───────────────────
@pytest.mark.parametrize(
    "cell",
    [
        "not|a|real cell key!!",
        "evening|vacant",
        "evening|vacant|light",
        "noon|vacant|light:turn_off",
        "evening|home|light:turn_off",
        "evening|vacant|light:turn off",
        "evening|vacant|light:turn_off|extra",
        "evening|vacant|li:ght:turn_off",
        "evening|vacant|:turn_off",
        "evening|vacant|" + "a" * 100 + ":" + "b" * 100,  # 200자 초과
    ],
)
def test_feedback_rejects_malformed_cell(cell: str) -> None:
    """cell_key 형식(time_bucket|occupancy|capability:action)이 아니면 400.

    예전엔 비어 있지만 않으면 받아서 `_PREF_CELLS` 가 아무 키로나 끝없이 불었다
    (R1-24b, 실측 임의 키 500개 → 500칸). 그런 키는 /api/simulate 가 조회하지도 않는다.
    """
    out = ds._apply_feedback(cell, "accepted")
    assert out["ok"] is False and out["field"] == "cell", out
    assert ds._PREF_CELLS == {}


@pytest.mark.parametrize(
    "cell",
    [
        "evening|vacant|light:turn_off",
        "morning|occupied|climate:set_temperature",
        "night|unknown|vacuum:stop",
        "day|vacant|가습기:켜기",  # 한글 capability 도 등록 가능하다
    ],
)
def test_feedback_accepts_cell_key_shapes(cell: str) -> None:
    out = ds._apply_feedback(cell, "accepted")
    assert out["ok"] is True, out
    assert cell in ds._PREF_CELLS


def test_feedback_distinct_cells_are_capped(monkeypatch: pytest.MonkeyPatch) -> None:
    """서로 다른 셀 수는 상한까지 — 넘치는 새 키는 400, 기존 키 갱신은 계속 된다."""
    monkeypatch.setattr(ds, "MAX_PREF_CELLS", 3)
    cells = [f"evening|vacant|fan{i}:turn_on" for i in range(3)]
    for c in cells:
        assert ds._apply_feedback(c, "accepted")["ok"] is True

    over = ds._apply_feedback("evening|vacant|fan9:turn_on", "accepted")
    assert over["ok"] is False and over["field"] == "cell", over
    assert "3" in over["error"], over
    assert set(ds._PREF_CELLS) == set(cells)

    again = ds._apply_feedback(cells[0], "accepted")
    assert again["ok"] is True and again["after"] > again["before"], again


def test_feedback_cell_cap_default_is_bounded() -> None:
    assert 0 < ds.MAX_PREF_CELLS <= 4096


# ── 자연어 지시 until: 시뮬레이션 시각 기준으로 만료 ─────────────────────
# _VACANT_LIGHT_BODY 는 time=evening → 시뮬레이션 시각 2026-06-02T19:00:00+00:00.
# 오늘(실시간)은 그 날짜보다 뒤라, 벽시계로 판정하면 아래 until 은 전부 만료다.


@pytest.mark.parametrize(
    "until", ["오늘 밤", "2026-13-40T09:00:00", "5", 5, "0001-01-01T00:00:00+01:00"]
)
def test_directive_bad_until_is_400_and_not_stored(until: Any) -> None:
    result, code = ds._apply_directive({"text": "불 켜놔줘", "until": until})
    assert code == 400 and result["ok"] is False, result
    assert result["field"] == "until", result
    assert ds._DIRECTIVES.all() == [], "형식이 틀린 until 인데 지시가 저장됐다"


def test_directive_response_lists_stored_until_without_wall_clock_filter() -> None:
    """등록 응답의 active 는 실시간으로 거르지 않는다 — 만료 판정은 simulate 몫."""
    result, code = ds._apply_directive(
        {"text": "불 켜놔줘", "until": "2000-01-01T00:00:00+00:00"}
    )
    assert code == 200 and result["ok"] is True, result
    assert result["parsed"][0]["until"] == "2000-01-01T00:00:00+00:00", result
    assert [d["until"] for d in result["active"]] == ["2000-01-01T00:00:00+00:00"], result


def test_directive_expired_before_simulated_time_does_not_suppress_tick() -> None:
    _, code = ds._apply_directive({"text": "불 켜놔줘", "until": "2026-06-02T18:00:00+00:00"})
    assert code == 200

    r = _simulate(dict(_VACANT_LIGHT_BODY))

    assert r["silence"] is False, r  # 18:00 에 만료 → 19:00 틱의 소등 제안이 살아 있다
    assert (r["proposal"]["capability"], r["proposal"]["action"]) == ("light", "turn_off"), r
    assert "directives" not in r, r


def test_directive_valid_at_simulated_time_suppresses_tick() -> None:
    """until 이 시뮬레이션 시각 뒤면 유효 — 실시간으로 판정하면 만료로 보여 억제가 풀린다."""
    _, code = ds._apply_directive({"text": "불 켜놔줘", "until": "2026-06-02T20:00:00+00:00"})
    assert code == 200

    r = _simulate(dict(_VACANT_LIGHT_BODY))

    assert r["silence"] is True, r  # keep_on(light) 이 소등 후보를 억제 → 침묵
    assert [d["until"] for d in r["directives"]["active"]] == ["2026-06-02T20:00:00+00:00"], r
