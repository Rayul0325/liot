"""라이브 그래프 단계 라이브러리(liot/live_graph.py) — 실제 모델·서버 없이 가짜 transport 로.

핵심 검증:
  - 운영 경로는 데몬 순서다: sense → classify → phase1 → gate → ladder → model → followup.
    1단계·게이트·사다리 사건이 나올 때까지 모델 요청은 0건이다.
  - 정상 값은 classify 에서 끝나고, unavailable 은 감시 불가 분기, 비안전 센서는 비안전 분기.
  - night 이면 게이트가 quiet_hours 로 판정한다(편의 제안은 침묵, 안전 알림은 우회).
  - 모델이 올리면 후속 알림, 낮추려 하면 injection_suspect 이고 최종값 = 하한.
  - 시험 경로: sit-gas 의 파급도 하한은 CRITICAL, 편의 건도 model 단계까지 간다.
  - 생성기: 같은 씨앗 = 같은 목록, 다른 씨앗 = 다른 목록, 특성 낱말 0, 운영 건은 등록 센서만.
"""

from __future__ import annotations

import json
import threading
from typing import Any

import pytest

from liot.app import LiotApp
from liot.core.clock import Clock
from liot.core.contracts import CandidateAction, ContextSnapshot, SensorReading
from liot.core.risk import RiskTier
from liot.core.severity import SeverityLevel
from liot.layers.l1_sensing.normalizer import normalize
from liot.layers.l3_judgment.decision_backend import DecisionBackend
from liot.layers.l3_judgment.llm_judge import FakeLLM, LLMResult
from liot.live_graph import (
    TRAIT_WORDS,
    CaseError,
    LiveEnv,
    build_one,
    gate_stage,
    generate,
    run_case,
    suite_cases,
)
from liot.registry import RuntimeRegistry

# 파급도 질문의 기준 문장에만 들어 있는 표지(행동 질문 기준 문장에는 없다).
_SEVERITY_MARK = "피해가 얼마나"
PROD_STAGES = ["sense", "classify", "phase1", "gate", "ladder", "model", "followup"]


def _lp(winner: str) -> dict[str, float]:
    """winner 글자가 압도적으로 높은 logprob 표."""
    return {letter: (-0.05 if letter == winner else -4.0 - i) for i, letter in enumerate("ABCD")}


class FakeTransport:
    """llama.cpp /tokenize·/completion 흉내(네트워크 없음). 받은 요청을 모두 기록한다."""

    def __init__(self, tier: str = "A", severity: str = "A") -> None:
        self.tier_lp = _lp(tier)
        self.severity_lp = _lp(severity)
        self.calls: list[str] = []

    def post(self, path: str, payload: dict[str, Any], *, timeout_s: float) -> dict[str, Any]:
        self.calls.append(path)
        if path == "/tokenize":
            return {"tokens": [2 if _SEVERITY_MARK in payload["content"] else 1]}
        lp = self.tier_lp if payload["prompt"] == [1] else self.severity_lp
        top = [{"token": t, "logprob": v} for t, v in lp.items()]
        return {"completion_probabilities": [{"token": "A", "top_logprobs": top}]}


def _backend(tier: str = "A", severity: str = "A") -> tuple[DecisionBackend, FakeTransport]:
    t = FakeTransport(tier, severity)
    return DecisionBackend("fake", t, timeout_s=1.0), t


def _prod(entity_id: str, value: Any, **kw: Any) -> dict[str, Any]:
    case: dict[str, Any] = {
        "path": "prod",
        "id": "t-prod",
        "entity_id": entity_id,
        "value": value,
        "time_bucket": "day",
        "at": "2026-10-19T13:00:00+00:00",
        "occupancy": "occupied",
        "vulnerable": False,
        "guardians": 0,
        "consent": False,
    }
    case.update(kw)
    return case


def _suite(cid: str) -> dict[str, Any]:
    return next(c for c in suite_cases() if c["id"] == cid)


