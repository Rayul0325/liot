"""결정 모델(llama.cpp) 백엔드 + Gate2 파급도 합성 검증 — 실제 모델·서버 없이.

핵심 검증:
  - 행동 확인 강도(tier): 하한보다 낮은 답 → 무시 + injection_suspect, 높은 답 → escalated.
  - 파급도(severity): 최종 = escalate_severity(severity_floor, model). 실제로 올렸을 때만
    'model' 출처가 붙고, 낮은 답은 무시돼 하한 그대로(injection_suspect 로 기록).
  - 파급도 질문은 severity_floor 가 주어졌을 때만 묻는다.
  - 판독: 선택지 글자만 softmax, top-k 밖 글자는 min(seen)-2, 글자가 하나도 없으면 degraded.
  - 서버 오류·연결 거부·타임아웃 → degraded(두 하한 모두 유지).
  - 같은 입력 → 같은 Judgment(judgment_id 제외), 요청은 temperature 0.
  - 거주자 특성은 요청 본문에 절대 들어가지 않는다.
"""

from __future__ import annotations

import json
import math
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
from liot.core.severity import SeverityLevel, SeveritySource
from liot.layers.l3_judgment.decision_backend import (
    DecisionBackend,
    HTTPDecisionTransport,
    build_decision_backend,
    read_option_probs,
)
from liot.layers.l3_judgment.guardrail import Guardrail
from liot.layers.l3_judgment.llm_judge import (
    FakeLLM,
    LLMJudge,
    LLMUnavailable,
    build_llm_backend,
)
from liot.layers.l3_judgment.ollama_backend import OllamaBackend

# 파급도 질문의 기준 문장에만 들어 있는 표지(행동 질문 기준 문장에는 없다).
_SEVERITY_MARK = "피해가 얼마나"

LIGHT = CandidateAction(capability="light", action="turn_on", params={},
                        entity_id="light.living_room", category="light")  # 하한 BREATH
GAS = CandidateAction(capability="valve", action="open", params={},
                      entity_id="switch.gas_valve", category="gas")  # 하한 EMBER(안전)


def _ctx(structured: dict[str, Any] | None = None, nl_summary: str = "") -> ContextSnapshot:
    return ContextSnapshot(
        snapshot_id="s", trace_id="t", home_id="singleton", trigger={},
        structured=structured if structured is not None else {"time_bucket": "night"},
        nl_summary=nl_summary, summary_template_sha="x",
        created_at="2026-06-02T22:00:00+00:00",
    )


def _lp(winner: str, others: str = "ABCD") -> dict[str, float]:
    """winner 글자가 압도적으로 높은 logprob 표."""
    return {letter: (-0.05 if letter == winner else -4.0 - i) for i, letter in enumerate(others)}


def _completion(logprobs: dict[str, float]) -> dict[str, Any]:
    top = [{"token": t, "logprob": v} for t, v in logprobs.items()]
    best = max(logprobs, key=lambda t: logprobs[t]) if logprobs else ""
    return {"completion_probabilities": [{"token": best, "top_logprobs": top}],
            "tokens_evaluated": 10}


class FakeTransport:
    """llama.cpp /tokenize·/completion 흉내(네트워크 없음). 받은 요청을 모두 기록한다."""

    def __init__(self, tier_lp: dict[str, float] | None = None,
                 severity_lp: dict[str, float] | None = None,
                 exc: Exception | None = None) -> None:
        self.tier_lp = tier_lp if tier_lp is not None else _lp("A")
        self.severity_lp = severity_lp if severity_lp is not None else _lp("A")
        self.exc = exc
        self.calls: list[tuple[str, dict[str, Any], float]] = []

    def post(self, path: str, payload: dict[str, Any], *, timeout_s: float) -> dict[str, Any]:
        self.calls.append((path, payload, timeout_s))
        if self.exc is not None:
            raise self.exc
        if path == "/tokenize":
            return {"tokens": [2 if _SEVERITY_MARK in payload["content"] else 1]}
        if path == "/completion":
            return _completion(self.tier_lp if payload["prompt"] == [1] else self.severity_lp)
        raise AssertionError(f"unexpected path {path}")

    def paths(self, path: str) -> list[dict[str, Any]]:
        return [p for (pp, p, _) in self.calls if pp == path]


