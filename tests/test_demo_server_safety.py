"""관제실 안전 설정 화면(T9) — 거주자·보호자·동의·전달 기록 API 와 파급도 표시.

지키는 것:
  - 값은 데몬과 같은 DB(LIOT_DB_PATH, 기본값은 liot.serve 한 곳)에 SafetyRepo 로만 쓴다.
  - "혼자 대피할 수 있나요? 아니요" 는 cannot_self_evacuate=True 로 저장된다.
  - 인지 저하는 보호자만 입력할 수 있다. 모르는 항목(생년월일 등)은 받지 않는다.
  - 보호자 비밀 토픽은 등록 응답에 한 번만 — 목록·이력 응답에는 절대 없다.
  - /api/safety/* 는 이 기기(루프백) 접속만 받는다.

모든 테스트는 LIOT_DB_PATH 를 tmp 파일로 돌린다(작업 디렉터리에 liot.db 를 만들지 않게).
네트워크는 127.0.0.1 루프백만 쓴다.
"""

from __future__ import annotations

import asyncio
import json
import socket
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

import pytest

from liot import demo_server, serve
from liot.app import LiotApp
from liot.core.severity import SeverityLevel
from liot.db.database import init_db
from liot.db.safety_repo import PERSON_TRAITS, SafetyRepo

pytestmark = pytest.mark.usefixtures("demo_state_reset")


# ------------------------------------------------------------------ 픽스처·도우미
@pytest.fixture()
def db_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    path = tmp_path / "shared.db"
    monkeypatch.setenv("LIOT_DB_PATH", str(path))
    monkeypatch.delenv("LIOT_HOME_ID", raising=False)
    return path


@pytest.fixture()
def server(demo_serve, db_path):
    with demo_serve() as port:
        yield f"http://127.0.0.1:{port}"


def _req(
    base: str, path: str, payload: dict[str, Any] | None = None
) -> tuple[int, dict[str, Any], str]:
    """(상태, JSON, 원문). payload 가 있으면 POST(JSON)."""
    data = None if payload is None else json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        base + path,
        data=data,
        headers={"Content-Type": "application/json"} if data is not None else {},
        method="GET" if data is None else "POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:  # noqa: S310 - 루프백
            raw = resp.read().decode("utf-8")
            return resp.status, json.loads(raw), raw
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode("utf-8")
        return exc.code, json.loads(raw), raw


def _repo_read(path: Path, fn: Any) -> Any:
    """같은 DB 파일을 SafetyRepo 로 직접 열어 확인한다(서버를 거치지 않는 독립 확인)."""

    async def go() -> Any:
        db = await init_db(str(path))
        try:
            return await fn(SafetyRepo(db), db)
        finally:
            await db.close()

    return asyncio.run(go())


def _add_resident(base: str, alias: str) -> str:
    status, body, _ = _req(base, "/api/safety/residents", {"alias": alias})
    assert status == 200, body
    rid: str = body["resident"]["resident_id"]
    return rid


def _add_guardian(base: str, alias: str) -> tuple[str, str]:
    status, body, _ = _req(base, "/api/safety/guardians", {"alias": alias})
    assert status == 200, body
    return body["guardian"]["guardian_id"], body["topic"]


# ------------------------------------------------------------------ 거주자
def test_questions_map_exactly_the_person_traits() -> None:
    """화면 질문 3개가 저장 특성 3종과 정확히 짝을 이룬다(빠짐·덧붙임 없음)."""
    assert {q[1] for q in demo_server._QUESTIONS} == set(PERSON_TRAITS)


