"""Alternative dispatch architectures for the first safety alert (E1).

``liot`` is the production ``LiotService`` (table first, model after). The two alternatives are
subclasses that reuse every production method (dispatch, ladder, persistence) and change ONE
thing: the first alert of a new situation episode waits for the decision model.

* ``wait_for_model``  — wait for the model judgment (up to the service's phase-2 deadline, the
  same bound production gives the model), then send ONE alert carrying the final severity.
* ``short_<w>``       — wait at most ``w`` (0.25/0.5/1.0 s) for the model, then send. If the model
  missed the window the table-based alert goes out and the late answer flows through the
  production phase-2 path (follow-up only when it raised).

No production function is copied. The model call itself is production
``LiotApp.run_deferred_judge`` run on a worker pool owned by the wrapper, so a late answer can
still be consumed by the unchanged ``_run_phase2``.
"""

from __future__ import annotations

import asyncio
from concurrent.futures import Future, ThreadPoolExecutor
from typing import Any

from experiments.common.service_kit import HOME, SECRET, TOPIC, Kit
from liot.app import StagedAlert
from liot.core.contracts import Proposal
from liot.core.severity import SeverityLevel
from liot.service import DispatchResult, LiotService

SHORT_WINDOWS = {"short_0.25": 0.25, "short_0.5": 0.5, "short_1.0": 1.0}
ARMS = ("liot", "wait_for_model", *SHORT_WINDOWS)
C27_ARMS = ("liot", "wait_for_model", "floor_aware")


class ModelFirstService(LiotService):
    """First situation alert waits for the model (see module docstring)."""

    def __init__(self, *args: Any, wait_s: float | None, skip_critical: bool = False,
                 **kw: Any) -> None:
        super().__init__(*args, **kw)
        self._wait_s = wait_s  # None -> the phase-2 deadline
        # C2.7 'floor_aware': a table floor that is already CRITICAL is sent at once (the model
        # cannot raise it further); only WARNING-floor situations wait for the model.
        self._skip_critical = skip_critical
        self._staged: dict[str, StagedAlert] = {}
        self._futs: dict[str, Future[Any]] = {}
        self._sync_done: set[str] = set()
        self._pool = ThreadPoolExecutor(max_workers=4, thread_name_prefix="model-first")
        orig_stage = self.app.process_situation_reading
        self._orig_judge = self.app.run_deferred_judge

        async def stage(reading: Any, *, situation_id: str, severity: Any) -> Any:
            staged = await orig_stage(reading, situation_id=situation_id, severity=severity)
            if staged is not None:
                self._staged[staged.proposal.proposal_id] = staged
            return staged

        def judge(req: Any) -> Any:
            fut = self._futs.get(req.situation_id)
            return fut.result() if fut is not None else self._orig_judge(req)

        self.app.process_situation_reading = stage  # type: ignore[method-assign]
        self.app.run_deferred_judge = judge  # type: ignore[method-assign]

    def shutdown(self) -> None:
        self._pool.shutdown(wait=False, cancel_futures=True)

    async def dispatch(self, proposal: Proposal) -> DispatchResult:
        staged = self._staged.pop(proposal.proposal_id, None)
        if staged is None:
            return await super().dispatch(proposal)
        req = staged.deferred
        if self._skip_critical and req.severity_floor >= SeverityLevel.CRITICAL:
            return await super().dispatch(proposal)  # production path; phase 2 follows as usual
        wait = self._phase2_deadline()
        if self._wait_s is not None:
            wait = min(wait, self._wait_s)
        fut = self._pool.submit(self._orig_judge, req)
        self._futs[req.situation_id] = fut
        afut = asyncio.wrap_future(fut)
        afut.add_done_callback(lambda f: f.cancelled() or f.exception())  # mark retrieved
        chosen = proposal
        try:
            judgment = await asyncio.wait_for(asyncio.shield(afut), timeout=wait)
        except (TimeoutError, Exception):  # noqa: BLE001 - table value goes out on any miss
            return await super().dispatch(chosen)
        await self.safety.save_judgment(judgment)
        followup = self.app.route_followup(judgment)
        if self._raised(proposal, followup):
            chosen = followup
        self._sync_done.add(req.situation_id)
        return await super().dispatch(chosen)

    def _spawn_phase2(self, staged: StagedAlert) -> None:
        if staged.deferred.situation_id in self._sync_done:
            return  # judged before sending: nothing left for phase 2
        super()._spawn_phase2(staged)


async def make_arm(kit: Kit, arm: str, deadline_s: float) -> Kit:
    """Swap ``kit.svc`` for the service of ``arm`` (same app/db/clock/transport)."""
    if arm == "liot":
        return kit
    wait_s = None if arm in ("wait_for_model", "floor_aware") else SHORT_WINDOWS[arm]
    kit.svc = ModelFirstService(
        kit.app, kit.db, home_id=HOME, clock=kit.clock, transport=kit.transport,
        topic=TOPIC, secret=SECRET, phase2_deadline_s=deadline_s, wait_s=wait_s,
        skip_critical=arm == "floor_aware",
    )
    return kit
