"""H1.4 fuzz: random event streams against the real ``LiotService``; count gate violations.

Each episode builds a fresh in-memory service (real LiotApp/LiotService/SafetyRepo), registers
random residents (random traits, lives_alone) and guardians (random consent, revoked/granted
mid-run), then plays 8-24 random steps: abnormal/normal/unavailable safety readings, comfort
readings + proactive ticks, fake-clock advances driving the ladder (+ guardian forwards and 119
guidance), consent changes, trait changes, feedback approvals (accept/reject/snooze/ok) and
forbidden-opening attempts (lock.unlock, valve.open — via signed feedback links, via the service
execution path and via the executor directly; also an undo of a lock.lock / valve.close).

Checks use ``experiments.common.invariants`` for hard gates 2-5 (gate 1 'safety alert never
silent' is checked here: every new situation must have produced a push when no failure was
injected). This measures whether OUR code keeps OUR rules; it says nothing about other designs.
"""

from __future__ import annotations

import contextlib
import logging
import random
import uuid
from datetime import timedelta
from typing import Any

from experiments.common import invariants
from experiments.common.service_kit import (
    COMFORT_YAML,
    HOME,
    SECRET,
    HighestBackend,
    LowestBackend,
    RaisingBackend,
    RandomBackend,
    RecordingBackend,
    RecordingExecutor,
    SlowBackend,
    TimedTransport,
    build_service,
    guardian_forwards,
    judgments,
    reading,
    situations,
)
from liot.core.contracts import CandidateAction, Proposal
from liot.core.risk import RiskTier
from liot.core.severity import SeverityLevel
from liot.layers.l3_judgment.llm_judge import LLMResult, LLMUnavailable
from liot.layers.l4_execution.executor import ExecutionPolicy
from liot.layers.l4_execution.notifier import sign_feedback

logging.getLogger("liot").setLevel(logging.CRITICAL)
BEHAVIORS = ("normal", "lowest", "highest", "raising", "slow", "random")

SENSORS: dict[str, tuple[str, object]] = {
    # category: (entity id, numeric/bool abnormal marker)
    "gas": ("binary_sensor.kitchen_gas", "on"),
    "smoke": ("binary_sensor.kitchen_smoke", "on"),
    "leak": ("binary_sensor.bath_leak", "on"),
    "door": ("binary_sensor.front_door", "on"),
    "lock": ("binary_sensor.front_lock", "on"),
    "oven": ("binary_sensor.oven_on", "on"),
    "high_temp": ("sensor.kitchen_temp", 70.0),
}
SENSORS_LIST = tuple(SENSORS)
CATEGORIES = {
    "binary_sensor.living_motion": "motion",
    "light.living_room": "light",
    **{eid: cat for cat, (eid, _) in SENSORS.items()},
}
TRAITS = ("age_75_plus", "cannot_self_evacuate", "cognitive_decline")


class ConcurBackend(RecordingBackend):
    """A model that agrees with the floors (the 'normal' behaviour)."""

    model_id = "concur-test-model"

    def _answer(self, candidate, ctx, gate1_floor, severity_floor):  # type: ignore[no-untyped-def]
        return LLMResult(
            tier=gate1_floor, confidence=0.8, rationale="concur", model_id=self.model_id,
            severity=severity_floor, probs=None,
        )


class ConsentWatchTransport(TimedTransport):
    """Transport that checks, at send time, that a guardian topic only gets a send while that
    guardian's consent is valid (consent only changes between fuzz steps, so the map is exact)."""

    def __init__(self) -> None:
        super().__init__()
        self.topic_guardian: dict[str, str] = {}
        self.consent: dict[str, bool] = {}
        self.unconsented_sends: list[dict[str, Any]] = []

    def send(self, topic: str, sanitized: dict[str, Any]) -> None:
        gid = self.topic_guardian.get(topic)
        if gid is not None and not self.consent.get(gid, False):
            self.unconsented_sends.append({"guardian_id": gid})
        super().send(topic, sanitized)


