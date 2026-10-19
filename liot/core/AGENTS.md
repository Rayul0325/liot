# liot/core

## Scope

The kernel every layer depends on:

- `risk.py` — `RiskTier` (AMBIENT < BREATH < DUSK < EMBER, Korean labels, glyphs, `required_confirmation`), `escalate_only`, built-in `SAFETY_CRITICAL_CATEGORIES`, extend-only `register_safety_category` / `is_safety_category` / `safety_categories`.
- `severity.py` — `SeverityLevel` (RECORD < NOTICE < WARNING < CRITICAL, `label_ko`, `ntfy_priority`), `escalate_severity`, `SeverityTable` / `load_severity_table` (reads `config/severity/situations.yaml`), abnormal rules, source order `("table", "resident", "model")`.
- `contracts.py` — pydantic v2 models crossing layer boundaries (`SensorReading`, `CandidateAction`, `ContextSnapshot`, `Judgment`, `Proposal`, `NotificationPayload`, `GuardianNotificationPayload`, candidate-set models), `compute_snapshot_hash`.
- `invariants.py` — exception hierarchy, `assert_monotonic`, `assert_executor_confined`, `assert_execution_gated`.
- `egress_gate.py` — `emit_external`, `NTFY_ALLOWED_KEYS`, PII patterns, `AliasMap` pseudonymization, LLM entity-shaped backstop.
- `event_bus.py` (`ALLOWED_EDGES`), `scoped_repo.py` (tenant scoping), `clock.py` (UTC clock, time buckets), `categories.py` (`resolve_category`), `env.py` (`env_flag`).

Not this module's scope: notification gating, ladder timing, judging, routing, SQL for the v3 safety tables, HTTP, config file contents.

## Boundaries

- No runtime import of `liot.layers`, `liot.db`, `liot.app`, `liot.service`. The only exception is `assert_execution_gated`, which lazily imports `liot.layers.l4_execution` to check it statically; `categories.py` imports the catalog type only under `TYPE_CHECKING`.
- No network and no DB access (except `ScopedRepo` / `assert_no_null_tenants`, which receive a connection).

## Invariants

- `escalate_only(a, b) == max(a, b)` and `escalate_severity(a, b) == max(a, b)` for every input pair; neither ever returns a lower value.
- `SAFETY_CRITICAL_CATEGORIES` stays a frozenset of exactly gas, smoke, door, leak, high_temp, lock, water_main, oven. Runtime registration only adds to `_SAFETY_EXTENSIONS`; there is no removal API (tests reset it via `_reset_safety_extensions_for_tests`).
- `SeverityTable.compute`: non-safety → `None`; failed table or unmapped safety category → CRITICAL; vulnerable resident raises exactly one level, capped at CRITICAL; sources contain `"table"` always and `"resident"` only when the level actually rose.
- `is_unmonitorable(norm)` is true exactly for the strings `"unavailable"`/`"unknown"` (HA cannot read the sensor); `is_abnormal` returns False for them in every rule, including the fail-closed table, and callers must check `unmonitorable` before treating `abnormal=False` as normal.
- `AbnormalRule.is_abnormal` (truthy): normal only for `False` or a non-bool number equal to 0 (the normalizer turns int 0 into 0.0); everything else, including strings, `None`, NaN, is abnormal. (above): normal only when `value <= threshold` holds — NaN and bools are abnormal.
- `load_severity_table` never raises; any failure returns `SeverityTable.failed(...)` with `load_failed` true and logs a warning.
- Every contract model keeps `ConfigDict(extra="forbid")`. `NotificationPayload` and `GuardianNotificationPayload` have no device, entity_id, room, rationale, or trait fields. New `Judgment`/`Proposal` fields are optional with `None`/empty defaults.
- `NTFY_ALLOWED_KEYS` is exactly: channel, proposal_id, risk_tier, glyph, label, signed_deeplink, topic, severity_label, situation_label, home_alias, at.
- `emit_external` rejects unknown destinations, non-dict payloads, any PII pattern left after sanitizing (except inside `signed_deeplink`), and for LLM any `word.word` entity-shaped string. It never sends anything itself.
- `ALLOWED_EDGES` is exactly {L1→L2, L2→L3, L3→L4, L4→L2}.
- `ScopedRepo` rejects `None`/blank home ids and only accepts tables in `_TENANT_TABLES` (v1/v2 tables; the v3 safety tables are not listed).
- `assert_execution_gated` checks: default policy disabled; no safety category is AUTO at any tier; open directions absent from every whitelist even with injected extras; A/B policies never weaker than tier baseline; `ONE_TAP_SAFE_ACTIONS` is a frozenset subset of `SAFETY_EXECUTABLE`; outside it no safety action is single-confirm; the router gives EMBER+light for the one-tap pairs and EMBER+double for `alert` under both conditions.

## Patterns

- Combine tiers/severities only through `escalate_only` / `escalate_severity`; normalize inputs with `RiskTier.from_str` / `SeverityLevel.from_str`.
- Category checks go through `is_safety_category` (built-in ∪ runtime), never `in SAFETY_CRITICAL_CATEGORIES`.
- Severity config values are read only from the YAML; do not mirror table cells as Python constants.
- `device` and other person/name-like keys are pseudonymized as `PERSON_n`, room-like keys as `ROOM_A…`; the alias token names are part of what models see.
- `Clock.time_bucket` uses the UTC hour (06–11 morning, 11–17 day, 17–22 evening, else night).

## Tests

- `tests/test_invariants.py`, `tests/test_invariant_checkers.py`, `tests/test_ci_invariant_name.py` (CI imports `assert_executor_confined` / `assert_execution_gated` by name).
- `tests/test_severity_level.py`, `tests/test_severity_table.py` pin every table cell, every mapping row, the +1/cap/no-stacking rule, unknown and runtime categories, each malformed-config fail-closed case, and boundary values (55.0 normal, 55.1 abnormal, 0/0.0 normal, `"unavailable"` abnormal).
- `tests/test_contracts_severity_fields.py`, `tests/test_contracts_new_types.py`, `tests/test_egress.py`, `tests/test_notifier_severity.py` pin exact field and key sets — adding a key means editing those expected sets on purpose.
- When you touch an escalate or abnormal rule, mutate it (flip the comparison, drop the cap) and confirm exactly its dedicated test fails.
