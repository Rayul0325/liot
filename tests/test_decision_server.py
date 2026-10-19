"""전용 결정 모델 서버 관리자 — 가짜 프로세스·가짜 health 로만 본다(llama-server 를 띄우지 않는다)."""

from __future__ import annotations

import atexit
import signal
import subprocess
from pathlib import Path

import pytest
import yaml

from liot import decision_server as ds
from liot.decision_server import DecisionServerError, DecisionServerManager

ROOT = Path(__file__).resolve().parents[1]


class FakeProc:
    def __init__(self, pid: int, *, rc: int | None = None, ignores_term: bool = False) -> None:
        self.pid = pid
        self.rc = rc
        self.ignores_term = ignores_term
        self.terminated = 0
        self.killed = 0

    def poll(self) -> int | None:
        return self.rc

    def terminate(self) -> None:
        self.terminated += 1
        if not self.ignores_term:
            self.rc = -15

    def kill(self) -> None:
        self.killed += 1
        self.rc = -9

    def wait(self, timeout: float | None = None) -> int:
        if self.rc is None:
            raise subprocess.TimeoutExpired("llama-server", timeout or 0)
        return self.rc


class World:
    """관리자에 주입할 가짜 OS: 살아 있는 pid·ps 명령·health·실행 기록."""

    def __init__(self, tmp_path: Path) -> None:
        self.model = tmp_path / ds.MODEL_FILE
        self.model.write_bytes(b"gguf")
        self.log = tmp_path / "logs" / "live.log"
        self.pidfile = tmp_path / "logs" / "live-decision.pid"
        self.alive: set[int] = set()
        self.commands: dict[int, str] = {}
        self.healthy = True
        self.port_busy = False
        self.launched: list[list[str]] = []
        self.next_proc: FakeProc | None = None
        self.log_text = ""
        self.binary: str | None = "/opt/homebrew/bin/llama-server"

    def launch(self, cmd: list[str], log_path: Path) -> FakeProc:
        self.launched.append(cmd)
        log_path.parent.mkdir(parents=True, exist_ok=True)
        log_path.write_text(self.log_text, encoding="utf-8")
        proc = self.next_proc or FakeProc(5000 + len(self.launched))
        self.alive.add(proc.pid)
        self.commands[proc.pid] = " ".join(cmd)
        return proc

    def manager(self) -> DecisionServerManager:
        return DecisionServerManager(
            model_path=self.model,
            log_path=self.log,
            pid_path=self.pidfile,
            binary_finder=lambda: self.binary,
            launcher=self.launch,
            health_probe=lambda _url: self.healthy,
            ps_reader=lambda pid: self.commands.get(pid),
            alive=lambda pid: pid in self.alive,
            port_busy=lambda _h, _p: self.port_busy,
            sleep=lambda _s: None,
            stop_wait_s=0.01,
        )

    def existing(self, pid: int, command: str) -> None:
        self.pidfile.parent.mkdir(parents=True, exist_ok=True)
        self.pidfile.write_text(f"{pid}\n", encoding="utf-8")
        self.alive.add(pid)
        self.commands[pid] = command

    def good_command(self) -> str:
        return " ".join(ds.build_command("/opt/homebrew/bin/llama-server", self.model))


@pytest.fixture
def world(tmp_path: Path) -> World:
    return World(tmp_path)


def test_model_identity_matches_bench_models_yaml():
    doc = yaml.safe_load((ROOT / "bench" / "risk" / "models.yaml").read_text(encoding="utf-8"))
    files = {m["key"]: m["file"] for m in doc["models"]}
    assert files[ds.MODEL_NAME] == ds.MODEL_FILE
    assert ds.MODEL_PATH.name == ds.MODEL_FILE


def test_command_matches_benchmark_flags_and_never_production_port(world: World):
    cmd = ds.build_command("llama-server", world.model)
    assert cmd == [
        "llama-server", "-m", str(world.model), "--host", "127.0.0.1", "--port", "18771",
        "-c", "4096", "-np", "1", "-ngl", "999", "--no-webui",
    ]
    assert "18765" not in " ".join(cmd)
    assert ds.PORT == 18771
    assert "18765" not in DecisionServerManager().base_url


def test_reuses_only_when_all_three_conditions_hold(world: World):
    world.existing(4242, world.good_command())
    handle = world.manager().ensure_running()
    assert handle == ("http://127.0.0.1:18771", "jevk5-4b-q8", 4242, False)
    assert world.launched == []


def test_alive_pid_without_model_path_spawns_new(world: World):
    world.existing(4242, "llama-server -m /other/model.gguf --port 18771")
    handle = world.manager().ensure_running()
    assert handle.spawned_by_us and handle.pid != 4242
    assert len(world.launched) == 1
    assert world.pidfile.read_text().strip() == str(handle.pid)


def test_alive_pid_with_other_port_spawns_new(world: World):
    world.existing(4242, world.good_command().replace("18771", "187710"))
    handle = world.manager().ensure_running()
    assert handle.spawned_by_us and len(world.launched) == 1


def test_dead_pid_spawns_and_overwrites_pidfile(world: World):
    world.existing(4242, world.good_command())
    world.alive.discard(4242)
    handle = world.manager().ensure_running()
    assert handle.spawned_by_us
    assert world.pidfile.read_text().strip() == str(handle.pid)


def test_unhealthy_reuse_candidate_on_busy_port_is_left_alone(world: World):
    world.existing(4242, world.good_command())
    world.healthy = False
    world.port_busy = True
    with pytest.raises(DecisionServerError, match="18771"):
        world.manager().ensure_running()
    assert world.launched == []
    assert world.pidfile.read_text().strip() == "4242"


