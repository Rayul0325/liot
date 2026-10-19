# liot/layers/l2_context

## Scope

Turning state into context and comfort candidates:

- `context_builder.py` — `should_snapshot` (safety change always snapshots), `ContextBuilder.build` (deterministic `structured`, pseudonymous `nl_summary` from a fixed template), `derive_occupancy`.
- `proactive.py`, `proactive_rules.py` — comfort rule loading (`config/proactive/*.yaml`), catalog-derived allow-list, `validate_proactive_candidate`, `ProactiveGenerator` (cooldown/bucket/day limits), candidate sets with NoOp.
- `proposal_lines.py` — Line A (rule/heuristic) and Line B (available-actions) deterministic fakes, merge into one `CandidateSet`.
- `evaluator.py` — `CandidateEvaluator`, the deterministic arbiter (weighted sum, NoOp baseline 0.35; safety penalty `0.5·max(0, tier−1)/2` = 0 for AMBIENT/BREATH, 0.25 DUSK, 0.5 EMBER; a rule-declared `benefit` is the candidate's effect score; air quality uses `pm25` Korean bands, good ≤15 / bad ≥36).
- `feature_vector.py` — preference cell map `time_bucket|occupancy|capability:action`, EMA (`EMA_ALPHA` 0.3, targets accepted 1.0 / dismissed 0.0 / expired·ignored 0.3) and decay (`DECAY_RATE` 0.05).
- `nl_directive.py` — keyword directive parser, `DirectiveStore` with `until` expiry, `apply_directives`.
- `event_detector.py`, `external_provider.py`, `candidate_llm.py`, `feedback_store.py`, `preference_store.py` — trigger gate, outdoor data (fake; KMA stub), LLM candidate fake, rejection snapshot, preference persistence.

Not this module's scope: safety situations, severity, abnormal detection, notification gating, persistence of proposals/judgments, risk floors (Gate1 owns them), anything that talks to a model over the network.

## Boundaries

- Imports `liot.core` and, as data only, `liot.layers.l4_execution.device_catalog`. Must not import L3, other L4 modules, db, app, or service.
- The evaluator and generators are synchronous and deterministic; async DB reads happen in the service and arrive here as snapshots (`InMemoryFeedbackStore`, preference cells).

## Invariants

- `should_snapshot` returns True whenever any safety-category entity's value changed, before checking ticks or thresholds.
- `nl_summary` is built only from `SUMMARY_TEMPLATE` (bucket, entity count, changed count, safety category names, trigger kind) — never entity ids, names, rooms, or free text. Whatever goes into this string is sent to Gate2 models.
- `structured` is deterministic for the same inputs; external data merges via `ExternalContext.to_structured()` without `fetched_at`.
- Every proactive candidate passes `validate_proactive_candidate`: (capability, action) in the catalog-derived allow-list, category not a safety category, capability equals the catalog's capability for that entity, catalog tier ≤ BREATH. `SAFETY_ACTUATOR_CAPABILITIES` (lock, valve, oven) can never be allowed by registration.
- `PROACTIVE_TIER_CEILING` is BREATH; anything above is suppressed (silence), not downgraded.
- Every `CandidateSet` contains a NoOp candidate (validator-enforced). `safety_risk` is always taken from the catalog, overriding any submitted value. Degraded external context makes the evaluator choose NoOp.
- `app.tick` fetches external context before `propose`; propose facts carry `external` (`ExternalContext.rule_facts()`) only when a provider answered, and `local_hour` only when the ruleset has an `hours:` rule. The candidate-set hash covers both, so on the shipped config (bedtime-dim uses `hours:`) the hash changes with the local hour even without a weather provider; only rulesets with neither keep the pre-2026-10-19 hash. `external:` rule predicates (pm25, cloud_cover, outdoor_temp_c; lt/le/gt/ge only) never hold when the context is missing, degraded, or the value is empty.
- A rule whose candidate key (`cap:action:entity:category`) is in the recently-rejected snapshot is skipped in `propose` before cooldown or the bucket/day budget is spent (`suppressed_by='rejected'`).
- Directives suppress only comfort candidates; safety categories and EMBER are never suppressed.
- Line confidence is telemetry only; it does not enter `final_score`.
- The derived allow-list on the shipped catalog is pinned as an exact 9-tuple set (the historical six plus `fan.set_speed`, `advice.ventilate`, `advice.close_window`; regression-tested).

## Patterns

- Category resolution always goes through `liot.core.categories.resolve_category` (map → catalog → prefix), never a local chain.
- New comfort rules use only closed comparators (`eq ne lt le gt ge`) and enum matches (`time_bucket`, `occupancy`); no expressions.
- Preference keys are stored as `"{scope}:{key}"` in `preferences`; embedding cells use scope `pref_embed`.

## Tests

- `tests/test_proactive.py`, `tests/test_candidate_generation.py`, `tests/test_proposal_lines.py`, `tests/test_evaluator.py`, `tests/test_llm_floor.py`, `tests/test_nl_directive.py`, `tests/test_preference_embedding.py`, `tests/test_event_detector.py`, `tests/test_external_provider.py`, `tests/test_external_snapshot_and_persist.py`, `tests/test_disposition.py`.
- Any new candidate source needs a test that a hostile source (LLM proposing unlock, valve.open, or a safety-category target) yields silence while the safety path still produces EMBER.
- If you change the summary template, assert it still contains no `domain.object` strings and update reproducibility expectations (snapshot hashes change).