def make_backend(behavior: str, rng: random.Random) -> tuple[Any, float | None]:
    """(backend, phase2 deadline seconds or None for the default)."""
    if behavior == "normal":
        return ConcurBackend(), None
    if behavior == "lowest":
        return LowestBackend(), None
    if behavior == "highest":
        return HighestBackend(), None
    if behavior == "raising":
        exc = LLMUnavailable("fuzz") if rng.random() < 0.5 else RuntimeError("fuzz")
        return RaisingBackend(exc), None
    if behavior == "slow":
        return SlowBackend(0.03, tier=RiskTier.EMBER, severity=SeverityLevel.CRITICAL), 0.01
    if behavior == "random":
        return RandomBackend(random.Random(rng.random())), None
    raise ValueError(behavior)


def _link(svc: Any, prop: Proposal, action: str) -> str:
    tier_int = int(prop.risk_tier)
    nonce = "" if action == "undo" else svc._nonce.get(prop.proposal_id, "")
    sig = sign_feedback(SECRET, prop.proposal_id, tier_int, action, nonce)
    return (
        f"GET /feedback?p={prop.proposal_id}&t={tier_int}"
        f"&n={nonce}&sig={sig}&a={action} HTTP/1.1"
    )


def _forbidden_proposal(cap: str, action: str, category: str, entity: str) -> Proposal:
    return Proposal(
        proposal_id=f"p_{uuid.uuid4().hex[:10]}", judgment_id="j-fuzz", home_id=HOME,
        risk_tier=RiskTier.EMBER,
        action=CandidateAction(capability=cap, action=action, category=category,
                               entity_id=entity, params={}),
        rationale="fuzz", required_confirmation=RiskTier.EMBER.required_confirmation,
        perishable=False, expires_at=None, state="pending", created_at="2026-06-02T12:00:00+00:00",
    )


FORBIDDEN = (
    ("lock", "unlock", "lock", "lock.front_door"),
    ("valve", "open", "gas", "switch.gas_valve"),
)
ALLOWED_ONE_TAP = (
    ("lock", "lock", "lock", "lock.front_door"),
    ("valve", "close", "gas", "switch.gas_valve"),
)


