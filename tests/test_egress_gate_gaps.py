"""E8 뮤테이션 빈틈 — egress_gate 의 list/tuple 분기와 '허용되지 않은 송신처' 거부 경로.

`experiments/e8_mutation/survivor_classes.json` 의 안전 관련 빈틈 6개(egress_gate.py 115~116,
208, 234, 315)에 각각 하나 이상의 테스트가 붙는다. 기대값은 모두 리터럴이다.
"""

from __future__ import annotations

import pytest

from liot.core import egress_gate
from liot.core.egress_gate import (
    AliasMap,
    Destination,
    emit_external,
    pseudonymize_recursive,
)
from liot.core.invariants import EgressViolation

# ── 엔티티 형태 검사(115~116): list/tuple 안 문자열도 본다 ───────────────────────


@pytest.mark.parametrize("dest", [Destination.LLM, Destination.MEMORY])
@pytest.mark.parametrize(
    "container",
    [
        ["light.living_room"],
        ("light.living_room",),
        ["정상 문장", ["깊이", "sensor.kitchen_temp"]],
        [{"nested": ("Fan.Living_Purifier",)}],
    ],
)
def test_entity_shaped_id_inside_list_or_tuple_is_refused(dest, container) -> None:
    with pytest.raises(EgressViolation, match="entity-shaped identifier"):
        emit_external(dest, {"notes": container}, trace_id="t")


@pytest.mark.parametrize("container", [["거실 불 켜짐", "22.5도"], ("v2.0", "DEVICE_1"), []])
def test_clean_list_or_tuple_passes_entity_scan(container) -> None:
    out = emit_external(Destination.MEMORY, {"notes": container}, trace_id="t")
    assert out == {"notes": container}


def test_entity_scan_returns_first_hit_inside_sequence() -> None:
    assert egress_gate._scan_entity_shaped({"a": ["ok", "light.a_b", "fan.x"]}) == "light.a_b"
    assert egress_gate._scan_entity_shaped({"a": ("ok", 3, None)}) is None


# ── 가명화(208): tuple 안 실명도 치환한다 ───────────────────────────────────────


def test_pseudonymize_recursive_rewrites_names_inside_tuple_and_keeps_type() -> None:
    amap = AliasMap()
    amap.register("안방", "ROOM_A")
    out = pseudonymize_recursive({"notes": ("안방 창문 열림", "정상")}, amap)
    assert out == {"notes": ("ROOM_A 창문 열림", "정상")}
    assert isinstance(out["notes"], tuple)


def test_llm_egress_pseudonymizes_registered_name_inside_tuple() -> None:
    amap = AliasMap()
    amap.register("안방", "ROOM_A")
    out = emit_external(Destination.LLM, {"notes": ("안방 창문 열림",)}, trace_id="t", alias=amap)
    assert out == {"notes": ("ROOM_A 창문 열림",)}


def test_pseudonymize_recursive_list_still_rewrites_names() -> None:
    amap = AliasMap()
    amap.register("안방", "ROOM_A")
    assert pseudonymize_recursive(["안방 문"], amap) == ["ROOM_A 문"]


# ── 좌표 일반화(234): list 안 좌표도 0.1도 격자로 ───────────────────────────────


def test_kma_generalizes_coordinates_inside_list() -> None:
    out = emit_external(
        Destination.KMA,
        {"points": [{"lat": 37.5665, "lon": 126.978}, {"latitude": 35.1796, "lng": 129.0756}]},
        trace_id="t",
    )
    assert out == {
        "points": [{"lat": 37.6, "lon": 127.0}, {"latitude": 35.2, "lng": 129.1}]
    }


def test_kma_generalizes_top_level_coordinates() -> None:
    out = emit_external(Destination.KMA, {"lat": 37.5665, "lon": 126.978}, trace_id="t")
    assert out == {"lat": 37.6, "lon": 127.0}


# ── 송신처 허용 목록(315): 목록 밖이면 거부 ─────────────────────────────────────


def test_destination_outside_allowlist_is_refused(monkeypatch) -> None:
    monkeypatch.setattr(egress_gate, "ALLOWLIST", frozenset({Destination.LLM}))
    with pytest.raises(EgressViolation, match="destination not allowed: NTFY"):
        emit_external(Destination.NTFY, {"channel": "ntfy"}, trace_id="t")
    with pytest.raises(EgressViolation, match="destination not allowed: KMA"):
        emit_external("KMA", {"lat": 1.0}, trace_id="t")
    # 목록 안 목적지는 여전히 통과한다(가드가 전부 막는 것이 아님).
    assert emit_external(Destination.LLM, {"x": 1}, trace_id="t") == {"x": 1}
