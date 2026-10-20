"""memgineering 기억 클라이언트(liot.memory_client) — 가짜 CLI 만 쓴다(실제 로그인·쓰기 없음).

지키는 것:
  - 모든 호출에 데모 home(절대경로)·업데이트 끄기·``--json``. 동사는 넷뿐.
  - ``use --json`` 이 호스티드 ``liot-demo``(로그인됨)가 아니면 기억이 꺼지고 아무것도 안 나간다.
  - 집은 ``resident-*`` 를 못 읽고, recall 은 늘 ``--scope`` 를 붙이고, 다른 범위 카드는 버린다.
  - remember·recall 질의는 특성 낱말 검사와 egress(MEMORY)를 지난다.
  - 실패·시간 초과는 기억 없이 계속. 카드는 제목·요약 한 줄만.
  - 형식: 문서화된 ``use``·``recall``·``remember`` JSON(앞에 안내문이 붙은 경우 포함).
"""

from __future__ import annotations

import json
import os
import stat
import subprocess
import sys
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

import pytest

from liot.live_graph import TRAIT_WORDS
from liot.memory_client import (
    ALLOWED_VERBS,
    DEMO_BRAIN,
    DEMO_HOME,
    RESIDENT_SCOPE,
    HouseMemory,
    MemoryCard,
    MemoryClient,
    MemoryRefused,
    ResidentMemory,
    house_scope,
    parse_cli_json,
)

HOME = Path("/tmp/liot-test-mg-home")  # 실제로 만들지 않는다(가짜 CLI 만 본다)
NOTICE = "## The guidance in your agent tools is older than this memgineering.\n\nrun setup\n\n"

# 문서화된 모양(memgineering 0.27 `use --json` 기준 저장본과 같은 키).
HOSTED: dict[str, Any] = {"name": DEMO_BRAIN, "id": "b-1", "api_url": "https://api.example",
                          "signed_in": True, "sign_in_pending": False}
USE_DEMO: dict[str, Any] = {"hosted": HOSTED, "destination": "hosted", "brain": None, "via": None,
                            "pointer": None}


def _card(title: str, scope: str, **extra: Any) -> dict[str, Any]:
    # recall --json 카드: 메모리 필드 + handle + path (+ scaffolding).
    return {"id": "m-" + title, "title": title, "summary": f"{title} 요약\n둘째 줄",
            "scope": scope, "kind": "episode", "type": "note", "handle": "h-" + title,
            "path": f"observations/{title}.md", "scaffolding": False, **extra}


class FakeCLI:
    """argv·env 를 기록하고 동사별로 답한다."""

    def __init__(self, use: dict[str, Any] | None = None) -> None:
        self.use = USE_DEMO if use is None else use
        self.calls: list[tuple[list[str], dict[str, str], float]] = []
        self.cards: list[dict[str, Any]] = []
        self.fail: dict[str, Any] = {}

    def __call__(self, argv: Sequence[str], env: Mapping[str, str],
                 timeout_s: float) -> subprocess.CompletedProcess[str]:
        argv = list(argv)
        self.calls.append((argv, dict(env), timeout_s))
        verb = argv[1]
        how = self.fail.get(verb)
        if how == "timeout":
            raise subprocess.TimeoutExpired(argv, timeout_s)
        if how == "exit":
            return subprocess.CompletedProcess(argv, 2, "", "boom")
        if how == "garbage":
            return subprocess.CompletedProcess(argv, 0, "not json at all", "")
        if verb == "use":
            out: dict[str, Any] = self.use
        elif verb == "recall":
            out = {"query": argv[2], "detail": "card", "cards": self.cards, "related": [],
                   "conflicts": []}
        elif verb == "remember":
            out = {"id": "m-new", "path": "observations/x.md", "op_id": "OP123",
                   "scope": argv[argv.index("--scope") + 1], "binding": "--rule" in argv,
                   "kind": None}
        else:
            out = {"account": {"id": "a"}}
        return subprocess.CompletedProcess(argv, 0, NOTICE + json.dumps(out, ensure_ascii=False), "")

    def verbs(self) -> list[str]:
        return [argv[1] for argv, _, _ in self.calls]


