"""LiotService — 파이프라인을 실제 세계에 잇는 "몸통"(오케스트레이션 + I/O).

LiotApp(두뇌, 네트워크 없음·결정만)을 둘러싸고 부수효과를 모은다:
  - **영속**: 모든 Proposal 을 DB(ProposalService)에 저장. 재시작에도 상태 보존.
  - **알림**: Notifier.gate_pipeline 로 발화 결정 → IMMEDIATE 면 정제 payload 를
    NtfyTransport 로 *실제 송신*. transport 는 egress 를 통과한 정제분만 받는다.
    (Notifier/egress 는 여전히 네트워크를 만지지 않는다 — 송신은 오직 여기서.)
  - **만료**: 주기적으로 expire_due() 로 신선도 지난 제안을 정리.
  - **틱**: 주기적 app.tick() 으로 능동 제안(AI 가 먼저)을 굴린다.
  - **피드백**: 서명 검증 HTTP 수신구로 수락/거절 → 상태 전이 → 수용률 학습.

불변식 보존:
  - 실행 없음(불변식 3): 여기서 하는 외부 I/O 는 *알림 송신* 뿐, 액추에이터
    호출이 아니다. HA 로는 읽기(stream)만, 쓰기는 없다.
  - egress 단일 통과(SE-D3): transport 는 emit_external(NTFY) 산출물만 받는다.
  - fail-closed: transport 송신 실패는 파이프라인을 죽이지 않고 '미전송'으로
    기록한다(제안은 pending 으로 남아 다음에 재시도/다이제스트 가능).
"""

from __future__ import annotations

import asyncio
import json
import secrets
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Any

from liot.app import LiotApp
from liot.core.clock import Clock
from liot.core.scoped_repo import ScopedRepo
from liot.layers.l2_context.feature_vector import (
    apply_decay,
    apply_signal,
    cell_key,
    load_cells,
    save_cells,
)
from liot.layers.l2_context.feedback_store import InMemoryFeedbackStore
from liot.layers.l2_context.preference_store import PreferenceStore
from liot.layers.l4_execution.executor import (
    ExecDecision,
    ExecutionDenied,
    ExecutionError,
    ExecutionPolicy,
    Executor,
)
from liot.layers.l4_execution.feedback_gateway import (
    FeedbackAuthError,
    FeedbackGateway,
    compare_token,
    verify_feedback_sig,
)
from liot.layers.l4_execution.notifier import (
    NotificationBudget,
    NotificationContext,
    Notifier,
    dedup_key,
)
from liot.layers.l4_execution.ntfy_transport import NtfyError, NtfyTransport
from liot.layers.l4_execution.proposal_service import ProposalService

if TYPE_CHECKING:  # pragma: no cover
    import aiosqlite

    from liot.core.contracts import Proposal, SensorReading
    from liot.layers.l1_sensing.ha_bridge import HABridge

__all__ = ["DEV_SIGNING_SECRET", "DispatchResult", "LiotService"]

_DEDUP_WINDOW = timedelta(hours=2)

# 이중확인 2차 링크(와 2차 미발송 재시도 링크)의 수명 상한. 사람이 방금 누른
# 버튼에 대한 후속 확인이라 짧아야 한다: 길면 캡처된 링크가 그만큼 오래 유효하고,
# 없으면(예전 판) 상태가 accepted 로 고정돼 영원히 유효해진다. 짧아도 안 된다 —
# 0 에 가까우면 R2-01 처럼 다음 틱에 죽어 안전 행동이 2/2 에 닿지 못한다.
# comfort 규칙의 기본 ttl(15분)과 같은 크기로 둔다(운영에서 조정 가능한 기본값).
_SECOND_CONFIRM_WINDOW = timedelta(minutes=15)

# 개발·테스트 전용 서명 키. 소스에 있으므로 **비밀이 아니다** — 이 값으로 서명된
# 딥링크는 저장소를 읽은 누구나 위조할 수 있다. 배포 진입점(liot.serve)은 피드백
# 수신구가 외부에서 닿는 구성일 때 이 값을 거부한다(fail-closed).
DEV_SIGNING_SECRET: bytes = b"liot-dev-signing-key"


