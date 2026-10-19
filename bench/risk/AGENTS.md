# bench/risk

## Scope

Korean benchmark for choosing the Gate2 decision model:

- `cases.yaml` — 61 inputs: comfort singles (9), comfort pairs (20), safety situation pairs calm/danger (16), injection cases (16: lower / no_raise / raise).
- `labels.yaml` — gold labels for tier and severity, `status: draft`, `reviewed_by: null`.
- `models.yaml` — candidate GGUF files (in `~/models/judge/`, never in git), size, quant, `small_hub`, `format_fit` against each model card.
- `run_bench.py` — `check` (validate cases/labels, print floors), `run --model <key>` (starts `llama-server` on a free port from 18770, measures, appends per case), `report` (rebuilds `results.md`).
- `raw/<model>.jsonl`, `raw/<model>.sessions.jsonl` — per-case records and server sessions; `results.md` — generated report; `results_notes.md` — hand-written notes appended to the report.

Not this module's scope: production configuration. The benchmark only recommends; it never changes `LIOT_DECISION_MODEL` defaults or any code under `liot/`.

## Boundaries

- Outside `make check`: pytest collects only `tests/`, ruff and mypy only `liot`, `tests`, `scripts`. Run `run_bench.py check` yourself after edits.
- Reuses the production path (Guardrail → severity table without resident traits → `DecisionBackend` → `LLMJudge`) by importing `liot`; it must not reimplement prompts or readout.
- Model files and server logs stay in `~/models/judge/` (logs in `~/models/judge/logs/`).

## Invariants

- No case text contains resident-trait words; the runner checks forbidden words and that model evidence has exactly the four production keys.
- Pair cases differ only in free text; both variants must have the same floors (the runner checks).
- Every case id in `cases.yaml` has a label in `labels.yaml` and vice versa.
- Labels stay `status: draft` until the owner reviews them; while draft, results are not a model choice and must be presented as draft.
- Selection rule (tunable defaults): required = identical probabilities over 2 runs, zero readout errors, both-question p95 < 1.0 s; then highest "raised when it should" with over-raise ≤ 10%; also mark the best ≤ 4B model for small hubs.
- `run` writes each case result immediately and resumes from existing records; delete or move `raw/<model>.jsonl` to re-measure.

## Patterns

- Cases set `context` as the snapshot's `nl_summary`. In production the summary is an aggregate template without free text, so raise-rates here measure a signal the daemon does not currently send — keep that caveat in any recommendation.
- Measure latency on a quiet machine; record `uptime` load. The 2026-10-19 run had load ≈ 10 and bimodal prompt-eval throughput, so its latency verdicts are not reliable.
- When a model's chat template is not ChatML (`format_fit: no`), its numbers describe the backbone under this prompt, not the model's own classifier.

## Tests

- `PYTHONPATH=. .venv/bin/python bench/risk/run_bench.py check` must succeed after any edit to cases, labels, or models.
- After relabeling, run `check` then `report` (model outputs are already in `raw/`).
