"""T5: 파급도(severity)에 따른 발화 게이트 우회 + ntfy 푸시 세기.

  - CRITICAL 상황 알림은 mute·recently_rejected 를 우회한다(헌장 OFF 만 존중).
  - dedup/quiet/ambient/budget 우회 조건 = perishable OR EMBER OR CRITICAL.
  - 파급도가 있는 제안은 ntfy 우선순위를 파급도에서(위험5·경고4·주의3·기록2),
    없는 제안은 예전처럼 위험도 tier 에서 받는다.
  - severity_label 은 egress(NTFY) 를 통과하되 device/room/rationale 은 여전히 제거.
  - severity=None 이면 게이트 판정·우선순위가 예전과 똑같다(회귀 규칙).
"""

from __future__ import annotations

from typing import Any

import pytest

from liot.core.clock import Clock
from liot.core.contracts import CandidateAction, FeedbackEvent, Proposal
from liot.core.egress_gate import NTFY_ALLOWED_KEYS, Destination, emit_external
from liot.core.risk import RiskTier
from liot.core.severity import SeverityLevel
from liot.layers.l4_execution.notifier import (
    NotificationBudget,
    NotificationContext,
    Notifier,
    dedup_key,
    gate_pipeline,
)
from liot.layers.l4_execution.ntfy_transport import HTTPNtfyTransport

DAY = "2026-06-02T12:00:00+00:00"
SECRET = b"test-secret-key"


def _prop(
    *,
    tier: RiskTier = RiskTier.BREATH,
    severity: SeverityLevel | None = None,
    category: str = "gas",
    entity_id: str = "binary_sensor.kitchen_gas",
    capability: str = "valve",
    action: str = "close",
    perishable: bool = False,
    pid: str = "p-sev",
) -> Proposal:
    """게이트 테스트용 최소 제안. 기본은 비-EMBER·비시의성(우회 사유 없음)."""
    return Proposal(
        proposal_id=pid, judgment_id="j", home_id="singleton",
        risk_tier=tier,
        action=CandidateAction(capability=capability, action=action, params={},
                               entity_id=entity_id, category=category),
        rationale="부엌 가스 감지(외부 누출 금지)", required_confirmation="valve",
        perishable=perishable, expires_at=None, state="pending", created_at=DAY,
        severity=severity,
        situation_id="gas_leak" if severity is not None else None,
    )


def _budget(exhausted: bool = False) -> NotificationBudget:
    return NotificationBudget(max_per_hour=0 if exhausted else 10)


# ── 1) CRITICAL 은 명시적 옵트아웃 중 mute·rejected 를 우회한다 ─────────────
@pytest.mark.parametrize("tier", [RiskTier.BREATH, RiskTier.EMBER])
def test_critical_bypasses_recently_rejected(tier: RiskTier) -> None:
    p = _prop(tier=tier, severity=SeverityLevel.CRITICAL)
    ctx = NotificationContext(recently_rejected_keys=frozenset({dedup_key(p)}))
    assert gate_pipeline(p, ctx, _budget(), DAY) == "IMMEDIATE"


@pytest.mark.parametrize("muted", ["category", "entity"])
@pytest.mark.parametrize("tier", [RiskTier.BREATH, RiskTier.EMBER])
def test_critical_bypasses_mute(muted: str, tier: RiskTier) -> None:
    p = _prop(tier=tier, severity=SeverityLevel.CRITICAL)
    if muted == "category":
        ctx = NotificationContext(muted_categories=frozenset({p.action.category}))
    else:
        ctx = NotificationContext(muted_entities=frozenset({p.action.entity_id}))
    assert gate_pipeline(p, ctx, _budget(), DAY) == "IMMEDIATE"


def test_critical_still_respects_charter_off() -> None:
    p = _prop(tier=RiskTier.EMBER, severity=SeverityLevel.CRITICAL, perishable=True)
    ctx = NotificationContext(charter_enabled=False)
    assert gate_pipeline(p, ctx, _budget(), DAY) == "SUPPRESS"


@pytest.mark.parametrize("severity", [SeverityLevel.WARNING, SeverityLevel.NOTICE,
                                      SeverityLevel.RECORD, None])
def test_non_critical_keeps_mute_and_rejected(severity: SeverityLevel | None) -> None:
    """CRITICAL 이 아니면 mute·rejected 는 예전처럼 침묵(EMBER 라도)."""
    p = _prop(tier=RiskTier.EMBER, severity=severity)
    muted = NotificationContext(muted_categories=frozenset({p.action.category}))
    rejected = NotificationContext(recently_rejected_keys=frozenset({dedup_key(p)}))
    assert gate_pipeline(p, muted, _budget(), DAY) == "SUPPRESS"
    assert gate_pipeline(p, rejected, _budget(), DAY) == "SUPPRESS"


