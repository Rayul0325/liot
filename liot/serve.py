"""LIOT 상시 데몬 진입점 — ``liot-serve``.

환경변수로 실배포를 구성한다(하드웨어/시크릿은 코드에 없음):

  필수 없음(전부 합리적 기본값). 자주 쓰는 것:
    LIOT_HOME_ID        : 테넌트 id(기본 'singleton').
    LIOT_DB_PATH        : SQLite 경로(기본 'liot.db'; ':memory:' 가능).
    LIOT_HA_URL         : HA WebSocket URL. 있으면 센서 스트림 소비.
    LIOT_HA_TOKEN       : HA 장기 액세스 토큰.
    LIOT_LLM_BACKEND    : 'ollama' 등(미설정 시 FakeLLM).
    LIOT_NTFY_URL       : ntfy 서버. 있으면 실제 알림 송신(미설정 시 sanitize-only).
    LIOT_NTFY_TOPIC     : 토픽(기본 'liot').
    LIOT_NTFY_FEEDBACK_URL : 알림 액션 버튼이 가리킬 피드백 base URL.
    LIOT_NOTIFY_SECRET  : 딥링크 HMAC 서명 비밀키. 미설정 시 소스에 공개된 dev
                          기본값이 쓰이며, 그 상태로 피드백 수신구가 외부에서
                          닿는 구성이면 수신구를 아예 열지 않는다(fail-closed).
    LIOT_TICK_SECONDS   : 능동 틱 주기(기본 300).
    LIOT_FEEDBACK_HOST/PORT : 피드백 수신구(기본 127.0.0.1:8099; 0=비활성).

데몬은 절대 액추에이터를 호출하지 않는다(제안-만). HA 로는 읽기만, 외부로는
ntfy 알림만 나간다.
"""

from __future__ import annotations

import asyncio
import ipaddress
import os
import sys
from urllib.parse import urlsplit

from liot.app import LiotApp
from liot.core.clock import Clock
from liot.db.database import init_db
from liot.layers.l3_judgment.llm_judge import build_llm_backend
from liot.layers.l4_execution.executor import HAExecutor, build_execution_policy
from liot.layers.l4_execution.ntfy_transport import build_ntfy_transport
from liot.service import DEV_SIGNING_SECRET, LiotService

DEV_SECRET_TEXT = DEV_SIGNING_SECRET.decode("utf-8")

_LOOPBACK_HOSTNAMES = frozenset({"localhost", "ip6-localhost", "ip6-loopback"})

__all__ = ["is_loopback_host", "feedback_block_reason", "main"]


def is_loopback_host(host: str) -> bool:
    """host 가 루프백(외부에서 닿지 않음)인가.

    빈 문자열은 '모든 인터페이스 바인드'라 외부 노출로 본다. 해석 못 하는
    호스트명은 보수적으로 외부로 본다(fail-closed).
    """
    h = (host or "").strip().strip("[]").lower()
    if not h:
        return False
    if h in _LOOPBACK_HOSTNAMES:
        return True
    try:
        return ipaddress.ip_address(h).is_loopback
    except ValueError:
        return False


def feedback_block_reason(
    secret: str, fb_host: str, feedback_base_url: str = ""
) -> str | None:
    """피드백 수신구를 열면 안 되는 이유(한 줄) 또는 None(열어도 됨).

    소스에 공개된 dev 서명 키를 쓰는 채로 수신구가 외부에서 닿으면, 저장소를
    읽은 누구나 수락/거절/undo 링크를 위조해 제안 상태를 바꿀 수 있다. 그래서
    (a) 비루프백 바인드이거나 (b) 알림 버튼이 가리킬 base URL 이 비루프백이면
    수신구를 열지 않는다. 실제 비밀키가 설정돼 있으면 아무것도 막지 않는다.
    """
    if secret.strip() != DEV_SECRET_TEXT:
        return None
    if not is_loopback_host(fb_host):
        return (
            f"LIOT_NOTIFY_SECRET 이 dev 기본값인데 바인드 호스트가 {fb_host or '(전체 인터페이스)'} "
            "라 외부에서 닿습니다. 서명이 위조 가능하므로 열지 않습니다"
        )
    if feedback_base_url:
        host = urlsplit(feedback_base_url).hostname or ""
        if not is_loopback_host(host):
            return (
                f"LIOT_NOTIFY_SECRET 이 dev 기본값인데 알림 버튼이 외부 주소"
                f"({feedback_base_url})를 가리킵니다. 서명이 위조 가능하므로 열지 않습니다"
            )
    return None


