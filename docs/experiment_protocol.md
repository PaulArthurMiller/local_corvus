# Local Corvus — Three-Condition Experiment Protocol

This document is the standard for constructing and executing a three-condition
experiment (`learned` / `static_weight` / `random_weight`) on Local Corvus.

**As of M19–M27, experiments are not run by hand.** A **Run Plan** (JSON) fully
describes one experiment; `scripts/run_experiment_harness.py` executes it
end-to-end — per-condition backend restart, graph wipe+seed, the full turn
loop (bulk chat turns + probe batteries), judging, and series/experiment
report generation — with zero manual API calls. A **Queue** (JSON) feeds
several Run Plans through the harness back-to-back, unattended, via
`scripts/run_experiment_queue.py`. This is the required path for a full long
series run (a 10–15-run screening series or larger): hand-driving `/chat` and
`/feedback` calls for hundreds of turns is not supported by this protocol and
should not be done for a real experiment.

The bulk of this document is a field-by-field reference for the Run Plan and
Queue schemas, so that every plan written for an automated run includes the
correct variables with the correct types. Keep this section in sync with
`scripts/harness/schema.py` (Run Plan) and `scripts/harness/queue.py` (Queue)
whenever those modules change — they are the source of truth; this document
is a readable mirror of them.

---

## Architecture — three layers

```
┌────────────────────────────────────────────────────────────────────┐
│  QUEUE LAYER  (queues/*.json + run_experiment_queue.py)             │
│  An ordered list of Run Plan files. No experimental intelligence — │
│  never reads results, never edits plans. Required for any run of   │
│  more than one plan.                                                │
└───────────────────────────┬───────────────────────────────────────┘
                             ▼  one plan at a time
┌────────────────────────────────────────────────────────────────────┐
│  HARNESS LAYER  (plans/*.json + run_experiment_harness.py)          │
│  execute_run(plan): per-condition container restart (fresh          │
│  EXPERIMENT_ID/CONDITION_LABEL/RUN_CONDITION) → wipe+seed graph →   │
│  turn loop (bulk /chat+feedback, probe /admin/run-probe) → judge →  │
│  series report at series close → experiment report at experiment    │
│  close → notification.                                              │
└───────────────────────────┬───────────────────────────────────────┘
                             ▼
        Docker backend (config.yaml hyperparameters) ── graph DB ── Ollama
```

`config.yaml` and the Run Plan are **not the same thing** and are not
interchangeable:

| | Lives in | Scope | Changing it |
|---|---|---|---|
| Hyperparameters (retrieval K1/K2, learning rate, edge priors, context budget, …) | `config.yaml` | Applies to every run until changed again | Requires a backend restart |
| Experimental design (how many turns, which conditions, which judge, probe ratio, budget, …) | Run Plan JSON | One experiment | New plan file, no backend restart needed |
| Batch scheduling (which plans, in what order, failure handling) | Queue JSON | One unattended session | New queue file |

**As of M43 Phase 2, a hyperparameter sweep is expressed per-plan, not by
hand-editing `config.yaml` between runs.** Two Run Plan fields carry it:

- **`config_overrides`** (dict[dotted-key, value]) rides the harness's
  existing per-condition backend restart as the `CONFIG_OVERRIDES_JSON` env
  var. The backend merges it onto `config.yaml` at import time, before any
  derived constant (`_CONFIG_HASH`, `EDGE_UPDATE_CFG`, …) is computed. An
  unknown dotted key — a typo, or a key that assumes a `config.yaml` shape
  that has since changed — is a **hard fail at backend startup**, not a
  silent no-op; a typo can never look like it took effect. For a **queue**
  run this same check also runs offline at preflight (see "Queue preflight"
  below) — a bad key is a `--dry-run` hard stop, not a crash hours into an
  unattended session at that plan's own restart.
