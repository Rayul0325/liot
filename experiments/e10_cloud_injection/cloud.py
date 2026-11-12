"""E10 cloud judges (Jev API, Claude Haiku, Gemini) on the injected / control items, budget-capped.

    PYTHONPATH=$PWD .venv/bin/python -m experiments.e10_cloud_injection.cloud --probe
    PYTHONPATH=$PWD .venv/bin/python -m experiments.e10_cloud_injection.cloud --judges jev-api,haiku --k 8
    PYTHONPATH=$PWD .venv/bin/python -m experiments.e10_cloud_injection.cloud --judges gemini --k 4

Interfaces (they differ, the report says so): jev-api = JevBackend.judge_raw (one POST, both questions,
per-option probabilities); haiku and gemini = the same generative-JSON path through
`liot.cheap_llm.build_request` (system text and question JSON are E5's, imported not copied).
Every outbound evidence string comes from EvidenceBuilder (emit_external, Destination.LLM) and the
generative judges pass it through emit_external once more. Keys come from the existing loader and are
never printed. OpenAI is not used. A hard cost cap stops the run and is recorded.

Raw responses go ONLY to raw/cloud_<judge>.jsonl (git-ignored). Derived answers go to
raw/answers_cloud.jsonl (committed): option ids, latency, tokens, load1.
"""

from __future__ import annotations

import argparse
import dataclasses
import json
import threading
import time
from pathlib import Path
from typing import Any

from experiments.common import io
from experiments.e5_models.cloud import (
    _HAIKU_SYSTEM,
    BudgetStop,
    haiku_user_prompt,
    parse_haiku_reply,
)
from experiments.e5_models.cloud import Meter as _E5Meter
from experiments.e10_cloud_injection import common as C

RAW = C.RAW
DERIVED = RAW / "answers_cloud.jsonl"
USAGE_PATH = RAW / "cloud_usage.json"  # git-ignored (name starts with cloud)
BUDGET_USD = 0.80
CALL_TIMEOUT_S = 40.0
MAX_CONSECUTIVE_ERRORS = 6
GEMINI_MAX_OUT = 1024  # thinking "low" still spends ~250 tokens before the JSON answer (256 truncates)

_write_lock = threading.Lock()


class Meter(_E5Meter):
    """E5's meter, but the cap also binds ACROSS invocations: the day book in this experiment's private
    usage file (raw/cloud_usage.json) carries the cumulative estimate, capped at the same budget."""

    def __init__(self, budget_usd: float, usage_path: Path) -> None:
        from liot.day_cloud import CloudLimits, UsageMeter, load_cloud_settings

        _, prices = load_cloud_settings()
        limits = CloudLimits(
            call_cap_per_run=100_000,
            cost_cap_per_run_usd=budget_usd,
            cost_cap_per_day_usd=budget_usd,
            usage_path=usage_path,
        )
        self.meter = UsageMeter(limits, prices)


def _save_raw(judge: str, key: tuple[str, str, str], seconds: float, data: Any, err: str | None) -> None:
    io.append_jsonl(
        RAW / f"cloud_{judge}.jsonl",
        {"judge": judge, "key": list(key), "seconds": round(seconds, 4), "error": err,
         "response": data, "meta": io.run_meta()},
    )


def _tokens(judge: str, data: dict[str, Any]) -> tuple[int | None, int | None]:
    if judge == "gemini":
        meta = data.get("usageMetadata")
        if not isinstance(meta, dict):
            return None, None
        out = (meta.get("candidatesTokenCount") or 0) + (meta.get("thoughtsTokenCount") or 0)
        return meta.get("promptTokenCount"), out
    usage = data.get("usage")
    if not isinstance(usage, dict):
        return None, None
    return usage.get("input_tokens"), usage.get("output_tokens")


class JevTransport:
    def __init__(self, inner: Any, meter: Meter, state: dict[str, Any]) -> None:
        self.inner, self.meter, self.state = inner, meter, state

    def post(self, payload: dict[str, Any], *, timeout_s: float) -> dict[str, Any]:
        self.meter.check()
        t0 = time.perf_counter()
        try:
            data = self.inner.post(payload, timeout_s=timeout_s)
        except Exception as exc:
            _save_raw("jev-api", self.state["key"], time.perf_counter() - t0, None,
                      type(exc).__name__ + ": " + str(exc)[:80])
            raise
        dt = time.perf_counter() - t0
        tin, tout = _tokens("jev-api", data)
        self.meter.record("jev-api", "jev-latest", dt, tin, tout)
        _save_raw("jev-api", self.state["key"], dt, data, None)
        return data


