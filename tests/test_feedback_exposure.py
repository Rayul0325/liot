"""피드백 수신구 노출 게이트 — dev 서명 키로는 외부에 열리지 않는다(fail-closed).

배경: `serve.py` 는 `LIOT_NOTIFY_SECRET` 미설정 시 소스에 공개된
`DEV_SIGNING_SECRET` 로 폴백했다. 피드백 수신구는 ntfy 액션 버튼이 폰에서
닿아야 하므로 외부에 노출되는데, 그 상태의 서명은 저장소를 읽은 누구나 위조해
수락/거절/undo 를 임의로 날릴 수 있었다(제안 상태 변경). ntfy transport 는 키가
없으면 버튼을 안 만드는 fail-closed 였던 반면 서비스 진입점만 fail-open 이었다.

이 테스트는 그 비대칭을 고정한다: dev 키인 채로 외부에서 닿는 구성이면
수신구를 열지 않고, 실제 비밀키가 있으면 아무것도 막지 않는다.
"""

from __future__ import annotations

import pytest

from liot import serve
from liot.service import DEV_SIGNING_SECRET, LiotService

DEV = DEV_SIGNING_SECRET.decode("utf-8")
REAL = "a-real-long-random-secret-value"


# ---------------------------------------------------------------- 상수 드리프트
def test_dev_secret_constant_is_single_sourced() -> None:
    # serve 의 비교 대상과 LiotService 의 기본값이 갈라지면 게이트가 조용히 무력화된다.
    assert serve.DEV_SECRET_TEXT == DEV
    assert LiotService.__init__.__kwdefaults__["secret"] is DEV_SIGNING_SECRET


# ------------------------------------------------------------------- 루프백 판정
@pytest.mark.parametrize("host", ["127.0.0.1", "127.0.0.5", "localhost", "LocalHost", "::1", "[::1]"])
def test_loopback_hosts_are_loopback(host: str) -> None:
    assert serve.is_loopback_host(host) is True


@pytest.mark.parametrize(
    "host",
    [
        "0.0.0.0",  # 전체 인터페이스
        "",  # 빈 값 = 전체 인터페이스 바인드
        "   ",
        "192.168.0.10",  # LAN
        "203.0.113.7",  # 공인 IP
        "liot.example.com",  # 해석 못 하는 호스트명은 보수적으로 외부
    ],
)
def test_non_loopback_hosts_are_external(host: str) -> None:
    assert serve.is_loopback_host(host) is False


# ------------------------------------------------------------- dev 키 + 노출 조합
def test_dev_secret_on_loopback_is_allowed() -> None:
    # 로컬 데모(기본 구성)는 그대로 열려야 한다 — 게이트가 개발을 막으면 안 된다.
    assert serve.feedback_block_reason(DEV, "127.0.0.1") is None


@pytest.mark.parametrize("host", ["0.0.0.0", "", "192.168.0.10"])
def test_dev_secret_on_external_bind_is_blocked(host: str) -> None:
    reason = serve.feedback_block_reason(DEV, host)
    assert reason is not None
    assert "LIOT_NOTIFY_SECRET" in reason


def test_dev_secret_with_external_button_url_is_blocked() -> None:
    # 터널 케이스: 바인드는 루프백이지만 알림 버튼이 공개 주소를 가리킨다.
    reason = serve.feedback_block_reason(DEV, "127.0.0.1", "https://liot.example.com/feedback")
    assert reason is not None
    assert "liot.example.com" in reason


def test_dev_secret_with_loopback_button_url_is_allowed() -> None:
    assert serve.feedback_block_reason(DEV, "127.0.0.1", "http://127.0.0.1:8099") is None


# --------------------------------------------------------- 실제 비밀키면 막지 않음
@pytest.mark.parametrize("host", ["0.0.0.0", "192.168.0.10", "127.0.0.1"])
def test_real_secret_never_blocks(host: str) -> None:
    assert serve.feedback_block_reason(REAL, host, "https://liot.example.com/feedback") is None


def test_secret_comparison_ignores_surrounding_whitespace() -> None:
    # env 값에 공백이 섞여도 dev 키는 dev 키로 인식돼야 한다(우회 방지).
    assert serve.feedback_block_reason(f"  {DEV}  ", "0.0.0.0") is not None
