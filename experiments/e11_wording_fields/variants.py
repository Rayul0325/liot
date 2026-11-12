"""Context variants of E11: field-richness arms (C4.3) and rewritten-sentence arms (C4.2).

Every arm is a list of ``Built`` cases (the bench runner's own type) whose ``ctx.nl_summary`` is replaced:
  sentence      the benchmark sentence (baseline, not rebuilt here)
  operational   ContextBuilder aggregate summary (baseline)
  f_A f_AB f_ABCD  summary + 1 / 2 / 4 fields, fixed order  A kinds, B rate, C concurrent, D duration
  f_B f_C f_D   summary + one field alone
  rw_neutral    the neutral rewrite (accepted danger-variant and calm-variant cases alike)
  rw_alarming   the alarming rewrite
Only the 45 non-injection cases are built (injection cases are outside every metric of this unit).
"""

from __future__ import annotations

import json
from typing import Any

from experiments.e5_models.benchkit import run_bench
from experiments.e11_wording_fields.prep_inputs import HERE

INPUTS = HERE / "inputs"
FIELD_ARMS: dict[str, tuple[str, ...]] = {
    "f_A": ("sensor_kinds",),
    "f_AB": ("sensor_kinds", "rate"),
    "f_ABCD": ("sensor_kinds", "rate", "concurrent", "duration"),
    "f_B": ("rate",),
    "f_C": ("concurrent",),
    "f_D": ("duration",),
}
REWRITE_ARMS = ("rw_neutral", "rw_alarming")
BASELINE_ARMS = ("sentence", "operational")
ALL_ARMS = (*BASELINE_ARMS, *FIELD_ARMS, *REWRITE_ARMS)


def load_fields() -> dict[str, dict[str, Any]]:
    """Case id -> coder-A field values (the values the models see; fixed before the run)."""
    mp = json.loads((INPUTS / "anon_codeA.json").read_text(encoding="utf-8"))
    coded = json.loads((INPUTS / "codeA_output.json").read_text(encoding="utf-8"))
    return {cid: coded[aid] for aid, cid in mp.items()}


def field_suffix(fields: dict[str, Any], present: tuple[str, ...]) -> str:
    parts = []
    for f in present:
        if f == "sensor_kinds":
            kinds = fields["sensor_kinds"]
            parts.append("sensor_kinds=" + ("+".join(kinds) if kinds else "none"))
        elif f == "rate":
            parts.append(f"rate={fields['rate']}")
        elif f == "concurrent":
            parts.append(f"concurrent={fields['concurrent']}")
        else:
            parts.append(f"abnormal_for={fields['duration']}")
    return "".join("; " + p for p in parts)


def load_accepted() -> dict[str, dict[str, str]]:
    """Arm -> {case id: accepted rewritten sentence}; written by ``accept.py`` after the checks."""
    p = INPUTS / "accepted_rewrites.json"
    return json.loads(p.read_text(encoding="utf-8")) if p.exists() else {"neutral": {}, "alarming": {}}


def _non_injection(rb: Any) -> list[Any]:
    # run_local replaces rb.build_all; keep the original so building the base cases never recurses.
    if not hasattr(rb, "_e11_orig_build_all"):
        rb._e11_orig_build_all = rb.build_all
    return [b for b in rb._e11_orig_build_all(rb.load_cases(), "sentence") if not b.spec.get("injection")]


def build_arm(arm: str) -> list[Any]:
    rb = run_bench()
    base = _non_injection(rb)
    if arm == "sentence":
        return base
    out: list[Any] = []
    if arm == "operational":
        for b in base:
            out.append(_with(b, rb.operational_summary(b)))
    elif arm in FIELD_ARMS:
        fields = load_fields()
        for b in base:
            out.append(_with(b, rb.operational_summary(b) + field_suffix(fields[b.cid], FIELD_ARMS[arm])))
    elif arm in REWRITE_ARMS:
        acc = load_accepted()[arm.split("_", 1)[1]]
        for b in base:
            if b.cid in acc:
                out.append(_with(b, acc[b.cid]))
    else:
        raise KeyError(arm)
    hits = [(b.cid, w) for b in out for w in rb.TRAIT_WORDS if w in b.ctx.nl_summary]
    if hits:
        raise SystemExit(f"trait word in {arm}: {hits}")
    return out


def _with(b: Any, text: str) -> Any:
    from liot.live_graph import Built

    return Built(
        b.cid, b.spec, b.candidate, b.ctx.model_copy(update={"nl_summary": text}),
        b.gate1, b.severity_floor, b.severity_sources,
    )


def build_local_all(arms: tuple[str, ...]) -> list[Any]:
    """One combined list with ids ``<arm>:<case>`` so one llama-server session covers every arm."""
    from liot.live_graph import Built

    out: list[Any] = []
    for arm in arms:
        for b in build_arm(arm):
            out.append(Built(f"{arm}:{b.cid}", b.spec, b.candidate, b.ctx, b.gate1, b.severity_floor, b.severity_sources))
    return out