- **`expected_config`** (same dotted-key shape) is checked against
  `GET /admin/effective-config` before any graph mutation (`runner.py`
  preflight, `queue.py` step 5). A mismatch — including a declared key
  entirely absent from the live config — is an unconditional hard stop, not
  subject to `error_policy` override. It is deliberately independent of
  `config_overrides`, not auto-derived from it: a plan can assert without
  supplying (documenting an assumed baseline), or supply without asserting.
  §1-3 of `phase_one_experiment_program.md` requires the two to carry
  identical key sets for any pre-registered R3 variation, so the assertion
  doubles as proof the injection actually took effect.

Both fields participate in `plan_config_hash` (`compute_config_hash()`) — a
plan's config state is part of its identity, not external to it. A plan with
neither field runs against whatever `config.yaml` is committed on disk,
exactly as before M43.

`tuning_knobs` (dict[string, float]) is a **legacy passthrough field,
retired in favor of `config_overrides`/`expected_config` above.** It is not
read by the backend and must not be used to vary hyperparameters per-plan;
it is kept only for schema backward-compatibility with plan files written
before M43.

See "Sweepable hyperparameter knobs" below for the specific knobs
pre-registered for the current sweep round, and their `config_overrides`
values.

---

## Prerequisites

- Docker Desktop running with Linux containers enabled
- Ollama running natively on Windows (not in Docker) with `gemma4:e4b` pulled
- `.env` file in the project root (see `docker-compose.yml` for the variables
  it reads: `OLLAMA_HOST`, `CONDITION_LABEL`, `EXPERIMENT_ID`,
  `RUN_CONDITION` — the harness sets the latter three itself per condition;
  leave them unset in `.env` for harness-driven runs)

```
OLLAMA_HOST=http://host.docker.internal:11434
```

Start the stack:

```
docker-compose up --build
```

Verify the backend is healthy:

```
GET http://localhost:8000/health
→ {"status":"ok","db":"ok"}
```

Run the full unit test suite (outside Docker, on host) and confirm it passes
in full before launching a real experiment:

```powershell
python -m pytest tests/ --ignore=tests/integration
```

The seed fixture and prompt bank referenced by a plan must already exist on
disk (see "Seed fixture and probe battery version" below) — the harness
never generates a seed fixture, and a queue-mode run refuses to generate a
prompt bank interactively (see "Prompt bank resolution" below).

---

## Run Plan schema (`plans/*.json`)

A Run Plan is validated by `RunPlan` in `scripts/harness/schema.py`. Every
field below is what the schema currently accepts — an unrecognized or
mistyped field, or one violating a constraint, fails validation with a
field-identifying error (never a raw stack trace) before any graph mutation.

### Top-level fields

