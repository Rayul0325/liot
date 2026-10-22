"""비밀 파일 읽기 — ``~/.config/liot/secrets.env`` 의 API 키(하루 시뮬레이터의 실제 모델용).

규칙:
  - ``KEY=value`` 줄만 읽는다(앞의 ``export `` 는 허용). 주석(#)·빈 줄·값이 빈 줄은 건너뛴다.
    값을 감싼 따옴표 한 쌍은 벗긴다. 키 이름은 대문자·숫자·밑줄만.
  - 파일 권한이 600 보다 넓거나(그룹·기타 사용자 권한, 실행 권한) 소유자가 지금 사용자가
    아니면 **읽지 않는다**. 이유는 한국어 한 줄이고 값은 들어가지 않는다.
  - 값은 로그·사건·repr·예외 어디에도 쓰지 않는다. 바깥에는 **이름별 있음/없음**만 보인다.
  - 판정 백엔드(l3_judgment)는 이 모듈을 불러오지 않는다 — 키는 생성자로 넘긴다.

데몬은 이 파일을 읽지 않는다(시뮬레이터 전용).
"""

from __future__ import annotations

import os
import re
import stat
from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path

__all__ = ["DEFAULT_SECRETS_PATH", "KNOWN_KEYS", "Secrets", "load_secrets", "parse_secrets"]

DEFAULT_SECRETS_PATH = Path.home() / ".config" / "liot" / "secrets.env"
KNOWN_KEYS = ("JEV_API_KEY", "OPENAI_API_KEY", "ANTHROPIC_API_KEY", "GEMINI_API_KEY")

_LINE = re.compile(r"^(?:export\s+)?([A-Z][A-Z0-9_]*)\s*=\s*(.*)$")


@dataclass(frozen=True)
class Secrets:
    """읽은 키. 값은 ``get`` 으로만 꺼내고, 나머지 표면은 이름·있음 여부뿐이다."""

    _values: dict[str, str] = field(default_factory=dict, repr=False)
    status: str = "ok"  # ok | missing | refused
    reason_ko: str = ""

    def __repr__(self) -> str:
        return f"Secrets(status={self.status!r}, names={sorted(self._values)})"

    def get(self, name: str) -> str | None:
        return self._values.get(name)

    def names(self) -> list[str]:
        return sorted(self._values)

    def presence(self, names: Iterable[str] = KNOWN_KEYS) -> dict[str, bool]:
        """이름 → 있음 여부(값 없음). 화면의 '키 3/4개' 표시는 이것만 쓴다."""
        return {n: n in self._values for n in names}


def _unquote(value: str) -> str:
    v = value.strip()
    if len(v) >= 2 and v[0] == v[-1] and v[0] in ("'", '"'):
        return v[1:-1]
    return v


def parse_secrets(text: str) -> dict[str, str]:
    """``KEY=value`` 줄만 고른다. 주석·빈 값·형식이 다른 줄은 버린다(오류도 값을 담지 않는다)."""
    out: dict[str, str] = {}
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        m = _LINE.match(line)
        if m is None:
            continue
        value = _unquote(m.group(2))
        if value:
            out[m.group(1)] = value
    return out


def load_secrets(path: Path | str = DEFAULT_SECRETS_PATH) -> Secrets:
    """비밀 파일을 읽는다. 없으면 status=missing, 권한이 넓으면 status=refused(읽지 않음)."""
    p = Path(path).expanduser()
    try:
        st = p.stat()
    except FileNotFoundError:
        return Secrets(status="missing", reason_ko="비밀 파일이 없습니다.")
    except OSError:
        return Secrets(status="refused", reason_ko="비밀 파일 정보를 읽지 못했습니다.")
    if not stat.S_ISREG(st.st_mode):
        return Secrets(status="refused", reason_ko="비밀 파일이 일반 파일이 아니라 읽지 않았습니다.")
    mode = stat.S_IMODE(st.st_mode)
    if mode & ~0o600:
        return Secrets(
            status="refused",
            reason_ko=(
                f"비밀 파일 권한이 {mode:o} 이라 읽지 않았습니다 — "
                "chmod 600 으로 본인만 읽게 바꿔 주세요."
            ),
        )
    if hasattr(os, "getuid") and st.st_uid != os.getuid():
        return Secrets(status="refused", reason_ko="비밀 파일의 소유자가 지금 사용자가 아니라 읽지 않았습니다.")
    try:
        text = p.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return Secrets(status="refused", reason_ko="비밀 파일을 읽지 못했습니다.")
    return Secrets(_values=parse_secrets(text))
