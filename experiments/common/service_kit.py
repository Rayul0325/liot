"""Assemble a real ``LiotService`` for experiments, plus recording/fault backends and helpers.

Modelled on the test helpers in ``tests/test_pipeline_situations.py`` and
``tests/test_escalation_ladder.py`` (tests are not importable, so the construction logic is
copied here). Production code under ``liot/`` is never modified.
"""

from __future__ import annotations

import json
import random
import tempfile
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import aiosqlite

from liot.app import LiotApp
from liot.core.clock import Clock
from liot.core.contracts import CandidateAction, ContextSnapshot, SensorReading
from liot.core.risk import RiskTier
from liot.core.severity import SeverityLevel
from liot.db.database import init_db
from liot.layers.l1_sensing.normalizer import normalize
from liot.layers.l2_context.proactive import ProactiveGenerator
from liot.layers.l2_context.proactive_rules import load_proactive_ruleset
from liot.layers.l3_judgment.llm_judge import LLMResult, LLMUnavailable
from liot.layers.l4_execution.executor import (
    _INVERSE_ACTION,  # same inverse table the real undo path uses
    ExecutionPolicy,
    ExecutionResult,
    FakeExecutor,
)
from liot.layers.l4_execution.ntfy_transport import FakeNtfyTransport, NtfyError
from liot.service import LiotService

__all__ = [
    "CATS",
    "DOOR",
    "GAS",
    "COMFORT_YAML",
    "HOME",
    "SECRET",
    "T0",
    "TOPIC",
    "FixedBackend",
    "HighestBackend",
    "Kit",
    "LowestBackend",
    "RaisingBackend",
    "RandomBackend",
    "RecordingBackend",
    "RecordingExecutor",
    "SlowBackend",
    "TimedTransport",
    "add_guardian",
    "advance",
    "advance_to",
    "build_service",
    "guardian_forwards",
    "judgments",
    "proposals",
    "reading",
    "situations",
]

T0 = "2026-06-02T12:00:00+00:00"
SECRET = b"test-secret-key"
TOPIC = "liot-test"
HOME = "singleton"
GAS = "binary_sensor.kitchen_gas"
DOOR = "binary_sensor.front_door"
CATS: dict[str, str] = {
    "binary_sensor.living_motion": "motion",
    "light.living_room": "light",
    GAS: "gas",
    DOOR: "door",
}
COMFORT_YAML = """
version: "1.0.0"
name: comfort
limits: {cooldown_minutes: 60, max_per_bucket: 5, max_per_day: 10}
rules:
  - id: vacant-light-off
    match: {occupancy: vacant}
    state: [{entity: light.living_room, op: eq, value: true}]
    propose: {target: light.living_room, capability: light, action: turn_off}
    ttl_minutes: 15
"""


# ── backends ─────────────────────────────────────────────────────────────────
class RecordingBackend:
    """Base backend: records every input it received (ctx, candidate, floors) and the thread.

    Subclasses override ``_answer``. ``model_inputs()`` renders the recorded inputs as plain
    JSON-safe dicts so trait-leak checks can scan them as text.
    """

    model_id = "recording-test-model"

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []
        self.thread_ids: list[int] = []
        self._lock = threading.Lock()

    def judge_raw(
        self,
        candidate: CandidateAction,
        ctx: ContextSnapshot | None,
        gate1_floor: RiskTier,
        severity_floor: SeverityLevel | None = None,
    ) -> LLMResult:
        with self._lock:
            self.thread_ids.append(threading.get_ident())
            self.calls.append(
                {
                    "candidate": candidate,
                    "ctx": ctx,
                    "gate1_floor": gate1_floor,
                    "severity_floor": severity_floor,
                }
            )
        return self._answer(candidate, ctx, gate1_floor, severity_floor)

    def _answer(
        self,
        candidate: CandidateAction,
        ctx: ContextSnapshot | None,
        gate1_floor: RiskTier,
        severity_floor: SeverityLevel | None,
    ) -> LLMResult:
        raise NotImplementedError

    def model_inputs(self) -> list[dict[str, Any]]:
        """Recorded inputs as JSON-safe dicts (pydantic models dumped)."""
        out: list[dict[str, Any]] = []
        for c in self.calls:
            out.append(
                {
                    "candidate": c["candidate"].model_dump(mode="json"),
                    "ctx": None if c["ctx"] is None else c["ctx"].model_dump(mode="json"),
                    "gate1_floor": int(c["gate1_floor"]),
                    "severity_floor": (
                        None if c["severity_floor"] is None else int(c["severity_floor"])
                    ),
                }
            )
        return out

    def release_all(self) -> None:
        """Release any blocked judge threads. No-op unless a subclass blocks."""