| Field | Type | Default | Notes |
|---|---|---|---|
| `plan_id` | string, non-empty | — (required) | Used to derive the run's identity, run directory name, and per-condition `experiment_id` (`{plan_id}_{condition}`). Keep it unique and filesystem-safe. |
| `plan_name` | string, non-empty | — (required) | Human-readable label only. |
| `conceptual_fields` | object | — (required) | See below. |
| `total_prompts` | int > 0 | derived | Exactly **two** of `total_prompts` / `series_count` / `turns_per_series` must be given; the third is derived. If all three are given, they must be mutually consistent (`total_prompts == series_count * turns_per_series`) or validation fails. |
| `series_count` | int > 0 | derived | Number of series (chat sessions) per condition. |
| `turns_per_series` | int > 0 | derived | Turns (bulk + probe combined) per series. |
| `probe_ratio` | float, 0.0–1.0 | `0.2` | Fraction of each series' turns that are probe turns (`round(turns_per_series * probe_ratio)`), never judged, never eligible for feedback. |
| `probe_placement` | string | `"end"` | One of `"random"`, `"end"`, `"fixed_checkpoints"`. `"end"` puts all probe turns as the tail of each series (recommended default — every series closes with a full battery pass). `"random"` shuffles probe positions per series (seeded, reproducible). `"fixed_checkpoints"` requires `probe_checkpoint_positions`. |
| `probe_checkpoint_positions` | list[int] or null | `null` | Required, non-empty, and in range `[0, turns_per_series)` when `probe_placement == "fixed_checkpoints"`. |
| `probe_set_version` | string, non-empty | `"v2"` | Selects `probes/probe_set_{version}.yaml`. Every Phase 1 plan sets this to `"v3"` — see "Seed fixture and probe battery version" below. |
| `seed_fixture_version` | string, non-empty | `"v2"` | Selects `seed_fixtures/seed_{version}.json.gz`, wiped and re-seeded per condition (`POST /admin/seed-graph`) and read for prompt-bank cluster content. Every Phase 1 plan sets this to `"v3"` — see below. |
| `trained_spokes` | dict[string, string] | `{cluster_a: a_spoke1, cluster_b: b_spoke1, cluster_c: c_spoke1, cluster_d: d_spoke1}` | Which spoke in each cluster is the single trained target prompt generation draws from. Shape-validated only (the 4 known cluster ids, values matching `{letter}_spokeN}`) — a value that doesn't resolve to real content in the selected fixture version fails loudly at runtime, not at schema-validation time. |
| `expected_config` | dict[dotted-key, value] | `{}` | Declared config-state assertion, checked against `GET /admin/effective-config` before any graph mutation. See the architecture section above. |
| `config_overrides` | dict[dotted-key, value] | `{}` | Per-condition config injection via `CONFIG_OVERRIDES_JSON`. See the architecture section above. |
| `difficulty` | object | `{hop_distance_levels: [0, 1]}` | See below. |
| `judge` | object | `{model: "gemma4:e4b", temperature: 0.0}` | See below. |
| `conditions` | list[string] | `["learned", "static_weight", "random_weight"]` | Each must be one of those three values; non-empty. **Two-condition plans (`["learned", "static_weight"]`, dropping `random_weight`) are accepted and are the Phase 1 default from Round 1 onward** — see `phase_one_experiment_program.md` §1-1's re-attach rule and §7-1 for the rationale (control cost, no coherent trajectory in two prior runs). `random_weight` remains fully supported for the confirmation replicate the re-attach rule requires. |
| `condition_order` | list[string] or null | `= conditions` | If given, must be a permutation of `conditions` — this is the execution order (each condition gets its own container restart + graph wipe/seed). |
| `tuning_knobs` | dict[string, float] | `{}` | **Retired as of M43** — legacy passthrough, not read by the backend. Use `config_overrides`/`expected_config` instead (see the architecture section above). Kept only for schema backward-compatibility with pre-M43 plan files. |
| `error_policy` | dict[string, string] | `{}` | Overrides on top of the default policy below. Each value must be `"hard_stop"` or `"soft_continue"`. |
| `notification` | object | `{enabled: true, channel: "log"}` | See below. |
| `budget_cap` | float ≥ 0 or null | `null` (uncapped) | Pre-run hard stop: if the estimated cost (prompt-bank generation + judge calls) exceeds this, the harness refuses to start. With a pre-generated bank and the local Ollama judge, actual marginal cost is ~$0 today — this is a guardrail for future hosted-judge/API configurations, not a real spend limiter yet. |
| `prompt_bank_path` | string or null | `null` | Path to an existing prompt bank JSON (see `prompt_banks/`). If absent and `conceptual_fields.subjects == "auto"`, the harness (interactive mode only) generates and saves one before executing. **Queue-mode runs must supply this or point at a bank the preflight can resolve by hash — see "Prompt bank resolution" below.** |
| `prompt_gen` | object | `{model: "claude-sonnet-5", max_retries: 2}` | See below. Irrelevant if `prompt_bank_path` is already set. |
| `dry_run` | bool | `false` | Scopes execution to `dry_run_n` turns per condition; also suffixes the run identity with `_dryrun` so dry-run analytics never contaminate a later real run's reports. |
| `dry_run_n` | int > 0 | `2` | Turns per condition when `dry_run` is true. |
| `random_seed` | int | `0` | Drives deterministic probe placement (`"random"`) and prompt-id assignment **only — it does NOT seed Thompson draws on training turns.** Those come from `edge_update.rng_seed`, which defaults to `null` and is then derived from `EXPERIMENT_ID` (`{plan_id}_{condition}`) — meaning **a new `plan_id` is a new Thompson stream even when `random_seed` is reused.** Reusing a `random_seed` across plans only pairs the turn/probe sequence ("quasi-paired"); to actually pair the Thompson draws too, set `edge_update.rng_seed` explicitly via `config_overrides` (see `phase_one_experiment_program.md` §7-3, open as of this writing). **Replicate plans must use distinct `random_seed` values** — two plans with an identical fully-resolved config (including `random_seed`) hash to the same `config_hash`, and both the single-run harness and the queue preflight treat that as an error, not a valid replicate. |