def test_add_resident_and_set_traits_round_trip(server, db_path) -> None:
    rid = _add_resident(server, "할머니")
    status, body, _ = _req(
        server,
        "/api/safety/residents/traits",
        {
            "resident_id": rid,
            "entered_by": "resident",
            "answers": {"age_75_plus": True, "can_self_evacuate": False},
        },
    )
    assert status == 200, body
    answers = body["resident"]["answers"]
    assert answers["age_75_plus"]["answer"] is True
    assert answers["can_self_evacuate"]["answer"] is False
    assert answers["can_self_evacuate"]["entered_by_ko"] == "거주자 본인"
    assert answers["cognitive_decline"]["answer"] is None

    # DB 에는 '아니요' 가 뒤집혀 cannot_self_evacuate=True 로, 입력자와 함께 저장된다.
    async def traits(repo: SafetyRepo, _db: Any) -> Any:
        res = await repo.get_resident(rid)
        assert res is not None
        return res.traits

    stored = _repo_read(db_path, traits)
    assert stored["cannot_self_evacuate"].value is True
    assert stored["cannot_self_evacuate"].entered_by == "resident"
    assert stored["age_75_plus"].value is True
    assert "cognitive_decline" not in stored

    status, body, _ = _req(server, "/api/safety/residents")
    assert status == 200
    assert [r["alias"] for r in body["residents"]] == ["할머니"]


def test_cognitive_decline_from_resident_is_rejected_and_nothing_written(server, db_path) -> None:
    rid = _add_resident(server, "할아버지")
    status, body, _ = _req(
        server,
        "/api/safety/residents/traits",
        {
            "resident_id": rid,
            "entered_by": "resident",
            "answers": {"age_75_plus": True, "cognitive_decline": True},
        },
    )
    assert status == 403
    assert body["ok"] is False and body["field"] == "cognitive_decline"
    assert "보호자" in body["error"]

    async def traits(repo: SafetyRepo, _db: Any) -> Any:
        res = await repo.get_resident(rid)
        assert res is not None
        return res.traits

    # 같은 요청의 다른 답(age_75_plus)도 저장되지 않는다 — 전부 검사한 뒤에만 쓴다.
    assert _repo_read(db_path, traits) == {}

    status, body, _ = _req(
        server,
        "/api/safety/residents/traits",
        {"resident_id": rid, "entered_by": "guardian", "answers": {"cognitive_decline": True}},
    )
    assert status == 200, body
    cd = body["resident"]["answers"]["cognitive_decline"]
    assert cd["answer"] is True and cd["entered_by"] == "guardian"


@pytest.mark.parametrize(
    "answers",
    [
        {"birth_date": "1940-01-01"},
        {"diagnosis": "치매"},
        {"cannot_self_evacuate": True},  # 저장 키 직접 입력은 받지 않는다(질문 답만)
        {"age_75_plus": 1},  # 예/아니오만
        {},
    ],
)
def test_traits_reject_unknown_or_non_boolean_answers(server, db_path, answers) -> None:
    rid = _add_resident(server, "엄마")
    status, body, _ = _req(
        server,
        "/api/safety/residents/traits",
        {"resident_id": rid, "entered_by": "guardian", "answers": answers},
    )
    assert status == 400, body
    assert body["ok"] is False


def test_traits_require_known_entered_by_and_resident(server) -> None:
    rid = _add_resident(server, "아빠")
    status, body, _ = _req(
        server,
        "/api/safety/residents/traits",
        {"resident_id": rid, "entered_by": "admin", "answers": {"age_75_plus": True}},
    )
    assert status == 400 and body["field"] == "entered_by"
    status, body, _ = _req(
        server,
        "/api/safety/residents/traits",
        {"resident_id": "nope", "entered_by": "resident", "answers": {"age_75_plus": True}},
    )
    assert status == 404


def test_delete_requires_confirm_then_purges_traits(server, db_path) -> None:
    rid = _add_resident(server, "삼촌")
    _req(
        server,
        "/api/safety/residents/traits",
        {"resident_id": rid, "entered_by": "guardian",
         "answers": {"age_75_plus": True, "cognitive_decline": True}},
    )
    status, body, _ = _req(server, "/api/safety/residents/delete", {"resident_id": rid})
    assert status == 400 and body["field"] == "confirm"

    status, body, _ = _req(
        server, "/api/safety/residents/delete", {"resident_id": rid, "confirm": True}
    )
    assert status == 200 and body["deleted"] == rid

    async def leftovers(_repo: SafetyRepo, db: Any) -> int:
        cur = await db.execute("SELECT COUNT(*) FROM resident_traits WHERE resident_id = ?", (rid,))
        row = await cur.fetchone()
        await cur.close()
        return int(row[0])

    assert _repo_read(db_path, leftovers) == 0
    status, body, _ = _req(server, "/api/safety/residents")
    assert body["residents"] == []
    status, _, _ = _req(
        server, "/api/safety/residents/delete", {"resident_id": rid, "confirm": True}
    )
    assert status == 404


