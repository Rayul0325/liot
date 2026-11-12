"""C4.10 (b): coalesced follow-up (raise) pushes — an experiment-side subclass of ``LiotService``.

Production sends the first alert from the table at once and, when the decision model later raises
the situation, ONE follow-up push (phase 2 spawns once per opened situation, ``_run_phase2``
dispatches at most one follow-up, later abnormal readings of an open situation are 'continuing' and
create no proposal). That is a structural fact worth stating before the measurement: **inside one
situation there is never a second follow-up to merge**, so merging "follow-ups of the same
situation" can only be a no-op.

Two variants therefore, both holding ONLY follow-ups and never the first alert:

  ``situation``  merge follow-ups of the same situation inside a window (structurally a no-op)
  ``incident``   merge follow-ups of DIFFERENT situations that arrive within the window into one
                 push (the highest severity wins; the others are persisted without a push so the
                 stored final severity, and with it the ladder cadence, stays correct)

The hold starts when the first follow-up of a group arrives and never extends, so the follow-up is
delayed by at most ``window_s``. The first-alert path is the production ``dispatch`` untouched, so
'first-alert latency unchanged' is a 구조상 결과 (measured anyway, interleaved).
"""

from __future__ import annotations

import asyncio
import threading
import time
from typing import Any

from experiments.common.service_kit import (
    HOME,
    SECRET,
    TOPIC,
    Kit,
    RecordingBackend,
    _result,
)
from liot.core.contracts import Proposal
from liot.core.risk import RiskTier
from liot.core.severity import SeverityLevel
from liot.layers.l3_judgment.llm_judge import LLMResult
from liot.service import DispatchResult, LiotService

VARIANTS = ("liot", "situation_1.0", "incident_0.5", "incident_1.0", "incident_2.0")


def parse_variant(name: str) -> tuple[str, float] | None:
    if name == "liot":
        return None
    scope, w = name.split("_")
    return scope, float(w)


class ReplayBackend(RecordingBackend):
    """A model with a planned latency and raise flag per entity (replayed bench latencies)."""

    model_id = "replay-test-model"

    def __init__(self, plan: dict[str, tuple[float, bool]]) -> None:
        super().__init__()
        self.plan = plan
        self._release = threading.Event()

    def _answer(self, candidate, ctx, gate1_floor, severity_floor):  # type: ignore[no-untyped-def]
        lat, raise_flag = self.plan.get(candidate.entity_id, (0.0, False))
        self._release.wait(timeout=lat)
        if raise_flag and severity_floor is not None:
            return _result(self.model_id, RiskTier.EMBER, SeverityLevel.CRITICAL, severity_floor)
        return LLMResult(
            tier=gate1_floor, confidence=0.8, rationale="concur", model_id=self.model_id,
            severity=severity_floor, probs=None,
        )

    def release_all(self) -> None:
        self._release.set()


class CoalescingService(LiotService):
    """Holds follow-up proposals for ``window_s`` and sends one merged push (see module doc)."""

    def __init__(self, *args: Any, window_s: float, scope: str, **kw: Any) -> None:
        super().__init__(*args, **kw)
        self._w = window_s
        self._scope = scope
        self._first_done: set[str] = set()
        self._held: dict[str, list[Proposal]] = {}
        self.hold_log: list[dict[str, float]] = []

    async def dispatch(self, proposal: Proposal) -> DispatchResult:
        sid = proposal.situation_id
        if sid is None or sid not in self._first_done:
            res = await super().dispatch(proposal)  # the first alert: production path untouched
            if sid is not None:
                self._first_done.add(sid)
            return res
        key = sid if self._scope == "situation" else "incident"
        group = self._held.get(key)
        if group is not None:
            group.append(proposal)
        else:
            self._held[key] = [proposal]
            task = asyncio.create_task(self._flush(key))
            self._phase2_tasks.add(task)
            task.add_done_callback(self._on_phase2_done)
        return DispatchResult(proposal, "HELD", sent=False)

    async def _flush(self, key: str) -> None:
        t0 = time.perf_counter()
        await asyncio.sleep(self._w)
        group = self._held.pop(key, [])
        if not group:
            return
        group.sort(key=lambda p: (int(p.severity or 0), int(p.risk_tier)), reverse=True)
        best = group[0]
        for other in group[1:]:
            await self.proposals.save(other)  # keep the stored final severity; no push
        await super().dispatch(best)
        for sid in {p.situation_id for p in group if p.situation_id}:
            await self._arm_ladder(sid)
        self.hold_log.append({"held_s": time.perf_counter() - t0, "merged": float(len(group))})


async def make_variant(kit: Kit, name: str, deadline_s: float) -> Kit:
    spec = parse_variant(name)
    if spec is None:
        return kit
    scope, w = spec
    kit.svc = CoalescingService(
        kit.app, kit.db, home_id=HOME, clock=kit.clock, transport=kit.transport,
        topic=TOPIC, secret=SECRET, phase2_deadline_s=deadline_s, window_s=w, scope=scope,
    )
    return kit
