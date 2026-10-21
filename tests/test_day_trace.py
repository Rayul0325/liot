"""하루 시뮬레이터 시연용 단계 기록(trace) — 편의 제안·침묵(1단계).

지키는 것:
  - trace 는 요청 키로만 켠다. 켜고 끈 같은 씨앗 실행은 trace 사건을 빼면 사건·요약·전송 수가 같다.
  - 편의 노드 순서 sense → rules → score → context → gate1 → judge → route → gate → phone
    (cloud 면 phone 뒤 reason). 침묵은 rules·score·gate1·judge 중 한 곳에서 end.
  - judge.real_ms 는 그 entity 의 1단계(편의) 판정 호출 시간 그대로(가짜 모드 포함).
  - 기록 코드가 예외를 던져도 결과가 같다. trace 사건은 pace_ms 대기를 하지 않는다.
  - 열쇠 기록·판정 시간은 단계가 끝나면 버린다. 특성 낱말 0개.
"""

from __future__ import annotations

import asyncio
import json
import re
from pathlib import Path
from typing import Any

import pytest

from liot import day_sim
from liot.core.risk import RiskTier
from liot.core.traits import TRAIT_WORDS
from liot.day_sim import DaySim, _TraceRecorder
from liot.layers.l3_judgment.guardrail import Gate1Result
from liot.layers.l3_judgment.jev_backend import JevBackend
from liot.live_runs import LiveRequestError
from tests.test_day_sim_cloud import FakeJev, _kit

Events = list[tuple[str, dict[str, Any]]]
COMFORT = ["sense", "rules", "score", "context", "gate1", "judge", "route", "gate", "phone"]


def _run(sim: DaySim) -> Events:
    events: Events = []
    asyncio.run(sim.run(lambda stage, data: events.append((stage, data))))
    return events


def _traces(events: Events) -> list[dict[str, Any]]:
    return [d for st, d in events if st == "trace"]


def _sends_per_step(sim: DaySim) -> list[int]:
    counts: list[int] = []
    for entry in sim.log:
        if entry[0] == "step":
            counts.append(0)
        elif entry[0] == "send" and counts:
            counts[-1] += 1
    return counts


def _end(trace: dict[str, Any]) -> str | None:
    return next((n["id"] for n in trace["nodes"] if n["state"] == "end"), None)


@pytest.fixture(scope="module")
def seed1() -> tuple[DaySim, Events]:
    sim = DaySim(seed=1, days=4, trace=True)
    return sim, _run(sim)


# ── 켜고 끔 ─────────────────────────────────────────────────────────────────


@pytest.mark.parametrize(("seed", "days"), [(7, 3), (1, 4)])
def test_trace_on_and_off_give_the_same_run(seed: int, days: int) -> None:
    off = DaySim(seed=seed, days=days)
    on = DaySim(seed=seed, days=days, trace=True)
    off_events, on_events = _run(off), _run(on)
    assert _traces(on_events) and not _traces(off_events)
    assert [(st, d) for st, d in on_events if st != "trace"] == off_events
    assert on.summary() == off.summary()
    assert _sends_per_step(on) == _sends_per_step(off)


def test_trace_off_by_default_and_request_key() -> None:
    assert DaySim().trace is False
    assert day_sim.parse_day_request({}).trace is False
    assert day_sim.parse_day_request({"trace": True}).trace is True
    for bad in ("yes", 1, None, [True]):
        with pytest.raises(LiveRequestError) as exc:
            day_sim.parse_day_request({"trace": bad})
        assert exc.value.field == "trace"


def test_day_runs_meta_and_response_carry_trace_only_when_on(tmp_path: Path) -> None:
    runs = day_sim.DayRuns(secrets_path=tmp_path / "none.env")
    off = runs.start({"days": 1})
    runs.join(30.0)
    on = runs.start({"days": 1, "trace": True})
    runs.join(30.0)
    assert "trace" not in off and on["trace"] is True
    view_off, view_on = runs.view(off["run_id"]), runs.view(on["run_id"])
    assert view_off is not None and view_on is not None
    assert "trace" not in view_off["meta"] and view_on["meta"]["trace"] is True


# ── 편의 노드 ───────────────────────────────────────────────────────────────


def test_comfort_nodes_in_real_order(seed1: tuple[DaySim, Events]) -> None:
    sim, events = seed1
    comfort = [t for t in _traces(events) if t["kind"] == "comfort"]
    assert comfort and len(comfort) == len(sim.proposals)
    for t in comfort:
        assert [n["id"] for n in t["nodes"]] == COMFORT
        assert t["part"] == "main" and t["proposal_id"] in {p["id"] for p in sim.proposals}
        assert all(n["state"] == "done" for n in t["nodes"])
        assert all(n["label_ko"] and n["detail_ko"] for n in t["nodes"])
        assert t["outcome_ko"]
    by_id = {p["id"]: p for p in sim.proposals}
    for t in comfort:
        p = by_id[t["proposal_id"]]
        gate = next(n for n in t["nodes"] if n["id"] == "gate")
        assert gate["detail_ko"] == {"IMMEDIATE": "바로 폰 알림", "DIGEST": "모아보기",
                                     "SUPPRESS": "보류"}[p["decision"]]
        assert p["title_ko"] in next(n for n in t["nodes"] if n["id"] == "phone")["detail_ko"]


def test_unmeasured_nodes_have_no_time(seed1: tuple[DaySim, Events]) -> None:
    _, events = seed1
    for t in _traces(events):
        for n in t["nodes"]:
            if n["id"] in ("sense", "context", "route", "gate", "phone") or n["state"] == "skipped":
                assert n["real_ms"] is None, n
        if t["kind"] == "comfort":
            measured = {n["id"]: n["real_ms"] for n in t["nodes"]}
            for nid in ("rules", "score", "gate1", "judge"):
                assert isinstance(measured[nid], float), (nid, t)


def test_cloud_comfort_adds_a_reason_node(tmp_path: Path) -> None:
    sim = DaySim(seed=7, days=1, models="cloud", cloud=_kit(tmp_path), trace=True)
    events = _run(sim)
    comfort = [t for t in _traces(events) if t["kind"] == "comfort"]
    assert comfort
    for t in comfort:
        assert [n["id"] for n in t["nodes"]] == [*COMFORT, "reason"]
        reason = t["nodes"][-1]
        assert "시뮬레이터가 카드 이유 문장을 쓴다" in reason["detail_ko"]
        assert isinstance(reason["real_ms"], float)
        judge = next(n for n in t["nodes"] if n["id"] == "judge")
        assert judge["model"] == "jev:jev-latest"


# ── 침묵 네 곳 ──────────────────────────────────────────────────────────────


def test_silence_ends_at_rules(seed1: tuple[DaySim, Events]) -> None:
    _, events = seed1
    silent = [t for t in _traces(events) if t["kind"] == "silent"]
    assert silent and {_end(t) for t in silent} == {"rules"}
    lines = [d for st, d in events if st == "step" and "silence" in d]
    assert len(silent) == len(lines)  # 타임라인 침묵 줄과 같은 때에만
    for t in silent:
        ids = [n["id"] for n in t["nodes"]]
        assert ids == COMFORT
        after = ids.index("rules") + 1
        assert all(n["state"] == "skipped" for n in t["nodes"][after:])


def test_silence_ends_at_score() -> None:
    sim = DaySim(seed=9, days=7, trace=True)
    events = _run(sim)
    silent = [t for t in _traces(events) if t["kind"] == "silent" and _end(t) == "score"]
    assert silent
    assert "아무것도 안 하기가 이김" in next(n for n in silent[0]["nodes"]
                                       if n["id"] == "score")["detail_ko"]


def test_silence_ends_at_gate1(monkeypatch: pytest.MonkeyPatch) -> None:
    real = day_sim._TimedGuardrail.evaluate

    def raised(self: Any, candidate: Any, ctx: Any = None) -> Gate1Result:
        result = real(self, candidate, ctx)
        if candidate.category not in ("leak", "gas"):  # 편의 후보만 하한을 올린 규칙이 있다고 치자
            result = Gate1Result(floor_tier=RiskTier.DUSK, gate1_verdict="test", blocked=False,
                                 matched_rule_ids=("test",), safety_critical=False)
            with __import__("contextlib").suppress(Exception):
                self.recorder.note((candidate.entity_id, "comfort"), gate1=result)
        return result

    monkeypatch.setattr(day_sim._TimedGuardrail, "evaluate", raised)
    sim = DaySim(seed=7, days=1, trace=True)
    events = _run(sim)
    ends = {_end(t) for t in _traces(events) if t["kind"] == "silent"}
    assert "gate1" in ends and not sim.proposals


def test_silence_ends_at_judge_when_the_model_raises_above_breath(tmp_path: Path) -> None:
    class Ember(FakeJev):
        def post(self, payload: dict[str, Any], *, timeout_s: float) -> dict[str, Any]:
            out = super().post(payload, timeout_s=timeout_s)
            for answer in out["answers"].values():
                answer["probabilities"] = {"0": 0.0, "1": 0.0, "2": 0.1, "3": 0.9}
            return out

    kit = _kit(tmp_path)
    kit.judge = JevBackend(transport=Ember())
    sim = DaySim(seed=7, days=1, models="cloud", cloud=kit, trace=True)
    events = _run(sim)
    judge_ends = [t for t in _traces(events) if t["kind"] == "silent" and _end(t) == "judge"]
    assert judge_ends and not sim.proposals
    judge = next(n for n in judge_ends[0]["nodes"] if n["id"] == "judge")
    assert "불씨" in judge["detail_ko"] and judge["model"] == "jev:jev-latest"


