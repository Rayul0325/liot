"""알림기(Notifier): 단일 발화 게이트 + egress 통과 알림.

발화 여부는 ``gate_pipeline`` 한 함수로 결정한다. 게이트는 **정해진 순서**로
필터를 적용하며, 첫 결정에서 멈춘다(early-return):

    1. charter        — 사용자가 전면 OFF(헌장)면 SUPPRESS.
    2. mute           — 카테고리/엔티티 음소거면 SUPPRESS (perishable 보다 우선).
    3. recently_rejected — 최근 30일 내 같은 류를 거절했으면 SUPPRESS.
    4. dedup          — 최근 2시간 내 동일 발화면 SUPPRESS.
    5. quiet_hours    — 야간(quiet)에는 EMBER 만 통과, 그 외 SUPPRESS.
    6. ambient        — AMBIENT 위험도는 즉시 알리지 않고 DIGEST(묶음).
    7. budget         — 시간당 예산 초과면 DIGEST.
    8. perishable 우회 — 위 4~7 에서 막혀도 perishable(시의성) 이면 IMMEDIATE
                         로 승격(단, charter/mute/rejected 는 우회 못 함).

발화(IMMEDIATE)로 결정되면 ``notify`` 가 NotificationPayload 를 만들고
반드시 ``emit_external(NTFY, ...)`` 를 통과시킨다 → device/room/rationale 가
구조적으로 제거된 페이로드만 외부로 나간다.

signed_deeplink 는 HMAC-SHA256 으로 서명한다(위조 방지).
"""

from __future__ import annotations

import hashlib
import hmac
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Literal

from liot.core.contracts import NotificationPayload, Proposal
from liot.core.egress_gate import Destination, emit_external
from liot.core.risk import RiskTier

if TYPE_CHECKING:  # pragma: no cover
    import aiosqlite

    from liot.core.clock import Clock

__all__ = [
    "GateDecision",
    "NotificationBudget",
    "NotificationContext",
    "Notifier",
    "gate_pipeline",
    "sign_deeplink",
    "sign_feedback",
]

GateDecision = Literal["IMMEDIATE", "DIGEST", "SUPPRESS"]

# dedup 창(초): 2시간. recently_rejected 창(일): 30일.
_DEDUP_WINDOW_SEC = 2 * 60 * 60
_REJECT_WINDOW_DAYS = 30


@dataclass
class NotificationBudget:
    """시간당 즉시 발화 예산. 초과 시 DIGEST 로 강등."""

    max_per_hour: int = 6
    used_this_hour: int = 0

    def exhausted(self) -> bool:
        return self.used_this_hour >= self.max_per_hour

    def consume(self) -> None:
        self.used_this_hour += 1


@dataclass
class NotificationContext:
    """게이트 판단에 필요한 가벼운 컨텍스트(순수 입력, 부수효과 없음).

    실제 운영에서는 preference_store/notifications 테이블에서 채워지지만,
    게이트 로직을 결정론적·테스트 가능하게 두기 위해 평면 구조로 받는다.
    """

    charter_enabled: bool = True
    # 음소거된 (category) 또는 (entity_id) 집합. 둘 중 하나라도 걸리면 mute.
    muted_categories: frozenset[str] = field(default_factory=frozenset)
    muted_entities: frozenset[str] = field(default_factory=frozenset)
    # 최근 30일 내 거절된 dedup-key 집합.
    recently_rejected_keys: frozenset[str] = field(default_factory=frozenset)
    # 최근 2시간 내 이미 발화한 dedup-key 집합.
    recent_notified_keys: frozenset[str] = field(default_factory=frozenset)
    # quiet hours(야간) 여부. 보통 clock.time_bucket()=='night' 로 채운다.
    quiet_hours: bool = False


def dedup_key(proposal: Proposal) -> str:
    """제안의 중복/거절 판정 키. (capability, action, entity_id, category)."""
    a = proposal.action
    return f"{a.capability}:{a.action}:{a.entity_id}:{a.category}"


def gate_pipeline(
    proposal: Proposal,
    ctx: NotificationContext,
    budget: NotificationBudget,
    now: str,
) -> GateDecision:
    """단일 발화 게이트. 정해진 순서로 평가하고 첫 결정에서 멈춘다.

    반환: 'IMMEDIATE' | 'DIGEST' | 'SUPPRESS'.
    """
    tier = RiskTier.from_str(proposal.risk_tier)
    key = dedup_key(proposal)
    # 시의성(perishable) **또는 안전 임계(EMBER)** 는 throttle 게이트
    # (dedup/quiet/ambient/budget)를 우회한다. 안전 알림이 중복/예산/야간으로
    # 묶이거나 삼켜지면 Safety-Never-Silent(불변식 6)의 정신이 전달 단계에서
    # 깨진다 → EMBER 는 언제나 즉시 발화(단, charter/mute/recently_rejected
    # 같은 *명시적 사용자 옵트아웃* 은 여전히 존중).
    bypass = bool(proposal.perishable) or tier == RiskTier.EMBER

    # 1) charter: 전면 OFF 면 무조건 침묵. 우회 못 함.
    if not ctx.charter_enabled:
        return "SUPPRESS"

    # 2) mute: 카테고리/엔티티 음소거. 우회 불가.
    if (
        proposal.action.category in ctx.muted_categories
        or proposal.action.entity_id in ctx.muted_entities
    ):
        return "SUPPRESS"

    # 3) recently_rejected(30일): 사용자가 최근 거절한 류면 침묵. 우회 불가.
    if key in ctx.recently_rejected_keys:
        return "SUPPRESS"

    # --- 여기부터는 bypass(시의성 또는 EMBER)면 IMMEDIATE 로 승격 ---

    # 4) dedup(2시간): 최근 동일 발화가 있으면 묶기/침묵.
    if key in ctx.recent_notified_keys:
        return "IMMEDIATE" if bypass else "SUPPRESS"

    # 5) quiet_hours: 야간에는 EMBER(불씨)만 통과. 그 외는 침묵.
    if ctx.quiet_hours and tier < RiskTier.EMBER:
        return "IMMEDIATE" if bypass else "SUPPRESS"

    # 6) ambient(잔잔): 즉시 알리지 않고 묶음(DIGEST).
    if tier == RiskTier.AMBIENT:
        return "IMMEDIATE" if bypass else "DIGEST"

    # 7) budget: 시간당 예산 초과면 묶음(DIGEST). EMBER 는 우회(안전은 throttle 없음).
    if budget.exhausted():
        return "IMMEDIATE" if bypass else "DIGEST"

    return "IMMEDIATE"


