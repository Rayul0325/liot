"""3D 집 시나리오 API(`POST /api/home3d/scenario`) — 실제 표·사다리·송신 문구로 타임라인 계산.

지키는 것(계획 2026-10-19 Phase 2):
  - 판정은 LiotApp.classify_safety, 하한은 severity_table.compute, 시각은 escalation 순수
    함수, 문구는 Notifier → egress → HTTPNtfyTransport._body/_headers 가 만든 그대로다.
  - DB 를 읽지도 쓰지도 않는다. 보호자 수·취약 거주자는 요청 값이다.
  - 아무것도 보내지 않는다(버튼 문구는 가짜 비밀값으로 만든 transport 의 헤더에서 뽑는다).

네트워크는 127.0.0.1 루프백만 쓴다.
"""

from __future__ import annotations

import asyncio
import json
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

import pytest

from liot import demo_server
from liot.core.risk import RiskTier
from liot.layers.l4_execution import escalation
from liot.layers.l4_execution.ntfy_transport import HTTPNtfyTransport

pytestmark = pytest.mark.usefixtures("demo_state_reset")

GAS = "binary_sensor.kitchen_gas"
LOCK = "lock.front_door"
T0 = "2026-06-02T12:00:00+00:00"


def _timeline(entity_id: str, value: Any, **kw: Any) -> dict[str, Any]:
    kw.setdefault("opened_at", T0)
    return asyncio.run(demo_server.scenario_timeline(entity_id, value, **kw))


def _at(out: dict[str, Any], kind: str, who: str | None = None) -> list[dict[str, Any]]:
    return [
        e for e in out["events"] if e["kind"] == kind and (who is None or e["who"] == who)
    ]


def _targets(event: dict[str, Any]) -> list[str]:
    return [a.get("target", "") for a in event["actions"]]


# ------------------------------------------------------------------ 가스(위험)
def test_gas_with_one_guardian_runs_the_full_ladder() -> None:
    out = _timeline(GAS, "on", consented_guardians=1)
    assert out["ok"] is True
    assert out["state"] == "abnormal"
    assert out["severity"]["final"] == 3 and out["severity"]["ko"] == "위험"
    assert out["severity"]["floor"] == 3

    alert = _at(out, "alert")
    assert [(e["t_s"], e["who"]) for e in alert] == [(0, "resident")]
    assert alert[0]["priority"] == "5"

    realerts = [e["t_s"] for e in _at(out, "realert")]
    assert realerts == [60, 120, 180, 240, 300, 360, 420]
    assert all(e["who"] == "resident" for e in _at(out, "realert"))

    forward = _at(out, "forward")
    assert [(e["t_s"], e["who"], e["guardian"]) for e in forward] == [(90, "guardian", 1)]

    guide = _at(out, "guidance_119")
    assert sorted((e["t_s"], e["who"]) for e in guide) == [(390, "guardian"), (390, "resident")]
    for e in guide:
        assert "tel:119" in _targets(e)

    # 타임라인 눈금은 서버가 준다(화면은 계산하지 않는다).
    assert [m["t_s"] for m in out["markers"]] == [0, 90, 390]


def test_gas_without_guardians_still_gets_119_guidance_for_the_resident() -> None:
    out = _timeline(GAS, "on", consented_guardians=0)
    assert _at(out, "forward") == []
    guide = _at(out, "guidance_119")
    assert [(e["t_s"], e["who"]) for e in guide] == [(390, "resident")]
    assert "tel:119" in _targets(guide[0])


def test_two_guardians_get_simultaneous_forwards() -> None:
    out = _timeline(GAS, "on", consented_guardians=2)
    forward = _at(out, "forward")
    assert [(e["t_s"], e["guardian"]) for e in forward] == [(90, 1), (90, 2)]
    assert sorted(e["guardian"] for e in _at(out, "guidance_119", "guardian")) == [1, 2]


# ------------------------------------------------------------------ 도어락(경고)
def test_lock_is_warning_with_three_realerts_and_no_guardian_path() -> None:
    out = _timeline(LOCK, "on", consented_guardians=2)
    assert out["severity"]["final"] == 2 and out["severity"]["ko"] == "경고"
    assert [e["t_s"] for e in _at(out, "realert")] == [600, 1200, 1800]
    assert _at(out, "forward") == []
    assert _at(out, "guidance_119") == []
    assert all(e["who"] == "resident" for e in out["events"])
    assert _at(out, "alert")[0]["priority"] == "4"


def test_lock_with_vulnerable_resident_is_critical_with_guardian_path() -> None:
    out = _timeline(LOCK, "on", vulnerable_resident=True, consented_guardians=1)
    assert out["severity"]["final"] == 3 and out["severity"]["floor"] == 3
    assert "resident" in out["severity"]["sources"]
    assert [e["t_s"] for e in _at(out, "forward")] == [90]
    assert {e["who"] for e in _at(out, "guidance_119")} == {"resident", "guardian"}


