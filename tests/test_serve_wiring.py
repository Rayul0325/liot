"""상시 데몬 배선(`liot.serve._amain`) 검증 — HA·ntfy 없이도 올바로 조립되는가.

이 함수가 게이트 밖에 있던 동안(커버리지 47%) 배선 실수는 런타임에서만 드러났다.
여기서는 env 조합만 바꿔 가며 조립 결과를 본다. 실제 루프(service.run)는 가짜로
바꿔치기하므로 네트워크·타이머·외부 송신이 전혀 일어나지 않는다.
"""

from __future__ import annotations

import pytest

from liot import serve
from liot.service import LiotService

REAL_SECRET = "a-real-long-signing-secret-value"


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch: pytest.MonkeyPatch):
    """데몬이 읽는 env 를 초기화한다(개발 셸 값이 새어 들어오지 않게)."""
    for key in (
        "LIOT_HOME_ID", "LIOT_DB_PATH", "LIOT_TICK_SECONDS", "LIOT_NOTIFY_SECRET",
        "LIOT_NTFY_TOPIC", "LIOT_NTFY_URL", "LIOT_HA_URL", "LIOT_HA_TOKEN",
        "LIOT_FEEDBACK_HOST", "LIOT_FEEDBACK_PORT", "LIOT_FEEDBACK_ALLOW_DEV_KEY",
        "LIOT_EXECUTION_ENABLED", "LIOT_LLM_BACKEND",
    ):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("LIOT_DB_PATH", ":memory:")
    monkeypatch.setenv("LIOT_NOTIFY_SECRET", REAL_SECRET)
    monkeypatch.setenv("LIOT_FEEDBACK_PORT", "0")  # 0 = 수신구 끔(포트 바인드 없음)
    monkeypatch.setenv("LIOT_TICK_SECONDS", "7")


def _capture_run(monkeypatch: pytest.MonkeyPatch) -> dict[str, object]:
    """service.run 을 가짜로 바꿔 '무엇을 들고 루프에 들어갔는지'만 본다."""
    seen: dict[str, object] = {}

    async def fake_run(self, bridge=None, *, tick_seconds: float = 0.0):  # noqa: ANN001
        seen["bridge"] = bridge
        seen["tick_seconds"] = tick_seconds
        seen["executor"] = self._executor
        seen["policy_enabled"] = self._policy.enabled
        seen["transport"] = self.transport
        seen["home_id"] = self.proposals.home_id

    monkeypatch.setattr(LiotService, "run", fake_run)
    return seen


async def test_amain_runs_without_ha_or_ntfy(monkeypatch: pytest.MonkeyPatch) -> None:
    """HA·ntfy 미설정이면 센서 스트림·실송신 없이 능동 틱만으로 조립된다."""
    seen = _capture_run(monkeypatch)
    rc = await serve._amain()
    assert rc == 0
    assert seen["bridge"] is None  # HA 미설정 → 브리지 없음
    assert seen["transport"] is None  # ntfy 미설정 → sanitize-only
    assert seen["tick_seconds"] == 7.0
    assert seen["executor"] is None and seen["policy_enabled"] is False


async def test_amain_keeps_execution_off_without_ha(monkeypatch: pytest.MonkeyPatch) -> None:
    """실행 옵트인을 켜도 HA 가 없으면 조작기는 만들어지지 않는다(제안-만 유지)."""
    monkeypatch.setenv("LIOT_EXECUTION_ENABLED", "1")
    seen = _capture_run(monkeypatch)
    assert await serve._amain() == 0
    assert seen["policy_enabled"] is True  # 정책은 켜졌지만
    assert seen["executor"] is None  # 조작 대상이 없어 실행기는 없다


async def test_amain_respects_home_id_and_tick(monkeypatch: pytest.MonkeyPatch) -> None:
    """home_id·tick 주기가 서비스까지 그대로 전달된다."""
    monkeypatch.setenv("LIOT_HOME_ID", "house-42")
    monkeypatch.setenv("LIOT_TICK_SECONDS", "13.5")
    seen = _capture_run(monkeypatch)
    assert await serve._amain() == 0
    assert seen["home_id"] == "house-42"
    assert seen["tick_seconds"] == 13.5


async def test_amain_with_dev_key_does_not_open_feedback_endpoint(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """dev 서명 키로는 수신구를 열지 않는다(fail-closed) — 사유를 찍고 데몬은 계속 돈다.

    포트를 실제 번호로 준다: `LIOT_FEEDBACK_PORT=0` 은 '수신구 끔'이라 가드보다
    먼저 반환돼(serve.py `if not port`) 이 경로를 지나가지 않는다.
    """
    import socket

    with socket.socket() as probe:  # 비어 있는 포트 하나를 확보만 하고 닫는다
        probe.bind(("127.0.0.1", 0))
        free_port = probe.getsockname()[1]
    monkeypatch.setenv("LIOT_FEEDBACK_PORT", str(free_port))
    monkeypatch.delenv("LIOT_NOTIFY_SECRET", raising=False)
    seen = _capture_run(monkeypatch)
    assert await serve._amain() == 0
    out = capsys.readouterr().out
    assert "수신구를 열지 않습니다" in out, out
    assert "LIOT_NOTIFY_SECRET" in out  # 어떻게 열지까지 알려 준다
    assert seen["bridge"] is None

    # 아무도 그 포트를 듣고 있지 않아야 한다(정말로 열지 않았다).
    with socket.socket() as check:
        check.settimeout(1)
        assert check.connect_ex(("127.0.0.1", free_port)) != 0


async def test_amain_opens_feedback_endpoint_with_a_real_secret(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """실제 비밀키면 수신구가 실제로 열리고, 종료 시 닫힌다."""
    import socket

    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        free_port = probe.getsockname()[1]
    monkeypatch.setenv("LIOT_FEEDBACK_PORT", str(free_port))
    _capture_run(monkeypatch)
    assert await serve._amain() == 0
    assert "수신 대기" in capsys.readouterr().out
    with socket.socket() as check:  # 루프 종료 후에는 닫혀 있어야 한다
        check.settimeout(1)
        assert check.connect_ex(("127.0.0.1", free_port)) != 0


def test_main_returns_zero_on_keyboard_interrupt(monkeypatch: pytest.MonkeyPatch) -> None:
    """Ctrl+C 는 오류가 아니라 정상 종료다."""

    def fake_run(coro):  # noqa: ANN001
        coro.close()  # 넘겨받은 코루틴을 닫는다(never awaited 경고 방지)
        raise KeyboardInterrupt

    monkeypatch.setattr("asyncio.run", fake_run)
    assert serve.main() == 0
