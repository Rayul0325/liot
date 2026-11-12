# liot/db

## Scope

- `schema.sql` — version-1 baseline schema (preferences, sensor_readings, context_snapshots, judgments, proposals, feedback, notifications, audit_log, llm_cache, trust_state).
- `migrations.py` — forward-only `_MIGRATIONS` list; `LATEST_VERSION` is 4. Version 2 adds `candidate_evaluations`; version 4 adds the nullable `escalation_ladders.anchor_at` (NULL = ladder timing anchored to `situations.opened_at`, exactly the old behaviour; set only by restart recovery, decision 0017); version 3 adds residents, resident_traits, home_settings, guardians, consent_events (append-only triggers), situations (one open episode per home/category/entity via partial unique index), escalation_ladders, guardian_forwards, and severity/situation columns on judgments and proposals.
- `database.py` — `connect` (WAL, foreign keys, synchronous FULL when durable) and `init_db` (connect + migrate).
- `safety_repo.py` — `SafetyRepo`: residents and traits, home settings, guardians, consent, situations, ladders, forwards, and `save_judgment` / `get_judgment`.

Not this module's scope: proposal persistence and feedback state transitions (`liot/layers/l4_execution/proposal_service.py`), preference storage (`liot/layers/l2_context/preference_store.py`), deciding when a situation opens or a guardian is paged (service), any network or logging of values.

## Boundaries

- Imports only `liot.core`. Must not import layers, app, or service.
- `safety_repo.py` has no logging and no outbound path. Do not add `logger` calls that format rows, ids, or topics.
- Do not edit `schema.sql`; change the schema only by appending a new `(version, note, fn)` entry.

## Invariants

- Migrations are idempotent: applying from empty to latest, and re-applying v2 → v3 twice, both succeed and leave old rows' new columns `NULL`. Never drop or rename a column.
- Every tenant table has `home_id TEXT NOT NULL DEFAULT 'singleton'`; v3 tables also have `CHECK (TRIM(home_id) <> '')`.
- `resident_traits.trait` ∈ {age_75_plus, cannot_self_evacuate, cognitive_decline}; `value` ∈ {0, 1}; `cognitive_decline` rows must have `entered_by = 'guardian'` (DB CHECK and `set_trait` both enforce it). `lives_alone` lives only in `home_settings` and is always entered by `resident`. There is no column for birth dates or diagnoses.
- `set_trait` / `set_lives_alone` accept only real `bool` values (an int such as a birth year is rejected).
- `delete_resident` deletes traits then the resident with `PRAGMA secure_delete = ON`, restores the previous setting, and runs `wal_checkpoint(TRUNCATE)`. Consent events keep the anonymous `by_resident_id`.
- `consent_events` rejects UPDATE and DELETE (triggers). Valid consent = latest event for (home, guardian) is `grant` and the guardian is active. Grant and revoke require `by_resident_id` to be a resident of that home (`PermissionError`); granting to an inactive guardian is a `ValueError`; revoking is always allowed.
- `list_open_situations(home_id)` returns the home's open episodes (oldest first); the service uses it at `start()` to recover situations that have no ladder.
- `open_or_get_situation` returns `(id, opened_new)`; when an episode is already open it raises `severity_floor` with `MAX(...)` and never lowers it.
- `Guardian.topic` is `repr=False`; topics are generated with `secrets.token_urlsafe(32)` and returned only by `add_guardian`.
- `record_forward`: `guidance_119` rows have `guardian_id = NULL`; `forward` / `resolved` require a guardian of that home.
- `Ladder.anchor_at` is written once by `create_ladder(..., anchor_at=...)` and never by `update_ladder`; `None` means "anchored to `opened_at`". Old rows read as `None`.
- `update_ladder` accepts only the whitelisted field names; unknown fields are `ValueError`, unknown ladders `LookupError`.

## Patterns

- Errors: invalid value → `ValueError`, missing target → `LookupError`, not a resident of this home → `PermissionError`. Callers map these to HTTP 400/404/403.
- Every write method commits before returning.
- Lists and dicts are stored as JSON text (`notified_guardian_ids`, `severity_sources`, `model_probs`); severities and tiers as integers.
- The v3 tables are not in `ScopedRepo`'s tenant list. Queries written here must include `home_id = ?` themselves where a home is known; lookups by `situation_id`, `resident_id`, and `list_open_ladders()` are home-agnostic, so callers compare the returned `home_id`.

## Tests

- `tests/test_migration_v3.py` (empty → latest, v2 → v3 twice, monotonic versions), `tests/test_safety_repo.py` (trait rules, purge on delete, consent by A / revoke by B, append-only history, one open episode, ladders surviving restart, forward validation, judgment round-trip), `tests/test_proposal_severity_persist.py`.
- A new table needs: idempotency test, CHECK on `home_id`, and a test that no secret or trait value can be read back through a list or repr path.
- For deletion behavior, open the DB file in a new connection after delete and assert the trait rows are gone.