def _result(
    model_id: str,
    tier: RiskTier,
    severity: SeverityLevel | None,
    severity_floor: SeverityLevel | None,
) -> LLMResult:
    asked = severity_floor is not None
    return LLMResult(
        tier=tier,
        confidence=0.8,
        rationale="experiment",
        model_id=model_id,
        severity=severity if asked else None,
        probs={"severity": {"A": 0.1, "B": 0.9}} if asked else None,
    )


class FixedBackend(RecordingBackend):
    """Returns a fixed tier and (when the severity question is asked) a fixed severity."""

    model_id = "fixed-test-model"

    def __init__(
        self, tier: RiskTier = RiskTier.EMBER, severity: SeverityLevel | None = None
    ) -> None:
        super().__init__()
        self.tier = tier
        self.severity = severity

    def _answer(self, candidate, ctx, gate1_floor, severity_floor):  # type: ignore[no-untyped-def]
        return _result(self.model_id, self.tier, self.severity, severity_floor)


class LowestBackend(FixedBackend):
    """Adversarial model that always answers the lowest tier and severity (tries to lower)."""

    model_id = "lowest-test-model"

    def __init__(self) -> None:
        super().__init__(RiskTier.AMBIENT, SeverityLevel.RECORD)


class HighestBackend(FixedBackend):
    """Model that always answers the highest tier and severity (tries to over-raise)."""

    model_id = "highest-test-model"

    def __init__(self) -> None:
        super().__init__(RiskTier.EMBER, SeverityLevel.CRITICAL)


class RaisingBackend(RecordingBackend):
    """Model that fails every call (default: ``LLMUnavailable``; pass any exception)."""

    model_id = "raising-test-model"

    def __init__(self, exc: BaseException | None = None) -> None:
        super().__init__()
        self.exc = exc if exc is not None else LLMUnavailable("raising backend")

    def _answer(self, candidate, ctx, gate1_floor, severity_floor):  # type: ignore[no-untyped-def]
        raise self.exc


class SlowBackend(FixedBackend):
    """Model that takes ``seconds`` to answer, but can be released early.

    The wait is an Event wait, so ``release_all()`` frees every blocked thread at once — call
    it (``Kit.close`` does) so threads do not pile up across thousands of runs.
    ``comfort_only=True`` delays only judgments that do not ask the severity question
    (non-safety candidates); safety judgments answer immediately.

    Caveats: ``release_all()`` is permanent (later calls no longer wait). Safety phase-2
    judgments run in worker threads, so a slow answer does not block the loop; comfort
    judgments (``run_tick``) run on the event-loop thread, so there ``seconds`` is a pure
    wall-clock cost and an asyncio task cannot release it (use ``threading.Timer``).
    """

    model_id = "slow-test-model"

    def __init__(
        self,
        seconds: float,
        *,
        comfort_only: bool = False,
        tier: RiskTier = RiskTier.EMBER,
        severity: SeverityLevel | None = None,
    ) -> None:
        super().__init__(tier, severity)
        self.seconds = seconds
        self.comfort_only = comfort_only
        self._release = threading.Event()
        self.started = threading.Event()

    def _answer(self, candidate, ctx, gate1_floor, severity_floor):  # type: ignore[no-untyped-def]
        self.started.set()
        if not (self.comfort_only and severity_floor is not None):
            self._release.wait(timeout=self.seconds)
        return super()._answer(candidate, ctx, gate1_floor, severity_floor)

    def release_all(self) -> None:
        self._release.set()


class RandomBackend(RecordingBackend):
    """Random tier and severity per call, drawn from the given ``random.Random``."""

    model_id = "random-test-model"

    def __init__(self, rng: random.Random) -> None:
        super().__init__()
        self.rng = rng

    def _answer(self, candidate, ctx, gate1_floor, severity_floor):  # type: ignore[no-untyped-def]
        with self._lock:
            tier = RiskTier(self.rng.randrange(4))
            sev = SeverityLevel(self.rng.randrange(4))
        return _result(self.model_id, tier, sev, severity_floor)


# ── transport / executor ─────────────────────────────────────────────────────
class TimedTransport(FakeNtfyTransport):
    """Fake ntfy transport that timestamps every send.

    ``stamps`` holds ``(perf_counter, clock_time_iso, topic, payload)`` for each *successful*
    send, stamped on arrival at the transport (before ``delay_s``). ``delay_s`` blocks the
    caller with ``time.sleep`` (a real transport blocks the event loop the same way).
    ``fail_topics`` raise ``NtfyError``; those attempts land in ``failed`` as
    ``(perf_counter, topic)``. ``sent`` (parent) stays in sync with ``stamps``.
    """

    def __init__(
        self,
        clock: Clock | None = None,
        *,
        delay_s: float = 0.0,
        fail_topics: set[str] | None = None,
    ) -> None:
        super().__init__()
        self.clock = clock
        self.delay_s = delay_s
        self.fail_topics: set[str] = set(fail_topics or ())
        self.stamps: list[tuple[float, str | None, str, dict[str, Any]]] = []
        self.failed: list[tuple[float, str]] = []

    def send(self, topic: str, sanitized: dict[str, Any]) -> None:
        arrived = time.perf_counter()
        if self.delay_s > 0:
            time.sleep(self.delay_s)
        if topic in self.fail_topics or self.fail:
            self.failed.append((arrived, topic))
            raise NtfyError(f"forced failure for {topic}")
        clock_time = None if self.clock is None else self.clock.now_utc()
        payload = json.loads(json.dumps(sanitized))
        self.stamps.append((arrived, clock_time, topic, payload))
        self.sent.append((topic, payload))