def _judge(
    backend: Any,
    candidate: CandidateAction = LIGHT,
    *,
    ctx: ContextSnapshot | None = None,
    severity_floor: SeverityLevel | None = None,
    severity_sources: tuple[SeveritySource, ...] = (),
    situation_id: str | None = None,
) -> tuple[Judgment, RiskTier]:
    g = Guardrail()
    c = ctx if ctx is not None else _ctx()
    gate1 = g.evaluate(candidate, c)
    jd = LLMJudge(backend=backend).judge(
        candidate, c, gate1, ruleset_version=g.ruleset_version,
        severity_floor=severity_floor, severity_sources=severity_sources,
        situation_id=situation_id,
    )
    return jd, gate1.floor_tier


# ── 행동 확인 강도(tier) ─────────────────────────────────────────────────
def test_tier_lower_answer_is_ignored_as_injection_suspect() -> None:
    backend = DecisionBackend("jevk5-test", FakeTransport(tier_lp=_lp("A")))
    jd, floor = _judge(backend, GAS)
    assert floor is RiskTier.EMBER
    assert jd.risk_tier is RiskTier.EMBER
    assert jd.gate2_verdict == "injection_suspect"
    assert jd.confidence == 0.0
    assert jd.degraded is False


def test_tier_raise_is_escalated() -> None:
    backend = DecisionBackend("jevk5-test", FakeTransport(tier_lp=_lp("C")))
    jd, floor = _judge(backend, LIGHT)
    assert floor is RiskTier.BREATH
    assert jd.risk_tier is RiskTier.DUSK
    assert jd.gate2_verdict == "escalated"
    assert jd.confidence > 0.9


def test_tier_equal_is_concur_and_rationale_names_decision_readout() -> None:
    backend = DecisionBackend("jevk5-test", FakeTransport(tier_lp=_lp("B")))
    jd, _ = _judge(backend, LIGHT)
    assert jd.gate2_verdict == "concur"
    assert "결정 모델 판독" in jd.rationale
    assert "breath" in jd.rationale
    assert jd.model_id == "decision:jevk5-test"


# ── 파급도(severity) 합성 ────────────────────────────────────────────────
def test_severity_raise_adds_model_source_and_final_is_higher() -> None:
    backend = DecisionBackend("m", FakeTransport(tier_lp=_lp("B"), severity_lp=_lp("D")))
    jd, _ = _judge(backend, LIGHT, severity_floor=SeverityLevel.NOTICE,
                   severity_sources=("table", "resident"), situation_id="sit-1")
    assert jd.severity is SeverityLevel.CRITICAL
    assert jd.severity_floor is SeverityLevel.NOTICE
    assert jd.severity_sources == ["table", "resident", "model"]
    assert jd.situation_id == "sit-1"
    assert jd.gate2_verdict == "concur"  # tier 는 하한과 같다


def test_severity_lower_answer_is_ignored_and_final_equals_floor() -> None:
    backend = DecisionBackend("m", FakeTransport(tier_lp=_lp("B"), severity_lp=_lp("A")))
    jd, _ = _judge(backend, LIGHT, severity_floor=SeverityLevel.WARNING,
                   severity_sources=("table",))
    assert jd.severity is SeverityLevel.WARNING
    assert jd.severity_floor is SeverityLevel.WARNING
    assert jd.severity_sources == ["table"]
    # 하한을 무르려는 답은 tier 의 낮은 답과 같은 방식으로 기록된다.
    assert jd.gate2_verdict == "injection_suspect"
    assert jd.confidence == 0.0
    assert "파급도" in jd.rationale


def test_severity_equal_to_floor_adds_no_model_source() -> None:
    backend = DecisionBackend("m", FakeTransport(tier_lp=_lp("B"), severity_lp=_lp("C")))
    jd, _ = _judge(backend, LIGHT, severity_floor=SeverityLevel.WARNING,
                   severity_sources=("table",))
    assert jd.severity is SeverityLevel.WARNING
    assert jd.severity_sources == ["table"]
    assert jd.gate2_verdict == "concur"


