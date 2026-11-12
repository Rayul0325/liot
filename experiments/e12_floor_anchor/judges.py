"""E12 judge calls on captured evidence strings: Jev, Haiku, Gemini, each under three floor values.

Floor-swap design (registered C4.5(b), metric review 2026-11-12): the Gate1 floor is one optional field,
``gate1_floor``, in the evidence JSON the judges see.  For every captured step each judge gets
  true  - the floor the simulator sent (an unchanged round trip; checked)
  up    - the floor one tier higher   (not defined when the true floor is ember)
  down  - the floor one tier lower    (not defined when the true floor is ambient)
and nothing else in the evidence changes.

Interfaces differ and the report says so: Jev = JevBackend request shape (one POST, per-option
probabilities); Haiku and Gemini = generative JSON through the cheap-LLM request builder (the model writes
{"tier": ..., "severity": ...}; no probabilities), the same prompt as E5's Haiku path.

Every outbound evidence string goes through emit_external(Destination.LLM) once more.  Keys come from the
existing loader (never printed).  Raw responses go ONLY to raw/cloud_<judge>.jsonl (git-ignored).
"""

from __future__ import annotations

import json
import threading
import time
from dataclasses import replace
from pathlib import Path
from typing import Any

from experiments.common import io
from experiments.e5_models.cloud import (
    _HAIKU_SYSTEM,
    BudgetStop,
    haiku_user_prompt,
    parse_haiku_reply,
)

EXP_DIR = Path(__file__).resolve().parent
RAW = EXP_DIR / "raw"
STEPS = RAW / "steps.jsonl"
CLOUD_STEPS = RAW / "cloud_steps.jsonl"
ANSWERS = RAW / "answers.jsonl"
USAGE_PATH = RAW / "cloud_usage_judges.json"  # private day book, git-ignored
TIERS = ("ambient", "breath", "dusk", "ember")
SEVERITIES = ("record", "notice", "warning", "critical")
JUDGES = ("jev", "haiku", "gemini")
CALL_TIMEOUT_S = 30.0
GEMINI_MAX_OUT = 1024  # room for the thinking tokens; the answer itself is ~15 tokens
MAX_CONSECUTIVE_ERRORS = 6

_write_lock = threading.Lock()


# ── evidence variants ────────────────────────────────────────────────────────
def floor_variants(floor: str) -> dict[str, str]:
    """cond -> floor value to show.  true always; up/down only when a tier exists in that direction."""
    i = TIERS.index(floor)
    out = {"true": floor}
    if i + 1 < len(TIERS):
        out["up"] = TIERS[i + 1]
    if i - 1 >= 0:
        out["down"] = TIERS[i - 1]
    return out


def set_floor(evidence: str, floor: str) -> str:
    """The same evidence JSON with only ``gate1_floor`` replaced (same serialisation as EvidenceBuilder)."""
    d = json.loads(evidence)
    if "gate1_floor" not in d:
        raise ValueError("evidence has no gate1_floor")
    d["gate1_floor"] = floor
    return json.dumps(d, ensure_ascii=False)


def jev_body(evidence: str, with_severity: bool, model: str = "jev-latest") -> dict[str, Any]:
    """Same body as JevBackend.request_body for this evidence (a test pins the equality)."""
    from liot.layers.l3_judgment.decision_backend import SEVERITY_QUESTION, TIER_QUESTION
    from liot.layers.l3_judgment.jev_backend import _question

    qs: dict[str, Any] = {"risk": _question(TIER_QUESTION)}
    if with_severity:
        qs["severity"] = _question(SEVERITY_QUESTION)
    return {"model": model, "state": evidence, "questions": qs}


def _sanitize(evidence: str, trace_id: str) -> str | None:
    from liot.core.egress_gate import Destination, emit_external
    from liot.core.invariants import EgressViolation

    try:
        return str(emit_external(Destination.LLM, {"evidence": evidence}, trace_id=trace_id)["evidence"])
    except EgressViolation:
        return None