class DispatchResult:
    """단일 제안 처리 결과(관측·테스트용)."""

    __slots__ = ("proposal", "decision", "sent", "error", "executed")

    def __init__(
        self,
        proposal: Proposal,
        decision: str,
        sent: bool,
        error: str | None = None,
        executed: bool = False,
    ) -> None:
        self.proposal = proposal
        self.decision = decision  # IMMEDIATE | DIGEST | SUPPRESS
        self.sent = sent
        self.error = error
        self.executed = executed  # 이 디스패치에서 실제 조작이 일어났는가(AUTO)

    def __repr__(self) -> str:  # pragma: no cover - 디버그 편의
        return (
            f"DispatchResult(decision={self.decision!r}, sent={self.sent}, "
            f"executed={self.executed}, error={self.error!r})"
        )


class LiotService:
    """파이프라인 + 영속 + 알림 + 만료 + 피드백을 잇는 상시 서비스."""

    def __init__(
        self,
        app: LiotApp,
        db: aiosqlite.Connection,
        *,
        home_id: str = "singleton",
        clock: Clock | None = None,
        notifier: Notifier | None = None,
        transport: NtfyTransport | None = None,
        topic: str = "liot",
        secret: bytes | str = DEV_SIGNING_SECRET,
        budget_per_hour: int = 6,
        executor: Executor | None = None,
        policy: ExecutionPolicy | None = None,
    ) -> None:
        self.app = app
        self.clock = clock or app.clock
        self.home_id = home_id
        self._db = db
        self._repo = ScopedRepo(home_id)
        self.proposals = ProposalService(db, home_id=home_id, clock=self.clock)
        self.prefs = PreferenceStore(home_id, db=db)
        # 알림 서명과 피드백 검증은 동일 비밀키를 써야 한다. notifier 를 직접
        # 주입하면 그 비밀키가 secret 인자와 일치해야 피드백 버튼이 검증된다.
        self.notifier = notifier or Notifier(secret=secret, topic=topic, clock=self.clock)
        self.transport = transport
        self.topic = topic
        self._secret = secret.encode("utf-8") if isinstance(secret, str) else secret
        self.feedback = FeedbackGateway(secret, self.proposals)
        # 발화 빈도 상태(인메모리; NotificationBudget 과 동일 성격).
        self._budget = NotificationBudget(max_per_hour=budget_per_hour)
        self._budget_hour: str | None = None
        self._recent_notified: dict[str, datetime] = {}
        # 조작(불변식 8): executor 없거나 policy 비활성이면 제안-만(현행) 유지.
        self._executor = executor
        self._policy = policy or ExecutionPolicy(enabled=False)
        self._prior: dict[str, dict[str, Any]] = {}  # pid → 실행 전 상태(되돌리기)
        self._awaiting_second: set[str] = set()  # 이중확인 1차 통과 대기 pid
        # 이중확인 1차는 통과했지만 2차 알림이 나가지 못한 pid. 이 제안에선 1차
        # 링크가 되살아나 있고, 그 링크는 발송 재시도만 할 수 있다(실행 불가).
        self._second_undelivered: set[str] = set()
        # 피드백 링크 1회용 토큰: proposal_id → 지금 유효한 nonce.
        # 서명만으로는 링크가 영구 재사용 가능한 권한이 된다 — 캡처한 수락 링크를
        # 두 번 재생하는 것만으로 EMBER 의 DOUBLE_CONFIRM(1차 보류 → 2차 실행)이
        # 충족됐다. 성공한 피드백마다 여기서 nonce 를 지워 링크를 1회용으로 만들고,
        # 2차 확인은 새 nonce 로 발급된 별도 링크를 요구한다. 예외는 둘 — 제안을
        # pending 으로 남기는 피드백(스누즈)과, 2차 알림이 나가지 못한 1차 수락.
        # 둘 다 사람 손에 다른 링크가 없어서, 소비하면 흐름이 끊긴다.
        self._nonce: dict[str, str] = {}
        # nonce 발급 시각(pid → datetime). 이중확인 2차 링크의 수명 상한을 재는 데
        # 쓴다 — 상태(accepted)만으로는 그 링크가 언제 죽어야 하는지 알 수 없다.
        self._nonce_issued: dict[str, datetime] = {}
        # 개인화 루프(Phase3): 제안 → 선호 셀 키(맥락+행동), 신호 중복 방지.
        self._prop_cell: dict[str, str] = {}  # proposal_id → cell_key
        self._pref_signaled: set[str] = set()  # 이미 신호 반영된 proposal_id

    # ------------------------------------------------------------------
    # 발화 컨텍스트
    # ------------------------------------------------------------------
    def _roll_budget(self, now_dt: datetime) -> None:
        """시(hour)가 바뀌면 시간당 예산을 리셋한다."""
        hour_key = now_dt.strftime("%Y-%m-%dT%H")
        if hour_key != self._budget_hour:
            self._budget_hour = hour_key
            self._budget.used_this_hour = 0

    def _recent_notified_keys(self, now_dt: datetime) -> frozenset[str]:
        """2시간 창 내 이미 발화한 dedup-key 집합(만료분 정리)."""
        cutoff = now_dt - _DEDUP_WINDOW
        self._recent_notified = {
            k: ts for k, ts in self._recent_notified.items() if ts >= cutoff
        }
        return frozenset(self._recent_notified)

    async def _build_context(self, now_dt: datetime) -> NotificationContext:
        charter = await self.prefs.get("charter_enabled", True)
        muted_cats = await self.prefs.get("muted_categories", [])
        muted_ents = await self.prefs.get("muted_entities", [])
        rejected = await self.proposals.recently_rejected_keys()
        return NotificationContext(
            charter_enabled=bool(charter),
            muted_categories=frozenset(muted_cats or []),
            muted_entities=frozenset(muted_ents or []),
            recently_rejected_keys=rejected,
            recent_notified_keys=self._recent_notified_keys(now_dt),
            quiet_hours=(self.clock.time_bucket() == "night"),
        )

    # ------------------------------------------------------------------
    # 디스패치(영속 + 발화 + 송신)
    # ------------------------------------------------------------------
    async def dispatch(self, proposal: Proposal) -> DispatchResult:
        """제안을 영속화하고 발화 게이트를 적용해 (가능하면) 실제 송신한다.

        조작이 활성(executor+policy)이면, 정책이 AUTO(비안전 ≤BREATH)로 판정한
        제안은 알림 게이트를 통과(IMMEDIATE)할 때 *즉시 실행*한다. DUSK/EMBER/
        안전은 여기서 실행하지 않고 알림만 — 실행은 사람 승인(피드백) 후로 미룬다.
        """
        await self.proposals.save(proposal)

        now_dt = self.clock.now_dt()
        self._roll_budget(now_dt)
        ctx = await self._build_context(now_dt)
        nonce = self._issue_nonce(proposal.proposal_id)
        decision, sanitized = self.notifier.notify(
            proposal, ctx, self._budget, nonce=nonce
        )
        exec_decision = self._policy.decide(proposal.action, proposal.risk_tier)

        if decision != "IMMEDIATE" or sanitized is None:
            return DispatchResult(proposal, decision, sent=False)

        # AUTO: 알림 게이트를 통과했고(=억제/뮤트/거절 아님) 정책이 자동이면 즉시 실행.
        executed = False
        if exec_decision is ExecDecision.AUTO and self._executor is not None:
            executed = await self._try_execute(proposal)

        if self.transport is None:
            # sanitize-only 모드: 실제 배달은 없다. dedup 을 오염시키지 않는다
            # (그래야 transport 가 붙은 뒤 같은 류가 억제되지 않는다).
            await self.proposals.record_notification(sanitized, now=self.clock.now_utc())
            return DispatchResult(proposal, decision, sent=False, executed=executed)

        # 실제 송신을 *먼저* 시도. 성공해야만 알림 기록 + dedup 갱신한다.
        # 실패 시 dedup 을 오염시키지 않아야 같은 류(특히 안전 EMBER)가 2시간
        # 동안 SUPPRESS 되지 않는다(재시도 가능 + Safety-Never-Silent 정신 보존).
        try:
            self.transport.send(self.topic, sanitized)
        except NtfyError as exc:
            # 송신 실패는 치명적이지 않다 — 제안은 pending 으로 남아 재시도 가능.
            return DispatchResult(proposal, decision, sent=False, error=str(exc), executed=executed)
        await self.proposals.record_notification(sanitized, now=self.clock.now_utc())
        self._recent_notified[dedup_key(proposal)] = now_dt
        return DispatchResult(proposal, decision, sent=True, executed=executed)

    # ------------------------------------------------------------------
    # 조작(불변식 8) — fail-closed: 실행 실패는 파이프라인을 죽이지 않는다.
    # ------------------------------------------------------------------
    async def _try_execute(self, proposal: Proposal) -> bool:
        """executor 로 실제 조작. 이전 상태를 캡처(되돌리기용). 실패는 False."""
        if self._executor is None:
            return False
        try:
            res = await self._executor.execute(proposal.action)
        except (ExecutionDenied, ExecutionError):
            return False
        if res.prior_state is not None:
            self._prior[proposal.proposal_id] = res.prior_state
        return res.ok

    def _issue_nonce(self, proposal_id: str) -> str:
        """제안에 새 1회용 피드백 토큰을 발급한다(이전 토큰은 즉시 무효)."""
        nonce = secrets.token_hex(16)
        self._nonce[proposal_id] = nonce
        self._nonce_issued[proposal_id] = self.clock.now_dt()
        return nonce

    def _drop_nonce(self, proposal_id: str) -> None:
        """죽은 토큰과 그에 딸린 대기 표식을 한 덩어리로 버린다.

        따로 지우면 남는 쪽이 새는 상태가 된다 — _awaiting_second 가 남으면
        '2차 대기'라는 이유로 다음에 발급되는 토큰이 창 없이 살아나고,
        _nonce_issued 가 남으면 맵이 무한히 자란다.
        """
        self._nonce.pop(proposal_id, None)
        self._nonce_issued.pop(proposal_id, None)
        self._awaiting_second.discard(proposal_id)
        self._second_undelivered.discard(proposal_id)

    async def _request_second_confirmation(self, proposal: Proposal) -> bool:
        """이중확인 2차용 **새 링크**를 발급해 다시 알린다.

        1차 링크는 이미 소비됐으므로, 같은 링크를 다시 눌러도(=재생해도) 2차가
        충족되지 않는다 — 두 번째 확인은 새 nonce 를 가진 별도 알림에서만 온다.

        발화 게이트·예산은 거치지 않는다. 그건 '먼저 말 거는' 알림을 위한
        장치인데, 이건 사람이 방금 누른 버튼에 대한 직접 응답이라 억제되면
        확인 흐름이 끊긴다. egress 는 그대로 통과한다(정제 보장 불변).

        반환: 2차 링크가 배달 경로에 실렸는가. 송신 실패(NtfyError)면 False —
        예전엔 이걸 삼키고도 '다시 확인하세요'라고 답해 흐름이 소리 없이
        끝났다(R2-17). 실패의 처리(알림·재시도 경로)는 호출자 _on_approved 몫이다.
        """
        nonce = self._issue_nonce(proposal.proposal_id)
        if self.transport is None:
            return True  # sanitize-only: 배달 경로 자체가 없다(링크도 없다) — 기존 동작
        sanitized = self.notifier.sanitize(proposal, nonce)
        try:
            self.transport.send(self.topic, sanitized)
        except NtfyError:
            return False
        return True

    async def _on_approved(self, proposal: Proposal, *, used_nonce: str) -> str:
        """승인(accepted) 피드백 후 정책에 따라 실행한다.

        CONFIRM(DUSK) → 즉시 실행. DOUBLE_CONFIRM(EMBER/안전) → 1차는 보류하고
        한 번 더 확인 요청, 2차에서 실행. AUTO 는 이미 dispatch 에서 실행됨.

        used_nonce: 방금 소비된 링크의 nonce. 2차 알림이 나가지 못했을 때 그
        링크를 되살리는 데만 쓴다.
        """
        if self._executor is None:
            return ""
        decision = self._policy.decide(proposal.action, proposal.risk_tier)
        if decision is ExecDecision.CONFIRM:
            return " · executed" if await self._try_execute(proposal) else " · exec-failed"
        if decision is ExecDecision.DOUBLE_CONFIRM:
            pid = proposal.proposal_id
            if pid in self._awaiting_second:
                self._awaiting_second.discard(pid)
                return (
                    " · executed(2/2)" if await self._try_execute(proposal) else " · exec-failed"
                )
            # 표식을 **먼저** 세운다. 발급(_issue_nonce)과 표식 사이에 스윕이 끼면
            # 표식 없는 nonce 를 죽은 것으로 보고 버려서 R2-01 이 되살아난다. 지금은
            # 그 사이에 await 양보가 없어(transport.send 가 동기) 실제로 끼어들 수
            # 없지만, 비동기 transport 로 바뀌는 순간 창이 열린다(리뷰 지적).
            # 발송 실패 시에는 아래에서 다시 내린다 — 실행 권한은 발송 성공에만 준다.
            self._awaiting_second.add(pid)
            if await self._request_second_confirmation(proposal):
                self._second_undelivered.discard(pid)
                return " · confirm-again(1/2)"
            self._awaiting_second.discard(pid)
            # 2차 알림이 나가지 못했다 — 사람 손엔 누를 2차 링크가 없다. 방금 누른
            # 1차 링크를 되살려, 같은 버튼을 다시 누르면 발송을 재시도하게 한다.
            # 이 링크로는 실행되지 않는다: 실행은 _awaiting_second 를 요구하고, 그건
            # 2차 링크가 실제로 나간 뒤에만 채워진다 — 그때 새 nonce 가 이 링크를
            # 대체하므로 1차 링크 재생으로 2/2 를 채우는 길은 여전히 없다.
            self._nonce[pid] = used_nonce
            self._nonce_issued[pid] = self.clock.now_dt()  # 되살린 링크에도 같은 상한
            self._second_undelivered.add(pid)
            return " · second-confirm-not-sent(retry)"
        return ""

    async def _handle_undo(self, pid: str, tier: str, sig: str) -> tuple[str, str]:
        """서명된 되돌리기. 직전 실행의 이전 상태로 복원한다.

        되돌리기 서명은 **nonce 를 쓰지 않는다**(대상 = pid:tier:undo:"").
        이유: (a) 되돌리기 링크는 알림에 실려 나가지 않아 비밀키 보유자만
        만들 수 있고, (b) 성공 시 self._prior 에서 이전 상태를 꺼내 지우므로
        같은 링크를 다시 던져도 되돌릴 것이 없다 — 이미 1회용이다.
        """
        if self._executor is None:
            return "404 Not Found", "undo unavailable"
        if not verify_feedback_sig(self._secret, pid, tier, "undo", sig):
            return "403 Forbidden", "invalid signature"
        prior = self._prior.get(pid)
        proposal = await self.proposals.get(pid)
        if proposal is None or prior is None:
            return "404 Not Found", "nothing to undo"
        try:
            await self._executor.undo(proposal.action, prior)
        except (ExecutionDenied, ExecutionError):
            return "200 OK", "undo-failed"
        self._prior.pop(pid, None)
        return "200 OK", "undone"

    # ------------------------------------------------------------------
    # 진입점들
    # ------------------------------------------------------------------
    async def handle_reading(self, reading: SensorReading) -> DispatchResult | None:
        """단일 센서 읽기를 파이프라인에 흘리고, 제안이 나오면 디스패치."""
        proposal = await self.app.process_reading(reading)
        if proposal is None:
            return None
        return await self.dispatch(proposal)

    async def run_tick(self) -> DispatchResult | None:
        """능동 틱 1회 + 만료 스윕. 능동 제안이 나오면 디스패치.

        틱 전에 거절 이력·수용률 스냅샷을 평가기의 선호 신호로 주입하고,
        틱 후에 후보 평가 결과(무행동 선택 포함)를 candidate_evaluations 로
        영속한다 — 어떤 후보들이 점수화됐고 왜 그 선택이었는지가 남는다.
        """
        await self.proposals.expire_due()
        await self._apply_expired_signals()  # 만료 = 약한 비선호 신호
        await self._sweep_nonces()
        await self._refresh_feedback()
        await self._load_pref_cells()  # 영속 선호 셀 → 임베더(개인화 반영)
        proposal = await self.app.tick()
        await self._record_evaluation()
        if proposal is None:
            return None
        self._remember_proposal_cell(proposal)  # 해소 시 신호 반영용 맥락 기억
        return await self.dispatch(proposal)

    # ------------------------------------------------------------------
    # 개인화 루프(Phase3) — 알림 응답 → 선호 임베딩 갱신(유튜브식)
    # ------------------------------------------------------------------
    async def _load_pref_cells(self) -> None:
        """영속 선호 셀맵을 임베더에 로드한다(다음 틱 선택이 개인화)."""
        cells = await load_cells(self.prefs)
        self.app.set_preference_cells(cells)

    def _remember_proposal_cell(self, proposal: Proposal) -> None:
        """제안이 만들어진 맥락+행동의 셀 키를 기억한다(해소 시 신호 반영용)."""
        facts = self.app.last_facts
        if not facts:
            return
        self._prop_cell[proposal.proposal_id] = cell_key(
            str(facts.get("time_bucket", "")),
            str(facts.get("occupancy", "")),
            proposal.action,
        )

    async def _apply_pref_signal(self, proposal_id: str, signal: str) -> None:
        """해소된 제안의 셀에 알림 응답 신호를 온라인 EMA 로 반영·영속한다(1회만).

        갱신 시 다른 셀은 한 스텝 시간감쇠(유튜브식 망각). 임베딩은 evaluator
        의 PREF 항만 움직이며 safety_risk(catalog)는 불변이다.
        """
        key = self._prop_cell.get(proposal_id)
        if key is None or proposal_id in self._pref_signaled:
            return
        cells = await load_cells(self.prefs)
        cells = apply_decay(cells, steps=1, exclude=frozenset({key}))
        cells = apply_signal(cells, key, signal)
        await save_cells(self.prefs, cells, updated_at=self.clock.now_utc())
        self._pref_signaled.add(proposal_id)

    async def _apply_expired_signals(self) -> None:
        """방금 만료된(추적 중) 제안에 약한 비선호 신호를 반영한다."""
        for pid in list(self._prop_cell):
            if pid in self._pref_signaled:
                continue
            p = await self.proposals.get(pid)
            if p is not None and (p.state or "").lower() == "expired":
                await self._apply_pref_signal(pid, "expired")

    async def _sweep_nonces(self) -> None:
        """더 이상 쓸 수 없는 제안의 1회용 토큰을 버린다.

        상태를 직접 보는 이유: 선호 셀 추적(_prop_cell)은 tick 제안만 담는다
        (_remember_proposal_cell 이 app.last_facts 를 요구하는데 그건 tick 에서만
        채워진다). 거기에 정리를 얹으면 **이벤트 기반 제안**(가스 경보 등)의
        토큰이 영원히 남아 상시 데몬에서 _nonce 가 무한히 자란다.

        살아 있는 토큰의 기준은 _nonce_is_live 가 정한다.

        상태는 토큰 전체를 **한 번의 쿼리**로 묻는다(states_for). 토큰마다 get()
        을 돌리던 예전 판은 틱마다 N 번 조회였다(2026-09-01 리뷰 R2-21).
        """
        # 토큰이 있는 pid 에 더해 **표식만 남은 pid** 도 함께 돈다. 종결 피드백 경로는
        # 표식을 남길 수 있는데(R-13), nonce 기준으로만 돌면 그 pid 를 다시 볼 일이
        # 없어 표식이 영원히 쌓인다.
        pids = list(dict.fromkeys([*self._nonce, *self._awaiting_second, *self._second_undelivered]))
        if not pids:
            return
        rows = await self.proposals.states_for(pids)
        now_dt = self.clock.now_dt()
        now_iso = self.clock.now_utc()
        for pid in pids:
            # 없는 제안(rows 에 없음)은 상태 ''→ 살아 있지 않음으로 판정된다.
            state, expires_at = rows.get(pid, ("", None))
            if not self._nonce_is_live(
                pid, state, expires_at, now_dt=now_dt, now_iso=now_iso
            ):
                self._drop_nonce(pid)

    def _nonce_is_live(
        self,
        pid: str,
        state: str | None,
        expires_at: str | None = None,
        *,
        now_dt: datetime | None = None,
        now_iso: str | None = None,
    ) -> bool:
        """이 제안의 토큰이 아직 사람이 누를 링크에 실려 있을 수 있는가.

        세 경우만 살아 있다.
          - pending: 첫 알림의 링크(스누즈 뒤에도 같은 링크). 만료는 expire_due
            가 상태로 옮기므로 여기서 따로 보지 않는다.
          - accepted + 이중확인 2차 대기: 1차 수락이 상태를 이미 accepted 로
            옮긴 뒤 _request_second_confirmation 이 새로 발급한 2차 링크.
            상태만 보고 버리면 틱 한 번에 이 링크가 죽어 EMBER·안전 행동이
            영원히 2/2 에 닿지 못한다(2026-09-01 리뷰 R2-01).
          - accepted + 2차 알림 미발송: 발송 재시도용으로 되살린 1차 링크(R2-17).

        accepted 두 경우에는 **상한이 둘** 있다. 상태가 accepted 인 제안은
        expire_due 가 건드리지 않아서(pending 만 전이) 상태만으로는 절대 죽지
        않기 때문이다 — 그대로 두면 R2-01 수정이 '한 틱 뒤 사망'을 '영원히 유효'
        로 바꿔 놓는다.
          1. 제안 자신의 expires_at 이 지나면 죽는다.
          2. 발급 후 _SECOND_CONFIRM_WINDOW 가 지나면 죽는다(expires_at 이 없는
             제안에도 상한을 준다).
        판정 시각을 인자로 받는 이유: 스윕은 토큰 전체를 한 시각으로 보고,
        클릭 경로는 그 클릭 시각으로 본다.
        """
        st = (state or "").lower()
        if st == "pending":
            return True
        if st != "accepted":
            return False
        if pid not in self._awaiting_second and pid not in self._second_undelivered:
            return False
        now_iso = now_iso or self.clock.now_utc()
        # ISO 문자열 사전순 비교. 시스템이 만든 값은 전부 `...+00:00`(clock.now_utc·
        # app 의 expires_at)이라 시각 순서와 같고, expire_due 의 SQL 비교도 같은
        # 가정을 쓴다. 다른 오프셋이 섞인 값을 외부에서 주입하면 어긋난다.
        if expires_at is not None and str(expires_at) <= now_iso:
            return False  # 제안 자체가 만료 시각을 지났다
        issued = self._nonce_issued.get(pid)
        if issued is None:
            return False  # 발급 시각을 모르는 토큰은 상한을 걸 수 없다 → fail-closed
        return ((now_dt or self.clock.now_dt()) - issued) <= _SECOND_CONFIRM_WINDOW

    async def _refresh_feedback(self) -> None:
        """DB 의 거절/수용 이력을 동기 스냅샷으로 빚어 앱에 주입한다.

        L2 평가기는 동기·결정론을 유지해야 하므로(비동기 DB 조회 금지),
        비동기 조회는 여기(서비스)서 하고 결과만 넘긴다.
        """
        rejected = await self.proposals.recently_rejected_keys()
        rates = await self.proposals.accept_rate()
        self.app.feedback_store = InMemoryFeedbackStore(
            rejected_keys=rejected,
            accept_rate={
                origin: v["accept_rate"]
                for origin, v in rates.items()
                if v.get("accept_rate") is not None
            },
        )

    async def _record_evaluation(self) -> None:
        """직전 틱의 후보 평가를 candidate_evaluations 에 남긴다(연구/감사 로그)."""
        ev = self.app.last_candidate_evaluation
        if ev is None:
            return
        self.app.last_candidate_evaluation = None  # 중복 기록 방지(1틱 1행)
        evaluation = ev["evaluation"]
        await self._repo.insert(
            self._db,
            "candidate_evaluations",
            {
                "trace_id": ev["trace_id"],
                "snapshot_hash": ev["snapshot_hash"],
                "scored_json": json.dumps(
                    [a.model_dump() for a in evaluation.scored], ensure_ascii=False
                ),
                "selected_index": evaluation.selected_index,
                "rationale": evaluation.rationale,
                "created_at": self.clock.now_utc(),
            },
        )
        await self._db.commit()

    async def run(
        self,
        bridge: HABridge | None = None,
        *,
        tick_seconds: float = 300.0,
        max_events: int | None = None,
        max_ticks: int | None = None,
    ) -> None:
        """상시 데몬: HA 스트림 소비 + 주기 틱을 동시에 굴린다.

        bridge 가 None 이면 능동 틱만(센서 스트림 없이) 돈다. max_events/
        max_ticks 는 테스트용 경계(미지정 시 무한).
        """
        tasks = [asyncio.create_task(self._tick_loop(tick_seconds, max_ticks))]
        if bridge is not None:
            tasks.append(asyncio.create_task(self._stream_loop(bridge, max_events)))
        try:
            await asyncio.gather(*tasks)
        finally:
            for t in tasks:
                t.cancel()

    async def _stream_loop(self, bridge: HABridge, max_events: int | None) -> None:
        n = 0
        async for reading in bridge.stream():
            await self.handle_reading(reading)
            n += 1
            if max_events and n >= max_events:
                return

    async def _tick_loop(self, tick_seconds: float, max_ticks: int | None) -> None:
        n = 0
        while True:
            await self.run_tick()
            n += 1
            if max_ticks and n >= max_ticks:
                return
            await asyncio.sleep(tick_seconds)

    # ------------------------------------------------------------------
    # 피드백 HTTP 수신구(서명 검증). 같은 이벤트 루프·DB 공유.
    # ------------------------------------------------------------------
    async def serve_feedback(self, host: str = "127.0.0.1", port: int = 8099) -> asyncio.AbstractServer:
        """최소 HTTP GET /feedback?p=&t=&sig=&a= 수신구를 연다(무의존)."""
        return await asyncio.start_server(self._handle_feedback_conn, host, port)

    async def _handle_feedback_conn(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        try:
            line = await asyncio.wait_for(reader.readline(), timeout=5.0)
            request = line.decode("latin-1", "replace").strip()
            # "GET /feedback?p=..&t=..&sig=..&a=.. HTTP/1.1"
            status, body = await self._route_feedback(request)
        except (TimeoutError, ValueError):
            status, body = "400 Bad Request", "bad request"
        except Exception:  # pragma: no cover - 수신구는 절대 죽지 않는다
            status, body = "500 Internal Server Error", "error"
        payload = body.encode("utf-8")
        writer.write(
            f"HTTP/1.1 {status}\r\nContent-Type: text/plain; charset=utf-8\r\n"
            f"Content-Length: {len(payload)}\r\nConnection: close\r\n\r\n".encode("ascii")
            + payload
        )
        try:
            await writer.drain()
        finally:
            writer.close()

    async def _route_feedback(self, request_line: str) -> tuple[str, str]:
        from urllib.parse import parse_qs, urlsplit

        parts = request_line.split(" ")
        if len(parts) < 2 or parts[0] != "GET":
            return "405 Method Not Allowed", "GET only"
        split = urlsplit(parts[1])
        if split.path != "/feedback":
            return "404 Not Found", "not found"
        q = parse_qs(split.query)
        pid = q.get("p", [""])[0]
        tier = q.get("t", [""])[0]
        sig = q.get("sig", [""])[0]
        action = q.get("a", [""])[0]
        nonce = q.get("n", [""])[0]
        key = (action or "").strip().lower()

        # 되돌리기: 상태 피드백이 아니라 별도 서명 경로. 되돌리기는 self._prior
        # 를 소비하므로 이미 1회용이다(nonce 없이도 재생이 무의미).
        if key == "undo":
            return await self._handle_undo(pid, tier, sig)

        # 1회용 토큰 검사 — 서명이 유효해도 이미 쓴 링크면 거부한다. 이게 없으면
        # 캡처한 수락 링크가 영구 권한이 되고, 두 번 재생하는 것만으로 EMBER 의
        # 이중확인이 충족된다(1차 보류 → 2차 실행).
        expected = self._nonce.get(pid, "")
        if not expected or not compare_token(expected, nonce):
            return "403 Forbidden", "stale or already-used link"

        # 토큰이 맞아도 **수명**을 넘겼으면 거부한다. 스윕은 틱에만 도는데 클릭은
        # 언제든 오므로, 여기서도 같은 기준으로 본다 — 안 그러면 틱이 뜸한 동안
        # 2차 링크가 창·만료를 넘겨서도 통한다.
        current = await self.proposals.get(pid)
        if current is None:
            return "404 Not Found", "no such proposal"
        if not self._nonce_is_live(pid, current.state, current.expires_at):
            self._drop_nonce(pid)
            return "403 Forbidden", "expired link"

        try:
            proposal = await self.feedback.verify_and_apply(
                pid, tier, sig, action, nonce=nonce
            )
        except FeedbackAuthError:
            return "403 Forbidden", "invalid signature or action"
        if proposal is None:
            return "404 Not Found", "no such proposal"

        # 소비: 이 링크는 여기서 끝난다. 2차 확인이 필요하면 _on_approved 가
        # 새 nonce 를 발급해 별도 알림으로 내보낸다.
        # 예외 — 제안이 pending 으로 남는 피드백(스누즈)은 소비하지 않는다. 새
        # 링크를 내보내는 경로가 없어서, 소비하면 사람이 가진 버튼이 전부 403 이
        # 되고 제안은 pending 으로 영원히 남는다(R2-05). 그 링크를 재생해도 상태가
        # pending 그대로라 얻는 것이 없다.
        if (proposal.state or "").lower() != "pending":
            # _drop_nonce 를 쓰지 않는다: 그건 _awaiting_second 까지 비우는데,
            # 바로 아래 _on_approved 가 그 표식으로 2/2 를 판정한다.
            self._nonce.pop(pid, None)
            self._nonce_issued.pop(pid, None)
            self._second_undelivered.discard(pid)  # 재시도 링크도 여기서 끝(재발급은 아래)

        # 승인 시 정책에 따라 실제 조작(불변식 8). 거절/스누즈는 실행 없음.
        note = ""
        if key in ("accept", "accepted"):
            note = await self._on_approved(proposal, used_nonce=nonce)
        # 종결 피드백(거절·스누즈 아님)이면 2차 대기 표식을 버린다. 위에서
        # _drop_nonce 를 못 쓰는 사정(바로 위 _on_approved 가 표식을 읽는다) 때문에
        # 이 표식만 남고, 스윕은 nonce 가 있는 pid 만 도니 영원히 안 지워진다 —
        # pid 문자열이 프로세스 수명만큼 쌓인다(2026-09-13 리뷰 R-13).
        # accepted 는 제외: 1차 수락 직후의 '2차 대기'가 바로 그 상태다.
        st = (proposal.state or "").lower()
        if st not in ("pending", "accepted"):
            self._awaiting_second.discard(pid)

        # 개인화: 수락/거부를 선호 셀에 반영(유튜브식 온라인 갱신).
        if st == "accepted":
            await self._apply_pref_signal(proposal.proposal_id, "accepted")
        elif st in ("dismissed", "rejected"):
            await self._apply_pref_signal(proposal.proposal_id, "dismissed")
        return "200 OK", f"ok: {proposal.state}{note}"
