# Reporting and data-suitability audit — 2026-08-26

> **Editorial note (added on salvage, 2026-08-27).** This audit was produced by
> Codex on branch `codex/review-reporting-features-and-data-collection`. That
> branch's *code* changes (runtime provenance in `full_config_snapshot`, a
> top-level `provenance` object in the experiment report, and the Markdown
> header fields) were **not merged** — they were deferred until the autonomous
> analysis loop is actually being built, so that provenance can be designed once
> against the fuller "Still required" list below rather than extended piecemeal.
>
> Read every "**Fixed here:**" in this document as "proposed, not landed."
> The body below is preserved verbatim as written.
>
> Two findings *were* acted on, in the same commit series that added this file:
> the P1 random-control finding and the malformed-JSON half of the P1
> comparability finding. See `backend/analytics/reports/control_shadow.py`.


## Scope and intended decision

This review traces the Phase 1 goal backward from the autonomous analyst's
decision: choose one-factor experiments that improve learned-minus-static
REACHED rate beyond the replicated noise floor, while retaining write-side
and conditional-yield diagnostics. It covers the run plan/harness, analytics
rows, probe rows, series reports, experiment reports, control shadow, and the
offline Alpha C/D reconstructions.

The system has strong turn-level retrieval instrumentation. It does **not yet
produce the Phase 1 decision record as one machine-readable artifact**. The
four-bucket partition, anchor trajectories, terminal-seed yield, and K2
runner-up rate live in `scripts/alpha_d_pathback.py`, not in the generated
experiment report. An autonomous analyst must not infer that the older §6.1
pass rate is the Phase 1 headline.

## Data-flow map

1. `RunPlan` fixes prompt/probe scheduling and declares overrides. The harness
   resolves prompt-bank entries and restarts the backend per condition.
2. The backend resolves config and Thompson RNG state at process start.
   `retrieve()` emits the candidate funnel, scores, traversal path, depths, and
   weights used; `RunRecord` stores those intermediates in `analytics`.
3. Read-only probe calls write `probe_results`, including battery mode, rank,
   and retrieved IDs. Judge feedback and posterior mutations are separately
   recorded in `feedback_events` and `edge_update_events`.
4. Series reports snapshot graph-dependent classifications before later
   condition wipes. Experiment reports aggregate those stored snapshots and
   query durable event/probe tables. Alpha D then reconstructs historical
   graph state to calculate the Phase 1 metrics outside the report pipeline.

## Findings, ranked by autonomous-analysis risk

### P0 — Phase 1 headline metrics are absent from generated reports

The program names the four-bucket path-back partition as the headline, anchor
strength as the write-side instrument, and chase-down/runner-up rates as
conditional yields. These are only emitted by the offline Alpha D script.
Consequences:

- report JSON cannot directly support the registered distinguishability rule;
- an analyst can accidentally optimize the legacy final probe pass rate;
- no schema/version tells a consumer which metric definition is authoritative;
- the counterfactual OUT_OF_DEPTH and DISCONNECTED labels can be presented
  without their necessary counterfactual qualifier if copied downstream.

**Gate:** integrate Alpha D's validated reconstruction into a versioned,
machine-readable cross-run report (or attach its JSON output), including per-run
and per-session numerators, denominators, battery-mode filter, metric-definition
version, and observed/counterfactual status. Do not start autonomous design
until this exists.

### P0 — replicate/noise-floor statistics are not assembled

Reports compare conditions inside one plan. The Phase 1 rule compares a
variation's learned-minus-static delta with the SD of that delta across three
baseline plans. Queue summaries do not assemble this estimand. An analyst would
have to discover compatible reports, validate fixture/probe/bank/config identity,
and implement the grouping correctly.

**Gate:** add a series-of-experiments artifact keyed by fixture hash, probe-set
hash, prompt-bank hash, metric version, factor-under-test, and replicate role.
Report per-replicate paired deltas, mean, sample SD, coverage, and missingness;
never pool probe rows as if they were independent replicates.

### P0 — provenance is split and incomplete in report JSON

Before this audit, `full_config_snapshot.edge_update.rng_seed` retained `null`
when the actual Thompson seed was derived from `EXPERIMENT_ID`. Bulk-turn
sampling parameters are not sent, so behavior depends on the Ollama server and
model defaults, but that fact was not explicit in the report. Experiment
reports also omitted the config snapshot entirely.

**Fixed here:** analytics snapshots add runtime experiment/condition, resolved
edge RNG seed, and the effective Ollama request shape. The provenance explicitly
records an empty sampling-parameter set with source `ollama_server_default`;
this reports the current behavior without changing it. Experiment JSON exposes
these values with the runtime config hash under top-level `provenance`.

