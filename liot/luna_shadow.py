"""OpenAI luna 그림자 비교기 — 판정에는 쓰지 않고 Jev 판정 옆에 기록만 한다. 시뮬레이터 전용.

설계 핵심:
  - **세 방식**(``config/models.yaml`` 의 ``luna_shadow.mode``):
    - ``decision_api``: 공개 문서가 없는 제한 프리뷰다. 0단계에서 엔드포인트·요청 형태를 확인하기
      전까지 요청을 만들지 않고 늘 '미접근'을 돌려준다(형태를 지어내지 않는다).
    - ``logprobs``: Chat Completions 에 결정 백엔드와 같은 4지선다를 묻고
      (``logprobs: true``, ``top_logprobs: 5``, ``reasoning_effort: "none"``,
      ``max_completion_tokens: 16``) 첫 토큰의 보기 글자(A~D)만 softmax 한다
      (``read_option_probs(missing="zero")`` — top 에 없는 보기는 0).
    - ``unavailable``: 부르지 않고 '미접근'.
  - **같은 증거**: Jev 에 보낸 증거 문자열(``EvidenceBuilder`` 출력)을 받는다. 그래도 한 번 더
    ``emit_external(Destination.LLM)`` 을 지나게 해 PII·엔티티 모양이 남았으면 보내지 않는다.
  - **절대 던지지 않는다**: 그림자는 판정 뒤에 따로 돌고, 어떤 실패든 '미접근' 결과로 끝난다.
    재시도하지 않는다. 판정의 안전 2단계 마감 밖이다.
  - **일치 여부**: 질문별 argmax 가 Jev 의 argmax 와 같은지(동률이면 앞 선택지).
"""

from __future__ import annotations

import math
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

import yaml

from liot.cheap_llm import (
    MAX_TOKEN_COUNT,
    MODELS_PATH,
    CheapLLMError,
    CheapTransport,
    HTTPCheapTransport,
    ModelConfigError,
    ProviderRequest,
)
from liot.core.egress_gate import AliasMap, Destination, emit_external
from liot.core.invariants import EgressViolation
from liot.layers.l3_judgment.decision_backend import (
    DECISION_SYSTEM,
    SEVERITY_QUESTION,
    TIER_QUESTION,
    argmax_option,
    option_prompt,
    read_option_probs,
)
from liot.layers.l3_judgment.llm_judge import LLMUnavailable

__all__ = [
    "MODES",
    "OPENAI_CHAT_URL",
    "MAX_COMPLETION_TOKENS",
    "REASONING_EFFORT",
    "TOP_LOGPROBS",
    "LunaConfig",
    "LunaShadow",
    "ShadowResult",
    "load_luna_config",
]

Mode = Literal["decision_api", "logprobs", "unavailable"]
MODES: tuple[Mode, ...] = ("decision_api", "logprobs", "unavailable")
OPENAI_CHAT_URL = "https://api.openai.com/v1/chat/completions"
# 2026-10-20 실측(gpt-6-luna): logprobs 는 reasoning_effort "none" 일 때만 받고, top_logprobs 는
# 5 이하, max_completion_tokens 1 은 HTTP 400("max_tokens reached") — 16 이면 첫 토큰이 보기 글자다.
# 아주 작은 글자는 top 에서 빠져 오므로(2개만 오기도 한다) 빠진 보기는 확률 0 으로 친다.
TOP_LOGPROBS = 5
MAX_COMPLETION_TOKENS = 16
REASONING_EFFORT = "none"

_QUESTIONS = {TIER_QUESTION.qid: TIER_QUESTION, SEVERITY_QUESTION.qid: SEVERITY_QUESTION}


@dataclass(frozen=True)
class LunaConfig:
    mode: Mode
    model: str
    timeout_s: float = 8.0


