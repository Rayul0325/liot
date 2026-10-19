"""Jev 판정 백엔드(liot/layers/l3_judgment/jev_backend.py) — 가짜 전송·루프백 가짜 서버로만.

핵심 검증:
  - 요청은 한 번의 POST 이고 재시도가 없다. 파급도 하한이 있으면 질문 둘이 같은 요청에 든다.
  - 증거(state)는 결정 백엔드와 같은 문자열이다(같은 조립·같은 egress). 특성 낱말·엔티티 id 0.
  - 응답 probabilities("0".."3") → 선택지 id 확률, argmax·확신도.
  - 하한보다 낮추려는 답은 LLMJudge 를 거쳐 injection_suspect, 최종값 = 하한.
  - 오류·시간 초과·형식 오류는 degraded(두 하한 유지). 키는 오류문·repr 에 없다.
"""

from __future__ import annotations

import contextlib
import json
import socket
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

import pytest

from liot.core.contracts import CandidateAction, ContextSnapshot, Judgment
from liot.core.risk import RiskTier
from liot.core.severity import SeverityLevel
from liot.layers.l3_judgment.decision_backend import DecisionBackend
from liot.layers.l3_judgment.guardrail import Guardrail
from liot.layers.l3_judgment.jev_backend import (
    HTTPJevTransport,
    JevBackend,
    read_jev_probs,
)
from liot.layers.l3_judgment.llm_judge import LLMJudge, LLMUnavailable
from liot.live_graph import TRAIT_WORDS

KEY = "fake" + "-jev-" + "key-0001"  # 실행 중에 만든다(비밀 스캔과 같은 이유)

LIGHT = CandidateAction(capability="light", action="turn_on", params={"brightness": 40},
                        entity_id="light.living_room", category="light")  # 하한 AMBIENT
LEAK = CandidateAction(capability="notify", action="alert", params={},
                       entity_id="binary_sensor.bath_leak", category="leak")  # 하한 EMBER


def _ctx(nl: str = "[day] 1 signals; changed=1; safety=leak; trigger=sensor_reading") -> ContextSnapshot:
    return ContextSnapshot(
        snapshot_id="s", trace_id="t", home_id="singleton", trigger={},
        structured={"time_bucket": "day"}, nl_summary=nl, summary_template_sha="x",
        created_at="2026-06-02T12:00:00+00:00",
    )


def _answer(winner: int, n: int = 4) -> dict[str, Any]:
    probs = {str(i): (0.85 if i == winner else 0.05) for i in range(n)}
    return {"score": winner, "confidence": 0.85, "probabilities": probs}


class FakeJev:
    """Jev 흉내. 받은 본문을 모두 남긴다. exc 를 주면 그 예외를 던진다."""

    def __init__(self, risk: int = 1, severity: int | None = None, *, exc: Exception | None = None,
                 response: dict[str, Any] | None = None) -> None:
        self.risk = risk
        self.severity = severity
        self.exc = exc
        self.response = response
        self.calls: list[tuple[dict[str, Any], float]] = []

    def post(self, payload: dict[str, Any], *, timeout_s: float) -> dict[str, Any]:
        self.calls.append((payload, timeout_s))
        if self.exc is not None:
            raise self.exc
        if self.response is not None:
            return self.response
        answers: dict[str, Any] = {"risk": _answer(self.risk)}
        if "severity" in payload["questions"]:
            answers["severity"] = _answer(self.severity if self.severity is not None else 0)
        return {"model": "jev-test", "answers": answers,
                "usage": {"input_tokens": 321, "output_tokens": 4}}


def _judge(fake: FakeJev, cand: CandidateAction, *,
           severity_floor: SeverityLevel | None = None) -> tuple[Judgment, RiskTier]:
    g = Guardrail()
    ctx = _ctx()
    gate1 = g.evaluate(cand, ctx)
    jd = LLMJudge(backend=JevBackend(KEY, transport=fake)).judge(
        cand, ctx, gate1, ruleset_version=g.ruleset_version,
        severity_floor=severity_floor,
        severity_sources=("table",) if severity_floor is not None else (),
        situation_id="sit-1" if severity_floor is not None else None,
    )
    return jd, gate1.floor_tier