def _ready(fake: FakeCLI | None = None) -> tuple[MemoryClient, FakeCLI]:
    fake = fake or FakeCLI()
    client = MemoryClient(home=HOME, runner=fake)
    assert client.check_ready()
    return client, fake


# ── 환경·동사 ─────────────────────────────────────────────────────────────


def test_every_call_has_demo_home_no_update_and_json() -> None:
    client, fake = _ready()
    client.recall("환기", scope="house-r1", reader="house")
    client.remember("환기 제안이 도움됐다", scope="house-r1", reason="시뮬레이션")
    client.whoami_signed_in()
    assert fake.verbs() == ["use", "recall", "remember", "whoami"]
    for argv, env, timeout in fake.calls:
        assert argv[0] == "memgineering" and argv[-1] == "--json"
        assert env["MEMGINEERING_HOME"] == str(HOME)
        assert env["MEMGINEERING_NO_UPDATE"] == "1"
        assert env.get("PATH") == os.environ.get("PATH")
        assert env.get("HOME") == os.environ.get("HOME")
        assert timeout == 10.0


def test_default_home_is_the_absolute_demo_home() -> None:
    assert DEMO_HOME.is_absolute()
    assert Path.home() / ".config" / "liot" / "memgineering" == DEMO_HOME
    assert MemoryClient(runner=FakeCLI()).env()["MEMGINEERING_HOME"] == str(DEMO_HOME)


def test_relative_home_is_rejected() -> None:
    with pytest.raises(ValueError):
        MemoryClient(home=Path("relative/home"), runner=FakeCLI())


def test_only_four_verbs_are_allowed() -> None:
    assert frozenset({"recall", "remember", "use", "whoami"}) == ALLOWED_VERBS
    client, fake = _ready()
    for verb in ("login", "create", "delete-brain", "push", "undo"):
        with pytest.raises(MemoryRefused):
            client._call(verb, [])
    assert fake.verbs() == ["use"]


# ── use 확인 ─────────────────────────────────────────────────────────────


@pytest.mark.parametrize("use", [
    {**USE_DEMO, "hosted": {**HOSTED, "name": "개인 브레인"}},
    {**USE_DEMO, "hosted": {**HOSTED, "signed_in": False}},
    {**USE_DEMO, "destination": "local"},
    {"hosted": None, "destination": None, "brain": None, "via": None, "pointer": None},
    {},
])
def test_use_not_pointing_at_signed_in_demo_brain_disables_memory(use: dict[str, Any]) -> None:
    fake = FakeCLI(use=use)
    client = MemoryClient(home=HOME, runner=fake)
    assert client.check_ready() is False
    assert client.enabled is False and client.disabled_reason_ko
    assert client.recall("환기", scope="house-r1", reader="house") == []
    assert client.remember("환기 좋았다", scope="house-r1", reason="r") is None
    assert fake.verbs() == ["use"]  # 아무것도 더 나가지 않는다


@pytest.mark.parametrize("how", ["timeout", "exit", "garbage"])
def test_use_failure_disables_memory_and_continues(how: str) -> None:
    fake = FakeCLI()
    fake.fail["use"] = how
    client = MemoryClient(home=HOME, runner=fake)
    assert client.check_ready() is False
    assert "실패" in (client.disabled_reason_ko or "")
    assert client.log[-1]["ok"] is False


def test_nothing_is_sent_before_check_ready() -> None:
    fake = FakeCLI()
    client = MemoryClient(home=HOME, runner=fake)
    assert client.recall("환기", scope="house-r1", reader="house") == []
    assert client.remember("환기", scope="house-r1", reason="r") is None
    assert fake.calls == []


# ── 범위 ─────────────────────────────────────────────────────────────────


def test_house_cannot_read_resident_scopes() -> None:
    client, fake = _ready()
    for scope in (RESIDENT_SCOPE, "resident-other", "", "global"):
        with pytest.raises(MemoryRefused):
            client.recall("성향", scope=scope, reader="house")
    assert fake.verbs() == ["use"]