def load_luna_config(path: Path = MODELS_PATH) -> LunaConfig:
    """``luna_shadow`` 절을 읽는다."""
    raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    section = raw.get("luna_shadow") if isinstance(raw, dict) else None
    if not isinstance(section, dict):
        raise ModelConfigError("luna_shadow 절이 없습니다.")
    mode = section.get("mode")
    if mode not in MODES:
        raise ModelConfigError(f"모르는 luna 방식입니다: {mode!r}")
    model = section.get("model")
    if not isinstance(model, str) or not model.strip():
        raise ModelConfigError("luna 모델 id 가 없습니다.")
    timeout = section.get("timeout_s", 8)
    if (
        isinstance(timeout, bool)
        or not isinstance(timeout, (int, float))
        or not math.isfinite(timeout)
        or timeout <= 0
    ):
        raise ModelConfigError("luna timeout_s 는 양의 초여야 합니다.")
    return LunaConfig(mode=mode, model=model.strip(), timeout_s=float(timeout))


@dataclass(frozen=True)
class ShadowResult:
    """그림자 결과. ``status == "unavailable"`` 이면 ``reason_ko`` 에 한국어 사유가 있다."""

    status: Literal["ok", "unavailable"]
    mode: Mode
    model: str
    reason_ko: str | None = None
    probs: dict[str, dict[str, float]] = field(default_factory=dict)
    choice: dict[str, str] = field(default_factory=dict)
    # 질문별 Jev 와의 일치. Jev 확률이 없는 질문은 싣지 않는다.
    agree: dict[str, bool] = field(default_factory=dict)
    seconds: float | None = None
    input_tokens: int | None = None
    output_tokens: int | None = None


def _count(value: object) -> int:
    """응답의 토큰 수. 정수가 아니거나 범위(0~MAX_TOKEN_COUNT) 밖이면 0."""
    if isinstance(value, bool) or not isinstance(value, int):
        return 0
    return value if 0 <= value <= MAX_TOKEN_COUNT else 0


