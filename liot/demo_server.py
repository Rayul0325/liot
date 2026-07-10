"""LIOT 데모 랩 서버 — 브라우저에서 센서 값을 세팅하면 *실제* 파이프라인을 탄다.

가짜 시뮬(web/assets/demo.js)이 아니라, 매 요청마다 진짜 ``LiotApp`` 을 만들어
L1 정규화 → L2 맥락/후보생성/평가 → L3 Gate1(가드레일)+Gate2(로컬 LLM) →
L4 개입까지 실제로 돌리고, 각 단계 산출물을 JSON 트레이스로 돌려준다.

  python -m liot.demo_server           # http://127.0.0.1:8765 (기본 gemma3:4b)

의존성: 표준 라이브러리만. LLM 은 로컬 ollama(HTTP :11434)의 gemma3:4b 기본.
정적 UI(web/lab.html 등)와 API(/api/simulate)를 같은 포트로 서빙(동일 출처).
데모 서버는 절대 액추에이터를 호출하지 않는다(제안-만; 실행 옵트인도 판정만 표시).
"""

from __future__ import annotations

import asyncio
import json
import os
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

from liot.app import LiotApp
from liot.core.clock import Clock
from liot.core.contracts import SensorReading
from liot.core.risk import RiskTier
from liot.layers.l1_sensing.normalizer import normalize
from liot.layers.l2_context.context_builder import derive_occupancy
from liot.layers.l2_context.external_provider import FakeExternalProvider
from liot.layers.l3_judgment.llm_judge import FakeLLM
from liot.layers.l3_judgment.ollama_backend import HTTPOllamaTransport, OllamaBackend
from liot.layers.l2_context.nl_directive import DirectiveStore, UserDirective
from liot.layers.l4_execution.device_catalog import load_device_catalog
from liot.layers.l4_execution.executor import ExecDecision, ExecutionPolicy

# 활성 자연어 지시(세션 스코프). /api/directive 로 추가·초기화, /api/simulate tick 에 반영.
_DIRECTIVES = DirectiveStore()


def _directive_view(d: UserDirective) -> dict[str, Any]:
    return {
        "capability": d.capability,
        "constraint": d.constraint,
        "entity_id": d.entity_id,
        "source": d.source,
        "raw_text": d.raw_text,
    }

WEB_DIR = Path(__file__).resolve().parent.parent / "web"
REPO_ROOT = Path(__file__).resolve().parent.parent

# 데모 주택의 엔티티 → LIOT 카테고리. 안전 임계(gas/smoke/leak/lock)는 코어가 EMBER.
CATEGORIES: dict[str, str] = {
    "sensor.living_temperature": "temperature",
    "sensor.living_humidity": "humidity",
    "binary_sensor.living_motion": "motion",
    "light.living_room": "light",
    "climate.living_room": "climate",
    "cover.living_room": "curtain",
    "binary_sensor.kitchen_gas": "gas",
    "binary_sensor.hall_smoke": "smoke",
    "binary_sensor.bath_leak": "leak",
    "lock.front_door": "lock",
}

# 시간대(UI) → 대표 UTC 시각(clock.time_bucket 기준: 06-11 morning / 11-17 day /
# 17-22 evening / 그 외 night). 능동 규칙의 time_bucket 매칭에 쓰인다.
_TIME_HOUR = {"morning": "08", "day": "13", "evening": "19", "night": "23"}

_BOOL_SENSORS = {
    "binary_sensor.living_motion",
    "binary_sensor.kitchen_gas",
    "binary_sensor.hall_smoke",
    "binary_sensor.bath_leak",
    "light.living_room",
}

