"""LiotService — 파이프라인을 실제 세계에 잇는 "몸통"(오케스트레이션 + I/O).

LiotApp(두뇌, 네트워크 없음·결정만)을 둘러싸고 부수효과를 모은다:
  - **영속**: 모든 Proposal 을 DB(ProposalService)에 저장. 재시작에도 상태 보존.
  - **알림**: Notifier.gate_pipeline 로 발화 결정 → IMMEDIATE 면 정제 payload 를
    NtfyTransport 로 *실제 송신*. transport 는 egress 를 통과한 정제분만 받는다.
    (Notifier/egress 는 여전히 네트워크를 만지지 않는다 — 송신은 오직 여기서.)
  - **만료**: 주기적으로 expire_due() 로 신선도 지난 제안을 정리.
  - **틱**: 주기적 app.tick() 으로 능동 제안(AI 가 먼저)을 굴린다.
  - **피드백**: 서명 검증 HTTP 수신구로 수락/거절 → 상태 전이 → 수용률 학습.
  - **상황 에피소드(spec §3.4.1·§6.1)**: 안전 센서의 이상 신호는 (집, 카테고리,
    엔티티) 에피소드를 열거나 잇고, 정상 신호는 닫는다. 새 에피소드만 상황 알림을
    만든다. 닫힐 때는 새 `alert` 없이 거주자에게 '해소됨'(NOTICE)을 보내고, 그
    상황의 대기 제안(원탭 링크 포함)을 만료시킨다.
  - **표 우선 두 단계 발송(spec §6.2)**: 상황 알림은 Gate1+파급도 하한으로 *먼저*
    보내고, 결정 모델 판정은 그 뒤 별도 스레드에서 제한시간 안에 돈다. 2단계는
    추적되는 백그라운드 태스크라 handle_reading 은 1단계 발송 직후 돌아온다 —
    다음 센서 신호(다른 안전 상황)가 앞 상황의 모델 판정 뒤에 줄 서지 않는다.
    모델이 올렸을 때만 같은 situation_id 의 후속 제안을 보낸다. 판단 행은 항상 저장한다.
  - **판단 저장**: 판단마다 judgments 에 한 행(SafetyRepo.save_judgment).
  - **에스컬레이션 사다리(T8)**: 상황마다 사다리 하나(escalation_ladders 에 영속).
    300초 틱과 별개인 사다리 루프(≤5초 해상도)가 DB 의 next_fire_at 을 보고 재알림·
    보호자 전달·119 안내를 낸다. 재알림 주기는 최종 파급도, 보호자·119 는 상황
    하한(표+거주자)이 CRITICAL 이고 동의가 유효할 때만. 거주자의 명시 응답이나 보호자의
    서명 링크가 사다리를 멈추고, 상황이 닫히면 취소 + 전달받은 보호자에게 '해소됨'.
    시작 시 열린 사다리를 DB 에서 다시 무장한다. 119 는 안내 문구뿐 — 자동 신고 없음.
  - **감시 불가(판정 보류)**: 안전 센서 값이 HA 의 unavailable·unknown 이면 위험이
    아니라 '읽지 못함'이다. 상황을 열지도 닫지도 않고 사다리·보호자 경로도 건드리지
    않는다. 거주자에게만 '<센서 종류> 센서 · 감시 불가(연결 끊김)'(주의)를 한 번 보낸다
    (엔티티별 메모리 표식, 정상·이상 값이 오면 지운다).

불변식 보존:
  - 실행 없음(불변식 3): 여기서 하는 외부 I/O 는 *알림 송신* 뿐, 액추에이터
    호출이 아니다. HA 로는 읽기(stream)만, 쓰기는 없다.
  - egress 단일 통과(SE-D3): transport 는 emit_external(NTFY) 산출물만 받는다.
  - fail-closed: transport 송신 실패는 파이프라인을 죽이지 않고 '미전송'으로
    기록한다(제안은 pending 으로 남아 다음에 재시도/다이제스트 가능).
"""

from __future__ import annotations

import asyncio
import contextvars
import json
import logging
import math
import secrets
import uuid
from dataclasses import replace
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Any, Literal

from liot.app import LiotApp, SafetySignal, StagedAlert
from liot.core.clock import Clock
from liot.core.contracts import CandidateAction, Judgment, Proposal, SensorReading
from liot.core.invariants import EgressViolation
from liot.core.risk import RiskTier
from liot.core.scoped_repo import ScopedRepo
from liot.core.severity import SeverityLevel, SeverityResult
from liot.db.safety_repo import DEFAULT_HOME_ALIAS, Ladder, SafetyRepo
from liot.layers.l2_context.feature_vector import (
    apply_decay,
    apply_signal,
    cell_key,
    load_cells,
    save_cells,
)
from liot.layers.l2_context.feedback_store import InMemoryFeedbackStore
from liot.layers.l2_context.preference_store import PreferenceStore
from liot.layers.l3_judgment.decision_backend import DEFAULT_DECISION_TIMEOUT_S
from liot.layers.l4_execution import escalation
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
    verify_guardian_ack_sig,
)
from liot.layers.l4_execution.notifier import (
    NotificationBudget,
    NotificationContext,
    Notifier,
    dedup_key,
    gate_pipeline,
)
from liot.layers.l4_execution.ntfy_transport import NtfyError, NtfyTransport
from liot.layers.l4_execution.proposal_service import ProposalService

if TYPE_CHECKING:  # pragma: no cover
    import aiosqlite

    from liot.layers.l1_sensing.ha_bridge import HABridge

__all__ = [
    "DEV_SIGNING_SECRET",
    "PHASE2_REQUESTS_PER_CALL",
    "UNMONITORABLE_RETRY",
    "DispatchResult",
    "LiotService",
    "default_phase2_deadline",
]

logger = logging.getLogger(__name__)

# 2단계(결정 모델) 판정 한 번이 보내는 HTTP 요청 수의 상한. 결정 백엔드의
# 제한시간은 요청 하나당이라(LIOT_DECISION_TIMEOUT), 판정 전체 마감은 그 배수다.
PHASE2_REQUESTS_PER_CALL = 4


def default_phase2_deadline(backend: object) -> float:
    """2단계 판정 전체 마감(초)의 기본값 = 요청당 제한시간 × 판정당 요청 수.

    백엔드가 요청당 제한시간(timeout_s)을 드러내면 그 값을, 아니면 결정 모델
    기본값(1.5초)을 쓴다. 조정 가능한 기본값이다(LiotService(phase2_deadline_s=)).
    """
    raw = getattr(backend, "timeout_s", None)
    per_request = DEFAULT_DECISION_TIMEOUT_S
    if (
        isinstance(raw, (int, float))
        and not isinstance(raw, bool)
        and math.isfinite(raw)
        and raw > 0
    ):
        per_request = float(raw)
    return per_request * PHASE2_REQUESTS_PER_CALL

_DEDUP_WINDOW = timedelta(hours=2)

# 감시 불가 알림 전송이 실패한 엔티티를 다시 시도하기까지의 최소 간격(조정 가능한 기본값).
# 재시도는 다음 읽기 불가 값이 올 때만 한다(시간 기반 재알림 없음). 간격이 없으면
# 연결이 깜빡이는 센서가 송신 실패(동기 송신이라 이벤트 루프를 제한시간만큼 묶는다)를
# 폭주시킨다. 성공한 엔티티는 간격이 아니라 표식이 막는다.
UNMONITORABLE_RETRY = timedelta(minutes=10)

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


ComfortHold = Literal["auto", "phone"]

