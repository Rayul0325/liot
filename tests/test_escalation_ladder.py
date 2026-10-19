"""T8: 상황별 에스컬레이션 사다리 — 재알림·보호자 전달·119 안내·확인·해소·재시작 재무장.

축(spec):
  - 첫 알림 세기·재알림 주기는 **최종** 파급도(모델 상향 포함)를 따른다.
  - 보호자 전달·119 안내는 상황의 severity_floor(표+거주자)가 CRITICAL 일 때만.
    모델만 올린 CRITICAL 은 1분 재알림만 받고 보호자·119 는 없다.
  - WARNING: 10분마다 최대 3회 재알림. CRITICAL: 1분마다 재알림, 90초 무응답이면
    동의한 보호자 전원에게 동시 전달, 그 뒤 5분 무응답이면 119 전화 **안내**(자동 신고 없음).
  - 확인(ack): 거주자의 명시 응답(accept·dismiss/reject·snooze·ok) 또는 보호자의
    서명된 확인 링크. 알림을 여는 것만으로는 확인이 아니다.
  - 상황이 닫히면 사다리 취소 + 이미 전달받은 보호자에게 '해소됨'.
  - 사다리 상태는 DB 에 남고, 시작 시 list_open_ladders 로 다시 무장한다.

시각은 전부 고정 Clock 을 옮겨서 잰다(실시간 잠자기 없음).
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta
from typing import Any
from urllib.parse import parse_qs, urlsplit

import pytest

from liot.app import LiotApp
from liot.core.clock import Clock
from liot.core.contracts import CandidateAction, ContextSnapshot, SensorReading
from liot.core.egress_gate import NTFY_ALLOWED_KEYS
from liot.core.risk import RiskTier
from liot.core.severity import SeverityLevel
from liot.db.database import init_db
from liot.db.safety_repo import PERSON_TRAITS
from liot.layers.l1_sensing.normalizer import normalize
from liot.layers.l2_context.feature_vector import load_cells
from liot.layers.l3_judgment.llm_judge import LLMResult
from liot.layers.l4_execution import escalation
from liot.layers.l4_execution.feedback_gateway import FeedbackGateway, verify_guardian_ack_sig
from liot.layers.l4_execution.notifier import guardian_ack_link, sign_feedback
from liot.layers.l4_execution.ntfy_transport import (
    FakeNtfyTransport,
    HTTPNtfyTransport,
    NtfyError,
)
from liot.layers.l4_execution.proposal_service import FEEDBACK_STATE_MAP
from liot.service import LiotService

T0 = "2026-06-02T12:00:00+00:00"
SECRET = b"test-secret-key"
TOPIC = "liot-test"
GAS = "binary_sensor.kitchen_gas"
DOOR = "binary_sensor.front_door"
CATS = {GAS: "gas", DOOR: "door", "light.living_room": "light"}
HOME = "singleton"
GUARDIAN_PAYLOAD_KEYS = {
    "channel",
    "situation_label",
    "home_alias",
    "at",
    "signed_deeplink",
    "severity_label",
}


# ── 테스트용 백엔드 ──────────────────────────────────────────────────────────
class _Backend:
    """정해진 파급도를 돌려주는 결정 모델(None 이면 하한 그대로)."""

    model_id = "ladder-test-model"

    def __init__(self, severity: SeverityLevel | None = None) -> None:
        self.severity = severity

    def judge_raw(self, candidate: CandidateAction, ctx: ContextSnapshot | None,
                  gate1_floor: RiskTier,
                  severity_floor: SeverityLevel | None = None) -> LLMResult:
        return LLMResult(
            tier=RiskTier.EMBER, confidence=0.8, rationale="test", model_id=self.model_id,
            severity=self.severity if severity_floor is not None else None,
        )


class _SelectiveFailTransport(FakeNtfyTransport):
    """지정한 토픽으로 가는 송신만 실패시킨다(나머지는 기록)."""

    def __init__(self, fail_topics: set[str]) -> None:
        super().__init__()
        self.fail_topics = fail_topics

    def send(self, topic: str, sanitized: dict[str, Any]) -> None:
        if topic in self.fail_topics:
            raise NtfyError(f"forced failure for {topic}")
        super().send(topic, sanitized)


# ── 도우미 ───────────────────────────────────────────────────────────────────
def _reading(eid: str, raw: object) -> SensorReading:
    return SensorReading(
        entity_id=eid, source="test", raw_value=raw, norm_value=normalize(raw),
        ts_utc=T0, home_id=HOME,
    )


def _at(seconds: float) -> str:
    base = datetime.fromisoformat(T0)
    return (base + timedelta(seconds=seconds)).isoformat()


async def _make(*, backend: Any = None, transport: Any = None, db: Any = None,
                clock: Clock | None = None):
    clock = clock or Clock(fixed=T0)
    db = db if db is not None else await init_db(":memory:")
    app = LiotApp(clock=clock, categories=CATS,
                  llm_backend=backend if backend is not None else _Backend())
    transport = transport if transport is not None else FakeNtfyTransport()
    svc = LiotService(app, db, clock=clock, transport=transport, topic=TOPIC, secret=SECRET)
    return svc, db, transport, clock


async def _guardian(svc: LiotService, *, consent: bool = True,
                    alias: str = "딸") -> tuple[str, str, str]:
    rid = await svc.safety.add_resident(HOME, "엄마")
    gid, topic = await svc.safety.add_guardian(HOME, alias)
    if consent:
        await svc.safety.grant_consent(HOME, gid, rid)
    return rid, gid, topic


async def _open(svc: LiotService, eid: str = GAS):
    res = await svc.handle_reading(_reading(eid, "on"))
    assert res is not None and res.proposal.situation_id is not None
    await svc.drain_phase2()
    return res


async def _advance(svc: LiotService, clock: Clock, seconds: float) -> None:
    clock.set_fixed(_at(seconds))
    await svc.ladder_tick()


def _to(transport: FakeNtfyTransport, topic: str) -> list[dict[str, Any]]:
    return [p for t, p in transport.sent if t == topic]


async def _forwards(svc: LiotService) -> list[tuple[str | None, str]]:
    return [(f.guardian_id, f.kind) for f in await svc.safety.list_forwards(HOME)]


def _ack_query(payload: dict[str, Any]) -> dict[str, str]:
    link = payload["signed_deeplink"]
    parts = urlsplit(link)
    assert parts.scheme == "liot" and parts.netloc == "ack"
    q = {k: v[0] for k, v in parse_qs(parts.query).items()}
    q["s"] = parts.path.rsplit("/", 1)[-1]
    return q


async def _resident_click(svc: LiotService, proposal: Any, action: str) -> tuple[str, str]:
    pid = proposal.proposal_id
    nonce = svc._nonce[pid]
    sig = sign_feedback(SECRET, pid, proposal.risk_tier, action, nonce)
    tier = int(proposal.risk_tier)
    return await svc._route_feedback(
        f"GET /feedback?p={pid}&t={tier}&n={nonce}&sig={sig}&a={action} HTTP/1.1"
    )


# ==========================================================================
# 1) CRITICAL 하한: 89/90 초 경계에서 보호자 전달, 그 뒤 5분 경계에서 119 안내
# ==========================================================================
async def test_guardian_forward_happens_at_90s_not_89s() -> None:
    svc, db, transport, clock = await _make()
    try:
        _, gid, gtopic = await _guardian(svc)
        res = await _open(svc)
        sid = res.proposal.situation_id
        ladder = await svc.safety.get_ladder(sid)
        assert ladder is not None and not ladder.acked and ladder.next_fire_at is not None

        await _advance(svc, clock, 89)
        assert _to(transport, gtopic) == []
        assert await _forwards(svc) == []

        await _advance(svc, clock, 90)
        sent = _to(transport, gtopic)
        assert len(sent) == 1
        assert sent[0]["severity_label"] == SeverityLevel.CRITICAL.label_ko
        assert await _forwards(svc) == [(gid, "forward")]
        ladder = await svc.safety.get_ladder(sid)
        assert ladder is not None and ladder.notified_guardian_ids == (gid,)

        # 같은 보호자에게 두 번 전달하지 않는다.
        await _advance(svc, clock, 150)
        assert len(_to(transport, gtopic)) == 1
    finally:
        await db.close()


async def test_119_guidance_five_minutes_after_forward() -> None:
    svc, db, transport, clock = await _make()
    try:
        _, gid, gtopic = await _guardian(svc)
        res = await _open(svc)
        sid = res.proposal.situation_id
        await _advance(svc, clock, 90)
        assert await _forwards(svc) == [(gid, "forward")]

        await _advance(svc, clock, 90 + 299)
        assert (None, "guidance_119") not in await _forwards(svc)
        assert len(_to(transport, gtopic)) == 1

        await _advance(svc, clock, 90 + 300)
        assert await _forwards(svc) == [(gid, "forward"), (None, "guidance_119")]
        guardian_msgs = _to(transport, gtopic)
        assert len(guardian_msgs) == 2
        assert "119" in guardian_msgs[1]["situation_label"]
        resident_guidance = [
            p for p in _to(transport, TOPIC) if "119" in str(p.get("situation_label", ""))
        ]
        assert len(resident_guidance) == 1
        ladder = await svc.safety.get_ladder(sid)
        assert ladder is not None and ladder.guidance_119_sent is True

        # 안내는 한 번뿐이다. 송신은 알림 토픽 두 곳뿐(전화·신고 경로 없음).
        await _advance(svc, clock, 1000)
        assert [k for _, k in await _forwards(svc)].count("guidance_119") == 1
        assert {t for t, _ in transport.sent} == {TOPIC, gtopic}
    finally:
        await db.close()


async def test_zero_guardians_gets_119_at_90s_plus_5min() -> None:
    svc, db, transport, clock = await _make()
    try:
        res = await _open(svc)
        sid = res.proposal.situation_id
        await _advance(svc, clock, 90)
        await _advance(svc, clock, 389)
        assert await _forwards(svc) == []
        await _advance(svc, clock, 390)
        assert await _forwards(svc) == [(None, "guidance_119")]
        guidance = [p for p in _to(transport, TOPIC) if "situation_label" in p]
        assert len(guidance) == 1 and "119" in guidance[0]["situation_label"]
        ladder = await svc.safety.get_ladder(sid)
        assert ladder is not None and ladder.guidance_119_sent is True
        # 재알림은 그 뒤에도 1분마다 계속된다.
        before = len(_to(transport, TOPIC))
        await _advance(svc, clock, 420)
        assert len(_to(transport, TOPIC)) == before + 1
    finally:
        await db.close()


# ==========================================================================
# 2) 재알림 주기: CRITICAL 1분, WARNING 10분 × 최대 3회
# ==========================================================================
async def test_critical_realert_every_minute() -> None:
    svc, db, transport, clock = await _make()
    try:
        await _open(svc)
        assert len(transport.sent) == 1  # 첫 알림
        await _advance(svc, clock, 59)
        assert len(transport.sent) == 1
        await _advance(svc, clock, 60)
        assert len(transport.sent) == 2
        _, realert = transport.sent[1]
        assert realert["severity_label"] == SeverityLevel.CRITICAL.label_ko
        await _advance(svc, clock, 119)
        assert len(transport.sent) == 2
        await _advance(svc, clock, 120)
        assert len(transport.sent) == 3
        await _advance(svc, clock, 180)
        assert len(transport.sent) == 4
    finally:
        await db.close()


async def test_warning_realert_every_ten_minutes_at_most_three_times() -> None:
    svc, db, transport, clock = await _make()
    try:
        _, _, gtopic = await _guardian(svc)
        res = await _open(svc, DOOR)
        assert res.proposal.severity == SeverityLevel.WARNING
        assert len(transport.sent) == 1
        await _advance(svc, clock, 599)
        assert len(transport.sent) == 1
        for n, t in enumerate((600, 1200, 1800), start=2):
            await _advance(svc, clock, t)
            assert len(transport.sent) == n
            assert transport.sent[-1][1]["severity_label"] == SeverityLevel.WARNING.label_ko
        await _advance(svc, clock, 2400)
        await _advance(svc, clock, 9000)
        assert len(transport.sent) == 4  # 첫 알림 + 재알림 3회
        ladder = await svc.safety.get_ladder(res.proposal.situation_id)
        assert ladder is not None and ladder.next_fire_at is None
        # WARNING 은 보호자·119 가 없다.
        assert _to(transport, gtopic) == []
        assert await _forwards(svc) == []
    finally:
        await db.close()


async def test_notice_and_record_have_no_ladder() -> None:
    assert escalation.realert_interval(SeverityLevel.NOTICE) is None
    assert escalation.realert_interval(SeverityLevel.RECORD) is None
    assert escalation.needs_ladder(SeverityLevel.NOTICE, SeverityLevel.NOTICE) is False
    assert escalation.needs_ladder(SeverityLevel.RECORD, SeverityLevel.RECORD) is False
    assert escalation.needs_ladder(SeverityLevel.WARNING, SeverityLevel.WARNING) is True
    assert escalation.realert_interval(SeverityLevel.CRITICAL) == timedelta(seconds=60)
    assert escalation.realert_interval(SeverityLevel.WARNING) == timedelta(minutes=10)


# ==========================================================================
# 3) 재알림 게이트: CRITICAL 은 헌장 OFF 만, WARNING 은 헌장 OFF·음소거(중복 게이트 없음)
# ==========================================================================
async def test_critical_realert_ignores_mute_but_respects_charter_off() -> None:
    svc, db, transport, clock = await _make()
    try:
        await _open(svc)
        await svc.prefs.set("muted_categories", ["gas"])
        await _advance(svc, clock, 60)
        assert len(transport.sent) == 2  # 음소거를 우회
        await svc.prefs.set("charter_enabled", False)
        await _advance(svc, clock, 120)
        assert len(transport.sent) == 2  # 헌장 OFF 는 존중
        await svc.prefs.set("charter_enabled", True)
        await _advance(svc, clock, 180)
        assert len(transport.sent) == 3  # 사다리는 살아 있었다
    finally:
        await db.close()


async def test_warning_realert_respects_mute_and_skips_dedup() -> None:
    svc, db, transport, clock = await _make()
    try:
        await _open(svc, DOOR)
        # 첫 알림이 dedup 창(2시간)에 들어 있어도 재알림은 나간다.
        await _advance(svc, clock, 600)
        assert len(transport.sent) == 2
        await svc.prefs.set("muted_entities", [DOOR])
        await _advance(svc, clock, 1200)
        assert len(transport.sent) == 2
        await svc.prefs.set("muted_entities", [])
        await svc.prefs.set("charter_enabled", False)
        await _advance(svc, clock, 1800)
        assert len(transport.sent) == 2
    finally:
        await db.close()


# ==========================================================================
# 4) 확인(ack): 거주자 명시 응답 / 보호자 서명 링크
# ==========================================================================
@pytest.mark.parametrize("action", ["accept", "reject", "dismiss", "snooze", "ok"])
async def test_resident_explicit_response_stops_the_ladder(action: str) -> None:
    svc, db, transport, clock = await _make()
    try:
        _, _, gtopic = await _guardian(svc)
        res = await _open(svc)
        clock.set_fixed(_at(10))
        status, _ = await _resident_click(svc, res.proposal, action)
        assert status == "200 OK"
        ladder = await svc.safety.get_ladder(res.proposal.situation_id)
        assert ladder is not None and ladder.acked is True and ladder.acked_by == "resident"
        assert ladder.acked_at == _at(10)
        for t in (60, 90, 400, 1000):
            await _advance(svc, clock, t)
        assert len(transport.sent) == 1  # 첫 알림뿐
        assert _to(transport, gtopic) == []
        assert await _forwards(svc) == []
        assert await svc.safety.list_open_ladders() == []
    finally:
        await db.close()


async def test_guardian_signed_ack_link_stops_the_ladder() -> None:
    svc, db, transport, clock = await _make()
    try:
        _, gid, gtopic = await _guardian(svc)
        res = await _open(svc)
        sid = res.proposal.situation_id
        await _advance(svc, clock, 90)
        q = _ack_query(_to(transport, gtopic)[0])
        assert q["s"] == sid and q["g"] == gid
        assert verify_guardian_ack_sig(SECRET, sid, gid, q["sig"])

        # 위조 서명·다른 보호자 id 는 거부된다.
        bad = await svc._route_feedback(f"GET /guardian-ack?s={sid}&g={gid}&sig=00 HTTP/1.1")
        assert bad[0] == "403 Forbidden"
        other = await svc._route_feedback(
            f"GET /guardian-ack?s={sid}&g=someone-else&sig={q['sig']} HTTP/1.1"
        )
        assert other[0] == "403 Forbidden"
        assert (await svc.safety.get_ladder(sid)).acked is False

        clock.set_fixed(_at(100))
        ok = await svc._route_feedback(
            f"GET /guardian-ack?s={sid}&g={gid}&sig={q['sig']} HTTP/1.1"
        )
        assert ok[0] == "200 OK"
        ladder = await svc.safety.get_ladder(sid)
        assert ladder is not None and ladder.acked is True
        assert ladder.acked_by == f"guardian:{gid}"
        sent_before = len(transport.sent)
        for t in (120, 400, 1000):
            await _advance(svc, clock, t)
        assert len(transport.sent) == sent_before  # 재알림도 119 도 없다
        assert (None, "guidance_119") not in await _forwards(svc)
    finally:
        await db.close()


async def test_guardian_ack_link_works_over_http() -> None:
    import asyncio

    svc, db, transport, clock = await _make()
    server = await svc.serve_feedback("127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    try:
        _, gid, gtopic = await _guardian(svc)
        res = await _open(svc)
        await _advance(svc, clock, 90)
        q = _ack_query(_to(transport, gtopic)[0])
        reader, writer = await asyncio.open_connection("127.0.0.1", port)
        writer.write(
            f"GET /guardian-ack?s={q['s']}&g={q['g']}&sig={q['sig']} HTTP/1.1\r\n\r\n".encode()
        )
        await writer.drain()
        data = (await reader.read()).decode("utf-8", "replace")
        writer.close()
        assert "200 OK" in data
        assert (await svc.safety.get_ladder(res.proposal.situation_id)).acked is True
    finally:
        server.close()
        await server.wait_closed()
        await db.close()


async def test_opening_a_notification_is_not_an_ack() -> None:
    """알림을 탭(Click)해 여는 것은 확인이 아니다 — 확인은 별도 액션 버튼에서만."""
    svc, db, transport, clock = await _make()
    try:
        _, _, gtopic = await _guardian(svc)
        res = await _open(svc)
        await _advance(svc, clock, 90)
        http = HTTPNtfyTransport(
            "https://ntfy.example", feedback_base_url="https://fb.example",
            signing_secret=SECRET,
        )
        guardian_headers = http._headers(_to(transport, gtopic)[0])
        assert "guardian-ack" not in guardian_headers.get("Click", "")
        assert "https://fb.example/guardian-ack?" in guardian_headers["Actions"]
        resident_headers = http._headers(transport.sent[0][1])
        assert resident_headers["Click"].startswith("liot://")
        assert "a=ok" in resident_headers["Actions"]  # '괜찮아요' 버튼
        # 아무도 버튼을 누르지 않았다 → 사다리는 그대로 열려 있다.
        assert (await svc.safety.get_ladder(res.proposal.situation_id)).acked is False
    finally:
        await db.close()


# ==========================================================================
# 5) 보호자 조건 = 동의 AND 하한 CRITICAL
# ==========================================================================
async def test_model_only_critical_gets_fast_realerts_but_no_guardian_or_119() -> None:
    svc, db, transport, clock = await _make(backend=_Backend(SeverityLevel.CRITICAL))
    try:
        _, _, gtopic = await _guardian(svc)
        res = await _open(svc, DOOR)
        assert res.proposal.severity == SeverityLevel.WARNING
        assert len(transport.sent) == 2  # 첫 알림 + 모델 상향 후속
        assert transport.sent[1][1]["severity_label"] == SeverityLevel.CRITICAL.label_ko

        await _advance(svc, clock, 60)  # 최종 파급도(CRITICAL) → 1분 재알림
        assert len(transport.sent) == 3
        assert transport.sent[2][1]["severity_label"] == SeverityLevel.CRITICAL.label_ko
        for t in (90, 120, 390, 400):
            await _advance(svc, clock, t)
        assert _to(transport, gtopic) == []
        assert await _forwards(svc) == []
        ladder = await svc.safety.get_ladder(res.proposal.situation_id)
        assert ladder is not None and ladder.guidance_119_sent is False
    finally:
        await db.close()


async def test_no_consent_means_no_forward() -> None:
    svc, db, transport, clock = await _make()
    try:
        _, gid, gtopic = await _guardian(svc, consent=False)
        await _open(svc)
        await _advance(svc, clock, 90)
        await _advance(svc, clock, 200)
        assert _to(transport, gtopic) == []
        assert all(g is None for g, _ in await _forwards(svc))
        # 동의한 보호자가 없으면 거주자 119 안내 경로를 탄다.
        await _advance(svc, clock, 390)
        assert await _forwards(svc) == [(None, "guidance_119")]
        assert _to(transport, gtopic) == []
    finally:
        await db.close()


async def test_consent_revoked_mid_ladder_stops_sends_to_that_guardian() -> None:
    svc, db, transport, clock = await _make()
    try:
        rid, gid, gtopic = await _guardian(svc)
        _, gid2, gtopic2 = await _guardian_second(svc, rid)
        res = await _open(svc)
        await _advance(svc, clock, 90)
        assert len(_to(transport, gtopic)) == 1 and len(_to(transport, gtopic2)) == 1
        await svc.safety.revoke_consent(HOME, gid, rid)
        await _advance(svc, clock, 390)
        assert len(_to(transport, gtopic)) == 1  # 철회한 보호자에게는 더 안 간다
        assert len(_to(transport, gtopic2)) == 2  # 남은 보호자는 119 안내를 받는다
        # 해소 알림도 철회한 보호자에게는 가지 않는다.
        await svc.handle_reading(_reading(GAS, "off"))
        assert len(_to(transport, gtopic)) == 1
        assert len(_to(transport, gtopic2)) == 3
        kinds = await _forwards(svc)
        assert (gid, "resolved") not in kinds and (gid2, "resolved") in kinds
        assert res.proposal.situation_id is not None
    finally:
        await db.close()


async def _guardian_second(svc: LiotService, rid: str) -> tuple[str, str, str]:
    gid, topic = await svc.safety.add_guardian(HOME, "아들")
    await svc.safety.grant_consent(HOME, gid, rid)
    return rid, gid, topic


# ==========================================================================
# 6) 해소: 사다리 취소 + 전달받은 보호자에게 '해소됨'
# ==========================================================================
async def test_close_cancels_the_ladder_and_resolves_forwarded_guardians() -> None:
    svc, db, transport, clock = await _make()
    try:
        _, gid, gtopic = await _guardian(svc)
        res = await _open(svc)
        sid = res.proposal.situation_id
        await _advance(svc, clock, 90)
        clock.set_fixed(_at(120))
        await svc.handle_reading(_reading(GAS, "off"))
        ladder = await svc.safety.get_ladder(sid)
        assert ladder is not None and ladder.cancelled is True and ladder.closed is True
        assert await svc.safety.list_open_ladders() == []
        msgs = _to(transport, gtopic)
        assert len(msgs) == 2
        assert "해소" in msgs[1]["situation_label"]
        # 해소 알림의 파급도는 NOTICE(주의) — 최고 우선순위로 가족을 다시 깨우지 않는다.
        assert msgs[1]["severity_label"] == "주의"
        assert msgs[1].get("signed_deeplink") in (None, "")
        assert await _forwards(svc) == [(gid, "forward"), (gid, "resolved")]
        sent = len(transport.sent)
        for t in (180, 400, 1000):
            await _advance(svc, clock, t)
        assert len(transport.sent) == sent
    finally:
        await db.close()


async def test_close_before_forward_sends_no_guardian_message() -> None:
    svc, db, transport, clock = await _make()
    try:
        _, _, gtopic = await _guardian(svc)
        await _open(svc)
        clock.set_fixed(_at(30))
        await svc.handle_reading(_reading(GAS, "off"))
        await _advance(svc, clock, 90)
        assert _to(transport, gtopic) == []
        assert await _forwards(svc) == []
    finally:
        await db.close()


# ==========================================================================
# 7) 재시작: DB 에서 다시 무장 / 만료는 사다리를 취소하지 않는다
# ==========================================================================
async def test_restart_rearms_open_ladders_from_the_db() -> None:
    svc, db, transport, clock = await _make()
    try:
        _, gid, gtopic = await _guardian(svc)
        res = await _open(svc)
        sid = res.proposal.situation_id
        clock.set_fixed(_at(30))
        # 새 프로세스: 같은 DB, 새 서비스·새 transport. 메모리 상태는 없다.
        svc2, _, transport2, _ = await _make(db=db, clock=clock)
        await svc2.start()
        assert [ld.situation_id for ld in await svc2.safety.list_open_ladders()] == [sid]
        await _advance(svc2, clock, 60)
        assert len(transport2.sent) == 1  # 재알림이 새 프로세스에서 나간다
        assert transport2.sent[0][0] == TOPIC
        await _advance(svc2, clock, 90)
        assert len(_to(transport2, gtopic)) == 1
        assert await _forwards(svc2) == [(gid, "forward")]
        assert transport.sent and len(transport.sent) == 1  # 옛 프로세스는 첫 알림뿐
    finally:
        await db.close()


async def test_restart_after_close_reconciles_a_dangling_ladder() -> None:
    """닫힌 상황의 사다리가 열려 남아 있으면(닫기 직후 죽음) 시작 때 정리한다."""
    svc, db, transport, clock = await _make()
    try:
        await _open(svc)
        res_sid = (await svc.safety.list_open_ladders())[0].situation_id
        await svc.safety.close_situation(res_sid, _at(10))
        svc2, _, transport2, _ = await _make(db=db, clock=clock)
        await svc2.start()
        assert await svc2.safety.list_open_ladders() == []
        await _advance(svc2, clock, 60)
        assert transport2.sent == []
    finally:
        await db.close()


async def test_proposal_expiry_does_not_cancel_the_ladder() -> None:
    svc, db, transport, clock = await _make()
    try:
        res = await _open(svc)
        pid = res.proposal.proposal_id
        await db.execute(
            "UPDATE proposals SET expires_at = ? WHERE proposal_id = ?", (_at(5), pid)
        )
        await db.commit()
        clock.set_fixed(_at(30))
        await svc.run_tick()
        assert (await svc.proposals.get(pid)).state == "expired"
        await _advance(svc, clock, 60)
        assert len(transport.sent) == 2  # 재알림은 계속된다
        ladder = await svc.safety.get_ladder(res.proposal.situation_id)
        assert ladder is not None and not ladder.acked and not ladder.cancelled
        # 만료된 제안의 재알림 링크로도 확인할 수 있다(상태는 바뀌지 않는다).
        status, body = await _resident_click(svc, res.proposal, "ok")
        assert status == "200 OK", body
        assert (await svc.safety.get_ladder(res.proposal.situation_id)).acked is True
        assert (await svc.proposals.get(pid)).state == "expired"
    finally:
        await db.close()


# ==========================================================================
# 8) 페이로드: 특성·기기·방·근거 없음, egress 통과 / 토픽은 로그에 없다
# ==========================================================================
async def test_guardian_and_resident_payloads_carry_no_identifying_fields() -> None:
    svc, db, transport, clock = await _make()
    try:
        rid, _, gtopic = await _guardian(svc)
        for trait in ("age_75_plus", "cannot_self_evacuate"):
            await svc.safety.set_trait(rid, trait, True, "resident")
        await svc.safety.set_lives_alone(HOME, True)
        await svc.safety.set_home_alias(HOME, "할머니 댁")
        await _open(svc)
        for t in (60, 90, 390):
            await _advance(svc, clock, t)
        await svc.handle_reading(_reading(GAS, "off"))

        guardian_msgs = _to(transport, gtopic)
        assert len(guardian_msgs) == 3  # 전달·119 안내·해소
        forbidden_keys = {"device", "entity_id", "room", "rationale", "lives_alone",
                          "proposal_id", "topic", *PERSON_TRAITS}
        for p in guardian_msgs:
            assert set(p) <= GUARDIAN_PAYLOAD_KEYS
            assert {"situation_label", "home_alias", "at", "severity_label"} <= set(p)
            assert p["home_alias"] == "할머니 댁"
            assert p["situation_label"].startswith("가스 누출")
        for _, p in transport.sent:
            assert set(p) <= NTFY_ALLOWED_KEYS
            assert not (set(p) & (forbidden_keys - {"proposal_id", "topic"}))
            dumped = json.dumps(p, ensure_ascii=False)
            assert GAS not in dumped and "kitchen" not in dumped
            for trait in (*PERSON_TRAITS, "lives_alone", "75세", "대피"):
                assert trait not in dumped
            assert gtopic not in dumped
        for p in guardian_msgs:
            assert not (set(p) & forbidden_keys)
    finally:
        await db.close()


def test_egress_allows_only_the_new_guardian_keys() -> None:
    assert {"situation_label", "home_alias", "at"} <= NTFY_ALLOWED_KEYS
    for trait in (*PERSON_TRAITS, "lives_alone", "signed_ack_link", "guardian_id"):
        assert trait not in NTFY_ALLOWED_KEYS


async def test_guardian_topic_never_appears_in_logs(caplog: pytest.LogCaptureFixture) -> None:
    clock = Clock(fixed=T0)
    db = await init_db(":memory:")
    try:
        svc, _, _, _ = await _make(db=db, clock=clock)
        _, _, gtopic = await _guardian(svc)
        # 등록(add_guardian) 뒤부터 모든 로거를 DEBUG 로 본다 — 사다리 전 구간(전달 실패
        # 포함)에서 토픽이 한 번도 찍히지 않아야 한다.
        caplog.set_level(logging.DEBUG)
        failing = _SelectiveFailTransport({gtopic})
        svc.transport = failing
        await _open(svc)
        for t in (60, 90, 390):
            await _advance(svc, clock, t)
        await svc.handle_reading(_reading(GAS, "off"))
        assert _to(failing, gtopic) == []
        assert gtopic not in caplog.text
        # 실패한 전달은 기록되지 않는다(배달되지 않았다).
        assert all(kind != "forward" for _, kind in await _forwards(svc))
    finally:
        await db.close()


# ==========================================================================
# 9) '괜찮아요'(ok): 사다리만 멈추고 선호 학습 신호는 없다
# ==========================================================================
async def test_ok_records_no_preference_signal() -> None:
    svc, db, transport, clock = await _make()
    try:
        res = await _open(svc)
        pid = res.proposal.proposal_id
        svc._prop_cell[pid] = "cell-under-test"  # 추적 중이어도 신호가 없어야 한다
        cells_before = await load_cells(svc.prefs)
        rate_before = await svc.proposals.accept_rate()
        status, body = await _resident_click(svc, res.proposal, "ok")
        assert status == "200 OK", body
        assert (await svc.proposals.get(pid)).state == "pending"
        assert pid not in svc._pref_signaled
        assert await load_cells(svc.prefs) == cells_before
        assert await svc.proposals.accept_rate() == rate_before
        assert await svc.proposals.recently_rejected_keys() == frozenset()
        assert (await svc.safety.get_ladder(res.proposal.situation_id)).acked is True
    finally:
        await db.close()


def test_ok_is_a_known_feedback_action_without_state_change() -> None:
    assert FeedbackGateway._ACTION_ALIASES["ok"] == "ok"
    assert "ok" in FEEDBACK_STATE_MAP and FEEDBACK_STATE_MAP["ok"] is None


# ==========================================================================
# 10) 사다리 루프(틱과 별개, ≤5초 해상도)
# ==========================================================================
async def test_ladder_loop_fires_due_ladders() -> None:
    svc, db, transport, clock = await _make()
    try:
        await _open(svc)
        clock.set_fixed(_at(60))
        await svc._ladder_loop(resolution_s=0.0, max_iterations=2)
        assert len(transport.sent) == 2  # 두 번 돌아도 재알림은 한 번
        assert escalation.LADDER_RESOLUTION_S <= 5.0
    finally:
        await db.close()


async def test_run_starts_and_stops_the_ladder_loop() -> None:
    svc, db, transport, clock = await _make()
    try:
        await _open(svc)
        clock.set_fixed(_at(60))
        await svc.run(None, tick_seconds=0.0, max_ticks=1, ladder_resolution_s=0.0)
        assert len(transport.sent) >= 1  # run 은 틱 루프가 끝나면 돌아온다
    finally:
        await db.close()


def test_guardian_ack_link_shape_and_signature() -> None:
    link = guardian_ack_link(SECRET, "sit-1", "g-1")
    parts = urlsplit(link)
    assert parts.scheme == "liot" and parts.netloc == "ack" and parts.path == "/sit-1"
    q = parse_qs(parts.query)
    assert q["g"] == ["g-1"]
    assert verify_guardian_ack_sig(SECRET, "sit-1", "g-1", q["sig"][0])
    assert not verify_guardian_ack_sig(SECRET, "sit-1", "g-2", q["sig"][0])
    assert not verify_guardian_ack_sig(b"other", "sit-1", "g-1", q["sig"][0])
    assert not verify_guardian_ack_sig(SECRET, "sit-1", "g-1", "é")


def test_situation_labels_are_korean_kind_labels() -> None:
    assert escalation.situation_label("gas") == "가스 누출"
    assert escalation.situation_label("smoke") == "연기·화재"
    assert escalation.situation_label("unknown_cat") == "안전 상황"


# ==========================================================================
# 11) 보호자 확인 링크의 거부 경로 · 송신 실패 재시도 · 별칭 PII 대체 · sanitize-only
# ==========================================================================
async def test_guardian_ack_rejections_and_idempotence() -> None:
    from liot.layers.l4_execution.notifier import sign_guardian_ack

    svc, db, transport, clock = await _make()
    try:
        rid, gid, gtopic = await _guardian(svc)
        res = await _open(svc)
        sid = res.proposal.situation_id
        sig = sign_guardian_ack(SECRET, sid, gid)
        # 아직 전달받지 않은 보호자(90초 전)는 확인할 수 없다.
        early = await svc._route_feedback(f"GET /guardian-ack?s={sid}&g={gid}&sig={sig} HTTP/1.1")
        assert early[0] == "403 Forbidden"
        # 없는 상황(서명은 맞다).
        ghost_sig = sign_guardian_ack(SECRET, "no-such", gid)
        ghost = await svc._route_feedback(
            f"GET /guardian-ack?s=no-such&g={gid}&sig={ghost_sig} HTTP/1.1"
        )
        assert ghost[0] == "404 Not Found"
        await _advance(svc, clock, 90)
        # 동의를 철회한 보호자의 링크는 더 이상 통하지 않는다.
        await svc.safety.revoke_consent(HOME, gid, rid)
        revoked = await svc._route_feedback(
            f"GET /guardian-ack?s={sid}&g={gid}&sig={sig} HTTP/1.1"
        )
        assert revoked == ("403 Forbidden", "consent revoked")
        await svc.safety.grant_consent(HOME, gid, rid)
        first = await svc._route_feedback(f"GET /guardian-ack?s={sid}&g={gid}&sig={sig} HTTP/1.1")
        again = await svc._route_feedback(f"GET /guardian-ack?s={sid}&g={gid}&sig={sig} HTTP/1.1")
        assert first == ("200 OK", "ok: acknowledged")
        assert again == ("200 OK", "ok: already acknowledged")
        assert gtopic not in first[1] + again[1]
    finally:
        await db.close()


async def test_failed_forward_and_guidance_are_retried() -> None:
    svc, db, _, clock = await _make()
    try:
        _, gid, gtopic = await _guardian(svc)
        transport = _SelectiveFailTransport({gtopic, TOPIC})
        svc.transport = transport
        res = await _open(svc)
        sid = res.proposal.situation_id
        await _advance(svc, clock, 90)
        assert await _forwards(svc) == []  # 배달되지 않은 전달은 기록하지 않는다
        ladder = await svc.safety.get_ladder(sid)
        assert ladder is not None and ladder.notified_guardian_ids == ()
        assert ladder.next_fire_at == _at(90 + escalation.LADDER_RETRY.total_seconds())
        transport.fail_topics = {TOPIC}
        await _advance(svc, clock, 120)
        assert await _forwards(svc) == [(gid, "forward")]
        # 119: 거주자 송신은 실패해도 보호자에게 나가면 한 번 기록된다.
        await _advance(svc, clock, 420)
        assert (None, "guidance_119") in await _forwards(svc)
        # 모두 실패하면 안내 단계는 끝나지 않고 다시 시도한다.
        svc2, db2, _, clock2 = await _make()
        try:
            res2 = await _open(svc2)
            svc2.transport = _SelectiveFailTransport({TOPIC})
            await _advance(svc2, clock2, 390)
            ladder2 = await svc2.safety.get_ladder(res2.proposal.situation_id)
            assert ladder2 is not None and ladder2.guidance_119_sent is False
            svc2.transport = FakeNtfyTransport()
            await _advance(svc2, clock2, 420)
            assert (await svc2.safety.get_ladder(res2.proposal.situation_id)).guidance_119_sent
        finally:
            await db2.close()
    finally:
        await db.close()


async def test_home_alias_rejected_by_egress_falls_back_to_default_alias() -> None:
    svc, db, transport, clock = await _make()
    try:
        _, _, gtopic = await _guardian(svc)
        await svc.safety.set_home_alias(HOME, "010-1234-5678 집")  # PII 스캔에 걸린다
        await _open(svc)
        await _advance(svc, clock, 90)
        msgs = _to(transport, gtopic)
        assert len(msgs) == 1 and msgs[0]["home_alias"] == "우리 집"
        assert "010" not in json.dumps(msgs, ensure_ascii=False)
    finally:
        await db.close()


async def test_sanitize_only_mode_runs_the_ladder_without_sending_or_recording() -> None:
    svc, db, _, clock = await _make()
    try:
        await _guardian(svc)
        svc.transport = None
        res = await _open(svc)
        for t in (60, 90, 390):
            await _advance(svc, clock, t)
        assert await _forwards(svc) == []
        ladder = await svc.safety.get_ladder(res.proposal.situation_id)
        assert ladder is not None and not ladder.acked and ladder.next_fire_at is not None
    finally:
        await db.close()


async def test_ladder_failure_is_logged_and_does_not_stop_the_tick(monkeypatch) -> None:
    svc, db, transport, clock = await _make()
    try:
        await _open(svc)

        async def boom(_sid: str) -> None:
            raise RuntimeError("boom")

        monkeypatch.setattr(svc, "_fire_ladder", boom)
        clock.set_fixed(_at(60))
        assert await svc.ladder_tick() == 1
        monkeypatch.setattr(svc, "ladder_tick", boom)
        await svc._ladder_loop(resolution_s=0.0, max_iterations=1)  # 죽지 않는다
    finally:
        await db.close()


def test_schedule_edges() -> None:
    opened = datetime.fromisoformat(T0)
    assert escalation.realerts_due(opened, SeverityLevel.CRITICAL, opened - timedelta(1)) == 0
    assert escalation.realerts_due(opened, SeverityLevel.NOTICE, opened + timedelta(1)) == 0
    assert escalation.next_realert_at(opened, SeverityLevel.NOTICE, 0) is None
    assert escalation.realerts_due(opened, SeverityLevel.WARNING, opened + timedelta(1)) == 3
    assert escalation.parse_iso("2026-06-02T12:00:00Z") == opened
    assert escalation.to_iso(None) is None
    late = opened + timedelta(seconds=200)
    assert escalation.guidance_due_at(opened, late) == late + timedelta(minutes=5)
    assert escalation.guidance_due_at(opened, None) == opened + timedelta(seconds=390)
