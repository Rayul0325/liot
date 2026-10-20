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


def test_safety_main_follows_the_first_send(seed7: tuple[DaySim, Events]) -> None:
    sim, _ = seed7
    # 첫 경보 전송이 handle_reading 안에서 끝난 뒤에야 trace 가 나간다 — 전송 기록에 그 제안이 있다.
    for alert in (s for s in sim.safety_events if s["kind"] == "first_alert"):
        assert ("send", sim._tracked[alert["id"]].pid) in sim.log


def test_followup_nodes_and_phase2_time(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[tuple[int, str, bool, float]] = []
    real = _TraceRecorder.judge_call

    def spy(self: _TraceRecorder, step_no: int, entity_id: str, phase2: bool,
            seconds: float) -> None:
        calls.append((step_no, entity_id, phase2, seconds))
        real(self, step_no, entity_id, phase2, seconds)

    monkeypatch.setattr(_TraceRecorder, "judge_call", spy)
    sim = DaySim(seed=7, days=3, trace=True)
    events = _run(sim)
    follow = [t for t in _traces(events) if t["kind"] == "safety" and t["part"] == "followup"]
    assert len(follow) == 3
    for t in follow:
        assert [n["id"] for n in t["nodes"]] == FOLLOWUP
        model = t["nodes"][0]
        phase2 = [round(s * 1000.0, 2) for _, _, p2, s in calls if p2]
        assert model["real_ms"] in phase2 and model["model"] == "fake-llm-v1"
        assert t["nodes"][1]["detail_ko"].startswith("파급도 ")
        assert t["nodes"][3]["detail_ko"].startswith("재알림 예약")


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
