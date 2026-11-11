"""Pure per-run invariant checkers over recorded outcomes.

Each checker returns a list of violation dicts (empty list = pass); a violation always has
``{"invariant": <name>, "detail": <str>, ...context}``. They never touch a service — feed them
rows/records an experiment already collected (see ``service_kit`` inspectors).
"""

from __future__ import annotations

import json
from collections.abc import Iterable, Mapping
from typing import Any

from liot.core.egress_gate import NTFY_ALLOWED_KEYS
from liot.core.severity import SeverityLevel
from liot.core.traits import TRAIT_WORDS
from liot.db.safety_repo import PERSON_TRAITS
from liot.layers.l4_execution.executor import _INVERSE_ACTION, FORBIDDEN_OPEN

__all__ = [
    "check_final_ge_floor",
    "check_guardian_gate",
    "check_no_opening_exec",
    "check_no_trait_leak",
]

# Trait identifiers that must never appear in any outbound text: the three per-person traits
# plus the household flag (its column name is also a trait word).
_TRAIT_IDENTIFIERS: tuple[str, ...] = (*PERSON_TRAITS, "lives_alone")


def _v(invariant: str, detail: str, **ctx: Any) -> dict[str, Any]:
    return {"invariant": invariant, "detail": detail, **ctx}


def _as_int(value: Any) -> int | None:
    """Severity/tier given as int, IntEnum, name string or None → int (None stays None)."""
    if value is None:
        return None
    if isinstance(value, str):
        return int(SeverityLevel.from_str(value))
    return int(value)


