"""외부 송신 단일 통로(egress gate).

모든 외부 송신(LLM 프롬프트, ntfy 알림, KMA 기상 조회, 기억 쓰기)은 ``emit_external`` 한
함수를 통과해야 한다. 이 게이트는:
  1. 목적지가 ALLOWLIST 에 있는지 확인하고(없으면 EgressViolation),
  2. 목적지별 정제(LLM=재귀 가명화 / NTFY=화이트리스트 키만 / KMA=좌표 그리드
     일반화 / MEMORY=변환 없음, 검사만)를 적용하며,
  3. 정제 후에도 PII 패턴이 남아 있으면 EgressViolation 을 던지고,
  4. 정제된 payload 를 반환한다.

주의: 이 게이트는 네트워크로 실제 송신하지 않는다. "무엇이 나가도 되는가"를
결정·정제할 뿐이다. ntfy 페이로드에는 디바이스명·방이름·rationale 가
절대 포함되지 않는다.
"""

from __future__ import annotations

import re
from enum import Enum
from typing import Any

from .invariants import EgressViolation

__all__ = [
    "Destination",
    "ALLOWLIST",
    "NTFY_ALLOWED_KEYS",
    "PII_PATTERNS",
    "AliasMap",
    "pseudonymize_recursive",
    "emit_external",
    "PII_SCAN_SKIP_KEYS",
]


class Destination(str, Enum):
    """허용되는 외부 목적지."""

    LLM = "LLM"
    NTFY = "NTFY"
    KMA = "KMA"
    # memgineering 브레인(하루 시뮬레이터의 데모 브레인 전용). 쓰는 문장은 그대로 나가므로
    # 변환 없이 PII 그물과 엔티티 모양 검사만 한다.
    MEMORY = "MEMORY"


# 명시적 허용 목록. 여기 없는 목적지로는 어떤 것도 나갈 수 없다.
ALLOWLIST: frozenset[Destination] = frozenset(
    {Destination.LLM, Destination.NTFY, Destination.KMA, Destination.MEMORY}
)

# ntfy 페이로드에 허용되는 키. 그 외 키는 모두 제거된다.
# 의도적으로 device/room/rationale 같은 식별 정보를 배제한다.
NTFY_ALLOWED_KEYS: frozenset[str] = frozenset(
    {
        "channel",
        "proposal_id",
        "risk_tier",
        "glyph",
        "label",
        "signed_deeplink",
        "topic",
        # 파급도 한국어 라벨(예: "위험"). 라벨 문자열뿐이라 식별 정보가 없다.
        # transport 가 이걸로 푸시 세기를 정한다.
        "severity_label",
        # 상황 요약 알림(보호자 전달·해소·119 안내)의 키. 상황 종류 라벨(예: "가스 누출"),
        # 집 별칭, ISO 시각뿐 — 기기·방·근거·거주자 특성 키는 여기 없다(GuardianNotification
        # Payload 계약과 짝). 확인 링크는 이미 허용된 signed_deeplink 키로 싣는다.
        "situation_label",
        "home_alias",
        "at",
    }
)

# 송신 직전 마지막 방어선으로 검출하는 PII 패턴.
PII_PATTERNS: dict[str, re.Pattern[str]] = {
    # 한국 휴대폰 번호 (010-1234-5678, 01012345678 등)
    "phone_kr": re.compile(r"01[016789][-\s]?\d{3,4}[-\s]?\d{4}"),
    # 이메일
    "email": re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}"),
    # 한국 주민등록번호 패턴 (YYMMDD-XXXXXXX)
    "rrn_kr": re.compile(r"\b\d{6}[-\s]?[1-4]\d{6}\b"),
    # 위/경도 좌표(소수점 4자리 이상) — KMA 외 목적지로의 정밀좌표 누출 방지
    "geo_precise": re.compile(r"\b\d{1,3}\.\d{4,}\b"),
}

