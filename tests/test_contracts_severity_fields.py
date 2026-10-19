"""상황 파급도(Severity) 계약 필드 — Judgment/Proposal 확장과 알림 페이로드.

고정하는 것:
  - 새 필드의 기본값(파급도 없는 판단 = None/None/빈 목록).
  - 새 필드를 채운 Judgment/Proposal 의 직렬화 왕복(SeverityLevel 은 RiskTier 처럼
    json 모드에서 정수).
  - extra="forbid" 가 여전히 모르는 키를 거부함.
  - GuardianNotificationPayload 의 정확한 필드 집합과 금지 필드의 부재.
  - NotificationPayload 에 device/room/rationale 필드가 여전히 없음.
"""

from __future__ import annotations

from typing import Any

import pytest
from pydantic import ValidationError

from liot.core import contracts
from liot.core.contracts import (
    CandidateAction,
    GuardianNotificationPayload,
    Judgment,
    NotificationPayload,
    Proposal,
)
from liot.core.risk import RiskTier
from liot.core.severity import SeverityLevel

# 외부 알림 페이로드에 절대 있어서는 안 되는 필드(장치·방·근거·거주자 특성).
_FORBIDDEN_EGRESS_FIELDS = frozenset(
    {
        "device",
        "device_id",
        "entity_id",
        "room",
        "room_id",
        "rationale",
        "action",
        "resident",
        "resident_traits",
        "traits",
    }
)


def _action() -> CandidateAction:
    return CandidateAction(
        capability="valve", action="close", entity_id="valve.gas", category="gas"
    )


def _judgment_kwargs() -> dict[str, Any]:
    return {
        "judgment_id": "j1",
        "trace_id": "t1",
        "home_id": "singleton",
        "snapshot_hash": "h",
        "action": _action(),
        "risk_tier": RiskTier.EMBER,
        "confidence": 0.9,
        "rationale": "가스 농도 상승",
        "gate1_verdict": "ember",
        "bundle_id": "b1",
        "model_id": "m1",
    }


def _proposal_kwargs() -> dict[str, Any]:
    return {
        "proposal_id": "p1",
        "judgment_id": "j1",
        "home_id": "singleton",
        "risk_tier": RiskTier.EMBER,
        "action": _action(),
        "rationale": "가스 농도 상승",
        "required_confirmation": "double",
        "created_at": "2026-10-19T00:00:00+00:00",
    }


def _guardian_kwargs() -> dict[str, Any]:
    return {
        "channel": "ntfy",
        "situation_label": "가스 누출",
        "home_alias": "우리집",
        "at": "2026-10-19T00:00:00+00:00",
        "signed_ack_link": "https://example.invalid/ack?sig=x",
        "severity_label": "위험",
    }


# ── 기본값 ─────────────────────────────────────────────────────────────────


def test_judgment_severity_defaults_are_empty() -> None:
    # 파급도가 없는 판단(쾌적 등): severity·floor 는 None, sources 는 빈 목록.
    j = Judgment(**_judgment_kwargs())
    assert j.severity is None
    assert j.severity_floor is None
    assert j.severity_sources == []
    assert j.situation_id is None
    assert j.model_probs is None


def test_judgment_severity_sources_default_not_shared() -> None:
    # default_factory 여야 한다(인스턴스 간 목록 공유 금지).
    a = Judgment(**_judgment_kwargs())
    b = Judgment(**_judgment_kwargs())
    a.severity_sources.append("table")
    assert b.severity_sources == []


def test_proposal_severity_defaults_are_none() -> None:
    p = Proposal(**_proposal_kwargs())
    assert p.severity is None
    assert p.situation_id is None


def test_notification_payload_severity_label_defaults_none() -> None:
    n = NotificationPayload(
        channel="ntfy",
        proposal_id="p1",
        risk_tier=RiskTier.EMBER,
        glyph="🛡",
        label="확인 필요",
        signed_deeplink="https://example.invalid/d?sig=x",
        topic="liot",
    )
    assert n.severity_label is None
    n2 = n.model_copy(update={"severity_label": SeverityLevel.CRITICAL.label_ko})
    assert n2.severity_label == "위험"


# ── 직렬화 왕복 ────────────────────────────────────────────────────────────


def _full_judgment() -> Judgment:
    return Judgment(
        **_judgment_kwargs(),
        severity=SeverityLevel.CRITICAL,
        severity_floor=SeverityLevel.WARNING,
        severity_sources=["table", "resident", "model"],
        situation_id="gas_leak",
        model_probs={
            "tier": {"ember": 0.8, "dusk": 0.2},
            "severity": {"critical": 0.7, "warning": 0.3},
        },
    )


def test_judgment_roundtrip_python_mode() -> None:
    j = _full_judgment()
    again = Judgment.model_validate(j.model_dump())
    assert again == j
    assert again.severity is SeverityLevel.CRITICAL
    assert again.severity_floor is SeverityLevel.WARNING


