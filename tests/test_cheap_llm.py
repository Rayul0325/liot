"""싼 모델 클라이언트(liot.cheap_llm) — 가짜 전송·루프백 서버만 쓴다(실제 키 호출 없음).

지키는 것:
  - 세 회사 요청 형태(헤더·URL·본문)와 응답 판독(글·토큰).
  - 정리 규칙(엔티티 모양 → 한글 이름, 경로·파일명 제거, 실수 1자리, name 키 제거).
  - egress 를 통과한 입력만 나간다. 거부·특성 낱말이면 보내지 않고 대체한다.
  - 출력 검증(한 줄·80자·한글·특성 낱말 0) 실패는 대체 표시. 재시도 없음. 키는 어디에도 없다.
"""

from __future__ import annotations

import contextlib
import json
import socket
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import pytest

from liot import cheap_llm
from liot.cheap_llm import (
    ANTHROPIC_URL,
    ANTHROPIC_VERSION,
    OPENAI_RESPONSES_URL,
    PROVIDERS,
    CheapLLM,
    CheapLLMError,
    CheapModelConfig,
    HTTPCheapTransport,
    ModelConfigError,
    ProviderRequest,
    build_request,
    load_cheap_config,
    parse_reply,
    sanitize_for_llm,
    validate_reason,
)
from liot.core.egress_gate import Destination, emit_external
from liot.live_graph import TRAIT_WORDS

KEY = "fake" + "-cheap-" + "key-0001"  # 실행 중에 만든다(비밀 스캔과 같은 이유)
FALLBACK = "미세먼지가 나빠 공기청정기를 켜면 좋아요."
GOOD = "미세먼지가 42㎍/㎥라 공기청정기를 세게 돌리면 숨쉬기 편해요."

FACTS: dict[str, Any] = {
    "title": "공기청정기 세게",
    "target": "fan.living_purifier",
    "pm25": 42.0,
    "outdoor_temp_c": 13.456,
    "hour": 9,
}


def _cfg(provider: str = "anthropic") -> CheapModelConfig:
    return CheapModelConfig(provider=provider, model=f"{provider}-model-x",  # type: ignore[arg-type]
                            timeout_s=1.0, max_output_tokens=64)


def _reply(provider: str, text: str, tokens_in: int = 31, tokens_out: int = 17) -> dict[str, Any]:
    """회사별 실제 응답 모양(필요한 필드만)."""
    if provider == "anthropic":
        return {"content": [{"type": "text", "text": text}],
                "usage": {"input_tokens": tokens_in, "output_tokens": tokens_out}}
    if provider == "gemini":
        return {"candidates": [{"content": {"role": "model", "parts": [
                    {"text": "생각 중", "thought": True}, {"text": text}]}}],
                "usageMetadata": {"promptTokenCount": tokens_in,
                                  "candidatesTokenCount": tokens_out}}
    return {"output": [{"type": "reasoning", "summary": []},
                       {"type": "message", "content": [{"type": "output_text", "text": text}]}],
            "usage": {"input_tokens": tokens_in, "output_tokens": tokens_out}}


class FakeTransport:
    def __init__(self, reply: dict[str, Any] | None = None, *, fail: bool = False) -> None:
        self.reply = reply or {}
        self.fail = fail
        self.requests: list[ProviderRequest] = []

    def post(self, request: ProviderRequest, *, timeout_s: float) -> dict[str, Any]:
        self.requests.append(request)
        if self.fail:
            raise CheapLLMError("HTTP 503")
        return self.reply


def _client(provider: str = "anthropic", text: str = GOOD, **kw: Any) -> tuple[CheapLLM, FakeTransport]:
    fake = FakeTransport(_reply(provider, text), **kw)
    return CheapLLM(_cfg(provider), KEY, transport=fake), fake


# ── 요청·응답 형태 ──────────────────────────────────────────────────────────


def test_anthropic_request_shape() -> None:
    req = build_request(_cfg("anthropic"), KEY, "SYS", "USER")
    assert req.url == ANTHROPIC_URL
    assert req.headers == {"x-api-key": KEY, "anthropic-version": ANTHROPIC_VERSION,
                           "content-type": "application/json"}
    assert req.body == {"model": "anthropic-model-x", "max_tokens": 64, "system": "SYS",
                        "messages": [{"role": "user", "content": "USER"}]}


