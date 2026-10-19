"""레이어 간에 흐르는 데이터 계약(pydantic v2).

이 모듈의 모델들은 파이프라인 경계를 넘나드는 유일한 형식이다.
다른 모든 모듈은 정확히 이 시그니처에 의존한다.

핵심 불변식:
  - ContextSnapshot.snapshot_hash 는 structured 페이로드에서 결정론적으로
    계산된다(재현 가능성). 빈 문자열로 들어오면 model_validator 가 채운다.
  - NotificationPayload 에는 device/room/rationale 필드가 존재하지 않는다
    (외부 송신 최소화, egress_gate 와 짝을 이룬다).
  - GuardianNotificationPayload(보호자 알림)에도 device/entity_id/room/rationale·
    거주자 특성 필드가 존재하지 않는다. 상황 종류·집 별칭·시각·파급도 라벨만 싣는다.
  - 파급도(SeverityLevel)는 RiskTier 와 별개의 축이며, 직렬화도 RiskTier 와 같이
    IntEnum 그대로 다룬다(model_dump(mode="json") 에서 정수).
"""

from __future__ import annotations

import hashlib
import json
from enum import Enum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, model_validator

from .risk import RiskTier
from .severity import SeverityLevel, SeveritySource

__all__ = [
    "Layer",
    "SensorReading",
    "CandidateAction",
    "ContextSnapshot",
    "Judgment",
    "Proposal",
    "FeedbackEvent",
    "NotificationPayload",
    "GuardianNotificationPayload",
    "EventMessage",
    "compute_snapshot_hash",
    # 다중 후보 파이프라인(새 기획: 생성→평가→선택)
    "NOOP_CAPABILITY",
    "make_noop_candidate",
    "is_noop",
    "ActionCandidate",
    "CandidateSet",
    "ExternalContext",
    "EvaluationResult",
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
    def _fill_snapshot_hash(self) -> ContextSnapshot:
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
    # ── 상황 파급도(RiskTier 와 별개의 축). 파급도가 없는 판단(쾌적 등)은
    # severity·severity_floor 가 None 이고 severity_sources 는 빈 목록이다.
    severity: SeverityLevel | None = None
    # 표+거주자 값으로 정해진 하한. 어떤 모델·지시도 이 아래로 내리지 못한다.
    severity_floor: SeverityLevel | None = None
    # 파급도가 어디서 왔는지(SEVERITY_SOURCE_ORDER 순서로 기록).
    severity_sources: list[SeveritySource] = Field(default_factory=list)
    situation_id: str | None = None
    # 질문 id("tier", "severity") → {선택지 id: 확률}. 모델 확률 원본 보존용.
    model_probs: dict[str, dict[str, float]] | None = None


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
    # 상황 파급도와 상황 식별자(판단에서 그대로 이어받는다).
    severity: SeverityLevel | None = None
    situation_id: str | None = None


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
    # 파급도의 한국어 라벨(예: "위험"). 라벨 문자열 외의 정보는 싣지 않는다.
    severity_label: str | None = None


class GuardianNotificationPayload(BaseModel):
    """보호자에게 나가는 알림 페이로드.

    상황 종류(situation_label)·집 별칭·시각·서명된 확인 링크·파급도 라벨만 싣는다.
    device/entity_id/room/rationale 과 거주자 특성 필드는 의도적으로 두지 않는다
    (외부 송신 최소화, extra="forbid" 로 구조적으로 차단).
    """

    model_config = ConfigDict(extra="forbid")

    channel: str
    situation_label: str  # 상황 종류(예: "가스 누출")
    home_alias: str
    at: str  # ISO 시각
    signed_ack_link: str
    severity_label: str


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


# ── 다중 후보 파이프라인 (새 기획: 생성 → 평가 → 선택) ──────────────────────
# 개념 8단계 중 L4 Candidate Generation · L5 Evaluation & Selection 의 데이터 계약.
# 기존 단일 CandidateAction 은 그대로 두고, 그 위에 점수·집합·외부맥락을 *추가*한다.
# 선택된 단일 CandidateAction 만 L3(가드레일)로 넘어가므로 L3·L4 핸들러는 무변경.

NOOP_CAPABILITY = "noop"
"""무행동(아무것도 하지 않음) 후보의 예약 capability.

별도 타입을 만들지 않고 CandidateAction 으로 표현한다(타입 폭증 회피).
NoOp 가 선택되면 파이프라인은 proposal 없이 침묵한다(가드레일을 거치지 않음).
"""


def make_noop_candidate(entity_id: str = "__none__") -> CandidateAction:
    """무행동 후보를 만든다. CandidateSet 은 항상 이것을 하나 이상 포함해야 한다."""
    return CandidateAction(
        capability=NOOP_CAPABILITY,
        action="hold",
        params={},
        entity_id=entity_id,
        category=NOOP_CAPABILITY,
    )


def is_noop(candidate: CandidateAction) -> bool:
    """후보가 무행동인지."""
    return candidate.capability == NOOP_CAPABILITY


class ActionCandidate(BaseModel):
    """점수가 매겨진 행동 후보(L5 평가 대상).

    candidate 는 기존 단일 행동 타입을 그대로 감싼다. 점수는 0~1 정규화.
    safety_risk 는 결정론적으로(catalog 기반) 채워지며 LLM 이 제출한 값은 무시된다.
    reason 은 설명 전용(점수 가중 0)이며 외부로 송신되지 않는다(egress 미포함).
    """

    model_config = ConfigDict(extra="forbid")

    candidate: CandidateAction
    expected_effect: float = 0.0
    user_preference_fit: float = 0.0
    energy_cost: float = 0.0
    disturbance: float = 0.0
    safety_risk: float = 0.0
    final_score: float = 0.0
    reason: str = ""
    origin: str = ""  # "rule:<id>" | "line_a:ml" | "line_b:llm" | "llm" | "noop"
    # 제안 라인이 실은 자신감(0~1). v1 은 텔레메트리/타이브레이크 전용이며
    # final_score 에 개입하지 않는다(결정론 아비터 = evaluator 무변경 유지).
    line_confidence: float = 0.0


class CandidateSet(BaseModel):
    """한 트리거에서 생성된 행동 후보 집합. 항상 무행동 후보를 포함한다.

    selected_index 는 평가 후 선택된 후보를 가리킨다(무행동 선택 가능).
    snapshot_hash 로 재현성을 고정한다(같은 스냅샷 → 같은 후보 집합).
    """

    model_config = ConfigDict(extra="forbid")

    trace_id: str
    home_id: str = "singleton"
    snapshot_hash: str
    candidates: list[ActionCandidate]
    selected_index: int = 0

    @model_validator(mode="after")
    def _validate(self) -> CandidateSet:
        if not self.candidates:
            raise ValueError("CandidateSet 은 비어 있을 수 없다(최소 무행동 후보 1개).")
        if not any(is_noop(c.candidate) for c in self.candidates):
            raise ValueError("CandidateSet 은 항상 무행동(NoOp) 후보를 포함해야 한다.")
        if not (0 <= self.selected_index < len(self.candidates)):
            raise ValueError(
                f"selected_index({self.selected_index})가 범위를 벗어났다"
                f"(0..{len(self.candidates) - 1})."
            )
        return self

    @property
    def selected(self) -> ActionCandidate:
        """선택된 후보."""
        return self.candidates[self.selected_index]

    def selected_action(self) -> CandidateAction:
        """선택된 행동(기존 단일 타입). L3 로 넘어가는 유일한 값."""
        return self.candidates[self.selected_index].candidate

    def selected_is_noop(self) -> bool:
        """선택이 무행동이면 True → 파이프라인은 침묵한다."""
        return is_noop(self.selected_action())


class ExternalContext(BaseModel):
    """외부 데이터(날씨·외기 공기질) 조회 결과(L3 Context Acquisition).

    degraded=True 면 외부 조회가 실패/불확실 → 평가는 보수적으로(무행동/저위험) 선택.
    to_structured() 산출물이 틱 트리거(→ structured)에 병합돼 snapshot_hash 에
    반영된다(외기가 후보 우선순위를 바꾸므로 해시에 고정돼야 재현 가능, 불변식 4).
    """

    model_config = ConfigDict(extra="forbid")

    outdoor_temp_c: float | None = None
    outdoor_aqi: int | None = None
    source: str = "fake"
    fetched_at: str
    degraded: bool = False

    def to_structured(self) -> dict[str, Any]:
        """ContextSnapshot.structured 에 병합할 결정론적 dict.

        fetched_at(타임스탬프)은 일부러 제외한다 — 같은 외기면 같은 해시여야
        재현성(불변식 4)이 유지되기 때문. 의미 있는 수치/플래그만 병합한다.
        """
        return {
            "outdoor_temp_c": self.outdoor_temp_c,
            "outdoor_aqi": self.outdoor_aqi,
            "ext_source": self.source,
            "ext_degraded": self.degraded,
        }


class EvaluationResult(BaseModel):
    """L5 평가·선택의 산출물(감사/데모용)."""

    model_config = ConfigDict(extra="forbid")

    scored: list[ActionCandidate]
    selected_index: int
    rationale: str

    @model_validator(mode="after")
    def _validate(self) -> EvaluationResult:
        if self.scored and not (0 <= self.selected_index < len(self.scored)):
            raise ValueError(
                f"selected_index({self.selected_index})가 범위를 벗어났다"
                f"(0..{len(self.scored) - 1})."
            )
        return self

    @property
    def selected(self) -> ActionCandidate:
        """선택된(점수가 매겨진) 후보."""
        return self.scored[self.selected_index]

    def selected_action(self) -> CandidateAction:
        """선택된 행동(기존 단일 타입)."""
        return self.scored[self.selected_index].candidate

    def selected_is_noop(self) -> bool:
        """선택이 무행동이면 True → 파이프라인은 침묵한다."""
        return is_noop(self.scored[self.selected_index].candidate)