# ── 2) CRITICAL 은 throttle(dedup/quiet/ambient/budget) 를 우회한다 ─────────
def test_critical_bypasses_quiet_hours() -> None:
    p = _prop(tier=RiskTier.BREATH, severity=SeverityLevel.CRITICAL)
    assert gate_pipeline(p, NotificationContext(quiet_hours=True), _budget(), DAY) == "IMMEDIATE"


def test_critical_bypasses_dedup() -> None:
    p = _prop(tier=RiskTier.BREATH, severity=SeverityLevel.CRITICAL)
    ctx = NotificationContext(recent_notified_keys=frozenset({dedup_key(p)}))
    assert gate_pipeline(p, ctx, _budget(), DAY) == "IMMEDIATE"


def test_critical_bypasses_budget() -> None:
    p = _prop(tier=RiskTier.BREATH, severity=SeverityLevel.CRITICAL)
    assert gate_pipeline(p, NotificationContext(), _budget(exhausted=True), DAY) == "IMMEDIATE"


def test_critical_bypasses_ambient() -> None:
    p = _prop(tier=RiskTier.AMBIENT, severity=SeverityLevel.CRITICAL)
    assert gate_pipeline(p, NotificationContext(), _budget(), DAY) == "IMMEDIATE"


# ── 3) WARNING(비-EMBER) 은 예전 throttle 그대로 ────────────────────────────
def test_warning_non_ember_keeps_normal_throttles() -> None:
    p = _prop(tier=RiskTier.BREATH, severity=SeverityLevel.WARNING)
    dedup = NotificationContext(recent_notified_keys=frozenset({dedup_key(p)}))
    assert gate_pipeline(p, dedup, _budget(), DAY) == "SUPPRESS"
    assert gate_pipeline(p, NotificationContext(quiet_hours=True), _budget(), DAY) == "SUPPRESS"
    assert gate_pipeline(p, NotificationContext(), _budget(exhausted=True), DAY) == "DIGEST"
    amb = _prop(tier=RiskTier.AMBIENT, severity=SeverityLevel.WARNING)
    assert gate_pipeline(amb, NotificationContext(), _budget(), DAY) == "DIGEST"


# ── 4) 회귀: severity=None 이면 게이트 판정이 예전과 똑같다 ─────────────────
def _legacy_gate(p: Proposal, ctx: NotificationContext, budget: NotificationBudget) -> str:
    """T5 이전 게이트의 판정표(회귀 기준). severity 를 보지 않는다."""
    tier = RiskTier.from_str(p.risk_tier)
    key = dedup_key(p)
    bypass = bool(p.perishable) or tier == RiskTier.EMBER
    if not ctx.charter_enabled:
        return "SUPPRESS"
    if p.action.category in ctx.muted_categories or p.action.entity_id in ctx.muted_entities:
        return "SUPPRESS"
    if key in ctx.recently_rejected_keys:
        return "SUPPRESS"
    if key in ctx.recent_notified_keys:
        return "IMMEDIATE" if bypass else "SUPPRESS"
    if ctx.quiet_hours and tier < RiskTier.EMBER:
        return "IMMEDIATE" if bypass else "SUPPRESS"
    if tier == RiskTier.AMBIENT:
        return "IMMEDIATE" if bypass else "DIGEST"
    if budget.exhausted():
        return "IMMEDIATE" if bypass else "DIGEST"
    return "IMMEDIATE"


def _all_contexts(p: Proposal) -> list[NotificationContext]:
    key = dedup_key(p)
    out = []
    for charter in (True, False):
        for mute in (False, True):
            for rejected in (False, True):
                for dedup in (False, True):
                    for quiet in (False, True):
                        out.append(NotificationContext(
                            charter_enabled=charter,
                            muted_categories=frozenset({p.action.category}) if mute else frozenset(),
                            recently_rejected_keys=frozenset({key}) if rejected else frozenset(),
                            recent_notified_keys=frozenset({key}) if dedup else frozenset(),
                            quiet_hours=quiet,
                        ))
    return out