def test_gemini_request_shape_key_in_header_not_url() -> None:
    req = build_request(_cfg("gemini"), KEY, "SYS", "USER")
    assert req.url == ("https://generativelanguage.googleapis.com/v1beta/models/"
                       "gemini-model-x:generateContent")
    assert KEY not in req.url and "key=" not in req.url
    assert req.headers == {"x-goog-api-key": KEY, "content-type": "application/json"}
    assert req.body == {"systemInstruction": {"parts": [{"text": "SYS"}]},
                        "contents": [{"role": "user", "parts": [{"text": "USER"}]}],
                        "generationConfig": {"maxOutputTokens": 64}}


def test_openai_responses_request_shape() -> None:
    req = build_request(_cfg("openai"), KEY, "SYS", "USER")
    assert req.url == OPENAI_RESPONSES_URL
    assert req.headers == {"authorization": f"Bearer {KEY}", "content-type": "application/json"}
    assert req.body == {"model": "openai-model-x", "instructions": "SYS", "input": "USER",
                        "max_output_tokens": 64}


@pytest.mark.parametrize("provider", PROVIDERS)
def test_reply_is_parsed_and_usage_recorded(provider: str) -> None:
    llm, fake = _client(provider)
    res = llm.reason(FACTS, fallback=FALLBACK)
    assert (res.source, res.text, res.fallback_reason) == ("model", GOOD, None)
    assert (res.input_tokens, res.output_tokens) == (31, 17)
    assert res.seconds is not None and res.seconds >= 0
    assert len(fake.requests) == 1
    assert llm.calls == [{"provider": provider, "model": f"{provider}-model-x", "ok": True,
                          "input_tokens": 31, "output_tokens": 17,
                          "seconds": res.seconds, "fallback_reason": None}]


@pytest.mark.parametrize("provider", PROVIDERS)
def test_reply_without_text_is_a_cheap_llm_error(provider: str) -> None:
    with pytest.raises(CheapLLMError):
        parse_reply(provider, {})  # type: ignore[arg-type]


def test_gemini_thought_parts_are_not_the_answer() -> None:
    text, _, _ = parse_reply("gemini", _reply("gemini", GOOD))
    assert text == GOOD


# ── 정리기 ──────────────────────────────────────────────────────────────────


def test_sanitizer_names_entities_in_korean_and_drops_paths_and_files() -> None:
    out = sanitize_for_llm({
        "a": "Light.Living_Room 을 켜기",
        "b": "fan.living_purifier 세게",
        "c": "foo.bar 확인",
        "d": "USER.md 와 run.LOG 참고",
        "e": "로그는 /Users/someone/liot/x.db 와 ~/notes/a 에 있다",
        "f": "5/10 단계",
    })
    assert out == {"a": "조명 을 켜기", "b": "공기청정기 세게", "c": "기기 확인",
                   "d": "와 참고", "e": "로그는 와 에 있다", "f": "5/10 단계"}


def test_sanitizer_rounds_floats_and_drops_name_keys_and_non_finite() -> None:
    out = sanitize_for_llm({
        "pm25": 37.56789, "text": "기온 13.4567도", "user_name": "홍길동", "Name": "x",
        "nan": float("nan"), "flag": True, "n": 3, "none": None,
        "nested": [{"temp": 21.04999, "nickname": "y"}, float("inf"), 1.25],
    })
    assert out == {"pm25": 37.6, "text": "기온 13.5도", "flag": True, "n": 3, "none": None,
                   "nested": [{"temp": 21.0}, 1.2]}


def test_raw_facts_fail_egress_but_sanitized_facts_pass() -> None:
    raw = {"facts": {"target": "light.desk", "lat": 37.56789}}
    from liot.core.invariants import EgressViolation

    with pytest.raises(EgressViolation):
        emit_external(Destination.LLM, raw, trace_id="t")
    sanitized = emit_external(Destination.LLM, sanitize_for_llm(raw), trace_id="t")
    assert sanitized == {"facts": {"target": "조명", "lat": 37.6}}


# ── egress 를 지난 입력만 나간다 ────────────────────────────────────────────