def test_garbage_pidfile_spawns(world: World):
    world.pidfile.parent.mkdir(parents=True)
    world.pidfile.write_text("not-a-pid")
    assert world.manager().ensure_running().spawned_by_us


def test_stop_if_ours_does_not_touch_reused_process(world: World):
    world.existing(4242, world.good_command())
    mgr = world.manager()
    mgr.ensure_running()
    assert mgr.stop_if_ours() is False
    assert world.pidfile.read_text().strip() == "4242"


def test_stop_if_ours_terminates_spawned_and_removes_pidfile(world: World):
    proc = FakeProc(7001)
    world.next_proc = proc
    mgr = world.manager()
    assert mgr.ensure_running().pid == 7001
    assert mgr.stop_if_ours() is True
    assert (proc.terminated, proc.killed) == (1, 0)
    assert not world.pidfile.exists()
    assert mgr.stop_if_ours() is False  # 두 번째는 할 일 없음


def test_stop_escalates_to_sigkill(world: World):
    proc = FakeProc(7002, ignores_term=True)
    world.next_proc = proc
    mgr = world.manager()
    mgr.ensure_running()
    mgr.stop_if_ours()
    assert (proc.terminated, proc.killed) == (1, 1)


def test_second_ensure_reports_our_own_server_as_ours(world: World):
    mgr = world.manager()
    first = mgr.ensure_running()
    second = mgr.ensure_running()
    assert second == first and second.spawned_by_us
    assert len(world.launched) == 1


def test_missing_binary_gives_korean_one_line_error(world: World):
    world.binary = None
    with pytest.raises(DecisionServerError) as exc:
        world.manager().ensure_running()
    msg = str(exc.value)
    assert "llama-server" in msg and "찾지 못했습니다" in msg and "\n" not in msg
    assert world.launched == []


def test_missing_model_file_gives_korean_one_line_error(world: World):
    world.model.unlink()
    with pytest.raises(DecisionServerError) as exc:
        world.manager().ensure_running()
    assert "모델 파일이 없습니다" in str(exc.value) and "\n" not in str(exc.value)


def test_early_exit_surfaces_log_tail(world: World):
    world.healthy = False
    world.next_proc = FakeProc(7003, rc=1)
    world.log_text = "".join(f"line {i}\n" for i in range(30)) + "error: failed to load model\n"
    with pytest.raises(DecisionServerError) as exc:
        world.manager().ensure_running()
    assert "rc=1" in str(exc.value)
    assert exc.value.log_tail[-1] == "error: failed to load model"
    assert len(exc.value.log_tail) == ds.LOG_TAIL_LINES
    assert not world.pidfile.exists()


def test_ready_timeout_stops_spawned_process(world: World):
    world.healthy = False
    proc = FakeProc(7004)
    world.next_proc = proc
    ticks = iter([0.0, 1.0, 5.0])
    mgr = world.manager()
    mgr._monotonic = lambda: next(ticks)
    with pytest.raises(DecisionServerError, match="시간 초과"):
        mgr.ensure_running(timeout_s=3)
    assert proc.terminated == 1
    assert mgr.stop_if_ours() is False


@pytest.fixture
def restore_signals():
    saved = {s: signal.getsignal(s) for s in (signal.SIGTERM, signal.SIGINT)}
    yield
    for s, h in saved.items():
        signal.signal(s, h)


def test_cleanup_install_is_idempotent_and_chains(
    world: World, monkeypatch: pytest.MonkeyPatch, restore_signals: None
):
    registered: list[object] = []
    monkeypatch.setattr(atexit, "register", registered.append)
    prev_calls: list[int] = []
    signal.signal(signal.SIGTERM, lambda signum, _frame: prev_calls.append(signum))

    proc = FakeProc(7005)
    world.next_proc = proc
    mgr = world.manager()
    mgr.ensure_running()
    mgr.install_cleanup()
    handler = signal.getsignal(signal.SIGTERM)
    mgr.install_cleanup()
    assert signal.getsignal(signal.SIGTERM) is handler
    assert len(registered) == 1

    assert callable(handler)
    handler(signal.SIGTERM, None)
    handler(signal.SIGTERM, None)
    assert proc.terminated == 1  # 두 번째 호출은 끌 것이 없다
    assert prev_calls == [signal.SIGTERM, signal.SIGTERM]  # 자기 자신이 아니라 원래 처리기로 이어진다


def test_sigint_keeps_keyboard_interrupt(world: World, restore_signals: None):
    signal.signal(signal.SIGINT, signal.default_int_handler)
    proc = FakeProc(7006)
    world.next_proc = proc
    mgr = world.manager()
    mgr.ensure_running()
    mgr.install_cleanup()
    handler = signal.getsignal(signal.SIGINT)
    assert callable(handler)
    with pytest.raises(KeyboardInterrupt):
        handler(signal.SIGINT, None)
    assert proc.terminated == 1


def test_alive_matching_but_unhealthy_spawns_new(world: World):
    world.existing(4242, world.good_command())
    calls = iter([False, True])  # 재사용 검사에서 실패, 새 서버 대기에서 준비
    mgr = world.manager()
    mgr._health = lambda _url: next(calls)
    handle = mgr.ensure_running()
    assert handle.spawned_by_us and handle.pid != 4242
    assert 4242 in world.alive  # 남의 프로세스는 건드리지 않는다


def test_stop_keeps_pidfile_rewritten_by_someone_else(world: World):
    mgr = world.manager()
    mgr.ensure_running()
    world.pidfile.write_text("9999\n")
    assert mgr.stop_if_ours() is True
    assert world.pidfile.read_text().strip() == "9999"
