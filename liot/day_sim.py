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
``proposal`` · ``digest`` · ``feedback`` · ``safety`` · ``day.end``(그날 지표). 실행 관리자
(live_runs)가 끝에 ``done`` 을 붙인다.

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
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from liot.app import LiotApp
from liot.core.clock import Clock
from liot.core.contracts import CandidateAction, ContextSnapshot, ExternalContext, SensorReading
from liot.core.risk import RiskTier
from liot.core.severity import SeverityLevel
from liot.db.database import init_db
from liot.layers.l1_sensing.normalizer import normalize
from liot.layers.l2_context.external_provider import FakeExternalProvider
from liot.layers.l2_context.proactive import ProactiveDecision, ProactiveGenerator
from liot.layers.l3_judgment.llm_judge import FakeLLM, LLMBackend, LLMResult
from liot.layers.l4_execution.escalation import GUIDANCE_119_TEXT
from liot.layers.l4_execution.notifier import sign_feedback
from liot.live_runs import LiveRequestError, LiveRun, LiveRuns, RunContext
from liot.registry import RuntimeRegistry
from liot.service import DispatchResult, LiotService

__all__ = [
    "HOME_TZ",
    "MAX_DAYS",
    "MAX_PACE_MS",
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

_ORIGIN_RULE = re.compile(r"proactive:rule:([^@]+)@")
_RUN_KEYS = frozenset({"days", "seed", "persona", "pace_ms"})
_SIGNALS = ("accepted", "dismissed")


# ── 요청 검사 ────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class DayRequest:
    days: int
    seed: int
    persona: str
    pace_ms: int


def _int(body: dict[str, Any], key: str, default: int, lo: int, hi: int) -> int:
    value = body.get(key, default)
    if isinstance(value, bool) or not isinstance(value, int):
        raise LiveRequestError(f"{key} 는 정수여야 합니다.", field=key)
    if not lo <= value <= hi:
        raise LiveRequestError(f"{key} 는 {lo}~{hi} 사이여야 합니다.", field=key)
    return value


def parse_day_request(body: dict[str, Any]) -> DayRequest:
    """``{days:1-7, seed:int, persona:"default", pace_ms?:0-500}``. 모르는 항목·값은 400."""
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
    return DayRequest(
        days=_int(body, "days", 3, 1, MAX_DAYS),
        seed=seed,
        persona=str(persona),
        pace_ms=_int(body, "pace_ms", 0, 0, MAX_PACE_MS),
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

    def __init__(self, log: list[tuple[str, Any]]) -> None:
        self.log = log
        self.phase = "idle"
        self.sent: list[tuple[str, str, dict[str, Any]]] = []

    def send(self, topic: str, sanitized: dict[str, Any]) -> None:
        self.sent.append((self.phase, topic, dict(sanitized)))
        self.log.append(("send", sanitized.get("proposal_id") or sanitized.get("label")))


class _RecordingJudge:
    """FakeLLM 을 감싸 모델 호출 순서를 남긴다(안전 2단계는 severity_floor 가 있다)."""

    def __init__(self, inner: LLMBackend, log: list[tuple[str, Any]]) -> None:
        self.inner = inner
        self.model_id = inner.model_id
        self.log = log

    def judge_raw(
        self,
        candidate: CandidateAction,
        ctx: ContextSnapshot | None,
        gate1_floor: RiskTier,
        severity_floor: SeverityLevel | None = None,
    ) -> LLMResult:
        self.log.append(("model", (candidate.entity_id, severity_floor is not None)))
        if severity_floor is None:
            return self.inner.judge_raw(candidate, ctx, gate1_floor)
        return self.inner.judge_raw(candidate, ctx, gate1_floor, severity_floor=severity_floor)


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
        "accepted": 0,
        "dismissed": 0,
        "expired": 0,
        "accept_rate": None,
        "safety_alerts": 0,
        "scenes": {},
    }


Emit = Callable[[str, dict[str, Any]], Any]


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

    def __post_init__(self) -> None:
        if self.persona not in PERSONAS:
            raise ValueError(f"unknown persona {self.persona!r}")
        self._viewer: queue.SimpleQueue[tuple[str, str]] = queue.SimpleQueue()
        self._rng = random.Random(f"{self.seed}:persona")
        self._tracked: dict[str, _Tracked] = {}
        self._by_pid: dict[str, str] = {}
        self._persona_due: list[tuple[str, str]] = []
        self._records: dict[str, dict[str, Any]] = {}
        self._open_safety: set[str] = set()  # 이상 값으로 상황이 열린 센서

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
        return {
            "seed": self.seed,
            "days": self.days,
            "persona": self.persona,
            "metrics": [dict(m) for m in self.metrics],
            "proposals": [dict(p) for p in self.proposals],
            "safety": [dict(s) for s in self.safety_events],
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
        app = LiotApp(
            clock=clock,
            catalog=catalog,
            categories=categories,
            llm_backend=_RecordingJudge(FakeLLM(), self.log),
            external_provider=weather,
            proactive=generator,
        )
        db = await init_db(str(self.tmp_dir / "day.db"))
        secret = secrets.token_bytes(32)
        svc = LiotService(app, db, clock=clock, transport=transport, topic="day-sim", secret=secret)

        async def out(stage: str, data: dict[str, Any]) -> None:
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
                await out(
                    "day.start",
                    {
                        "day": plan.day,
                        "date": plan.steps[0].at_local.date().isoformat(),
                        "kind": plan.kind,
                        "safety": plan.safety,
                    },
                )
                stopped = False
                for step in plan.steps:
                    if should_stop():
                        stopped = True
                        break
                    clock.set_fixed(step.at_local)
                    await self._step(svc, app, generator, weather, transport, step, last, out,
                                     secret)
                self._finish_day(metrics)
                await out("day.end", {"day": plan.day, "partial": stopped, "metrics": dict(metrics)})
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
        await self._apply_taps(svc, transport, step, out, secret)

        changed = {k: v for k, v in step.readings.items() if last.get(k) != v}
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
                await self._safety_reading(svc, step, eid, bool(value), res, out)

        transport.phase = "tick"
        app.last_trace = None
        generator.last_decision = None
        result = await svc.run_tick()
        silence = self._silence(app, generator, result)
        if result is not None:
            await self._record_proposal(app, step, result, out)

        transport.phase = "phase2"
        await svc.drain_phase2()
        transport.phase = "ladder"
        await svc.ladder_tick()
        await self._safety_sends(transport, step, out)
        transport.phase = "idle"
        await self._expiries(svc, step, out)

        if changed or weather_changed or silence or at.minute == 0:
            data: dict[str, Any] = {
                "day": step.day,
                "at": at.isoformat(),
                "hhmm": hhmm,
                "changed": dict(changed),
                "weather": dict(step.weather),
                "bucket": svc.clock.time_bucket(),
            }
            if silence:
                data["silence"] = silence
            await out("step", data)

    def _silence(
        self, app: LiotApp, generator: _RecordingGenerator, result: DispatchResult | None
    ) -> dict[str, Any] | None:
        """규칙이 성립했는데 제안이 나가지 않은 까닭(거절·쿨다운·무행동 선택·상한)."""
        d = generator.last_decision
        if result is not None or d is None or not d.matched_rule_ids:
            return None
        if d.candidate is None:
            return {"rules": list(d.matched_rule_ids), "reason": d.suppressed_by}
        trace = app.last_trace or {}
        reason = trace.get("silence_reason") or "evaluator_noop"
        return {"rules": list(d.matched_rule_ids), "reason": str(reason)}

    async def _record_proposal(
        self,
        app: LiotApp,
        step: StepPlan,
        result: DispatchResult,
        out: Callable[[str, dict[str, Any]], Awaitable[None]],
    ) -> None:
        p = result.proposal
        origin = str(p.action.params.get("__origin__", ""))
        m = _ORIGIN_RULE.search(origin)
        rule_id = m.group(1) if m else None
        scene = SCENES.get(rule_id or "")
        trace = app.last_trace or {}
        decision = trace.get("proactive")
        evaluation = trace.get("evaluation")
        score = None
        if evaluation is not None:
            score = evaluation.scored[evaluation.selected_index].final_score
        sim_id = f"p{len(self._tracked) + 1}"
        tracked = _Tracked(sim_id, p.proposal_id, step.day, "comfort", scene, rule_id,
                           result.decision, p.risk_tier)
        self._tracked[sim_id] = tracked
        self._by_pid[p.proposal_id] = sim_id
        destination = {"IMMEDIATE": "phone", "DIGEST": "tray"}.get(result.decision, "blocked")
        rec = {
            "id": sim_id,
            "day": step.day,
            "at": step.at_local.isoformat(),
            "hhmm": step.at_local.strftime("%H:%M"),
            "scene": scene,
            "rule_id": rule_id,
            "capability": p.action.capability,
            "action": p.action.action,
            "entity_id": p.action.entity_id,
            "params": {k: v for k, v in p.action.params.items() if not k.startswith("__")},
            "tier": p.risk_tier.name.lower(),
            "confirmation": p.required_confirmation,
            "decision": result.decision,
            "destination": destination,
            "sent": result.sent,
            "score": score,
            "reason_ko": getattr(decision, "reason_ko", "") or "",
            # 성립한 규칙 전부(선언 순). 승자 앞의 규칙이 빠졌다면 거절·쿨다운으로 건너뛴 것이다.
            "matched_rules": list(getattr(decision, "matched_rule_ids", ()) or ()),
            "expires_at": p.expires_at,
            "state": "pending",
        }
        self.proposals.append(rec)
        self._records[sim_id] = rec
        metrics = self.metrics[-1]
        metrics["proposals"] += 1
        if scene:
            metrics["scenes"][scene] = metrics["scenes"].get(scene, 0) + 1
        if result.decision == "IMMEDIATE":
            metrics["pushes"] += 1
            reaction = self._persona_reaction(scene)
            if reaction is not None:
                self._persona_due.append((sim_id, reaction))
        elif result.decision == "DIGEST":
            metrics["digests"] += 1
        else:
            metrics["suppressed"] += 1
        await out("proposal", dict(rec))
        if result.decision == "DIGEST":
            await out("digest", {"id": sim_id, "day": step.day, "scene": scene,
                                 "reason_ko": rec["reason_ko"]})

    def _persona_reaction(self, scene: str | None) -> str | None:
        table = PERSONAS[self.persona]
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
                                   "status": "404", "result": "없는 제안"})
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
        if code == "200" and tracked.kind == "comfort":
            new_state = "accepted" if signal == "accepted" else "dismissed"
            if tracked.state == "pending":
                tracked.state = new_state
                self._records[sim_id]["state"] = new_state
                day_m = self.metrics[tracked.day - 1]
                day_m["accepted" if new_state == "accepted" else "dismissed"] += 1
        await out(
            "feedback",
            {
                "id": sim_id,
                "day": step.day,
                "at": step.at_local.isoformat(),
                "signal": signal,
                "source": source,
                "status": code,
                "result": body,
                "scene": tracked.scene,
                "kind": tracked.kind,
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
            if state == "expired":
                t.state = "expired"
                self._records[t.sim_id]["state"] = "expired"
                self.metrics[t.day - 1]["expired"] += 1
                await out("feedback", {"id": t.sim_id, "day": step.day,
                                       "at": step.at_local.isoformat(), "signal": "expired",
                                       "source": "timeout", "status": "200",
                                       "result": "무시(만료)", "scene": t.scene,
                                       "kind": t.kind})

    # ── 안전 ───────────────────────────────────────────────────────────────

    async def _safety_reading(
        self,
        svc: LiotService,
        step: StepPlan,
        eid: str,
        abnormal: bool,
        res: DispatchResult | None,
        out: Callable[[str, dict[str, Any]], Awaitable[None]],
    ) -> None:
        category = "leak" if eid == LEAK else "gas"
        base = {"day": step.day, "at": step.at_local.isoformat(),
                "hhmm": step.at_local.strftime("%H:%M"), "category": category}
        if abnormal:
            self._open_safety.add(eid)
        if abnormal and res is not None:
            p = res.proposal
            sim_id = f"p{len(self._tracked) + 1}"
            self._tracked[sim_id] = _Tracked(sim_id, p.proposal_id, step.day, "safety", None,
                                             None, res.decision, p.risk_tier)
            self._by_pid[p.proposal_id] = sim_id
            self.metrics[-1]["safety_alerts"] += 1
            rec = {**base, "kind": "first_alert", "id": sim_id, "decision": res.decision,
                   "sent": res.sent, "tier": p.risk_tier.name.lower(),
                   "severity": None if p.severity is None else p.severity.name.lower()}
            self.safety_events.append(rec)
            await out("safety", rec)
            # 거주자는 다음 단계에 [괜찮아요](ok)로 확인한다 — 사다리가 멈춘다.
            self._persona_due.append((sim_id, "ok"))
        elif not abnormal and eid in self._open_safety:
            self._open_safety.discard(eid)
            rec = {**base, "kind": "normal"}
            self.safety_events.append(rec)
            await out("safety", rec)

    async def _safety_sends(
        self,
        transport: _SimTransport,
        step: StepPlan,
        out: Callable[[str, dict[str, Any]], Awaitable[None]],
    ) -> None:
        """이번 단계에 나간 안전 송신(재알림·해소·119 안내)을 사건으로 남긴다."""
        sent, transport.sent = transport.sent, []
        for phase, _topic, payload in sent:
            pid = payload.get("proposal_id")
            if pid in self._by_pid and self._tracked[self._by_pid[pid]].kind == "comfort":
                continue
            if "severity_label" not in payload and "situation_label" not in payload:
                continue
            label = str(payload.get("situation_label") or payload.get("label") or "")
            if phase == "reading" and pid in self._by_pid:
                continue  # 첫 알림은 _safety_reading 이 이미 남겼다
            if GUIDANCE_119_TEXT in label:
                kind = "guidance_119"
            elif phase == "reading":
                kind = "resolved"  # 정상 복귀 값이 보낸 '해소됨' 알림
            elif phase == "ladder":
                kind = "realert"
            else:
                kind = "notice"
            rec = {"day": step.day, "at": step.at_local.isoformat(),
                   "hhmm": step.at_local.strftime("%H:%M"), "kind": kind, "label": label,
                   "severity_label": payload.get("severity_label"), "phase": phase}
            self.safety_events.append(rec)
            await out("safety", rec)

    def _finish_day(self, metrics: dict[str, Any]) -> None:
        pushes = metrics["pushes"]
        metrics["accept_rate"] = None if pushes == 0 else round(metrics["accepted"] / pushes, 3)


# ── 실행 관리(관제실 서버가 쓴다) ────────────────────────────────────────────


class DayRuns:
    """하루 시뮬레이션 실행 관리자. ``LiveRuns`` 위에 시뮬레이터 객체를 붙인다."""

    def __init__(self, runs: LiveRuns | None = None) -> None:
        self.runs = runs if runs is not None else LiveRuns()
        self._sims: dict[str, DaySim] = {}

    def start(self, body: dict[str, Any]) -> dict[str, Any]:
        req = parse_day_request(body)
        sim = DaySim(seed=req.seed, days=req.days, persona=req.persona, pace_ms=req.pace_ms)

        def make_worker() -> Callable[[RunContext], None]:
            def worker(ctx: RunContext) -> None:
                asyncio.run(
                    sim.run(
                        lambda stage, data: ctx.emit(stage, data),
                        lambda: ctx.stop_requested,
                        lambda summary: ctx.update_summary(**summary),
                    )
                )
                ctx.update_summary(**sim.summary())

            return worker

        run = self.runs.start(make_worker, meta={"seed": req.seed, "days": req.days,
                                                 "persona": req.persona,
                                                 "pace_ms": req.pace_ms})
        self._sims[run.run_id] = sim
        live = set(self.runs.run_ids())
        for rid in [r for r in self._sims if r not in live]:
            del self._sims[rid]
        return {"ok": True, "run_id": run.run_id, "days": req.days, "seed": req.seed,
                "persona": req.persona, "pace_ms": req.pace_ms}

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
