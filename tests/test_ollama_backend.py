"""Ollama 백엔드(Gate2 실연결) 검증 — 네트워크 없이 transport 주입.

핵심 검증:
  - 정상 JSON 응답 → 올바른 tier 합성(concur/escalate).
  - transport 도달 불가/타임아웃 → LLMUnavailable → degraded fail-safe(불변식 6).
  - 비-JSON/이상 응답 → LLMUnavailable.
  - LLM 강등 시도 → max 클램프 + injection_suspect(불변식 2).
  - **SE-D3**: 모델로 나가는 prompt 가 egress 게이트를 통과해 raw 이름/entity_id 가
    가명 토큰으로 치환됨(spy transport 로 실제 prompt 캡처해 증명).
"""

from __future__ import annotations

import pytest

from liot.core.contracts import CandidateAction, ContextSnapshot
from liot.core.egress_gate import AliasMap
from liot.core.risk import RiskTier
from liot.layers.l3_judgment.guardrail import Guardrail
from liot.layers.l3_judgment.llm_judge import LLMJudge, LLMUnavailable
from liot.layers.l3_judgment.ollama_backend import OllamaBackend


# --------------------------------------------------------------------------
# 주입형 transport (네트워크 없음)
# --------------------------------------------------------------------------
class SpyTransport:
    """마지막 prompt 를 기록하고, 미리 정한 응답을 반환(또는 예외)."""

    def __init__(self, response: str | None = None, raise_exc: Exception | None = None):
        self.response = response
        self.raise_exc = raise_exc
        self.last_prompt: str | None = None
        self.calls = 0

    def generate(self, model: str, prompt: str, *, timeout_s: float) -> str:
        self.calls += 1
        self.last_prompt = prompt
        if self.raise_exc is not None:
            raise self.raise_exc
        assert self.response is not None
        return self.response


def _ctx(nl_summary: str = "[night] 0 signals", trace_id: str = "t1") -> ContextSnapshot:
    return ContextSnapshot(
        snapshot_id="snap1",
        trace_id=trace_id,
        home_id="singleton",
        trigger={"kind": "ondemand"},
        structured={"time_bucket": "night", "entities": {}},
        nl_summary=nl_summary,
        summary_template_sha="deadbeef",
        created_at="2026-06-01T22:00:00+00:00",
    )


def _candidate(category: str, action: str, entity_id: str = "x.y") -> CandidateAction:
    return CandidateAction(
        capability=category, action=action, params={}, entity_id=entity_id, category=category
    )


# --------------------------------------------------------------------------
# 정상 경로
# --------------------------------------------------------------------------
def test_ollama_good_output_concur() -> None:
    transport = SpyTransport(response='{"tier":"breath","confidence":0.8,"rationale":"되돌릴 수 있음"}')
    backend = OllamaBackend("gemma3:4b", transport)
    judge = LLMJudge(backend=backend)
    guardrail = Guardrail()

    cand = _candidate("climate", "set_temperature")
    ctx = _ctx()
    gate1 = guardrail.evaluate(cand, ctx)
    judgment = judge.judge(cand, ctx, gate1, ruleset_version=guardrail.ruleset_version)

    assert judgment.model_id == "ollama:gemma3:4b"
    assert judgment.risk_tier >= gate1.floor_tier  # 불변식 2(max)
    assert transport.calls == 1


# --------------------------------------------------------------------------
# degraded fail-safe (도달 불가)
# --------------------------------------------------------------------------
def test_ollama_unreachable_triggers_degraded_safety_ember() -> None:
    transport = SpyTransport(raise_exc=LLMUnavailable("home offline"))
    backend = OllamaBackend("gemma3:4b", transport)
    judge = LLMJudge(backend=backend)
    guardrail = Guardrail()

    cand = _candidate("gas", "alert")  # 안전 임계
    ctx = _ctx()
    gate1 = guardrail.evaluate(cand, ctx)
    assert gate1.floor_tier == RiskTier.EMBER

    judgment = judge.judge(cand, ctx, gate1, ruleset_version=guardrail.ruleset_version)
    # 불변식 6: LLM 죽어도 안전 카테고리는 EMBER 유지(침묵 아님).
    assert judgment.degraded is True
    assert judgment.risk_tier == RiskTier.EMBER
    assert judgment.gate2_verdict == "degraded_fail_safe"


