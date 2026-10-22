"""하루 시뮬레이션 실행 API(/api/day/*) — 실제 ThreadingHTTPServer(포트 0) 위에서.

핵심 검증:
  - 끝난 뒤 늦게 붙은 스트림도 사건 전부를 받고 마지막이 done 이며, 수가 runs/<id> 와 같다.
  - from=N · Last-Event-ID 로 다시 붙으면 빠짐도 중복도 없다.
  - 진행 중 두 번째 실행은 409, 멈추기는 stopped + done.stopped.
  - 보는 사람 응답은 큐로 들어가 다음 단계에 실제 피드백 경로로 적용된다.
  - 이 기기가 아닌 Host·Origin 은 403, 모르는 항목·범위 밖 값은 400.
"""

from __future__ import annotations

import contextlib
import http.client
import json
import threading
import time
from collections.abc import Iterator
from typing import Any

import pytest

from liot import demo_server
from liot.day_sim import DayRuns
from liot.live_runs import LiveRequestError


@pytest.fixture()
def server() -> Iterator[tuple[int, DayRuns]]:
    runs = DayRuns()
    httpd = demo_server.make_server("127.0.0.1", 0, day_runs=runs)
    thread = threading.Thread(target=httpd.serve_forever, kwargs={"poll_interval": 0.05},
                              daemon=True)
    thread.start()
    try:
        yield httpd.server_port, runs
    finally:
        with contextlib.suppress(LiveRequestError):  # 진행 중 실행이 없으면 409
            runs.stop()
        runs.join(10.0)
        httpd.shutdown()
        httpd.server_close()
        thread.join(timeout=5)


def _req(
    port: int,
    method: str,
    path: str,
    body: dict[str, Any] | None = None,
    headers: dict[str, str] | None = None,
) -> tuple[int, dict[str, Any]]:
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=30)
    hdrs = {"Host": f"127.0.0.1:{port}"}
    data = None
    if body is not None:
        data = json.dumps(body).encode("utf-8")
        hdrs["Content-Type"] = "application/json"
    hdrs.update(headers or {})
    conn.request(method, path, body=data, headers=hdrs)
    resp = conn.getresponse()
    raw = resp.read().decode("utf-8")
    conn.close()
    return resp.status, json.loads(raw)


def _stream(port: int, run_id: str, *, frm: int | None = None,
            last_id: str | None = None) -> list[dict[str, Any]]:
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=30)
    path = f"/api/day/stream?run={run_id}" + ("" if frm is None else f"&from={frm}")
    hdrs = {"Host": f"127.0.0.1:{port}"}
    if last_id is not None:
        hdrs["Last-Event-ID"] = last_id
    conn.request("GET", path, headers=hdrs)
    resp = conn.getresponse()
    assert resp.status == 200
    assert resp.getheader("Content-Type", "").startswith("text/event-stream")
    text = resp.read().decode("utf-8")
    conn.close()
    events = []
    for block in text.split("\n\n"):
        lines = [ln for ln in block.split("\n") if ln and not ln.startswith(":")]
        if lines:
            fields = dict(ln.split(": ", 1) for ln in lines)
            ev = json.loads(fields["data"])
            assert int(fields["id"]) == ev["seq"] and fields["event"] == ev["stage"]
            events.append(ev)
    return events


def _start(port: int, **body: Any) -> str:
    status, out = _req(port, "POST", "/api/day/run", {"days": 3, "seed": 7, **body})
    assert status == 200, out
    return str(out["run_id"])


def _wait_done(runs: DayRuns, run_id: str, timeout: float = 30.0) -> dict[str, Any]:
    t0 = time.monotonic()
    while time.monotonic() - t0 < timeout:
        view = runs.view(run_id)
        assert view is not None
        if view["status"] in ("done", "stopped", "failed"):
            return view
        time.sleep(0.02)
    raise AssertionError("run did not finish")


# ── 실행·스트림 ───────────────────────────────────────────────────────────────


