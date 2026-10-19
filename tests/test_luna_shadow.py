"""luna 그림자 비교기(liot.luna_shadow) — 가짜 전송만 쓴다(실제 키 호출 없음).

지키는 것:
  - decision_api·unavailable 방식은 요청을 만들지 않고 '미접근'.
  - logprobs 방식은 결정 백엔드와 같은 지시·4지선다를 Chat Completions 로 묻고
    (logprobs true, top_logprobs 20) 글자 확률을 softmax 한다. 파급도는 Jev 가 물었을 때만.
  - Jev 와의 일치 여부. 어떤 실패도 던지지 않고 '미접근'(재시도 없음). 키는 기록에 없다.
"""

from __future__ import annotations

import json
import math
from pathlib import Path
from typing import Any

import pytest

from liot.cheap_llm import CheapLLMError, ModelConfigError, ProviderRequest
from liot.layers.l3_judgment.decision_backend import (
    DECISION_SYSTEM,
    SEVERITY_QUESTION,
    TIER_QUESTION,
    option_prompt,
)
from liot.luna_shadow import (
    MODES,
    OPENAI_CHAT_URL,
    TOP_LOGPROBS,
    LunaConfig,
    LunaShadow,
    load_luna_config,
)

KEY = "fake" + "-luna-" + "key-0001"  # 실행 중에 만든다(비밀 스캔과 같은 이유)
EVIDENCE = json.dumps({"device": "DEVICE_1", "context": "[day] safety=leak"}, ensure_ascii=False)
JEV_TIER = {"ambient": 0.05, "breath": 0.1, "dusk": 0.15, "ember": 0.7}
JEV_SEV = {"record": 0.1, "notice": 0.1, "warning": 0.6, "critical": 0.2}


def _completion(logprobs: dict[str, float]) -> dict[str, Any]:
    top = [{"token": t, "logprob": lp, "bytes": list(t.encode())} for t, lp in logprobs.items()]
    return {"choices": [{"logprobs": {"content": [{"token": "A", "logprob": 0.0,
                                                   "top_logprobs": top}]}}],
            "usage": {"prompt_tokens": 120, "completion_tokens": 1}}


class FakeTransport:
    def __init__(self, replies: list[dict[str, Any]] | None = None, *, fail: bool = False) -> None:
        self.replies = list(replies or [])
        self.fail = fail
        self.requests: list[ProviderRequest] = []

    def post(self, request: ProviderRequest, *, timeout_s: float) -> dict[str, Any]:
        self.requests.append(request)
        if self.fail:
            raise CheapLLMError("HTTP 403")
        return self.replies.pop(0)


def _shadow(mode: str = "logprobs", replies: list[dict[str, Any]] | None = None,
            key: str = KEY, **kw: Any) -> tuple[LunaShadow, FakeTransport]:
    fake = FakeTransport(replies, **kw)
    cfg = LunaConfig(mode=mode, model="luna-x", timeout_s=1.0)  # type: ignore[arg-type]
    return LunaShadow(cfg, key, transport=fake), fake


@pytest.mark.parametrize("mode", ["unavailable", "decision_api"])
def test_non_logprob_modes_are_unavailable_without_a_request(mode: str) -> None:
    shadow, fake = _shadow(mode)
    res = shadow.compare(EVIDENCE, {"tier": JEV_TIER, "severity": JEV_SEV})
    assert res.status == "unavailable" and res.mode == mode
    assert res.reason_ko is not None and "미접근" in res.reason_ko
    assert res.probs == {} and res.agree == {}
    assert fake.requests == [] and shadow.calls == []


def test_decision_api_says_endpoint_unconfirmed() -> None:
    shadow, _ = _shadow("decision_api")
    reason = shadow.compare(EVIDENCE, {"tier": JEV_TIER}).reason_ko
    assert reason is not None and "0단계" in reason


def test_logprobs_request_shape() -> None:
    shadow, fake = _shadow(replies=[_completion({"D": -0.1, "C": -2.5})])
    shadow.compare(EVIDENCE, {"tier": JEV_TIER})
    req = fake.requests[0]
    assert req.url == OPENAI_CHAT_URL
    assert req.headers == {"authorization": f"Bearer {KEY}", "content-type": "application/json"}
    assert req.body == {
        "model": "luna-x",
        "messages": [{"role": "system", "content": DECISION_SYSTEM},
                     {"role": "user", "content": option_prompt(TIER_QUESTION, EVIDENCE)}],
        "logprobs": True,
        "top_logprobs": TOP_LOGPROBS,
        "max_completion_tokens": 1,
    }
    assert TOP_LOGPROBS == 20


