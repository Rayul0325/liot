"""Ollama transport 선택 — 기본은 로컬 HTTP, SSH 는 호스트 명시 시에만.

2026-09-13 Mac Studio 이관으로 LIOT 가 홈 서버(ssh 별칭 home) 자신에서 돈다.
예전 기본값(transport=ssh, host=home)은 이제 자기 자신에게 ssh 하는 설정이라,
기본값이 다시 ssh 로 돌아가거나 호스트 없는 ssh 가 조용히 만들어지면 여기서 잡는다.
"""

from __future__ import annotations

import pytest

from liot.layers.l3_judgment.ollama_backend import (
    HTTPOllamaTransport,
    SSHOllamaTransport,
    build_ollama_backend,
)

_ENV_KEYS = (
    "LIOT_OLLAMA_TRANSPORT",
    "LIOT_OLLAMA_SSH_HOST",
    "LIOT_OLLAMA_HTTP_URL",
    "LIOT_OLLAMA_TIMEOUT",
)


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for key in _ENV_KEYS:
        monkeypatch.delenv(key, raising=False)


def test_default_transport_is_local_http() -> None:
    backend = build_ollama_backend()
    assert isinstance(backend.transport, HTTPOllamaTransport)
    assert backend.transport.base_url == "http://127.0.0.1:11434"


def test_http_url_env_overrides_default(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LIOT_OLLAMA_HTTP_URL", "http://10.0.0.5:11434/")
    backend = build_ollama_backend()
    assert isinstance(backend.transport, HTTPOllamaTransport)
    assert backend.transport.base_url == "http://10.0.0.5:11434"


def test_ssh_transport_requires_explicit_host(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LIOT_OLLAMA_TRANSPORT", "ssh")
    with pytest.raises(ValueError, match="LIOT_OLLAMA_SSH_HOST"):
        build_ollama_backend()


def test_ssh_transport_uses_given_host(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LIOT_OLLAMA_TRANSPORT", "ssh")
    monkeypatch.setenv("LIOT_OLLAMA_SSH_HOST", "school")
    backend = build_ollama_backend()
    assert isinstance(backend.transport, SSHOllamaTransport)
    assert backend.transport.ssh_host == "school"