# 엔티티 → 한글 표시 이름(센서·기기 공용). 데모 카탈로그 뷰의 단일 원천.
# (JS 는 라벨을 절대 하드코딩하지 않고 /api/catalog 응답만으로 렌더한다.)
_ENTITY_LABELS: dict[str, str] = {
    "sensor.living_temperature": "거실 온도",
    "sensor.living_humidity": "거실 습도",
    "binary_sensor.living_motion": "거실 움직임",
    "light.living_room": "거실 조명",
    "climate.living_room": "거실 냉난방",
    "cover.living_room": "거실 커튼",
    "binary_sensor.kitchen_gas": "주방 가스",
    "binary_sensor.hall_smoke": "복도 연기",
    "binary_sensor.bath_leak": "욕실 누수",
    "lock.front_door": "현관 잠금",
    # device_catalog.yaml 전용 엔티티
    "switch.gas_valve": "가스 밸브",
    "cover.garage": "차고 문",
}

# 수치 센서의 단위·범위. 나머지 엔티티는 불리언(토글)로 취급한다.
_NUMERIC_SENSORS: dict[str, dict[str, Any]] = {
    "sensor.living_temperature": {"unit": "°C", "min": -10, "max": 40},
    "sensor.living_humidity": {"unit": "%", "min": 0, "max": 100},
}


def _raw_for(entity_id: str, value: Any) -> Any:
    """UI 값 → SensorReading.raw_value. 불리언 센서는 on/off 문자열로."""
    if isinstance(value, bool):
        return "on" if value else "off"
    if entity_id in _BOOL_SENSORS and isinstance(value, (int, float)):
        return "on" if value else "off"
    if entity_id == "lock.front_door" and isinstance(value, bool):
        return "locked" if value else "unlocked"
    return value


def _reading(entity_id: str, value: Any, ts: str) -> SensorReading:
    raw = _raw_for(entity_id, value)
    return SensorReading(
        entity_id=entity_id,
        source="lab",
        raw_value=raw,
        norm_value=normalize(raw),
        ts_utc=ts,
        home_id="singleton",
    )


def _tier_view(tier: RiskTier) -> dict[str, Any]:
    return {"name": tier.name.lower(), "ko": tier.label_ko, "glyph": tier.glyph, "level": int(tier)}


def _catalog_view() -> dict[str, Any]:
    """센서·기기·행동·위험등급을 한 응답으로 합성한다(`GET /api/catalog`).

    이 합성부는 **이 함수 한 곳에만 격리**한다 — P2 에서 데이터 원천을
    런타임 레지스트리로 바로 이 지점에서 교체한다. 지금은:
      * 센서 = ``CATEGORIES`` + ``_NUMERIC_SENSORS`` 에서 메타 합성
        (수치 센서는 kind="number"+단위/범위, 나머지는 kind="bool").
      * 기기·행동 = ``load_device_catalog().available_actions()`` 를
        entity_id 로 묶음.
      * 위험등급 legend = ``RiskTier.label_ko`` 단일 원천(한글 라벨 이중관리 없음).
    """
    sensors: list[dict[str, Any]] = []
    for eid, category in CATEGORIES.items():
        num = _NUMERIC_SENSORS.get(eid)
        meta: dict[str, Any] = {
            "entity_id": eid,
            "name": _ENTITY_LABELS.get(eid, eid),
            "category": category,
        }
        if num is not None:
            meta.update(kind="number", unit=num["unit"], min=num["min"], max=num["max"])
        else:
            meta.update(kind="bool", unit=None, min=None, max=None)
        sensors.append(meta)

    catalog = load_device_catalog()
    grouped: dict[str, dict[str, Any]] = {}
    for act in catalog.available_actions():
        eid = str(act["entity_id"])
        dev = grouped.get(eid)
        if dev is None:
            dev = grouped[eid] = {
                "entity_id": eid,
                "name": _ENTITY_LABELS.get(eid, eid),
                "category": act.get("category"),
                "capability": act.get("capability"),
                "actions": [],
            }
        tier = RiskTier.from_str(str(act["tier"]))
        dev["actions"].append(
            {"action": act["action"], "tier": act["tier"], "tier_ko": tier.label_ko}
        )

    tiers = {
        str(int(t)): {"name": t.name.lower(), "label_ko": t.label_ko} for t in RiskTier
    }
    return {"sensors": sensors, "devices": list(grouped.values()), "tiers": tiers}


