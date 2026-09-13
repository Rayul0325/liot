"""시크릿 스캔(scripts/secret_scan.sh)이 실제로 잡고, 실패를 '통과'로 읽지 않는다.

2026-09-01 리뷰가 재현한 세 구멍을 고정한다:
  - 개인키 패턴이 '-' 로 시작해 git grep 이 옵션으로 읽었다(rc=129) → 키가 있어도 OK.
  - YAML 형태(`LIOT_HA_TOKEN: 값`)는 `=` 만 보던 패턴을 비껴갔다.
  - git 체크아웃 밖에서 돌리면 git grep 이 실패(rc=128)했는데도 OK 를 찍었다.

스캔 대상 문자열은 조립해서 쓴다 — 이 파일 자체가 저장소 스캔에 걸리면 안 된다.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "secret_scan.sh"

PRIVATE_KEY = "-----BEGIN " + "RSA PRIVATE KEY-----\nMIIE\n-----END RSA PRIVATE KEY-----\n"
YAML_TOKEN = "LIOT_HA" + "_TOKEN: abcdefgh12345678\n"
ENV_SECRET = "LIOT_NOTIFY" + "_SECRET=abcdefgh12345678\n"


def _repo(tmp_path: Path, files: dict[str, str]) -> Path:
    """스크립트 사본을 둔 임시 git 저장소. git grep 은 추적 파일만 보므로 add 한다."""
    root = tmp_path / "repo"
    (root / "scripts").mkdir(parents=True)
    shutil.copy2(SCRIPT, root / "scripts" / "secret_scan.sh")
    subprocess.run(["git", "init", "-q"], cwd=root, check=True)
    for name, text in files.items():
        (root / name).write_text(text, encoding="utf-8")
    subprocess.run(["git", "add", "-A"], cwd=root, check=True)
    return root


def _scan(root: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["sh", str(root / "scripts" / "secret_scan.sh")],
        capture_output=True,
        text=True,
        timeout=30,
    )


def test_clean_repo_passes(tmp_path: Path) -> None:
    proc = _scan(_repo(tmp_path, {"app.py": "print('hello')\n"}))
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "OK" in proc.stdout


@pytest.mark.parametrize(
    "content",
    [PRIVATE_KEY, YAML_TOKEN, ENV_SECRET],
    ids=["private-key-block", "yaml-token", "env-secret"],
)
def test_committed_secret_fails(tmp_path: Path, content: str) -> None:
    proc = _scan(_repo(tmp_path, {"leak.txt": content}))
    assert proc.returncode == 1, proc.stdout + proc.stderr
    assert "leak.txt" in proc.stdout


def test_placeholder_values_pass(tmp_path: Path) -> None:
    text = "LIOT_HA" + "_TOKEN=***\nLIOT_NTFY" + "_TOKEN: <token>\n"
    proc = _scan(_repo(tmp_path, {"README.md": text}))
    assert proc.returncode == 0, proc.stdout + proc.stderr


def test_git_failure_is_not_reported_as_clean(tmp_path: Path) -> None:
    root = tmp_path / "not-a-repo"
    (root / "scripts").mkdir(parents=True)
    shutil.copy2(SCRIPT, root / "scripts" / "secret_scan.sh")
    # git 이 PATH 에 있어야 rc=128(저장소 아님)이 나온다. 없으면 rc=127 로도 테스트가
    # 통과해 '엉뚱한 이유로 초록'이 된다(리뷰 R-8) — 실제 경로를 찾아 넣는다.
    git = shutil.which("git")
    assert git, "git 을 찾을 수 없어 이 테스트의 전제가 성립하지 않는다"
    proc = subprocess.run(
        ["sh", str(root / "scripts" / "secret_scan.sh")],
        capture_output=True,
        text=True,
        timeout=30,
        env={
            "PATH": f"{Path(git).parent}:/usr/bin:/bin",  # sh 도 필요하다
            "GIT_CEILING_DIRECTORIES": str(tmp_path),
        },
    )
    assert "not a git repository" in (proc.stdout + proc.stderr).lower() or "rc=" in proc.stdout, (
        proc.stdout + proc.stderr
    )
    assert proc.returncode != 0, proc.stdout
    assert "OK" not in proc.stdout
