"""데모 서버 견고성 — 악의적/잘못된 요청이 서버를 갉아먹지 못한다.

2026-08-29 코드리뷰가 찾은 세 가지를 고정한다. 셋 다 실측으로 재현됐다:

  1) **Content-Length 신뢰** — 부호 검사도 상한도 없어서 `Content-Length: -1`
     이면 `rfile.read(-1)` 이 EOF 까지 읽으려 하고, 연결이 열려 있는 한 핸들러
     스레드가 영원히 묶였다(반쯤 열린 요청 5개로 스레드 2 → 7, 전부 응답 없음).
     `ThreadingHTTPServer` 는 연결당 스레드라 그대로 스레드 고갈이다.
  2) **본문 타입 미검증** — `_read_json` 은 `dict | None` 을 선언했지만 JSON 이
     주는 대로 돌려줬다. 배열 본문(`["a"]`)이 오면 호출부의 `body.get(...)` 이
     AttributeError 로 터져 응답 없이 연결이 끊겼다(3개 엔드포인트).
  3) **정적 파일 경계** — 문자열 접두사 비교라 `web` 로 시작하는 형제 디렉터리가
     통과했다. `/../web_secret/leak.html` 이 200 으로 내용까지 새어 나갔다.
"""

from __future__ import annotations

import socket
import threading
import time
from contextlib import contextmanager
from http.server import ThreadingHTTPServer
from pathlib import Path

import pytest

from liot import demo_server


@contextmanager
def _serve(timeout: int | None = None):
    """임시 데모 서버(루프백). timeout 을 주면 Handler 소켓 타임아웃을 덮어쓴다.

    ``shutdown()`` 은 serve_forever 의 폴링 간격만큼 기다리므로, 테스트마다
    서버를 새로 띄우면 그 대기가 그대로 쌓인다(29개 × 0.5초 = 15초). 기본
    타임아웃을 쓰는 테스트는 아래 모듈 스코프 ``port`` 픽스처를 공유한다.
    """
    original = demo_server.Handler.timeout
    if timeout is not None:
        demo_server.Handler.timeout = timeout
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), demo_server.Handler)
    thread = threading.Thread(target=httpd.serve_forever, kwargs={"poll_interval": 0.05},
                              daemon=True)
    thread.start()
    try:
        yield httpd.server_address[1]
    finally:
        httpd.shutdown()
        httpd.server_close()
        thread.join(timeout=5)
        demo_server.Handler.timeout = original


@pytest.fixture(scope="module")
def port():
    """모듈 전체가 공유하는 서버(기본 타임아웃)."""
    with _serve() as p:
        yield p


def _raw(port: int, request: bytes, *, wait: float = 3.0, hold: list[socket.socket] | None = None) -> str:
    """생 HTTP 요청을 던지고 응답 전체를 돌려준다(응답 없으면 빈 문자열).

    ``recv`` 한 번으로 끝내면 안 된다 — 헤더와 본문이 다른 TCP 세그먼트로
    나뉘어 오면 본문을 못 본 채 단언이 깨진다(실제로 겪었다). 서버가 HTTP/1.0
    으로 응답 후 연결을 닫으므로 EOF 까지 읽는 게 정확하다.
    """
    sock = socket.socket()
    sock.settimeout(wait)
    sock.connect(("127.0.0.1", port))
    sock.sendall(request)
    chunks: list[bytes] = []
    try:
        while True:
            chunk = sock.recv(8192)
            if not chunk:
                break
            chunks.append(chunk)
    except TimeoutError:
        pass
    if hold is not None:
        hold.append(sock)  # 일부러 열어 둔다(반쯤 열린 연결 재현)
    else:
        sock.close()
    return b"".join(chunks).decode("utf-8", "replace")


def _post(path: str, body: bytes, *, content_length: str | None = None) -> bytes:
    length = content_length if content_length is not None else str(len(body))
    head = (
        f"POST {path} HTTP/1.1\r\nHost: x\r\n"
        f"Content-Type: application/json\r\nContent-Length: {length}\r\n\r\n"
    )
    return head.encode("ascii") + body


@pytest.fixture(autouse=True)
def _clean_state():
    demo_server._REGISTRY.reset()
    demo_server._PREF_CELLS.clear()
    yield
    demo_server._REGISTRY.reset()
    demo_server._PREF_CELLS.clear()


# ───────────────────────────────── 1) Content-Length 를 믿지 않는다
@pytest.mark.parametrize(
    "length,expect_in_body",
    [
        ("-1", "허용 범위"),  # 음수 → read(-1) = EOF 까지
        ("100000000", "허용 범위"),  # 상한 초과
        ("abc", "Content-Length"),  # 숫자가 아님
    ],
)
def test_bad_content_length_is_rejected(port, length: str, expect_in_body: str) -> None:
    resp = _raw(port, _post("/api/feedback", b"", content_length=length))
    assert "400" in resp, resp[:200]
    assert expect_in_body in resp, resp[:200]


