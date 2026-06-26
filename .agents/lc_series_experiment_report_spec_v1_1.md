# Local Corvus — Series & Experiment Report Specification (v1)

**Audience:** CC `/planner`, for breaking into build milestones
**Status:** Spec only — no implementation yet
**Depends on:** `lc_instrumentation_audit_v2.md` (data layer this report reads from)

## 1. Purpose

This spec defines two generated report artifacts — the **Series Report** and the **Experiment Report** — that convert already-logged instrumentation into a fixed, repeatable set of answers to the questions that otherwise require a manual graph/log dive every time. It introduces **no new logging requirements**; it is a read-only aggregation layer over data the instrumentation audit already commits to capturing. Anywhere a required field's exact name or location is unconfirmed, that's flagged explicitly rather than assumed.

## 2. Terminology change (project-wide, not just this report)

- **Experiment** — one full run of one of the three conditions (learned / static-weight control / random-weight control) against one wiped, freshly-seeded graph.
- **Series** — a turn sequence sharing one `series_id`. This is what's currently called a "session." No runtime continuity is implied by the term — it is an analysis-time grouping label only.
- **Turn** — one user_input → agent_output exchange; the atomic unit.

**Rationale:** "Session" implies behavioral continuity LC deliberately does not provide right now (session context is gated off; only retrieval surfaces prior content). Reserving "session" for a future iteration that actually carries context forward keeps the vocabulary honest about current behavior. This is a rename only — no behavior change. Apply it everywhere `session_id` currently appears: data layer, logs, UI, this report.

## 3. Data dependencies

This report assumes the following already exist per the instrumentation audit. **Confirm exact field names/locations against CC's ops document before building** — names below are conceptual, not asserted as literal schema:

- MU records carrying `source_type` (user_input / agent_output), creation timestamp, and (post-rename) `series_id`
- A flag/tag on agent_output MUs marking `finish_reason=length`-empty failures
- Per-edge-creation and per-edge-update records (distinct record types), each with weight-at-creation / weight-as-of-update
- A feedback-event log with an FK linking each edge update to its triggering feedback event, with the no-feedback-on-probe-turns rule enforced upstream
- Traversal-chain records per turn: entry point(s), ordered hops, edge IDs per hop, hop depth, pre-ranking candidate set
- Per-probe ground-truth labels with phenomenon-type tags and a bridge-MU flag
- Seed-fixture provenance fields on seeded MUs

**Open item for CC:** confirm whether `series_id` (under either name) is currently stamped on MUs at creation time. If not, that is a prerequisite milestone before any series-scoped grouping in this report is possible.

## 4. Report tiers

- **Series Report** — generated at series close. Scoped to one series within one experiment.
- **Experiment Report** — generated at experiment close. Aggregates all series within that experiment, and separately renders both control experiments in the same structure for direct, side-by-side comparison ("shadowing").

Both emit JSON (machine-consumable, feeds the rollup) plus a rendered Markdown summary (human-readable, surfaced in the admin/analytics layer).

## 5. Series Report — sections