def test_late_stream_gets_every_event_and_matches_run_view(server: tuple[int, DayRuns]) -> None:
    port, runs = server
    run_id = _start(port)
    _wait_done(runs, run_id)
    events = _stream(port, run_id)
    status, view = _req(port, "GET", f"/api/day/runs/{run_id}")
    assert status == 200 and view["status"] == "done"
    assert len(events) == view["event_count"]
    assert [e["seq"] for e in events] == list(range(1, len(events) + 1))
    assert events[-1]["stage"] == "done" and events[-1]["stopped"] is False
    assert events[0]["stage"] == "day.start"
    stages = {e["stage"] for e in events}
    assert {"step", "proposal", "feedback", "digest", "safety", "day.end"} <= stages
    metrics = view["summary"]["metrics"]
    assert [m["day"] for m in metrics] == [1, 2, 3]
    assert [e["metrics"] for e in events if e["stage"] == "day.end"] == metrics
    # 거주자 특성은 어디에도 없다.
    assert "vulnerable" not in json.dumps(events, ensure_ascii=False)


def test_reconnect_from_n_and_last_event_id_have_no_gap(server: tuple[int, DayRuns]) -> None:
    port, runs = server
    run_id = _start(port, days=1)
    _wait_done(runs, run_id)
    full = _stream(port, run_id)
    n = len(full) // 2
    tail = _stream(port, run_id, frm=n)
    assert [e["seq"] for e in full[: n - 1] + tail] == [e["seq"] for e in full]
    resumed = _stream(port, run_id, last_id=str(n))
    assert resumed[0]["seq"] == n + 1 and resumed == full[n:]


def test_second_run_while_active_is_409_and_stop_works(server: tuple[int, DayRuns]) -> None:
    port, runs = server
    run_id = _start(port, pace_ms=200)
    status, out = _req(port, "POST", "/api/day/run", {"days": 1, "seed": 1})
    assert status == 409 and out["ok"] is False and "실행 중" in out["error"]
    status, out = _req(port, "POST", "/api/day/stop", {})
    assert status == 200 and out["status"] == "stopping"
    view = _wait_done(runs, run_id)
    assert view["status"] == "stopped"
    events = _stream(port, run_id)
    assert events[-1]["stage"] == "done" and events[-1]["stopped"] is True
    status, out = _req(port, "POST", "/api/day/stop", {})
    assert status == 409


def test_viewer_feedback_is_queued_and_applied_through_the_real_route(
    server: tuple[int, DayRuns],
) -> None:
    port, runs = server
    run_id = _start(port, days=1, pace_ms=150)
    t0 = time.monotonic()
    while True:  # 첫 제안(환기)이 나올 때까지 기다린다
        run = runs.get(run_id)
        assert run is not None
        if any(ev["stage"] == "proposal" for ev in list(run.events)):
            break
        assert time.monotonic() - t0 < 20
        time.sleep(0.01)
    status, out = _req(port, "POST", "/api/day/feedback",
                       {"run": run_id, "proposal_id": "p1", "signal": "dismissed"})
    assert status == 202 and out["queued"] is True
    t0 = time.monotonic()
    while True:
        run = runs.get(run_id)
        assert run is not None
        taps = [ev for ev in list(run.events) if ev["stage"] == "feedback" and ev["id"] == "p1"]
        if taps:
            break
        assert time.monotonic() - t0 < 20
        time.sleep(0.01)
    assert (taps[0]["source"], taps[0]["signal"], taps[0]["status"]) == (
        "viewer", "dismissed", "200",
    )


def test_feedback_request_validation(server: tuple[int, DayRuns]) -> None:
    port, runs = server
    status, out = _req(port, "POST", "/api/day/feedback",
                       {"run": "nope", "proposal_id": "p1", "signal": "accepted"})
    assert status == 404
    run_id = _start(port, days=1, pace_ms=50)
    status, out = _req(port, "POST", "/api/day/feedback",
                       {"run": run_id, "proposal_id": "p1", "signal": "maybe"})
    assert status == 400 and out["field"] == "signal"
    status, out = _req(port, "POST", "/api/day/feedback",
                       {"run": run_id, "proposal_id": "p1", "signal": "accepted", "x": 1})
    assert status == 400 and out["field"] == "x"
    runs.stop()
    _wait_done(runs, run_id)
    status, out = _req(port, "POST", "/api/day/feedback",
                       {"run": run_id, "proposal_id": "p1", "signal": "accepted"})
    assert status == 409


