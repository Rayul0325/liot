"""피드백 수신구 노출 게이트 — dev 서명 키로는 열리지 않는다(fail-closed).

배경: `serve.py` 는 `LIOT_NOTIFY_SECRET` 미설정 시 소스에 공개된
`DEV_SIGNING_SECRET` 로 폴백한다. 피드백 수신구는 ntfy 액션 버튼이 폰에서
닿아야 하므로 노출되는데, 그 상태의 서명은 저장소를 읽은 누구나 위조해
수락/거절/undo 를 임의로 날릴 수 있다(제안 상태 변경).

이 파일은 2026-08-29 코드리뷰에서 드러난 **첫 판의 구멍 세 개**를 함께 고정한다.

  1) **빈 시크릿 우회** — 첫 판은 dev 키 문자열과 정확히 같을 때만 막았다.
     `LIOT_NOTIFY_SECRET=""` 이면 '진짜 키가 설정됨'으로 오인해 0.0.0.0 에
     열렸고, HMAC 은 빈 키로 계산됐다(누구나 위조 가능).
  2) **루프백 = 안전이라는 틀린 전제** — 프로세스 안에서는 앞단의 리버스
     프록시·터널을 알 수 없다. README 가 안내하는 폰 연동이 정확히 그
     형태라, 루프백 바인드로 게이트를 통과한 채 공개될 수 있었다.
  3) **가드가 배선에서 떨어질 수 있었음** — 호출부 한 줄을 지워도 310 테스트가
     전부 통과했다. 이제 가드와 개방이 한 함수(`open_feedback_endpoint`)에
     있고, 아래 테스트가 그 함수의 행동을 직접 본다.
"""

from __future__ import annotations

import asyncio
import inspect
from typing import cast

import pytest

from liot import serve
from liot.layers.l4_execution.notifier import Notifier
from liot.service import DEV_SIGNING_SECRET, LiotService

DEV = DEV_SIGNING_SECRET.decode("utf-8")
REAL = "a-real-long-random-secret-value"


class _FakeService:
    """serve_feedback 이 불렸는지만 기록하는 최소 대역(FeedbackServing 프로토콜)."""

    def __init__(self) -> None:
        self.opened: list[tuple[str, int]] = []

    async def serve_feedback(self, host: str, port: int) -> asyncio.AbstractServer:
        self.opened.append((host, port))
        # 대역이라 실제 서버를 만들지 않는다 — 호출부는 None 여부만 본다.
        return cast("asyncio.AbstractServer", object())


# ───────────────────────────────────── 1) dev 키 상수가 갈라지지 않는다
def test_dev_secret_literal_is_not_duplicated_across_modules() -> None:
    """dev 키가 모듈마다 따로 박히면, 하나만 바꿔도 게이트가 조용히 무력화된다."""
    # 진입점의 비교 대상은 상수에서 파생된다(별도 리터럴 없음).
    assert serve.DEV_SECRET_TEXT == DEV
    # LiotService 와 Notifier 의 기본값도 같은 상수여야 한다.
    service_default = inspect.signature(LiotService.__init__).parameters["secret"].default
    assert service_default is DEV_SIGNING_SECRET
    notifier_default = inspect.signature(Notifier.__init__).parameters["secret"].default
    assert notifier_default == DEV_SIGNING_SECRET, (
        "Notifier 의 dev 키 기본값이 DEV_SIGNING_SECRET 과 갈라졌다"
    )


# ───────────────────────────────────── 2) 시크릿 정규화(빈 값 = 미설정)
@pytest.mark.parametrize("raw", ["", "   ", "\t\n", None])
def test_blank_secret_resolves_to_dev_key(raw: str | None) -> None:
    """빈 값·공백·미설정은 전부 dev 키로 수렴한다 — '설정됨'으로 오인 금지."""
    assert serve.resolve_notify_secret(raw) == DEV


def test_real_secret_is_stripped_to_match_the_signing_side() -> None:
    """서명(ntfy_transport)이 .strip() 하므로 검증 쪽도 벗겨야 버튼이 동작한다."""
    assert serve.resolve_notify_secret("  s3cret  ") == "s3cret"


# ───────────────────────────────────── 3) 게이트 판정
def test_dev_key_is_blocked_regardless_of_host() -> None:
    """도달 가능성을 추정하지 않는다 — dev 키면 무조건 닫힌다."""
    reason = serve.feedback_block_reason(DEV)
    assert reason is not None
    assert "LIOT_NOTIFY_SECRET" in reason


def test_blank_secret_is_blocked_after_normalization() -> None:
    """첫 판의 구멍: 빈 시크릿이 게이트를 통과했다."""
    assert serve.feedback_block_reason(serve.resolve_notify_secret("")) is not None
    assert serve.feedback_block_reason(serve.resolve_notify_secret("   ")) is not None


def test_real_secret_is_never_blocked() -> None:
    assert serve.feedback_block_reason(REAL) is None


@pytest.mark.parametrize("padded", [DEV + " ", "  " + DEV, "\t" + DEV + "\n"])
def test_whitespace_padded_dev_key_is_still_the_dev_key(padded: str) -> None:
    """공개 함수는 정규화 전 값을 받을 수 있다 — 공백을 두른 dev 키도 dev 키다.

    2026-09-01 리뷰 R2-12: feedback_block_reason 의 .strip() 을 지워도 모든
    테스트가 통과했다. resolve_notify_secret 이 먼저 벗기는 호출부에선 무해하지만,
    이 함수에 원시 값을 넘기는 호출자에게는 dev 키 게이트가 공백 한 칸으로 열린다.
    """
    assert serve.feedback_block_reason(padded) is not None


def test_dev_key_opens_only_with_explicit_optin() -> None:
    """로컬 확인용 탈출구는 침묵이 아니라 선언을 요구한다."""
    assert serve.feedback_block_reason(DEV, allow_dev_key=True) is None


# ───────────────────────── 4) 가드와 개방이 한 함수 — 떼면 여기서 적색
async def test_endpoint_is_not_opened_with_dev_key() -> None:
    svc = _FakeService()
    server = await serve.open_feedback_endpoint(
        svc, host="0.0.0.0", port=8099, secret=DEV
    )
    assert server is None
    assert svc.opened == [], "dev 키인데 수신구가 열렸다"


async def test_endpoint_is_not_opened_with_blank_secret() -> None:
    svc = _FakeService()
    server = await serve.open_feedback_endpoint(
        svc, host="0.0.0.0", port=8099, secret=serve.resolve_notify_secret("")
    )
    assert server is None
    assert svc.opened == []


async def test_endpoint_opens_with_real_secret() -> None:
    svc = _FakeService()
    server = await serve.open_feedback_endpoint(
        svc, host="127.0.0.1", port=8099, secret=REAL
    )
    assert server is not None
    assert svc.opened == [("127.0.0.1", 8099)]


async def test_endpoint_opens_with_dev_key_when_explicitly_allowed() -> None:
    svc = _FakeService()
    server = await serve.open_feedback_endpoint(
        svc, host="127.0.0.1", port=8099, secret=DEV, allow_dev_key=True
    )
    assert server is not None
    assert svc.opened == [("127.0.0.1", 8099)]


async def test_port_zero_disables_the_endpoint() -> None:
    svc = _FakeService()
    assert await serve.open_feedback_endpoint(svc, host="127.0.0.1", port=0, secret=REAL) is None
    assert svc.opened == []