# 열린 안전 상황 동안의 편의 제안 보류(결정 0016). ``run_tick`` 이 자기 디스패치 동안만 걸고
# ``dispatch`` 가 읽는다. 인자 대신 컨텍스트 변수를 쓰는 이유: (1) 실험 하네스가
# ``dispatch(proposal)`` 시그니처로 서브클래스를 둔다, (2) 인스턴스 속성이면 디스패치가
# await 하는 사이 끼어드는 다른 태스크(상황 알림)가 보류를 물려받는다 — 컨텍스트 변수는
# 태스크마다 따로라 그럴 수 없다.
_COMFORT_HOLD: contextvars.ContextVar[ComfortHold | None] = contextvars.ContextVar(
    "liot_comfort_hold", default=None
)


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

    # 연구용 재정의(기본 = 출하값, 운영 경로는 건드리지 않는다). 하루 시뮬레이터가 인스턴스에만
    # 덮어쓴다: 선호 EMA 학습률·감쇠율, '그만해' 뒤 같은 류를 쉬는 일수(0 = 쉬지 않음).
    pref_alpha: float | None = None
    pref_decay: float | None = None
    reject_window_days: int = 30

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
        safety_repo: SafetyRepo | None = None,
        phase2_deadline_s: float | None = None,
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
        # 안전 영속(상황 에피소드·취약 거주자 여부·판단 저장).
        self.safety = safety_repo or SafetyRepo(db, clock=self.clock)
        # 2단계 판정 전체 마감(초). None 이면 판정 시점의 백엔드로 기본값을 계산한다.
        self._phase2_deadline_s = phase2_deadline_s
        self._started = False  # 시작 시 1회 작업(표 로드 실패 감사) 완료 여부
        # 진행 중인 2단계 판정 태스크. 끝나면 스스로 빠진다(_on_phase2_done).
        self._phase2_tasks: set[asyncio.Task[DispatchResult | None]] = set()
        # 감시 불가 알림 표식(엔티티 id). 보냈거나(성공) 헌장 OFF 로 일부러 건너뛰었으면
        # 남는다. 정상·이상 값이 오면 지운다. 메모리 상태다 — 재시작하면 비워진다.
        self._unmonitorable_marked: set[str] = set()
        # 감시 불가 알림의 마지막 **실패한** 시도 시각(엔티티 id → datetime).
        # UNMONITORABLE_RETRY 안에는 다시 시도하지 않는다. 정상·이상 값으로는 지우지 않는다
        # (깜빡임 폭주 방지가 목적이라) — 성공하면 지운다.
        self._unmonitorable_failed_at: dict[str, datetime] = {}
        # 지금 이 프로세스가 열거나 복구하는 중인 상황 id. 열린 상황에 사다리도 알림도 아직
        # 없다는 사실은 '열던 중'일 때도 참이라, 복구(_recover_situation)가 이 집합에 든 상황은
        # 건드리지 않는다 — 같은 상황의 첫 알림이 두 번 나가거나 제안이 둘 생기지 않게.
        # 메모리 상태다: 프로세스가 죽으면 비워지고, 그때 DB 에 남은 고아 상황이 복구 대상이다.
        self._opening: set[str] = set()

    # ------------------------------------------------------------------
    # 시작
    # ------------------------------------------------------------------
    async def start(self) -> None:
        """시작 시 1회 작업. 진입점(handle_reading·run_tick·run)이 먼저 부른다.

        사다리 없이 열린 상황(첫 알림 전후에 프로세스가 죽은 흔적)을 먼저 복구하고
        (_recover_open_situations), 이어서 열린 에스컬레이션 사다리를 DB
        (list_open_ladders)에서 다시 무장한다 — 재시작이 진행 중인 재알림·보호자 전달·
        119 안내를 지우지 않게(_rearm_ladders).

        파급도 표가 로드에 실패해 fail-closed(모든 안전 카테고리 CRITICAL)로 돌고
        있으면, 그 사실을 audit_log 에 **한 번** 남기고 경고 로그를 낸다 — 조용히
        fail-closed 로만 돌면 '표가 안 읽혔다'는 걸 아무도 모른다.
        """
        if self._started:
            return
        self._started = True
        await self._recover_open_situations()
        await self._rearm_ladders()
        table = self.app.severity_table
        if not table.load_failed:
            return
        logger.warning(
            "severity table load failed — running fail-closed (every safety category "
            "CRITICAL) source=%s error=%s",
            table.source,
            table.load_error,
        )
        await self._repo.insert(
            self._db,
            "audit_log",
            {
                "id": None,
                "trace_id": None,
                "actor": "SYSTEM",
                "event_type": "severity_table_load_failed",
                "ref_id": table.source,
                "detail": json.dumps(
                    {"error": table.load_error, "fallback": "all safety categories CRITICAL"},
                    ensure_ascii=False,
                ),
                "ts": self.clock.now_utc(),
            },
        )
        await self._db.commit()

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
        rejected = await self.proposals.recently_rejected_keys(days=self.reject_window_days)
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

        열린 안전 상황 동안의 편의(능동 틱) 제안 보류(결정 0016)는 ``run_tick`` 이 거는
        컨텍스트 변수(``_COMFORT_HOLD``)로 받는다: ``"auto"`` 는 자동 실행만 막고(알림은
        게이트 그대로), ``"phone"`` 은 폰 발송도 막는다 — 영속만 하고 DIGEST 로 돌려주며
        감사 행을 남긴다. 걸지 않은 호출(명시 요청·상황 알림)은 보류가 없다.

        조작이 활성(executor+policy)이면, 정책이 AUTO(비안전 ≤BREATH)로 판정한
        제안은 알림 게이트를 통과(IMMEDIATE)할 때 *즉시 실행*한다. DUSK/EMBER/
        안전은 여기서 실행하지 않고 알림만 — 실행은 사람 승인(피드백) 후로 미룬다.
        """
        await self.proposals.save(proposal)

        comfort_hold = _COMFORT_HOLD.get()
        if comfort_hold == "phone":
            # 게이트·nonce 를 거치지 않는다: 어디로도 나가지 않는 제안에 1회용 링크가
            # 남으면 안 되고, 예산·dedup 도 소모하지 않는다.
            await self._audit_comfort_held(proposal)
            return DispatchResult(proposal, "DIGEST", sent=False)

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
        # 열린 안전 상황이 있으면(comfort_hold) 편의 제안은 자동 실행하지 않는다.
        if (
            exec_decision is ExecDecision.AUTO
            and self._executor is not None
            and comfort_hold is None
        ):
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

    async def _comfort_hold(self) -> ComfortHold | None:
        """열린 안전 상황에 따른 편의 제안 보류 수준(결정 0016).

        열린 상황이 하나라도 있으면 ``"auto"``(자동 실행 금지), 그중 하나라도 하한이
        CRITICAL 이면 ``"phone"``(폰 발송도 보류). 하한(severity_floor)만 본다 — 모델이
        올린 최종 파급도는 보호자 조건과 마찬가지로 여기에도 쓰지 않는다.
        """
        opened = await self.safety.list_open_situations(self.home_id)
        if not opened:
            return None
        if any(s.severity_floor == SeverityLevel.CRITICAL for s in opened):
            return "phone"
        return "auto"

    async def _audit_comfort_held(self, proposal: Proposal) -> None:
        """폰 발송을 보류한 편의 제안을 audit_log 에 남긴다(사유 comfort_held_open_situation)."""
        await self._repo.insert(
            self._db,
            "audit_log",
            {
                "id": None,
                "trace_id": None,
                "actor": "SYSTEM",
                "event_type": "comfort_held_open_situation",
                "ref_id": proposal.proposal_id,
                "detail": json.dumps({"decision": "DIGEST"}, ensure_ascii=False),
                "ts": self.clock.now_utc(),
            },
        )
        await self._db.commit()

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

        CONFIRM(DUSK, 그리고 원탭 안전 조치) → 즉시 실행. DOUBLE_CONFIRM(EMBER/안전)
        → 1차는 보류하고 한 번 더 확인 요청, 2차에서 실행. AUTO 는 이미 dispatch 에서
        실행됨.

        원탭 예외(T4): ONE_TAP_SAFE_ACTIONS(밸브 잠그기·현관 잠그기)는 실행 정책이
        CONFIRM 으로 판정하므로 2차 확인 대기(_awaiting_second)·2차 알림
        (_request_second_confirmation)을 건너뛰고 첫 승인에서 실행된다. 링크의 1회용
        nonce 는 그대로라 같은 링크를 재생해도 두 번 실행되지 않는다. 그 밖의 안전
        조치는 여전히 이중확인이다(이 분기는 실행 정책만 따르고 스스로 완화하지 않는다).

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
            result = await self._executor.undo(proposal.action, prior)
        except (ExecutionDenied, ExecutionError):
            return "200 OK", "undo-failed"
        # 예외가 없어도 실패일 수 있다(ok=False). 그걸 안 보면 '되돌렸다'고 답한 뒤
        # 상태는 그대로인 상황이 생긴다 — 2026-09-16 스윕이 이 자리를 짚었다.
        if not getattr(result, "ok", True):
            return "200 OK", "undo-failed"
        self._prior.pop(pid, None)
        return "200 OK", "undone"

    # ------------------------------------------------------------------
    # 진입점들
    # ------------------------------------------------------------------
    async def handle_reading(self, reading: SensorReading) -> DispatchResult | None:
        """단일 센서 읽기를 파이프라인에 흘리고, 제안이 나오면 디스패치.

        안전 카테고리(spec §3.4.1):
          - 이상 + 열린 상황 없음 → 상황을 열고 1단계 상황 알림을 즉시 보낸 뒤
            2단계 판정(별도 스레드, 제한시간)을 돌린다. 반환은 1단계 결과.
          - 이상 + 열린 상황 있음 → 같은 상황을 잇는다. 새 알림 제안 없음(None).
            재알림은 사다리(T8) 몫이다.
          - 정상 → 열린 상황을 닫고 해소 알림. 새 `alert` 없음(None). 열린 상황이
            없으면 아무 일도 없다.
          - 판정 보류(unavailable·unknown) → **이상/정상보다 먼저** 처리한다. 상황을
            열지도 닫지도 않고 사다리·보호자 경로를 건드리지 않는다. 거주자 감시 불가
            알림만(None). 먼저 거르지 않으면 정상으로 흘러 열린 상황이 닫힌다.
        비안전 카테고리는 예전 그대로다(관찰은 침묵, 판단이 있으면 저장).
        """
        await self.start()
        signal = self.app.classify_safety(reading)
        if signal is None:
            proposal = await self.app.process_reading(reading)
            await self._save_last_judgment()
            if proposal is None:
                return None
            return await self.dispatch(proposal)
        if signal.unmonitorable:
            await self._on_unmonitorable(signal)
            return None
        # 정상·이상 값이 왔다 = 다시 읽힌다 → 감시 불가 표식을 지운다.
        self._unmonitorable_marked.discard(reading.entity_id)
        if signal.abnormal:
            return await self._on_abnormal(signal)
        await self._on_normal(signal)
        return None

    # ------------------------------------------------------------------
    # 상황 에피소드(spec §3.4.1·§6.1·§6.2)
    # ------------------------------------------------------------------
    async def _on_abnormal(self, signal: SafetySignal) -> DispatchResult | None:
        """이상 신호: 상황을 열거나 잇는다. 새 에피소드만 상황 알림을 보낸다."""
        reading = signal.reading
        # 취약 거주자 여부는 불리언 하나만 쓴다(특성 자체는 여기서 끝난다 — 모델
        # 입력·알림·로그에 들어가지 않는다). spec §3.6.
        vulnerable = await self.safety.has_vulnerable_resident(self.home_id)
        sev = self.app.severity_table.compute(signal.category, vulnerable_resident=vulnerable)
        if sev is None:  # pragma: no cover - 안전 카테고리면 표가 항상 값을 낸다(방어)
            sev = SeverityResult(
                floor=SeverityLevel.CRITICAL, sources=("table",), damage=None, time_to_harm=None
            )
        sid, opened_new = await self.safety.open_or_get_situation(
            self.home_id, signal.category, reading.entity_id, sev.floor, self.clock.now_utc()
        )
        if not opened_new:
            self.app.observe_reading(
                reading, reason="열린 상황 이어감 — 새 알림 제안 없음(재알림은 사다리 몫)"
            )
            # 이어가는 상황이 사다리도 첫 알림도 없는 채 남아 있으면(여는 도중 프로세스가
            # 죽은 고아) 이 신호를 삼키지 않고 여기서 복구한다. 정상 상황은 사다리가 있어
            # 조회 한 번으로 돌아온다.
            await self._recover_situation(sid, reading)
            return None

        # 이 상황을 여는 중이라는 표식 — 첫 알림이 나가고 사다리가 서기 전까지 복구가
        # 끼어들지 못하게 한다(같은 상황의 두 번째 신호가 이 틈에 고아로 오인되면 안 된다).
        self._opening.add(sid)
        try:
            # 1단계: 표 우선 상황 알림을 만들어 **즉시** 보낸다(모델 호출 없음).
            # 여기서 실패하면 방금 연 상황을 닫는다 — 알림 없이 열린 상황이 남으면 다음
            # 이상 신호가 '이어 가기'로 묻혀 그 상황 전체가 조용해진다. (예외가 아니라 프로세스가
            # 통째로 죽으면 이 닫기는 돌지 못한다 — 그 고아는 시작 때 _recover_open_situations 가
            # 맡는다.)
            try:
                staged = await self.app.process_situation_reading(
                    reading, situation_id=sid, severity=sev
                )
                if staged is None:
                    await self.safety.close_situation(sid, self.clock.now_utc())
                    return None
                first = await self.dispatch(staged.proposal)
            except BaseException:
                await self.safety.close_situation(sid, self.clock.now_utc())
                raise
            await self.safety.save_judgment(staged.judgment)
            await self._on_situation_opened(sid, staged.proposal)
        finally:
            self._opening.discard(sid)
        # 2단계: 결정 모델(별도 스레드·제한시간)을 백그라운드로 돌린다. 기다리지 않는다 —
        # 기다리면 스트림의 다음 신호(예: 가스 직후 연기)가 이 상황의 모델 판정이
        # 끝날 때까지 묶여, 두 번째 안전 알림이 늦게 나간다(spec §6.2 '즉시').
        self._spawn_phase2(staged)
        return first

    # ------------------------------------------------------------------
    # 고아 상황 복구 — 상황 행은 커밋됐는데 첫 알림 또는 사다리가 서기 전에 프로세스가 죽은 경우
    # ------------------------------------------------------------------
    async def _recover_open_situations(self) -> int:
        """시작 시: 이 집의 열린 상황 중 사다리가 없는 것을 복구한다. 복구한 수를 돌려준다.

        사다리가 있는 상황은 _rearm_ladders 몫이다(U5 의 첫 알림 재시도 포함). 한 상황의
        복구 실패가 다른 상황·기동을 막지 않는다(로그만 남긴다).
        """
        recovered = 0
        for situation in await self.safety.list_open_situations(self.home_id):
            try:
                if await self._recover_situation(situation.situation_id, None):
                    recovered += 1
            except Exception as exc:  # noqa: BLE001 — 한 상황이 기동을 막지 않는다
                logger.error("open-situation recovery failed: %s", type(exc).__name__, exc_info=exc)
        if recovered:
            logger.warning("recovered %d open situation(s) that had no escalation ladder", recovered)
        return recovered

    async def _recover_situation(self, situation_id: str, reading: SensorReading | None) -> bool:
        """사다리 없이 열린 상황 하나를 복구한다. 손댔으면 True.

        시작 때는 reading=None, 열린 상황에 이상 신호가 이어질 때는 그 신호가 들어온다.
        세 갈래(알림 기록은 송신 성공 뒤에만 쓴다 — `situation_notified`):
          (a) 알림 기록 없음 + 제안은 저장됨 → U5 의 재송신 경로(_first_alert_unsent 가 사용자가
              정한 침묵 — 헌장 OFF·음소거·최근 거절 — 을 걸러 준다)로 첫 알림을 보낸다.
          (b) 알림 기록 없음 + 제안도 없음(저장 전에 죽음) → 상황 행(분류·엔티티·하한)에서
              첫 알림을 다시 만들어 보낸다(_rebuild_first_alert). 닫기만 하면 HA 는 시작 때
              현재 상태를 다시 보내지 않으므로(ha_bridge.stream 은 state_changed 구독뿐) 센서가
              이상인 채 조용하면 다음 신호가 영영 오지 않는다.
          (c) 이미 알림이 나갔는데 사다리만 없음(알림 직후·사다리 생성 전에 죽음) → 첫 알림은
              다시 보내지 않고 사다리만 세운다. 시각은 상황이 열린 때 기준 그대로다.
        마지막은 모두 _arm_ladder 다: 송신이 실패했으면 그 안에서 30초 재시도가 잡힌다.
        """
        if situation_id in self._opening:
            return False  # 이 프로세스가 지금 여는 중이거나 이미 복구 중 — 고아가 아니다
        # 표식은 첫 await 전에 세운다 — 조회 사이에 끼어든 두 번째 복구가 같은 상황을 또 잡지 않게.
        self._opening.add(situation_id)
        try:
            if await self.safety.get_ladder(situation_id) is not None:
                return False
            info = await escalation.load_situation(self._db, situation_id)
            # 목록을 읽은 뒤 정상 신호로 닫혔을 수 있다(닫힌 상황에 낡은 경보 금지). 집 조건은
            # load_situation 에 없어(상황 id 조회) 여기서 다시 비교한다.
            if info is None or info.closed_at is not None or info.home_id != self.home_id:
                return False
            # 알림이 기록된 제안은 반드시 제안 행이 있으므로 'pid 없음' = 알림 기록 없음이다. 제안이
            # 있으면 _first_alert_unsent 가 알림 기록·게이트(사용자가 정한 침묵)를 함께 본다.
            pid = await escalation.latest_alert_proposal_id(self._db, self.home_id, situation_id)
            if pid is None:
                await self._rebuild_first_alert(info, reading)
            else:
                unsent = await self._first_alert_unsent(info)
                if unsent is not None:
                    await self._resend_first_alert(unsent)
            await self._arm_ladder(situation_id)
        finally:
            self._opening.discard(situation_id)
        return True

    async def _rebuild_first_alert(
        self, info: escalation.SituationInfo, reading: SensorReading | None
    ) -> None:
        """저장된 제안이 없는 열린 상황의 첫 알림을 상황 행에서 다시 만들어 보낸다.

        읽기값은 DB 에 없다(센서 읽기는 영속하지 않는다). 시작 때는 '이상, 값 모름'을 뜻하는
        불리언 True 를 쓴다 — 모든 이상 규칙(불리언·수치 초과)에서 이상이고, 지어낸 수치가
        아니다. 알림 내용은 분류·집 별칭·시각·파급도뿐이라(egress) 값이 나가지 않는다. 이어지는
        실제 신호가 있으면(reading) 그 값을 쓰고 2단계 결정 모델도 돌린다. 합성 읽기로는
        돌리지 않는다 — 모델 입력에 없던 값을 넣지 않는다(모델은 하한을 올리기만 하므로 빼도
        하한은 그대로다).
        파급도 하한은 저장된 값과 지금 표·거주자 값 중 높은 쪽이다(하한은 오르기만 한다).
        발화 게이트는 dispatch 가 그대로 건다 — 헌장 OFF·음소거·최근 거절이면 보내지 않고
        제안만 pending 으로 남긴다.
        """
        vulnerable = await self.safety.has_vulnerable_resident(self.home_id)
        sev = self.app.severity_table.compute(info.category, vulnerable_resident=vulnerable)
        if sev is None:  # pragma: no cover - 안전 카테고리면 표가 항상 값을 낸다(방어)
            sev = SeverityResult(
                floor=SeverityLevel.CRITICAL, sources=("table",), damage=None, time_to_harm=None
            )
        if info.severity_floor > sev.floor:
            sources = sev.sources if "resident" in sev.sources else (*sev.sources, "resident")
            sev = replace(sev, floor=info.severity_floor, sources=sources)
        real = reading is not None
        source_reading = reading or SensorReading(
            entity_id=info.entity_id,
            source="recovery",
            raw_value=True,
            norm_value=True,
            ts_utc=self.clock.now_utc(),
            home_id=self.home_id,
        )
        staged = await self.app.process_situation_reading(
            source_reading, situation_id=info.situation_id, severity=sev
        )
        if staged is None:  # pragma: no cover - 안전 카테고리면 항상 제안이 나온다(방어)
            logger.error("orphan situation could not be rebuilt into a first alert")
            return
        await self.dispatch(staged.proposal)
        await self.safety.save_judgment(staged.judgment)
        if real:
            self._spawn_phase2(staged)

    def _spawn_phase2(self, staged: StagedAlert) -> None:
        """2단계 판정을 추적되는 백그라운드 태스크로 띄운다."""
        task = asyncio.create_task(self._run_phase2(staged))
        self._phase2_tasks.add(task)
        task.add_done_callback(self._on_phase2_done)

    def _on_phase2_done(self, task: asyncio.Task[DispatchResult | None]) -> None:
        """끝난 2단계 태스크를 추적에서 빼고, 예외가 있으면 로그로 남긴다(삼키지 않는다)."""
        self._phase2_tasks.discard(task)
        if task.cancelled():
            return
        exc = task.exception()
        if exc is not None:
            logger.error("phase-2 judgment task failed: %s", type(exc).__name__, exc_info=exc)

    async def drain_phase2(self, timeout: float | None = None) -> None:
        """진행 중인 2단계 태스크가 모두 끝날 때까지 기다린다.

        timeout(초)을 주면 그 안에 끝나지 않은 태스크는 취소한다(종료 경로용).
        각 태스크는 자체 마감(_phase2_deadline)이 있어 timeout 없이도 유한하게 끝난다.
        테스트는 잠자기 대신 이걸 기다린다.
        """
        while self._phase2_tasks:
            pending = set(self._phase2_tasks)
            _, not_done = await asyncio.wait(pending, timeout=timeout)
            for t in not_done:
                t.cancel()
            if not_done:
                await asyncio.gather(*not_done, return_exceptions=True)
            self._phase2_tasks.difference_update(pending)

    def _phase2_deadline(self) -> float:
        if self._phase2_deadline_s is not None:
            return self._phase2_deadline_s
        return default_phase2_deadline(self.app.judge.backend)

    async def _run_phase2(self, staged: StagedAlert) -> DispatchResult | None:
        """2단계 판정을 이벤트 루프 밖(스레드)에서 돌리고, 행을 저장하고, 올렸으면 보낸다.

        제한시간을 넘기거나 판정이 예외로 끝나면 하한 그대로의 degraded 행을 남긴다
        (알림은 이미 1단계로 나갔다). 판단 행은 결과와 무관하게 **항상** 저장한다 —
        하한 아래로 낮추려 한 답(injection_suspect)도 감사 기록에 남는다.
        """
        req = staged.deferred
        deadline = self._phase2_deadline()
        try:
            judgment = await asyncio.wait_for(
                asyncio.to_thread(self.app.run_deferred_judge, req), timeout=deadline
            )
        except TimeoutError:
            judgment = self.app.deadline_judgment(
                req, reason=f"제한시간 {deadline:g}초 초과"
            )
        except Exception as exc:  # noqa: BLE001 — 2단계 실패는 1단계 알림을 무르지 않는다
            judgment = self.app.deadline_judgment(req, reason=f"판정 오류 {type(exc).__name__}")
        await self.safety.save_judgment(judgment)
        followup = self.app.route_followup(judgment)
        if not self._raised(staged.proposal, followup):
            return None
        # 2단계가 백그라운드로 도는 동안 상황이 닫혔을 수 있다(정상 복귀). 닫힌 상황의
        # 후속 알림은 낡은 경보다 — 보내지 않는다(판단 행은 위에서 이미 남겼다).
        cand = req.candidate
        current = await self.safety.get_open_situation(
            self.home_id, cand.category, cand.entity_id
        )
        if current is None or current.situation_id != req.situation_id:
            return None
        result = await self.dispatch(followup)
        # 최종 파급도가 올랐다 → 재알림 주기를 다시 잡는다(없던 사다리면 지금 만든다).
        # 보호자·119 조건(하한)은 모델이 바꾸지 못하므로 그대로다.
        await self._arm_ladder(req.situation_id)
        return result

    @staticmethod
    def _raised(first: Proposal, followup: Proposal) -> bool:
        """2단계 제안이 1단계보다 확인 강도나 파급도를 올렸는가."""
        if followup.risk_tier > first.risk_tier:
            return True
        if followup.severity is None:
            return False
        return first.severity is None or followup.severity > first.severity

    async def _on_unmonitorable(self, signal: SafetySignal) -> None:
        """판정 보류 신호: 거주자 감시 불가 알림만 보낸다(엔티티별 한 번).

        상황 테이블·사다리·보호자는 건드리지 않는다 — 열린 상황은 열린 채, 사다리는
        원래 시각대로 돈다. 표식은 전송에 성공했을 때, 또는 헌장 OFF 로 일부러 보내지
        않았을 때 남는다(헌장을 다시 켜도 값이 돌아오기 전까지는 알림이 없다).
        실패하면 표식 없이 시도 시각만 남겨, UNMONITORABLE_RETRY 뒤의 다음 읽기 불가
        값에서 다시 시도한다.
        """
        reading = signal.reading
        eid = reading.entity_id
        self.app.observe_reading(reading, reason="감시 불가(판정 보류) — 상황 판단 없음")
        if eid in self._unmonitorable_marked:
            return
        now_dt = self.clock.now_dt()
        failed_at = self._unmonitorable_failed_at.get(eid)
        if failed_at is not None and now_dt - failed_at < UNMONITORABLE_RETRY:
            return
        # 헌장 OFF 만 존중한다. 음소거는 보지 않는다 — 안전 감시가 끊긴 사실을
        # 음소거로 숨기면 안 된다.
        ctx = await self._build_context(now_dt)
        if not ctx.charter_enabled:
            self._unmonitorable_marked.add(eid)
            return
        if await self._send_unmonitorable_notice(signal.category):
            self._unmonitorable_marked.add(eid)
            self._unmonitorable_failed_at.pop(eid, None)
        else:
            self._unmonitorable_failed_at[eid] = now_dt

    async def _send_unmonitorable_notice(self, category: str) -> bool:
        """거주자 토픽으로 감시 불가 알림(파급도 NOTICE) 한 건. 보냈으면 True.

        상황 요약 알림 경로(build_guardian_payload → sanitize_summary)를 그대로 쓴다 —
        싣는 것은 센서 종류 라벨·집 별칭·시각·파급도 라벨뿐(상황 id·보호자 id·링크 없음).
        집 별칭이 egress 에 걸리면 기본 별칭으로 한 번 더 만든다(_summary_message 와 같다).
        """
        settings = await self.safety.get_home_settings(self.home_id)
        label = escalation.message_label(category, "unmonitorable")
        at = self.clock.now_utc()
        for alias in dict.fromkeys((settings.home_alias, DEFAULT_HOME_ALIAS)):
            payload = self.notifier.build_guardian_payload(
                situation_label=label,
                home_alias=alias,
                at=at,
                severity=SeverityLevel.NOTICE,
            )
            try:
                sanitized = self.notifier.sanitize_summary(
                    payload, trace_id=f"unmonitorable:{category}"
                )
            except EgressViolation:
                logger.warning("unmonitorable notice rejected by egress (category=%s)", category)
                continue
            return self._deliver(self.topic, sanitized)
        return False

    async def _on_normal(self, signal: SafetySignal) -> None:
        """정상 신호: 열린 상황이 있으면 닫는다. 새 `alert` 는 만들지 않는다."""
        reading = signal.reading
        self.app.observe_reading(reading, reason="정상 신호 — 새 알림 제안 없음")
        situation = await self.safety.get_open_situation(
            self.home_id, signal.category, reading.entity_id
        )
        if situation is None:
            return
        if not await self.safety.close_situation(situation.situation_id, self.clock.now_utc()):
            return  # pragma: no cover - 같은 연결에서 방금 읽은 열린 상황(방어)
        sid = situation.situation_id
        # 그 상황의 대기 제안(상황 알림·원탭 조치)을 만료 → 낡은 링크가 통하지 않는다.
        for pid in await self.proposals.expire_situation(sid):
            self._drop_nonce(pid)
        await self._send_resolved_notice(sid, signal)
        await self._on_situation_closed(sid)

    async def _send_resolved_notice(self, situation_id: str, signal: SafetySignal) -> bool:
        """거주자에게 '해소됨' 알림(파급도 NOTICE)을 보낸다. 보냈으면 True.

        제안으로 영속하지 않는다 — 응답할 행동이 없는 정보 알림이라 수용률·대기
        목록에 섞이면 안 된다. 발화 게이트는 그대로 지난다: 헌장 OFF·음소거는
        존중하고, 시의성(perishable)으로 중복·야간·예산에만 묶이지 않는다.
        정제(egress)도 그대로다 — 기기·방·근거는 나가지 않는다.
        """
        now = self.clock.now_utc()
        notice = Proposal(
            proposal_id=str(uuid.uuid4()),
            judgment_id=f"situation:{situation_id}",
            home_id=self.home_id,
            risk_tier=RiskTier.BREATH,
            action=CandidateAction(
                capability=signal.category,
                action="resolved",
                params={"__origin__": "safety:resolved"},
                entity_id=signal.reading.entity_id,
                category=signal.category,
            ),
            rationale="상황 해소(센서 정상 복귀) — 거주자 알림",
            required_confirmation="none",
            perishable=True,
            state="expired",  # 응답 대상이 아니다(영속하지도 않는다)
            created_at=now,
            severity=SeverityLevel.NOTICE,
            situation_id=situation_id,
        )
        now_dt = self.clock.now_dt()
        self._roll_budget(now_dt)
        ctx = await self._build_context(now_dt)
        decision, sanitized = self.notifier.notify(notice, ctx, self._budget)
        if decision != "IMMEDIATE" or sanitized is None:
            return False
        if self.transport is not None:
            try:
                self.transport.send(self.topic, sanitized)
            except NtfyError:
                return False
        await self.proposals.record_notification(sanitized, now=now)
        return self.transport is not None

    async def _on_situation_opened(self, situation_id: str, proposal: Proposal) -> None:
        """새 상황 에피소드의 첫 알림을 보낸 직후 불린다 — 이 상황의 사다리를 시작한다.

        보호자 전달 조건은 상황의 severity_floor(표+거주자, situations 테이블)를
        본다 — 모델 상향은 그 값을 바꾸지 못한다. 재알림 주기는 최종 파급도다.
        """
        await self._arm_ladder(situation_id)

    async def _on_situation_closed(self, situation_id: str) -> None:
        """상황이 닫힌 직후(거주자 해소 알림 뒤) 불린다.

        사다리를 취소하고, 이미 전달받은 보호자 중 지금도 동의가 유효한 사람에게
        '해소됨'을 보낸다(전달 기록 kind='resolved'). 이미 닫힌 사다리면 아무것도
        하지 않는다 — 해소 알림이 두 번 나가지 않게.
        """
        ladder = await self.safety.get_ladder(situation_id)
        if ladder is None or ladder.closed:
            return
        await self.safety.update_ladder(
            situation_id, closed=True, cancelled=True, next_fire_at=None
        )
        if not ladder.notified_guardian_ids:
            return
        info = await escalation.load_situation(self._db, situation_id)
        if info is None:  # pragma: no cover - 사다리는 상황 없이 만들 수 없다(방어)
            return
        final = await self._final_severity(info)
        consented = {g.guardian_id: g for g in await self.safety.consented_guardians(self.home_id)}
        now = self.clock.now_utc()
        for gid in ladder.notified_guardian_ids:
            guardian = consented.get(gid)
            if guardian is None:
                continue  # 동의를 철회했다 — 더는 보내지 않는다
            msg = await self._summary_message(info, final, "resolved", at=now)
            if msg is not None and self._deliver(guardian.topic, msg):
                await self.safety.record_forward(situation_id, gid, "resolved", now)

    # ------------------------------------------------------------------
    # 에스컬레이션 사다리(T8) — 정책은 escalation 모듈, 송신·영속은 여기
    # ------------------------------------------------------------------
    @staticmethod
    def _ladder_open(ladder: Ladder) -> bool:
        return not (ladder.acked or ladder.closed or ladder.cancelled)

    async def _final_severity(self, info: escalation.SituationInfo) -> SeverityLevel:
        """최종 파급도 = max(상황 하한, 그 상황 제안들의 최대 파급도)."""
        found = await escalation.final_severity_of(self._db, self.home_id, info.situation_id)
        if found is None or found < info.severity_floor:
            return info.severity_floor
        return found

    async def _first_forward_at(self, situation_id: str) -> datetime | None:
        """그 상황의 첫 보호자 전달 시각(전달 기록에서). 없으면 None."""
        times = [
            f.at
            for f in await self.safety.list_forwards(self.home_id)
            if f.situation_id == situation_id and f.kind == "forward"
        ]
        return escalation.parse_iso(min(times)) if times else None

    async def _plan_next(
        self,
        info: escalation.SituationInfo,
        final: SeverityLevel,
        *,
        stage: int,
        guidance_sent: bool,
        retry: bool = False,
    ) -> str | None:
        return escalation.to_iso(
            escalation.next_fire_at(
                opened=escalation.parse_iso(info.opened_at),
                final=final,
                floor=info.severity_floor,
                realerts_sent=stage,
                now=self.clock.now_dt(),
                guidance_sent=guidance_sent,
                first_forward_at=await self._first_forward_at(info.situation_id),
                retry=retry,
            )
        )

    async def _arm_ladder(self, situation_id: str) -> None:
        """사다리를 만들거나(없으면) 다음 발화 시각을 다시 잡는다. 여기서는 보내지 않는다.

        RECORD·NOTICE(재알림 없음, 하한도 CRITICAL 아님)는 사다리를 만들지 않는다.
        """
        info = await escalation.load_situation(self._db, situation_id)
        if info is None or info.closed_at is not None:
            return
        final = await self._final_severity(info)
        if not escalation.needs_ladder(final, info.severity_floor):
            return
        ladder = await self.safety.get_ladder(situation_id)
        # 첫 거주자 알림이 아직 못 나갔으면(송신 실패) 다음 발화를 30초 뒤로 당긴다.
        # 이 사실은 DB(알림 기록 없음 + 게이트 통과)에서 매번 다시 읽으므로 재시작 뒤
        # 재무장해도 유지된다.
        first_retry = await self._first_alert_unsent(info) is not None
        if ladder is None:
            nxt = await self._plan_next(
                info, final, stage=0, guidance_sent=False, retry=first_retry
            )
            await self.safety.create_ladder(situation_id, 0, nxt)
            return
        if not self._ladder_open(ladder):
            return
        nxt = await self._plan_next(
            info,
            final,
            stage=ladder.stage,
            guidance_sent=ladder.guidance_119_sent,
            retry=first_retry,
        )
        if first_retry and ladder.next_fire_at is not None and (
            nxt is None or ladder.next_fire_at < nxt
        ):
            # 이미 잡힌 재시도가 더 이르면 미루지 않는다(2단계 후속·재시작 재무장이 30초를
            # 다시 세지 않게).
            nxt = ladder.next_fire_at
        await self.safety.update_ladder(situation_id, next_fire_at=nxt)

    async def _first_alert_unsent(self, info: escalation.SituationInfo) -> Proposal | None:
        """아직 거주자에게 한 번도 못 나간 상황 알림이 있으면 그 제안을, 없으면 None.

        '못 나갔다' = 그 상황의 어떤 제안도 알림 기록이 없고(기록은 송신 성공 뒤에만 쓴다),
        배달 경로(transport)가 있고, 지금 첫 알림과 같은 발화 게이트가 IMMEDIATE 로 통과시킬
        때다. 헌장 OFF·음소거·최근 거절로 막힌 첫 알림은 송신 실패가 아니라 사용자가 정한
        침묵이라 여기 잡히지 않는다(재시도 없음). 게이트 판정만 읽고 예산은 차감하지 않는다.
        """
        if self.transport is None:
            return None
        if await escalation.situation_notified(self._db, self.home_id, info.situation_id):
            return None
        pid = await escalation.latest_alert_proposal_id(self._db, self.home_id, info.situation_id)
        proposal = await self.proposals.get(pid) if pid is not None else None
        if proposal is None:
            return None
        now_dt = self.clock.now_dt()
        self._roll_budget(now_dt)
        ctx = await self._build_context(now_dt)
        if gate_pipeline(proposal, ctx, self._budget, self.clock.now_utc()) != "IMMEDIATE":
            return None
        return proposal

    async def _resend_first_alert(self, proposal: Proposal) -> bool:
        """못 나간 첫 알림을 다시 보낸다(dispatch 와 같은 순서: 송신 → 기록 → dedup 갱신).

        링크는 살아 있는 토큰을 그대로 쓴다(_send_realert 와 같다). 재알림이 아니라 첫 알림이므로
        재알림 횟수(stage)는 쓰지 않는다.
        """
        nonce = self._nonce.get(proposal.proposal_id) or self._issue_nonce(proposal.proposal_id)
        sanitized = self.notifier.sanitize(proposal, nonce)
        if not self._deliver(self.topic, sanitized):
            return False
        await self.proposals.record_notification(sanitized, now=self.clock.now_utc())
        self._recent_notified[dedup_key(proposal)] = self.clock.now_dt()
        return True

    async def _rearm_ladders(self) -> int:
        """시작 시 열린 사다리를 DB 에서 다시 무장한다. 무장한 수를 돌려준다.

        상황이 이미 닫혔는데 사다리가 열려 있으면(닫기 직후 프로세스가 죽은 경우)
        닫기 처리(취소 + 보호자 해소 알림)를 마저 한다.
        """
        armed = 0
        for ladder in await self.safety.list_open_ladders():
            if ladder.home_id != self.home_id:
                continue
            info = await escalation.load_situation(self._db, ladder.situation_id)
            if info is None or info.closed_at is not None:
                await self._on_situation_closed(ladder.situation_id)
                continue
            await self._arm_ladder(ladder.situation_id)
            armed += 1
        if armed:
            logger.info("re-armed %d open escalation ladder(s)", armed)
        return armed

    async def ladder_tick(self) -> int:
        """발화 시각(next_fire_at)이 된 사다리를 처리한다. 처리한 사다리 수를 돌려준다.

        사다리 하나의 실패가 다른 사다리를 막지 않는다(로그만 남긴다).
        """
        now_iso = self.clock.now_utc()
        fired = 0
        for ladder in await self.safety.list_open_ladders():
            if ladder.home_id != self.home_id:
                continue
            if ladder.next_fire_at is None or ladder.next_fire_at > now_iso:
                continue
            try:
                await self._fire_ladder(ladder.situation_id)
            except Exception as exc:  # noqa: BLE001 — 루프는 죽지 않는다
                logger.error("escalation ladder failed: %s", type(exc).__name__, exc_info=exc)
            fired += 1
        return fired

    async def _ladder_still_open(self, situation_id: str) -> bool:
        """송신 직전 재확인 — 그 사이 확인(ack)·해소가 들어왔으면 보내지 않는다."""
        ladder = await self.safety.get_ladder(situation_id)
        return ladder is not None and self._ladder_open(ladder)

    async def _fire_ladder(self, situation_id: str) -> None:
        """사다리 한 번 발화: 밀린 재알림 1회 → (하한 CRITICAL) 보호자 전달 → 119 안내."""
        ladder = await self.safety.get_ladder(situation_id)
        if ladder is None or not self._ladder_open(ladder):
            return
        info = await escalation.load_situation(self._db, situation_id)
        if info is None or info.closed_at is not None:
            await self._on_situation_closed(situation_id)
            return
        now_dt = self.clock.now_dt()
        opened = escalation.parse_iso(info.opened_at)
        final = await self._final_severity(info)

        stage = ladder.stage
        due = escalation.realerts_due(opened, final, now_dt)
        realert_attempted = False
        if due > stage:
            # 공유 연결에서 닫힘이 끼어들었을 수 있다 — 해소 뒤 낡은 재알림 금지.
            if not await self._ladder_still_open(situation_id):
                return
            await self._send_realert(info, final)
            realert_attempted = True
            stage = due  # 재시작 뒤 밀린 재알림은 한 번으로 따라잡는다

        # 첫 거주자 알림이 아직 못 나갔으면 다시 보낸다(재알림 횟수는 쓰지 않는다). 같은
        # 발화에서 재알림이 방금 시도됐으면 그것이 나갔는지만 보고 같은 틱에 또 보내지 않는다.
        notified = list(ladder.notified_guardian_ids)
        guidance_sent = ladder.guidance_119_sent
        retry = False
        unsent = await self._first_alert_unsent(info)
        if unsent is not None:
            if realert_attempted:
                retry = True
            elif await self._ladder_still_open(situation_id):
                retry = not await self._resend_first_alert(unsent)
        # 보호자 전달·119 안내: 상황 하한(표+거주자)이 CRITICAL 일 때만.
        if escalation.guardian_path(info.severity_floor):
            if now_dt >= escalation.forward_due_at(opened) and await self._ladder_still_open(
                situation_id
            ):
                newly, failed = await self._forward_to_guardians(info, final, notified)
                notified.extend(newly)
                retry = retry or failed
            if not guidance_sent:
                due_119 = escalation.guidance_due_at(
                    opened, await self._first_forward_at(situation_id)
                )
                if now_dt >= due_119 and await self._ladder_still_open(situation_id):
                    guidance_sent = await self._send_guidance_119(info, final, notified)

        nxt = await self._plan_next(
            info, final, stage=stage, guidance_sent=guidance_sent, retry=retry
        )
        await self.safety.update_ladder(
            situation_id,
            stage=stage,
            next_fire_at=nxt,
            notified_guardian_ids=notified,
            guidance_119_sent=guidance_sent,
        )

    async def _send_realert(self, info: escalation.SituationInfo, final: SeverityLevel) -> bool:
        """거주자 재알림: 그 상황의 가장 높은 파급도 제안을 다시 보낸다.

        게이트는 escalation.realert_allowed 뿐이다(CRITICAL=헌장 OFF 만, WARNING=헌장
        OFF·음소거). 링크는 살아 있는 토큰을 그대로 쓴다 — 새로 발급하면 앞선 알림의
        버튼이 전부 무효가 돼, 첫 알림에서 '괜찮아요'를 눌러도 403 이 된다.
        """
        ctx = await self._build_context(self.clock.now_dt())
        if not escalation.realert_allowed(final, ctx, info.category, info.entity_id):
            return False
        pid = await escalation.latest_alert_proposal_id(self._db, self.home_id, info.situation_id)
        proposal = await self.proposals.get(pid) if pid is not None else None
        if proposal is None:
            return False
        nonce = self._nonce.get(proposal.proposal_id) or self._issue_nonce(proposal.proposal_id)
        sanitized = self.notifier.sanitize(proposal, nonce)
        if not self._deliver(self.topic, sanitized):
            return False
        await self.proposals.record_notification(sanitized, now=self.clock.now_utc())
        return True

    async def _forward_to_guardians(
        self,
        info: escalation.SituationInfo,
        final: SeverityLevel,
        notified: list[str],
    ) -> tuple[list[str], bool]:
        """동의가 유효한 보호자 전원(아직 안 받은 사람)에게 동시에 전달한다.

        반환: (이번에 전달된 보호자 id 목록, 실패가 있었는가). 실패한 보호자는 다음
        발화에서 다시 시도한다. 전달마다 kind='forward' 기록을 남긴다.
        """
        newly: list[str] = []
        failed = False
        now = self.clock.now_utc()
        for guardian in await self.safety.consented_guardians(self.home_id):
            gid = guardian.guardian_id
            if gid in notified:
                continue
            msg = await self._summary_message(
                info, final, "forward", at=info.opened_at, guardian_id=gid
            )
            if msg is not None and self._deliver(guardian.topic, msg):
                await self.safety.record_forward(info.situation_id, gid, "forward", now)
                newly.append(gid)
            else:
                failed = True
        return newly, failed

    async def _send_guidance_119(
        self,
        info: escalation.SituationInfo,
        final: SeverityLevel,
        notified: list[str],
    ) -> bool:
        """119 전화 **안내**(번호 문구 + 상황 요약)를 거주자와 전달받은 보호자에게 보낸다.

        전화를 걸거나 신고하지 않는다 — 알림 한 건씩일 뿐이다. 거주자 쪽은 헌장 OFF 를
        존중하고, 보호자 쪽은 지금도 동의가 유효한 사람만 받는다. 한 곳이라도 나가면
        kind='guidance_119' 기록 한 줄(보호자 칸 없음). 반환: 이 단계가 끝났는가
        (모든 송신이 실패했으면 False → 재시도).
        """
        delivered = False
        failed = False
        ctx = await self._build_context(self.clock.now_dt())
        if ctx.charter_enabled:
            msg = await self._summary_message(info, final, "guidance_119", at=info.opened_at)
            if msg is not None and self._deliver(self.topic, msg):
                delivered = True
            else:
                failed = True
        consented = {g.guardian_id: g for g in await self.safety.consented_guardians(self.home_id)}
        for gid in notified:
            guardian = consented.get(gid)
            if guardian is None:
                continue  # 철회했다 — 더는 보내지 않는다
            msg = await self._summary_message(
                info, final, "guidance_119", at=info.opened_at, guardian_id=gid
            )
            if msg is not None and self._deliver(guardian.topic, msg):
                delivered = True
            else:
                failed = True
        if delivered:
            await self.safety.record_forward(
                info.situation_id, None, "guidance_119", self.clock.now_utc()
            )
        return delivered or not failed

    async def _summary_message(
        self,
        info: escalation.SituationInfo,
        final: SeverityLevel,
        kind: escalation.MessageKind,
        *,
        at: str,
        guardian_id: str | None = None,
    ) -> dict[str, Any] | None:
        """상황 요약 알림을 만들어 egress 로 정제한다. 정제가 거부하면 None.

        집 별칭이 PII 스캔에 걸리면 기본 별칭으로 한 번 더 시도한다 — 별칭 때문에
        보호자 알림이 통째로 막히면 안전이 조용해진다. 확인 링크는 보호자 전달과
        보호자 119 안내에만 싣는다(해소 알림·거주자 안내는 링크 없음).
        """
        settings = await self.safety.get_home_settings(self.home_id)
        with_link = guardian_id if kind != "resolved" else None
        for alias in dict.fromkeys((settings.home_alias, DEFAULT_HOME_ALIAS)):
            payload = self.notifier.build_guardian_payload(
                situation_label=escalation.message_label(info.category, kind),
                home_alias=alias,
                at=at,
                # 해소 알림은 NOTICE(주의) — 상황의 최종 파급도로 다시 깨우지 않는다.
                severity=SeverityLevel.NOTICE if kind == "resolved" else final,
                situation_id=info.situation_id if with_link else None,
                guardian_id=with_link,
            )
            try:
                return self.notifier.sanitize_summary(payload, trace_id=info.situation_id)
            except EgressViolation:
                logger.warning("summary payload rejected by egress (kind=%s)", kind)
        return None

    def _deliver(self, topic: str, sanitized: dict[str, Any]) -> bool:
        """정제된 알림 한 건을 보낸다. 보냈으면 True.

        토픽은 보호자의 비밀일 수 있어 로그에 남기지 않는다(예외 메시지도 남기지 않는다).
        transport 가 없으면(sanitize-only) 배달이 없으므로 False.
        """
        if self.transport is None:
            return False
        try:
            self.transport.send(topic, sanitized)
        except NtfyError as exc:
            logger.warning("escalation send failed: %s", type(exc).__name__)
            return False
        return True

    async def _ack_situation(self, situation_id: str, by: str) -> bool:
        """그 상황의 사다리를 확인(ack)으로 멈춘다. 멈췄으면 True(이미 멈췄으면 False)."""
        ladder = await self.safety.get_ladder(situation_id)
        if ladder is None or ladder.home_id != self.home_id or not self._ladder_open(ladder):
            return False
        await self.safety.update_ladder(
            situation_id,
            acked=True,
            acked_by=by,
            acked_at=self.clock.now_utc(),
            next_fire_at=None,
        )
        return True

    async def _handle_guardian_ack(self, sid: str, gid: str, sig: str) -> tuple[str, str]:
        """보호자 확인 링크(GET /guardian-ack?s=&g=&sig=). 알림 열기는 이 경로가 아니다.

        서명(보호자별 HMAC)이 맞고, 그 보호자가 이 상황을 전달받았고, 지금도 동의가
        유효해야 한다. 확인은 멱등이다.
        """
        if not (sid and gid and verify_guardian_ack_sig(self._secret, sid, gid, sig)):
            return "403 Forbidden", "invalid signature"
        ladder = await self.safety.get_ladder(sid)
        if ladder is None or ladder.home_id != self.home_id:
            return "404 Not Found", "no such situation"
        if gid not in ladder.notified_guardian_ids:
            return "403 Forbidden", "not forwarded to this guardian"
        consented = {g.guardian_id for g in await self.safety.consented_guardians(self.home_id)}
        if gid not in consented:
            return "403 Forbidden", "consent revoked"
        if not self._ladder_open(ladder):
            return "200 OK", "ok: already acknowledged"
        await self._ack_situation(sid, f"guardian:{gid}")
        return "200 OK", "ok: acknowledged"

    async def _ack_only_click(
        self, proposal: Proposal, tier: str, sig: str, key: str, nonce: str
    ) -> bool:
        """살아 있지 않은 제안(만료 등)의 재알림 링크를 확인 전용으로 받는다.

        제안 만료는 사다리를 취소하지 않으므로, 재알림은 만료된 제안으로도 나간다.
        그 링크의 응답은 상태를 바꾸지 않고(만료 그대로) 사다리만 멈춘다. 서명은
        피드백과 똑같이 검증한다(action·nonce 바인딩).
        """
        if proposal.situation_id is None or key not in FeedbackGateway._ACTION_ALIASES:
            return False
        if not verify_feedback_sig(self._secret, proposal.proposal_id, tier, key, sig, nonce):
            return False
        return await self._ack_situation(proposal.situation_id, "resident")

    async def _ladder_loop(
        self,
        resolution_s: float = escalation.LADDER_RESOLUTION_S,
        max_iterations: int | None = None,
    ) -> None:
        """300초 틱과 별개인 사다리 루프. resolution_s 마다 DB 의 발화 시각을 본다."""
        n = 0
        while True:
            try:
                await self.ladder_tick()
            except Exception as exc:  # noqa: BLE001 — 사다리 루프는 죽지 않는다
                logger.error("escalation ladder tick failed: %s", type(exc).__name__)
            n += 1
            if max_iterations and n >= max_iterations:
                return
            await asyncio.sleep(resolution_s)

    async def _save_last_judgment(self) -> None:
        """앱이 직전 진입에서 만든 판단을 judgments 에 한 행으로 남긴다(1회만)."""
        judgment: Judgment | None = self.app.last_judgment
        if judgment is None:
            return
        self.app.last_judgment = None  # 같은 판단을 두 번 저장하지 않는다
        await self.safety.save_judgment(judgment)

    async def run_tick(self) -> DispatchResult | None:
        """능동 틱 1회 + 만료 스윕. 능동 제안이 나오면 디스패치.

        틱 전에 거절 이력·수용률 스냅샷을 평가기의 선호 신호로 주입하고,
        틱 후에 후보 평가 결과(무행동 선택 포함)를 candidate_evaluations 로
        영속한다 — 어떤 후보들이 점수화됐고 왜 그 선택이었는지가 남는다.
        """
        await self.start()
        await self.proposals.expire_due()
        await self._apply_expired_signals()  # 만료 = 약한 비선호 신호
        await self._sweep_nonces()
        await self._refresh_feedback()
        await self._load_pref_cells()  # 영속 선호 셀 → 임베더(개인화 반영)
        proposal = await self.app.tick()
        await self._save_last_judgment()  # 컴포트 판단도 저장(억제된 판단 포함)
        await self._record_evaluation()
        if proposal is None:
            return None
        # 열린 안전 상황 동안의 편의 보류(결정 0016). run_tick 이 편의 제안의 유일한
        # 입구라 여기서 건다(제안선 후보는 __origin__ 이 없어 출처로는 못 가른다).
        hold = await self._comfort_hold()
        if hold != "phone":
            # 보류분은 선호 셀에 넣지 않는다 — 만료가 비선호로 학습되면 안 된다.
            self._remember_proposal_cell(proposal)  # 해소 시 신호 반영용 맥락 기억
        token = _COMFORT_HOLD.set(hold)
        try:
            return await self.dispatch(proposal)
        finally:
            _COMFORT_HOLD.reset(token)

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
        cells = apply_decay(cells, steps=1, exclude=frozenset({key}), rate=self.pref_decay)
        cells = apply_signal(cells, key, signal, alpha=self.pref_alpha)
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
        rejected = await self.proposals.recently_rejected_keys(days=self.reject_window_days)
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
        ladder_resolution_s: float = escalation.LADDER_RESOLUTION_S,
    ) -> None:
        """상시 데몬: HA 스트림 소비 + 주기 틱 + 사다리 루프를 동시에 굴린다.

        bridge 가 None 이면 능동 틱만(센서 스트림 없이) 돈다. max_events/
        max_ticks 는 테스트용 경계(미지정 시 무한). 사다리 루프(ladder_resolution_s
        해상도)는 틱과 별개로 돌고, 틱·스트림이 끝나면 함께 멈춘다.
        """
        await self.start()
        tasks = [asyncio.create_task(self._tick_loop(tick_seconds, max_ticks))]
        if bridge is not None:
            tasks.append(asyncio.create_task(self._stream_loop(bridge, max_events)))
        ladder_task = asyncio.create_task(self._ladder_loop(ladder_resolution_s))
        try:
            await asyncio.gather(*tasks)
        finally:
            for t in tasks:
                t.cancel()
            ladder_task.cancel()
            await asyncio.gather(ladder_task, return_exceptions=True)
            # 남은 2단계 판정을 DB 가 닫히기 전에 마무리한다(호출자가 run 뒤에 DB 를
            # 닫는다). 각 태스크의 자체 마감 + 여유 1초를 넘기면 취소한다.
            await self.drain_phase2(timeout=self._phase2_deadline() + 1.0)

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
        if split.path == "/guardian-ack":
            gq = parse_qs(split.query)
            return await self._handle_guardian_ack(
                gq.get("s", [""])[0], gq.get("g", [""])[0], gq.get("sig", [""])[0]
            )
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
            # 예외 하나: 만료된 상황 제안의 재알림 링크는 사다리 확인으로만 받는다.
            acked = await self._ack_only_click(current, tier, sig, key, nonce)
            self._drop_nonce(pid)
            if acked:
                return "200 OK", "ok: acknowledged"
            return "403 Forbidden", "expired link"

        try:
            proposal = await self.feedback.verify_and_apply(
                pid, tier, sig, action, nonce=nonce
            )
        except FeedbackAuthError:
            return "403 Forbidden", "invalid signature or action"
        if proposal is None:
            return "404 Not Found", "no such proposal"
        # 거주자의 명시 응답(수락·거절·스누즈·괜찮아요 등)은 그 상황의 사다리를 멈춘다.
        if proposal.situation_id is not None:
            await self._ack_situation(proposal.situation_id, "resident")

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
