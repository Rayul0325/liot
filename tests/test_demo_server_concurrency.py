"""데모 서버 공유 상태의 동시성 — ThreadingHTTPServer 아래에서 깨지지 않는다.

`demo_server` 는 `ThreadingHTTPServer` 라 핸들러가 여러 스레드에서 동시에 돈다.
그런데 `_REGISTRY`·`_PREF_CELLS`·`_DIRECTIVES` 는 모듈 전역 가변 상태다. 잠금이
없던 시절 실제로 깨지던 두 가지를 고정한다:

  1) **프리셋 롤백이 남의 등록을 지운다** — `_apply_preset` 은 시작 시 스냅샷을
     뜨고 실패하면 `restore(snap)` 한다. 그 사이 다른 요청이 등록한 센서는
     스냅샷에 없으므로 롤백과 함께 사라진다(lost update).
  2) **피드백 갱신이 유실된다** — `_apply_feedback` 은 `_PREF_CELLS` 를 읽어 새
     dict 로 갈아끼우는 read-modify-write 라, 동시 갱신 중 하나가 덮인다.

두 테스트 모두 **동기화 프리미티브로 경합 시점을 고정한다** — 느린 지점을
주입해 창을 넓히되, 그 창에 들어갔는지는 sleep 으로 추측하지 않고 Event·Barrier
로 확인한다. 이게 중요한 이유는 빗나가는 방향이 false green 이기 때문이다:
등록이 스냅샷 전에 끝나면 롤백이 그걸 보존해, 락이 없어도 통과해 버린다.

루프백 HTTP 왕복만 쓰고 외부 네트워크는 호출하지 않는다 —
`tests/test_service.py` 의 실 HTTP 왕복과 같은 방식이다.

3) **락은 스냅샷 구간만, 응답은 락 밖에서** — 파이프라인이 락을 쥔 채 돌거나
   응답을 락 아래에서 쓰면 다른 요청이 전부 멈춘다. 반대로 스냅샷을 떠 놓고도
   응답 조립이 전역 상태를 다시 읽으면, 판단이 쓰지 않은 값이 화면에 섞인다.
   `STATE_LOCK` 을 기록용 대역으로 바꿔 '어디서 락을 쥐었나'를 직접 단언한다.
"""

from __future__ import annotations

import asyncio
import json
import threading
import time
import urllib.error
import urllib.request
from typing import Any

import pytest

from liot import demo_server
from liot.app import LiotApp
from liot.layers.l2_context import feature_vector
from liot.layers.l2_context.feature_vector import NEUTRAL_PREF
from liot.registry import RegistryError

# 데모 서버 전역 초기화·서버 기동은 conftest 공용 픽스처가 맡는다.
pytestmark = pytest.mark.usefixtures("demo_state_reset")

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
def server(demo_serve):
    """임의 포트에 실제 데모 서버를 띄운다(루프백 전용 — 기동은 conftest demo_serve)."""
    with demo_serve() as port:
        yield f"http://127.0.0.1:{port}"


def post(
    base: str, path: str, payload: dict[str, Any], *, timeout: float = 10
) -> tuple[int, dict[str, Any]]:
    req = urllib.request.Request(
        base + path,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310 - 루프백
            return resp.status, json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read().decode("utf-8"))


def get(base: str, path: str) -> dict[str, Any]:
    with urllib.request.urlopen(base + path, timeout=10) as resp:  # noqa: S310
        body: dict[str, Any] = json.loads(resp.read().decode("utf-8"))
        return body