def test_severity_question_asked_only_when_floor_given() -> None:
    t_none = FakeTransport(tier_lp=_lp("B"), severity_lp=_lp("D"))
    jd_none, _ = _judge(DecisionBackend("m", t_none), LIGHT)
    assert len(t_none.paths("/tokenize")) == 1
    assert all(_SEVERITY_MARK not in p["content"] for p in t_none.paths("/tokenize"))
    assert jd_none.severity is None and jd_none.severity_floor is None
    assert jd_none.severity_sources == []
    assert jd_none.model_probs is not None and set(jd_none.model_probs) == {"tier"}

    t_floor = FakeTransport(tier_lp=_lp("B"), severity_lp=_lp("D"))
    jd_floor, _ = _judge(DecisionBackend("m", t_floor), LIGHT,
                         severity_floor=SeverityLevel.RECORD, severity_sources=("table",))
    assert len(t_floor.paths("/tokenize")) == 2
    assert any(_SEVERITY_MARK in p["content"] for p in t_floor.paths("/tokenize"))
    assert jd_floor.model_probs is not None
    assert set(jd_floor.model_probs) == {"tier", "severity"}


def test_model_probs_populated_and_normalized() -> None:
    backend = DecisionBackend("m", FakeTransport(tier_lp=_lp("C"), severity_lp=_lp("B")))
    jd, _ = _judge(backend, LIGHT, severity_floor=SeverityLevel.RECORD,
                   severity_sources=("table",))
    assert jd.model_probs is not None
    tier_p = jd.model_probs["tier"]
    sev_p = jd.model_probs["severity"]
    assert list(tier_p) == ["ambient", "breath", "dusk", "ember"]
    assert list(sev_p) == ["record", "notice", "warning", "critical"]
    assert math.isclose(sum(tier_p.values()), 1.0, rel_tol=1e-9)
    assert math.isclose(sum(sev_p.values()), 1.0, rel_tol=1e-9)
    assert max(tier_p, key=lambda k: tier_p[k]) == "dusk"
    assert max(sev_p, key=lambda k: sev_p[k]) == "notice"
    assert jd.severity is SeverityLevel.NOTICE
    assert jd.severity_sources == ["table", "model"]


def test_fake_and_ollama_backends_never_raise_severity() -> None:
    """severity=None 인 기존 백엔드 → 파급도 = 하한, 출처 보존('model' 없음)."""
    jd, _ = _judge(FakeLLM(), LIGHT, severity_floor=SeverityLevel.NOTICE,
                   severity_sources=("table", "resident"))
    assert jd.severity is SeverityLevel.NOTICE
    assert jd.severity_sources == ["table", "resident"]
    assert jd.model_probs is None

    class _Spy:
        def __init__(self) -> None:
            self.prompts: list[str] = []

        def generate(self, model: str, prompt: str, *, timeout_s: float) -> str:
            self.prompts.append(prompt)
            return '{"tier":"breath","confidence":0.8,"rationale":"r"}'

    jd2, _ = _judge(OllamaBackend("g", _Spy()), LIGHT, severity_floor=SeverityLevel.WARNING,
                    severity_sources=("table",))
    assert jd2.severity is SeverityLevel.WARNING
    assert jd2.severity_sources == ["table"]
    assert jd2.degraded is False


def test_unknown_severity_source_is_rejected() -> None:
    with pytest.raises(ValueError):
        _judge(FakeLLM(), LIGHT, severity_floor=SeverityLevel.NOTICE,
               severity_sources=("sensor",))  # type: ignore[arg-type]


# ── 판독(readout) ────────────────────────────────────────────────────────
def test_readout_missing_letter_gets_floor_logprob() -> None:
    top = [{"token": "A", "logprob": -0.5}, {"token": "B", "logprob": -1.0},
           {"token": "X", "logprob": -3.0}]
    probs = read_option_probs(top, ("a", "b", "c", "d"))
    floor = -3.0 - 2.0
    z = [-0.5, -1.0, floor, floor]
    w = [math.exp(v - max(z)) for v in z]
    expected = [x / sum(w) for x in w]
    assert list(probs) == ["a", "b", "c", "d"]
    for got, want in zip(probs.values(), expected, strict=True):
        assert math.isclose(got, want, rel_tol=1e-12)


