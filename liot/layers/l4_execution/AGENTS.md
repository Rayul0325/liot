# liot/layers/l4_execution

## Scope

- `risk_router.py` — `RiskRouter`: `Judgment` → `Proposal` (catalog may only raise the tier; confirmation from the intervention policy; the one-tap exception; copies `severity` and `situation_id`).
- `intervention.py` — condition A (`UniformIntervention`) / B (`TieredIntervention`), `CONFIRMATION_ORDER`.
- `notifier.py` — `gate_pipeline` (fixed gate order), payload building, link signing (`sign_deeplink`, `sign_feedback`, `sign_guardian_ack`, `guardian_ack_link`, `tier_int`), egress sanitizing.
- `ntfy_transport.py` — `HTTPNtfyTransport` (priority, tags, action buttons), `FakeNtfyTransport`, `build_ntfy_transport`.
- `escalation.py` — pure ladder timing (re-alert intervals, guardian forward at 90 s, 119 guidance +5 min, retry 30 s, resolution 5 s), situation labels, read-only queries.
- `feedback_gateway.py` — signature verification (`verify_feedback_sig`, `verify_guardian_ack_sig`, `compare_token`) and feedback application.
- `proposal_service.py` — proposal persistence, expiry (`expire_due`, `expire_situation`), feedback state map (including `ok` = no state change), rejected keys, accept rate, audit rows.
- `executor.py` — `ExecutionPolicy`, `HAExecutor`, `FakeExecutor`, `COMFORT_EXECUTABLE`, `SAFETY_EXECUTABLE`, `FORBIDDEN_OPEN`, `ONE_TAP_SAFE_ACTIONS`.
- `device_catalog.py` — `(capability, action, direction) → tier`, default-deny EMBER.

Not this module's scope: deciding when a situation opens or closes, running the ladder loop and sending (service), severity computation (core), judging (L3).

## Boundaries

- Imports only `liot.core` (plus siblings). Must not import L1–L3, db, app, or service.
- `notifier`, `escalation`, and `proposal_service` never perform network I/O; only `ntfy_transport` and `HAExecutor` do, and transports accept only already-sanitized dicts.
- Concrete classes with `Executor` in the name exist only in `executor.py`.

## Invariants

- Gate order is fixed: charter → mute → recently rejected (30 days) → dedup (2 h) → quiet hours (night, below EMBER) → ambient digest → budget. Charter OFF suppresses everything, including CRITICAL. Mute and recently-rejected are bypassed only when `proposal.severity == CRITICAL`. Steps 4–7 are bypassed (IMMEDIATE) when `perishable or tier == EMBER or severity == CRITICAL`. With `severity=None` the result is identical to the pre-severity gate.
- ntfy priority: from `severity_label` if present (CRITICAL 5, WARNING 4, NOTICE 3, RECORD 2), else from tier (0→2, 1→3, 2→4, 3→5).
- `ONE_TAP_SAFE_ACTIONS == frozenset({("valve","close"), ("lock","lock")})`, a subset of `SAFETY_EXECUTABLE`, distinct object. The router gives these EMBER + `light`; every other EMBER proposal (including `alert`) gets `double` under both conditions. No intervention policy may return a confirmation weaker than the baseline (`InvariantError`).
- `ExecutionPolicy`: disabled → NOTIFY_ONLY always. Safety category: never AUTO; outside `SAFETY_EXECUTABLE` → NOTIFY_ONLY; one-tap pair → CONFIRM; else DOUBLE_CONFIRM. Comfort: ≤BREATH AUTO, DUSK CONFIRM, EMBER DOUBLE_CONFIRM. `effective_comfort` always subtracts `FORBIDDEN_OPEN` (`lock.unlock`, `valve.open`).
- Signing: feedback = HMAC of `pid:tier_int:action:nonce`; deeplink = HMAC of `pid:tier_int` with nonce as carried data; guardian ack = HMAC of `guardian-ack:situation_id:guardian_id`. Signer and verifier use the same `tier_int`. `compare_token` treats non-ASCII as mismatch.
- `guardian_path(floor)` is true only for `floor == CRITICAL`; re-alert interval and cap come from the final severity; `realert_allowed` respects charter for CRITICAL and charter + mute for WARNING, and never looks at dedup, rejection, quiet hours or budget.
- Summary payloads (forward, resolved, 119) carry only channel, situation_label, home_alias, at, severity_label and, for guardian forward/119, the ack link moved into `signed_deeplink`. Summary notifications have no `Click` header.
- `expire_situation` touches only `pending` proposals of that situation. `FEEDBACK_STATE_MAP["ok"] is None`.
- No code path may call `lock.unlock` or `valve.open`. **Known violation, unfixed:** `HAExecutor.undo` maps `close → open` and `lock → unlock` (`_INVERSE_ACTION`) without re-checking `FORBIDDEN_OPEN`. The service stores a prior state for every successful execution, including an approved one-tap `valve.close` / `lock.lock`, so a correctly signed `a=undo` request then calls HA `valve.open` / `lock.unlock` (reproduced 2026-10-19 with execution enabled). Undo links are not sent in notifications today; do not add them, and do not extend undo, until undo refuses any inverse in `FORBIDDEN_OPEN` (or refuses safety categories entirely) with a test.

## Patterns

- Adding a payload key: contract model → `NTFY_ALLOWED_KEYS` → transport headers/body → the exact-set tests, in that order.
- Ladder numbers are module constants in `escalation.py`; change them there only, with boundary tests on both sides.
- `HTTPNtfyTransport.send` is synchronous and blocks the caller's event loop for up to its timeout.

## Tests

- `tests/test_notifier_severity.py` (gate matrix, priorities, exact allowed-key set), `tests/test_one_tap_safe_actions.py` (one test per pair, A/B, mutation-safe literals), `tests/test_risk_router.py`, `tests/test_intervention_ab.py`, `tests/test_executor.py`, `tests/test_execution_wiring.py`, `tests/test_router_repo_guards.py`, `tests/test_escalation_ladder.py` (`test_schedule_edges` for pure timing), `tests/test_device_catalog.py`.
- New gate conditions need: a CRITICAL case, a WARNING/EMBER case, and a `severity=None` regression case.
- Mutation check: removing a pair from `ONE_TAP_SAFE_ACTIONS` or a bypass term from the gate must fail exactly that guard's test.