def test_silence_end_classifier_covers_the_four_places() -> None:
    from liot.day_sim import _silence_end

    class Dec:
        def __init__(self, cand: Any) -> None:
            self.candidate = cand

    class Ev:
        def __init__(self, noop: bool) -> None:
            self.noop = noop

        def selected_is_noop(self) -> bool:
            return self.noop

    ok = Gate1Result(floor_tier=RiskTier.AMBIENT, gate1_verdict="", blocked=False,
                     matched_rule_ids=(), safety_critical=False)
    high = Gate1Result(floor_tier=RiskTier.DUSK, gate1_verdict="", blocked=False,
                       matched_rule_ids=(), safety_critical=False)
    blocked = Gate1Result(floor_tier=RiskTier.AMBIENT, gate1_verdict="", blocked=True,
                          matched_rule_ids=(), safety_critical=False)
    assert _silence_end(None, None, None) == "rules"
    assert _silence_end(Dec(None), None, None) == "rules"  # type: ignore[arg-type]
    assert _silence_end(Dec(1), Ev(True), ok) == "score"  # type: ignore[arg-type]
    assert _silence_end(Dec(1), Ev(False), high) == "gate1"  # type: ignore[arg-type]
    assert _silence_end(Dec(1), Ev(False), blocked) == "gate1"  # type: ignore[arg-type]
    assert _silence_end(Dec(1), Ev(False), ok) == "judge"  # type: ignore[arg-type]


# ── 판정 시간 ───────────────────────────────────────────────────────────────