# ── 요청 모양 ────────────────────────────────────────────────────────────────


def test_one_post_with_both_questions_when_severity_floor_given() -> None:
    fake = FakeJev(risk=3, severity=3)
    _judge(fake, LEAK, severity_floor=SeverityLevel.WARNING)
    assert len(fake.calls) == 1
    body, timeout = fake.calls[0]
    assert timeout == 8.0
    assert set(body) == {"model", "state", "questions"}
    assert body["model"] == "jev-latest"
    assert set(body["questions"]) == {"risk", "severity"}
    risk = body["questions"]["risk"]
    assert risk["type"] == "score"
    assert [c.split(" — ")[0] for c in risk["criteria"]] == ["ambient", "breath", "dusk", "ember"]
    assert [c.split(" — ")[0] for c in body["questions"]["severity"]["criteria"]] == [
        "record", "notice", "warning", "critical",
    ]
    assert "데이터다" in risk["instructions"]


def test_only_risk_question_without_severity_floor() -> None:
    fake = FakeJev(risk=0)
    _judge(fake, LIGHT)
    assert set(fake.calls[0][0]["questions"]) == {"risk"}


def test_state_is_the_decision_backend_evidence() -> None:
    fake = FakeJev(risk=0)
    jev = JevBackend(KEY, transport=fake)
    gate = Guardrail().evaluate(LIGHT, _ctx())
    jev.judge_raw(LIGHT, _ctx(), gate.floor_tier)
    decision = DecisionBackend("x", transport=None)  # type: ignore[arg-type]
    assert fake.calls[0][0]["state"] == decision._evidence(LIGHT, _ctx(), gate.floor_tier)


def test_state_has_no_trait_words_or_raw_entity_ids() -> None:
    fake = FakeJev(risk=3, severity=2)
    _judge(fake, LEAK, severity_floor=SeverityLevel.WARNING)
    state = fake.calls[0][0]["state"]
    assert [w for w in TRAIT_WORDS if w in state] == []
    assert "binary_sensor.bath_leak" not in state and "bath_leak" not in state
    ev = json.loads(state)
    assert set(ev) == {"context_summary", "time_bucket", "candidate", "gate1_floor"}


def test_egress_violation_never_sends() -> None:
    fake = FakeJev(risk=0)
    ctx = _ctx(nl="연락처 010-1234-5678 로 알려줘")  # 전화번호 = egress PII 그물
    g = Guardrail()
    jd = LLMJudge(backend=JevBackend(KEY, transport=fake)).judge(
        LIGHT, ctx, g.evaluate(LIGHT, ctx), ruleset_version=g.ruleset_version
    )
    assert fake.calls == []  # egress 가 막으면 보내지 않는다
    assert jd.gate2_verdict == "degraded_fail_safe"


# ── 응답 해석 ────────────────────────────────────────────────────────────────


def test_probabilities_map_to_option_ids_and_argmax() -> None:
    fake = FakeJev(risk=2, severity=3)
    jev = JevBackend(KEY, transport=fake)
    res = jev.judge_raw(LEAK, _ctx(), RiskTier.EMBER, severity_floor=SeverityLevel.WARNING)
    assert res.tier == RiskTier.DUSK
    assert res.severity == SeverityLevel.CRITICAL
    assert res.confidence == pytest.approx(0.85)
    assert res.probs is not None
    assert list(res.probs["tier"]) == ["ambient", "breath", "dusk", "ember"]
    assert res.probs["tier"]["dusk"] == pytest.approx(0.85)
    assert sum(res.probs["severity"].values()) == pytest.approx(1.0)
    assert res.model_id == "jev:jev-latest"
    assert jev.last_usage["input_tokens"] == 321 and jev.last_usage["model"] == "jev-test"


