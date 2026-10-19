# config

## Scope

Data the daemon and the control room load at startup:

- `device_catalog.yaml` — devices (`entity_id → capability, category`) and `(capability, action[, direction]) → tier`.
- `rules/*.yaml` — Gate1 rule sets (`match` on enum facts, `set_floor`, optional `verdict: block`).
- `severity/situations.yaml` — the severity lookup table (4 damage types × 3 time-to-harm columns), safety category → (damage, time, abnormal rule) mapping, and the reserved `inactivity` row (`enabled: false`).
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

## Patterns

- To change a severity outcome, change the category's `(damage, time)` row first; change table cells only when the meaning of a cell changes for every category.
- Adding a safety category: add its row here; without a row it is CRITICAL with the truthy rule.
- Keep YAML comments in Korean like the existing files.

## Tests

- `tests/test_severity_table.py` pins all 12 cells, all 8 mapping rows, the reserved row, the default path not being under `rules/`, and each malformed-file fail-closed case. Editing a cell or row requires editing its literal expectation.
- `tests/test_gate1.py` and `tests/test_proactive.py` cover rule loading (demotion keys rejected, allow-list equivalence on the shipped catalog).
- After editing `rules/`, check `load_rulesets().ruleset_version` lists only real rule sets (currently `emergency:1.0.0+night_quiet:1.0.0`).
