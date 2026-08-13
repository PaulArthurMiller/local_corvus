# Local Corvus — Experiment Harness Usage Guide

This is the operator's guide for running a full-sized, three-condition experiment
through the automated harness (`scripts/run_experiment_harness.py`). The harness
automates everything `docs/experiment_protocol.md` describes as manual API calls:
per-condition backend restarts, graph wipe/seed, turn execution, probe batteries,
judging, verdict posting, checkpointing, budget enforcement, and report generation.

For **several plans back-to-back** — replicates, or a one-factor sweep — see
§7, which covers `scripts/run_experiment_queue.py` and the `config.yaml`
boundary that decides how a multi-config round must be split.

Read this document top to bottom before your first full run. After that, the
**Quick Start** section is all you need.

---

## Quick Start (for operators who have run one before)

```powershell
# 1. Stack up, Ollama serving, tests green
docker compose up -d --build
python -m pytest

# 2. Dry-run the plan (2 turns, 1 condition, isolated _dryrun identity)
python scripts/run_experiment_harness.py --plan plans/my_plan.json --dry-run

# 3. Full run
python scripts/run_experiment_harness.py --plan plans/my_plan.json

# Several plans sharing one config.yaml state (see §7):
python scripts/run_experiment_queue.py --queue queues/my_queue.json --dry-run
python scripts/run_experiment_queue.py --queue queues/my_queue.json
```

Everything below explains what those three commands actually do, what can stop
them, and how to verify the results.

---

## 1. What the harness is (and is not)

The harness **executes** run plans; it does not design them. Given a validated
plan JSON, it deterministically resolves every turn of every series of every
condition *before anything runs*, writes that resolved sequence to a manifest,
and then works through it with a checkpoint saved after every single turn.
Determinism is the core design principle: the same `(plan, random_seed)` always
produces the same turn sequence, the same prompt assignment, and the same probe
schedule — which is what makes an interrupted run resumable with zero drift.

One full run produces, per condition:

- A backend restart stamped with that condition's `EXPERIMENT_ID`,
  `RUN_CONDITION`, and `CONDITION_LABEL`
- A graph wipe + re-seed from `seed_fixtures/seed_v2.json.gz`
- N series of bulk turns (real prompts from the bank, judged by Gemma, verdicts
  posted — weight updates applied only in the `learned` condition)
- Probe battery measurements via `POST /admin/run-probe` (read-only, never
  judged, never mutate the graph)
- A series report at the close of each series

…and at experiment close: one experiment report for the `learned` condition
with the two controls wired in as `control_experiment_ids`, plus a completion
notification.

**What it does not do:** author prompt banks or probe sets (those are artifacts
you prepare or generate beforehand), merge PRs, or interpret results.

---

## 2. Prerequisites checklist

Work through this list before every full-sized run:

1. **Clean `main`.** Pull latest; make sure any pending fix PRs that touch the
   harness or report layer are merged. Run the full suite on the host:
   ```powershell
   python -m pytest
   ```
   All tests must pass. A full run takes an hour-plus of wall clock — don't
   spend it on a known-broken tree.

2. **Ollama serving natively on Windows** with the agent/judge model pulled:
   ```powershell
   ollama pull gemma4:e4b
   curl http://localhost:11434/api/tags
   ```
   The harness talks to Ollama at `localhost:11434` (host-side); the backend
   container reaches it at `host.docker.internal:11434`.

3. **Docker stack healthy:**
   ```powershell
   docker compose up -d --build
   curl http://localhost:8000/health
   curl http://localhost:8000/ollama-health
   ```
   Both must return `"status": "ok"`. The harness runs its own health preflight
   too, but catching this early is cheaper.

4. **A prompt bank** — either an existing `prompt_banks/prompt_bank_<hash>.json`
   referenced by the plan, or `ANTHROPIC_API_KEY` set in your shell so the
   harness can generate one (see §4). Never set the API key inside Docker; bank
   generation is host-side only.

5. **The probe set the plan names** exists at
   `probes/probe_set_{probe_set_version}.yaml` (currently `v1` and `v2`; `v2`
   is the 10-probe default).

6. **Power settings.** A 375-turn run takes roughly 75 minutes. Per-turn
   checkpointing means a laptop sleep mid-run is recoverable (this happened in
   the 2026-07-06 pilot; the run resumed cleanly, no turns lost), but it's
   simpler to disable sleep for the duration.

---

## 3. Writing the run plan

Plans live in `plans/*.json` and are validated against
`scripts/harness/schema.py` (`RunPlan`). A bad plan exits with a
field-identifying error before anything touches the graph.

A known-good full-sized reference: `plans/pilot_125_2026-07-05.json`
(5 series × 25 turns × 3 conditions = 375 total turns).

### Field reference