### `conceptual_fields`

| Field | Type | Default | Notes |
|---|---|---|---|
| `count` | int > 0 | — (required) | Number of conceptual fields (clusters) to draw prompts from. |
| `subjects` | list[string] or the literal string `"auto"` | `"auto"` | An explicit non-empty subject list, or `"auto"` to let prompt generation choose. |

### `difficulty`

| Field | Type | Default | Notes |
|---|---|---|---|
| `hop_distance_levels` | list[int], non-empty, all ≥ 0 | `[0, 1]` | Hop-distance difficulty levels used by prompt generation. The only difficulty mechanism currently in scope. |

### `judge`

| Field | Type | Default | Notes |
|---|---|---|---|
| `model` | string | `"gemma4:e4b"` | Judge model identity (config-swappable, need not match the agent model). |
| `temperature` | float, 0.0–0.1 | `0.0` | Deliberately tight — judge calls need near-0 temperature for cross-condition verdict comparability. |
| `pinned_snapshot` | string or null | `null` | Optional pinned model snapshot/digest for reproducibility. |

### `notification`

| Field | Type | Default | Notes |
|---|---|---|---|
| `enabled` | bool | `true` | |
| `channel` | string | `"log"` | One of `"log"`, `"email"`, `"webhook"`. Only `"log"` is implemented today (writes to console + `notifications.log`); `"email"`/`"webhook"` validate but fall back to `"log"` with a warning. |

### `prompt_gen`

| Field | Type | Default | Notes |
|---|---|---|---|
| `model` | string | `"claude-sonnet-5"` | Sonnet model used for prompt-bank generation. Set to a fixed string like `"session-authored-offline"` for a hand-authored bank so its content hash can never collide with a real Sonnet-generated bank. |
| `max_retries` | int ≥ 0 | `2` | Retries on generation validation failure. |

### Default error policy

`error_policy` overrides these defaults (from `DEFAULT_ERROR_POLICY`); do not
duplicate a key unless you're intentionally changing its action:

| Key | Default action | Meaning |
|---|---|---|
| `connectivity` | `hard_stop` | Backend unreachable / DB failure. |
| `turn_ambiguous` | `soft_continue` | A single turn's result is malformed or missing. |
| `judge_unparseable` | `soft_continue` | Judge response yields no usable **verdict** even after one re-ask (malformed JSON, unknown verdict value, Ollama unreachable). |
| `report_generation` | `soft_continue` | Series/experiment report call failed — regenerable later; must never abort an otherwise-successful run. |

### Worked example (single plan, matches `plans/exp01-cfgA-rep1_2026-08-12.json`)

A Phase 1-shaped plan — v3 fixture/probes, a two-condition sweep, and a
config sweep expressed via `config_overrides`/`expected_config` rather than a
manual `config.yaml` edit:

```json
{
  "plan_id": "exp01-cfgA-rep1-2026-08-12",
  "plan_name": "Experiment Round 2 (exp01) config A replicate 1 — slow-evidence seed priors",
  "conceptual_fields": { "count": 4, "subjects": "auto" },
  "series_count": 5,
  "turns_per_series": 70,
  "probe_ratio": 0.4,
  "probe_placement": "end",
  "probe_set_version": "v3",
  "seed_fixture_version": "v3",
  "expected_config": {
    "edge_update.seed_prior_alpha": 3.0,
    "edge_update.seed_prior_beta": 3.0
  },
  "config_overrides": {
    "edge_update.seed_prior_alpha": 3.0,
    "edge_update.seed_prior_beta": 3.0
  },
  "trained_spokes": {
    "cluster_a": "a_spoke1",
    "cluster_b": "b_spoke3",
    "cluster_c": "c_spoke1",
    "cluster_d": "d_spoke5"
  },
  "difficulty": { "hop_distance_levels": [0, 1] },
  "judge": { "model": "gemma4:e4b", "temperature": 0.0 },
  "conditions": ["learned", "static_weight"],
  "condition_order": ["learned", "static_weight"],
  "error_policy": {},
  "notification": { "enabled": true, "channel": "log" },
  "budget_cap": 25.0,
  "prompt_gen": { "model": "claude-sonnet-5", "max_retries": 2 },
  "dry_run": false,
  "random_seed": 20260809,
  "prompt_bank_path": "prompt_banks/prompt_bank_e2adf7962c0a780e1df48f362c6cf0d8158a40bfc57e0e83765afca95e661403.json"
}
```

Note `total_prompts` is *absent* here — it's legally derivable from
`series_count * turns_per_series` (5 × 70 = 350), so only two of the three
count fields need to be supplied. `config_overrides` and `expected_config`
carry identical key sets (§1-3 of `phase_one_experiment_program.md`), and
`conditions` drops `random_weight` per the Phase 1 default noted above.

---

## Sweepable hyperparameter knobs

The knobs currently pre-registered for the Phase 1 R3 sweep
(`phase_one_experiment_program.md` §3), each expressed as a
`config_overrides`/`expected_config` pair against the Round 1 baseline. Read
the named `sensitivity_baseline.md` entry before running any of these — each
carries mechanism findings from archived-data counterfactual analysis, not
just the knob's numeric range.

| # | `config.yaml` leaf | Baseline | R3 value(s) | `sensitivity_baseline.md` entry |
|---|---|---|---|---|
| V1 | `hyperparameters.retrieval_level2_top_k` | `1` | `3`, `5` | "M45 Phase 2" — K2 knob pricing (~5× REACHED). |
| V2 | `edge_update.credit_lambda` | `0.6` | `0.3`, `0.9` | "M45 Phase 2" — write-side near-frozen finding. |
| V3 | `edge_update.credit_scale` | `1.0` | `3.0` | Same entry — stands in for `decay` until implemented. |
| V4 | `edge_update.negative_scope` | `path` | `terminal` | Same entry — tests path-wide negative credit suppression. |
| V5 | `edge_update.evidence_cap.enabled` / `.n_max` | `false` | `true` / `50` | Same entry — constant-effective-learning-rate variant. |
| V6 | `hyperparameters.k1_centrality_weight` | `1.0` | `0.0` | "2026-08-26 — V6 pre-registration" — 88% K1-set churn on ~25% of score magnitude; removing it makes K1 *more* conv-dominated (88.8% → 95.9%); effect decays after session 2. Added Milestone 47. |

`k1_centrality_weight`'s `config_overrides` pair (V6, both replicates):

```json
"expected_config": { "hyperparameters.k1_centrality_weight": 0.0 },
"config_overrides": { "hyperparameters.k1_centrality_weight": 0.0 }
```