def test_sent_input_is_exactly_the_egress_output() -> None:
    llm, fake = _client("openai")
    llm.reason(FACTS, fallback=FALLBACK, trace_id="t1")
    expected = emit_external(Destination.LLM, {"facts": sanitize_for_llm(FACTS)}, trace_id="t1")
    assert fake.requests[0].body["input"] == json.dumps(expected, ensure_ascii=False)
    sent = json.dumps(fake.requests[0].body, ensure_ascii=False)
    assert "fan.living_purifier" not in sent and "13.456" not in sent
    assert not any(word in sent for word in TRAIT_WORDS)


def test_egress_violation_never_sends() -> None:
    llm, fake = _client()
    res = llm.reason({**FACTS, "memo": "보호자 010-1234-5678"}, fallback=FALLBACK)
    assert (res.source, res.text, res.fallback_reason) == ("fallback", FALLBACK, "egress")
    assert fake.requests == [] and llm.calls == []


def test_trait_word_in_input_never_sends() -> None:
    llm, fake = _client()
    res = llm.reason({**FACTS, "memo": f"{TRAIT_WORDS[0]} 거주자"}, fallback=FALLBACK)
    assert (res.source, res.fallback_reason) == ("fallback", "trait_in_input")
    assert fake.requests == []


def test_system_prompt_has_no_trait_words() -> None:
    assert not any(word in cheap_llm._SYSTEM for word in TRAIT_WORDS)


# ── 출력 검증 → 대체 ────────────────────────────────────────────────────────


@pytest.mark.parametrize("text", [
    "", "   ", "첫 줄\n둘째 줄", "첫 줄\r둘째", "가" * 81, "Open the window now.",
    f"{TRAIT_WORDS[2]}께는 환기가 좋아요", 42, None,
])
def test_validator_rejects(text: Any) -> None:
    assert validate_reason(text) is None


def test_validator_accepts_one_korean_line_up_to_80_chars() -> None:
    assert validate_reason("  " + "가" * 80 + "  ") == "가" * 80
    assert validate_reason(GOOD) == GOOD


@pytest.mark.parametrize("bad", ["Ventilate now.", "환기\n하세요", "가" * 81,
                                 f"{TRAIT_WORDS[1]}이라 환기가 필요해요"])
def test_invalid_output_falls_back_with_usage(bad: str) -> None:
    llm, fake = _client("gemini", bad)
    res = llm.reason(FACTS, fallback=FALLBACK)
    assert (res.source, res.text, res.fallback_reason) == ("fallback", FALLBACK, "invalid_output")
    assert (res.input_tokens, res.output_tokens) == (31, 17)
    assert len(fake.requests) == 1
    assert llm.calls[0]["ok"] is False and llm.calls[0]["fallback_reason"] == "invalid_output"


def test_transport_failure_falls_back_once_no_retry() -> None:
    llm, fake = _client(fail=True)
    res = llm.reason(FACTS, fallback=FALLBACK)
    assert (res.source, res.fallback_reason) == ("fallback", "transport")
    assert len(fake.requests) == 1
    assert [c["fallback_reason"] for c in llm.calls] == ["transport"]


def test_reply_without_text_falls_back() -> None:
    llm = CheapLLM(_cfg(), KEY, transport=FakeTransport({"content": []}))
    res = llm.reason(FACTS, fallback=FALLBACK)
    assert (res.source, res.fallback_reason) == ("fallback", "bad_response")


# ── 키 ─────────────────────────────────────────────────────────────────────


def test_key_never_appears_in_repr_or_records() -> None:
    llm, fake = _client()
    llm.reason(FACTS, fallback=FALLBACK)
    assert KEY not in repr(llm)
    assert KEY not in repr(fake.requests[0])
    assert KEY not in json.dumps(llm.calls)
    assert KEY not in json.dumps(fake.requests[0].body)


def test_empty_key_is_rejected() -> None:
    with pytest.raises(ValueError, match="ANTHROPIC_API_KEY"):
        CheapLLM(_cfg(), "  ")


# ── 설정 ───────────────────────────────────────────────────────────────────


@pytest.mark.parametrize("provider", PROVIDERS)
def test_shipped_config_loads_each_provider(provider: str) -> None:
    cfg = load_cheap_config(provider=provider)
    assert cfg.provider == provider and cfg.model
    assert cfg.timeout_s > 0 and 1 <= cfg.max_output_tokens <= 4096


def test_shipped_config_default_provider() -> None:
    assert load_cheap_config().provider in PROVIDERS


