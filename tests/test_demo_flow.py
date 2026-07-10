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

from liot.demo_server import _REGISTRY, _run
from liot.registry import RegistryError


@pytest.fixture(autouse=True)
def _reset_demo_registry():
    """모듈 전역 데모 레지스트리를 매 테스트 전후로 초기화(격리)."""
    _REGISTRY.reset()
    yield
    _REGISTRY.reset()


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