# LLM 목적지 한정 백스톱: entity_id 형태('domain.object') 문자열 검출.
# 키 기반 가명화(_looks_like_identifier)는 'device'/'room' 같은 키 아래의
# 값만 치환하므로, 새 필드/비식별 키 아래로 raw entity_id 가 새는 것을
# 구조적으로 못 막는다. LLM 으로 나가는 payload 의 모든 문자열 값에서
# 이 형태가 발견되면 송신 자체를 거부한다(fail-closed → LLMUnavailable
# → degraded. 안전 카테고리는 그래도 EMBER 유지 — 불변식 6).
# 대소문자 무시('Light.Living_Room' 같은 우회 차단). 가명 토큰
# (DEVICE_1/ROOM_A/PERSON_1)은 점이 아니라 밑줄을 쓰므로 여전히 안 걸린다.
# 양쪽 다 영문자로 시작해야 하므로 소수(22.5)·버전(v2.0)은 매치되지 않는다.
_ENTITY_SHAPED_RE = re.compile(r"\b[a-z][a-z0-9_]*\.[a-z][a-z0-9_]*\b", re.IGNORECASE)


def _scan_entity_shaped(payload: Any) -> str | None:
    """payload 의 문자열 값에서 entity_id 형태를 찾는다(첫 매치 반환)."""

    def walk(obj: Any) -> str | None:
        if isinstance(obj, str):
            m = _ENTITY_SHAPED_RE.search(obj)
            return m.group(0) if m else None
        if isinstance(obj, dict):
            for v in obj.values():
                hit = walk(v)
                if hit:
                    return hit
            return None
        if isinstance(obj, (list, tuple)):
            for v in obj:
                hit = walk(v)
                if hit:
                    return hit
        return None

    return walk(payload)


class AliasMap:
    """실제 식별자 <-> 가명('ROOM_A', 'PERSON_1') 양방향 매핑.

    같은 실제 값에는 같은 가명을 안정적으로 부여한다(결정론적, 등장 순서 기준).
    """

    def __init__(self) -> None:
        self._real_to_alias: dict[str, str] = {}
        self._alias_to_real: dict[str, str] = {}
        self._counters: dict[str, int] = {}

    def alias_for(self, real: str, kind: str = "PERSON") -> str:
        """real 에 대응하는 가명을 반환(없으면 새로 발급).

        kind 는 가명 접두사를 정한다: 'ROOM' -> ROOM_A, ROOM_B...
        그 외 -> PERSON_1, PERSON_2...
        """
        if real in self._real_to_alias:
            return self._real_to_alias[real]
        n = self._counters.get(kind, 0)
        self._counters[kind] = n + 1
        if kind.upper() == "ROOM":
            alias = f"ROOM_{chr(ord('A') + n)}"
        else:
            alias = f"{kind.upper()}_{n + 1}"
        self._real_to_alias[real] = alias
        self._alias_to_real[alias] = real
        return alias

    def real_for(self, alias: str) -> str | None:
        """가명에 대응하는 실제 값(역매핑). 없으면 None."""
        return self._alias_to_real.get(alias)

    def register(self, real: str, alias: str) -> None:
        """명시적으로 (real, alias) 쌍을 등록한다."""
        self._real_to_alias[real] = alias
        self._alias_to_real[alias] = real

    def redact_text(self, text: str) -> str:
        """자유 텍스트 안에 박힌 '등록된 실명'을 가명으로 치환하는 백스톱.

        상류(context_builder)가 nl_summary 를 가명 토큰으로만 생성한다는 1차
        보장이 어떤 이유로든 깨져 실명이 자유 텍스트에 섞여 나가더라도,
        AliasMap 이 알고 있는 실제 라벨은 이 백스톱에서 토큰으로 대체된다.
        부분 겹침을 피하기 위해 긴 실명부터 치환한다.
        """
        if not self._real_to_alias:
            return text
        for real in sorted(self._real_to_alias, key=len, reverse=True):
            if real and real in text:
                text = text.replace(real, self._real_to_alias[real])
        return text


