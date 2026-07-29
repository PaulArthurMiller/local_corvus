# Local Corvus — Experiment Harness Usage Guide

This is the operator's guide for running a full-sized, three-condition experiment
through the automated harness (`scripts/run_experiment_harness.py`). The harness
automates everything `docs/experiment_protocol.md` describes as manual API calls:
per-condition backend restarts, graph wipe/seed, turn execution, probe batteries,
judging, verdict posting, checkpointing, budget enforcement, and report generation.

Read this document top to bottom before your first full run. After that, the
**Quick Start** section is all you need.

---

## Quick Start (for operators who have run one before)

```powershell
# 1. Stack up, Ollama serving, tests green
docker compose up -d --build
pytest

# 2. Dry-run the plan (2 turns, 1 condition, isolated _dryrun identity)
python scripts/run_experiment_harness.py --plan plans/my_plan.json --dry-run

# 3. Full run
python scripts/run_experiment_harness.py --plan plans/my_plan.json
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
   pytest
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
| `error_policy` | Overrides per error category | See §7. Empty `{}` = defaults, which are correct for routine runs |
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
judged bulk turns.) `--interactive` shows each verdict + rationale and lets you
accept (`a`), override (`o` — recorded with `judge_source: "human"`), or `skip`.
This is calibration mode, not routine use — never use it on the real run.

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
| `--interactive` | Per-verdict accept/override prompts (calibration only) |
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

## 7. Interruption, resume, and re-run semantics

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
| `judge_unparseable` | `soft_continue` | No usable **verdict** after one re-ask (turn completes, no verdict recorded) |
| `report_generation` | `soft_continue` | A report call failing — reports are re-generatable later; a nearly-finished run is not |

Override per-category in the plan's `error_policy`. Some stops are
**unconditional** and cannot be overridden: budget exceeded, missing probe
battery, missing judge model, and Docker restart failure.

#### Judge verdicts vs. criterion citations

`judge_unparseable` covers only failures that leave **no verdict at all**.
A judge that returns a perfectly good `down` but cites a criterion that
matches none of the prompt's is handled separately, because that failure mode
is *directional*: only a `down` carries a citation, so treating it as
unparseable discarded `down` verdicts at a rate `up` verdicts could never
suffer — which biases exactly the up-skew a calibration run measures.

The citation is resolved in three steps, most to least certain:

1. Exact match against the prompt's criteria.
2. Normalized match — case, surrounding whitespace, markdown emphasis
   (`**X**`), quotes, trailing punctuation, and leading label prefixes
   (`answer:`, `criterion:`, `expected:`, …) are ignored. The colon is
   required, so a criterion like `states three` is never truncated.
3. Single-criterion prompts: a `down` can only mean that one criterion.

Only if all three fail is the citation unresolved, and even then the verdict
survives:

- **`--interactive`**: you are shown the judge's raw citation and asked which
  criterion the response failed; the verdict posts normally.
- **Otherwise**: the verdict is not posted (the backend rejects a `down` with
  no criterion, and guessing one would corrupt per-criterion analysis), but the
  turn's `judge_model_verdict` still records `down` in the checkpoint, so the
  turn stays countable. Never fuzzy-matched.

The resolved value is always the **bank's exact criterion text**, never the
judge's paraphrase — §6 groups failures by that string.

#### Unparseable judge verdicts in `--interactive`

When the judge produces no verdict at all during a calibration run, you are
shown the full prompt/criteria/response context and offered
`Grade this turn yourself, or skip it? [g/skip]`. Grading it posts with
`judge_source="human"` and leaves `judge_model_verdict` null (there was no
model verdict). Skipping soft-continues as before. Under
`judge_unparseable: hard_stop` the run aborts without prompting.

---

## 8. After the run — where things are and what to verify

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

## 9. Designing for measurable learning (science notes)

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

## 10. Troubleshooting

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
| `criterion_unresolved` on several turns | The judge is citing criteria it can't match. Re-run `--interactive` to grade those turns by hand, and check whether the bank's criteria are phrased in a way the judge paraphrases (long criteria, embedded punctuation) |
| Report `§6.x` numbers look contaminated across attempts/dry runs | Should be impossible post-M21 (`experiment_id` scoping + `_dryrun` suffix). If seen, check for pre-M21 legacy rows in the DB and report it as a bug |
| All conditions share one `experiment_id` | You ran multi-condition with `--no-docker-restart`. The run is not salvageable — re-run without the flag |

---

## Related documents

- `docs/experiment_protocol.md` — the manual, API-call-level protocol the
  harness automates; useful for understanding what each phase does and for
  one-off manual interventions (snapshots, single probes, SQL sanity checks).
- `BUILD-PLAN.md` — milestone rationale for the harness design (M19–M21).
- `PROGRESS.md` — run history, bugs found in past runs, and their fixes.
