"""E8 runner: mutation testing of the safety-critical modules (H6.3).

    PYTHONPATH=$PWD .venv/bin/python -m experiments.e8_mutation.run --scratch DIR            # full sweep
    PYTHONPATH=$PWD .venv/bin/python -m experiments.e8_mutation.run --scratch DIR --quick 12  # smoke
    PYTHONPATH=$PWD .venv/bin/python -m experiments.e8_mutation.run --analyze                 # summary+figure

Safety design (a past incident left production code mutated after a timeout):

* Nothing outside ``--scratch`` is ever written. The main checkout and this worktree are only
  *read* (``git show HEAD:<file>``). Each of the N workers owns a full private copy of the
  tree (``scratch/w0 .. w{N-1}``), so parallel mutants cannot see each other.
* Every mutant is applied inside ``try/finally``; the ``finally`` restores the original bytes,
  deletes that module's cached bytecode and asserts ``sha256 == git HEAD`` — also after a
  timeout, a crash or Ctrl-C. A mismatch aborts the sweep (``IntegrityError``).
* At the end every target file in every copy, in this worktree and (read-only) in the main
  checkout is compared with the HEAD blob; the result is written to raw/integrity.jsonl.
* ``liot.__file__`` is asserted to resolve inside the copy, and a canary mutant
  (``raise RuntimeError`` appended to the module) must be *killed* by the selected tests
  before the first real mutant of a module runs in that copy; otherwise every mutant would
  falsely survive.

Phases (each resumable through ``done_keys``): coverage -> baseline -> canary+sweep ->
(survivors only) full-suite second pass -> integrity check.
"""

from __future__ import annotations

import argparse
import ast
import contextlib
import hashlib
import os
import queue
import random
import re
import shutil
import signal
import subprocess
import sys
import threading
import time
from collections import Counter, defaultdict
from collections.abc import Iterable
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

from experiments.common import io, plotstyle
from experiments.e8_mutation import analyze, mutate

EXP_DIR = Path(__file__).resolve().parent
REPO = io.repo_root()

TARGETS: tuple[str, ...] = (
    "liot/core/risk.py",
    "liot/core/severity.py",
    "liot/core/egress_gate.py",
    "liot/layers/l3_judgment/guardrail.py",
    "liot/layers/l3_judgment/llm_judge.py",
    "liot/layers/l4_execution/notifier.py",
    "liot/layers/l4_execution/escalation.py",
    "liot/layers/l4_execution/executor.py",
    "liot/layers/l4_execution/intervention.py",
    "liot/layers/l4_execution/feedback_gateway.py",
)
ALWAYS_TESTS = ("tests/test_invariants.py",)
MUTANT_TIMEOUT_S = 120
BASELINE_TIMEOUT_S = 900
SEED = 20261112
PYTEST_BASE = ["-o", "addopts=", "-x", "-q", "-p", "no:cacheprovider", "--no-header", "--tb=no", "-rfE"]

_STOP = threading.Event()
_ACTIVE: set[subprocess.Popen[str]] = set()
_ACTIVE_LOCK = threading.Lock()
_IO_LOCK = threading.Lock()


class IntegrityError(RuntimeError):
    """A scratch copy's target file no longer equals its git HEAD blob after a restore."""


# ── helpers ──────────────────────────────────────────────────────────────────
def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def module_name(rel: str) -> str:
    return rel[:-3].replace("/", ".")


def git_head_blob(rel: str) -> bytes:
    res = subprocess.run(
        ["git", "show", f"HEAD:{rel}"], cwd=REPO, capture_output=True, check=True, timeout=30
    )
    return res.stdout


def append(path: Path, rec: dict[str, Any]) -> None:
    with _IO_LOCK:
        io.append_jsonl(path, rec)


def pyc_clear(root: Path, rel: str) -> None:
    p = root / rel
    for pyc in (p.parent / "__pycache__").glob(f"{p.stem}.*.pyc"):
        pyc.unlink(missing_ok=True)