def _looks_like_identifier(key: str) -> str | None:
    """payload 키 이름으로 가명화 종류를 추정한다.

    'room'/'area'/'zone' 계열 -> 'ROOM', 'person'/'user'/'name'/'device' 계열
    -> 'PERSON'. 아니면 None(가명화 대상 아님).
    """
    k = key.lower()
    if any(tok in k for tok in ("room", "area", "zone", "space")):
        return "ROOM"
    if any(tok in k for tok in ("person", "user", "name", "device", "occupant", "owner")):
        return "PERSON"
    return None


def pseudonymize_recursive(payload: Any, alias: AliasMap) -> Any:
    """payload 를 재귀적으로 순회하며 식별자성 값을 가명으로 치환한다.

    dict 의 키 이름이 방/사람/디바이스를 가리키면 그 값을 가명화한다.
    list/tuple 은 원소별로 재귀, 그 외 스칼라는 그대로 둔다.
    """
    if isinstance(payload, dict):
        out: dict[Any, Any] = {}
        for key, value in payload.items():
            kind = _looks_like_identifier(str(key))
            if kind is not None and isinstance(value, str):
                out[key] = alias.alias_for(value, kind=kind)
            else:
                out[key] = pseudonymize_recursive(value, alias)
        return out
    if isinstance(payload, list):
        return [pseudonymize_recursive(v, alias) for v in payload]
    if isinstance(payload, tuple):
        return tuple(pseudonymize_recursive(v, alias) for v in payload)
    # 비식별 키 아래의 자유 텍스트 스칼라도 AliasMap 백스톱으로 한 번 더 훑는다.
    # (등록된 실명이 없으면 no-op → 기존 동작 보존)
    if isinstance(payload, str):
        return alias.redact_text(payload)
    return payload


def _generalize_coords(payload: Any) -> Any:
    """KMA 목적지용: 위/경도를 격자(grid)로 일반화한다.

    소수점 정밀 좌표를 0.1도 격자 셀로 반올림해 정밀 위치를 흐린다.
    """
    if isinstance(payload, dict):
        out: dict[Any, Any] = {}
        for key, value in payload.items():
            k = str(key).lower()
            if k in ("lat", "lon", "lng", "latitude", "longitude") and isinstance(
                value, (int, float)
            ):
                # 0.1도 격자(약 10km)로 일반화.
                out[key] = round(round(float(value) * 10) / 10, 1)
            else:
                out[key] = _generalize_coords(value)
        return out
    if isinstance(payload, list):
        return [_generalize_coords(v) for v in payload]
    return payload


# PII 스캔에서 제외하는 불투명 토큰 키. 이 값들은 HMAC 서명·해시 등
# *구조적으로 PII 가 불가능한* 토큰이며, 무작위 16진수 서명이 우연히 PII
# 패턴(예: rrn_kr 의 13자리 숫자열)과 일치해 정당한 알림(특히 안전 EMBER)이
# 확률적으로 차단되는 것을 막는다. signed_deeplink 는
# `liot://p/<proposal_id>?t=<tier>&sig=<hmac>[&n=<nonce>]` 형태(uuid·정수·
# 16진수 다이제스트·token_hex)라 사용자 PII 를 담을 수 없다.
PII_SCAN_SKIP_KEYS: frozenset[str] = frozenset({"signed_deeplink"})

# 값이 *정확히 uuid 형태일 때만* 스캔을 건너뛰는 키. 제안 id 는 uuid4 라 사용자 PII 를
# 담을 수 없는데, 무작위 16진수 구간(예: 'c010-1234-5678')이 phone_kr 패턴과 우연히
# 겹치면(실측 약 0.05%) 안전 알림 전체가 차단됐다. uuid 가 아닌 값은 그대로 스캔한다.
PII_SCAN_UUID_KEYS: frozenset[str] = frozenset({"proposal_id"})
_UUID_RE = re.compile(
    r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}"
)