@pytest.mark.parametrize(
    "body",
    [{"days": 8}, {"days": 0}, {"persona": "x"}, {"pace_ms": 600}, {"days": 1, "model": "jev"}],
)
def test_run_request_validation(server: tuple[int, DayRuns], body: dict[str, Any]) -> None:
    port, _ = server
    status, out = _req(port, "POST", "/api/day/run", body)
    assert status == 400 and out["ok"] is False and out["error"]


def test_unknown_run_is_404(server: tuple[int, DayRuns]) -> None:
    port, _ = server
    assert _req(port, "GET", "/api/day/runs/missing")[0] == 404
    assert _req(port, "GET", "/api/day/stream?run=missing")[0] == 404


# ── 이 기기 접속만 ───────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("method", "path", "headers"),
    [
        ("GET", "/api/day/runs/x", {"Host": "evil.example"}),
        ("GET", "/api/day/stream?run=x", {"Host": "evil.example"}),
        ("POST", "/api/day/run", {"Origin": "http://evil.example"}),
        ("POST", "/api/day/stop", {"Origin": "http://evil.example"}),
        ("POST", "/api/day/feedback", {"Host": "evil.example"}),
    ],
)
def test_non_local_host_or_origin_is_403(
    server: tuple[int, DayRuns], method: str, path: str, headers: dict[str, str]
) -> None:
    port, runs = server
    body = {"days": 1} if method == "POST" else None
    status, out = _req(port, method, path, body, headers)
    assert status == 403 and out["ok"] is False
    assert runs.runs.run_ids() == []  # 막힌 요청은 실행을 만들지 않았다


def test_post_without_json_content_type_is_403(server: tuple[int, DayRuns]) -> None:
    port, _ = server
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
    conn.request("POST", "/api/day/run", body=b"{}",
                 headers={"Host": f"127.0.0.1:{port}", "Content-Type": "text/plain"})
    assert conn.getresponse().status == 403
    conn.close()


# ── 화면(web/day.html) ───────────────────────────────────────────────────────

ROOT = __import__("pathlib").Path(__file__).resolve().parents[1]


def test_day_page_and_assets_are_served(server: tuple[int, DayRuns]) -> None:
    port, _ = server
    for path, ctype, needle in (
        ("/day.html", "text/html", "하루 시뮬레이션"),
        ("/assets/day.js", "application/javascript", "/api/day/stream"),
        ("/assets/day.css", "text/css", "--accent: #0071e3"),
    ):
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
        conn.request("GET", path, headers={"Host": f"127.0.0.1:{port}"})
        resp = conn.getresponse()
        body = resp.read().decode("utf-8")
        conn.close()
        assert resp.status == 200, path
        assert resp.getheader("Content-Type", "").startswith(ctype), path
        assert needle in body, path


def test_day_page_loads_nothing_from_other_origins_and_never_uses_inner_html() -> None:
    import re

    html = (ROOT / "web" / "day.html").read_text(encoding="utf-8")
    js = "\n".join((ROOT / "web" / "assets" / name).read_text(encoding="utf-8")
                   for name in ("day.js", "day-demo.js"))
    css = "\n".join((ROOT / "web" / "assets" / name).read_text(encoding="utf-8")
                    for name in ("day.css", "day-demo.css"))
    assert re.search(r'(src|href)="(https?:)?//', html) is None
    # SVG 이름 공간 식별자 한 문자열만 허용한다(불러오는 주소가 아니다). 그 밖의 http(s):// 는 없다.
    svg_ns = '"http://www.w3.org/2000/svg"'
    assert js.count(svg_ns) == 1
    assert "http://" not in js.replace(svg_ns, "") and "https://" not in js
    assert "url(" not in css
    assert "innerHTML" not in js and "outerHTML" not in js and "insertAdjacentHTML" not in js
    # 화면은 /api/day/* 만 부른다.
    assert set(re.findall(r'"(/api/[a-z/]+)', js)) <= {
        "/api/day/run", "/api/day/stop", "/api/day/stream", "/api/day/runs/", "/api/day/feedback",
        "/api/day/keys",
    }