class LunaShadow:
    """Jev 판정 옆에 luna 확률을 나란히 기록한다. 판정 결과를 바꾸지 않는다."""

    def __init__(
        self,
        cfg: LunaConfig,
        api_key: str = "",
        *,
        transport: CheapTransport | None = None,
        alias: AliasMap | None = None,
    ) -> None:
        self.cfg = cfg
        self._api_key = api_key.strip()
        self.transport: CheapTransport = (
            transport if transport is not None else HTTPCheapTransport()
        )
        self.alias = alias
        # 보낸 요청마다 한 줄: 질문·지연·토큰·성공 여부. 키·본문은 없다.
        self.calls: list[dict[str, Any]] = []

    def __repr__(self) -> str:
        return f"LunaShadow(mode={self.cfg.mode!r}, model={self.cfg.model!r})"

    def _unavailable(self, reason_ko: str, **extra: Any) -> ShadowResult:
        return ShadowResult(
            status="unavailable", mode=self.cfg.mode, model=self.cfg.model, reason_ko=reason_ko,
            **extra,
        )

    def request(self, question_id: str, evidence: str) -> ProviderRequest:
        """logprobs 방식의 요청 하나(질문 하나). 결정 백엔드와 같은 지시·선택지."""
        question = _QUESTIONS[question_id]
        return ProviderRequest(
            url=OPENAI_CHAT_URL,
            headers={
                "authorization": f"Bearer {self._api_key}",
                "content-type": "application/json",
            },
            body={
                "model": self.cfg.model,
                "messages": [
                    {"role": "system", "content": DECISION_SYSTEM},
                    {"role": "user", "content": option_prompt(question, evidence)},
                ],
                "logprobs": True,
                "top_logprobs": TOP_LOGPROBS,
                "reasoning_effort": REASONING_EFFORT,
                # 첫 토큰(보기 글자)만 읽는다. 1 은 거부된다(0단계 실측) — 16 이면 첫 토큰이 글자다.
                "max_completion_tokens": MAX_COMPLETION_TOKENS,
            },
        )

    def compare(
        self,
        evidence: str,
        jev_probs: Mapping[str, Mapping[str, float]],
        *,
        trace_id: str = "luna",
        allow: Callable[[], bool] | None = None,
        on_request: Callable[[dict[str, Any]], None] | None = None,
    ) -> ShadowResult:
        """tier 는 늘, severity 는 Jev 가 물었을 때만 묻는다. 어떤 실패도 '미접근'으로 끝난다.

        ``allow`` 는 요청 하나마다 그 직전에 부른다(계량기 자리). False 면 보내지 않고 '미접근'.
        ``on_request`` 는 보낸 요청 하나마다 그 기록(지연·토큰·성공)을 받는다.
        """
        if self.cfg.mode == "unavailable":
            return self._unavailable("luna 미접근 · 설정에서 꺼짐")
        if self.cfg.mode == "decision_api":
            return self._unavailable(
                "luna 미접근 · 호출 주소 미확인(0단계)"
            )
        if not self._api_key:
            return self._unavailable("luna 미접근 · 키 없음")
        try:
            sanitized = emit_external(
                Destination.LLM, {"evidence": evidence}, trace_id=trace_id, alias=self.alias
            )
        except EgressViolation:
            return self._unavailable("luna 미접근 · 송신 검사")
        safe_evidence = str(sanitized["evidence"])
        qids = [TIER_QUESTION.qid] + (
            [SEVERITY_QUESTION.qid] if SEVERITY_QUESTION.qid in jev_probs else []
        )
        probs: dict[str, dict[str, float]] = {}
        seconds = 0.0
        tokens_in = tokens_out = 0
        def done(record: dict[str, Any]) -> None:
            self.calls.append(record)
            if on_request is not None:
                on_request(dict(record))

        for qid in qids:
            if allow is not None and not allow():
                return self._unavailable("luna 미접근 · 호출 상한")
            t0 = time.perf_counter()
            record: dict[str, Any] = {"question": qid, "model": self.cfg.model, "ok": False}
            try:
                data = self.transport.post(
                    self.request(qid, safe_evidence), timeout_s=self.cfg.timeout_s
                )
            except CheapLLMError:
                record["seconds"] = round(time.perf_counter() - t0, 4)
                done(record)
                # 예외 글은 싣지 않는다 — 이 사유는 화면·기록으로 나간다.
                return self._unavailable("luna 미접근 · 호출 실패")
            elapsed = time.perf_counter() - t0
            seconds += elapsed
            record["seconds"] = round(elapsed, 4)
            usage = data.get("usage")
            if isinstance(usage, Mapping):
                tokens_in += _count(usage.get("prompt_tokens"))
                tokens_out += _count(usage.get("completion_tokens"))
                record.update(
                    input_tokens=_count(usage.get("prompt_tokens")),
                    output_tokens=_count(usage.get("completion_tokens")),
                )
            try:
                top = data["choices"][0]["logprobs"]["content"][0]["top_logprobs"]
                probs[qid] = read_option_probs(top, _QUESTIONS[qid].option_ids, missing="zero")
            except (KeyError, IndexError, TypeError, LLMUnavailable):
                done(record)
                return self._unavailable("luna 미접근 · 응답 해석 실패")
            record["ok"] = True
            done(record)
        choice = {qid: argmax_option(p) for qid, p in probs.items()}
        agree = {
            qid: choice[qid] == argmax_option(dict(jev_probs[qid]))
            for qid in choice
            if qid in jev_probs and jev_probs[qid]
        }
        return ShadowResult(
            status="ok",
            mode=self.cfg.mode,
            model=self.cfg.model,
            probs=probs,
            choice=choice,
            agree=agree,
            seconds=round(seconds, 4),
            input_tokens=tokens_in,
            output_tokens=tokens_out,
        )