async def run_episode(behavior: str, ep: int) -> dict[str, Any]:
    rng = random.Random(f"e2/{behavior}/{ep}")
    backend, deadline = make_backend(behavior, rng)
    executor = RecordingExecutor()
    transport = ConsentWatchTransport()
    kit = await build_service(
        backend=backend, comfort_yaml=COMFORT_YAML, categories=CATEGORIES,
        transport=transport, executor=executor, policy=ExecutionPolicy(enabled=True),
        phase2_deadline_s=deadline,
    )
    svc = kit.svc
    violations: list[dict[str, Any]] = []
    anomalies: list[dict[str, Any]] = []  # robustness notes that are not one of the five gates
    counts = {"sensor": 0, "advance": 0, "approve": 0, "attack": 0, "consent": 0, "trait": 0,
              "comfort": 0, "new_situations": 0, "silent_new_situation": 0,
              "escaped_model_exception": 0, "safety_path_exception": 0,
              "guidance_119_without_consenting_guardian": 0}
    try:
        # ── random household ────────────────────────────────────────────
        residents: list[str] = []
        for k in range(rng.randint(0, 3)):
            rid = await svc.safety.add_resident(HOME, f"R{k}")
            residents.append(rid)
            for trait in TRAITS:
                if rng.random() < 0.25:
                    by = "guardian" if trait == "cognitive_decline" else rng.choice(
                        ["resident", "guardian"])
                    await svc.safety.set_trait(rid, trait, True, by)
        if rng.random() < 0.3:
            await svc.safety.set_lives_alone(HOME, True)
        guardians: list[str] = []
        consent: dict[str, bool] = transport.consent
        n_guard = rng.randint(0, 3)
        if n_guard and not residents:
            residents.append(await svc.safety.add_resident(HOME, "R9"))
        for k in range(n_guard):
            gid, topic = await svc.safety.add_guardian(HOME, f"G{k}")
            guardians.append(gid)
            transport.topic_guardian[topic] = gid
            consent[gid] = False
            if rng.random() < 0.7:
                await svc.safety.grant_consent(HOME, gid, residents[0])
                consent[gid] = True
            if rng.random() < 0.15:
                transport.fail_topics.add(topic)
        seen_forwards = 0
        elapsed = 0.0
        failure_injected = bool(transport.fail_topics)

        async def check_new_forwards() -> None:
            nonlocal seen_forwards
            fwds = await guardian_forwards(kit)
            new = fwds[seen_forwards:]
            seen_forwards = len(fwds)
            if not new:
                return
            sits = await situations(kit.db)
            floors = {s["situation_id"]: s["severity_floor"] for s in sits}
            gate_rows = [r for r in new if r["kind"] == "forward"]
            violations.extend(invariants.check_guardian_gate(gate_rows, consent, floors))
            # 119 guidance goes to the resident even with no consenting guardian (business-rules.md
            # '동의한 보호자가 0명이어도 …'); the shared checker demands >=1 consenting guardian, so
            # the floor-only part is checked with a sentinel consent and the spec gap is counted.
            guid = [r for r in new if r["kind"] == "guidance_119"]
            violations.extend(invariants.check_guardian_gate(guid, {"__resident__": True}, floors))
            if guid and not any(consent.values()):
                counts["guidance_119_without_consenting_guardian"] += len(guid)
            resolved = [r for r in new if r["kind"] == "resolved"]
            if resolved:
                prior = {(r["situation_id"], r["guardian_id"]) for r in fwds[: len(fwds) - len(new)]
                         if r["kind"] == "forward"}
                for r in resolved:
                    if (r["situation_id"], r["guardian_id"]) not in prior:
                        violations.append({"invariant": "guardian_gate",
                                           "detail": "resolved notice without prior forward",
                                           **{k: r[k] for k in ("situation_id", "guardian_id")}})
                    if not consent.get(str(r["guardian_id"]), False):
                        violations.append({"invariant": "guardian_gate",
                                           "detail": "resolved notice after consent revoked",
                                           "guardian_id": r["guardian_id"]})

        for _ in range(rng.randint(8, 24)):
            await check_new_forwards()  # consent only changes between steps: exact snapshot
            r = rng.random()
            if r < 0.45:  # ── safety sensor reading
                counts["sensor"] += 1
                cat = rng.choice(list(SENSORS))
                eid, abn = SENSORS[cat]
                kind = rng.random()
                if kind < 0.6:
                    val: object = abn
                elif kind < 0.85:
                    val = "off" if not isinstance(abn, float) else 20.0
                else:
                    val = rng.choice(["unavailable", "unknown"])
                n_sit_before = len(await situations(kit.db))
                n_push_before = len(transport.stamps)
                transport.fail = failure_injected and rng.random() < 0.3
                try:
                    await svc.handle_reading(reading(eid, val))
                except Exception as exc:  # noqa: BLE001 - recorded, never swallowed silently
                    counts["safety_path_exception"] += 1
                    anomalies.append({"where": f"handle_reading({cat})", "exc": type(exc).__name__})
                n_sit_after = len(await situations(kit.db))
                if n_sit_after > n_sit_before:
                    counts["new_situations"] += 1
                    if not transport.fail and len(transport.stamps) <= n_push_before:
                        counts["silent_new_situation"] += 1
                        cur = await kit.db.execute(
                            "SELECT COUNT(*) FROM proposals AS p WHERE p.state = 'rejected'")
                        rejected_before = int((await cur.fetchone())[0])
                        await cur.close()
                        violations.append({
                            "invariant": "safety_never_silent",
                            "detail": f"new {cat} situation produced no push",
                            "category": cat, "rejected_proposals_in_db": rejected_before,
                            "charter_off": False,
                        })
                transport.fail = False
            elif r < 0.53:  # ── comfort reading + proactive tick
                counts["comfort"] += 1
                try:
                    await svc.handle_reading(reading("binary_sensor.living_motion", "off"))
                    await svc.handle_reading(reading("light.living_room", "on"))
                    await svc.run_tick()
                except Exception as exc:  # noqa: BLE001 - a model exception escaping the comfort path
                    counts["escaped_model_exception"] += 1
                    anomalies.append({"where": "comfort path", "exc": type(exc).__name__})
            elif r < 0.73:  # ── advance the fake clock, run the ladder
                counts["advance"] += 1
                dt = rng.choice([1, 30, 61, 95, 200, 310, 400, 700, 1200])
                elapsed += dt
                kit.clock.set_fixed(kit.clock.now_dt() + timedelta(seconds=dt))
                transport.fail = failure_injected and rng.random() < 0.2
                await svc.ladder_tick()
                transport.fail = False
                await check_new_forwards()
            elif r < 0.81:  # ── consent change
                counts["consent"] += 1
                if guardians and residents:
                    gid = rng.choice(guardians)
                    if rng.random() < 0.5:
                        await svc.safety.grant_consent(HOME, gid, rng.choice(residents))
                        consent[gid] = True
                    else:
                        await svc.safety.revoke_consent(HOME, gid, rng.choice(residents))
                        consent[gid] = False
            elif r < 0.85:  # ── traits / household
                counts["trait"] += 1
                if residents:
                    rid = rng.choice(residents)
                    trait = rng.choice(TRAITS)
                    by = "guardian" if trait == "cognitive_decline" else "resident"
                    await svc.safety.set_trait(rid, trait, rng.random() < 0.5, by)
                await svc.safety.set_lives_alone(HOME, rng.random() < 0.5)
            elif r < 0.93:  # ── feedback approval on a random stored proposal
                counts["approve"] += 1
                cur = await kit.db.execute("SELECT p.proposal_id FROM proposals AS p")
                ids = [x[0] for x in await cur.fetchall()]
                await cur.close()
                rows = [p for p in [await svc.proposals.get(i) for i in ids] if p]
                if rows:
                    prop = rng.choice(rows)
                    await svc._route_feedback(
                        _link(svc, prop, rng.choice(["accept", "accept", "reject", "snooze", "ok"]))
                    )
            else:  # ── forbidden-opening attempts + allowed one-taps + undo
                counts["attack"] += 1
                cap, act, cat, ent = rng.choice(FORBIDDEN + ALLOWED_ONE_TAP)
                prop = _forbidden_proposal(cap, act, cat, ent)
                await svc.dispatch(prop)
                route = rng.random()
                if route < 0.4:
                    for _ in range(2):  # double-confirm
                        await svc._route_feedback(_link(svc, prop, "accept"))
                elif route < 0.7:
                    await svc._try_execute(prop)  # service execution path, bypassing the policy
                elif route < 0.85:
                    with contextlib.suppress(Exception):  # a denial is the expected outcome
                        await executor.execute(prop.action)  # the executor directly
                else:
                    await svc._try_execute(prop)
                    await svc._route_feedback(_link(svc, prop, "undo"))

        await svc.drain_phase2()
        await check_new_forwards()
        # ── final invariant sweep ───────────────────────────────────────
        judg = await judgments(kit.db)
        violations.extend(invariants.check_final_ge_floor(judg))
        violations.extend(invariants.check_no_opening_exec(executor.attempts))
        for u in transport.unconsented_sends:
            violations.append({"invariant": "guardian_gate",
                               "detail": "send to a guardian topic without valid consent", **u})
        violations.extend(invariants.check_no_trait_leak(transport.sent, backend.model_inputs()))
        fwds = await guardian_forwards(kit)
        return {
            "behavior": behavior, "episode": ep, "n_violations": len(violations),
            "violations": violations[:20], "anomalies": anomalies[:10],
            "pushes": len(transport.stamps),
            "forwards": sum(1 for f in fwds if f["kind"] == "forward"),
            "guidance_119": sum(1 for f in fwds if f["kind"] == "guidance_119"),
            "resolved": sum(1 for f in fwds if f["kind"] == "resolved"),
            "exec_attempts": len(executor.attempts),
            "exec_denied": sum(1 for a in executor.attempts if a["outcome"] == "denied"),
            "exec_executed": sum(1 for a in executor.attempts if a["outcome"] == "executed"),
            "exec_opening_attempts": sum(
                1 for a in executor.attempts
                if (a["capability"], a["effective_action"]) in {("lock", "unlock"), ("valve", "open")}
            ),
            "model_calls": len(backend.calls), "judgment_rows": len(judg),
            # model answers below the floor that the real composer discarded: a design WITHOUT
            # the floor rule would have lowered the alert here (structural, not a comparison)
            "answers_below_floor_discarded": sum(
                1 for j in judg if j["gate2_verdict"] == "injection_suspect"),
            "critical_floor_situations": sum(
                1 for s in await situations(kit.db) if s["severity_floor"] == 3),
            "n_guardians": len(guardians), "n_residents": len(residents), **counts,
        }
    finally:
        await kit.close()
