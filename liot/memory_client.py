"""memgineering 기억 클라이언트 — 하루 시뮬레이터의 데모 브레인(``liot-demo``) 전용.

설계 핵심:
  - **CLI 만 쓴다**: ``subprocess.run(["memgineering", 동사, …, "--json"])``. 환경은
    ``os.environ`` 에 ``MEMGINEERING_HOME=<데모 home 절대경로>`` 와 ``MEMGINEERING_NO_UPDATE=1``
    을 더한 것이다(``HOME``·``PATH`` 유지). 제한시간 10초. 동사는 recall·remember·use·whoami 뿐.
  - **목적지 확인이 유일한 안전장치**: 쓰기는 ``--brain`` 으로 목적지를 정할 수 없다. 그래서 실행
    시작 때 ``use --json`` 이 호스티드 ``liot-demo``(로그인됨)를 가리키는지 보고, 아니면 그 실행의
    기억을 끄고 이유를 남긴다(``check_ready``). 확인 전에는 어떤 recall·remember 도 나가지 않는다.
  - **범위**: 집은 ``house-<run_id>``, 거주자 성향은 ``resident-default``. recall 은 늘 ``--scope``
    를 붙이고(없으면 브레인 전체를 읽는다), 집은 ``resident-*`` 를 읽을 수 없다. 돌아온 카드 중
    요청한 범위가 아닌 것은 버린다(카드에 scope 가 있을 때 — 실제 CLI 카드에는 없고 서버가 거른다).
  - **쓰기도 외부 송신**: remember 의 문장·이유와 recall 의 질의는 특성 낱말 검사와
    ``emit_external(Destination.MEMORY)`` 를 지난 것만 나간다.
  - **카드는 제목과 요약 한 줄만**: 경로·핸들·id 는 버린다.
  - **실패해도 계속**: 시간 초과·종료 코드·형식 오류·못 푸는 바이트·실행기 예외는 예외 대신
    빈 결과와 기록으로 끝난다(``tests/test_memory_client.py`` 가 각 경우를 고정한다).
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

from liot.core.egress_gate import Destination, emit_external
from liot.core.invariants import EgressViolation
from liot.core.traits import TRAIT_WORDS

__all__ = [
    "ALLOWED_VERBS",
    "DEMO_BRAIN",
    "DEMO_HOME",
    "RESIDENT_SCOPE",
    "HouseMemory",
    "MemoryCard",
    "MemoryClient",
    "MemoryRefused",
    "ResidentMemory",
    "Runner",
    "house_scope",
    "parse_cli_json",
]

DEMO_BRAIN = "liot-demo"
DEMO_HOME = Path.home() / ".config" / "liot" / "memgineering"
RESIDENT_SCOPE = "resident-default"
ALLOWED_VERBS = frozenset({"recall", "remember", "use", "whoami"})
DEFAULT_TIMEOUT_S = 10.0
_SUMMARY_CHARS = 120
_RUN_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
# 부모 환경에서 물려받지 않는 변수(서버 주소 덮어쓰기).
_DROPPED_ENV = frozenset({"MEMGINEERING_API_URL", "KORDIS_API_URL"})
_KINDS = frozenset({"episode", "fact", "decision", "preference", "goal", "procedure"})

# (argv, env, timeout_s) → 끝난 프로세스. 시험은 가짜 CLI 를 넣는다.
Runner = Callable[[Sequence[str], Mapping[str, str], float], "subprocess.CompletedProcess[str]"]


class MemoryRefused(Exception):
    """보내기 전에 막혔다(동사·범위·특성 낱말·egress). 문구에 보낼 문장을 넣지 않는다."""


def _default_runner(
    argv: Sequence[str], env: Mapping[str, str], timeout_s: float
) -> subprocess.CompletedProcess[str]:
    # 바이트로 받아 UTF-8 로 풀되 못 푸는 바이트는 바꿔 넣는다(errors="replace"). text=True 는
    # 지역 인코딩으로 풀다 UnicodeDecodeError 를 내 기억 확인 전체가 깨진다.
    raw = subprocess.run(  # noqa: S603 — argv 는 고정 동사 + 검사한 인자
        list(argv), env=dict(env), capture_output=True, timeout=timeout_s, check=False
    )
    return subprocess.CompletedProcess(
        raw.args, raw.returncode,
        (raw.stdout or b"").decode("utf-8", errors="replace"),
        (raw.stderr or b"").decode("utf-8", errors="replace"),
    )


def house_scope(run_id: str) -> str:
    """실행마다 따로인 집 범위. run_id 는 영문·숫자·_·- 1~64자."""
    if not _RUN_ID_RE.fullmatch(run_id):
        raise ValueError(f"run_id 형식이 틀렸습니다: {run_id!r}")
    return f"house-{run_id}"


def parse_cli_json(stdout: str) -> dict[str, Any] | None:
    """``--json`` 출력에서 JSON 객체 하나를 꺼낸다.

    CLI 는 JSON 앞에 안내문(예: 오래된 안내 파일 공지)을 찍을 수 있다. 그래서 전체를 먼저 읽어 보고,
    안 되면 ``{`` 로 시작하는 줄을 뒤에서부터 찾아 그 줄부터 끝까지를 읽는다. 객체가 아니면 None.
    """
    text = stdout.strip()
    candidates = [text]
    lines = stdout.splitlines()
    for i in range(len(lines) - 1, -1, -1):
        if lines[i].lstrip().startswith("{"):
            candidates.append("\n".join(lines[i:]).strip())
    for chunk in candidates:
        if not chunk:
            continue
        try:
            value = json.loads(chunk)
        except (ValueError, RecursionError):  # 형식 오류·지나치게 깊은 중첩
            continue
        if isinstance(value, dict):
            return value
    return None


def _one_line(value: object, limit: int = _SUMMARY_CHARS) -> str:
    if not isinstance(value, str):
        return ""
    line = " ".join(value.split())
    return line if len(line) <= limit else line[: limit - 1] + "…"


@dataclass(frozen=True)
class MemoryCard:
    """recall 카드에서 쓰는 것만: 제목·요약 한 줄·종류·규칙 여부. 경로·핸들은 없다."""

    title: str
    summary: str
    kind: str | None = None
    rule: bool = False


@dataclass
class MemoryClient:
    """데모 home 에서만 도는 memgineering CLI 감싸개. 실패는 ``log`` 에 남기고 계속한다."""

    home: Path = DEMO_HOME
    brain: str = DEMO_BRAIN
    executable: str = "memgineering"
    timeout_s: float = DEFAULT_TIMEOUT_S
    runner: Runner = _default_runner
    enabled: bool = field(default=False, init=False)
    # 기억이 꺼진 한국어 사유(확인 전·확인 실패). 켜지면 None.
    disabled_reason_ko: str | None = field(default="아직 브레인 확인 전입니다.", init=False)
    # 호출마다 한 줄: 동사·범위·성공·지연·실패 사유. 문장 본문·카드 내용은 없다.
    log: list[dict[str, Any]] = field(default_factory=list, init=False)

    def __post_init__(self) -> None:
        home = Path(os.path.expanduser(str(self.home)))
        if not home.is_absolute():
            raise ValueError("MEMGINEERING_HOME 은 절대경로여야 합니다.")
        self.home = home

    def env(self) -> dict[str, str]:
        """``os.environ`` + 데모 home + 업데이트 끄기. HOME·PATH 는 그대로.

        부모 환경의 서버 주소 덮어쓰기(``MEMGINEERING_API_URL``·``KORDIS_API_URL``)는 뺀다 — 데모
        home 이 가리키는 서버가 아닌 곳으로 쓰기가 가지 않게. 데모 home 값은 부모 값보다 뒤에 둬서
        늘 이긴다.
        """
        base = {k: v for k, v in os.environ.items() if k not in _DROPPED_ENV}
        return {**base, "MEMGINEERING_HOME": str(self.home), "MEMGINEERING_NO_UPDATE": "1"}

    def _call(self, verb: str, args: Sequence[str], *, scope: str | None = None) -> dict[str, Any] | None:
        if verb not in ALLOWED_VERBS:
            raise MemoryRefused(f"허용되지 않은 동사: {verb}")
        argv = [self.executable, verb, *args, "--json"]
        entry: dict[str, Any] = {"verb": verb, "scope": scope, "ok": False}
        t0 = time.perf_counter()
        try:
            proc = self.runner(argv, self.env(), self.timeout_s)
        except subprocess.TimeoutExpired:
            entry["error"] = "timeout"
            return self._finish(entry, t0, None)
        except OSError as exc:
            entry["error"] = f"spawn:{type(exc).__name__}"
            return self._finish(entry, t0, None)
        except Exception as exc:  # noqa: BLE001 — 바깥 프로세스 경계: 어떤 실패도 기억 없이 계속
            entry["error"] = f"runner:{type(exc).__name__}"
            return self._finish(entry, t0, None)
        if proc.returncode != 0:
            entry["error"] = f"exit {proc.returncode}"
            return self._finish(entry, t0, None)
        stdout = proc.stdout if isinstance(proc.stdout, str) else ""
        data = parse_cli_json(stdout)
        if data is None:
            entry["error"] = "not json"
            return self._finish(entry, t0, None)
        entry["ok"] = True
        return self._finish(entry, t0, data)

    def _finish(
        self, entry: dict[str, Any], t0: float, data: dict[str, Any] | None
    ) -> dict[str, Any] | None:
        entry["seconds"] = round(time.perf_counter() - t0, 4)
        self.log.append(entry)
        return data

    # ── 확인 ──────────────────────────────────────────────────────────────

    def check_ready(self) -> bool:
        """``use --json`` 이 호스티드 데모 브레인(로그인됨)을 가리킬 때만 기억을 켠다."""
        self.enabled = False
        data = self._call("use", [])
        if data is None:
            self.disabled_reason_ko = "브레인 확인(use)이 실패해 이번 실행은 기억 없이 진행합니다."
            return False
        hosted = data.get("hosted")
        if data.get("destination") != "hosted" or not isinstance(hosted, Mapping):
            self.disabled_reason_ko = "데모 home 이 호스티드 브레인을 가리키지 않아 기억을 껐습니다."
            return False
        if hosted.get("signed_in") is not True:
            self.disabled_reason_ko = "데모 home 이 로그인되어 있지 않아 기억을 껐습니다."
            return False
        if hosted.get("name") != self.brain:
            self.disabled_reason_ko = (
                f"데모 home 이 '{self.brain}' 가 아닌 다른 브레인을 가리켜 기억을 껐습니다."
            )
            return False
        self.enabled = True
        self.disabled_reason_ko = None
        return True

    def whoami_signed_in(self) -> bool:
        """``whoami --json`` 에 계정이 있으면 True. 실패는 False."""
        data = self._call("whoami", [])
        return isinstance(data, dict) and isinstance(data.get("account"), Mapping)

    # ── 송신 검사 ─────────────────────────────────────────────────────────

    @staticmethod
    def _outbound(fields: Mapping[str, str]) -> None:
        """특성 낱말 → egress(MEMORY). 걸리면 MemoryRefused(보내지 않는다)."""
        if any(word in value for value in fields.values() for word in TRAIT_WORDS):
            raise MemoryRefused("특성 낱말이 있어 보내지 않았습니다.")
        try:
            emit_external(Destination.MEMORY, dict(fields), trace_id="memory")
        except EgressViolation:
            raise MemoryRefused("송신 검사를 통과하지 못해 보내지 않았습니다.") from None

    # ── recall · remember ─────────────────────────────────────────────────

    def recall(
        self, query: str, *, scope: str, reader: Literal["house", "resident"], limit: int = 3
    ) -> list[MemoryCard]:
        """한 범위만 읽는다. 집(reader="house")은 ``resident-*`` 를 읽을 수 없다."""
        if not scope:
            raise MemoryRefused("범위 없는 recall 은 브레인 전체를 읽어 막습니다.")
        if reader == "house" and not scope.startswith("house-"):
            raise MemoryRefused("집은 자기 범위(house-*)만 읽습니다.")
        if reader == "resident" and scope != RESIDENT_SCOPE:
            raise MemoryRefused("거주자 성향은 resident-default 만 읽습니다.")
        if not self.enabled:
            return []
        self._outbound({"query": query})
        data = self._call(
            "recall", [query, "--scope", scope, "--limit", str(max(1, min(limit, 10)))], scope=scope
        )
        if data is None:
            return []
        cards = data.get("cards")
        if not isinstance(cards, list):
            return []
        out: list[MemoryCard] = []
        for card in cards:
            if not isinstance(card, Mapping):
                continue
            # 실제 CLI(0.27) 카드에는 scope 필드가 없다 — 서버가 --scope 로 이미 걸렀다(2026-10-20
            # 데모 브레인 실측: 다른 범위로 물으면 0장). 그래서 필드가 없으면 받고, 있는데 다르면
            # (서버가 다른 범위를 섞어 보낸 것) 버린다.
            if "scope" in card and card["scope"] != scope:
                continue
            title = _one_line(card.get("title"), 80)
            if not title:
                continue
            summary = _one_line(card.get("summary"))
            # 특성 낱말이 든 카드는 버린다 — 화면 기록·이유 입력 어디에도 닿지 않게.
            if any(word in title or word in summary for word in TRAIT_WORDS):
                continue
            kind = card.get("kind")
            out.append(
                MemoryCard(
                    title=title,
                    summary=summary,
                    kind=kind if isinstance(kind, str) else None,
                    rule=card.get("binding") is True,
                )
            )
        return out

    def remember(
        self,
        text: str,
        *,
        scope: str,
        reason: str,
        rule: bool = False,
        kind: str | None = None,
    ) -> str | None:
        """한 문장을 그 범위에 쓴다. 성공하면 되돌리기 id(``op_id``), 아니면 None."""
        if not scope or not (scope.startswith("house-") or scope == RESIDENT_SCOPE):
            raise MemoryRefused("쓰기 범위는 house-* 또는 resident-default 뿐입니다.")
        if not text.strip() or text.lstrip().startswith("-"):
            raise MemoryRefused("문장이 비었거나 옵션처럼 시작합니다.")
        if kind is not None and kind not in _KINDS:
            raise MemoryRefused(f"모르는 종류: {kind}")
        if not self.enabled:
            return None
        self._outbound({"text": text, "reason": reason})
        args = [text, "--scope", scope, "--reason", reason]
        if rule:
            args.append("--rule")
        if kind is not None:
            args += ["--kind", kind]
        data = self._call("remember", args, scope=scope)
        if data is None:
            return None
        op_id = data.get("op_id")
        return op_id if isinstance(op_id, str) and op_id else str(data.get("id") or "") or None


# ── 역할별 창구: 범위를 고를 수 없게 묶는다 ─────────────────────────────


@dataclass
class HouseMemory:
    """한 실행의 집 기억. 자기 범위(``house-<run_id>``)만 읽고 쓴다."""

    client: MemoryClient
    run_id: str
    scope: str = field(init=False)

    def __post_init__(self) -> None:
        self.scope = house_scope(self.run_id)

    def recall(self, query: str, *, limit: int = 3) -> list[MemoryCard]:
        return self.client.recall(query, scope=self.scope, reader="house", limit=limit)

    def remember(self, text: str, *, reason: str, rule: bool = False,
                 kind: str | None = None) -> str | None:
        return self.client.remember(text, scope=self.scope, reason=reason, rule=rule, kind=kind)


@dataclass
class ResidentMemory:
    """가상 거주자의 성향(``resident-default``). 집 창구와 섞이지 않는다."""

    client: MemoryClient
    scope: str = field(default=RESIDENT_SCOPE, init=False)

    def recall(self, query: str, *, limit: int = 3) -> list[MemoryCard]:
        return self.client.recall(query, scope=self.scope, reader="resident", limit=limit)

    def exists(self, sentence: str) -> bool:
        """이미 같은 제목·요약의 취향 카드가 있는가(CLI 호출 한 번)."""
        found = self.recall(sentence, limit=5)
        return any(card.title == sentence or card.summary == sentence for card in found)

    def plant(self, sentence: str, *, reason: str) -> str | None:
        """취향 문장 하나를 심는다(CLI 호출 한 번). 되돌리기 id 또는 None."""
        return self.client.remember(sentence, scope=self.scope, reason=reason, kind="preference")

    def seed(self, sentences: Sequence[str], *, reason: str) -> int:
        """취향 문장을 처음 한 번만 심는다. 이미 같은 제목이 있으면 건너뛴다. 심은 수."""
        planted = 0
        for sentence in sentences:
            if self.exists(sentence):
                continue
            if self.plant(sentence, reason=reason) is not None:
                planted += 1
        return planted