# ------------------------------------------------------------------ 감시 불가
def test_gas_unavailable_is_one_unmonitorable_notice() -> None:
    out = _timeline(GAS, "unavailable", consented_guardians=2)
    assert out["state"] == "unmonitorable"
    assert [(e["t_s"], e["who"], e["kind"]) for e in out["events"]] == [
        (0, "resident", "unmonitorable")
    ]
    body = out["events"][0]["body"]
    assert escalation.message_label("gas", "unmonitorable") in body
    assert "(주의)" in body
    assert out["events"][0]["priority"] == "3"


def test_normal_value_has_no_events() -> None:
    out = _timeline(GAS, "off", consented_guardians=1)
    assert out["ok"] is True and out["state"] == "normal"
    assert out["events"] == []


# ------------------------------------------------------------------ 확인(ack)
def test_ack_stops_everything_after_it() -> None:
    out = _timeline(GAS, "on", consented_guardians=1, ack_at_s=60)
    assert out["events"], "확인 전 사건(첫 알림)은 남아야 한다"
    assert max(e["t_s"] for e in out["events"]) <= 60
    assert _at(out, "forward") == []
    assert out["acked_at_s"] == 60


# ------------------------------------------------------------------ 문구 = 실제 송신 문구
def test_texts_are_exactly_what_the_transport_would_send() -> None:
    out = _timeline(GAS, "on", consented_guardians=1)
    tier = RiskTier.EMBER
    alert = _at(out, "alert")[0]
    assert alert["body"] == HTTPNtfyTransport._body({"glyph": tier.glyph, "label": tier.label_ko}).decode()
    assert alert["title"] == "LIOT"
    # 거주자 상황 알림 버튼: 수락·거절·괜찮아요(서버가 붙인 한국어 표기).
    assert [a["label"] for a in alert["actions"]] == ["Accept", "Dismiss", "OK"]
    assert [a["label_ko"] for a in alert["actions"]] == ["수락", "거절", "괜찮아요"]

    forward = _at(out, "forward")[0]
    expected = HTTPNtfyTransport._body(
        {
            "situation_label": escalation.message_label("gas", "forward"),
            "home_alias": "우리 집",
            "at": T0,
            "severity_label": "위험",
        }
    ).decode()
    assert forward["body"] == expected
    assert "가스 누출" in forward["body"]
    assert [a["label_ko"] for a in forward["actions"]] == ["확인"]

    resident_119 = _at(out, "guidance_119", "resident")[0]
    assert [a["label_ko"] for a in resident_119["actions"]] == ["119 전화"]
    # 119 는 전화 앱을 여는 view 버튼뿐 — 자동 발신(http) 아님.
    assert resident_119["actions"][0]["kind"] == "view"


def test_no_device_or_room_names_leak_into_texts() -> None:
    out = _timeline(GAS, "on", consented_guardians=2)
    blob = json.dumps(out["events"], ensure_ascii=False)
    assert GAS not in blob
    assert "주방" not in blob


# ------------------------------------------------------------------ HTTP
def _post(base: str, payload: Any) -> tuple[int, dict[str, Any]]:
    req = urllib.request.Request(
        base + demo_server.SCENARIO_PATH,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:  # noqa: S310 - 루프백
            return resp.status, json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read().decode("utf-8"))


@pytest.fixture()
def server(demo_serve, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    db = tmp_path / "must-not-exist.db"
    monkeypatch.setenv("LIOT_DB_PATH", str(db))
    with demo_serve() as port:
        yield f"http://127.0.0.1:{port}", db


def test_http_scenario_is_200_and_touches_no_db(server) -> None:
    base, db = server
    code, out = _post(base, {"entity_id": GAS, "value": "on", "consented_guardians": 1})
    assert code == 200 and out["ok"] is True
    assert [e["t_s"] for e in out["events"] if e["kind"] == "forward"] == [90]
    assert [e["t_s"] for e in out["events"] if e["kind"] == "guidance_119"] == [390, 390]
    code, out = _post(base, {"entity_id": GAS, "value": "unavailable"})
    assert code == 200
    assert [(e["kind"], e["t_s"]) for e in out["events"]] == [("unmonitorable", 0)]
    assert not db.exists(), "시나리오 API 는 DB 를 열지 않는다"


@pytest.mark.parametrize(
    ("payload", "field"),
    [
        ({"entity_id": "binary_sensor.nope", "value": "on"}, "entity_id"),
        ({"entity_id": "light.living_room", "value": "on"}, "entity_id"),
        ({"value": "on"}, "entity_id"),
        ({"entity_id": GAS, "value": "on", "consented_guardians": 3}, "consented_guardians"),
        ({"entity_id": GAS, "value": "on", "consented_guardians": "1"}, "consented_guardians"),
        ({"entity_id": GAS, "value": "on", "vulnerable_resident": "yes"}, "vulnerable_resident"),
        ({"entity_id": GAS, "value": "on", "ack_at_s": -1}, "ack_at_s"),
        ({"entity_id": GAS, "value": "on", "ack_at_s": "soon"}, "ack_at_s"),
    ],
)
def test_http_bad_requests_are_400(server, payload: dict[str, Any], field: str) -> None:
    base, _db = server
    code, out = _post(base, payload)
    assert code == 400
    assert out["ok"] is False and out["error"]
    assert out.get("field") == field
