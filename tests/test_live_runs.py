"""일반 실행 관리자(liot/live_runs.py) — 가짜 작업자로 보관·다시 보내기·409·멈추기·보관 개수.

핵심 검증:
  - 끝난 뒤 늦게 붙어도 사건 전부를 받고, 마지막은 done 이며, 수가 event_count 와 같다.
  - from=N 으로 다시 붙으면 빠짐도 중복도 없다. Last-Event-ID 는 그 다음부터다.
  - 진행 중(또는 준비 중) 새 실행은 409 다.
  - 멈추기: stopping → stopped, done.stopped 가 참이고 다음 단위는 돌지 않는다.
  - 최근 3개만 남는다.
  - 작업자 예외는 error → done 으로 끝나고, 사건이 없는 동안 keepalive 가 간다.
"""

from __future__ import annotations

import json
import threading
from typing import Any

import pytest

from liot.live_runs import (
    LiveRequestError,
    LiveRun,
    LiveRuns,
    RunContext,
    Worker,
    resume_from,
    sse_message,
)


def _counting(n: int) -> Worker:
    def worker(ctx: RunContext) -> None:
        for i in range(n):
            if ctx.stop_requested:
                return
            ctx.emit("step", {"i": i})
        ctx.update_summary(steps=n)

    return worker


def _gated(gate: threading.Event, started: threading.Event, n: int = 5) -> Worker:
    """한 단위를 내보낸 뒤 gate 가 열릴 때까지 멈춰 있는 작업자(진행 중 상태를 만든다)."""

    def worker(ctx: RunContext) -> None:
        for i in range(n):
            if ctx.stop_requested:
                return
            ctx.emit("step", {"i": i})
            started.set()
            assert gate.wait(5.0)

    return worker


def _parse(stream: bytes) -> list[dict[str, Any]]:
    """SSE 바이트 → 사건 목록(주석 줄은 버린다). id·event 줄이 data 와 맞는지도 본다."""
    out: list[dict[str, Any]] = []
    for block in stream.decode("utf-8").split("\n\n"):
        lines = [ln for ln in block.split("\n") if ln and not ln.startswith(":")]
        if not lines:
            continue
        fields = dict(ln.split(": ", 1) for ln in lines)
        ev = json.loads(fields["data"])
        assert int(fields["id"]) == ev["seq"]
        assert fields["event"] == ev["stage"]
        out.append(ev)
    return out


def _collect(runs: LiveRuns, run: LiveRun, from_seq: int = 1) -> tuple[list[dict[str, Any]], int]:
    chunks: list[bytes] = []
    sent = runs.follow(run, from_seq, chunks.append)
    return _parse(b"".join(chunks)), sent


def _finished(runs: LiveRuns, make: Worker) -> LiveRun:
    run = runs.start(lambda: make)
    runs.join(5.0)
    assert run.finished
    return run


def test_late_attach_gets_every_event_then_done() -> None:
    runs = LiveRuns()
    run = _finished(runs, _counting(4))
    events, sent = _collect(runs, run)
    assert [e["stage"] for e in events] == ["step"] * 4 + ["done"]
    assert [e["seq"] for e in events] == [1, 2, 3, 4, 5]
    assert {e["run"] for e in events} == {run.run_id}
    assert sent == len(events) == run.view()["event_count"] == 5
    assert events[-1]["stopped"] is False
    assert events[-1]["summary"] == {"steps": 4}
    assert run.view()["status"] == "done"


@pytest.mark.parametrize("n", [1, 3, 5])
def test_reconnect_from_n_has_no_gap_and_no_duplicate(n: int) -> None:
    runs = LiveRuns()
    run = _finished(runs, _counting(4))
    first, _ = _collect(runs, run)
    head = first[: n - 1]
    tail, _ = _collect(runs, run, from_seq=n)
    assert [e["seq"] for e in head + tail] == [e["seq"] for e in first]
    assert tail[0]["seq"] == n


def test_reconnect_during_live_run_has_no_gap() -> None:
    runs = LiveRuns()
    gate, started = threading.Event(), threading.Event()
    run = runs.start(lambda: _gated(gate, started, n=3))
    assert started.wait(5.0)
    chunks: list[bytes] = []
    # 이미 쌓인 1건을 받은 뒤, seq 2 부터 다시 붙어 끝까지 받는다.
    first = _parse(sse_message(run.events[0]))
    t = threading.Thread(target=lambda: runs.follow(run, 2, chunks.append))
    t.start()
    gate.set()
    t.join(5.0)
    rest = _parse(b"".join(chunks))
    assert [e["seq"] for e in first + rest] == [1, 2, 3, 4]
    assert rest[-1]["stage"] == "done"


