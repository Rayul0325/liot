# liot/layers/l3_judgment

## Scope

- `guardrail.py` — Gate1 `Guardrail` (alias `SafetyGate`): deterministic tier floor from category baseline, matching rules' `set_floor`, safety EMBER, block → EMBER, unknown category → EMBER. Extend-only `register_category_floor`.
- `rules_loader.py` — loads and merges every YAML in `config/rules/`, rejects demotion keys and non-enum match keys, computes `ruleset_version` / `ruleset_hash`.
- `llm_judge.py` — `LLMJudge` (Gate2 composition), `LLMResult`, `LLMUnavailable`, `FakeLLM`, `build_llm_backend`.
- `decision_backend.py` — `DecisionBackend` for a llama.cpp server: two four-option questions, first-token letter probabilities, `read_option_probs`.
- `ollama_backend.py` — `OllamaBackend` (JSON generation) and the shared model payload builder.
- `jev_backend.py` — `JevBackend` for the Jev (typesafe.ai systemone) remote judge, used only by the day simulator: one POST per judgment, `read_jev_probs`.
- `prompt_registry.py` — bundle manifests (`bundle_id`).

Not this module's scope: severity table and abnormal rules (core), deciding to call the model in phase 2 or not (app/service), routing to proposals and confirmations (L4), notification.

## Boundaries

- Imports only `liot.core`. Must not import L1, L2, L4, db, app, or service.
- Every byte sent to a model goes through `emit_external(Destination.LLM, ...)`; an `EgressViolation` becomes `LLMUnavailable` (never a raw send).
- Do not delete `ollama_backend.py`; it is no longer the default but stays supported.

## Invariants

- Final tier = `escalate_only(gate1.floor_tier, model tier)`. A model tier below the floor is ignored, `gate2_verdict = "injection_suspect"`, confidence 0.
- Final severity = `escalate_severity(severity_floor, model severity)`. A model severity below the floor is ignored and also marks `injection_suspect`. `"model"` is appended to `severity_sources` only when the model is strictly higher. `severity_floor=None` means the severity question is not asked at all and severity fields stay empty.
- On `LLMUnavailable`: safety categories keep EMBER, non-safety is bumped one level with a DUSK minimum, `gate2_verdict = "degraded_fail_safe"`, severity = floor, sources = floor sources.
- `FakeLLM` and `OllamaBackend` always return `severity=None` (never raise severity).
- `DecisionBackend`: tier question always; severity question only when `severity_floor` is given; requests are `/tokenize` (`parse_special: true`) then `/completion` (`n_predict 1`, `n_probs 40`, `temperature 0`, `cache_prompt false`); only tokens exactly equal to A–D count; missing letters get `min(seen) - 2`; no letter at all, HTTP error, timeout, or malformed JSON → `LLMUnavailable`. Ties pick the earlier option.
- The model payload contains exactly `context_summary`, `time_bucket`, `candidate` (capability, action, category, pseudonymized `device`, numeric/direction-only `params`), `gate1_floor`. No resident traits, no free-form param strings.
- `JevBackend`: the `state` is exactly `EvidenceBuilder.build` (the same evidence string as `DecisionBackend`: OllamaBackend payload → `emit_external(LLM)` → `system` removed). One POST with `questions.risk` always and `questions.severity` only when `severity_floor` is given; **no retries**. `answers.<q>.probabilities` keyed "0".."3" maps to option ids in `TIER_QUESTION`/`SEVERITY_QUESTION` order; missing keys are 0, anything non-finite, negative, unknown or all-zero is `LLMUnavailable`. HTTP errors, timeouts and malformed bodies are `LLMUnavailable`, with no key in the message. The key comes in through the constructor — this layer never reads the secrets file. `model_id = "jev:<model>"`, default model `jev-latest`, per-request timeout 8 s.
- `LIOT_DECISION_TIMEOUT` is per HTTP request (default 1.5 s) and must be a positive finite number; one judgment is at most 4 requests.
- Gate1 reads only enum facts (category, capability, action, time_bucket, occupancy, source, trust); it never reads free text. Rule files cannot lower a floor (`demote`, `to_tier`, `set_ceiling`, … are load errors; `set_floor` is mandatory).
- `register_category_floor` can only raise a floor; built-in floors never change.

## Patterns

- Backends implement `judge_raw(candidate, ctx, gate1_floor, severity_floor=None) -> LLMResult`. `LLMJudge` calls the three-argument form when there is no severity floor, so older backends keep working.
- `build_llm_backend()` reads `LIOT_LLM_BACKEND`: empty/`fake` → `FakeLLM`, `ollama|gemma|home|local` → ollama, `decision` → llama.cpp, anything else → a stub that always raises `LLMUnavailable`.
- The decision prompt is ChatML with an empty `<think>` block; swapping in a model with another chat template will fail the letter readout.
- `bundle_id` is built from the registry's default judge prompt, ruleset version and backend `model_id`; backend prompt constants are not part of it — change `LIOT_DECISION_MODEL`/`model_id` when you change a backend prompt.

## Tests

- `tests/test_gate1.py`, `tests/test_llm_judge_contract.py`, `tests/test_llm_floor.py`, `tests/test_decision_backend.py` (fake transport and a loopback fake llama server; lower answers ignored, letters missing, timeout, egress violation never sends, no trait data in request body, deterministic repeat), `tests/test_ollama_backend.py`, `tests/test_ollama_transport_default.py`.
- No test may need a real model. New backend behavior is tested with an injected transport.
- For any change to composition, assert both directions per question: lower answer → floor kept + `injection_suspect`; higher answer → raised + source `model`; equal → `concur`, no `model` source.
