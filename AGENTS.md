# LIOT

LIOT is a single-home, local-first proactive smart-home judgment system (Python package `liot`, one asyncio daemon per home, SQLite, Home Assistant input, ntfy push output). It proposes one action per moment and executes only after a human approves; safety is decided deterministically, and a local decision model may only raise risk. It is a research/product project of one owner and a small team, running on a Mac Studio home server, with a later target of 8–16 GB home hubs.

## Project structure

```
liot/                                   (repository root)
├── CLAUDE.md                           ← agent entry point (Claude Code)
├── AGENTS.md                           ← agent entry point (other agents), same content
├── README.md                           ← Korean project introduction and run guide
├── MODULE_SPEC.md                      ← Korean signature reference for the core kernel
├── docs/
│   ├── architecture.md                 ← components, data flows, external systems
│   ├── business-rules.md               ← the two risk axes, situations, ladder, consent rules
│   ├── security.md                     ← protected assets, link signing, who may do what, audit
│   ├── standards.md                    ← MUST / MUST NOT rules and the verification gate
│   ├── engineering-notes.md            ← traps and non-obvious mechanisms (symptom → cause → fix)
│   ├── operations.md                   ← setup, run, environment variables, model server
│   ├── contracts.md                    ← ntfy payloads, HTTP routes, HA input, model server API
│   ├── pipeline-overview.md            ← Mermaid overview for outside readers
│   └── tracking/
│       ├── status.md                   ← built vs verified, what remains
│       ├── findings.md                 ← unresolved problems with blast radius
│       └── decisions/
│           ├── index.md                ← decision index
│           └── NNNN-<slug>.md          ← one decision record each
├── liot/
│   ├── AGENTS.md                       ← pipeline glue, daemon service, entry points, control-room server
│   ├── core/
│   │   └── AGENTS.md                   ← risk/severity types, contracts, invariants, egress gate
│   ├── db/
│   │   └── AGENTS.md                   ← schema, forward-only migrations, safety repository
│   └── layers/
│       ├── l1_sensing/
│       │   └── AGENTS.md               ← HA bridge, normalizer, state cache
│       ├── l2_context/
│       │   └── AGENTS.md               ← snapshots, proactive candidates, arbiter, personalization
│       ├── l3_judgment/
│       │   └── AGENTS.md               ← Gate1 guardrail, Gate2 judge, decision/ollama backends
│       └── l4_execution/
│           └── AGENTS.md               ← router, notifier gate, ladder, feedback, executor
├── config/
│   └── AGENTS.md                       ← device catalog, Gate1 rules, severity table, comfort rules
├── web/
│   └── AGENTS.md                       ← browser control room (pipeline lab + safety settings)
└── bench/
    └── risk/
        └── AGENTS.md                   ← Korean decision-model benchmark (outside make check)
```

## Hard gates

These are permanent. The full rule set is in `docs/standards.md`.

1. **A safety alert never goes silent or late.** An abnormal reading in a safety category produces an immediate push from the table and the Gate1 floor alone; nothing (model, severity, personalization, directive, I/O) may remove an existing gate bypass or make the first alert wait.
2. **Floors only rise.** The Gate1 tier floor and `severity_floor` (table + vulnerable resident) cannot be lowered by any model, directive, preference, or sensor confidence. The only relaxation anywhere is the confirmation of `ONE_TAP_SAFE_ACTIONS` = {(valve, close), (lock, lock)}, and their tier stays EMBER.
3. **Resident traits never leave the device and are never inferred.** They must not appear in ntfy payloads, LLM or decision-model input, logs, or any outbound path; they exist only as self-declared, consented answers in the local DB.
4. **Family is paged only with consent and a CRITICAL floor.** Guardian forwarding and 119 guidance require valid home-level consent AND `severity_floor == CRITICAL`; a model-only raise never reaches family. 119 is guidance text only — no code path may place a call or report.
5. **The system never opens the home.** `lock.unlock` and `valve.open` are never executable; any actuator call needs execution opt-in, whitelist membership, and (for safety categories) a human approval.

## Before you start

Always read `docs/standards.md`, `docs/engineering-notes.md`, and the `AGENTS.md` of every module you will touch. Then, by task:

- **Notification gate, ladder, or dispatch** (`liot/service.py`, `notifier.py`, `escalation.py`): read the gate order, bypass rules and ladder timings in `docs/business-rules.md`, and the phase-2 / drain / synchronous-send notes in `docs/engineering-notes.md`.
- **Any outbound payload or allowed key** (ntfy, guardian summary, LLM/decision evidence): read `docs/contracts.md` and the sensitive-data section of `docs/security.md`. The allowed-key set and the payload field sets are pinned by exact-set tests; changing them is a deliberate test edit.
- **A new YAML under `config/`**: read `config/AGENTS.md` first — every YAML in `config/rules/` is loaded as a Gate1 rule file.
- **Schema or persistence**: read `liot/db/AGENTS.md`; migrations are append-only and the new safety tables are not covered by the tenant scanner.
- **Decision model, prompt, or default model**: read `bench/risk/AGENTS.md` and `docs/tracking/findings.md`; labels are drafts and no default model is chosen.
- **Residents, guardians, consent, or the control room**: read `docs/security.md` (no login; `entered_by` is the only guardian-only enforcement).
- **Running tests from a git worktree**: the venv imports the main checkout unless you run `PYTHONPATH=$PWD make check VENV=/Users/rayul/liot/.venv`.

## When something goes wrong

Report to the owner immediately, before continuing, if you find or cause any of these:

- a safety alert silenced or delayed (an abnormal safety reading that does not push immediately, a first alert held behind the model or another I/O wait, a bypass removed, the HA stream dying unnoticed);
- a floor lowered (final tier or severity below its floor, or `ONE_TAP_SAFE_ACTIONS` widened or its tier lowered);
- a resident trait leaked off-device (in an ntfy payload, a model request, a log line, or the notifications table);
- a guardian paged without valid consent or without `severity_floor == CRITICAL` (including a model-only raise reaching family), or any automatic 119 call;
- execution of an opening direction (`lock.unlock`, `valve.open`) or an actuator call without approval;
- a real secret or guardian topic committed or logged.

Everything else that you cannot fix in the current task goes into `docs/tracking/findings.md` with its condition, symptom, blast radius, and why it cannot be fixed now.