class RecordingExecutor(FakeExecutor):
    """FakeExecutor that also records every attempt, including denied ones.

    ``attempts`` entries: ``{"kind": "execute"|"undo", "capability", "action", "entity_id",
    "effective_action", "outcome": "executed"|"denied"|"error"}``. ``effective_action`` is the
    service actually invoked (the inverse for an undo).
    """

    def __init__(self, **kw: Any) -> None:
        super().__init__(**kw)
        self.attempts: list[dict[str, Any]] = []

    async def execute(self, action: CandidateAction) -> ExecutionResult:
        return await self._record("execute", action, super().execute)

    async def undo(self, action: CandidateAction, prior_state: dict[str, Any]) -> ExecutionResult:
        async def call(a: CandidateAction) -> ExecutionResult:
            return await super(RecordingExecutor, self).undo(a, prior_state)

        return await self._record("undo", action, call)

    async def _record(self, kind: str, action: CandidateAction, call: Any) -> ExecutionResult:
        eff = action.action if kind == "execute" else _INVERSE_ACTION.get(action.action, "")
        rec: dict[str, Any] = {
            "kind": kind,
            "capability": action.capability,
            "action": action.action,
            "entity_id": action.entity_id,
            "effective_action": eff,
            "outcome": "executed",
        }
        self.attempts.append(rec)
        try:
            return await call(action)  # type: ignore[no-any-return]
        except Exception as exc:
            rec["outcome"] = "error" if type(exc).__name__ == "ExecutionError" else "denied"
            raise


# ── kit ──────────────────────────────────────────────────────────────────────
@dataclass
class Kit:
    """A built service and everything an experiment needs to drive and inspect it."""

    svc: LiotService
    app: LiotApp
    clock: Clock
    transport: Any
    db: aiosqlite.Connection
    backend: Any = None
    executor: Any = None
    home_id: str = HOME
    t0: datetime = field(default_factory=lambda: datetime.fromisoformat(T0))

    async def close(self) -> None:
        """Release blocked backends, drain phase 2, close the DB (always safe to call twice)."""
        release = getattr(self.backend, "release_all", None)
        if release is not None:
            release()
        try:
            await self.svc.drain_phase2(timeout=5.0)
        finally:
            await self.db.close()


async def build_service(
    *,
    backend: Any = None,
    clock: Clock | None = None,
    phase2_deadline_s: float | None = None,
    comfort_yaml: str | None = None,
    db_path: str = ":memory:",
    transport: Any = None,
    executor: Any = None,
    policy: ExecutionPolicy | None = None,
    categories: dict[str, str] | None = None,
    severity_table: Any = None,
    topic: str = TOPIC,
    secret: bytes = SECRET,
    app_kwargs: dict[str, Any] | None = None,
) -> Kit:
    """Build a real ``LiotService`` on an in-memory (or given) DB with fake I/O.

    * ``backend`` defaults to ``FixedBackend()``; ``transport`` to ``TimedTransport(clock)``.
    * ``clock`` defaults to a fixed clock at ``T0`` (UTC) — drive it with ``advance``.
    * ``comfort_yaml`` (``COMFORT_YAML`` for the vacant-light rule) enables proactive comfort
      candidates; omit for safety-only runs.
    * ``executor``/``policy`` pass through (``policy=ExecutionPolicy(enabled=True)`` opts in).
    * The caller must ``await kit.close()`` (it drains phase 2 before closing the DB).
    """
    clock = clock if clock is not None else Clock(fixed=T0)
    cats = dict(categories) if categories is not None else dict(CATS)
    db = await init_db(db_path)
    backend = backend if backend is not None else FixedBackend()
    proactive = None
    if comfort_yaml is not None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "comfort.yaml"
            path.write_text(comfort_yaml, encoding="utf-8")
            ruleset = load_proactive_ruleset(path)  # loaded eagerly; the file can go away
        proactive = ProactiveGenerator(ruleset=ruleset, categories=cats, clock=clock)
    app = LiotApp(
        clock=clock,
        categories=cats,
        proactive=proactive,
        llm_backend=backend,
        severity_table=severity_table,
        **(app_kwargs or {}),
    )
    transport = transport if transport is not None else TimedTransport(clock)
    svc = LiotService(
        app,
        db,
        clock=clock,
        transport=transport,
        topic=topic,
        secret=secret,
        executor=executor,
        policy=policy,
        phase2_deadline_s=phase2_deadline_s,
    )
    return Kit(
        svc=svc, app=app, clock=clock, transport=transport, db=db, backend=backend,
        executor=executor, t0=clock.now_dt(),
    )


