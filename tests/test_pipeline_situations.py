"""T7: 파이프라인 연결 — 상황 에피소드·파급도 하한·표 우선 두 단계 발송·판단 저장.

축(spec §3.4.1·§3.5·§3.6·§6.2·§11):
  1) 이상 신호: 열린 상황이 없으면 열고 알림 제안 1개. 열려 있으면 같은 상황을
     이어 가고 새 알림 제안은 없다.
  2) 정상 신호: 열린 상황을 닫고 거주자에게 '해소됨'(NOTICE)을 보낸다. 새 `alert`
     는 없고, 그 상황의 대기 중 제안(원탭 링크 포함)은 만료된다. 열린 상황이
     없으면 아무 일도 없다.
  3) 두 단계: 1단계 제안(표 하한)은 모델 응답 전에 나간다. 2단계 판정은 별도
     스레드에서 돌고, 모델이 올렸을 때만 같은 situation_id 의 후속 제안이 나간다.
     2단계 판단 행은 올렸든 아니든(낮춘 답·제한시간 초과 포함) 항상 저장된다.
  4) 컴포트 경로는 예전과 같다(파급도 없음, 상황 없음) — 판단 행만 새로 남는다.
  5) 표 로드 실패는 시작 시 audit_log 에 한 번 남는다.
"""

from __future__ import annotations

import asyncio
import json
import threading
from typing import Any

import pytest

from liot.app import (
    PENDING_GATE2_VERDICT,
    PENDING_MODEL_ID,
    LiotApp,
)
from liot.core.clock import Clock
from liot.core.contracts import CandidateAction, ContextSnapshot, Judgment, SensorReading
from liot.core.risk import RiskTier
from liot.core.severity import SeverityLevel, load_severity_table
from liot.db.database import init_db
from liot.layers.l1_sensing.normalizer import normalize
from liot.layers.l2_context.proactive import ProactiveGenerator
from liot.layers.l2_context.proactive_rules import load_proactive_ruleset
from liot.layers.l3_judgment.llm_judge import LLMResult
from liot.layers.l4_execution.ntfy_transport import FakeNtfyTransport
from liot.layers.l4_execution.risk_router import RiskRouter
from liot.service import LiotService

