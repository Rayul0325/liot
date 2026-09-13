"""LIOT 데모 서버 — 브라우저에서 센서 값을 세팅하면 *실제* 파이프라인을 탄다.

가짜 시뮬이 아니라, 매 요청마다 진짜 ``LiotApp`` 을 만들어
L1 정규화 → L2 맥락/후보생성/평가 → L3 Gate1(가드레일)+Gate2(로컬 LLM) →
L4 개입까지 실제로 돌리고, 각 단계 산출물을 JSON 트레이스로 돌려준다.

  python -m liot.demo_server           # http://127.0.0.1:8765 (기본 gemma3:4b)

의존성: 표준 라이브러리만. LLM 은 로컬 ollama(HTTP :11434)의 gemma3:4b 기본.
관제실 UI(web/app.html)와 API(/api/*)를 같은 포트로 서빙(동일 출처): ``/`` 가
app.html, ``/assets/*`` 가 정적 자산, 나머지는 WEB_DIR 안에서 정적 파일 매핑.
데모 서버는 절대 액추에이터를 호출하지 않는다(제안-만; 실행 옵트인도 판정만 표시).
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

from liot.app import LiotApp
from liot.core.clock import Clock
from liot.core.contracts import SensorReading
from liot.core.risk import RiskTier, _reset_safety_extensions_for_tests
from liot.layers.l1_sensing.normalizer import normalize
from liot.layers.l2_context.context_builder import derive_occupancy
from liot.layers.l2_context.external_provider import FakeExternalProvider
from liot.layers.l2_context.feature_vector import (
    NEUTRAL_PREF,
    SIGNAL_TARGET,
    FakePreferenceEmbedder,
    apply_signal,
    cell_key,
)
from liot.layers.l2_context.nl_directive import DirectiveStore, UserDirective
from liot.layers.l2_context.proactive import ProactiveGenerator
from liot.layers.l3_judgment.guardrail import _reset_floor_extensions_for_tests
from liot.layers.l3_judgment.llm_judge import FakeLLM, LLMBackend
from liot.layers.l3_judgment.ollama_backend import HTTPOllamaTransport, OllamaBackend
from liot.layers.l4_execution.executor import ExecutionPolicy
from liot.registry import RegistryError, RuntimeRegistry

# ── 세션 공유 상태와 그 잠금 ─────────────────────────────────────────────
# 이 서버는 ThreadingHTTPServer 라 핸들러가 여러 스레드에서 동시에 돈다. 아래
# 세 전역(_DIRECTIVES·_REGISTRY·_PREF_CELLS)과 프로세스 전역 안전 확장은 전부
# 공유 가변 상태다. 잠금이 없으면 최소 두 가지가 실제로 깨진다:
#   1) 프리셋 롤백이 남의 등록을 지운다 — _apply_preset 은 시작 시 스냅샷을
#      뜨고 실패 시 restore 하므로, 그 사이 다른 요청이 등록한 센서·기기가
#      함께 사라진다(lost update).
#   2) 피드백 갱신이 유실된다 — _apply_feedback 은 _PREF_CELLS 를 읽어 새 dict
#      로 갈아끼우는 read-modify-write 라, 동시 갱신 중 하나가 덮인다.
# 그래서 공유 상태를 만지는 구간은 전부 이 락 아래에서만 돈다. 재진입 가능
# (RLock)이라 락을 쥔 코어 함수끼리 중첩 호출해도 안전하다.
#
# 시뮬레이트(_run)는 **앞머리의 스냅샷 구간만** 이 락 아래에서 돈다 — 레지스트리
# 파생물(카탈로그·룰셋·카테고리·실행 확장분)·선호 셀맵·활성 지시를 한 번에 떠
# 두고, 파이프라인(LLM 네트워크 호출 포함)과 응답 조립은 락 밖에서 그 스냅샷만
# 읽는다. 락을 쥔 채 파이프라인을 돌리면 LLM 을 기다리는 동안 등록·피드백·카탈로그
# 조회까지 전부 멈춘다. HTTP 응답 쓰기도 락 밖에서 한다 — 느린 수신자가 소켓
# 타임아웃까지 락을 붙들지 못하게. 정적 자산 GET 은 락을 잡지 않는다.
STATE_LOCK = threading.RLock()

# 활성 자연어 지시(세션 스코프). /api/directive 로 추가·초기화, /api/simulate tick 에 반영.
_DIRECTIVES = DirectiveStore()

# 런타임 등록 레지스트리(세션 스코프 싱글턴). 센서·기기·행동의 단일 원천 —
# /api/catalog·/api/simulate·/api/registry/* 가 전부 이 인스턴스를 읽고 쓴다.
# (demo_server 는 요청마다 LiotApp 을 새로 만들므로 등록 직후 자동 반영된다.)
_REGISTRY = RuntimeRegistry()

# 선호 임베딩 셀맵(세션 스코프). 폰 알림 응답(수락/거절/무시)이 /api/feedback 로
# 이 맵을 온라인 EMA 갱신하고(feature_vector.apply_signal), 매 /api/simulate 가
# app.set_preference_cells 로 주입해 다음 틱의 선호 차원(user_preference_fit)과
# embedding 트레이스 표시에 반영한다. 빈 맵이면 중립(0.5)만 — 이걸로 "임베딩이
# 항상 중립으로 표시되던" 데모 버그가 해소된다. /api/registry/reset 이 함께 비운다.
_PREF_CELLS: dict[str, float] = {}

# 피드백 셀 키는 feature_vector.cell_key 가 만드는 형식만 받는다:
# `time_bucket|occupancy|capability:action`. UI 는 서버가 만든 셀(embedding.cell)만
# 되돌려 보내므로 정상 흐름엔 영향이 없다. 형식 검사가 없으면 아무 문자열이나
# _PREF_CELLS 의 새 칸이 돼 맵이 끝없이 분다(그런 키는 /api/simulate 가 조회도
# 안 한다). 형식을 지켜도 capability:action 은 등록으로 계속 늘 수 있어 칸 수에도
# 상한을 둔다 — 넘치면 새 칸만 거부하고 기존 칸 갱신은 계속 받는다.
_CELL_KEY_RE = re.compile(
    r"(?:morning|day|evening|night)\|(?:occupied|vacant|unknown)\|[^|:\s]+:[^|:\s]+"
)
MAX_CELL_KEY_LEN = 200
MAX_PREF_CELLS = 512


def _apply_feedback(cell: Any, signal: Any) -> dict[str, Any]:
    """폰 알림 응답(수락/거절/무시)을 세션 선호 셀맵에 온라인 EMA 로 반영한다.

    ``/api/feedback`` 엔드포인트와 테스트가 공유하는 코어. 세션 셀맵 ``_PREF_CELLS``
    을 갱신하고 갱신 전/후 선호값을 돌려준다 — UI 가 "선호 0.50 → 0.65" 로 표시하고
    다음 ``/api/simulate`` 가 이 값을 반영한다(수락=선호↑ · 거절=선호↓ · 무시=약한
    선호↓, feature_vector.SIGNAL_TARGET). ``ok:False`` 면 검증 실패(서버가 400 매핑).
    """
    global _PREF_CELLS
    cell = str(cell or "").strip()
    signal = str(signal or "").strip()
    if not cell:
        return {"ok": False, "error": "피드백 대상 셀(cell)이 없습니다.", "field": "cell"}
    if len(cell) > MAX_CELL_KEY_LEN or not _CELL_KEY_RE.fullmatch(cell):
        return {
            "ok": False,
            "error": "피드백 대상 셀(cell) 형식이 올바르지 않습니다"
                     " (시간대|재실|capability:action).",
            "field": "cell",
        }
    if signal not in SIGNAL_TARGET:
        return {
            "ok": False, "error": f"알 수 없는 신호입니다: {signal or '(빈값)'}",
            "field": "signal",
        }
    with STATE_LOCK:
        # 칸 수 확인과 추가가 한 덩어리여야 동시 요청이 상한을 함께 넘지 못한다.
        full = cell not in _PREF_CELLS and len(_PREF_CELLS) >= MAX_PREF_CELLS
        if not full:
            before = round(float(_PREF_CELLS.get(cell, NEUTRAL_PREF)), 3)
            _PREF_CELLS = apply_signal(_PREF_CELLS, cell, signal)
            after = round(float(_PREF_CELLS.get(cell, NEUTRAL_PREF)), 3)
    if full:
        return {
            "ok": False,
            "error": f"선호 셀이 최대 {MAX_PREF_CELLS}칸에 도달해 새 셀을 더할 수 없습니다"
                     " (초기화 후 다시 시도하세요).",
            "field": "cell",
        }
    return {"ok": True, "cell": cell, "signal": signal, "before": before, "after": after}


# 프리셋 번들 적용 단계 → 실패 사유의 한글 접두사(어느 항목에서 막혔는지).
_PRESET_STAGE_KO = {"sensor": "센서", "device": "기기", "rule": "규칙"}


def _apply_preset(payload: Any) -> tuple[dict[str, Any], int]:
    """프리셋(등록 번들)을 서버측에서 **원자적으로** 적용한다(``/api/registry/preset``).

    payload = ``{name?, reset?, sensors:[...], devices:[...], rules:[...], values:{...}}``.
    ``reset`` 이 참이면 레지스트리를 먼저 초기화하고, 이어서 sensors → devices →
    rules 를 **기존 add_* 검증 경로 그대로** 등록한다(우회 없음 — 폼으로 못 넣는
    것은 프리셋으로도 못 넣는다). 한 항목이라도 거부되면 프리셋 이전 상태로
    롤백하고(레지스트리 스냅샷/복원 + 선호 셀맵 복원) 어느 단계에서 막혔는지
    한글 사유를 반환한다(서버가 HTTP 400). 성공하면 적용 개수와 ``values`` 를
    돌려준다 — ``values`` 는 클라이언트가 보드 컨트롤 상태로 반영한다.

    반환은 ``(응답 dict, HTTP 상태)``. 엔드포인트와 테스트가 공유하는 코어다.

    프로세스 전역 안전 확장(안전 카테고리·카테고리 하한)은 롤백하지 않는다 —
    확장 전용(escalate만)이라 남겨도 안전을 낮추지 않으며, 되돌리는 것이 오히려
    확장-전용 불변식을 깨기 때문이다. 프리셋 ``reset`` 도 안전 확장은 건드리지
    않는다(전체 초기화는 ``/api/registry/reset`` 이 담당). 출하 프리셋 4종은
    안전 카테고리를 등록하지 않으므로 실무상 확장이 생기지 않는다.
    """
    if not isinstance(payload, dict):
        return {"ok": False, "error": "프리셋 정의가 올바르지 않습니다."}, 400

    # 최상위 필드의 타입부터 본다. 예전엔 아래 list(123)·dict([1, 2]) 가 try 밖에서
    # TypeError 로 터져 400 없이 연결이 끊겼다. 빠진 필드(None)는 빈 값으로 본다.
    shapes: tuple[tuple[str, type, str], ...] = (
        ("sensors", list, "배열이어야"),
        ("devices", list, "배열이어야"),
        ("rules", list, "배열이어야"),
        ("values", dict, "객체여야"),
    )
    for field, kind, must_be in shapes:
        raw = payload.get(field)
        if raw is not None and not isinstance(raw, kind):
            return {
                "ok": False,
                "error": f"프리셋 정의가 올바르지 않습니다: {field} 는 {must_be} 합니다"
                         f" (받은 값: {type(raw).__name__}).",
                "field": field,
            }, 400

    sensors = list(payload.get("sensors") or [])
    devices = list(payload.get("devices") or [])
    rules = list(payload.get("rules") or [])
    values = dict(payload.get("values") or {})
    reset = bool(payload.get("reset"))

    # 항목 하나하나가 dict 인지 **아무것도 건드리기 전에** 확인한다. 예전에는
    # reset 을 먼저 실행한 뒤 dict(123) 에서 TypeError 가 났고, 그 예외는
    # except RegistryError 를 그대로 빠져나가 롤백도 응답도 없이 연결이 끊겼다
    # (레지스트리와 선호 셀맵이 통째로 날아간 채로).
    for field, items in (("sensors", sensors), ("devices", devices), ("rules", rules)):
        for item in items:
            if not isinstance(item, dict):
                where = _PRESET_STAGE_KO.get(field[:-1], field)
                return {
                    "ok": False,
                    "error": f"프리셋 적용 실패({where}): 항목은 객체여야 합니다"
                             f" (받은 값: {type(item).__name__}).",
                    "field": field,
                }, 400

    # 스냅샷→등록→(실패 시)롤백 전체가 한 덩어리여야 한다. 락 밖에서 돌면
    # 롤백이 그 사이 다른 요청이 등록한 것까지 되돌려 지운다(lost update).
    with STATE_LOCK:
        snap = _REGISTRY.snapshot()
        pref_snap = dict(_PREF_CELLS)
        applied = {"sensors": 0, "devices": 0, "rules": 0}
        stage = ""
        try:
            if reset:
                _REGISTRY.reset()
                _PREF_CELLS.clear()
            stage = "sensor"
            for s in sensors:
                _REGISTRY.add_sensor(dict(s))
                applied["sensors"] += 1
            stage = "device"
            for d in devices:
                d = dict(d)
                _REGISTRY.add_device(
                    entity_id=d.get("entity_id", ""),
                    name=d.get("name", ""),
                    capability=d.get("capability", ""),
                    category=d.get("category", ""),
                    actions=d.get("actions") or [],
                    safety_critical=d.get("safety_critical", False),
                    floor=d.get("floor"),
                )
                applied["devices"] += 1
            stage = "rule"
            for r in rules:
                _REGISTRY.add_rule(dict(r))
                applied["rules"] += 1
        except RegistryError as exc:
            # 원자 롤백: 프리셋이 건드린 등록 상태·선호 셀맵을 이전으로 되돌린다.
            _REGISTRY.restore(snap)
            _PREF_CELLS.clear()
            _PREF_CELLS.update(pref_snap)
            where = _PRESET_STAGE_KO.get(stage, stage)
            return {
                "ok": False,
                "error": f"프리셋 적용 실패({where}): {exc.message}",
                "field": exc.field,
            }, 400
        except Exception as exc:  # noqa: BLE001 - 상태 파괴 방지가 우선
            # 검증을 통과한 뒤에도 예상 못 한 예외가 날 수 있다(레지스트리 내부
            # 변경, 새 필드 등). 그때 롤백 없이 빠져나가면 reset 이 이미 지운
            # 상태가 그대로 남는다 — 어떤 예외든 반드시 이전 상태로 되돌린다.
            _REGISTRY.restore(snap)
            _PREF_CELLS.clear()
            _PREF_CELLS.update(pref_snap)
            where = _PRESET_STAGE_KO.get(stage, stage)
            return {
                "ok": False,
                "error": f"프리셋 적용 실패({where}): {type(exc).__name__}: {exc}",
                "field": None,
            }, 400
    return {"ok": True, "applied": applied, "values": values}, 200


def _directive_view(d: UserDirective) -> dict[str, Any]:
    return {
        "capability": d.capability,
        "constraint": d.constraint,
        "entity_id": d.entity_id,
        "source": d.source,
        "raw_text": d.raw_text,
        "until": d.until,
    }


def _apply_directive(body: dict[str, Any]) -> tuple[dict[str, Any], int]:
    """자연어 지시 추가·초기화(``/api/directive``). 반환은 ``(응답 dict, HTTP 상태)``.

    엔드포인트와 테스트가 공유하는 코어. 락은 여기서만 쥐고, 응답 쓰기는 호출자가
    락 밖에서 한다 — 예전엔 초기화 분기가 락을 쥔 채 응답을 써서, 느린 수신자가
    소켓 타임아웃(Handler.timeout)까지 전역 락을 붙들 수 있었다.

    ``until``(선택, ISO8601)이 있으면 그 시각부터 지시가 만료된다. 형식이 틀리면
    아무것도 저장하지 않고 400(field=until). 응답의 ``active`` 는 **실시간으로
    거르지 않은** 저장 목록이다 — 만료 판정은 시뮬레이션 시각(2026-06-02)을 아는
    ``/api/simulate`` 의 몫이고, 여기서 실시간으로 거르면 시뮬레이션 안에서는 아직
    유효한 지시까지 전부 만료로 보인다.
    """
    if body.get("reset"):
        with STATE_LOCK:
            _DIRECTIVES.clear()
        return {"ok": True, "parsed": [], "active": []}, 200
    text = str(body.get("text") or "")
    source = str(body.get("source") or "chat")
    raw_until = body.get("until")
    until = "" if raw_until is None else str(raw_until)
    try:
        with STATE_LOCK:
            parsed = _DIRECTIVES.add_text(text, source=source, until=until)
            active = _DIRECTIVES.all()
    except (ValueError, OverflowError):
        # add_text 는 저장 전에 형식을 검증한다 — 여기로 오면 아무것도 저장되지 않았다.
        # OverflowError: 0001-01-01T00:00+01:00 처럼 UTC 로 옮기면 범위를 벗어나는 시각.
        return {
            "ok": False,
            "error": f"지시 만료 시각(until)을 해석할 수 없습니다: {until!r}"
                     " (ISO8601 시각, 예: 2026-06-02T22:00:00+09:00).",
            "field": "until",
        }, 400
    return {
        "ok": True,
        "parsed": [_directive_view(d) for d in parsed],
        "active": [_directive_view(d) for d in active],
    }, 200


# 끝까지 resolve 한다. `web` 자체가 심볼릭 링크면 do_GET 의 `(WEB_DIR / rel).resolve()`
# 는 링크 너머 실제 경로가 되는데 WEB_DIR 은 링크 경로 그대로라, is_relative_to 가
# 늘 False 여서 정적 파일이 전부 404 가 된다.
WEB_DIR = (Path(__file__).resolve().parent.parent / "web").resolve()

# 요청 본문 상한. 이 서버가 받는 건 센서 값·등록 폼·프리셋 번들뿐이라 1MiB 면
# 넉넉하다. 상한이 없으면 Content-Length 만 크게 불러 놓고 본문을 안 보내는
# 것만으로 핸들러 스레드를 잡아둘 수 있다(ThreadingHTTPServer = 연결당 스레드).
MAX_BODY_BYTES = 1 << 20
REPO_ROOT = Path(__file__).resolve().parent.parent

# 시간대(UI) → 대표 UTC 시각(clock.time_bucket 기준: 06-11 morning / 11-17 day /
# 17-22 evening / 그 외 night). 능동 규칙의 time_bucket 매칭에 쓰인다.
_TIME_HOUR = {"morning": "08", "day": "13", "evening": "19", "night": "23"}


def _reading(entity_id: str, value: Any, ts: str) -> SensorReading:
    """UI 값 → SensorReading. 강제변환·미등록 거부는 레지스트리가 담당한다.

    미등록 센서면 ``RegistryError`` 를 던져 서버가 HTTP 400 으로 매핑한다.
    """
    raw = _REGISTRY.coerce_value(entity_id, value)
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

    데이터 원천은 런타임 레지스트리(``_REGISTRY``) 단일 지점 — 브라우저에서 새
    센서·기기·행동을 등록하면 이 응답이 즉시 반영된다. JS 는 이 응답만으로
    보드를 렌더한다(엔티티 ID·한글 라벨 하드코딩 0).
      * 센서 = ``_REGISTRY.sensors_meta()`` (수치=number+단위/범위, 나머지=bool).
      * 기기·행동 = ``_REGISTRY.to_catalog().available_actions()`` 를 entity_id 로 묶음.
      * 위험등급 legend = ``RiskTier.label_ko`` 단일 원천(한글 라벨 이중관리 없음).
    """
    # 센서·카탈로그·기기명·규칙을 여러 번 나눠 읽으므로, 그 사이 등록이 끼면
    # 서로 어긋난 조각이 한 응답에 섞인다. 한 스냅샷으로 묶어 읽는다.
    with STATE_LOCK:
        sensors = _REGISTRY.sensors_meta()

        catalog = _REGISTRY.to_catalog()
        grouped: dict[str, dict[str, Any]] = {}
        for act in catalog.available_actions():
            eid = str(act["entity_id"])
            dev = grouped.get(eid)
            if dev is None:
                dev = grouped[eid] = {
                    "entity_id": eid,
                    "name": _REGISTRY.device_name(eid),
                    "category": act.get("category"),
                    "capability": act.get("capability"),
                    "actions": [],
                }
            tier = RiskTier.from_str(str(act["tier"]))
            dev["actions"].append(
                {"action": act["action"], "tier": act["tier"], "tier_ko": tier.label_ko}
            )

        rules = _REGISTRY.rules_meta()

    tiers = {
        str(int(t)): {"name": t.name.lower(), "label_ko": t.label_ko} for t in RiskTier
    }
    return {
        "sensors": sensors,
        "devices": list(grouped.values()),
        "tiers": tiers,
        "rules": rules,
    }


