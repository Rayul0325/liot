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
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Any

from liot.app import LiotApp
from liot.core.clock import Clock
from liot.layers.l2_context.preference_store import PreferenceStore
from liot.layers.l4_execution.feedback_gateway import FeedbackAuthError, FeedbackGateway
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

__all__ = ["DispatchResult", "LiotService"]

_DEDUP_WINDOW = timedelta(hours=2)


class DispatchResult:
    """단일 제안 처리 결과(관측·테스트용)."""

    __slots__ = ("proposal", "decision", "sent", "error")

    def __init__(
        self, proposal: "Proposal", decision: str, sent: bool, error: str | None = None
    ) -> None:
        self.proposal = proposal
        self.decision = decision  # IMMEDIATE | DIGEST | SUPPRESS
        self.sent = sent
        self.error = error

    def __repr__(self) -> str:  # pragma: no cover - 디버그 편의
        return f"DispatchResult(decision={self.decision!r}, sent={self.sent}, error={self.error!r})"


class LiotService:
    """파이프라인 + 영속 + 알림 + 만료 + 피드백을 잇는 상시 서비스."""

    def __init__(
        self,
        app: LiotApp,
        db: "aiosqlite.Connection",
        *,
        home_id: str = "singleton",
        clock: Clock | None = None,
        notifier: Notifier | None = None,
        transport: NtfyTransport | None = None,
        topic: str = "liot",
        secret: bytes | str = b"liot-dev-signing-key",
        budget_per_hour: int = 6,
    ) -> None:
        self.app = app
        self.clock = clock or app.clock
        self.home_id = home_id
        self.proposals = ProposalService(db, home_id=home_id, clock=self.clock)
        self.prefs = PreferenceStore(home_id, db=db)
        # 알림 서명과 피드백 검증은 동일 비밀키를 써야 한다. notifier 를 직접
        # 주입하면 그 비밀키가 secret 인자와 일치해야 피드백 버튼이 검증된다.
        self.notifier = notifier or Notifier(secret=secret, topic=topic, clock=self.clock)
        self.transport = transport
        self.topic = topic
        self.feedback = FeedbackGateway(secret, self.proposals)
        # 발화 빈도 상태(인메모리; NotificationBudget 과 동일 성격).
        self._budget = NotificationBudget(max_per_hour=budget_per_hour)
        self._budget_hour: str | None = None
        self._recent_notified: dict[str, datetime] = {}

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
    async def dispatch(self, proposal: "Proposal") -> DispatchResult:
        """제안을 영속화하고 발화 게이트를 적용해 (가능하면) 실제 송신한다."""
        await self.proposals.save(proposal)

        now_dt = self.clock.now_dt()
        self._roll_budget(now_dt)
        ctx = await self._build_context(now_dt)
        decision, sanitized = self.notifier.notify(proposal, ctx, self._budget)

        if decision != "IMMEDIATE" or sanitized is None:
            return DispatchResult(proposal, decision, sent=False)

        # IMMEDIATE: 정제분을 기록하고, transport 가 있으면 실제 송신(fail-closed).
        await self.proposals.record_notification(sanitized, now=self.clock.now_utc())
        self._recent_notified[dedup_key(proposal)] = now_dt

        if self.transport is None:
            return DispatchResult(proposal, decision, sent=False)  # sanitize-only 모드
        try:
            self.transport.send(self.topic, sanitized)
            return DispatchResult(proposal, decision, sent=True)
        except NtfyError as exc:
            # 송신 실패는 치명적이지 않다 — 제안은 pending 으로 남아 재시도 가능.
            return DispatchResult(proposal, decision, sent=False, error=str(exc))

    # ------------------------------------------------------------------
    # 진입점들
    # ------------------------------------------------------------------
    async def handle_reading(self, reading: "SensorReading") -> DispatchResult | None:
        """단일 센서 읽기를 파이프라인에 흘리고, 제안이 나오면 디스패치."""
        proposal = await self.app.process_reading(reading)
        if proposal is None:
            return None
        return await self.dispatch(proposal)

    async def run_tick(self) -> DispatchResult | None:
        """능동 틱 1회 + 만료 스윕. 능동 제안이 나오면 디스패치."""
        await self.proposals.expire_due()
        proposal = await self.app.tick()
        if proposal is None:
            return None
        return await self.dispatch(proposal)

    async def run(
        self,
        bridge: "HABridge | None" = None,
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

    async def _stream_loop(self, bridge: "HABridge", max_events: int | None) -> None:
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
    async def serve_feedback(self, host: str = "127.0.0.1", port: int = 8099) -> "asyncio.AbstractServer":
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
        except (asyncio.TimeoutError, ValueError):
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
        try:
            proposal = await self.feedback.verify_and_apply(pid, tier, sig, action)
        except FeedbackAuthError:
            return "403 Forbidden", "invalid signature or action"
        if proposal is None:
            return "404 Not Found", "no such proposal"
        return "200 OK", f"ok: {proposal.state}"