def test_house_view_recalls_only_its_own_scope_with_scope_flag() -> None:
    client, fake = _ready()
    house = HouseMemory(client, "r1")
    house.recall("환기")
    argv = fake.calls[-1][0]
    assert argv[1] == "recall" and argv[argv.index("--scope") + 1] == "house-r1"
    assert not any(a.startswith("resident") for a in argv)


def test_cards_from_another_scope_are_dropped() -> None:
    client, fake = _ready()
    fake.cards = [_card("환기 도움", "house-r1"), _card("소음 싫음", RESIDENT_SCOPE),
                  _card("다른 실행", "house-r2"), _card("범위 없음", None)]  # type: ignore[arg-type]
    cards = HouseMemory(client, "r1").recall("환기")
    assert [c.title for c in cards] == ["환기 도움"]


def test_card_keeps_only_title_and_one_line_summary() -> None:
    client, fake = _ready()
    fake.cards = [_card("환기 도움", "house-r1", binding=True)]
    (card,) = HouseMemory(client, "r1").recall("환기")
    assert card == MemoryCard(title="환기 도움", summary="환기 도움 요약 둘째 줄", kind="episode",
                              rule=True)
    assert set(vars(card)) == {"title", "summary", "kind", "rule"}


def test_resident_view_reads_only_resident_default() -> None:
    client, fake = _ready()
    fake.cards = [_card("아침 환기 좋아함", RESIDENT_SCOPE), _card("집 기록", "house-r1")]
    cards = ResidentMemory(client).recall("환기")
    assert [c.title for c in cards] == ["아침 환기 좋아함"]
    with pytest.raises(MemoryRefused):
        client.recall("환기", scope="house-r1", reader="resident")


def test_house_scopes_differ_per_run_and_reject_bad_ids() -> None:
    assert house_scope("r1") != house_scope("r2")
    for bad in ("", "a/b", "x" * 65, "../x", "a b"):
        with pytest.raises(ValueError):
            house_scope(bad)


# ── remember ─────────────────────────────────────────────────────────────


def test_remember_args_rule_and_kind() -> None:
    client, fake = _ready()
    house = HouseMemory(client, "r1")
    assert house.remember("공기청정기 제안은 그만해 달라고 했다", reason="[그만해]", rule=True) == "OP123"
    argv = fake.calls[-1][0]
    assert argv[:3] == ["memgineering", "remember", "공기청정기 제안은 그만해 달라고 했다"]
    assert argv[argv.index("--scope") + 1] == "house-r1"
    assert argv[argv.index("--reason") + 1] == "[그만해]"
    assert "--rule" in argv
    house.remember("환기 제안이 도움됐다", reason="[도움됐어요]", kind="episode")
    argv = fake.calls[-1][0]
    assert "--rule" not in argv and argv[argv.index("--kind") + 1] == "episode"


@pytest.mark.parametrize("text", [
    "보호자 010-1234-5678 로 연락", "light.living_room 을 껐다", "좌표 37.56789",
    f"{TRAIT_WORDS[0]} 거주자는 환기를 싫어한다",
])
def test_remember_goes_through_egress_and_trait_check(text: str) -> None:
    client, fake = _ready()
    with pytest.raises(MemoryRefused):
        client.remember(text, scope="house-r1", reason="r")
    assert fake.verbs() == ["use"]


def test_remember_reason_is_checked_too() -> None:
    client, fake = _ready()
    with pytest.raises(MemoryRefused):
        client.remember("환기 좋았다", scope="house-r1", reason="010-1234-5678")
    assert fake.verbs() == ["use"]


def test_recall_query_goes_through_egress() -> None:
    client, fake = _ready()
    with pytest.raises(MemoryRefused):
        client.recall("010-1234-5678", scope="house-r1", reader="house")
    assert fake.verbs() == ["use"]


