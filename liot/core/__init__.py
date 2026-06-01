"""LIOT core kernel.

이 패키지는 레이어들이 의존하는 공개 계약(contracts), 위험도 모델(risk),
불변식 검증기(invariants), 토폴로지를 강제하는 이벤트 버스(event_bus),
테넌시 격리 저장소(scoped_repo), 외부 송신 단일 통로(egress_gate),
그리고 시계(clock)를 제공한다.
"""

__all__ = [
    "risk",
    "contracts",
    "invariants",
    "event_bus",
    "scoped_repo",
    "egress_gate",
    "clock",
]
