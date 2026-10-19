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

파급도(severity)는 긴급도를 **더하기만** 한다(기존 우회는 하나도 없애지 않는다):
  - ``severity == CRITICAL``(위험) 상황 알림은 mute·recently_rejected 도 우회한다.
    헌장(charter) OFF 만 존중한다 — 가스 알림을 한 번 거절했다고 다음 가스
    상황이 조용해지면 안 된다.
  - 4~7 의 우회 조건 = perishable OR EMBER OR severity == CRITICAL.
  - severity 가 None 이면 판정은 예전과 똑같다.

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

from liot.core.contracts import GuardianNotificationPayload, NotificationPayload, Proposal
from liot.core.egress_gate import Destination, emit_external
from liot.core.risk import RiskTier
from liot.core.severity import SeverityLevel

if TYPE_CHECKING:  # pragma: no cover

    from liot.core.clock import Clock

__all__ = [
    "GateDecision",
    "NotificationBudget",
    "NotificationContext",
    "Notifier",
    "gate_pipeline",
    "guardian_ack_link",
    "sign_deeplink",
    "sign_feedback",
    "sign_guardian_ack",
    "tier_int",
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
    now: str,  # 현재 미사용 — 아래 주석 참조
) -> GateDecision:
    """단일 발화 게이트. 정해진 순서로 평가하고 첫 결정에서 멈춘다.

    반환: 'IMMEDIATE' | 'DIGEST' | 'SUPPRESS'.

    ``now`` 는 **지금은 쓰이지 않는다**. dedup(2시간)·recently_rejected(30일) 창은
    호출자가 미리 계산해 ``ctx.recent_notified_keys``/``recently_rejected_keys`` 로
    넘기기 때문이다(서비스가 DB 조회로 채운다). 시각이 판단에 들어오는 날을 위해
    시그니처만 남겨 뒀다 — 2026-09-16 뮤테이션 스윕에서 이 인자를 바꿔도 아무 일이
    일어나지 않는 것으로 확인됐으니, '시각이 게이트에 반영된다'고 읽지 말 것.
    """
    tier = RiskTier.from_str(proposal.risk_tier)
    key = dedup_key(proposal)
    # 시의성(perishable) **또는 안전 임계(EMBER)** 는 throttle 게이트
    # (dedup/quiet/ambient/budget)를 우회한다. 안전 알림이 중복/예산/야간으로
    # 묶이거나 삼켜지면 Safety-Never-Silent(불변식 6)의 정신이 전달 단계에서
    # 깨진다 → EMBER 는 언제나 즉시 발화(단, charter/mute/recently_rejected
    # 같은 *명시적 사용자 옵트아웃* 은 여전히 존중).
    #
    # 파급도 '위험'(CRITICAL) 상황 알림도 같은 우회를 받는다. 파급도는 긴급도를
    # 더하기만 하므로, 위의 두 우회 사유는 그대로 둔 채 OR 로 덧붙인다.
    critical = proposal.severity == SeverityLevel.CRITICAL
    bypass = bool(proposal.perishable) or tier == RiskTier.EMBER or critical

    # 1) charter: 전면 OFF 면 무조건 침묵. 우회 못 함(CRITICAL 이라도).
    if not ctx.charter_enabled:
        return "SUPPRESS"

    # 2) mute: 카테고리/엔티티 음소거. CRITICAL 상황 알림만 우회한다.
    if not critical and (
        proposal.action.category in ctx.muted_categories
        or proposal.action.entity_id in ctx.muted_entities
    ):
        return "SUPPRESS"

    # 3) recently_rejected(30일): 사용자가 최근 거절한 류면 침묵.
    #    CRITICAL 상황 알림만 우회한다(가스 거절 뒤 다음 가스 상황도 즉시 발화).
    if not critical and key in ctx.recently_rejected_keys:
        return "SUPPRESS"

    # --- 여기부터는 bypass(시의성·EMBER·CRITICAL)면 IMMEDIATE 로 승격 ---

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


