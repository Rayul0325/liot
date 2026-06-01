"""L4 Execution Safety 레이어 (Phase1 = 제안-만).

Phase1 의 핵심 불변식: **액추에이터 실행 코드 부재**.
이 패키지 어디에도 'Executor' 라는 이름의 구체 클래스가 있어서는 안 된다
(``liot.core.invariants.assert_executor_absent()`` 가 이를 검증한다).

L4 는 L3 의 Judgment 를 받아 사용자 대면 Proposal 로만 변환한다:
  - device_catalog : (capability, action, 파라미터 방향) -> RiskTier 매핑.
  - risk_router    : Judgment -> Proposal (실행 호출 없음).
  - proposal_service : Proposal 의 생성/만료/상태/피드백(ScopedRepo).
  - notifier       : 단일 발화 게이트 + egress_gate 통과 알림.

여기서는 추상 인터페이스(ExecutorProtocol)조차 재정의하지 않는다.
필요하면 ``liot.core.invariants.ExecutorProtocol`` 을 직접 참조하라.
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