def _build_backend(cfg: dict[str, Any]):
    """LLM 백엔드 구성. llm_enabled=false 면 결정론 FakeLLM(비교용)."""
    if not cfg.get("llm_enabled", True):
        return FakeLLM(), None
    model = str(cfg.get("llm_model") or "gemma3:4b")
    url = str(cfg.get("llm_url") or "http://127.0.0.1:11434")
    timeout = float(cfg.get("llm_timeout") or 60)
    be = OllamaBackend(model, HTTPOllamaTransport(url), timeout_s=timeout)
    return be, model


def _candidate_rows(cand_eval: dict[str, Any] | None) -> list[dict[str, Any]]:
    """last_candidate_evaluation → UI 후보 점수판."""
    if not cand_eval:
        return []
    ev = cand_eval.get("evaluation")
    if ev is None:
        return []
    sel = ev.selected_index
    rows = []
    for i, ac in enumerate(ev.scored):
        c = ac.candidate
        rows.append(
            {
                "capability": c.capability,
                "action": c.action,
                "entity_id": c.entity_id,
                "origin": ac.origin,
                "is_noop": c.capability == "noop",
                "is_selected": i == sel,
                "expected_effect": round(ac.expected_effect, 3),
                "user_preference_fit": round(ac.user_preference_fit, 3),
                "energy_cost": round(ac.energy_cost, 3),
                "disturbance": round(ac.disturbance, 3),
                "safety_risk": round(ac.safety_risk, 3),
                "final_score": round(ac.final_score, 4),
                "reason": ac.reason,
            }
        )
    return rows


def _lines_view(cand_eval: dict[str, Any] | None) -> dict[str, Any]:
    """scored 후보의 origin 으로 Line A(규칙/ML) vs Line B(LLM) 제안을 분리."""
    empty = {"line_a": [], "line_b": []}
    if not cand_eval or cand_eval.get("evaluation") is None:
        return empty
    a: list[dict[str, Any]] = []
    b: list[dict[str, Any]] = []
    for ac in cand_eval["evaluation"].scored:
        c = ac.candidate
        if c.capability == "noop":
            continue
        origins = ac.origin.split("+")
        entry = {
            "label": f"{c.capability}.{c.action}",
            "entity_id": c.entity_id,
            "line_confidence": round(float(ac.line_confidence), 3),
        }
        if any(o.startswith("rule:") or o.startswith("line_a") for o in origins):
            a.append(entry)
        if any(o.startswith("line_b") for o in origins):
            b.append(dict(entry))
    return {"line_a": a, "line_b": b}


def _arbiter_view(cand_eval: dict[str, Any] | None) -> dict[str, Any] | None:
    """두 라인 후보를 융합해 최종 1개를 고르는 결정론 아비터(=CandidateEvaluator)."""
    if not cand_eval or cand_eval.get("evaluation") is None:
        return None
    ev = cand_eval["evaluation"]
    sel = ev.scored[ev.selected_index]
    return {
        "method": "deterministic-weighted",
        "selected": f"{sel.candidate.capability}.{sel.candidate.action}",
        "is_noop": ev.selected_is_noop(),
        "final_score": round(float(sel.final_score), 4),
        "origin": sel.origin,  # 어떤 소스(규칙/ML/LLM)들이 이 행동에 동의했나
        "rationale": ev.rationale,
    }