def test_home_alias_and_lives_alone(server, db_path) -> None:
    status, body, _ = _req(server, "/api/safety/home")
    assert status == 200
    assert body["home"] == {"home_alias": "우리 집", "lives_alone": False}

    status, body, _ = _req(
        server, "/api/safety/home", {"home_alias": "부모님 댁", "lives_alone": True}
    )
    assert status == 200, body
    assert body["home"] == {"home_alias": "부모님 댁", "lives_alone": True}

    async def settings(repo: SafetyRepo, _db: Any) -> Any:
        return await repo.get_home_settings("singleton")

    assert tuple(_repo_read(db_path, settings)) == (True, "부모님 댁")

    for bad in ({}, {"home_alias": "  "}, {"lives_alone": "yes"}):
        status, body, _ = _req(server, "/api/safety/home", bad)
        assert status == 400, (bad, body)


# ------------------------------------------------------------------ 보호자
def test_guardian_topic_is_returned_once_and_never_listed(server) -> None:
    gid, topic = _add_guardian(server, "큰딸")
    assert len(topic) >= 20

    rid = _add_resident(server, "할머니")
    _req(server, "/api/safety/consent",
         {"guardian_id": gid, "by_resident_id": rid, "action": "grant"})

    for path in (
        "/api/safety/guardians",
        "/api/safety/residents",
        "/api/safety/home",
        "/api/safety/consent-history",
        "/api/safety/forwards",
    ):
        status, body, raw = _req(server, path)
        assert status == 200, path
        assert topic not in raw, path
        assert "topic" not in raw, path

    status, body, _ = _req(server, "/api/safety/guardians")
    (g,) = body["guardians"]
    assert set(g) == {"guardian_id", "alias", "created_at", "active", "consented"}


def test_deactivate_guardian(server) -> None:
    gid, topic = _add_guardian(server, "아들")
    status, body, raw = _req(server, "/api/safety/guardians/deactivate", {"guardian_id": gid})
    assert status == 200 and body["changed"] is True
    assert topic not in raw
    assert body["guardians"][0]["active"] is False

    status, body, _ = _req(server, "/api/safety/guardians/deactivate", {"guardian_id": gid})
    assert status == 200 and body["changed"] is False
    status, _, _ = _req(server, "/api/safety/guardians/deactivate", {"guardian_id": "nope"})
    assert status == 404

    # 사용 중지된 보호자에게는 동의할 수 없다.
    rid = _add_resident(server, "할머니")
    status, body, _ = _req(server, "/api/safety/consent",
                           {"guardian_id": gid, "by_resident_id": rid, "action": "grant"})
    assert status == 400 and body["field"] == "guardian_id"


# ------------------------------------------------------------------ 동의
def test_grant_and_revoke_by_different_residents_with_history(server) -> None:
    gid, _ = _add_guardian(server, "큰딸")
    grandma = _add_resident(server, "할머니")
    grandpa = _add_resident(server, "할아버지")

    status, body, _ = _req(server, "/api/safety/consent",
                           {"guardian_id": gid, "by_resident_id": grandma, "action": "grant"})
    assert status == 200, body
    assert body["guardians"][0]["consented"] is True

    status, body, _ = _req(server, "/api/safety/consent",
                           {"guardian_id": gid, "by_resident_id": grandpa, "action": "revoke"})
    assert status == 200, body
    assert body["guardians"][0]["consented"] is False

    status, body, _ = _req(server, "/api/safety/consent-history")
    assert status == 200
    rows = body["history"]  # 최근 것이 위
    assert [(r["event_ko"], r["by_resident_alias"], r["guardian_alias"]) for r in rows] == [
        ("동의 철회", "할아버지", "큰딸"),
        ("전달 동의", "할머니", "큰딸"),
    ]

    # 이력은 거주자가 삭제돼도 남고, 별칭은 비어 '삭제된 사람'으로 보인다.
    _req(server, "/api/safety/residents/delete", {"resident_id": grandma, "confirm": True})
    status, body, _ = _req(server, "/api/safety/consent-history")
    assert body["history"][1]["by_resident_alias"] is None
    assert body["history"][1]["by_resident_id"] == grandma