# ── drivers and inspectors ───────────────────────────────────────────────────
def reading(entity_id: str, raw_value: object, at: str | datetime | None = None) -> SensorReading:
    """A ``SensorReading`` built the way the tests do (normalised value, ``singleton`` home)."""
    ts = T0 if at is None else (at.isoformat() if isinstance(at, datetime) else at)
    return SensorReading(
        entity_id=entity_id,
        source="test",
        raw_value=raw_value,
        norm_value=normalize(raw_value),
        ts_utc=ts,
        home_id=HOME,
    )


async def advance(kit: Kit, seconds: float) -> int:
    """Move the (fixed) clock forward by ``seconds`` and run one ladder tick; returns fires."""
    kit.clock.set_fixed(kit.clock.now_dt() + timedelta(seconds=seconds))
    return await kit.svc.ladder_tick()


async def advance_to(kit: Kit, seconds_since_t0: float) -> int:
    """Set the clock to ``kit.t0 + seconds_since_t0`` and run one ladder tick."""
    kit.clock.set_fixed(kit.t0 + timedelta(seconds=seconds_since_t0))
    return await kit.svc.ladder_tick()


async def add_guardian(
    kit: Kit, *, consent: bool = True, alias: str = "딸", resident_alias: str = "엄마"
) -> tuple[str, str, str]:
    """Register a resident and a guardian (optionally with consent): (rid, gid, topic)."""
    rid = await kit.svc.safety.add_resident(kit.home_id, resident_alias)
    gid, topic = await kit.svc.safety.add_guardian(kit.home_id, alias)
    if consent:
        await kit.svc.safety.grant_consent(kit.home_id, gid, rid)
    return rid, gid, topic


async def _rows(db: aiosqlite.Connection, sql: str, keys: tuple[str, ...]) -> list[dict[str, Any]]:
    cur = await db.execute(sql)
    rows = await cur.fetchall()
    await cur.close()
    return [dict(zip(keys, tuple(r), strict=True)) for r in rows]


async def judgments(db: aiosqlite.Connection) -> list[dict[str, Any]]:
    """All judgment rows (insert order) as dicts; severity columns are ints or None."""
    keys = ("judgment_id", "model_id", "gate2_verdict", "degraded", "severity",
            "severity_floor", "severity_sources", "situation_id", "snapshot_hash",
            "model_probs", "risk_tier")
    return await _rows(
        db,
        "SELECT j.judgment_id, j.model_id, j.gate2_verdict, j.degraded, j.severity,"
        " j.severity_floor, j.severity_sources, j.situation_id, j.snapshot_hash,"
        " j.model_probs, j.risk_tier FROM judgments AS j ORDER BY j.rowid",
        keys,
    )


async def proposals(db: aiosqlite.Connection) -> list[dict[str, Any]]:
    """All proposal rows (insert order); ``action`` is the decoded candidate action dict."""
    keys = ("proposal_id", "judgment_id", "state", "severity", "situation_id",
            "action_json", "risk_tier")
    rows = await _rows(
        db,
        "SELECT p.proposal_id, p.judgment_id, p.state, p.severity, p.situation_id,"
        " p.action_json, p.risk_tier FROM proposals AS p ORDER BY p.rowid",
        keys,
    )
    for r in rows:
        r["action"] = json.loads(r.pop("action_json")).get("action")
    return rows


async def situations(db: aiosqlite.Connection) -> list[dict[str, Any]]:
    """All situation rows (insert order); ``severity_floor`` is an int."""
    keys = ("situation_id", "category", "entity_id", "opened_at", "closed_at", "severity_floor")
    return await _rows(
        db,
        "SELECT s.situation_id, s.category, s.entity_id, s.opened_at, s.closed_at,"
        " s.severity_floor FROM situations AS s ORDER BY s.rowid",
        keys,
    )


async def guardian_forwards(kit: Kit, home: str | None = None) -> list[dict[str, Any]]:
    """Guardian-forward rows for ``home`` (default: the kit's home) as plain dicts."""
    fwds = await kit.svc.safety.list_forwards(home or kit.home_id)
    return [
        {
            "forward_id": f.forward_id,
            "home_id": f.home_id,
            "situation_id": f.situation_id,
            "guardian_id": f.guardian_id,
            "kind": f.kind,
            "at": f.at,
        }
        for f in fwds
    ]

