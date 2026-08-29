"""물리연결 레이어 검증 — 오케스트레이션 + 영속 + 실송신 + 피드백.

축:
  1) dispatch: 제안 영속 + 발화 게이트 + (정제분만) 실제 송신.
  2) fail-closed: transport 실패가 파이프라인을 죽이지 않음.
  3) egress: 송신 payload 에 device/room/rationale 부재(화이트리스트만).
  4) recently_rejected: 거절한 류는 이후 억제(DB 영속 학습).
  5) feedback gateway: 서명 검증(위조 거부) + 상태 전이 + HTTP 왕복.
  6) accept_rate: 수용률 메트릭(North Star) 출처별 집계.
  7) run_tick: 능동 제안 디스패치 + 만료 스윕.
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from liot.app import LiotApp
from liot.core.clock import Clock
from liot.core.contracts import FeedbackEvent, SensorReading
from liot.core.risk import RiskTier
from liot.db.database import init_db
from liot.layers.l1_sensing.normalizer import normalize
from liot.layers.l2_context.proactive import ProactiveGenerator
from liot.layers.l2_context.proactive_rules import load_proactive_ruleset
from liot.layers.l4_execution.feedback_gateway import (
    FeedbackAuthError,
    verify_deeplink_sig,
)
from liot.layers.l4_execution.notifier import (
    NotificationBudget,
    sign_deeplink,
    sign_feedback,
)
from liot.layers.l4_execution.ntfy_transport import FakeNtfyTransport
from liot.service import LiotService

DAY = "2026-06-02T12:00:00+00:00"
SECRET = b"test-secret-key"
CATS = {
    "binary_sensor.living_motion": "motion",
    "light.living_room": "light",
    "sensor.living_temperature": "temperature",
    "binary_sensor.kitchen_gas": "gas",
}
COMFORT_YAML = """
version: "1.0.0"
name: comfort
limits: {cooldown_minutes: 60, max_per_bucket: 5, max_per_day: 10}
rules:
  - id: vacant-light-off
    match: {occupancy: vacant}
    state: [{entity: light.living_room, op: eq, value: true}]
    propose: {target: light.living_room, capability: light, action: turn_off}
    ttl_minutes: 15