async def _amain() -> int:
    home_id = os.environ.get("LIOT_HOME_ID", "singleton").strip() or "singleton"
    db_path = os.environ.get("LIOT_DB_PATH", "liot.db").strip() or "liot.db"
    tick_seconds = float(os.environ.get("LIOT_TICK_SECONDS", "300") or "300")
    secret = os.environ.get("LIOT_NOTIFY_SECRET", "liot-dev-signing-key")
    topic = os.environ.get("LIOT_NTFY_TOPIC", "liot").strip() or "liot"
    ha_url = os.environ.get("LIOT_HA_URL", "").strip()
    ha_token = os.environ.get("LIOT_HA_TOKEN", "").strip()
    fb_host = os.environ.get("LIOT_FEEDBACK_HOST", "127.0.0.1").strip()
    fb_port = int(os.environ.get("LIOT_FEEDBACK_PORT", "8099") or "8099")
    fb_base_url = os.environ.get("LIOT_NTFY_FEEDBACK_URL", "").strip()

    clock = Clock()
    db = await init_db(db_path, durable=True)

    # HA 연결(있으면): 카테고리 맵 주입 + 센서 스트림.
    bridge = None
    categories: dict[str, str] = {}
    if ha_url and ha_token:
        from liot.layers.l1_sensing.ha_bridge import HABridge, WebsocketsHAConnection

        conn = WebsocketsHAConnection(ha_url)
        await conn.open()
        bridge = HABridge(conn, ha_token)
        categories = await bridge.entity_categories()
        print(f"[HA] {len(categories)} entities mapped.")
    else:
        print("[HA] LIOT_HA_URL/TOKEN 미설정 → 센서 스트림 없이 능동 틱만 구동.")

    app = LiotApp(clock=clock, categories=categories, llm_backend=build_llm_backend())
    transport = build_ntfy_transport()

    # 조작(불변식 8): LIOT_EXECUTION_ENABLED 가 참이고 HA 브리지가 있을 때만 활성.
    # HA 가 없으면 조작 대상이 없으므로 executor 미구성(제안-만 유지).
    policy = build_execution_policy()
    executor = None
    if policy.enabled:
        if bridge is not None:
            executor = HAExecutor(bridge.call_service, bridge.get_state)
        else:
            print("[exec] LIOT_EXECUTION_ENABLED 이나 HA 미연결 → 조작 비활성(제안-만).")

    service = LiotService(
        app, db, home_id=home_id, clock=clock, transport=transport,
        topic=topic, secret=secret, executor=executor, policy=policy,
    )
    print(
        f"[LIOT] backend={app.judge.backend.model_id} "
        f"ntfy={'on' if transport else 'sanitize-only'} "
        f"exec={'ON' if executor else 'off(제안-만)'} tick={tick_seconds}s db={db_path}"
    )

    fb_server = None
    if fb_port:
        blocked = feedback_block_reason(secret, fb_host, fb_base_url)
        if blocked:
            print(f"[feedback] 수신구를 열지 않습니다 — {blocked}.")
            print(
                "[feedback] LIOT_NOTIFY_SECRET 에 임의의 긴 비밀키를 넣고 다시 실행하면 열립니다."
            )
        else:
            fb_server = await service.serve_feedback(fb_host, fb_port)
            print(f"[feedback] http://{fb_host}:{fb_port}/feedback 수신 대기")

    try:
        await service.run(bridge, tick_seconds=tick_seconds)
    finally:
        if fb_server is not None:
            fb_server.close()
        await db.close()
    return 0


def main() -> int:
    try:
        return asyncio.run(_amain())
    except KeyboardInterrupt:
        return 0


if __name__ == "__main__":
    sys.exit(main())