@pytest.mark.parametrize("tier", list(RiskTier))
@pytest.mark.parametrize("perishable", [False, True])
def test_severity_none_gate_is_unchanged(tier: RiskTier, perishable: bool) -> None:
    p = _prop(tier=tier, severity=None, perishable=perishable)
    for ctx in _all_contexts(p):
        for exhausted in (False, True):
            assert gate_pipeline(p, ctx, _budget(exhausted), DAY) == _legacy_gate(
                p, ctx, _budget(exhausted)
            ), (tier, perishable, ctx, exhausted)


@pytest.mark.parametrize("severity", list(SeverityLevel))
@pytest.mark.parametrize("tier", list(RiskTier))
@pytest.mark.parametrize("perishable", [False, True])
def test_severity_never_makes_a_today_immediate_quieter(
    severity: SeverityLevel, tier: RiskTier, perishable: bool
) -> None:
    """오늘 IMMEDIATE 인 알림은 어떤 파급도가 붙어도 여전히 IMMEDIATE(조용해지지 않음)."""
    base = _prop(tier=tier, severity=None, perishable=perishable)
    with_sev = _prop(tier=tier, severity=severity, perishable=perishable)
    for ctx in _all_contexts(base):
        for exhausted in (False, True):
            if gate_pipeline(base, ctx, _budget(exhausted), DAY) == "IMMEDIATE":
                assert gate_pipeline(with_sev, ctx, _budget(exhausted), DAY) == "IMMEDIATE"


def test_ember_without_severity_is_unchanged() -> None:
    p = _prop(tier=RiskTier.EMBER, severity=None)
    key = dedup_key(p)
    # throttle 우회는 그대로, 명시적 옵트아웃(mute/rejected)은 그대로 존중.
    assert gate_pipeline(p, NotificationContext(quiet_hours=True), _budget(), DAY) == "IMMEDIATE"
    assert gate_pipeline(p, NotificationContext(recent_notified_keys=frozenset({key})),
                         _budget(), DAY) == "IMMEDIATE"
    assert gate_pipeline(p, NotificationContext(), _budget(exhausted=True), DAY) == "IMMEDIATE"
    assert gate_pipeline(p, NotificationContext(recently_rejected_keys=frozenset({key})),
                         _budget(), DAY) == "SUPPRESS"
    assert gate_pipeline(p, NotificationContext(muted_entities=frozenset({p.action.entity_id})),
                         _budget(), DAY) == "SUPPRESS"


# ── 5) 페이로드: severity_label 은 egress 를 통과, 누출 키는 여전히 제거 ─────
def test_payload_carries_severity_label_only_when_severity_present() -> None:
    n = Notifier(secret=SECRET, topic="liot")
    with_sev = n.sanitize(_prop(tier=RiskTier.EMBER, severity=SeverityLevel.CRITICAL))
    assert with_sev["severity_label"] == "위험"
    without = n.sanitize(_prop(tier=RiskTier.EMBER, severity=None))
    # 회귀: 파급도가 없으면 페이로드 키 집합이 예전(7개)과 똑같다.
    assert "severity_label" not in without
    assert set(without) == {
        "channel", "proposal_id", "risk_tier", "glyph", "label", "signed_deeplink", "topic"
    }
    for leaked in ("device", "room", "rationale", "entity_id", "action", "category",
                   "situation_id", "severity"):
        assert leaked not in with_sev


def test_severity_label_survives_ntfy_egress_but_leaks_are_stripped() -> None:
    payload = {
        "channel": "ntfy", "proposal_id": "p1", "risk_tier": 3, "glyph": "🛡",
        "label": "불씨", "signed_deeplink": "liot://p/p1?t=3&sig=abc", "topic": "liot",
        "severity_label": "경고",
        "device": "안방 도어락", "room": "안방", "rationale": "현관문이 열렸습니다",
        "situation_id": "door_open", "resident_trait": "독거", "severity": 2,
    }
    out = emit_external(Destination.NTFY, payload, trace_id="t")
    assert out["severity_label"] == "경고"
    for leaked in ("device", "room", "rationale", "situation_id", "resident_trait", "severity"):
        assert leaked not in out
    assert set(out) <= NTFY_ALLOWED_KEYS


def test_ntfy_allowed_keys_gained_only_severity_label_and_summary_keys() -> None:
    # T8: 상황 요약 알림(보호자 전달·해소·119 안내) 키 3개만 더해졌다 — 상황 종류 라벨·
    # 집 별칭·시각. 기기·방·근거·거주자 특성 키는 여전히 없다.
    expected = frozenset({
        "channel", "proposal_id", "risk_tier", "glyph", "label", "signed_deeplink", "topic",
        "severity_label", "situation_label", "home_alias", "at",
    })
    assert expected == NTFY_ALLOWED_KEYS


