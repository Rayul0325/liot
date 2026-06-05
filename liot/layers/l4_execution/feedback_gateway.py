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

__all__ = ["FeedbackAuthError", "FeedbackGateway", "verify_deeplink_sig"]


class FeedbackAuthError(Exception):
    """서명 검증 실패(위조/변조/형식 오류)."""


def verify_deeplink_sig(secret: bytes, proposal_id: str, tier: str | int, sig: str) -> bool:
    """sign_deeplink 와 동일 규약으로 HMAC-SHA256 서명을 검증한다.

    서명 대상 = f"{proposal_id}:{tier_int}". 상수 시간 비교(compare_digest).
    """
    try:
        tier_int = int(tier)
    except (TypeError, ValueError):
        return False
    message = f"{proposal_id}:{tier_int}".encode("utf-8")
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
        """서명 검증 후 피드백을 적용한다. 서명 불일치면 FeedbackAuthError.

        알 수 없는 action 은 FeedbackAuthError(시스템 내부 매핑 밖 입력 거부).
        """
        if not verify_deeplink_sig(self._secret, proposal_id, tier, sig):
            raise FeedbackAuthError(f"invalid signature for proposal {proposal_id!r}")
        key = (action or "").strip().lower()
        if key not in self._ACTION_ALIASES:
            raise FeedbackAuthError(f"unknown feedback action: {action!r}")
        user_action = self._ACTION_ALIASES[key]
        feedback = FeedbackEvent(
            proposal_id=proposal_id,
            trace_id=f"fb-{proposal_id}",
            user_action=user_action,
            latency_ms=latency_ms,
        )
        return await self._svc.apply_feedback(feedback)