def test_readout_ignores_non_letter_tokens_and_non_finite() -> None:
    top = [{"token": "A", "logprob": -1.0}, {"token": " B", "logprob": -0.1},
           {"token": "C", "logprob": float("-inf")}, {"token": "D", "logprob": -2.0}]
    probs = read_option_probs(top, ("a", "b", "c", "d"))
    # ' B'(공백 포함)는 글자 B 가 아니다. C 는 -inf 라 본 적 없는 것으로 친다.
    assert probs["a"] > probs["d"] > 0
    assert math.isclose(probs["b"], probs["c"])


def test_readout_no_letters_raises_unavailable() -> None:
    with pytest.raises(LLMUnavailable):
        read_option_probs([{"token": "X", "logprob": -0.1}], ("a", "b", "c", "d"))
    with pytest.raises(LLMUnavailable):
        read_option_probs([], ("a", "b", "c", "d"))
    with pytest.raises(LLMUnavailable):
        read_option_probs("not-a-list", ("a", "b", "c", "d"))


def test_all_letters_missing_degrades_keeping_both_floors() -> None:
    t = FakeTransport(tier_lp={"X": -0.1, "Y": -2.0}, severity_lp={"X": -0.1})
    jd, floor = _judge(DecisionBackend("m", t), GAS, severity_floor=SeverityLevel.WARNING,
                       severity_sources=("table", "resident"), situation_id="gas-leak")
    assert jd.degraded is True
    assert jd.gate2_verdict == "degraded_fail_safe"
    assert jd.risk_tier is RiskTier.EMBER and floor is RiskTier.EMBER
    assert jd.severity is SeverityLevel.WARNING
    assert jd.severity_floor is SeverityLevel.WARNING
    assert jd.severity_sources == ["table", "resident"]
    assert jd.situation_id == "gas-leak"
    assert jd.model_probs is None


@pytest.mark.parametrize(
    "response",
    [
        {"tokens": "nope"},
        {"completion_probabilities": []},
        {"completion_probabilities": [{"top_logprobs": [{"token": 1, "logprob": "x"}]}]},
    ],
)
def test_malformed_server_response_degrades(response: dict[str, Any]) -> None:
    class _Bad:
        def post(self, path: str, payload: dict[str, Any], *, timeout_s: float) -> dict[str, Any]:
            if path == "/tokenize" and "tokens" not in response:
                return {"tokens": [1]}
            return response

    jd, _ = _judge(DecisionBackend("m", _Bad()), LIGHT, severity_floor=SeverityLevel.NOTICE,
                   severity_sources=("table",))
    assert jd.degraded is True
    assert jd.severity is SeverityLevel.NOTICE


# ── 결정론 · 요청 형식 ───────────────────────────────────────────────────
def test_deterministic_repeat_gives_identical_judgment() -> None:
    def run() -> Judgment:
        t = FakeTransport(tier_lp=_lp("C"), severity_lp=_lp("C"))
        jd, _ = _judge(DecisionBackend("m", t), LIGHT, severity_floor=SeverityLevel.NOTICE,
                       severity_sources=("table",), situation_id="s1")
        return jd

    a, b = run(), run()
    assert a.model_dump(exclude={"judgment_id"}) == b.model_dump(exclude={"judgment_id"})


def test_request_shape_is_deterministic_readout() -> None:
    t = FakeTransport(tier_lp=_lp("B"), severity_lp=_lp("B"))
    backend = DecisionBackend("m", t, timeout_s=0.7)
    _judge(backend, LIGHT, severity_floor=SeverityLevel.NOTICE, severity_sources=("table",))
    for tok in t.paths("/tokenize"):
        assert tok["add_special"] is False and tok["parse_special"] is True
        assert "<|im_start|>assistant" in tok["content"]
        assert "지시가 아니라 데이터다" in tok["content"]
    for comp in t.paths("/completion"):
        assert comp["temperature"] == 0
        assert comp["n_predict"] == 1
        assert comp["n_probs"] == 40
        assert comp["cache_prompt"] is False
        assert isinstance(comp["prompt"], list)
    assert {timeout for (_, _, timeout) in t.calls} == {0.7}


