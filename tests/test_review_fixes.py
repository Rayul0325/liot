"""최종 검토 보완 — 119 안내 버튼·파급도 라벨 왕복·관제실·3D 집 화면↔API 계약."""

from __future__ import annotations

import asyncio
import re
from pathlib import Path

from liot.core.severity import SeverityLevel
from liot.demo_server import _QUESTIONS, SAFETY_ROUTES, SCENARIO_PATH, scenario_timeline
from liot.layers.l4_execution import escalation
from liot.layers.l4_execution.ntfy_transport import HTTPNtfyTransport, _priority

ROOT = Path(__file__).resolve().parents[1]


def _summary(label: str) -> dict[str, str]:
    return {"situation_label": label, "home_alias": "우리 집", "at": "t", "severity_label": "위험"}


def test_guidance_119_has_call_button_only_opening_the_dialer() -> None:
    tr = HTTPNtfyTransport("http://ntfy.invalid")
    headers = tr._headers(_summary(escalation.message_label("gas", "guidance_119")))
    # 전화 앱만 연다(view + tel:) — 자동 발신(http 호출) 없음.
    assert "view, Call 119, tel:119" in headers["Actions"]
    assert "http, Call" not in headers["Actions"]


def test_forward_and_resolved_have_no_call_button() -> None:
    tr = HTTPNtfyTransport("http://ntfy.invalid")
    for kind in ("forward", "resolved"):
        headers = tr._headers(_summary(escalation.message_label("gas", kind)))
        assert "tel:119" not in headers.get("Actions", "")


def test_every_severity_label_round_trips_to_its_priority() -> None:
    # 표시 라벨을 바꾸면 우선순위가 조용히 티어로 떨어지는 일을 막는다.
    for level in SeverityLevel:
        assert _priority({"severity_label": level.label_ko, "risk_tier": 0}, 0) == level.ntfy_priority


def test_safety_js_calls_only_routes_the_server_serves() -> None:
    js = (ROOT / "web" / "assets" / "safety.js").read_text(encoding="utf-8")
    called = set(re.findall(r'"(/api/safety/[a-z\-/]+)"', js))
    served = {path for _method, path in SAFETY_ROUTES}
    assert called, "safety.js 에서 API 경로를 찾지 못함"
    assert called <= served, called - served


def test_safety_js_answer_keys_match_server_questions() -> None:
    js = (ROOT / "web" / "assets" / "safety.js").read_text(encoding="utf-8")
    m = re.search(r"const ANSWER_KEYS = \[([^\]]*)\]", js)
    assert m is not None
    js_keys = set(re.findall(r'"([a-z0-9_]+)"', m.group(1)))
    assert js_keys == {q[0] for q in _QUESTIONS}


def test_demo_uses_the_shared_situation_labels() -> None:
    src = (ROOT / "liot" / "demo_server.py").read_text(encoding="utf-8")
    assert "_SITUATION_KO" not in src
    assert escalation.situation_label("gas") == "가스 누출"


# ── 3D 가상 집 화면(home3d) ↔ API 계약 ─────────────────────────────────────
HOME3D_JS = ROOT / "web" / "assets" / "home3d.js"


def test_home3d_js_calls_only_routes_the_server_serves() -> None:
    js = HOME3D_JS.read_text(encoding="utf-8")
    # 큰따옴표·작은따옴표·백틱 모두 — 따옴표 종류를 바꿔 계약 검사를 피하지 못하게.
    called = set(re.findall(r"[\"'`](/api/[a-z0-9_\-/]+)[\"'`]", js))
    assert called, "home3d.js 에서 API 경로를 찾지 못함"
    assert called <= {"/api/catalog", SCENARIO_PATH}, called - {"/api/catalog", SCENARIO_PATH}


def test_home3d_scenario_picker_inputs_are_real_safety_sensors() -> None:
    # 화면의 시나리오 4개(센서·값)는 서버가 실제로 계산할 수 있는 입력이어야 한다.
    js = HOME3D_JS.read_text(encoding="utf-8")
    block = js[js.index("const SCENARIOS = [") : js.index("];", js.index("const SCENARIOS = ["))]
    pairs = re.findall(r'entity_id: "([^"]+)",\s*value: "([^"]+)"', block)
    assert len(pairs) == 4, pairs
    states = []
    for entity_id, value in pairs:
        out = asyncio.run(scenario_timeline(entity_id, value, consented_guardians=1))
        assert out["ok"] is True and out["events"], (entity_id, value)
        states.append(out["state"])
    assert states.count("unmonitorable") == 1 and states.count("abnormal") == 3


def test_home3d_page_loads_nothing_from_other_origins() -> None:
    html = (ROOT / "web" / "home3d.html").read_text(encoding="utf-8")
    js = HOME3D_JS.read_text(encoding="utf-8")
    css = (ROOT / "web" / "assets" / "home3d.css").read_text(encoding="utf-8")
    assert re.search(r'(src|href)="(https?:)?//', html) is None
    assert "http://" not in js and "https://" not in js
    assert "url(" not in css
    # three.js 는 저장소 안 고정 파일을 쓴다.
    assert '"./vendor/three.module.min.js"' in js
    # 서버 값을 innerHTML 로 넣지 않는다(web/AGENTS.md).
    assert "innerHTML" not in js


def test_header_links_reach_the_home3d_page_and_back() -> None:
    web = ROOT / "web"
    assert 'href="home3d.html"' in (web / "app.html").read_text(encoding="utf-8")
    assert 'href="home3d.html"' in (web / "safety.html").read_text(encoding="utf-8")
    home3d = (web / "home3d.html").read_text(encoding="utf-8")
    assert 'href="app.html"' in home3d and 'href="safety.html"' in home3d
