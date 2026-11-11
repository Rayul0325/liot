"""Jev(typesafe.ai systemone) 판정 백엔드 — 원격 확률 판정기. 하루 시뮬레이터 전용.

설계 핵심:
  - **증거는 결정 백엔드와 같다**: ``EvidenceBuilder``(OllamaBackend 의 payload 조립 →
    ``emit_external(Destination.LLM)`` → system 제거)를 그대로 써서 Jev 의 ``state`` 로 보낸다.
    거주자 특성은 payload 에 없고, 엔티티 id 는 egress 가 가명화한다.
  - **한 번의 POST**: 위험도(4단계) 질문은 늘, 파급도(4단계) 질문은 ``severity_floor`` 가
    주어졌을 때만 같은 요청에 넣는다. **재시도하지 않는다** — 늦은 판정보다 degraded 가 낫고
    (첫 알림은 이미 나갔다), 재시도는 비용과 지연을 곱한다.
  - **응답**: ``answers.<질문>.probabilities`` 는 "0".."3" 을 키로 하는 확률이다. 선택지 id
    (ambient…ember / record…critical)로 옮긴 뒤 argmax(동률이면 앞 선택지)와 그 확률을 쓴다.
  - **fail-closed**: HTTP 오류·연결 실패·시간 초과·형식 오류는 모두 ``LLMUnavailable`` →
    LLMJudge 의 degraded fail-safe(두 하한 유지). 하한 합성(escalate_only)은 LLMJudge 몫이다.
  - **키**: 생성자로 받는다(이 계층은 core 만 불러올 수 있어 비밀 파일을 직접 읽지 않는다).
    키는 오류문·repr·로그에 넣지 않는다.
"""

from __future__ import annotations

import http.client
import json
import math
import re
import threading
import time
from collections.abc import Mapping, Sequence
from typing import Any, Protocol, runtime_checkable

from ...core.contracts import CandidateAction, ContextSnapshot
from ...core.egress_gate import AliasMap
from ...core.risk import RiskTier
from ...core.severity import SeverityLevel
from .decision_backend import SEVERITY_QUESTION, TIER_QUESTION, EvidenceBuilder
from .llm_judge import LLMResult, LLMUnavailable

__all__ = [
    "DEFAULT_JEV_MODEL",
    "DEFAULT_JEV_TIMEOUT_S",
    "JEV_URL",
    "HTTPJevTransport",
    "JevBackend",
    "JevTransport",
    "read_jev_probs",
    "response_model",
]

JEV_URL = "https://api.typesafe.ai/v1/systemone"
DEFAULT_JEV_MODEL = "jev-latest"
DEFAULT_JEV_TIMEOUT_S = 8.0

# 질문 키(Jev 요청) → 질문 정의(결정 백엔드와 같은 기준 문장·선택지 순서).
_RISK_KEY = "risk"
_SEVERITY_KEY = "severity"


@runtime_checkable
class JevTransport(Protocol):
    """Jev 호출 추상화. JSON 본문을 한 번 POST 하고 JSON 객체를 돌려준다(재시도 없음)."""

    def post(self, payload: dict[str, Any], *, timeout_s: float) -> dict[str, Any]: ...


class HTTPJevTransport:
    """urllib 로 ``JEV_URL`` 에 한 번 POST 한다. 실패는 전부 ``LLMUnavailable``(키는 문구에 없음)."""

    def __init__(self, api_key: str, url: str = JEV_URL) -> None:
        if not api_key or not api_key.strip():
            raise ValueError("Jev 키가 비어 있습니다.")
        self._api_key = api_key.strip()
        self.url = url

    def __repr__(self) -> str:  # 키를 드러내지 않는다
        return f"HTTPJevTransport(url={self.url!r}, api_key=***)"

    def post(self, payload: dict[str, Any], *, timeout_s: float) -> dict[str, Any]:
        import urllib.error
        import urllib.request

        req = urllib.request.Request(
            self.url,
            data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            headers={
                "Authorization": f"Bearer {self._api_key}",
                "Content-Type": "application/json",
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=timeout_s) as resp:  # noqa: S310
                data = json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            raise LLMUnavailable(f"jev HTTP {exc.code}") from None
        except (urllib.error.URLError, http.client.HTTPException, OSError, TimeoutError,
                ValueError) as exc:
            raise LLMUnavailable(f"jev transport failed: {type(exc).__name__}") from None
        if not isinstance(data, dict):
            raise LLMUnavailable("jev response is not an object")
        return data


_MODEL_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,63}$")