def _embedding_view(
    cand_eval: dict[str, Any] | None, facts: dict[str, Any]
) -> dict[str, Any] | None:
    """선택 후보의 선호 임베딩 셀 값(Phase3 개인화 전 = 중립 0.5)."""
    from liot.layers.l2_context.feature_vector import (
        NEUTRAL_PREF,
        FakePreferenceEmbedder,
        cell_key,
    )

    if not cand_eval or cand_eval.get("evaluation") is None:
        return None
    ev = cand_eval["evaluation"]
    sel = ev.scored[ev.selected_index].candidate
    if sel.capability == "noop":
        return None
    pref = FakePreferenceEmbedder().preference_for(sel, facts)
    return {
        "cell": cell_key(
            str(facts.get("time_bucket", "")), str(facts.get("occupancy", "")), sel
        ),
        "preference": round(float(pref), 3),
        "note": "개인화 이력 없음 → 중립" if pref == NEUTRAL_PREF else "개인화 반영",
    }


async def _run(body: dict[str, Any]) -> dict[str, Any]:
    sensors: dict[str, Any] = dict(body.get("sensors") or {})
    cfg: dict[str, Any] = dict(body.get("config") or {})
    outdoor: dict[str, Any] | None = body.get("outdoor")
    mode: str = body.get("mode") or "tick"
    tod: str = str(cfg.get("time") or "evening")

    hour = _TIME_HOUR.get(tod, "19")
    ts = f"2026-06-02T{hour}:00:00+00:00"
    clock = Clock(fixed=ts)

    backend, model = _build_backend(cfg)
    external = None
    if outdoor and (outdoor.get("temp_c") is not None or outdoor.get("aqi") is not None):
        external = FakeExternalProvider(
            clock=clock,
            outdoor_temp_c=outdoor.get("temp_c"),
            outdoor_aqi=outdoor.get("aqi"),
        )

    app = LiotApp(
        clock=clock,
        categories=CATEGORIES,
        llm_backend=backend,
        condition=str(cfg.get("condition") or "B"),
        external_provider=external,
    )

    # 1) 모든 센서를 관찰로 주입(맥락/캐시 갱신, 대개 침묵).
    for eid, val in sensors.items():
        if eid not in CATEGORIES:
            continue
        await app.process_reading(_reading(eid, val, ts), force_snapshot=False)

    # seeding 관찰들이 남긴 last_trace 를 비운다 — tick 이 무행동(NoOp)으로
    # 조기 종료하면 last_trace 를 안 채우므로, 비우지 않으면 마지막 seed 트레이스가
    # stale 로 표시된다. 이렇게 해야 침묵 틱은 후보/평가만 보이고 stale 스냅샷은 안 뜬다.
    # 활성 자연어 지시를 앱에 반영(tick 의 comfort 후보 억제; 안전은 억제 불가).
    app.set_directives(_DIRECTIVES.active())

    app.last_trace = None
    llm_ms: float | None = None
    t0 = time.time()

    if mode == "event":
        ev = dict(body.get("event") or {})
        eid = ev.get("entity")
        if not eid or eid not in CATEGORIES:
            return {"ok": False, "error": f"unknown event entity: {eid!r}"}
        val = ev.get("value", sensors.get(eid))
        action = ev.get("action")  # None → 카테고리 기본(안전=alert)
        proposal = await app.process_reading(
            _reading(eid, val, ts), action=action, force_snapshot=True
        )
    else:  # proactive tick
        proposal = await app.tick()

    llm_ms = round((time.time() - t0) * 1000)

    trace: dict[str, Any] = app.last_trace or {}
    cand_eval = app.last_candidate_evaluation
    cur = app.state_cache.norm_values()

    # ── 응답 조립 ──────────────────────────────────────────────────────
    out: dict[str, Any] = {
        "ok": True,
        "mode": mode,
        "input": {
            "time_bucket": clock.time_bucket(),
            "occupancy": derive_occupancy(cur, CATEGORIES),
            "condition": app.condition,
            "execution_enabled": bool(cfg.get("execution_enabled", False)),
            "llm": {"enabled": bool(cfg.get("llm_enabled", True)), "model": model},
            "outdoor": outdoor if external is not None else None,
            "state": {k: cur.get(k) for k in sensors if k in CATEGORIES},
        },
        "candidates": _candidate_rows(cand_eval),
        "timing_ms": {"pipeline_and_llm": llm_ms},
    }

    # 평가(무행동 선택 포함)
    if cand_eval and cand_eval.get("evaluation") is not None:
        ev = cand_eval["evaluation"]
        out["evaluation"] = {
            "selected": f"{ev.selected_action().capability}.{ev.selected_action().action}",
            "is_noop": ev.selected_is_noop(),
            "rationale": ev.rationale,
        }

    # ── 능동 파이프라인 확장 트레이스: 트리거 · 2라인 · 임베딩 · 아비터 ──
    _facts = {
        "time_bucket": clock.time_bucket(),
        "occupancy": derive_occupancy(cur, CATEGORIES),
    }
    _pro = trace.get("proactive")
    out["trigger"] = {
        "kind": mode,
        "source": "rule" if mode == "tick" else "event",
        "origin": (
            getattr(_pro, "origin", None)
            if _pro is not None
            else (body.get("event") or {}).get("entity")
        ),
    }
    out["lines"] = _lines_view(cand_eval)
    if _DIRECTIVES.active():
        out["directives"] = {
            "active": [_directive_view(d) for d in _DIRECTIVES.active()]
        }
    emb = _embedding_view(cand_eval, _facts)
    if emb is not None:
        out["embedding"] = emb
    arb = _arbiter_view(cand_eval)
    if arb is not None:
        out["arbiter"] = arb

    # 스냅샷
    snap = trace.get("snapshot")
    if snap is not None:
        out["snapshot"] = {
            "nl_summary": getattr(snap, "nl_summary", ""),
            "snapshot_hash": getattr(snap, "snapshot_hash", "")[:16],
        }

    # Gate1(가드레일)
    g1 = trace.get("gate1")
    if g1 is not None:
        out["gate1"] = {
            "floor_tier": _tier_view(g1.floor_tier),
            "verdict": g1.gate1_verdict,
            "blocked": g1.blocked,
            "safety_critical": g1.safety_critical,
            "matched_rules": list(g1.matched_rule_ids),
        }

    # Gate2(LLM) + 최종 판단
    jd = trace.get("judgment")
    if jd is not None:
        out["gate2"] = {
            "verdict": jd.gate2_verdict,
            "model_id": jd.model_id,
            "degraded": jd.degraded,
            "confidence": round(float(jd.confidence), 3),
        }
        out["judgment"] = {
            "risk_tier": _tier_view(jd.risk_tier),
            "rationale": jd.rationale,
            "gate1_verdict": jd.gate1_verdict,
            "gate2_verdict": jd.gate2_verdict,
            "degraded": jd.degraded,
        }

    # 최종 제안 or 침묵
    if proposal is not None:
        tier = RiskTier.from_str(proposal.risk_tier)
        exec_policy = ExecutionPolicy(enabled=bool(cfg.get("execution_enabled", False)))
        decision = exec_policy.decide(proposal.action, tier)
        out["proposal"] = {
            "tier": _tier_view(tier),
            "capability": proposal.action.capability,
            "action": proposal.action.action,
            "entity_id": proposal.action.entity_id,
            "category": proposal.action.category,
            "required_confirmation": proposal.required_confirmation,
            "rationale": proposal.rationale,
            "expires_at": proposal.expires_at,
            "origin": proposal.action.params.get("__origin__"),
        }
        out["intervention"] = {
            "condition": app.condition,
            "required_confirmation": proposal.required_confirmation,
            "execution_decision": decision.value,
            "execution_enabled": exec_policy.enabled,
        }
        out["silence"] = False
    else:
        out["silence"] = True
        out["silence_reason"] = trace.get("silence_reason") or (
            "무행동(NoOp) 선택 — 어떤 행동도 무행동 기준선을 넘지 못함 → 침묵"
            if (cand_eval and cand_eval.get("evaluation") is not None
                and cand_eval["evaluation"].selected_is_noop())
            else "제안 조건 미충족(관찰/쿨다운/예산/무매칭) → 침묵"
        )

    return out