class GenTransport:
    def __init__(self, inner: Any, meter: Meter, state: dict[str, Any], judge: str, model: str) -> None:
        self.inner, self.meter, self.state, self.judge, self.model = inner, meter, state, judge, model

    def post(self, request: Any, *, timeout_s: float) -> dict[str, Any]:
        self.meter.check()
        t0 = time.perf_counter()
        try:
            data = self.inner.post(request, timeout_s=timeout_s)
        except Exception as exc:
            _save_raw(self.judge, self.state["key"], time.perf_counter() - t0, None,
                      type(exc).__name__ + ": " + str(exc)[:80])
            raise
        dt = time.perf_counter() - t0
        tin, tout = _tokens(self.judge, data)
        self.meter.record(self.judge, self.model, dt, tin, tout)
        _save_raw(self.judge, self.state["key"], dt, data, None)
        return data


def call_jev(b: Any, judge: str, key: tuple[str, str, str], backend: Any) -> dict[str, Any]:
    from liot.layers.l3_judgment.llm_judge import LLMUnavailable

    t0 = time.perf_counter()
    try:
        res = backend.judge_raw(b.candidate, b.ctx, b.gate1.floor_tier, severity_floor=b.severity_floor)
    except LLMUnavailable as exc:
        return C.row(b, judge, key, error=str(exc)[:80], seconds=round(time.perf_counter() - t0, 4))
    usage = backend.last_usage
    probs = res.probs or {}
    return C.row(
        b, judge, key,
        tier=res.tier.name.lower(),
        severity=None if res.severity is None else res.severity.name.lower(),
        p_tier=[round(float(probs["tier"][k]), 4) for k in C.TIERS] if probs.get("tier") else None,
        p_sev=[round(float(probs["severity"][k]), 4) for k in C.SEVERITIES] if probs.get("severity") else None,
        seconds=usage.get("seconds"),
        input_tokens=usage.get("input_tokens"),
        output_tokens=usage.get("output_tokens"),
    )


def call_generative(
    b: Any, judge: str, key: tuple[str, str, str], provider: str, cfg: Any, api_key: str,
    transport: Any, evidence_builder: Any,
) -> dict[str, Any]:
    from liot.cheap_llm import CheapLLMError, build_request, parse_reply, reply_truncated
    from liot.core.egress_gate import Destination, emit_external
    from liot.core.invariants import EgressViolation

    t0 = time.perf_counter()
    evidence = evidence_builder.build(b.candidate, b.ctx, b.gate1.floor_tier)
    try:
        safe = str(emit_external(Destination.LLM, {"evidence": evidence}, trace_id=b.ctx.trace_id)["evidence"])
    except EgressViolation:
        return C.row(b, judge, key, error="egress_blocked", seconds=0.0)
    with_sev = b.severity_floor is not None
    req = build_request(cfg, api_key, _HAIKU_SYSTEM, haiku_user_prompt(safe, with_sev))
    try:
        data = transport.post(req, timeout_s=CALL_TIMEOUT_S)
    except CheapLLMError as exc:
        return C.row(b, judge, key, error=str(exc)[:80], seconds=round(time.perf_counter() - t0, 4))
    dt = time.perf_counter() - t0
    tin, tout = _tokens(judge, data)
    try:
        text, _, _ = parse_reply(provider, data)  # type: ignore[arg-type]
    except CheapLLMError:
        return C.row(b, judge, key, error="no_text", seconds=round(dt, 4), input_tokens=tin, output_tokens=tout)
    if reply_truncated(provider, data):  # type: ignore[arg-type]
        return C.row(b, judge, key, error="truncated", seconds=round(dt, 4), input_tokens=tin, output_tokens=tout)
    tier, sev, err = parse_haiku_reply(text, with_sev)
    return C.row(b, judge, key, tier=tier, severity=sev, error=err, seconds=round(dt, 4),
                 input_tokens=tin, output_tokens=tout)


def make_caller(judge: str, secrets: Any, meter: Meter, state: dict[str, Any]) -> Any:
    from liot.cheap_llm import HTTPCheapTransport, load_cheap_config
    from liot.layers.l3_judgment.decision_backend import EvidenceBuilder
    from liot.layers.l3_judgment.jev_backend import HTTPJevTransport, JevBackend

    if judge == "jev-api":
        key = secrets.get("JEV_API_KEY") or ""
        if not key:
            return None
        backend = JevBackend(key, transport=JevTransport(HTTPJevTransport(key), meter, state),
                             timeout_s=CALL_TIMEOUT_S)
        return lambda b, k: call_jev(b, judge, k, backend)
    provider, name = {"haiku": ("anthropic", "ANTHROPIC_API_KEY"), "gemini": ("gemini", "GEMINI_API_KEY")}[judge]
    key = secrets.get(name) or ""
    if not key:
        return None
    cfg = load_cheap_config(provider=provider)
    if judge == "gemini":
        cfg = dataclasses.replace(cfg, max_output_tokens=GEMINI_MAX_OUT)
    transport = GenTransport(HTTPCheapTransport(), meter, state, judge, cfg.model)
    ev = EvidenceBuilder(judge)
    return lambda b, k: call_generative(b, judge, k, provider, cfg, key, transport, ev)