def test_judge_time_is_the_phase1_call_time_of_that_entity(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[tuple[int, str, bool, float]] = []
    real = _TraceRecorder.judge_call

    def spy(self: _TraceRecorder, step_no: int, entity_id: str, phase2: bool,
            seconds: float) -> None:
        calls.append((step_no, entity_id, phase2, seconds))
        real(self, step_no, entity_id, phase2, seconds)

    monkeypatch.setattr(_TraceRecorder, "judge_call", spy)
    sim = DaySim(seed=7, days=3, trace=True)
    events = _run(sim)
    by_id = {p["id"]: p for p in sim.proposals}
    comfort = [t for t in _traces(events) if t["kind"] == "comfort"]
    assert comfort
    for t in comfort:
        entity = by_id[t["proposal_id"]]["entity_id"]
        judge = next(n for n in t["nodes"] if n["id"] == "judge")
        mine = [round(s * 1000.0, 2) for _, e, p2, s in calls if e == entity and not p2]
        assert judge["real_ms"] in mine
    assert any(p2 for _, _, p2, _ in calls)  # 2단계 호출도 기록됐다(편의 시간에는 안 섞인다)


# ── 기록이 깨져도 결과는 같다 · 잠자지 않는 내보내기 · 단계 끝 버림 ─────────────────


def test_recording_errors_never_change_the_run(monkeypatch: pytest.MonkeyPatch) -> None:
    def boom(*a: Any, **kw: Any) -> Any:
        raise RuntimeError("recorder broke")

    for name in ("note", "judge_call", "take", "take_judge", "next_id"):
        monkeypatch.setattr(_TraceRecorder, name, boom)
    on = DaySim(seed=7, days=3, trace=True)
    off = DaySim(seed=7, days=3)
    on_events, off_events = _run(on), _run(off)
    assert [(st, d) for st, d in on_events if st != "trace"] == off_events
    assert on.summary() == off.summary()


def test_trace_events_do_not_wait_for_pace(monkeypatch: pytest.MonkeyPatch) -> None:
    order: list[str] = []
    real_sleep = asyncio.sleep

    async def fake_sleep(delay: float, *a: Any, **kw: Any) -> Any:
        if delay > 0:
            order.append("sleep")
        return await real_sleep(0)

    monkeypatch.setattr(asyncio, "sleep", fake_sleep)
    sim = DaySim(seed=7, days=1, pace_ms=5, trace=True)
    asyncio.run(sim.run(lambda stage, data: order.append(stage)))
    traces = [i for i, x in enumerate(order) if x == "trace"]
    assert traces
    assert all(order[i + 1] != "sleep" for i in traces if i + 1 < len(order))
    others = [i for i, x in enumerate(order) if x not in ("trace", "sleep")]
    assert all(order[i + 1] == "sleep" for i in others if i + 1 < len(order))


def test_keyed_records_and_judge_times_are_dropped_at_step_end() -> None:
    rec = _TraceRecorder()
    rec.begin_step()
    rec.note(("fan.x", "comfort"), gate1="g")
    rec.judge_call(rec.step_no, "fan.x", False, 0.5)
    rec.end_step()
    rec.begin_step()
    assert rec.take(("fan.x", "comfort")) == {}
    assert rec.take_judge("fan.x", False) is None
    # 늦게 온(앞 단계 번호) 판정 기록은 지금 단계에서 꺼내지지 않는다.
    rec.judge_call(rec.step_no - 1, "fan.x", True, 0.7)
    assert rec.take_judge("fan.x", True) is None
    rec.judge_call(rec.step_no, "fan.x", True, 0.9)
    assert rec.take_judge("fan.x", False) is None and rec.take_judge("fan.x", True) == 0.9


def test_no_trait_words_in_any_trace(seed1: tuple[DaySim, Events]) -> None:
    _, events = seed1
    blob = json.dumps(_traces(events), ensure_ascii=False)
    assert not any(word in blob for word in TRAIT_WORDS)


# ── 1b: 안전 main · followup · 피드백 ──────────────────────────────────────────

SAFETY_MAIN = ["sense", "classify", "floor", "first_alert", "phone"]
FOLLOWUP = ["model", "severity", "followup", "ladder"]


@pytest.fixture(scope="module")
def seed7() -> tuple[DaySim, Events]:
    sim = DaySim(seed=7, days=3, trace=True)
    return sim, _run(sim)


def test_safety_main_nodes_and_only_first_alert_is_timed(seed7: tuple[DaySim, Events]) -> None:
    sim, events = seed7
    mains = [t for t in _traces(events) if t["kind"] == "safety" and t["part"] == "main"]
    firsts = [s for s in sim.safety_events if s["kind"] == "first_alert"]
    assert len(mains) == len(firsts) == 3
    for t, alert in zip(mains, firsts, strict=True):
        assert [n["id"] for n in t["nodes"]] == SAFETY_MAIN
        assert t["proposal_id"] == alert["id"]
        timed = {n["id"]: n["real_ms"] for n in t["nodes"]}
        assert isinstance(timed["first_alert"], float) and timed["first_alert"] >= 0
        assert all(v is None for k, v in timed.items() if k != "first_alert")
        assert "읽기부터 첫 경보 전송까지" in t["nodes"][3]["detail_ko"]
        assert not any("binary_sensor." in n["detail_ko"] for n in t["nodes"])


def test_safety_main_comes_right_after_its_first_alert_event(seed7: tuple[DaySim, Events]) -> None:
    _, events = seed7
    for i, (st, d) in enumerate(events):
        if st == "trace" and d["kind"] == "safety" and d["part"] == "main":
            prev_st, prev = events[i - 1]
            assert prev_st == "safety" and prev["kind"] == "first_alert"
            assert prev["id"] == d["proposal_id"]


def test_safety_main_follows_the_first_send() -> None:
    # 첫 경보 전송이 handle_reading 안에서 끝난 뒤에야 trace 가 나간다 — trace 를 받은 순간의
    # 송신 기록(sim.log, 실시간으로 쌓인다)에 그 제안의 첫 송신이 이미 있다.
    sim = DaySim(seed=7, days=3, trace=True)
    at_trace: list[tuple[str, int]] = []

    def emit(stage: str, data: dict[str, Any]) -> None:
        if stage == "trace" and data["kind"] == "safety" and data["part"] == "main":
            at_trace.append((data["proposal_id"], len(sim.log)))

    asyncio.run(sim.run(emit))
    assert len(at_trace) == 3
    for sim_id, seen in at_trace:
        first_send = sim.log.index(("send", sim._tracked[sim_id].pid))
        assert first_send < seen


def test_followup_nodes_and_phase2_time(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[tuple[int, str, bool, float]] = []
    real = _TraceRecorder.judge_call

    def spy(self: _TraceRecorder, step_no: int, entity_id: str, phase2: bool,
            seconds: float) -> None:
        calls.append((step_no, entity_id, phase2, seconds))
        real(self, step_no, entity_id, phase2, seconds)

    monkeypatch.setattr(_TraceRecorder, "judge_call", spy)
    # followup 을 낼 때의 단계 번호와 상황 센서(사건 순서대로) — 시간은 같은 단계·같은 센서 것이어야 한다.
    emitted: list[tuple[int, str]] = []
    real_emit = DaySim._emit_followups

    async def emit_spy(self: DaySim) -> None:
        assert self._rec is not None
        emitted.extend((self._rec.step_no, s["eid"]) for s in self._pending_safety)
        await real_emit(self)

    monkeypatch.setattr(DaySim, "_emit_followups", emit_spy)
    sim = DaySim(seed=7, days=3, trace=True)
    events = _run(sim)
    follow = [t for t in _traces(events) if t["kind"] == "safety" and t["part"] == "followup"]
    assert len(follow) == len(emitted) == 3
    for t, (step_no, eid) in zip(follow, emitted, strict=True):
        assert [n["id"] for n in t["nodes"]] == FOLLOWUP
        model = t["nodes"][0]
        mine = [round(s * 1000.0, 2) for st, e, p2, s in calls if p2 and st == step_no and e == eid]
        assert len(mine) == 1
        assert model["real_ms"] == mine[0] and model["model"] == "fake-llm-v1"
        assert t["nodes"][1]["detail_ko"].startswith("파급도 ")
        assert re.fullmatch(r"재알림 예약 \d\d:\d\d", t["nodes"][3]["detail_ko"])


def test_followup_after_deadline_has_no_time_and_late_writes_do_not_leak(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    import threading
    import time as _time

    class SlowSeverity(FakeJev):
        """첫 2단계(파급도 질문이 있는) 요청만 마감보다 늦게 답한다."""

        def __init__(self) -> None:
            super().__init__()
            self.slowed = threading.Event()

        def post(self, payload: dict[str, Any], *, timeout_s: float) -> dict[str, Any]:
            if "severity" in payload["questions"] and not self.slowed.is_set():
                self.slowed.set()
                _time.sleep(0.6)
            return super().post(payload, timeout_s=timeout_s)

    monkeypatch.setattr(day_sim, "CLOUD_PHASE2_DEADLINE_S", 0.2)
    kit = _kit(tmp_path)
    kit.judge = JevBackend(transport=SlowSeverity())
    sim = DaySim(seed=7, days=2, models="cloud", cloud=kit, trace=True)
    events = _run(sim)
    follow = [t for t in _traces(events) if t["kind"] == "safety" and t["part"] == "followup"]
    assert len(follow) == 2
    late, later = follow[0]["nodes"][0], follow[1]["nodes"][0]
    assert late["real_ms"] is None and late["detail_ko"] == "마감 안에 답이 없어 하한 판정 유지"
    # 늦게 끝난 첫 호출(≈600ms)이 다음 상황의 시간으로 섞이지 않는다.
    assert isinstance(later["real_ms"], float) and later["real_ms"] < 500


def test_feedback_traces_follow_the_applied_tap(seed7: tuple[DaySim, Events]) -> None:
    _, events = seed7
    fb = [(i, d) for i, (st, d) in enumerate(events) if st == "trace" and d["kind"] == "feedback"]
    assert fb
    for i, t in fb:
        prev_st, prev = events[i - 1]
        assert prev_st == "feedback" and prev["id"] == t["proposal_id"]
        ids = [n["id"] for n in t["nodes"]]
        if prev["kind"] == "safety":
            assert ids == ["phone", "ack"] and prev["signal"] == "ok"
        else:
            assert ids == ["phone", "feedback", "prefs"]
            prefs = t["nodes"][2]["detail_ko"]
            expect = "이번 단계 판단부터 반영" if prev["signal"] == "expired" else "다음 판단부터 반영"
            assert prefs == expect
    kinds = {events[i - 1][1]["signal"] for i, _ in fb}
    assert {"accepted", "dismissed", "expired", "ok"} <= kinds


def test_viewer_tap_trace_lands_where_the_tap_applies() -> None:
    def on_event(sim: DaySim, stage: str, data: dict[str, Any]) -> None:
        if stage == "proposal" and data["id"] == "p1":
            sim.enqueue_feedback("p1", "dismissed")

    sim = DaySim(seed=7, days=1, trace=True)
    events: Events = []

    def emit(stage: str, data: dict[str, Any]) -> None:
        events.append((stage, data))
        on_event(sim, stage, data)

    asyncio.run(sim.run(emit))
    idx = next(i for i, (st, d) in enumerate(events)
               if st == "feedback" and d["id"] == "p1" and d["source"] == "viewer")
    st, t = events[idx + 1]
    assert st == "trace" and t["kind"] == "feedback" and t["proposal_id"] == "p1"
    assert "보는 사람" in t["nodes"][0]["detail_ko"] and "그만해" in t["nodes"][0]["detail_ko"]


def test_recording_errors_still_send_the_first_alert(monkeypatch: pytest.MonkeyPatch) -> None:
    def boom(*a: Any, **kw: Any) -> Any:
        raise RuntimeError("recorder broke")

    for name in ("note", "judge_call", "take", "take_judge", "next_id"):
        monkeypatch.setattr(_TraceRecorder, name, boom)
    sim = DaySim(seed=7, days=3, trace=True)
    _run(sim)
    firsts = [s for s in sim.safety_events if s["kind"] == "first_alert"]
    assert len(firsts) == 3 and all(f["sent"] and f["decision"] == "IMMEDIATE" for f in firsts)


def test_no_trait_words_in_safety_and_feedback_traces(seed7: tuple[DaySim, Events]) -> None:
    _, events = seed7
    blob = json.dumps(_traces(events), ensure_ascii=False)
    assert not any(word in blob for word in TRAIT_WORDS)


# ── 문구: trace 설명에 영문 열거값이 새지 않는다 ──────────────────────────────

# 허용: 모델 id(provider:model, 예 jev:jev-latest·anthropic:claude-haiku-5-5), 단위 ms, 모델 이름 luna.
_ALLOWED_ASCII = {"ms", "luna", "Jev"}


def _ascii_words(text: str) -> set[str]:
    import re

    text = re.sub(r"\b[a-z0-9]+:[\w.\-]+", "", text)
    return set(re.findall(r"[A-Za-z]{2,}", text)) - _ALLOWED_ASCII


def test_trace_details_have_no_ascii_enum_words(
    seed1: tuple[DaySim, Events], seed7: tuple[DaySim, Events], tmp_path: Path,
) -> None:
    cloud = DaySim(seed=7, days=2, models="cloud", cloud=_kit(tmp_path), trace=True)
    runs = [seed1[1], seed7[1], _run(cloud)]
    leaked: set[str] = set()
    for events in runs:
        for t in _traces(events):
            for n in t["nodes"]:
                leaked |= _ascii_words(n["detail_ko"])
    assert leaked == set()


def test_route_node_shows_the_confirmation_in_korean(seed7: tuple[DaySim, Events]) -> None:
    _, events = seed7
    routes = [n["detail_ko"] for t in _traces(events) if t["kind"] == "comfort"
              for n in t["nodes"] if n["id"] == "route"]
    assert routes
    assert all(any(ko in r for ko in ("확인 불필요", "가벼운 확인", "명시 확인", "이중 확인"))
               for r in routes)
    assert not any(word in r for r in routes for word in ("light", "none", "explicit", "double"))


# ── trace 조립 함수가 깨져도 실행은 같다(가드마다 하나씩) ─────────────────────────


@pytest.mark.parametrize("name", [
    "_last_comfort_judgment", "_comfort_trace", "_emit_safety_main", "_severity_sources_ko",
    "_emit_followups", "_safety_judgment_row", "_followup_sent", "_emit_feedback_trace",
])
def test_trace_builder_errors_never_change_the_run(
    name: str, monkeypatch: pytest.MonkeyPatch,
) -> None:
    def boom(*a: Any, **kw: Any) -> Any:
        raise RuntimeError("trace builder broke")

    monkeypatch.setattr(DaySim, name, boom)
    on = DaySim(seed=7, days=3, trace=True)
    off = DaySim(seed=7, days=3)
    on_events, off_events = _run(on), _run(off)
    assert [(st, d) for st, d in on_events if st != "trace"] == off_events
    assert on.summary() == off.summary()
    firsts = [s for s in on.safety_events if s["kind"] == "first_alert"]
    assert len(firsts) == 3 and all(f["sent"] and f["decision"] == "IMMEDIATE" for f in firsts)


# ── followup 은 상황마다: 2단계가 정말 올린 상황만 '보냈어요' ──────────────────────


class _RaiseFirstSituation(FakeJev):
    """첫 2단계 요청(파급도 질문이 있는)에만 가장 높은 선택지를 답한다 — 그 상황만 올라간다."""

    def __init__(self) -> None:
        super().__init__()
        self.raised = 0

    def post(self, payload: dict[str, Any], *, timeout_s: float) -> dict[str, Any]:
        out = super().post(payload, timeout_s=timeout_s)
        if "severity" in payload["questions"] and not self.raised:
            self.raised += 1
            for answer in out["answers"].values():
                answer["probabilities"] = {"0": 0.0, "1": 0.0, "2": 0.1, "3": 0.9}
        return out


def test_followup_says_sent_only_for_the_situation_phase2_raised(tmp_path: Path) -> None:
    kit = _kit(tmp_path)
    kit.judge = JevBackend(transport=_RaiseFirstSituation())
    sim = DaySim(seed=7, days=3, models="cloud", cloud=kit, trace=True)
    events = _run(sim)
    follow = [t for t in _traces(events) if t["kind"] == "safety" and t["part"] == "followup"]
    assert len(follow) == 3
    said = [t["nodes"][2]["detail_ko"] for t in follow]
    assert said == ["올린 판정으로 따라가는 알림을 보냈어요"] + ["따라가는 알림 없음 · 올렸는지 기록 없음"] * 2
    # 실제로 후속 알림이 나갔다(2단계가 백그라운드라 송신 단계는 phase2 가 아닐 수 있다).
    notices = [s for s in sim.safety_events if s["kind"] == "notice"]
    first = next(s for s in sim.safety_events if s.get("id") == follow[0]["proposal_id"])
    assert len(notices) == 1 and (notices[0]["day"], notices[0]["hhmm"]) == (first["day"], first["hhmm"])
    assert follow[0]["nodes"][1]["detail_ko"] == "파급도 위험(하한 경고)"
    for t in follow:
        assert t["nodes"][3]["detail_ko"] in ("확인돼 재알림 멈춤", "재알림 예약 없음") or re.fullmatch(
            r"재알림 예약 \d\d:\d\d", t["nodes"][3]["detail_ko"])


def test_cloud_recording_errors_never_change_the_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    def boom(*a: Any, **kw: Any) -> Any:
        raise RuntimeError("recorder broke")

    def run(trace: bool, sub: str) -> tuple[DaySim, Events]:
        kit = _kit(tmp_path / sub)
        kit.judge = JevBackend(transport=_RaiseFirstSituation())
        sim = DaySim(seed=7, days=2, models="cloud", cloud=kit, trace=trace)
        return sim, _run(sim)

    off, off_events = run(False, "off")
    for name in ("note", "judge_call", "take", "take_judge", "next_id"):
        monkeypatch.setattr(_TraceRecorder, name, boom)
    for name in ("_comfort_trace", "_emit_followups", "_emit_safety_main"):
        monkeypatch.setattr(DaySim, name, boom)
    on, on_events = run(True, "on")

    def stable(events: Events) -> list[tuple[Any, ...]]:
        # 실제 걸린 시간(초·토큰 장부)은 실행마다 다르다 — 무엇이 어떤 순서로 나갔는지만 견준다.
        keep = ("proposal", "safety", "feedback", "step")
        return [(st, d.get("id"), d.get("kind"), d.get("hhmm")) for st, d in events if st in keep]

    assert stable(on_events) == stable(off_events)
    assert [p["id"] for p in on.proposals] == [p["id"] for p in off.proposals]
    assert [(s["kind"], s.get("id")) for s in on.safety_events] == [
        (s["kind"], s.get("id")) for s in off.safety_events]
    firsts = [s for s in on.safety_events if s["kind"] == "first_alert"]
    assert firsts and all(f["sent"] and f["decision"] == "IMMEDIATE" for f in firsts)
    assert not any(st == "trace" and d["kind"] in ("comfort", "safety") for st, d in on_events)


# ── luna 의 기기 열쇠는 trace 실행에만 · 하한의 출처는 실제 판단에서 ─────────────────


def test_luna_entity_id_appears_in_the_summary_only_when_trace_is_on(tmp_path: Path) -> None:
    def luna(trace: bool, sub: str) -> list[dict[str, Any]]:
        sim = DaySim(seed=7, days=1, models="cloud", cloud=_kit(tmp_path / sub), trace=trace)
        _run(sim)
        rows: list[dict[str, Any]] = sim.summary()["luna"]
        assert rows and all("entity_id" in r for r in sim.luna_results)
        return rows

    assert not any("entity_id" in r for r in luna(False, "off"))
    assert all("entity_id" in r for r in luna(True, "on"))


def test_floor_names_the_severity_sources_of_the_first_alert_judgment(
    seed7: tuple[DaySim, Events],
) -> None:
    from types import SimpleNamespace

    _, events = seed7
    floors = [t["nodes"][2]["detail_ko"] for t in _traces(events)
              if t["kind"] == "safety" and t["part"] == "main"]
    assert len(floors) == 3 and all(f.endswith("(표)") for f in floors)

    sim = DaySim(seed=7, days=1, trace=True)
    judgment = SimpleNamespace(judgment_id="j1", severity_sources=["table", "resident"])
    sim._svc = SimpleNamespace(app=SimpleNamespace(last_trace={"judgment": judgment}))  # type: ignore[assignment]
    assert sim._severity_sources_ko(SimpleNamespace(judgment_id="j1")) == "표·거주자 설정"  # type: ignore[arg-type]
    assert sim._severity_sources_ko(SimpleNamespace(judgment_id="j2")) == "출처 기록 없음"  # type: ignore[arg-type]


def test_floor_text_comes_from_the_sources_lookup(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[str] = []
    real = DaySim._severity_sources_ko

    def spy(self: DaySim, p: Any) -> str:
        out = real(self, p)
        seen.append(out)
        return out + "†"  # 표시가 실제로 이 값에서 왔는지 가린다

    monkeypatch.setattr(DaySim, "_severity_sources_ko", spy)
    sim = DaySim(seed=7, days=3, trace=True)
    floors = [t["nodes"][2]["detail_ko"] for t in _traces(_run(sim))
              if t["kind"] == "safety" and t["part"] == "main"]
    assert seen == ["표"] * 3
    assert len(floors) == 3 and all(f.endswith("(표†)") for f in floors)


class _RaiseEverySituation(FakeJev):
    """2단계(파급도 질문이 있는) 요청마다 가장 높은 선택지를 답한다."""

    def post(self, payload: dict[str, Any], *, timeout_s: float) -> dict[str, Any]:
        out = super().post(payload, timeout_s=timeout_s)
        if "severity" in payload["questions"]:
            for answer in out["answers"].values():
                answer["probabilities"] = {"0": 0.0, "1": 0.0, "2": 0.1, "3": 0.9}
        return out


def test_two_situations_in_one_step_get_their_own_followup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    real_script = day_sim.build_script

    def both_at_once(seed: int, days: int) -> list[Any]:
        plans = real_script(seed, days)
        for step in plans[0].steps:  # 첫날: 누수와 가스가 같은 단계에 함께 열린다
            step.readings[day_sim.GAS] = step.readings[day_sim.LEAK]
        return plans

    monkeypatch.setattr(day_sim, "build_script", both_at_once)
    kit = _kit(tmp_path)
    kit.judge = JevBackend(transport=_RaiseEverySituation())
    sim = DaySim(seed=7, days=1, models="cloud", cloud=kit, trace=True)
    events = _run(sim)
    follow = {t["outcome_ko"].split(" — ")[0]: t for t in _traces(events)
              if t["kind"] == "safety" and t["part"] == "followup"}
    assert set(follow) == {"누수", "가스 누출"}
    leak, gas = follow["누수"]["nodes"], follow["가스 누출"]["nodes"]
    # 누수는 하한(경고) 위로 올라 후속을 보냈고, 가스는 하한이 이미 위험이라 올릴 것이 없다.
    assert leak[1]["detail_ko"] == "파급도 위험(하한 경고)"
    assert leak[2]["detail_ko"] == "올린 판정으로 따라가는 알림을 보냈어요"
    assert gas[1]["detail_ko"] == "파급도 위험(하한 위험)"
    assert gas[2]["detail_ko"] == "따라가는 알림 없음 · 올렸는지 기록 없음"
    firsts = [s for s in sim.safety_events if s["kind"] == "first_alert"]
    assert len({(s["day"], s["hhmm"]) for s in firsts}) == 1 and len(firsts) == 2


def test_ladder_says_stopped_when_the_resident_already_acked(tmp_path: Path) -> None:
    # cloud 모드는 그 단계의 읽기 뒤에 누름을 적용한다 — 거주자의 [괜찮아요]가 같은 단계에 들어가
    # 단계 끝 사다리는 확인됨(acked, 다음 발화 없음)이다.
    sim = DaySim(seed=7, days=3, models="cloud", cloud=_kit(tmp_path), trace=True)
    events = _run(sim)
    follow = [t for t in _traces(events) if t["kind"] == "safety" and t["part"] == "followup"]
    assert len(follow) == 3
    for t in follow:
        first = next(s for s in sim.safety_events if s.get("id") == t["proposal_id"])
        ack = next(d for st, d in events if st == "feedback" and d["id"] == t["proposal_id"])
        assert ack["signal"] == "ok" and (ack["day"], ack["hhmm"]) == (first["day"], first["hhmm"])
        assert t["nodes"][3]["detail_ko"] == "확인돼 재알림 멈춤"


# ── 갈림길·엣지 문구(편의·침묵) ──────────────────────────────────────────────

_FIXED_BRANCHES = {
    "rules": day_sim._RULES_BRANCHES, "gate1": day_sim._GATE1_BRANCHES,
    "judge": day_sim._JUDGE_BRANCHES, "gate": day_sim._GATE_BRANCHES,
}


def _spy_comfort(monkeypatch: pytest.MonkeyPatch) -> list[tuple[dict[str, Any], Any, dict[str, Any]]]:
    """편의 trace 조립마다 (재료, 그때의 평가 결과, 만든 trace) 를 남긴다."""
    seen: list[tuple[dict[str, Any], Any, dict[str, Any]]] = []
    real = DaySim._comfort_trace

    def spy(self: DaySim, rec: _TraceRecorder, p: dict[str, Any]) -> dict[str, Any]:
        evaluation = rec.evaluation
        out = real(self, rec, p)
        seen.append((p, evaluation, out))
        return out

    monkeypatch.setattr(DaySim, "_comfort_trace", spy)
    return seen


def _taken(node: dict[str, Any]) -> dict[str, Any]:
    taken: list[dict[str, Any]] = [b for b in node["branches"] if b["taken"]]
    assert len(taken) == 1, node
    return taken[0]


def _node_of(trace: dict[str, Any], nid: str) -> dict[str, Any]:
    return next(n for n in trace["nodes"] if n["id"] == nid)


def test_comfort_branch_sets_match_the_tables_with_exactly_one_taken(
    seed1: tuple[DaySim, Events], seed7: tuple[DaySim, Events], tmp_path: Path,
) -> None:
    cloud = _run(DaySim(seed=7, days=2, models="cloud", cloud=_kit(tmp_path), trace=True))
    count = 0
    for events in (seed1[1], seed7[1], cloud):
        for t in _traces(events):
            if t["kind"] not in ("comfort", "silent"):
                continue
            for n in t["nodes"]:
                assert set(n) >= {"branches", "edge_ko"}
                if n["id"] in _FIXED_BRANCHES and n["state"] != "skipped":
                    assert [b["key"] for b in n["branches"]] == [
                        k for k, _, _ in _FIXED_BRANCHES[n["id"]]]
                    _taken(n)
                    count += 1
                if n["state"] == "skipped":
                    assert n["branches"] == [] and n["edge_ko"] is None
    assert count > 20


def test_silent_traces_take_the_exit_where_they_ended(monkeypatch: pytest.MonkeyPatch) -> None:
    seen = _spy_comfort(monkeypatch)
    _run(DaySim(seed=1, days=4, trace=True))
    _run(DaySim(seed=9, days=7, trace=True))  # 점수 침묵이 나는 씨앗
    ends = set()
    for p, _ev, t in seen:
        if t["kind"] != "silent":
            continue
        end = _end(t)
        ends.add(end)
        node = _node_of(t, str(end))
        taken = _taken(node)
        assert taken["exit"] and node["edge_ko"] is None
        if end == "rules":
            why = p["decision"].suppressed_by
            assert taken["key"] == (why if why in day_sim._SILENCE_KO else "unknown")
            assert taken["value_ko"].endswith("마지막으로 본 규칙의 사유")
        if end == "score":
            assert taken["key"] == "noop"
    assert {"rules", "score"} <= ends


def test_gate1_and_judge_exits_follow_the_recorded_values(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    import dataclasses

    from liot.core.risk import RiskTier as RT

    real = day_sim._TimedGuardrail.evaluate

    def high(self: Any, candidate: Any, ctx: Any = None) -> Gate1Result:
        out = real(self, candidate, ctx)
        if candidate.category not in ("leak", "gas"):
            out = dataclasses.replace(out, floor_tier=RT.DUSK)
            self.recorder.note((candidate.entity_id, "comfort"), gate1=out)
        return out

    monkeypatch.setattr(day_sim._TimedGuardrail, "evaluate", high)
    events = _run(DaySim(seed=7, days=1, trace=True))
    g1 = [t for t in _traces(events) if t["kind"] == "silent" and _end(t) == "gate1"]
    assert g1
    for t in g1:
        taken = _taken(_node_of(t, "gate1"))
        assert taken["key"] == "ceiling" and taken["value_ko"] == "하한 땅거미 > 능동 상한 숨결 → 침묵"


def test_score_branches_are_the_evaluation_values(monkeypatch: pytest.MonkeyPatch) -> None:
    seen = _spy_comfort(monkeypatch)
    _run(DaySim(seed=7, days=3, trace=True))
    checked = 0
    for _p, ev, t in seen:
        node = _node_of(t, "score")
        if ev is None:
            continue
        values = {b["key"]: b["value_ko"] for b in node["branches"]}
        for key, value in values.items():
            if key.startswith("c"):
                assert value == f"{ev.scored[int(key[1:])].final_score:.3f}"
        taken = _taken(node)
        noop = next(i for i, ac in enumerate(ev.scored) if ac.origin == "noop")
        assert taken["key"] == ("noop" if ev.selected_index == noop else f"c{ev.selected_index}")
        if t["kind"] == "comfort":
            sel = ev.scored[ev.selected_index]
            sign = ">" if sel.final_score > ev.scored[noop].final_score else "≥"
            assert f" {sel.final_score:.3f} {sign} 아무것도 안 하기 {ev.scored[noop].final_score:.3f}" in (
                node["edge_ko"])
            assert node["edge_ko"].endswith("· 출처: 규칙 줄")
        checked += 1
    assert checked >= 5


def test_route_edge_is_the_required_confirmation(monkeypatch: pytest.MonkeyPatch) -> None:
    seen = _spy_comfort(monkeypatch)
    _run(DaySim(seed=7, days=3, trace=True))
    routes = [(p["result"].proposal, p["judgment"]["judgment"], _node_of(t, "route"))
              for p, _e, t in seen if t["kind"] == "comfort"]
    cases = set()
    for prop, judged, node in routes:
        confirm = day_sim._CONFIRMATION_KO[prop.required_confirmation]
        if prop.risk_tier != judged.risk_tier:  # 라우터가 기기 기준으로 올렸다(risk_router.py:87-95)
            assert prop.risk_tier > judged.risk_tier
            assert node["edge_ko"] == (f"판정 {judged.risk_tier.label_ko} → 기기 기준 "
                                       f"{prop.risk_tier.label_ko}로 올림 → {confirm}")
            cases.add("raised")
        else:
            assert node["edge_ko"] == f"{prop.risk_tier.label_ko} → {confirm}"
            cases.add("equal")
    assert cases == {"raised", "equal"}


def test_judge_edge_fake_and_cloud_success(seed7: tuple[DaySim, Events], tmp_path: Path) -> None:
    fake = [_node_of(t, "judge") for t in _traces(seed7[1]) if t["kind"] == "comfort"]
    assert fake and all(n["edge_ko"] == "가짜 판정(확률 없음)" for n in fake)
    sim = DaySim(seed=7, days=1, models="cloud", cloud=_kit(tmp_path), trace=True)
    cloud = [_node_of(t, "judge") for t in _traces(_run(sim)) if t["kind"] == "comfort"]
    assert cloud
    heads = set()
    for n in cloud:
        # FakeJev 는 가장 낮은 선택지를 답한다 — 하한이 그보다 높은 후보는 injection_suspect 다.
        head = n["edge_ko"].split(" · ")[0]
        heads.add("injection" if head == "모델이 하한보다 낮게 답해 하한 유지" else head[:3])
        assert head.startswith("최종 ") and "(하한 " in head or head.startswith("모델이 하한보다")
        assert " · Jev 최댓값 " in n["edge_ko"]
        assert n["edge_ko"].endswith(("% 일치", "% 불일치", "% —"))
    assert heads == {"최종 ", "injection"}  # 두 경우가 실제 실행에서 모두 나온다


def test_judge_edge_names_luna_when_it_could_not_compare(tmp_path: Path) -> None:
    from liot.luna_shadow import LunaConfig, LunaShadow

    class BrokenLuna:
        def post(self, request: Any, *, timeout_s: float) -> dict[str, Any]:
            raise RuntimeError("luna down")

    kit = _kit(tmp_path)
    kit.luna = LunaShadow(LunaConfig("logprobs", "gpt-6-luna", 1.0), "k", transport=BrokenLuna())
    sim = DaySim(seed=7, days=1, models="cloud", cloud=kit, trace=True)
    edges = [_node_of(t, "judge")["edge_ko"] for t in _traces(_run(sim)) if t["kind"] == "comfort"]
    assert edges
    reasons = {r["reason_ko"] for r in sim.luna_results}
    for e in edges:
        assert " · Jev 최댓값 " in e and any(e.endswith(r) for r in reasons if r)


def test_judge_ceiling_value_when_the_cloud_judge_fails(tmp_path: Path) -> None:
    class Down(FakeJev):
        def post(self, payload: dict[str, Any], *, timeout_s: float) -> dict[str, Any]:
            raise RuntimeError("jev down")

    kit = _kit(tmp_path)
    kit.judge = JevBackend(transport=Down())
    events = _run(DaySim(seed=7, days=1, models="cloud", cloud=kit, trace=True))
    ends = [t for t in _traces(events) if t["kind"] == "silent" and _end(t) == "judge"]
    assert ends
    for t in ends:
        node = _node_of(t, "judge")
        taken = _taken(node)
        assert taken["key"] == "ceiling" and node["edge_ko"] is None
        assert taken["value_ko"] == "모델 응답 없음 → 안전하게 올림(땅거미) → 상한이라 침묵"


def test_judge_edge_after_the_call_cap_switches_to_the_fake(tmp_path: Path) -> None:
    sim = DaySim(seed=7, days=2, models="cloud", cloud=_kit(tmp_path, call_cap=3), trace=True)
    edges = [_node_of(t, "judge")["edge_ko"] for t in _traces(_run(sim)) if t["kind"] == "comfort"]
    assert "상한 도달 · 가짜 판정(확률 없음)" in edges


def test_judge_text_for_injection_suspect_and_unreadable_probabilities(tmp_path: Path) -> None:
    from types import SimpleNamespace

    from liot.core.risk import RiskTier as RT

    sim = DaySim(seed=7, days=1, models="cloud", cloud=_kit(tmp_path), trace=True)
    assert sim._kit is not None and sim._kit.judge is not None
    model_id = sim._kit.judge.model_id
    gate1 = SimpleNamespace(floor_tier=RT.BREATH)
    sus = SimpleNamespace(model_id=model_id, gate2_verdict="injection_suspect", degraded=False,
                          risk_tier=RT.BREATH,
                          model_probs={"tier": {"ambient": 0.8, "breath": 0.2}})
    assert sim._judge_text_ko(sus, gate1, "light.x", "07:00") == (  # type: ignore[arg-type]
        "모델이 하한보다 낮게 답해 하한 유지 · Jev 최댓값 잔잔 80%")
    odd = SimpleNamespace(model_id=model_id, gate2_verdict="concur", degraded=False,
                          risk_tier=RT.BREATH, model_probs={"tier": {"???": 1.0}})
    assert sim._judge_text_ko(odd, gate1, "light.x", "07:00") == "최종 숨결(하한 숨결)"  # type: ignore[arg-type]


def test_branch_text_has_no_ascii_trait_words_or_device_ids(
    seed1: tuple[DaySim, Events], seed7: tuple[DaySim, Events], tmp_path: Path,
) -> None:
    cloud = _run(DaySim(seed=7, days=2, models="cloud", cloud=_kit(tmp_path), trace=True))
    texts: list[str] = []
    for events in (seed1[1], seed7[1], cloud):
        for t in _traces(events):
            for n in t["nodes"]:
                texts.append(n["edge_ko"] or "")
                for b in n["branches"]:
                    texts += [b["label_ko"], b["value_ko"] or ""]
    leaked: set[str] = set()
    for text in texts:
        leaked |= _ascii_words(text)
        assert not any(w in text for w in TRAIT_WORDS)
        assert "sensor." not in text and "light." not in text
    assert leaked == set()


@pytest.mark.parametrize("name", ["_score_branches", "_judge_text_ko", "_luna_ko", "_branch_title"])
def test_branch_builder_errors_never_change_the_run(
    name: str, monkeypatch: pytest.MonkeyPatch,
) -> None:
    def boom(*a: Any, **kw: Any) -> Any:
        raise RuntimeError("branch builder broke")

    monkeypatch.setattr(DaySim, name, boom)
    on = DaySim(seed=7, days=3, trace=True)
    off = DaySim(seed=7, days=3)
    on_events, off_events = _run(on), _run(off)
    assert [(st, d) for st, d in on_events if st != "trace"] == off_events
    assert on.summary() == off.summary()


def test_a_value_outside_the_table_takes_unknown() -> None:
    for taken in ("budget_v2", None, ""):
        out = day_sim._branches(day_sim._RULES_BRANCHES, taken)
        assert [b["key"] for b in out if b["taken"]] == ["unknown"]
    out = day_sim._branches(day_sim._GATE_BRANCHES, "DIGEST", {"DIGEST": "x"})
    assert [(b["key"], b["value_ko"]) for b in out if b["taken"]] == [("DIGEST", "x")]


def test_score_edge_uses_greater_or_equal_on_a_tie(monkeypatch: pytest.MonkeyPatch) -> None:
    seen = _spy_comfort(monkeypatch)
    sim = DaySim(seed=7, days=1, trace=True)
    _run(sim)
    ev = next(e for _p, e, t in seen if t["kind"] == "comfort" and e is not None)
    noop = next(i for i, ac in enumerate(ev.scored) if ac.origin == "noop")
    sel = ev.scored[ev.selected_index]
    tied = ev.model_copy(deep=True)
    tied.scored[noop] = tied.scored[noop].model_copy(update={"final_score": sel.final_score})
    _branches, edge = sim._score_branches(tied)
    assert edge is not None and f"{sel.final_score:.3f} ≥ 아무것도 안 하기 {sel.final_score:.3f}" in edge
    _branches, edge = sim._score_branches(ev)
    assert edge is not None and " > 아무것도 안 하기 " in edge


# ── 갈림길(안전·followup·피드백) · 선호 전후 ──────────────────────────────────

_FIXED_BRANCHES_2 = {
    ("followup", "model"): day_sim._MODEL_BRANCHES, ("followup", "ladder"): day_sim._LADDER_BRANCHES,
    ("feedback", "phone"): day_sim._CHOICE_BRANCHES, ("feedback", "feedback"): day_sim._APPLY_BRANCHES,
}


def _followups(events: Events) -> list[dict[str, Any]]:
    return [t for t in _traces(events) if t["kind"] == "safety" and t["part"] == "followup"]


def test_safety_and_feedback_branch_sets_match_the_tables(
    seed7: tuple[DaySim, Events], tmp_path: Path,
) -> None:
    cloud = _run(DaySim(seed=7, days=2, models="cloud", cloud=_kit(tmp_path), trace=True))
    seen: set[tuple[str, str]] = set()
    for events in (seed7[1], cloud):
        for t in _traces(events):
            for n in t["nodes"]:
                key = (t["part"], n["id"])
                is_comfort_fb = t["kind"] == "feedback" and _node_of(t, "phone") and len(t["nodes"]) == 3
                if key in _FIXED_BRANCHES_2 and (key[0] != "feedback" or is_comfort_fb):
                    assert [b["key"] for b in n["branches"]] == [
                        k for k, _, _ in _FIXED_BRANCHES_2[key]]
                    _taken(n)
                    seen.add(key)
                elif t["kind"] == "safety" or (t["kind"] == "feedback" and not is_comfort_fb):
                    assert n["branches"] == []
    assert seen == set(_FIXED_BRANCHES_2)
    for events in (seed7[1], cloud):
        for t in _traces(events):
            if t["kind"] == "safety" and t["part"] == "main":
                assert _node_of(t, "classify")["edge_ko"] == "이상 값 → 안전 경보 경로"
                first = _node_of(t, "first_alert")
                assert first["edge_ko"] == f"모델 없이 즉시 · {first['real_ms']:.1f}ms"


def test_followup_model_edges_fake_and_cloud(seed7: tuple[DaySim, Events], tmp_path: Path) -> None:
    for t in _followups(seed7[1]):
        assert _node_of(t, "model")["edge_ko"] == "가짜 판정(확률 없음)"
        assert _taken(_node_of(t, "model"))["key"] == "not_sent"
        assert _node_of(t, "severity")["edge_ko"] == "따라가는 알림 없음 · 올렸는지 기록 없음"
        ladder = _taken(_node_of(t, "ladder"))
        assert ladder["key"] == "scheduled" and re.fullmatch(r"\d\d:\d\d", ladder["value_ko"])
    sim = DaySim(seed=7, days=2, models="cloud", cloud=_kit(tmp_path), trace=True)
    cloud = _followups(_run(sim))
    assert cloud
    for t in cloud:
        edge = _node_of(t, "model")["edge_ko"]
        assert edge.startswith("Jev 최댓값 ") and " · 파급도 최댓값 " in edge and " · luna " in edge
        assert _taken(_node_of(t, "ladder"))["key"] == "acked"


def test_followup_sent_branch_and_its_edge(tmp_path: Path) -> None:
    kit = _kit(tmp_path)
    kit.judge = JevBackend(transport=_RaiseFirstSituation())
    follow = _followups(_run(DaySim(seed=7, days=3, models="cloud", cloud=kit, trace=True)))
    keys = [_taken(_node_of(t, "model"))["key"] for t in follow]
    assert keys == ["sent", "not_sent", "not_sent"]
    assert _node_of(follow[0], "severity")["edge_ko"] == "따라가는 알림 보냄"


def test_followup_not_sent_when_the_raised_situation_already_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    import sys

    from liot.db.safety_repo import SafetyRepo

    real = SafetyRepo.get_open_situation

    async def closed_for_phase2(self: Any, *a: Any, **kw: Any) -> Any:
        # 2단계가 '올렸는데' 그 사이 상황이 닫힌 경우를 흉내 낸다(서비스는 보내지 않고 None).
        if sys._getframe(1).f_code.co_name == "_run_phase2":
            return None
        return await real(self, *a, **kw)

    monkeypatch.setattr(SafetyRepo, "get_open_situation", closed_for_phase2)
    kit = _kit(tmp_path)
    kit.judge = JevBackend(transport=_RaiseFirstSituation())
    sim = DaySim(seed=7, days=1, models="cloud", cloud=kit, trace=True)
    follow = _followups(_run(sim))
    assert len(follow) == 1
    assert not [s for s in sim.safety_events if s["kind"] == "notice"]
    model = _node_of(follow[0], "model")
    assert _taken(model)["key"] == "not_sent"
    assert _node_of(follow[0], "followup")["detail_ko"] == "따라가는 알림 없음 · 올렸는지 기록 없음"


def test_followup_error_and_deadline_branches(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    class Down(FakeJev):
        def post(self, payload: dict[str, Any], *, timeout_s: float) -> dict[str, Any]:
            raise RuntimeError("jev down")

    kit = _kit(tmp_path / "down")
    kit.judge = JevBackend(transport=Down())
    follow = _followups(_run(DaySim(seed=7, days=1, models="cloud", cloud=kit, trace=True)))
    assert follow and all(_taken(_node_of(t, "model"))["key"] == "error" for t in follow)
    assert all(_node_of(t, "model")["edge_ko"] == "모델 응답 없음 · 하한 유지" for t in follow)

    import threading
    import time as _time

    class SlowSeverity(FakeJev):
        def __init__(self) -> None:
            super().__init__()
            self.slowed = threading.Event()

        def post(self, payload: dict[str, Any], *, timeout_s: float) -> dict[str, Any]:
            if "severity" in payload["questions"] and not self.slowed.is_set():
                self.slowed.set()
                _time.sleep(0.6)
            return super().post(payload, timeout_s=timeout_s)

    monkeypatch.setattr(day_sim, "CLOUD_PHASE2_DEADLINE_S", 0.2)
    kit = _kit(tmp_path / "slow")
    kit.judge = JevBackend(transport=SlowSeverity())
    follow = _followups(_run(DaySim(seed=7, days=1, models="cloud", cloud=kit, trace=True)))
    assert follow and _taken(_node_of(follow[0], "model"))["key"] == "deadline"


def test_refused_tap_takes_the_refused_branch_with_the_choice() -> None:
    sim = DaySim(seed=7, days=1, trace=True)
    events: Events = []

    def emit(stage: str, data: dict[str, Any]) -> None:
        events.append((stage, data))
        if stage == "proposal" and data["id"] == "p1":
            sim.enqueue_feedback("p1", "dismissed")
            sim.enqueue_feedback("p1", "accepted")  # 이미 처리된 제안 — 서버가 거절한다

    asyncio.run(sim.run(emit))
    fb = [d for st, d in events if st == "feedback" and d["id"] == "p1" and d["source"] == "viewer"]
    assert [d["status"] for d in fb] == ["200", "403"]
    traces = [t for t in _traces(events) if t["kind"] == "feedback" and t["proposal_id"] == "p1"]
    refused = traces[1]
    # 폰은 거주자가 고른 것(도움됐어요) — 거절은 피드백 노드의 출구다.
    choice = _taken(_node_of(refused, "phone"))
    assert choice["key"] == "accepted" and _node_of(refused, "phone")["edge_ko"] == "도움됐어요"
    taken = _taken(_node_of(refused, "feedback"))
    assert taken["key"] == "refused" and taken["exit"] and taken["value_ko"] == "서버 응답 403"
    assert _node_of(refused, "feedback")["edge_ko"] is None
    assert _node_of(refused, "prefs")["state"] == "skipped"
    assert _taken(_node_of(traces[0], "phone"))["key"] == "dismissed"
    assert _taken(_node_of(traces[0], "feedback"))["key"] == "applied"


def test_pref_edges_equal_independent_reads_around_taps_and_ticks(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import datetime

    from liot.layers.l2_context.feature_vector import NEUTRAL_PREF, load_cells
    from liot.service import LiotService

    taps: dict[str, tuple[float, float]] = {}
    # 틱마다 (집 지역 날짜·시각, 제안 → 셀, 직전 셀, 직후 셀).
    ticks: dict[tuple[str, str], tuple[dict[str, str], dict[str, float], dict[str, float]]] = {}
    real_route, real_tick = LiotService._route_feedback, LiotService.run_tick

    async def route(self: LiotService, request_line: str) -> Any:
        pid = request_line.split("p=", 1)[1].split("&", 1)[0]
        key = self._prop_cell.get(pid)
        before = await load_cells(self.prefs)
        out = await real_route(self, request_line)
        after = await load_cells(self.prefs)
        if key is not None:
            taps[pid] = (before.get(key, NEUTRAL_PREF), after.get(key, NEUTRAL_PREF))
        return out

    async def tick(self: LiotService) -> Any:
        local = self.clock.local_dt()
        cells, cell_of = await load_cells(self.prefs), dict(self._prop_cell)
        out = await real_tick(self)
        ticks[(local.date().isoformat(), local.strftime("%H:%M"))] = (
            cell_of, cells, await load_cells(self.prefs))
        return out

    monkeypatch.setattr(LiotService, "_route_feedback", route)
    monkeypatch.setattr(LiotService, "run_tick", tick)
    sim = DaySim(seed=7, days=3, trace=True)
    events = _run(sim)
    tap_n = exp_n = 0
    for t in _traces(events):
        if t["kind"] != "feedback" or len(t["nodes"]) != 3 or t["nodes"][1]["state"] != "done":
            continue
        pid = sim._tracked[t["proposal_id"]].pid
        edge = _node_of(t, "feedback")["edge_ko"]
        if _taken(_node_of(t, "phone"))["key"] == "expired":
            # 만료는 그 만료가 일어난 단계(trace 의 날·시각)의 틱 직전·직후 값이다.
            day, hhmm = t["clock_ko"].split("일차 ")
            date = (datetime.date.fromisoformat(day_sim.START_DATE)
                    + datetime.timedelta(days=int(day) - 1)).isoformat()
            cell_of, before, after = ticks[(date, hhmm)]
            key = cell_of[pid]
            assert edge == (f"이 제안 칸 선호 {before.get(key, NEUTRAL_PREF):.2f} → "
                            f"{after.get(key, NEUTRAL_PREF):.2f} · 같은 순간 다른 만료의 감쇠 포함")
            assert before.get(key, NEUTRAL_PREF) != after.get(key, NEUTRAL_PREF)
            exp_n += 1
        else:
            tap_before, tap_after = taps[pid]
            assert edge == f"이 제안 칸 선호 {tap_before:.2f} → {tap_after:.2f}"
            tap_n += 1
    assert tap_n >= 3 and exp_n >= 1


@pytest.mark.parametrize("name", ["_pref_cells", "_expiry_pref", "_safety_model_text_ko"])
def test_phase2_builder_errors_never_change_the_run(
    name: str, monkeypatch: pytest.MonkeyPatch,
) -> None:
    def boom(*a: Any, **kw: Any) -> Any:
        raise RuntimeError("builder broke")

    monkeypatch.setattr(DaySim, name, boom)
    on = DaySim(seed=7, days=3, trace=True)
    off = DaySim(seed=7, days=3)
    on_events, off_events = _run(on), _run(off)
    assert [(st, d) for st, d in on_events if st != "trace"] == off_events
    assert on.summary() == off.summary()
    firsts = [s for s in on.safety_events if s["kind"] == "first_alert"]
    assert len(firsts) == 3 and all(f["sent"] and f["decision"] == "IMMEDIATE" for f in firsts)
    if name == "_pref_cells":
        edges = [_node_of(t, "feedback")["edge_ko"] for t in _traces(on_events)
                 if t["kind"] == "feedback" and len(t["nodes"]) == 3
                 and t["nodes"][1]["state"] == "done"]
        assert edges and all(e.startswith("선호 변화 기록 없음") for e in edges)


def test_cloud_trace_on_and_off_make_the_same_decisions_and_sends(tmp_path: Path) -> None:
    def run(trace: bool) -> tuple[DaySim, Events]:
        sim = DaySim(seed=7, days=3, models="cloud", cloud=_kit(tmp_path / str(trace)), trace=trace)
        return sim, _run(sim)

    (on, on_events), (off, off_events) = run(True), run(False)

    def stable(events: Events) -> list[tuple[Any, ...]]:
        keep = ("proposal", "safety", "feedback", "step", "digest", "day.end")
        return [(st, d.get("id"), d.get("kind"), d.get("hhmm"), d.get("decision"), d.get("signal"),
                 d.get("status")) for st, d in events if st in keep]

    assert stable(on_events) == stable(off_events)
    assert [(s["kind"], s.get("phase"), s.get("id"), s.get("hhmm")) for s in on.safety_events] == [
        (s["kind"], s.get("phase"), s.get("id"), s.get("hhmm")) for s in off.safety_events]
    assert [(p["id"], p["decision"], p.get("state")) for p in on.proposals] == [
        (p["id"], p["decision"], p.get("state")) for p in off.proposals]


def test_a_failing_after_read_leaves_no_pref_value_and_the_run_alone(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # 전 읽기는 되고 후 읽기만 깨지는 경우(틱·누름 모두 전·후가 이어서 불린다): 짝수 번째 호출만 실패.
    calls = {"n": 0}
    real = DaySim._pref_cells

    async def flaky(self: DaySim, svc: Any) -> Any:
        calls["n"] += 1
        if calls["n"] % 2 == 0:
            raise RuntimeError("after read broke")
        return await real(self, svc)

    monkeypatch.setattr(DaySim, "_pref_cells", flaky)
    on = DaySim(seed=7, days=3, trace=True)
    off = DaySim(seed=7, days=3)
    on_events, off_events = _run(on), _run(off)
    assert [(st, d) for st, d in on_events if st != "trace"] == off_events
    edges = [_node_of(t, "feedback")["edge_ko"] for t in _traces(on_events)
             if t["kind"] == "feedback" and len(t["nodes"]) == 3 and t["nodes"][1]["state"] == "done"]
    assert edges and all(e.startswith("선호 변화 기록 없음") for e in edges)
    assert calls["n"] > 10


def test_pref_reads_happen_only_in_trace_runs(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = {"n": 0}
    real = DaySim._pref_cells

    async def count(self: DaySim, svc: Any) -> Any:
        calls["n"] += 1
        return await real(self, svc)

    monkeypatch.setattr(DaySim, "_pref_cells", count)
    _run(DaySim(seed=7, days=2))
    assert calls["n"] == 0
    _run(DaySim(seed=7, days=2, trace=True))
    assert calls["n"] > 0


def test_gate1_blocked_takes_the_blocked_exit(monkeypatch: pytest.MonkeyPatch) -> None:
    import dataclasses

    real = day_sim._TimedGuardrail.evaluate

    def blocked(self: Any, candidate: Any, ctx: Any = None) -> Gate1Result:
        out = real(self, candidate, ctx)
        if candidate.category not in ("leak", "gas"):  # 편의 후보를 가드레일이 막았다고 치자(하한은 그대로)
            out = dataclasses.replace(out, blocked=True)
            self.recorder.note((candidate.entity_id, "comfort"), gate1=out)
        return out

    monkeypatch.setattr(day_sim._TimedGuardrail, "evaluate", blocked)
    sim = DaySim(seed=7, days=1, trace=True)
    events = _run(sim)
    ends = [t for t in _traces(events) if t["kind"] == "silent" and _end(t) == "gate1"]
    assert ends and not sim.proposals
    for t in ends:
        node = _node_of(t, "gate1")
        taken = _taken(node)
        assert taken["key"] == "blocked" and node["edge_ko"] is None
        assert re.fullmatch(r"차단 · 하한 \S+ → 침묵", taken["value_ko"])


def test_safety_model_edge_says_when_jev_sat_below_the_floor(tmp_path: Path) -> None:
    kit = _kit(tmp_path)
    kit.judge = JevBackend(transport=_RaiseFirstSituation())
    follow = _followups(_run(DaySim(seed=7, days=3, models="cloud", cloud=kit, trace=True)))
    edges = [_node_of(t, "model")["edge_ko"] for t in follow]
    assert len(edges) == 3
    # 첫 상황: Jev 가 가장 높은 답(불씨·위험) — 하한 아래가 아니다.
    assert "아래라 반영 안 됨" not in edges[0] and edges[0].startswith("Jev 최댓값 불씨 ")
    # 나머지: FakeJev 의 가장 낮은 답(잔잔·기록만) — 첫 경보 단계(불씨)와 파급도 하한 아래다.
    for e in edges[1:]:
        assert e.startswith("Jev 최댓값 잔잔 40% · 하한(불씨) 아래라 반영 안 됨 · 파급도 최댓값 기록만 40% · ")
        assert "파급도 하한(" in e and e.count("아래라 반영 안 됨") == 2


def test_first_alert_edge_uses_the_same_rounded_ms_as_the_node() -> None:
    # 반올림 경계 값: 2.6549ms → real_ms 2.65. 잰 초에서 바로 쓰면 '2.7', 같은 반올림 값에서는 '2.6'.
    import datetime
    from types import SimpleNamespace

    sim = DaySim(seed=7, days=1, trace=True)
    out: list[dict[str, Any]] = []
    sim._trace_emit = out.append
    p = SimpleNamespace(proposal_id="pid-1", risk_tier=RiskTier.EMBER, severity=None,
                        judgment_id="j-1", situation_id="s-1")
    transport = SimpleNamespace(sent_at={"pid-1": 10.0026549})
    step = SimpleNamespace(at_local=datetime.datetime(2026, 6, 1, 13, 0))
    sim._emit_safety_main(transport, step, day_sim.LEAK, "leak", "누수", "p1", p, 10.0)  # type: ignore[arg-type]
    first = _node_of(out[0], "first_alert")
    assert first["real_ms"] == round(0.0026549 * 1000.0, 2)
    assert first["edge_ko"] == f"모델 없이 즉시 · {first['real_ms']:.1f}ms"


def test_gate_main_edge_only_for_immediate_and_exit_text_in_the_branch() -> None:
    events = _run(DaySim(seed=9, days=7, trace=True))
    seen = set()
    for t in _traces(events):
        if t["kind"] != "comfort":
            continue
        gate = _node_of(t, "gate")
        taken = _taken(gate)
        seen.add(taken["key"])
        if taken["key"] == "IMMEDIATE":
            assert gate["edge_ko"] == "바로 폰 알림" and taken["value_ko"] is None
        elif taken["key"] == "DIGEST":
            # 모아보기는 폰으로 가는 두 번째 길(밤 리뷰) — 끊긴 출구가 아니다.
            assert gate["edge_ko"] == "밤 10시 모아보기로" and not taken["exit"]
            assert taken["value_ko"] is None
        elif taken["key"] == "SUPPRESS":
            assert gate["edge_ko"] is None and taken["value_ko"] == "사유 기록 없음"
    assert {"IMMEDIATE", "DIGEST"} <= seen


def test_outcome_lines_have_no_ascii_enum_words(
    seed1: tuple[DaySim, Events], seed7: tuple[DaySim, Events], tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import dataclasses

    class Down(FakeJev):
        def post(self, payload: dict[str, Any], *, timeout_s: float) -> dict[str, Any]:
            raise RuntimeError("jev down")

    kit = _kit(tmp_path / "down")
    kit.judge = JevBackend(transport=Down())
    judge_silent = _run(DaySim(seed=7, days=1, models="cloud", cloud=kit, trace=True))
    cloud = _run(DaySim(seed=7, days=2, models="cloud", cloud=_kit(tmp_path / "ok"), trace=True))
    real = day_sim._TimedGuardrail.evaluate

    def high(self: Any, candidate: Any, ctx: Any = None) -> Gate1Result:
        out = real(self, candidate, ctx)
        if candidate.category not in ("leak", "gas"):
            out = dataclasses.replace(out, floor_tier=RiskTier.DUSK)
            self.recorder.note((candidate.entity_id, "comfort"), gate1=out)
        return out

    monkeypatch.setattr(day_sim._TimedGuardrail, "evaluate", high)
    gate1_silent = _run(DaySim(seed=7, days=1, trace=True))
    ends = set()
    leaked: set[str] = set()
    for events in (seed1[1], seed7[1], cloud, judge_silent, gate1_silent):
        for t in _traces(events):
            leaked |= _ascii_words(t["outcome_ko"])
            if t["kind"] == "silent":
                ends.add(_end(t))
    assert leaked == set()
    assert {"rules", "gate1", "judge"} <= ends
    outcome = next(t["outcome_ko"] for t in _traces(gate1_silent) if t["kind"] == "silent"
                   and _end(t) == "gate1")
    assert outcome.endswith("건너뜀: 하한 땅거미 > 능동 상한 숨결 → 침묵")


def test_feedback_main_edge_names_the_applied_answer() -> None:
    sim = DaySim(seed=7, days=1, trace=True)
    events: Events = []

    def emit(stage: str, data: dict[str, Any]) -> None:
        events.append((stage, data))
        if stage == "proposal" and data["id"] == "p1":
            sim.enqueue_feedback("p1", "dismissed")
            sim.enqueue_feedback("p1", "accepted")  # 거절된다

    asyncio.run(sim.run(emit))
    keys = set()
    for t in _traces(events):
        if t["kind"] != "feedback" or len(t["nodes"]) != 3:
            continue
        phone = _node_of(t, "phone")
        taken = _taken(phone)
        keys.add(taken["key"])
        keys.add(_taken(_node_of(t, "feedback"))["key"])
        # 반영되든 거절되든 폰 → 피드백 본선 문구는 고른 응답 이름이다.
        assert phone["edge_ko"] == taken["label_ko"]
    assert {"dismissed", "accepted", "applied", "refused"} <= keys


def test_first_alert_detail_uses_the_same_rounded_ms_as_the_edge() -> None:
    import datetime
    from types import SimpleNamespace

    sim = DaySim(seed=7, days=1, trace=True)
    out: list[dict[str, Any]] = []
    sim._trace_emit = out.append
    p = SimpleNamespace(proposal_id="pid-1", risk_tier=RiskTier.EMBER, severity=None,
                        judgment_id="j-1", situation_id="s-1")
    transport = SimpleNamespace(sent_at={"pid-1": 10.0026549})  # 2.6549ms — 반올림 경계
    step = SimpleNamespace(at_local=datetime.datetime(2026, 6, 1, 13, 0))
    sim._emit_safety_main(transport, step, day_sim.LEAK, "leak", "누수", "p1", p, 10.0)  # type: ignore[arg-type]
    first = _node_of(out[0], "first_alert")
    ms = f"{first['real_ms']:.1f}ms"
    assert first["detail_ko"] == f"읽기부터 첫 경보 전송까지 {ms} · 모델 없이"
    assert first["edge_ko"].endswith(ms)


def test_jev_answer_equal_to_the_floor_is_not_called_below_it(tmp_path: Path) -> None:
    from liot.core.severity import SeverityLevel

    sim = DaySim(seed=7, days=1, models="cloud", cloud=_kit(tmp_path), trace=True)
    assert sim._kit is not None and sim._kit.judge is not None
    row = {"degraded": False, "model_id": sim._kit.judge.model_id,
           "severity_floor": SeverityLevel.WARNING,
           "model_probs": {"tier": {"ambient": 0.1, "ember": 0.9},
                           "severity": {"record": 0.2, "warning": 0.8}}}
    text = sim._safety_model_text_ko(row, day_sim.LEAK, "13:00", RiskTier.EMBER)
    assert text == "Jev 최댓값 불씨 90% · 파급도 최댓값 경고 80%"
    # 한 칸 낮으면 둘 다 '아래'다(같은 값에서만 빠진다).
    row["model_probs"] = {"tier": {"dusk": 0.9}, "severity": {"notice": 0.8}}
    text = sim._safety_model_text_ko(row, day_sim.LEAK, "13:00", RiskTier.EMBER)
    assert text.count("아래라 반영 안 됨") == 2


def test_exit_flags_are_the_drawn_exits() -> None:
    # 화면은 서버의 exit 표시만 출구로 그린다 — 표마다 고정한다(모아보기는 폰으로 가는 길, 출구 아님).
    flags = {name: {k: x for k, _, x in table} for name, table in {
        "rules": day_sim._RULES_BRANCHES, "gate1": day_sim._GATE1_BRANCHES,
        "judge": day_sim._JUDGE_BRANCHES, "gate": day_sim._GATE_BRANCHES,
        "model": day_sim._MODEL_BRANCHES, "ladder": day_sim._LADDER_BRANCHES,
        "choice": day_sim._CHOICE_BRANCHES, "apply": day_sim._APPLY_BRANCHES}.items()}
    assert {k for k, x in flags["rules"].items() if not x} == {"next"}
    assert {k for k, x in flags["gate"].items() if x} == {"SUPPRESS", "unknown"}
    assert {k for k, x in flags["model"].items() if x} == {"not_sent", "deadline", "error", "unknown"}
    assert {k for k, x in flags["choice"].items() if x} == {"unknown"}
    assert {k for k, x in flags["apply"].items() if x} == {"refused", "unknown"}
    assert {k for k, x in flags["ladder"].items() if x} == {"unknown"}


def test_expired_phone_edge_and_the_rules_exit_value_are_pinned(
    seed1: tuple[DaySim, Events], seed7: tuple[DaySim, Events],
) -> None:
    expired = [t for t in _traces(seed7[1]) if t["kind"] == "feedback" and len(t["nodes"]) == 3
               and _taken(_node_of(t, "phone"))["key"] == "expired"]
    assert expired and all(_node_of(t, "phone")["edge_ko"] == "무시·만료" for t in expired)
    rules = [t for t in _traces(seed1[1]) if t["kind"] == "silent" and _end(t) == "rules"]
    assert rules
    for t in rules:
        taken = _taken(_node_of(t, "rules"))
        assert taken["value_ko"] == "마지막으로 본 규칙의 사유"


def test_clock_text_for_the_tray_review_route() -> None:
    assert day_sim._clock_ko("22:00") == "밤 10시"
    assert day_sim._clock_ko("22:30") == "밤 10시 30분"
    assert day_sim._clock_ko("09:00") == "오전 9시"
    assert day_sim._clock_ko("12:05") == "오후 12시 5분"
    assert day_sim._clock_ko("00:00") == "오전 12시"
    route = day_sim._DIGEST_ROUTE_KO
    assert route == day_sim._clock_ko(day_sim.TRAY_REVIEW_AT) + " 모아보기로"