### 5.1 Failure summary
- Empty/failed turns in this series (turn index, cause)
- Count of failed-turn MUs *retrieved into context* by a later turn in this series (the "ghost MU" check — confirms a previously-fixed failure mode isn't resurfacing via retrieval)

### 5.2 Retrieval trace digest
Per turn: query terms, entry point(s), retrieved MU set, which term(s) triggered which node. A flattened, human-scannable rendering of the traversal chain — not the raw chain object.

### 5.3 Intra-series vs. cross-series retrieval rate
Of everything retrieved across this series, the fraction whose source MU was created earlier in *this* series vs. elsewhere (other series, seed fixtures, other experiments). Report as one series-level rate plus a per-turn breakdown.

### 5.4 Cross-series edges
Any edge created or traversed in this series connecting to an MU from a different series — which series, which edge, weight.

### 5.5 Edge weight deltas (series-scoped)
Start vs. end weight for every edge touched during this series.

### 5.6 Probe integrity check
Pass/fail assertion: zero feedback events logged against turns flagged as probes in this series. Any violation is a hard data-integrity flag, not a soft warning.

### 5.7 Mode-3 trigger count
Turns that crossed the summarization threshold; flag any summarization output that looks truncated or anomalous (length sanity check, not a content-quality judgment).

### 5.8 num_ctx pressure
Turns that approached or hit the ceiling, with a note on whether it traces to retrieval-volume growth specifically (the regression check tied to the ghost-MU/ceiling discussion).

## 6. Experiment Report — sections

Everything in §5, rolled up across the experiment's series, plus:

### 6.1 Learning measures (core section)

The section meant to answer "did learning occur, and where" without a manual graph dive.

**Probe outcome summary**
- Per probe: pass / fail / negative-flagged at every replay, across the whole experiment
- Per-probe *state transitions* across replays — specifically flag fail→pass (learned), pass→fail (regressed), and flat (no movement). Transitions are more informative than a single end-state tally.
- Aggregated by conceptual cluster, and rolled up for the whole experiment

**MUs pulled by probe prompts, segmented by outcome**
- For every probe replay: full retrieved set and traversal path, grouped by pass/fail/negative-flagged
- Lets you compare "what got retrieved when it worked" vs. "what got retrieved when it didn't" directly, rather than inferring it from the pass/fail number alone

**Edge weight delta ledger**
- Every edge touched during the experiment: increased / decreased / unchanged, magnitude, and whether the change traces to a logged feedback event via the FK (orphan-update count should be zero in the learned condition — report it explicitly so a non-zero count is visible, not assumed away)
- Segmented additionally by within-cluster vs. cross-cluster (bridge edge) — cross-cluster movement is the more diagnostic signal and gets its own line rather than disappearing into the general tally

**Cross-series MUs underlying probe results**
- For each probe pass/fail: whether any MU in its retrieved/traversal set originated from a different series than the probe itself, and which series

**Bridge-MU / cross-cluster transfer (dedicated subsection)**
- Bridge-MU rank trajectory across the experiment
- Explicit check: did bridge-MU rank move in cluster B following reinforcement events that occurred only in cluster A (or vice versa)? This is the strongest available evidence of genuine learning vs. memoization per the project's own framing — specific enough to warrant its own subsection rather than folding into the general edge ledger.

**Saturation / runaway-reinforcement check**
- Any edge weight approaching a configured ceiling, or showing accelerating rather than diminishing increments — flagged as a best-practices item: unbounded positive feedback on a small dataset is a known failure mode worth catching at the report level rather than discovering it after the formal run.

### 6.2 Control shadow

Every measure in §6.1, computed identically for the static-weight control and the random-weight control experiments, rendered side-by-side with the learned condition. Control-specific sanity checks:
- **Static control:** assert zero edge weight changes occurred across the entire experiment (a non-zero count here is a bug, not a result)
- **Random control:** assert weights are actually re-randomized per the designed schedule, not accidentally static
- **Both controls:** assert zero correlation between feedback events and edge updates — feedback is causally inert in both by design, and the report should verify that rather than assume it

**Comparative deltas:** for each learning measure, compute learned-minus-control directly (e.g., probe pass-rate improvement over replay index, learned vs. each control) so the headline comparison is a number already in the report, not something computed by hand afterward.

### 6.3 Data-integrity / confound checks
- Seed-fixture provenance identical across all three experiments' starting graphs (so any divergence is attributable to the algorithm, not a different starting point)
- Graph-wipe verification: each experiment started from a frozen seed snapshot, no leftover state from a prior run
- Probe-replay-index normalization: learning curves reported against probe replay index, not raw turn number, since series length varies — keeps short and long series comparable on the same axis

### 6.4 Statistical caveat (rendered text, not a computed field)
At ~100+ prompts total per experiment, the report states raw counts and simple deltas without implying statistical significance the sample can't support. This belongs in the template itself so the caveat isn't omitted under time pressure when reading results later.

## 7. Output format
- `series_report_{experiment_id}_{series_id}.json` + matching `.md`
- `experiment_report_{experiment_id}.json` + matching `.md`, with the control-shadow comparison embedded as a sub-structure (not a separate file, so the comparison can't drift out of sync with the primary results)

## 8. Build milestones for /planner

- **M1 — Field audit:** confirm exact field names/locations for everything in §3 against the ops document; confirm `series_id` stamping. Gates everything else.
- **M2 — Series Report generator:** §5 in full, JSON + Markdown render, surfaced in admin/analytics.
- **M3 — Experiment rollup (non-learning sections):** §5 aggregation across series, §6.3 integrity checks.
- **M4 — Learning measures module:** §6.1 in full. Most logic-heavy piece (state-transition detection, bridge-MU trajectory, saturation check) — its own milestone, not folded into M3.
- **M5 — Control shadow + comparative deltas:** §6.2. Depends on M4 having run for all three experiments.
- **M6 — Render + UI surfacing:** Markdown templating, admin dashboard integration, statistical-caveat text.

## 9. Open decisions (flag to Paul, not assumptions to bake in silently)
- Exact pass/fail/negative-flagged tri-state handling for probes that don't cleanly fall into one category
- Whether negative-flagged probes should be excluded from rank-movement averages or reported separately — averaging them in could mask a real regression
- Whether the saturation check needs a configured weight ceiling now, or can flag purely on rate-of-change until one exists