# ------------------------------------------- 1) 프리셋 롤백 vs 동시 센서 등록
def test_preset_rollback_does_not_erase_concurrent_registration(server, monkeypatch):
    """실패한 프리셋의 롤백이, 그 사이 등록된 센서까지 되돌려 지우면 안 된다."""

    # 경합 창에 '들어갔음'을 신호로 확인한다. sleep 으로 때를 맞추면 빗나갔을 때
    # 센서 등록이 스냅샷 이전에 끝나 롤백이 그걸 보존해 버리고, 락이 없어도
    # 테스트가 통과한다(false green). 그래서 추측 대신 동기화를 쓴다.
    in_rule_stage = threading.Event()

    def slow_failing_add_rule(spec: dict[str, Any]) -> dict[str, Any]:
        in_rule_stage.set()  # 스냅샷·기기 등록을 마치고 규칙 단계에 진입했다
        time.sleep(0.4)  # 롤백 직전 구간을 넓힌다(경합 창)
        raise RegistryError("의도적 실패(동시성 테스트)", field="capability")

    monkeypatch.setattr(demo_server._REGISTRY, "add_rule", slow_failing_add_rule)

    out: dict[str, Any] = {}

    def apply_preset() -> None:
        out["preset"] = post(server, "/api/registry/preset", PRESET)

    worker = threading.Thread(target=apply_preset)
    worker.start()
    assert in_rule_stage.wait(timeout=10), "프리셋이 규칙 단계에 도달하지 못했다"
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
    real_apply_signal = feature_vector.apply_signal

    def slow_apply_signal(cells, cell, signal):
        time.sleep(0.25)  # 읽기와 쓰기 사이를 벌린다(경합 창)
        return real_apply_signal(cells, cell, signal)

    monkeypatch.setattr(demo_server, "apply_signal", slow_apply_signal)

    # 실제 cell_key() 형식(time_bucket|occupancy|capability:action). 3분절이 아닌
    # 키는 /api/simulate 가 절대 조회하지 못해, 통과해도 아무것도 증명하지 않는다.
    payload = {"cell": "evening|vacant|light:turn_off", "signal": "accepted"}
    results: list[dict[str, Any]] = []
    lock = threading.Lock()

    # 두 요청이 실제로 겹치도록 배리어로 출발을 맞춘다. 한쪽이 늦게 출발해
    # 앞선 갱신이 이미 끝나 있으면, 락이 없어도 값이 정상으로 보인다(false green).
    start = threading.Barrier(2, timeout=10)

    def send() -> None:
        start.wait()
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


# ------------------------------------- 3) 락은 스냅샷 구간만, 응답은 락 밖에서
# 빈집 + 조명 on → vacant-light-off 규칙이 light.turn_off 를 제안한다(FakeLLM).
VACANT_LIGHT_BODY: dict[str, Any] = {
    "mode": "tick",
    "config": {"time": "evening", "llm_enabled": False},
    "sensors": {"binary_sensor.living_motion": "off", "light.living_room": "on"},
}


class _ProbeLock:
    """STATE_LOCK 대역 — 스레드별로 '지금 쥐고 있나'와 총 획득 횟수를 기록한다."""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._local = threading.local()
        self.acquired = 0

    def held(self) -> bool:
        return int(getattr(self._local, "depth", 0)) > 0

    def __enter__(self) -> _ProbeLock:
        self._lock.acquire()
        self._local.depth = int(getattr(self._local, "depth", 0)) + 1
        self.acquired += 1
        return self

    def __exit__(self, *exc: object) -> None:
        self._local.depth -= 1
        self._lock.release()


def test_simulate_pipeline_does_not_hold_state_lock(server, monkeypatch):
    """파이프라인(느린 LLM 대기 포함)이 도는 동안 다른 요청이 락에 막히지 않는다(R1-04).

    파이프라인 안에서 멈추는 tick 대역을 끼우고, 그 사이 /api/directive 가 2초 안에
    답하는지 본다. asyncio.run(_run(...)) 을 락으로 감싸던 옛 구조로 되돌리면
    지시 요청이 tick 이 풀릴 때까지 묶여 시간 초과로 빨갛다.
    """
    entered = threading.Event()
    release = threading.Event()

    async def blocking_tick(self):
        entered.set()
        release.wait(timeout=10)  # 느린 LLM 호출 대역 — 이 요청 스레드만 묶는다
        return None

    monkeypatch.setattr(LiotApp, "tick", blocking_tick)
    out: dict[str, Any] = {}

    def simulate() -> None:
        out["sim"] = post(server, "/api/simulate", VACANT_LIGHT_BODY)

    worker = threading.Thread(target=simulate)
    worker.start()
    try:
        assert entered.wait(timeout=10), "simulate 가 파이프라인에 도달하지 못했다"
        started = time.monotonic()
        status, body = post(server, "/api/directive", {"text": "불 켜놔줘"}, timeout=2)
        elapsed = time.monotonic() - started
    finally:
        release.set()
        worker.join(timeout=10)

    assert status == 200 and body["ok"] is True, body
    assert elapsed < 2, f"지시 요청이 {elapsed:.1f}초 묶였다"
    assert out["sim"][0] == 200, out


def test_registry_reset_also_clears_directives(server):
    """/api/registry/reset 은 활성 지시도 비운다(R1-14) — 남으면 tick 이 이유 없이 침묵."""
    status, body = post(server, "/api/directive", {"text": "불 켜놔줘"})
    assert status == 200 and len(body["active"]) == 1, body

    status, _ = post(server, "/api/registry/reset", {})
    assert status == 200

    status, body = post(server, "/api/directive", {"text": ""})
    assert status == 200 and body["active"] == [], f"리셋 뒤에도 옛 지시가 남았다: {body}"