def test_refs(path: Path) -> set[str]:
    """Dotted `liot...` names a test file imports or mentions as a string (monkeypatch targets)."""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.update(a.name for a in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            names.add(node.module)
            names.update(f"{node.module}.{a.name}" for a in node.names)
        elif (
            isinstance(node, ast.Constant)
            and isinstance(node.value, str)
            and node.value.startswith("liot.")
        ):
            names.add(node.value)
    return names


def select_tests(rel: str) -> list[str]:
    """Test files that import/reference the module (AST scan of tests/*.py) + test_invariants."""
    mod = module_name(rel)
    hits = set(ALWAYS_TESTS)
    for t in sorted((REPO / "tests").glob("test_*.py")):
        if any(n == mod or n.startswith(mod + ".") for n in test_refs(t)):
            hits.add(f"tests/{t.name}")
    return sorted(hits)


def parse_pytest(out: str) -> dict[str, Any]:
    failed = re.findall(r"^(?:FAILED|ERROR) (\S+)", out, flags=re.M)
    passed = re.search(r"(\d+) passed", out)
    nfail = re.search(r"(\d+) failed", out)
    nerr = re.search(r"(\d+) errors?", out)
    return {
        "killed_by": failed[0] if failed else None,
        "passed": int(passed.group(1)) if passed else 0,
        "failed": int(nfail.group(1)) if nfail else 0,
        "errors": int(nerr.group(1)) if nerr else 0,
    }


def run_pytest(
    worker: Path, files: list[str], timeout: int, extra: Iterable[str] = ()
) -> dict[str, Any]:
    """Run pytest (nice -n 10, own process group) in a worker copy; never raises on timeout."""
    env = dict(os.environ)
    env["PYTHONPATH"] = str(worker)
    env["PYTHONHASHSEED"] = "0"
    env.pop("PYTEST_ADDOPTS", None)
    cmd = ["nice", "-n", "10", sys.executable, "-m", "pytest", *PYTEST_BASE, *extra, *files]
    t0 = time.perf_counter()
    proc: subprocess.Popen[str] = subprocess.Popen(
        cmd, cwd=worker, env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        text=True, start_new_session=True,
    )
    with _ACTIVE_LOCK:
        _ACTIVE.add(proc)
    timed_out = False
    try:
        try:
            out, _ = proc.communicate(timeout=timeout)
        except subprocess.TimeoutExpired:
            timed_out = True
            _kill_group(proc)
            out, _ = proc.communicate()
    finally:
        with _ACTIVE_LOCK:
            _ACTIVE.discard(proc)
    return {
        "returncode": proc.returncode,
        "conftest_error": "while loading conftest" in out,
        "timed_out": timed_out,
        "duration_s": round(time.perf_counter() - t0, 3),
        "tail": out[-1500:],
        **parse_pytest(out),
    }


def _kill_group(proc: subprocess.Popen[str]) -> None:
    with contextlib.suppress(ProcessLookupError, PermissionError):
        os.killpg(proc.pid, signal.SIGKILL)


def classify(res: dict[str, Any]) -> tuple[str, str]:
    """(outcome, kind). returncode: 0 pass, 1 test failed, 2 interrupted/collection error."""
    if res["timed_out"]:
        return "timeout", "timeout"
    rc = res["returncode"]
    if rc == 0:
        return "survived", ""
    if rc == 1:
        return "killed", "assertion"
    if rc == 2 and (res["errors"] or "rror" in res["tail"]):
        return "killed", "collection_error"
    if rc == 4 and res["conftest_error"]:  # tests/conftest.py imports liot.app -> import crash
        return "killed", "collection_error"
    return "error", f"rc={rc}"


# ── scratch copies ───────────────────────────────────────────────────────────
def _ignore(_dir: str, names: list[str]) -> set[str]:
    skip = {".git", "__pycache__", ".pytest_cache", ".venv", "experiments", ".coverage"}
    return {n for n in names if n in skip or n.endswith(".pyc")}


def prepare_workers(scratch: Path, n: int, originals: dict[str, bytes]) -> list[Path]:
    """Fresh private copies under *scratch* (never anywhere else)."""
    scratch = scratch.resolve()
    if scratch == REPO or REPO in scratch.parents or scratch == Path("/") or len(scratch.parts) < 3:
        raise SystemExit(f"refusing scratch dir {scratch}")
    scratch.mkdir(parents=True, exist_ok=True)
    workers: list[Path] = []
    for i in range(n):
        w = scratch / f"w{i}"
        if w.exists():
            shutil.rmtree(w)
        shutil.copytree(REPO, w, ignore=_ignore)
        for rel, blob in originals.items():
            (w / rel).write_bytes(blob)
        workers.append(w)
    return workers


def assert_import_root(worker: Path, rel: str) -> str:
    """The module a subprocess imports with PYTHONPATH=<worker> must live inside the worker."""
    env = {**os.environ, "PYTHONPATH": str(worker)}
    code = (
        "import importlib, liot; "
        f"m = importlib.import_module({module_name(rel)!r}); print(liot.__file__); print(m.__file__)"
    )
    res = subprocess.run(
        [sys.executable, "-c", code], cwd=worker, env=env, capture_output=True, text=True,
        timeout=120, check=True,
    )
    lines = res.stdout.strip().splitlines()
    for p in lines[-2:]:
        if not Path(p).resolve().is_relative_to(worker.resolve()):
            raise IntegrityError(f"import resolves outside the copy: {p} (copy {worker})")
    return lines[-1]


# ── phases ───────────────────────────────────────────────────────────────────
def phase_coverage(worker: Path, raw: Path) -> None:
    if io.read_jsonl(raw / "coverage.jsonl"):
        return
    out = worker / "cov.json"
    res = run_pytest(
        worker, ["tests"], 3600,
        extra=["--cov=liot", f"--cov-report=json:{out}", "--cov-report="],
    )
    if res["returncode"] != 0 or not out.exists():
        raise SystemExit(f"coverage run failed: {res['tail']}")
    import json

    data = json.loads(out.read_text())
    for rel in TARGETS:
        f = data["files"][rel]
        s = f["summary"]
        append(raw / "coverage.jsonl", {
            "module": rel,
            "num_statements": s["num_statements"],
            "covered_lines": s["covered_lines"],
            "percent_covered": s["percent_covered"],
            "missing_lines": f["missing_lines"],
            "suite_duration_s": res["duration_s"],
            "meta": io.run_meta(),
        })


def ordered_tests(files: list[str], rel: str, kills: Counter[str]) -> list[str]:
    stem = Path(rel).stem.split("_")[0]
    return sorted(files, key=lambda f: (-kills[f], stem not in Path(f).name, f))


def phase_baseline(workers: list[Path], raw: Path, tests: dict[str, list[str]]) -> dict[str, bool]:
    done = {k[0] for k in io.done_keys(raw / "baseline.jsonl", ("module",))}
    todo = [r for r in TARGETS if r not in done]
    q: queue.Queue[Path] = queue.Queue()
    for w in workers:
        q.put(w)

    def one(rel: str) -> None:
        w = q.get()
        try:
            res = run_pytest(w, tests[rel], BASELINE_TIMEOUT_S, extra=["--tb=line"])
            if res["returncode"] != 0:  # one retry: distinguishes flaky from broken
                retry = run_pytest(w, tests[rel], BASELINE_TIMEOUT_S, extra=["--tb=line"])
                res["first_attempt"] = {k: res[k] for k in ("returncode", "killed_by", "tail")}
                res.update({k: retry[k] for k in ("returncode", "killed_by", "passed", "failed",
                                                  "errors", "duration_s", "tail", "timed_out")})
            append(raw / "baseline.jsonl", {
                "module": rel,
                "tests": tests[rel],
                "n_test_files": len(tests[rel]),
                "ok": res["returncode"] == 0,
                "worker": w.name,
                "import_file": assert_import_root(w, rel),
                **res,
                "meta": io.run_meta(),
            })
        finally:
            q.put(w)

    with ThreadPoolExecutor(max_workers=len(workers)) as ex:
        list(ex.map(one, todo))
    return {r["module"]: bool(r["ok"]) for r in io.read_jsonl(raw / "baseline.jsonl")}


class Sweep:
    def __init__(self, workers: list[Path], raw: Path, originals: dict[str, bytes],
                 tests: dict[str, list[str]], deadline: float) -> None:
        self.raw = raw
        self.originals = originals
        self.tests = tests
        self.deadline = deadline
        self.q: queue.Queue[Path] = queue.Queue()
        for w in workers:
            self.q.put(w)
        self.canary_ok: set[tuple[str, str]] = set()
        self.kills: dict[str, Counter[str]] = defaultdict(Counter)
        self.lock = threading.Lock()
        self.fatal: BaseException | None = None

    def _check_sha(self, w: Path, rel: str) -> None:
        if sha256((w / rel).read_bytes()) != sha256(self.originals[rel]):
            (w / rel).write_bytes(self.originals[rel])
            raise IntegrityError(f"{w / rel} differed from git HEAD after restore")

    def _canary(self, w: Path, rel: str) -> None:
        key = (w.name, rel)
        if key in self.canary_ok:
            return
        orig = self.originals[rel]
        try:
            pyc_clear(w, rel)
            (w / rel).write_bytes(orig + b'\nraise RuntimeError("e8-canary")\n')
            res = run_pytest(w, self.tests[rel], MUTANT_TIMEOUT_S)
        finally:
            (w / rel).write_bytes(orig)
            pyc_clear(w, rel)
            self._check_sha(w, rel)
        outcome, kind = classify(res)
        append(self.raw / "canary.jsonl", {
            "module": rel, "worker": w.name, "outcome": outcome, "kind": kind,
            "duration_s": res["duration_s"], "meta": io.run_meta(),
        })
        if outcome != "killed":
            raise IntegrityError(f"canary survived for {rel} in {w.name}: harness is blind")
        self.canary_ok.add(key)

    def run_one(self, m: mutate.Mutant, mid: str, covered: bool, tier: str) -> None:
        if _STOP.is_set() or time.time() > self.deadline or self.fatal is not None:
            return
        rel = m.module
        base = {
            **m.to_record(), "mutant_id": mid, "tier": tier, "covered": covered,
            "source_sha256": sha256(self.originals[rel]),
        }
        if not covered:  # no test executes this statement: survival is certain (spot-checked)
            append(self.raw / "mutants.jsonl", {
                **base, "outcome": "survived", "kind": "uncovered", "tests_run": 0,
                "test_files": [], "duration_s": 0.0, "worker": None, "meta": io.run_meta(),
            })
            return
        w = self.q.get()
        try:
            self._canary(w, rel)
            with self.lock:
                files = ordered_tests(self.tests[rel], rel, self.kills[rel])
            res = self._run_mutant(w, m, files)
            outcome, kind = classify(res)
            if outcome == "killed" and res["killed_by"]:
                with self.lock:
                    self.kills[rel][res["killed_by"].split("::")[0]] += 1
            append(self.raw / "mutants.jsonl", {
                **base, "outcome": outcome, "kind": kind,
                "tests_run": res["passed"] + res["failed"], "test_files": files,
                "killed_by": res["killed_by"], "returncode": res["returncode"],
                "duration_s": res["duration_s"], "worker": w.name,
                "tail": res["tail"][-300:] if outcome in ("error",) else "",
                "meta": io.run_meta(),
            })
        except IntegrityError as exc:
            self.fatal = exc
            _STOP.set()
            raise
        finally:
            self.q.put(w)

    def _run_mutant(self, w: Path, m: mutate.Mutant, files: list[str],
                    extra: Iterable[str] = ()) -> dict[str, Any]:
        rel = m.module
        orig = self.originals[rel]
        try:
            (w / rel).write_bytes(mutate.apply(orig, m))
            pyc_clear(w, rel)
            return run_pytest(w, files, MUTANT_TIMEOUT_S, extra=extra)
        finally:
            (w / rel).write_bytes(orig)
            pyc_clear(w, rel)
            self._check_sha(w, rel)

    def _run_mutant_timeout(self, w: Path, m: mutate.Mutant, files: list[str],
                            timeout: int) -> dict[str, Any]:
        orig = self.originals[m.module]
        try:
            (w / m.module).write_bytes(mutate.apply(orig, m))
            pyc_clear(w, m.module)
            return run_pytest(w, files, timeout)
        finally:
            (w / m.module).write_bytes(orig)
            pyc_clear(w, m.module)
            self._check_sha(w, m.module)

    def run_full(self, m: mutate.Mutant, mid: str, timeout: int) -> None:
        """Survivor second pass: the whole suite, to separate 'no direct test' from 'no test'."""
        if _STOP.is_set() or time.time() > self.deadline or self.fatal is not None:
            return
        w = self.q.get()
        try:
            orig = self.originals[m.module]
            try:
                (w / m.module).write_bytes(mutate.apply(orig, m))
                pyc_clear(w, m.module)
                res = run_pytest(w, ["tests"], timeout, extra=[])
            finally:
                (w / m.module).write_bytes(orig)
                pyc_clear(w, m.module)
                self._check_sha(w, m.module)
            outcome, kind = classify(res)
            append(self.raw / "survivors_full.jsonl", {
                "module": m.module, "mutant_id": mid, "outcome": outcome, "kind": kind,
                "killed_by": res["killed_by"], "duration_s": res["duration_s"],
                "worker": w.name, "meta": io.run_meta(),
            })
        except IntegrityError as exc:
            self.fatal = exc
            _STOP.set()
            raise
        finally:
            self.q.put(w)


def mutant_id(m: mutate.Mutant) -> str:
    return f"{m.module}:{m.line}:{m.col}:{m.operator}:{hashlib.sha1(m.replacement).hexdigest()[:8]}"


def enumerate_mutants(
    originals: dict[str, bytes], cov: dict[str, set[int]], excluded: set[str]
) -> list[tuple[mutate.Mutant, str, bool, str]]:
    """(mutant, id, covered, tier) for every target not excluded; covered = line ran in the suite."""
    out = []
    for rel in TARGETS:
        if rel in excluded:
            continue
        for m in mutate.generate(originals[rel], rel, rel):
            tier = "extended" if m.operator in mutate.EXTENDED_OPERATORS else "core"
            out.append((m, mutant_id(m), m.cov_line not in cov[rel], tier))
    return out


def head_blob_in(root: Path, rel: str) -> bytes:
    res = subprocess.run(
        ["git", "-C", str(root), "show", f"HEAD:{rel}"], capture_output=True, check=True, timeout=30
    )
    return res.stdout


def final_integrity(workers: list[Path], originals: dict[str, bytes], raw: Path) -> list[str]:
    """Every target file in every copy == its git HEAD blob; main checkout is only read."""
    main = Path("/Users/rayul/liot")
    bad: list[str] = []
    rows: list[dict[str, Any]] = []
    roots = [(w.name, w) for w in workers] + [("worktree", REPO), ("main_checkout", main)]
    for label, root in roots:
        for rel, blob in originals.items():
            p = root / rel
            got = sha256(p.read_bytes()) if p.exists() else None
            head = sha256(blob) if label != "main_checkout" else sha256(head_blob_in(root, rel))
            ok = got == head
            rows.append({"root": label, "module": rel, "sha256": got, "head_sha256": head,
                         "equal": ok})
            if not ok:
                bad.append(f"{label}:{rel}")
    append(raw / "integrity.jsonl", {"rows": rows, "all_equal": not bad, "meta": io.run_meta()})
    return bad


# ── driver ───────────────────────────────────────────────────────────────────
def run_all(args: argparse.Namespace) -> int:
    raw = EXP_DIR / args.raw_name
    deadline = time.time() + args.max_hours * 3600
    signal.signal(signal.SIGTERM, lambda *_: (_ for _ in ()).throw(KeyboardInterrupt()))

    originals = {rel: git_head_blob(rel) for rel in TARGETS}
    for rel, blob in originals.items():  # this worktree must be clean for the targets
        if (REPO / rel).read_bytes() != blob:
            raise SystemExit(f"{rel} differs from git HEAD in the worktree; commit/restore first")
    workers = prepare_workers(Path(args.scratch), args.workers, originals)
    print(f"workers: {[str(w) for w in workers]}", flush=True)
    print(f"liot import check: {assert_import_root(workers[0], TARGETS[0])}", flush=True)

    tests = {rel: select_tests(rel) for rel in TARGETS}
    phase_coverage(workers[0], raw)
    cov = {r["module"]: set(r["missing_lines"]) for r in io.read_jsonl(raw / "coverage.jsonl")}
    ok = phase_baseline(workers, raw, tests)
    excluded = {r for r, good in ok.items() if not good}
    print(f"baseline ok: {sum(ok.values())}/{len(ok)}; excluded: {sorted(excluded)}", flush=True)

    plan = enumerate_mutants(originals, cov, excluded)
    planned = {k[0] for k in io.done_keys(raw / "plan.jsonl", ("module",))}
    for rel in TARGETS:
        if rel in planned or rel in excluded:
            continue
        mine = [p for p in plan if p[0].module == rel]
        append(raw / "plan.jsonl", {
            "module": rel, "all": len(mine), "core": sum(1 for p in mine if p[3] == "core"),
            "uncovered": sum(1 for p in mine if not p[2]),
            "source_sha256": sha256(originals[rel]), "meta": io.run_meta(),
        })
    done = {k[0] for k in io.done_keys(raw / "mutants.jsonl", ("mutant_id",))}
    todo = [p for p in plan if p[1] not in done]
    random.Random(SEED).shuffle(todo)  # a truncated sweep is then a random sample
    if args.quick:
        todo = todo[: args.quick]
    print(f"mutants: planned {len(plan)}, done {len(done)}, to run {len(todo)}", flush=True)

    sweep = Sweep(workers, raw, originals, tests, deadline)
    interrupted = False
    try:
        with ThreadPoolExecutor(max_workers=len(workers)) as ex:
            futs = [ex.submit(sweep.run_one, *p) for p in todo]
            try:
                for i, f in enumerate(futs, 1):
                    f.result()
                    if i % 25 == 0:
                        print(f"  {i}/{len(futs)} done, load1={os.getloadavg()[0]:.1f}", flush=True)
            except KeyboardInterrupt:
                interrupted = True
                _STOP.set()
                with _ACTIVE_LOCK:
                    for p in list(_ACTIVE):
                        _kill_group(p)
                raise
    finally:
        if interrupted:
            _STOP.set()
        # threads have all restored their file in `finally`; verify before anything else
        bad = final_integrity(workers, originals, raw)
        print(f"integrity: {bad or 'ALL EQUAL'}", flush=True)
    if sweep.fatal is not None:
        raise sweep.fatal

    if not args.quick and not args.skip_full:
        phase_full(sweep, plan, raw, args)
        bad = final_integrity(workers, originals, raw)
        print(f"integrity (after full pass): {bad or 'ALL EQUAL'}", flush=True)
    return 1 if bad else 0


def phase_full(sweep: Sweep, plan: list[tuple[mutate.Mutant, str, bool, str]],
               raw: Path, args: argparse.Namespace) -> None:
    rows = {r["mutant_id"]: r for r in io.read_jsonl(raw / "mutants.jsonl")}
    for rc in io.read_jsonl(raw / "timeout_recheck.jsonl"):  # re-judged timeouts
        if rows[rc["mutant_id"]]["outcome"] == "timeout":
            rows[rc["mutant_id"]]["outcome"] = rc["outcome"]
    done = {k[0] for k in io.done_keys(raw / "survivors_full.jsonl", ("mutant_id",))}
    byid = {p[1]: p[0] for p in plan}
    todo = [byid[mid] for mid, r in rows.items()
            if r["outcome"] == "survived" and r["covered"] and mid not in done and mid in byid]
    print(f"full-suite second pass over {len(todo)} covered survivors", flush=True)
    with ThreadPoolExecutor(max_workers=args.workers) as ex:
        list(ex.map(lambda m: sweep.run_full(m, mutant_id(m), args.full_timeout), todo))


def phase_full_only(args: argparse.Namespace) -> None:
    """Full-suite pass for covered survivors that have no survivors_full record yet."""
    raw = EXP_DIR / args.raw_name
    originals = {rel: git_head_blob(rel) for rel in TARGETS}
    workers = prepare_workers(Path(args.scratch), args.workers, originals)
    tests = {rel: select_tests(rel) for rel in TARGETS}
    cov = {r["module"]: set(r["missing_lines"]) for r in io.read_jsonl(raw / "coverage.jsonl")}
    plan = enumerate_mutants(originals, cov, set())
    sweep = Sweep(workers, raw, originals, tests, time.time() + 3 * 3600)
    phase_full(sweep, plan, raw, args)
    bad = final_integrity(workers, originals, raw)
    print(f"integrity: {bad or 'ALL EQUAL'}", flush=True)


def phase_confirm_kills(args: argparse.Namespace) -> None:
    """Re-run, for every assertion-killed mutant, ONLY the test that killed it (alone).

    The sweep ran under heavy machine load (load1 up to ~100), so a timing-sensitive test could
    have failed for load rather than because of the mutation. A kill is *confirmed* when that one
    test fails again on the mutant and passes on the unmutated copy (baseline per node, once).
    """
    raw = EXP_DIR / args.raw_name
    originals = {rel: git_head_blob(rel) for rel in TARGETS}
    workers = prepare_workers(Path(args.scratch), args.workers, originals)
    tests = {rel: select_tests(rel) for rel in TARGETS}
    cov = {r["module"]: set(r["missing_lines"]) for r in io.read_jsonl(raw / "coverage.jsonl")}
    byid = {p[1]: p for p in enumerate_mutants(originals, cov, set())}
    sweep = Sweep(workers, raw, originals, tests, time.time() + 3 * 3600)
    done = {k[0] for k in io.done_keys(raw / "kill_confirm.jsonl", ("mutant_id",))}
    todo = [r for r in io.read_jsonl(raw / "mutants.jsonl")
            if r["outcome"] == "killed" and r["kind"] == "assertion"
            and r["mutant_id"] not in done]
    nodes = sorted({r["killed_by"] for r in todo if r.get("killed_by")})
    base_ok: dict[str, bool] = {}
    base_q: queue.Queue[Path] = queue.Queue()
    for w in workers:
        base_q.put(w)

    def base_one(node: str) -> None:
        w = base_q.get()
        try:
            res = run_pytest(w, [node], 300)
            if res["returncode"] in (4, 5):  # node id cut at a space: use its file
                res = run_pytest(w, [node.split("::")[0]], 600)
            base_ok[node] = res["returncode"] == 0
        finally:
            base_q.put(w)

    with ThreadPoolExecutor(max_workers=len(workers)) as ex:
        list(ex.map(base_one, nodes))

    def one(r: dict[str, Any]) -> None:
        m = byid[r["mutant_id"]][0]
        node = r.get("killed_by") or ""
        w = sweep.q.get()
        try:
            sweep._canary(w, m.module)
            res = sweep._run_mutant_timeout(w, m, [node], 300)
            target = node
            if res["returncode"] in (4, 5):
                target = node.split("::")[0]
                res = sweep._run_mutant_timeout(w, m, [target], 600)
            outcome, kind = classify(res)
            append(raw / "kill_confirm.jsonl", {
                "mutant_id": r["mutant_id"], "module": m.module, "node": node,
                "ran": target, "outcome": outcome, "kind": kind,
                "baseline_node_passes": base_ok.get(node),
                "confirmed": outcome == "killed" and bool(base_ok.get(node)),
                "load1": os.getloadavg()[0], "duration_s": res["duration_s"],
                "meta": io.run_meta(),
            })
        finally:
            sweep.q.put(w)

    with ThreadPoolExecutor(max_workers=len(workers)) as ex:
        list(ex.map(one, todo))
    bad = final_integrity(workers, originals, raw)
    print(f"integrity: {bad or 'ALL EQUAL'}", flush=True)


def phase_uncovered_check(args: argparse.Namespace, n: int = 12) -> None:
    """Really run a sample of 'uncovered' mutants to confirm they survive (validates the skip)."""
    raw = EXP_DIR / args.raw_name
    originals = {rel: git_head_blob(rel) for rel in TARGETS}
    workers = prepare_workers(Path(args.scratch), args.workers, originals)
    tests = {rel: select_tests(rel) for rel in TARGETS}
    cov = {r["module"]: set(r["missing_lines"]) for r in io.read_jsonl(raw / "coverage.jsonl")}
    plan = [p for p in enumerate_mutants(originals, cov, set()) if not p[2]]
    random.Random(SEED + 1).shuffle(plan)
    sweep = Sweep(workers, raw, originals, tests, time.time() + 3600)
    done = {k[0] for k in io.done_keys(raw / "uncovered_check.jsonl", ("mutant_id",))}

    def one(p: tuple[mutate.Mutant, str, bool, str]) -> None:
        m, mid, _, _ = p
        if mid in done:
            return
        w = sweep.q.get()
        try:
            sweep._canary(w, m.module)
            res = sweep._run_mutant(w, m, tests[m.module])
            outcome, kind = classify(res)
            append(raw / "uncovered_check.jsonl", {
                "mutant_id": mid, "module": m.module, "line": m.line, "operator": m.operator,
                "outcome": outcome, "kind": kind, "killed_by": res["killed_by"],
                "duration_s": res["duration_s"], "meta": io.run_meta(),
            })
        finally:
            sweep.q.put(w)

    with ThreadPoolExecutor(max_workers=args.workers) as ex:
        list(ex.map(one, plan[:n]))
    bad = final_integrity(workers, originals, raw)
    print(f"integrity: {bad or 'ALL EQUAL'}", flush=True)


def phase_recheck_timeouts(args: argparse.Namespace) -> None:
    """Re-run every mutant that hit the 120 s timeout with a 900 s limit and *no parallel load*
    from this runner (1 worker): separates a real hang from a slow run on a loaded machine."""
    raw = EXP_DIR / args.raw_name
    originals = {rel: git_head_blob(rel) for rel in TARGETS}
    workers = prepare_workers(Path(args.scratch), 1, originals)
    tests = {rel: select_tests(rel) for rel in TARGETS}
    cov = {r["module"]: set(r["missing_lines"]) for r in io.read_jsonl(raw / "coverage.jsonl")}
    byid = {p[1]: p for p in enumerate_mutants(originals, cov, set())}
    done = {k[0] for k in io.done_keys(raw / "timeout_recheck.jsonl", ("mutant_id",))}
    sweep = Sweep(workers, raw, originals, tests, time.time() + 4 * 3600)
    for r in io.read_jsonl(raw / "mutants.jsonl"):
        if r["outcome"] != "timeout" or r["mutant_id"] in done:
            continue
        m = byid[r["mutant_id"]][0]
        sweep._canary(workers[0], m.module)
        t0 = time.perf_counter()
        res = sweep._run_mutant_timeout(workers[0], m, tests[m.module], 900)
        outcome, kind = classify(res)
        append(raw / "timeout_recheck.jsonl", {
            "mutant_id": r["mutant_id"], "module": m.module, "line": m.line,
            "outcome": outcome, "kind": kind, "killed_by": res["killed_by"],
            "duration_s": res["duration_s"], "load1": os.getloadavg()[0], "meta": io.run_meta(),
        })
        print(f"recheck {m.module}:{m.line} -> {outcome}/{kind} {time.perf_counter() - t0:.0f}s",
              flush=True)
    bad = final_integrity(workers, originals, raw)
    print(f"integrity: {bad or 'ALL EQUAL'}", flush=True)


def make_figure(summary: dict[str, Any]) -> Path:
    import matplotlib.pyplot as plt

    plotstyle.setup()
    mods = summary["modules"]
    fig, (ax1, ax2) = plt.subplots(1, 2, figsize=(11, 4.4), gridspec_kw={"width_ratios": [1.15, 1]})
    for name, m in mods.items():
        if m["score_core"] is None:
            continue
        short = name.split("/")[-1][:-3]
        x, y = m["coverage_pct"], 100 * m["score_core"]
        ax1.scatter(x, y, s=60, color=plotstyle.ACCENT, zorder=3)
        lo, hi = m["score_core_ci95"]
        ax1.vlines(x, 100 * lo, 100 * hi, color=plotstyle.GREY, linewidth=1.2, zorder=2)
        ax1.annotate(short, (x, y), textcoords="offset points", xytext=(5, 4), fontsize=8)
    ax1.axhline(90, color=plotstyle.GREY_DARK, linestyle="--", linewidth=1)
    ax1.axvline(90, color=plotstyle.GREY_DARK, linestyle=":", linewidth=1)
    ax1.set_xlim(min(90, min(m['coverage_pct'] for m in mods.values()) - 1), 101)
    ax1.set_ylim(0, 102)
    ax1.set_xlabel("줄 커버리지 (%)")
    ax1.set_ylabel("뮤테이션 점수 (%, 지정 연산자·직접 관련 테스트)")
    ax1.set_title("커버리지 vs 뮤테이션 점수 (점선 = 90%)")
    ops = summary["by_operator"]
    names = [k for k in ops if ops[k]["n"] > 0]
    vals = [100 * ops[k]["score"] for k in names]
    colors = [plotstyle.ACCENT if k in mutate.OPERATORS else plotstyle.GREY for k in names]
    ax2.barh(names, vals, color=colors)
    for i, k in enumerate(names):
        ax2.text(vals[i] + 1, i, f"{ops[k]['killed']}/{ops[k]['n']}", va="center", fontsize=8)
    ax2.axvline(90, color=plotstyle.GREY_DARK, linestyle="--", linewidth=1)
    ax2.set_xlim(0, 112)
    ax2.set_xlabel("잡은 비율 (%)")
    ax2.set_title("연산자별 (파랑 = 지정, 회색 = 추가)")
    ax2.invert_yaxis()
    fig.tight_layout()
    return plotstyle.save(fig, EXP_DIR, "coverage_vs_mutation")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
    ap.add_argument("--analyze", action="store_true")
    ap.add_argument("--scratch", help="dir for the private worker copies (never the repo)")
    ap.add_argument("--workers", type=int, default=4)
    ap.add_argument("--max-hours", type=float, default=3.5)
    ap.add_argument("--quick", type=int, default=0, help="smoke: run only N mutants")
    ap.add_argument("--raw-name", default="raw")
    ap.add_argument("--skip-full", action="store_true")
    ap.add_argument("--full-timeout", type=int, default=600)
    ap.add_argument("--uncovered-check", action="store_true")
    ap.add_argument("--recheck-timeouts", action="store_true")
    ap.add_argument("--full-only", action="store_true")
    ap.add_argument("--confirm-kills", action="store_true")
    args = ap.parse_args()
    if args.analyze:
        summary = analyze.summarize(EXP_DIR / args.raw_name)
        print(f"summary: {io.write_summary(EXP_DIR, summary)}")
        print(f"figure: {make_figure(summary)}")
        return
    if not args.scratch:
        raise SystemExit("--scratch DIR is required")
    args.workers = min(args.workers, 4)
    if args.uncovered_check:
        phase_uncovered_check(args)
        return
    if args.recheck_timeouts:
        phase_recheck_timeouts(args)
        return
    if args.full_only:
        phase_full_only(args)
        return
    if args.confirm_kills:
        phase_confirm_kills(args)
        return
    sys.exit(run_all(args))


if __name__ == "__main__":
    main()