# ── budget meter (private day book, hard cap) ────────────────────────────────
class Meter:
    def __init__(self, budget_usd: float, usage_path: Path = USAGE_PATH) -> None:
        from liot.day_cloud import CloudLimits, UsageMeter, load_cloud_settings

        _, prices = load_cloud_settings()
        limits = CloudLimits(call_cap_per_run=100_000, cost_cap_per_run_usd=budget_usd,
                             cost_cap_per_day_usd=budget_usd * 100, usage_path=usage_path)
        self.meter = UsageMeter(limits, prices)

    def check(self) -> None:
        if not self.meter.allow():
            raise BudgetStop(self.meter.trip_reason_ko() or "budget")

    def record(self, judge: str, model: str, seconds: float, tin: int | None, tout: int | None) -> None:
        self.meter.record(judge, model, seconds=seconds, input_tokens=tin, output_tokens=tout)

    def summary(self) -> dict[str, Any]:
        return self.meter.summary()


def _save_raw(judge: str, key: dict[str, Any], seconds: float, data: Any, err: str | None) -> None:
    with _write_lock:
        io.append_jsonl(RAW / f"cloud_{judge}.jsonl",
                        {**key, "seconds": round(seconds, 4), "error": err, "response": data})


# ── per-judge calls: each returns a derived row (no model text) ──────────────
def call_jev(evidence: str, with_sev: bool, transport: Any, meter: Meter, key: dict[str, Any]) -> dict[str, Any]:
    from liot.layers.l3_judgment.decision_backend import SEVERITY_QUESTION, TIER_QUESTION
    from liot.layers.l3_judgment.jev_backend import read_jev_probs
    from liot.layers.l3_judgment.llm_judge import LLMUnavailable

    meter.check()
    t0 = time.perf_counter()
    try:
        data = transport.post(jev_body(evidence, with_sev), timeout_s=CALL_TIMEOUT_S)
    except LLMUnavailable as exc:
        _save_raw("jev", key, time.perf_counter() - t0, None, str(exc)[:80])
        return {"error": str(exc)[:80], "seconds": round(time.perf_counter() - t0, 4)}
    dt = time.perf_counter() - t0
    usage = data.get("usage") if isinstance(data.get("usage"), dict) else {}
    meter.record("jev", "jev-latest", dt, usage.get("input_tokens"), usage.get("output_tokens"))
    _save_raw("jev", key, dt, data, None)
    try:
        answers = data["answers"]
        tp = read_jev_probs(answers.get("risk"), TIER_QUESTION.option_ids)
        sp = read_jev_probs(answers.get("severity"), SEVERITY_QUESTION.option_ids) if with_sev else None
    except (KeyError, TypeError, LLMUnavailable) as exc:
        return {"error": f"parse: {str(exc)[:60]}", "seconds": round(dt, 4)}
    return {"tier": max(tp, key=lambda k: tp[k]), "p_tier": [round(tp[k], 4) for k in TIERS],
            "severity": None if sp is None else max(sp, key=lambda k: sp[k]),
            "seconds": round(dt, 4), "input_tokens": usage.get("input_tokens"),
            "output_tokens": usage.get("output_tokens"), "served_model": data.get("model")
            if isinstance(data.get("model"), str) else None, "error": None}


def _gemini_tokens(data: dict[str, Any]) -> tuple[int | None, int | None]:
    meta = data.get("usageMetadata")
    if not isinstance(meta, dict):
        return None, None
    out = (meta.get("candidatesTokenCount") or 0) + (meta.get("thoughtsTokenCount") or 0)
    return meta.get("promptTokenCount"), out  # thinking tokens are billed as output: count them