@pytest.mark.parametrize("payload", [{"reset": True}, {"text": "불 켜놔줘"}])
def test_directive_response_is_written_outside_state_lock(server, monkeypatch, payload):
    """지시 변경은 락 아래에서, 응답 쓰기는 락 밖에서(R1-05).

    예전엔 초기화 분기가 락을 쥔 채 응답을 써서 느린 수신자가 소켓 타임아웃까지
    전역 락을 붙들 수 있었다.
    """
    probe = _ProbeLock()
    monkeypatch.setattr(demo_server, "STATE_LOCK", probe)
    sent_while_held: list[bool] = []
    real_send = demo_server.Handler._send

    def spy_send(self, code, body, ctype):
        sent_while_held.append(probe.held())
        real_send(self, code, body, ctype)

    monkeypatch.setattr(demo_server.Handler, "_send", spy_send)

    status, body = post(server, "/api/directive", payload)

    assert status == 200 and body["ok"] is True, body
    assert probe.acquired >= 1, "지시 변경이 락 없이 일어났다"
    assert sent_while_held == [False], f"응답을 락을 쥔 채 썼다: {sent_while_held}"


def test_event_payload_validation_does_not_take_state_lock(monkeypatch):
    """entity 없는 event 는 순수 페이로드 오류 — 전역 락을 잡기 전에 거절한다(R2-35)."""
    probe = _ProbeLock()
    monkeypatch.setattr(demo_server, "STATE_LOCK", probe)

    out = asyncio.run(
        demo_server._run({"mode": "event", "event": {}, "config": {"llm_enabled": False}})
    )

    assert out["ok"] is False and "entity" in out["error"], out
    assert probe.acquired == 0, "순수 페이로드 검증이 전역 락을 잡았다"


def test_simulate_reads_directives_once_under_the_lock(monkeypatch):
    """활성 지시는 락 아래에서 한 번만 읽고, 응답도 그 스냅샷을 쓴다(R2-10).

    예전엔 판단용으로 한 번(락 안), 응답용으로 두 번 더(락 밖) 읽어 그 사이의
    변경이나 만료가 판단과 다른 목록을 화면에 보여줄 수 있었다.
    """
    probe = _ProbeLock()
    monkeypatch.setattr(demo_server, "STATE_LOCK", probe)
    demo_server._DIRECTIVES.add_text("불 켜놔줘")
    calls: list[bool] = []
    real_active = demo_server._DIRECTIVES.active

    def spy_active(*args, **kwargs):
        calls.append(probe.held())
        return real_active(*args, **kwargs)

    monkeypatch.setattr(demo_server._DIRECTIVES, "active", spy_active)

    out = asyncio.run(demo_server._run(dict(VACANT_LIGHT_BODY)))

    assert out["ok"] is True, out
    assert calls == [True], f"지시를 락 밖에서 또는 여러 번 읽었다: {calls}"
    assert [d["raw_text"] for d in out["directives"]["active"]] == ["불 켜놔줘"], out


def test_embedding_trace_uses_the_preference_snapshot_the_pipeline_saw(monkeypatch):
    """파이프라인이 도는 사이 들어온 피드백이 이번 응답의 임베딩 표시를 바꾸면 안 된다(R2-09).

    tick 대역이 스냅샷 이후(락 밖) 같은 셀에 수락 피드백을 넣는다. 평가는 스냅샷
    (중립 0.5)을 썼으므로 트레이스도 0.5 여야 한다 — 전역 셀맵을 다시 읽으면 0.65.
    """
    cell = "evening|vacant|light:turn_off"
    real_tick = LiotApp.tick

    async def tick_with_concurrent_feedback(self):
        assert demo_server._apply_feedback(cell, "accepted")["ok"] is True
        return await real_tick(self)

    monkeypatch.setattr(LiotApp, "tick", tick_with_concurrent_feedback)

    out = asyncio.run(demo_server._run(dict(VACANT_LIGHT_BODY)))

    assert demo_server._PREF_CELLS[cell] > NEUTRAL_PREF, "피드백이 실제로 반영되지 않았다"
    assert out["embedding"]["cell"] == cell, out.get("embedding")
    assert out["embedding"]["preference"] == pytest.approx(NEUTRAL_PREF), (
        f"임베딩 표시가 판단이 쓰지 않은 최신 셀맵을 읽었다: {out['embedding']}"
    )
