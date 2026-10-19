"""egress_gate 단일 외부 송신 통로 검증.

  - ntfy payload 에 device/room/rationale 키 부재(화이트리스트만).
  - 비허용 dest 거부(EgressViolation).
  - 정제 후에도 PII 매치가 남으면 EgressViolation.
  - LLM dest 는 식별자성 값을 재귀 가명화.
"""

from __future__ import annotations

import pytest

from liot.core.egress_gate import (
    NTFY_ALLOWED_KEYS,
    AliasMap,
    Destination,
    emit_external,
    pseudonymize_recursive,
)
from liot.core.invariants import EgressViolation


def test_ntfy_drops_device_room_rationale_keys() -> None:
    payload = {
        "channel": "ntfy",
        "proposal_id": "p1",
        "risk_tier": 3,
        "glyph": "🛡",
        "label": "불씨",
        "signed_deeplink": "liot://p/p1?t=3&sig=abc",
        "topic": "liot",
        # 아래 키들은 절대 외부로 나가면 안 된다.
        "device": "안방 도어락",
        "room": "안방",
        "rationale": "현관문이 열렸습니다(누출 금지)",
    }
    out = emit_external(Destination.NTFY, payload, trace_id="t")
    assert "device" not in out
    assert "room" not in out
    assert "rationale" not in out
    assert set(out.keys()) <= NTFY_ALLOWED_KEYS
    # 화이트리스트 키는 보존.
    assert out["proposal_id"] == "p1"
    assert out["glyph"] == "🛡"


def test_unknown_destination_rejected() -> None:
    with pytest.raises(EgressViolation):
        emit_external("SLACK", {"x": 1}, trace_id="t")


def test_pii_in_llm_payload_raises() -> None:
    # 가명화 대상이 아닌 자유 텍스트 필드에 PII 가 박혀 있으면 정제 후에도 잔존.
    payload = {"note": "연락처 010-1234-5678 로 알려주세요"}
    with pytest.raises(EgressViolation):
        emit_external(Destination.LLM, payload, trace_id="t")


def test_email_pii_blocked() -> None:
    payload = {"summary": "보호자 hong@example.com 에게 전달"}
    with pytest.raises(EgressViolation):
        emit_external(Destination.LLM, payload, trace_id="t")


def test_llm_recursive_pseudonymization() -> None:
    alias = AliasMap()
    payload = {
        "room": "안방",
        "nested": {"person": "홍길동", "device": "스마트플러그"},
        "list": [{"area": "거실"}],
        "metric": 21.0,
    }
    out = emit_external(Destination.LLM, payload, trace_id="t", alias=alias)
    # 식별자성 키의 값은 가명 토큰으로 치환.
    assert out["room"] == "ROOM_A"
    assert out["nested"]["person"] == "PERSON_1"
    assert out["nested"]["device"] == "PERSON_2"
    assert out["list"][0]["area"] == "ROOM_B"
    # 비식별 스칼라는 그대로.
    assert out["metric"] == 21.0
    # 역매핑이 보존됨.
    assert alias.real_for("ROOM_A") == "안방"


def test_pseudonymize_recursive_standalone() -> None:
    alias = AliasMap()
    out = pseudonymize_recursive(
        {"user": "alice", "items": [{"room": "주방"}]}, alias
    )
    assert out["user"] == "PERSON_1"
    assert out["items"][0]["room"] == "ROOM_A"


def test_aliasmap_backstop_redacts_real_names_in_free_text() -> None:
    """방어심층 백스톱: 등록된 실명이 자유 텍스트(비식별 키)에 박혀 있어도
    LLM egress 에서 가명 토큰으로 치환되어 raw 이름이 절대 나가지 않는다.
    (상류 nl_summary 가명-only 보장이 깨져도 egress 가 막는다)
    """
    alias = AliasMap()
    alias.register("엄마", "PERSON_1")
    alias.register("거실", "ROOM_A")
    payload = {
        "summary": "거실 에서 엄마 감지",          # 자유 텍스트 + 비식별 키
        "actions": [{"entity": "거실 조명 켜기"}],   # 'entity' 는 식별자 키 아님
    }
    out = emit_external(Destination.LLM, payload, trace_id="t", alias=alias)
    flat = str(out)
    assert "엄마" not in flat
    assert "거실" not in flat
    assert "PERSON_1" in flat
    assert "ROOM_A" in flat


