"""실행 관리자(일반) — 백그라운드 작업 하나를 돌리며 사건을 보관하고 실시간으로 다시 보낸다.

관제실 서버의 실행형 화면(하루 시뮬레이션 등)이 같이 쓴다. 무엇을 돌릴지는 모른다 —
작업자(worker)는 ``RunContext`` 하나를 받는 함수이고, 사건을 ``ctx.emit`` 으로 내보내며
``ctx.stop_requested`` 를 보고 멈춘다.

- 실행은 한 번에 하나다. 진행 중이거나 준비 중(작업자 공장이 도는 중)이면 새 실행은 409.
- 사건마다 ``run``·``seq``(1부터 단조 증가)를 붙여 실행별 목록에 쌓고 Condition 으로
  스트림 대기자를 깨운다.
- 다시 보내기: ``follow(run, from_seq, write)`` 가 seq >= from_seq 보관분을 먼저 보내고 새 사건을
  ``done`` 까지 이어 보낸다. 늦게 붙어도, 다시 붙어도 빠짐·중복이 없다. 사건이 없으면
  keepalive 주석을 보낸다.
- 멈추기: 표식만 세운다. 작업자가 다음 단위 앞에서 보고 멈춘다(지금 단위는 끝까지 간다).
- 작업자가 예외로 끝나면 ``error`` 사건을 남긴다. 끝에는 늘 ``done`` 사건이 하나 붙는다.
- 최근 실행 3개만 메모리에 둔다.

관제실 세션 상태(STATE_LOCK)는 여기서 쓰지 않는다. 소켓 쓰기는 Condition 밖에서 한다 —
느린 수신자가 작업자의 emit 을 막지 못하게.
"""

from __future__ import annotations

import itertools
import json
import threading
import uuid
from collections import OrderedDict
from collections.abc import Callable
from typing import Any

__all__ = [
    "KEEP_RUNS",
    "LiveRequestError",
    "LiveRun",
    "LiveRuns",
    "RunContext",
    "Worker",
    "resume_from",
    "sse_message",
]

KEEP_RUNS = 3
DEFAULT_KEEPALIVE_S = 15.0  # 관제실 소켓 제한(30초)보다 짧게


