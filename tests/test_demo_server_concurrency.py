"""데모 서버 공유 상태의 동시성 — ThreadingHTTPServer 아래에서 깨지지 않는다.

`demo_server` 는 `ThreadingHTTPServer` 라 핸들러가 여러 스레드에서 동시에 돈다.
그런데 `_REGISTRY`·`_PREF_CELLS`·`_DIRECTIVES` 는 모듈 전역 가변 상태다. 잠금이
없던 시절 실제로 깨지던 두 가지를 고정한다:

  1) **프리셋 롤백이 남의 등록을 지운다** — `_apply_preset` 은 시작 시 스냅샷을
     뜨고 실패하면 `restore(snap)` 한다. 그 사이 다른 요청이 등록한 센서는
     스냅샷에 없으므로 롤백과 함께 사라진다(lost update).
  2) **피드백 갱신이 유실된다** — `_apply_feedback` 은 `_PREF_CELLS` 를 읽어 새
     dict 로 갈아끼우는 read-modify-write 라, 동시 갱신 중 하나가 덮인다.

두 테스트 모두 느린 지점을 주입해 경합 창을 넓히므로 타이밍에 기대지 않는다
(잠금이 없으면 결정적으로 적색). 루프백 HTTP 왕복만 쓰고 외부 네트워크는
호출하지 않는다 — `tests/test_service.py` 의 실 HTTP 왕복과 같은 방식이다.
"""

from __future__ import annotations

import json
import threading
import time
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from typing import Any

import pytest

from liot import demo_server
from liot.layers.l2_context.feature_vector import NEUTRAL_PREF
from liot.registry import RegistryError

SENSOR = {
    "entity_id": "sensor.concurrent_probe",
    "name": "동시성 프로브",
    "category": "moisture",
    "kind": "number",
    "unit": "%",
    "min": 0,
    "max": 100,
}

PRESET = {
    "reset": False,
    "sensors": [],
    "devices": [
        {
            "entity_id": "fan.preset_probe",
            "name": "프리셋 팬",
            "capability": "fan",
            "category": "appliance",
            "actions": [{"action": "turn_on", "tier": "ambient"}],
        }
    ],
    # add_rule 을 느리게 실패하도록 패치하므로, 이 규칙에서 반드시 롤백된다.
    "rules": [{"capability": "fan", "action": "turn_on"}],
}


# ------------------------------------------------------------------ 서버 픽스처
@pytest.fixture()
def server():
    """임의 포트에 실제 데모 서버를 띄운다(루프백 전용)."""
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), demo_server.Handler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{httpd.server_address[1]}"
    finally:
        httpd.shutdown()
        httpd.server_close()
        thread.join(timeout=5)


@pytest.fixture(autouse=True)
def _clean_registry():
    """전역 레지스트리·선호 셀맵을 테스트마다 초기화(순서 의존 제거)."""
    demo_server._REGISTRY.reset()
    demo_server._PREF_CELLS.clear()
    yield
    demo_server._REGISTRY.reset()
    demo_server._PREF_CELLS.clear()


def post(base: str, path: str, payload: dict[str, Any]) -> tuple[int, dict[str, Any]]:
    req = urllib.request.Request(
        base + path,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:  # noqa: S310 - 루프백
            return resp.status, json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read().decode("utf-8"))


def get(base: str, path: str) -> dict[str, Any]:
    with urllib.request.urlopen(base + path, timeout=10) as resp:  # noqa: S310
        return json.loads(resp.read().decode("utf-8"))


# ------------------------------------------- 1) 프리셋 롤백 vs 동시 센서 등록
def test_preset_rollback_does_not_erase_concurrent_registration(server, monkeypatch):
    """실패한 프리셋의 롤백이, 그 사이 등록된 센서까지 되돌려 지우면 안 된다."""

    def slow_failing_add_rule(spec: dict[str, Any]) -> dict[str, Any]:
        time.sleep(0.4)  # 롤백 직전 구간을 넓힌다(경합 창)
        raise RegistryError("의도적 실패(동시성 테스트)", field="capability")

    monkeypatch.setattr(demo_server._REGISTRY, "add_rule", slow_failing_add_rule)

    out: dict[str, Any] = {}

    def apply_preset() -> None:
        out["preset"] = post(server, "/api/registry/preset", PRESET)

    worker = threading.Thread(target=apply_preset)
    worker.start()
    time.sleep(0.1)  # 프리셋이 규칙 단계(느린 실패)에 들어간 뒤
    out["sensor"] = post(server, "/api/registry/sensor", SENSOR)
    worker.join(timeout=10)

    assert out["preset"][0] == 400, "프리셋은 규칙 단계에서 실패해야 한다"
    assert out["sensor"][0] == 200, f"동시 센서 등록은 성공해야 한다: {out['sensor']}"

    catalog = get(server, "/api/catalog")
    ids = {s["entity_id"] for s in catalog["sensors"]}
    assert SENSOR["entity_id"] in ids, "롤백이 동시에 등록된 센서를 지웠다(lost update)"
    # 프리셋이 등록한 기기는 롤백으로 사라져야 한다(원자성의 반대편).
    device_ids = {d["entity_id"] for d in catalog["devices"]}
    assert "fan.preset_probe" not in device_ids, "실패한 프리셋의 기기가 남았다"


# --------------------------------------------------- 2) 피드백 read-modify-write
def test_concurrent_feedback_updates_are_not_lost(server, monkeypatch):
    """같은 셀에 대한 동시 피드백 두 건이 서로를 덮어쓰면 안 된다."""
    real_apply_signal = demo_server.apply_signal

    def slow_apply_signal(cells, cell, signal):
        time.sleep(0.25)  # 읽기와 쓰기 사이를 벌린다(경합 창)
        return real_apply_signal(cells, cell, signal)

    monkeypatch.setattr(demo_server, "apply_signal", slow_apply_signal)

    payload = {"cell": "night|away", "signal": "accepted"}
    results: list[dict[str, Any]] = []
    lock = threading.Lock()

    def send() -> None:
        _, body = post(server, "/api/feedback", payload)
        with lock:
            results.append(body)

    threads = [threading.Thread(target=send) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=10)

    assert len(results) == 2 and all(r.get("ok") for r in results), results
    first, second = sorted(results, key=lambda r: r["before"])
    assert first["before"] == pytest.approx(NEUTRAL_PREF), "첫 갱신은 중립에서 출발한다"
    assert second["before"] == pytest.approx(first["after"]), (
        "두 번째 갱신이 첫 번째 결과 위에 쌓이지 않았다 — 갱신 유실"
    )
    assert second["after"] > second["before"], "수락 신호는 선호를 올려야 한다"
