"""관제실 라이브 화면 전용 결정 모델 서버(llama.cpp) 관리.

운영 데몬의 서버(18765)와 따로, 고정 포트 18771 에 4B Q8 모델 하나만 띄운다.
명령 인자는 벤치 측정(bench/risk/run_bench.py)과 같다 — 결과 재현의 조건이다.

- 재사용: pid 파일의 프로세스가 살아 있고, 그 명령에 모델 경로와 ``--port 18771`` 이
  있고, ``/health`` 가 200 일 때만. 하나라도 아니면 새로 띄운다.
- 정리: 이 파이썬 프로세스가 띄운 서버만 끈다. 재사용한 서버·남의 프로세스는 건드리지
  않는다.
- 수동 복구: ``kill $(cat ~/models/judge/logs/live-decision.pid)``
"""

from __future__ import annotations

import atexit
import contextlib
import os
import shutil
import signal
import socket
import subprocess
import threading
import time
import urllib.request
from collections.abc import Callable
from pathlib import Path
from types import FrameType
from typing import NamedTuple, Protocol

HOST = "127.0.0.1"
PORT = 18771
# bench/risk/models.yaml 의 키·파일과 같아야 한다(tests/test_decision_server.py 가 맞춰 본다).
MODEL_NAME = "jevk5-4b-q8"
MODEL_FILE = "jevk5-4b-v0.3-Q8_0.gguf"
JUDGE_DIR = Path.home() / "models" / "judge"
MODEL_PATH = JUDGE_DIR / MODEL_FILE
LOG_PATH = JUDGE_DIR / "logs" / "live.log"
PID_PATH = JUDGE_DIR / "logs" / "live-decision.pid"
FALLBACK_BINARY = "/opt/homebrew/bin/llama-server"
LOG_TAIL_LINES = 15


class DecisionServerError(RuntimeError):
    """화면에 그대로 보일 한 줄 한국어 메시지. 로드 실패면 로그 끝부분을 함께 싣는다."""

    def __init__(self, message: str, log_tail: tuple[str, ...] = ()) -> None:
        super().__init__(message)
        self.log_tail = log_tail


class ServerHandle(NamedTuple):
    base_url: str
    model_name: str
    pid: int
    spawned_by_us: bool


class ServerProcess(Protocol):
    @property
    def pid(self) -> int: ...

    def poll(self) -> int | None: ...

    def terminate(self) -> None: ...

    def kill(self) -> None: ...

    def wait(self, timeout: float | None = None) -> int: ...


Launcher = Callable[[list[str], Path], ServerProcess]


def find_binary() -> str | None:
    found = shutil.which("llama-server")
    if found:
        return found
    if os.path.isfile(FALLBACK_BINARY) and os.access(FALLBACK_BINARY, os.X_OK):
        return FALLBACK_BINARY
    return None


def build_command(binary: str, model_path: Path) -> list[str]:
    return [
        binary, "-m", str(model_path), "--host", HOST, "--port", str(PORT),
        "-c", "4096", "-np", "1", "-ngl", "999", "--no-webui",
    ]


def popen_launcher(cmd: list[str], log_path: Path) -> ServerProcess:
    log_path.parent.mkdir(parents=True, exist_ok=True)
    with log_path.open("w", encoding="utf-8") as log:
        return subprocess.Popen(
            cmd, stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT
        )


def http_health(base_url: str) -> bool:
    try:
        with urllib.request.urlopen(base_url + "/health", timeout=2) as r:  # noqa: S310
            return bool(r.status == 200)
    except OSError:  # 연결 거부·로딩 중 503(HTTPError)·시간 초과
        return False


def ps_command(pid: int) -> str | None:
    out = subprocess.run(
        ["ps", "-o", "command=", "-p", str(pid)], capture_output=True, text=True, check=False
    )
    text = out.stdout.strip()
    return text if out.returncode == 0 and text else None


