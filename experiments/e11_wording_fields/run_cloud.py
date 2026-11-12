"""E11 cloud judges (Jev API, Claude Haiku, Gemini) over the E11 arms; OpenAI is excluded.

    PYTHONPATH=$PWD .venv/bin/python -m experiments.e11_wording_fields.run_cloud [--quick] [--judges a,b] [--arms x,y]

Reuses ``experiments.e5_models.cloud`` (meter, metered transports, Jev and Haiku call paths).  Gemini mirrors
E5's Haiku generative-JSON path through the cheap_llm Gemini request builder (``gemini-3.8-flash``).  Every
evidence string is built by ``EvidenceBuilder`` -> ``emit_external(LLM)``; the Haiku/Gemini paths pass it
through ``emit_external`` once more.  Raw responses go to ``raw/cloud_<judge>.jsonl`` (git-ignored); derived
answers go to ``raw/answers_cloud.jsonl``.  Hard cap for the unit: $0.60 across all invocations (the spend of
earlier invocations is read from ``raw/budget_log.jsonl``).  A provider that fails 6 times in a row is stopped.
"""

from __future__ import annotations

import argparse
import json
import threading
import time
from pathlib import Path
from typing import Any

from experiments.common import io
from experiments.e11_wording_fields import variants
from experiments.e5_models import cloud as c5

EXP_DIR = Path(__file__).resolve().parent
RAW = EXP_DIR / "raw"
DERIVED = RAW / "answers_cloud.jsonl"
UNIT_CAP_USD = 0.60
JUDGES = ("jev-api", "haiku", "gemini")
GEMINI_MODEL = "gemini-3.8-flash"
# priority order: a budget stop cuts the least important arms first
ARM_ORDER = (
    "sentence", "operational", "rw_neutral", "rw_alarming", "f_ABCD", "f_A", "f_AB", "f_B", "f_D", "f_C",
)
REPS = {"jev-api": 2, "haiku": 2, "gemini": 1}
GEMINI_SENTENCE_REPS = 2  # test-retest basis for the C4.2 normalisation

c5.RAW = RAW  # raw cloud responses of this unit stay in this unit's raw/


def spent_so_far() -> float:
    return sum(
        float(r.get("cost", {}).get("run_usd", 0.0)) for r in io.read_jsonl(RAW / "budget_log.jsonl")
    )


class GeminiMeteredTransport(c5.CheapMeteredTransport):
    """Same as the Haiku transport but reads Gemini ``usageMetadata`` (thinking tokens bill as output)."""

    def post(self, request: Any, *, timeout_s: float) -> dict[str, Any]:
        self.meter.check()
        t0 = time.perf_counter()
        try:
            data = self.inner.post(request, timeout_s=timeout_s)
        except Exception as exc:
            c5._save_raw(self.tag, "post", time.perf_counter() - t0, None, type(exc).__name__ + ": " + str(exc)[:80])
            raise
        dt = time.perf_counter() - t0
        meta = data.get("usageMetadata") or {}
        tin = meta.get("promptTokenCount")
        tout = int(meta.get("candidatesTokenCount") or 0) + int(meta.get("thoughtsTokenCount") or 0)
        self.meter.record("gemini", self.model, dt, tin, tout)
        c5._save_raw(self.tag, "post", dt, data, None)
        return data


def call_gemini(b: Any, tag: Any, cfg: Any, key: str, transport: Any, evidence_builder: Any) -> dict[str, Any]:
    from liot.cheap_llm import CheapLLMError, build_request, parse_reply, reply_truncated
    from liot.core.egress_gate import Destination, emit_external
    from liot.core.invariants import EgressViolation

    t0 = time.perf_counter()
    evidence = evidence_builder.build(b.candidate, b.ctx, b.gate1.floor_tier)
    try:
        safe = str(emit_external(Destination.LLM, {"evidence": evidence}, trace_id=b.ctx.trace_id)["evidence"])
    except EgressViolation:
        return c5._row(b, tag, error="egress_blocked", seconds=0.0)
    with_sev = b.severity_floor is not None
    req = build_request(cfg, key, c5._HAIKU_SYSTEM, c5.haiku_user_prompt(safe, with_sev))
    try:
        data = transport.post(req, timeout_s=c5.CALL_TIMEOUT_S)
    except CheapLLMError as exc:
        return c5._row(b, tag, error=str(exc)[:80], seconds=round(time.perf_counter() - t0, 4))
    dt = time.perf_counter() - t0
    meta = data.get("usageMetadata") or {}
    tin = meta.get("promptTokenCount")
    tout = int(meta.get("candidatesTokenCount") or 0) + int(meta.get("thoughtsTokenCount") or 0)
    try:
        text, _, _ = parse_reply("gemini", data)
    except CheapLLMError:
        return c5._row(b, tag, error="no_text", seconds=round(dt, 4), input_tokens=tin, output_tokens=tout)
    if reply_truncated("gemini", data):
        return c5._row(b, tag, error="truncated", seconds=round(dt, 4), input_tokens=tin, output_tokens=tout)
    tier, sev, err = c5.parse_haiku_reply(text, with_sev)
    return c5._row(
        b, tag, tier=tier, severity=sev, error=err, seconds=round(dt, 4),
        input_tokens=tin, output_tokens=tout, served_model=cfg.model,
    )


