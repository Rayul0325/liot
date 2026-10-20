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
    ExternalContext,
    SensorReading,
    is_noop,
)
from liot.core.risk import RiskTier
from liot.core.severity import SeverityLevel
from liot.day_cloud import CloudKit, build_cloud_kit
from liot.db.database import init_db
from liot.layers.l1_sensing.normalizer import normalize
from liot.layers.l2_context.external_provider import FakeExternalProvider
from liot.layers.l2_context.feedback_store import InMemoryFeedbackStore, candidate_key
from liot.layers.l2_context.proactive import (
    ProactiveDecision,
    ProactiveGenerator,
    validate_proactive_candidate,
)
from liot.layers.l2_context.proactive_rules import ProactiveRule
from liot.layers.l3_judgment.decision_backend import EvidenceBuilder
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
_RUN_KEYS = frozenset({"days", "seed", "persona", "pace_ms", "models"})
_SIGNALS = ("accepted", "dismissed")


# ── 요청 검사 ────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class DayRequest:
    days: int
    seed: int
    persona: str
    pace_ms: int
    models: str = "fake"


def _int(body: dict[str, Any], key: str, default: int, lo: int, hi: int) -> int:
    value = body.get(key, default)
    if isinstance(value, bool) or not isinstance(value, int):
        raise LiveRequestError(f"{key} 는 정수여야 합니다.", field=key)
    if not lo <= value <= hi:
        raise LiveRequestError(f"{key} 는 {lo}~{hi} 사이여야 합니다.", field=key)
    return value


