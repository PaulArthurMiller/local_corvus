# Local Corvus — Phase 1 Experiment Program

**Status:** living document · **Owner:** Paul · **Consumers:** Paul, CC, and
(once built) the analyst and theorizer agents.

This is the checklist for finishing Phase 1. It sits *above*
`experiment_protocol.md` (the Run Plan / Queue schema reference) and
`sensitivity_baseline.md` (mechanism facts). It says what rounds run, in what
order, what "done" means for each, and what gets written down afterward. If a
round is not on this list, it does not run until it is added here with a
rationale.

Any agent prompted with this document must (a) treat §1 as hard constraints,
(b) work only on the round marked 🟡 in §3, and (c) append to §6 before
proposing anything for the next round. Its Phase 2 successor will be a new
file, not an edit of this one.

Status key: ✅ complete · 🟡 active · ⬜ not started · ⛔ blocked (blocker named)

---

## 1. Standing invariants (do not relax without a §7 entry explaining why)

1. **Controls.** Every plan runs `learned` against at least `static_weight`,
   graph wiped and re-seeded between conditions, never shared. `random_weight`
   is not run by default from M40 onward (rationale: BUILD-PLAN.md M40 — it
   twice demonstrated that feedback-uncorrelated weight motion produces no
   coherent trajectory, and costs ~1/3 of wall clock). It remains fully
   supported. **Re-attach rule:** any R3 variation classified *distinguishable*
   in the positive direction gets one three-condition confirmation replicate
   before it enters the R4 survivor set. The theorizer may not promote a
   positive without this.
2. **Probe turns are read-only.** Measured, never judged, never reinforced.
   Harness-enforced; `control_shadow` `zero_weight_changes` must pass.
3. **One factor per plan.** A plan differs from the Round 1 baseline by exactly
   the keys in `config_overrides`, and `expected_config` asserts those same
   keys against the live backend before any mutation.
   `analytics.full_config_snapshot` on every run row is the provenance record.
4. **Frozen inputs for all of Phase 1.** `seed_v3` fixture, `probe_set_v3`,
   the same prompt bank (`prompt_bank_e2adf796…`), `trained_spokes` as in
   exp01, judge `gemma4:e4b` at temperature 0.0. Nothing generated per run.
5. **Replication before belief.** No config difference is reported as real
   until the Round 1 noise floor (§3, R1-b) exists and the difference exceeds
   it. Replicates use distinct `plan_id` (this alone changes the Thompson
   stream — see §2) and, unless a paired design is explicitly intended,
   distinct `random_seed`.
6. **Pre-registration.** The variation list in R3 is fixed before R3 starts.
   Additions go in §7 with a rationale and a date; they do not silently appear
   in a queue.
7. **`num_ctx` pinned.** `OLLAMA_CONTEXT_LENGTH=8192` on the host, matching
   `model.context_window_tokens`. Verify `n_ctx` in `server.log` after any
   Ollama restart (config.yaml notes the silent-truncation failure mode).
8. **Rationale travels with the idea.** Every finding, dead knob, or dropped
   variation gets a `sensitivity_baseline.md` entry naming the script/data it
   came from.
9. **Smoketests ≠ experiments.** A `dry_run` plan never contributes a data
   point. Its `_dryrun` suffix keeps it out of the archive.

---

## 2. Sources of run-to-run variance and how each is controlled

Know these before reading any two runs as "different."

| Source | Controlled by | Note |
|---|---|---|
| Probe placement, prompt-id assignment | Run Plan `random_seed` | Same seed → same turn/probe sequence. |
| Thompson draws on training turns | `edge_update.rng_seed: null` → derived from `EXPERIMENT_ID` = `{plan_id}_{condition}` | **A new `plan_id` is a new Thompson stream even when `random_seed` is reused.** exp01's "quasi-paired" reuse of rep2's seed pairs the turn sequence only. To truly pair, set `edge_update.rng_seed` explicitly via `config_overrides` — decision pending, §7-3. |
| Probe sample-battery draws | Separate RNG stream in `run_probe()`; `sample_battery_repeats: 25` | Never perturbs training-turn reproducibility. |
| Agent (Gemma) generation | Not pinned in `config.yaml` | Ollama generation at nonzero temperature is a variance source on every bulk turn's output MU content and therefore on co-activation edges. **Action R0-4:** record what temperature the orchestrator actually sends. |
| Judge verdicts | `judge.temperature: 0.0` | Near-deterministic; residual Ollama nondeterminism possible. |
| Seed fixture, prompt bank | Frozen (§1-4) | Hash-checked at preflight. |

