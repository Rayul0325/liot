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
    LIOT_NOTIFY_SECRET  : 딥링크 HMAC 서명 비밀키(미설정 시 dev 기본값 — 운영 필수 교체).
    LIOT_TICK_SECONDS   : 능동 틱 주기(기본 300).
    LIOT_FEEDBACK_HOST/PORT : 피드백 수신구(기본 127.0.0.1:8099; 0=비활성).

데몬은 절대 액추에이터를 호출하지 않는다(제안-만). HA 로는 읽기만, 외부로는
ntfy 알림만 나간다.
"""

from __future__ import annotations

import asyncio
import os
import sys

from liot.app import LiotApp
from liot.core.clock import Clock
from liot.db.database import init_db
from liot.layers.l3_judgment.llm_judge import build_llm_backend
from liot.layers.l4_execution.ntfy_transport import build_ntfy_transport
from liot.service import LiotService


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
    service = LiotService(
        app, db, home_id=home_id, clock=clock, transport=transport,
        topic=topic, secret=secret,
    )
    print(
        f"[LIOT] backend={app.judge.backend.model_id} "
        f"ntfy={'on' if transport else 'sanitize-only'} tick={tick_seconds}s db={db_path}"
    )

    fb_server = None
    if fb_port:
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