def response_model(value: object) -> str | None:
    """응답의 ``model``(예: ``jev-1.13.0``). 짧은 이름 모양이 아니면 None(기록에 싣지 않는다)."""
    return value if isinstance(value, str) and _MODEL_NAME_RE.fullmatch(value) else None


def _question(question: Any) -> dict[str, Any]:
    """결정 백엔드 질문 정의 → Jev score 질문(선택지 순서 = 확률 키 "0".."3")."""
    return {
        "type": "score",
        "instructions": question.criterion,
        "criteria": [f"{oid} — {desc}" for oid, desc in question.options],
    }


def read_jev_probs(answer: object, option_ids: Sequence[str]) -> dict[str, float]:
    """``answers.<q>`` → {선택지 id: 확률}(option_ids 순서, 합 1 로 정규화).

    ``probabilities`` 는 "0".."n-1" 키의 유한한 비음수여야 한다. 없는 키는 0. 모두 0 이거나
    형식이 틀리면 ``LLMUnavailable``.
    """
    if not isinstance(answer, Mapping):
        raise LLMUnavailable("jev answer is not an object")
    raw = answer.get("probabilities")
    if not isinstance(raw, Mapping):
        raise LLMUnavailable("jev answer has no probabilities")
    allowed = {str(i) for i in range(len(option_ids))}
    vals = [0.0] * len(option_ids)
    for key, value in raw.items():
        if str(key) not in allowed:
            raise LLMUnavailable("jev probabilities has an unknown option key")
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise LLMUnavailable("jev probability is not a number")
        try:
            v = float(value)  # 너무 큰 정수는 OverflowError — 형식 오류로 친다
        except OverflowError:
            raise LLMUnavailable("jev probability overflows a float") from None
        if not math.isfinite(v) or v < 0:
            raise LLMUnavailable("jev probability is not a finite non-negative number")
        vals[int(key)] = v
    total = sum(vals)
    if not math.isfinite(total) or total <= 0:
        raise LLMUnavailable("jev probabilities sum to zero or overflow")
    return {oid: v / total for oid, v in zip(option_ids, vals, strict=True)}


def _argmax(probs: dict[str, float]) -> str:
    best = next(iter(probs))
    for oid, p in probs.items():
        if p > probs[best]:
            best = oid
    return best