---

## 3. Rounds

### R0 — Reporting review 🟡 (CC; gates everything below)

Purpose: the analyst agent will read reports without a human correcting for
known defects, so the fields it consumes must be right *now*.

- [ ] R0-1 CC audits every field the cross-run comparison will consume
      (the 4-bucket partition counts, `gold_mu_rank`, anchor-strength
      trajectories per trained spoke, terminal-seed yield, K2 runner-up rate,
      `control_shadow` sanity checks) against raw `probe_results` /
      `analytics` rows for one archived run. Cross-validation like the
      REACHED-vs-`gold_mu_rank` match in M45 Phase 2 is the model.
- [ ] R0-2 Confirm each run row records: `plan_id`, `random_seed`, the
      *resolved* `edge_update.rng_seed` (not `null`), prompt-bank hash,
      fixture version, `full_config_snapshot`, harness + backend git SHA.
- [ ] R0-3 Confirm the prompt bank is loaded from `prompt_bank_path` and never
      regenerated in queue mode (protocol says so; verify the code path).
- [ ] R0-4 Record the agent-model sampling parameters actually sent to Ollama
      on bulk turns (§2). Pin them in `config.yaml` if not already.
- [ ] R0-5 `c_spoke1` anomaly (sensitivity F4): check whether the prompt bank /
      probe content for `cluster_c` can ever surface `c_spoke1` on a real
      turn. Outcome goes in `sensitivity_baseline.md` either way — content
      bug or mechanism finding.
- [ ] R0-6 Errata in §8 applied to `experiment_protocol.md`.

**Done when:** R0-1 through R0-6 checked, and `sensitivity_baseline.md` has
an entry for R0-5.

### R1 — Baseline noise floor ✅ runs / ⬜ analysis

Runs exist: baseline ×3 (`prior 1.0/1.0`, `weight_mode: sample`,
`retrieval_level2_top_k: 1`). What is missing is the analysis.

- [ ] R1-a Per-metric mean and SD across the three baseline learned runs, for
      every metric in §5, at the whole-run level and per session where the
      metric is trended.
- [ ] R1-b Same for `static_weight`, and for the learned–static delta (the
      quantity R3 actually compares).
- [ ] R1-c Entry in `sensitivity_baseline.md`: "Round 1 noise floor," with
      the script path so it can be re-run when more baselines exist.
- [ ] R1-d Decide the distinguishability rule (§5) using these numbers.

**Done when:** R1-c and R1-d exist. Three replicates is thin; if SDs are
large relative to the effects in cfgA, queue baseline rep4–rep5 before R3.

### R2 — Round 2 configs (cfgA ✅ runs, cfgB ⛔)

- cfgA (`seed_prior 3.0/3.0`) ×3: runs complete. Analysis against the R1 floor
  after R1 is done. Note the quasi-pairing caveat in §2 when reading rep1.
- cfgB (`retrieval_level2_top_k: 2`): the override never reached the probe
  path (M44). The two runs are **not valid cfgB data**. Disposition (§7-2):
  either re-run under the fixed wiring as part of R3's K2 sweep, or archive
  as "invalid — override not applied."

**Done when:** cfgA has a `sensitivity_baseline.md` entry stating effect vs.
noise floor, and cfgB's disposition is recorded.

### R3 — Pre-registered one-factor sweep ⬜ (blocked on R0, R1)

Each variation ×2 replicates (distinct `plan_id`, distinct `random_seed`);
a third replicate only if the delta lands inside the noise band and the knob
is one the theorizer will care about. Baseline reference is R1.