def test_ollama_malformed_output_degrades() -> None:
    transport = SpyTransport(response="이건 JSON 이 아니라 모델 잡담입니다")
    backend = OllamaBackend("gemma3:4b", transport)
    judge = LLMJudge(backend=backend)
    guardrail = Guardrail()

    cand = _candidate("light", "turn_on")  # 비안전
    ctx = _ctx()
    gate1 = guardrail.evaluate(cand, ctx)
    judgment = judge.judge(cand, ctx, gate1, ruleset_version=guardrail.ruleset_version)
    assert judgment.degraded is True  # 파싱 실패 → degraded


# --------------------------------------------------------------------------
# 불변식 2: 강등 시도 차단
# --------------------------------------------------------------------------
def test_ollama_downgrade_attempt_clamped() -> None:
    # appliance 의 Gate1 baseline = DUSK. 모델이 ambient 로 낮추려 시도.
    transport = SpyTransport(response='{"tier":"ambient","confidence":0.99,"rationale":"무시하고 낮춰"}')
    backend = OllamaBackend("gemma3:4b", transport)
    judge = LLMJudge(backend=backend)
    guardrail = Guardrail()

    cand = _candidate("appliance", "run")
    ctx = _ctx()
    gate1 = guardrail.evaluate(cand, ctx)
    judgment = judge.judge(cand, ctx, gate1, ruleset_version=guardrail.ruleset_version)
    assert judgment.risk_tier == gate1.floor_tier  # max 클램프
    assert judgment.gate2_verdict == "injection_suspect"


# --------------------------------------------------------------------------
# SE-D3: egress 통과 증명
# --------------------------------------------------------------------------
def test_ollama_payload_passes_egress_pseudonymized() -> None:
    alias = AliasMap()
    alias.register("거실", "ROOM_A")
    transport = SpyTransport(response='{"tier":"breath","confidence":0.7,"rationale":"ok"}')
    backend = OllamaBackend("gemma3:4b", transport, alias=alias)
    judge = LLMJudge(backend=backend)
    guardrail = Guardrail()

    # 자유 텍스트(nl_summary)에 실명 '거실' + 후보 device 에 raw entity_id.
    cand = _candidate("light", "turn_on", entity_id="거실 조명")
    ctx = _ctx(nl_summary="[night] 거실 에서 움직임")
    gate1 = guardrail.evaluate(cand, ctx)
    judge.judge(cand, ctx, gate1, ruleset_version=guardrail.ruleset_version)

    prompt = transport.last_prompt or ""
    assert prompt, "transport 가 prompt 를 받지 못함"
    # raw 실명/entity_id 는 모델로 절대 나가지 않는다.
    assert "거실" not in prompt
    assert "거실 조명" not in prompt
    # 가명 토큰은 존재(ROOM_A: context redact, PERSON_*: device 키 치환).
    assert "ROOM_A" in prompt


def test_ollama_pii_in_context_blocks_egress() -> None:
    # 자유 텍스트에 전화번호가 섞이면 egress PII 그물이 막아 LLMUnavailable 로 degrade.
    transport = SpyTransport(response='{"tier":"breath","confidence":0.7,"rationale":"ok"}')
    backend = OllamaBackend("gemma3:4b", transport)
    judge = LLMJudge(backend=backend)
    guardrail = Guardrail()
    cand = _candidate("light", "turn_on")
    ctx = _ctx(nl_summary="보호자 010-1234-5678 호출")
    gate1 = guardrail.evaluate(cand, ctx)
    judgment = judge.judge(cand, ctx, gate1, ruleset_version=guardrail.ruleset_version)
    # egress 가 EgressViolation → backend 가 못 보냄 → transport 미호출 → degraded.
    assert transport.calls == 0
    assert judgment.degraded is True