This table is pre-registered per §1-6 — additions go in
`phase_one_experiment_program.md` §7 with a rationale and a date before they
appear here.

---

## Queue schema (`queues/*.json`)

A Queue is validated by `QueueFile` in `scripts/harness/queue.py`. Required
for any run of more than one plan, and recommended even for a single plan so
its preflight checks, resume behavior, and `queue_summary.md` are available.

| Field | Type | Default | Notes |
|---|---|---|---|
| `queue_id` | string, non-empty | — (required) | Identifies this queue's launch directories (`harness_runs/queues/{queue_id}_{timestamp}/`) and drives resume auto-detection. |
| `on_plan_failure` | string | `"continue"` | `"continue"` or `"halt"`. On a failed plan, `"continue"` re-checks backend + judge health before proceeding — a genuinely dead backend halts regardless of this setting, since a dead backend would otherwise convert one failure into every remaining plan failing too. |
| `max_total_budget_usd` | float ≥ 0 or null | `null` | Aggregate pre-run cost estimate across every enabled plan must not exceed this — hard stop at preflight, before any plan runs. |
| `notification` | object | `{enabled: true, channel: "log"}` | Same `NotificationConfig` schema as the Run Plan's. |
| `plans` | list of `{plan_path, enabled}`, min length 1 | — (required) | See below. |

Each entry in `plans`:

| Field | Type | Default | Notes |
|---|---|---|---|
| `plan_path` | string, non-empty | — (required) | Resolves **relative to the queue file's own directory** when not absolute. |
| `enabled` | bool | `true` | `false` entries are recorded in the manifest as `skipped_disabled` and never loaded, validated, or executed — this is the JSON substitute for commenting a plan out. |

### Worked example (matches `queues/example_queue.json`)

```json
{
  "queue_id": "screening_a",
  "on_plan_failure": "continue",
  "max_total_budget_usd": 25.0,
  "notification": { "enabled": true, "channel": "log" },
  "plans": [
    { "plan_path": "../plans/replicate_seed1.json" },
    { "plan_path": "../plans/replicate_seed2.json", "enabled": true },
    { "plan_path": "../plans/sweep_k1_4.json", "enabled": false }
  ]
}
```

### Queue preflight (must fully pass before any plan's first turn)

