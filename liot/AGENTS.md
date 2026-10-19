# liot (top-level modules)

## Scope

The files directly in `liot/`:

- `app.py` — `LiotApp`, the brain. Wires L1→L2→L3→L4 on the in-process bus, turns one reading or one tick into at most one `Proposal`. No network, no DB.
- `service.py` — `LiotService`, the body. Persistence, notification dispatch, situation episodes, table-first two-phase dispatch, escalation ladder loop, feedback HTTP receiver, nonce lifecycle, expiry sweep, personalization signals, judgment storage.
- `serve.py` — `liot-serve` entry point; builds everything from environment variables. Owns `DEFAULT_DB_PATH` / `resolve_db_path`.
- `demo_server.py` — control-room HTTP server: the pipeline lab (`/api/simulate`, `/api/registry/*`, `/api/feedback`, `/api/directive`, `/api/catalog`) and safety settings (`/api/safety/*`, shared daemon DB).
- `registry.py` — `RuntimeRegistry`, in-memory runtime registration used only by the lab.
- `demo.py` — CLI demo.

Not this module's scope: risk/severity types, contracts, egress, invariant checkers (`liot/core`); schema and SQL (`liot/db`); gate, judge, router, notifier, ladder policy, executor logic (`liot/layers/*`); browser code (`web/`); config data (`config/`).

## Boundaries

- `app.py` must not import `liot.db`, `liot.service`, or do I/O. Anything that needs the DB (residents, episodes, ladders) is passed in by the service as plain values (`vulnerable_resident: bool`, `situation_id`, `SeverityResult`).
- `service.py` must not import `demo_server` or `registry`.
- `demo_server.py` must not start or talk to the daemon; it shares state only through the SQLite file. Lab state (`_REGISTRY`, `_PREF_CELLS`, `_DIRECTIVES`, runtime safety extensions) is process memory and never reaches the daemon.
- Outbound sends happen only in `service.py` (`transport.send`) and only with dicts returned by `Notifier.sanitize` / `notify` / `sanitize_summary` (egress-gated).

## Invariants

- A new abnormal safety episode dispatches its phase-1 proposal before any model call. `_on_abnormal` must not `await` phase 2; it calls `_spawn_phase2` and returns.
- Phase-1 judgments carry `model_id == PENDING_MODEL_ID` ("pending-decision-model"), `gate2_verdict == "pending"`, `degraded is False`; phase-2 rows share `situation_id` and `snapshot_hash`. Every phase-2 outcome (raised, concur, injection_suspect, timeout, exception) is saved via `safety.save_judgment`.
- A follow-up proposal is dispatched only if `_raised(first, followup)` and the situation is still the same open episode.
- If phase-1 fails after `open_or_get_situation` opened a new episode, the episode is closed (`close_situation`) — otherwise later abnormal readings would be swallowed as "continuing".
- A normal reading never creates an `alert`; it closes the episode, expires that situation's pending proposals and drops their nonces, sends the resident resolved notice, then `_on_situation_closed` (cancel ladder, resolved to still-consented forwarded guardians).
- Guardian forwarding and 119 guidance read `SituationInfo.severity_floor` (situations table), never the final severity. `_forward_to_guardians` and the guardian side of `_send_guidance_119` re-read `consented_guardians()` at send time.
- Re-alerts reuse the live nonce (`self._nonce.get(pid) or self._issue_nonce(pid)`); issuing a new one would kill the buttons of earlier alerts.
- `_awaiting_second` is set before `_request_second_confirmation` issues the second link and cleared on send failure; execution (2/2) requires it. `_drop_nonce` removes nonce, issue time and both marker sets together.
- The feedback receiver refuses to open with the dev signing key unless `LIOT_FEEDBACK_ALLOW_DEV_KEY` is set; the guard and the open call stay inside `open_feedback_endpoint`.
- `/api/safety/*` passes `safety_block_reason` (loopback client, local Host, local Origin and JSON for POST) before any DB access; guardian views never include `topic`; `POST /api/safety/guardians` is the only response that returns it.
- `drain_phase2()` runs before the DB is closed (`run()`'s finally does it with deadline + 1 s).

## Patterns

- App paths: `handle_reading` → `classify_safety` → `_on_abnormal` / `_on_normal`; non-safety → `process_reading` then `_save_last_judgment` then `dispatch`. Tick: `run_tick` does expire → expired signals → nonce sweep → feedback snapshot → pref cells → `app.tick()` → save judgment → record evaluation → dispatch.
- Ladder: pure timing in `escalation`; `_fire_ladder` sends one catch-up re-alert, then forward, then 119, then `_plan_next`. `_ladder_still_open` is re-checked right before the forward step and before the 119 step, so an ack that lands mid-fire stops the next stage.
- Lab responses are built from a snapshot taken under `STATE_LOCK`; the pipeline runs outside the lock. Safety API calls run under `SAFETY_DB_LOCK` with a fresh `init_db` per request.
- Error bodies of `/api/safety/*` are one Korean line; never put exception text in them.

## Tests

- Situation flow: `tests/test_pipeline_situations.py`; ladder, guardians, ack, restart re-arm: `tests/test_escalation_ladder.py`; feedback links, nonces, second confirmation: `tests/test_service.py`, `tests/test_execution_wiring.py`; dev-key gate: `tests/test_feedback_exposure.py`; safety API and host guard: `tests/test_demo_server_safety.py`.
- Always `await svc.drain_phase2()` before asserting phase-2 effects and before `db.close()`. Never sleep.
- Move time with a fixed `Clock` (`clock.set_fixed(...)` then `svc.ladder_tick()`); assert both sides of every boundary (89 s vs 90 s, 5 min).
- For any change to dispatch or gating, include a case where the model backend blocks forever and assert the first alert was still sent.
- Assert payloads carry no device/room/rationale/trait keys and that guardian topics never appear in `caplog`.
