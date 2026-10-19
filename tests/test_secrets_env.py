"""비밀 파일 읽기(liot/secrets_env.py) — 임시 파일로만. 실제 ~/.config/liot/secrets.env 는 열지 않는다.

핵심 검증:
  - KEY=value 줄만 읽는다(주석·빈 값·형식이 다른 줄은 버림, export·따옴표 허용).
  - 권한이 600 보다 넓으면 읽지 않고, 한국어 이유에 값이 들어가지 않는다.
  - 바깥 표면(repr·presence·names)에는 값이 없다.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from liot.secrets_env import KNOWN_KEYS, load_secrets, parse_secrets

# 값은 실행 중에 만든다 — 키처럼 생긴 문자열을 저장소에 두지 않는다(비밀 스캔과 같은 이유).
FAKE = "fake" + "-value-" + "0123456789"


def _write(tmp_path: Path, text: str, mode: int = 0o600) -> Path:
    p = tmp_path / "secrets.env"
    p.write_text(text, encoding="utf-8")
    os.chmod(p, mode)
    return p


def test_parses_only_key_value_lines() -> None:
    text = "\n".join(
        [
            "# 주석 줄",
            f"JEV_API_KEY={FAKE}",
            "OPENAI_API_KEY=",  # 빈 값은 없는 것으로 친다
            f'export ANTHROPIC_API_KEY="{FAKE}2"',
            f"GEMINI_API_KEY = '{FAKE}3'",
            f"lower_case={FAKE}",  # 이름 규칙 밖
            "그냥 문장",
            f"  # JEV_API_KEY={FAKE}9",
        ]
    )
    assert parse_secrets(text) == {
        "JEV_API_KEY": FAKE,
        "ANTHROPIC_API_KEY": FAKE + "2",
        "GEMINI_API_KEY": FAKE + "3",
    }


def test_loads_a_600_file_and_exposes_presence_by_name_only(tmp_path: Path) -> None:
    secrets = load_secrets(_write(tmp_path, f"JEV_API_KEY={FAKE}\nGEMINI_API_KEY={FAKE}\n"))
    assert secrets.status == "ok"
    assert secrets.get("JEV_API_KEY") == FAKE
    assert secrets.presence() == {
        "JEV_API_KEY": True, "OPENAI_API_KEY": False,
        "ANTHROPIC_API_KEY": False, "GEMINI_API_KEY": True,
    }
    assert list(secrets.presence()) == list(KNOWN_KEYS)
    assert secrets.names() == ["GEMINI_API_KEY", "JEV_API_KEY"]
    assert FAKE not in repr(secrets) and FAKE not in str(secrets)


@pytest.mark.parametrize("mode", [0o644, 0o640, 0o604, 0o700, 0o660, 0o666])
def test_wider_than_600_is_refused_without_the_value(tmp_path: Path, mode: int) -> None:
    secrets = load_secrets(_write(tmp_path, f"JEV_API_KEY={FAKE}\n", mode))
    assert secrets.status == "refused"
    assert secrets.get("JEV_API_KEY") is None
    assert secrets.presence()["JEV_API_KEY"] is False
    assert f"{mode:o}" in secrets.reason_ko and "chmod 600" in secrets.reason_ko
    assert FAKE not in secrets.reason_ko and FAKE not in repr(secrets)


@pytest.mark.parametrize("mode", [0o600, 0o400])
def test_600_or_narrower_is_read(tmp_path: Path, mode: int) -> None:
    assert load_secrets(_write(tmp_path, f"JEV_API_KEY={FAKE}\n", mode)).status == "ok"


def test_missing_file_is_missing(tmp_path: Path) -> None:
    secrets = load_secrets(tmp_path / "none.env")
    assert secrets.status == "missing"
    assert not any(secrets.presence().values())


def test_directory_is_refused(tmp_path: Path) -> None:
    d = tmp_path / "secrets.env"
    d.mkdir()
    os.chmod(d, 0o600)
    assert load_secrets(d).status == "refused"