def test_consent_rejects_non_resident_and_bad_input(server) -> None:
    gid, _ = _add_guardian(server, "큰딸")
    rid = _add_resident(server, "할머니")
    status, body, _ = _req(server, "/api/safety/consent",
                           {"guardian_id": gid, "by_resident_id": "stranger", "action": "grant"})
    assert status == 403 and body["field"] == "by_resident_id"
    status, body, _ = _req(server, "/api/safety/consent",
                           {"guardian_id": "nope", "by_resident_id": rid, "action": "grant"})
    assert status == 404
    status, body, _ = _req(server, "/api/safety/consent",
                           {"guardian_id": gid, "by_resident_id": rid, "action": "maybe"})
    assert status == 400 and body["field"] == "action"


# ------------------------------------------------------------------ 전달 기록
def test_forwarding_history_shows_when_what_and_whom(server, db_path) -> None:
    gid, topic = _add_guardian(server, "큰딸")

    async def seed(repo: SafetyRepo, _db: Any) -> None:
        sid, _ = await repo.open_or_get_situation(
            "singleton", "gas", "binary_sensor.kitchen_gas", SeverityLevel.CRITICAL,
            "2026-10-19T05:00:00+00:00",
        )
        await repo.record_forward(sid, gid, "forward", "2026-10-19T05:01:00+00:00")
        await repo.record_forward(sid, None, "guidance_119", "2026-10-19T05:02:00+00:00")

    _repo_read(db_path, seed)
    status, body, raw = _req(server, "/api/safety/forwards")
    assert status == 200
    assert topic not in raw
    first, second = body["forwards"]  # 최근 것이 위
    assert first["kind_ko"] == "거주자에게 119 안내" and first["guardian_id"] is None
    assert second["situation_ko"] == "가스 누출"
    assert second["kind_ko"] == "보호자에게 알림 전달"
    assert second["guardian_alias"] == "큰딸"
    assert second["at"] == "2026-10-19T05:01:00+00:00"


# ------------------------------------------------------------------ 영속·기본 경로
def test_data_persists_across_two_server_instances(demo_serve, db_path) -> None:
    with demo_serve() as port:
        rid = _add_resident(f"http://127.0.0.1:{port}", "할머니")
    assert db_path.exists()
    with demo_serve() as port:
        status, body, _ = _req(f"http://127.0.0.1:{port}", "/api/safety/residents")
    assert status == 200
    assert [r["resident_id"] for r in body["residents"]] == [rid]


