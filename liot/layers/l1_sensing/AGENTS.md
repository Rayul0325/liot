# liot/layers/l1_sensing

## Scope

- `ha_bridge.py` — `HABridge` over an `HAConnection` (real `WebsocketsHAConnection`, scripted `FakeHAConnection`): auth handshake, `subscribe()`, `core_state()` (`get_config`), `entity_states()` (`get_states` → `HASnapshot`: category map + current readings, restored placeholders excluded), `next_reading(timeout)` (the supervisor's reader; raises `HAConnectionClosed`/`HAMalformedMessage`), the legacy `stream()`, and `call_service` / `get_state` for the opt-in executor. Also the connection factory type `BridgeFactory`, `connect_ha(url, token)`, and the one-shot helpers `call_service_once` / `get_state_once` (open → auth → call → close, one short connection per executor call).
- `normalizer.py` — deterministic `normalize(raw)`.
- `state_cache.py` — latest reading per entity, ignores older timestamps.
- `fake_source.py`, `base.py` — deterministic test source and the `SensorSource` protocol.

Not this module's scope: deciding abnormal vs normal (core severity table), opening situations, snapshots, any judgment. `call_service` policy decisions belong to the executor.

## Boundaries

- Imports only `liot.core.contracts`. Must not import other layers, db, app, or service.
- The bridge never logs or embeds the HA token in messages or exceptions.
- Nothing in this module calls `call_service` on its own; only the executor does, and only when execution is opted in.

## Invariants

- `normalize`: bool stays bool; int/float → float (so `0` → `0.0`); strings in the true set (on, true, 1, yes, open, opened, detected, motion, present, home, wet, unlocked, active) → `True`; false set (off, false, 0, no, closed, close, clear, no_motion, absent, away, dry, locked, idle, inactive) → `False`; leading number with a unit → float; anything else → stripped lowercase string (`"unavailable"`, `"unknown"` stay strings). Changing these sets changes which safety situations open and close.
- `infer_category`: domain map first (lock, light, switch, climate, media_player→media, cover, fan, vacuum→appliance, valve→water_main), then `attributes.device_class` (gas/carbon_monoxide→gas, smoke, moisture/water→leak, door/garage_door→door, window, lock, motion/occupancy/presence→motion, temperature, humidity), else the domain prefix. Safety lives in `device_class`, not the domain: `binary_sensor.kitchen_gas` is only safe because of `device_class: gas`.
- The category map is rebuilt on every (re)connection by the supervisor (`entity_states()`), not once; `LiotService._ha_merge_categories` merges it into the known map and calls `LiotApp.set_categories`. A restored placeholder (`attributes.restored: true`) has no `device_class`, so it falls to its domain prefix — that is why it is excluded from `readings` and why the merge never lets it overwrite a known entity.
- `_request` keeps `type == "event"` messages that arrive while it waits for a result in `_pending`; `next_reading`/`stream` drain that buffer first. The supervisor subscribes **before** `get_states` — dropping buffered events (or swapping the order) loses a safety change between snapshot and subscription (test `test_event_between_snapshot_and_subscribe_is_not_lost`).
- A closed real websocket becomes `HAConnectionClosed`, a non-JSON/non-object frame or a malformed `state_changed` event becomes `HAMalformedMessage` (a skipped event is a lost safety signal; raising makes the supervisor reconnect and resync). Both are `HAConnectionError`. Every request has a 30 s timeout (`HAConnectionError`).
- `stream()` keeps its legacy contract (returns quietly on `HAConnectionError`); it does not reconnect. Reconnection is the supervisor's job (`LiotService._ha_supervisor`, decision 0014).
- `open()` wraps both `OSError` and `websockets` `WebSocketException` (handshake rejection) in `HAConnectionError`.
- `StateCache.update` rejects a reading whose `ts_utc` sorts before the cached one.

## Patterns

- Readings carry `raw_value` and `norm_value`; downstream code must use `norm_value`.
- The `HAConnection` protocol is the seam for tests: script inbound frames (callables may echo request ids) and inspect `sent`.
- `get_state` fetches all states and filters (HA has no single-entity WS query).

## Tests

- `tests/test_ha_bridge.py` covers unit flows with `FakeHAConnection`, a real websockets round-trip against a loopback mock HA server, and gas event → EMBER proposal end-to-end. `tests/test_ha_reconnect.py` (with `tests/ha_world.py`, an in-memory/loopback fake HA) covers buffering, snapshots, restored placeholders, the supervisor, notices, the executor's short connections and `serve` signal handling.
- `tests/test_disposition.py` pins that a stream of non-safety sensor changes produces zero proposals.
- When touching `normalize`, add cases for both directions of every new token and re-run the severity table tests (`0`/`0.0` normal, `"unavailable"`/`"unknown"` = hold/unmonitorable, garbled strings abnormal).
- When touching `infer_category`, assert each safety device_class still maps to its safety category.
