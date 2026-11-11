"""보호자 확인 링크는 situation_id 와 guardian_id 가 **둘 다** 있을 때만 만든다.

E8 뮤테이션 빈틈(notifier.py:378): `situation_id and guardian_id` 가 `or` 로 바뀌어도 아무
테스트도 안 깨졌다. `or` 이면 한쪽 id 만 와도 `g=None` 같은 서명 링크가 나간다 — 보호자별 서명
계약("보호자마다 다른 서명")을 깨는, 존재하지 않는 보호자 앞으로 서명된 링크다. 호출자는 지금
항상 둘 다 또는 둘 다 없음을 넘기므로 이 규칙은 시험이 지켜야 한다.
"""

from __future__ import annotations

import hashlib
import hmac

import pytest

from liot.core.severity import SeverityLevel
from liot.layers.l4_execution.notifier import Notifier

_SECRET = b"unit-test-secret"


def _payload(situation_id: str | None, guardian_id: str | None):
    return Notifier(secret=_SECRET).build_guardian_payload(
        situation_label="가스 누출",
        home_alias="우리집",
        at="2026-06-02T22:00:00+00:00",
        severity=SeverityLevel.CRITICAL,
        situation_id=situation_id,
        guardian_id=guardian_id,
    )


def test_both_ids_give_the_guardian_specific_signed_link() -> None:
    sig = hmac.new(_SECRET, b"guardian-ack:sit-1:g-1", hashlib.sha256).hexdigest()
    p = _payload("sit-1", "g-1")
    assert p.signed_ack_link == f"liot://ack/sit-1?g=g-1&sig={sig}"


@pytest.mark.parametrize(
    ("situation_id", "guardian_id"),
    [("sit-1", None), (None, "g-1"), ("sit-1", ""), ("", "g-1"), (None, None), ("", "")],
)
def test_missing_either_id_gives_no_link(situation_id, guardian_id) -> None:
    p = _payload(situation_id, guardian_id)
    assert p.signed_ack_link == ""
    assert "None" not in p.model_dump_json()


def test_summary_without_link_omits_the_deeplink_key() -> None:
    n = Notifier(secret=_SECRET)
    p = _payload("sit-1", None)
    out = n.sanitize_summary(p, trace_id="t")
    assert "signed_deeplink" not in out
    assert out["situation_label"] == "가스 누출"
    full = n.sanitize_summary(_payload("sit-1", "g-1"), trace_id="t")
    assert full["signed_deeplink"].startswith("liot://ack/sit-1?g=g-1&sig=")