"""


def _reading(eid: str, raw: object, ts: str = DAY) -> SensorReading:
    return SensorReading(
        entity_id=eid, source="test", raw_value=raw, norm_value=normalize(raw),
        ts_utc=ts, home_id="singleton",
    )


async def _make_service(tmp_path, *, transport=None, clock=None):
    clock = clock or Clock(fixed=DAY)
    db = await init_db(":memory:")
    rs = load_proactive_ruleset(_write(tmp_path, COMFORT_YAML))
    gen = ProactiveGenerator(ruleset=rs, categories=CATS, clock=clock)
    app = LiotApp(clock=clock, categories=CATS, proactive=gen)
    svc = LiotService(
        app, db, clock=clock, transport=transport, topic="liot-test", secret=SECRET
    )
    return svc, db


def _write(tmp_path, text, name="comfort.yaml"):
    p = tmp_path / name
    p.write_text(text, encoding="utf-8")
    return p


# ==========================================================================
# 1) dispatch + 3) egress
# ==========================================================================
async def test_safety_proposal_sent_immediately_sanitized(tmp_path) -> None:
    transport = FakeNtfyTransport()
    svc, db = await _make_service(tmp_path, transport=transport)
    try:
        res = await svc.handle_reading(_reading("binary_sensor.kitchen_gas", "on"))
        assert res is not None
        assert res.proposal.risk_tier == RiskTier.EMBER
        assert res.decision == "IMMEDIATE"
        assert res.sent is True
        # 정확히 1건 송신, 화이트리스트 키만(누출 키 부재).
        assert len(transport.sent) == 1
        topic, payload = transport.sent[0]
        assert topic == "liot-test"
        assert set(payload) <= {
            "channel", "proposal_id", "risk_tier", "glyph", "label", "signed_deeplink", "topic"
        }
        for leaked in ("device", "room", "rationale", "entity_id", "action", "category"):
            assert leaked not in payload
        # DB 영속 확인.
        stored = await svc.proposals.get(res.proposal.proposal_id)
        assert stored is not None and stored.state == "pending"
    finally:
        await db.close()


async def test_ambient_proactive_is_digested_not_sent(tmp_path) -> None:
    transport = FakeNtfyTransport()
    svc, db = await _make_service(tmp_path, transport=transport)
    try:
        # 자리 비움 + 조명 켜짐 관찰(침묵) → 캐시 갱신.
        for eid, raw in [("binary_sensor.living_motion", "off"), ("light.living_room", "on")]:
            assert await svc.handle_reading(_reading(eid, raw)) is None
        res = await svc.run_tick()
        assert res is not None
        assert res.proposal.action.action == "turn_off"
        # 능동 light=AMBIENT → DIGEST(즉시 핑 안 함). 송신 없음.
        assert res.decision == "DIGEST"
        assert res.sent is False
        assert transport.sent == []
    finally:
        await db.close()


# ==========================================================================
# 2) fail-closed
# ==========================================================================
async def test_transport_failure_is_fail_closed(tmp_path) -> None:
    transport = FakeNtfyTransport(fail=True)
    svc, db = await _make_service(tmp_path, transport=transport)
    try:
        res = await svc.handle_reading(_reading("binary_sensor.kitchen_gas", "on"))
        assert res is not None
        assert res.decision == "IMMEDIATE"
        assert res.sent is False  # 송신 실패
        assert res.error is not None  # 사유 기록
        # 그래도 제안은 영속(재시도 가능), 파이프라인은 안 죽음.
        assert await svc.proposals.get(res.proposal.proposal_id) is not None
    finally:
        await db.close()


async def test_failed_send_does_not_poison_dedup(tmp_path) -> None:
    # 안전 EMBER 송신이 실패해도 dedup 을 오염시키지 않아, 다음 동일 류가
    # 2시간 SUPPRESS 되지 않는다(Safety-Never-Silent 전달 보존).
    transport = FakeNtfyTransport(fail=True)
    svc, db = await _make_service(tmp_path, transport=transport)
    try:
        p1 = await _propose(svc, "binary_sensor.kitchen_gas", "on")
        r1 = await svc.dispatch(p1)
        assert r1.sent is False  # 송신 실패
        assert svc._recent_notified == {}  # dedup 오염 없음
        # transport 복구 후 같은 류 → 여전히 IMMEDIATE + 송신 성공(억제 안 됨).
        transport.fail = False
        p2 = await _propose(svc, "binary_sensor.kitchen_gas", "on")
        r2 = await svc.dispatch(p2)
        assert r2.decision == "IMMEDIATE" and r2.sent is True
    finally:
        await db.close()


async def test_ember_bypasses_budget_and_dedup(tmp_path) -> None:
    # 안전 EMBER 는 시간당 예산·중복 게이트를 우회한다(throttle 로 안전이 묶이면 안 됨).
    transport = FakeNtfyTransport()
    svc, db = await _make_service(tmp_path, transport=transport)
    try:
        # max_per_hour=0 → _roll_budget 리셋 후에도 exhausted()=True(0>=0).
        svc._budget = NotificationBudget(max_per_hour=0)
        p = await _propose(svc, "binary_sensor.kitchen_gas", "on")
        res = await svc.dispatch(p)
        assert res.decision == "IMMEDIATE" and res.sent is True  # 예산 무시
        # 같은 류 재차(dedup 셋에 이미 있음) — EMBER 라 여전히 IMMEDIATE.
        p2 = await _propose(svc, "binary_sensor.kitchen_gas", "on")
        res2 = await svc.dispatch(p2)
        assert res2.decision == "IMMEDIATE"  # dedup 도 우회
    finally:
        await db.close()


# ==========================================================================
# 4) recently_rejected (DB 영속 학습)
# ==========================================================================
async def _propose(svc, eid: str, raw: object):
    # force_snapshot 으로 매번 동일 dedup_key 의 제안을 강제 생성(값 무변화여도).
    return await svc.app.process_reading(_reading(eid, raw), force_snapshot=True)


async def test_recently_rejected_suppresses_same_kind(tmp_path) -> None:
    transport = FakeNtfyTransport()
    svc, db = await _make_service(tmp_path, transport=transport)
    try:
        p1 = await _propose(svc, "binary_sensor.kitchen_gas", "on")
        r1 = await svc.dispatch(p1)
        assert r1.sent is True
        # 사용자가 거절 → dismissed 상태(DB 영속).
        await svc.proposals.apply_feedback(
            FeedbackEvent(proposal_id=p1.proposal_id, trace_id="fb", user_action="dismissed")
        )
        # dedup(2시간) 혼선 제거 → SUPPRESS 원인을 recently_rejected 로 격리.
        svc._recent_notified.clear()
        p2 = await _propose(svc, "binary_sensor.kitchen_gas", "on")
        r2 = await svc.dispatch(p2)
        assert r2.decision == "SUPPRESS"  # recently_rejected(30일) 로 억제
        assert r2.sent is False
    finally:
        await db.close()


# ==========================================================================
# 5) feedback gateway (서명 검증)
# ==========================================================================
def test_ntfy_egress_does_not_pii_flag_signed_deeplink() -> None:
    # 회귀: HMAC 16진수 서명이 우연히 rrn_kr(13자리 숫자열) 패턴과 일치해도
    # NTFY 송신이 차단되면 안 된다(안전 EMBER 알림이 확률적으로 죽던 잠복 버그).
    from liot.core.egress_gate import Destination, emit_external

    digit_heavy = "liot://p/" + "a" * 8 + "?t=3&sig=" + "0123456123456" + "ab" * 25
    payload = {
        "channel": "ntfy", "proposal_id": "p-1", "risk_tier": 3, "glyph": "🛡",
        "label": "불씨", "signed_deeplink": digit_heavy, "topic": "liot",
    }
    out = emit_external(Destination.NTFY, payload, trace_id="t")  # 예외 없이 통과
    assert out["signed_deeplink"] == digit_heavy
    # 하지만 비-딥링크 필드의 진짜 PII(전화번호)는 여전히 차단된다.
    # 예외 타입과 **어떤 탐지기가 걸렸는지**까지 고정한다. 예전에는
    # `pytest.raises(Exception)` 이라, egress 가드가 사라지고 필드 이름이 바뀌어
    # TypeError 가 나도 이 테스트는 초록이었다 — 틀린 이유로 통과하는 단언.
    from liot.core.invariants import EgressViolation

    with pytest.raises(EgressViolation) as caught:
        emit_external(
            Destination.NTFY,
            {**payload, "label": "연락처 010-1234-5678"},
            trace_id="t",
        )
    assert "phone_kr" in str(caught.value), caught.value


def test_verify_deeplink_sig_roundtrip() -> None:
    deeplink = sign_deeplink(SECRET, "p-123", RiskTier.EMBER)
    # sign_deeplink 형식: liot://p/p-123?t=3&sig=<hex>
    sig = deeplink.split("sig=", 1)[1]
    assert verify_deeplink_sig(SECRET, "p-123", 3, sig) is True
    assert verify_deeplink_sig(SECRET, "p-123", 2, sig) is False  # tier 변조
    assert verify_deeplink_sig(SECRET, "p-999", 3, sig) is False  # id 변조
    assert verify_deeplink_sig(b"wrong", "p-123", 3, sig) is False  # 키 변조


async def test_feedback_gateway_applies_and_rejects_forgery(tmp_path) -> None:
    transport = FakeNtfyTransport()
    svc, db = await _make_service(tmp_path, transport=transport)
    try:
        res = await svc.handle_reading(_reading("binary_sensor.kitchen_gas", "on"))
        pid = res.proposal.proposal_id
        tier = int(res.proposal.risk_tier)
        accept_sig = sign_feedback(SECRET, pid, res.proposal.risk_tier, "accept")
        # 위조 서명 → 거부, 상태 불변.
        with pytest.raises(FeedbackAuthError):
            await svc.feedback.verify_and_apply(pid, tier, "deadbeef", "accept")
        assert (await svc.proposals.get(pid)).state == "pending"
        # action 까지 묶인 정상 서명 → accepted 전이.
        updated = await svc.feedback.verify_and_apply(pid, tier, accept_sig, "accept")
        assert updated is not None and updated.state == "accepted"
        # 알 수 없는 action → 거부.
        with pytest.raises(FeedbackAuthError):
            await svc.feedback.verify_and_apply(pid, tier, accept_sig, "launch_missiles")
    finally:
        await db.close()


async def test_action_bound_sig_prevents_choice_flip(tmp_path) -> None:
    # accept 용 서명으로 dismiss 를 수행할 수 없다(도청자의 선택 뒤집기 방어).
    transport = FakeNtfyTransport()
    svc, db = await _make_service(tmp_path, transport=transport)
    try:
        res = await svc.handle_reading(_reading("binary_sensor.kitchen_gas", "on"))
        pid, tier = res.proposal.proposal_id, int(res.proposal.risk_tier)
        accept_sig = sign_feedback(SECRET, pid, res.proposal.risk_tier, "accept")
        with pytest.raises(FeedbackAuthError):
            await svc.feedback.verify_and_apply(pid, tier, accept_sig, "dismiss")
        assert (await svc.proposals.get(pid)).state == "pending"  # 불변
    finally:
        await db.close()


async def test_apply_feedback_no_pollution_for_ghost_or_cross_home(tmp_path) -> None:
    # 유령 id / 타 홈 id 는 feedback·audit 행을 남기지 않는다(존재 확인 우선).
    svc, db = await _make_service(tmp_path, transport=FakeNtfyTransport())
    try:
        ghost = await svc.proposals.apply_feedback(
            FeedbackEvent(proposal_id="p-ghost", trace_id="t", user_action="accepted")
        )
        assert ghost is None
        cur = await db.execute("SELECT COUNT(*) FROM feedback")
        assert (await cur.fetchone())[0] == 0  # 오염 없음
        await cur.close()
    finally:
        await db.close()


async def test_feedback_http_roundtrip(tmp_path) -> None:
    transport = FakeNtfyTransport()
    svc, db = await _make_service(tmp_path, transport=transport)
    server = await svc.serve_feedback("127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    try:
        res = await svc.handle_reading(_reading("binary_sensor.kitchen_gas", "on"))
        pid = res.proposal.proposal_id
        tier = int(res.proposal.risk_tier)
        nonce = svc._nonce[pid]  # 알림 링크에 박혀 나간 1회용 토큰
        good = sign_feedback(SECRET, pid, res.proposal.risk_tier, "accept", nonce)

        async def _get(path: str) -> str:
            reader, writer = await asyncio.open_connection("127.0.0.1", port)
            writer.write(f"GET {path} HTTP/1.1\r\nHost: x\r\n\r\n".encode("ascii"))
            await writer.drain()
            data = await reader.read()
            writer.close()
            return data.decode("utf-8", "replace")

        ok = await _get(f"/feedback?p={pid}&t={tier}&n={nonce}&sig={good}&a=accept")
        assert "200 OK" in ok and "accepted" in ok
        # 같은 링크 재생 → nonce 가 소비됐으므로 거부(영구 재사용 방지).
        replay = await _get(f"/feedback?p={pid}&t={tier}&n={nonce}&sig={good}&a=accept")
        assert "403" in replay
        bad = await _get(f"/feedback?p={pid}&t={tier}&n={nonce}&sig=deadbeef&a=accept")
        assert "403" in bad
        nf = await _get("/nope")
        assert "404" in nf
    finally:
        server.close()
        await db.close()


# ==========================================================================
# 6) accept_rate 메트릭
# ==========================================================================
async def test_accept_rate_by_origin(tmp_path) -> None:
    transport = FakeNtfyTransport()
    svc, db = await _make_service(tmp_path, transport=transport)
    try:
        # 안전 제안 2건(force 로 동일 dedup_key 강제): 1 수락 / 1 거절.
        p1 = await _propose(svc, "binary_sensor.kitchen_gas", "on")
        await svc.dispatch(p1)
        await svc.proposals.apply_feedback(
            FeedbackEvent(proposal_id=p1.proposal_id, trace_id="f", user_action="accepted")
        )
        p2 = await _propose(svc, "binary_sensor.kitchen_gas", "on")
        await svc.dispatch(p2)
        await svc.proposals.apply_feedback(
            FeedbackEvent(proposal_id=p2.proposal_id, trace_id="f", user_action="dismissed")
        )
        par = await svc.proposals.accept_rate()
        assert "safety" in par
        assert par["safety"]["accepted"] == 1
        assert par["safety"]["dismissed"] == 1
        assert par["safety"]["accept_rate"] == 0.5
    finally:
        await db.close()


# ==========================================================================
# 7) 데몬 루프(경계) — 능동 틱만 구동
# ==========================================================================
def test_http_ntfy_transport_real_post() -> None:
    # 실제 urllib POST + 헤더 구성을 로컬 mock ntfy 서버로 왕복 검증.
    import threading
    from http.server import BaseHTTPRequestHandler, HTTPServer

    from liot.layers.l4_execution.ntfy_transport import HTTPNtfyTransport

    captured: dict[str, Any] = {}

    class _Handler(BaseHTTPRequestHandler):
        def do_POST(self):  # noqa: N802
            length = int(self.headers.get("Content-Length", "0"))
            captured["path"] = self.path
            captured["body"] = self.rfile.read(length).decode("utf-8")
            captured["headers"] = dict(self.headers.items())
            self.send_response(200)
            self.end_headers()

        def log_message(self, *a):  # 조용히
            pass

    server = HTTPServer(("127.0.0.1", 0), _Handler)
    port = server.server_address[1]
    t = threading.Thread(target=server.handle_request, daemon=True)
    t.start()
    try:
        transport = HTTPNtfyTransport(
            f"http://127.0.0.1:{port}",
            feedback_base_url="http://127.0.0.1:9/cb",
            signing_secret=SECRET,
        )
        sanitized = {
            "channel": "ntfy", "proposal_id": "p-1", "risk_tier": 3,
            "glyph": "🛡", "label": "불씨", "topic": "liot",
            "signed_deeplink": "liot://p/p-1?t=3&sig=abc123",
        }
        transport.send("liot", sanitized)
        t.join(timeout=3)
        assert captured["path"] == "/liot"
        assert captured["headers"]["Priority"] == "5"  # ember
        assert captured["headers"]["Click"] == "liot://p/p-1?t=3&sig=abc123"
        # 액션 버튼은 action-bound 서명을 싣는다(accept 서명으로 dismiss 못 함).
        expected_accept = sign_feedback(SECRET, "p-1", 3, "accept")
        assert f"sig={expected_accept}&a=accept" in captured["headers"]["Actions"]
        assert "a=dismiss" in captured["headers"]["Actions"]
        # 본문엔 위험도 라벨/글리프만(디바이스/방 부재).
        assert "불씨" in captured["body"]
        for leaked in ("p-1 device", "room", "rationale"):
            assert leaked not in captured["body"]
    finally:
        server.server_close()


async def test_run_tick_only_daemon_bounded(tmp_path) -> None:
    transport = FakeNtfyTransport()
    svc, db = await _make_service(tmp_path, transport=transport)
    try:
        for eid, raw in [("binary_sensor.living_motion", "off"), ("light.living_room", "on")]:
            await svc.handle_reading(_reading(eid, raw))
        # bridge 없이 능동 틱만, 1틱 경계로 종료(무한루프 아님).
        await svc.run(None, tick_seconds=0.0, max_ticks=1)
        # 능동 제안이 DB 에 영속됐는지(틱이 실제로 돌았는지).
        pend = await svc.proposals.list_pending()
        assert any(p.action.action == "turn_off" for p in pend)
    finally:
        await db.close()


# ── 1회용 토큰의 수명: 이벤트 기반 제안도 정리된다 ──────────────────────
async def test_nonce_sweep_covers_event_driven_proposals(tmp_path) -> None:
    """해소된 제안의 nonce 는 스윕에서 사라진다 — tick 제안이 아니어도.

    처음 구현은 정리를 `_apply_expired_signals` 에 얹었는데, 그 경로는
    `_prop_cell` 에 있는 제안만 훑는다. `_remember_proposal_cell` 은
    `app.last_facts` 를 요구하고 그건 `tick()` 에서만 채워지므로,
    **이벤트 기반 제안(가스 경보 등)은 아예 담기지 않는다** — 그 토큰들이
    상시 데몬에서 영원히 쌓였다. 지금은 제안 상태를 직접 보고 버린다.
    """
    svc, db = await _make_service(tmp_path)
    try:
        res = await svc.handle_reading(_reading("binary_sensor.kitchen_gas", "on"))
        pid = res.proposal.proposal_id

        assert pid in svc._nonce, "디스패치가 토큰을 발급하지 않았다"
        # 이 제안이 옛 정리 경로에 안 잡힌다는 사실 자체를 고정한다.
        assert pid not in svc._prop_cell, (
            "이벤트 기반 제안이 _prop_cell 에 담겼다 — 이 테스트의 전제가 바뀌었다"
        )

        # 스윕: 아직 pending 이면 토큰을 유지해야 한다(살아 있는 링크를 죽이지 않는다).
        await svc._sweep_nonces()
        assert pid in svc._nonce, "pending 제안의 토큰이 잘못 회수됐다"

        # 해소되면 회수된다.
        await svc.proposals.apply_feedback(
            FeedbackEvent(proposal_id=pid, trace_id=f"fb-{pid}", user_action="dismissed")
        )
        await svc._sweep_nonces()
        assert pid not in svc._nonce, "해소된 제안의 토큰이 남았다(무한 증가)"
    finally:
        await db.close()