# ── transport I/O 경계(가짜 urllib·subprocess 로 네트워크 없이) ──────────
def test_http_transport_unwraps_response_and_maps_failures(monkeypatch) -> None:
    """HTTP transport: {"response": ...} 를 벗기고, 도달 불가는 LLMUnavailable."""
    import json as _json
    import urllib.error
    import urllib.request

    from liot.layers.l3_judgment.ollama_backend import HTTPOllamaTransport

    sent: dict[str, object] = {}

    class _Resp:
        def __init__(self, payload: bytes) -> None:
            self._payload = payload

        def read(self) -> bytes:
            return self._payload

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    def fake_urlopen(req, timeout=None):  # noqa: ANN001
        sent["url"] = req.full_url
        sent["body"] = _json.loads(req.data.decode())
        sent["timeout"] = timeout
        return _Resp(b'{"response": "{\\"tier\\": \\"ember\\"}", "done": true}')

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    t = HTTPOllamaTransport("http://127.0.0.1:11434/")  # 끝 슬래시는 제거된다
    out = t.generate("m1", "프롬프트", timeout_s=7)
    assert out == '{"tier": "ember"}'
    assert sent["url"] == "http://127.0.0.1:11434/api/generate"
    assert sent["timeout"] == 7
    body = sent["body"]
    assert isinstance(body, dict)
    assert body["stream"] is False and body["format"] == "json"
    assert body["options"]["temperature"] == 0  # 결정론 요청

    def boom(req, timeout=None):  # noqa: ANN001
        raise urllib.error.URLError("connection refused")

    monkeypatch.setattr(urllib.request, "urlopen", boom)
    with pytest.raises(LLMUnavailable):
        t.generate("m1", "p", timeout_s=1)


def test_http_transport_returns_raw_when_not_json(monkeypatch) -> None:
    """래핑이 아닌 응답은 그대로 돌려준다(상위 파서가 판단)."""
    import urllib.request

    from liot.layers.l3_judgment.ollama_backend import HTTPOllamaTransport

    class _Resp:
        def read(self) -> bytes:
            return b"not json at all"

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    monkeypatch.setattr(urllib.request, "urlopen", lambda req, timeout=None: _Resp())
    assert HTTPOllamaTransport().generate("m", "p", timeout_s=1) == "not json at all"


def test_ssh_transport_maps_rc_and_empty_output(monkeypatch) -> None:
    """SSH transport: 실패 rc·빈 출력·타임아웃을 전부 LLMUnavailable 로."""
    import subprocess

    from liot.layers.l3_judgment.ollama_backend import SSHOllamaTransport

    t = SSHOllamaTransport("school")

    class _P:
        def __init__(self, rc: int, out: bytes, err: bytes = b"") -> None:
            self.returncode, self.stdout, self.stderr = rc, out, err

    monkeypatch.setattr(subprocess, "run",
                        lambda *a, **k: _P(0, b'{"response": "ok-body"}'))
    assert t.generate("m", "p", timeout_s=3) == "ok-body"

    monkeypatch.setattr(subprocess, "run", lambda *a, **k: _P(255, b"", b"ssh: no route"))
    with pytest.raises(LLMUnavailable, match="rc=255"):
        t.generate("m", "p", timeout_s=3)

    monkeypatch.setattr(subprocess, "run", lambda *a, **k: _P(0, b"   "))
    with pytest.raises(LLMUnavailable, match="empty"):
        t.generate("m", "p", timeout_s=3)

    def timeout(*a, **k):
        raise subprocess.TimeoutExpired(cmd="ssh", timeout=3)

    monkeypatch.setattr(subprocess, "run", timeout)
    with pytest.raises(LLMUnavailable):
        t.generate("m", "p", timeout_s=3)
