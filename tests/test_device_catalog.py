"""디바이스 카탈로그 — default-deny 와 방향 추론.

위험도 조회는 Gate1 의 입력이라 여기서 틀리면 안전 하한이 통째로 틀어진다.
그런데 방향 추론(같은 action 이 올리는 방향이냐 내리는 방향이냐)과 로딩 실패
경로는 게이트 밖에 있었다(커버리지 75%) — 그 경계를 테스트로 들인다.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from liot.core.risk import RiskTier
from liot.layers.l4_execution.device_catalog import (
    DeviceCatalog,
    load_device_catalog,
)

RAW = {
    "devices": [
        {"entity_id": "climate.living", "capability": "climate", "category": "climate"},
        {"entity_id": "ghost.device", "capability": "unknown_cap", "category": "x"},
        {"entity_id": "nocap.device", "category": "x"},
    ],
    "capabilities": {
        "climate": {
            "set_temperature": {
                "tier": "breath",
                # 세 tier 를 전부 다르게 둔다 — 방향 추론이 실제로 갈리는지 보려면
                # increase·decrease·기본값이 서로 달라야 한다.
                "directional": {"increase": "dusk", "decrease": "ambient"},
            },
            "raise_temperature": {
                "tier": "breath",
                "directional": {"increase": "dusk", "decrease": "ambient"},
            },
            "lower_temperature": {
                "tier": "breath",
                "directional": {"increase": "dusk", "decrease": "ambient"},
            },
            "turn_off": {"tier": "ambient"},
            "broken": {},  # tier 누락
        }
    },
}


def _catalog() -> DeviceCatalog:
    return DeviceCatalog(RAW)


# ── default-deny ────────────────────────────────────────────────────────
@pytest.mark.parametrize(
    "cap,action",
    [("unknown_cap", "anything"), ("climate", "unknown_action"), ("", "")],
)
def test_unknown_pairs_default_deny_to_ember(cap: str, action: str) -> None:
    """모르는 조합은 무조건 EMBER — '모르면 안전 쪽'이 이 카탈로그의 기본값이다."""
    assert _catalog().risk_for(cap, action, {}) is RiskTier.EMBER


def test_missing_tier_is_also_default_deny() -> None:
    """항목은 있는데 tier 가 없으면 역시 EMBER(빈 스펙을 '위험 없음'으로 읽지 않는다)."""
    assert _catalog().risk_for("climate", "broken", {}) is RiskTier.EMBER


# ── 방향 추론 ───────────────────────────────────────────────────────────
@pytest.mark.parametrize(
    "params,expected",
    [
        ({"delta": 2}, RiskTier.DUSK),            # 숫자 양수 → 올림
        ({"delta": -2}, RiskTier.AMBIENT),        # 숫자 음수 → 내림
        ({"direction": "up"}, RiskTier.DUSK),     # 토큰
        ({"direction": "down"}, RiskTier.AMBIENT),
        ({"step": "+3"}, RiskTier.DUSK),          # 문자열 부호(+)
        ({"step": "-3"}, RiskTier.AMBIENT),       # 문자열 부호(-)
        ({"temperature": "+1"}, RiskTier.DUSK),   # directional.param 경로
        ({}, RiskTier.BREATH),                    # 추론 실패 → 기본 tier
        ({"delta": 0}, RiskTier.BREATH),          # 0 은 방향이 아니다
    ],
)
def test_direction_decides_tier(params: dict[str, object], expected: RiskTier) -> None:
    """방향이 위험도를 가른다 — 추론에 실패하면 기본 tier 로 떨어진다.

    문자열 부호("+3"/"-3")는 param 분기에만 있고 delta/step 분기에는 없었다.
    기본 tier 가 decrease 보다 낮으면 그대로 과소 승격이 된다(2026-09-16 야간 발견).
    """
    spec = dict(RAW["capabilities"]["climate"]["set_temperature"])  # type: ignore[index]
    directional = dict(spec["directional"])
    if "temperature" in params:
        directional["param"] = "temperature"
    raw = {
        "devices": RAW["devices"],
        "capabilities": {"climate": {"set_temperature": {**spec, "directional": directional}}},
    }
    assert DeviceCatalog(raw).risk_for("climate", "set_temperature", params) is expected


@pytest.mark.parametrize(
    "action,expected",
    [("raise_temperature", RiskTier.DUSK), ("lower_temperature", RiskTier.AMBIENT),
     ("set_temperature", RiskTier.BREATH)],
)
def test_action_name_infers_direction(action: str, expected: RiskTier) -> None:
    """params 가 비어도 action 이름의 토큰(raise/lower)으로 방향을 읽는다."""
    assert _catalog().risk_for("climate", action, {}) is expected


# ── 메타 조회 ───────────────────────────────────────────────────────────
def test_metadata_lookups_for_unknown_entities() -> None:
    cat = _catalog()
    assert cat.capability_for("climate.living") == "climate"
    assert cat.category_for("climate.living") == "climate"
    assert cat.known_entity("climate.living") is True
    assert cat.capability_for("nope.device") is None
    assert cat.category_for("nope.device") is None
    assert cat.known_entity("nope.device") is False


def test_available_actions_skips_devices_without_usable_capability() -> None:
    """capability 가 없거나 카탈로그에 없는 기기는 열거에서 빠진다(후보 생성 상한)."""
    rows = _catalog().available_actions()
    entities = {r["entity_id"] for r in rows}
    assert entities == {"climate.living"}  # ghost(미지 capability)·nocap(capability 없음) 제외
    by_action = {r["action"]: r["tier"] for r in rows}
    assert by_action["turn_off"] == "ambient"
    assert by_action["broken"] == "ember"  # tier 누락은 열거에서도 EMBER 로 보인다


# ── 로딩 ────────────────────────────────────────────────────────────────
def test_missing_file_gives_empty_catalog_that_denies_everything(tmp_path: Path) -> None:
    """파일이 없으면 빈 카탈로그 — 조용히 '다 허용'이 아니라 '다 거부'다."""
    cat = load_device_catalog(tmp_path / "does-not-exist.yaml")
    assert cat.risk_for("light", "turn_on", {}) is RiskTier.EMBER
    assert cat.available_actions() == []


def test_non_mapping_yaml_is_treated_as_empty(tmp_path: Path) -> None:
    p = tmp_path / "bad.yaml"
    p.write_text("- 목록이라 매핑이 아니다\n", encoding="utf-8")
    assert load_device_catalog(p).risk_for("light", "turn_on", {}) is RiskTier.EMBER


def test_shipped_catalog_loads_and_keeps_open_costlier_than_close() -> None:
    """출하 카탈로그: 여는 방향이 닫는 방향보다 비싸다(fail-safe 방향이 싸다)."""
    cat = load_device_catalog()
    assert cat.risk_for("valve", "open", {}) >= cat.risk_for("valve", "close", {})
    assert cat.risk_for("cover", "open", {}) >= cat.risk_for("cover", "close", {})