class LiveRequestError(ValueError):
    """실행 요청을 받을 수 없다. ``message`` 는 화면에 그대로 보일 한국어 한 줄."""

    def __init__(self, message: str, status: int = 400, *, field: str | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.status = status
        self.field = field


class LiveRun:
    """실행 하나의 사건 목록·상태·요약. 모든 변경은 ``cond`` 아래에서 한다."""

    def __init__(self, run_id: str, meta: dict[str, Any] | None = None) -> None:
        self.run_id = run_id
        self.meta = dict(meta or {})
        self.cond = threading.Condition()
        self.events: list[dict[str, Any]] = []
        self.status = "running"  # running → stopping → done | stopped | failed
        self.stop_requested = False
        self.summary: dict[str, Any] = {}
        self.error: str | None = None
        self._seq = itertools.count(1)

    @property
    def finished(self) -> bool:
        return self.status in ("done", "stopped", "failed")

    def append(self, stage: str, data: dict[str, Any]) -> dict[str, Any]:
        """사건 하나에 run·seq 를 붙여 쌓고 대기자를 깨운다. 쌓인 사건을 돌려준다."""
        with self.cond:
            if self.finished:
                raise RuntimeError("끝난 실행에는 사건을 더할 수 없다")
            ev = {**data, "run": self.run_id, "seq": next(self._seq), "stage": stage}
            self.events.append(ev)
            self.cond.notify_all()
            return ev

    def finish(self, error: BaseException | None = None) -> None:
        """끝 상태를 정하고 ``done`` 사건을 붙인다(예외로 끝났으면 그 앞에 ``error``)."""
        with self.cond:
            if error is not None:
                self.error = f"{type(error).__name__}: {error}"
                self.append("error", {"error": self.error})
            done = {
                "stopped": self.stop_requested and error is None,
                "failed": error is not None,
                "summary": dict(self.summary),
            }
            self.append("done", done)
            if error is not None:
                self.status = "failed"
            else:
                self.status = "stopped" if self.stop_requested else "done"

    def view(self) -> dict[str, Any]:
        with self.cond:
            return {
                "ok": True,
                "run_id": self.run_id,
                "status": self.status,
                "event_count": len(self.events),
                "meta": dict(self.meta),
                "summary": dict(self.summary),
                "error": self.error,
            }


class RunContext:
    """작업자에게 넘기는 손잡이: 사건 내보내기·멈춤 표식·요약 갱신."""

    def __init__(self, run: LiveRun) -> None:
        self._run = run

    @property
    def run_id(self) -> str:
        return self._run.run_id

    def emit(self, stage: str, data: dict[str, Any] | None = None) -> dict[str, Any]:
        """사건 하나를 내보낸다. ``data`` 는 JSON 값만 담는 작은 dict."""
        return self._run.append(stage, dict(data or {}))

    @property
    def stop_requested(self) -> bool:
        with self._run.cond:
            return self._run.stop_requested

    def update_summary(self, **fields: Any) -> None:
        """``runs/<id>`` 와 ``done`` 에 실릴 요약을 덮어쓴다(서버가 계산한 집계)."""
        with self._run.cond:
            self._run.summary.update(fields)


Worker = Callable[[RunContext], None]


class LiveRuns:
    """실행 관리자. 관제실 서버 하나에 하나(화면 종류마다 하나씩 둬도 된다)."""

    def __init__(self, *, keep: int = KEEP_RUNS, keepalive_s: float = DEFAULT_KEEPALIVE_S) -> None:
        self.keep = keep
        self.keepalive_s = keepalive_s
        self._lock = threading.Lock()
        self._runs: OrderedDict[str, LiveRun] = OrderedDict()
        self._active: LiveRun | None = None
        self._starting = False
        self._threads: list[threading.Thread] = []

    def get(self, run_id: str) -> LiveRun | None:
        with self._lock:
            return self._runs.get(run_id)

    def run_ids(self) -> list[str]:
        with self._lock:
            return list(self._runs)

    def _busy(self) -> bool:
        return self._starting or (self._active is not None and not self._active.finished)

    def start(
        self, make_worker: Callable[[], Worker], *, meta: dict[str, Any] | None = None
    ) -> LiveRun:
        """새 실행을 시작한다. 진행 중이거나 준비 중이면 ``LiveRequestError``(409).

        ``make_worker`` 는 락 밖에서 부른다(모델 서버 띄우기처럼 오래 걸릴 수 있다). 그 안의
        예외는 그대로 올라가고 실행은 만들어지지 않는다.
        """
        with self._lock:
            if self._busy():
                raise LiveRequestError("이미 실행 중입니다 — 끝나거나 멈춘 뒤 다시 시작하세요.", 409)
            self._starting = True
        try:
            worker = make_worker()
            run = LiveRun(uuid.uuid4().hex[:12], meta)
            thread = threading.Thread(
                target=self._work, args=(run, worker), name=f"live-run-{run.run_id}", daemon=True
            )
            with self._lock:
                self._runs[run.run_id] = run
                self._active = run
                while len(self._runs) > self.keep:
                    self._runs.popitem(last=False)
                self._threads = [t for t in self._threads if t.is_alive()] + [thread]
        finally:
            with self._lock:
                self._starting = False
        thread.start()
        return run

    def stop(self) -> LiveRun:
        """진행 중 실행에 멈춤 표식을 세운다. 없으면 ``LiveRequestError``(409)."""
        with self._lock:
            run = self._active
        if run is None:
            raise LiveRequestError("진행 중인 실행이 없습니다.", 409)
        with run.cond:
            if run.finished:
                raise LiveRequestError("진행 중인 실행이 없습니다.", 409)
            run.stop_requested = True
            run.status = "stopping"
        return run

    def join(self, timeout: float | None = None) -> None:
        """실행 스레드가 끝나기를 기다린다(시험·종료용)."""
        for t in list(self._threads):
            t.join(timeout)

    @staticmethod
    def _work(run: LiveRun, worker: Worker) -> None:
        try:
            worker(RunContext(run))
        except Exception as exc:  # noqa: BLE001 — 작업자 실패는 error·done 사건으로 남긴다
            run.finish(exc)
            return
        run.finish()

    def follow(self, run: LiveRun, from_seq: int, write: Callable[[bytes], None]) -> int:
        """seq >= from_seq 사건을 보관분부터 보내고 ``done`` 까지 이어 보낸다. 보낸 사건 수.

        쓰기 오류(끊김)는 호출자에게 그대로 올라간다 — 실행은 영향을 받지 않는다.
        """
        idx = max(0, from_seq - 1)
        sent = 0
        while True:
            with run.cond:
                if len(run.events) <= idx and not run.finished:
                    run.cond.wait(self.keepalive_s)
                batch = run.events[idx:]
                finished = run.finished
            if not batch:
                if finished:
                    return sent  # from_seq 가 done 뒤를 가리켰다
                write(b": keepalive\n\n")
                continue
            idx += len(batch)
            write(b"".join(sse_message(ev) for ev in batch))
            sent += len(batch)
            if batch[-1]["stage"] == "done":
                return sent


def sse_message(ev: dict[str, Any]) -> bytes:
    """사건 하나 → ``id: <seq>\\nevent: <stage>\\ndata: <json>\\n\\n``."""
    data = json.dumps(ev, ensure_ascii=False, separators=(",", ":"))
    return f"id: {ev['seq']}\nevent: {ev['stage']}\ndata: {data}\n\n".encode()


def resume_from(from_param: str | None, last_event_id: str | None) -> int:
    """스트림 시작 seq. ``Last-Event-ID``(브라우저 자동 재접속)가 있으면 그 다음부터,
    없으면 ``from`` 값(기본 1). 숫자가 아니거나 음수면 ``LiveRequestError``(400)."""
    if last_event_id is not None and last_event_id.strip():
        return _nonneg(last_event_id, "Last-Event-ID") + 1
    if from_param is None or not from_param.strip():
        return 1
    return max(1, _nonneg(from_param, "from"))


def _nonneg(raw: str, name: str) -> int:
    try:
        value = int(raw.strip())
    except ValueError:
        raise LiveRequestError(f"{name} 는 0 이상의 정수여야 합니다.", field=name) from None
    if value < 0:
        raise LiveRequestError(f"{name} 는 0 이상의 정수여야 합니다.", field=name)
    return value
