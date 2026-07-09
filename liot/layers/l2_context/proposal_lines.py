"""2갈래 제안 라인 (개념 ③ Proposal Lines) + 병합.

한 맥락에서 두 독립 엔진이 각각 행동 후보 + 자신감(가중치)을 낸다.

  Line A (ML)  : 선호 임베딩 + 전체 값 → 맥락 규칙 + 휴리스틱 자신감.
  Line B (LLM) : 선호 임베딩 + available_actions(조작 가능 행동 목록) → 제안.

두 라인의 모든 후보는 반드시 ``validate_proactive_candidate`` 를 통과해야 병합
후보풀에 든다(비선택 후보도 이 게이트에만 의존 — 안전 경계). 라인 자신감은
``ActionCandidate.origin`` + ``line_confidence`` 에 실려 텔레메트리/타이브레이크로
만 쓰인다(final_score 미개입 → 결정론 아비터 = evaluator 무변경).

v1 은 결정론 Fake(네트워크 없음). 실모델 ML/LLM 라인은 같은 Protocol 뒤 슬롯(예정).
아비터(CandidateEvaluator)가 병합셋을 받아 최종 선택한다(Phase 2).
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Mapping, Protocol, Sequence, runtime_checkable

from liot.core.contracts import (
    ActionCandidate,
    CandidateAction,
    CandidateSet,
    compute_snapshot_hash,
    make_noop_candidate,
)

from .feature_vector import PreferenceEmbedder, cell_key
from .proactive import validate_proactive_candidate

if TYPE_CHECKING:  # pragma: no cover
    from liot.layers.l4_execution.device_catalog import DeviceCatalog

__all__ = [
    "ProposalLine",
    "MLProposalLine",
    "LLMProposalLine",
    "merge_candidate_set",
]

# "켜짐"으로 간주하는 정규화 값(조명 상태 판단).
_ON_VALUES = frozenset({True, 1, "1", "on", "ON", "true", "True", "on_"})


def _is_on(value: Any) -> bool:
    return value in _ON_VALUES


def _context_candidates(
    facts: Mapping[str, Any],
    cur: Mapping[str, Any],
    available_actions: Sequence[Mapping[str, Any]] | None,
) -> list[CandidateAction]:
    """맥락에 맞는 컴포트 후보를 결정론적으로 고른다(양 라인이 공유하는 규칙).

    v1 규칙(확장 가능):
      - 빈 집(occupancy=vacant) + 켜진 조명 → 그 조명 끄기.
    실모델 라인에선 이 자리에 ML/LLM 이 들어가지만, 안전 게이트
    (validate_proactive_candidate)는 어느 쪽이든 병합 직전에 강제된다.
    """
    occ = str(facts.get("occupancy", "")).strip().lower()
    out: list[CandidateAction] = []
    for act in available_actions or []:
        cap = str(act.get("capability") or "")
        action = str(act.get("action") or "")
        eid = str(act.get("entity_id") or "")
        cat = str(act.get("category") or cap)
        if not cap or not action or not eid:
            continue
        if (
            occ == "vacant"
            and cap == "light"
            and action == "turn_off"
            and _is_on(cur.get(eid))
        ):
            out.append(
                CandidateAction(
                    capability=cap, action=action, params={}, entity_id=eid, category=cat
                )
            )
    return out


@runtime_checkable
class ProposalLine(Protocol):
    """행동 후보 + 자신감을 내는 제안 엔진의 공통 계약."""

    name: str

    def propose(
        self,
        facts: Mapping[str, Any],
        cur: Mapping[str, Any],
        *,
        embedder: PreferenceEmbedder | None = None,
        available_actions: Sequence[Mapping[str, Any]] | None = None,
    ) -> list[ActionCandidate]: ...


class MLProposalLine:
    """Line A · ML(결정론 휴리스틱) 제안.

    맥락 규칙으로 후보를 고르고, 자신감 = 기본치와 선호 임베딩의 결합(결정론).
    실 학습 모델은 같은 Protocol 뒤 슬롯(예정).
    """

    name = "line_a:ml"
    _base = 0.70

    def propose(
        self,
        facts: Mapping[str, Any],
        cur: Mapping[str, Any],
        *,
        embedder: PreferenceEmbedder | None = None,
        available_actions: Sequence[Mapping[str, Any]] | None = None,
    ) -> list[ActionCandidate]:
        out: list[ActionCandidate] = []
        for ca in _context_candidates(facts, cur, available_actions):
            pref = embedder.preference_for(ca, facts) if embedder is not None else 0.5
            conf = round(0.5 * self._base + 0.5 * float(pref), 4)
            out.append(
                ActionCandidate(
                    candidate=ca,
                    origin=self.name,
                    line_confidence=conf,
                    reason="ML: 맥락 규칙 + 선호",
                )
            )
        return out


class LLMProposalLine:
    """Line B · LLM 제안(Fake, 네트워크 없음).

    available_actions(조작 가능 행동 목록)를 받아 맥락에 맞는 제안을 낸다.
    v1 은 결정론 Fake; 실모델 LLM 슬롯은 예정.
    """

    name = "line_b:llm"
    _base = 0.65

    def propose(
        self,
        facts: Mapping[str, Any],
        cur: Mapping[str, Any],
        *,
        embedder: PreferenceEmbedder | None = None,
        available_actions: Sequence[Mapping[str, Any]] | None = None,
    ) -> list[ActionCandidate]:
        out: list[ActionCandidate] = []
        for ca in _context_candidates(facts, cur, available_actions):
            pref = embedder.preference_for(ca, facts) if embedder is not None else 0.5
            conf = round(0.5 * self._base + 0.5 * float(pref), 4)
            out.append(
                ActionCandidate(
                    candidate=ca,
                    origin=self.name,
                    line_confidence=conf,
                    reason="LLM: 조작 가능 행동 중 맥락 제안",
                )
            )
        return out


def _dedup_key(ac: ActionCandidate) -> tuple[str, str, str]:
    c = ac.candidate
    return (c.capability, c.action, c.entity_id)


def merge_candidate_set(
    *,
    trace_id: str,
    facts: Mapping[str, Any],
    cur: Mapping[str, Any],
    lines: Sequence[ProposalLine],
    catalog: "DeviceCatalog | None",
    embedder: PreferenceEmbedder | None = None,
    available_actions: Sequence[Mapping[str, Any]] | None = None,
    home_id: str = "singleton",
) -> CandidateSet:
    """두 라인의 후보를 검증·병합해 CandidateSet 을 만든다(NoOp 항상 포함).

    - 양 라인 모든 후보는 validate_proactive_candidate 통과분만 남는다(fail-closed).
    - (capability, action, entity_id) 중복은 하나로 합치되, origin 을 합치고
      더 높은 line_confidence 를 취한다(두 라인이 같은 행동에 동의했다는 신호 보존).
    - snapshot_hash 는 (facts + cur + 선호 임베딩값)을 전부 덮는다 → 같은 입력이면
      같은 후보집합(불변식 4). 임베딩이 우선순위를 바꾸므로 해시에 고정돼야 한다.
    - selected_index 는 NoOp 을 가리키는 안전 기본값 — 실제 선택은 아비터(Phase 2).
    """
    merged: dict[tuple[str, str, str], ActionCandidate] = {}
    pref_fingerprint: dict[str, float] = {}
    for line in lines:
        for ac in line.propose(
            facts, cur, embedder=embedder, available_actions=available_actions
        ):
            if validate_proactive_candidate(ac.candidate, catalog) is not None:
                continue  # fail-closed: 금지 후보 폐기(NoOp 생존)
            if embedder is not None:
                tb = str(facts.get("time_bucket", ""))
                occ = str(facts.get("occupancy", ""))
                pref_fingerprint[cell_key(tb, occ, ac.candidate)] = float(
                    embedder.preference_for(ac.candidate, facts)
                )
            key = _dedup_key(ac)
            prev = merged.get(key)
            if prev is None:
                merged[key] = ac
            else:
                origins = "+".join(
                    dict.fromkeys(prev.origin.split("+") + [ac.origin])
                )
                merged[key] = prev.model_copy(
                    update={
                        "origin": origins,
                        "line_confidence": max(
                            prev.line_confidence, ac.line_confidence
                        ),
                    }
                )

    candidates = sorted(merged.values(), key=_dedup_key)
    noop_idx = len(candidates)  # NoOp 을 마지막에 붙인다
    candidates.append(
        ActionCandidate(candidate=make_noop_candidate(), origin="noop")
    )

    snapshot_hash = compute_snapshot_hash(
        {
            "facts": dict(facts),
            "cur": dict(cur),
            "prefs": pref_fingerprint,
        }
    )
    return CandidateSet(
        trace_id=trace_id,
        home_id=home_id,
        snapshot_hash=snapshot_hash,
        candidates=candidates,
        selected_index=noop_idx,
    )