def run_judge(
    judge: str, secrets: Any, built: dict[Any, Any], work: list[tuple[str, str, str]],
    meter: Meter, stop: dict[str, str],
) -> None:
    state: dict[str, Any] = {"key": ("", "", "")}
    caller = make_caller(judge, secrets, meter, state)
    if caller is None:
        stop[judge] = "no key"
        return
    done = io.done_keys(DERIVED, ("judge", "id", "variant", "arm"))
    consecutive = 0
    for key in work:
        if (judge, *key) in done:
            continue
        state["key"] = key
        try:
            row = caller(built[key], key)
        except BudgetStop as exc:
            stop[judge] = f"budget: {exc}"
            return
        meta = io.run_meta()
        row["load1"], row["ts"] = meta["load1"], meta["ts"]
        with _write_lock:
            io.append_jsonl(DERIVED, row)
        consecutive = consecutive + 1 if row["error"] else 0
        if consecutive >= MAX_CONSECUTIVE_ERRORS:
            stop[judge] = f"{consecutive} consecutive errors (last: {row['error']})"
            return
    stop[judge] = "complete"


def probe() -> None:
    """One Gemini call on one item; prints the outcome (no text from the answer beyond option ids)."""
    from liot.secrets_env import load_secrets

    secrets = load_secrets()
    built, dsg = C.build_items(1)
    key = (sorted(dsg)[0], dsg[sorted(dsg)[0]][0], "inj")
    meter = Meter(BUDGET_USD, USAGE_PATH)
    state: dict[str, Any] = {"key": key}
    caller = make_caller("gemini", secrets, meter, state)
    if caller is None:
        print("probe: no GEMINI_API_KEY")
        return
    t0 = time.perf_counter()
    row = caller(built[key], key)
    print("probe row:", json.dumps({k: row[k] for k in (
        "tier", "severity", "error", "seconds", "input_tokens", "output_tokens")}, ensure_ascii=False))
    print("elapsed", round(time.perf_counter() - t0, 2), "s; cost summary:", json.dumps(
        {k: meter.summary()[k] for k in ("calls", "run_usd", "unknown_price_calls")}))
    io.append_jsonl(RAW / "cloud_probe.jsonl", {"judge": "gemini", "key": list(key), "row": row,
                                                  "cost": meter.summary()["run_usd"], "meta": io.run_meta()})


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--probe", action="store_true")
    parser.add_argument("--judges", default=",".join(C.CLOUD_JUDGES))
    parser.add_argument("--k", type=int, default=C.K_MAX, help="variants per case (<= K_MAX)")
    parser.add_argument("--budget", type=float, default=BUDGET_USD)
    parser.add_argument("--limit", type=int, default=0, help="smoke: only the first N work items")
    args = parser.parse_args()
    if args.probe:
        probe()
        return
    from liot.secrets_env import load_secrets

    secrets = load_secrets()
    if secrets.status != "ok":
        raise SystemExit(f"secrets not usable: {secrets.status}")
    built, dsg = C.build_items(C.K_MAX)
    work = C.work_list(built, dsg, args.k)
    if args.limit:
        work = work[: args.limit]
    RAW.mkdir(parents=True, exist_ok=True)
    meter = Meter(args.budget, USAGE_PATH)
    stop: dict[str, str] = {}
    threads = [
        threading.Thread(target=run_judge, args=(j, secrets, built, work, meter, stop), name=j)
        for j in args.judges.split(",")
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    summ = meter.summary()
    print("stop reasons:", json.dumps(stop, ensure_ascii=False))
    print("cost summary:", json.dumps({k: summ[k] for k in (
        "calls", "run_usd", "run_cap_usd", "tripped", "by_kind", "unknown_price_calls", "estimated_calls")},
        ensure_ascii=False))
    io.append_jsonl(RAW / "budget_log.jsonl", {
        "args": vars(args), "stop": stop,
        "cost": {k: summ[k] for k in ("calls", "run_usd", "run_cap_usd", "tripped")},
        "by_kind": summ["by_kind"], "meta": io.run_meta()})


if __name__ == "__main__":
    main()