def run_judge(
    judge: str, secrets: Any, arms: dict[str, list[Any]], meter: Any, only: str | None,
    stop: dict[str, str], quick: bool,
) -> None:
    from liot.cheap_llm import CheapModelConfig, HTTPCheapTransport, load_cheap_config
    from liot.layers.l3_judgment.decision_backend import EvidenceBuilder
    from liot.layers.l3_judgment.jev_backend import HTTPJevTransport, JevBackend

    tag = c5.Tag(judge)
    ev = EvidenceBuilder(judge)
    backend = cfg = None
    transport: Any = None
    if judge == "jev-api":
        key = secrets.get("JEV_API_KEY") or ""
        backend = JevBackend(
            key, transport=c5.JevMeteredTransport(HTTPJevTransport(key), tag, meter), timeout_s=c5.CALL_TIMEOUT_S
        )
    elif judge == "haiku":
        key = secrets.get("ANTHROPIC_API_KEY") or ""
        cfg = load_cheap_config(provider="anthropic")
        transport = c5.CheapMeteredTransport(HTTPCheapTransport(), tag, meter, cfg.model)
    else:
        key = secrets.get("GEMINI_API_KEY") or ""
        base = load_cheap_config(provider="gemini")
        cfg = CheapModelConfig(provider="gemini", model=base.model, timeout_s=base.timeout_s, max_output_tokens=1024)
        transport = GeminiMeteredTransport(HTTPCheapTransport(), tag, meter, cfg.model)
    if not key:
        stop[judge] = "no key"
        return
    done = io.done_keys(DERIVED, ("judge", "context", "id", "rep"))
    consecutive = 0
    for arm in ARM_ORDER:
        if arm not in arms:
            continue
        reps = GEMINI_SENTENCE_REPS if (judge == "gemini" and arm == "sentence") else REPS[judge]
        if quick:
            reps = 1
        for rep in range(reps):
            for b in arms[arm]:
                if only and not b.cid.startswith(only):
                    continue
                if (judge, arm, b.cid, rep) in done:
                    continue
                tag.context, tag.case, tag.rep = arm, b.cid, rep
                try:
                    if judge == "jev-api":
                        row = c5.call_jev(b, tag, backend)
                    elif judge == "haiku":
                        row = c5.call_haiku(b, tag, cfg, key, transport, ev)
                    else:
                        row = call_gemini(b, tag, cfg, key, transport, ev)
                except c5.BudgetStop as exc:
                    stop[judge] = f"budget: {exc}"
                    return
                row["load1"] = io.run_meta()["load1"]
                row["ts"] = io.run_meta()["ts"]
                with c5._write_lock:
                    io.append_jsonl(DERIVED, row)
                consecutive = consecutive + 1 if row["error"] else 0
                if consecutive >= c5.MAX_CONSECUTIVE_ERRORS:
                    stop[judge] = f"{consecutive} consecutive errors (last: {row['error']})"
                    return
    stop[judge] = "complete"


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--quick", action="store_true")
    ap.add_argument("--judges", default=",".join(JUDGES))
    ap.add_argument("--arms", default=",".join(ARM_ORDER))
    args = ap.parse_args()
    from liot.secrets_env import load_secrets

    secrets = load_secrets()
    if secrets.status != "ok":
        raise SystemExit(f"secrets not usable: {secrets.status}")
    want = args.arms.split(",")
    arms = {a: variants.build_arm(a) for a in want}
    only = "sit-gas" if args.quick else None
    if args.quick:
        arms = {a: arms[a] for a in ("operational", "f_A") if a in arms}
    RAW.mkdir(parents=True, exist_ok=True)
    left = UNIT_CAP_USD - spent_so_far()
    if left <= 0.005:
        raise SystemExit(f"unit cap reached (spent {spent_so_far():.4f} of {UNIT_CAP_USD})")
    meter = c5.Meter(left, RAW / "cloud_usage.json")
    stop: dict[str, str] = {}
    threads = [
        threading.Thread(target=run_judge, args=(j, secrets, arms, meter, only, stop, args.quick), name=j)
        for j in args.judges.split(",")
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    summ = meter.summary()
    print("stop reasons:", json.dumps(stop, ensure_ascii=False))
    keys = ("calls", "run_usd", "run_cap_usd", "tripped", "by_kind", "unknown_price_calls", "estimated_calls")
    print("cost summary:", json.dumps({k: summ[k] for k in keys}, ensure_ascii=False))
    io.append_jsonl(
        RAW / "budget_log.jsonl",
        {"stop": stop, "cost": {k: summ[k] for k in ("calls", "run_usd", "run_cap_usd", "tripped")},
         "by_kind": summ["by_kind"], "quick": args.quick, "meta": io.run_meta()},
    )


if __name__ == "__main__":
    main()
