"""E10 shared pieces: cases, injection variants, neutral controls and the design (C4.1, cycle-4 form).

Registered form (HYPOTHESES.md "사이클 4 가설" + "사이클 4 지표 적대 검토 반영"):
  * base cases = 36 NEW situations (`new_cases.yaml`) written by a label-blind agent that saw only an
    assignment list (time bucket + proposed action) and the 45 old sentences for style;
  * injection variants = `variants.yaml` (48, label-blind agent); control texts = `neutral_controls.yaml`
    (one per variant, length-matched to +-2 chars, no instruction);
  * an item = (case, variant); its injected arm = sentence + " " + variant text, its control arm =
    sentence + " " + the variant's neutral text. Nothing else differs (candidate, bucket, floors);
  * every case receives `K_MAX` variants from a cyclic design over a seeded shuffle of the variant ids;
    a judge may use only the first k of them (nested), the same for every judge of that run.
"""

from __future__ import annotations

import random
from pathlib import Path
from typing import Any

import yaml

from experiments.e5_models.benchkit import run_bench

EXP_DIR = Path(__file__).resolve().parent
RAW = EXP_DIR / "raw"
VARIANTS_PATH = EXP_DIR / "variants.yaml"
NEUTRAL_PATH = EXP_DIR / "neutral_controls.yaml"
CASES_PATH = EXP_DIR / "new_cases.yaml"

K_MAX = 8
DESIGN_SEED = 4101
LOCAL_MODELS = (
    "jevk5-9b-q8",
    "jevk5-4b-q8",
    "jevk5-4b-q4",
    "apus-openjev-4b-q4",
    "jev-9b-q6",
)
JEVK5 = ("jevk5-9b-q8", "jevk5-4b-q8", "jevk5-4b-q4")
CLOUD_JUDGES = ("jev-api", "haiku", "gemini")
INTERFACE = {
    "jev-api": "score-probability (Jev API, both questions in one POST)",
    "haiku": "generative-json (Claude Haiku, text answer, no probabilities)",
    "gemini": "generative-json (Gemini, text answer, no probabilities)",
    **{m: "letter-probability (llama-server first token, deterministic)" for m in LOCAL_MODELS},
}
TIERS = ("ambient", "breath", "dusk", "ember")
SEVERITIES = ("record", "notice", "warning", "critical")
ARMS = ("inj", "ctl")


def load_variants() -> list[dict[str, str]]:
    doc = yaml.safe_load(VARIANTS_PATH.read_text(encoding="utf-8"))
    return [{"id": v["id"], "style": v["style"], "text": v["text"]} for v in doc["variants"]]


def load_neutral() -> dict[str, str]:
    doc = yaml.safe_load(NEUTRAL_PATH.read_text(encoding="utf-8"))
    return {c["id"]: c["text"] for c in doc["controls"]}


def case_specs() -> list[dict[str, Any]]:
    doc = yaml.safe_load(CASES_PATH.read_text(encoding="utf-8"))
    return list(doc["cases"])


def design(case_ids: list[str], variant_ids: list[str], k: int = K_MAX) -> dict[str, list[str]]:
    """case id -> its k variant ids (cyclic over a seeded shuffle)."""
    perm = list(variant_ids)
    random.Random(DESIGN_SEED).shuffle(perm)
    n = len(perm)
    if k > n:
        raise ValueError("more variants per case than variants")
    return {cid: [perm[(i * k + j) % n] for j in range(k)] for i, cid in enumerate(sorted(case_ids))}


def build_items(k: int = K_MAX) -> tuple[dict[tuple[str, str, str], Any], dict[str, list[str]]]:
    """({(case id, variant id, arm): Built}, design).

    Built through the production path (`build_one`: Guardrail floor, severity table, trait-word
    check) on the modified sentence. The floor must equal that of the unmodified case.
    """
    from liot.core.severity import load_severity_table
    from liot.layers.l3_judgment.guardrail import Guardrail

    rb = run_bench()
    guard = Guardrail()
    table = load_severity_table()
    if table.load_failed:
        raise SystemExit(f"severity table failed: {table.load_error}")
    specs = case_specs()
    variants = {v["id"]: v["text"] for v in load_variants()}
    neutral = load_neutral()
    dsg = design([s["id"] for s in specs], list(variants), k)
    out: dict[tuple[str, str, str], Any] = {}
    for s in specs:
        b0 = rb.build_one(s, guard, table)
        for vid in dsg[s["id"]]:
            for arm, text in (("inj", variants[vid]), ("ctl", neutral[vid])):
                spec = dict(s)
                spec["context"] = f"{s['context']} {text}"
                b = rb.build_one(spec, guard, table)
                if (b.gate1.floor_tier, b.severity_floor) != (b0.gate1.floor_tier, b0.severity_floor):
                    raise SystemExit(f"{s['id']}+{vid}: text changed the floor — design invalid")
                out[(s["id"], vid, arm)] = b
    return out, dsg


def work_list(
    built: dict[tuple[str, str, str], Any], dsg: dict[str, list[str]], k: int
) -> list[tuple[str, str, str]]:
    """Ordered work: item-major (both arms of an item adjacent), first k variants of each case,
    round-robin over cases so a cut run still covers all cases."""
    keys: list[tuple[str, str, str]] = []
    for j in range(k):
        for cid in sorted(dsg):
            vid = dsg[cid][j]
            keys += [(cid, vid, "inj"), (cid, vid, "ctl")]
    assert all(key in built for key in keys)
    return keys


def row(b: Any, judge: str, key: tuple[str, str, str], **kw: Any) -> dict[str, Any]:
    cid, vid, arm = key
    return {
        "judge": judge,
        "id": cid,
        "variant": vid,
        "arm": arm,
        "tier_floor": b.gate1.floor_tier.name.lower(),
        "severity_floor": None if b.severity_floor is None else b.severity_floor.name.lower(),
        "tier": None,
        "severity": None,
        "p_tier": None,
        "p_sev": None,
        "error": None,
        "seconds": None,
        "input_tokens": None,
        "output_tokens": None,
        **kw,
    }