def test_demo_assets_are_served_and_draw_only_with_safe_dom_calls(
    server: tuple[int, DayRuns],
) -> None:
    port, _ = server
    for path, ctype, needle in (
        ("/assets/day-demo.js", "application/javascript", "createElementNS"),
        ("/assets/day-demo.css", "text/css", ".dm-overlay"),
    ):
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
        conn.request("GET", path, headers={"Host": f"127.0.0.1:{port}"})
        resp = conn.getresponse()
        body = resp.read().decode("utf-8")
        conn.close()
        assert resp.status == 200 and resp.getheader("Content-Type", "").startswith(ctype)
        assert needle in body
    demo = (ROOT / "web" / "assets" / "day-demo.js").read_text(encoding="utf-8")
    demo_css = (ROOT / "web" / "assets" / "day-demo.css").read_text(encoding="utf-8")
    assert "marker" not in demo and "url(#" not in demo and "url(" not in demo_css
    assert "innerHTML" not in demo and "/api/" not in demo  # 시연 화면은 API 를 직접 부르지 않는다
    html = (ROOT / "web" / "day.html").read_text(encoding="utf-8")
    assert 'src="assets/day-demo.js"' in html and 'href="assets/day-demo.css"' in html
    assert 'id="demo"' in html and 'id="demoClose"' in html and "나가기" in html


def test_demo_narrow_layout_stacks_the_phone_below_the_graph() -> None:
    # 390px 배치는 브라우저로만 잴 수 있다 — 여기서는 그 배치의 재료가 제자리에 있는지만 본다.
    demo = (ROOT / "web" / "assets" / "day-demo.js").read_text(encoding="utf-8")
    demo_css = (ROOT / "web" / "assets" / "day-demo.css").read_text(encoding="utf-8")
    html = (ROOT / "web" / "day.html").read_text(encoding="utf-8")
    assert '"(max-width: 640px)"' in demo and "@media (max-width: 640px)" in demo_css
    assert 'id="demoPane"' in html and 'hidden' in html.split('id="demoPane"')[1].split(">")[0]
    assert html.index('id="demoSvg"') < html.index('id="demoPane"')


def test_demo_button_never_starts_a_run_and_new_runs_are_explicit() -> None:
    # 다시 열기가 새 실행(cloud 면 새 비용)을 부르지 않는다 — 새 실행은 덮개의 [새로 돌리기]로만.
    day_js = (ROOT / "web" / "assets" / "day.js").read_text(encoding="utf-8")
    html = (ROOT / "web" / "day.html").read_text(encoding="utf-8")
    on_demo = day_js.split("function onDemo()")[1].split("\n  }\n")[0]
    assert "startRun" not in on_demo and "reset" not in on_demo
    assert "시연 기록이 꺼진 실행이에요" in on_demo
    on_new = day_js.split("async function onDemoNew()")[1].split("\n  }\n")[0]
    assert "startRun(true)" in on_new
    assert 'id="demoNew"' in html and 'id="demoReplay"' in html and "처음부터 다시 보기" in html


def test_demo_steps_one_node_at_a_time_with_prev_and_next() -> None:
    # 한 단계씩: 세그먼트의 네 번째 칸 + [‹ 이전]·[다음 ›] + 키보드 ←·→. 넘기기는 화면 재생만 바꾼다.
    html = (ROOT / "web" / "day.html").read_text(encoding="utf-8")
    demo = (ROOT / "web" / "assets" / "day-demo.js").read_text(encoding="utf-8")
    assert 'data-speed="step"' in html and "한 단계씩" in html
    assert 'id="demoPrev"' in html and 'id="demoNext"' in html and 'id="demoStep"' in html
    assert '"ArrowRight"' in demo and '"ArrowLeft"' in demo
    step_fns = demo.split("function stepNext()")[1].split("function showWaiting()")[0]
    assert "fetch" not in step_fns and "startRun" not in step_fns