@pytest.mark.parametrize("text", [
    "{}",
    "cheap_llm: {provider: mistral, models: {mistral: m}}",
    "cheap_llm: {provider: anthropic, models: {}}",
    "cheap_llm: {provider: anthropic, models: {anthropic: m}, timeout_s: 0}",
    "cheap_llm: {provider: anthropic, models: {anthropic: m}, timeout_s: true}",
    "cheap_llm: {provider: anthropic, models: {anthropic: m}, max_output_tokens: 0}",
])
def test_bad_config_is_rejected(tmp_path: Path, text: str) -> None:
    path = tmp_path / "models.yaml"
    path.write_text(text, encoding="utf-8")
    with pytest.raises(ModelConfigError):
        load_cheap_config(path)


# ── HTTP 전송(루프백 가짜 서버) ──────────────────────────────────────────────


class _Handler(BaseHTTPRequestHandler):
    status = 200
    hits: list[dict[str, Any]] = []

    def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
        pass

    def do_POST(self) -> None:  # noqa: N802
        length = int(self.headers.get("Content-Length", "0"))
        body = json.loads(self.rfile.read(length).decode("utf-8"))
        type(self).hits.append({"path": self.path, "key": self.headers.get("x-api-key"),
                                "body": body})
        out = json.dumps(_reply("anthropic", GOOD)).encode()
        self.send_response(self.status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(out)))
        self.end_headers()
        with contextlib.suppress(OSError):
            self.wfile.write(out)


@contextmanager
def _server(status: int) -> Iterator[str]:
    _Handler.status = status
    _Handler.hits = []
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    thread = threading.Thread(target=httpd.serve_forever, kwargs={"poll_interval": 0.05},
                              daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{httpd.server_port}/v1/messages"
    finally:
        httpd.shutdown()
        httpd.server_close()
        thread.join(timeout=5)


def _loopback(url: str) -> ProviderRequest:
    req = build_request(_cfg("anthropic"), KEY, "SYS", "USER")
    return ProviderRequest(url=url, headers=req.headers, body=req.body)


def test_http_transport_posts_once_with_header_key() -> None:
    with _server(200) as url:
        data = HTTPCheapTransport().post(_loopback(url), timeout_s=2)
    assert parse_reply("anthropic", data)[0] == GOOD
    assert len(_Handler.hits) == 1
    assert _Handler.hits[0]["key"] == KEY and "key" not in _Handler.hits[0]["path"]


def test_http_error_is_cheap_llm_error_once_and_hides_key() -> None:
    with _server(500) as url, pytest.raises(CheapLLMError) as exc:
        HTTPCheapTransport().post(_loopback(url), timeout_s=2)
    assert str(exc.value) == "HTTP 500" and KEY not in str(exc.value)
    assert len(_Handler.hits) == 1


def test_connection_refused_is_cheap_llm_error() -> None:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    with pytest.raises(CheapLLMError, match="transport failed"):
        HTTPCheapTransport().post(_loopback(f"http://127.0.0.1:{port}/v1/messages"), timeout_s=1)


# ── 보기 고르기(가상 거주자) ────────────────────────────────────────────────


OPTIONS = {"도움됐어요": "accepted", "그만해": "dismissed", "무시": "ignore"}


@pytest.mark.parametrize(("text", "expected"), [
    ("도움됐어요", "accepted"), (" [그만해] ", "dismissed"), ("「무시」.", "ignore"),
    ("그만해요", None), ("도움됐어요\n그만해", None), ("", None), (3, None),
])
def test_match_option(text: Any, expected: str | None) -> None:
    from liot.cheap_llm import match_option

    assert match_option(text, OPTIONS) == expected


def test_choose_sends_the_options_and_falls_back_on_an_odd_answer() -> None:
    llm, fake = _client("anthropic", "그만해")
    res = llm.choose(FACTS, OPTIONS, fallback="accepted")
    assert (res.source, res.text) == ("model", "dismissed")
    assert "도움됐어요 / 그만해 / 무시" in fake.requests[0].body["system"]
    odd, _ = _client("anthropic", "글쎄요")
    res = odd.choose(FACTS, OPTIONS, fallback="accepted")
    assert (res.source, res.text, res.fallback_reason) == ("fallback", "accepted", "invalid_output")
    assert not any(word in cheap_llm._CHOOSE_SYSTEM for word in TRAIT_WORDS)
