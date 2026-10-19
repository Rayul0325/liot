# liot (top-level modules)

## Scope

The files directly in `liot/`:

- `app.py` — `LiotApp`, the brain. Wires L1→L2→L3→L4 on the in-process bus, turns one reading or one tick into at most one `Proposal`. No network, no DB.
- `service.py` — `LiotService`, the body. Persistence, notification dispatch, situation episodes, table-first two-phase dispatch, escalation ladder loop, feedback HTTP receiver, nonce lifecycle, expiry sweep, personalization signals, judgment storage.
- `serve.py` — `liot-serve` entry point; builds everything from environment variables. Owns `DEFAULT_DB_PATH` / `resolve_db_path`.
- `demo_server.py` — control-room HTTP server: the pipeline lab (`/api/simulate`, `/api/registry/*`, `/api/feedback`, `/api/directive`, `/api/catalog`) and safety settings (`/api/safety/*`, shared daemon DB), and the day simulation (`/api/day/*`, temp DB per run).
- `registry.py` — `RuntimeRegistry`, in-memory runtime registration used only by the lab.
- `secrets_env.py` — reads `~/.config/liot/secrets.env` (`KEY=value` lines only) for the simulator's real-model keys; refuses files wider than 600 or owned by someone else; exposes presence by name, never values. The daemon does not read it.
- `cheap_llm.py` — `CheapLLM`, the simulator's one-line suggestion reason from a cheap model (Anthropic Messages, Gemini generateContent, OpenAI Responses over urllib; one POST, no retry). Input is sanitized (entity-shaped words → Korean names, paths and file names dropped, floats to 1 decimal, `name` keys dropped) and then passes `emit_external(Destination.LLM)`; a trait word or an egress refusal means nothing is sent. Output must be one line, ≤ 80 chars, with Hangul and no trait word, else the caller's fallback is returned with `source="fallback"`. Model ids come from `config/models.yaml` and are unverified until Phase 0. Not wired anywhere yet.
- `luna_shadow.py` — `LunaShadow`, a record-only comparison next to the Jev judgment (never changes it, never raises). Modes from `config/models.yaml`: `decision_api` (always unavailable until Phase 0 confirms an endpoint — no request shape is invented), `logprobs` (Chat Completions, `top_logprobs: 20`, the decision backend's prompt and `read_option_probs`), `unavailable`. Not wired anywhere yet.
- `memory_client.py` — memgineering CLI wrapper for the day simulator's demo brain only: `subprocess` with `MEMGINEERING_HOME` (absolute demo home) and `MEMGINEERING_NO_UPDATE=1`, 10 s timeout, `--json`, verbs recall/remember/use/whoami only. `check_ready` enables memory only when `use --json` is the signed-in hosted `liot-demo`; nothing is sent before it. Recall always carries `--scope`; the house (`house-<run_id>`) can never read `resident-*`, and cards from another scope are dropped. Remember text, reason and recall queries pass the trait-word check and `emit_external(Destination.MEMORY)`. Cards keep title + one-line summary only. Failures continue without memory.
- `live_runs.py` — generic run manager: one background worker at a time (409 otherwise), stored events with `seq`, SSE replay (`from`, `Last-Event-ID`), keepalive, stop flag, last 3 runs.
- `live_graph.py` — stage-by-stage runner for one situation through the real functions (prod order and bench order); used by the bench runner (`build_one`, `TRAIT_WORDS`).
- `day_sim.py` — simulated days on a real `LiotService` over a temp SQLite file (KST clock, fake transport, FakeLLM, FakeExternalProvider); persona and viewer taps go through `_route_feedback` with real signed links; `DayRuns` backs `/api/day/*`. `models: "cloud"` (request key; default `fake`, whose events and summary are byte-identical to before) swaps in the `CloudKit`: `_RecordingJudge` picks Jev or FakeLLM per call (its `model_id` is the one it picked), `phase2_deadline_s=12`, the reason writer (house recall → cheap model, rule sentence on failure) at `_record_proposal`, the resident decider (resident recall → cheap model `choose`, persona table on an odd answer) at `_persona_reaction`, house remember on taps/expiry (`[그만해]` with `--rule`), luna shadow at step end. In cloud mode taps and the 22:00 tray review run after the step's readings, safety sensors are applied first, and every external call goes through `_ext` (meter slot → `to_thread` + `wait_for` → usage). `GET /api/day/keys` returns key names and presence only.
- `day_cloud.py` — the day simulator's cloud parts: `UsageMeter` (per-run call cap, per-run and per-day estimated cost caps from `config/models.yaml` `day_cloud`; daily totals in `~/.config/liot/usage.json`; reserves a slot before each call; trips — and stays tripped — on any cap or an unreadable/unwritable usage file), and `CloudKit`/`build_cloud_kit` (Jev judge, cheap model, luna shadow, memory client; a part whose key is missing stays fake and says why in `status`, names only).
- `demo.py` — CLI demo.

Not this module's scope: risk/severity types, contracts, egress, invariant checkers (`liot/core`); schema and SQL (`liot/db`); gate, judge, router, notifier, ladder policy, executor logic (`liot/layers/*`); browser code (`web/`); config data (`config/`).

## Boundaries

- `app.py` must not import `liot.db`, `liot.service`, or do I/O. Anything that needs the DB (residents, episodes, ladders) is passed in by the service as plain values (`vulnerable_resident: bool`, `situation_id`, `SeverityResult`).
- `service.py` must not import `demo_server` or `registry`.
- `day_sim.py` never opens the daemon DB, never sends real notifications, and applies viewer taps only on its own thread at a step boundary (the HTTP thread only enqueues).
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
