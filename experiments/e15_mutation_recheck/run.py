"""E15 runner: mutation re-measurement after the E8 gap tests (C4.11).

    PYTHONPATH=$PWD .venv/bin/python -m experiments.e15_mutation_recheck.run --scratch DIR --all
    PYTHONPATH=$PWD .venv/bin/python -m experiments.e15_mutation_recheck.run --scratch DIR --phase coverage
    PYTHONPATH=$PWD .venv/bin/python -m experiments.e15_mutation_recheck.run --scratch DIR --quick 8 --raw-name raw_quick
    PYTHONPATH=$PWD .venv/bin/python -m experiments.e15_mutation_recheck.run --gemini [--quick 3]
    PYTHONPATH=$PWD .venv/bin/python -m experiments.e15_mutation_recheck.run --analyze

The mutation engine is E8's, imported (``experiments.e8_mutation.run``), never copied: private scratch
copies, ``try/finally`` restore + sha256 check after every mutant (also on timeout/crash), canary per
module, final integrity check of every copy, this worktree and (read-only) the main checkout. Only the
output directory is redirected (``e8.EXP_DIR``), so E8's committed raw/ is never touched.

Phases (each resumable through ``done_keys``):
  coverage  one full-suite run with ``--cov-context=test`` -> coverage.jsonl + line_load.jsonl
            (per executed line: number of distinct test functions that execute it and their static
            assertion count, ``density.py``)
  sweep     E8's mutants (same operators, same 10 modules) at the current code and tests
  recheck   120 s timeouts re-judged at 900 s with one worker (E8's procedure)
  full      covered core survivors against the whole suite (E8's second pass; core tier only)
  fresh     a seeded sample of mutants from operators E8 never had (fresh_ops.py), run against the
            current tests (raw/fresh_new/) and against E8-time tests (raw/fresh_old/, commit 1e83525
            tests on the current code) so the same mutants are scored before/after the gap tests
"""

from __future__ import annotations

import argparse
import json
import os
import random
import shutil
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

from experiments.common import io
from experiments.e8_mutation import run as e8
from experiments.e15_mutation_recheck import analyze, density, fresh_ops, identity

EXP_DIR = Path(__file__).resolve().parent
REPO = io.repo_root()
e8.EXP_DIR = EXP_DIR  # redirect E8's output root; E8's own raw/ stays untouched

OLD_REF = "1e83525"  # the commit E8 measured (source sha256 of all 10 targets verified in identity)
FRESH_SEED = 20261201
FRESH_PER_MODULE = 30
# Time cut (decided while running, before any fresh outcome was analysed; recorded as 미수행 in RESULTS):
# the full-suite-heavy modules need > 75 s per mutant under the machine load (load1 ~ 55); `risk` runs
# 37 test files in ~128 s, longer than the 120 s mutant timeout, so its unmutated tests already time
# out. Fresh sample size per module after the cut:
FRESH_CAP = {"liot/core/risk.py": 0, "liot/core/severity.py": 10,
             "liot/layers/l3_judgment/guardrail.py": 10}
STATE = EXP_DIR / "raw"


def log(msg: str) -> None:
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)


# ── coverage with contexts ───────────────────────────────────────────────────
def test_function_table() -> dict[str, dict[str, int]]:
    out: dict[str, dict[str, int]] = {}
    for p in sorted((REPO / "tests").glob("test_*.py")):
        out.update(density.function_counts(p.read_text(encoding="utf-8"), f"tests/{p.name}"))
    return out


