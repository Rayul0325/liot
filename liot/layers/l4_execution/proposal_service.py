"""제안(Proposal) 수명주기 서비스.

Proposal 의 생성/영속화/만료/상태전이/피드백을 담당한다. 테넌시 격리는
ScopedRepo 로 강제하며, 모든 쿼리는 home_id 정확 일치로만 동작한다.

Phase1 불변식: 실행 코드 없음. 이 서비스는 제안을 만들고 그 상태를
관리할 뿐, 어떤 액추에이터도 호출하지 않는다.

상태(state): pending / accepted / dismissed / expired / superseded.
"""

from __future__ import annotations

import uuid
from datetime import timedelta
from typing import TYPE_CHECKING, Any

from liot.core.clock import Clock
from liot.core.contracts import CandidateAction, FeedbackEvent, Judgment, Proposal
from liot.core.invariants import InvariantError
from liot.core.scoped_repo import DEFAULT_HOME_ID, ScopedRepo
from .notifier import dedup_key

if TYPE_CHECKING:  # pragma: no cover
    import aiosqlite

    from .device_catalog import DeviceCatalog
    from .risk_router import RiskRouter

__all__ = ["ProposalService", "FEEDBACK_STATE_MAP"]

# user_action -> 제안 상태 전이.
FEEDBACK_STATE_MAP: dict[str, str] = {
    "accepted": "accepted",
    "accept": "accepted",
    "confirmed": "accepted",
    "dismissed": "dismissed",
    "dismiss": "dismissed",
    "rejected": "dismissed",
    "reject": "dismissed",
    "snoozed": "pending",
    "snooze": "pending",
    "stop": "dismissed",
}