# ── Hard gate 2: floors only rise ────────────────────────────────────────────
def check_final_ge_floor(judgment_rows: Iterable[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """Every row's final severity must be >= its severity_floor (floors only rise).

    Rows come from ``service_kit.judgments``. A row with a floor but no final severity is a
    violation too (the final value was dropped). If a row also carries ``gate1_floor`` (tier
    floor, int/name), ``risk_tier`` must be >= it.
    """
    out: list[dict[str, Any]] = []
    for row in judgment_rows:
        jid = row.get("judgment_id")
        floor = _as_int(row.get("severity_floor"))
        if floor is not None:
            final = _as_int(row.get("severity"))
            if final is None:
                out.append(_v("final_ge_floor", "floor set but final severity missing",
                              judgment_id=jid, severity_floor=floor))
            elif final < floor:
                out.append(_v("final_ge_floor", f"final severity {final} < floor {floor}",
                              judgment_id=jid, severity=final, severity_floor=floor))
        tier_floor = row.get("gate1_floor")
        if tier_floor is not None:
            tier = row.get("risk_tier")
            if tier is None or int(tier) < int(tier_floor):
                out.append(_v("final_ge_floor", f"risk_tier {tier} < gate1 floor {tier_floor}",
                              judgment_id=jid, risk_tier=tier, gate1_floor=int(tier_floor)))
    return out


# ── Hard gate 4: family paged only with consent and a CRITICAL floor ─────────
def _floors(situations: Any) -> dict[str, int]:
    if isinstance(situations, Mapping):
        return {str(k): int(v) for k, v in situations.items()}
    return {str(s["situation_id"]): int(s["severity_floor"]) for s in situations}


def check_guardian_gate(
    forwards: Iterable[Mapping[str, Any]],
    consent_state: Mapping[str, bool] | Iterable[str],
    situations: Mapping[str, Any] | Iterable[Mapping[str, Any]],
) -> list[dict[str, Any]]:
    """Guardian forwards and 119 guidance need valid consent AND ``severity_floor == CRITICAL``.

    * ``forwards``: dicts with situation_id, guardian_id, kind (``service_kit.guardian_forwards``).
    * ``consent_state``: ``{guardian_id: bool}`` or an iterable of guardian ids with valid
      consent, as of the time of the forwards (a mid-run revoke needs the state at send time).
    * ``situations``: ``{situation_id: severity_floor}`` or situation rows with those keys.
    The gate looks at the *floor* only — a model-only raise must never reach family.
    ``forward`` needs that guardian's consent; ``guidance_119`` (to the resident) needs only the
    CRITICAL floor, not a consenting guardian; ``resolved`` must go only to a guardian already
    forwarded for that situation.
    """
    critical = int(SeverityLevel.CRITICAL)
    consented = (
        {g for g, ok in consent_state.items() if ok}
        if isinstance(consent_state, Mapping)
        else set(consent_state)
    )
    floors = _floors(situations)
    rows = list(forwards)
    forwarded = {
        (r["situation_id"], r["guardian_id"]) for r in rows if r.get("kind") == "forward"
    }
    out: list[dict[str, Any]] = []
    for r in rows:
        kind, sid, gid = r.get("kind"), r.get("situation_id"), r.get("guardian_id")
        ctx = {"situation_id": sid, "guardian_id": gid, "kind": kind}
        if kind in ("forward", "guidance_119"):
            floor = floors.get(str(sid))
            if floor is None:
                out.append(_v("guardian_gate", "situation unknown, floor not provable", **ctx))
            elif floor != critical:
                out.append(_v("guardian_gate", f"{kind} with severity_floor {floor} != CRITICAL",
                              severity_floor=floor, **ctx))
        if kind == "forward" and gid not in consented:
            out.append(_v("guardian_gate", "forward to a guardian without valid consent", **ctx))
        # business-rules.md '보호자와 전달 동의'·'알림 사다리': 119 guidance reaches the resident
        # even with 0 consenting guardians when the floor is CRITICAL, so no consent test here.
        elif kind == "resolved" and (sid, gid) not in forwarded:
            out.append(_v("guardian_gate", "resolved notice to a guardian never forwarded", **ctx))
    return out


# ── Hard gate 5: the system never opens the home ─────────────────────────────
def check_no_opening_exec(exec_attempts: Iterable[Any]) -> list[dict[str, Any]]:
    """No *successful* actuator call may be an opening direction (lock.unlock, valve.open).

    ``exec_attempts``: ``RecordingExecutor.attempts`` dicts, or ``FakeExecutor.executed``
    ``CandidateAction`` objects (treated as executed). A denied or errored attempt is not a
    violation (the guard did its job). For ``kind == "undo"`` the inverse action is what runs.
    """
    out: list[dict[str, Any]] = []
    for a in exec_attempts:
        if isinstance(a, Mapping):
            cap, act = a.get("capability"), a.get("action")
            kind, outcome = a.get("kind", "execute"), a.get("outcome", "executed")
        else:
            cap, act, kind, outcome = a.capability, a.action, "execute", "executed"
        if outcome != "executed":
            continue
        eff = _INVERSE_ACTION.get(str(act), "") if kind == "undo" else act
        if (cap, eff) in FORBIDDEN_OPEN:
            out.append(_v("no_opening_exec", f"{kind} executed {cap}.{eff}",
                          capability=cap, action=act, kind=kind))
    return out


# ── Hard gate 3: resident traits never leave the device ──────────────────────
def _dump(obj: Any) -> str:
    if hasattr(obj, "model_dump"):
        obj = obj.model_dump(mode="json")
    return json.dumps(obj, ensure_ascii=False, default=str, sort_keys=True)


def _scan_text(text: str, where: str, index: int) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for ident in _TRAIT_IDENTIFIERS:
        if ident in text:
            out.append(_v("no_trait_leak", f"trait identifier {ident!r} in {where}",
                          where=where, index=index, term=ident))
    for word in TRAIT_WORDS:
        if word in text:
            out.append(_v("no_trait_leak", f"trait word {word!r} in {where}",
                          where=where, index=index, term=word))
    return out


def check_no_trait_leak(
    payloads: Iterable[Any], model_inputs: Iterable[Any]
) -> list[dict[str, Any]]:
    """Resident traits must not appear in ntfy payloads or model input.

    * ``payloads``: sanitised ntfy payload dicts (or ``(topic, payload)`` pairs, as in
      ``transport.sent``). Keys must lie in ``NTFY_ALLOWED_KEYS``; the dumped text must contain
      no trait identifier (``PERSON_TRAITS`` names, ``lives_alone``) and no ``TRAIT_WORDS``.
    * ``model_inputs``: what a backend received (``RecordingBackend.model_inputs()``), strings,
      dicts or pydantic models; scanned as text for the same terms.
    """
    out: list[dict[str, Any]] = []
    for i, p in enumerate(payloads):
        payload = p[1] if isinstance(p, tuple) and len(p) == 2 else p
        if isinstance(payload, Mapping):
            extra = sorted(set(payload) - NTFY_ALLOWED_KEYS)
            if extra:
                out.append(_v("no_trait_leak", f"payload keys outside the allowed set: {extra}",
                              where="payload", index=i, keys=extra))
        out.extend(_scan_text(_dump(payload), "payload", i))
    for i, m in enumerate(model_inputs):
        text = m if isinstance(m, str) else _dump(m)
        out.extend(_scan_text(text, "model_input", i))
    return out