# ==========================================================================
# HTTP
# ==========================================================================
_STATIC_TYPES = {
    ".html": "text/html; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".js": "application/javascript; charset=utf-8",
    ".svg": "image/svg+xml",
    ".ico": "image/x-icon",
    ".json": "application/json; charset=utf-8",
}


class Handler(BaseHTTPRequestHandler):
    server_version = "LiotDemoLab/1.0"

    def log_message(self, *a: Any) -> None:  # quiet
        pass

    def _send(self, code: int, body: bytes, ctype: str) -> None:
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _json(self, code: int, obj: dict[str, Any]) -> None:
        self._send(code, json.dumps(obj, ensure_ascii=False).encode("utf-8"), _STATIC_TYPES[".json"])

    def do_GET(self) -> None:
        path = self.path.split("?", 1)[0]
        if path == "/api/catalog":
            self._json(200, _catalog_view())
            return
        if path == "/":
            path = "/app.html"  # 새 2D 단일 페이지(관제실)
        elif path in ("/lab", "/lab.html"):
            path = "/lab.html"  # 구 페이지는 P8 삭제 전까지 유지
        rel = path.lstrip("/")
        target = (WEB_DIR / rel).resolve()
        if not str(target).startswith(str(WEB_DIR)) or not target.is_file():
            self._send(404, b"not found", "text/plain; charset=utf-8")
            return
        ctype = _STATIC_TYPES.get(target.suffix, "application/octet-stream")
        self._send(200, target.read_bytes(), ctype)

    def _read_json(self) -> dict[str, Any] | None:
        try:
            length = int(self.headers.get("Content-Length", "0") or "0")
            return json.loads(self.rfile.read(length).decode("utf-8")) if length else {}
        except (ValueError, TypeError) as exc:
            self._json(400, {"ok": False, "error": f"bad json: {exc}"})
            return None

    def do_POST(self) -> None:
        path = self.path.split("?", 1)[0]
        if path == "/api/directive":
            body = self._read_json()
            if body is None:
                return
            if body.get("reset"):
                _DIRECTIVES.clear()
                self._json(200, {"ok": True, "parsed": [], "active": []})
                return
            parsed = _DIRECTIVES.add_text(
                str(body.get("text") or ""), source=str(body.get("source") or "chat")
            )
            self._json(
                200,
                {
                    "ok": True,
                    "parsed": [_directive_view(d) for d in parsed],
                    "active": [_directive_view(d) for d in _DIRECTIVES.active()],
                },
            )
            return
        if path != "/api/simulate":
            self._send(404, b"not found", "text/plain; charset=utf-8")
            return
        body = self._read_json()
        if body is None:
            return
        try:
            result = asyncio.run(_run(body))
        except Exception as exc:  # noqa: BLE001 - 데모: 어떤 실패도 화면에 노출
            self._json(200, {"ok": False, "error": f"{type(exc).__name__}: {exc}"})
            return
        self._json(200, result)


def main() -> None:
    host = os.environ.get("LIOT_LAB_HOST", "127.0.0.1")
    port = int(os.environ.get("LIOT_LAB_PORT", "8765") or "8765")
    os.chdir(REPO_ROOT)  # config/proactive 등 상대경로 로딩 보장
    httpd = ThreadingHTTPServer((host, port), Handler)
    print(f"LIOT 데모 랩: http://{host}:{port}/  (Ctrl+C 로 종료)")
    print(f"  정적 UI={WEB_DIR}  · 파이프라인=실제 LiotApp · Gate2=로컬 ollama")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        httpd.server_close()


if __name__ == "__main__":
    main()