@pytest.mark.parametrize("kwargs", [
    {"text": "--rule", "scope": "house-r1"},
    {"text": " ", "scope": "house-r1"},
    {"text": "환기", "scope": "global"},
    {"text": "환기", "scope": "house-r1", "kind": "secret"},
])
def test_remember_rejects_bad_arguments(kwargs: dict[str, Any]) -> None:
    client, fake = _ready()
    with pytest.raises(MemoryRefused):
        client.remember(reason="r", **kwargs)
    assert fake.verbs() == ["use"]


@pytest.mark.parametrize("how", ["timeout", "exit", "garbage"])
def test_failures_continue_without_memory(how: str) -> None:
    client, fake = _ready()
    fake.fail["recall"] = how
    fake.fail["remember"] = how
    assert client.recall("환기", scope="house-r1", reader="house") == []
    assert client.remember("환기 좋았다", scope="house-r1", reason="r") is None
    assert [e["ok"] for e in client.log] == [True, False, False]
    assert client.enabled is True  # 한 번 실패로 실행 전체를 끄지 않는다


def test_log_has_no_sentence_or_card_content() -> None:
    client, fake = _ready()
    fake.cards = [_card("환기 도움", "house-r1")]
    client.recall("환기 질의", scope="house-r1", reader="house")
    client.remember("비밀스러운 문장", scope="house-r1", reason="이유 문장")
    dumped = json.dumps(client.log, ensure_ascii=False)
    for text in ("환기 도움", "환기 질의", "비밀스러운 문장", "이유 문장"):
        assert text not in dumped


# ── 거주자 성향 심기 ──────────────────────────────────────────────────────


def test_seed_plants_once_and_skips_existing() -> None:
    client, fake = _ready()
    fake.cards = [_card("아침 창문 환기를 좋아한다", RESIDENT_SCOPE)]
    planted = ResidentMemory(client).seed(
        ["아침 창문 환기를 좋아한다", "공기청정기 소음을 싫어한다"], reason="시뮬레이션 성향")
    assert planted == 1
    remembers = [argv for argv, _, _ in fake.calls if argv[1] == "remember"]
    assert len(remembers) == 1 and remembers[0][2] == "공기청정기 소음을 싫어한다"
    assert remembers[0][remembers[0].index("--scope") + 1] == RESIDENT_SCOPE
    assert remembers[0][remembers[0].index("--kind") + 1] == "preference"


# ── JSON 형식 ─────────────────────────────────────────────────────────────


def test_parse_cli_json_tolerates_leading_notice_and_pretty_output() -> None:
    assert parse_cli_json(NOTICE + json.dumps(USE_DEMO)) == USE_DEMO
    assert parse_cli_json(json.dumps(USE_DEMO, indent=2)) == USE_DEMO
    assert parse_cli_json(NOTICE + json.dumps(USE_DEMO, indent=2)) == USE_DEMO
    assert parse_cli_json("") is None
    assert parse_cli_json("text only") is None
    assert parse_cli_json("[1, 2]") is None


def test_saved_baseline_use_shape_is_understood_as_not_the_demo_brain(tmp_path: Path) -> None:
    # 기본 home 의 실제 use 출력 모양(이름만 바꿔 둠) — 데모 브레인이 아니므로 꺼져야 한다.
    other = {"hosted": {"name": "개인 브레인", "id": "x", "api_url": "https://api.example",
                        "signed_in": True, "sign_in_pending": False},
             "destination": "hosted", "brain": None, "via": None, "pointer": None}
    client = MemoryClient(home=HOME, runner=FakeCLI(use=other))
    assert client.check_ready() is False and "다른 브레인" in (client.disabled_reason_ko or "")


# ── 실제 subprocess 경로(가짜 실행 파일) ─────────────────────────────────────


def _fake_executable(tmp_path: Path, body: str) -> Path:
    script = tmp_path / "memgineering"
    script.write_text(f"#!{sys.executable}\n{body}", encoding="utf-8")
    script.chmod(script.stat().st_mode | stat.S_IXUSR)
    return script