def _scan_pii(payload: Any, skip_keys: frozenset[str] = frozenset()) -> list[str]:
    """정제된 payload 의 모든 문자열 값에서 PII 패턴을 검출한다.

    skip_keys 에 해당하는 dict 키의 *값* 은 스캔하지 않는다(불투명 서명 토큰).
    반환: 매치된 패턴 이름들(없으면 빈 리스트).
    """
    hits: list[str] = []

    def walk(obj: Any) -> None:
        if isinstance(obj, str):
            for name, pattern in PII_PATTERNS.items():
                if pattern.search(obj):
                    hits.append(name)
        elif isinstance(obj, dict):
            for k, v in obj.items():
                walk(str(k))
                if str(k) in skip_keys:
                    continue  # 불투명 서명 토큰: 값은 스캔 제외(키는 스캔함)
                if (
                    str(k) in PII_SCAN_UUID_KEYS
                    and isinstance(v, str)
                    and _UUID_RE.fullmatch(v)
                ):
                    continue  # uuid 제안 id: 구조상 PII 불가
                walk(v)
        elif isinstance(obj, (list, tuple)):
            for v in obj:
                walk(v)

    walk(payload)
    return hits


def emit_external(
    dest: Destination | str,
    payload: dict[str, Any],
    trace_id: str,
    alias: AliasMap | None = None,
) -> dict[str, Any]:
    """외부 송신 단일 진입점. 정제된 payload 를 반환(실제 송신은 안 함).

    절차:
      1) dest 가 ALLOWLIST 에 없으면 EgressViolation.
      2) dest 별 변환:
         - LLM : 재귀 가명화(pseudonymize_recursive)
         - NTFY: 화이트리스트 키(NTFY_ALLOWED_KEYS)만 남김
         - KMA : 좌표 그리드 일반화
         - MEMORY: 변환 없음(얕은 복사) — 문장이 그대로 저장되므로 아래 두 검사가 전부다
      3) 정제 후에도 PII 패턴이 남아 있으면 EgressViolation.
      4) 정제된 payload 반환.

    KMA 도 정밀좌표가 일반화되었는지 PII 스캐너로 한 번 더 확인한다.
    """
    if isinstance(dest, str):
        try:
            dest = Destination(dest)
        except ValueError as exc:
            raise EgressViolation(f"unknown destination: {dest!r}") from exc

    if dest not in ALLOWLIST:
        raise EgressViolation(f"destination not allowed: {dest.value}")

    if not isinstance(payload, dict):
        raise EgressViolation("payload must be a dict")

    if dest is Destination.LLM:
        amap = alias if alias is not None else AliasMap()
        sanitized: dict[str, Any] = pseudonymize_recursive(payload, amap)
    elif dest is Destination.NTFY:
        # 화이트리스트 키만 통과. device/room/rationale 등은 구조적으로 제거.
        sanitized = {k: v for k, v in payload.items() if k in NTFY_ALLOWED_KEYS}
    elif dest is Destination.KMA:
        sanitized = _generalize_coords(payload)
    elif dest is Destination.MEMORY:
        sanitized = dict(payload)
    else:  # pragma: no cover - ALLOWLIST 가 이미 막음
        raise EgressViolation(f"unhandled destination: {dest.value}")

    pii_hits = _scan_pii(sanitized, skip_keys=PII_SCAN_SKIP_KEYS)
    if pii_hits:
        raise EgressViolation(
            f"PII detected in egress to {dest.value} (trace={trace_id}): "
            f"{', '.join(sorted(set(pii_hits)))}"
        )

    # LLM·MEMORY: 가명화를 비껴간(또는 가명화가 없는) entity_id 형태 식별자가 남아 있으면 거부.
    if dest in (Destination.LLM, Destination.MEMORY):
        hit = _scan_entity_shaped(sanitized)
        if hit:
            raise EgressViolation(
                f"entity-shaped identifier in egress to {dest.value} (trace={trace_id}): "
                f"{hit!r} survived pseudonymization"
            )

    return sanitized