def test_default_db_path_is_shared_with_daemon(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("LIOT_DB_PATH", raising=False)
    assert serve.DEFAULT_DB_PATH == "liot.db"
    assert serve.resolve_db_path() == serve.DEFAULT_DB_PATH
    monkeypatch.setenv("LIOT_DB_PATH", "   ")
    assert serve.resolve_db_path() == serve.DEFAULT_DB_PATH
    # 데모 서버는 자기 사본이 아니라 데몬의 함수를 그대로 쓴다.
    assert vars(demo_server)["resolve_db_path"] is serve.resolve_db_path

    # 기본값을 한 곳에서 바꾸면 데모 서버가 쓰는 파일도 따라 바뀐다.
    monkeypatch.delenv("LIOT_DB_PATH", raising=False)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(serve, "DEFAULT_DB_PATH", "moved-default.db")
    result, code = demo_server.safety_call("POST", "/api/safety/residents", {"alias": "할머니"})
    assert code == 200, result
    assert (tmp_path / "moved-default.db").exists()


async def test_daemon_opens_resolved_default_db_path(monkeypatch: pytest.MonkeyPatch) -> None:
    """데몬(_amain)도 같은 resolve_db_path 로 DB 를 연다(하드코딩 경로 없음)."""
    from liot.service import LiotService

    for key in ("LIOT_DB_PATH", "LIOT_HA_URL", "LIOT_HA_TOKEN", "LIOT_NTFY_URL",
                "LIOT_EXECUTION_ENABLED", "LIOT_LLM_BACKEND"):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("LIOT_NOTIFY_SECRET", "a-real-long-signing-secret-value")
    monkeypatch.setenv("LIOT_FEEDBACK_PORT", "0")
    monkeypatch.setattr(serve, "DEFAULT_DB_PATH", "probe-default.db")

    seen: list[str] = []
    real_init_db = init_db

    async def spy_init_db(path: str, durable: bool = False) -> Any:
        seen.append(path)
        return await real_init_db(":memory:", durable=durable)

    async def fake_run(
        self: Any, bridge: Any = None, *, ha_factory: Any = None, tick_seconds: float = 0.0
    ) -> None:
        return None

    monkeypatch.setattr(serve, "init_db", spy_init_db)
    monkeypatch.setattr(LiotService, "run", fake_run)
    assert await serve._amain() == 0
    assert seen == ["probe-default.db"]


# ------------------------------------------------------------------ 이 기기 전용
@pytest.mark.parametrize(
    ("ip", "host", "origin", "ctype", "write", "blocked"),
    [
        ("127.0.0.1", "127.0.0.1:8765", None, None, False, False),
        ("::1", "[::1]:8765", None, None, False, False),
        ("::ffff:127.0.0.1", "localhost:8765", None, None, False, False),
        ("127.0.0.1", "localhost", "http://localhost:8765", "application/json", True, False),
        ("192.168.0.20", "localhost:8765", None, None, False, True),  # 다른 기기(Host 를 속여도)
        ("127.0.0.1", "evil.example:8765", None, None, False, True),  # DNS 리바인딩
        ("127.0.0.1", None, None, None, False, True),
        ("127.0.0.1", "127.0.0.1", "http://evil.example", "application/json", True, True),
        ("127.0.0.1", "127.0.0.1", "null", "application/json", True, True),
        ("127.0.0.1", "127.0.0.1", None, "text/plain", True, True),  # 사전 확인 없는 위조 요청
        ("127.0.0.1", "127.0.0.1", None, None, True, True),
    ],
)
def test_safety_block_reason(ip, host, origin, ctype, write, blocked) -> None:
    reason = demo_server.safety_block_reason(ip, host, origin, ctype, write=write)
    assert (reason is not None) is blocked, reason


def test_non_json_or_foreign_host_requests_are_refused_over_http(server) -> None:
    port = int(server.rsplit(":", 1)[1])

    def raw(request: bytes) -> str:
        with socket.create_connection(("127.0.0.1", port), timeout=5) as sock:
            sock.sendall(request)
            chunks = []
            while chunk := sock.recv(8192):
                chunks.append(chunk)
        return b"".join(chunks).decode("utf-8", "replace")

    body = json.dumps({"alias": "몰래"}).encode("utf-8")
    forged = (
        f"POST /api/safety/residents HTTP/1.1\r\nHost: 127.0.0.1:{port}\r\n"
        f"Content-Type: text/plain\r\nContent-Length: {len(body)}\r\n"
        "Connection: close\r\n\r\n"
    ).encode("ascii") + body
    assert " 403 " in raw(forged).split("\r\n", 1)[0]

    rebound = (
        "GET /api/safety/residents HTTP/1.1\r\nHost: evil.example\r\nConnection: close\r\n\r\n"
    ).encode("ascii")
    assert " 403 " in raw(rebound).split("\r\n", 1)[0]

    status, body_json, _ = _req(server, "/api/safety/residents")
    assert status == 200 and body_json["residents"] == []


def test_unknown_safety_route_is_404(server) -> None:
    status, body, _ = _req(server, "/api/safety/nothing")
    assert status == 404 and body["ok"] is False


def test_db_failure_is_reported_not_raised(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # 디렉터리를 DB 파일로 지정하면 열 수 없다 → 500 + 한국어 사유(연결이 끊기지 않는다).
    monkeypatch.setenv("LIOT_DB_PATH", str(tmp_path))
    result, code = demo_server.safety_call("GET", "/api/safety/residents")
    assert code == 500 and result["ok"] is False
    assert "저장소" in result["error"]


# ------------------------------------------------------------------ 파급도 표시
def test_severity_view_labels_and_sources() -> None:
    assert demo_server._severity_view(None) is None
    view = demo_server._severity_view(SeverityLevel.WARNING, ["table", "resident", "model"])
    assert view == {
        "level": 2,
        "name": "warning",
        "ko": "경고",
        "sources": ["table", "resident", "model"],
        "sources_ko": ["표", "거주자", "모델"],
    }
    assert [demo_server._severity_view(lv)["ko"] for lv in SeverityLevel] == [  # type: ignore[index]
        "기록만", "주의", "경고", "위험",
    ]


_GAS_EVENT = {
    "mode": "event",
    "config": {"time": "evening", "llm_enabled": False},
    "sensors": {"binary_sensor.kitchen_gas": True},
    "event": {"entity": "binary_sensor.kitchen_gas", "value": "on"},
}


def test_simulate_shows_no_severity_when_judgment_has_none() -> None:
    out = asyncio.run(demo_server._run(dict(_GAS_EVENT)))
    assert out["silence"] is False
    assert out["judgment"]["severity"] is None
    assert out["proposal"]["severity"] is None


def test_simulate_shows_severity_label_and_sources(monkeypatch: pytest.MonkeyPatch) -> None:
    real = LiotApp.process_reading

    async def with_severity(self: LiotApp, reading: Any, **kw: Any) -> Any:
        proposal = await real(self, reading, **kw)
        trace = self.last_trace or {}
        jd = trace.get("judgment")
        if jd is not None:
            trace["judgment"] = jd.model_copy(update={
                "severity": SeverityLevel.CRITICAL,
                "severity_floor": SeverityLevel.WARNING,
                "severity_sources": ["table", "resident", "model"],
            })
        if proposal is None:
            return None
        return proposal.model_copy(update={"severity": SeverityLevel.CRITICAL})

    monkeypatch.setattr(LiotApp, "process_reading", with_severity)
    out = asyncio.run(demo_server._run(dict(_GAS_EVENT)))
    sev = out["judgment"]["severity"]
    assert sev["ko"] == "위험" and sev["sources_ko"] == ["표", "거주자", "모델"]
    assert out["judgment"]["severity_floor"]["ko"] == "경고"
    psev = out["proposal"]["severity"]
    assert psev["ko"] == "위험" and psev["sources_ko"] == ["표", "거주자", "모델"]


def test_safety_page_is_served_and_has_no_birthdate_or_diagnosis_fields(server) -> None:
    with urllib.request.urlopen(server + "/safety.html", timeout=10) as resp:  # noqa: S310
        html = resp.read().decode("utf-8")
    with urllib.request.urlopen(server + "/assets/safety.js", timeout=10) as resp:  # noqa: S310
        js = resp.read().decode("utf-8")
    for text in (html, js):
        lowered = text.lower()
        assert 'type="date"' not in lowered
        assert "birth" not in lowered and "생년월일 입력" not in text
        assert "diagnos" not in lowered and "진단명 입력" not in text
    assert "75세 이상인가요?" in demo_server._QUESTIONS[0][3]
    # 비밀 토픽을 브라우저 저장소에 남기지 않는다.
    assert "localStorage" not in js and "sessionStorage" not in js
    # 관제실에서 이 화면으로 가는 길이 있다.
    with urllib.request.urlopen(server + "/", timeout=10) as resp:  # noqa: S310
        root_html = resp.read().decode("utf-8")
    assert 'href="safety.html"' in root_html
    # 3D 가상 집으로 가는 길도 관제실·이 화면 둘 다에 있다.
    assert 'href="home3d.html"' in root_html and 'href="home3d.html"' in html