def tier_int(value: RiskTier | str | int) -> int:
    """서명 대상에 쓰는 tier 정수. 숫자 문자열("3")도 받는다.

    검증 쪽(verify_feedback_sig)은 예전부터 ``int(tier)`` 로 숫자 문자열을 받는데
    서명 쪽은 ``RiskTier.from_str`` 만 써서 이름("ember")만 받았다. 딥링크 URL 의
    ``t=`` 는 항상 숫자 문자열이라, 그 값을 그대로 서명 함수에 넘기면 서명은
    터지고 검증은 통과하는 비대칭이 된다 — 그래서 둘의 강제변환을 맞춘다.

    범위 검사는 유지한다: 0~3 밖의 숫자는 ValueError. 예전 구현(``RiskTier.from_str``
    단독)은 이름만 받는 대신 범위는 보고 있었는데, 숫자 문자열을 받으려고 int() 를
    앞에 두면서 그 검사가 사라졌다(2026-09-13 리뷰 R-4).
    """
    if isinstance(value, RiskTier):
        return int(value)
    try:
        number = int(value)
    except (TypeError, ValueError):
        return int(RiskTier.from_str(value))
    return int(RiskTier(number))  # 범위 밖이면 ValueError


def sign_deeplink(
    secret: bytes,
    proposal_id: str,
    risk_tier: RiskTier | str | int,
    nonce: str = "",
) -> str:
    """제안 '열기' 딥링크를 HMAC-SHA256 으로 서명한다(위조 방지).

    형식: ``liot://p/<proposal_id>?t=<tier>&sig=<hmac_hex>[&n=<nonce>]``.
    서명 대상은 ``<proposal_id>:<tier_int>`` 의 정규 문자열. 이 딥링크는
    '앱에서 열기'(상태 변경 없음)용이며, 상태를 바꾸는 피드백 액션은
    action 과 nonce 까지 묶은 별도 서명(sign_feedback)을 쓴다.

    nonce 는 서명 대상이 아니라 **운반물**이다 — transport 가 이걸 읽어 액션
    버튼 서명을 만든다. 위조 걱정이 없는 이유: nonce 를 바꿔치기해도 그 nonce
    에 대한 유효한 액션 서명을 만들 수 없다(비밀키가 필요하다).
    """
    t_int = tier_int(risk_tier)
    message = f"{proposal_id}:{t_int}".encode()
    sig = hmac.new(secret, message, hashlib.sha256).hexdigest()
    url = f"liot://p/{proposal_id}?t={t_int}&sig={sig}"
    return f"{url}&n={nonce}" if nonce else url


def sign_feedback(
    secret: bytes,
    proposal_id: str,
    risk_tier: RiskTier | str | int,
    action: str,
    nonce: str = "",
) -> str:
    """피드백 액션 서명. 대상 = ``<proposal_id>:<tier_int>:<action>:<nonce>``.

    - **action 바인딩**: 한 버튼의 서명으로 다른 액션(수락↔거절)을 수행할 수
      없다 — 도청자가 사용자의 선택을 뒤집지 못한다.
    - **nonce 바인딩**: 서비스가 발급한 1회용 토큰을 함께 묶는다. 서비스는
      성공한 피드백마다 nonce 를 소비하므로, 캡처한 링크를 다시 던져도 통하지
      않는다. 이게 없던 시절엔 수락 링크 하나를 두 번 재생하는 것만으로
      EMBER 의 DOUBLE_CONFIRM(1차 보류 → 2차 실행)이 충족됐다.

    tier 강제변환은 검증 쪽과 같다(tier_int) — 숫자 문자열도 받는다.
    """
    message = f"{proposal_id}:{tier_int(risk_tier)}:{(action or '').strip().lower()}:{nonce}".encode()
    return hmac.new(secret, message, hashlib.sha256).hexdigest()


def sign_guardian_ack(secret: bytes, situation_id: str, guardian_id: str) -> str:
    """보호자 확인 링크 서명. 대상 = ``guardian-ack:<situation_id>:<guardian_id>``.

    보호자마다 다른 서명이다 — 한 보호자의 링크로 다른 보호자인 척 확인할 수 없다.
    접두사로 피드백 서명(``<pid>:<tier>:<action>:<nonce>``)과 대상 공간을 가른다.
    nonce 를 묶지 않는 이유: 확인은 멱등이다(두 번 눌러도 사다리가 한 번 멈출 뿐).
    """
    message = f"guardian-ack:{situation_id}:{guardian_id}".encode()
    return hmac.new(secret, message, hashlib.sha256).hexdigest()


