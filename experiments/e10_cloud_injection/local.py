"""E10 local judges (the five E5 models) on the injected / control items.

    PYTHONPATH=$PWD nice -n 10 .venv/bin/python -m experiments.e10_cloud_injection.local --model jevk5-4b-q4
    PYTHONPATH=$PWD zsh experiments/e10_cloud_injection/run_local.sh        # all five, one server at a time

Wraps `bench/risk/run_bench.py` (server start, DecisionBackend, LLMJudge, readout are the bench's own;
nothing is re-implemented). One llama-server at a time, same flags as the bench. Answers are the
model-only tier/severity (before the floor composition). Resumable: records already in
raw/answers_local.jsonl are skipped.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import time

from experiments.common import io
from experiments.e5_models.benchkit import run_bench
from experiments.e10_cloud_injection import common as C

DERIVED = C.RAW / "answers_local.jsonl"


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--model", required=True)
    ap.add_argument("--k", type=int, default=C.K_MAX)
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--port", type=int, default=0)
    args = ap.parse_args()
    rb = run_bench()
    models = {m["key"]: m for m in rb.load_models()}
    if args.model not in models:
        raise SystemExit(f"unknown model {args.model}")
    m = models[args.model]
    built, dsg = C.build_items(C.K_MAX)
    work = C.work_list(built, dsg, args.k)
    if args.limit:
        work = work[: args.limit]
    done = io.done_keys(DERIVED, ("judge", "id", "variant", "arm"))
    todo = [k for k in work if (args.model, *k) not in done]
    if not todo:
        print(f"{args.model}: nothing to do ({len(work)} items done)")
        return 0
    gguf = rb.MODEL_DIR / m["file"]
    if not gguf.exists():
        print(f"model file missing: {gguf}")
        return 2
    port = rb.pick_port(args.port or rb.PORT_BASE)
    base = f"http://127.0.0.1:{port}"
    rb.LOG_DIR.mkdir(parents=True, exist_ok=True)
    log_path = rb.LOG_DIR / f"e10-{m['key']}.log"
    cmd = ["llama-server", "-m", str(gguf), "--host", "127.0.0.1", "--port", str(port),
           "-c", "4096", "-np", "1", "-ngl", "999", "--no-webui"]
    with log_path.open("w", encoding="utf-8") as log:
        proc = subprocess.Popen(cmd, stdout=log, stderr=subprocess.STDOUT)
    n = 0
    try:
        load_s = rb.wait_ready(base, proc)
        print(f"{args.model}: server ready in {load_s:.1f}s, {len(todo)} items", flush=True)
        rec_t = rb.RecordingTransport(rb.HTTPDecisionTransport(base))
        backend = rb.DecisionBackend(m["key"], rec_t, timeout_s=rb.REQUEST_TIMEOUT_S)
        timed = rb.TimedBackend(backend)
        judge = rb.LLMJudge(backend=timed, registry=rb.PromptRegistry())
        ruleset = rb.Guardrail().ruleset_version
        for b in built.values():  # no resident trait / extra key reaches the model
            ev = json.loads(backend._evidence(b.candidate, b.ctx, b.gate1.floor_tier))
            if set(ev) != rb.EVIDENCE_KEYS:
                raise SystemExit(f"{b.cid}: unexpected evidence keys {sorted(ev)}")
        warm = next(b for b in built.values() if b.severity_floor is not None)
        for _ in range(2):
            rb.measure_one(warm, judge, timed, rec_t, ruleset)
        for key in todo:
            b = built[key]
            l0 = os.getloadavg()[0]
            r = rb.measure_one(b, judge, timed, rec_t, ruleset)
            row = C.row(
                b, args.model, key,
                tier=r.get("model_tier"), severity=r.get("model_severity"),
                error=None if not r.get("error") else str(r["error"])[:80],
                seconds=r["seconds"],
                p_tier=[round(float(r["probs"]["tier"][t]), 4) for t in C.TIERS] if r.get("probs") else None,
                p_sev=[round(float(r["probs"]["severity"][t]), 4) for t in C.SEVERITIES]
                if r.get("probs") and r["probs"].get("severity") else None,
                load1=round(l0, 2), ts=time.strftime("%Y-%m-%dT%H:%M:%S%z"),
                meta=io.run_meta(),
            )
            io.append_jsonl(DERIVED, row)
            n += 1
            if n % 50 == 0:
                print(f"{args.model}: {n}/{len(todo)} load1={l0:.1f}", flush=True)
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=30)
        except subprocess.TimeoutExpired:
            proc.kill()
    print(f"{args.model}: wrote {n} rows")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