def phase_coverage(scratch: Path, raw: Path, originals: dict[str, bytes]) -> None:
    if io.read_jsonl(raw / "coverage.jsonl") and io.read_jsonl(raw / "line_load.jsonl"):
        return
    base = e8.prepare_workers(scratch / "cov", 1, originals)[0]
    log("coverage run (full suite, --cov-context=test)")
    res = e8.run_pytest(
        base, ["tests"], 7200, extra=["--cov=liot", "--cov-context=test", "--cov-report="]
    )
    if res["returncode"] != 0:
        raise SystemExit(f"coverage run failed: {res['tail']}")
    out = base / "cov_ctx.json"
    env = {**os.environ, "PYTHONPATH": str(base)}
    subprocess.run(
        [sys.executable, "-m", "coverage", "json", "--show-contexts", "-o", str(out)],
        cwd=base, env=env, check=True, capture_output=True, timeout=900,
    )
    data = json.loads(out.read_text())
    table = test_function_table()
    for f in (raw / "coverage.jsonl", raw / "line_load.jsonl"):
        f.unlink(missing_ok=True)
    for rel in e8.TARGETS:
        fd = data["files"][rel]
        s = fd["summary"]
        io.append_jsonl(raw / "coverage.jsonl", {
            "module": rel, "num_statements": s["num_statements"],
            "covered_lines": s["covered_lines"], "percent_covered": s["percent_covered"],
            "missing_lines": fd["missing_lines"], "suite_duration_s": res["duration_s"],
            "meta": io.run_meta(),
        })
        lines: dict[str, list[int]] = {}
        for ln, ctxs in fd.get("contexts", {}).items():
            tests = {t for t in (density.context_to_test(c) for c in ctxs) if t}
            known = [table.get(t) for t in tests]
            a = sum(k["asserts"] + k["raises"] for k in known if k)
            a_only = sum(k["asserts"] for k in known if k)
            unknown = sum(1 for k in known if k is None)
            lines[ln] = [len(tests), a, a_only, unknown]
        io.append_jsonl(raw / "line_load.jsonl", {
            "module": rel, "fields": ["n_tests", "assertions", "asserts_only", "n_unknown_tests"],
            "lines": lines, "meta": io.run_meta(),
        })
    log(f"coverage done in {res['duration_s']}s")


# ── main sweep (E8's engine, E8's mutants) ───────────────────────────────────
def main_args(a: argparse.Namespace, raw_name: str) -> argparse.Namespace:
    return argparse.Namespace(
        scratch=str(a.scratch), workers=min(a.workers, 4), max_hours=a.max_hours,
        quick=a.quick, raw_name=raw_name, skip_full=True, full_timeout=a.full_timeout,
    )


def phase_sweep(a: argparse.Namespace, raw: Path, raw_name: str) -> None:
    log("sweep: E8 mutants at current code")
    rc = e8.run_all(main_args(a, raw_name))
    if rc:
        raise SystemExit("integrity failure in sweep")


def phase_recheck(a: argparse.Namespace, raw_name: str) -> None:
    raw = EXP_DIR / raw_name
    if not any(r["outcome"] == "timeout" for r in io.read_jsonl(raw / "mutants.jsonl")):
        return
    log("recheck of 120 s timeouts")
    e8.phase_recheck_timeouts(argparse.Namespace(scratch=str(a.scratch), raw_name=raw_name))


def phase_full(a: argparse.Namespace, raw_name: str) -> None:
    raw = EXP_DIR / raw_name
    originals = {rel: e8.git_head_blob(rel) for rel in e8.TARGETS}
    workers = e8.prepare_workers(Path(a.scratch), min(a.workers, 4), originals)
    tests = {rel: e8.select_tests(rel) for rel in e8.TARGETS}
    cov = {r["module"]: set(r["missing_lines"]) for r in io.read_jsonl(raw / "coverage.jsonl")}
    plan = [p for p in e8.enumerate_mutants(originals, cov, set()) if p[3] == "core"]
    sweep = e8.Sweep(workers, raw, originals, tests, time.time() + 3 * 3600)
    log("full-suite second pass over covered core survivors")
    e8.phase_full(sweep, plan, raw, argparse.Namespace(workers=len(workers),
                                                        full_timeout=a.full_timeout))
    bad = e8.final_integrity(workers, originals, raw)
    log(f"integrity after full pass: {bad or 'ALL EQUAL'}")
    if bad:
        raise SystemExit("integrity failure in full pass")


# ── fresh sample ─────────────────────────────────────────────────────────────
def fresh_plan(raw: Path, originals: dict[str, bytes]) -> list[dict[str, Any]]:
    path = raw / "fresh_plan.jsonl"
    rows = io.read_jsonl(path)
    if rows:
        return rows
    rng = random.Random(FRESH_SEED)
    for rel in e8.TARGETS:
        ms = fresh_ops.generate_fresh(originals[rel], rel, rel)
        pick = sorted(rng.sample(range(len(ms)), min(FRESH_PER_MODULE, len(ms))))
        for i in pick:
            m = ms[i]
            io.append_jsonl(path, {
                **m.to_record(), "mutant_id": e8.mutant_id(m), "tier": "fresh",
                "module_population": len(ms), "seed": FRESH_SEED,
            })
    return io.read_jsonl(path)