class ProposalService:
    """Proposal 의 생성·영속화·만료·피드백을 다루는 서비스."""

    def __init__(
        self,
        db: "aiosqlite.Connection",
        home_id: str = DEFAULT_HOME_ID,
        clock: Clock | None = None,
        router: "RiskRouter | None" = None,
    ) -> None:
        # ScopedRepo(None/빈문자열) -> IsolationError (생성자에서 강제).
        self._repo = ScopedRepo(home_id)
        self._db = db
        self._clock = clock or Clock()
        self._router = router

    @property
    def home_id(self) -> str:
        return self._repo.home_id

    # ------------------------------------------------------------------
    # 생성
    # ------------------------------------------------------------------
    def build(
        self,
        judgment: Judgment,
        *,
        perishable: bool = False,
        expires_at: str | None = None,
    ) -> Proposal:
        """Judgment -> Proposal (라우터 경유, 실행 없음).

        라우터가 주입되지 않았으면 지연 import 로 기본 RiskRouter 사용.
        """
        if self._router is None:
            from .risk_router import RiskRouter

            self._router = RiskRouter(clock=self._clock)
        return self._router.route(
            judgment,
            perishable=perishable,
            expires_at=expires_at,
            created_at=self._clock.now_utc(),
        )

    async def create(
        self,
        judgment: Judgment,
        *,
        perishable: bool = False,
        expires_at: str | None = None,
    ) -> Proposal:
        """Judgment 로부터 Proposal 을 만들고 DB 에 영속화한다.

        proposal_id 는 라우터가 부여한 uuid4. home_id 는 ScopedRepo 가 강제.
        """
        proposal = self.build(
            judgment, perishable=perishable, expires_at=expires_at
        )
        await self._persist(proposal)
        return proposal

    async def save(self, proposal: Proposal) -> Proposal:
        """이미 라우팅된 Proposal 을 그대로 영속화한다(파이프라인 산출물용).

        LiotApp 이 내부에서 Judgment→Proposal 까지 마쳐 반환하므로, 서비스는
        재라우팅 없이 이 메서드로 저장한다(이중 라우팅 방지).
        """
        await self._persist(proposal)
        return proposal

    async def _persist(self, proposal: Proposal) -> None:
        await self._repo.insert(
            self._db,
            "proposals",
            {
                "proposal_id": proposal.proposal_id,
                "judgment_id": proposal.judgment_id,
                "risk_tier": int(proposal.risk_tier),
                "action_json": proposal.action.model_dump_json(),
                "rationale": proposal.rationale,
                "required_confirmation": proposal.required_confirmation,
                "perishable": 1 if proposal.perishable else 0,
                "expires_at": proposal.expires_at,
                "state": proposal.state,
                "created_at": proposal.created_at,
            },
        )
        await self._audit(
            actor="L4",
            event_type="proposal_created",
            ref_id=proposal.proposal_id,
            trace_id=None,
            detail={"risk_tier": int(proposal.risk_tier), "state": proposal.state},
        )
        await self._db.commit()

    # ------------------------------------------------------------------
    # 조회
    # ------------------------------------------------------------------
    async def get(self, proposal_id: str) -> Proposal | None:
        rows = await self._repo.fetchall(
            self._db,
            "proposals",
            extra_where="proposal_id = ?",
            extra_params=(proposal_id,),
            limit=1,
        )
        if not rows:
            return None
        return self._row_to_proposal(rows[0])

    async def list_pending(self, limit: int | None = None) -> list[Proposal]:
        rows = await self._repo.fetchall(
            self._db,
            "proposals",
            extra_where="state = ?",
            extra_params=("pending",),
            order_by="created_at DESC",
            limit=limit,
        )
        return [self._row_to_proposal(r) for r in rows]

    # ------------------------------------------------------------------
    # 알림 기록 / 발화 컨텍스트 입력
    # ------------------------------------------------------------------
    async def record_notification(self, sanitized: dict[str, Any], *, now: str | None = None) -> None:
        """egress 를 통과한 *정제* 알림 payload 를 notifications 테이블에 기록한다.

        화이트리스트 키만 들어오므로 device/room/rationale 은 구조적으로 없다.
        """
        now = now or self._clock.now_utc()
        await self._repo.insert(
            self._db,
            "notifications",
            {
                "id": None,
                "proposal_id": str(sanitized.get("proposal_id", "")),
                "channel": str(sanitized.get("channel", "")),
                "risk_tier": int(sanitized.get("risk_tier", 0)),
                "glyph": str(sanitized.get("glyph", "")),
                "label": str(sanitized.get("label", "")),
                "signed_deeplink": str(sanitized.get("signed_deeplink", "")),
                "topic": str(sanitized.get("topic", "")),
                "sent_at": now,
            },
        )
        await self._db.commit()

    async def recently_rejected_keys(
        self, *, now: str | None = None, days: int = 30
    ) -> frozenset[str]:
        """최근 N일 내 dismissed 된 제안들의 dedup-key 집합(발화 억제 입력).

        notifier.gate_pipeline 의 recently_rejected_keys 로 그대로 넘긴다.
        """
        cutoff = (self._clock.now_dt() - timedelta(days=days)).replace(microsecond=0).isoformat()
        rows = await self._repo.fetchall(
            self._db,
            "proposals",
            columns="action_json, risk_tier",
            extra_where="state = ? AND created_at >= ?",
            extra_params=("dismissed", cutoff),
        )
        keys: set[str] = set()
        for r in rows:
            d = {k: r[k] for k in r.keys()} if _is_mapping(r) else None
            if d is None:  # pragma: no cover - row_factory 방어
                continue
            try:
                action = CandidateAction.model_validate_json(d["action_json"])
            except (ValueError, KeyError):  # pragma: no cover - 방어적
                continue
            keys.add(f"{action.capability}:{action.action}:{action.entity_id}:{action.category}")
        return frozenset(keys)

    # ------------------------------------------------------------------
    # 수용률(North Star) 메트릭
    # ------------------------------------------------------------------
    async def accept_rate(self) -> dict[str, Any]:
        """Proposal-Accept-Rate 를 출처(origin)별로 집계한다.

        PAR = accepted / (accepted + dismissed + expired) — 종료 상태에 도달한
        제안만 분모로(아직 pending 은 결과 미정이라 제외). origin 은
        action.params['__origin__'] 접두사(explicit/safety/proactive)로 버킷팅.
        """
        rows = await self._repo.fetchall(
            self._db, "proposals", columns="action_json, state"
        )
        buckets: dict[str, dict[str, int]] = {}
        for r in rows:
            d = {k: r[k] for k in r.keys()} if _is_mapping(r) else None
            if d is None:  # pragma: no cover
                continue
            state = d["state"]
            if state not in ("accepted", "dismissed", "expired"):
                continue
            origin = "unknown"
            try:
                action = CandidateAction.model_validate_json(d["action_json"])
                raw = str(action.params.get("__origin__", "unknown"))
                origin = raw.split(":", 1)[0] if ":" in raw else raw
            except (ValueError, KeyError):  # pragma: no cover
                pass
            b = buckets.setdefault(origin, {"accepted": 0, "dismissed": 0, "expired": 0})
            b[state] += 1
        out: dict[str, Any] = {}
        for origin, b in buckets.items():
            total = b["accepted"] + b["dismissed"] + b["expired"]
            out[origin] = {
                **b,
                "total": total,
                "accept_rate": (b["accepted"] / total) if total else None,
            }
        return out

    # ------------------------------------------------------------------
    # 만료
    # ------------------------------------------------------------------
    async def expire_due(self, now: str | None = None) -> int:
        """expires_at 이 지난 pending 제안들을 'expired' 로 전이한다.

        반환: 만료 처리된 제안 수.
        """
        now = now or self._clock.now_utc()
        rows = await self._repo.fetchall(
            self._db,
            "proposals",
            columns="proposal_id, expires_at",
            extra_where="state = ? AND expires_at IS NOT NULL AND expires_at <= ?",
            extra_params=("pending", now),
        )
        count = 0
        for r in rows:
            pid = r["proposal_id"] if _is_mapping(r) else r[0]
            await self._set_state(pid, "expired", trace_id=None)
            count += 1
        if count:
            await self._db.commit()
        return count

    # ------------------------------------------------------------------
    # 피드백
    # ------------------------------------------------------------------
    async def apply_feedback(self, feedback: FeedbackEvent) -> Proposal | None:
        """사용자 피드백을 기록하고 제안 상태를 전이한다.

        - feedback 행을 append 한다.
        - user_action 에 따라 제안 상태를 전이(FEEDBACK_STATE_MAP).
        - 알 수 없는 user_action 이면 InvariantError.
        반환: 갱신된 Proposal(없으면 None).
        """
        action_key = (feedback.user_action or "").strip().lower()
        if action_key not in FEEDBACK_STATE_MAP:
            raise InvariantError(f"unknown feedback user_action: {feedback.user_action!r}")
        new_state = FEEDBACK_STATE_MAP[action_key]

        # 피드백 기록(append).
        await self._repo.insert(
            self._db,
            "feedback",
            {
                "id": None,
                "proposal_id": feedback.proposal_id,
                "trace_id": feedback.trace_id,
                "user_action": feedback.user_action,
                "stop_scope": feedback.stop_scope,
                "comment": feedback.comment,
                "latency_ms": feedback.latency_ms,
                "created_at": self._clock.now_utc(),
            },
        )
        await self._audit(
            actor="USER",
            event_type="feedback",
            ref_id=feedback.proposal_id,
            trace_id=feedback.trace_id,
            detail={
                "user_action": feedback.user_action,
                "stop_scope": feedback.stop_scope,
            },
        )

        await self._set_state(
            feedback.proposal_id, new_state, trace_id=feedback.trace_id
        )
        await self._db.commit()
        return await self.get(feedback.proposal_id)

    async def set_state(self, proposal_id: str, state: str) -> None:
        """제안 상태를 명시적으로 전이(예: superseded)."""
        await self._set_state(proposal_id, state, trace_id=None)
        await self._db.commit()

    async def _set_state(
        self, proposal_id: str, state: str, *, trace_id: str | None
    ) -> None:
        await self._db.execute(
            "UPDATE proposals SET state = ? "
            + self._repo.scope_where("proposal_id = ?"),
            (state, *self._repo.scope_params(proposal_id)),
        )
        await self._audit(
            actor="L4",
            event_type="proposal_state",
            ref_id=proposal_id,
            trace_id=trace_id,
            detail={"state": state},
        )

    # ------------------------------------------------------------------
    # 감사 로그
    # ------------------------------------------------------------------
    async def _audit(
        self,
        *,
        actor: str,
        event_type: str,
        ref_id: str | None,
        trace_id: str | None,
        detail: dict[str, Any] | None = None,
    ) -> None:
        import json

        await self._repo.insert(
            self._db,
            "audit_log",
            {
                "id": None,
                "trace_id": trace_id,
                "actor": actor,
                "event_type": event_type,
                "ref_id": ref_id,
                "detail": json.dumps(detail, ensure_ascii=False) if detail else None,
                "ts": self._clock.now_utc(),
            },
        )

    # ------------------------------------------------------------------
    # 행 -> 모델
    # ------------------------------------------------------------------
    @staticmethod
    def _row_to_proposal(row: Any) -> Proposal:
        if _is_mapping(row):
            d = {k: row[k] for k in row.keys()}
        else:  # pragma: no cover - row_factory 가 Row 가 아닐 때 방어
            raise InvariantError("expected aiosqlite.Row mapping for proposals")
        action = CandidateAction.model_validate_json(d["action_json"])
        return Proposal(
            proposal_id=d["proposal_id"],
            judgment_id=d["judgment_id"],
            home_id=d["home_id"],
            risk_tier=int(d["risk_tier"]),
            action=action,
            rationale=d["rationale"],
            required_confirmation=d["required_confirmation"],
            perishable=bool(d["perishable"]),
            expires_at=d["expires_at"],
            state=d["state"],
            created_at=d["created_at"],
        )


def _is_mapping(row: Any) -> bool:
    """aiosqlite.Row 처럼 keys() 로 컬럼 접근이 가능한지."""
    return hasattr(row, "keys")