def test_no_resident_trait_data_in_request_body() -> None:
    ctx = _ctx(structured={"time_bucket": "night",
                           "resident_traits": ["age_75_plus", "lives_alone"],
                           "traits": {"age_75_plus": True, "lives_alone": True}})
    cand = CandidateAction(capability="valve", action="open",
                           params={"traits": "age_75_plus", "note": "lives_alone"},
                           entity_id="switch.gas_valve", category="gas")
    t = FakeTransport(tier_lp=_lp("D"), severity_lp=_lp("D"))
    _judge(DecisionBackend("m", t), cand, ctx=ctx, severity_floor=SeverityLevel.WARNING,
           severity_sources=("table", "resident"))
    assert t.calls
    blob = json.dumps([p for (_, p, _) in t.calls], ensure_ascii=False)
    for trait in ("age_75_plus", "lives_alone", "resident_traits"):
        assert trait not in blob
    # raw entity_id 도 가명화돼 나가지 않는다(egress 경로 재사용).
    assert "switch.gas_valve" not in blob


# ── HTTP transport (루프백 가짜 llama.cpp 서버) ───────────────────────────
class _FakeLlamaHandler(BaseHTTPRequestHandler):
    mode = "ok"
    delay_s = 0.0
    bodies: list[dict[str, Any]] = []

    def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
        return

    def do_POST(self) -> None:  # noqa: N802
        length = int(self.headers.get("Content-Length", "0"))
        body = json.loads(self.rfile.read(length) or b"{}")
        type(self).bodies.append({"path": self.path, **body})
        if self.delay_s:
            time.sleep(self.delay_s)
        if self.mode == "500":
            self.send_response(500)
            self.end_headers()
            self.wfile.write(b"boom")
            return
        if self.mode == "garbage":
            out = b"not json"
        elif self.path == "/tokenize":
            q = 2 if _SEVERITY_MARK in body.get("content", "") else 1
            out = json.dumps({"tokens": [q]}).encode()
        else:
            lp = _lp("C") if body.get("prompt") == [1] else _lp("D")
            out = json.dumps(_completion(lp)).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(out)))
        self.end_headers()
        self.wfile.write(out)


@contextmanager
def _llama_server(mode: str = "ok", delay_s: float = 0.0) -> Iterator[str]:
    handler = type("H", (_FakeLlamaHandler,), {"mode": mode, "delay_s": delay_s, "bodies": []})
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=httpd.serve_forever, kwargs={"poll_interval": 0.05},
                              daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{httpd.server_port}"
    finally:
        httpd.shutdown()
        httpd.server_close()
        thread.join(timeout=5)


def test_http_transport_end_to_end_with_fake_llama_server() -> None:
    with _llama_server() as url:
        backend = DecisionBackend("m", HTTPDecisionTransport(url), timeout_s=2.0)
        jd, _ = _judge(backend, LIGHT, severity_floor=SeverityLevel.NOTICE,
                       severity_sources=("table",))
    assert jd.degraded is False
    assert jd.risk_tier is RiskTier.DUSK
    assert jd.severity is SeverityLevel.CRITICAL
    assert jd.severity_sources == ["table", "model"]


def test_timeout_degrades_keeping_both_floors() -> None:
    with _llama_server(delay_s=0.6) as url:
        backend = DecisionBackend("m", HTTPDecisionTransport(url), timeout_s=0.15)
        jd, floor = _judge(backend, GAS, severity_floor=SeverityLevel.CRITICAL,
                           severity_sources=("table",))
    assert jd.degraded is True
    assert jd.gate2_verdict == "degraded_fail_safe"
    assert jd.risk_tier is RiskTier.EMBER and floor is RiskTier.EMBER
    assert jd.severity is SeverityLevel.CRITICAL
    assert jd.severity_sources == ["table"]