class JevBackend:
    """LLMBackend 구현. 결정 백엔드와 같은 증거를 Jev 에 한 번 보내 확률을 받는다."""

    def __init__(
        self,
        api_key: str = "",
        *,
        model: str = DEFAULT_JEV_MODEL,
        transport: JevTransport | None = None,
        timeout_s: float = DEFAULT_JEV_TIMEOUT_S,
        alias: AliasMap | None = None,
    ) -> None:
        if not math.isfinite(timeout_s) or timeout_s <= 0:
            raise ValueError("Jev 제한시간은 양의 초여야 합니다.")
        self.model = model
        self.model_id = f"jev:{model}"
        self.timeout_s = timeout_s
        self.transport: JevTransport = (
            transport if transport is not None else HTTPJevTransport(api_key)
        )
        self._evidence = EvidenceBuilder(model, alias=alias)
        # 마지막 호출의 관찰값(지연·토큰·응답 모델) — 시뮬레이터가 비용·지연을 센다.
        # 스레드마다 따로 둔다: 컴포트 판정과 2단계 판정이 서로 다른 워커 스레드에서 겹쳐 돌 수
        # 있어, 객체 하나에 두면 한 호출의 사용량을 다른 호출이 덮어쓴다. 한 호출은 한 스레드에서
        # 끝나고 읽는 쪽도 같은 스레드(판정 직후)라서 읽는 방법(`backend.last_usage`)은 그대로다.
        self._usage_tls = threading.local()

    @property
    def last_usage(self) -> dict[str, Any]:
        """이 스레드에서 마지막으로 끝난 호출의 사용량(호출이 없었으면 빈 dict)."""
        usage: dict[str, Any] | None = getattr(self._usage_tls, "usage", None)
        if usage is None:
            usage = {}
            self._usage_tls.usage = usage
        return usage

    @last_usage.setter
    def last_usage(self, value: dict[str, Any]) -> None:
        self._usage_tls.usage = value

    def __repr__(self) -> str:
        return f"JevBackend(model_id={self.model_id!r})"

    def request_body(
        self,
        candidate: CandidateAction,
        ctx: ContextSnapshot | None,
        gate1_floor: RiskTier,
        severity_floor: SeverityLevel | None = None,
    ) -> dict[str, Any]:
        """보낼 본문(state = egress 를 지난 증거). 파급도 질문은 하한이 있을 때만."""
        questions: dict[str, Any] = {_RISK_KEY: _question(TIER_QUESTION)}
        if severity_floor is not None:
            questions[_SEVERITY_KEY] = _question(SEVERITY_QUESTION)
        return {
            "model": self.model,
            "state": self._evidence.build(candidate, ctx, gate1_floor),
            "questions": questions,
        }

    def judge_raw(
        self,
        candidate: CandidateAction,
        ctx: ContextSnapshot | None,
        gate1_floor: RiskTier,
        severity_floor: SeverityLevel | None = None,
    ) -> LLMResult:
        """한 번 POST(재시도 없음). 위험도는 늘, 파급도는 하한이 있을 때만. 실패는 LLMUnavailable."""
        self.last_usage = {}  # 이 스레드의 앞 호출 값이 이 호출 것으로 읽히지 않게 비운다
        body = self.request_body(candidate, ctx, gate1_floor, severity_floor)
        t0 = time.perf_counter()
        try:
            data = self.transport.post(body, timeout_s=self.timeout_s)
        finally:
            self.last_usage = {"seconds": round(time.perf_counter() - t0, 4)}
        answers = data.get("answers")
        if not isinstance(answers, Mapping):
            raise LLMUnavailable("jev response has no answers")
        tier_probs = read_jev_probs(answers.get(_RISK_KEY), TIER_QUESTION.option_ids)
        tier_id = _argmax(tier_probs)
        probs: dict[str, dict[str, float]] = {"tier": tier_probs}
        severity: SeverityLevel | None = None
        if severity_floor is not None:
            sev_probs = read_jev_probs(answers.get(_SEVERITY_KEY), SEVERITY_QUESTION.option_ids)
            probs["severity"] = sev_probs
            severity = SeverityLevel.from_str(_argmax(sev_probs))
        usage = data.get("usage")
        served = response_model(data.get("model"))
        self.last_usage.update(
            {
                "model": served,
                "input_tokens": usage.get("input_tokens") if isinstance(usage, Mapping) else None,
                "output_tokens": usage.get("output_tokens") if isinstance(usage, Mapping) else None,
            }
        )
        summary = "; ".join(
            f"{qid} " + "/".join(f"{oid} {p:.2f}" for oid, p in opts.items())
            for qid, opts in probs.items()
        )
        # model_id 는 요청한 이름(jev:jev-latest — degraded 기록과 같은 이름)이고, 실제로 답한 판(예:
        # jev-1.13.0)은 판정 근거에 남긴다(판단 기록·속 보기에서 보인다).
        served_note = f" · 응답 모델 {served}" if served else ""
        return LLMResult(
            tier=RiskTier.from_str(tier_id),
            confidence=tier_probs[tier_id],
            rationale=f"Jev 판정{served_note} ({summary})",
            model_id=self.model_id,
            severity=severity,
            probs=probs,
        )