def test_aliasmap_backstop_noop_without_registration() -> None:
    """등록된 실명이 없으면 백스톱은 기존 동작을 바꾸지 않는다(no-op)."""
    alias = AliasMap()
    out = emit_external(Destination.LLM, {"summary": "거실 상태 정상"}, trace_id="t", alias=alias)
    assert out["summary"] == "거실 상태 정상"


def test_kma_generalizes_coordinates() -> None:
    payload = {"lat": 37.123456, "lon": 127.987654, "topic": "weather"}
    out = emit_external(Destination.KMA, payload, trace_id="t")
    # 0.1도 격자로 일반화 → 정밀좌표 PII 패턴(소수 4자리+)에 걸리지 않음.
    assert out["lat"] == 37.1
    assert out["lon"] == 128.0


# ── 뮤테이션 스윕이 찾은 미고정 가드(2026-09-16 야간) ────────────────────
def test_redact_text_replaces_longest_real_name_first() -> None:
    """부분 겹침 방어: 긴 실명부터 치환해야 짧은 쪽이 먼저 먹어치우지 않는다.

    스윕에서 `sorted(..., reverse=True)` 를 뒤집어도 아무 테스트가 울지 않았다.
    뒤집히면 '거실' 이 먼저 치환돼 '거실 조명' 이 'ROOM_A 조명' 으로 반쪽만 가려진다.
    """
    alias = AliasMap()
    long_alias = alias.alias_for("거실 조명", kind="ROOM")
    short_alias = alias.alias_for("거실", kind="ROOM")
    out = alias.redact_text("지금 거실 조명 상태")
    assert long_alias in out
    assert "조명" not in out, out  # 긴 실명이 통째로 사라져야 한다
    assert f"{short_alias} 조명" not in out


def test_redact_text_ignores_empty_real_name() -> None:
    """빈 실명이 등록돼도 텍스트를 갈기갈기 찢지 않는다(`if real and ...` 가드).

    `"" in text` 는 언제나 참이라, 가드가 없으면 replace("") 가 글자 사이마다
    가명을 끼워 넣는다.
    """
    alias = AliasMap()
    alias.register("", "PERSON_9")
    assert alias.redact_text("거실 조명") == "거실 조명"


def test_coordinate_generalization_only_touches_numeric_coordinates() -> None:
    """좌표 키 **이면서** 숫자일 때만 격자화한다(둘 중 하나만으론 건드리지 않는다).

    스윕에서 그 `and` 를 `or` 로 바꿔도 아무 테스트가 울지 않았다.
    """
    from liot.core.egress_gate import _generalize_coords

    out = _generalize_coords({"lat": 37.123456, "lon": 127.987654})
    assert out["lat"] != 37.123456 and round(out["lat"], 1) == out["lat"]

    # 좌표 키지만 숫자가 아니다 → 그대로.
    assert _generalize_coords({"lat": "삼십칠"})["lat"] == "삼십칠"
    # 숫자지만 좌표 키가 아니다 → 그대로(온도까지 뭉개면 판단이 망가진다).
    assert _generalize_coords({"temp_c": 37.123456})["temp_c"] == 37.123456


# ── 무작위 uuid 제안 id 가 전화번호 패턴에 우연히 걸려 알림이 막히면 안 된다 ──────
def test_uuid_proposal_id_never_trips_phone_pattern():
    from liot.core.egress_gate import Destination, emit_external

    # uuid4 의 'c010-1234-5678' 구간이 phone_kr 패턴(010-1234-5678)과 겹치는 실제 형태.
    payload = {
        "channel": "ntfy",
        "proposal_id": "aaaaaaaa-c010-1234-5678-bbbbbbbbbbbb",
        "risk_tier": 3,
        "glyph": "🛡",
        "label": "불씨",
        "signed_deeplink": "liot://p/x?t=3&sig=00",
        "topic": "t",
    }
    out = emit_external(Destination.NTFY, payload, trace_id="t", alias=None)
    assert out["proposal_id"] == payload["proposal_id"]


def test_non_uuid_proposal_id_is_still_scanned():
    import pytest as _pytest

    from liot.core.egress_gate import Destination, emit_external
    from liot.core.invariants import EgressViolation

    payload = {"channel": "ntfy", "proposal_id": "call 010-1234-5678", "risk_tier": 1,
               "glyph": "✓", "label": "숨결", "signed_deeplink": "x", "topic": "t"}
    with _pytest.raises(EgressViolation):
        emit_external(Destination.NTFY, payload, trace_id="t", alias=None)
