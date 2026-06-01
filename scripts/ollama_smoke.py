#!/usr/bin/env python3
"""ssh home 의 ollama(gemma 계열)로 LIOT Gate2 실연결 스모크.

동작:
  1) ssh <host> 'ollama list' 로 설치 모델을 조회하고 gemma* 태그를 자동 감지.
  2) 감지한 태그로 OllamaBackend 를 구성해 몇 가지 후보 행동을 실제 판정.
  3) home 이 오프라인이면 명확히 보고하고, 파이프라인이 degraded fail-safe
     (안전=EMBER 유지, 불변식 6)로 안전하게 떨어지는지 보여준다.

사용:
  .venv/bin/python scripts/ollama_smoke.py            # host=home 자동 감지
  LIOT_OLLAMA_SSH_HOST=home-lan .venv/bin/python scripts/ollama_smoke.py
  LIOT_OLLAMA_MODEL=gemma3:4b   .venv/bin/python scripts/ollama_smoke.py  # 태그 강제
"""

from __future__ import annotations

import os
import subprocess
import sys
import time

from liot.core.contracts import CandidateAction, ContextSnapshot
from liot.layers.l3_judgment.guardrail import Guardrail
from liot.layers.l3_judgment.llm_judge import LLMJudge
from liot.layers.l3_judgment.ollama_backend import OllamaBackend, SSHOllamaTransport

HOST = os.environ.get("LIOT_OLLAMA_SSH_HOST", "home")


def detect_model() -> str | None:
    forced = os.environ.get("LIOT_OLLAMA_MODEL")
    if forced:
        return forced
    try:
        proc = subprocess.run(
            ["ssh", "-o", "ConnectTimeout=8", "-o", "BatchMode=yes", HOST, "ollama list"],
            capture_output=True,
            timeout=20,
        )
    except (subprocess.TimeoutExpired, OSError) as exc:
        print(f"[detect] ssh {HOST} 실패: {exc}")
        return None
    if proc.returncode != 0:
        print(f"[detect] rc={proc.returncode}: {proc.stderr.decode('utf-8','replace')[:200]}")
        return None
    lines = proc.stdout.decode("utf-8", "replace").splitlines()
    for line in lines[1:]:  # 헤더 제외
        tag = line.split()[0] if line.split() else ""
        if "gemma" in tag.lower():
            print(f"[detect] gemma 모델 감지: {tag}")
            return tag
    print(f"[detect] gemma 태그를 못 찾음. 설치 목록:\n{proc.stdout.decode('utf-8','replace')}")
    return lines[1].split()[0] if len(lines) > 1 and lines[1].split() else None


def _ctx() -> ContextSnapshot:
    return ContextSnapshot(
        snapshot_id="s",
        trace_id="smoke",
        home_id="singleton",
        trigger={"kind": "smoke"},
        structured={"time_bucket": "night", "entities": {}},
        nl_summary="[night] 거실 움직임 없음, 외출 추정",
        summary_template_sha="x",
        created_at="2026-06-01T22:00:00+00:00",
    )


def main() -> int:
    model = detect_model()
    if model is None:
        model = os.environ.get("LIOT_OLLAMA_MODEL", "gemma3:4b")
        print(f"[smoke] 모델 감지 실패 → 기본 '{model}' 로 시도(오프라인이면 degraded 시연).")
    backend = OllamaBackend(model, SSHOllamaTransport(HOST), timeout_s=float(os.environ.get("LIOT_OLLAMA_TIMEOUT", "30")))
    judge = LLMJudge(backend=backend)
    g = Guardrail()
    print(f"[smoke] backend={backend.model_id} host={HOST}\n")

    cases = [
        ("light", "turn_on", "일상 조명"),
        ("climate", "set_temperature", "야간 온도 조절"),
        ("gas", "alert", "가스 누출(안전)"),
    ]
    for cat, act, desc in cases:
        c = CandidateAction(capability=cat, action=act, params={}, entity_id=f"{cat}.x", category=cat)
        ctx = _ctx()
        g1 = g.evaluate(c, ctx)
        t0 = time.time()
        j = judge.judge(c, ctx, g1, ruleset_version=g.ruleset_version)
        dt = time.time() - t0
        flag = "DEGRADED" if j.degraded else "LIVE"
        print(f"[{desc}] gate1={g1.floor_tier.name} → {j.risk_tier.name} "
              f"[{flag}/{j.gate2_verdict}] {dt:.1f}s")
        print(f"    {j.rationale[:110]}")
    print("\n안전(gas)은 LLM 상태와 무관하게 EMBER 를 유지해야 한다(불변식 6).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