def test_default_runner_passes_env_and_reads_json(tmp_path: Path) -> None:
    script = _fake_executable(tmp_path, (
        "import json, os, sys\n"
        "print('## notice')\n"
        "print(json.dumps({'hosted': {'name': 'liot-demo', 'signed_in': True},\n"
        "                  'destination': 'hosted',\n"
        "                  'home': os.environ.get('MEMGINEERING_HOME'),\n"
        "                  'no_update': os.environ.get('MEMGINEERING_NO_UPDATE'),\n"
        "                  'argv': sys.argv[1:]}))\n"
    ))
    client = MemoryClient(home=HOME, executable=str(script))
    data = client._call("use", [])
    assert data is not None
    assert data["home"] == str(HOME) and data["no_update"] == "1"
    assert data["argv"] == ["use", "--json"]
    assert client.check_ready() is True


def test_default_runner_timeout_continues(tmp_path: Path) -> None:
    script = _fake_executable(tmp_path, "import time\ntime.sleep(5)\n")
    client = MemoryClient(home=HOME, executable=str(script), timeout_s=0.2)
    assert client.check_ready() is False
    assert client.log[-1]["error"] == "timeout"


def test_missing_executable_continues(tmp_path: Path) -> None:
    client = MemoryClient(home=HOME, executable=str(tmp_path / "nope"))
    assert client.check_ready() is False
    assert client.log[-1]["error"].startswith("spawn:")


# ── 리뷰 B3/B4 MEM-DECODE: 이상한 CLI 출력·실행기 예외에도 기억 없이 계속 ─────────────


def test_undecodable_cli_output_disables_memory_without_raising(tmp_path: Path) -> None:
    script = _fake_executable(tmp_path, (
        "import sys\n"
        "sys.stdout.buffer.write(b'\\xff\\xfe\\x80 broken\\n')\n"
        "sys.stdout.buffer.write(b'{\"hosted\": {\"name\": \"liot-demo\", \"signed_in\": true}, '\n"
        "                        b'\"destination\": \"hosted\", \"note\": \"\\xff\"}\\n')\n"
    ))
    client = MemoryClient(home=HOME, executable=str(script))
    # 못 푸는 바이트는 바꿔 넣고 읽는다 — JSON 줄은 그대로 읽혀 확인이 통과한다.
    assert client.check_ready() is True
    (tmp_path / "g").mkdir()
    garbage = _fake_executable(tmp_path / "g", "import sys\nsys.stdout.buffer.write(b'\\xff\\xfe\\xfa')\n")
    other = MemoryClient(home=HOME, executable=str(garbage))
    assert other.check_ready() is False and other.log[-1]["error"] == "not json"


@pytest.mark.parametrize("exc", [
    UnicodeDecodeError("utf-8", b"\xff", 0, 1, "invalid start byte"),
    RuntimeError("boom"),
    ValueError("bad"),
])
def test_runner_exceptions_never_escape(exc: Exception) -> None:
    class Raising(FakeCLI):
        raise_on: set[str] = set()

        def __call__(self, argv: Sequence[str], env: Mapping[str, str],
                     timeout_s: float) -> subprocess.CompletedProcess[str]:
            if list(argv)[1] in self.raise_on:
                raise exc
            return super().__call__(argv, env, timeout_s)

    fake = Raising()
    fake.raise_on = {"use"}
    client = MemoryClient(home=HOME, runner=fake)
    assert client.check_ready() is False
    assert client.log[-1]["error"] == f"runner:{type(exc).__name__}"
    fake.raise_on = {"recall", "remember"}
    ready = MemoryClient(home=HOME, runner=fake)
    assert ready.check_ready() is True
    assert ready.recall("환기", scope="house-r1", reader="house") == []
    assert ready.remember("환기 좋았다", scope="house-r1", reason="r") is None


def test_deeply_nested_json_is_not_a_recursion_error() -> None:
    assert parse_cli_json("[" * 100_000 + "]" * 100_000) is None
    deep = "{\"a\": " * 100_000 + "1" + "}" * 100_000
    assert parse_cli_json(deep) is None  # RecursionError 를 형식 오류로 친다