| # | Knob | Values | Why (from sensitivity findings) |
|---|---|---|---|
| V1 | `retrieval_level2_top_k` | 3, 5 | F3 priced it retrospectively at ~5× REACHED; this is the confirming run, and it retires cfgB. |
| V2 | `credit_lambda` | 0.3, 0.9 | Write side near-frozen (~6% updated); path credit depth is the cheapest write-side lever. |
| V3 | `credit_scale` | 3.0 | Learning-rate knob; equivalence to counter discounting means this stands in for decay until `decay` is implemented. |
| V4 | `negative_scope` | `terminal` | Tests whether path-wide negative credit is suppressing the stepping stones F1 says are present. |
| V5 | `evidence_cap.enabled` | `true`, `n_max: 50` | Constant-effective-learning-rate variant; keeps late-session priors movable. |
| V6 | K1 `edge_weight_sum` centrality term | flagged off (needs a config knob — CC) | Standing finding: undirected degree rewards conv-MU accumulation independent of feedback. Must be a flag, not a deletion. |

Not in R3, with rationale: `weight_mode: mean` (already characterized as a
structural null for seed_v3); `decay.*` (not implemented; backend refuses
`enabled: true`); `retrieval_level1_top_k` (interacts with pass-rank
thresholds — a Phase 2 item once thresholds are revisited).

Queue shape: 6 variations × 2 reps = 12 plans; at 2–3 h each that is four to
five nights. Order the queue so V1 runs first (it is the one most likely to
change what the rest mean).

**Done when:** each V has a `sensitivity_baseline.md` entry classifying it as
*distinguishable* / *indistinguishable* / *harmful* against the R1 floor;
every positive *distinguishable* V has a three-condition confirmation
replicate (§1-1) whose `random_weight` arm shows no coherent trajectory; and
the surviving set for R4 is named in §6.

### R4 — Fixture generalization ⬜ (blocked on R3)

New fixture `seed_v4` (topology and vocabulary changed, convention families
regenerated; prompt bank generated once via Sonnet, then frozen). Run baseline
×2 and each R3 survivor ×2. Question: which R3 effects hold on a corpus the
configs were not tuned against. Cross-cluster bridge-MU rank movement is the
primary instrument here.

**Done when:** each survivor is marked *generalizes* / *fixture-specific*.

### R5 — Phase 1 closeout ⬜

- [ ] Learning-signal markers: the short list of metrics that moved with
      config in a direction that survived R1 noise and R4 fixture change.
- [ ] Draft the theorizer's ranking rule (§5) from those markers.
- [ ] Write `phase2_experiment_program.md`; this file becomes read-only.

---

## 4. Per-plan checklist

Pre-launch (all of):
- [ ] `python -m pytest tests/ --ignore=tests/integration` passes in full
- [ ] `n_ctx` verified at 8192 in Ollama `server.log`
- [ ] `GET /health` → `db: ok`
- [ ] Plan's `config_overrides` and `expected_config` are identical key sets
      and differ from baseline by exactly the pre-registered knob
- [ ] `prompt_bank_path` set and hash matches the frozen Phase 1 bank
- [ ] `seed_fixture_version` / `probe_set_version` both `v3`
- [ ] Queue `--dry-run` passes preflight with zero mutation
- [ ] Free disk ≥ (plans × conditions × ~archive size) — note the WARN threshold

Post-run (all of):
- [ ] `queue_summary.md` shows every plan `complete`; no `soft_continue`
      counts above the R1 baseline's own counts
- [ ] `control_shadow` sanity checks pass for every non-learned condition
- [ ] Series and experiment reports present for every condition
- [ ] Sensitivity entry written (even for "no effect")
- [ ] §6 updated

---

## 5. Metrics and the comparison rule

Composite structure as adopted (2026-08-26):

- **Headline:** 4-bucket path-back partition of probe turns
  (REACHED / OUTBID / OUT_OF_DEPTH / DISCONNECTED), `battery_mode="mean"`,
  reported as learned – static delta per bucket.
- **Write-side instrument:** anchor strength — hub-edge (alpha, beta)
  trajectory per trained spoke; fraction of created edges ever updated.
- **Conditional yields:** terminal-seed chase-down yield (COUNTERFACTUAL
  until `retrieve()` performs the walk); gold-as-K2-runner-up rate.
- **Generalization (R4 only):** bridge-MU rank movement.