def call_generative(judge: str, evidence: str, with_sev: bool, cfg: Any, api_key: str, transport: Any,
                    meter: Meter, key: dict[str, Any]) -> dict[str, Any]:
    from liot.cheap_llm import CheapLLMError, build_request, parse_reply, reply_truncated

    provider = cfg.provider
    req = build_request(cfg, api_key, _HAIKU_SYSTEM, haiku_user_prompt(evidence, with_sev))
    meter.check()
    t0 = time.perf_counter()
    try:
        data = transport.post(req, timeout_s=CALL_TIMEOUT_S)
    except CheapLLMError as exc:
        _save_raw(judge, key, time.perf_counter() - t0, None, str(exc)[:80])
        return {"error": str(exc)[:80], "seconds": round(time.perf_counter() - t0, 4)}
    dt = time.perf_counter() - t0
    if provider == "gemini":
        tin, tout = _gemini_tokens(data)
    else:
        u = data.get("usage") if isinstance(data.get("usage"), dict) else {}
        tin, tout = u.get("input_tokens"), u.get("output_tokens")
    meter.record(judge, cfg.model, dt, tin, tout)
    _save_raw(judge, key, dt, data, None)
    base = {"seconds": round(dt, 4), "input_tokens": tin, "output_tokens": tout, "served_model": cfg.model}
    try:
        text, _, _ = parse_reply(provider, data)
    except CheapLLMError:
        return {**base, "error": "no_text"}
    if reply_truncated(provider, data):
        return {**base, "error": "truncated"}
    tier, sev, err = parse_haiku_reply(text, with_sev)
    return {**base, "tier": tier, "severity": sev, "error": err}


# ── worker ───────────────────────────────────────────────────────────────────
def load_steps(limit: int | None = None) -> list[dict[str, Any]]:
    ev = {r["step_id"]: r for r in io.read_jsonl(CLOUD_STEPS)}
    steps = []
    for s in io.read_jsonl(STEPS):
        e = ev[s["step_id"]]
        steps.append({**s, "evidence": e["evidence"], "with_sev": "severity" in e["jev_probs"]})
    return steps if limit is None else steps[:limit]


def run_judge(judge: str, secrets: Any, steps: list[dict[str, Any]], reps: int, meter: Meter,
              stop: dict[str, str], answers: Path = ANSWERS) -> None:
    from liot.cheap_llm import HTTPCheapTransport, load_cheap_config
    from liot.layers.l3_judgment.jev_backend import HTTPJevTransport

    cfg: Any = None
    transport: Any = None
    if judge == "jev":
        api_key = secrets.get("JEV_API_KEY") or ""
        transport = HTTPJevTransport(api_key) if api_key else None
    elif judge == "haiku":
        api_key = secrets.get("ANTHROPIC_API_KEY") or ""
        cfg = load_cheap_config(provider="anthropic")
        transport = HTTPCheapTransport()
    else:
        api_key = secrets.get("GEMINI_API_KEY") or ""
        cfg = replace(load_cheap_config(provider="gemini"), max_output_tokens=GEMINI_MAX_OUT)
        transport = HTTPCheapTransport()
    if not api_key:
        stop[judge] = "no key"
        return
    done = io.done_keys(answers, ("judge", "cond", "step_id", "rep"))
    consecutive = 0
    for rep in range(reps):
        for s in steps:  # steps outer, conditions inner: a budget stop leaves balanced coverage
            variants = floor_variants(s["floor"])
            for cond, shown in variants.items():
                if (judge, cond, s["step_id"], rep) in done:
                    continue
                ev = set_floor(s["evidence"], shown)
                if cond == "true" and ev != s["evidence"]:
                    raise AssertionError("true-floor round trip changed the evidence")
                safe = _sanitize(ev, f"e12-{s['step_id']}")
                key = {"judge": judge, "cond": cond, "step_id": s["step_id"], "rep": rep}
                if safe is None:
                    row: dict[str, Any] = {"error": "egress_blocked"}
                else:
                    try:
                        if judge == "jev":
                            row = call_jev(safe, s["with_sev"], transport, meter, key)
                        else:
                            row = call_generative(judge, safe, s["with_sev"], cfg, api_key, transport,
                                                  meter, key)
                    except BudgetStop as exc:
                        stop[judge] = f"budget: {exc}"
                        return
                meta = io.run_meta()
                row = {**key, "given_floor": shown, "true_floor": s["floor"], "tier": None, "severity": None,
                       **row, "load1": meta["load1"], "ts": meta["ts"]}
                with _write_lock:
                    io.append_jsonl(answers, row)
                consecutive = consecutive + 1 if row.get("error") else 0
                if consecutive >= MAX_CONSECUTIVE_ERRORS:
                    stop[judge] = f"{consecutive} consecutive errors (last: {row.get('error')})"
                    return
    stop[judge] = "complete"