def _by_stage(events: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    return {e["stage"]: e for e in events}


@pytest.fixture(scope="module")
def env() -> LiveEnv:
    return LiveEnv.build()


# ── 운영 경로 ────────────────────────────────────────────────────────────────


async def test_prod_gas_abnormal_runs_daemon_order_and_phase1_calls_no_model(env: LiveEnv) -> None:
    backend, transport = _backend("D", "D")
    seen_calls: dict[str, int] = {}

    def emit(ev: dict[str, Any]) -> None:
        seen_calls[ev["stage"]] = len(transport.calls)

    case = _prod("binary_sensor.kitchen_gas", True, guardians=1, consent=True)
    events = await run_case(case, backend, env=env, emit=emit)

    assert [e["stage"] for e in events] == PROD_STAGES
    assert all(e["case"] == "t-prod" and e["ok"] for e in events)
    by = _by_stage(events)
    assert by["classify"]["out"]["branch"] == "abnormal"
    assert by["phase1"]["out"]["model_calls"] == 0
    assert by["phase1"]["out"]["gate2_verdict"] == "pending"
    assert by["phase1"]["out"]["severity_floor"] == "critical"
    # 1단계·게이트·사다리까지 모델 요청 0건, 2단계에서 두 질문 × 2 요청.
    assert seen_calls["phase1"] == 0
    assert seen_calls["gate"] == 0
    assert seen_calls["ladder"] == 0
    assert seen_calls["model"] == 4
    assert by["gate"]["out"]["verdict"] == "IMMEDIATE"
    assert by["gate"]["out"]["bypass"] is True
    ladder = by["ladder"]["out"]
    assert ladder["guardian_paged"] is True
    assert ladder["guardian_at_s"] == 90.0
    assert ladder["guidance_119"] is True
    assert ladder["guidance_119_at_s"] == 390.0
    assert by["model"]["out"]["calls"] == 1
    # 운영 경로의 맥락 요약은 집계 템플릿이다(자유 문장 없음).
    assert by["phase1"]["out"]["context_summary"].startswith("[day] ")
    json.dumps(events)  # 사건은 JSON 값만 담는다


async def test_prod_without_consent_pages_no_guardian_but_keeps_119_guidance(env: LiveEnv) -> None:
    backend, _ = _backend()
    case = _prod("binary_sensor.kitchen_gas", True, guardians=2, consent=False)
    ladder = _by_stage(await run_case(case, backend, env=env))["ladder"]["out"]
    assert ladder["guardians_consented"] == 0
    assert ladder["guardian_paged"] is False
    assert ladder["guardian_at_s"] is None
    assert ladder["guidance_119_at_s"] == 390.0


async def test_prod_normal_value_ends_at_classify(env: LiveEnv) -> None:
    backend, transport = _backend()
    events = await run_case(_prod("binary_sensor.kitchen_gas", False), backend, env=env)
    assert [e["stage"] for e in events] == ["sense", "classify"]
    out = events[-1]["out"]
    assert out["branch"] == "normal"
    assert out["terminal"] is True
    assert transport.calls == []


async def test_prod_unavailable_takes_unmonitorable_branch(env: LiveEnv) -> None:
    backend, transport = _backend()
    events = await run_case(_prod("binary_sensor.kitchen_gas", "unavailable"), backend, env=env)
    assert [e["stage"] for e in events] == ["sense", "classify"]
    out = events[-1]["out"]
    assert out["branch"] == "unmonitorable"
    assert out["abnormal"] is False
    assert transport.calls == []


async def test_prod_non_safety_sensor_is_silent_observation(env: LiveEnv) -> None:
    backend, transport = _backend()
    events = await run_case(_prod("sensor.living_temperature", 23.5), backend, env=env)
    assert [e["stage"] for e in events] == ["sense", "classify"]
    out = events[-1]["out"]
    assert out["branch"] == "non_safety"
    assert out["category"] == "temperature"
    assert out["proposal"] is False
    assert transport.calls == []


async def test_prod_night_safety_alert_bypasses_quiet_hours(env: LiveEnv) -> None:
    backend, _ = _backend()
    case = _prod(
        "binary_sensor.bath_leak", True, time_bucket="night", at="2026-10-19T23:30:00+00:00"
    )
    gate = _by_stage(await run_case(case, backend, env=env))["gate"]["out"]
    assert gate["quiet_hours"] is True
    assert gate["verdict"] == "IMMEDIATE"
    assert gate["bypass"] is True


async def _comfort_proposal(env: LiveEnv, at: str) -> Any:
    """편의 제안 하나(커튼 열기 = DUSK). 데몬은 센서 읽기로 편의 제안을 만들지 않으므로
    관제실 실험실 경로(명시 action)로 만든다 — 게이트 단계만 따로 검사하기 위해서다."""
    app = LiotApp(
        clock=Clock(fixed=at), catalog=env.catalog, categories=env.categories,
        llm_backend=FakeLLM(),
    )
    reading = SensorReading(
        entity_id="cover.living_room", source="ha", raw_value="on",
        norm_value=normalize("on"), ts_utc=at,
    )
    proposal = await app.process_reading(reading, action="open")
    assert proposal is not None
    assert proposal.risk_tier == RiskTier.DUSK
    assert proposal.severity is None
    return proposal


async def test_night_comfort_proposal_is_judged_with_quiet_hours(env: LiveEnv) -> None:
    night = "2026-10-19T23:30:00+00:00"
    out = gate_stage(await _comfort_proposal(env, night), Clock(fixed=night))
    assert out["quiet_hours"] is True
    assert out["verdict"] == "SUPPRESS"
    assert out["bypass"] is False
    day = "2026-10-19T13:00:00+00:00"
    out_day = gate_stage(await _comfort_proposal(env, day), Clock(fixed=day))
    assert out_day["quiet_hours"] is False
    assert out_day["verdict"] == "IMMEDIATE"


async def test_prod_model_raise_produces_followup_but_never_pages_family(env: LiveEnv) -> None:
    # 누수 하한 WARNING. 모델이 CRITICAL 로 올려도 보호자·119 는 하한만 본다.
    backend, _ = _backend("D", "D")
    case = _prod("binary_sensor.bath_leak", True, guardians=2, consent=True)
    by = _by_stage(await run_case(case, backend, env=env))
    assert by["phase1"]["out"]["severity_floor"] == "warning"
    assert by["ladder"]["out"]["guardian_paged"] is False
    assert by["ladder"]["out"]["realert_interval_s"] == 600.0
    follow = by["followup"]["out"]
    assert follow["raised"] is True
    assert follow["final_severity"] == "critical"
    assert follow["followup_severity"] == "critical"
    assert follow["realert_interval_s"] == 60.0
    assert follow["guardian_paged"] is False


async def test_prod_model_lower_is_injection_suspect_and_final_equals_floor(env: LiveEnv) -> None:
    backend, _ = _backend("A", "A")
    by = _by_stage(await run_case(_prod("binary_sensor.kitchen_gas", True), backend, env=env))
    assert by["model"]["out"]["model_tier"] == "ambient"
    assert by["model"]["out"]["model_severity"] == "record"
    follow = by["followup"]["out"]
    assert follow["gate2_verdict"] == "injection_suspect"
    assert follow["final_tier"] == follow["floor_tier"] == "ember"
    assert follow["final_severity"] == follow["severity_floor"] == "critical"
    assert follow["raised"] is False


async def test_prod_vulnerable_resident_raises_floor_with_resident_source(env: LiveEnv) -> None:
    backend, _ = _backend()
    case = _prod("binary_sensor.bath_leak", True, vulnerable=True, guardians=1, consent=True)
    by = _by_stage(await run_case(case, backend, env=env))
    assert by["phase1"]["out"]["severity_floor"] == "critical"
    assert by["phase1"]["out"]["sources"] == ["table", "resident"]
    assert by["ladder"]["out"]["guardian_paged"] is True


class _RaisingBackend:
    model_id = "decision:boom"

    def judge_raw(
        self,
        candidate: CandidateAction,
        ctx: ContextSnapshot | None,
        gate1_floor: RiskTier,
        severity_floor: SeverityLevel | None = None,
    ) -> LLMResult:
        raise RuntimeError("boom")


class _SlowBackend:
    model_id = "decision:slow"
    timeout_s = 0.01  # 전체 마감 = 0.01 × 4 = 0.04초

    def __init__(self) -> None:
        self.release = threading.Event()

    def judge_raw(
        self,
        candidate: CandidateAction,
        ctx: ContextSnapshot | None,
        gate1_floor: RiskTier,
        severity_floor: SeverityLevel | None = None,
    ) -> LLMResult:
        self.release.wait(5.0)
        return LLMResult(tier=gate1_floor, confidence=1.0, rationale="late", model_id=self.model_id)


async def test_prod_phase2_exception_keeps_floor_as_degraded(env: LiveEnv) -> None:
    by = _by_stage(await run_case(_prod("binary_sensor.kitchen_gas", True), _RaisingBackend(),
                                  env=env))
    assert by["model"]["ok"] is False
    assert by["model"]["out"]["degraded"] is True
    assert "RuntimeError" in by["model"]["out"]["error"]
    assert by["followup"]["out"]["gate2_verdict"] == "degraded_fail_safe"
    assert by["followup"]["out"]["final_severity"] == "critical"
    assert by["followup"]["out"]["raised"] is False


async def test_prod_phase2_deadline_keeps_floor_as_degraded(env: LiveEnv) -> None:
    slow = _SlowBackend()
    try:
        by = _by_stage(await run_case(_prod("binary_sensor.kitchen_gas", True), slow, env=env))
    finally:
        slow.release.set()
    assert by["model"]["ok"] is False
    assert "제한시간" in by["model"]["out"]["error"]
    assert by["followup"]["out"]["gate2_verdict"] == "degraded_fail_safe"
    assert by["followup"]["out"]["raised"] is False


async def test_prod_transport_failure_is_degraded_not_silent(env: LiveEnv) -> None:
    class _Down:
        def post(self, path: str, payload: dict[str, Any], *, timeout_s: float) -> dict[str, Any]:
            return {}

    backend = DecisionBackend("down", _Down(), timeout_s=1.0)
    by = _by_stage(await run_case(_prod("binary_sensor.hall_smoke", True), backend, env=env))
    assert by["gate"]["out"]["verdict"] == "IMMEDIATE"
    assert by["model"]["ok"] is False
    assert by["followup"]["out"]["gate2_verdict"] == "degraded_fail_safe"
    assert by["followup"]["out"]["final_tier"] == "ember"


# ── 시험 경로 ────────────────────────────────────────────────────────────────


async def test_bench_sit_gas_has_critical_severity_floor(env: LiveEnv) -> None:
    backend, transport = _backend("D", "D")
    events = await run_case(_suite("sit-gas/calm"), backend, env=env)
    assert [e["stage"] for e in events] == ["floor", "model", "compose"]
    by = _by_stage(events)
    assert by["floor"]["out"]["severity_floor"] == "critical"
    assert by["floor"]["out"]["tier_floor"] == "ember"
    assert by["floor"]["out"]["questions"] == 2
    assert len(transport.calls) == 4
    compose = by["compose"]["out"]
    assert compose["final_severity"] == "critical"
    assert compose["gold"] == {"tier": "ember", "severity": "critical"}
    assert compose["match"] is True


async def test_bench_comfort_case_reaches_model_and_scores_against_gold(env: LiveEnv) -> None:
    backend, transport = _backend("A")
    events = await run_case(_suite("amb-light-dim"), backend, env=env)
    assert [e["stage"] for e in events] == ["floor", "model", "compose"]
    by = _by_stage(events)
    assert by["floor"]["out"]["severity_floor"] is None
    assert by["floor"]["out"]["questions"] == 1
    assert transport.calls == ["/tokenize", "/completion"]
    assert by["model"]["out"]["probs"]["tier"]["ambient"] > 0.9
    compose = by["compose"]["out"]
    assert compose["final_tier"] == "ambient"
    assert compose["match_tier"] is True
    assert compose["match"] is True


async def test_bench_mismatch_against_gold_is_marked(env: LiveEnv) -> None:
    backend, _ = _backend("D")  # amb-light-dim 정답 초안은 ambient
    compose = _by_stage(await run_case(_suite("amb-light-dim"), backend, env=env))["compose"]
    assert compose["out"]["final_tier"] == "ember"
    assert compose["out"]["match_tier"] is False
    assert compose["out"]["match"] is False


async def test_bench_generated_case_has_no_gold(env: LiveEnv) -> None:
    backend, _ = _backend()
    case = next(c for c in generate(7, 0, 5) if c["path"] == "bench")
    compose = _by_stage(await run_case(case, backend, env=env))["compose"]["out"]
    assert compose["gold"] is None
    assert compose["match"] is None


def test_build_one_rejects_trait_words(env: LiveEnv) -> None:
    spec = dict(_suite("amb-light-dim")["spec"])
    spec["context"] = "[evening] 독거 가구, 조도 낮음"
    with pytest.raises(CaseError, match="거주자 특성"):
        build_one(spec, env.guardrail, env.table)


def test_build_one_rejects_situation_on_non_safety_category(env: LiveEnv) -> None:
    spec = dict(_suite("amb-light-dim")["spec"])
    spec["situation"] = True
    with pytest.raises(CaseError, match="안전 카테고리가 아니다"):
        build_one(spec, env.guardrail, env.table)


async def test_unknown_path_is_rejected(env: LiveEnv) -> None:
    backend, _ = _backend()
    with pytest.raises(CaseError, match="모르는 경로"):
        await run_case({"path": "other", "id": "x"}, backend, env=env)


# ── 생성기 ───────────────────────────────────────────────────────────────────


def test_generator_is_deterministic_per_seed_and_differs_across_seeds() -> None:
    a = generate(7, 30, 10)
    assert a == generate(7, 30, 10)
    assert a != generate(8, 30, 10)
    assert len(a) == 40
    assert sum(c["path"] == "prod" for c in a) == 30
    assert sum(c["path"] == "bench" for c in a) == 10
    assert len({c["id"] for c in a}) == 40
    # 경로마다 난수열이 따로다 — 운영 건 수를 바꿔도 시험 건은 그대로다.
    bench_a = [c for c in a if c["path"] == "bench"]
    assert bench_a == [c for c in generate(7, 3, 10) if c["path"] == "bench"]


def test_generator_has_no_trait_words() -> None:
    cases = generate(7, 30, 40) + suite_cases()
    text = json.dumps(cases, ensure_ascii=False)
    assert [w for w in TRAIT_WORDS if w in text] == []


def test_generator_prod_cases_use_only_registered_sensors() -> None:
    registered = {m["entity_id"]: m for m in RuntimeRegistry().sensors_meta()}
    prod = [c for c in generate(11, 200, 0) if c["path"] == "prod"]
    assert {c["entity_id"] for c in prod} <= set(registered)
    for c in prod:
        assert c["time_bucket"] == Clock().time_bucket(c["at"])
        assert c["occupancy"] in ("occupied", "vacant")
        assert 0 <= c["guardians"] <= 2
        assert not (c["consent"] and c["guardians"] == 0)
        if registered[c["entity_id"]]["kind"] == "number":
            assert isinstance(c["value"], float)
        else:
            assert c["value"] in (True, False, "unavailable")
    # 섞임: 이상·정상·감시 불가·수치가 모두 나온다.
    values = {type(c["value"]).__name__ if isinstance(c["value"], float) else c["value"]
              for c in prod}
    assert values == {True, False, "unavailable", "float"}


async def test_generated_cases_all_run_to_a_terminal_stage(env: LiveEnv) -> None:
    backend, _ = _backend("C", "C")
    for case in generate(7, 30, 10):
        events = await run_case(case, backend, env=env)
        last = events[-1]
        assert {e["case"] for e in events} == {case["id"]}
        if case["path"] == "bench":
            assert last["stage"] == "compose"
        else:
            assert last["stage"] in ("classify", "followup")
            assert (last["stage"] == "classify") == last["out"].get("terminal", False)
