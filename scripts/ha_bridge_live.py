#!/usr/bin/env python3
"""실제 Home Assistant 에 붙어 LIOT 파이프라인을 돌리는 라이브 러너.

필요 환경변수:
  LIOT_HA_URL    : HA WebSocket URL (예: ws://homeassistant.local:8123/api/websocket)
  LIOT_HA_TOKEN  : HA 장기 액세스 토큰(설정 > 보안 > 장기 액세스 토큰)
선택:
  LIOT_HA_MAX_EVENTS : 처리할 이벤트 수 후 종료(기본 10, 0=무한)
  LIOT_LLM_BACKEND   : 'ollama' 등(미설정 시 FakeLLM)

사용:
  LIOT_HA_URL=ws://<host>:8123/api/websocket LIOT_HA_TOKEN=*** \
    .venv/bin/python scripts/ha_bridge_live.py
"""

from __future__ import annotations

import asyncio
import os
import sys

from liot.app import LiotApp
from liot.layers.l1_sensing.ha_bridge import (
    HAAuthError,
    HABridge,
    HAConnectionError,
    WebsocketsHAConnection,
)
from liot.layers.l3_judgment.llm_judge import build_llm_backend


async def run() -> int:
    url = os.environ.get("LIOT_HA_URL", "").strip()
    token = os.environ.get("LIOT_HA_TOKEN", "").strip()
    if not url or not token:
        print("LIOT_HA_URL 과 LIOT_HA_TOKEN 환경변수가 필요합니다.")
        print("예: LIOT_HA_URL=ws://homeassistant.local:8123/api/websocket")
        return 2
    max_events = int(os.environ.get("LIOT_HA_MAX_EVENTS", "10") or "10")

    conn = WebsocketsHAConnection(url)
    try:
        await conn.open()
    except HAConnectionError as exc:
        print(f"[연결 실패] {exc}")
        return 1

    bridge = HABridge(conn, token)
    try:
        cats = await bridge.entity_categories()
    except HAAuthError as exc:
        print(f"[인증 실패] {exc}  (토큰을 확인하세요)")
        await conn.close()
        return 1
    safety = {e: c for e, c in cats.items() if c in (
        "gas", "smoke", "door", "leak", "high_temp", "lock", "water_main", "oven"
    )}
    print(f"[HA] 엔티티 {len(cats)}개 카테고리 매핑 완료. 안전 임계 엔티티 {len(safety)}개.")
    if safety:
        print("     안전 엔티티 예:", list(safety.items())[:8])

    app = LiotApp(categories=cats, llm_backend=build_llm_backend())
    print(f"[LIOT] backend={app.judge.backend.model_id}. 이벤트 수신 시작"
          f"{' (무한)' if max_events == 0 else f' (최대 {max_events}건)'}...\n")

    n = 0
    async for reading in bridge.stream():
        proposal = await app.process_reading(reading)
        if proposal is None:
            continue  # 의미 변화 미달 → 스냅샷 생략
        n += 1
        print(f"[{n}] {reading.entity_id} = {reading.norm_value!r}")
        print(f"    → {proposal.risk_tier.name} {proposal.risk_tier.glyph} "
              f"({proposal.required_confirmation})  {proposal.rationale[:100]}")
        if max_events and n >= max_events:
            break

    await conn.close()
    print(f"\n완료: {n}건 제안 생성. (Phase1: 자동 실행 없음, 제안만)")
    return 0


def main() -> int:
    try:
        return asyncio.run(run())
    except KeyboardInterrupt:
        return 0


if __name__ == "__main__":
    sys.exit(main())