def test_resume_from_prefers_last_event_id() -> None:
    assert resume_from(None, None) == 1
    assert resume_from("3", None) == 3
    assert resume_from("0", None) == 1
    assert resume_from("3", "7") == 8
    with pytest.raises(LiveRequestError) as bad:
        resume_from("x", None)
    assert bad.value.status == 400
    with pytest.raises(LiveRequestError):
        resume_from(None, "-1")


def test_second_run_while_active_is_409_and_allowed_after() -> None:
    runs = LiveRuns()
    gate, started = threading.Event(), threading.Event()
    runs.start(lambda: _gated(gate, started))
    assert started.wait(5.0)
    with pytest.raises(LiveRequestError) as busy:
        runs.start(lambda: _counting(1))
    assert busy.value.status == 409
    gate.set()
    runs.join(5.0)
    assert _finished(runs, _counting(1)).status == "done"


def test_run_is_busy_while_worker_factory_prepares() -> None:
    runs = LiveRuns()
    entered, release = threading.Event(), threading.Event()
    errors: list[int] = []

    def slow_factory() -> Worker:
        entered.set()
        assert release.wait(5.0)
        return _counting(1)

    t = threading.Thread(target=lambda: runs.start(slow_factory))
    t.start()
    assert entered.wait(5.0)
    try:
        runs.start(lambda: _counting(1))
    except LiveRequestError as exc:
        errors.append(exc.status)
    release.set()
    t.join(5.0)
    runs.join(5.0)
    assert errors == [409]


def test_factory_failure_creates_no_run_and_frees_the_slot() -> None:
    runs = LiveRuns()

    def broken() -> Worker:
        raise LiveRequestError("모델 서버를 띄우지 못했습니다.", 503)

    with pytest.raises(LiveRequestError) as exc:
        runs.start(broken)
    assert exc.value.status == 503
    assert runs.run_ids() == []
    assert _finished(runs, _counting(1)).status == "done"


def test_stop_finishes_current_unit_then_stops() -> None:
    runs = LiveRuns()
    gate, started = threading.Event(), threading.Event()
    run = runs.start(lambda: _gated(gate, started, n=5))
    assert started.wait(5.0)
    assert runs.stop() is run
    assert run.view()["status"] == "stopping"
    gate.set()
    runs.join(5.0)
    events, _ = _collect(runs, run)
    assert [e["stage"] for e in events] == ["step", "done"]  # 다음 단위는 돌지 않았다
    assert events[-1]["stopped"] is True
    assert run.view()["status"] == "stopped"
    with pytest.raises(LiveRequestError) as none_left:
        runs.stop()
    assert none_left.value.status == 409


def test_stop_without_any_run_is_409() -> None:
    with pytest.raises(LiveRequestError) as exc:
        LiveRuns().stop()
    assert exc.value.status == 409


def test_keeps_only_last_three_runs() -> None:
    runs = LiveRuns()
    ids = [_finished(runs, _counting(1)).run_id for _ in range(5)]
    assert runs.run_ids() == ids[-3:]
    assert runs.get(ids[0]) is None
    assert runs.get(ids[-1]) is not None


def test_worker_exception_ends_with_error_then_done() -> None:
    def boom(ctx: RunContext) -> None:
        ctx.emit("step", {"i": 0})
        raise RuntimeError("터짐")

    runs = LiveRuns()
    run = _finished(runs, boom)
    events, _ = _collect(runs, run)
    assert [e["stage"] for e in events] == ["step", "error", "done"]
    assert events[1]["error"] == "RuntimeError: 터짐"
    assert events[-1]["failed"] is True
    assert run.view()["status"] == "failed"


def test_keepalive_is_sent_while_waiting_for_events() -> None:
    runs = LiveRuns(keepalive_s=0.01)
    gate, started = threading.Event(), threading.Event()
    run = runs.start(lambda: _gated(gate, started, n=1))
    assert started.wait(5.0)
    chunks: list[bytes] = []

    def write(b: bytes) -> None:
        chunks.append(b)
        if b == b": keepalive\n\n":
            gate.set()

    runs.follow(run, 2, write)
    runs.join(5.0)
    assert b": keepalive\n\n" in chunks
    assert [e["stage"] for e in _parse(b"".join(chunks))] == ["done"]


def test_broken_writer_ends_only_the_stream() -> None:
    runs = LiveRuns()
    gate, started = threading.Event(), threading.Event()
    run = runs.start(lambda: _gated(gate, started, n=3))
    assert started.wait(5.0)

    def broken(_: bytes) -> None:
        raise BrokenPipeError

    with pytest.raises(BrokenPipeError):
        runs.follow(run, 1, broken)
    gate.set()
    runs.join(5.0)
    assert run.view()["status"] == "done"
    assert run.view()["event_count"] == 4


def test_finished_run_rejects_new_events() -> None:
    run = _finished(LiveRuns(), _counting(0))
    with pytest.raises(RuntimeError):
        run.append("step", {})
