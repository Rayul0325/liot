"""제안(Proposal) 수명주기 서비스.

Proposal 의 생성/영속화/만료/상태전이/피드백을 담당한다. 테넌시 격리는
ScopedRepo 로 강제하며, 모든 쿼리는 home_id 정확 일치로만 동작한다.

Phase1 불변식: 실행 코드 없음. 이 서비스는 제안을 만들고 그 상태를
관리할 뿐, 어떤 액추에이터도 호출하지 않는다.

상태(state): pending / accepted / dismissed / expired / superseded.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import timedelta
from typing import TYPE_CHECKING, Any

from liot.core.clock import Clock
from liot.core.contracts import CandidateAction, FeedbackEvent, Judgment, Proposal
from liot.core.invariants import InvariantError
from liot.core.risk import RiskTier
from liot.core.scoped_repo import DEFAULT_HOME_ID, ScopedRepo
from liot.core.severity import SeverityLevel

if TYPE_CHECKING:  # pragma: no cover
    import aiosqlite

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
        db: aiosqlite.Connection,
        home_id: str = DEFAULT_HOME_ID,
        clock: Clock | None = None,
        router: RiskRouter | None = None,
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
                # 파급도·상황 id(마이그레이션 3). 없으면 NULL.
                "severity": None if proposal.severity is None else int(proposal.severity),
                "situation_id": proposal.situation_id,
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

    async def states_for(self, proposal_ids: Sequence[str]) -> dict[str, tuple[str, str | None]]:
        """여러 제안의 (상태, expires_at)을 한 번의 SELECT 로 읽는다.

        없는 id 는 결과에 없다. 1회용 토큰 스윕이 토큰 하나마다 get() 을 돌리면
        틱마다 N 번 조회가 된다(2026-09-01 리뷰 R2-21: 토큰 300개에 37ms). 상태와
        만료시각만 필요하므로 행 전체를 Proposal 로 빚지 않는다. expires_at 도
        같이 주는 이유: 이중확인 2차 링크는 상태가 accepted 라 상태만으로는
        만료를 알 수 없고(expire_due 는 pending 만 전이한다), 그러면 링크에
        상한이 없어진다. SQLite 바인딩 상한(기본 999)을 넘지 않게 끊어 묻는다.
        """
        ids = [str(p) for p in proposal_ids]
        if not ids:
            return {}
        out: dict[str, tuple[str, str | None]] = {}
        for start in range(0, len(ids), 500):
            chunk = ids[start : start + 500]
            placeholders = ",".join("?" for _ in chunk)
            rows = await self._repo.fetchall(
                self._db,
                "proposals",
                columns="proposal_id, state, expires_at",
                extra_where=f"proposal_id IN ({placeholders})",
                extra_params=tuple(chunk),
            )
            for row in rows:
                # row_factory 가 Row 가 아닐 때도 읽히게 — 같은 파일의 expire_due 가
                # 쓰는 방식과 맞춘다(내부 일관성, 리뷰 R-6).
                if _is_mapping(row):
                    pid_val, state_val, expires = (
                        row["proposal_id"], row["state"], row["expires_at"]
                    )
                else:  # pragma: no cover - row_factory 가 Row 가 아닐 때 방어
                    pid_val, state_val, expires = row[0], row[1], row[2]
                out[str(pid_val)] = (
                    str(state_val or ""),
                    None if expires is None else str(expires),
                )
        return out

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
            # sqlite3.Row 는 그냥 반복하면 컬럼명이 아니라 '값'이 나온다 → .keys() 필수
            d = {k: r[k] for k in r.keys()} if _is_mapping(r) else None  # noqa: SIM118
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
            # sqlite3.Row 는 그냥 반복하면 컬럼명이 아니라 '값'이 나온다 → .keys() 필수
            d = {k: r[k] for k in r.keys()} if _is_mapping(r) else None  # noqa: SIM118
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

    async def expire_situation(self, situation_id: str) -> list[str]:
        """그 상황(situation_id)의 pending 제안을 모두 'expired' 로 전이한다.

        상황이 닫히면(센서 정상 복귀) 그 상황에서 나온 대기 제안 — 상황 알림과
        원탭 조치(밸브 잠그기·현관 잠그기) — 의 링크가 더는 통하면 안 된다. 기존
        만료 상태를 그대로 쓴다(새 상태 없음). 이미 결론 난 제안(accepted 등)은
        건드리지 않는다. 반환: 만료 처리된 proposal_id 목록.
        """
        rows = await self._repo.fetchall(
            self._db,
            "proposals",
            columns="proposal_id",
            extra_where="state = ? AND situation_id = ?",
            extra_params=("pending", situation_id),
        )
        expired: list[str] = []
        for r in rows:
            pid = str(r["proposal_id"] if _is_mapping(r) else r[0])
            await self._set_state(pid, "expired", trace_id=None)
            expired.append(pid)
        if expired:
            await self._db.commit()
        return expired

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

        # 존재·스코프 먼저 확인(home_id 정확 일치). 유령/타 홈 id 면 feedback·
        # audit 행을 남기지 않고 None 반환(테이블 오염 방지).
        existing = await self.get(feedback.proposal_id)
        if existing is None:
            return None

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
            # sqlite3.Row 는 그냥 반복하면 컬럼명이 아니라 '값'이 나온다 → .keys() 필수
            d = {k: row[k] for k in row.keys()}  # noqa: SIM118
        else:  # pragma: no cover - row_factory 가 Row 가 아닐 때 방어
            raise InvariantError("expected aiosqlite.Row mapping for proposals")
        action = CandidateAction.model_validate_json(d["action_json"])
        raw_severity = d.get("severity")
        return Proposal(
            proposal_id=d["proposal_id"],
            judgment_id=d["judgment_id"],
            home_id=d["home_id"],
            risk_tier=RiskTier.from_str(d["risk_tier"]),
            action=action,
            rationale=d["rationale"],
            required_confirmation=d["required_confirmation"],
            perishable=bool(d["perishable"]),
            expires_at=d["expires_at"],
            state=d["state"],
            created_at=d["created_at"],
            severity=None if raw_severity is None else SeverityLevel.from_str(int(raw_severity)),
            situation_id=d.get("situation_id"),
        )


def _is_mapping(row: Any) -> bool:
    """aiosqlite.Row 처럼 keys() 로 컬럼 접근이 가능한지."""
    return hasattr(row, "keys")