def test_judgment_roundtrip_json_mode_severity_is_int() -> None:
    j = _full_judgment()
    dumped = j.model_dump(mode="json")
    # RiskTier 와 같은 방식: json 모드에서 IntEnum 은 정수.
    assert dumped["risk_tier"] == 3
    assert dumped["severity"] == 3
    assert dumped["severity_floor"] == 2
    assert type(dumped["severity"]) is int
    assert dumped["severity_sources"] == ["table", "resident", "model"]
    again = Judgment.model_validate(dumped)
    assert again == j
    assert again.severity is SeverityLevel.CRITICAL
    # JSON 문자열 왕복도 성립한다.
    assert Judgment.model_validate_json(j.model_dump_json()) == j


def test_judgment_none_severity_roundtrip() -> None:
    j = Judgment(**_judgment_kwargs())
    dumped = j.model_dump(mode="json")
    assert dumped["severity"] is None
    assert dumped["severity_floor"] is None
    assert dumped["severity_sources"] == []
    assert Judgment.model_validate(dumped) == j


def test_judgment_rejects_unknown_severity_source() -> None:
    with pytest.raises(ValidationError):
        Judgment.model_validate({**_judgment_kwargs(), "severity_sources": ["llm"]})


def test_proposal_roundtrip_with_severity() -> None:
    p = Proposal(
        **_proposal_kwargs(),
        severity=SeverityLevel.WARNING,
        situation_id="gas_leak",
    )
    dumped = p.model_dump(mode="json")
    assert dumped["severity"] == 2
    assert dumped["situation_id"] == "gas_leak"
    again = Proposal.model_validate(dumped)
    assert again == p
    assert again.severity is SeverityLevel.WARNING
    assert Proposal.model_validate(p.model_dump()) == p


# ── extra="forbid" ─────────────────────────────────────────────────────────


def test_judgment_still_forbids_unknown_keys() -> None:
    with pytest.raises(ValidationError):
        Judgment.model_validate({**_judgment_kwargs(), "severity_note": "x"})


def test_proposal_still_forbids_unknown_keys() -> None:
    with pytest.raises(ValidationError):
        Proposal.model_validate(
            {**_proposal_kwargs(), "severity_floor": SeverityLevel.NOTICE}
        )


def test_notification_payload_still_forbids_unknown_keys() -> None:
    with pytest.raises(ValidationError):
        NotificationPayload.model_validate(
            {
                "channel": "ntfy",
                "proposal_id": "p1",
                "risk_tier": RiskTier.EMBER,
                "glyph": "🛡",
                "label": "확인 필요",
                "signed_deeplink": "https://example.invalid/d?sig=x",
                "topic": "liot",
                "room": "거실",
            }
        )


# ── 보호자 알림 페이로드 ───────────────────────────────────────────────────


def test_guardian_payload_exact_field_set() -> None:
    assert set(GuardianNotificationPayload.model_fields) == {
        "channel",
        "situation_label",
        "home_alias",
        "at",
        "signed_ack_link",
        "severity_label",
    }


def test_guardian_payload_has_no_forbidden_fields() -> None:
    fields = set(GuardianNotificationPayload.model_fields)
    assert fields.isdisjoint(_FORBIDDEN_EGRESS_FIELDS)
    # 필드 이름 일부로라도 장치·방·근거·거주자를 암시하지 않는다.
    for name in fields:
        for marker in ("device", "entity", "room", "rationale", "resident", "trait"):
            assert marker not in name, name


@pytest.mark.parametrize("forbidden", sorted(_FORBIDDEN_EGRESS_FIELDS))
def test_guardian_payload_rejects_forbidden_fields(forbidden: str) -> None:
    with pytest.raises(ValidationError):
        GuardianNotificationPayload.model_validate(
            {**_guardian_kwargs(), forbidden: "x"}
        )


def test_guardian_payload_all_fields_required() -> None:
    for key in _guardian_kwargs():
        partial = {k: v for k, v in _guardian_kwargs().items() if k != key}
        with pytest.raises(ValidationError):
            GuardianNotificationPayload.model_validate(partial)


def test_guardian_payload_roundtrip() -> None:
    g = GuardianNotificationPayload(**_guardian_kwargs())
    assert GuardianNotificationPayload.model_validate(g.model_dump()) == g
    assert g.model_dump() == _guardian_kwargs()


# ── 거주자 알림 페이로드: 금지 필드 부재 ───────────────────────────────────


def test_notification_payload_has_no_device_room_rationale() -> None:
    fields = set(NotificationPayload.model_fields)
    assert fields.isdisjoint(_FORBIDDEN_EGRESS_FIELDS)
    # 새로 추가된 것은 severity_label 하나뿐이다.
    assert fields == {
        "channel",
        "proposal_id",
        "risk_tier",
        "glyph",
        "label",
        "signed_deeplink",
        "topic",
        "severity_label",
    }


def test_new_names_exported() -> None:
    assert "GuardianNotificationPayload" in contracts.__all__
