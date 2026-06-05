"""피드백 수신구(USER → 시스템) — 서명 검증 후 제안 상태 전이.

알림의 수락/거절 버튼은 서명 딥링크의 3요소(proposal_id, tier, sig)를 그대로
실어 보낸다. 게이트웨이는 ``sign_deeplink`` 와 동일한 HMAC 로 재계산해
``hmac.compare_digest`` 로 검증한다 — 서명이 맞아야만 ``apply_feedback`` 으로
상태를 전이한다. 위조/변조된 요청은 ``FeedbackAuthError`` 로 거부된다.

이 모듈은 순수 로직(소켓 없음)이라 단위 테스트가 쉽다. 실제 HTTP 수신은
service 의 얇은 asyncio 어댑터가 담당한다(같은 이벤트 루프·DB 공유).
"""

from __future__ import annotations

import hashlib
import hmac
from typing import TYPE_CHECKING

from liot.core.contracts import FeedbackEvent

if TYPE_CHECKING:  # pragma: no cover
    from liot.core.contracts import Proposal

    from .proposal_service import ProposalService

__all__ = [
    "FeedbackAuthError",
    "FeedbackGateway",
    "verify_deeplink_sig",
    "verify_feedback_sig",
]


class FeedbackAuthError(Exception):
    """서명 검증 실패(위조/변조/형식 오류)."""


def verify_deeplink_sig(secret: bytes, proposal_id: str, tier: str | int, sig: str) -> bool:
    """sign_deeplink('열기' 딥링크) 서명 검증. 대상 = f"{pid}:{tier_int}".

    상태를 바꾸지 않는 '열기'용. 상태 전이 피드백은 verify_feedback_sig 사용.
    """
    try:
        tier_int = int(tier)
    except (TypeError, ValueError):
        return False
    message = f"{proposal_id}:{tier_int}".encode("utf-8")
    expected = hmac.new(secret, message, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, str(sig))


def verify_feedback_sig(
    secret: bytes, proposal_id: str, tier: str | int, action: str, sig: str
) -> bool:
    """sign_feedback 규약 검증. 대상 = f"{pid}:{tier_int}:{action}".

    action 을 서명에 묶으므로 한 버튼 서명으로 다른 액션을 수행할 수 없다.
    상수 시간 비교(compare_digest).
    """
    try:
        tier_int = int(tier)
    except (TypeError, ValueError):
        return False
    message = f"{proposal_id}:{tier_int}:{(action or '').strip().lower()}".encode("utf-8")
    expected = hmac.new(secret, message, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, str(sig))


class FeedbackGateway:
    """서명 검증 + ProposalService.apply_feedback 연결."""

    # 버튼/요청의 action → FeedbackEvent.user_action(ProposalService 가 매핑).
    _ACTION_ALIASES = {
        "accept": "accepted",
        "accepted": "accepted",
        "dismiss": "dismissed",
        "dismissed": "dismissed",
        "reject": "dismissed",
        "snooze": "snoozed",
        "stop": "stop",
    }

    def __init__(self, secret: bytes | str, proposal_service: "ProposalService") -> None:
        self._secret = secret.encode("utf-8") if isinstance(secret, str) else secret
        self._svc = proposal_service

    async def verify_and_apply(
        self,
        proposal_id: str,
        tier: str | int,
        sig: str,
        action: str,
        *,
        latency_ms: int | None = None,
    ) -> "Proposal | None":
        """서명(action-bound) 검증 후 피드백을 적용한다.

        - action 이 시스템 매핑 밖이면 FeedbackAuthError.
        - 서명이 (proposal_id, tier, action) 에 대해 유효하지 않으면 FeedbackAuthError.
          (action 까지 묶으므로 한 서명으로 다른 액션 수행 불가 — 선택 뒤집기 방어.)
        존재하지 않거나 타 홈 proposal 은 apply_feedback 이 None 을 반환하며
        feedback/audit 행을 남기지 않는다(테이블 오염 방지).
        """
        key = (action or "").strip().lower()
        if key not in self._ACTION_ALIASES:
            raise FeedbackAuthError(f"unknown feedback action: {action!r}")
        if not verify_feedback_sig(self._secret, proposal_id, tier, key, sig):
            raise FeedbackAuthError(
                f"invalid signature for proposal {proposal_id!r} action {key!r}"
            )
        user_action = self._ACTION_ALIASES[key]
        feedback = FeedbackEvent(
            proposal_id=proposal_id,
            trace_id=f"fb-{proposal_id}",
            user_action=user_action,
            latency_ms=latency_ms,
        )
        return await self._svc.apply_feedback(feedback)