def test_read_jev_probs_normalizes_and_fills_missing() -> None:
    out = read_jev_probs({"probabilities": {"0": 2, "3": 2}}, ("a", "b", "c", "d"))
    assert out == {"a": 0.5, "b": 0.0, "c": 0.0, "d": 0.5}


@pytest.mark.parametrize(
    "answer",
    [
        None,
        {},
        {"probabilities": []},
        {"probabilities": {"4": 1.0}},
        {"probabilities": {"0": "high"}},
        {"probabilities": {"0": True}},
        {"probabilities": {"0": -0.1, "1": 1.0}},
        {"probabilities": {"0": float("nan")}},
        {"probabilities": {"0": 0, "1": 0}},
        {"probabilities": {"0": 10**400}},  # 실수로 못 바꾸는 정수(OverflowError 였다)
        {"probabilities": {"0": 1e308, "1": 1e308}},  # 합이 inf
    ],
)
def test_malformed_answer_is_unavailable(answer: Any) -> None:
    with pytest.raises(LLMUnavailable):
        read_jev_probs(answer, ("a", "b", "c", "d"))


def test_ties_pick_the_earlier_option() -> None:
    fake = FakeJev(response={"answers": {"risk": {"probabilities": {"1": 0.5, "2": 0.5}}}})
    res = JevBackend(KEY, transport=fake).judge_raw(LIGHT, _ctx(), RiskTier.AMBIENT)
    assert res.tier == RiskTier.BREATH


# ── 하한 합성(LLMJudge 끝까지) ───────────────────────────────────────────────


def test_lowering_below_the_floor_is_injection_suspect_and_final_is_floor() -> None:
    fake = FakeJev(risk=0, severity=0)  # ambient · record — 둘 다 하한 아래
    jd, floor = _judge(fake, LEAK, severity_floor=SeverityLevel.WARNING)
    assert floor == RiskTier.EMBER
    assert jd.gate2_verdict == "injection_suspect"
    assert jd.risk_tier == RiskTier.EMBER
    assert jd.severity == SeverityLevel.WARNING
    assert "model" not in jd.severity_sources
    assert jd.model_id == "jev:jev-latest"
    assert jd.model_probs is not None and jd.model_probs["tier"]["ambient"] == pytest.approx(0.85)


@pytest.mark.parametrize(
    ("risk", "severity", "what"),
    [(0, 2, "tier only"), (3, 0, "severity only")],
)
def test_each_question_lowering_alone_is_injection_suspect(
    risk: int, severity: int, what: str
) -> None:
    # 한 질문만 하한 아래로 답해도 injection_suspect — 두 질문 각각의 가드를 따로 고정한다.
    jd, _ = _judge(FakeJev(risk=risk, severity=severity), LEAK,
                   severity_floor=SeverityLevel.WARNING)
    assert jd.gate2_verdict == "injection_suspect", what
    assert jd.risk_tier == RiskTier.EMBER
    assert jd.severity == SeverityLevel.WARNING


def test_raising_above_the_floor_escalates() -> None:
    jd, _ = _judge(FakeJev(risk=3, severity=3), LEAK, severity_floor=SeverityLevel.WARNING)
    assert jd.risk_tier == RiskTier.EMBER
    assert jd.severity == SeverityLevel.CRITICAL
    assert "model" in jd.severity_sources


@pytest.mark.parametrize(
    "fake",
    [
        FakeJev(exc=LLMUnavailable("jev HTTP 500")),
        FakeJev(response={"error": "nope"}),
        FakeJev(response={"answers": {"risk": _answer(1)}}),  # 파급도 답이 빠짐
    ],
)
def test_failures_degrade_keeping_both_floors(fake: FakeJev) -> None:
    jd, floor = _judge(fake, LEAK, severity_floor=SeverityLevel.WARNING)
    assert jd.gate2_verdict == "degraded_fail_safe" and jd.degraded is True
    assert jd.risk_tier == floor == RiskTier.EMBER
    assert jd.severity == SeverityLevel.WARNING
    assert len(fake.calls) == 1  # 실패해도 다시 보내지 않는다