**Distinguishability rule (draft, finalize at R1-d):** a variation is
*distinguishable* if the mean learned–static delta on the headline REACHED
rate differs from baseline by more than 2 × baseline SD, *and* the sign is
consistent across replicates. Everything else is *indistinguishable* and is
still recorded.

**Theorizer ranking rule:** not yet defined. It must be a written ordering
over the metrics above before any agent is allowed to propose R3+ changes.
Draft at R5, informed by which metrics actually moved.

---

## 6. Progress log (append-only, newest first)

- **2026-08-26 (later)** — §7-1 resolved from BUILD-PLAN M40; re-attach
  rule added to §1-1 and R3 done-when. V6 knob milestone requested. §5
  distinguishability rule left as draft until the theorizer is built.
- **2026-08-26** — Document created. R0 opened (CC reporting review). R1 runs
  exist, analysis not started. cfgA ×3 complete, cfgB ×2 flagged invalid
  (M44). R3 variation list pre-registered as V1–V6.

---

## 7. Decisions and open questions (dated; resolve in place)

1. **`random_weight` dropped from Round 1+ plans.** *Resolved 2026-08-26.*
   Deliberate, per BUILD-PLAN.md M40 (drafted 2026-08-09) and restated in the
   M41 scope notes: exp0 and exp0b both retain the full three-condition
   record; the control had shown twice that feedback-uncorrelated weight
   motion yields no coherent trajectory, and it costs ~1/3 of each run.
   Kept as a confirmation arm — now codified as the re-attach rule in §1-1.
   `experiment_protocol.md` still documents three conditions as the standard;
   §8 asks it to cross-reference this.
1a. **V6 knob (K1 centrality term flag).** Planner to add as a milestone
   before R3 opens. R3 cannot start with V6 in the queue until it lands;
   V1–V5 may run ahead of it if needed.
2. **cfgB disposition.** Re-run under fixed wiring as R3-V1, or archive as
   invalid. Recommendation: archive; V1 supersedes it. *Owner: Paul, after
   R1.*
3. **Paired vs. independent replicates.** If paired comparisons are wanted,
   `edge_update.rng_seed` must be set explicitly per plan pair via
   `config_overrides`. Otherwise stop describing seed reuse as pairing.
   *Owner: Paul.*
4. **`series_count` 5 vs. 15.** Round 1/2 plans use 5 series × 70 turns;
   Experiment 0 used 15. The R1 noise floor is therefore a 5-series floor.
   Keep 5 for all of Phase 1 for comparability; note it in R1-c. *Decided
   2026-08-26 unless objected.*
5. **`c_spoke1` never retrieved.** R0-5. Content bug or mechanism? *Owner: CC.*
6. **Agent sampling parameters unpinned.** R0-4. *Owner: CC.*

---

## 8. Errata for `experiment_protocol.md` (CC to sync against `schema.py`)

The schema reference predates several milestones. Update these so the two
documents do not contradict each other:

- **Hyperparameter variation per plan.** The doc says sweeps are "one
  `config.yaml` value per plan run in isolation" and that `tuning_knobs` is
  inert. As of M43 Phase 2, `config_overrides` supplies per-plan config and
  `expected_config` asserts it against the live backend. Document both
  fields, their types, and the assertion failure behavior. Retire or
  re-scope `tuning_knobs`.
- **`seed_fixture_version`** is a plan field in use; it is absent from the
  schema table.
- **v3 wiring.** The "Seed fixture and probe battery version" section says v3
  is not wired and plans must stay on v2. It is wired; Phase 1 plans use v3
  for both fixture and probe set. Rewrite the section.
- **`random_seed` description.** Add that it does *not* seed Thompson draws;
  those come from `edge_update.rng_seed`, derived from `EXPERIMENT_ID` when
  null. Add the pairing caveat from §2.
- **Conditions default.** Note that two-condition plans are accepted and
  currently in use, and cross-reference §7-1 of this document.
- **Worked example.** Replace the `pilot_125` example with an exp01-style plan
  showing `config_overrides` / `expected_config` / `seed_fixture_version`.
- **Report field list.** Add a short list of the fields the analyst agent
  consumes (from R0-1) so future report changes know what is load-bearing.