def fresh_subset(raw: Path, plan: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """The cut sample actually run (seeded sub-sample of the drawn plan); recorded once."""
    path = raw / "fresh_subset.jsonl"
    rows = io.read_jsonl(path)
    if rows:
        return rows
    rng = random.Random(FRESH_SEED + 1)
    for rel in e8.TARGETS:
        mine = [r for r in plan if r["module"] == rel]
        k = min(FRESH_CAP.get(rel, FRESH_PER_MODULE), len(mine))
        for r in sorted(rng.sample(mine, k), key=lambda r: r["mutant_id"]):
            io.append_jsonl(path, {"mutant_id": r["mutant_id"], "module": rel})
    return io.read_jsonl(path)


def old_tests_dir(scratch: Path) -> Path:
    """Extract the E8-time ``tests/`` (commit OLD_REF) into scratch once; returns the parent dir."""
    dest = scratch / "old_tests_ref"
    if (dest / "tests").is_dir():
        return dest
    dest.mkdir(parents=True, exist_ok=True)
    tar = scratch / "old_tests.tar"
    with tar.open("wb") as fh:
        subprocess.run(["git", "archive", OLD_REF, "tests"], cwd=REPO, stdout=fh, check=True,
                       timeout=120)
    subprocess.run(["tar", "-xf", str(tar), "-C", str(dest)], check=True, timeout=120)
    return dest


def select_tests_in(tests_dir: Path, rel: str) -> list[str]:
    mod = e8.module_name(rel)
    hits = set(e8.ALWAYS_TESTS)
    for t in sorted(tests_dir.glob("test_*.py")):
        if any(n == mod or n.startswith(mod + ".") for n in e8.test_refs(t)):
            hits.add(f"tests/{t.name}")
    return sorted(hits)


def phase_fresh(a: argparse.Namespace, which: str) -> None:
    """which: 'new' (current tests) or 'old' (E8-time tests on current code)."""
    scratch = Path(a.scratch)
    raw = EXP_DIR / "raw" / f"fresh_{which}"
    originals = {rel: e8.git_head_blob(rel) for rel in e8.TARGETS}
    plan = fresh_plan(EXP_DIR / "raw", originals)
    keep = {r["mutant_id"] for r in fresh_subset(EXP_DIR / "raw", plan)}
    plan = [r for r in plan if r["mutant_id"] in keep]
    n_workers = min(a.workers, 4)
    workers = e8.prepare_workers(scratch / f"fresh_{which}", n_workers, originals)
    if which == "old":
        ref = old_tests_dir(scratch)
        for w in workers:
            shutil.rmtree(w / "tests")
            shutil.copytree(ref / "tests", w / "tests")
        tests = {rel: select_tests_in(ref / "tests", rel) for rel in e8.TARGETS}
    else:
        tests = {rel: e8.select_tests(rel) for rel in e8.TARGETS}
    ok = e8.phase_baseline(workers, raw, tests)
    excluded = {r for r, good in ok.items() if not good}
    log(f"fresh_{which}: baseline ok {sum(ok.values())}/{len(ok)}, excluded {sorted(excluded)}")
    byid: dict[str, Any] = {}
    for rel in e8.TARGETS:
        for m in fresh_ops.generate_fresh(originals[rel], rel, rel):
            byid[e8.mutant_id(m)] = m
    done = {k[0] for k in io.done_keys(raw / "mutants.jsonl", ("mutant_id",))}
    todo = [r for r in plan if r["mutant_id"] not in done and r["module"] not in excluded]
    if a.quick:
        todo = todo[: a.quick]
    sweep = e8.Sweep(workers, raw, originals, tests, time.time() + a.max_hours * 3600)
    log(f"fresh_{which}: {len(todo)} mutants to run")
    with ThreadPoolExecutor(max_workers=n_workers) as ex:
        futs = [ex.submit(sweep.run_one, byid[r["mutant_id"]], r["mutant_id"], True, "fresh")
                for r in todo]
        for i, f in enumerate(futs, 1):
            f.result()
            if i % 25 == 0:
                log(f"  fresh_{which} {i}/{len(futs)}, load1={os.getloadavg()[0]:.1f}")
    bad = e8.final_integrity(workers, originals, raw)
    log(f"fresh_{which} integrity: {bad or 'ALL EQUAL'}")
    if bad:
        raise SystemExit("integrity failure in fresh sweep")


# ── identity table (E8 old mutants vs new mutants) ───────────────────────────
def phase_identity(raw: Path) -> None:
    path = raw / "identity.jsonl"
    path.unlink(missing_ok=True)
    e8_raw = EXP_DIR.parent / "e8_mutation" / "raw"
    old_plan = {r["module"]: r["source_sha256"] for r in io.read_jsonl(e8_raw / "plan.jsonl")}
    old_rows = io.read_jsonl(e8_raw / "mutants.jsonl")
    new_rows = io.read_jsonl(raw / "mutants.jsonl")
    for rel in e8.TARGETS:
        old_blob = subprocess.run(
            ["git", "show", f"{OLD_REF}:{rel}"], cwd=REPO, capture_output=True, check=True,
            timeout=30,
        ).stdout
        assert e8.sha256(old_blob) == old_plan[rel], f"{rel}: {OLD_REF} blob != E8 measured source"
        new_blob = e8.git_head_blob(rel)
        same = old_blob == new_blob
        lmap = None if same else identity.line_map(old_blob, new_blob)
        recs = identity.match_module(
            [r for r in old_rows if r["module"] == rel],
            [r for r in new_rows if r["module"] == rel], lmap,
        )
        for r in recs:
            io.append_jsonl(path, {**r, "source_changed": not same})
    log(f"identity table written: {len(io.read_jsonl(path))} rows")


def phase_density(raw: Path, scratch: Path) -> None:
    """Descriptive module-level density, now and at E8 time (module-level view; not the verdict)."""
    path = raw / "module_density.jsonl"
    path.unlink(missing_ok=True)
    ref = old_tests_dir(scratch) / "tests"
    for rel in e8.TARGETS:
        for label, tdir in (("current", REPO / "tests"), ("e8_time", ref)):
            files = [tdir / Path(f).name for f in select_tests_in(tdir, rel)]
            d = density.module_density(files)
            io.append_jsonl(path, {"module": rel, "tests_version": label, **d})


# ── driver ───────────────────────────────────────────────────────────────────
def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
    ap.add_argument("--analyze", action="store_true")
    ap.add_argument("--gemini", action="store_true")
    ap.add_argument("--all", action="store_true")
    ap.add_argument("--phase", choices=["coverage", "sweep", "recheck", "full", "fresh_new",
                                        "fresh_old", "identity", "density"])
    ap.add_argument("--scratch")
    ap.add_argument("--workers", type=int, default=4)
    ap.add_argument("--max-hours", type=float, default=2.5)
    ap.add_argument("--quick", type=int, default=0)
    ap.add_argument("--raw-name", default="raw")
    ap.add_argument("--full-timeout", type=int, default=600)
    a = ap.parse_args()
    raw = EXP_DIR / a.raw_name
    if a.analyze:
        summary = analyze.summarize(EXP_DIR / "raw")
        print(f"summary: {io.write_summary(EXP_DIR, summary)}")
        print(f"figure: {analyze.make_figure(summary, EXP_DIR)}")
        return
    if a.gemini:
        from experiments.e15_mutation_recheck import gemini

        gemini.run(EXP_DIR / "raw", quick=a.quick)
        return
    if not a.scratch:
        raise SystemExit("--scratch DIR is required")
    a.scratch = Path(a.scratch)
    originals = {rel: e8.git_head_blob(rel) for rel in e8.TARGETS}
    phases = ["coverage", "sweep", "recheck", "full", "identity", "density", "fresh_new", "fresh_old"]
    todo = phases if a.all else [a.phase]
    for ph in todo:
        if ph == "coverage":
            phase_coverage(a.scratch, raw, originals)
        elif ph == "sweep":
            phase_sweep(a, raw, a.raw_name)
        elif ph == "recheck":
            phase_recheck(a, a.raw_name)
        elif ph == "full":
            phase_full(a, a.raw_name)
        elif ph == "identity":
            phase_identity(raw)
        elif ph == "density":
            phase_density(raw, a.scratch)
        elif ph == "fresh_new":
            phase_fresh(a, "new")
        elif ph == "fresh_old":
            phase_fresh(a, "old")


if __name__ == "__main__":
    main()
