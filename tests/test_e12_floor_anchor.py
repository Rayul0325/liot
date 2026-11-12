"""E12 (C4.5) experiment helpers: the floor-swap variants change only ``gate1_floor`` and the Jev body built on
captured evidence is the body JevBackend itself would send."""

from __future__ import annotations

import json
from typing import Any

import pytest

pytest.importorskip("numpy")  # 실험 분석은 [experiments] 의존성이 있어야 돈다(CI 는 미설치)

from experiments.e12_floor_anchor import analyze, judges  # noqa: E402
from liot.core.risk import RiskTier
from liot.layers.l3_judgment.jev_backend import JevBackend

EVIDENCE = json.dumps(
    {
        "context_summary": "[day] 4 signals; changed=0; safety=none; trigger=proactive",
        "time_bucket": "day",
        "candidate": {"capability": "fan", "action": "set_speed", "category": "climate",
                      "device": "PERSON_1", "params": {}},
        "gate1_floor": "breath",
    },
    ensure_ascii=False,
)


def test_floor_variants_exist_only_inside_the_tier_range() -> None:
    assert judges.floor_variants("ambient") == {"true": "ambient", "up": "breath"}
    assert judges.floor_variants("breath") == {"true": "breath", "up": "dusk", "down": "ambient"}
    assert judges.floor_variants("ember") == {"true": "ember", "down": "dusk"}


def test_set_floor_changes_only_the_floor_field_and_round_trips() -> None:
    assert judges.set_floor(EVIDENCE, "breath") == EVIDENCE
    swapped = json.loads(judges.set_floor(EVIDENCE, "dusk"))
    original = json.loads(EVIDENCE)
    assert swapped.pop("gate1_floor") == "dusk"
    original.pop("gate1_floor")
    assert swapped == original


def test_set_floor_refuses_evidence_without_a_floor() -> None:
    with pytest.raises(ValueError):
        judges.set_floor(json.dumps({"candidate": {}}), "dusk")


@pytest.mark.parametrize("with_severity", [False, True])
def test_jev_body_equals_the_backend_request_body(
    monkeypatch: pytest.MonkeyPatch, with_severity: bool
) -> None:
    class _NoPost:
        def post(self, payload: dict[str, Any], *, timeout_s: float) -> dict[str, Any]:
            raise AssertionError("no request in this test")

    backend = JevBackend("k", transport=_NoPost())
    monkeypatch.setattr(backend._evidence, "build", lambda cand, ctx, floor: EVIDENCE)
    sev: Any = None
    if with_severity:
        from liot.core.severity import SeverityLevel

        sev = SeverityLevel.NOTICE
    body = backend.request_body(None, None, RiskTier.BREATH, sev)  # type: ignore[arg-type]
    assert judges.jev_body(EVIDENCE, with_severity) == body


def test_cluster_ratio_resamples_scene_types_not_steps() -> None:
    import numpy as np

    rng = np.random.default_rng(1)
    items = [("a", 1, 1)] * 10 + [("b", 0, 1)] * 10
    r = analyze.cluster_ratio(rng, items)
    assert (r["clusters"], r["num"], r["den"], r["rate"]) == (2, 10, 20, 0.5)
    # two clusters: a resample can draw only 'a' or only 'b', so the interval spans 0..1
    assert r["lo"] == 0.0 and r["hi"] == 1.0