def sign_deeplink(secret: bytes, proposal_id: str, risk_tier: RiskTier) -> str:
    """제안 '열기' 딥링크를 HMAC-SHA256 으로 서명한다(위조 방지).

    형식: ``liot://p/<proposal_id>?t=<tier>&sig=<hmac_hex>``.
    서명 대상은 ``<proposal_id>:<tier_int>`` 의 정규 문자열. 이 딥링크는
    '앱에서 열기'(상태 변경 없음)용이며, 상태를 바꾸는 피드백 액션은
    action 까지 묶은 별도 서명(sign_feedback)을 쓴다.
    """
    tier_int = int(RiskTier.from_str(risk_tier))
    message = f"{proposal_id}:{tier_int}".encode("utf-8")
    sig = hmac.new(secret, message, hashlib.sha256).hexdigest()
    return f"liot://p/{proposal_id}?t={tier_int}&sig={sig}"


def sign_feedback(secret: bytes, proposal_id: str, risk_tier: RiskTier, action: str) -> str:
    """피드백 액션 서명. 서명 대상 = ``<proposal_id>:<tier_int>:<action>``.

    action 을 서명에 묶으므로, 한 버튼의 서명으로 다른 액션(수락↔거절)을
    수행할 수 없다 — 도청자가 사용자의 선택을 뒤집지 못한다.
    """
    tier_int = int(RiskTier.from_str(risk_tier))
    message = f"{proposal_id}:{tier_int}:{(action or '').strip().lower()}".encode("utf-8")
    return hmac.new(secret, message, hashlib.sha256).hexdigest()


class Notifier:
    """발화 게이트를 적용하고, 통과 시 egress_gate 로 알림을 내보낸다.

    실제 네트워크 송신은 하지 않는다(egress_gate 와 동일하게 '무엇이 나가도
    되는가'까지만 책임). 발화 시 NotificationPayload 를 만들고 반드시
    emit_external(NTFY, ...) 를 통과시켜 device/room/rationale 부재를 보장한다.
    """

    def __init__(
        self,
        secret: bytes | str = b"liot-dev-signing-key",
        topic: str = "liot",
        channel: str = "ntfy",
        clock: "Clock | None" = None,
    ) -> None:
        self._secret = secret.encode("utf-8") if isinstance(secret, str) else secret
        self._topic = topic
        self._channel = channel
        self._clock = clock

    def decide(
        self,
        proposal: Proposal,
        ctx: NotificationContext,
        budget: NotificationBudget,
        now: str | None = None,
    ) -> GateDecision:
        """발화 결정만 반환(부수효과 없음)."""
        now = now or self._now()
        return gate_pipeline(proposal, ctx, budget, now)

    def build_payload(self, proposal: Proposal) -> NotificationPayload:
        """제안에서 외부 알림 페이로드를 만든다(rationale/device/room 없음).

        라벨은 위험도 한국어 라벨만 사용한다(엔티티명/방이름 누설 금지).
        """
        signed = sign_deeplink(self._secret, proposal.proposal_id, proposal.risk_tier)
        tier = RiskTier.from_str(proposal.risk_tier)
        return NotificationPayload(
            channel=self._channel,
            proposal_id=proposal.proposal_id,
            risk_tier=tier,
            glyph=tier.glyph,
            label=tier.label_ko,  # 위험도 라벨만. 디바이스/방 정보 없음.
            signed_deeplink=signed,
            topic=self._topic,
        )

    def notify(
        self,
        proposal: Proposal,
        ctx: NotificationContext,
        budget: NotificationBudget,
        now: str | None = None,
    ) -> tuple[GateDecision, dict[str, Any] | None]:
        """게이트 적용 후, IMMEDIATE 면 egress_gate 로 정제 페이로드를 만든다.

        반환: (decision, sanitized_payload | None).
          - IMMEDIATE: NotificationPayload -> emit_external(NTFY) 통과 결과 반환.
                       budget.consume() 으로 예산 차감.
          - DIGEST/SUPPRESS: (decision, None).

        emit_external(NTFY) 가 화이트리스트 키만 남기므로, 설령 payload 에
        부가 키가 있어도 device/room/rationale 은 구조적으로 제거된다.
        """
        now = now or self._now()
        decision = gate_pipeline(proposal, ctx, budget, now)
        if decision != "IMMEDIATE":
            return decision, None

        payload = self.build_payload(proposal)
        raw = payload.model_dump()
        # risk_tier 는 외부 송신용 정수로 직렬화(IntEnum -> int).
        raw["risk_tier"] = int(payload.risk_tier)

        sanitized = emit_external(
            Destination.NTFY,
            raw,
            trace_id=proposal.proposal_id,
        )
        budget.consume()
        return decision, sanitized

    def _now(self) -> str:
        if self._clock is not None:
            return self._clock.now_utc()
        from liot.core.clock import Clock

        return Clock().now_utc()
