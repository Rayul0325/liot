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
