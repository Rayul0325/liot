"""L4 Execution Safety 레이어.

기본은 여전히 **제안-만**(불변식 3). 조작 능력은 *옵트인*으로만 켜지며, 구체
Executor 는 전용 모듈 ``executor`` 한 곳에만 둔다
(``liot.core.invariants.assert_executor_confined()`` 가 강제). 실행은
``assert_execution_gated()`` 가 지키는 불변식 8(기본 OFF·안전 자동금지·여는
방향 제외) 아래에서만 일어난다.

L4 구성:
  - device_catalog : (capability, action, 파라미터 방향) -> RiskTier 매핑.
  - risk_router    : Judgment -> Proposal.
  - proposal_service : Proposal 의 생성/만료/상태/피드백(ScopedRepo).
  - notifier       : 단일 발화 게이트 + egress_gate 통과 알림.
  - executor       : (옵트인) 승인된 동작을 HA 로 실제 조작 + 되돌리기.
"""

from __future__ import annotations

from .device_catalog import DeviceCatalog, load_device_catalog
from .notifier import GateDecision, NotificationBudget, Notifier
from .proposal_service import ProposalService
from .risk_router import RiskRouter, route

__all__ = [
    "DeviceCatalog",
    "load_device_catalog",
    "RiskRouter",
    "route",
    "ProposalService",
    "Notifier",
    "NotificationBudget",
    "GateDecision",
]
