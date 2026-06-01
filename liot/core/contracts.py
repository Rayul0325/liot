"""레이어 간에 흐르는 데이터 계약(pydantic v2).

이 모듈의 모델들은 파이프라인 경계를 넘나드는 유일한 형식이다.
다른 모든 모듈은 정확히 이 시그니처에 의존한다.

핵심 불변식:
  - ContextSnapshot.snapshot_hash 는 structured 페이로드에서 결정론적으로
    계산된다(재현 가능성). 빈 문자열로 들어오면 model_validator 가 채운다.
  - NotificationPayload 에는 device/room/rationale 필드가 존재하지 않는다
    (외부 송신 최소화, egress_gate 와 짝을 이룬다).
"""

from __future__ import annotations

import hashlib
import json
from enum import Enum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, model_validator

from .risk import RiskTier

__all__ = [
    "Layer",
    "SensorReading",
    "CandidateAction",
    "ContextSnapshot",
    "Judgment",
    "Proposal",
    "FeedbackEvent",
    "NotificationPayload",
    "EventMessage",
    "compute_snapshot_hash",
]


class Layer(str, Enum):
    """파이프라인 레이어 식별자. 토폴로지 검증에 쓰인다."""

    L1 = "L1"
    L2 = "L2"
    L3 = "L3"
    L4 = "L4"
    USER = "USER"


def _canonical_json(payload: Any) -> str:
    """결정론적 직렬화. 키 정렬 + 컴팩트 구분자."""
    return json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )


def compute_snapshot_hash(structured: dict[str, Any]) -> str:
    """structured 페이로드의 결정론적 sha256(hex)."""
    return hashlib.sha256(_canonical_json(structured).encode("utf-8")).hexdigest()


class SensorReading(BaseModel):
    """L1 이 방출하는 정규화된 센서 읽기."""

    model_config = ConfigDict(extra="forbid")

    entity_id: str
    source: str
    raw_value: Any
    norm_value: Any
    unit: str | None = None
    confidence: float = 1.0
    ts_utc: str
    home_id: str = "singleton"


class CandidateAction(BaseModel):
    """판단 대상이 되는 후보 행동(제안 후보)."""

    model_config = ConfigDict(extra="forbid")

    capability: str
    action: str
    params: dict[str, Any] = Field(default_factory=dict)
    entity_id: str
    category: str


class ContextSnapshot(BaseModel):
    """L2 가 빚어낸 맥락 스냅샷. 재현 가능한 판단의 입력이다."""

    model_config = ConfigDict(extra="forbid")

    snapshot_id: str
    trace_id: str
    home_id: str = "singleton"
    trigger: dict[str, Any]
    structured: dict[str, Any]
    nl_summary: str
    summary_template_sha: str
    created_at: str
    snapshot_hash: str = ""

    @model_validator(mode="after")
    def _fill_snapshot_hash(self) -> "ContextSnapshot":
        """snapshot_hash 가 비어 있으면 structured 에서 결정론적으로 계산한다."""
        if not self.snapshot_hash:
            object.__setattr__(
                self, "snapshot_hash", compute_snapshot_hash(self.structured)
            )
        return self


class Judgment(BaseModel):
    """L3 의 판단 결과. snapshot_hash + bundle_id + rationale 를 함께 보존한다.

    최종 risk_tier 는 max(gate1, gate2) 이며 Gate2(LLM)는 Gate1 을 무를 수 없다.
    """

    model_config = ConfigDict(extra="forbid")

    judgment_id: str
    trace_id: str
    home_id: str
    snapshot_hash: str
    action: CandidateAction
    risk_tier: RiskTier
    confidence: float
    rationale: str
    gate1_verdict: str
    gate2_verdict: str | None = None
    bundle_id: str
    model_id: str
    degraded: bool = False


class Proposal(BaseModel):
    """L4 가 사용자에게 내미는 제안. Phase1 에서 시스템이 만드는 최종 산출물.

    Phase1 = 제안-만-모델: 이 제안은 결코 자동 실행되지 않는다.
    """

    model_config = ConfigDict(extra="forbid")

    proposal_id: str
    judgment_id: str
    home_id: str
    risk_tier: RiskTier
    action: CandidateAction
    rationale: str
    required_confirmation: str
    perishable: bool = False
    expires_at: str | None = None
    state: str = "pending"
    created_at: str


class FeedbackEvent(BaseModel):
    """사용자(USER) 가 제안에 보내는 피드백."""

    model_config = ConfigDict(extra="forbid")

    proposal_id: str
    trace_id: str
    user_action: str
    stop_scope: str | None = None
    comment: str | None = None
    latency_ms: int | None = None


class NotificationPayload(BaseModel):
    """외부 알림 채널로 나가는 페이로드.

    의도적으로 device/room/rationale 필드를 두지 않는다. 외부에 새어나갈 수
    있는 식별 정보를 구조적으로 차단한다(egress_gate 화이트리스트와 짝).
    """

    model_config = ConfigDict(extra="forbid")

    channel: str
    proposal_id: str
    risk_tier: RiskTier
    glyph: str
    label: str
    signed_deeplink: str
    topic: str


class EventMessage(BaseModel):
    """이벤트 버스를 흐르는 봉투. layer_from→layer_to 가 토폴로지 검증의 핵심."""

    model_config = ConfigDict(extra="forbid")

    trace_id: str
    home_id: str
    layer_from: Layer
    layer_to: Layer
    type: str
    ref_id: str
    partition_key: str
    ts: str
