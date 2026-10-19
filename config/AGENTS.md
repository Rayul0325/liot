# config

## Scope

Data the daemon and the control room load at startup:

- `device_catalog.yaml` — devices (`entity_id → capability, category`) and `(capability, action[, direction]) → tier`.
- `rules/*.yaml` — Gate1 rule sets (`match` on enum facts, `set_floor`, optional `verdict: block`).
- `severity/situations.yaml` — the severity lookup table (4 damage types × 3 time-to-harm columns), safety category → (damage, time, abnormal rule) mapping, and the reserved `inactivity` row (`enabled: false`).
- `models.yaml` — cheap-model reason client and luna shadow settings for the day simulator only (provider, model ids, timeouts, luna mode), the cloud-mode caps (`day_cloud`: call cap 120 is provisional until Phase 0 counts call sites, $0.5 per run, $2 per day, usage file path) and the estimated price table (`prices`, USD per 1M tokens, source URL per row, `estimate: true` for secondary sources). The model ids are unverified candidates until Phase 0 lists the real models; keys never go here.
- `proactive/*.yaml` — comfort rules (`limits`, closed-comparator `state` predicates, optional `external` weather predicates over `pm25`·`cloud_cover`·`outdoor_temp_c` with lt/le/gt/ge, optional `benefit` 0..1, `propose`).

Not this module's scope: code constants (safety category set, one-tap pairs, forbidden open directions live in Python and cannot be widened from YAML), environment variables, secrets.

## Boundaries

- Every `*.yaml` / `*.yml` in `rules/` is merged into the Gate1 rule set. Never put any other kind of file there: a file without `rules:` silently joins as an empty rule set and changes `ruleset_version` (a reproducibility key); a rule without `set_floor` fails app construction.
- The severity file must stay at `config/severity/situations.yaml`; if it is missing or malformed the table fails closed (every safety category CRITICAL, thresholds gone).
- No secrets or real topics in any file here (the secret scan covers this directory).

## Invariants

- `situations.yaml` table: exactly rows `life, injury, property, inconvenience` and columns `minutes, hour, later`, values are level names (`RECORD|NOTICE|WARNING|CRITICAL`), never numbers or booleans. Current cells: life C/C/W, injury C/W/W, property W/W/N, inconvenience N/N/R.
- Current mapping: gas, smoke → life·minutes (CRITICAL); oven, high_temp → injury·hour (WARNING); leak, door → property·minutes (WARNING); water_main, lock → property·hour (WARNING). `high_temp` is abnormal `above: 55` (°C); every other row is `truthy`. Mapping rows accept only `damage`, `time`, `enabled`, `abnormal_when`.
- The cell values are an in-house adaptation of CAP 1.2, not CAP's own values; there is no multiplication anywhere.
- Gate1 rules may only raise floors. Keys such as `demote`, `downgrade`, `to_tier`, `set_tier`, `set_ceiling`, `ceiling`, `cap`, `max_tier`, `lower_to`, `reduce_to` are load errors. `match` keys are limited to `action_category, time_bucket, occupancy, source, trust, capability, action` with scalar values.
- Catalog: unknown `(capability, action)` is EMBER; `lock.unlock` and `valve.open` are EMBER; `valve.close` and `lock.lock` are BREATH in the catalog (the safety-category floor still makes their proposals EMBER).
- Comfort rules may only propose actions in the catalog-derived allow-list (catalog tier ≤ BREATH, capability not lock/valve/oven) and may not target safety-category entities.
- Shipped comfort rules (`proactive/comfort.yaml`, limits 60 min cooldown · 1 per bucket · 4 per day) start with the four plan scenes in priority order: `morning-ventilate` (morning · occupied · pm25 ≤ 15 → `advice.ventilate` on `advice.home`, BREATH), `bad-air-purifier` (occupied · pm25 ≥ 36 → `fan.set_speed` high on `fan.living_purifier`, BREATH), `bedtime-dim` (evening · local hour 21 · living light on → brightness 30, AMBIENT), `cloudy-desk-light` (day · occupied · cloud_cover ≥ 0.8 → `light.desk` on, AMBIENT). `reason_ko` templates (placeholders `{pm25}`, `{cloud_pct}`, `{outdoor_temp_c}` only) appear on the generator's decision, never in ntfy payloads.
- New devices use already-known categories so no Gate1 floor is added: `fan.living_purifier` → `climate`, `light.desk` → `light`, `advice.home` → `info`. Catalog tiers `fan.set_speed`, `advice.ventilate`, `advice.close_window` are BREATH. `info` has an AMBIENT Gate1 floor; the advice proposal reaches BREATH through the router's catalog escalation.
- `advice` is not an executable capability (`executor.NON_EXECUTABLE_CAPABILITIES`): the policy returns NOTIFY_ONLY before reading any whitelist, `effective_comfort` drops it from extensions, both executors raise `ExecutionDenied` before any call, and `RuntimeRegistry.executable_extras` excludes it.

## Patterns

- To change a severity outcome, change the category's `(damage, time)` row first; change table cells only when the meaning of a cell changes for every category.
- Adding a safety category: add its row here; without a row it is CRITICAL with the truthy rule.
- Keep YAML comments in Korean like the existing files.

## Tests

- `tests/test_severity_table.py` pins all 12 cells, all 8 mapping rows, the reserved row, the default path not being under `rules/`, and each malformed-file fail-closed case. Editing a cell or row requires editing its literal expectation.
- `tests/test_gate1.py` and `tests/test_proactive.py` cover rule loading (demotion keys rejected, allow-list equivalence on the shipped catalog).
- `tests/test_proactive_scenes.py` runs each of the four scenes through `LiotService.run_tick` (tier and gate destination) and its negative conditions; `tests/test_proactive_external.py` covers `external:`, `benefit` and the rejected-key skip.
- After editing `rules/`, check `load_rulesets().ruleset_version` lists only real rule sets (currently `emergency:1.0.0+night_quiet:1.0.0`).