@pytest.mark.parametrize("mode", ["500", "garbage"])
def test_server_error_or_garbage_raises_unavailable(mode: str) -> None:
    with _llama_server(mode=mode) as url, pytest.raises(LLMUnavailable):
        HTTPDecisionTransport(url).post("/tokenize", {"content": "x"}, timeout_s=2.0)


def test_connection_refused_raises_unavailable() -> None:
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()  # 아무도 듣지 않는 포트
    with pytest.raises(LLMUnavailable):
        HTTPDecisionTransport(f"http://127.0.0.1:{port}").post(
            "/tokenize", {"content": "x"}, timeout_s=1.0)


# ── 환경 변수로 백엔드 선택 ──────────────────────────────────────────────
def test_env_selects_decision_backend(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LIOT_LLM_BACKEND", "decision")
    monkeypatch.setenv("LIOT_DECISION_URL", "http://127.0.0.1:19999/")
    monkeypatch.setenv("LIOT_DECISION_MODEL", "jevk5-9b-q8")
    monkeypatch.setenv("LIOT_DECISION_TIMEOUT", "2.5")
    backend = build_llm_backend()
    assert isinstance(backend, DecisionBackend)
    assert backend.model_id == "decision:jevk5-9b-q8"
    assert backend.timeout_s == 2.5
    assert isinstance(backend.transport, HTTPDecisionTransport)
    assert backend.transport.base_url == "http://127.0.0.1:19999"


def test_decision_backend_defaults(monkeypatch: pytest.MonkeyPatch) -> None:
    for k in ("LIOT_DECISION_URL", "LIOT_DECISION_MODEL", "LIOT_DECISION_TIMEOUT"):
        monkeypatch.delenv(k, raising=False)
    backend = build_decision_backend()
    assert backend.timeout_s == 1.5
    assert isinstance(backend.transport, HTTPDecisionTransport)
    assert backend.transport.base_url == "http://127.0.0.1:18765"


@pytest.mark.parametrize(
    ("value", "kind"),
    [("", FakeLLM), ("fake", FakeLLM), ("ollama", OllamaBackend), ("gemma", OllamaBackend),
     ("decision", DecisionBackend), ("DECISION", DecisionBackend)],
)
def test_env_selection_keeps_existing_meanings(
    monkeypatch: pytest.MonkeyPatch, value: str, kind: type
) -> None:
    monkeypatch.setenv("LIOT_LLM_BACKEND", value)
    monkeypatch.delenv("LIOT_OLLAMA_TRANSPORT", raising=False)
    assert isinstance(build_llm_backend(), kind)


def test_egress_violation_never_sends_and_degrades() -> None:
    """PII 가 정제 후에도 남으면 보내지 않고 degraded(두 하한 유지)."""
    t = FakeTransport(tier_lp=_lp("A"))
    jd, _ = _judge(DecisionBackend("m", t), GAS, ctx=_ctx(nl_summary="보호자 010-1234-5678 호출"),
                   severity_floor=SeverityLevel.CRITICAL, severity_sources=("table",))
    assert t.calls == []
    assert jd.degraded is True
    assert jd.risk_tier is RiskTier.EMBER
    assert jd.severity is SeverityLevel.CRITICAL


@pytest.mark.parametrize("value", ["0", "-1", "nan", "abc"])
def test_invalid_decision_timeout_is_rejected(monkeypatch: pytest.MonkeyPatch, value: str) -> None:
    monkeypatch.setenv("LIOT_DECISION_TIMEOUT", value)
    with pytest.raises(ValueError):
        build_decision_backend()


@pytest.mark.parametrize("logprob", [10**400, -(10**400)])
def test_oversized_logprob_is_unavailable_not_overflow(logprob: int) -> None:
    """리뷰 B2: 실수로 못 바꾸는 큰 정수 logprob 은 OverflowError 가 아니라 판독 불가다."""
    with pytest.raises(LLMUnavailable):
        read_option_probs([{"token": "A", "logprob": logprob}], ("a", "b", "c", "d"))