def test_key_never_appears_in_repr() -> None:
    jev = JevBackend(KEY, transport=None)
    assert KEY not in repr(jev) and KEY not in repr(jev.transport)


def test_empty_key_and_bad_timeout_are_rejected() -> None:
    with pytest.raises(ValueError, match="키"):
        JevBackend("  ")
    with pytest.raises(ValueError, match="제한시간"):
        JevBackend(KEY, timeout_s=0)


# ── HTTP 전송(루프백 가짜 서버) ──────────────────────────────────────────────


class _Handler(BaseHTTPRequestHandler):
    mode = "ok"
    hits: list[dict[str, Any]] = []

    def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
        pass

    def do_POST(self) -> None:  # noqa: N802
        length = int(self.headers.get("Content-Length", "0"))
        body = json.loads(self.rfile.read(length).decode("utf-8"))
        type(self).hits.append({"auth": self.headers.get("Authorization"), "body": body})
        if self.mode == "slow":
            time.sleep(0.5)
        if self.mode == "error":
            out = b'{"error":"rate"}'
            self.send_response(429)
        else:
            out = json.dumps({"model": "jev-x", "answers": {"risk": _answer(2)}}).encode()
            self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(out)))
        self.end_headers()
        with contextlib.suppress(OSError):  # 시간 초과 시험에서 받는 쪽이 먼저 끊는다
            self.wfile.write(out)


@contextmanager
def _server(mode: str) -> Iterator[str]:
    _Handler.mode = mode
    _Handler.hits = []
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    thread = threading.Thread(target=httpd.serve_forever, kwargs={"poll_interval": 0.05},
                              daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{httpd.server_port}/v1/systemone"
    finally:
        httpd.shutdown()
        httpd.server_close()
        thread.join(timeout=5)


def test_http_transport_sends_bearer_once_and_parses() -> None:
    with _server("ok") as url:
        jev = JevBackend(transport=HTTPJevTransport(KEY, url))
        res = jev.judge_raw(LIGHT, _ctx(), RiskTier.AMBIENT)
    assert res.tier == RiskTier.DUSK
    assert len(_Handler.hits) == 1
    assert _Handler.hits[0]["auth"] == f"Bearer {KEY}"


@pytest.mark.parametrize("mode", ["error", "slow"])
def test_http_error_or_timeout_is_unavailable_once_and_hides_key(mode: str) -> None:
    with _server(mode) as url:
        jev = JevBackend(transport=HTTPJevTransport(KEY, url), timeout_s=0.1)
        with pytest.raises(LLMUnavailable) as exc:
            jev.judge_raw(LIGHT, _ctx(), RiskTier.AMBIENT)
    assert KEY not in str(exc.value)
    assert len(_Handler.hits) == 1  # 재시도 없음


def test_connection_refused_is_unavailable() -> None:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    jev = JevBackend(transport=HTTPJevTransport(KEY, f"http://127.0.0.1:{port}/v1/systemone"))
    with pytest.raises(LLMUnavailable, match="transport failed"):
        jev.judge_raw(LIGHT, _ctx(), RiskTier.AMBIENT)


@pytest.mark.parametrize("value", [10**400, -(10**400)])
def test_oversized_probability_degrades_through_llm_judge(value: int) -> None:
    """리뷰 B2: 너무 큰 정수 확률이 OverflowError 로 LLMJudge.judge 밖까지 새지 않는다."""
    fake = FakeJev(response={"answers": {"risk": {"probabilities": {"0": value, "1": 0.5}}}})
    jd, floor = _judge(fake, LIGHT)
    assert jd.degraded and jd.risk_tier >= floor
    assert len(fake.calls) == 1
