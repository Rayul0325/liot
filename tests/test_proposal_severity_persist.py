"""제안 영속화가 파급도(severity)·상황 id(situation_id)를 저장·복원한다.

값이 없는 제안(쾌적 등)은 두 컬럼이 NULL 로 남고 None 으로 돌아온다.
"""

from __future__ import annotations

from liot.core.clock import Clock
from liot.core.contracts import CandidateAction, Proposal
from liot.core.risk import RiskTier
from liot.core.severity import SeverityLevel
from liot.layers.l4_execution.proposal_service import ProposalService

DAY = "2026-06-02T12:00:00+00:00"


def _proposal(pid: str, **over: object) -> Proposal:
    base: dict[str, object] = {
        "proposal_id": pid,
        "judgment_id": "j-1",
        "home_id": "singleton",
        "risk_tier": RiskTier.EMBER,
        "action": CandidateAction(
            capability="valve", action="close", params={},
            entity_id="switch.gas_valve", category="gas",
        ),
        "rationale": "r",
        "required_confirmation": "explicit",
        "created_at": DAY,
    }
    base.update(over)
    return Proposal.model_validate(base)


async def test_proposal_roundtrips_severity_and_situation(tmp_db) -> None:
    svc = ProposalService(tmp_db, clock=Clock(fixed=DAY))
    p = _proposal("p-1", severity=SeverityLevel.CRITICAL, situation_id="sit-1")
    await svc.save(p)
    back = await svc.get("p-1")
    assert back is not None
    assert back.severity is SeverityLevel.CRITICAL
    assert back.situation_id == "sit-1"
    assert back == p
    pending = await svc.list_pending()
    assert [(x.severity, x.situation_id) for x in pending] == [(SeverityLevel.CRITICAL, "sit-1")]
    cur = await tmp_db.execute(
        "SELECT severity, situation_id FROM proposals WHERE proposal_id = 'p-1'"
    )
    row = await cur.fetchone()
    await cur.close()
    assert row is not None and tuple(row) == (3, "sit-1")


async def test_proposal_without_severity_keeps_null(tmp_db) -> None:
    svc = ProposalService(tmp_db, clock=Clock(fixed=DAY))
    await svc.save(_proposal("p-2"))
    back = await svc.get("p-2")
    assert back is not None
    assert back.severity is None and back.situation_id is None
    cur = await tmp_db.execute(
        "SELECT severity, situation_id FROM proposals WHERE proposal_id = 'p-2'"
    )
    row = await cur.fetchone()
    await cur.close()
    assert row is not None and tuple(row) == (None, None)
