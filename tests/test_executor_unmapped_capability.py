"""HAExecutor: 허용 목록에는 있으나 HA 서비스 도메인에 매핑되지 않은 기능은 호출 전에 거부한다.

E8 뮤테이션 빈틈(executor.py:303): `_service_for` 의 거부 가드를 지워도 아무 테스트도 안 깨졌다
(그 줄이 한 번도 실행되지 않음). 가드가 없으면 `call_service(None, ...)` 가 불린다 — 작동기
호출 게이트의 마지막 방어선이다. 카탈로그에서 파생된 확장 실행 허용분(예: vacuum.start)이
`_CAP_DOMAIN` 에 없을 때가 실제 경로다.
"""

from __future__ import annotations

import pytest

from liot.core.contracts import CandidateAction
from liot.layers.l4_execution.executor import ExecutionDenied, HAExecutor


def _vacuum(action: str = "start") -> CandidateAction:
    return CandidateAction(
        capability="vacuum", action=action, category="appliance",
        entity_id="vacuum.test_entity", params={},
    )


def _executor(calls: list[tuple[object, ...]]) -> HAExecutor:
    async def call(domain, service, entity_id, data):
        calls.append((domain, service, entity_id, data))

    return HAExecutor(call_service=call, extra_executable=frozenset({("vacuum", "start")}))


async def test_allowlisted_but_unmapped_capability_is_denied_before_any_call() -> None:
    calls: list[tuple[object, ...]] = []
    with pytest.raises(ExecutionDenied, match="unmapped capability: 'vacuum'"):
        await _executor(calls).execute(_vacuum())
    assert calls == []  # HA 로는 아무것도 나가지 않았다


async def test_unmapped_capability_undo_is_denied_before_any_call() -> None:
    calls: list[tuple[object, ...]] = []
    with pytest.raises(ExecutionDenied, match="unmapped capability: 'vacuum'"):
        await _executor(calls).undo(_vacuum(), {"attributes": {}})
    assert calls == []


async def test_mapped_capability_still_executes() -> None:
    # 대조군: 매핑된 기능(switch.toggle 확장분)은 도메인으로 번역돼 호출된다.
    calls: list[tuple[object, ...]] = []

    async def call(domain, service, entity_id, data):
        calls.append((domain, service, entity_id, data))

    ex = HAExecutor(call_service=call, extra_executable=frozenset({("switch", "toggle")}))
    res = await ex.execute(
        CandidateAction(capability="switch", action="toggle", category="appliance",
                        entity_id="switch.test_entity", params={})
    )
    assert res.ok is True and res.service == "switch.toggle"
    assert calls == [("switch", "toggle", "switch.test_entity", {})]
