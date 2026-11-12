"""E11 (wording/fields experiment) helpers: rule map validation, field egress hygiene, rewrite checks."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml

from experiments.e11_wording_fields import checks, rule, variants
from liot.core.egress_gate import Destination, emit_external
from liot.core.traits import TRAIT_WORDS

MAP = Path(rule.MAP_PATH)


def test_rule_map_parses_and_every_field_value_maps_to_a_tier() -> None:
    m = rule.load_map()
    for kind in rule.KINDS:
        for trig in rule.TRIGGERS:
            for rate in rule.RATES:
                f = {"sensor_kinds": [kind], "rate": rate, "concurrent": "1", "duration": "unknown"}
                assert 0 <= rule.hazard(m, f, rule.FIELDS, trig) <= 3


def test_rule_map_rejects_unknown_key_and_out_of_range(tmp_path: Path) -> None:
    doc = yaml.safe_load(MAP.read_text(encoding="utf-8"))
    bad = dict(doc, extra={"x": 1})
    p = tmp_path / "a.yaml"
    p.write_text(yaml.safe_dump(bad), encoding="utf-8")
    with pytest.raises(rule.RuleMapError):
        rule.load_map(p)
    doc["kind_h"]["gas"]["h"] = 9
    p.write_text(yaml.safe_dump(doc), encoding="utf-8")
    with pytest.raises(rule.RuleMapError):
        rule.load_map(p)


def test_hazard_ignores_fields_not_given_and_empty_kinds() -> None:
    m = rule.load_map()
    f = {"sensor_kinds": [], "rate": "rising_fast", "concurrent": "3plus", "duration": "gt60m"}
    assert rule.hazard(m, f, rule.FIELDS, "sensor_reading") == 0  # no anomalous sensor: deltas ignored
    g = {"sensor_kinds": ["gas"], "rate": "falling", "concurrent": "1", "duration": "unknown"}
    assert rule.hazard(m, g, ("sensor_kinds",), "sensor_reading") == m["kind_h"]["gas"]


def test_every_field_arm_passes_egress_and_has_no_trait_word() -> None:
    from liot.layers.l3_judgment.decision_backend import EvidenceBuilder

    ev = EvidenceBuilder("test-e11")
    for arm in variants.FIELD_ARMS:
        built = variants.build_arm(arm)
        assert len(built) == 45
        for b in built:
            text = b.ctx.nl_summary
            assert not any(w in text for w in TRAIT_WORDS)
            payload = json.loads(ev.build(b.candidate, b.ctx, b.gate1.floor_tier))  # emit_external inside
            assert set(payload) == {"context_summary", "time_bucket", "candidate", "gate1_floor"}
            emit_external(Destination.LLM, {"evidence": json.dumps(payload, ensure_ascii=False)}, trace_id="t")


def test_checks_flag_number_tag_length_and_added_entity() -> None:
    orig = "[day] 오븐 220도 40분째 가동, 주방 무인."
    ents = ["오븐", "주방"]
    assert checks.check_one(orig, "[day] 오븐 220도 40분째 가동, 주방 무인.", ents) == []
    assert any(f.startswith("numbers") for f in checks.check_one(orig, "[day] 오븐 220도 41분째 가동, 주방 무인.", ents))
    assert "tag" in checks.check_one(orig, "[night] 오븐 220도 40분째 가동, 주방 무인.", ents)
    assert any(f.startswith("length") for f in checks.check_one(orig, orig + " " + orig, ents))
    assert any(f.startswith("entities_added") for f in checks.check_one(orig, "[day] 오븐 220도 40분째 가동, 주방 무인, 화재.", ents))
    assert any(f.startswith("entities_missing") for f in checks.check_one(orig, "[day] 220도 40분째 가동, 주방 무인.", ents))


def test_field_information_counts_pairs_and_default_cases() -> None:
    pytest.importorskip("numpy")  # analyze 는 [experiments] 의존성이 필요하다(CI 는 미설치)
    from experiments.e11_wording_fields import analyze

    null = {"sensor_kinds": [], "rate": "unknown", "concurrent": "0", "duration": "unknown"}
    hot = {"sensor_kinds": ["gas"], "rate": "rising", "concurrent": "1", "duration": "unknown"}
    design = {
        "codes_A": {"p/calm": null, "p/danger": hot, "q/calm": null, "q/danger": dict(null)},
        "pair_variant": {
            "p/calm": {"pair": "p", "variant": "calm"}, "p/danger": {"pair": "p", "variant": "danger"},
            "q/calm": {"pair": "q", "variant": "calm"}, "q/danger": {"pair": "q", "variant": "danger"},
        },
    }
    info = analyze._field_information(design)
    assert info["pairs"] == 2
    assert info["pairs_where_field_differs"]["all_four_identical"] == 1
    assert info["pairs_where_field_differs"]["sensor_kinds_emptiness"] == 1
    assert info["pairs_where_field_differs"]["rate"] == 1
    assert info["cases_with_any_non_default_field"] == 1
