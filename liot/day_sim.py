"""하루 시뮬레이터 — 실제 ``LiotService`` 위에서 가상 시계(집 시간대)를 15분씩 옮기며 며칠을 돈다.

데몬 코드를 그대로 돈다(데모용 우회 없음):
  - 실행마다 임시 디렉터리에 SQLite 파일을 만들고 ``LiotService`` 를 조립한다. 끝나면 지운다.
  - 알림 송신기는 가짜(받아 적기만). 판정기는 FakeLLM. 날씨는 FakeExternalProvider(단계마다
    ``set_values``). 레지스트리·카탈로그·능동 규칙은 출하본 그대로. 시계는 Asia/Seoul.
  - 단계마다: 시계 이동 → 대기 중인 응답(거주자 성향·보는 사람) 적용 → 대본의 센서 값을
    ``handle_reading`` → ``run_tick`` → ``drain_phase2`` → ``ladder_tick``.
  - 응답은 폰의 서명 링크를 누르는 실제 경로(``_route_feedback`` + ``sign_feedback`` +
    살아 있는 nonce)로 넣는다 — 1회용 토큰·선호 갱신(EMA)·사다리 확인이 실제처럼 돈다.
    [도움됐어요] = accepted, [그만해] = dismissed(실제 규칙상 30일 차단), [무시] = 누르지 않고
    만료(실제 ``_apply_expired_signals`` 가 신호로 바꾼다).
  - 보는 사람의 응답은 HTTP 스레드에서 큐에만 넣고, 시뮬레이터 스레드가 다음 단계 경계에서
    적용한다(서비스를 HTTP 스레드에서 만지지 않는다).

같은 씨앗이면 같은 사건 목록이다(가짜 판정기·고정 대본·씨앗 난수). 제안 id 는 서비스의
uuid 대신 실행 안 순번(``p1``, ``p2``…)으로 내보낸다.

사건(``emit(stage, data)``): ``day.start`` · ``step``(바뀐 것이 있을 때 + 매시 정각) ·
``proposal`` · ``digest`` · ``feedback`` · ``tray.review``(22시 모아보기 확인) · ``safety`` ·
``day.end``(그날 지표). 실행 관리자(live_runs)가 끝에 ``done`` 을 붙인다. 화면이 계산하지 않도록
사건마다 한국어 문구(``line_ko``·``title_ko``·``state_ko`` 등)와 그날 지표(``metrics``)를 싣는다.

거주자 특성(취약 여부 등)은 다루지 않는다 — 시뮬레이션 집에는 거주자 등록이 없다.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import queue
import random
import re
import secrets
import shutil
import tempfile
import threading
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, TypeVar
from zoneinfo import ZoneInfo

from liot.app import LiotApp
from liot.cheap_llm import ModelConfigError, ReasonResult
from liot.core.clock import Clock
from liot.core.contracts import (
    PM25_BAD_MIN,
    PM25_GOOD_MAX,
    CandidateAction,
    ContextSnapshot,
    EvaluationResult,
    ExternalContext,
    Judgment,
    Proposal,
    SensorReading,
    is_noop,
)
from liot.core.risk import RiskTier, is_safety_category
from liot.core.severity import SeverityLevel
from liot.day_cloud import CloudKit, build_cloud_kit
from liot.db.database import init_db
from liot.layers.l1_sensing.normalizer import normalize
from liot.layers.l2_context.evaluator import CandidateEvaluator
from liot.layers.l2_context.external_provider import FakeExternalProvider
from liot.layers.l2_context.feature_vector import NEUTRAL_PREF, load_cells
from liot.layers.l2_context.feedback_store import InMemoryFeedbackStore, candidate_key
from liot.layers.l2_context.proactive import (
    PROACTIVE_TIER_CEILING,
    ProactiveDecision,
    ProactiveGenerator,
    validate_proactive_candidate,
)
from liot.layers.l2_context.proactive_rules import ProactiveRule
from liot.layers.l3_judgment.decision_backend import EvidenceBuilder
from liot.layers.l3_judgment.guardrail import Gate1Result, Guardrail
from liot.layers.l3_judgment.llm_judge import FakeLLM, LLMBackend, LLMResult, LLMUnavailable
from liot.layers.l4_execution import escalation
from liot.layers.l4_execution.escalation import GUIDANCE_119_TEXT
from liot.layers.l4_execution.notifier import sign_feedback
from liot.layers.l4_execution.ntfy_transport import HTTPNtfyTransport
from liot.live_runs import LiveRequestError, LiveRun, LiveRuns, RunContext
from liot.luna_shadow import ShadowResult
from liot.memory_client import HouseMemory, MemoryCard, MemoryRefused, ResidentMemory
from liot.registry import RuntimeRegistry
from liot.secrets_env import DEFAULT_SECRETS_PATH, KNOWN_KEYS, Secrets, load_secrets
from liot.service import DispatchResult, LiotService

__all__ = [
    "HOME_TZ",
    "MAX_DAYS",
    "MAX_PACE_MS",
    "MODELS",
    "PERSONAS",
    "SCENES",
    "DayRequest",
    "DayRuns",
    "DaySim",
    "build_script",
    "parse_day_request",
]

HOME_TZ = "Asia/Seoul"
STEP = timedelta(minutes=15)
STEPS_PER_DAY = 96
MAX_DAYS = 7
MAX_PACE_MS = 500
START_DATE = "2026-06-01"  # 월요일. 대본의 첫날(집 지역 날짜)

MOTION = "binary_sensor.living_motion"
LIVING_LIGHT = "light.living_room"
TEMPERATURE = "sensor.living_temperature"
LEAK = "binary_sensor.bath_leak"
GAS = "binary_sensor.kitchen_gas"

# 능동 규칙 id → 기획 장면 이름. 그 밖의 규칙은 장면 이름 없이 규칙 id 로 남는다.
SCENES: dict[str, str] = {
    "morning-ventilate": "ventilate",
    "bad-air-purifier": "purifier",
    "bedtime-dim": "bedtime",
    "cloudy-desk-light": "desk_light",
}

# 거주자 성향: 장면 → 반응. accepted = [도움됐어요], dismissed = [그만해],
# ignore = 누르지 않음(만료), coin = 반반(씨앗 난수로 accepted 또는 ignore).
# 폰에 즉시 뜬 제안(IMMEDIATE)에만 반응한다. 모아보기(DIGEST)는 누르지 않는다.
PERSONAS: dict[str, dict[str, str]] = {
    "default": {
        "ventilate": "accepted",
        "purifier": "dismissed",
        "desk_light": "coin",
        "bedtime": "accepted",
    },
}

# 22시: 거주자가 모아보기를 열어 본다(하루 한 번). 아직 만료되지 않은 것만 성향대로 누른다.
TRAY_REVIEW_AT = "22:00"
TRAY_PERSONAS: dict[str, dict[str, str]] = {
    "default": {"bedtime": "accepted", "desk_light": "coin"},
}

# 화면이 계산하지 않도록 서버가 붙이는 한국어 문구.
_DECISION_KO = {"IMMEDIATE": "바로 폰 알림", "DIGEST": "모아보기", "SUPPRESS": "보내지 않음"}
_STATE_KO = {
    "pending": "응답 기다림",
    "accepted": "수락함",
    "dismissed": "그만 받기로 함",
    "expired": "만료됨",
}
_SIGNAL_KO = {
    "accepted": "도움됐어요",
    "dismissed": "그만해",
    "ok": "괜찮아요",
    "expired": "무시(만료)",
}
_SOURCE_KO = {"persona": "거주자", "viewer": "보는 사람", "timeout": "시간 지남", "tray": "모아보기"}
_SEVERITY_SOURCE_KO = {"table": "표", "resident": "거주자 설정", "model": "모델"}
_SILENCE_KO = {
    "rejected": "지난번 [그만해]로 30일 쉬는 중",
    "cooldown": "방금 제안해서 잠시 쉬는 중",
    "evaluator_noop": "점수가 '아무것도 안 하기'보다 낮음",
    "budget": "오늘 또는 이 시간대 제안 수를 다 씀",
    "no_match": "조건이 맞지 않음",
    "tier_ceiling": "확인이 많이 필요한 행동이라 먼저 제안하지 않음",
    "safety_target": "안전 기기는 먼저 제안하지 않음",
    "not_allowed": "먼저 제안할 수 없는 행동",
    "capability_mismatch": "기기 종류가 맞지 않음",
}
_SAFETY_KIND_KO = {
    "realert": "다시 알렸어요",
    "guidance_119": "119 전화 안내를 보냈어요",
    "resolved": "해소 알림을 보냈어요",
    "forward": "보호자에게 전달했어요",
    "notice": "안전 알림을 보냈어요",
}
PM25_VERY_BAD_MIN = 76.0  # 한국 예보 '매우 나쁨' 경계

# 실행 모드: fake = 가짜 판정기·규칙 문장·성향 표(결정적, 기본) · cloud = 실제 모델·기억.
MODELS = ("fake", "cloud")
# cloud 모드 2단계 판정 마감(초). Jev 한 번(제한 8초) + 여유. 감싸는 판정기에 timeout_s 가 없어
# 명시하지 않으면 기본 6초가 된다(service.default_phase2_deadline).
CLOUD_PHASE2_DEADLINE_S = 12.0
# 가상 거주자의 취향(cloud 모드에서 resident-default 에 한 번 심는다). 보호 대상 특성은 없다.
RESIDENT_PREFERENCES = (
    "아침 창문 환기를 좋아한다",
    "공기청정기 소음을 싫어한다",
    "자기 전 거실 조명을 낮추는 것을 좋아한다",
    "흐린 낮 책상 조명 제안은 그때그때 다르다",
)
# 가상 거주자가 고르는 보기(한국어 보기 → 반응). 무시 = 누르지 않음.
_RESIDENT_OPTIONS = {"도움됐어요": "accepted", "그만해": "dismissed", "무시": "ignore"}
_DECIDED_KO = {"model": "모델이 고름", "persona_table": "성향 표"}
# 모델 대신 규칙 문장·성향 표를 쓴 까닭(화면용 한국어).
_FALLBACK_KO = {
    "invalid_output": "모델 답이 형식에 맞지 않음",
    "transport": "모델 호출 실패",
    "bad_response": "모델 응답을 읽지 못함",
    "egress": "송신 검사에서 막힘",
    "trait_in_input": "특성 낱말이 있어 보내지 않음",
    "cap": "호출·비용 상한에 닿음",
    "timeout": "시간 초과",
    "refused": "보내기 전에 막힘",
    "no_kit": "키가 없음",
    "no_template": "규칙 문장이 없음",
    "error": "외부 호출 오류",
}
_PART_KO = {"judge": "판정", "reason": "이유", "resident": "거주자", "luna": "luna 비교",
            "memory": "기억"}
_MEMORY_VERB_KO = {"recall": "찾음", "remember": "기억함", "seed": "취향 심음"}
_MEMORY_WHO_KO = {"house": "집", "resident": "거주자"}
_QUESTION_KO = {"tier": "확인 강도", "severity": "파급도"}


def pm25_band_ko(pm25: float) -> str:
    """미세먼지(PM2.5) 한국 예보 등급 이름."""
    if pm25 <= PM25_GOOD_MAX:
        return "좋음"
    if pm25 < PM25_BAD_MIN:
        return "보통"
    if pm25 < PM25_VERY_BAD_MIN:
        return "나쁨"
    return "매우 나쁨"


def weather_line_ko(weather: dict[str, Any]) -> str:
    """'미세먼지 12㎍/㎥ 좋음 · 구름 20% · 18°C' 형식의 날씨 한 줄."""
    parts: list[str] = []
    pm25 = weather.get("pm25")
    if pm25 is not None:
        parts.append(f"미세먼지 {float(pm25):.0f}㎍/㎥ {pm25_band_ko(float(pm25))}")
    cloud = weather.get("cloud_cover")
    if cloud is not None:
        parts.append(f"구름 {float(cloud) * 100:.0f}%")
    temp = weather.get("outdoor_temp_c")
    if temp is not None:
        parts.append(f"{float(temp):.0f}°C")
    return " · ".join(parts)


# 실제 송신 본문·제목을 만드는 데만 쓴다(보내지 않는다). 주소는 쓰이지 않는 루프백.
_NTFY_VIEW = HTTPNtfyTransport("http://127.0.0.1:9")


def ntfy_view(payload: dict[str, Any]) -> dict[str, str]:
    """정제된 페이로드 → 폰에 실제로 뜨는 제목·본문·우선순위(실제 transport 코드 그대로)."""
    headers = _NTFY_VIEW._headers(payload)
    return {
        "title": headers.get("Title", ""),
        "body": _NTFY_VIEW._body(payload).decode("utf-8"),
        "priority": headers.get("Priority", ""),
    }


_ORIGIN_RULE = re.compile(r"proactive:rule:([^@]+)@")


def _rule_of(params: dict[str, Any]) -> str | None:
    """후보 params 의 출처(__origin__)에서 능동 규칙 id. 규칙 출처가 아니면 None."""
    m = _ORIGIN_RULE.search(str(params.get("__origin__", "")))
    return m.group(1) if m else None
_RUN_KEYS = frozenset({"days", "seed", "persona", "pace_ms", "models", "trace"})
_SIGNALS = ("accepted", "dismissed")


# ── 요청 검사 ────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class DayRequest:
    days: int
    seed: int
    persona: str
    pace_ms: int
    models: str = "fake"
    trace: bool = False


def _int(body: dict[str, Any], key: str, default: int, lo: int, hi: int) -> int:
    value = body.get(key, default)
    if isinstance(value, bool) or not isinstance(value, int):
        raise LiveRequestError(f"{key} 는 정수여야 합니다.", field=key)
    if not lo <= value <= hi:
        raise LiveRequestError(f"{key} 는 {lo}~{hi} 사이여야 합니다.", field=key)
    return value


def parse_day_request(body: dict[str, Any]) -> DayRequest:
    """``{days:1-7, seed:int, persona:"default", pace_ms?:0-500, models?:fake|cloud, trace?:bool}``.

    모르는 항목·값은 400.
    """
    unknown = sorted(set(body) - _RUN_KEYS)
    if unknown:
        raise LiveRequestError(f"모르는 항목입니다: {', '.join(unknown)}", field=unknown[0])
    persona = body.get("persona", "default")
    if persona not in PERSONAS:
        raise LiveRequestError(
            f"persona 는 {', '.join(sorted(PERSONAS))} 중 하나여야 합니다.", field="persona"
        )
    seed = body.get("seed", 7)
    if isinstance(seed, bool) or not isinstance(seed, int):
        raise LiveRequestError("seed 는 정수여야 합니다.", field="seed")
    models = body.get("models", "fake")
    if models not in MODELS:
        raise LiveRequestError("models 는 fake·cloud 중 하나여야 합니다.", field="models")
    trace = body.get("trace", False)
    if not isinstance(trace, bool):
        raise LiveRequestError("trace 는 true·false 여야 합니다.", field="trace")
    return DayRequest(
        days=_int(body, "days", 3, 1, MAX_DAYS),
        seed=seed,
        persona=str(persona),
        pace_ms=_int(body, "pace_ms", 0, 0, MAX_PACE_MS),
        models=str(models),
        trace=trace,
    )


# ── 하루 대본 ────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class StepPlan:
    """한 단계(15분)의 대본: 센서 값과 날씨. 안전 사건은 값으로 들어 있다."""

    day: int  # 1부터
    at_local: datetime
    readings: dict[str, Any]
    weather: dict[str, Any]


@dataclass(frozen=True)
class DayPlan:
    day: int
    kind: str  # home_bad_air | out_clear | home_cloudy | home_clear
    safety: str  # leak | gas
    safety_at: str  # 지역 시각 HH:MM
    steps: tuple[StepPlan, ...]


@dataclass(frozen=True)
class _DayKind:
    name: str
    home: bool  # 낮에도 집에 있다(아니면 08:30~18:30 무렵 외출)
    bad_air: bool  # 낮(11~17시) 미세먼지 나쁨
    cloudy: bool  # 낮(09~18시) 구름 많음


_KINDS = {
    k.name: k
    for k in (
        _DayKind("home_bad_air", True, True, False),
        _DayKind("out_clear", False, False, False),
        _DayKind("home_cloudy_bad_air", True, True, True),
        _DayKind("home_clear", True, False, False),
        _DayKind("home_cloudy", True, False, True),
    )
}
# 첫 사흘은 4장면이 다 나오도록 고정한다: 아침 환기는 날마다(아침 공기는 늘 좋음), 미세먼지는
# 1·3일(3일째에도 나쁜 공기가 오므로 공기청정기 도달 감소는 [그만해] 학습 때문이다), 흐린 낮은
# 3일, 취침 준비는 날마다. 넷째 날부터는 씨앗으로 고른다.
_FIRST_DAYS = ("home_bad_air", "out_clear", "home_cloudy_bad_air")
_LATER_DAYS = ("home_clear", "out_clear", "home_bad_air", "home_cloudy")


def _minutes(hh: int, mm: int) -> int:
    return hh * 60 + mm


def _day_plan(rng: random.Random, day: int, kind_name: str, date: datetime) -> DayPlan:
    kind = _KINDS[kind_name]
    wake = _minutes(6, rng.choice((30, 45)))
    bed = _minutes(22, rng.choice((30, 45)))
    leave = _minutes(8, rng.choice((30, 45)))
    back = _minutes(18, rng.choice((15, 30, 45)))
    good_air = round(rng.uniform(6.0, 12.0), 1)
    bad_air = round(rng.uniform(42.0, 60.0), 1)
    evening_air = round(rng.uniform(18.0, 26.0), 1)
    cloudy = round(rng.uniform(0.85, 0.95), 2)
    clear = round(rng.uniform(0.1, 0.3), 2)
    safety = rng.choice(("leak", "gas"))
    if kind.home:
        event_min = _minutes(rng.randint(12, 19), rng.choice((0, 15, 30, 45)))
    else:
        event_min = _minutes(19, rng.choice((15, 30, 45)))
    event_len = rng.choice((30, 45, 60))
    sensor = LEAK if safety == "leak" else GAS

    steps: list[StepPlan] = []
    for i in range(STEPS_PER_DAY):
        m = i * 15
        awake = wake <= m < bed
        home = awake and (kind.home or not leave <= m < back)
        # 거실 조명: 아침 9시 전과 저녁(돌아온 뒤·18시 뒤)에 켜고, 자기 15분 전에 끈다.
        evening_from = _minutes(18, 0) if kind.home else back
        light_on = home and (m < _minutes(9, 0) or m >= evening_from) and m < bed - 15
        daytime_bad = kind.bad_air and _minutes(11, 0) <= m < _minutes(17, 0)
        pm25 = bad_air if daytime_bad else good_air
        if m >= _minutes(18, 0):
            pm25 = evening_air
        cloud = cloudy if kind.cloudy and _minutes(9, 0) <= m < _minutes(18, 0) else clear
        temp = round(22.0 + 3.0 * (1 if _minutes(12, 0) <= m < _minutes(17, 0) else 0), 1)
        readings: dict[str, Any] = {
            MOTION: home,
            LIVING_LIGHT: light_on,
            TEMPERATURE: temp,
            sensor: event_min <= m < event_min + event_len,
        }
        steps.append(
            StepPlan(
                day=day,
                at_local=date + timedelta(minutes=m),
                readings=readings,
                weather={"pm25": pm25, "cloud_cover": cloud, "outdoor_temp_c": 18.0},
            )
        )
    return DayPlan(
        day=day,
        kind=kind.name,
        safety=safety,
        safety_at=f"{event_min // 60:02d}:{event_min % 60:02d}",
        steps=tuple(steps),
    )


def build_script(seed: int, days: int) -> list[DayPlan]:
    """씨앗과 날 수로 대본을 만든다(결정론). 날짜는 집 시간대의 START_DATE 부터."""
    rng = random.Random(f"{seed}:script")
    tz = ZoneInfo(HOME_TZ)
    first = datetime.fromisoformat(START_DATE).replace(tzinfo=tz)
    plans: list[DayPlan] = []
    for d in range(days):
        kind = _FIRST_DAYS[d] if d < len(_FIRST_DAYS) else rng.choice(_LATER_DAYS)
        plans.append(_day_plan(rng, d + 1, kind, first + timedelta(days=d)))
    return plans


# ── 관찰 도구(가짜 송신기·기록하는 판정기·기록하는 생성기) ──────────────────


class _SimTransport:
    """송신을 받아 적는다(외부 송신 없음). 같은 순서 기록(log)에 남긴다."""

    topic = "day-sim"  # 거주자 토픽. 다른 토픽으로 간 송신은 보호자 전달이다.

    def __init__(self, log: list[tuple[str, Any]]) -> None:
        self.log = log
        self.phase = "idle"
        self.sent: list[tuple[str, str, dict[str, Any]]] = []
        # trace 를 켠 실행에서만: 제안 id → 그 첫 전송을 받은 시각(perf_counter, 시뮬레이터 스레드).
        self.sent_at: dict[str, float] | None = None

    def send(self, topic: str, sanitized: dict[str, Any]) -> None:
        self.sent.append((self.phase, topic, dict(sanitized)))
        self.log.append(("send", sanitized.get("proposal_id") or sanitized.get("label")))
        if self.sent_at is not None:
            with contextlib.suppress(Exception):
                self.sent_at.setdefault(str(sanitized.get("proposal_id")), time.perf_counter())


class _TraceRecorder:
    """시연용 단계 기록기(``trace`` 를 켠 실행에만 만든다). 결과를 바꾸지 않고, 예외를 내지 않는다.

    - 기록은 ``(entity_id, phase)`` 열쇠로 묶는다(phase: comfort·safety1·safety2). 사건을 낼 때 같은
      열쇠의 것만 꺼내고, 단계가 끝나면 남은 것은 버린다 — 늦게 온 기록이 다음 사건에 섞이지 않는다.
    - 판정 호출 시간은 호출마다 ``(단계 번호, entity_id, 2단계 여부, 초)`` 한 줄로 별도 잠금 아래 목록에
      붙인다. 2단계는 작업 스레드에서 쓴다. 꺼내기는 시뮬레이터 스레드에서, 지금 단계 번호의 것만.
    - 점수(평가기)는 그 틱의 것만 둔다(틱 시작에 지운다).
    """

    def __init__(self) -> None:
        self.step_no = 0
        self._lock = threading.Lock()
        self._judge_calls: list[tuple[int, str, bool, float]] = []
        self._keyed: dict[tuple[str, str], dict[str, Any]] = {}
        self.evaluation: EvaluationResult | None = None
        self.evaluation_s: float | None = None
        self.rules_s: float | None = None
        self.count = 0

    def begin_step(self) -> None:
        self.step_no += 1

    def begin_tick(self) -> None:
        self.evaluation = None
        self.evaluation_s = None
        self.rules_s = None

    def note(self, key: tuple[str, str], **fields: Any) -> None:
        with contextlib.suppress(Exception):
            self._keyed.setdefault(key, {}).update(fields)

    def take(self, key: tuple[str, str]) -> dict[str, Any]:
        return self._keyed.pop(key, {})

    def judge_call(self, step_no: int, entity_id: str, phase2: bool, seconds: float) -> None:
        with contextlib.suppress(Exception), self._lock:
            self._judge_calls.append((step_no, entity_id, phase2, seconds))

    def take_judge(self, entity_id: str, phase2: bool) -> float | None:
        """지금 단계에서 그 entity·단계의 판정 호출 시간(초). 없으면 None. 꺼낸 것은 지운다."""
        with self._lock:
            hits = [c for c in self._judge_calls
                    if c[0] == self.step_no and c[1] == entity_id and c[2] is phase2]
            for c in hits:
                self._judge_calls.remove(c)
        return hits[-1][3] if hits else None

    def end_step(self) -> None:
        self._keyed.clear()
        with self._lock:
            self._judge_calls.clear()

    def next_id(self) -> str:
        self.count += 1
        return f"t{self.count}"


def _phase_of(category: str | None) -> str:
    return "safety1" if is_safety_category(category or "") else "comfort"


class _TimedGuardrail(Guardrail):
    """출하 Gate1 그대로 + 그 결과와 걸린 시간을 기록기에 남긴다(결과는 손대지 않는다)."""

    def __init__(self, recorder: _TraceRecorder) -> None:
        super().__init__()
        self.recorder = recorder

    def evaluate(self, candidate: CandidateAction, ctx: ContextSnapshot | None = None) -> Gate1Result:
        t0 = time.perf_counter()
        result = super().evaluate(candidate, ctx)
        with contextlib.suppress(Exception):
            self.recorder.note((candidate.entity_id, _phase_of(candidate.category)),
                               gate1=result, gate1_s=time.perf_counter() - t0)
        return result


class _TimedEvaluator(CandidateEvaluator):
    """출하 평가기 그대로 + 이번 틱의 평가 결과와 걸린 시간을 기록기에 남긴다."""

    def __init__(self, recorder: _TraceRecorder, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self.recorder = recorder

    def evaluate(self, candidate_set: Any, **kwargs: Any) -> EvaluationResult:
        t0 = time.perf_counter()
        result = super().evaluate(candidate_set, **kwargs)
        with contextlib.suppress(Exception):
            self.recorder.evaluation = result
            self.recorder.evaluation_s = time.perf_counter() - t0
        return result


class _RecordingJudge:
    """판정기를 감싸 모델 호출 순서를 남긴다(안전 2단계는 severity_floor 가 있다).

    fake 모드: FakeLLM 그대로. cloud 모드: 호출마다 계량기가 허락하면 실제 판정기(Jev)를,
    상한에 닿았으면 FakeLLM 을 고른다 — ``model_id`` 도 그때 고른 쪽 이름이다(LLMJudge 가 호출
    직전에 읽어 degraded 기록에도 남는다). 실제 판정기 호출은 잠금으로 한 번에 하나씩 돈다
    (2단계 스레드와 능동 판정이 겹쳐도 사용량 기록이 섞이지 않게). 성공한 판정은 같은 증거와
    확률을 luna 그림자 대기열에 넣는다(그림자는 판정 밖, 단계 끝에 돈다).
    """

    def __init__(
        self,
        inner: LLMBackend,
        log: list[tuple[str, Any]],
        *,
        cloud: LLMBackend | None = None,
        kit: CloudKit | None = None,
        shadow: list[tuple[str, dict[str, dict[str, float]], str, str | None, str]] | None = None,
        recorder: _TraceRecorder | None = None,
    ) -> None:
        self.recorder = recorder
        self.inner = inner
        self.log = log
        self.cloud = cloud
        self.kit = kit
        self.shadow = shadow
        self._lock = threading.Lock()
        self._evidence = EvidenceBuilder() if cloud is not None else None

    def _use_cloud(self) -> bool:
        return self.cloud is not None and self.kit is not None and self.kit.meter.tripped is None

    @property
    def model_id(self) -> str:
        return self.cloud.model_id if self._use_cloud() and self.cloud else self.inner.model_id

    @model_id.setter
    def model_id(self, value: str) -> None:  # LLMBackend 프로토콜(설정 가능한 속성)용
        self.inner.model_id = value

    def judge_raw(
        self,
        candidate: CandidateAction,
        ctx: ContextSnapshot | None,
        gate1_floor: RiskTier,
        severity_floor: SeverityLevel | None = None,
    ) -> LLMResult:
        self.log.append(("model", (candidate.entity_id, severity_floor is not None)))
        if self.cloud is not None and self.kit is not None and self.kit.meter.allow():
            return self._cloud_judge(self.cloud, self.kit, candidate, ctx, gate1_floor,
                                     severity_floor)
        rec = self.recorder
        if rec is None:
            if severity_floor is None:
                return self.inner.judge_raw(candidate, ctx, gate1_floor)
            return self.inner.judge_raw(candidate, ctx, gate1_floor, severity_floor=severity_floor)
        # 가짜 경로: 잠금 없이(예전과 같은 순서·대기) 그 호출만 직접 잰다.
        step_no = 0
        with contextlib.suppress(Exception):
            step_no = rec.step_no
        t0 = time.perf_counter()
        try:
            if severity_floor is None:
                return self.inner.judge_raw(candidate, ctx, gate1_floor)
            return self.inner.judge_raw(candidate, ctx, gate1_floor, severity_floor=severity_floor)
        finally:
            with contextlib.suppress(Exception):  # 기록은 판정을 절대 깨지 않는다
                rec.judge_call(step_no, candidate.entity_id, severity_floor is not None,
                               time.perf_counter() - t0)

    def _cloud_judge(
        self,
        cloud: LLMBackend,
        kit: CloudKit,
        candidate: CandidateAction,
        ctx: ContextSnapshot | None,
        gate1_floor: RiskTier,
        severity_floor: SeverityLevel | None,
    ) -> LLMResult:
        self.log.append(("ext", "judge"))
        step_no = 0
        if self.recorder is not None:
            with contextlib.suppress(Exception):
                step_no = self.recorder.step_no
        with self._lock:
            t0 = time.perf_counter()
            try:
                if severity_floor is None:
                    result = cloud.judge_raw(candidate, ctx, gate1_floor)
                else:
                    result = cloud.judge_raw(candidate, ctx, gate1_floor,
                                             severity_floor=severity_floor)
            except LLMUnavailable:
                raise
            except Exception as exc:  # noqa: BLE001 — 예상 밖 오류도 degraded(하한 유지)로 보낸다
                self.log.append(("ext_error", ("judge", type(exc).__name__)))
                raise LLMUnavailable(f"cloud judge failed: {type(exc).__name__}") from None
            finally:
                if self.recorder is not None:  # 잠금 안에서 잰 시간(잠금 대기는 빼고)
                    with contextlib.suppress(Exception):
                        self.recorder.judge_call(step_no, candidate.entity_id,
                                                 severity_floor is not None,
                                                 time.perf_counter() - t0)
                usage = getattr(cloud, "last_usage", None) or {}
                kit.meter.record(
                    "judge", getattr(cloud, "model", None),
                    seconds=round(time.perf_counter() - t0, 4),
                    input_tokens=usage.get("input_tokens"),
                    output_tokens=usage.get("output_tokens"),
                )
        served = usage.get("model") if isinstance(usage.get("model"), str) else None
        if self.shadow is not None and self._evidence is not None and result.probs:
            try:
                evidence = self._evidence.build(candidate, ctx, gate1_floor)
            except LLMUnavailable:
                return result
            self.shadow.append((evidence, dict(result.probs), result.model_id, served,
                                candidate.entity_id))
        return result


class _RecordingGenerator(ProactiveGenerator):
    """출하 규칙 생성기 그대로 + 마지막 결정을 남긴다(침묵 사유 관찰용)."""

    last_decision: ProactiveDecision | None = None
    recorder: _TraceRecorder | None = None

    def propose(
        self,
        cur: Any,
        *,
        external: ExternalContext | None = None,
        rejected_keys: frozenset[str] = frozenset(),
    ) -> ProactiveDecision:
        t0 = time.perf_counter()
        d = super().propose(cur, external=external, rejected_keys=rejected_keys)
        self.last_decision = d
        if self.recorder is not None:
            with contextlib.suppress(Exception):
                self.recorder.rules_s = time.perf_counter() - t0
        return d


# ── 기록 ─────────────────────────────────────────────────────────────────────


@dataclass
class _Tracked:
    sim_id: str
    pid: str
    day: int
    kind: str  # comfort | safety
    scene: str | None
    rule_id: str | None
    decision: str
    risk_tier: RiskTier
    state: str = "pending"


def _new_metrics(day: int) -> dict[str, Any]:
    return {
        "day": day,
        "proposals": 0,
        "pushes": 0,
        "digests": 0,
        "suppressed": 0,
        "accepted": 0,  # 폰 알림(IMMEDIATE)에서 [도움됐어요]
        "tray_accepted": 0,  # 모아보기에서 [도움됐어요]
        "dismissed": 0,
        "expired": 0,
        "accept_rate": None,  # = accepted / pushes (폰 알림이 없으면 None)
        "accept_rate_ko": "—",
        "safety_alerts": 0,
        "scenes": {},
    }


def _refresh_rate(m: dict[str, Any]) -> None:
    pushes = m["pushes"]
    m["accept_rate"] = None if pushes == 0 else round(m["accepted"] / pushes, 3)
    m["accept_rate_ko"] = "—" if m["accept_rate"] is None else f"{m['accept_rate'] * 100:.0f}%"


Emit = Callable[[str, dict[str, Any]], Any]
_T = TypeVar("_T")


@dataclass
class DaySim:
    """시뮬레이션 실행 하나. ``run`` 은 시뮬레이터 스레드의 이벤트 루프에서 돈다."""

    seed: int = 7
    days: int = 3
    persona: str = "default"
    pace_ms: int = 0
    log: list[tuple[str, Any]] = field(default_factory=list)
    tmp_dir: Path | None = None
    metrics: list[dict[str, Any]] = field(default_factory=list)
    proposals: list[dict[str, Any]] = field(default_factory=list)
    safety_events: list[dict[str, Any]] = field(default_factory=list)
    # cloud 모드 전용. fake 모드에서는 쓰지 않는다(사건·요약이 예전과 바이트까지 같다).
    models: str = "fake"
    cloud: CloudKit | None = None
    house_id: str = "local"  # 집 기억 범위 house-<house_id>. 관리자가 실행 id 를 넣는다.
    memory_events: list[dict[str, Any]] = field(default_factory=list)
    luna_results: list[dict[str, Any]] = field(default_factory=list)
    # 시연용 단계 기록(trace 사건). 끄면(기본) 사건·요약이 예전과 같다.
    trace: bool = False

    def __post_init__(self) -> None:
        if self.persona not in PERSONAS:
            raise ValueError(f"unknown persona {self.persona!r}")
        if self.models not in MODELS:
            raise ValueError(f"unknown models {self.models!r}")
        if self.models == "cloud" and self.cloud is None:
            raise ValueError("cloud 모드에는 CloudKit 이 필요합니다.")
        self._shadow_due: list[tuple[str, dict[str, dict[str, float]], str, str | None, str]] = []
        self._rec: _TraceRecorder | None = _TraceRecorder() if self.trace else None
        self._trace_emit: Callable[[dict[str, Any]], None] | None = None
        self._pending_comfort: dict[str, Any] | None = None
        self._last_reason_s: float | None = None
        self._db: Any = None
        self._pending_safety: list[dict[str, Any]] = []
        # trace 실행에만: 이번 틱 직전·직후의 선호 셀(만료 신호의 전후 값). (제안→셀, 전, 후)
        self._pref_tick: tuple[dict[str, str], dict[str, float], dict[str, float] | None] | None = None
        self._svc: LiotService | None = None
        self._house: HouseMemory | None = None
        self._resident: ResidentMemory | None = None
        self._decided_by: dict[str, str] = {}
        self._reaction_fallback: dict[str, str] = {}
        self._memory_unsent: list[dict[str, Any]] = []
        self._usage_mark: dict[str, float] = {}
        self.day_usage: list[dict[str, Any]] = []
        self._ext_miss: str | None = None
        self._trip_announced = False
        self._viewer: queue.SimpleQueue[tuple[str, str]] = queue.SimpleQueue()
        self._rng = random.Random(f"{self.seed}:persona")
        self._tracked: dict[str, _Tracked] = {}
        self._by_pid: dict[str, str] = {}
        self._persona_due: list[tuple[str, str]] = []
        self._records: dict[str, dict[str, Any]] = {}
        self._open_safety: set[str] = set()  # 이상 값으로 상황이 열린 센서
        self._open_alert: dict[str, str] = {}  # 열린 안전 상황의 센서 → 첫 알림 sim id
        self._rules: dict[str, ProactiveRule] = {}
        self._reg: RuntimeRegistry | None = None
        self._clock_ko = ""
        self._day = 0
        self._last_silence: tuple[tuple[str, ...], str] | None = None

    # ── 보는 사람 응답(HTTP 스레드에서 부른다 — 큐에만 넣는다) ──────────────

    def enqueue_feedback(self, sim_id: str, signal: str) -> None:
        if signal not in _SIGNALS:
            raise LiveRequestError("signal 은 accepted·dismissed 중 하나여야 합니다.", field="signal")
        if not re.fullmatch(r"p\d{1,6}", sim_id or ""):
            raise LiveRequestError("proposal_id 형식이 올바르지 않습니다.", field="proposal_id")
        self._viewer.put((sim_id, signal))

    # ── 실행 ───────────────────────────────────────────────────────────────

    async def run(
        self,
        emit: Emit,
        should_stop: Callable[[], bool] = lambda: False,
        on_summary: Callable[[dict[str, Any]], None] | None = None,
    ) -> dict[str, Any]:
        """대본 전체를 돈다. 임시 DB 는 끝나면(예외여도) 지운다. 요약을 돌려준다."""
        self.tmp_dir = Path(tempfile.mkdtemp(prefix="liot-day-"))
        try:
            return await self._run(emit, should_stop, on_summary)
        finally:
            shutil.rmtree(self.tmp_dir, ignore_errors=True)

    def summary(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "seed": self.seed,
            "days": self.days,
            "persona": self.persona,
            "metrics": [dict(m) for m in self.metrics],
            "proposals": [dict(p) for p in self.proposals],
            "safety": [dict(s) for s in self.safety_events],
        }
        if self._kit is not None:
            out["models"] = self._models_view(self._kit)
            out["memory"] = [dict(m) for m in self.memory_events]
            # entity_id 는 trace 의 followup 이 luna 비교를 찾는 열쇠다 — trace 를 켠 실행에만 싣는다.
            out["luna"] = [{k: v for k, v in r.items() if self.trace or k != "entity_id"}
                           for r in self.luna_results]
        return out

    @property
    def _kit(self) -> CloudKit | None:
        return self.cloud if self.models == "cloud" else None

    def _models_view(self, kit: CloudKit) -> dict[str, Any]:
        compared = [r for r in self.luna_results if r["status"] == "ok"]
        agree = [r for r in compared if r["agree"].get("tier") is not None]
        return {
            "mode": "cloud",
            "status": [dict(st) for st in kit.status],
            "usage": kit.meter.summary(),
            "fallback_ko": kit.meter.trip_reason_ko(),
            "days": [dict(d) for d in self.day_usage],
            "luna_compared": len(compared),
            "luna_tier_agree_rate": (None if not agree else
                                     round(sum(bool(r["agree"]["tier"]) for r in agree) / len(agree), 3)),
        }

    async def _run(
        self,
        emit: Emit,
        should_stop: Callable[[], bool],
        on_summary: Callable[[dict[str, Any]], None] | None,
    ) -> dict[str, Any]:
        assert self.tmp_dir is not None
        plans = build_script(self.seed, self.days)
        first_at = plans[0].steps[0].at_local
        clock = Clock(fixed=first_at, tz=HOME_TZ)
        reg = RuntimeRegistry()
        catalog, categories = reg.to_catalog(), reg.categories()
        weather = FakeExternalProvider(clock=clock)
        transport = _SimTransport(self.log)
        generator = _RecordingGenerator(catalog=catalog, categories=categories, clock=clock)
        self._rules = {r.rule_id: r for r in generator.ruleset.rules}
        self._reg = reg
        rec = self._rec
        # trace 를 켠 실행에만 시간을 재는 감싸개를 넣는다(결과는 출하본 그대로). 끄면 예전과 같다.
        timed: dict[str, Any] = {}
        if rec is not None:
            generator.recorder = rec
            timed = {"guardrail": _TimedGuardrail(rec),
                     "evaluator": _TimedEvaluator(rec, catalog=catalog)}
        app = LiotApp(
            clock=clock,
            catalog=catalog,
            categories=categories,
            llm_backend=_RecordingJudge(
                FakeLLM(), self.log,
                cloud=None if self._kit is None else self._kit.judge,
                kit=self._kit,
                shadow=self._shadow_due if self._kit is not None and self._kit.luna else None,
                recorder=rec,
            ),
            external_provider=weather,
            proactive=generator,
            **timed,
        )
        db = await init_db(str(self.tmp_dir / "day.db"))
        self._db = db
        secret = secrets.token_bytes(32)
        svc = LiotService(app, db, clock=clock, transport=transport, topic=transport.topic,
                          secret=secret,
                          phase2_deadline_s=None if self._kit is None else CLOUD_PHASE2_DEADLINE_S)
        self._svc = svc

        async def out(stage: str, data: dict[str, Any]) -> None:
            # 모든 사건에 지금 시각(가상)을 붙인다 — 화면의 시계는 이 값만 쓴다.
            data.setdefault("day", self._day)
            data.setdefault("clock_ko", self._clock_ko)
            emit(stage, data)
            if self.pace_ms:
                await asyncio.sleep(self.pace_ms / 1000.0)

        def out_trace(data: dict[str, Any]) -> None:
            # trace 사건은 잠자지 않는다(pace_ms 대기 없음) — 시연 딜레이는 화면이 넣는다.
            data.setdefault("day", self._day)
            data.setdefault("clock_ko", self._clock_ko)
            emit("trace", data)

        if rec is not None:
            self._trace_emit = out_trace
            transport.sent_at = {}

        last: dict[str, Any] = {}
        try:
            for plan in plans:
                if should_stop():
                    break
                metrics = _new_metrics(plan.day)
                self.metrics.append(metrics)
                self._day = plan.day
                self._clock_ko = f"{plan.day}일차 00:00"
                await out(
                    "day.start",
                    {
                        "day": plan.day,
                        "date": plan.steps[0].at_local.date().isoformat(),
                        "kind": plan.kind,
                        "safety": plan.safety,
                        "line_ko": f"{plan.day}일차가 시작돼요",
                        "metrics": dict(metrics),
                    },
                )
                if self._kit is not None:
                    self._usage_mark = self._usage_totals(self._kit.meter.summary())
                if self._kit is not None and plan is plans[0]:
                    await self._cloud_start(self._kit, out)
                stopped = False
                for step in plan.steps:
                    if should_stop():
                        stopped = True
                        break
                    clock.set_fixed(step.at_local)
                    await self._step(svc, app, generator, weather, transport, step, last, out,
                                     secret)
                self._finish_day(metrics)
                end: dict[str, Any] = {"day": plan.day, "partial": stopped, "metrics": dict(metrics),
                                       "line_ko": f"{plan.day}일차가 끝났어요"}
                if self._kit is not None:
                    end["usage"] = self._kit.meter.summary()
                    end["usage_day"] = self._close_day_usage(plan.day, end["usage"])
                await out("day.end", end)
                if on_summary is not None:
                    on_summary(self.summary())
                if stopped:
                    break
            await svc.drain_phase2()
        finally:
            await svc.drain_phase2(timeout=5.0)
            await db.close()
        return self.summary()

    # ── 단계 ───────────────────────────────────────────────────────────────

    async def _step(
        self,
        svc: LiotService,
        app: LiotApp,
        generator: _RecordingGenerator,
        weather: FakeExternalProvider,
        transport: _SimTransport,
        step: StepPlan,
        last: dict[str, Any],
        out: Callable[[str, dict[str, Any]], Awaitable[None]],
        secret: bytes,
    ) -> None:
        at = step.at_local
        hhmm = at.strftime("%H:%M")
        self._clock_ko = f"{step.day}일차 {hhmm}"
        self.log.append(("step", (step.day, hhmm)))
        if self._rec is not None:
            self._rec.begin_step()
        # cloud 모드: 응답 적용·모아보기(기억 쓰기·거주자 모델 호출)는 센서 반영과 안전 첫 알림 뒤로
        # 간다. fake 모드는 예전 순서 그대로다(사건 목록이 바뀌지 않게).
        late = self._kit is not None
        if not late:
            await self._apply_taps(svc, transport, step, out, secret)
            if hhmm == TRAY_REVIEW_AT:
                await self._tray_review(svc, step, out, secret)

        changed = {k: v for k, v in step.readings.items() if last.get(k) != v}
        if late:
            # 안전 센서를 먼저 반영한다 — 같은 단계의 다른 값이 판정(외부 호출)을 먼저 부르지 않게.
            changed = dict(sorted(changed.items(), key=lambda kv: kv[0] not in (LEAK, GAS)))
        weather_changed = last.get("__weather__") != step.weather
        weather.set_values(**step.weather)
        last.update(step.readings)
        last["__weather__"] = dict(step.weather)

        # 센서 값 → 데몬과 같은 진입점. 안전 이상이면 첫 알림이 여기서 바로 나간다.
        transport.phase = "reading"
        for eid, value in changed.items():
            raw = ("on" if value else "off") if isinstance(value, bool) else value
            t_read = time.perf_counter()
            res = await svc.handle_reading(
                SensorReading(
                    entity_id=eid,
                    source="ha",
                    raw_value=raw,
                    norm_value=normalize(raw),
                    ts_utc=svc.clock.now_utc(),
                )
            )
            if eid in (LEAK, GAS):
                await self._safety_reading(svc, transport, step, eid, bool(value), res, out,
                                           t_read=t_read)

        if late:
            await self._apply_taps(svc, transport, step, out, secret)
            if hhmm == TRAY_REVIEW_AT:
                await self._tray_review(svc, step, out, secret)

        transport.phase = "tick"
        app.last_trace = None
        generator.last_decision = None
        if self._rec is not None:
            self._rec.begin_tick()
            self._pref_tick = None
            with contextlib.suppress(Exception):  # 선호 읽기(만료 전후 값) — trace 에만, 읽기만
                prop_cell, cells = await self._pref_cells(svc)
                self._pref_tick = (prop_cell, cells, None)
        result = await svc.run_tick()
        if self._rec is not None and self._pref_tick is not None:
            with contextlib.suppress(Exception):
                self._pref_tick = (self._pref_tick[0], self._pref_tick[1],
                                   (await self._pref_cells(svc))[1])
        silence = self._silence(app, generator, result)
        # 같은 규칙이 같은 까닭으로 단계마다 건너뛰면 처음 한 번만 남긴다(타임라인이 넘치지 않게).
        key = None if silence is None else (tuple(silence["rules"]), silence["reason"])
        if key is not None and key == self._last_silence:
            silence = None
        self._last_silence = key if result is None else None
        if result is not None:
            await self._record_proposal(app, generator, step, result, out)
        if self._rec is not None and (result is not None or silence is not None):
            # 편의 trace 는 단계 끝(luna 비교 뒤)에 낸다 — 여기서는 재료만 모은다.
            judged = None
            with contextlib.suppress(Exception):
                judged = self._last_comfort_judgment(app, result)
            self._pending_comfort = {
                "result": result, "silence": silence, "decision": generator.last_decision,
                "judgment": judged, "changed": len(changed),
                "weather_ko": weather_line_ko(step.weather),
                "sim_id": self._by_pid.get(result.proposal.proposal_id) if result else None,
                "hhmm": hhmm,
            }

        transport.phase = "phase2"
        await svc.drain_phase2()
        transport.phase = "ladder"
        await svc.ladder_tick()
        await self._safety_sends(transport, step, out)
        transport.phase = "idle"
        await self._expiries(svc, step, out)
        if self._kit is not None:
            await self._luna_shadows(self._kit, step, out)
            await self._announce_trip(self._kit, out)
            await self._flush_memory(out)
        if self._rec is not None:
            self._emit_step_traces()
            with contextlib.suppress(Exception):
                await self._emit_followups()
            self._pending_safety = []
            self._rec.end_step()

        if changed or weather_changed or silence or at.minute == 0:
            data: dict[str, Any] = {
                "day": step.day,
                "at": at.isoformat(),
                "hhmm": hhmm,
                "changed": dict(changed),
                "weather": dict(step.weather),
                "weather_ko": weather_line_ko(step.weather),
                "bucket": svc.clock.time_bucket(),
            }
            if silence:
                data["silence"] = silence
                data["line_ko"] = f"{silence['title_ko']} — 건너뜀: {silence['reason_ko']}"
            await out("step", data)

    def _silence(
        self, app: LiotApp, generator: _RecordingGenerator, result: DispatchResult | None
    ) -> dict[str, Any] | None:
        """규칙이 성립했는데 제안이 나가지 않은 까닭(거절·쿨다운·무행동 선택·상한)."""
        d = generator.last_decision
        if result is not None or d is None or not d.matched_rule_ids:
            return None
        if d.candidate is None:
            reason = str(d.suppressed_by)
        else:
            trace = app.last_trace or {}
            reason = str(trace.get("silence_reason") or "evaluator_noop")
        first = self._rules.get(d.matched_rule_ids[0])
        return {
            "rules": list(d.matched_rule_ids),
            "reason": reason,
            "reason_ko": _SILENCE_KO.get(reason, reason),
            "title_ko": (first.title_ko if first and first.title_ko else d.matched_rule_ids[0]),
        }

    def _title(self, rule_id: str | None, entity_id: str) -> str:
        """사람이 읽는 행동 제목: 규칙의 title_ko, 없으면 기기 이름 + '제안'."""
        rule = self._rules.get(rule_id or "")
        if rule is not None and rule.title_ko:
            return rule.title_ko
        name = self._reg.device_name(entity_id) if self._reg is not None else entity_id
        return f"{name} 제안"

    def _skipped(
        self, app: LiotApp, generator: _RecordingGenerator, matched: list[str], winner: str | None
    ) -> list[dict[str, str]]:
        """승자 앞에서 성립했지만 건너뛴 규칙과 까닭(발의와 같은 순서로 다시 본다 — 관찰용)."""
        out: list[dict[str, str]] = []
        store = app.feedback_store
        rejected = store.rejected_keys if isinstance(store, InMemoryFeedbackStore) else frozenset()
        for rid in matched:
            if rid == winner:
                break
            rule = self._rules.get(rid)
            if rule is None:
                continue
            cand = generator._build_candidate(rule)
            reason = validate_proactive_candidate(cand, generator.catalog)
            if reason is None:
                reason = "rejected" if candidate_key(cand) in rejected else "cooldown"
            out.append({"rule_id": rid, "title_ko": self._title(rid, rule.target_entity),
                        "reason": reason, "reason_ko": _SILENCE_KO.get(reason, reason)})
        return out

    async def _record_proposal(
        self,
        app: LiotApp,
        generator: _RecordingGenerator,
        step: StepPlan,
        result: DispatchResult,
        out: Callable[[str, dict[str, Any]], Awaitable[None]],
    ) -> None:
        p = result.proposal
        rule_id = _rule_of(p.action.params)
        scene = SCENES.get(rule_id or "")
        trace = app.last_trace or {}
        decision = trace.get("proactive")
        evaluation = trace.get("evaluation")
        score: float | None = None
        noop_score: float | None = None
        candidates: list[dict[str, Any]] = []
        if evaluation is not None:
            score = evaluation.scored[evaluation.selected_index].final_score
            for i, ac in enumerate(evaluation.scored):
                c = ac.candidate
                if is_noop(c):
                    noop_score = ac.final_score
                label = "아무것도 안 하기" if is_noop(c) else self._title(
                    _rule_of(c.params), c.entity_id
                )
                candidates.append(
                    {"label_ko": label, "score": ac.final_score,
                     "selected": i == evaluation.selected_index}
                )
        matched = list(getattr(decision, "matched_rule_ids", ()) or ())
        sim_id = f"p{len(self._tracked) + 1}"
        tracked = _Tracked(sim_id, p.proposal_id, step.day, "comfort", scene, rule_id,
                           result.decision, p.risk_tier)
        self._tracked[sim_id] = tracked
        self._by_pid[p.proposal_id] = sim_id
        destination = {"IMMEDIATE": "phone", "DIGEST": "tray"}.get(result.decision, "blocked")
        title = self._title(rule_id, p.action.entity_id)
        template_reason = getattr(decision, "reason_ko", "") or ""
        cloud_fields: dict[str, Any] = {}
        if self._kit is not None:
            template_reason, cloud_fields = await self._cloud_reason(
                self._kit, title, scene, step, template_reason
            )
        rec = {
            "id": sim_id,
            "day": step.day,
            "at": step.at_local.isoformat(),
            "hhmm": step.at_local.strftime("%H:%M"),
            "scene": scene,
            "rule_id": rule_id,
            "title_ko": title,
            "capability": p.action.capability,
            "action": p.action.action,
            "entity_id": p.action.entity_id,
            "params": {k: v for k, v in p.action.params.items() if not k.startswith("__")},
            "tier": p.risk_tier.name.lower(),
            "tier_ko": p.risk_tier.label_ko,
            "confirmation": p.required_confirmation,
            "decision": result.decision,
            "decision_ko": _DECISION_KO.get(result.decision, result.decision),
            "destination": destination,
            "sent": result.sent,
            "score": score,
            "noop_score": noop_score,
            "candidates": candidates,
            "reason_ko": template_reason,
            # 성립한 규칙 전부(선언 순). 승자 앞의 규칙이 빠졌다면 거절·쿨다운으로 건너뛴 것이다.
            "matched_rules": matched,
            "skipped": self._skipped(app, generator, matched, rule_id),
            "expires_at": p.expires_at,
            "state": "pending",
            "state_ko": _STATE_KO["pending"],
            "line_ko": f"{title} — {_DECISION_KO.get(result.decision, result.decision)}",
            **cloud_fields,
        }
        self.proposals.append(rec)
        self._records[sim_id] = rec
        metrics = self.metrics[-1]
        metrics["proposals"] += 1
        if scene:
            metrics["scenes"][scene] = metrics["scenes"].get(scene, 0) + 1
        if result.decision == "IMMEDIATE":
            metrics["pushes"] += 1
            if self._kit is None:
                reaction = self._persona_reaction(scene)
            else:
                reaction = await self._cloud_reaction(self._kit, sim_id, scene, title,
                                                      template_reason, step)
            if reaction is not None:
                self._persona_due.append((sim_id, reaction))
        elif result.decision == "DIGEST":
            metrics["digests"] += 1
        else:
            metrics["suppressed"] += 1
        _refresh_rate(metrics)
        await out("proposal", {**rec, "metrics": dict(metrics)})
        if result.decision == "DIGEST":
            await out("digest", {"id": sim_id, "day": step.day, "scene": scene,
                                 "title_ko": title, "reason_ko": rec["reason_ko"],
                                 "hhmm": rec["hhmm"]})

    def _persona_reaction(self, scene: str | None, table: dict[str, str] | None = None) -> str | None:
        table = PERSONAS[self.persona] if table is None else table
        choice = table.get(scene or "", "ignore")
        if choice == "coin":
            choice = "accepted" if self._rng.random() < 0.5 else "ignore"
        return None if choice == "ignore" else choice

    # ── 응답 적용(시뮬레이터 스레드, 단계 경계) ─────────────────────────────

    async def _apply_taps(
        self,
        svc: LiotService,
        transport: _SimTransport,
        step: StepPlan,
        out: Callable[[str, dict[str, Any]], Awaitable[None]],
        secret: bytes,
    ) -> None:
        transport.phase = "feedback"
        viewer: list[tuple[str, str, str]] = []
        while True:
            try:
                sim_id, signal = self._viewer.get_nowait()
            except queue.Empty:
                break
            viewer.append((sim_id, signal, "viewer"))
        persona = [(sid, sig, "persona") for sid, sig in self._persona_due]
        self._persona_due = []
        for sim_id, signal, source in viewer + persona:
            await self._tap(svc, step, sim_id, signal, source, out, secret)

    async def _tray_review(
        self,
        svc: LiotService,
        step: StepPlan,
        out: Callable[[str, dict[str, Any]], Awaitable[None]],
        secret: bytes,
    ) -> None:
        """22시: 거주자가 모아보기를 연다. 아직 살아 있는 항목만 성향대로 누른다(만료는 그대로)."""
        table = TRAY_PERSONAS.get(self.persona, {})
        items: list[dict[str, Any]] = []
        for t in self._tracked.values():
            if t.kind != "comfort" or t.decision != "DIGEST" or t.day != step.day:
                continue
            before = t.state
            reaction: str | None = None
            if before == "pending" and self._kit is None:
                reaction = self._persona_reaction(t.scene, table)
            elif before == "pending" and self._kit is not None:
                rec0 = self._records[t.sim_id]
                reaction = await self._cloud_reaction(self._kit, t.sim_id, t.scene,
                                                      rec0["title_ko"], rec0["reason_ko"], step,
                                                      table=table)
            if reaction is not None:
                await self._tap(svc, step, t.sim_id, reaction, "tray", out, secret)
            items.append({
                "id": t.sim_id,
                "title_ko": self._records[t.sim_id]["title_ko"],
                "before_ko": _STATE_KO[before],
                "tapped": reaction,
                "after_ko": _STATE_KO[t.state],
            })
        tapped = sum(1 for i in items if i["tapped"])
        await out("tray.review", {
            "items": items,
            "line_ko": f"모아보기를 열어 봤어요 — {len(items)}개 중 {tapped}개를 눌렀어요",
            "metrics": dict(self.metrics[step.day - 1]),
        })

    async def _tap(
        self,
        svc: LiotService,
        step: StepPlan,
        sim_id: str,
        signal: str,
        source: str,
        out: Callable[[str, dict[str, Any]], Awaitable[None]],
        secret: bytes,
    ) -> None:
        tracked = self._tracked.get(sim_id)
        if tracked is None:
            await out("feedback", {"id": sim_id, "signal": signal, "source": source,
                                   "status": "404", "result": "없는 제안",
                                   "line_ko": "없는 제안이라 반영하지 못했어요"})
            return
        if source == "persona" and tracked.state != "pending":
            return  # 보는 사람이 먼저 눌렀다 — 거주자는 다시 누르지 않는다
        pid = tracked.pid
        nonce = svc._nonce.get(pid, "")
        sig = sign_feedback(secret, pid, tracked.risk_tier, signal, nonce)
        pref_before: tuple[dict[str, str], dict[str, float]] | None = None
        if self._rec is not None and tracked.kind == "comfort":
            with contextlib.suppress(Exception):  # 선호 읽기(누름 전후 값) — trace 에만, 읽기만
                pref_before = await self._pref_cells(svc)
        status, body = await svc._route_feedback(
            f"GET /feedback?p={pid}&t={int(tracked.risk_tier)}&n={nonce}&sig={sig}&a={signal} "
            "HTTP/1.1"
        )
        pref: tuple[float, float] | None = None
        if pref_before is not None:
            with contextlib.suppress(Exception):
                key = pref_before[0][pid]
                after = (await self._pref_cells(svc))[1]
                pref = (pref_before[1].get(key, NEUTRAL_PREF), after.get(key, NEUTRAL_PREF))
        code = status.split(" ", 1)[0]
        day_m = self.metrics[tracked.day - 1]
        if code == "200" and tracked.kind == "comfort" and tracked.state == "pending":
            new_state = "accepted" if signal == "accepted" else "dismissed"
            tracked.state = new_state
            self._records[sim_id]["state"] = new_state
            self._records[sim_id]["state_ko"] = _STATE_KO[new_state]
            if new_state == "dismissed":
                day_m["dismissed"] += 1
            elif tracked.decision == "IMMEDIATE":
                day_m["accepted"] += 1
            else:
                day_m["tray_accepted"] += 1
            _refresh_rate(day_m)
            if self._kit is not None:
                await self._remember_feedback(self._kit, tracked, new_state, step, out)
        who = _SOURCE_KO.get(source, source)
        what = self._records[sim_id]["title_ko"] if sim_id in self._records else "안전 경보"
        actor = {"tray": "거주자가 모아보기에서", "viewer": "보는 사람이"}.get(source, f"{who}가")
        decided: dict[str, Any] = {}
        if self._kit is not None and source in ("persona", "tray") and sim_id in self._decided_by:
            decided = {"decided_by": self._decided_by[sim_id],
                       "decided_by_ko": _DECIDED_KO[self._decided_by[sim_id]]}
            if sim_id in self._reaction_fallback:
                decided["reaction_fallback"] = self._reaction_fallback[sim_id]
                decided["reaction_fallback_ko"] = _FALLBACK_KO.get(
                    self._reaction_fallback[sim_id], self._reaction_fallback[sim_id])
        line = (f"{actor} [{_SIGNAL_KO.get(signal, signal)}]를 눌렀어요 — {what}" if code == "200"
                else f"{who}의 [{_SIGNAL_KO.get(signal, signal)}]가 반영되지 않았어요({code})")
        await out(
            "feedback",
            {
                "id": sim_id,
                "day": step.day,
                "at": step.at_local.isoformat(),
                "hhmm": step.at_local.strftime("%H:%M"),
                "signal": signal,
                "signal_ko": _SIGNAL_KO.get(signal, signal),
                "source": source,
                "source_ko": who,
                "status": code,
                "result": body,
                "scene": tracked.scene,
                "kind": tracked.kind,
                "state": tracked.state,
                "state_ko": _STATE_KO.get(tracked.state, tracked.state),
                "line_ko": line,
                "metrics": dict(day_m),
                **decided,
            },
        )
        if self._rec is not None:  # 누름 trace 는 누름이 적용된 자리 바로 뒤
            with contextlib.suppress(Exception):
                self._emit_feedback_trace(tracked, signal, source, code, what, pref=pref)

    async def _expiries(
        self,
        svc: LiotService,
        step: StepPlan,
        out: Callable[[str, dict[str, Any]], Awaitable[None]],
    ) -> None:
        pending = [t for t in self._tracked.values() if t.kind == "comfort" and t.state == "pending"]
        if not pending:
            return
        rows = await svc.proposals.states_for([t.pid for t in pending])
        for t in pending:
            state = rows.get(t.pid, ("", None))[0]
            if state != "expired":
                continue
            t.state = "expired"
            rec = self._records[t.sim_id]
            rec["state"] = "expired"
            rec["state_ko"] = _STATE_KO["expired"]
            day_m = self.metrics[t.day - 1]
            day_m["expired"] += 1
            if self._kit is not None:
                await self._remember_feedback(self._kit, t, "expired", step, out)
            await out("feedback", {
                "id": t.sim_id, "day": step.day, "at": step.at_local.isoformat(),
                "hhmm": step.at_local.strftime("%H:%M"), "signal": "expired",
                "signal_ko": _SIGNAL_KO["expired"], "source": "timeout",
                "source_ko": _SOURCE_KO["timeout"], "status": "200", "result": "무시(만료)",
                "scene": t.scene, "kind": t.kind, "state": "expired",
                "state_ko": _STATE_KO["expired"],
                "line_ko": f"아무도 누르지 않아 만료됐어요 — {rec['title_ko']}",
                "metrics": dict(day_m),
            })
            if self._rec is not None:
                with contextlib.suppress(Exception):
                    self._emit_feedback_trace(t, "expired", "timeout", "200", rec["title_ko"],
                                              pref=self._expiry_pref(t.pid))

    # ── 안전 ───────────────────────────────────────────────────────────────

    async def _safety_reading(
        self,
        svc: LiotService,
        transport: _SimTransport,
        step: StepPlan,
        eid: str,
        abnormal: bool,
        res: DispatchResult | None,
        out: Callable[[str, dict[str, Any]], Awaitable[None]],
        *,
        t_read: float | None = None,
    ) -> None:
        category = "leak" if eid == LEAK else "gas"
        situation_ko = escalation.situation_label(category)
        base = {"day": step.day, "at": step.at_local.isoformat(),
                "hhmm": step.at_local.strftime("%H:%M"), "category": category,
                "situation_ko": situation_ko}
        if abnormal:
            self._open_safety.add(eid)
        if abnormal and res is not None:
            p = res.proposal
            sim_id = f"p{len(self._tracked) + 1}"
            self._tracked[sim_id] = _Tracked(sim_id, p.proposal_id, step.day, "safety", None,
                                             None, res.decision, p.risk_tier)
            self._by_pid[p.proposal_id] = sim_id
            self._open_alert[eid] = sim_id
            day_m = self.metrics[-1]
            day_m["safety_alerts"] += 1
            # 폰에 실제로 뜬 문구: 송신 기록의 정제 페이로드를 실제 transport 코드로 펼친다.
            payload = next(
                (pl for _ph, _tp, pl in transport.sent if pl.get("proposal_id") == p.proposal_id),
                None,
            )
            rec = {**base, "kind": "first_alert", "id": sim_id, "decision": res.decision,
                   "sent": res.sent, "tier": p.risk_tier.name.lower(),
                   "severity": None if p.severity is None else p.severity.name.lower(),
                   "severity_ko": None if p.severity is None else p.severity.label_ko,
                   "ntfy": None if payload is None else ntfy_view(payload),
                   "guardian_forwarded": False,
                   "line_ko": f"{situation_ko} — 바로 경보를 보냈어요",
                   "metrics": dict(day_m)}
            self.safety_events.append(rec)
            await out("safety", rec)
            if self._rec is not None:
                # 안전 main trace 는 첫 경보 사건 바로 뒤(같은 단계)에 낸다.
                with contextlib.suppress(Exception):
                    self._emit_safety_main(transport, step, eid, category, situation_ko, sim_id,
                                           p, t_read)
            # 거주자는 다음 단계에 [괜찮아요](ok)로 확인한다 — 사다리가 멈춘다.
            self._persona_due.append((sim_id, "ok"))
        elif not abnormal and eid in self._open_safety:
            self._open_safety.discard(eid)
            rec = {**base, "kind": "normal", "id": self._open_alert.pop(eid, None),
                   "line_ko": f"{situation_ko} — 센서가 정상으로 돌아왔어요"}
            self.safety_events.append(rec)
            await out("safety", rec)

    async def _safety_sends(
        self,
        transport: _SimTransport,
        step: StepPlan,
        out: Callable[[str, dict[str, Any]], Awaitable[None]],
    ) -> None:
        """이번 단계에 나간 안전 송신(재알림·보호자 전달·해소·119 안내)을 사건으로 남긴다."""
        sent, transport.sent = transport.sent, []
        for phase, topic, payload in sent:
            pid = payload.get("proposal_id")
            if pid in self._by_pid and self._tracked[self._by_pid[pid]].kind == "comfort":
                continue
            if "severity_label" not in payload and "situation_label" not in payload:
                continue
            label = str(payload.get("situation_label") or payload.get("label") or "")
            if phase == "reading" and pid in self._by_pid:
                continue  # 첫 알림은 _safety_reading 이 이미 남겼다
            if topic != transport.topic:
                kind = "forward"  # 거주자 토픽이 아닌 곳 = 보호자
            elif GUIDANCE_119_TEXT in label:
                kind = "guidance_119"
            elif phase == "reading":
                kind = "resolved"  # 정상 복귀 값이 보낸 '해소됨' 알림
            elif phase == "ladder":
                kind = "realert"
            else:
                kind = "notice"
            alert_id = self._by_pid.get(pid) if pid else None
            if kind == "forward":
                for rec0 in self.safety_events:
                    if rec0.get("kind") == "first_alert" and rec0.get("id") in self._open_alert.values():
                        rec0["guardian_forwarded"] = True
            rec = {"day": step.day, "at": step.at_local.isoformat(),
                   "hhmm": step.at_local.strftime("%H:%M"), "kind": kind, "label": label,
                   "severity_label": payload.get("severity_label"), "phase": phase,
                   "id": alert_id, "ntfy": ntfy_view(payload),
                   "line_ko": _SAFETY_KIND_KO.get(kind, kind)}
            self.safety_events.append(rec)
            await out("safety", rec)

    def _finish_day(self, metrics: dict[str, Any]) -> None:
        _refresh_rate(metrics)

    # ── cloud 모드(실제 모델·기억). fake 모드에서는 하나도 불리지 않는다 ──────────

    async def _ext(
        self,
        kit: CloudKit,
        kind: str,
        fn: Callable[[], _T],
        usage: Callable[[_T], tuple[str | None, int | None, int | None]] | None = None,
    ) -> _T | None:
        """외부 호출 하나: 계량기 자리 → 스레드에서 실행(제한시간) → 사용량 기록.

        상한에 닿았거나 시간이 넘으면 None(호출자는 가짜로 대체한다). 기억 쪽의 송신 전 거부
        (MemoryRefused)도 None 이다. 그 밖의 예상 밖 예외도 None 이고 ``("ext_error", (종류, 예외형))``
        을 기록에 남긴다 — 실행은 계속된다. None 의 까닭은 ``self._ext_miss``(cap·timeout·refused·error).
        """
        self._ext_miss = None
        if not kit.meter.allow():
            self._ext_miss = "cap"
            return None
        self.log.append(("ext", kind))
        t0 = time.perf_counter()
        result: _T | None = None
        try:
            result = await asyncio.wait_for(asyncio.to_thread(fn), timeout=kit.call_timeout_s)
        except TimeoutError:
            self._ext_miss = "timeout"
        except MemoryRefused:
            self._ext_miss = "refused"
        except Exception as exc:  # noqa: BLE001 — 외부 호출 하나의 오류로 실행을 멈추지 않는다
            self._ext_miss = "error"
            self.log.append(("ext_error", (kind, type(exc).__name__)))
        finally:
            model, tokens_in, tokens_out = (None, None, None)
            if result is not None and usage is not None:
                model, tokens_in, tokens_out = usage(result)
            kit.meter.record(kind, model, seconds=round(time.perf_counter() - t0, 4),
                             input_tokens=tokens_in, output_tokens=tokens_out)
        return result

    @staticmethod
    def _last_comfort_judgment(app: LiotApp, result: DispatchResult | None) -> dict[str, Any] | None:
        """이번 틱의 편의 판단 — 최종 확인 강도와 판정 모델.

        서비스가 틱 안에서 판단을 저장하고 앱의 last_judgment 를 비운다(``_save_last_judgment``).
        그래서 앱이 틱마다 새로 남기는 단계 사본(last_trace, 틱 시작에 비운다)의 판단을 읽는다.
        제안이 나갔으면 그 제안의 judgment_id 와 같은 판단만 쓴다.
        """
        trace = app.last_trace or {}
        judgment: Judgment | None = trace.get("judgment")
        if judgment is None or judgment.situation_id is not None:
            return None
        if result is not None and judgment.judgment_id != result.proposal.judgment_id:
            return None
        return {"risk_tier": judgment.risk_tier, "model_id": judgment.model_id, "judgment": judgment}

    def _emit_step_traces(self) -> None:
        """이번 단계의 trace 사건을 낸다(잠자지 않는 내보내기). 조립 실패는 실행을 멈추지 않는다."""
        pending, self._pending_comfort = self._pending_comfort, None
        emit = self._trace_emit
        if pending is None or emit is None or self._rec is None:
            return
        with contextlib.suppress(Exception):
            emit(self._comfort_trace(self._rec, pending))

    def _comfort_trace(self, rec: _TraceRecorder, p: dict[str, Any]) -> dict[str, Any]:
        """편의 틱 하나의 단계(sense → … → phone[→ reason]). 침묵이면 끝난 노드에서 멈춘다."""
        result: DispatchResult | None = p["result"]
        silence: dict[str, Any] | None = p["silence"]
        decision: ProactiveDecision | None = p["decision"]
        judgment = p["judgment"]
        evaluation = rec.evaluation
        selected = None
        if evaluation is not None and not evaluation.selected_is_noop():
            selected = evaluation.selected_action()
        entity = (result.proposal.action.entity_id if result is not None
                  else selected.entity_id if selected is not None
                  else decision.candidate.entity_id if decision and decision.candidate else "")
        keyed = rec.take((entity, "comfort")) if entity else {}
        gate1: Gate1Result | None = keyed.get("gate1")
        judge_s = rec.take_judge(entity, False) if entity else None
        end = None if silence is None else _silence_end(decision, evaluation, gate1)
        nodes: list[dict[str, Any]] = []

        sense_ko = (f"바뀐 센서 {p['changed']}개" if p["changed"] else "바뀐 센서 없음")
        nodes.append(_node("sense", sense_ko + (f" · {p['weather_ko']}" if p["weather_ko"] else "")))

        matched = list(getattr(decision, "matched_rule_ids", ()) or ())
        titles = [self._title(r, self._rules[r].target_entity) for r in matched if r in self._rules]
        if end == "rules":
            why = str(getattr(decision, "suppressed_by", "") or (silence or {}).get("reason", ""))
            rules_ko = (f"{titles[0] if titles else '규칙'} 성립 — 건너뜀: "
                        f"{_SILENCE_KO.get(why, why)}")
        else:
            rules_ko = (f"성립: {titles[0]}" + (f" 외 {len(titles) - 1}개" if len(titles) > 1 else "")
                        if titles else "성립한 규칙")
        # 규칙 갈림길: 기록된 suppressed_by 그대로(마지막으로 본 규칙의 사유뿐이다).
        why_key = None if end != "rules" else getattr(decision, "suppressed_by", None)
        rule_taken = "next" if end != "rules" else why_key
        rule_values: dict[str, str | None] = {}
        if end == "rules" and why_key in _SILENCE_KO:
            rule_values[str(why_key)] = f"{_SILENCE_KO[str(why_key)]} · 마지막으로 본 규칙의 사유"
        nodes.append(_node("rules", rules_ko, real_s=rec.rules_s,
                           state="end" if end == "rules" else "done",
                           branches=_branches(_RULES_BRANCHES, rule_taken, rule_values),
                           edge_ko=None if end == "rules" else f"맞은 규칙 {len(matched)}개"))

        def tail(after: str) -> None:
            ids = list(_COMFORT_NODES[_COMFORT_NODES.index(after) + 1:])
            if self._kit is not None:
                ids.append("reason")
            for nid in ids:
                nodes.append(_node(nid, "건너뜀", state="skipped"))

        if end == "rules":
            tail("rules")
            return self._trace_event(rec, "silent", "main", None, nodes, silence)

        if evaluation is not None:
            noop = next((ac.final_score for ac in evaluation.scored if is_noop(ac.candidate)), None)
            if end == "score":
                score_ko = f"아무것도 안 하기가 이김({noop})"
            else:
                sel = evaluation.scored[evaluation.selected_index]
                label = self._title(_rule_of(sel.candidate.params), sel.candidate.entity_id)
                score_ko = f"{label} {sel.final_score}" + (
                    f" · 아무것도 안 하기 {noop}" if noop is not None else "")
        else:
            score_ko = "점수 기록 없음"
        score_branches, score_edge = self._score_branches(evaluation)
        nodes.append(_node("score", score_ko, real_s=rec.evaluation_s,
                           state="end" if end == "score" else "done", branches=score_branches,
                           edge_ko=None if end == "score" else score_edge))
        if end == "score":
            tail("score")
            return self._trace_event(rec, "silent", "main", None, nodes, silence)

        nodes.append(_node("context", "판단에 쓸 맥락 스냅샷"))
        gate1_ko = ("하한 기록 없음" if gate1 is None else
                    f"하한 {gate1.floor_tier.label_ko}" + (" · 차단" if gate1.blocked else ""))
        if end == "gate1":
            gate1_ko += " — 먼저 제안할 수 없는 확인 강도라 침묵"
        # 1차 안전 검사 갈림길: 갈래는 기록된 Gate1Result.blocked·floor_tier 로 정한다(문자열 아님).
        if gate1 is None:
            gate1_taken = None
        elif end == "gate1":
            gate1_taken = ("blocked" if gate1.blocked
                           else "ceiling" if gate1.floor_tier > PROACTIVE_TIER_CEILING else None)
        else:
            gate1_taken = ("next" if not gate1.blocked and gate1.floor_tier <= PROACTIVE_TIER_CEILING
                           else None)
        gate1_values: dict[str, str | None] = {}
        if gate1 is not None and gate1_taken == "blocked":
            gate1_values["blocked"] = f"차단 · 하한 {gate1.floor_tier.label_ko} → 침묵"
        elif gate1 is not None and gate1_taken == "ceiling":
            gate1_values["ceiling"] = (f"하한 {gate1.floor_tier.label_ko} > 능동 상한 "
                                       f"{PROACTIVE_TIER_CEILING.label_ko} → 침묵")
        nodes.append(_node("gate1", gate1_ko, real_s=keyed.get("gate1_s"),
                           state="end" if end == "gate1" else "done",
                           branches=_branches(_GATE1_BRANCHES, gate1_taken, gate1_values),
                           edge_ko=(f"하한 {gate1.floor_tier.label_ko} · 상한 안"
                                    if gate1 is not None and gate1_taken == "next" else None)))
        if end == "gate1":
            tail("gate1")
            return self._trace_event(rec, "silent", "main", None, nodes, silence)

        # 이번 틱에 판정 호출이 있었을 때만 판단을 믿는다(규칙·점수·Gate1 침묵이면 판정이 없다).
        if judge_s is None:
            judgment = None
        tier = None if judgment is None else judgment["risk_tier"]
        judge_ko = "판정 기록 없음" if tier is None else f"최종 확인 강도 {tier.label_ko}"
        if end == "judge":
            judge_ko += " — 먼저 제안하기엔 높아 침묵"
        judged: Judgment | None = None if judgment is None else judgment.get("judgment")
        judge_text = None if judged is None else self._judge_text_ko(judged, gate1, entity, p["hhmm"])
        if judged is None:
            judge_taken = None
        elif end == "judge":
            judge_taken = "ceiling" if judged.risk_tier > PROACTIVE_TIER_CEILING else None
        else:
            judge_taken = "next" if judged.risk_tier <= PROACTIVE_TIER_CEILING else None
        nodes.append(_node("judge", judge_ko, real_s=judge_s,
                           model=None if judgment is None else judgment["model_id"],
                           state="end" if end == "judge" else "done",
                           branches=_branches(_JUDGE_BRANCHES, judge_taken,
                                              {"ceiling": judge_text} if judge_taken == "ceiling"
                                              else {}),
                           edge_ko=judge_text if judge_taken == "next" else None))
        if end == "judge" or result is None:
            tail("judge")
            return self._trace_event(rec, "silent", "main", None, nodes, silence)

        prop = result.proposal
        confirm_ko = _CONFIRMATION_KO.get(prop.required_confirmation, "확인 강도 기록 없음")
        # 라우터는 카탈로그 기준으로 '올리기만' 한다(risk_router.py:87-95). 판정과 다르면 올린 것을 보인다.
        if judged is not None and prop.risk_tier != judged.risk_tier:
            route_edge = (f"판정 {judged.risk_tier.label_ko} → 기기 기준 {prop.risk_tier.label_ko}로 올림"
                          f" → {confirm_ko}")
        else:
            route_edge = f"{prop.risk_tier.label_ko} → {confirm_ko}"
        nodes.append(_node("route", f"{prop.risk_tier.label_ko} · {confirm_ko}", edge_ko=route_edge))
        gate_ko = _GATE_KO.get(result.decision, result.decision)
        suppressed = result.decision not in ("IMMEDIATE", "DIGEST")
        nodes.append(_node("gate", gate_ko, state="end" if suppressed else "done",
                           branches=_branches(_GATE_BRANCHES, result.decision,
                                              {"SUPPRESS": "사유 기록 없음"}
                                              if result.decision == "SUPPRESS" else {}),
                           edge_ko=None if suppressed else gate_ko))
        sim_id = p["sim_id"]
        rec_view = self._records.get(sim_id or "", {})
        title = rec_view.get("title_ko", "")
        if suppressed:
            nodes.append(_node("phone", "건너뜀", state="skipped"))
        else:
            where = "폰에 바로 알림" if result.decision == "IMMEDIATE" else "모아보기에 담김"
            nodes.append(_node("phone", f"{where} — {title}" if title else where))
        if self._kit is not None:
            if suppressed:
                nodes.append(_node("reason", "건너뜀", state="skipped"))
            else:
                if rec_view.get("reason_source") == "model":
                    reason_ko = f"작성 {rec_view.get('reason_model')} — 시뮬레이터가 카드 이유 문장을 쓴다"
                else:
                    reason_ko = ("문장 틀 · " + str(rec_view.get("reason_fallback_ko") or "")
                                 + " — 시뮬레이터가 카드 이유 문장을 쓴다")
                nodes.append(_node("reason", reason_ko, real_s=self._last_reason_s,
                                   model=rec_view.get("reason_model")))
        return self._trace_event(rec, "comfort", "main", sim_id, nodes, None,
                                 outcome_ko=rec_view.get("line_ko", gate_ko))

    def _branch_title(self, candidate: CandidateAction) -> str:
        """갈림길 칩 이름: 규칙 제목, 없으면 기기 이름(기기 id 는 쓰지 않는다)."""
        rule = self._rules.get(_rule_of(candidate.params) or "")
        if rule is not None and rule.title_ko:
            return rule.title_ko
        name = self._reg.device_name(candidate.entity_id) if self._reg is not None else ""
        return f"{name} 제안" if name and name != candidate.entity_id else "후보 행동"

    def _score_branches(
        self, evaluation: EvaluationResult | None,
    ) -> tuple[list[dict[str, Any]], str | None]:
        """점수 갈림길: 후보 상위 3개(선택 포함) + 아무것도 안 하기. 값은 평가 결과 그대로."""
        if evaluation is None or not evaluation.scored:
            return _branches((_UNKNOWN,), None), None
        scored = evaluation.scored
        sel = evaluation.selected_index
        noop_idx = next((i for i, ac in enumerate(scored) if is_noop(ac.candidate)), None)
        ranked = sorted((i for i in range(len(scored)) if i != noop_idx),
                        key=lambda i: -scored[i].final_score)[:3]
        if sel != noop_idx and sel not in ranked:
            ranked = [*ranked[:2], sel]
        table: list[tuple[str, str, bool]] = [
            (f"c{i}", self._branch_title(scored[i].candidate), False) for i in ranked]
        values: dict[str, str | None] = {f"c{i}": f"{scored[i].final_score}" for i in ranked}
        if noop_idx is not None:
            table.append(("noop", "아무것도 안 하기", True))
            values["noop"] = f"{scored[noop_idx].final_score}"
        table.append(_UNKNOWN)
        taken = "noop" if sel == noop_idx else f"c{sel}"
        edge = None
        if sel != noop_idx:
            chosen = scored[sel]
            edge = f"{self._branch_title(chosen.candidate)} {chosen.final_score}"
            if noop_idx is not None:
                noop_score = scored[noop_idx].final_score
                # 같은 점수면 앞 후보를 고른다(evaluator.py:229, max 의 첫 값) — 그래서 '≥'.
                edge += f" {'>' if chosen.final_score > noop_score else '≥'} 아무것도 안 하기 {noop_score}"
            edge += f" · 출처: {_origin_ko(chosen.origin)}"
        return _branches(tuple(table), taken, values), edge

    def _luna_ko(self, entity: str, hhmm: str) -> str | None:
        """같은 단계·같은 기기의 luna 비교 기록(단계 끝 그림자) — 없으면 None."""
        luna = next((r for r in reversed(self.luna_results)
                     if r.get("entity_id") == entity and r["day"] == self._day
                     and r["hhmm"] == hhmm), None)
        if luna is None:
            return None
        probs = (luna.get("luna") or {}).get("tier") or {}
        if luna.get("status") != "ok" or not probs:
            return str(luna.get("reason_ko") or "luna 미접근")
        top = max(probs, key=lambda k: probs[k])
        agree = (luna.get("agree") or {}).get("tier")
        agree_ko = "—" if agree is None else ("일치" if agree else "불일치")
        return f"luna {float(probs[top]) * 100:.0f}% {agree_ko}"

    def _judge_text_ko(self, judgment: Judgment, gate1: Gate1Result | None, entity: str,
                       hhmm: str) -> str:
        """판정 문구: 최종 단계와 모델 답(Jev 최댓값)을 나눠 적는다. 기록된 값만 읽는다."""
        kit = self._kit
        if kit is None:
            return "가짜 판정(확률 없음)"
        if kit.judge is None or judgment.model_id != kit.judge.model_id:
            # 상한에 닿아 가짜 판정기로 바뀌었다(_RecordingJudge.model_id — 그때 고른 쪽 이름).
            return "상한 도달 · 가짜 판정(확률 없음)"
        if judgment.gate2_verdict == "degraded_fail_safe" or judgment.degraded:
            return (f"모델 응답 없음 → 안전하게 올림({judgment.risk_tier.label_ko})"
                    " → 상한이라 침묵")
        parts: list[str] = []
        floor = gate1.floor_tier if gate1 is not None else None
        if judgment.gate2_verdict == "injection_suspect":
            parts.append("모델이 하한보다 낮게 답해 하한 유지")
        else:
            parts.append(f"최종 {judgment.risk_tier.label_ko}"
                         + (f"(하한 {floor.label_ko})" if floor is not None else ""))
        jev = _jev_ko(judgment.model_probs, "tier")
        if jev:
            parts.append(jev)
        luna = self._luna_ko(entity, hhmm)
        if luna:
            parts.append(luna)
        return " · ".join(parts)

    def _safety_model_text_ko(self, row: dict[str, Any], entity: str, hhmm: str,
                              floor_tier: RiskTier | None = None) -> str:
        """안전 2단계 판정 값(model → severity 엣지): Jev 최댓값(단계·파급도)과 luna. 기록만 읽는다.

        Jev 최댓값이 하한(첫 경보 단계 · 행의 파급도 하한)보다 낮으면 반영되지 않았다고 적는다 —
        LLMJudge 는 하한 아래 답을 무시한다(escalate_only).
        """
        kit = self._kit
        if kit is None:
            return "가짜 판정(확률 없음)"
        if row["degraded"]:
            return "모델 응답 없음 · 하한 유지"
        if kit.judge is None or row["model_id"] != kit.judge.model_id:
            return "상한 도달 · 가짜 판정(확률 없음)"
        probs = row["model_probs"]
        parts: list[str | None] = [_jev_ko(probs, "tier")]
        top = _jev_top(probs, "tier")
        with contextlib.suppress(Exception):
            if parts[0] and top is not None and floor_tier is not None and RiskTier.from_str(top) < floor_tier:
                parts.append(f"하한({floor_tier.label_ko}) 아래라 반영 안 됨")
        parts.append(_jev_ko(probs, "severity"))
        sev_top = _jev_top(probs, "severity")
        sev_floor: SeverityLevel | None = row.get("severity_floor")
        with contextlib.suppress(Exception):
            if parts[-1] and sev_top is not None and sev_floor is not None and (
                    SeverityLevel.from_str(sev_top) < sev_floor):
                parts.append(f"파급도 하한({sev_floor.label_ko}) 아래라 반영 안 됨")
        parts.append(self._luna_ko(entity, hhmm))
        shown = [x for x in parts if x]
        return " · ".join(shown) if shown else "확률 기록 없음"

    def _trace_event(self, rec: _TraceRecorder, kind: str, part: str, proposal_id: str | None,
                     nodes: list[dict[str, Any]], silence: dict[str, Any] | None, *,
                     outcome_ko: str | None = None) -> dict[str, Any]:
        out: dict[str, Any] = {"trace_id": rec.next_id(), "kind": kind, "part": part,
                               "nodes": nodes}
        if proposal_id is not None:
            out["proposal_id"] = proposal_id
        if outcome_ko is None and silence is not None:
            outcome_ko = f"{silence['title_ko']} — 건너뜀: {silence['reason_ko']}"
        out["outcome_ko"] = outcome_ko or ""
        return out

    def _emit_safety_main(
        self,
        transport: _SimTransport,
        step: StepPlan,
        eid: str,
        category: str,
        situation_ko: str,
        sim_id: str,
        p: Any,
        t_read: float | None,
    ) -> None:
        """안전 main: sense → classify → floor → first_alert → phone. 시간은 first_alert 하나뿐 —
        읽기 시작부터 그 상황 첫 전송을 받은 때까지(나머지 단계는 따로 재지 않는다)."""
        rec, emit = self._rec, self._trace_emit
        if rec is None or emit is None:
            return
        keyed = rec.take((eid, "safety1"))
        gate1: Gate1Result | None = keyed.get("gate1")
        sent = (transport.sent_at or {}).get(p.proposal_id)
        first_s = None if sent is None or t_read is None else max(0.0, sent - t_read)
        name = self._reg.device_name(eid) if self._reg is not None else eid
        if not name or name == eid:  # 기기 id 는 화면 글에 쓰지 않는다
            name = f"{situation_ko} 감지기"
        floor_ko = "하한 " + (gate1.floor_tier.label_ko if gate1 is not None else p.risk_tier.label_ko)
        if p.severity is not None:
            floor_ko += f" · 파급도 {p.severity.label_ko}({self._severity_sources_ko(p)})"
        nodes = [
            _node("sense", f"{name} 이상 값"),
            _node("classify", f"안전 카테고리 — {situation_ko}", edge_ko="이상 값 → 안전 경보 경로"),
            _node("floor", floor_ko),
            _node("first_alert", "읽기부터 첫 경보 전송까지 "
                  + ("—" if first_s is None else f"{first_s * 1000.0:.1f}ms") + " · 모델 없이",
                  real_s=first_s,
                  # 노드의 real_ms 와 같은 반올림 값에서 만든다 — 두 값이 갈리지 않는다.
                  edge_ko="모델 없이 즉시" + ("" if first_s is None
                                           else f" · {_ms(first_s):.1f}ms")),
            _node("phone", "폰에 바로 경보"),
        ]
        emit(self._trace_event(rec, "safety", "main", sim_id, nodes, None,
                               outcome_ko=f"{situation_ko} — 바로 경보를 보냈어요"))
        self._pending_safety.append({"eid": eid, "category": category, "sim_id": sim_id,
                                     "situation_ko": situation_ko, "first": p,
                                     "situation_id": p.situation_id,
                                     "hhmm": step.at_local.strftime("%H:%M")})

    def _severity_sources_ko(self, p: Proposal) -> str:
        """첫 경보 파급도 하한의 출처(표·거주자 설정) — 그 경보의 1단계 판단(앱 단계 사본)에서 읽는다."""
        trace = (self._svc.app.last_trace if self._svc is not None else None) or {}
        judgment: Judgment | None = trace.get("judgment")
        if judgment is None or judgment.judgment_id != p.judgment_id:
            return "출처 기록 없음"
        names = [_SEVERITY_SOURCE_KO.get(src, src) for src in judgment.severity_sources]
        return "·".join(names) if names else "출처 기록 없음"

    async def _safety_judgment_row(self, situation_id: str | None,
                                   first_judgment_id: str) -> dict[str, Any] | None:
        """그 상황의 2단계 판단 행 — 시뮬레이터 자신의 DB(읽기만). 1단계 행뿐이면 None."""
        if self._db is None or situation_id is None:
            return None
        cur = await self._db.execute(
            "SELECT risk_tier, severity, severity_floor, model_id, degraded, rationale,"
            " judgment_id, model_probs FROM judgments WHERE situation_id = ? AND judgment_id != ?"
            " ORDER BY rowid DESC LIMIT 1",
            (situation_id, first_judgment_id),
        )
        row = await cur.fetchone()
        await cur.close()
        if row is None:
            return None
        return {"risk_tier": RiskTier.from_str(int(row[0])),
                "severity": None if row[1] is None else SeverityLevel(int(row[1])),
                "severity_floor": None if row[2] is None else SeverityLevel(int(row[2])),
                "model_id": str(row[3]), "degraded": bool(row[4]),
                "rationale": str(row[5]), "situation_id": situation_id,
                "model_probs": _json_probs(row[7])}

    async def _followup_sent(self, situation_id: str | None, first_pid: str) -> bool:
        """그 상황에 첫 경보 말고 다른 제안(2단계가 올린 후속)이 저장됐고 실제로 송신됐는가."""
        svc = self._svc
        sent_at = getattr(svc.transport, "sent_at", None) if svc is not None else None
        if self._db is None or situation_id is None or not sent_at:
            return False
        cur = await self._db.execute(
            "SELECT proposal_id FROM proposals WHERE situation_id = ? AND proposal_id != ?",
            (situation_id, first_pid),
        )
        rows = await cur.fetchall()
        await cur.close()
        return any(str(row[0]) in sent_at for row in rows)

    async def _emit_followups(self) -> None:
        """안전 followup: model → severity → followup → ladder(이번 단계에 열린 상황마다)."""
        rec, emit, svc = self._rec, self._trace_emit, self._svc
        if rec is None or emit is None or svc is None:
            return
        for s in self._pending_safety:
            eid = s["eid"]
            judge_s = rec.take_judge(eid, True)
            first: Proposal = s["first"]
            row = await self._safety_judgment_row(s["situation_id"], first.judgment_id)
            deadline = row is not None and row["degraded"] and "제한시간" in row["rationale"]
            luna = next((r for r in reversed(self.luna_results)
                         if r.get("entity_id") == eid and r["day"] == self._day
                         and r["hhmm"] == s["hhmm"]), None)
            if row is None:
                model_ko, model_s = "2단계 판정 기록 없음", None
            elif deadline:
                model_ko, model_s = "마감 안에 답이 없어 하한 판정 유지", None
            else:
                model_ko = f"2단계 위험 판정 — {row['risk_tier'].label_ko}"
                if row["degraded"]:
                    model_ko += " · 모델 비가용, 하한 유지"
                model_s = judge_s
                if luna is not None and luna.get("agree"):
                    agree = luna["agree"].get("tier")
                    model_ko += " · luna " + ("—" if agree is None else ("일치" if agree else "불일치"))
            raised = await self._followup_sent(s["situation_id"], first.proposal_id)
            if row is None:
                model_taken = None
            elif deadline:
                model_taken = "deadline"
            elif row["degraded"]:
                model_taken = "error"
            else:
                model_taken = "sent" if raised else "not_sent"
            model_branches = _branches(_MODEL_BRANCHES, model_taken)
            nodes = [_node("model", model_ko, real_s=model_s,
                           model=None if row is None else row["model_id"],
                           branches=model_branches,
                           edge_ko=None if row is None else self._safety_model_text_ko(
                               row, eid, s["hhmm"], first.risk_tier))]
            if row is not None and row["severity"] is not None:
                sev_ko = f"파급도 {row['severity'].label_ko}"
                if row["severity_floor"] is not None:
                    sev_ko += f"(하한 {row['severity_floor'].label_ko})"
            else:
                sev_ko = "파급도 기록 없음"
            taken_model = next(b for b in model_branches if b["taken"])
            nodes.append(_node("severity", sev_ko, edge_ko=taken_model["label_ko"]))
            nodes.append(_node("followup", "올린 판정으로 따라가는 알림을 보냈어요" if raised
                               else "따라가는 알림 없음 · 올렸는지 기록 없음"))
            ladder_ko = "재알림 예약 기록 없음"
            ladder_taken: str | None = None
            ladder_values: dict[str, str | None] = {}
            if row is not None:
                ladder = await svc.safety.get_ladder(row["situation_id"])
                if ladder is not None and ladder.next_fire_at:
                    when = datetime.fromisoformat(ladder.next_fire_at).astimezone(
                        ZoneInfo(HOME_TZ)).strftime("%H:%M")
                    ladder_ko = f"재알림 예약 {when}"
                    ladder_taken, ladder_values = "scheduled", {"scheduled": when}
                elif ladder is not None and ladder.acked:
                    ladder_ko = "확인돼 재알림 멈춤"
                    ladder_taken = "acked"
                elif ladder is not None:
                    ladder_ko = "재알림 예약 없음"
                    ladder_taken = "none"
            nodes.append(_node("ladder", ladder_ko,
                               branches=_branches(_LADDER_BRANCHES, ladder_taken, ladder_values)))
            emit(self._trace_event(rec, "safety", "followup", s["sim_id"], nodes, None,
                                   outcome_ko=f"{s['situation_ko']} — 2단계 판정 뒤"))

    async def _pref_cells(self, svc: LiotService) -> tuple[dict[str, str], dict[str, float]]:
        """선호 셀 읽기(읽기 전용 SELECT, preference_store.py:78-105): (제안 → 셀 키, 셀 값)."""
        return dict(svc._prop_cell), await load_cells(svc.prefs)

    def _expiry_pref(self, pid: str) -> tuple[float, float] | None:
        """만료된 제안 칸의 이번 틱 직전·직후 선호. 못 읽었으면 None."""
        tick = self._pref_tick
        if tick is None or tick[2] is None or pid not in tick[0]:
            return None
        key = tick[0][pid]
        return tick[1].get(key, NEUTRAL_PREF), tick[2].get(key, NEUTRAL_PREF)

    def _emit_feedback_trace(self, tracked: _Tracked, signal: str, source: str, code: str,
                             title: str, *, pref: tuple[float, float] | None = None) -> None:
        """피드백 trace: 편의는 phone → feedback → prefs, 안전 확인은 phone → ack."""
        rec, emit = self._rec, self._trace_emit
        if rec is None or emit is None:
            return
        who = _SOURCE_KO.get(source, source)
        signal_ko = _SIGNAL_KO.get(signal, signal)
        ok = code == "200"
        if tracked.kind == "safety":
            nodes = [_node("phone", f"{who} [{signal_ko}] — {title}"),
                     _node("ack", "확인 반영 — 재알림을 멈춰요" if ok else f"반영되지 않음({code})",
                           state="done" if ok else "end")]
        elif signal == "expired":
            pref_ko = ("선호 변화 기록 없음" if pref is None
                       else f"이 제안 칸 선호 {pref[0]:.2f} → {pref[1]:.2f}")
            nodes = [_node("phone", f"아무도 누르지 않아 만료 — {title}",
                           branches=_branches(_FEEDBACK_BRANCHES, "expired")),
                     _node("feedback", "만료 신호 반영",
                           edge_ko=pref_ko + " · 같은 순간 다른 만료의 감쇠 포함"),
                     _node("prefs", "이번 단계 판단부터 반영")]
        else:
            pref_ko = ("선호 변화 기록 없음" if pref is None
                       else f"이 제안 칸 선호 {pref[0]:.2f} → {pref[1]:.2f}")
            nodes = [_node("phone", f"{who} [{signal_ko}] — {title}",
                           branches=_branches(_FEEDBACK_BRANCHES, signal if ok else "refused",
                                              {} if ok else {"refused": f"[{signal_ko}]"})),
                     _node("feedback", "서명 링크 확인 → 반영" if ok else f"반영되지 않음({code})",
                           state="done" if ok else "end", edge_ko=pref_ko if ok else None),
                     _node("prefs", "다음 판단부터 반영" if ok else "건너뜀",
                           state="done" if ok else "skipped")]
        line = (f"{who} [{signal_ko}] — {title}" if signal != "expired"
                else f"만료 — {title}")
        emit(self._trace_event(rec, "feedback", "feedback", tracked.sim_id, nodes, None,
                               outcome_ko=line))

    async def _announce_trip(
        self, kit: CloudKit, out: Callable[[str, dict[str, Any]], Awaitable[None]]
    ) -> None:
        """상한에 처음 닿은 단계에서 한 번 알린다. 남은 단계는 가짜 모델로 돈다."""
        if kit.meter.tripped is None or self._trip_announced:
            return
        self._trip_announced = True
        await out("models.fallback", {"reason": kit.meter.tripped,
                                      "line_ko": kit.meter.trip_reason_ko() or "",
                                      "usage": kit.meter.summary()})

    @staticmethod
    def _usage_totals(usage: dict[str, Any]) -> dict[str, float]:
        kinds = usage["by_kind"].values()
        return {"calls": float(usage["calls"]), "usd": float(usage["run_usd"]),
                "seconds": float(sum(k["seconds"] for k in kinds)),
                "done": float(sum(k["calls"] for k in kinds))}

    def _close_day_usage(self, day: int, usage: dict[str, Any]) -> dict[str, Any]:
        """그날 쓴 만큼(호출 수·평균 지연·추정 비용) — 하루 시작 때 잡아 둔 값과의 차이."""
        now, start = self._usage_totals(usage), self._usage_mark
        calls = int(now["calls"] - start.get("calls", 0.0))
        done = now["done"] - start.get("done", 0.0)
        avg = None if done <= 0 else round((now["seconds"] - start.get("seconds", 0.0)) / done, 3)
        usd = round(now["usd"] - start.get("usd", 0.0), 6)
        row = {"day": day, "calls": calls, "avg_seconds": avg, "usd": usd,
               "calls_ko": f"{calls}회",
               "avg_ko": "—" if avg is None else f"{avg:.2f}초",
               "usd_ko": f"${usd:.4f}"}
        self.day_usage.append(row)
        return row

    def _memory_event(self, verb: str, who: str, title: str, *, rule: bool = False,
                      ok: bool = True) -> dict[str, Any]:
        """기억 패널 한 줄. 남기고(요약) 다음 사건 경계에서 ``memory`` 사건으로 내보낸다."""
        verb_ko, who_ko = _MEMORY_VERB_KO[verb], _MEMORY_WHO_KO[who]
        rec = {"day": self._day, "clock_ko": self._clock_ko, "verb": verb, "verb_ko": verb_ko,
               "who": who, "who_ko": who_ko, "title_ko": title, "rule": rule, "ok": ok,
               "source_ko": "memgineering · liot-demo",
               "line_ko": f"{who_ko} 기억 {verb_ko}{' (규칙)' if rule else ''}"
                          f"{'' if ok else ' — 실패'} — {title}"}
        self.memory_events.append(rec)
        self._memory_unsent.append(rec)
        return rec

    async def _flush_memory(self, out: Callable[[str, dict[str, Any]], Awaitable[None]]) -> None:
        unsent, self._memory_unsent = self._memory_unsent, []
        for rec in unsent:
            await out("memory", dict(rec))

    async def _cloud_start(
        self, kit: CloudKit, out: Callable[[str, dict[str, Any]], Awaitable[None]]
    ) -> None:
        """실행 시작: 데모 브레인 확인(use) → 거주자 취향 심기(처음 한 번) → 부품 상태 알림."""
        memory = kit.memory
        status = next((st for st in kit.status if st["part"] == "memory"), None)
        if memory is not None:
            ready = await self._ext(kit, "memory", memory.check_ready)
            if ready:
                self._house = HouseMemory(memory, self.house_id)
                self._resident = ResidentMemory(memory)
                await self._seed_resident(kit, self._resident)
            if status is not None:
                status.update(state="cloud" if ready else "fake",
                              reason_ko=None if ready else memory.disabled_reason_ko)
        await self._flush_memory(out)
        await out("models.status", {"status": [{**st, "part_ko": _PART_KO.get(st["part"], st["part"])}
                                               for st in kit.status],
                                    "line_ko": "실제 모델·기억으로 시작해요",
                                    "note_ko": "실제 모델은 같은 씨앗이어도 결과가 매번 달라요.",
                                    "usage": kit.meter.summary()})
        await self._announce_trip(kit, out)

    async def _seed_resident(self, kit: CloudKit, resident: ResidentMemory) -> None:
        """거주자 취향 심기. CLI 호출(있나 보기·심기) 하나하나가 계량기 자리 하나다."""
        reason = "하루 시뮬레이션 가상 거주자 취향"
        for sentence in RESIDENT_PREFERENCES:
            def look(s: str = sentence) -> bool:
                return resident.exists(s)

            def plant(s: str = sentence) -> str | None:
                return resident.plant(s, reason=reason)

            found = await self._ext(kit, "memory", look)
            if found is None or found:
                continue  # 상한·시간 초과이거나 이미 있다
            if await self._ext(kit, "memory", plant) is not None:
                self._memory_event("seed", "resident", sentence)

    async def _recall(self, kit: CloudKit, view: HouseMemory | ResidentMemory | None,
                      query: str, who: str) -> list[MemoryCard]:
        if view is None:
            return []
        cards = await self._ext(kit, "memory", lambda: view.recall(query))
        for card in cards or []:
            self._memory_event("recall", who, card.title, rule=card.rule)
        return list(cards or [])

    async def _cloud_reason(
        self, kit: CloudKit, title: str, scene: str | None, step: StepPlan, template: str
    ) -> tuple[str, dict[str, Any]]:
        """이유 작성기: 집 기억(장면 이름으로 recall)과 날씨 숫자로 한 줄. 실패면 규칙 문장."""
        cards = await self._recall(kit, self._house, title, "house")
        fields: dict[str, Any] = {"reason_source": "template", "reason_model": "template",
                                  "memory_used": [c.title for c in cards]}
        cheap = kit.cheap
        if cheap is None or not template:
            fields["reason_fallback"] = "no_kit" if cheap is None else "no_template"
            fields["reason_fallback_ko"] = _FALLBACK_KO[fields["reason_fallback"]]
            return template, fields
        weather = step.weather
        facts = {
            "title": title,
            "scene": scene or "",
            "hhmm": step.at_local.strftime("%H:%M"),
            "pm25": weather.get("pm25"),
            "cloud_pct": None if weather.get("cloud_cover") is None
            else round(float(weather["cloud_cover"]) * 100),
            "outdoor_temp_c": weather.get("outdoor_temp_c"),
            "rule_sentence": template,
            "memories": [f"{c.title} — {c.summary}" if c.summary else c.title for c in cards],
        }
        t_reason = time.perf_counter()
        res = await self._ext(kit, "reason", lambda: cheap.reason(facts, fallback=template),
                              usage=_cheap_usage)
        self._last_reason_s = time.perf_counter() - t_reason
        if res is None or res.source != "model":
            fields["reason_fallback"] = (self._ext_miss or "timeout") if res is None else (
                res.fallback_reason or "invalid_output")
            fields["reason_fallback_ko"] = _FALLBACK_KO.get(fields["reason_fallback"],
                                                            fields["reason_fallback"])
            return template, fields
        fields.update(reason_source="model", reason_model=f"{res.provider}:{res.model}")
        return res.text, fields

    async def _cloud_reaction(
        self,
        kit: CloudKit,
        sim_id: str,
        scene: str | None,
        title: str,
        reason: str,
        step: StepPlan,
        table: dict[str, str] | None = None,
    ) -> str | None:
        """거주자 결정기: 취향 기억을 읽고 싼 모델이 고른다. 답이 이상하면 성향 표."""
        # 성향 표 값은 늘 먼저 정한다(동전 난수를 fake 와 같은 순서로 쓴다) — 대체값이기도 하다.
        fallback = self._persona_reaction(scene, table) or "ignore"
        self._decided_by[sim_id] = "persona_table"
        cheap = kit.cheap
        if cheap is None:
            self._note_reaction(sim_id, "no_kit")
            return None if fallback == "ignore" else fallback
        cards = await self._recall(kit, self._resident, title, "resident")
        facts = {
            "title": title,
            "reason": reason,
            "hhmm": step.at_local.strftime("%H:%M"),
            "where": "모아보기" if table is not None else "폰 알림",
            "likes": [c.title for c in cards],
        }
        res = await self._ext(
            kit, "resident",
            lambda: cheap.choose(facts, _RESIDENT_OPTIONS, fallback=fallback),
            usage=_cheap_usage,
        )
        choice = fallback
        if res is not None and res.source == "model" and res.text in _RESIDENT_OPTIONS.values():
            choice = res.text
            self._decided_by[sim_id] = "model"
            self._note_reaction(sim_id, None)
        elif res is None:
            self._note_reaction(sim_id, self._ext_miss or "timeout")
        else:
            self._note_reaction(sim_id, res.fallback_reason or "invalid_output")
        return None if choice == "ignore" else choice

    def _note_reaction(self, sim_id: str, why: str | None) -> None:
        """거주자 결정의 출처를 제안 기록에 붙인다(cloud 모드만). 성향 표면 그 까닭도."""
        rec = self._records.get(sim_id)
        if why is None:
            self._reaction_fallback.pop(sim_id, None)
        else:
            self._reaction_fallback[sim_id] = why
        if rec is not None:
            rec["decided_by"] = self._decided_by[sim_id]
            rec["decided_by_ko"] = _DECIDED_KO[self._decided_by[sim_id]]
            if why is None:
                rec.pop("reaction_fallback", None)
                rec.pop("reaction_fallback_ko", None)
            else:
                rec["reaction_fallback"] = why
                rec["reaction_fallback_ko"] = _FALLBACK_KO.get(why, why)

    async def _remember_feedback(
        self,
        kit: CloudKit,
        tracked: _Tracked,
        state: str,
        step: StepPlan,
        out: Callable[[str, dict[str, Any]], Awaitable[None]],
    ) -> None:
        """집 기억: [도움됐어요]·만료는 일화, [그만해]는 규칙(--rule)."""
        house = self._house
        if house is None or tracked.kind != "comfort":
            return
        title = self._records[tracked.sim_id]["title_ko"]
        hhmm = step.at_local.strftime("%H:%M")
        if state == "dismissed":
            text, rule, kind = f"'{title}' 제안은 [그만해] — 다시 먼저 제안하지 않는다", True, "decision"
        elif state == "accepted":
            text, rule, kind = f"{step.day}일차 {hhmm} '{title}' 제안이 도움됐다", False, "episode"
        else:
            text, rule, kind = f"{step.day}일차 '{title}' 제안은 아무도 누르지 않아 만료됐다", False, "episode"
        op = await self._ext(kit, "memory", lambda: house.remember(
            text, reason=f"하루 시뮬레이션 [{_SIGNAL_KO.get(state, state)}]", rule=rule, kind=kind))
        self._memory_event("remember", "house", text, rule=rule, ok=op is not None)
        await self._flush_memory(out)

    async def _luna_shadows(
        self, kit: CloudKit, step: StepPlan, out: Callable[[str, dict[str, Any]], Awaitable[None]]
    ) -> None:
        """이번 단계의 Jev 판정마다 luna 확률을 나란히 남긴다(판정은 바꾸지 않는다).

        luna 요청 하나하나(위험도 1 + 파급도 1)가 계량기 자리 하나다: ``allow`` 가 요청 직전에
        자리를 잡고(외부 호출 기록), ``on_request`` 가 끝난 요청의 지연·토큰을 더한다.
        """
        due, self._shadow_due[:] = list(self._shadow_due), []
        luna = kit.luna
        if luna is None:
            return

        def gate() -> bool:
            if not kit.meter.allow():
                return False
            self.log.append(("ext", "luna"))
            return True

        def spent(record: dict[str, Any]) -> None:
            kit.meter.record("luna", luna.cfg.model, seconds=record.get("seconds"),
                             input_tokens=record.get("input_tokens"),
                             output_tokens=record.get("output_tokens"))

        for evidence, probs, model_id, served, entity_id in due:
            res: ShadowResult | None
            miss_ko = "luna 미접근 — 시간 초과"
            if luna.cfg.mode == "logprobs":
                def compare(ev: str = evidence,
                            pr: dict[str, dict[str, float]] = probs) -> ShadowResult:
                    return luna.compare(ev, pr, allow=gate, on_request=spent)

                try:
                    res = await asyncio.wait_for(asyncio.to_thread(compare),
                                                 timeout=2 * kit.call_timeout_s)
                except TimeoutError:
                    res = None
                except Exception as exc:  # noqa: BLE001 — 그림자는 판정·실행을 멈추지 않는다
                    self.log.append(("ext_error", ("luna", type(exc).__name__)))
                    miss_ko = "luna 미접근 — 비교 중 오류"
                    res = None
            else:
                res = luna.compare(evidence, probs)  # 부르지 않는 방식 — 외부 호출 없음
            rec: dict[str, Any] = {
                "day": step.day, "hhmm": step.at_local.strftime("%H:%M"), "judge": model_id,
                "judge_model": served, "entity_id": entity_id,
                "jev": probs, "status": "unavailable" if res is None else res.status,
                "luna": {} if res is None else res.probs,
                "agree": {} if res is None else res.agree,
                "reason_ko": (miss_ko if res is None else res.reason_ko),
            }
            self.luna_results.append(rec)
            await self._attach_judge(step, entity_id, rec, out)

    async def _attach_judge(
        self,
        step: StepPlan,
        entity_id: str,
        rec: dict[str, Any],
        out: Callable[[str, dict[str, Any]], Awaitable[None]],
    ) -> None:
        """같은 단계·같은 기기의 컴포트 제안에 Jev·luna 확률을 붙인다('속 보기'용, 계산은 여기서)."""
        hhmm = step.at_local.strftime("%H:%M")
        target = next((r for r in self._records.values()
                       if r["day"] == step.day and r["hhmm"] == hhmm
                       and r["entity_id"] == entity_id and "judge" not in r), None)
        if target is None:
            return
        questions: list[dict[str, Any]] = []
        for qid, jev in rec["jev"].items():
            luna = rec["luna"].get(qid, {})
            labels = (RiskTier.from_str if qid == "tier" else SeverityLevel.from_str)
            jev_choice = max(jev, key=lambda k: jev[k]) if jev else None
            luna_choice = max(luna, key=lambda k: luna[k]) if luna else None
            agree = rec["agree"].get(qid)
            questions.append({
                "question": qid,
                "question_ko": _QUESTION_KO.get(qid, qid),
                "options": [
                    {"id": oid, "label_ko": labels(oid).label_ko,
                     "jev": round(float(jev[oid]), 3),
                     "luna": None if oid not in luna else round(float(luna[oid]), 3),
                     "jev_pct_ko": f"{float(jev[oid]) * 100:.0f}%",
                     "luna_pct_ko": "—" if oid not in luna else f"{float(luna[oid]) * 100:.0f}%"}
                    for oid in jev
                ],
                "jev_choice": jev_choice,
                "luna_choice": luna_choice,
                "agree": agree,
                "agree_ko": "—" if agree is None else ("일치" if agree else "불일치"),
            })
        view = {"model_id": rec["judge"], "served_model": rec["judge_model"],
                "luna_status": rec["status"], "luna_reason_ko": rec["reason_ko"],
                "questions": questions}
        target["judge"] = view
        await out("judge", {"id": target["id"], "judge": view})


# ── 시연용 단계 기록(trace) 사건 조립 ───────────────────────────────────────

_COMFORT_NODES = ("sense", "rules", "score", "context", "gate1", "judge", "route", "gate", "phone")
_NODE_KO = {
    "sense": "감지", "rules": "규칙", "score": "점수", "context": "맥락", "gate1": "1차 안전 검사",
    "judge": "위험 판정", "route": "확인 강도", "gate": "알림 게이트", "phone": "폰",
    "reason": "이유 작성",
    "classify": "안전 분류", "floor": "하한", "first_alert": "첫 경보", "model": "2단계 판정",
    "severity": "파급도", "followup": "따라가는 알림", "ladder": "재알림 사다리",
    "feedback": "피드백", "prefs": "개인화", "ack": "확인",
}
_GATE_KO = {"IMMEDIATE": "바로 폰 알림", "DIGEST": "모아보기", "SUPPRESS": "보류"}
# 확인 강도(risk.required_confirmation) → 한국어. 관제실 실험실(app.js CONFIRM_LABELS)의
# none·explicit 표기를 따르고, light·double 은 판정 질문의 선택지 문구(가벼운 승인·이중확인)를 따른다.
_CONFIRMATION_KO = {"none": "확인 불필요", "light": "가벼운 확인", "explicit": "명시 확인",
                    "double": "이중 확인"}


# ── 갈림길(branches) — 노드에서 나가는 길. (key, 한국어 이름, 출구 여부) ─────────────
# 표에 없는 값이 오면 'unknown'(기록 없음)이 간 길이 된다 — 간 길은 늘 정확히 하나다.
_UNKNOWN = ("unknown", "기록 없음", True)
# 규칙: ProactiveDecision.suppressed_by 값(proactive.py:301-364, validate_proactive_candidate
# proactive.py:136-151). budget·no_match 는 규칙이 하나도 맞지 않을 때만 나며, 그때는 후보가 없어
# trace 를 내지 않는다(이 그림에는 흐린 갈림길로만 남는다).
_RULES_BRANCHES: tuple[tuple[str, str, bool], ...] = (
    ("next", "다음으로", False),
    ("cooldown", "잠시 쉬는 중", True),
    ("rejected", "30일 쉬는 중", True),
    ("budget", "제안 수 다 씀", True),
    ("no_match", "조건 안 맞음", True),
    ("tier_ceiling", "확인 강도 상한", True),
    ("safety_target", "안전 기기", True),
    ("not_allowed", "먼저 제안할 수 없는 행동", True),
    ("capability_mismatch", "기기 종류 불일치", True),
    _UNKNOWN,
)
# 1차 안전 검사: Gate1Result.blocked·floor_tier 와 능동 상한(app.py:403-414).
_GATE1_BRANCHES: tuple[tuple[str, str, bool], ...] = (
    ("next", "상한 안", False),
    ("ceiling", "능동 상한", True),
    ("blocked", "차단", True),
    _UNKNOWN,
)
# 위험 판정: 판정 뒤 능동 상한(app.py:449-455).
_JUDGE_BRANCHES: tuple[tuple[str, str, bool], ...] = (
    ("next", "상한 안", False),
    ("ceiling", "판정 뒤 상한", True),
    _UNKNOWN,
)
# 알림 게이트: GateDecision(notifier.py:59) = DispatchResult.decision. 보류 사유는 서비스가 내주지 않는다.
_GATE_BRANCHES: tuple[tuple[str, str, bool], ...] = (
    ("IMMEDIATE", "바로 폰 알림", False),
    ("DIGEST", "모아보기", False),
    ("SUPPRESS", "보류", True),
    _UNKNOWN,
)
# 안전 2단계: 서비스는 올렸을 때만 후속을 보내고(service.py:641-672), '그대로'와 '올렸지만
# 상황이 닫힘'은 둘 다 전송 없이 None 을 돌려준다(service.py:649-658) — 기록으로 가를 수 없어
# not_sent 하나로 합친다. 마감 초과·판정 오류는 app.deadline_judgment(degraded, 제한시간/판정 오류).
_MODEL_BRANCHES: tuple[tuple[str, str, bool], ...] = (
    ("sent", "따라가는 알림 보냄", False),
    ("not_sent", "따라가는 알림 없음 · 올렸는지 기록 없음", False),
    ("deadline", "마감 초과 · 하한 유지", False),
    ("error", "판정 오류 · 하한 유지", False),
    _UNKNOWN,
)
# 재알림 사다리: 단계 끝 SafetyRepository.get_ladder 의 next_fire_at·acked.
_LADDER_BRANCHES: tuple[tuple[str, str, bool], ...] = (
    ("scheduled", "재알림 예약", False),
    ("acked", "확인돼 멈춤", False),
    ("none", "재알림 예약 없음", False),
    _UNKNOWN,
)
# 피드백(편의): 응답 신호(feature_vector.py:48-55 SIGNAL_TARGET). 서버가 거절하면 refused 가 간 길이다.
_FEEDBACK_BRANCHES: tuple[tuple[str, str, bool], ...] = (
    ("accepted", "도움됐어요", False),
    ("expired", "무시·만료", False),
    ("dismissed", "그만해 → 30일 쉬기", False),
    ("refused", "반영되지 않음", True),
    _UNKNOWN,
)

# 후보 출처(ActionCandidate.origin, contracts.py:303 — 여러 줄이 같으면 '+'로 합친다,
# proposal_lines.py:290) → 한국어.
_ORIGIN_KO = {"rule": "규칙 줄", "line_a": "학습 줄", "line_b": "언어 모델 줄", "llm": "언어 모델 줄"}


def _branches(table: tuple[tuple[str, str, bool], ...], taken: str | None,
              values: dict[str, str | None] | None = None) -> list[dict[str, Any]]:
    """표의 갈림길 전부. ``taken`` 이 표에 없으면 unknown 이 간 길이다(정확히 하나)."""
    keys = {k for k, _, _ in table}
    hit = taken if taken in keys else "unknown"
    values = values or {}
    return [{"key": k, "label_ko": label, "value_ko": values.get(k), "taken": k == hit,
             "exit": is_exit} for k, label, is_exit in table]


def _origin_ko(origin: str) -> str:
    parts = [_ORIGIN_KO.get(part.split(":", 1)[0]) for part in origin.split("+") if part]
    if not parts or not all(parts):
        return "출처 기록 없음"
    return "+".join(x for x in dict.fromkeys(parts) if x)


def _json_probs(raw: Any) -> dict[str, dict[str, float]] | None:
    """judgments.model_probs(JSON 글) → 질문별 확률. 읽지 못하면 None."""
    if raw is None:
        return None
    try:
        data = json.loads(raw)
    except (TypeError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def _jev_top(probs: dict[str, dict[str, float]] | None, question: str) -> str | None:
    """Jev 답의 최댓값 선택지 id(없으면 None)."""
    answer = (probs or {}).get(question) or {}
    return max(answer, key=lambda k: answer[k]) if answer else None


def _jev_ko(probs: dict[str, dict[str, float]] | None, question: str) -> str | None:
    """Jev 답의 최댓값 한 줄(단계 또는 파급도). 모르는 선택지면 None — 문구에서 뺀다."""
    answer = (probs or {}).get(question) or {}
    if not answer:
        return None
    top = max(answer, key=lambda k: answer[k])
    try:
        label = (RiskTier.from_str(top) if question == "tier" else SeverityLevel.from_str(top)).label_ko
        pct = float(answer[top]) * 100
    except Exception:  # noqa: BLE001 — 표시용 글, 실패하면 그 조각만 뺀다
        return None
    return f"{'Jev 최댓값' if question == 'tier' else '파급도 최댓값'} {label} {pct:.0f}%"


def _ms(seconds: float | None) -> float | None:
    return None if seconds is None else round(seconds * 1000.0, 2)


def _node(nid: str, detail_ko: str, *, real_s: float | None = None, state: str = "done",
          model: str | None = None, branches: list[dict[str, Any]] | None = None,
          edge_ko: str | None = None) -> dict[str, Any]:
    return {"id": nid, "label_ko": _NODE_KO.get(nid, nid), "detail_ko": detail_ko,
            "real_ms": _ms(real_s), "model": model, "state": state,
            "branches": list(branches or []), "edge_ko": edge_ko}


def _silence_end(decision: ProactiveDecision | None, evaluation: EvaluationResult | None,
                 gate1: Gate1Result | None) -> str:
    """침묵이 끝난 노드: rules(후보 없음) · score(아무것도 안 하기) · gate1(능동 상한) · judge(판정 뒤 상한)."""
    if decision is None or decision.candidate is None:
        return "rules"
    if evaluation is not None and evaluation.selected_is_noop():
        return "score"
    if gate1 is not None and (gate1.blocked or gate1.floor_tier > PROACTIVE_TIER_CEILING):
        return "gate1"
    return "judge"


def _cheap_usage(res: ReasonResult) -> tuple[str | None, int | None, int | None]:
    return res.model, res.input_tokens, res.output_tokens


# ── 실행 관리(관제실 서버가 쓴다) ────────────────────────────────────────────


class DayRuns:
    """하루 시뮬레이션 실행 관리자. ``LiveRuns`` 위에 시뮬레이터 객체를 붙인다."""

    def __init__(
        self,
        runs: LiveRuns | None = None,
        *,
        secrets_path: Path = DEFAULT_SECRETS_PATH,
        kit_factory: Callable[[Secrets], CloudKit] = build_cloud_kit,
    ) -> None:
        self.runs = runs if runs is not None else LiveRuns()
        self._sims: dict[str, DaySim] = {}
        # cloud 모드에서만 읽는다(fake 실행은 비밀 파일을 열지 않는다). 시험은 임시 경로를 넣는다.
        self.secrets_path = secrets_path
        self.kit_factory = kit_factory

    def key_status(self) -> dict[str, Any]:
        """키 상태 — 이름과 있음/없음만(값은 절대 싣지 않는다)."""
        loaded = load_secrets(self.secrets_path)
        presence = loaded.presence(KNOWN_KEYS)
        return {
            "ok": True,
            "status": loaded.status,
            "reason_ko": loaded.reason_ko,
            "keys": [{"name": name, "present": present} for name, present in presence.items()],
            "present": sum(presence.values()),
            "total": len(presence),
        }

    def start(self, body: dict[str, Any]) -> dict[str, Any]:
        req = parse_day_request(body)
        sim_box: list[DaySim] = []

        def make_worker() -> Callable[[RunContext], None]:
            kit: CloudKit | None = None
            if req.models == "cloud":
                try:
                    kit = self.kit_factory(load_secrets(self.secrets_path))
                except (ModelConfigError, OSError) as exc:
                    raise LiveRequestError(
                        f"실제 모델 설정을 읽지 못했습니다({type(exc).__name__}).", 503,
                        field="models") from None
            sim = DaySim(seed=req.seed, days=req.days, persona=req.persona, pace_ms=req.pace_ms,
                         models=req.models, cloud=kit, trace=req.trace)
            sim_box.append(sim)

            def worker(ctx: RunContext) -> None:
                sim.house_id = ctx.run_id
                asyncio.run(
                    sim.run(
                        lambda stage, data: ctx.emit(stage, data),
                        lambda: ctx.stop_requested,
                        lambda summary: ctx.update_summary(**summary),
                    )
                )
                ctx.update_summary(**sim.summary())

            return worker

        meta: dict[str, Any] = {"seed": req.seed, "days": req.days, "persona": req.persona,
                                "pace_ms": req.pace_ms}
        if req.models != "fake":
            meta["models"] = req.models
        if req.trace:
            meta["trace"] = True
        run = self.runs.start(make_worker, meta=meta)
        sim = sim_box[0]
        self._sims[run.run_id] = sim
        live = set(self.runs.run_ids())
        for rid in [r for r in self._sims if r not in live]:
            del self._sims[rid]
        out: dict[str, Any] = {"ok": True, "run_id": run.run_id, "days": req.days,
                               "seed": req.seed, "persona": req.persona, "pace_ms": req.pace_ms}
        if req.models != "fake":
            out["models"] = req.models
        if req.trace:
            out["trace"] = True
        return out

    def get(self, run_id: str) -> LiveRun | None:
        return self.runs.get(run_id)

    def view(self, run_id: str) -> dict[str, Any] | None:
        run = self.runs.get(run_id)
        return None if run is None else run.view()

    def stop(self) -> dict[str, Any]:
        run = self.runs.stop()
        return {"ok": True, "run_id": run.run_id, "status": run.status}

    def feedback(self, body: dict[str, Any]) -> dict[str, Any]:
        unknown = sorted(set(body) - {"run", "proposal_id", "signal"})
        if unknown:
            raise LiveRequestError(f"모르는 항목입니다: {', '.join(unknown)}", field=unknown[0])
        run_id = str(body.get("run") or "")
        run = self.runs.get(run_id)
        sim = self._sims.get(run_id)
        if run is None or sim is None:
            raise LiveRequestError("없는 실행입니다.", 404, field="run")
        if run.finished:
            raise LiveRequestError("이미 끝난 실행입니다.", 409, field="run")
        sim.enqueue_feedback(str(body.get("proposal_id") or ""), str(body.get("signal") or ""))
        return {"ok": True, "queued": True}

    def join(self, timeout: float | None = None) -> None:
        self.runs.join(timeout)
