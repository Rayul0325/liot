"""Re-read every annotated number and assert it equals what the figures drew.

Run after ``make_figs.py``: ``PYTHONPATH=$PWD python experiments/paper_figs/check.py``; exit 0 = all equal.
Summary values are re-read from ``summary.json`` with the kit's resolver; derived values are recomputed by the
same function (``derive.FUNCS``) from the raw files with the arguments recorded in ``annotations.json``.
Two cross-checks beside the recorded values: the raw-computed E3 effect means must equal the E3 summary
means, and the kit's verdict labels must equal ``hypotheses.yaml``.
"""

from __future__ import annotations

import json
import math
import sys
from typing import Any

from experiments.kit.check_numbers import resolve
from experiments.paper_figs import derive
from experiments.paper_figs.data import ANNOTATIONS, summary


def _same(a: Any, b: Any) -> bool:
    if isinstance(a, bool) or isinstance(b, bool) or isinstance(a, str) or isinstance(b, str):
        return bool(a == b)
    if isinstance(a, list) or isinstance(b, list):
        return bool(a == b)
    return math.isclose(float(a), float(b), rel_tol=1e-9, abs_tol=1e-12)


def main() -> int:
    recs = json.loads(ANNOTATIONS.read_text(encoding="utf-8"))
    bad: list[str] = []
    counts = {"summary": 0, "derived": 0, "key_number": 0, "verdict": 0, "cross": 0}
    import yaml

    from experiments.kit.check_numbers import KIT_DIR
    from experiments.kit.figures import effective_verdicts

    shown = {r["id"]: r["shown"] for r in effective_verdicts()[0]}
    raw_yaml = {h["id"]: h["verdict"] for h in yaml.safe_load((KIT_DIR / "hypotheses.yaml").read_text(encoding="utf-8"))["hypotheses"]}
    for r in recs:
        kind = r["kind"]
        counts[kind] += 1
        try:
            if kind == "summary":
                raw = resolve(summary(r["exp"]), r["path"], r["agg"])
                now = raw * r["scale"] if isinstance(raw, (int, float)) and not isinstance(raw, bool) else raw
            elif kind == "derived":
                now = derive.FUNCS[r["fn"]](*r["args"])
            elif kind == "key_number":
                now = derive.key_number(r["exp"], r["path"], r["prefix"])
            elif kind == "verdict":
                now = shown[r["id"]]
                if raw_yaml[r["id"]] != now:
                    bad.append(f"{r['fig']} verdict {r['id']}: yaml {raw_yaml[r['id']]} != shown {now}")
            else:
                bad.append(f"unknown kind {kind}")
                continue
        except Exception as exc:  # noqa: BLE001 - report every failure, do not stop at the first
            bad.append(f"{r['fig']} {kind} {r.get('path') or r.get('fn') or r.get('id')}: {exc!r}")
            continue
        if not _same(now, r["value"]):
            bad.append(f"{r['fig']} {kind} {r.get('path') or r.get('fn') or r.get('id')}: drawn {r['value']!r} != re-read {now!r}")
    # cross-check: raw-computed E3 effects reproduce the E3 summary means (same sets, same definitions)
    s3 = summary("e3_policy")
    for m in ("m0.0", "m0.1"):
        base = s3["c2_2"]["exp"][m]["liot_real_vs_flat_same_liot_ceiling"]
        for metric, key in (("help", "help_diff"), ("harm", "harm_diff"), ("burden", "burden_diff")):
            counts["cross"] += 1
            raw_mean = derive.e3_c22_effect(metric, float(m[1:]), "mean")
            if not math.isclose(raw_mean, base[key]["mean"], abs_tol=2e-6):
                bad.append(f"cross-check c22 {m} {metric}: raw {raw_mean} != summary {base[key]['mean']}")
    for pol in ("flat_explicit", "liot_full", "liot_real", "binary_opt"):
        for metric, key in (("burden", "burden_per_day"), ("harm", "harm_rate")):
            counts["cross"] += 1
            raw_mean = derive.e3_policy_stat("main", f"{pol}|exp|m0.1", metric, "mean")
            sm = s3["policy_table"]["exp"]["m0.1"][pol][key]["mean"]
            if not math.isclose(raw_mean, sm, abs_tol=2e-6):
                bad.append(f"cross-check policy {pol} {metric}: raw {raw_mean} != summary {sm}")
    print(f"checked {len(recs)} annotations {counts}")
    for line in bad:
        print("MISMATCH", line)
    print("check.py:", "FAIL" if bad else "OK (exit 0)")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