def parse_day_request(body: dict[str, Any]) -> DayRequest:
    """``{days:1-7, seed:int, persona:"default", pace_ms?:0-500, models?:fake|cloud}``.

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
    return DayRequest(
        days=_int(body, "days", 3, 1, MAX_DAYS),
        seed=seed,
        persona=str(persona),
        pace_ms=_int(body, "pace_ms", 0, 0, MAX_PACE_MS),
        models=str(models),
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

    def send(self, topic: str, sanitized: dict[str, Any]) -> None:
        self.sent.append((self.phase, topic, dict(sanitized)))
        self.log.append(("send", sanitized.get("proposal_id") or sanitized.get("label")))


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
        shadow: list[tuple[str, dict[str, dict[str, float]], str, str | None]] | None = None,
    ) -> None:
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
        if severity_floor is None:
            return self.inner.judge_raw(candidate, ctx, gate1_floor)
        return self.inner.judge_raw(candidate, ctx, gate1_floor, severity_floor=severity_floor)

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
        with self._lock:
            t0 = time.perf_counter()
            try:
                if severity_floor is None:
                    result = cloud.judge_raw(candidate, ctx, gate1_floor)
                else:
                    result = cloud.judge_raw(candidate, ctx, gate1_floor,
                                             severity_floor=severity_floor)
            finally:
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
            self.shadow.append((evidence, dict(result.probs), result.model_id, served))
        return result


class _RecordingGenerator(ProactiveGenerator):
    """출하 규칙 생성기 그대로 + 마지막 결정을 남긴다(침묵 사유 관찰용)."""

    last_decision: ProactiveDecision | None = None

    def propose(
        self,
        cur: Any,
        *,
        external: ExternalContext | None = None,
        rejected_keys: frozenset[str] = frozenset(),
    ) -> ProactiveDecision:
        d = super().propose(cur, external=external, rejected_keys=rejected_keys)
        self.last_decision = d
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

    def __post_init__(self) -> None:
        if self.persona not in PERSONAS:
            raise ValueError(f"unknown persona {self.persona!r}")
        if self.models not in MODELS:
            raise ValueError(f"unknown models {self.models!r}")
        if self.models == "cloud" and self.cloud is None:
            raise ValueError("cloud 모드에는 CloudKit 이 필요합니다.")
        self._shadow_due: list[tuple[str, dict[str, dict[str, float]], str, str | None]] = []
        self._house: HouseMemory | None = None
        self._resident: ResidentMemory | None = None
        self._decided_by: dict[str, str] = {}
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
            out["luna"] = [dict(r) for r in self.luna_results]
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
        app = LiotApp(
            clock=clock,
            catalog=catalog,
            categories=categories,
            llm_backend=_RecordingJudge(
                FakeLLM(), self.log,
                cloud=None if self._kit is None else self._kit.judge,
                kit=self._kit,
                shadow=self._shadow_due if self._kit is not None and self._kit.luna else None,
            ),
            external_provider=weather,
            proactive=generator,
        )
        db = await init_db(str(self.tmp_dir / "day.db"))
        secret = secrets.token_bytes(32)
        svc = LiotService(app, db, clock=clock, transport=transport, topic=transport.topic,
                          secret=secret,
                          phase2_deadline_s=None if self._kit is None else CLOUD_PHASE2_DEADLINE_S)

        async def out(stage: str, data: dict[str, Any]) -> None:
            # 모든 사건에 지금 시각(가상)을 붙인다 — 화면의 시계는 이 값만 쓴다.
            data.setdefault("day", self._day)
            data.setdefault("clock_ko", self._clock_ko)
            emit(stage, data)
            if self.pace_ms:
                await asyncio.sleep(self.pace_ms / 1000.0)

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
                await self._safety_reading(svc, transport, step, eid, bool(value), res, out)

        if late:
            await self._apply_taps(svc, transport, step, out, secret)
            if hhmm == TRAY_REVIEW_AT:
                await self._tray_review(svc, step, out, secret)

        transport.phase = "tick"
        app.last_trace = None
        generator.last_decision = None
        result = await svc.run_tick()
        silence = self._silence(app, generator, result)
        # 같은 규칙이 같은 까닭으로 단계마다 건너뛰면 처음 한 번만 남긴다(타임라인이 넘치지 않게).
        key = None if silence is None else (tuple(silence["rules"]), silence["reason"])
        if key is not None and key == self._last_silence:
            silence = None
        self._last_silence = key if result is None else None
        if result is not None:
            await self._record_proposal(app, generator, step, result, out)

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
        status, body = await svc._route_feedback(
            f"GET /feedback?p={pid}&t={int(tracked.risk_tier)}&n={nonce}&sig={sig}&a={signal} "
            "HTTP/1.1"
        )
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
        (MemoryRefused)도 None 이다.
        """
        if not kit.meter.allow():
            return None
        self.log.append(("ext", kind))
        t0 = time.perf_counter()
        result: _T | None = None
        try:
            result = await asyncio.wait_for(asyncio.to_thread(fn), timeout=kit.call_timeout_s)
        except (TimeoutError, MemoryRefused):
            result = None
        finally:
            model, tokens_in, tokens_out = (None, None, None)
            if result is not None and usage is not None:
                model, tokens_in, tokens_out = usage(result)
            kit.meter.record(kind, model, seconds=round(time.perf_counter() - t0, 4),
                             input_tokens=tokens_in, output_tokens=tokens_out)
        return result

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

    def _memory_event(self, verb: str, who: str, title: str, *, rule: bool = False,
                      ok: bool = True) -> dict[str, Any]:
        rec = {"day": self._day, "clock_ko": self._clock_ko, "verb": verb, "who": who,
               "title_ko": title, "rule": rule, "ok": ok,
               "source_ko": "memgineering · liot-demo"}
        self.memory_events.append(rec)
        return rec

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
        await out("models.status", {"status": [dict(st) for st in kit.status],
                                    "line_ko": "실제 모델·기억으로 시작해요",
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
        res = await self._ext(kit, "reason", lambda: cheap.reason(facts, fallback=template),
                              usage=_cheap_usage)
        if res is None or res.source != "model":
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
        return None if choice == "ignore" else choice

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
        rec = self._memory_event("remember", "house", text, rule=rule, ok=op is not None)
        await out("memory", {**rec, "line_ko": ("규칙으로 기억했어요 — " if rule else "기억했어요 — ")
                             + text})

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

        for evidence, probs, model_id, served in due:
            res: ShadowResult | None
            if luna.cfg.mode == "logprobs":
                def compare(ev: str = evidence,
                            pr: dict[str, dict[str, float]] = probs) -> ShadowResult:
                    return luna.compare(ev, pr, allow=gate, on_request=spent)

                try:
                    res = await asyncio.wait_for(asyncio.to_thread(compare),
                                                 timeout=2 * kit.call_timeout_s)
                except TimeoutError:
                    res = None
            else:
                res = luna.compare(evidence, probs)  # 부르지 않는 방식 — 외부 호출 없음
            rec: dict[str, Any] = {
                "day": step.day, "hhmm": step.at_local.strftime("%H:%M"), "judge": model_id,
                "judge_model": served,
                "jev": probs, "status": "unavailable" if res is None else res.status,
                "luna": {} if res is None else res.probs,
                "agree": {} if res is None else res.agree,
                "reason_ko": ("luna 미접근 — 시간 초과" if res is None else res.reason_ko),
            }
            self.luna_results.append(rec)


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
                         models=req.models, cloud=kit)
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