1. Queue file parses and validates.
2. Every **enabled** plan loads and validates as a `RunPlan`.
3. Duplicate `config_hash` across enabled plans → hard stop ("replicates need
   distinct `random_seed` values").
4. Per-plan prompt-bank resolution (see below) → hard stop if any plan would
   need fresh Sonnet generation.
5. Backend health check; for every enabled plan, `expected_config` is asserted
   against the live backend **only when that plan has no `config_overrides`**
   (an override-carrying plan's config only becomes real once its own
   per-condition restart applies it — asserting against whatever the
   *previous* plan left running would false-positive). A plan **with**
   `config_overrides` instead gets a shape check: every dotted key must
   resolve to an existing `config.yaml` leaf (checked against the live
   config's shape, not its values) — a typo'd or renamed key is a hard stop
   here, not a `ConfigOverrideError` crash hours into an unattended queue at
   that plan's own restart. This is a shape check only; a resolvable key
   holding the *wrong* value is not caught here — `config_overrides` **is**
   the plan's intended value, and its provenance is recorded on every real
   run row via `analytics.full_config_snapshot` for after-the-fact audit.
   Judge model availability is checked for every distinct judge model
   referenced in the same step.
6. Aggregate budget check against `max_total_budget_usd`.
7. Disk-space note: projected archive count (enabled plans × their condition
   counts) is logged; a free-space WARN fires below a conservative threshold
   (not a hard stop — archive-size prediction is inherently approximate).

Run `--dry-run` to execute the full preflight and print the resolved
execution order with **zero** HTTP mutation calls and zero files written:

```powershell
python scripts/run_experiment_queue.py --queue queues/screening_a.json --dry-run
```

### Prompt bank resolution (applies to both single-plan and queue runs)

- If `prompt_bank_path` is set, it must exist and pass validation — used as-is.
- If unset and `conceptual_fields.subjects == "auto"`, the harness looks for
  an already-generated bank matching this plan's content hash. If found, its
  path is stamped onto the plan automatically. If not found, fresh Sonnet
  generation would be required.
- **Queue-mode runs never call the Sonnet API.** A plan needing fresh
  generation is a preflight hard stop with the message "run `--generate-bank`
  for this plan interactively first." Generate it ahead of time:

  ```powershell
  python scripts/run_experiment_harness.py --plan plans/my_plan.json --generate-bank
  ```

- A plan with neither an existing `prompt_bank_path` nor `subjects == "auto"`
  is also a hard stop — there is no third option; running placeholder-text
  turns unattended overnight wastes GPU-hours for no data.

### Resume

Re-invoking the same queue command auto-detects the most recent
`harness_runs/queues/{queue_id}_*` launch directory and resumes it, **guarded
by an exact `queue_content_hash` match** against the current queue file — a
mismatch (the queue file changed since that launch) hard-stops rather than
guessing which plans already ran. Use `--resume-dir <dir>` to target an
explicit launch directory instead of auto-detection.

Resume is safe at any granularity: a plan whose own harness run was mid-turn
resumes at the next unrecorded turn (per-turn checkpointing); a plan whose
harness run had already fully completed reconciles to `complete` without
re-invoking `execute_run()` at all — closing the crash window between the
harness finishing and the queue's own status write.

---

## Running an experiment

### Single plan (interactive elicitation or a plan file)

```powershell
# Interactive — prompts for every field, offers to save the resulting plan JSON
python scripts/run_experiment_harness.py

# From a saved plan file
python scripts/run_experiment_harness.py --plan plans/my_plan.json

# Dry run first (forces plan.dry_run regardless of the file's own setting)
python scripts/run_experiment_harness.py --plan plans/my_plan.json --dry-run
```

This single call performs, per condition in `condition_order`: a backend
container restart with that condition's `EXPERIMENT_ID`/`CONDITION_LABEL`/
`RUN_CONDITION` env vars, a graph wipe + seed-fixture load, the resolved turn
sequence (bulk `/chat` turns judged and fed back automatically; probe turns
via `/admin/run-probe`, read-only, never judged), a series report at the
close of each series, and an experiment report (with control-shadow
comparison against the other conditions) at the close of the whole plan.
Nothing here is a manual step — do not hand-drive `/chat`/`/feedback` calls
for a real experiment.

### A full long series run (queue)

```powershell
# 1. Validate every plan + resolve prompt banks, zero mutation
python scripts/run_experiment_queue.py --queue queues/screening_a.json --dry-run

# 2. Launch for real (unattended — safe to close the terminal and reattach later)
python scripts/run_experiment_queue.py --queue queues/screening_a.json

# 3. If interrupted (crash, reboot, manual stop) — same command resumes automatically
python scripts/run_experiment_queue.py --queue queues/screening_a.json
```

Progress and final results:

- `harness_runs/queues/{queue_id}_{launch_ts}/queue_manifest.json` — resolved
  plan list, per-plan `config_hash`/expected `run_id`, preflight results.
- `harness_runs/queues/{queue_id}_{launch_ts}/queue_checkpoint.json` — live
  per-plan status (`pending` / `running` / `complete` / `failed` / `skipped`).
- `harness_runs/queues/{queue_id}_{launch_ts}/queue_summary.md` — written at
  queue end: per-plan durations (wall-clock + per-condition), report paths,
  pass/fail summary.
- `harness_runs/queues/{queue_id}_{launch_ts}/notifications.log` — one JSONL
  line per `queue_plan_complete` / `queue_plan_failed` / `queue_complete` /
  `queue_aborted` event.

Each individual plan's own harness output lands at
`harness_runs/{plan_id}_{config_hash[:12]}/` exactly as it would for a
single-plan run — the queue changes nothing about the harness layer.

---

## Seed fixture and probe battery version

The schema default for both `seed_fixture_version` and `probe_set_version`
is still `"v2"` (backward compatibility with pre-v3 plan files), but **every
Phase 1 plan sets both explicitly to `"v3"`**
(`seed_fixtures/seed_v3.json.gz` — counterfactual content, 34 MUs, 64
reciprocal edges; `probes/probe_set_v3.yaml`). The "v3 cascade" wiring
(harness fixture load, prompt generation reading v3 cluster content, and the
probe endpoint resolving `probe_set_v3.yaml`) is complete and has been
exercised by every archived Phase 1 run (exp0, exp0b, exp01) — do not leave
`seed_fixture_version`/`probe_set_version` unset on a new Phase 1 plan
expecting them to default correctly; set both to `"v3"` explicitly.

---

## Report generation

Series and experiment reports are generated automatically by `execute_run()`
— a series report at the close of every series, an experiment report (with
`control_shadow` comparisons against non-`learned` conditions) at the close
of the whole plan. No manual `POST /admin/reports/*` call is part of the
standard flow; those endpoints remain available for regenerating or
inspecting a report after the fact:

```
GET  http://localhost:8000/admin/reports              # list all generated reports
GET  http://localhost:8000/admin/reports/{id}          # fetch report JSON
GET  http://localhost:8000/admin/reports/{id}/markdown # fetch report Markdown
POST http://localhost:8000/admin/reports/generate-series      # re-generate manually
POST http://localhost:8000/admin/reports/generate-experiment  # re-generate manually
```

Open the Admin UI (`http://localhost:3001` → Reports tab) to browse them
visually; the comparison table auto-renders whenever `control_shadow` is
non-null.

---

## Troubleshooting

**Preflight fails with "duplicate config_hash across plans":**
Two enabled plans in the queue resolve to an identical fully-serialized
config. This is not a valid replicate — give each a distinct `random_seed`.

**Preflight fails with "needs fresh Sonnet prompt-bank generation":**
Run `python scripts/run_experiment_harness.py --plan <that plan> --generate-bank`
first (interactively, outside the queue), then re-run the queue's `--dry-run`.

**Preflight fails with "aggregate estimated cost ... exceeds max_total_budget_usd":**
Raise `max_total_budget_usd` in the queue file, or reduce plan scope
(`total_prompts`/`series_count`/`turns_per_series`) — the same message from
`check_prerun_budget` also appears for a single plan's own `budget_cap`.

**Resume hard-stops with a `queue_content_hash` mismatch:**
The queue file was edited since the launch it's trying to resume. Either
start a fresh launch (new `queue_id` or accept a new launch directory) or
pass `--resume-dir` pointing at the exact prior launch directory if you
intend to resume that one despite the edit.

**Backend healthcheck fails (`db: error`):**
The graph DB volume mount may be locked or missing. Check
`docker-compose logs backend` for the SQLite traceback; restarting the
container usually resolves transient lock contention.

**A condition's probe results all show `null` pass rates:**
Confirm the plan's `probe_set_version` matches a battery file that actually
exists under `probes/`, and that the run didn't hard-stop before any probe
turn executed (check `error_summary` in the harness or queue checkpoint).

**`control_shadow` sanity check `zero_weight_changes` fails for `static_weight`:**
Something outside the harness sent feedback during that condition's run
(the harness itself never does for non-`learned` conditions). Inspect
`analytics`/`feedback_events` for the offending `experiment_id`.

**`weights_rerandomized` fails for `random_weight`:**
Should not occur under harness-driven execution (`execute_run()` calls
`POST /admin/edges/randomize` before each series in this condition
automatically) — if it does, treat it as a bug and file it rather than
re-randomizing by hand.