# ── 6) ntfy 우선순위: 파급도 우선, 없으면 tier ─────────────────────────────
def _priority(sanitized: dict[str, Any]) -> str:
    return HTTPNtfyTransport("http://127.0.0.1:9")._headers(sanitized)["Priority"]


@pytest.mark.parametrize(("severity", "expected"), [
    (SeverityLevel.CRITICAL, "5"),
    (SeverityLevel.WARNING, "4"),
    (SeverityLevel.NOTICE, "3"),
    (SeverityLevel.RECORD, "2"),
])
def test_priority_comes_from_severity_when_present(severity: SeverityLevel, expected: str) -> None:
    n = Notifier(secret=SECRET)
    sanitized = n.sanitize(_prop(tier=RiskTier.EMBER, severity=severity))
    assert _priority(sanitized) == expected


def test_door_situation_at_ember_goes_out_at_four() -> None:
    """현관 열림 상황(EMBER·경고): 즉시 발화는 그대로, 푸시 세기만 5→4(승인됨)."""
    door = _prop(tier=RiskTier.EMBER, severity=SeverityLevel.WARNING, category="door",
                 entity_id="lock.front_door", capability="lock", action="lock")
    n = Notifier(secret=SECRET)
    decision, sanitized = n.notify(door, NotificationContext(quiet_hours=True), _budget(), DAY)
    assert decision == "IMMEDIATE"
    assert sanitized is not None
    assert _priority(sanitized) == "4"


@pytest.mark.parametrize(("tier", "expected"), [
    (RiskTier.AMBIENT, "2"), (RiskTier.BREATH, "3"), (RiskTier.DUSK, "4"), (RiskTier.EMBER, "5"),
])
def test_priority_without_severity_uses_tier_map(tier: RiskTier, expected: str) -> None:
    n = Notifier(secret=SECRET)
    assert _priority(n.sanitize(_prop(tier=tier, severity=None))) == expected


@pytest.mark.parametrize("bad", [None, "", "모름", 3])
def test_unknown_severity_label_falls_back_to_tier(bad: object) -> None:
    sanitized = {"risk_tier": 3, "severity_label": bad}
    assert _priority(sanitized) == "5"


# ── 7) 결과: 가스 알림을 거절한 뒤에도 다음 가스 '상황' 알림은 즉시 간다 ────────
async def test_gas_situation_after_rejection_is_still_sent(tmp_path) -> None:
    from liot.app import LiotApp
    from liot.core.contracts import SensorReading
    from liot.db.database import init_db
    from liot.layers.l1_sensing.normalizer import normalize
    from liot.layers.l4_execution.ntfy_transport import FakeNtfyTransport
    from liot.service import LiotService

    clock = Clock(fixed=DAY)
    db = await init_db(":memory:")
    cats = {"binary_sensor.kitchen_gas": "gas"}
    app = LiotApp(clock=clock, categories=cats)
    transport = FakeNtfyTransport()
    svc = LiotService(app, db, clock=clock, transport=transport, topic="liot-test", secret=SECRET)

    def reading() -> SensorReading:
        return SensorReading(entity_id="binary_sensor.kitchen_gas", source="test",
                             raw_value="on", norm_value=normalize("on"), ts_utc=DAY,
                             home_id="singleton")

    async def propose() -> Proposal:
        p = await svc.app.process_reading(reading(), force_snapshot=True)
        assert p is not None
        return p

    try:
        p1 = await propose()
        assert (await svc.dispatch(p1)).sent is True
        await svc.proposals.apply_feedback(
            FeedbackEvent(proposal_id=p1.proposal_id, trace_id="fb", user_action="dismissed")
        )
        svc._recent_notified.clear()
        p2 = await propose()
        # 파급도 없는 같은 류는 예전처럼 거절 학습으로 침묵.
        assert (await svc.dispatch(p2)).decision == "SUPPRESS"
        # 같은 류라도 '위험' 상황 알림이면 즉시 간다.
        p3 = (await propose()).model_copy(update={"severity": SeverityLevel.CRITICAL, "situation_id": "gas_leak"})
        r3 = await svc.dispatch(p3)
        assert r3.decision == "IMMEDIATE"
        assert r3.sent is True
        _, payload = transport.sent[-1]
        assert payload["severity_label"] == "위험"
        assert _priority(payload) == "5"
        for leaked in ("device", "room", "rationale", "entity_id", "situation_id"):
            assert leaked not in payload
    finally:
        await db.close()