**Still required:** carry `plan_id`, attempt, `random_seed`, prompt-bank content
hash, seed-fixture content hash (not only version), probe-set content hash,
harness git SHA, backend git SHA, and report/metric schema versions into the
same provenance object. A filesystem path or mutable version label is not
sufficient evidence of identical inputs.

### P1 — report aggregation can hide missing data

Several aggregators default absent counts/rates to zero. Stored-series fallback
can recompute graph-dependent sections from the live graph even after a wipe;
the warning is logged but the output does not mark that section as degraded.
Integrity checks count `SKIP` as acceptable for `overall_pass`. Thus “zero,”
“not measured,” and “could not reconstruct” may be indistinguishable to a
machine consumer.

**Required:** every metric carries `status`, numerator, denominator, expected
row count, observed row count, missing reason, and source (`stored_snapshot` or
`live_recompute`). `overall_pass` should distinguish PASS, WARN/INCOMPLETE, and
FAIL; a required check's SKIP must never be equivalent to PASS.

### P1 — condition comparability needs stronger checks

The retrieval-geometry check validates only the maximum probe slate size. That
detects the historical K2 wiring failure, but it does not prove every probe used
the intended resolved config, probe set, or expected number of sample repeats.
Likewise, the static-control check treats malformed `weight_updates` JSON as an
empty list, which can turn corrupted evidence into PASS.

**Required:** validate per-row config hash/geometry, exact probe-ID × session ×
battery-repeat coverage, uniqueness, and parse validity. Malformed values are
integrity failures. Include an explicit learned/static input-alignment table.

### P1 — random-control sanity reads mutable present-day edges

`weights_rerandomized` gathers historical activated edge IDs but fetches their
current weights from the live graph. After later wipes/reseeds this can SKIP,
sample a different graph state, or test final weights rather than weights used
on those turns. The correct historical values already exist in each run's
`edge_weights_used`.

**Required:** compute random-weight variance by series and turn from
`analytics.edge_weights_used`; verify the expected re-randomization boundary
and report coverage. Do not use the current `edges` table for a historical
control assertion.

### P1 — rank summaries are selection-biased

Mean gold rank excludes probes where gold is absent (`None`). This can improve
when failures increase because only successful retrievals remain. The four
buckets avoid this for the headline, but legacy comparison fields remain easy
for an autonomous analyst to misuse.

**Required:** deprecate unqualified mean-rank deltas. Always pair conditional
rank with retrieval coverage, or use a declared censored-rank convention.

### P2 — ordering assumptions are implicit

Seed-share drift, first/last comparisons, and trajectory rendering depend on
series order. Preserve and sort by explicit `series_index`; do not rely on DB
or dictionary insertion order or parse it from `series_id`.

### P2 — the intervention-to-metric contract is not encoded

The report does not state the factor under test, baseline value, variation
value, predicted metric direction, primary metric, stopping rule, or promotion
rule. Those remain prose in the experiment program and plan-adjacent notes.

**Required before autonomous design:** extend the plan schema with a registered
analysis contract and copy it unchanged into reports. Reject post-run changes
to primary outcomes. The analyst may propose a plan, but a separate validator
must enforce one-factor scope, frozen inputs, controls, replication, budgets,
and the pre-registered variation allow-list.

## Recommended autonomous-analysis gate

Do not allow an analyst to propose the next experiment unless all are true:

1. Every condition has complete, unique mean-battery coverage and the declared
   number of sample repeats; probe turns have no feedback or graph mutation.
2. Full content hashes and code SHAs match the comparison cohort; the sole
   config difference equals the registered factor.
3. Runtime RNG seeds and the model sampling source are present (including an
   explicit server-default marker when unpinned), and input/prompt schedules
   are aligned as declared.
4. The versioned Phase 1 metrics artifact exists with counts, denominators,
   missingness, and observed/counterfactual labels.
5. Replicate is the unit of inference; the baseline learned-minus-static noise
   floor and variation delta are computed from compatible plans.
6. All required integrity checks PASS. SKIP, parse failure, fallback
   recomputation after wipe, or incomplete coverage blocks promotion.
7. The proposed plan passes a deterministic policy validator before execution.

## Recommended implementation order

1. Promote Alpha D Phase 2 outputs to versioned JSON and generated reports.
2. Add immutable run/report provenance and exact coverage checks.
3. Build the cross-plan replicate/noise-floor report.
4. Add the plan analysis contract and autonomous-plan validator.
5. Only then enable experiment-design suggestions; keep execution approval
   separate until several shadow-mode proposal rounds reproduce human choices.