def test_handler_declares_a_socket_timeout() -> None:
    """기본 소켓 타임아웃이 실제로 설정돼 있어야 한다.

    아래 동작 테스트는 빨리 끝내려고 타임아웃을 1초로 덮어쓴다. 그래서 클래스
    기본값이 None 으로 되돌아가도 그 테스트는 통과한다(실측: mutation 이
    안 잡혔다). 기본값 자체는 여기서 고정한다 — 없으면 헤더만 보내고 본문을
    안 보내는 연결이 스레드를 영원히 붙든다.
    """
    timeout = demo_server.Handler.timeout
    assert isinstance(timeout, (int, float)) and not isinstance(timeout, bool), timeout
    assert 0 < timeout <= 120, f"기본 소켓 타임아웃이 비정상: {timeout}"


def test_short_body_does_not_hang_the_handler() -> None:
    """선언한 길이보다 본문이 짧으면 타임아웃 뒤 400 — 무한 대기하지 않는다."""
    with _serve(timeout=1) as port:
        started = time.monotonic()
        resp = _raw(port, _post("/api/feedback", b"short", content_length="5000"), wait=10)
        elapsed = time.monotonic() - started
    assert "400" in resp, resp[:200]
    assert elapsed < 8, f"응답까지 {elapsed:.1f}초 — 타임아웃이 걸리지 않았다"


def test_half_open_requests_do_not_leak_threads() -> None:
    """응답 뒤 연결을 열어 둬도 핸들러 스레드가 남지 않는다(스레드 고갈 방어)."""
    held: list[socket.socket] = []
    with _serve() as leak_port:
        baseline = threading.active_count()
        for _ in range(8):
            _raw(leak_port, _post("/api/feedback", b"", content_length="-1"), hold=held)
        time.sleep(0.5)  # 핸들러 스레드가 정리될 여유
        leaked = threading.active_count() - baseline
    for sock in held:
        sock.close()
    assert leaked <= 1, f"반쯤 열린 연결 8개 뒤 스레드가 {leaked}개 남았다"


# ───────────────────────────────── 2) 본문은 JSON '객체' 여야 한다
@pytest.mark.parametrize(
    "path",
    [
        "/api/directive",
        "/api/feedback",
        "/api/registry/sensor",
        "/api/registry/preset",
        "/api/simulate",
    ],
)
@pytest.mark.parametrize("body", [b'["a"]', '"문자열"'.encode(), b"123", b"null"])
def test_non_object_body_is_rejected(port, path: str, body: bytes) -> None:
    resp = _raw(port, _post(path, body))
    assert "400" in resp, resp[:200]
    assert "JSON 객체" in resp, resp[:200]


def test_valid_object_body_still_works(port) -> None:
    """게이트가 정상 요청까지 막지 않는다."""
    body = b'{"cell":"evening|vacant|light:turn_off","signal":"accepted"}'
    resp = _raw(port, _post("/api/feedback", body))
    assert "200 OK" in resp, resp[:200]


# ───────────────────────────────── 3) 정적 파일은 web/ 밖으로 못 나간다
def test_sibling_directory_starting_with_web_is_not_served() -> None:
    """`web` 로 시작하는 형제 디렉터리는 접두사만 같을 뿐 web/ 안이 아니다."""
    sibling = Path(demo_server.WEB_DIR).parent / "web_secret_probe"
    sibling.mkdir(exist_ok=True)
    leak = sibling / "leak.html"
    leak.write_text("TOP-SECRET-LEAK", encoding="utf-8")
    try:
        with _serve() as probe_port:
            resp = _raw(probe_port,
                        b"GET /../web_secret_probe/leak.html HTTP/1.1\r\nHost: x\r\n\r\n")
    finally:
        leak.unlink()
        sibling.rmdir()
    assert "404" in resp, resp[:200]
    assert "TOP-SECRET-LEAK" not in resp, "web/ 밖 파일이 새어 나갔다"


def test_parent_traversal_is_not_served(port) -> None:
    resp = _raw(port, b"GET /../pyproject.toml HTTP/1.1\r\nHost: x\r\n\r\n")
    assert "404" in resp, resp[:200]
    assert "[project]" not in resp


def test_real_static_file_is_still_served(port) -> None:
    resp = _raw(port, b"GET /app.html HTTP/1.1\r\nHost: x\r\n\r\n")
    assert "200 OK" in resp, resp[:200]