def test_softmax_over_letters_and_agreement() -> None:
    shadow, fake = _shadow(replies=[_completion({"D": -0.2, "C": -1.9, "x": -0.05}),
                                    _completion({"B": -0.3, "C": -1.0})])
    res = shadow.compare(EVIDENCE, {"tier": JEV_TIER, "severity": JEV_SEV})
    assert res.status == "ok" and len(fake.requests) == 2
    assert fake.requests[1].body["messages"][1]["content"] == option_prompt(
        SEVERITY_QUESTION, EVIDENCE)
    # 본 logprob 최솟값(-1.9) - 2 = -3.9 를 못 본 글자(A, B)에 준다 — 결정 백엔드와 같은 규칙.
    z = [-3.9, -3.9, -1.9, -0.2]
    w = [math.exp(v) for v in z]
    expected = {oid: x / sum(w) for oid, x in zip(TIER_QUESTION.option_ids, w, strict=True)}
    assert res.probs["tier"] == pytest.approx(expected)
    assert res.choice == {"tier": "ember", "severity": "notice"}
    assert res.agree == {"tier": True, "severity": False}
    assert (res.input_tokens, res.output_tokens) == (240, 2)
    assert [c["question"] for c in shadow.calls] == ["tier", "severity"]
    assert all(c["ok"] for c in shadow.calls)


def test_severity_only_when_jev_asked_it() -> None:
    shadow, fake = _shadow(replies=[_completion({"A": -0.1})])
    res = shadow.compare(EVIDENCE, {"tier": JEV_TIER})
    assert len(fake.requests) == 1 and set(res.probs) == {"tier"}
    assert res.agree == {"tier": False}


def test_transport_failure_is_unavailable_once_no_retry() -> None:
    shadow, fake = _shadow(fail=True)
    res = shadow.compare(EVIDENCE, {"tier": JEV_TIER, "severity": JEV_SEV})
    assert res.status == "unavailable" and "HTTP 403" in (res.reason_ko or "")
    assert len(fake.requests) == 1
    assert KEY not in (res.reason_ko or "")


@pytest.mark.parametrize("reply", [
    {}, {"choices": []}, {"choices": [{"logprobs": None}]},
    _completion({"x": -0.1, "y": -0.2}),  # 보기 글자가 하나도 없다
])
def test_unreadable_reply_is_unavailable(reply: dict[str, Any]) -> None:
    shadow, fake = _shadow(replies=[reply])
    res = shadow.compare(EVIDENCE, {"tier": JEV_TIER})
    assert res.status == "unavailable" and len(fake.requests) == 1


def test_no_key_is_unavailable_without_a_request() -> None:
    shadow, fake = _shadow(key="")
    res = shadow.compare(EVIDENCE, {"tier": JEV_TIER})
    assert res.status == "unavailable" and "OPENAI_API_KEY" in (res.reason_ko or "")
    assert fake.requests == []


@pytest.mark.parametrize("evidence", ["보호자 010-1234-5678", "light.living_room 켜짐"])
def test_evidence_failing_egress_is_never_sent(evidence: str) -> None:
    shadow, fake = _shadow(replies=[_completion({"A": -0.1})])
    res = shadow.compare(evidence, {"tier": JEV_TIER})
    assert res.status == "unavailable" and fake.requests == []


def test_key_never_in_repr_or_records() -> None:
    shadow, fake = _shadow(replies=[_completion({"A": -0.1})])
    shadow.compare(EVIDENCE, {"tier": JEV_TIER})
    assert KEY not in repr(shadow) and KEY not in repr(fake.requests[0])
    assert KEY not in json.dumps(shadow.calls)


def test_shipped_config_loads() -> None:
    cfg = load_luna_config()
    assert cfg.mode in MODES and cfg.model and cfg.timeout_s > 0


@pytest.mark.parametrize("text", [
    "{}",
    "luna_shadow: {mode: realtime, model: m}",
    "luna_shadow: {mode: logprobs, model: ''}",
    "luna_shadow: {mode: logprobs, model: m, timeout_s: -1}",
    "luna_shadow: {mode: logprobs, model: m, timeout_s: .nan}",
])
def test_bad_config_is_rejected(tmp_path: Path, text: str) -> None:
    path = tmp_path / "models.yaml"
    path.write_text(text, encoding="utf-8")
    with pytest.raises(ModelConfigError):
        load_luna_config(path)