def guardian_ack_link(secret: bytes, situation_id: str, guardian_id: str) -> str:
    """보호자 확인 링크. 형식: ``liot://ack/<situation_id>?g=<guardian_id>&sig=<hmac_hex>``.

    알림을 여는(Click) 것으로는 확인되지 않는다 — transport 가 이 링크를 별도
    액션 버튼(``/guardian-ack``)으로만 옮긴다.
    """
    sig = sign_guardian_ack(secret, situation_id, guardian_id)
    return f"liot://ack/{situation_id}?g={guardian_id}&sig={sig}"


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
        clock: Clock | None = None,
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

    def build_payload(self, proposal: Proposal, nonce: str = "") -> NotificationPayload:
        """제안에서 외부 알림 페이로드를 만든다(rationale/device/room 없음).

        라벨은 위험도 한국어 라벨만 사용한다(엔티티명/방이름 누설 금지).
        파급도가 있으면(상황 알림) 파급도 한국어 라벨(severity_label)만 덧붙인다 —
        상황 id·근거·디바이스는 싣지 않는다.
        """
        signed = sign_deeplink(
            self._secret, proposal.proposal_id, proposal.risk_tier, nonce
        )
        tier = RiskTier.from_str(proposal.risk_tier)
        return NotificationPayload(
            channel=self._channel,
            proposal_id=proposal.proposal_id,
            risk_tier=tier,
            glyph=tier.glyph,
            label=tier.label_ko,  # 위험도 라벨만. 디바이스/방 정보 없음.
            signed_deeplink=signed,
            topic=self._topic,
            severity_label=(
                proposal.severity.label_ko if proposal.severity is not None else None
            ),
        )

    def notify(
        self,
        proposal: Proposal,
        ctx: NotificationContext,
        budget: NotificationBudget,
        now: str | None = None,
        nonce: str = "",
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

        sanitized = self.sanitize(proposal, nonce)
        budget.consume()
        return decision, sanitized

    def sanitize(self, proposal: Proposal, nonce: str = "") -> dict[str, Any]:
        """게이트를 거치지 않고 정제 페이로드만 만든다(egress 는 반드시 통과).

        2차 확인 요청처럼 **사용자 행동에 대한 직접 응답**으로 나가는 알림에
        쓴다. 발화 게이트/예산은 '먼저 말 거는' 알림을 위한 장치라, 사람이
        방금 누른 버튼에 대한 후속 확인까지 억제하면 흐름이 끊긴다.
        """
        payload = self.build_payload(proposal, nonce)
        raw = payload.model_dump()
        # risk_tier 는 외부 송신용 정수로 직렬화(IntEnum -> int).
        raw["risk_tier"] = int(payload.risk_tier)
        # 파급도가 없으면 severity_label 키 자체를 빼서, 페이로드가 예전과 똑같게
        # 둔다(None 이 실려 나가면 transport·기록 쪽이 '값 있음'으로 오해할 여지).
        if raw.get("severity_label") is None:
            raw.pop("severity_label", None)
        return emit_external(Destination.NTFY, raw, trace_id=proposal.proposal_id)

    def build_guardian_payload(
        self,
        *,
        situation_label: str,
        home_alias: str,
        at: str,
        severity: SeverityLevel,
        situation_id: str | None = None,
        guardian_id: str | None = None,
    ) -> GuardianNotificationPayload:
        """상황 요약 알림(보호자 전달·해소·119 안내) 페이로드를 만든다.

        싣는 것은 상황 종류 라벨·집 별칭·시각·서명된 확인 링크·파급도 라벨뿐이다.
        계약(extra="forbid")에 기기·방·근거·거주자 특성 칸이 없어 구조적으로 못 싣는다.
        situation_id 와 guardian_id 를 둘 다 주면 그 보호자 전용 확인 링크를 싣고,
        아니면(해소 알림·거주자 119 안내) 링크 없이 보낸다.
        """
        link = (
            guardian_ack_link(self._secret, situation_id, guardian_id)
            if situation_id and guardian_id
            else ""
        )
        return GuardianNotificationPayload(
            channel=self._channel,
            situation_label=situation_label,
            home_alias=home_alias,
            at=at,
            signed_ack_link=link,
            severity_label=severity.label_ko,
        )

    def sanitize_summary(
        self, payload: GuardianNotificationPayload, *, trace_id: str
    ) -> dict[str, Any]:
        """요약 알림 페이로드를 egress(NTFY) 로 정제한다.

        확인 링크는 ``signed_deeplink`` 키로 옮겨 싣는다 — 이미 허용된 키이고, 그
        값(HMAC 16진수)은 PII 스캔에서 빠지는 불투명 토큰 자리라 무작위 서명이 우연히
        전화번호 패턴에 걸려 안전 알림이 막히는 일이 없다. 링크가 없으면 키를 뺀다.
        """
        raw = payload.model_dump()
        link = raw.pop("signed_ack_link", "")
        if link:
            raw["signed_deeplink"] = link
        return emit_external(Destination.NTFY, raw, trace_id=trace_id)

    def _now(self) -> str:
        if self._clock is not None:
            return self._clock.now_utc()
        from liot.core.clock import Clock

        return Clock().now_utc()