DAY = "2026-06-02T12:00:00+00:00"
SECRET = b"test-secret-key"
GAS = "binary_sensor.kitchen_gas"
DOOR = "binary_sensor.front_door"
CATS = {
    "binary_sensor.living_motion": "motion",
    "light.living_room": "light",
    GAS: "gas",
    DOOR: "door",
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


# ── 테스트용 백엔드 ──────────────────────────────────────────────────────────
class _FixedBackend:
    """정해진 tier·파급도를 바로 돌려주는 백엔드. 받은 입력과 실행 스레드를 기록한다."""

    model_id = "fixed-test-model"

    def __init__(self, *, tier: RiskTier = RiskTier.EMBER,
                 severity: SeverityLevel | None = None) -> None:
        self.tier = tier
        self.severity = severity
        self.calls: list[dict[str, Any]] = []
        self.thread_ids: list[int] = []

    def judge_raw(self, candidate: CandidateAction, ctx: ContextSnapshot | None,
                  gate1_floor: RiskTier,
                  severity_floor: SeverityLevel | None = None) -> LLMResult:
        self.thread_ids.append(threading.get_ident())
        self.calls.append({"candidate": candidate, "ctx": ctx, "severity_floor": severity_floor})
        return LLMResult(
            tier=self.tier, confidence=0.8, rationale="test", model_id=self.model_id,
            severity=self.severity if severity_floor is not None else None,
            probs={"severity": {"A": 0.1, "B": 0.9}} if severity_floor is not None else None,
        )


class _BlockingBackend(_FixedBackend):
    """테스트가 풀어 줄 때까지 응답을 막는 백엔드(모델이 늦는 상황)."""

    model_id = "blocking-test-model"

    def __init__(self, **kw: Any) -> None:
        super().__init__(**kw)
        self.started = threading.Event()
        self.release = threading.Event()

    def judge_raw(self, candidate: CandidateAction, ctx: ContextSnapshot | None,
                  gate1_floor: RiskTier,
                  severity_floor: SeverityLevel | None = None) -> LLMResult:
        self.started.set()
        self.release.wait(timeout=10)
        return super().judge_raw(candidate, ctx, gate1_floor, severity_floor)


class _RecordingService(LiotService):
    """T8 확장 지점(열림·닫힘 훅) 호출을 기록한다."""

    def __init__(self, *a: Any, **kw: Any) -> None:
        super().__init__(*a, **kw)
        self.opened: list[tuple[str, str]] = []
        self.closed: list[str] = []
        self.sent_at_close: list[int] = []

    async def _on_situation_opened(self, situation_id: str, proposal: Any) -> None:
        self.opened.append((situation_id, proposal.proposal_id))

    async def _on_situation_closed(self, situation_id: str) -> None:
        self.closed.append(situation_id)
        transport = self.transport
        assert isinstance(transport, FakeNtfyTransport)
        self.sent_at_close.append(len(transport.sent))


# ── 도우미 ───────────────────────────────────────────────────────────────────
def _reading(eid: str, raw: object, ts: str = DAY) -> SensorReading:
    return SensorReading(
        entity_id=eid, source="test", raw_value=raw, norm_value=normalize(raw),
        ts_utc=ts, home_id="singleton",
    )


async def _make(tmp_path, *, backend: Any = None, table: Any = None,
                deadline: float | None = None, transport: Any = None):
    clock = Clock(fixed=DAY)
    db = await init_db(":memory:")
    p = tmp_path / "comfort.yaml"
    p.write_text(COMFORT_YAML, encoding="utf-8")
    gen = ProactiveGenerator(ruleset=load_proactive_ruleset(p), categories=CATS, clock=clock)
    app = LiotApp(clock=clock, categories=CATS, proactive=gen,
                  llm_backend=backend if backend is not None else _FixedBackend(),
                  severity_table=table)
    transport = transport if transport is not None else FakeNtfyTransport()
    svc = _RecordingService(app, db, clock=clock, transport=transport, topic="liot-test",
                            secret=SECRET, phase2_deadline_s=deadline)
    return svc, db, transport


async def _judgments(db) -> list[dict[str, Any]]:
    cur = await db.execute(
        "SELECT j.judgment_id, j.model_id, j.gate2_verdict, j.degraded, j.severity,"
        " j.severity_floor, j.severity_sources, j.situation_id, j.snapshot_hash,"
        " j.model_probs, j.risk_tier FROM judgments AS j ORDER BY j.rowid"
    )
    rows = await cur.fetchall()
    await cur.close()
    keys = ("judgment_id", "model_id", "gate2_verdict", "degraded", "severity",
            "severity_floor", "severity_sources", "situation_id", "snapshot_hash",
            "model_probs", "risk_tier")
    return [dict(zip(keys, tuple(r), strict=True)) for r in rows]


async def _proposals(db) -> list[dict[str, Any]]:
    cur = await db.execute(
        "SELECT p.proposal_id, p.judgment_id, p.state, p.severity, p.situation_id,"
        " p.action_json, p.risk_tier FROM proposals AS p ORDER BY p.rowid"
    )
    rows = await cur.fetchall()
    await cur.close()
    out = []
    for r in rows:
        d = dict(zip(("proposal_id", "judgment_id", "state", "severity", "situation_id",
                      "action_json", "risk_tier"), tuple(r), strict=True))
        d["action"] = json.loads(d.pop("action_json"))["action"]
        out.append(d)
    return out


async def _situations(db) -> list[dict[str, Any]]:
    cur = await db.execute(
        "SELECT s.situation_id, s.category, s.entity_id, s.closed_at, s.severity_floor"
        " FROM situations AS s ORDER BY s.rowid"
    )
    rows = await cur.fetchall()
    await cur.close()
    return [dict(zip(("situation_id", "category", "entity_id", "closed_at", "severity_floor"),
                     tuple(r), strict=True)) for r in rows]


async def _wait_until(pred, timeout: float = 5.0) -> None:
    loop = asyncio.get_running_loop()
    end = loop.time() + timeout
    while not pred():
        if loop.time() > end:
            raise AssertionError("condition not met in time")
        await asyncio.sleep(0.005)


# ==========================================================================
# 1) 표 우선: 모델이 답하기 전에 CRITICAL 제안이 나간다
# ==========================================================================
async def test_gas_alert_is_dispatched_before_the_model_answers(tmp_path) -> None:
    backend = _BlockingBackend()
    svc, db, transport = await _make(tmp_path, backend=backend)
    try:
        # handle_reading 은 1단계 발송 직후 돌아온다 — 모델 판정을 기다리지 않는다.
        res = await asyncio.wait_for(svc.handle_reading(_reading(GAS, "on")), timeout=5)
        assert len(transport.sent) == 1
        # 모델은 아직 막혀 있다 — 그래도 첫 알림은 이미 나갔다.
        assert await asyncio.to_thread(backend.started.wait, 5)
        assert len(svc._phase2_tasks) == 1
        assert len(await _judgments(db)) == 1  # 1단계 행만(2단계는 진행 중)
        _, payload = transport.sent[0]
        assert payload["severity_label"] == SeverityLevel.CRITICAL.label_ko
        assert payload["risk_tier"] == int(RiskTier.EMBER)

        backend.release.set()
        await asyncio.wait_for(svc.drain_phase2(), timeout=5)
        assert svc._phase2_tasks == set()
        assert res is not None and res.decision == "IMMEDIATE" and res.sent is True
        assert res.proposal.severity == SeverityLevel.CRITICAL
        assert res.proposal.situation_id is not None
        assert res.proposal.action.action == "alert"
        # 2단계는 이벤트 루프 스레드가 아닌 곳에서 돌았다.
        assert backend.thread_ids and backend.thread_ids[0] != threading.get_ident()

        rows = await _judgments(db)
        assert len(rows) == 2
        p1, p2 = rows
        assert p1["judgment_id"] == res.proposal.judgment_id
        assert p1["model_id"] == PENDING_MODEL_ID == "pending-decision-model"
        assert p1["gate2_verdict"] == PENDING_GATE2_VERDICT == "pending"
        assert p1["degraded"] == 0
        assert p1["severity"] == int(SeverityLevel.CRITICAL)
        assert p1["severity_floor"] == int(SeverityLevel.CRITICAL)
        assert json.loads(p1["severity_sources"]) == ["table"]
        # 2단계 행은 같은 상황·같은 스냅샷이며 모델 id·확률을 남긴다.
        assert p2["situation_id"] == p1["situation_id"] == res.proposal.situation_id
        assert p2["snapshot_hash"] == p1["snapshot_hash"]
        assert p2["model_id"] == backend.model_id
        assert p2["gate2_verdict"] == "concur"
        assert json.loads(p2["model_probs"]) == {"severity": {"A": 0.1, "B": 0.9}}
        # 모델이 올리지 않았으니 후속 제안은 없다.
        assert len(transport.sent) == 1
        assert len(await _proposals(db)) == 1
        # T8 확장 지점: 열림 훅이 첫 발송 뒤 정확히 한 번.
        assert svc.opened == [(res.proposal.situation_id, res.proposal.proposal_id)]
    finally:
        backend.release.set()
        await svc.drain_phase2()
        await db.close()


# ==========================================================================
# 2) 모델 상향 → 같은 situation_id 의 후속 제안 + 출처에 model
# ==========================================================================
async def test_model_raise_sends_a_followup_on_the_same_situation(tmp_path) -> None:
    backend = _FixedBackend(severity=SeverityLevel.CRITICAL)
    svc, db, transport = await _make(tmp_path, backend=backend)
    try:
        res = await svc.handle_reading(_reading(DOOR, "on"))
        assert res is not None
        assert res.proposal.severity == SeverityLevel.WARNING  # 표 하한(문 열림)
        await svc.drain_phase2()
        sid = res.proposal.situation_id

        props = await _proposals(db)
        assert len(props) == 2
        follow = props[1]
        assert follow["situation_id"] == sid
        assert follow["severity"] == int(SeverityLevel.CRITICAL)
        assert follow["action"] == "alert"
        assert len(transport.sent) == 2
        assert transport.sent[1][1]["severity_label"] == SeverityLevel.CRITICAL.label_ko

        rows = await _judgments(db)
        assert len(rows) == 2
        assert follow["judgment_id"] == rows[1]["judgment_id"]
        assert json.loads(rows[1]["severity_sources"]) == ["table", "model"]
        # 보호자 조건이 보는 하한은 모델이 못 바꾼다(T8 은 floor 를 본다).
        assert rows[1]["severity_floor"] == int(SeverityLevel.WARNING)
        sits = await _situations(db)
        assert sits[0]["severity_floor"] == int(SeverityLevel.WARNING)
        # 열림 훅은 첫 발송 한 번뿐(후속 제안은 새 에피소드가 아니다).
        assert len(svc.opened) == 1
    finally:
        await svc.drain_phase2()
        await db.close()


async def test_model_tier_raise_alone_also_sends_a_followup(tmp_path) -> None:
    """확인 강도만 올라도 후속 제안이 나간다(라우터 단계에서 비교)."""
    svc, db, transport = await _make(tmp_path)
    try:
        res = await svc.handle_reading(_reading(GAS, "on"))
        assert res is not None
        staged_like = res.proposal.model_copy(update={"risk_tier": RiskTier.DUSK})
        # 1단계 제안이 더 낮았다고 가정하면, EMBER 2단계는 '올림'이다.
        assert svc._raised(staged_like, res.proposal) is True
        assert svc._raised(res.proposal, res.proposal) is False
    finally:
        await svc.drain_phase2()
        await db.close()


# ==========================================================================
# 3) 하한 아래 답 → 후속 없음, 하지만 injection_suspect 행은 남는다
# ==========================================================================
async def test_model_lower_answer_is_recorded_but_sends_nothing(tmp_path) -> None:
    backend = _FixedBackend(severity=SeverityLevel.RECORD)
    svc, db, transport = await _make(tmp_path, backend=backend)
    try:
        res = await svc.handle_reading(_reading(GAS, "on"))
        assert res is not None and res.sent is True
        assert len(transport.sent) == 1
        assert len(await _proposals(db)) == 1
        await svc.drain_phase2()
        rows = await _judgments(db)
        assert len(rows) == 2
        assert rows[1]["gate2_verdict"] == "injection_suspect"
        assert rows[1]["severity"] == int(SeverityLevel.CRITICAL)  # 하한 유지
        assert rows[1]["situation_id"] == rows[0]["situation_id"]
        assert rows[1]["snapshot_hash"] == rows[0]["snapshot_hash"]
    finally:
        await svc.drain_phase2()
        await db.close()


# ==========================================================================
# 4) 2단계 제한시간 → degraded 행, 후속 없음
# ==========================================================================
async def test_phase2_deadline_stores_degraded_row_and_no_followup(tmp_path) -> None:
    backend = _BlockingBackend(severity=SeverityLevel.CRITICAL)  # 답했으면 올렸을 것
    svc, db, transport = await _make(tmp_path, backend=backend, deadline=0.05)
    try:
        res = await asyncio.wait_for(svc.handle_reading(_reading(DOOR, "on")), timeout=5)
        await svc.drain_phase2()
        assert res is not None and res.sent is True
        assert len(transport.sent) == 1
        assert len(await _proposals(db)) == 1
        rows = await _judgments(db)
        assert len(rows) == 2
        late = rows[1]
        assert late["degraded"] == 1
        assert late["gate2_verdict"] == "degraded_fail_safe"
        assert late["severity"] == int(SeverityLevel.WARNING)  # 하한 그대로
        assert late["risk_tier"] == int(RiskTier.EMBER)
        assert late["situation_id"] == rows[0]["situation_id"]
        assert late["snapshot_hash"] == rows[0]["snapshot_hash"]
        assert late["judgment_id"] != rows[0]["judgment_id"]
    finally:
        backend.release.set()
        await svc.drain_phase2()
        await db.close()


def test_default_phase2_deadline_is_four_backend_timeouts() -> None:
    from liot.service import PHASE2_REQUESTS_PER_CALL, default_phase2_deadline

    class _B:
        timeout_s = 2.0

    assert PHASE2_REQUESTS_PER_CALL == 4
    assert default_phase2_deadline(_B()) == pytest.approx(8.0)
    # 제한시간 속성이 없는 백엔드는 결정 모델 기본값(1.5초) 기준.
    assert default_phase2_deadline(object()) == pytest.approx(6.0)


# ==========================================================================
# 5) 열린 동안 반복된 이상 신호 → 새 알림 제안 없음
# ==========================================================================
async def test_repeated_abnormal_while_open_makes_no_new_alert(tmp_path) -> None:
    svc, db, transport = await _make(tmp_path)
    try:
        first = await svc.handle_reading(_reading(GAS, "on"))
        assert first is not None
        assert await svc.handle_reading(_reading(GAS, "on")) is None
        assert await svc.handle_reading(_reading(GAS, "detected")) is None
        assert len(await _proposals(db)) == 1
        sits = await _situations(db)
        assert len(sits) == 1 and sits[0]["closed_at"] is None
        assert len(transport.sent) == 1
        assert len(svc.opened) == 1
        # 반복 신호도 상태 캐시는 갱신한다(맥락은 살아 있다).
        assert svc.app.state_cache.norm_values()[GAS] is True
    finally:
        await svc.drain_phase2()
        await db.close()


# ==========================================================================
# 6) 정상 신호 → 닫힘 + 해소 NOTICE + alert 없음 + 대기 제안 만료
# ==========================================================================
async def test_normal_reading_closes_notifies_and_expires(tmp_path) -> None:
    svc, db, transport = await _make(tmp_path)
    try:
        first = await svc.handle_reading(_reading(GAS, "on"))
        assert first is not None
        sid = first.proposal.situation_id
        assert sid is not None
        # 같은 상황의 원탭 조치 제안(밸브 잠그기)이 대기 중이라고 하자.
        valve = first.proposal.model_copy(update={
            "proposal_id": "one-tap-valve",
            "action": first.proposal.action.model_copy(
                update={"capability": "valve", "action": "close"}),
        })
        await svc.proposals.save(valve)
        # 상황과 무관한 대기 제안은 건드리지 않는다.
        other = first.proposal.model_copy(update={"proposal_id": "unrelated",
                                                  "situation_id": None})
        await svc.proposals.save(other)
        svc._nonce["one-tap-valve"] = "n1"

        res = await svc.handle_reading(_reading(GAS, "off"))
        assert res is None  # 새 제안(=alert) 없음

        sits = await _situations(db)
        assert sits[0]["closed_at"] is not None
        props = {p["proposal_id"]: p for p in await _proposals(db)}
        assert len(props) == 3  # 새 제안이 하나도 생기지 않았다
        assert props[first.proposal.proposal_id]["state"] == "expired"
        assert props["one-tap-valve"]["state"] == "expired"
        assert props["unrelated"]["state"] == "pending"
        assert "one-tap-valve" not in svc._nonce  # 낡은 원탭 링크는 즉시 죽는다

        # 거주자 해소 알림: NOTICE(주의) 1건.
        assert len(transport.sent) == 2
        _, notice = transport.sent[1]
        assert notice["severity_label"] == SeverityLevel.NOTICE.label_ko
        assert notice["proposal_id"] != first.proposal.proposal_id
        # 해소 알림이 먼저, 그다음 닫힘 훅(T8 확장 지점).
        assert svc.closed == [sid]
        assert svc.sent_at_close == [2]

        # 다시 이상이면 새 에피소드 = 새 알림.
        again = await svc.handle_reading(_reading(GAS, "on"))
        assert again is not None and again.proposal.situation_id != sid
    finally:
        await svc.drain_phase2()
        await db.close()


async def test_resolved_notice_respects_charter_off(tmp_path) -> None:
    """해소 알림은 헌장 OFF 를 존중한다(정보성 알림 — 안전 알림 우회 권한 없음)."""
    svc, db, transport = await _make(tmp_path)
    try:
        assert await svc.handle_reading(_reading(GAS, "on")) is not None
        await svc.prefs.set("charter_enabled", False)
        assert await svc.handle_reading(_reading(GAS, "off")) is None
        assert len(transport.sent) == 1  # 첫 알림만
        assert (await _situations(db))[0]["closed_at"] is not None
    finally:
        await svc.drain_phase2()
        await db.close()


async def test_resolved_notice_send_failure_does_not_break_close(tmp_path) -> None:
    transport = FakeNtfyTransport()
    svc, db, _ = await _make(tmp_path, transport=transport)
    try:
        assert await svc.handle_reading(_reading(GAS, "on")) is not None
        transport.fail = True
        assert await svc.handle_reading(_reading(GAS, "off")) is None
        assert (await _situations(db))[0]["closed_at"] is not None
        assert len(svc.closed) == 1
    finally:
        await svc.drain_phase2()
        await db.close()


async def test_resolved_notice_in_sanitize_only_mode(tmp_path) -> None:
    clock = Clock(fixed=DAY)
    db = await init_db(":memory:")
    app = LiotApp(clock=clock, categories=CATS, llm_backend=_FixedBackend())
    svc = LiotService(app, db, clock=clock, transport=None, secret=SECRET)
    try:
        first = await svc.handle_reading(_reading(GAS, "on"))
        assert first is not None
        assert await svc.handle_reading(_reading(GAS, "off")) is None
        cur = await db.execute("SELECT COUNT(*) FROM notifications")
        row = await cur.fetchone()
        await cur.close()
        assert row is not None and row[0] == 2  # 알림 + 해소(정제분 기록)
        # 기본 훅은 아무것도 하지 않는다(T8 이 채운다).
        await svc._on_situation_opened("x", first.proposal)
        await svc._on_situation_closed("x")
    finally:
        await svc.drain_phase2()
        await db.close()


async def test_normal_reading_without_episode_does_nothing(tmp_path) -> None:
    svc, db, transport = await _make(tmp_path)
    try:
        assert await svc.handle_reading(_reading(GAS, "off")) is None
        assert transport.sent == []
        assert await _situations(db) == []
        assert await _proposals(db) == []
        assert await _judgments(db) == []
        assert svc.closed == []
    finally:
        await svc.drain_phase2()
        await db.close()


# ==========================================================================
# 7) 취약 거주자 가산: 독거 + 문 열림 → CRITICAL
# ==========================================================================
async def test_lives_alone_door_open_is_critical(tmp_path) -> None:
    backend = _FixedBackend()
    svc, db, transport = await _make(tmp_path, backend=backend)
    try:
        await svc.safety.set_lives_alone("singleton", True)
        rid = await svc.safety.add_resident("singleton", "할머니")
        await svc.safety.set_trait(rid, "age_75_plus", True, entered_by="resident")
        res = await svc.handle_reading(_reading(DOOR, "on"))
        assert res is not None
        assert res.proposal.severity == SeverityLevel.CRITICAL
        await svc.drain_phase2()
        rows = await _judgments(db)
        assert json.loads(rows[0]["severity_sources"]) == ["table", "resident"]
        assert rows[0]["severity_floor"] == int(SeverityLevel.CRITICAL)
        # 특성은 모델 입력에 들어가지 않는다 — 하한(불리언 결과)만 간다.
        call = backend.calls[0]
        assert call["severity_floor"] == SeverityLevel.CRITICAL
        blob = json.dumps(call["ctx"].model_dump(), ensure_ascii=False, default=str)
        blob += call["candidate"].model_dump_json()
        for trait in ("lives_alone", "age_75_plus", "cannot_self_evacuate",
                      "cognitive_decline", "할머니", "vulnerable"):
            assert trait not in blob
    finally:
        await svc.drain_phase2()
        await db.close()


async def test_door_open_without_vulnerable_resident_is_warning(tmp_path) -> None:
    svc, db, _ = await _make(tmp_path)
    try:
        res = await svc.handle_reading(_reading(DOOR, "on"))
        assert res is not None and res.proposal.severity == SeverityLevel.WARNING
        assert res.decision == "IMMEDIATE"  # 오늘처럼 즉시(EMBER) — 조용해지지 않는다
    finally:
        await svc.drain_phase2()
        await db.close()


# ==========================================================================
# 8) 정규화 불가 값 → 이상
# ==========================================================================
async def test_unnormalizable_value_counts_as_abnormal(tmp_path) -> None:
    svc, db, transport = await _make(tmp_path)
    try:
        assert await svc.handle_reading(_reading(GAS, "off")) is None
        res = await svc.handle_reading(_reading(GAS, "??garbled??"))
        assert res is not None and res.proposal.action.action == "alert"
        assert res.proposal.severity == SeverityLevel.CRITICAL
    finally:
        await svc.drain_phase2()
        await db.close()


def test_classify_safety_signal() -> None:
    app = LiotApp(clock=Clock(fixed=DAY), categories=CATS, llm_backend=_FixedBackend())
    sig = app.classify_safety(_reading(GAS, "on"))
    assert sig is not None and sig.abnormal is True and sig.category == "gas"
    off = app.classify_safety(_reading(GAS, "off"))
    assert off is not None and off.abnormal is False
    raw_only = SensorReading(entity_id=GAS, source="t", raw_value="garbled",
                             norm_value=None, ts_utc=DAY)
    weird = app.classify_safety(raw_only)
    assert weird is not None and weird.abnormal is True
    none_at_all = SensorReading(entity_id=GAS, source="t", raw_value=None,
                                norm_value=None, ts_utc=DAY)
    blank = app.classify_safety(none_at_all)
    assert blank is not None and blank.abnormal is True
    assert app.classify_safety(_reading("light.living_room", "on")) is None


# ==========================================================================
# 9) 컴포트 경로 회귀 + 판단 행 저장
# ==========================================================================
async def test_comfort_path_unchanged_and_judgment_stored(tmp_path) -> None:
    # 조명 끄기는 잔잔(AMBIENT) — 모델도 그렇게 답하는 백엔드(FakeLLM 과 같은 판정).
    svc, db, transport = await _make(tmp_path, backend=_FixedBackend(tier=RiskTier.AMBIENT))
    try:
        for eid, raw in [("binary_sensor.living_motion", "off"), ("light.living_room", "on")]:
            assert await svc.handle_reading(_reading(eid, raw)) is None
        res = await svc.run_tick()
        assert res is not None
        assert res.proposal.action.action == "turn_off"
        assert res.decision == "DIGEST" and res.sent is False
        assert res.proposal.severity is None and res.proposal.situation_id is None
        rows = await _judgments(db)
        assert len(rows) == 1
        assert rows[0]["judgment_id"] == res.proposal.judgment_id
        assert rows[0]["severity"] is None and rows[0]["situation_id"] is None
        assert json.loads(rows[0]["severity_sources"]) == []
        assert rows[0]["model_id"] == _FixedBackend.model_id
        assert await _situations(db) == []
        # 다음 틱이 침묵하면 같은 판단을 두 번 저장하지 않는다.
        assert await svc.run_tick() is None
        assert len(await _judgments(db)) == 1
    finally:
        await svc.drain_phase2()
        await db.close()


async def test_suppressed_proactive_judgment_is_still_stored(tmp_path) -> None:
    """모델이 BREATH 위로 올려 억제된 능동 제안도 판단 행은 남는다(감사)."""
    backend = _FixedBackend(tier=RiskTier.EMBER)
    svc, db, _ = await _make(tmp_path, backend=backend)
    try:
        for eid, raw in [("binary_sensor.living_motion", "off"), ("light.living_room", "on")]:
            await svc.handle_reading(_reading(eid, raw))
        assert await svc.run_tick() is None
        rows = await _judgments(db)
        assert len(rows) == 1 and rows[0]["risk_tier"] == int(RiskTier.EMBER)
    finally:
        await svc.drain_phase2()
        await db.close()


# ==========================================================================
# 10) 표 로드 실패 → audit_log 에 한 번
# ==========================================================================
async def test_table_load_failure_is_audited_once(tmp_path, caplog) -> None:
    table = load_severity_table(str(tmp_path / "missing.yaml"))
    assert table.load_failed
    svc, db, _ = await _make(tmp_path, table=table)
    try:
        with caplog.at_level("WARNING"):
            res = await svc.handle_reading(_reading(DOOR, "on"))
            await svc.handle_reading(_reading(GAS, "on"))
            await svc.run_tick()
        assert res is not None and res.proposal.severity == SeverityLevel.CRITICAL
        cur = await db.execute(
            "SELECT a.actor, a.detail FROM audit_log AS a"
            " WHERE a.event_type = 'severity_table_load_failed'"
        )
        rows = await cur.fetchall()
        await cur.close()
        assert len(rows) == 1
        assert rows[0][0] == "SYSTEM"
        assert "FileNotFoundError" in json.loads(rows[0][1])["error"]
        assert any("severity table" in r.getMessage() for r in caplog.records)
    finally:
        await svc.drain_phase2()
        await db.close()


async def test_healthy_table_writes_no_failure_audit(tmp_path) -> None:
    svc, db, _ = await _make(tmp_path)
    try:
        await svc.start()
        await svc.start()
        cur = await db.execute(
            "SELECT COUNT(*) FROM audit_log AS a"
            " WHERE a.event_type = 'severity_table_load_failed'"
        )
        row = await cur.fetchone()
        await cur.close()
        assert row is not None and row[0] == 0
    finally:
        await svc.drain_phase2()
        await db.close()


# ==========================================================================
# 11) 1단계가 실패하면 열린 상황을 남기지 않는다(다음 이상 신호가 침묵하지 않게)
# ==========================================================================
async def test_phase1_failure_closes_the_just_opened_situation(tmp_path, monkeypatch) -> None:
    svc, db, transport = await _make(tmp_path)
    try:
        async def _boom(*a: Any, **kw: Any) -> Any:
            raise RuntimeError("pipeline exploded")

        monkeypatch.setattr(svc.app, "process_situation_reading", _boom)
        with pytest.raises(RuntimeError):
            await svc.handle_reading(_reading(GAS, "on"))
        sits = await _situations(db)
        assert len(sits) == 1 and sits[0]["closed_at"] is not None
        monkeypatch.undo()
        res = await svc.handle_reading(_reading(GAS, "on"))
        assert res is not None and res.sent is True  # 다음 이상 신호는 알린다
    finally:
        await svc.drain_phase2()
        await db.close()


async def test_phase1_without_proposal_closes_the_situation(tmp_path, monkeypatch) -> None:
    svc, db, _ = await _make(tmp_path)
    try:
        async def _none(*a: Any, **kw: Any) -> Any:
            return None

        monkeypatch.setattr(svc.app, "process_situation_reading", _none)
        assert await svc.handle_reading(_reading(GAS, "on")) is None
        assert (await _situations(db))[0]["closed_at"] is not None
    finally:
        await svc.drain_phase2()
        await db.close()


# ==========================================================================
# 12) 라우터가 파급도·상황 id 를 제안으로 옮긴다
# ==========================================================================
def test_router_copies_severity_and_situation_id() -> None:
    judgment = Judgment(
        judgment_id="j1", trace_id="t", home_id="singleton", snapshot_hash="h",
        action=CandidateAction(capability="gas", action="alert", entity_id=GAS,
                               category="gas"),
        risk_tier=RiskTier.EMBER, confidence=1.0, rationale="r", gate1_verdict="ok",
        bundle_id="b", model_id="m", severity=SeverityLevel.WARNING,
        severity_floor=SeverityLevel.WARNING, severity_sources=["table"],
        situation_id="sit-1",
    )
    p = RiskRouter().route(judgment, created_at=DAY)
    assert p.severity == SeverityLevel.WARNING and p.situation_id == "sit-1"
    bare = RiskRouter().route(
        judgment.model_copy(update={"severity": None, "situation_id": None}), created_at=DAY
    )
    assert bare.severity is None and bare.situation_id is None


# ==========================================================================
# 13) 상황 단위 만료는 그 상황의 pending 만 건드린다
# ==========================================================================
async def test_expire_situation_only_touches_pending_of_that_situation(tmp_path) -> None:
    svc, db, _ = await _make(tmp_path)
    try:
        res = await svc.handle_reading(_reading(GAS, "on"))
        assert res is not None
        sid = res.proposal.situation_id
        assert sid is not None
        accepted = res.proposal.model_copy(update={"proposal_id": "acc", "state": "accepted"})
        await svc.proposals.save(accepted)
        expired = await svc.proposals.expire_situation(sid)
        assert expired == [res.proposal.proposal_id]
        acc = await svc.proposals.get("acc")
        assert acc is not None and acc.state == "accepted"
        assert await svc.proposals.expire_situation(sid) == []
    finally:
        await svc.drain_phase2()
        await db.close()


async def test_phase2_backend_error_stores_degraded_row(tmp_path) -> None:
    """판정기가 LLMUnavailable 이 아닌 예외로 죽어도 1단계 알림은 그대로, 행은 degraded."""

    class _Exploding(_FixedBackend):
        model_id = "exploding-test-model"

        def judge_raw(self, candidate: CandidateAction, ctx: ContextSnapshot | None,
                      gate1_floor: RiskTier,
                      severity_floor: SeverityLevel | None = None) -> LLMResult:
            raise ValueError("backend bug")

    svc, db, transport = await _make(tmp_path, backend=_Exploding())
    try:
        res = await svc.handle_reading(_reading(GAS, "on"))
        assert res is not None and res.sent is True
        await svc.drain_phase2()
        rows = await _judgments(db)
        assert len(rows) == 2
        assert rows[1]["degraded"] == 1 and rows[1]["model_id"] == "exploding-test-model"
        assert rows[1]["gate2_verdict"] == "degraded_fail_safe"
        assert len(transport.sent) == 1
    finally:
        await svc.drain_phase2()
        await db.close()


# ==========================================================================
# 14) 2단계는 백그라운드: 앞 상황의 모델 판정이 다음 안전 알림을 막지 않는다
# ==========================================================================
async def test_second_safety_alert_is_not_held_behind_first_model_call(tmp_path) -> None:
    """스트림으로 가스 → 문 열림. 모델이 막혀 있어도 두 알림이 모두 먼저 나간다."""
    backend = _BlockingBackend()
    svc, db, transport = await _make(tmp_path, backend=backend)

    class _Bridge:
        async def stream(self):
            yield _reading(GAS, "on")
            yield _reading(DOOR, "on")

    try:
        await asyncio.wait_for(svc._stream_loop(_Bridge(), 2), timeout=5)
        assert not backend.release.is_set()
        assert len(transport.sent) == 2  # 모델이 풀리기 전에 둘 다 발송
        labels = [payload["severity_label"] for _, payload in transport.sent]
        assert labels == [SeverityLevel.CRITICAL.label_ko, SeverityLevel.WARNING.label_ko]
        assert len(svc._phase2_tasks) == 2
        backend.release.set()
        await asyncio.wait_for(svc.drain_phase2(), timeout=5)
        rows = await _judgments(db)
        assert len(rows) == 4  # 두 상황 × (1단계 + 2단계)
        assert len({r["situation_id"] for r in rows}) == 2
    finally:
        backend.release.set()
        await svc.drain_phase2()
        await db.close()


async def test_followup_is_not_sent_after_the_situation_closed(tmp_path) -> None:
    """2단계가 도는 사이 정상 복귀로 닫힌 상황엔 후속 알림을 보내지 않는다(행은 남김)."""
    backend = _BlockingBackend(severity=SeverityLevel.CRITICAL)  # 답하면 올린다
    svc, db, transport = await _make(tmp_path, backend=backend)
    try:
        first = await svc.handle_reading(_reading(DOOR, "on"))
        assert first is not None
        assert await svc.handle_reading(_reading(DOOR, "off")) is None  # 닫힘 + 해소 알림
        backend.release.set()
        await asyncio.wait_for(svc.drain_phase2(), timeout=5)
        assert len(transport.sent) == 2  # 알림 + 해소, 후속 없음
        assert len(await _proposals(db)) == 1
        rows = await _judgments(db)
        assert len(rows) == 2
        assert json.loads(rows[1]["severity_sources"]) == ["table", "model"]
    finally:
        backend.release.set()
        await svc.drain_phase2()
        await db.close()


async def test_run_drains_phase2_before_returning(tmp_path) -> None:
    """run() 은 끝나기 전에 남은 2단계를 마무리한다(호출자가 그 뒤 DB 를 닫는다)."""
    backend = _FixedBackend()
    svc, db, _ = await _make(tmp_path, backend=backend)

    class _Bridge:
        async def stream(self):
            yield _reading(GAS, "on")

    try:
        await asyncio.wait_for(
            svc.run(bridge=_Bridge(), tick_seconds=0.0, max_ticks=1, max_events=1),
            timeout=10,
        )
        assert svc._phase2_tasks == set()
        assert len(await _judgments(db)) == 2
    finally:
        await db.close()


async def test_drain_timeout_cancels_stuck_phase2(tmp_path) -> None:
    backend = _BlockingBackend()
    svc, db, _ = await _make(tmp_path, backend=backend, deadline=30.0)
    try:
        assert await svc.handle_reading(_reading(GAS, "on")) is not None
        await asyncio.wait_for(svc.drain_phase2(timeout=0.05), timeout=5)
        assert svc._phase2_tasks == set()
        assert len(await _judgments(db)) == 1  # 취소된 2단계는 행이 없다(종료 경로)
    finally:
        backend.release.set()
        await db.close()


async def test_phase2_task_exception_is_logged(tmp_path, monkeypatch, caplog) -> None:
    """2단계 태스크의 예외는 삼키지 않고 로그로 남긴다. 1단계 알림은 이미 나갔다."""
    svc, db, transport = await _make(tmp_path)
    try:
        async def _boom(*a: Any, **kw: Any) -> Any:
            raise RuntimeError("phase-2 exploded")

        monkeypatch.setattr(svc, "_run_phase2", _boom)
        with caplog.at_level("ERROR"):
            res = await svc.handle_reading(_reading(GAS, "on"))
            await svc.drain_phase2()
        assert res is not None and res.sent is True and len(transport.sent) == 1
        assert svc._phase2_tasks == set()
        assert any("phase-2 judgment task failed" in r.getMessage() for r in caplog.records)
    finally:
        await db.close()