| Field | Meaning | Notes |
|---|---|---|
| `plan_id` | Identity root for the whole run | `experiment_id` becomes `{plan_id}_{condition}` — keep it unique per experiment (date-stamp it) |
| `plan_name` | Human label | Free text |
| `conceptual_fields.count` | Number of seed clusters to draw from | 4 for seed_v2 |
| `conceptual_fields.subjects` | `"auto"` or explicit list | `"auto"` + no `prompt_bank_path` triggers bank generation |
| `series_count` / `turns_per_series` / `total_prompts` | Run size | Provide exactly **two**; the third is derived. If all three are given they must be consistent |
| `probe_ratio` | Fraction of each series' turns that are probes | `round(turns_per_series × probe_ratio)` probes per series. `0.4` × 25 turns = 10 = exactly one full v2 battery per series |
| `probe_placement` | `"end"`, `"random"`, or `"fixed_checkpoints"` | `"end"` closes every series with the battery — the standard choice. `fixed_checkpoints` requires `probe_checkpoint_positions` (0-based, `< turns_per_series`) |
| `probe_set_version` | Which `probes/probe_set_*.yaml` to run | Default `"v2"`. Recorded in the manifest |
| `difficulty.hop_distance_levels` | Hop levels for bank generation | Default `[0, 1]` |
| `judge.model` | Ollama judge model | Default `gemma4:e4b`. Hard preflight: run aborts if not present on Ollama |
| `judge.temperature` | Must be ≤ 0.1 | Verdict comparability requires near-determinism; keep `0.0` |
| `conditions` / `condition_order` | Which of `learned` / `static_weight` / `random_weight` run, and in what order | `condition_order` must be a permutation of `conditions`. Run all three for a real experiment — the experiment report's control shadow needs both controls |
| `error_policy` | Overrides per error category | See §8. Empty `{}` = defaults, which are correct for routine runs |
| `notification.channel` | `"log"` (implemented), `"email"`/`"webhook"` (fall back to log with a warning) | |
| `budget_cap` | USD cap, or `null` for none | Checked pre-run against the estimate and mid-run after every judge call. Local Ollama judge calls cost $0, so today this only bites on Sonnet bank generation — but set it anyway |
| `prompt_bank_path` | Path to an existing bank | Set this to reuse a validated bank ($0 Sonnet spend). Omit it (with `subjects: "auto"`) to generate |
| `dry_run` / `dry_run_n` | Dry-run scoping | Prefer the `--dry-run` CLI flag over baking `dry_run: true` into a plan file |
| `random_seed` | Drives probe placement (random mode) and prompt assignment | Record it; same seed = same resolved sequence. Date-stamp convention: `20260705` |

### Sizing guidance

- The M19 science goal is ≥100 bulk prompts per condition. 5×25 with
  `probe_ratio 0.4` gives 75 bulk + 50 probe turns per condition.
- Wall clock on the real gemma4:e4b box: ~12 s/turn including judging —
  budget **~75 minutes per 375 turns**.
- The prompt bank must have at least as many prompts as you want distinct
  content for; `assign_prompt_ids` draws without replacement and reshuffles
  once the pool is exhausted, so a 75-prompt bank covers 75 bulk turns per
  condition exactly once.

---

## 4. The prompt bank

### Reusing an existing bank (the normal case)

Set `prompt_bank_path` in the plan to an existing
`prompt_banks/prompt_bank_<sha256>.json`. This is fully offline and free.
The bank's `criteria` per prompt are what the judge grades against — a bank
that ran a successful pilot is a calibrated instrument; prefer reuse.

### Generating a new bank

If the plan has `subjects: "auto"` and no `prompt_bank_path`, the harness
generates a bank with Sonnet before the run. You can also generate ahead of
time without running anything:

```powershell
$env:ANTHROPIC_API_KEY = "sk-ant-..."
python scripts/run_experiment_harness.py --plan plans/my_plan.json --generate-bank
```

The generator shows a cost estimate and asks for confirmation before any API
call (declining aborts with zero spend). The resulting bank is content-hashed —
an identical-parameters regeneration reuses the existing file at $0. After
generation, set `prompt_bank_path` in the plan so future runs skip the
generation path entirely.

### Authoring/reviewing bank prompts — the retrieval constraint

`retrieve()` takes the **first 5 non-stopword tokens** of the message as its
BM25 keywords, with exact (inflection-sensitive) matching. Prompts must
**front-load exact target-MU vocabulary** or baseline retrieval finds nothing
and every condition flatlines. This was learned the hard way: the first 75-
prompt draft failed baseline retrieval on 48/75 prompts before anchor-word
revision. If you author or edit a bank, run a clean-graph baseline eval on it
before committing to a full run.

---

## 5. Dry run first — always

```powershell
python scripts/run_experiment_harness.py --plan plans/my_plan.json --dry-run
```

Dry-run mode scopes execution to `dry_run_n` turns (default 2) of series 0 of
the **first** condition in `condition_order`, and — critically — runs under a
separate `{plan_id}_dryrun` identity, so its analytics/probe rows can never
contaminate the later real run (they share a database; `wipe_graph()` never
deletes analytics, feedback events, or probe results).

What a dry run verifies cheaply: plan validity, bank loads, probe battery
loads, backend and judge reachable, a real chat turn and (if the first scoped
turns include one) a real probe turn complete, checkpoint/manifest written.

### Judge calibration (before the first full run on a new bank)

Spot-check 15–20 judged turns interactively so you catch ambiguously-written
criteria before they grade 225 turns:

```powershell
python scripts/run_experiment_harness.py --plan plans/my_plan.json --dry-run --interactive
```

(Temporarily raise `dry_run_n` in the plan or via a copy of it to get enough
judged bulk turns.) `--interactive` shows the full per-criterion checklist the
judge graded — each criterion marked `[x]`/`[ ]`, **with the exact words the
judge quoted from the response as its evidence**, plus any host-side evidence
flags — then the derived verdict and rationale. This is calibration mode, not
routine use; never use it on the real run.

You then choose:

| Answer | Does |
|---|---|
| `r` | **Regrade per-criterion** — walk the checklist one criterion at a time (`y`/`n`, Enter keeps the judge's answer); the verdict is re-derived from your corrected checklist by the same rule the judge path uses |
| `a` | Accept the checklist as graded |
| `o` | Override the verdict directly (`up`/`down`/`none`), leaving the checklist untouched |
| `skip` | Record no verdict for this turn |

**Prefer `r`.** Calibrating per-criterion instead of per-verdict is the
Milestone 37 change, and the reason is what the 2026-08-04 pass could and
could not measure: 3 overrides out of 20 turns is a rate with a 5–36%
confidence interval — uninformative — while the *composition* of those
overrides was decisive (all three were the judge crediting a criterion on a
looser standard than it states, every one lenient). Regrading produces one
observation per criterion rather than one per turn (~2.4x on the current
bank) and each observation names its criterion, so the next pass measures
the composition directly instead of reconstructing it by hand afterwards.

Use `o` only when you disagree with the **derivation rule** itself rather
than with any individual criterion — it records no per-criterion data.

A regrade in which you keep every judge answer is recorded as the judge's own
verdict, not a human one: confirming a checklist is an affirmation, and
counting it as human-sourced would understate the judge-alone up-rate.

#### The calibration summary

Every `--interactive` run writes `calibration_summary.md` into its
`harness_runs/{run_id}/` directory (and prints it at the end), covering:

- **Per-criterion agreement** — criteria graded, agreement rate, and the
  lenient/strict split. A split skewed to one side is a systematic grading
  standard problem; a balanced split at a similar total is noise in a hard
  judgment call.
- **Turn-level outcomes** — regraded, confirmed, verdict-overridden, skipped.
- **Verdict mix** — recorded vs. judge-alone (see the next section).
- **Evidence checks** — how often each host-side check fired.

It is written on an aborted pass too: a pass that stopped at turn 12 still
measured 12 turns' worth of criteria.

#### Reading a calibration run's report (Milestone 36)

An override changes the verdict that drove learning, so a calibration run's
headline up-rate is **human-assisted** — it is not the judge's calibration.
The report's Verdict Mix section states both:

| Line | Means |
|---|---|
| `Overall up-rate` | What actually drove learning on this run, overrides included |
| `Human-sourced verdicts` | How many turns you overrode or hand-graded |
| `Judge-alone up-rate` | What the judge would have recorded unaided — **the number that describes the real, unattended run** |

A fully automated run has zero human-sourced verdicts and the two rates
coincide; the gap between them is the judge's measured miscalibration, and
the direction of the gap is the direction of its bias. The 2026-08-04
calibration read 70.6% recorded against 82.4% judge-alone across 3 overrides
— none of which appeared in the report before this milestone.

Two caveats on the judge-alone figure:

- Verdicts recorded before M36 have no stored pre-override verdict. Those
  rows are excluded from the judge-alone tallies and counted in an explicit
  ⚠️ coverage warning rather than guessed at, so an old experiment reports a
  partial view instead of a confidently wrong rate.
- `rationale` on an overridden row describes *your* verdict; the judge's own
  reasoning is preserved separately as `judge_model_rationale`. Query that
  column when auditing *why* the judge disagreed — it is the per-turn
  evidence behind the rate.

---

## 6. The full run

```powershell
python scripts/run_experiment_harness.py --plan plans/my_plan.json
```

### CLI flags

| Flag | Purpose |
|---|---|
| `--plan PATH` | The plan file. Omit it for interactive plan elicitation |
| `--dry-run` | Force dry-run scoping + `_dryrun` identity |
| `--backend-url URL` | Default `http://localhost:8000` |
| `--judge-host URL` | Default local Ollama |
| `--generate-bank` | Generate/reuse the plan's bank, then exit |
| `--interactive` | Per-criterion regrade / accept / override prompts (calibration only) |
| `--no-docker-restart` | Skip per-condition backend restarts. **Only safe for single-condition runs** — without the restart, every condition gets stamped with the same `EXPERIMENT_ID`/`RUN_CONDITION` and the experiment is garbage |

### What happens, in order

1. **Plan validation** — field-identifying errors, exit before any mutation.
2. **Pre-run budget gate** — printed estimate (`bank_generation` +
   `judge_calls`) checked against `budget_cap`. Exceeding it aborts here.
3. **Bank generation** (only if needed) — with its own cost-confirmation prompt.
4. **Turn resolution + manifest** — every turn's type, prompt_id, probe_id, and
   `measurement_session_index` fixed and written to
   `harness_runs/{run_id}/manifest.json` before turn 1.
5. **Preflights (all hard stops, all before any graph mutation):**
   probe battery present if the plan has probe turns (unconditional, not
   policy-overridable); backend `/health`; judge model present on Ollama
   (unconditional).
6. **Per condition, in `condition_order`:**
   - Backend container restarted with `EXPERIMENT_ID={run_identity}_{condition}`,
     `RUN_CONDITION={condition}`, `CONDITION_LABEL={condition}`; health polled.
     A failed restart is an unconditional hard stop.
   - Graph wiped (pre-wipe archive saved) and re-seeded from seed_v2; counts
     printed.
   - `random_weight` only: edge weights re-randomized once at the start of each
     series (watch for `edges_randomized=<N>` in the console — N should be >10).
   - Turns execute. Bulk turns → `POST /chat` with the deterministic
     `series_id`, then judged, verdict posted (`apply_weight_update` true only
     for learned + up/down). Probe turns → `POST /admin/run-probe`, never
     judged.
   - Checkpoint saved after **every** turn.
   - Series report generated at each series' final turn.
7. **Experiment close** — experiment report for `{run_identity}_learned` with
   `control_experiment_ids` assembled automatically; notification fired with
   the report path.

### Console landmarks to watch for

- `Pre-run cost estimate: ... budget_cap=...`
- `Backend restarted for condition=<c> EXPERIMENT_ID=<id>` — three times
- `Wiped graph ... / Seeded graph ... mu_count=34`
- `turn (condition, series, turn) type=bulk -> ok verdict=up`
- `turn (...) type=probe -> ok probe=p03 session=4`
- `Series report generated: ...` — once per series per condition
- `Experiment report generated: experiment_id=<plan_id>_learned`
- `COMPLETE: <N> turn(s) processed.` and exit code 0

---

## 7. Running several plans: the queue

One `run_experiment_harness.py` invocation runs one plan. When you have
several — replicates of a condition, or a one-factor sweep — a **queue** feeds
an ordered list of plan files through the same `execute_run()` back-to-back,
unattended, with zero interaction after launch.

```powershell
python scripts/run_experiment_queue.py --queue queues/my_queue.json --dry-run
python scripts/run_experiment_queue.py --queue queues/my_queue.json
```

The queue is a **playlist player**. It never reads results, never interprets
them, never edits plans. A wrong plan fails preflight and a human fixes it.

### The queue file

```json
{
  "queue_id": "exp01_cfgA_slow_evidence_priors",
  "on_plan_failure": "continue",
  "max_total_budget_usd": 50.0,
  "notification": { "enabled": true, "channel": "log" },
  "plans": [
    { "plan_path": "../plans/exp01-cfgA-rep1_2026-08-12.json" },
    { "plan_path": "../plans/exp01-cfgA-rep2_2026-08-12.json" }
  ]
}
```

| Field | Meaning |
|---|---|
| `queue_id` | Identity root; launch dirs are `harness_runs/queues/{queue_id}_{timestamp}/` |
| `on_plan_failure` | `"continue"` (default) or `"halt"`. See the health-recheck caveat below |
| `max_total_budget_usd` | Checked against the **aggregate** estimate across all enabled plans, before plan 1 starts |
| `plans[].plan_path` | Resolves **relative to the queue file's own directory** when not absolute |
| `plans[].enabled` | Defaults `true`; disabled entries are recorded `skipped_disabled` and never validated or executed |

Queue settings live in the queue file only — never in `config.yaml`. They are
per-queue operational choices, not experimental knobs under study.

### ⚠️ One queue = one `config.yaml` state (conditional as of M43)

**This is the constraint that decides how you split your runs.** Before
Milestone 43 it was absolute and its violation was silent; M43 closes the
silence (Phase 1 — see "Plan-declared config: the M43 assertion" below) and,
for a plan that opts in via `config_overrides` (Phase 2), the constraint no
longer applies to that plan at all.

`RunPlan` has no *general* hyperparameter fields. Hyperparameters live in
`config.yaml`, which is bind-mounted into the backend container and applies to
every run until changed again — unless a plan supplies its own
`config_overrides` (M43 Phase 2), in which case the harness's own
per-condition backend restart injects them for that plan only, and
`config.yaml` itself is never touched.

| | Same `config.yaml`? | Any plan uses `config_overrides`? | Verdict |
|---|---|---|---|
| Replicates of one config (differing only in `random_seed`) | yes | no | **Queue them** (unchanged) |
| Two configs in a sweep, `config.yaml` switched by hand between them | no | no | **Separate invocations, one per config** (unchanged — M43 Phase 1 now also *catches* a forgotten switch, see below) |
| Two configs in a sweep, each plan supplies its own `config_overrides` | no | yes | **Queue them together** — each plan's own restart carries its own config (M43 Phase 2) |

This is why exp01's Round 2 still ships as *two* queue files
(`queues/exp01_cfgA_*.json`, `queues/exp01_cfgB_*.json`) — those plans predate
M43 and rely on the manual config-switch protocol below. A plan written with
`config_overrides` from the start would not need this split.

**Why the pre-M43 failure was silent.** `config_hash` is computed over the
*plan file only*. Two plans that differ solely in which `config.yaml` they
assume produce byte-identical hashes for everything the harness checks. If you
forgot the edit, the run completed normally, wrote reports, and passed every
§6.2/§6.3 integrity and sanity check — it was simply a replicate of the
previous config, silently. The only provenance was
`analytics.full_config_snapshot`, stamped on every run row — checkable only
*after* the fact, once the night was already spent.

### Plan-declared config: the M43 assertion + injection

**`GET /admin/effective-config`** (additive; `GET /admin/config` is
unchanged and still just re-reads the file) returns what the orchestrator
actually *resolved* at process startup — `config.yaml` merged with any
`CONFIG_OVERRIDES_JSON` — plus `runtime_config_hash` (a sha256 over that
resolved config, distinct from `config_hash`, which hashes the *plan*) and
`overridden_keys`. Re-reading the file would certify the exact bug this
exists to catch: edit `config.yaml`, skip the restart, and a naive check
would see the fresh disk value while every turn still executes on the stale
one that was resolved at the last container boot.

- **`RunPlan.expected_config`** (Phase 1) — `{"dotted.key": expected_value}`.
  Checked once per condition, after that condition's restart (if any) and
  before its wipe/seed. A mismatch — including a declared key absent from
  the live config — is an unconditional hard stop naming every offending
  key as `key: expected=X actual=Y`. `queue.py`'s own preflight (step 5)
  checks it too, once per plan, but only enforces it for a plan whose
  `config_overrides` is empty — a plan that supplies its own overrides gets
  its config established by its own restart once it actually runs, so
  checking it against whatever the *previous* plan left the backend running
  would false-positive.
- **`RunPlan.config_overrides`** (Phase 2) — same dotted-key shape, injected
  via the harness's existing per-condition backend restart as
  `CONFIG_OVERRIDES_JSON`. `config.yaml` is never written — the merge happens
  in-process (`core/orchestrator.py` and every other module that
  independently loads `config.yaml`: `memory/extractor.py`,
  `analytics/seed_fixture.py`, `analytics/reports/experiment_report.py`,
  `analytics/reports/series_report.py`) before anything is derived from it.
  A malformed override or an unknown dotted key hard-fails at container
  import — before any run state exists — the same posture as
  `validate_edge_update_config()`.
- Both fields participate in `plan_config_hash` — a plan that declares its
  config *is* a different plan, which is the actual fix for "two plans that
  differ only in an assumed `config.yaml` state look identical to the
  harness."
- Every run and queue manifest records `runtime_config_hash` + the full
  resolved snapshot, regardless of whether `expected_config` is set — this
  is the provenance half, independent of the assertion half.

### The config-switch protocol (manual — for plans without `config_overrides`)

A plan that doesn't opt into `config_overrides` (M43 Phase 2) still relies on
this manual protocol; the difference from before M43 is that step 5's
verification is no longer optional discipline — a plan with `expected_config`
set will hard-stop on its own if you skip the restart, instead of silently
running the previous config.

Per config, in order:

1. Working tree clean; `config.yaml` at baseline (committed state).
2. Edit `config.yaml` — exactly the lines that config names, and **all** of
   them. A config may require more than one edit: exp01's cfgB needs both
   `hyperparameters.retrieval_level2_top_k: 2` *and*
   `reporting.pass_rank_thresholds.spoke_retrieval: 5`, because a spoke gold
   reached via K2 now occupies rank 4 or 5. Setting the knob without the
   threshold leaves the measurement wrong rather than the run broken.
3. `docker compose restart backend` — the file is mounted, so a restart applies
   it.
4. Restart Ollama (the 2026-08-06 prompt-cache OOM lesson).
5. **Verify the live config before burning a wipe+seed:**
   ```powershell
   docker compose exec backend python -c "import yaml; c = yaml.safe_load(open('/app/config.yaml')); print('seed_priors', c['edge_update']['seed_prior_alpha'], c['edge_update']['seed_prior_beta'], '| k2', c['hyperparameters']['retrieval_level2_top_k'], '| spoke_thresh', c['reporting']['pass_rank_thresholds']['spoke_retrieval'])"
   ```
   It must print the intended values for the config about to run. This step is
   the only thing standing between a forgotten edit and a wasted night.
6. Launch the queue.
7. **After the queue: `git checkout -- config.yaml`, restart backend.** Never
   leave a config experiment's values as the working state — the next run to
   launch will silently inherit them.

Record which config was live in `PROGRESS.md` alongside the run, and confirm it
from `full_config_snapshot` at the start of the read session.

### What the preflight checks (all before turn 1)

The whole point of a queue is that a bad *second* plan fails at minute zero
rather than two hours in. In protocol order, all hard stops:

| Step | Check |
|---|---|
| 2 | Every enabled plan loads and validates (field-identifying errors) |
| 3 | **Duplicate `config_hash` across plans** — catches a forgotten `random_seed` bump between replicates, with the message "replicates need distinct `random_seed` values" |
| 4 | Per-plan bank resolution. A plan needing fresh Sonnet generation, or one with neither a bank nor `subjects: "auto"`, hard-stops — placeholder turns in an unattended overnight run are wasted GPU-hours |
| 5 | Backend `/health`; `GET /admin/effective-config` (M43 — enforced per plan only when that plan's `config_overrides` is empty; always recorded for provenance); every distinct `judge.model` present on Ollama |
| 6 | Aggregate estimated cost across all plans vs. `max_total_budget_usd` |
| 7 | Projected archive count + free-disk WARN (never a hard stop) |

`--dry-run` runs all seven and prints the resolved execution order, then exits
0 — zero HTTP mutation calls, zero graph mutation, nothing written to disk.
Always run it before the real launch.

### Failure and resume semantics

- **`on_plan_failure: "continue"` is conditional.** After a failed plan the
  queue re-checks backend health; if that fails it halts the remainder anyway,
  rather than letting every remaining plan fail identically against a dead
  backend. An unexpected exception (as opposed to a clean harness abort) always
  halts.
- **Resume: just relaunch the same command.** The CLI auto-detects the latest
  incomplete `{queue_id}_*` launch directory, guarded by a queue-content hash.
  A hash mismatch — the queue file changed since that launch — hard-stops
  rather than guessing; use `--resume-dir` to point at one explicitly.
- **Resume prompts are auto-answered `y`.** The queue runs unattended, so every
  prompt `execute_run()` would otherwise raise is answered automatically:
  already-done turns are skipped, nothing blocks overnight.
- **A plan whose own harness checkpoint is already `complete` is reconciled,
  not re-run.** This covers a crash between the harness finishing and the queue
  recording it, and an identity already run manually outside the queue — either
  way it does not burn a fresh, wasted attempt.

### Artifacts

| Artifact | Location |
|---|---|
| Queue manifest, checkpoint, `queue_summary.md` | `harness_runs/queues/{queue_id}_{timestamp}/` |
| Per-plan manifests/checkpoints | `harness_runs/{run_id}/` — unchanged, exactly as a solo run |
| Series + experiment reports | Backend data dir, as always |

`queue_summary.md` carries one row per plan: final status, run_id, wall-clock
and per-condition durations, generated report paths, and any error summary.

---

## 8. Interruption, resume, and re-run semantics

This is the part most worth understanding precisely, because getting it wrong
mixes trials.

- **Interrupted run (crash, Ctrl+C, sleep):** relaunch the same command. The
  harness finds the incomplete checkpoint and asks
  `Resume this run? (y/n) [y]:`. **Accept.** Resuming keeps the original
  identity; already-done turns are skipped; a mid-flight condition is *not*
  re-wiped/re-seeded (the graph context its completed turns built is
  preserved) and a mid-flight `random_weight` series is *not* re-randomized.
  Fully-completed conditions are skipped entirely.
- **Declining a resume, or re-running an already-complete plan:** the harness
  bumps to a new attempt identity (`{plan_id}_attempt2`, …) rather than reusing
  one that already has data. Nothing is overwritten — but note the new
  `experiment_id`s when you go looking for reports.
- **Never hand-edit** `harness_runs/{run_id}/checkpoint.json` to force
  behavior; the identity rules exist because `wipe_graph()` never deletes
  analytics/feedback_events/probe_results.

### Error policy (what soft-continues vs. aborts)

| Category | Default | Covers |
|---|---|---|
| `connectivity` | `hard_stop` | Backend unreachable / DB failure |
| `turn_ambiguous` | `soft_continue` | One turn's result malformed/missing |
| `judge_unparseable` | `record_none` | No usable **verdict** after one re-ask (turn completes; a `none` verdict is recorded) |
| `report_generation` | `soft_continue` | A report call failing — reports are re-generatable later; a nearly-finished run is not |

Override per-category in the plan's `error_policy`. Some stops are
**unconditional** and cannot be overridden: budget exceeded, missing probe
battery, missing judge model, and Docker restart failure.

##### `record_none` (judge_unparseable only)

`judge_unparseable` is the one category that accepts a third action, and it is
the default as of 2026-08-06. Instead of recording nothing, the turn stores a
`none` verdict with `judge_source="model:<name>:unparseable"` and a rationale
naming the parse failure.

The reason is that a dropped verdict is **invisible**: nothing in the data
distinguishes it from a turn that never ran, so the denominator quietly
shrinks. The 2026-08-06 calibration lost 1 turn in 20 that way, which is ~50
turns across exp0's 1,050.

Two properties make this safe rather than a thumb on the scale:

- **Learning is unaffected.** A `none` verdict triggers no weight update —
  exactly what recording nothing did. The change is to the record, not to the
  experiment.
- **The rows stay separable.** The `:unparseable` suffix means these are never
  silently pooled with genuine partial grades, while still being
  model-attributed (an unattended run is precisely what they describe).

In `--interactive`, an explicit operator skip still wins: the policy exists so
an *unattended* run does not lose the turn, not to overrule someone who was
asked and declined. Use `soft_continue` to restore the old drop-it behavior.

#### Judge protocol: the quote-bearing per-criterion checklist (M31 + M37)

The judge does not assert `up`/`down`/`none` itself. For each of the prompt's
criteria it returns one checklist entry — the exact words it copied from the
response as evidence, then `true`/`false` — plus a `rationale`:

```json
{"checklist": [{"quote": "the applicable rule is L-2", "met": true}],
 "rationale": "..."}
```

The quote is requested **before** the boolean on purpose: a small model that
has to produce its evidence first cannot arrive at the judgment by vibe, and
the quote is what makes the claim checkable afterwards — by the host and by
you during calibration.

The harness then **derives** the verdict host-side:

- **All criteria met → `up`.**
- **No criteria met → `down`.** `criterion_triggered` is set to the *first*
  listed criterion — since every criterion failed, which one is cited is
  uninformative by construction (the rationale carries the real detail); this
  just satisfies the backend's non-empty-criterion contract for `down`
  without inventing or guessing anything.
- **Some but not all met → `none`.** A partial answer gets no feedback — no
  weight update — matching the backend's existing "absence of signal is an
  explicit observation" contract. This replaced an earlier any-criterion-
  unmet-means-`down` rule that produced a punishment-dominated ~34% up-rate
  against exp0's fairly demanding criteria; per-criterion grading is also an
  easier task for a small local judge model than applying a global rule in
  one shot.

Because the verdict is derived from a fixed-length checklist rather than a
free-text citation, there is no "unresolved criterion" failure mode anymore:
`judge_unparseable` covers every parse failure (malformed JSON, a
missing/wrong-length/non-bool checklist, or an HTTP failure), and every one
of them is direction-neutral — unlike the pre-Milestone-31 protocol, no
failure mode can silently delete only `down` verdicts.

##### Criterion families: `[EXACT]` vs `[CONCEPT]` (Milestone 37)

The bank writes criteria in two styles, and one grading rule cannot serve
both. Each criterion is tagged in the judge prompt and held to its own
standard:

| Family | Looks like | Standard given to the judge |
|---|---|---|
| `[EXACT]` | `answer: L-2`, `answer: UTC (Universal Coordinated Time)` | The response must state that answer itself. A different code, label, or term does not count, "even if you believe it means the same thing." |
| `[CONCEPT]` | `mentions consistent/fixed field ordering` | The idea may be worded differently, but it must actually be stated — not hinted at or implied. |

An `answer:` criterion whose answer is a whole clause (`answer: all claims
must remain fully auditable`) is classified `[CONCEPT]`: there is no token to
match literally. Slash-separated alternatives and parentheticals are each
accepted variants (`local temporal readings/local time` → either;
`UTC (Universal Coordinated Time)` → either).

This split is why the fix is not simply "demand literal matches everywhere."
The `mentions ...` criteria are three quarters of the bank and are
deliberately paraphrase-tolerant; holding them to a literal standard would
trade a lenient judge for an over-strict one rather than calibrating it.

**Known limitation.** The tag is derived from the `answer:` prefix alone, so a
`[CONCEPT]` criterion carrying specific terminology (`states fields must
appear in declared **spine** order`) is still graded paraphrase-tolerantly,
and the judge will accept a generic restatement of it. That is where the
residual leniency measured on 2026-08-06 lives — see "Naming a subject is not
stating a fact about it" below.

##### The topical-mention rule (2026-08-06)

The 2026-08-06 recalibration found the judge's three lenient calls were all
one move: **the quote named the criterion's subject without asserting its
content.** `"K-1: Addresses field order."` was credited for *states fields
must appear in the fixed declared order*.

The evidence checks below cannot catch this — those quotes really were in the
response, they simply did not support the criterion — so the judge prompt
carries two extra rules and a worked example:

- Naming a subject is not stating a fact about it. A quote that only points at
  what the criterion is about (a rule number, a heading, a label, a sentence
  saying the subject matters) does not meet it.
- The quote **by itself** must satisfy the criterion; nothing else in the
  response may fill in what it leaves out.

The worked example is deliberately in a subject the bank never touches. An
example drawn from the Kestrel fixtures would prime the judge on the very
turns it grades.

Measured against the real `gemma4:e4b` on the 20 calibration turns, scored
against the operator's regrade: **3 lenient / 0 strict → 1 lenient / 1
strict** (stable across 3 runs; the pre-change prompt reproduces the recorded
pass exactly). **This reduced the bias, it did not remove it** — and the
sample is far too small to read 1-and-1 as balanced.

##### Host-side evidence checks (Milestone 37)

| Check | Effect | Why that effect |
|---|---|---|
| `met: true` with `"quote": ""` | **Forced to `false`** | The protocol tells the judge an empty quote requires `met: false`, so this is the checklist contradicting itself — resolved the only way the protocol allows, not second-guessed |
| Quote not found in the response | **Flag only** | A heuristic over normalized text; a judge may legitimately stitch clauses together |
| `[EXACT]` criterion met, required answer absent from the response | **Flag only** | Also a heuristic (`minute-level precision` vs. a response saying `minute precision`) |

Flags never change a verdict — silently reclassifying on a heuristic would
corrupt the record the run exists to produce. They surface in `--interactive`
and are counted in the calibration summary, which is what will say whether
either is worth promoting into enforcement.

**On 2026-08-06 that question got a real answer: do not enforce them.** One
flag fired across the whole pass, on a criterion the operator confirmed *met*
— promoting it would have forced the checklist `[True, False]` → `[False,
False]` and flipped that turn from `none` to `down`. It also caught none of
the pass's three genuine (lenient) errors. The flags are aimed at fabricated
quotes; the actual failure mode was real quotes that did not support the
criterion, which is a grading-standard problem, not a verification one.

One bailout: if the judge **omits** the `quote` key entirely while still
marking criteria met, enforcement is skipped for that turn and
`evidence_enforced` records it. A 4B model dropping an output field is a
protocol miss, not a response that failed everything, and demoting the whole
checklist would manufacture a `down` out of a formatting failure. Supplying
`"quote": ""` is *not* this case — that is the model answering the field.

The checklist is recorded on the checkpoint whenever a model verdict was
parsed, whether or not you later correct it: `criteria_met` (final),
`judge_model_criteria_met` (the judge's own, before any regrade),
`criteria_evidence`, `evidence_flags`, and `evidence_enforced`. These are
harness-side only — no backend schema change.

#### Unparseable judge verdicts in `--interactive`

When the judge produces no verdict at all during a calibration run, you are
shown the full prompt/criteria/response context and offered
`Grade this turn yourself, or skip it? [g/skip]`. Grading it posts with
`judge_source="human"` and leaves `judge_model_verdict` null (there was no
model verdict). Skipping soft-continues as before — an explicit operator skip
is honored even under `record_none`, which exists so an *unattended* run does
not lose the turn, not to overrule someone who was asked. Under
`judge_unparseable: hard_stop` the run aborts without prompting.

Both paths mark `judge_error` (`judge_unparseable (hand-graded)` /
`judge_unparseable (recorded none)`) so the calibration summary can bucket
them. Without that marker they are indistinguishable from an ordinary graded
turn — every checklist field is `None` on all three — which is why the
2026-08-06 summary accounted for only 19 of its 20 turns.

---

## 9. After the run — where things are and what to verify

### Artifact locations

| Artifact | Location |
|---|---|
| Manifest, checkpoint, notification log | `harness_runs/{run_id}/` (run_id = `{plan_id}_{config_hash_prefix}`) |
| Series + experiment reports (MD + JSON) | Backend data dir (`/app/data/reports/` in-container); browsable via Admin UI → Reports, or `GET /admin/reports` |
| Analytics, feedback events, probe results, edge-update events | `memory/graph.db` (survive wipes by design) |
| Pre-wipe graph archives | Written by `wipe_graph()` per condition |

### Post-run verification checklist

1. Exit code 0 and `COMPLETE: <expected turn count>`.
2. Checkpoint has 0 (or an explainable number of) `soft_continue` turns:
   turns, verdicts, `judge_error`s are all in
   `harness_runs/{run_id}/checkpoint.json`.
3. Experiment report **§6.3 integrity checks**: all 3 PASS
   (`seed_fixture_provenance`, `graph_wipe_verification`,
   `probe_replay_index_normalization`).
4. **§6.2 control sanity checks**: all 4 PASS (static: zero weight changes,
   zero feedback-edge correlation; random: weights re-randomized above the
   variance floor, zero correlation). Comparative deltas are meaningless
   unless these pass.
5. **§6.1**: `probe_count` matches the battery you ran; no
   `insufficient_data` noise rows.
6. Learning fired only where it should: edge-update events exist for the
   learned experiment_id and are zero for both controls.
7. Probe bookkeeping: `probe_results` rows = conditions × sessions × battery
   size, `measurement_session_index` contiguous 1..N per condition.

### Regenerating reports

Reports can be regenerated after the fact (e.g. after a report-layer bugfix):

```
POST http://localhost:8000/admin/reports/generate-experiment
Body: {
  "experiment_id": "<plan_id>_learned",
  "control_experiment_ids": {
    "static_weight": "<plan_id>_static_weight",
    "random_weight": "<plan_id>_random_weight"
  }
}
```

Post-hoc §5 sections are served from the series report JSONs persisted at
series close (the authoritative snapshots) — the live graph only holds the
*last* condition's MUs, so recomputation from it is only trustworthy at series
close. This is handled automatically; just don't be surprised that the stored
snapshots, not the current graph, are the source of truth.

### Update the docs

Log the run in `PROGRESS.md`: plan file, run_id, turn counts, integrity/sanity
outcomes, report paths, and anything the run taught you.

---

## 10. Designing for measurable learning (science notes)

Lessons from the 2026-07-06 real-model pilot that affect how you set up the
*next* full run:

- **Probe headroom.** If gold MUs already sit at rank ≤4 from turn 1 under
  plain BM25 + seed graph, edge learning has no room to show up — probe rank
  trajectories flatline in *every* condition and the §6.2 deltas are genuinely
  zero. Before a run whose point is detecting learning, check the baseline:
  prefer probes with hop ≥ 1 gold targets that BM25 alone cannot surface.
- **Judge verdict balance.** The stub pilot's verdicts were balanced; real
  gemma4 skewed heavily "up" (~71 up / 5 down in learned). A one-sided verdict
  stream weakens the learning signal — if you see this in the first series,
  consider whether the bank's criteria discriminate hard enough.
- **Run all three conditions in one harness invocation.** The automatic
  `control_experiment_ids` assembly, §6.2 shadow, and identity bookkeeping all
  assume it. Splitting conditions across separate invocations of different
  plans forfeits that wiring.

---

## 11. Troubleshooting

| Symptom | Cause / fix |
|---|---|
| `plan validation failed: - <field>: ...` | Fix the named field. Common: only one of the three size fields given; `condition_order` not a permutation of `conditions` |
| `pre-run estimated cost $X exceeds budget_cap $Y` | Raise/remove `budget_cap`, or point at an existing bank so generation cost is $0 |
| `probe battery required: ...` | `probes/probe_set_{version}.yaml` missing or empty for the plan's `probe_set_version` |
| `judge model unavailable: 'gemma4:e4b' not found` | `ollama pull gemma4:e4b`; confirm `--judge-host` points at the right Ollama |
| `ANTHROPIC_API_KEY is not set` | Only needed for bank generation; set it host-side or reuse an existing bank |
| `docker restart failed for condition ...` | Docker Desktop down, or compose project broken. Fix and relaunch; resume the checkpoint |
| Hard stop mid-run on `connectivity` | Backend container died — `docker compose logs backend`. Relaunch and resume |
| Judge verdicts all `judge_unparseable` | Judge model responding off-format — verify the model tag; check with a dry run `--interactive` |
| Judge verdicts mostly `none` | Expected under the checklist protocol whenever answers are frequently partial — check the checklists on individual turns (`--interactive`) before assuming a bug; a large `none` share was the expected side effect of fixing exp0's punishment-dominated regime |
| `judge_unparseable` citing a wrong-length or non-bool checklist | The judge model isn't following the checklist schema (wrong number of entries, or a non-boolean `met`). Verify the model tag with a dry run `--interactive`; a smaller/older model may need a different prompt or model choice |
| Many turns with `evidence_enforced: false` | The judge is omitting the `quote` key, so the M37 evidence checks are being skipped wholesale and the up-skew they correct is back. Check a turn's raw checklist with `--interactive`; this is the signal that the model is not following the M37 schema even though the checklist itself parses |
| Calibration summary shows a large lenient split | The judge is crediting criteria on a looser standard than they state — the 2026-08-04 failure mode. Look at the flagged criteria first; they are pre-sorted candidates |
| Report `§6.x` numbers look contaminated across attempts/dry runs | Should be impossible post-M21 (`experiment_id` scoping + `_dryrun` suffix). If seen, check for pre-M21 legacy rows in the DB and report it as a bug |
| All conditions share one `experiment_id` | You ran multi-condition with `--no-docker-restart`. The run is not salvageable — re-run without the flag |
| Queue: `duplicate config_hash across plans ... replicates need distinct random_seed values` | Two enabled plans in the queue are identical in every field the hash covers. Give each replicate its own `random_seed` |
| Queue: `plan ... needs fresh Sonnet prompt-bank generation` | A queued plan has no resolvable bank. Generate it interactively first (`--generate-bank`), then set `prompt_bank_path` — the queue refuses to spend on API calls unattended |
| Queue: `was launched from a different queue file (queue_content_hash mismatch)` | The queue file changed since that launch directory was created. Start a fresh launch, or `--resume-dir` the correct one. Never edit a queue file mid-flight |
| Queue halts after one plan despite `on_plan_failure: "continue"` | The post-failure backend health recheck failed, so the remainder was halted deliberately rather than failing identically against a dead backend. Check `docker compose logs backend` |
| Two configs' results look identical / a sweep shows no effect | **Check `full_config_snapshot` on a run row from each experiment first.** For a plan predating M43 (no `expected_config`), the likeliest cause is a `config.yaml` edit that was never made or never applied. A plan with `expected_config` set would have hard-stopped instead of running silently — if it didn't, the config really was live; look elsewhere. See §7 |
| `expected_config mismatch — key: expected=X actual=Y` | The live backend (`GET /admin/effective-config`) disagrees with the plan's declared config. Either `config.yaml` was never edited, or it was edited but the backend was never restarted — the M43 Phase 1 assertion exists specifically to catch the second case, which a plain `GET /admin/config` re-read would miss |
| `expected_config mismatch ... actual=<missing>` | The plan declares a dotted key that doesn't exist in the live `config.yaml` at all — a typo, or a key renamed since the plan was written |

---

## Related documents

- `docs/experiment_protocol.md` — the manual, API-call-level protocol the
  harness automates; useful for understanding what each phase does and for
  one-off manual interventions (snapshots, single probes, SQL sanity checks).
- `BUILD-PLAN.md` — milestone rationale for the harness design (M19–M21).
- `PROGRESS.md` — run history, bugs found in past runs, and their fixes.