def _build_backend(cfg: dict[str, Any]) -> tuple[LLMBackend, str | None]:
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
    empty: dict[str, list[Any]] = {"line_a": [], "line_b": []}
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
    cand_eval: dict[str, Any] | None, facts: dict[str, Any], pref_cells: dict[str, float]
) -> dict[str, Any] | None:
    """선택 후보의 선호 임베딩 셀 값(세션 셀맵 반영 — 피드백 후 중립에서 이동).

    ``pref_cells`` 는 이번 판단에 주입한 **스냅샷**이어야 한다. 전역 ``_PREF_CELLS``
    를 다시 읽으면 파이프라인이 도는 사이 들어온 피드백이 섞여, 평가가 쓰지 않은
    선호값이 트레이스에 표시된다.
    """
    if not cand_eval or cand_eval.get("evaluation") is None:
        return None
    ev = cand_eval["evaluation"]
    sel = ev.scored[ev.selected_index].candidate
    if sel.capability == "noop":
        return None
    pref = FakePreferenceEmbedder(pref_cells).preference_for(sel, facts)
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

    # event 페이로드 검증은 공유 상태와 무관한 순수 검사다 — 락을 잡기 전에 끝낸다.
    event_spec: dict[str, Any] = {}
    event_entity: Any = None
    if mode == "event":
        event_spec = dict(body.get("event") or {})
        event_entity = event_spec.get("entity")
        if not event_entity:
            return {"ok": False, "error": "event 에 entity 가 없습니다"}

    # ── 공유 상태 스냅샷 (락은 여기까지만) ─────────────────────────────
    # 레지스트리 파생물은 전부 새 객체(to_catalog=deepcopy, categories/ruleset=
    # 새로 조립)라, 락 아래에서 한 번 떠 두면 이후 등록이 끼어도 이번 판단은
    # 일관된 상태 위에서 끝난다. 값 강제변환(coerce)도 레지스트리를 읽으므로
    # 여기서 미리 마친다 — 미등록 엔티티면 파이프라인 시작 전에 RegistryError.
    # 선호 셀맵·활성 지시도 여기서 뜬 스냅샷을 판단과 응답 조립이 **함께** 쓴다 —
    # 응답 단계에서 전역을 다시 읽으면 판단이 보지 않은 값이 화면에 섞인다.
    #
    # **파이프라인 자체는 락 밖에서 돈다.** 안에서 LLM 네트워크 호출이 일어나고
    # (OllamaBackend, 기본 timeout 60초) 락을 쥔 채 돌면 그 시간 동안
    # /api/catalog·/api/feedback·등록까지 전부 멈춘다 — 보드가 통째로 얼어붙는다.
    with STATE_LOCK:
        categories = _REGISTRY.categories()
        catalog = _REGISTRY.to_catalog()
        # 능동 생성기에 레지스트리 병합 룰셋(config 베이스라인 + 런타임 임계 규칙)을
        # 공급한다 — 등록한 규칙이 다음 tick 부터 propose()/후보집합에 흐른다(D5).
        proactive = ProactiveGenerator(
            ruleset=_REGISTRY.proactive_ruleset(),
            catalog=catalog,
            categories=categories,
            clock=clock,
        )
        app = LiotApp(
            clock=clock,
            catalog=catalog,
            categories=categories,
            llm_backend=backend,
            condition=str(cfg.get("condition") or "B"),
            external_provider=external,
            proactive=proactive,
        )
        # 폰 피드백으로 갱신된 세션 선호 셀맵을 이번 틱에 주입한다. 빈 맵이면 중립만
        # 반환(기존 동작 동일) — 수락 피드백 후엔 해당 셀의 user_preference_fit 이 상승해
        # 평가·embedding 트레이스에 반영된다.
        pref_cells = dict(_PREF_CELLS)
        app.set_preference_cells(pref_cells)
        # 활성 자연어 지시를 앱에 반영(tick 의 comfort 후보 억제; 안전은 억제 불가).
        # tick() 에서만 쓰이므로 seeding 앞에 둬도 동작이 같다.
        # until 만료는 **시뮬레이션 시각**(clock, 2026-06-02) 기준으로 판정한다 — 실시간
        # 기준이면 오늘이 시뮬레이션 날짜보다 뒤라 until 이 붙은 지시가 전부 만료로 보인다.
        directives = _DIRECTIVES.active(now=clock.now_dt())
        app.set_directives(directives)
        # 카탈로그 파생 실행 확장분 — 응답 조립 단계에서 쓴다.
        exec_extras = _REGISTRY.executable_extras()

        seed_readings = [_reading(eid, val, ts) for eid, val in sensors.items()]
        event_reading = None
        event_action = None
        if mode == "event":
            event_action = event_spec.get("action")  # None → 카테고리 기본(안전=alert)
            event_reading = _reading(
                event_entity, event_spec.get("value", sensors.get(event_entity)), ts
            )

    # 1) 모든 센서를 관찰로 주입(맥락/캐시 갱신, 대개 침묵).
    for reading in seed_readings:
        await app.process_reading(reading, force_snapshot=False)

    # seeding 관찰들이 남긴 last_trace 를 비운다 — tick 이 무행동(NoOp)으로
    # 조기 종료하면 last_trace 를 안 채우므로, 비우지 않으면 마지막 seed 트레이스가
    # stale 로 표시된다. 이렇게 해야 침묵 틱은 후보/평가만 보이고 stale 스냅샷은 안 뜬다.
    app.last_trace = None
    llm_ms: float | None = None
    t0 = time.time()

    if event_reading is not None:
        proposal = await app.process_reading(
            event_reading, action=event_action, force_snapshot=True
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
            "occupancy": derive_occupancy(cur, categories),
            "condition": app.condition,
            "execution_enabled": bool(cfg.get("execution_enabled", False)),
            "llm": {"enabled": bool(cfg.get("llm_enabled", True)), "model": model},
            "outdoor": outdoor if external is not None else None,
            "state": {k: cur.get(k) for k in sensors if k in categories},
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
        "occupancy": derive_occupancy(cur, categories),
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
    if directives:
        out["directives"] = {"active": [_directive_view(d) for d in directives]}
    emb = _embedding_view(cand_eval, _facts, pref_cells)
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
        # 카탈로그 파생 실행 확장분(등록된 breath 이하 비-안전-액추에이터 행동)을
        # 정책에 주입 — 등록 행동이 L4 실행 결정(AUTO/NOTIFY_ONLY)에 반영된다.
        # FORBIDDEN_OPEN(lock.unlock·valve.open)은 어떤 확장에도 못 든다(불변).
        exec_policy = ExecutionPolicy(
            enabled=bool(cfg.get("execution_enabled", False)),
            extra_executable=exec_extras,
        )
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
    # 소켓 수준 타임아웃. StreamRequestHandler.setup() 이 이 값을 연결에 건다.
    # 없으면 헤더만 보내고 본문을 안 보내는 연결이 스레드를 영원히 붙든다
    # (실측: 그런 요청 5개로 스레드 2 → 7, 전부 응답 없음). 파이프라인 계산은
    # 소켓 연산이 아니라서 느린 LLM 틱이 이 타임아웃에 걸리지 않는다.
    timeout = 30

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
        rel = path.lstrip("/")
        # 경로 '포함 관계'로 판정한다. 예전 검사는 문자열 접두사 비교라
        # `web` 로 시작하는 형제 디렉터리(web_secret·web-dist·website…)가 전부
        # 통과했다 — /../web_secret/leak.html 이 200 으로 새어 나갔다(실측).
        try:
            target = (WEB_DIR / rel).resolve()
        except (OSError, ValueError):
            self._send(404, b"not found", "text/plain; charset=utf-8")
            return
        if not target.is_relative_to(WEB_DIR) or not target.is_file():
            self._send(404, b"not found", "text/plain; charset=utf-8")
            return
        ctype = _STATIC_TYPES.get(target.suffix, "application/octet-stream")
        self._send(200, target.read_bytes(), ctype)

    def _read_json(self) -> dict[str, Any] | None:
        """요청 본문을 JSON **객체**로 읽는다. 아니면 400 을 보내고 None.

        예전 판은 Content-Length 를 그대로 믿고 `rfile.read(length)` 했다:
          - 음수(-1)면 EOF 까지 읽으려 해서 연결이 열려 있는 한 스레드가 묶였다.
          - 상한이 없어 1억을 부르고 5바이트만 보내도 마찬가지였다.
          - 반환 타입이 dict 로 선언돼 있었지만 실제로는 JSON 이 주는 대로
            돌려줘서, 배열 본문(`["a"]`)이 오면 호출부의 body.get(...) 이
            AttributeError 로 터지고 응답 없이 연결이 끊겼다(3개 엔드포인트).
        """
        raw = self.headers.get("Content-Length", "0") or "0"
        try:
            length = int(raw)
        except (TypeError, ValueError):
            self._json(400, {"ok": False, "error": "Content-Length 헤더가 올바르지 않습니다."})
            return None
        if length < 0 or length > MAX_BODY_BYTES:
            self._json(
                400,
                {"ok": False, "error": f"본문 길이가 허용 범위를 벗어났습니다"
                                       f" (0~{MAX_BODY_BYTES} 바이트, 받은 값: {length})."},
            )
            return None
        if length == 0:
            return {}
        try:
            data = self.rfile.read(length)
        except OSError as exc:  # 소켓 타임아웃 포함(본문이 오다 말았다)
            self._json(400, {"ok": False, "error": f"본문을 읽지 못했습니다: {type(exc).__name__}"})
            return None
        if len(data) != length:
            self._json(400, {"ok": False, "error": "본문이 Content-Length 보다 짧습니다."})
            return None
        try:
            payload = json.loads(data.decode("utf-8"))
        except (ValueError, UnicodeDecodeError) as exc:
            self._json(400, {"ok": False, "error": f"bad json: {exc}"})
            return None
        except RecursionError:
            # 1MiB 안에서도 `[[[[…` 를 수만 겹 쌓으면 디코더가 RecursionError 를
            # 던진다. ValueError 가 아니라서 예전엔 그대로 새어 응답 없이 끊겼다.
            self._json(400, {"ok": False, "error": "요청 본문 JSON 이 너무 깊게 중첩돼 있습니다."})
            return None
        if not isinstance(payload, dict):
            self._json(
                400,
                {"ok": False, "error": "요청 본문은 JSON 객체여야 합니다"
                                       f" (받은 값: {type(payload).__name__})."},
            )
            return None
        return payload

    def do_POST(self) -> None:
        path = self.path.split("?", 1)[0]
        if path == "/api/directive":
            body = self._read_json()
            if body is None:
                return
            # 락은 코어 안에서만 — 응답은 락을 놓은 뒤에 쓴다.
            result, code = _apply_directive(body)
            self._json(code, result)
            return
        if path == "/api/feedback":
            self._feedback_post()
            return
        if path.startswith("/api/registry/"):
            self._registry_post(path)
            return
        if path != "/api/simulate":
            self._send(404, b"not found", "text/plain; charset=utf-8")
            return
        body = self._read_json()
        if body is None:
            return
        try:
            # _run 이 앞머리에서 레지스트리 스냅샷만 잠그고 파이프라인은 락 밖에서
            # 돈다(LLM 호출 동안 다른 요청을 막지 않기 위해).
            result = asyncio.run(_run(body))
        except RegistryError as exc:  # 미등록 엔티티 값 등 → 400 구조화 에러
            self._json(400, {"ok": False, "error": exc.message, "field": exc.field})
            return
        except Exception as exc:  # noqa: BLE001 - 데모: 어떤 실패도 화면에 노출
            self._json(200, {"ok": False, "error": f"{type(exc).__name__}: {exc}"})
            return
        self._json(200, result)

    def _feedback_post(self) -> None:
        """폰 알림 응답(수락/거절/무시) → 세션 선호 셀 갱신(코어는 ``_apply_feedback``)."""
        body = self._read_json()
        if body is None:
            return
        result = _apply_feedback(body.get("cell"), body.get("signal"))
        self._json(200 if result.get("ok") else 400, result)

    def _registry_post(self, path: str) -> None:
        """런타임 등록 엔드포인트(sensor·device·reset). 검증 실패는 400."""
        if path == "/api/registry/reset":
            # 데모 리셋 = 완전 초기화. 인스턴스 등록분(_REGISTRY)에 더해 프로세스
            # 전역 확장 안전 카테고리·카테고리 하한도 비운다(내장 set 은 불변).
            # 다섯 가지를 한 덩어리로 비워야 반쯤 초기화된 상태가 노출되지 않는다.
            # _DIRECTIVES 를 빼먹으면 리셋했다고 믿는 세션에서 옛 지시가 계속
            # comfort 후보를 억눌러, tick 이 이유 없이 침묵한다.
            with STATE_LOCK:
                _REGISTRY.reset()
                _reset_safety_extensions_for_tests()
                _reset_floor_extensions_for_tests()
                _PREF_CELLS.clear()  # 세션 선호 셀맵도 초기화(중립 복귀)
                _DIRECTIVES.clear()  # 활성 자연어 지시도 초기화
            self._json(200, {"ok": True})
            return
        body = self._read_json()
        if body is None:
            return
        if path == "/api/registry/preset":
            result, code = _apply_preset(body)
            self._json(code, result)
            return
        try:
            if path == "/api/registry/sensor":
                with STATE_LOCK:
                    rec = _REGISTRY.add_sensor(body)
                self._json(200, {"ok": True, "sensor": rec})
                return
            if path == "/api/registry/device":
                with STATE_LOCK:
                    rec = _REGISTRY.add_device(
                        entity_id=body.get("entity_id", ""),
                        name=body.get("name", ""),
                        capability=body.get("capability", ""),
                        category=body.get("category", ""),
                        actions=body.get("actions") or [],
                        safety_critical=body.get("safety_critical", False),
                        floor=body.get("floor"),
                    )
                self._json(200, {"ok": True, "device": rec})
                return
            if path == "/api/registry/rule":
                with STATE_LOCK:
                    rec = _REGISTRY.add_rule(body)
                self._json(200, {"ok": True, "rule": rec})
                return
        except RegistryError as exc:
            self._json(400, {"ok": False, "error": exc.message, "field": exc.field})
            return
        self._send(404, b"not found", "text/plain; charset=utf-8")


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