def pid_alive(pid: int) -> bool:
    if pid <= 0:  # 0·음수는 프로세스 그룹 신호가 된다
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def port_in_use(host: str, port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(0.5)
        return s.connect_ex((host, port)) == 0


def command_matches(command: str, model_path: Path) -> bool:
    """명령에 이 모델 경로와 ``--port 18771`` 이 둘 다 있는가(포트는 토큰 단위로 본다)."""
    tokens = command.split()
    port_ok = any(
        tok == "--port" and i + 1 < len(tokens) and tokens[i + 1] == str(PORT)
        for i, tok in enumerate(tokens)
    )
    return port_ok and str(model_path) in command


class DecisionServerManager:
    """전용 결정 서버 하나를 띄우고·재사용하고·정리한다. 스레드 안전."""

    def __init__(
        self,
        *,
        model_path: Path = MODEL_PATH,
        log_path: Path = LOG_PATH,
        pid_path: Path = PID_PATH,
        binary_finder: Callable[[], str | None] = find_binary,
        launcher: Launcher = popen_launcher,
        health_probe: Callable[[str], bool] = http_health,
        ps_reader: Callable[[int], str | None] = ps_command,
        alive: Callable[[int], bool] = pid_alive,
        port_busy: Callable[[str, int], bool] = port_in_use,
        sleep: Callable[[float], None] = time.sleep,
        monotonic: Callable[[], float] = time.monotonic,
        poll_interval_s: float = 0.5,
        stop_wait_s: float = 10.0,
        stop_lock_wait_s: float = 2.0,
    ) -> None:
        self.model_path = model_path
        self.log_path = log_path
        self.pid_path = pid_path
        self.base_url = f"http://{HOST}:{PORT}"
        self.model_name = MODEL_NAME
        self._binary_finder = binary_finder
        self._launcher = launcher
        self._health = health_probe
        self._ps = ps_reader
        self._alive = alive
        self._port_busy = port_busy
        self._sleep = sleep
        self._monotonic = monotonic
        self._poll_interval_s = poll_interval_s
        self._stop_wait_s = stop_wait_s
        self._stop_lock_wait_s = stop_lock_wait_s
        # 재진입 락: 신호 처리기는 락을 쥔 메인 스레드 위에서도 돌 수 있다.
        self._lock = threading.RLock()
        # 띄우기 구간(launcher 호출 → _proc 기록) 전용 짧은 락. 준비 대기로 큰 락을 오래 쥔
        # 스레드가 있어도, 락 없이 끄는 쪽이 '방금 띄웠지만 아직 기록 안 된' 프로세스를
        # 놓치지 않게 이 구간만은 기다린다(놓치면 서버가 고아로 남는다). 재진입 락인 이유는
        # 큰 락과 같다 — 띄우기 구간 안의 메인 스레드 위에서 신호 처리기가 돌면 교착이 난다.
        self._launch_lock = threading.RLock()
        # 큰 락을 못 잡은 채 끄기가 왔는데 아직 띄우기 전이면 세운다 — 그 띄우기를 취소한다.
        self._cancel_spawn = False
        self._proc: ServerProcess | None = None
        self._cleanup_installed = False

    # ── 띄우기·재사용 ──────────────────────────────────────────────────────────

    def ensure_running(self, timeout_s: float = 180.0) -> ServerHandle:
        with self._lock:
            # 앞선 호출 때 세워진 취소 표식은 그 호출 몫이었다 — 새 호출은 깨끗하게 시작한다.
            with self._launch_lock:
                self._cancel_spawn = False
            if self._proc is not None and self._proc.poll() is not None:
                self._proc = None  # 우리가 띄운 서버가 그새 죽었다
            reused = self._reusable_pid()
            if reused is not None:
                ours = self._proc is not None and self._proc.pid == reused
                return ServerHandle(self.base_url, self.model_name, reused, ours)
            return self._spawn(timeout_s)

    def _reusable_pid(self) -> int | None:
        pid = self._read_pid()
        if pid is None or not self._alive(pid):
            return None
        command = self._ps(pid)
        if command is None or not command_matches(command, self.model_path):
            return None
        return pid if self._health(self.base_url) else None

    def _spawn(self, timeout_s: float) -> ServerHandle:
        binary = self._binary_finder()
        if binary is None:
            raise DecisionServerError(
                "llama-server 실행 파일을 찾지 못했습니다 — brew install llama.cpp 로 설치하세요."
            )
        if not self.model_path.is_file():
            raise DecisionServerError(f"결정 모델 파일이 없습니다: {self.model_path}")
        if self._port_busy(HOST, PORT):
            # 재사용 조건을 못 맞춘 무언가가 포트를 쥐고 있다. 남의 프로세스는 끄지 않고,
            # 그쪽 pid 파일도 덮어쓰지 않는다.
            raise DecisionServerError(
                f"포트 {PORT} 를 다른 프로세스가 쓰고 있습니다 — 확인 후 다시 시도하세요."
            )
        with self._launch_lock:
            if self._cancel_spawn:
                self._cancel_spawn = False
                raise DecisionServerError("종료 중이라 결정 모델 서버를 띄우지 않았습니다.")
            proc = self._launcher(build_command(binary, self.model_path), self.log_path)
            self._proc = proc
            self._write_pid(proc.pid)
        t0 = self._monotonic()
        while True:
            rc = proc.poll()
            if rc is not None:
                self._forget(proc)
                raise DecisionServerError(
                    f"결정 모델 서버가 준비 전에 종료됐습니다(rc={rc}).", self._log_tail()
                )
            if self._health(self.base_url):
                return ServerHandle(self.base_url, self.model_name, proc.pid, True)
            if self._monotonic() - t0 >= timeout_s:
                self._terminate(proc)
                self._forget(proc)
                raise DecisionServerError(
                    f"결정 모델 서버 준비 시간 초과({timeout_s:g}초).", self._log_tail()
                )
            self._sleep(self._poll_interval_s)

    # ── 정리 ───────────────────────────────────────────────────────────────────

    def stop_if_ours(self) -> bool:
        """이 프로세스가 띄운 서버만 끈다. 끈 게 있으면 True."""
        # 다른 스레드가 준비 대기로 락을 오래 쥐고 있어도 종료는 막히면 안 된다.
        # 못 잡으면 큰 락 없이 진행한다 — 대기 중인 쪽은 poll() 로 종료를 보고 빠진다.
        # 띄우기 구간(_launch_lock)만은 기다린다: 그 안에서는 프로세스가 이미 떴는데
        # _proc 가 아직 비어 있을 수 있다. 아직 띄우기 전이면 그 띄우기를 취소한다.
        got = self._lock.acquire(timeout=self._stop_lock_wait_s)
        try:
            with self._launch_lock:
                proc = self._proc
                if proc is None:
                    if not got:
                        self._cancel_spawn = True
                    return False
            self._terminate(proc)
            self._forget(proc)
            return True
        finally:
            if got:
                self._lock.release()

    def _terminate(self, proc: ServerProcess) -> None:
        if proc.poll() is not None:
            return
        proc.terminate()
        try:
            proc.wait(timeout=self._stop_wait_s)
        except subprocess.TimeoutExpired:
            proc.kill()
            with contextlib.suppress(subprocess.TimeoutExpired):
                proc.wait(timeout=self._stop_wait_s)

    def _forget(self, proc: ServerProcess) -> None:
        if self._proc is proc:
            self._proc = None
        # pid 파일은 우리 pid 를 가리킬 때만 지운다(그새 다른 관리자가 썼을 수 있다).
        if self._read_pid() == proc.pid:
            with contextlib.suppress(FileNotFoundError):
                self.pid_path.unlink()

    def install_cleanup(self) -> None:
        """atexit 와 SIGTERM·SIGINT 에 정리를 건다. 여러 번 불러도 한 번만 건다."""
        with self._lock:
            if self._cleanup_installed:
                return
            self._cleanup_installed = True
        atexit.register(self.stop_if_ours)
        for signum in (signal.SIGTERM, signal.SIGINT):
            try:
                previous = signal.getsignal(signum)
                signal.signal(signum, self._chained_handler(previous))
            except ValueError:  # 메인 스레드가 아니면 신호 처리기를 못 건다 — atexit 만 남는다
                pass

    def _chained_handler(
        self, previous: Callable[[int, FrameType | None], object] | int | None
    ) -> Callable[[int, FrameType | None], None]:
        def handler(signum: int, frame: FrameType | None) -> None:
            self.stop_if_ours()
            if callable(previous):
                # SIGINT 의 기본 파이썬 처리기(default_int_handler)는 여기서
                # KeyboardInterrupt 를 낸다 — 기존 동작이 그대로 남는다.
                previous(signum, frame)
                return
            if previous == signal.SIG_IGN:
                return
            # SIG_DFL(또는 C 쪽 처리기): 기본 동작으로 되돌려 같은 신호를 다시 받는다.
            signal.signal(signum, signal.SIG_DFL)
            os.kill(os.getpid(), signum)

        return handler

    # ── pid 파일·로그 ──────────────────────────────────────────────────────────

    def _read_pid(self) -> int | None:
        try:
            return int(self.pid_path.read_text(encoding="utf-8").strip())
        except (OSError, ValueError):
            return None

    def _write_pid(self, pid: int) -> None:
        self.pid_path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.pid_path.with_name(self.pid_path.name + ".tmp")
        tmp.write_text(f"{pid}\n", encoding="utf-8")
        os.replace(tmp, self.pid_path)

    def _log_tail(self) -> tuple[str, ...]:
        try:
            text = self.log_path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            return ()
        return tuple(text.splitlines()[-LOG_TAIL_LINES:])

