# Progress

## Current state

**2026-08-27 update (1): Codex's reporting audit salvaged; two control-shadow
sanity checks found reporting unearned verdicts and fixed.**
Review session, not a milestone. Codex ran a reporting/data-suitability audit
online (PR #86, `codex/review-reporting-features-and-data-collection`) ahead of
the autonomous Karpathy loop, and made code changes beyond the report that was
asked for. **Decision: close #86 without merging, keep only the document.** Its
provenance code (runtime block in `full_config_snapshot`, top-level
`provenance` in the experiment report, Markdown header fields) was correct and
behavior-neutral — the Ollama payload was verified byte-identical — but it is
**forward-only**: every existing analytics row predates the `runtime` block, so
it buys nothing for the Alpha A-D corpus. Deferred so provenance can be built
once against the audit's fuller "Still required" list (plan_id, content hashes
rather than version labels, harness/backend git SHAs, schema versions) instead
of extended piecemeal. Two defects in the audit's PR were noted and not
inherited: a Markdown field mislabelled "Runtime config hash" when the value is
`sha256(_CONFIG)` and excludes the runtime block, and a
`.get(key, 'unavailable')` fallback that never fires because the key is always
set (renders `None` for every pre-PR experiment).

**A separate temperature pin was split out of that PR first**, at review
request: bulk turns send no sampling params, so all of exp0/exp0b/exp01 ran at
Ollama's model default. Pinning to 0.0 would change the data-generating process
mid-programme — collapsing the sampling-noise floor the Phase 1 rule compares
against, and gutting the replicate variance the same audit wants to measure. If
it is ever pinned, `backend/memory/extractor.py`'s summarisation call needs the
same treatment (it also sends none, and its summary MU is wired into the graph
and into `activated_mu_ids`), and exp02 must be treated as a new baseline
rather than pooled with exp01. Not done; recorded as an open decision.

**Two real defects were found by verifying the audit's P1 claims** and are
fixed on `claude-reporting-audit-and-control-fixes` (PR #87):
`_check_random_weights_rerandomized` took std-dev **across different edges** of
their **present-day** weights from the live `edges` table — neither the right
quantity (re-randomisation is per-edge across turns) nor the right source. All
1324 live edges now sit at `0.5` after a later reseed, so the check returned
**FAIL for both real random-control experiments**, whose weights did vary. Now
computed per-edge from each run's own `edge_weights_used`; verified through the
real code path against the real `graph.db` (random 0.128987 / 0.114395 PASS,
static 0.0 FAIL). `random_control_variance_floor` stays 0.05 — it already
separates 0.11-0.13 from 0.0, so `config.yaml` is untouched. Second defect:
malformed `weight_updates` JSON was coerced to `[]` in both the static and
feedback-correlation checks, and `[]` is exactly their PASS condition, so
corrupted evidence scored as a clean control; now an integrity FAIL naming the
run_ids. The two replaced tests had encoded the first bug — the old "PASS" case
built five distinct edges with one weight each and called that
re-randomisation. Full suite 1282 passed, 2 skipped. AWAITING PAUL: PR #87
review + merge, and close #86.

**Next step:** the audit's remaining P0/P1 items belong to the Karpathy-loop
milestone, in its recommended order — promote Alpha D outputs to versioned
report JSON, then immutable provenance + coverage checks, then the cross-plan
replicate/noise-floor artifact, then the plan-contract validator. See
`docs/reporting_data_audit_2026-08-26.md`.

**2026-08-26 update (4): Milestone 47 built and complete — the R3-V6 config
knob is live, tested, and verified against a real backend; not run.**
`/build` session. `hyperparameters.k1_centrality_weight` added to
`config.yaml` (default `1.0`); `backend/memory/retrieval.py`'s `_k1_score()`
applies it. 6 new tests: a 3-candidate fixture in `test_retrieval.py` proving
the coefficient is a genuine scalar (three distinct total K1 orderings across
0.0/0.5/1.0 — a two-item fixture couldn't have distinguished a real
coefficient from a boolean gate), plus a config-override wiring test proving
the leaf resolves through the real orchestrator against the real, committed
`config.yaml`. **Live verification (M44 pattern, real Docker backend, same
`graph.db` state both times):** the same probe (`vs_a1`) returned a different
slate under the override — seed hub `a0000001…` dropped out of the K1+K2
slate entirely and `gold_mu_rank` moved from 4 to `None` — a direct,
real-data confirmation of the planning pass's prediction that hub entry
points would essentially vanish at `0.0`. Backend restored to its original
baseline hash afterward; `git status` clean; `config.yaml` committed at
`1.0`. Also folded in 6 of 7 `docs/experiment_protocol.md` §8 errata items
(the report-field-list item stays open, pending R0-1) and wrote (not ran) the
two-replicate R3-V6 plan file (`plans/exp02-v6-rep1/rep2_2026-08-26.json`).
One Done-when text/code mismatch found and worked around: queue-level
`--dry-run` doesn't print `expected_config verified` for a plan that also
carries `config_overrides` (by design — that assertion runs at real
per-condition execution instead); verified the same assertion function
directly against a live backend rather than mutating the graph to force the
literal CLI path. Full suite 1266 passed, no regressions. Branch:
`milestone-47-k1-centrality-coefficient`. AWAITING PAUL: PR review + merge.
R3 remains blocked on R0/R1 per `phase_one_experiment_program.md` — this
milestone only delivers the knob, per its own explicit non-scope.

**2026-08-26 update (3): Milestone 47 planned — the R3-V6 config knob — and
the V6 mechanism priced from archived data before any of it runs.**
`/planner` session, docs only, no code. **M47** (BUILD-PLAN.md) adds
`hyperparameters.k1_centrality_weight` so `_k1_score()`'s undirected-total-
degree term becomes a plan override instead of a hard-coded constant. Default
`1.0` reproduces pre-M47 behavior bit-for-bit; `0.0` is R3-V6's pre-registered
"flagged off." **Decision: scalar coefficient, not a boolean** — `0.0` *is*
the pre-registered off value exactly, so V6 is unchanged, and Phase 2 gets a
dial without a second milestone. ~4 lines of code; the tests and the live
probe-reaches-the-knob check (the M44 pattern, run twice from one graph state)
are the milestone. Unblocks `phase_one_experiment_program.md` §7-1a, now
resolved.

**The planning pass also produced a real finding, written to
`docs/sensitivity_baseline.md` as a labeled pre-registration entry** (not a
result — no run stands behind it). Counterfactual replay over 630 archived
baseline turns / 12,324 candidate rows, re-sorting the recorded
`candidate_set_before_ranking` by BM25 alone. This is *exact* at the K1 stage,
not reconstructed: the candidate pool is unaffected by the scoring change and
every pool member's `bm25_score` and `edge_weight_sum` are both recorded.
Three things it says:

1. **The term is small and decisive.** Centrality is a median 24.9% of the K1
   composite score, yet it changes the K1 set on 88.4% of turns and the
   top-ranked entry point on 71.3%. BM25 scores among multi-hit candidates
   cluster tightly, so a minority additive term flips the order almost every
   time. V6 will not be a null for lack of leverage.
2. **The standing finding is true per-MU and inverted in aggregate.** Of the
   908 MUs the term promotes into K1, 81.8% are conv — but 96.6% of the 908 it
   displaces are also conv. Nearly all churn is conv-vs-conv. The term's one
   systematic effect is holding seed hubs in K1: 9.4% of K1 slots with it,
   1.3% without. **Turning it off makes K1 *more* conv-dominated (88.8% →
   95.9%), not less** — the opposite of the naive reading. The standing finding
   at the foot of the sensitivity doc now carries a cross-reference saying so.
3. **It is an early-session effect that decays.** Hub share of K1 runs
   29.9% → 8.7% → 1.9% → 2.6% → 3.7% across the five sessions, because the
   highest-degree conv MU climbs past a hub's fixed `edge_weight_sum` of 8.0
   by session 4 (median max 14.60) and doubles it by session 5 (18.39).

**The consequence for R3, and the thing to understand before the overnight:**
seed_v3 gave every v2 edge a reciprocal partner *specifically* so hub
centrality would double and "further consolidat[e] hubs as entry points
(desired)" (`seed_v3_mu_spec.md` §2). Hubs sit at 8.0 vs. spokes at 1.0 — an
8:1 ratio that exists only through the term V6 switches off. So **V6 also
tests the fixture's designed entry-point structure**, and a movement is
ambiguous between "undirected degree was noise" and "the hub route was
load-bearing" — separable mainly in session 1. **R3-V6 must be read
per-session**; a pooled headline dilutes exactly where the two mechanisms
differ. Flagged in the milestone's risks and in the R3 table row.

**Open, deliberately:** out-degree-only (count `source_mu_id` only) is the
untested middle case — it is *undirected* degree the finding complains about,
not centrality as such. A scalar coefficient cannot reach it; it needs a mode
enum. Not pre-registered for R3 (§1-6); flagged for Phase 2 if V6 returns
*distinguishable*. Also open: whether `scripts/verify_seed_v3.py`'s second
copy of the K1 formula should mirror the knob (recommendation: no, just
comment the divergence point — decide during build).

**Next step:** M46 and M47 are independent; M47 is the one on the R3 critical
path. Analysis script: `scripts/v6_centrality_counterfactual.py`.


**2026-08-26 update (2): Milestone 45 Phase 2 built and the milestone is
complete — item D's first read is in, and it's a clean answer.** Metrics
layer added to `scripts/alpha_d_pathback.py` (four-bucket partition,
chase-down yield, K2 runner-up rank distribution, anchor strength), 33 new
tests (63 total for this file), branch `milestone-45-pathback-reconstructor`
reused (Phase 1 already merged via PR #82; this is a follow-on commit on a
fresh branch off main), PR pending. **Headline: 80–82% of probe turns
pooled land in OUTBID** (gold present, one hop from a retrieved MU, lost the
K2 argmax) **against only 0–3% DISCONNECTED** (no path at all) — late-session
seed-share collapse is a read-side problem, not a write-side one, settling
item D.c. Cross-validated exactly against `probe_results.gold_mu_rank` in
every one of the 8 runs (own dedicated test, beyond what the plan asked
for). Two counterfactuals qualify it: chase-down yield (greedy
highest-weight walk, 2 hops) reaches gold only 6% of the time pooled even
within OUTBID+OUT_OF_DEPTH turns, and K2 runner-up pricing shows
P(rank<=5)=60% vs. today's ~10% at top_k=1, with 13% of turns never
entering the K2 pool regardless of K. Anchor strength found `cluster_c`'s
trained spoke never engages its hub edge in any of the 8 runs — confirms
the write-side mechanism behind Alpha C's "vs_c1 never wins" finding.
**Full tables:** `docs/evaluations/alpha_d_pathback_analysis.md`.
**Structural findings:** `docs/sensitivity_baseline.md` (new — the doc
BUILD-PLAN.md had forward-referenced since Milestone 27 and never built).
**Read:** `EVALUATIONS.md`'s 2026-08-26 entry. **Decision:**
`EXPERIMENT-PLAN.md` item D marked done; item F revised to prioritize the
read-side chase-down candidate over the write-side one (reversing item C's
lean, on stronger evidence), with `retrieval_level2_top_k` widening as a
priced adjunct, not a substitute. cfgB recommended written off as an
experimental condition — its probes never actually measured its declared
`level2_k` override (the M44 bug), so nothing is gained by re-running it,
and the pooled K2 rank distribution already prices that knob at every value
that matters. Two design points the Phase 2 spec hadn't fully pinned down,
both recorded in BUILD-PLAN.md's new "Phase 2 build notes": probe
hyperparameters had to be hardcoded to the pre-M44 disk-config values
(never a run's own `hyperparameters_snapshot`, which would have silently
mis-scored cfgB), and probe measurement sessions map to real-turn graph
state via an empirically-derived cutoff (`compute_session_cutoffs()`), not
an assumed turns-per-session constant. **Next step: Milestone 46**
(probe traversal instrumentation) — converts this read's probe numbers from
"validated by analogy with real turns" to "validated directly," which
matters before item F's knob search starts making decisions from them.

**2026-08-26 update: Milestone 45 Phase 1 built — the path-back reconstructor,
zero mismatches across all 8 archived runs.** `scripts/alpha_d_pathback.py` +
30 tests (`tests/test_scripts/test_alpha_d_reconstructor.py`), on branch
`milestone-45-pathback-reconstructor`, PR pending. All three tie-back
components (α/β, topology, slate) passed clean on the first full run against
real data — 1,680 real turns, 0 mismatches — confirming `retrieve()` has had
no silent drift from what the archived sources can reconstruct. One design
point the plan hadn't nailed down: conv edges (prompt→input, input→output,
output→activated) get random UUIDs at creation that nothing downstream
records once the graph is wiped, so the reconstructor tracks them by
`(source_mu_id, target_mu_id)` pair instead — sound because every conv pair
is created at most once ever. Full rationale in the script's module
docstring and in BUILD-PLAN.md's new "Phase 1 build notes" subsection.
**Next step: Milestone 45 Phase 2** — the metrics layer (four-bucket
partition, chase-down yield, K2 runner-up rank distribution, anchor
strength), the first read, and `docs/sensitivity_baseline.md`.

**2026-08-24 update: Alpha item D scoped and filed as Milestones 45 + 46; M44 merged.**
Milestone 44 is merged (cb39b85) and `main` is clean. The Alpha D metric
discussion resumed and closed all three open questions from the 2026-08-20
entry. **Decisions:** (1) D.a adopts a **composite**, not either sketch — a
four-bucket exclusive partition (reached / outbid / out-of-depth /
disconnected) as headline, anchor strength retained as a separate
longitudinal write-side metric, terminal-seed folded in as a conditional
"chase-down yield" within buckets 2–3, standalone 1-hop rate dropped, plus a
new cheap **gold-as-K2-runner-up rank distribution** that retrospectively
prices `retrieval_level2_top_k` — the knob cfgB was supposed to test and
never did. (2) **Hop horizon 2**; if bucket 3 comes back large, that finding
is the argument for horizon 3, with data. (3) **Archived-data analysis
script**, validated on real turns before being applied to probe turns.
(4) cfgB disposition **deferred** until baseline bucket distributions exist —
the K2 runner-up distribution is the number that should decide it.

**Two premise corrections made during scoping.** The directive called for a
standalone highest-priority `admin.py` config-fix milestone gating item F —
that is **already done**: M44 Phase 1 shipped it (`backend/api/admin.py:375`
reads `config = _CONFIG`; the only surviving disk read is the deliberate
`GET /admin/config` at line 261). **Item F's measurement gate is therefore
already clear**; F remains gated on D only. And the "sensitivity/variance
baseline doc" the findings were to be recorded in **did not exist** — the
phrase appears once in the repo, at `BUILD-PLAN.md:4629`, as a forward
reference to a layer never built. It is now created by M45 Phase 2.

**One feasibility caveat found and designed around.** `config.yaml:357`
commits `weight_mode: sample` for training turns, so per-turn edge weights
were Thompson-sampled and cannot be regenerated. The single-criterion
tie-back was therefore split into **three deterministic components** (α/β,
topology, slate), and the slate component injects the recorded sampled
weights from `candidate_set_before_ranking` / `edge_weights_used` rather than
resampling — a documented limit. D's metrics are RNG-independent regardless:
buckets read topology, chase-down yield and anchor strength read posterior
α/β.

**Two structural findings recorded for `docs/sensitivity_baseline.md`** (M45
Phase 2), both established from archived data during scoping: **~11% of
created edges are ever traversed and ~6% ever weight-updated**, dead
consistent across all 8 runs and both configs — co-activation edges are
structurally near-frozen, making the write side co-binding with `level2_k`
for item F; and **K1's `edge_weight_sum` is undirected total degree**, a
centrality term rather than a learned-direction one, so conv MUs gain K1 rank
mechanically at ~6 edges/turn independent of feedback. Flagged as a knob
candidate; not changed now.

**Next step (superseded — see the 2026-08-26 update above): build Milestone
45 Phase 1.** Phase 1 is now complete; Phase 2 is next. M46 follows M45.

**2026-08-20 update (2): Plan Alpha item D discussion opened; a measurement defect found and filed as Milestone 44.**
Reading `retrieve()` to settle item D.a's metric definitions surfaced that
`POST /admin/run-probe` re-opens `config.yaml` from disk
(`backend/api/admin.py:362-364`) instead of using the orchestrator's resolved
`_CONFIG`, which the same file already imports at line 46. **M43 Phase 2's
`CONFIG_OVERRIDES_JSON` therefore reaches every real turn and no probe
measurement.** Confirmed against `memory/graph.db`: exp01-cfgB declared
`retrieval_level2_top_k: 2`, its 210 real turns per series retrieved 5 MUs,
and all 3,500 of its probe rows retrieved 4 — cfgB's "2/8 = null" is a null
on a knob the probe battery never saw. cfgA is unaffected (seed priors are
applied at fixture load by `seed_fixture.py`, on M43's list, and land in the
`edges` table where probes read them), so **Plan Alpha items B and C stand as
read.** Filed as BUILD-PLAN Milestone 44 (two phases: use the resolved config
+ a loader-inventory guard test; then a §6.3 `probe_retrieval_geometry` check
whose acceptance criterion is that it FAILS the two archived cfgB reports and
PASSES the other six). It should land before any item D instrumentation or
item F knob run, since every item F read-side candidate is a
`hyperparameters.*` knob measured by probes. **Open with Paul, not yet
decided:** (1) whether item D.a adopts the exclusive reachability partition
proposed in discussion in place of the three independent rates; (2) whether
item D is scoped as an analysis script over archived data (feasible — the
graph is exactly reconstructible from `analytics` rows plus the seed fixture,
even though MU/edge tables are wiped per run); (3) whether cfgB gets re-run
under correct measurement or is written off. Paul is thinking on (1) and (2).

**2026-08-17 update: Alpha item A (run-documentation process) is done — docs-only `/build`, no code.**
`EVALUATIONS.md` created at repo root (fixed-heading run-evaluation log,
format agreed in Alpha A.a) with its first entry backfilled — the
2026-08-16 exp01 read, cross-linked to the four experiment reports rather
than restating their numbers. The per-experiment setup-and-goals doc
convention (Alpha A.c) is now a copyable template,
`plans/TEMPLATE_setup-and-goals.md`; the first real use will be the
`exp01-cfgA-rep3` setup doc (Alpha B.a). `EXPERIMENT-PLAN.md`'s item A and
its three subitems are marked done with pointers to both new files. **Next
step: Alpha B** (write the rep3 setup doc from the template, then run
`exp01-cfgA-rep3` overnight) **with Alpha C** (lock-in analysis on existing
data) **in parallel, before the rep3 read.** See the 2026-08-17 session log
entry.

**2026-08-16 update: exp01 (Round 2) RAN and the read session is done; EXPERIMENT-PLAN.md created.**
All four exp01 experiments (cfgA rep1/rep2, cfgB rep1/rep2) completed clean —
0 failed turns, 0 ghosts, all §6.3 checks PASS, M43 config injection verified
working (cfgB's retrieval K change visibly took effect: 210 vs 168 retrieved
per series). **Against the pre-registered M42 decision rules: cfgA = 4/8
final-session cluster-slots aligned to trained spokes (vs 2/12 Round 1
baseline) — exactly on the "clear signal" line; cfgB = 2/8 = null, not
extended.** cfgA also produced the two strongest champions of the program
(rep1 vs_a1 at 0.92–1.00 every session; rep2 vs_d5 climbing 0.40→0.96), and
cfgA rep1's trained-subset final Δ of 0.00 is the first value outside the
Round 1 raw envelope [−0.46, −0.12]. Caveats: replicate variance still dwarfs
the config effect (vs_a1 = 1.00 champion in rep1, flat 0.00 in rep2), and two
of cfgA's four aligned slots are weak winners (0.16, 0.36). Full discussion
and agreed next steps live in **`EXPERIMENT-PLAN.md`** (new, repo root) —
experiment planning now lives there as per-session Greek-lettered plans
("Plan Concept Alpha" is this one), not BUILD-PLAN milestones. **Next step:
Alpha items A–B — create EVALUATIONS.md + the rep3 setup doc, then run
`exp01-cfgA-rep3` overnight; Alpha item C (early-feedback lock-in analysis on
existing data) in parallel, before the rep3 read.** See the 2026-08-16
session log entry.

**2026-08-13 update:** Milestone 43 (Plan-Declared Config State — assertion +
env-override automation) is COMPLETE and pushed, branch
`milestone-43-plan-declared-config-build`, PR awaiting Paul's review. Both
phases landed in one session per Paul's sequencing decision (both must land
before exp01 runs). See BUILD-PLAN.md's Milestone 43 status paragraph and
the session log entry below for full detail. 1151 tests passing, no
regressions (1166 after the review pass below). **UPDATE (review pass,
same day):** Paul's PR review found four gaps — all fixed on the same
branch — and the live Docker verification the build session had to skip is
now done, covering every "Done when" criterion in both phases. **AWAITING
PAUL:** review + merge the PR, then exp01's queues
(`queues/exp01_cfgA_*.json`, `queues/exp01_cfgB_*.json`) can run. The 4
plans now declare **both** `expected_config` and `config_overrides`, so
exp01 needs **no manual `config.yaml` switch at all** — each plan's own
per-condition restart establishes its config, and the assertion verifies
the injection took effect. The rest of this
"Current state" section (below) predates this and is retained for history;
see BUILD-PLAN.md's own per-milestone Status log for the more current
narrative on milestones after M34.

Project: Local Corvus — local LLM learning research platform  
Phase: Active — M19–M30 merged/pushed. Milestone 30 (Dual-Mode Probe Battery) is now **fully code-complete and pushed**: Part A (harness+backend dual-mode dispatch, built 2026-07-15) and Part B (§6.1 report consumption fix, built 2026-07-26) both landed as commits on `fix/smoketest-2026-07-15`, updating the already-open PR #51 rather than opening a new one. The M28 baseline tie-break was re-confirmed stable under `static_weight`/`mean` (2 independent reseeds) AND under `learned`/`mean` (identical, as expected). But a follow-up check under `learned`/`weight_mode: sample` — the mode the learning mechanism actually needs for exploration — found the tie-break is a **qualitatively different, noisy regime** there: no clean "always-fail" spoke subset exists at N=25 (see `docs/m28_tiebreak_learned_mode_findings_2026-07-15.md`). The original M28/M29 premise ("avoid `b_spoke1`/`d_spoke1`, pick a clean-fail spoke instead") is `weight_mode: mean`-specific and does not straightforwardly generalize to `sample`. Milestone 30 is the direct answer: harness+backend now run both a mean pass and K=25 sample repeats at every probe checkpoint, and `experiment_report.py`'s §6.1 now correctly filters `battery_mode="mean"` for the existing mean-only subsections and exposes a new `sample_pass_rates` subsection (JSON + Markdown) for reading the sample-mode data directly.  
Current milestone: **Experiment 0 prep (2026-07-26 /smoketest).** Milestone 30 is complete and PR #51 is merged. The experiment-0 run plan now exists (`plans/exp0_2026-07-26.json`), and the three questions left open since M28 are decided and committed: `weight_mode: sample` (a structural requirement — under `mean` no spoke outside the initial tie-break winner can ever gain evidence, so the run would return a null for mechanical reasons), `trained_spokes` a1/b3/c1/d5, and sizing 5×70 @ `probe_ratio` 0.4 (28 probe turns/series = exactly one full v3 battery per session; the docs' v2-era 5×25 shape would have probed only 10 of 28 probes and never touched clusters c/d). Two real harness bugs on the bank-generation path were found and fixed (cost-gate traceback; the coherence gate that could never pass because adaptive thinking is on by default on `claude-sonnet-5` and consumed the whole `max_tokens` budget). 798 tests passing.  
That blocker is **resolved**: the v3 cross-cluster entry-vocabulary rule is now scoped to the BM25 keyword window (the first 5 non-stopword tokens `retrieve()` actually reads), with answer tokens still forbidden whole-prompt. Two further generation-path bugs surfaced by a paid trial were fixed alongside it (the v3 generation prompt was inheriting v2's instruction to *avoid* the cluster's own vocabulary, which is backwards for v3; and the per-target output budget could be consumed entirely by thinking). **The experiment-0 prompt bank is generated and verified: `prompt_bank_e2adf796...json`, 210 prompts, 0 exclusions, all 7 verification groups passing** — including that all 210 prompts land ≥2 of their first 5 BM25 keywords on their target hub, which is what the generator's own gates do *not* check and is the exact failure mode that wasted the first v2-era bank draft. 820 tests passing. Decisions written up in `docs/exp0_design_decisions_2026-07-26.md`.  
**Judge calibration is done (2026-07-27).** The 20-turn `--interactive` pass completed: 15 up / 5 down posted, plus one turn lost to a judge criterion-citation failure. That failure was traced to an asymmetry that could only ever delete `down` verdicts — biasing the up-skew measurement the pass exists to produce — and is fixed on `claude-fix-judge-criterion-normalization` (normalized criterion resolution, verdict-preserving unresolved-citation handling, and operator recovery for unparseable judge responses in `--interactive`; 856 tests). **Judge skew as measured: 15/20 up, with 5 genuine downs — the judge is not blanket-approving, which the 6/6 up streak before this run left open.** Consider a short re-run of the calibration plan on the fixed code before committing ~2.4 h, since turn 4's `down` was never posted.

**Experiment 0 RAN CLEAN (2026-08-02).** All 3 conditions × 5 series × 70 turns = 630 turns, 0 failed, 0 ghost MUs, all §6.3 integrity checks PASS (`memory/reports/experiment_report_exp0-2026-07-26_learned.md`). Science: null vs controls (consistent with the M21 pilot), but the analysis session surfaced three structural findings — seed-share of retrieval collapses across series in every condition (42%→14% learned, →4% random; judge up-rate declines in lock-step, so environment drift dominates the time trend), 78% of weight updates landed on "unknown cluster" conversation-MU edges the probes can't see, and the any-criterion-unmet-→-down judge rule produced a punishment-dominated regime (34% up-rate). Paul's decisions (2026-08-02): measure accumulation rather than control it; classify/quantify the conversation-edge territory; change the judge to up=all-criteria-met / down=none-met / partial=abstain. **Milestone 32 (seed-share + verdict-mix report metrics) is built (2026-08-02)** — the first of Milestones 31–35 (BUILD-PLAN.md "Experiment-0 Follow-Ups"). One open science read pending M34: whether vs_d4's sample pass-rate climb (4%→60%) replicates in controls (drift) or not (first real learning signal).

**Milestone 33 (conversation-edge classification) is built (2026-08-02).** §6.1's edge delta ledger now classifies weight updates by edge class (`seed_seed`/`conv_seed`/`conv_conv`) x cluster relation (`same_cluster`/`cross_cluster`/`mixed`/`unresolvable`) instead of the old flat within/cross/unknown counts. Verified live against exp0's real "learned" data: all 176 formerly-"unknown" updates resolved (0 residual) — see the Milestone 33 session log entry below for the full distribution and reading.

**Milestone 34 (§6.2 control shadow: sample-mode comparison + rendering) is built (2026-08-02) — the M34 open science question is answered.** `control_shadow.py` gained a sample-mode (`weight_mode=sample`) pass-rate comparison alongside the existing mean-mode one, and `markdown_renderer.py`'s two rendering bugs (hardcoded `—`/`(baseline)` instead of the learned baseline value) are fixed. **Read against real exp0 data: vs_d4's sample pass-rate climb (4%→60%) does NOT replicate in either control** — flat 0% under `static_weight`, uncorrelated noise under `random_weight` — confirming it's a real learning-driven effect of the `learned` condition specifically, not shared environment drift. See the Milestone 34 session log entry below for the full reading, including a second unplanned finding (`vs_c1`'s near-ceiling pass rate is explained by static default weights, not the RL mechanism — it collapses under `random_weight`).

**Milestone 35 (traversal-counter + runaway-detector instrumentation fixes) is built (2026-08-02) — the last of the report-layer Experiment-0 follow-ups.** §5.5's `traversals: 0` bug is fixed (the count now comes from `activated_edge_ids` occurrences, not the dead `edges.traversal_count` DB column nothing ever increments), and the runaway-reinforcement detector no longer flags ordinary shrinking-magnitude convergence streaks as anomalies. **Live-verified against real exp0 data: `runaway_flag_count` drops from the stored report's 13 to the true 0** — confirming the exp0 analysis finding that all 13 were false positives, not genuine runaway reinforcement — and traversal counts are now nonzero and correct (surfacing 15 new `zero_delta_high_traversal` flags in series s0 alone, a signal that could never fire before). See the Milestone 35 session log entry below for full detail.

**Milestone 31 (judge verdict scheme — partial answers abstain) is code-complete (2026-08-02)** — the last of Milestones 31–35, built last per BUILD-PLAN.md's sequencing rule since it changes the learning signal itself. The judge now grades a per-criterion checklist (`criteria_met`, a list of booleans) instead of asserting up/down/none itself; the harness derives the verdict host-side (all met → up, none met → down citing the first criterion, mixed → none/abstain — no weight update). This directly answers the exp0 finding that the old any-criterion-unmet-→-down rule produced a punishment-dominated 34% up-rate. See the Milestone 31 session log entry below for full detail. **AWAITING PAUL: the live 20-turn `--interactive` recalibration pass** (`plans/exp0_calibration_2026-08-02.json`, cloned from the 2026-07-27 calibration plan with the same 20-turn slice) — this needs a real local Ollama instance and live human judgment on real judge output, which is outside what an autonomous `/build` session can do. Record the up/down/none mix once run (exp0's ledger predicts a large `none` share); only after that is done should an exp0b re-baseline run be considered. **Next step: Paul runs the recalibration pass**, then decides on exp0b timing.

**The M31 recalibration pass RAN (2026-08-04), and Milestone 36 closed the reporting gap it exposed.** 20 bulk turns, 0 failures, recorded mix 12 up / 5 down / 3 none (70.6% up-rate) — but Paul's 3 overrides appeared nowhere in the report, because §5.2 aggregated the recorded verdict only. M36 adds the judge-alone view (`judge_model_verdict`/`judge_model_rationale` on `feedback_events`, surfaced in §5.2 as human-sourced count + judge-alone up-rate) and stops overrides from inheriting the model's rationale. **Judge-alone up-rate for the M31 pass is 82.4% vs. the 70.6% recorded** — the number that describes an unattended run. The override audit found all 3 disagreements share one defect: the judge marks a criterion `true` on a looser standard than the criterion states (self-contradicting checklist, invented synonym, paraphrase-as-match), all lenient in direction, **none indicting M31's mixed→none derivation rule**. **Next step: tighten the judge prompt toward literal-match discipline, then recalibrate grading per-criterion rather than per-verdict** (denser signal per turn, and it targets the layer that's actually wrong) before committing to exp0b. Optional, Paul's call: backfill the 3 pre-M36 override rows in `memory/graph.db` from the checkpoint so the stored M31 report reads the true 82.4% instead of the partial-coverage 80.0%.

**Milestone 37 (judge literal-match discipline + per-criterion calibration) is code-complete (2026-08-05)** — both halves of the next step M36 recommended. **Prompt side:** the judge now returns a quote-bearing checklist (`{"checklist": [{"quote": ..., "met": ...}]}`, quote requested before the boolean so it commits to evidence before judgment), criteria are tagged `[EXACT]`/`[CONCEPT]` and held to different standards, and there is an explicit "if unsure, answer false" default. **The single most important design finding: "literal match" cannot be applied blanket.** The bank holds two criterion families — `answer: L-2` (a required answer, where the audit's synonym defect lives) and `mentions consistent/fixed field ordering` (an idea, deliberately paraphrase-tolerant, ~3/4 of the bank). Demanding literal matches everywhere would have fixed the first and broken the second, trading a lenient judge for an over-strict one. The tag is derived host-side from the bank's own `answer:` convention, so no criteria text changed and `content_hash` survives. **Host-side evidence checks:** `met: true` with a supplied-but-empty quote is forced false (the protocol says an empty quote requires `met: false`, so it is the checklist contradicting itself); an unverifiable quote and an `[EXACT]` criterion whose answer is absent from the response **flag only, never demote** — they are heuristics, and the recalibration pass is what says whether either earns enforcement (Paul's call on real numbers). **Calibration side:** `--interactive` offers `[r/a/o/skip]` with `r` = regrade the checklist one criterion at a time (Enter keeps the judge's answer); the verdict is re-derived by the same `derive_verdict()` the judge path uses, so a pass can never measure the rule instead of the judge. Every interactive run now writes `calibration_summary.md` beside its checkpoint — per-criterion agreement rate, the lenient/strict split (the direction is the finding), recorded vs. judge-alone verdict mix, and evidence-check counts. 1003 tests passing, no regressions. **AWAITING PAUL: the live 20-turn `--interactive` recalibration pass** (`plans/exp0_calibration_2026-08-05.json` — same `random_seed`/20-turn slice as M31's, so the two passes are directly comparable). **Next step: Paul runs that pass**, then compares the per-criterion agreement and lenient/strict split against M31's 82.4% judge-alone / 3-override read, decides whether either evidence flag graduates to enforcement, and only then commits to exp0b.

**That pass RAN (2026-08-06, attempt2) and the answer was: not yet ready for exp0b.** 27/30 per-criterion agreement, but **3 lenient / 0 strict** — the one-sided skew the summary's own rule calls systematic — and it **reproduced M31's result on the identical 20-turn slice** (judge-alone "up" = 14 in both passes). The three lenient calls were one failure mode: the judge's quote named the criterion's *topic* without asserting its *content*, which the evidence checks cannot catch because those quotes really were in the response. **Fixed and measured on branch `claude-judge-leniency-and-unparseable-accounting`:** two flat prompt rules plus a domain-neutral worked example take the same 20 turns from 3 lenient / 0 strict to **1 lenient / 1 strict** against the real `gemma4:e4b` (stable across 3 runs; the old prompt reproduces the recorded pass exactly, which validates the A/B). Also on that branch: `judge_unparseable` now defaults to `record_none` (a dropped verdict is invisible in the data — the observed 1-in-20 rate is ~50 turns across exp0), the re-ask names its rejection reason, and `calibration_summary.md` accounts for all its turns. **Residual leniency is a KNOWN ISSUE, not closed** — the sample is far too small to call 1-and-1 balanced, and it was measured only on the `learned` arm, which is exactly where a rule-parroting-triggered bias would fail to cancel between conditions. **Next step: re-run the `--interactive` pass on that branch**, then decide exp0b with the residual documented. See the 2026-08-06 (c) session log entry.

**The re-run RAN (2026-08-06, attempt3) and the decision is: GO for exp0b, with the residuals documented.** `harness_runs/exp0-calibration-2026-08-05-m37_3940644e27e0_attempt3/`. Per-criterion agreement **29/32 (90.6%)**, **2 lenient / 1 strict**, 20 of 20 turns accounted, 0 excluded from judge-alone (recorded 10 up / 4 down / 6 none = 50.0%; judge-alone 11 up / 5 down / 4 none = 55.0%). **What the fixes provably closed:** the unparseable-accounting hole — attempt2 silently dropped a turn and reported a judge-alone denominator of 19 against a recorded 20; attempt3's ledger reconciles at 20/20. **What they did not close:** the leniency. 90.0% → 90.6% agreement is one criterion, and 3L/0S → 2L/1S is three disagreements either way — neither is resolvable at n≈32. Both residual lenient calls (turns 16 and 19) are the *same* rule-label-parroting failure mode the prompt rules targeted; the offline A/B predicted 1L/1S on a fixed replay, the live pass gave 2L/1S on freshly sampled responses. The prompt rules reduced the rate; they did not remove the mechanism.

**Paul's decision (2026-08-06), after weighing further calibration and declining it:** proceed to exp0b. The reasoning, recorded because it is not recoverable from the artifacts: (1) more `--interactive` passes cannot resolve a three-disagreement difference — n=20 is at its resolution floor, so another attempt buys nothing that changes the decision; (2) **BM25 retrieval quality is an accepted design simplification, not a defect to chase.** Its limits were priced in when it was chosen; embeddings + vector search are a planned future upgrade. Exp0b's question is purely whether learning-algorithm config adjustments move the k-graph, and that signal should survive imperfect retrieval — arguably it is *sharpened* by it, since poor retrievals are common and the edges that produce them get starved by exactly the down-vote → lower-weight → less-selected loop the mechanism exists to run. Credit is defined by outcome in this regime, not by semantic relevance, so the learned edge values are conditional on BM25 and that is coherent; (3) exp0-a already showed created MUs crowding out seed MUs later in each series — a real signal about config effects on learning, and a reason not to treat seed-MU hit rate as the thing to optimize. Do **not** open a retrieval-quality investigation before exp0b.

**KNOWN ISSUES carried into exp0b — document in the report, do not block on them:**
  - **Residual judge leniency, learned arm only.** 2L/1S on 32 criteria, same failure mode as attempt2, measured only on `learned` (`dry_run_n` grades the first condition in `condition_order`, `runner.py:842`). Nothing is known about the judge on `static_weight`/`random_weight`. The threat is specifically that rule-label-parroting leniency is plausibly *correlated with the treatment* — the one case where a bias does not cancel between arms. **Reporting rule for exp0b: never compare arms on up-rate alone, and publish the judge-alone rate with this limitation attached.** Note the confound if it is ever revisited: a higher lenient rate on `learned` would conflate judge bias with genuinely harder near-the-line judgments (control responses built on irrelevant MUs are plainly non-responsive and easy to grade). That distinction matters for fixing the judge; it does not change the correction exp0b needs, since either way the learned arm's up-rate carries more judge error than control's.
  - **BM25 retrieval is known-limited and deliberately unaddressed** (see decision (2) above). Expect wrong-hub K1 entries; expect down-votes to land on the traversed edge regardless of whether the miss originated upstream. This adds variance and therefore costs power — it is not a validity problem for a config-comparison question.

**Next step: run exp0b.** `python scripts/run_experiment_harness.py --plan plans/exp0b_2026-08-06.json`. The plan is byte-identical to `plans/exp0_2026-07-26.json` except `plan_id`/`plan_name` — same `random_seed` (20260726), same bank, same `trained_spokes`, same 3×5×70 shape — so exp0b is a true re-baseline in which the only variables are the judge scheme and the merged harness fixes. The new `plan_id` gives it its own `experiment_id`/`series_id` namespace, so its rows can never mix with exp0-a's in `analytics`/`feedback_events` (which `wipe_graph()` never deletes).

**Exp0b RAN CLEAN (2026-08-07) and the baseline read is done (2026-08-09).** 630 turns, 0 failed, 0 ghosts, all §6.3 checks PASS (`memory/reports/experiment_report_exp0b-2026-08-06_learned.md`). What it showed, against exp0-a: (1) the aggregate learned-vs-control comparison is still null (mean-mode 0.0000; sample-mode mean Δ ~0.00–0.02) — but the mean is polluted by static tie-break pins (vs_a1/vs_c1/vs_d1 at 1.0 in every control session, learned's gains cancelled by −1.0 deltas against those ceilings); (2) seed-share collapse replicated and is now quantified (45.2%→4.2% across series, up-rate 52.9%→9.7% in lock-step; conv↔conv edges take 116/171 updates, seed↔seed dies 27→2) — the run trains a graph the probes can't see, as before; (3) the verdict regime is still punishment-dominated under the new judge (29.1% up-rate, 71% of updates decrements, 45 abstains showing M31 live); (4) **the decisive finding: the concentration effect replicated in kind but not identity.** exp0-a's climber was vs_d4 (d_spoke4, untrained); exp0b's is vs_d6 (d_spoke6, untrained, 48%→84%, controls flat 0), with vs_a2 (untrained) elevated too — same plan/seed/bank both times. Every winner is a within-cluster *neighbor* of a trained spoke; the trained spokes themselves sit at ~0–12% (vs_b3's early 52% starved back to 0). Read: the mechanism reliably concentrates, but the winner is selected by early stochastic feedback and amplified rich-get-richer — it learns "this cluster," not "this spoke." The central question for the config rounds: can any config make the trained spoke win its own cluster? **Drafted 2026-08-09 (branch `claude-exp-round1-prep`): Milestone 39** (config-comparison endpoints: trained-subset delta, per-cluster winner/alignment table, pin-adjusted mean deltas — BUILD-PLAN.md) and **Milestone 40 / Round 1** (baseline seed replicates: `plans/exp0b-rep2_2026-08-09.json` seed 20260809, `plans/exp0b-rep3_2026-08-09.json` seed 20260810, learned+static only, `queues/exp0b_replicates_2026-08-09.json`, pre-registered read in BUILD-PLAN M40 — establishes the winner-identity/trained-delta noise floor every config round is judged against). All three plans schema-validated, config hashes distinct. ~~Next step: review/merge the PR, `/build` M39, then run the Round 1 queue overnight~~ — **M39 is built (2026-08-09), see below. Next step: run the Round 1 queue overnight** (`python scripts/run_experiment_queue.py queues/exp0b_replicates_2026-08-09.json` — restart Ollama first), then read it through M39's new sections.

**Milestone 40 / Round 1 RAN CLEAN (queue completed 2026-08-10) and the pre-registered read is done (2026-08-11).** Both replicates completed via `queues/exp0b_replicates_2026-08-09.json`: rep2 (seed 20260809) in one pass; rep3 (seed 20260810) hit a chat timeout mid `static_weight` s2 turn 31 overnight and was resumed via the M27 machinery the next morning — control arm only, learned arm untouched, resumed series report shows 0 failed turns / integrity PASS, so the replicate stands (queue summary's rep3 durations are bookkeeping artifacts of the resume, not data problems). All §6.3 checks PASS on both learned reports, 0 failed turns, 0 ghosts. **The pre-registered answers (BUILD-PLAN M40):** (a) **a trained spoke CAN win its own cluster — rep3 is the first run where it happened, twice**: vs_b3 (trained) wins cluster_b in *every* session (0.76 final) and vs_d5 (trained) jumps 0→0.68 to take cluster_d in the final session; rep1/rep2 remain 0-for-4. Baseline alignment rate: **2 of 12 cluster-slots across the three replicates.** (b) **Winner identity is NOT stable across replicates, as predicted** — final-session winners: rep1 a2/b2/c3/d6, rep2 a3/b5/c7/d6, rep3 a2/b3✅/c5/d5✅ — though vs_d6 winning decisively in 2 of 3 (0.84, 0.44) hints d_spoke6 has some structural (likely BM25-lexical) advantage worth a look before it's read as pure chance. (c) **The trained-subset final-session Δ vs static spans −0.46 / −0.20 / −0.12** (rep1/rep2/rep3) — a ~0.34-wide noise floor, and the metric is **pin-contaminated**: rep3's −0.12 is two decisive trained wins (+0.76, +0.68) cancelled by two control-pinned ceilings (vs_a1, vs_c1 at 1.0 → −0.96 each), so read the winner-alignment table and pin-adjusted deltas as primary, raw trained-subset Δ as secondary. Pin-adjusted final Δ (excl. pinned) is small-positive in all three replicates (+0.096 / +0.007 / +0.039). Seed-share collapse replicated a third and fourth time (47.6→5.9, 53.6→10.7) — robust. **But the up-rate lock-step decline did NOT fully replicate**: rep2/rep3 up-rates dip then *recover* (78.8→29.7→42.1; 66.7→30.3→41.7) while seed-share stays collapsed, partially decoupling up-rate from seed-share; and overall up-rate varies 29.1%–44.0% across identical-config replicates, so config rounds cannot attribute up-rate movements smaller than ~15 pp to the config. **Decision: baseline is characterized — ready for Round 2 (config adjustments).** Judge caveat stands: never compare arms on up-rate alone.

**Milestone 42 / Round 2 (officially "exp01") is DRAFTED and validated (2026-08-12, branch `claude-exp01-round2-prep`).** Two configs targeting the pre-registered question "can a config make early feedback land on the trained spoke and stick?", 2 replicates each, seeds deliberately reusing Round 1's (20260809/20260810) for a quasi-paired winner-identity comparison: **cfgA** = slow-evidence seed priors (`edge_update.seed_prior_alpha/beta: 3.0` — 3× evidence mass at the same 0.5 mean keeps Thompson exploration alive longer so the trained spoke's consistent correct-answer signal can beat early luck) and **cfgB** = wider K2 (`retrieval_level2_top_k: 2` + `pass_rank_thresholds.spoke_retrieval: 5` — trained spoke retrieved ~2× as often early; pass-rate *levels* not cross-config comparable, only winner/alignment tables and within-run learned-vs-static). Knobs rejected with reasons in BUILD-PLAN M42 (`credit_lambda`/`negative_scope` verified inert under single-hop retrieval). **RunPlan cannot express hyperparameters, so the config lives in config.yaml (bind-mounted) via a documented switch protocol: edit → restart backend → restart Ollama → verify with a one-liner → run queue → `git checkout -- config.yaml`; provenance is `analytics.full_config_snapshot` on every run row, and the read session must confirm it per experiment before reading results.** Artifacts: `plans/exp01-cfg{A,B}-rep{1,2}_2026-08-12.json`, `queues/exp01_cfg{A,B}_2026-08-12.json` — all four load through `RunPlan`, config hashes distinct from each other and all prior runs, queues load through `load_queue_file()`. Decision rules pre-registered against the Round 1 floor (alignment ≥4/8 slots = signal, ≤2/8 = null; trained-subset Δ must leave [−0.46, −0.12]). **M41 (pin-adjusted trained-subset Δ) recommended before the exp01 READ, does not block the runs** (reports regenerate retroactively, M33–35 precedent). **Next step: merge this PR, then night 1 = cfgA queue, night 2 = cfgB queue, then the read session (through M39+M41 sections).**

**Milestone 41 (pin-adjusted trained-subset Δ) is DRAFTED as a build-ready BUILD-PLAN section (2026-08-12 (c)).** Report-layer only: `_compute_trained_subset_comparison()` in `control_shadow.py` gains an optional `pinned_probe_ids` param (reusing M39's existing `_compute_pinned_probes()` rule verbatim — no second pin definition) and emits raw and excluding-pinned means side by side, with the session-mean arithmetic extracted into one shared helper so the two can't diverge; `markdown_renderer.py`'s trained-subset block gains the pinned list, a second headline figure, and two columns, both means always shown **with their probe counts** because an n=2 mean is high-variance. Two hazards written into the scope: all-trained-probes-pinned must render `None`/`—`, never `0.0` (indistinguishable from a real null), and **M42's pre-registered [−0.46, −0.12] envelope is the RAW metric's** — M41 §7 requires recomputing the Round 1 envelope on the adjusted metric and amending M42's decision rule to name both, or exp01's read compares a pin-adjusted value against a raw envelope. Done-criterion is the hand-computed rep3 read: pinned = {vs_a1, vs_c1}, raw final Δ unchanged at −0.12, excluding-pinned +0.72. **Build M41 before the exp01 read session; it does not block the runs.**

**Milestone 41 is BUILT and live-verified (2026-08-12 (d), branch `milestone-41-pin-adjusted-trained-subset-delta`).** Implemented as drafted; 10 new tests (1093 total, no regressions). Live regeneration against the real `memory/graph.db` confirms the raw trained-subset Δ and M39 all-probe pin-adjusted finals are exactly unchanged for all three Round 1 replicates — proof the milestone touched nothing outside its scope. **One correction to the hand-worked example above:** rep3 pins only `vs_c1`, not `vs_a1` — `vs_a1`'s static control is 0.0 in 2 of rep3's 5 sessions (the plan's table had read only the 1.0 final-session value), so excluding-pinned final Δ is **+0.16 (n=3)**, not the predicted +0.72 (n=2); raw final Δ is confirmed unchanged at −0.12. Corrected across all three replicates, real pin counts are `vs_a1`: 1/3, `vs_c1`: 2/3 (the earlier "3/3" claim for `vs_a1` was a hand-tally generalization error). **Recomputed pin-adjusted trained-subset Δ envelope: [−0.20, +0.16]** (rep1 0.0, rep2 −0.20, rep3 +0.16) — recorded in BUILD-PLAN M42's decision rule alongside the raw envelope [−0.46, −0.12], naming which metric each governs. See the 2026-08-12 (d) session log entry for full per-probe detail. **Next step: PR review/merge, then the exp01 (M42) run nights and read session.**

**Superseded 2026-08-02 (kept for the carried-over notes): next step was: run experiment 0.** Docker is up and everything needed is in place. Recommended order: `--dry-run` to confirm the stack end to end, then `--dry-run --interactive` judge calibration on 15–20 turns (the 210 criteria are new and have never been graded by `gemma4:e4b`), then the full ~2.4 h three-condition run. **The `weight_mode`/`trained_spokes` decision is still Paul's to make** — M30 built the measurement tool (`sample_pass_rates`), it doesn't answer the question. Once merged: run a real dual-mode probe battery through a real experiment, read the new §6.1 `sample_pass_rates` section, then decide. Only after that: pick real `trained_spokes` values and run `--generate-bank` (paid Sonnet call — Paul triggers himself). Also still open from M28: `weight_mode` itself (`mean` vs `sample` — leaning `sample` per the exploration-mechanism argument, not yet locked in `config.yaml`/a real RunPlan), turn/probe counts, `budget_cap`. AFTER the bank is generated and the v3 RunPlan is designed: the full experimental pilot on v3's counterfactual content, and the Sequential Batch Queue Runner (M26+M27) is ready to drive the 10-15-run screening series unattended. Science note carried over from the M21 pilot, still open: probe rank trajectories were nearly flat across all three conditions in the v2 battery — v3's counterfactual content is itself a partial answer to this; revisit after the v3 pilot. Housekeeping: `docs/experiment_protocol.md`'s uncommitted rewrite was committed 2026-07-15 (`1f39c23`, now pushed with M30) — but its RunPlan field reference table still needs a `trained_spokes` row (M29's own scope note, still not done) and the "record condition+weight_mode with every baseline claim" protocol rule (see BUILD-PLAN.md Status block, added 2026-07-15).

## Open engineering to-dos

Forward-looking work items surfaced by past sessions but not yet built. Each says explicitly whether it blocks experiment 0.

### TODO-1 — §6.1's N+1 probe query in `generate-experiment` (does NOT block experiment 0)

**What:** `backend/analytics/reports/experiment_report.py:1153-1183` issues **one `store.query_probe_results()` call per probe definition**, twice over (mean rows, then sample rows since M30), each loop followed by a `condition_label` fallback pass over the same 43 probes. `query_probe_results()` opens and closes its own SQLite connection per call (`store.py:1111-1116`, the project's connection-per-operation pattern), so the cost is dominated by round-trips, not rows.

**Measured 2026-07-27:** `POST /admin/reports/generate-experiment` took **55.7 s**, of which ~50 s sat in this one step (backend log gap between `§6.1: 1 runs loaded` and `§6.1: 38 probe(s) with results`). That was against a near-empty DB — 1114 `probe_results` rows — and it is what pushed the call past the old 60 s client timeout. Timeout since raised to 600 s (`af542ef`), so this is no longer a failure, only waste.

**Why it does not block experiment 0** (checked, not assumed):
- `probe_results` **is** indexed on `experiment_id` (`idx_probe_results_experiment`), and `EXPLAIN QUERY PLAN` confirms the query uses it — `SEARCH probe_results USING INDEX idx_probe_results_experiment`. All six filters are applied in SQL, so there is no full-table scan and no Python-side filtering to degrade.
- The 172 round-trips measured today happen because **both** experiment-scoped loops returned zero rows and therefore triggered **both** `condition_label` fallback passes. In experiment 0 the experiment-scoped queries will match, so the fallbacks do not run and the count **halves to 86**.
- Net expectation: experiment 0's report is comparable to or faster than today's 55.7 s, against a 600 s budget. Ample headroom.

**Why it is still worth doing:** ~86 sequential connection opens to answer what is fundamentally four queries is the kind of fixed cost that compounds once the Sequential Batch Queue Runner (M26+M27) drives the 10–15-run screening series unattended — 15 runs × ~1 min of pure connection churn per experiment report, plus whatever series reports add.

**Proposed fix (a `/build`-sized backend change, not a smoketest fix):** replace each per-probe loop with a **single** `query_probe_results(experiment_id=..., battery_mode=...)` call — no `probe_id` filter — and group the rows by `probe_id` in Python, keyed off the `get_all_probes()` list already loaded at line 1146. That turns 86–172 queries into **4** (mean, sample, and their two fallbacks) with no change to the returned shape, so `_compute_state_transitions()` / `_compute_sample_pass_rates()` and their tests are untouched. Worth a regression test asserting a probe with no results still gets an empty list rather than being dropped from the dict — the loops guarantee a key for every probe today, and a naive `groupby` would not.

## Session log

- 2026-08-17: `/build` — Alpha item A (run-documentation process) complete — docs only, branch `alpha-a-run-documentation-process`
  - **a. `EVALUATIONS.md` created** at repo root, matching the format agreed
    in `EXPERIMENT-PLAN.md` A.a: newest-entry-first, fixed headings (What
    ran / What it showed / Comparison to prior runs / Interpretation /
    Implications / Next steps / Longer-term notes), deep dives deferred to
    `docs/evaluations/` (not created yet — no entry has needed one).
  - **b. First entry backfilled**: the 2026-08-16 exp01 read, covering all
    four Round 2 reports against the three Round 1 baselines — cfgA 4/8
    aligned (clear signal), cfgB 2/8 (null), the early-feedback lock-in
    hypothesis, and Paul's seed-share reframe (conv MUs as permanent
    infrastructure needing path-back, not noise to suppress). Numbers kept
    to inline evidence for specific claims, not copied tables — the guardrail
    the format itself specifies.
  - **c. Setup-and-goals doc convention made concrete**:
    `plans/TEMPLATE_setup-and-goals.md`, a copyable three-heading template
    (What we adjusted / What we're watching for / How we'll measure) matching
    A.c's spec exactly, with the naming rule (same basename as the plan
    file, `.md` beside its `.json`) stated at the top and stripped from the
    copy.
  - `EXPERIMENT-PLAN.md` item A and all three subitems marked done, each
    pointing at the file it produced.
  - Files changed: `EVALUATIONS.md` (new), `plans/TEMPLATE_setup-and-goals.md`
    (new), `EXPERIMENT-PLAN.md`, `PROGRESS.md`. No code, no tests — a pure
    documentation milestone; BUILD-PLAN.md untouched (this is an
    EXPERIMENT-PLAN item, not a numbered build milestone).
  - **Next step:** Alpha B (rep3 setup doc from the new template, then run
    `exp01-cfgA-rep3`) with Alpha C (lock-in analysis) in parallel.

- 2026-08-16: exp01 read session + EXPERIMENT-PLAN.md created — no code
  - **The read (all four exp01 reports vs the three Round 1 baselines):** cfgA 4/8 aligned slots (rep1: vs_a1 only, dominant at 0.92–1.00; rep2: vs_b3 0.36, vs_c1 0.16, vs_d5 0.96) = pre-registered "clear signal", vs baseline 2/12. cfgB 2/8 (rep1: vs_a1 0.12, vs_c1 0.40; rep2: none) = pre-registered null. Trained-subset final Δ (raw): cfgA 0.00 / −0.13, cfgB −0.12 / −0.22 — only cfgA rep1 escapes the Round 1 envelope [−0.46, −0.12]. Pin-adjusted (M41) deltas small-positive in 3 of 4 runs; vs_c1/vs_d1 pinned again in several controls, vindicating M41. Universal dynamics held in all 4 runs: seed-share collapse (~45–55% → 4–17%), within-run up-rate decay, cross-cluster/mixed edges a trickle (2–9), zero failures/pressure/saturation. Mean-mode metrics flat 0.0000/4.0 again — dead instrument. cfgB rep1's 69.2% up-rate is the highest ever recorded, but rep2's 48.2% is in the normal band, so "wider K → better-judged answers" is one replicate's suggestion. cfgA's pre-registered "too slow to concentrate" signature did NOT appear (champions ≥ baseline band), so the Round 3 redirect to 2.0/2.0 priors is not triggered.
  - **The central open question sharpened:** identical configs produce a 1.00 champion in one replicate and flat 0.00 for the same probe in the other, and rising curves appear on untrained spokes (vs_d3 twice) — concentration without alignment. Hypothesis: the winner is decided by an early-feedback lottery in session 0–1, amplified rich-get-richer, with config only shifting the odds. Testable on existing data (concept Ba).
  - **Paul's decisions this session** (recorded in EXPERIMENT-PLAN.md, Plan Concept Alpha): (1) run cfgA rep3 (Alpha B); (2) do the lock-in analysis (Alpha C); (3) **seed-share collapse reframed** — conv MUs are intended infrastructure, not noise; the goal is making them *stepping stones back* to the seed MU (strong seed↔prompt/seed↔response strengthening on upvoted seed-centered answers, retrieval that chases highest-weight edges back to a base MU). In a deployed system seeds won't be designated — they'll be enthroned/dis-enthroned by connected edge weight. Decay deferred; suppressing conv MUs rejected (Alpha D/F). (4) Judge: post-run re-grade loop with Sonnet/Opus over larger samples (Gemma failure rate ~10–20% from prior calibrations); Paul's leading explanation for the up-rate down-slope is *incomplete information in retrieved conv MUs*, i.e. a real retrieval problem the judge correctly reports, not judge drift — the strong-judge pass distinguishes these (Alpha E). (5) Cross-cluster transfer: keep passive tracking now, dedicated series later (Alpha standing item). (6) New process: EVALUATIONS.md run-evaluation log (root, fixed headings, deep dives in `docs/evaluations/`) and per-experiment setup-and-goals doc in `plans/` with the plan file's basename, from rep3 forward (Alpha A).
  - **EXPERIMENT-PLAN.md created (repo root):** experiment planning moves out of BUILD-PLAN.md (which had bloated with experiment-specific reporting work). Each planning session yields an independent Greek-lettered plan (Alpha, Beta, …); within a plan, items A/B/C in time sequence (A completes before B unless noted), subitems a/b/c, then aa/ab, non-sequential lists bulleted. Explicitly not milestones and not numbered. Code needed by a plan item still becomes a BUILD-PLAN milestone. (Layout revised same session from an initial theme-letter scheme, which wouldn't have survived successive planning sessions.)
  - Files changed: `EXPERIMENT-PLAN.md` (new), `PROGRESS.md`. No code, nothing committed yet.

- 2026-08-13 (b): Milestone 43 (plan-declared config state) planned — /planner, no code, branch `claude-milestone-43-plan-declared-config`
  - **Origin:** the exp01 queueing question ("run replicates separately or queue them?") surfaced that `RunPlan` cannot express hyperparameters, so cfgA and cfgB plans are byte-identical except `plan_id`/`plan_name` and the actual knob lives in hand-edited `config.yaml`. Nothing in the harness reads `config.yaml`, so a forgotten edit yields a clean run, clean reports, and passing §6.2/§6.3 checks that are silently a replicate of the previous config.
  - **Why this reverses M42's explicit non-goal.** M42 declined RunPlan hyperparameter overrides on cost-of-repetition grounds ("two hand-switched queues do not justify it yet"). The answer flips on information M42 did not have: the cost that matters is not the switching but that **the mistake is undetectable before it has already wasted the night** (~5 h queue + a read session analyzing a config that was never live). It also resolves an inconsistency with the queue layer's own non-goal — "a wrong plan fails preflight and a human fixes it" — which a wrong `config.yaml` does not satisfy. Justified by the missing *check*, not the missing automation, which is why Phase 1 is the assertion and ships first.
  - **The design finding that shapes Phase 1:** `GET /admin/config` already exists and is the obvious thing for preflight to call — and it would **certify the exact bug it exists to prevent**. It re-opens `config.yaml` from disk per request (`admin.py:254`), while the orchestrator binds `_CONFIG` once at import (`orchestrator.py:54`). Edit the file, skip the restart, and the endpoint returns the new values while every turn runs on the old ones: check green, run wrong. Phase 1 therefore requires a new additive `GET /admin/effective-config` reporting the orchestrator's *resolved* state; `/admin/config` is left alone (the Admin UI reads it as the editable registry).
  - **Provenance is already correct — the gap is strictly pre-run.** Verified against the real `memory/graph.db`: `full_config_snapshot` coverage is **100%, 2,300+ analytics rows across every experiment back to 2026-07-03, zero empty**. Snapshots diff cleanly (exp0-a vs exp0b-rep3 → 0 differing leaves across all 57 config leaves — the correct answer for a same-config re-baseline). Historical comparability is a read problem, not a re-run problem.
  - **Naming hazard recorded:** two unrelated things are called `config_hash` — `orchestrator.py:67` hashes live `config.yaml` (stamped per run row); `harness/manifest.py` hashes the `RunPlan` (drives `run_id`/duplicate detection/resume). The milestone calls them `runtime_config_hash` and `plan_config_hash` in prose; **neither field is renamed** (both persisted).
  - **Paul's decisions this session:** (1) build **both** phases before exp01 runs; (2) overrides are **per-plan, not per-queue** — the plan is already the unit of identity, so per-plan puts config inside `plan_config_hash`, which is the root-cause fix, where per-queue would leave it outside the hash and recreate the ambiguity one level up; (3) **cross-config queueing becomes legal** once each plan carries its own config; (4) Phase 1 scope = **declared keys hard-stop + record `runtime_config_hash` and the full snapshot in the manifest**, rejecting an exhaustive curated key list.
  - **Why not exhaustive** (Paul leaned that way initially; resolved on evidence): historical comparability is already solved by `full_config_snapshot`, so exhaustive declaration re-records what run rows already hold; "exhaustive over config.yaml" is the wrong scope (`model.ollama_host` legitimately differs host-vs-container, `experiment.*` is overridden per-condition by the harness's own env stamping), so it means a curated subset — a new artifact to maintain; and every new config key would then either invalidate every plan or make it silently under-specify while *looking* exhaustive. **Phase 2 dissolves the question by supply rather than assertion:** once the backend takes values from the plan, a plan supplying the experimental surface cannot drift from itself.
  - **Phase 2 shape:** never mutate `config.yaml` (git-tracked, bind-mounted; a crash mid-queue would leave the tree dirty with experiment values — M42 step 7's hazard). Instead extend the existing env→config→default resolution, for which there is direct precedent: `_resolve_experiment_id()`, `USE_SESSION_CONTEXT`, `_resolve_run_condition()`, and M9 solving the identical "avoid config.yaml edits between runs" problem for `condition_label` that exact way. One `CONFIG_OVERRIDES_JSON` env var carrying dotted-key JSON, **not** one compose entry per knob (compose needs a static `${VAR:-}` line per variable, so per-knob means editing compose for every newly-sweepable hyperparameter). `docker_lifecycle.py` already merges `env_overrides` into the per-condition restart — no structural change.
  - **Risks recorded:** import-time merge ordering is Phase 2's whole correctness surface (`_CONFIG_HASH`, `EDGE_UPDATE_CFG`, `_HP`, `INITIAL_EDGE_WEIGHT` all bind at import — the merge must precede all of them); **`memory/extractor.py` loads config independently** (`_CONFIG = _load_config()` at line 53) and would not see the merge — silent divergence between two modules' views of config is exactly the bug class this milestone exists to kill; `decay.enabled: true` must still hard-reject through `validate_edge_update_config()`. Verified this session: no `config.yaml` key contains a literal dot (57 leaves), so the dotted-path scheme is safe today — worth a preflight assertion in case one is ever added.
  - Files changed: `BUILD-PLAN.md` (new "Plan-Declared Config State — Milestone 43" section before `## Status`), `PROGRESS.md`. No code.
  - **Next step:** /build Milestone 43 Phase 1, then Phase 2, then run exp01 fully automated.

- 2026-08-13: `/debugger` — exp01 pre-flight blocked by a phantom `ModuleNotFoundError`; `REVIEW-NOTES.md` created — branch `claude-review-notes-init`
  - **Symptom:** running the harness pre-flight per `docs/harness_usage.md`, the host test suite died during collection with `ModuleNotFoundError: No module named 'aiosqlite'` at `backend/memory/store.py:17` — despite `aiosqlite==0.20.0` being pinned in `backend/requirements.txt` and confirmed installed by `pip list`. Docker came up clean, which masked it: the container has its own interpreter and its own install, so stack health said nothing about the host.
  - **Root cause — wrong interpreter, not a missing dependency.** Two independent facts collided: (1) backend deps were installed with `pip install --user`, which puts packages in `AppData\Roaming\Python\Python39\site-packages` (importable, hence `python -m pytest` works) but console scripts in `AppData\Roaming\Python\Python39\Scripts`, which is not a `PATH` entry — so this project has no `pytest.exe` on `PATH` at all; (2) an unrelated project's virtualenv, `Projects\Demonstration\shakespeare_poet\venv\Scripts`, is the **first** entry in the persisted *user* `PATH` (not an activated venv — `VIRTUAL_ENV` was empty). Bare `pytest` therefore resolved to that venv's runner under **Python 3.11.9**, which imported Corvus's `store.py` and accurately reported that *its* environment had no `aiosqlite`. Meanwhile `python` resolves to `C:\tools\miniconda3\python.exe` (3.9.12), which has everything.
  - **The diagnostic tell was pytest's own header line** (`platform win32 -- Python 3.11.9, pytest-9.0.2`), not the traceback — the project runs on 3.9.12, and that one line identified the problem before any of the stack mattered.
  - **Fix:** `python -m pytest`, which resolves the runner *through* the named interpreter so runner and code-under-test cannot disagree. Verified: **1093 passed in 18.8s**, no code changes required — the tree was green the whole time.
  - **Docs corrected:** `docs/harness_usage.md` Quick Start (Paul, commit `48bb8f6`) plus its §2 prerequisites checklist and `docs/experiment_protocol.md`'s pre-flight block (this session) — all three recommended bare `pytest`, which is what routed into the failure. No bare-`pytest` invocations remain in `docs/`.
  - **Created `REVIEW-NOTES.md`** — the cumulative pattern library named in the global skill flow but never instantiated on this project. First entry is the above, written as a recognizable pattern (*a `ModuleNotFoundError` for a package you know is installed is an interpreter question, not a dependency question*) rather than a one-off incident log. The file also carries a **Deferred / outside work** section for items surfaced but consciously not fixed.
  - **Deferred by Paul's decision:** removing the stale `shakespeare_poet\venv\Scripts` entry from the user `PATH`. It would close this class of failure permanently — that venv also exports `fastapi.exe`, `uvicorn.exe`, `httpx.exe`, so any bare invocation of those prefers it too — but it is a system-level change reaching into a different project. Logged in `REVIEW-NOTES.md` under Deferred / outside work; `python -m <tool>` is the standing workaround and the better habit regardless.
  - **Next step:** exp01 is unblocked — pre-flight passes with `python -m pytest`.

- 2026-08-12 (d): Milestone 41 (pin-adjusted trained-subset Δ) BUILT and live-verified — /build, branch `milestone-41-pin-adjusted-trained-subset-delta`
  - **Built exactly to the (c) plan below:** `_compute_session_mean_deltas()` extracted from `_compute_trained_subset_comparison()`'s inline arithmetic, called twice (raw trained list, unpinned trained list) so the two mean definitions run through one code path; `_compute_trained_subset_comparison()` gained an optional `pinned_probe_ids` param (default empty set, every pre-M41 call site unaffected) and four new additive output keys plus a `control_pinned` flag per per_probe row; `generate_control_shadow()` reordered so `pinned_probe_ids` is computed before the trained-subset call, feeding both M39's and M41's pin-adjustment from one computation. Markdown block retitled `(M39/M41)`, gained the pinned-trained-probes line, a second headline figure, and two table columns — both means always shown with their probe counts. All-pinned degrades to `None`/count-0 (never `0.0`), gated for backward compat on **presence** of the M41 keys, not a defaulted zero, so a pre-M41 stored report's `matched_probe_count_excluding_pinned` defaulting to 0 can never trigger a false "all trained probes pinned" note. 10 new tests (1093 total, no regressions).
  - **Live verification against the real `memory/graph.db`** (direct `generate_experiment_report()` calls via a scratch script — `MEMORY_PATH` pointed at the real `memory/` dir, no Docker/Ollama needed since this is pure report-layer read access): regenerated `exp0-2026-07-26_learned` (exp0-a), `exp0b-2026-08-06_learned` (rep1), `exp0b-rep2-2026-08-09_learned`, `exp0b-rep3-2026-08-09_learned`, overwriting the stored JSON/MD in `memory/reports/` (gitignored, DB-derived, reproducible — the M33–35 retroactive-regeneration precedent).
  - **Done-criterion, confirmed:** raw trained-subset final Δ came back **exactly unchanged** for all three Round 1 replicates — rep1 −0.46, rep2 −0.20, rep3 −0.12 — matching the pre-M41 stored values bit for bit. The M39 §4 all-probe pin-adjusted finals also came back exactly unchanged: +0.096 / +0.0074 / +0.0385 (rep1/rep2/rep3, rounds to the previously-recorded +0.096/+0.007/+0.039). Both are proof the milestone touched nothing outside its scope.
  - **One correction the live data surfaced, against the (c) plan's own hand-worked rep3 table:** the plan assumed `vs_a1` was control-pinned in rep3 (from its 1.0 *final-session* control value) alongside `vs_c1`, predicting excluding-pinned Δ = +0.72 (n=2). The real per-session data: `vs_a1`'s static control reads **[1.0, 1.0, 0.0, 0.0, 1.0]** across rep3's 5 sessions — not 1.0 throughout — so the unchanged "1.0 in *every* session" rule correctly does not pin it. Only `vs_c1` ([1.0]×5) qualifies. Real result: excluding-pinned final Δ = **+0.16** (n=3: vs_a1 −0.96, vs_b3 +0.76, vs_d5 +0.68 → mean +0.16), arithmetic-verified by hand against the per-probe rows. This is not a code defect — it is the plan's own hand-tally being wrong, corrected by the code that now exists to compute it precisely instead of eyeballing it, which is the exact failure mode M41 was built to remove.
  - **Full per-replicate pin picture (static_weight), correcting the "vs_a1 3/3" claim carried since the 2026-08-11 Round 1 read:** vs_a1 pinned in **1 of 3** (exp0b/rep1 only — genuinely 1.0 in both of rep1's sessions with data), vs_c1 pinned in **2 of 3** (rep1, rep3) — the vs_c1 fraction was already correct; the vs_a1 fraction was a generalization from the one verified case (exp0b) to all three replicates without individually checking rep2/rep3. exp0-a (not a Round 1 replicate, regenerated separately per scope §8) also pins only vs_c1 (excl-pinned final Δ −0.29, n=3).
  - **Recomputed pin-adjusted trained-subset Δ envelope across Round 1 (rep1/rep2/rep3): [−0.20, +0.16]** (0.0, −0.20, +0.16) — narrower than and overlapping zero, unlike the raw envelope [−0.46, −0.12] which sits entirely negative. Recorded in BUILD-PLAN M42's decision rule alongside the raw envelope, naming which metric each governs, per scope §7.
  - **Read implication for M42/exp01, not yet acted on (Paul's call at the read session):** the pin-adjusted envelope crossing zero means a config's pin-adjusted trained-subset Δ needs to clear a directional bar (positive and outside [−0.20, +0.16]) to claim signal on this metric alone — the winner-alignment table remains co-primary as originally intended, more so now that this envelope is wider relative to zero than the raw one.
  - Files changed this session: `backend/analytics/reports/control_shadow.py`, `backend/analytics/reports/markdown_renderer.py`, `tests/test_analytics/test_control_shadow.py`, `tests/test_analytics/test_markdown_renderer.py`, `tests/test_analytics/test_experiment_report.py`, `BUILD-PLAN.md` (M41 status → COMPLETE, worked example corrected, M42 decision rule amended with both envelopes, Milestone 41 entry appended to the Status block), `PROGRESS.md`, plus 4 regenerated files in `memory/reports/` (gitignored).
  - **Next step:** open the PR for review/merge, then proceed to the exp01 (M42) run nights and read session, now with both envelopes on record.

- 2026-08-12 (c): Milestone 41 (pin-adjusted trained-subset Δ) planned — /planner, no code
  - **The defect, restated from the Round 1 read:** M39 built pin-adjustment for the *all-probe* by-phenomenon mean (§4) and the trained-subset comparison (§2) but never connected them. The trained-subset Δ — the number every Round 2+ decision rule keys off — still averages over trained probes whose control sits at a degenerate 1.0 tie-break ceiling. rep3 final session: (+0.76 vs_b3) + (+0.68 vs_d5) + (−0.96 vs_a1) + (−0.96 vs_c1) / 4 = **−0.12 raw vs. +0.72 excluding pinned**. The bias is systematic, not incidental — static pinned trained `vs_a1` in 3/3 replicates and trained `vs_c1` in 2/3.
  - **It is one missing wire, not new machinery.** Both inputs already exist and are already computed per control; in `generate_control_shadow()` (`control_shadow.py:735-744`) `pinned_probe_ids` is simply computed *after* the trained-subset call, so part of the change is a reordering.
  - **Design decisions recorded in the milestone:** reuse `_compute_pinned_probes()` verbatim (two pin definitions in one report is the exact failure mode M39's `_aggregate_deltas_by_phenomenon` extraction existed to prevent); extract the session-mean arithmetic into one helper called twice, so "trained-subset mean Δ" cannot acquire two definitions; pinned status stays control-specific (exp0b pinned `vs_d5` under random only); existing keys/values untouched (M34 never-silently-redefine), new keys absent from pre-M41 JSONs render `—` (M32 backward-compat).
  - **Two hazards the scope makes explicit.** (1) All-trained-probes-pinned is reachable with only 4 trained probes — the adjusted mean must be `None`/`—`, never `0.0`, which would read as "no effect" and be indistinguishable from a real null. (2) **M42's pre-registered [−0.46, −0.12] envelope is the RAW metric's.** Comparing an exp01 pin-adjusted Δ against it is apples-to-oranges, so M41 §7 requires regenerating the three Round 1 replicates, recording the pin-adjusted envelope here, and amending M42's decision rule to name both envelopes with which metric each governs — done to M42's rule text this session, the envelope value itself awaits the build.
  - **Small-n risk, accepted and mitigated by rendering rather than by dropping the metric:** excluding 2 of 4 trained probes leaves an n=2 mean that swings on one probe. Both means always render with their probe counts, and the winner-alignment table stays co-primary in the read rather than the adjusted Δ becoming sole authority. If the recomputed envelope turns out wide enough to make exp01 unfalsifiable on this metric, that is itself the finding — say so, and let M42's ≥4/8 alignment rule carry the decision.
  - **Done-criterion for the builder:** regenerate exp0-a, exp0b, and all three Round 1 replicates; rep3 must come back pinned={vs_a1, vs_c1}, raw final Δ **unchanged** at −0.12, excluding-pinned +0.72; and the M39 §4 all-probe finals must still read +0.096 / +0.007 / +0.039 (proof nothing else moved).
  - **Open question left for the build, not assumed:** whether rep1/rep2 pin more than {vs_a1, vs_c1} — resolve by reading the regenerated reports.
  - Files changed this session: `BUILD-PLAN.md` (new M41 section between M40 and M42; M42's "Sequencing with M41" and trained-subset decision rule amended), `PROGRESS.md`. No code.

- 2026-08-12 (b): Milestone 42 / Round 2 (exp01) drafted — two early-feedback-alignment configs, 2 replicates each
  - **Scope decision:** exp01 tests the single sharpest question from Round 1 — can a config make early feedback land on the trained spoke and stick — with one knob per config, each attacking a different stage of the selection loop: **cfgA slows the evidence side** (seed-edge Beta priors 1.0→3.0: same 0.5 mean, 3× evidence mass, Thompson draws overlap longer, exploration survives deeper into each series so consistency beats early variance) and **cfgB widens the exposure side** (K2 top-k 1→2: two spokes surface per traversal, trained spoke enters the retrieved set ~2× as often early; verdict credit is shared by both retrieved spokes' edges, so this raises the rate of informative trained-in/trained-out contrast turns, not per-turn discrimination).
  - **Knob audit recorded in the milestone:** `credit_lambda` and `negative_scope` are inert under current single-hop retrieval (every credited path is a single edge at hop_k=0 — read directly from `edge_update.py`'s `get_path_edge_ids_for_mu`/`compute_path_increments`); `credit_scale` reduction is near-equivalent to cfgA; `evidence_cap` fixes late lock-in, not early selection (Round 3 candidate); asymmetric up/down credit needs code; conv-MU accumulation knobs excluded on the standing measure-don't-control rule.
  - **The structural discovery that shaped the protocol: RunPlan has no hyperparameter-override mechanism.** Configs live in `config.yaml` (bind-mounted per `docker-compose.yml`), so each config is a documented working-tree edit + backend restart, verified by a tested one-liner *before* the queue burns a wipe+seed, reverted by `git checkout -- config.yaml` after. Provenance is not the plan's `config_hash` (plan-only) but `analytics.full_config_snapshot`, stamped on every run row by `orchestrator.py:656` — the read session's first act per experiment is confirming it. A RunPlan-override milestone was considered and deliberately deferred: two hand-switched queues don't justify it; revisit if config rounds continue past Round 3.
  - **cfgB's measurement consequence, pre-registered:** retrieved set 4→5 MUs; `pass_rank_thresholds.spoke_retrieval` moves 4→5 (config.yaml's own comment mandates adjusting with K1/K2). Pass-rate *levels* under cfgB are incomparable to baseline/cfgA — only per-cluster winner/alignment tables and within-run learned-vs-static deltas cross configs. Watch §5.8 context pressure (Round 1: zero pressure turns; +1 MU expected fine against the 2048 budget).
  - **Seeds 20260809/20260810 deliberately reuse Round 1's replicate seeds** — same turn/probe sequences as baseline rep2/rep3 gives a quasi-paired secondary read (at matched seed, did the config change the winner?), and rep2-of-each runs the seed where baseline produced its only trained-spoke wins (vs_b3/vs_d5): preserve or disrupt. Not true pairing (response stochasticity + edge-RNG stream differ); variance reduction only.
  - **Pre-registered decision rules** (in BUILD-PLAN M42 before any data): per config, 8 final-session cluster-slots across 2 replicates — ≥4/8 aligned = clear signal, 3/8 = extend with a third replicate, ≤2/8 = null vs the 2/12 baseline; trained-subset Δ claims movement only outside Round 1's [−0.46, −0.12] envelope; cfgA's "too slow to concentrate" signature (no champion at the baseline 0.44–0.84 level) is a finding that redirects Round 3 to 2.0/2.0 priors, not a dead end.
  - **Validated:** all 4 plans load through `RunPlan`, config hashes distinct from each other and from exp0b/rep2/rep3 (`32904494` / `666ec8f8` / `fd4f53ac` / `49f7a62f`), bank path exists, both queues load through `load_queue_file()` with resolving plan paths, and the config-verification one-liner prints baseline values against the current config.yaml. One typo caught in validation prep (truncated bank hash in cfgB-rep2) — fixed before commit.
  - **M41 recommendation to Paul: yes, build it, separate session, before the exp01 read** — static pins landed on trained probes in every Round 1 replicate (vs_a1 3/3, vs_c1 2/3), so exp01's primary comparison number carries a structural negative bias until the pin-adjustment covers the trained subset; the runs don't wait on it (retroactive regeneration, M33–35 precedent).
  - **Next step:** merge this PR → night 1 cfgA queue, night 2 cfgB queue (config-switch protocol in BUILD-PLAN M42, restart Ollama each night) → read session.
  - Branch: `claude-exp01-round2-prep`.

- 2026-08-12: d_spoke6 lexical-overlap check — the "structural advantage" hypothesis is REFUTED
  - **Trigger:** the Round 1 read flagged vs_d6 winning cluster_d in 2 of 3 replicates under different seeds as possible evidence of a structural (BM25-lexical) advantage for d_spoke6's content. Checked directly, replicating `backend/memory/retrieval.py`'s exact pipeline (same `_tokenize` regex + stopword set, first-5-keyword window, BM25Okapi over the 34-MU seed_v3 corpus, per-keyword top-20 pool, ≥2-hit K1 candidacy) against all 210 bank prompts. Script: session scratchpad `d6_overlap_check.py`.
  - **d_spoke6 is the *least* lexically favored spoke in its cluster on every measure:** lowest K1 candidacy (29/52 d-cluster prompts vs 32–41 for siblings), lowest mean BM25 score (1.38 vs 1.91–4.25), and it enters the s0 uniform-weight K1 top-3 on just 1/52 prompts (siblings: 6–26). Topology is symmetric too — every d spoke has exactly 2 reciprocal hub edges at 0.5; only the hub touches bridge_cd. No advantage exists on any structural channel.
  - **The 2-of-3 recurrence is statistically unremarkable:** with 7 spokes per cluster, P(at least one of the 4 clusters shows a repeated winner across 3 replicates) ≈ 86% even under uniform chance — and real winner selection is *not* uniform, which only raises it. d6's wins are what the rich-get-richer mechanism looks like when early K2 traversal (winner-take-all on the strongest sampled hub↔spoke edge) happens to land on it: selection is chance, persistence is the mechanism. This *strengthens* the Round 1 path-dependence conclusion.
  - **Corroborating detail:** rep3's trained-spoke wins line up with lexical exposure — b_spoke3 (3.48) and d_spoke5 (4.25) are the top-scoring spokes in their clusters, so when chance seeds them, BM25 co-occurrence keeps feeding them. And probe-side blindness holds: no spoke_retrieval probe lands ≥2 keywords on its own gold (max 1 hit, below the K1 threshold), confirming probe pass-rates measure edge weights, not probe wording.
  - **Caveat:** analysis is against the s0 seed-only corpus with uniform 0.5 weights (the state early stochastic feedback acts on); accumulated conversation MUs change the BM25 index as a series progresses. Candidacy/score columns are weight-independent; only the K1-top-3 column approximates.
  - **Consequence for Round 2:** no fixture surgery needed — d6 is not a rigged winner. Winner-identity instability is a genuine property of the mechanism, and the 2/12 alignment baseline stands as stated.

- 2026-08-11: Milestone 40 / Round 1 read — baseline noise floor established, first trained-spoke wins observed
  - **Run health:** rep2 clean end-to-end. rep3's `static_weight` arm timed out at (s2, turn 31) overnight (`chat call failed (timeout, not retried)` — the deliberate no-retry-on-timeout rule) and was resumed next morning via M27; resumed series reports show 0 failed turns and probe-integrity PASS, learned arm was already complete, both §6.3 blocks PASS. Replicate accepted. The queue summary's odd rep3 numbers (plan duration 1934 s vs static_weight=49998 s) are resume bookkeeping — the per-condition figure includes the ~13 h overnight gap, the plan figure only the second invocation.
  - **Pre-registered question (a) — does a trained spoke ever win? YES, in rep3.** vs_b3 (trained b_spoke3) is cluster_b's winner in all 5 sessions — the only session-stable winner in that run — following a dip-and-recover arc (0.76→0.12→0.16→0.44→0.76) where rep1's early vs_b3 spike (0.52) had starved to 0. And vs_d5 (trained d_spoke5) takes cluster_d in the final session with a late 0→0.68 jump. This reframes the exp0-a/exp0b conclusion: the mechanism is not *incapable* of alignment — alignment is stochastic. When early feedback happens to favor the trained spoke, rich-get-richer amplifies it exactly as it amplifies a neighbor. Baseline alignment rate to beat: **2/12 cluster-slots** (rep1 0/4, rep2 0/4, rep3 2/4).
  - **Question (b) — winner stability: NO, as predicted.** No cluster keeps the same winner across all three replicates. Exception worth flagging: **vs_d6 wins cluster_d decisively in both rep1 (0.84) and rep2 (0.44, stable 0.44–0.68 all sessions) under different seeds** — 2-of-3 recurrence of one specific untrained spoke is weak evidence of a structural (plausibly BM25-lexical) advantage for d_spoke6's content, not pure path-dependence. Cheap pre-Round-2 check if desired: eyeball d_spoke6's keyword overlap with the d-cluster bank prompts.
  - **Question (c) — noise floor.** Trained-subset final-session Δ vs static: **−0.46 / −0.20 / −0.12** (rep1/rep2/rep3). All negative, ~0.34 spread. **Metric caveat discovered in the read: the trained-subset Δ is pin-contaminated** — M39's pin-adjustment applies only to the all-probe mean, not the trained subset. rep3's −0.12 decomposes as (+0.76 b3) + (+0.68 d5) + (−0.96 a1) + (−0.96 c1) / 4, where a1/c1's controls sit at a 1.0 tie-break ceiling. The static tie-break lands on vs_a1 (trained) in all three replicates and vs_c1 (trained) in two, so the trained subset is *structurally* biased negative under this control. **Round-2 reading rule: winner-alignment table and pin-adjusted deltas are primary; raw trained-subset Δ is secondary with the pin caveat attached.** Optional small M41: pin-adjusted trained-subset Δ in `control_shadow.py` (the per-probe rows to hand-compute it are already in every report).
  - **Replicate variance that resets two earlier conclusions:** (1) up-rate across identical-config replicates spans 29.1%–44.0% — rep1's "punishment-dominated 29.1%" was partly seed luck; any config-round up-rate claim under ~15 pp is noise. (2) The "up-rate declines in lock-step with seed-share" story from rep1 did not fully replicate: rep2/rep3 up-rates recover in s3–s4 (42.1% / 41.7% final) while seed-share stays collapsed (5.9% / 10.7%) — the two are decoupled once past s1; the agent earns ups from conversation-MU retrieval, consistent with conv↔conv edges taking the majority of updates (133/198 rep2, 107/194 rep3).
  - **Seed-share collapse is now 4-for-4** (45.2→4.2, 47.6→5.9, 53.6→10.7 across exp0b + reps; exp0-a matches) — a robust baseline property, per the 2026-08-02 decision measured, not controlled.
  - **Decision recorded: Round 1 complete, baseline characterized, GO for Round 2 config experiments.** What a config round must show to claim an effect: alignment clearly above 2/12 (e.g. 3–4 of 4 clusters aligned in a single run), or a trained-subset/pin-adjusted delta outside the replicate envelope above. Given the demonstrated variance, plan config rounds as ≥2 replicates per config or demand large effects.
  - No code changed this session; read-only analysis of `memory/reports/experiment_report_exp0b-rep2-2026-08-09_learned.md`, `...rep3...`, and the regenerated exp0-a/exp0b reports.

- 2026-08-09 (b): Milestone 39 built (config-comparison endpoints) + live verification
  - **Built exactly the scope drafted in the session above:** `backend/analytics/reports/spoke_membership.py` (new) resolves `RunPlan.trained_spokes` to probe ids via a duplicated spoke_key -> mu_id table -- duplicated, not imported, because `scripts/` (the fixture generator's home) is never mounted into the backend container (checked `docker-compose.yml`: only `backend/`, `probes/`, `seed_fixtures/` cross that boundary), mirroring the M29 `TRAINED_SPOKE_CLUSTERS` precedent one boundary over. Cross-checked in tests against the real frozen `seed_fixtures/seed_v3.json.gz` and `probes/probe_set_v3.yaml` on disk, not just against itself.
  - **`experiment_report.py._compute_cluster_winner_table()`:** per-cluster argmax-by-sample-pass-rate, one row per cluster per session, `trained` flag, ties recorded explicitly (`tied_with`) with deterministic probe_id-ascending tie-break. Computed identically for the learned condition and every control via the same `_section_61_learning_measures()` call both sides already go through.
  - **`control_shadow.py` gains two new comparisons:** `_compute_trained_subset_comparison()` (the primary config-comparison number the milestone exists for -- restricts the existing sample-mode learned/control comparison to just the trained probes, mean delta per session + final) and `_compute_pinned_probes()`/`_compute_pin_adjusted_mean_deltas()` (pinned = control pass_rate 1.0 in *every* session, not just the final one -- both the unmodified all-probe mean and the excluding-pinned mean are always rendered side by side, M34's never-silently-redefine rule).
  - **Threaded end-to-end, not just admin-endpoint-optional:** `trained_spokes`/`seed_fixture_version` are new optional params on `generate_experiment_report()`/`generate_control_shadow()`/`_section_61_learning_measures()`, the admin `POST /reports/generate-experiment` request body, and `scripts/harness/client.py`/`runner.py` (`_generate_experiment_report()` now passes `plan.trained_spokes`/`plan.seed_fixture_version` at experiment close) -- so every future harness run populates these sections automatically, not only reports generated by hand. Backward compatible: omitting the param (every pre-M39 call site) degrades to empty trained_probes/cluster_winner_table, never a crash.
  - **91 new tests, 1083 total, no regressions:** new `test_spoke_membership.py` (16 tests, including the fixture/probe-set cross-checks), additions to `test_learning_measures.py` (winner/argmax/tie arithmetic), `test_control_shadow.py` (trained-subset + pin-adjusted computation), `test_experiment_report.py` (end-to-end through real TempDB + `generate_experiment_report()`, including a pre-M39-call backward-compat test), `test_markdown_renderer.py` (renders-with-data and renders-without-crashing-when-absent for every new block), and `test_runner.py` (the new client call shape).
  - **Live verification (the milestone's own done-criterion), not a synthetic check:** regenerated `experiment_report_exp0-2026-07-26_learned` and `experiment_report_exp0b-2026-08-06_learned` via the real `POST /admin/reports/generate-experiment` endpoint against the real `memory/graph.db` (Docker backend healthy throughout), with `trained_spokes={cluster_a: a_spoke1, cluster_b: b_spoke3, cluster_c: c_spoke1, cluster_d: d_spoke5}`. **exp0b's per-cluster final-session winners: a_spoke2 / b has no stable winner (all 7 tied at pass_rate 0.0) / c_spoke3 / d_spoke6 -- none trained.** This is an exact match to the pre-registered hand-analysis the milestone's own scope note wrote down *before* the sections existed -- the done-criterion passed, not just "ran without crashing." **Pinned-probe classification caught exactly the worked example from the milestone spec:** `vs_a1`/`vs_c1`/`vs_d1` pinned under `static_weight`, `vs_d5` pinned under `random_weight`, on real exp0b data. **Pin-adjustment's effect was exactly the predicted direction:** the static-weight sample-mode mean delta moved from ~+0.002 (all 28 probes) to +0.118 (excluding the 3 pinned) -- confirming that control tie-break pins were cancelling learned's real gains in the old all-probe mean, exactly the exp0b-read finding that motivated building this section. **Trained-subset final-session mean delta came back negative against both controls on exp0b** (static -0.46, random -0.21) -- the trained spokes did not win their own clusters under the baseline config, which is now a number instead of an inference from eyeballing per-probe rows. Also confirmed on exp0-a: cluster_c's winner (vs_c1, at 0.96-1.0) wins under *both* `learned` and `static_weight` -- the table's own alignment-vs-artifact distinction correctly flags this as a tie-break artifact, not evidence the mechanism trained cluster_c.
  - **Next step:** run the Round 1 queue overnight (M40, already drafted and validated -- see the session entry above), then read it through these new sections. PR still needs Paul's review/merge.
  - Branch: `milestone-39-config-comparison-endpoints`.

- 2026-08-09: exp0b baseline read + Round 1 prep (M39/M40 drafted)
  - **Read `experiment_report_exp0b-2026-08-06_learned.md` against exp0-a's.** Full reading recorded in the current-state entry above; the four headline points: aggregate null persists (and is partly an endpoint artifact — control tie-break pins at 1.0 cancel real learned gains in the mean), seed-share collapse and lock-step up-rate decline replicated and now quantified, punishment dominance worsened slightly under the stricter judge (71% decrements), and the concentration winner **moved** (exp0-a vs_d4 → exp0b vs_d6, both untrained neighbors of trained d_spoke5, controls flat) under an identical plan/seed/bank. Confirmed the probe→spoke mapping in `probes/probe_set_v3.yaml` before drawing the alignment conclusion: vs_XN's gold MU is X_spokeN, trained = a1/b3/c1/d5.
  - **Why that ordering of conclusions matters:** "learned moves retrieval where controls don't" is now twice-replicated and real; "learned moves it *toward the trained content*" is twice-refuted. The existing endpoints measure the first claim badly and the second not at all — hence M39 before any config round.
  - **Drafted Milestone 39 (BUILD-PLAN.md):** report-layer only. Trained-probe derivation at report time (`trained_spokes` → gold-MU spoke membership, extending M33's cluster lookup), §6.2 trained-subset sample-mode comparison (the primary config endpoint), per-cluster winner/alignment tables for learned *and* controls (a winner that also wins under static is a tie-break artifact), and pin-adjusted mean deltas (pinned = control pass rate 1.0 in every session — catches a1/c1/d1 static + d5 random on exp0b, correctly excludes b1's single-session 1.0). Both means rendered, all-probe and excluding-pinned — M34's never-silently-redefine rule. Done-criterion: regenerated exp0-a/exp0b reports must reproduce the hand-read winners.
  - **Drafted Milestone 40 / Round 1 (BUILD-PLAN.md + artifacts):** noise floor before knobs. Two replicate plans differing from exp0b only in plan_id/plan_name/random_seed/conditions (seeds 20260809/20260810; learned+static_weight only — random_weight has twice served its purpose and costs a third of the wall clock; re-attach it for any surprising positive later). Same bank/fixture/shape/judge. Queue `queues/exp0b_replicates_2026-08-09.json` (`on_plan_failure: continue`; the M26 "replicates need distinct random_seed" preflight check finally gets used for its intended purpose). Pre-registered read written into M40 *before the data exists*: per-cluster final-session winner + trained? flag, trained-subset final-session Δ vs static, seed-share s0/s4 + up-rate trajectory; questions (a) does a trained spoke ever win, (b) is winner identity stable across replicates (prediction: no), (c) the replicate spread of the trained-subset delta = the minimum effect any config round must exceed.
  - **Validated, not just written:** both plans load through `RunPlan`, config hashes distinct from each other and from exp0b's (`693078fa` / `c5350179` / `80b3f78b`), and the queue file loads through `load_queue_file()` with both plan paths resolving and validating.
  - **Numbering note recorded in BUILD-PLAN.md:** "M38" is the 2026-08-06 judge-leniency/unparseable branch (PR #65) — the exp0b plan_name already calls it that, so the new milestones are 39/40.
  - **Next step:** merge this PR, `/build` M39, run the Round 1 queue overnight (restart Ollama first), then the read session through M39's new sections. Sequencing is flexible — reports regenerate retroactively (M33–35 precedent), so the queue can run before M39 merges if convenient.
  - Branch: `claude-exp-round1-prep`.

- 2026-08-06 (c): judge leniency + unparseable accounting — reading the M37 attempt2 calibration, and acting on it
  - **Trigger:** Paul read `harness_runs/exp0-calibration-2026-08-05-m37_..._attempt2/calibration_summary.md` as clean and ready for exp0b. The instrumentation was clean; the grading standard was not, and the summary said so by its own rule (line 15: a one-sided lenient/strict split is a systematic problem, a balanced one is noise). The pass was **3 lenient / 0 strict**.
  - **The finding that mattered: it reproduced.** The M37 plan reuses M31's `random_seed`, so the two passes graded the *same 20 turns* — a paired comparison, not two samples. M31 (08-02): judge-alone 14 up / 3 down / 3 none vs recorded 12/5/3. M37 attempt2 (08-06): judge-alone 14/4/1 vs recorded 13/5/2. **Judge-alone "up" was 14 in both, every operator correction in both went the same direction.** The literal-match prompt improved what you could see about the grading without moving the grading.
  - **One failure mode, not three.** Reading the judge's own quotes against the criteria: the quote named the criterion's *topic* without asserting its *content* — `"K-1: Addresses field order."` credited for *states fields must appear in the fixed declared order*; `"failure to comply will result in a P-fault"` credited for *notes deviation invalidates parsing*; `"Every field must strictly conform to its designated position…"` credited for *states fields must appear in declared spine order*. **The evidence checks caught none of them** — every quote genuinely was in the response — so the fix had to be in the standard, not the verifier.
  - **Fixed in the prompt:** two flat rules (naming a subject is not stating a fact about it; the quote by itself must satisfy the criterion, nothing else in the response may fill it in) plus a **domain-neutral** worked example. Deliberately not drawn from the Kestrel fixtures — an example from the bank the judge grades would prime it on the very turns under test.
  - **Measured, not assumed — A/B against the real `gemma4:e4b` on the same 20 turns, scored against Paul's regrade as ground truth.** The old prompt reproduced the recorded pass exactly (**27/30, 3 lenient, 0 strict**), which validates the harness. New prompt: **28/30, 1 lenient, 1 strict**, identical across 3 runs (deterministic at temperature 0). The skew is gone; the remaining disagreements are one each way. **This is an improvement, not a fix — see the known issue below.**
  - **The `[CONCEPT]`/`[EXACT]` boundary is where the residue lives.** The surviving lenient call (turn 19: *states fields must appear in declared spine order*) is a criterion carrying specific terminology — "declared spine order" — while wearing a `[CONCEPT]` tag, so the judge is licensed to accept a paraphrase. `_criterion_kind()` derives the tag from the bank's `answer:` convention alone, and this criterion has no such prefix.
  - **The new "strict" disagreement is probably not an error.** Turn 8 criterion 2 (*notes it mandates the encoding declaration*) against `"K-6, which specifically governs encoding"` — that is **structurally the same move** as turn 16's `"K-1: Addresses field order."`, which Paul marked *not met*. The judge is now self-consistent across both; the reference standard is not. Worth deciding which way that criterion should go before the residue is read as a judge defect.
  - **Second fix — unparseable verdicts stopped being silent.** `error_policy["judge_unparseable"]` defaults to a new `record_none` instead of `soft_continue`: the turn stores a `none` verdict with `judge_source="model:<name>:unparseable"` rather than nothing at all. A dropped verdict is indistinguishable in the data from a turn that never ran, and the observed 1-in-20 rate is ~50 such turns across exp0's 1,050. **Learning is untouched** — a `none` triggers no weight update, exactly as recording nothing did. An explicit operator skip in `--interactive` still wins over the policy.
  - **Third — the re-ask now names the rejection reason.** At `temperature 0.0` the old retry re-sent a byte-identical prompt, so the retry budget existed and bought almost nothing. (The single unparseable turn in the pass now parses under the new prompt on the first attempt anyway.)
  - **Fourth — `calibration_summary.md` accounts for every turn.** The hand-graded turn matched no bucket, so the outcomes summed to 19 of 20 while presenting as complete. New buckets for hand-graded and recorded-none, an explicit `Accounted: N of M` line, the judge-alone denominator stated (it is smaller than the recorded one by exactly the excluded count), and the hardcoded "The two flagged rows changed no judgment" sentence — which rendered verbatim on attempt1 with zero flags — replaced with the real count.
  - **Evidence heuristics stay advisory, on evidence.** The one flag that fired (turn 11, `unverified_quote`) was on a criterion Paul confirmed *met*; promoting it to enforcement would have forced `[True, False]` → `[False, False]` and flipped that turn from `none` to `down`. It also caught none of the three real errors. **Do not enforce on this data.**
  - **1038 tests passing.** Verification scripts in the session scratchpad (`ab_prompt_eval.py`, `repeat_new.py`) — they read the checkpoint, manifest, bank and `memory/graph.db` directly and make real Ollama calls.
  - **KNOWN ISSUE — residual judge leniency, unresolved.** 1 lenient / 1 strict on 30 criteria is a balanced split at a sample far too small to call it balanced. The bias is *reduced and no longer one-sided*, not eliminated, and it was measured **only on the `learned` arm** (`dry_run_n=20` grades the first condition in `condition_order`). Nothing is known about how the judge grades `static_weight`/`random_weight` responses. **This matters for exp0b specifically:** leniency triggered by rule-label-parroting answers is plausibly *correlated with the treatment*, since a learned-memory condition produces more of them — the one case where a bias does not cancel between arms. **If exp0b runs on this:** do not compare arms on up-rate alone, and report the judge-alone rate with this limitation attached.
  - **Next step: re-run `plans/exp0_calibration_2026-08-05.json --dry-run --interactive` on this branch.** The A/B is a strong prediction but not a substitute — it reuses stored responses and Paul's existing regrade, so it cannot catch a criterion where the new standard and his judgment now diverge. Optional and higher-value if the residue is to be closed: extend `_criterion_kind()` so criteria carrying specific terminology are held to the `[EXACT]` standard without needing an `answer:` prefix.

- 2026-08-06 (b): failed-turn rollback in the orchestrator — closing the orphan-MU hole the retry PR left open
  - **Trigger:** Paul's call on reviewing PR #63. The retry landed with a known cost — each failed attempt stranded a `user_input` MU — and he rejected leaving it in place before another calibration run. **His reasoning was sharper than the PR's:** retrieval reads at K1=3 + K2=1, a **four-slot window**, so an orphan does not merely add noise, it **evicts** a real seed MU from that turn's context; and because orphans carry no seed edges, traversals reaching them earn evidence on edges the experiment never intended to train. Agreed and implemented — no disagreement to register.
  - **Built — `run_interaction()` is now a thin rollback wrapper** around `_run_interaction_inner()`. The rollback window opens at the first MU write and closes when the inner call returns (which is *after* `capture_run`), so **a turn is either fully recorded — run row + MUs + edges — or leaves no trace at all.** Chosen over re-indenting the 340-line body in a `try:`, which would have been a ~300-line diff for the same semantics. Every `create_mu` site appends to a `created_mu_ids` tracker; `_run_tool_loop` takes the tracker too, since Ollama is called once per iteration and a later iteration can fail after earlier ones already wrote tool MUs.
  - **Catches `BaseException`, not `Exception`, deliberately** — `asyncio.CancelledError` is a `BaseException`, and a client that disconnects mid-turn strands MUs exactly like a crash does. Covers "regardless of source of failure" as specified: gate errors, DB errors, and cancellations roll back, not just `httpx` ones.
  - **The distinction that must not be lost:** `llm_failure_reason` (token limit / empty output) is **not** a rollback case. Those turns complete, return 200, and deliberately record `agent_output_failed_token_limit` / `agent_output_failed_empty_output` MUs — that is experimental data the failure-mode analysis depends on, and the source types are already excluded from retrieval. Only genuine exceptions (the turn never completed) roll back.
  - **Built — `GraphStore.delete_turn_artifacts(mu_ids, run_id)`.** Deliberately narrow, since this is the only non-wipe delete path in the system: it takes an explicit id list plus a run_id and cannot express "delete everything matching a predicate", so it cannot be repurposed into a second wipe. **Both arguments are load-bearing** — `mu_ids` because `memory_units` has no run_id column, and `run_id` because the `co_activation` edges point from the turn's output MU at *surviving* seed MUs and would otherwise dangle. Edges are deleted before MUs (they're declared `REFERENCES memory_units(mu_id)`). Unlike `wipe_graph` it does **not** archive first: blast radius is one failed turn's own artifacts, the caller logs the ids, and archiving every transient Ollama failure would bury the real archives.
  - **Rollback never raises.** It runs inside an exception handler, so a failure there is logged at ERROR with the orphaned ids (`ROLLBACK FAILED ... will be retrievable: [...]`) and swallowed — masking a diagnosable Ollama fault with a confusing SQLite one would be strictly worse. The README troubleshooting entry now tells you to grep for that string.
  - **Verified live, not just in tests.** Backend restarted on the new code, graph at **42 MUs**; Ollama killed; `POST /chat` → 502; graph back at **42 MUs**, with the backend log showing input MU created → prompt MU created → `Ollama connection error` → `rolled back turn artifacts run_id=... deleted 2 MU(s) and 1 edge(s)`. Then Ollama restarted and a successful turn took the graph 42 → 45, confirming no over-eager rollback. **1030 tests passing** (18 new: 8 store-level in `tests/test_memory/test_delete_turn_artifacts.py`, 10 orchestrator-level in `tests/test_core/test_turn_rollback.py`).
  - **Docs corrected, not just added:** PR #63's `client.py` docstring and README troubleshooting entry both documented the orphan cost as a live tradeoff. Both now describe the rollback instead, and `client.py` carries the forward-looking warning — *do not raise the retry budget for a failure class the orchestrator does not roll back.*
  - **Next step unchanged: re-run `plans/exp0_calibration_2026-08-05.json --dry-run --interactive`.** Restart Ollama first. The graph currently holds verification-turn MUs from this session; the harness wipes and reseeds per condition, so they clear themselves at run start.

- 2026-08-06: `/debug` — the M37 calibration pass aborted on turn 0; `/chat` retry + `OLLAMA_CONTEXT_LENGTH` pin
  - **Symptom:** `plans/exp0_calibration_2026-08-05.json --dry-run --interactive` aborted at turn `('learned', 0, 0)` with a 502 from `/chat` wrapping a 500 from `http://host.docker.internal:11434/v1/chat/completions`. The `host.docker.internal` in the error made it look like a Docker networking fault. **It was not** — the request reached Ollama fine and came back 20.5 s later.
  - **Root cause (from `%LOCALAPPDATA%\Ollama\server.log`, local-time stamps vs. the backend's UTC — `07:25:34` local *is* the `11:25:34` in the harness output):** `llama-server` hit `CUDA error: out of memory` inside `ggml_backend_cuda_buffer_get_tensor` while **evicting prompt-cache entries** (`prompt_save: saving prompt with length 1041, total state size = 36.284 MiB` → two `making room ... removing oldest entry` lines → OOM), then died with `exit.hex=0xc0000409` (Windows hard crash), which is why the backend saw a bare 500 with no usable body.
  - **Why VRAM ran out — and a correction worth recording:** *not* because the model is too big for the GPU. `gemma4:e4b` loads `offloaded 43/43 layers to GPU` at ~4.3 GB of the 8151 MiB card; the 9.6 GB figure is the on-disk blob. The real cause is **unbounded prompt-cache growth on a runner that never cycles**: `OLLAMA_KEEP_ALIVE=-1` pins the model indefinitely, that `llama-server` process had been up since **2026-08-04** (`task 424424`, `graphs reused = 417206`), and the prompt cache limit is **8192 MiB — larger than the entire GPU**. Eviction eventually had no VRAM left to stage a device→host copy in. **Mitigation is operational, not code: restart Ollama before a long run.**
  - **The fault is self-healing**, which is what makes the harness behaviour the real bug: Ollama logs `runtime OOM detected; expiring loaded models to clear memory before next request` and the next call reloads cleanly (verified — the immediately following `/chat` returned 200 in 16.5 s). One retry would have sailed through it. Instead `client.chat()` had **no retry at all** and `error_policy["connectivity"]` defaults to `hard_stop`, so a recoverable fault cost the whole 1050-turn run *after* the wipe and reseed were already spent.
  - **Built — `/chat` retry (`scripts/harness/client.py`):** two retries, three attempts, backoff `(5.0, 15.0)`. The backoff is deliberately slow relative to the fault — recovery is a full cold model load (~15 s measured), so a 1–2 s retry just burns an attempt against a GPU still reloading. **Which failures retry is a correctness question, not tuning, because `/chat` is not idempotent:** `ConnectError`/`NetworkError` retry (nothing reached the backend); 5xx retries (this fault); **`TimeoutException` deliberately does not** (the turn may still be in flight server-side, so a retry races a turn that can still succeed and double-writes on top of it). 4xx still raises `BackendTurnError` unretried. Budget is configurable (`chat_max_retries=0` restores the old behaviour) and `sleep_fn` is injectable so tests assert the schedule without sleeping it. 9 new tests in `tests/test_harness/test_client_chat_retry.py`; 446 harness/scripts tests passing.
  - **Known cost of the 5xx retry, accepted deliberately:** `backend/core/orchestrator.py` persists the input MU and the prompt MU *before* calling Ollama and its `httpx.HTTPStatusError` path (`orchestrator.py:692`) re-raises **without rolling either back**, so every failed attempt orphans a `user_input` MU. `source_type="prompt"` is already excluded via `retrieval_exclude_source_types`, but `user_input` is **not**, so each orphan is a retrievable near-duplicate of the turn's message. **This was observed live, not theorized:** a verification `/chat` after the abort retrieved `022fdd36-f2a8-4899-a0da-370ad168f0b4` — the orphan from the crashed turn. Accepted because `hard_stop` leaves the identical orphan behind *and* loses the run. **Open to-do: the clean fix belongs in the orchestrator** (roll back or mark MUs when the LLM call fails); does not block exp0b, but it is graph contamination that scales with retries.
  - **Fixed alongside — a silent experiment-validity hole.** `config.yaml`'s `model.context_window_tokens: 8192` is **only a §5.8 reporting threshold; it does not configure Ollama**, and nothing validates the two at startup. Ollama's default is VRAM-derived and this box had been serving **`n_ctx = 4096`** all along (1346 occurrences in the log). In that direction the failure is quiet: llama.cpp truncates any prompt over the real 4096 ceiling while §5.8 reports the turn as comfortable. Pinned with `setx OLLAMA_CONTEXT_LENGTH 8192` and verified after reload (`llama_context: n_ctx = 8192`, `n_ctx_slot = 8192`). **Cost measured, not assumed: +68 MiB VRAM** (4302 → 4370 MiB used, 3530 MiB still free); KV cache 104 → 168 MiB, cheap because gemma4's sliding-window layers keep a fixed-size buffer and only the full-attention half scales. **No past result is invalidated** — observed turns run ~470–530 tokens in, nowhere near either ceiling. Documented in `config.yaml`, README prerequisites, and a new README troubleshooting entry for the 502/0xc0000409 signature.
  - **Next step: re-run `plans/exp0_calibration_2026-08-05.json --dry-run --interactive`** — the M37 pass still has not produced its numbers. Restart Ollama first. Note the aborted run already wiped and reseeded the graph, and left the orphan MU above in it.

- 2026-08-05: Milestone 37 — judge literal-match discipline + per-criterion calibration
  - **Trigger:** the M36 override audit's own recommendation. M31 fixed the *derivation* layer; the audit found the *grading* layer above it systematically permissive — all 3 overrides were the judge marking a criterion `true` on a looser standard than the criterion states, all lenient, none indicting the mixed→none rule. Judge-alone up-rate 82.4%.
  - **Design finding that shaped everything (worth remembering): the bank has two criterion families, and one grading rule cannot serve both.** `answer: L-2` / `answer: UTC` demand the literal token — that is where the audit's "V-1 is a recognized synonym" defect lives. `mentions consistent/fixed field ordering` is an idea and is *supposed* to accept paraphrase; it is ~3/4 of the bank. A blanket "demand exact matches" prompt would have swapped a lenient judge for an over-strict one and looked like progress on the up-rate while being just as wrong. The judge prompt now tags each criterion `[EXACT]` or `[CONCEPT]` and gives each its own standard, derived host-side from the bank's existing `answer:` convention (slash alternatives and parentheticals are accepted variants; an `answer:` whose answer is a whole clause classifies `[CONCEPT]` — no token to match). **No criteria text changed; `content_hash` survives**, same rule as the 07-27 normalization fix.
  - **Built — prompt (`scripts/harness/judge.py`):** quote-bearing checklist schema with the quote requested *before* the boolean, so a small model has to produce its evidence before the judgment rather than justify one it already made — and the quote is what makes the claim checkable afterwards. Flat, imperative, few rules (a 4B instruct model follows a handful of unconditional rules far better than a nested rubric), plus an explicit "if unsure, answer false" default, since a small model with no stated default drifts agreeable — precisely the observed error direction. Response is placed before the criteria so the rules land last.
  - **Built — host-side evidence checks (`_check_evidence`):** exactly one check may change a judgment: `met: true` with a *supplied-but-empty* quote is **forced false**, because the protocol tells the judge an empty quote requires `met: false`, so the pair is the checklist contradicting itself (the turn-5 defect) — resolved the only way the protocol allows, not second-guessed. The two content checks — a quote absent from the response, and an `[EXACT]` criterion whose required answer is nowhere in the response — **flag only**. They are heuristics over normalized text, and silently reclassifying a verdict on a heuristic would corrupt the record the run exists to produce. Both are counted in the calibration summary, so promoting one into enforcement is a decision on real numbers rather than a guess.
  - **Built — parse tolerance:** `_extract_checklist()` accepts the M37 object form under `checklist` or `criteria`, and M31's bare `criteria_met` boolean list. Rejecting a correctly-graded checklist costs a re-ask and possibly a whole turn's verdict; accepting one under a drifted key costs nothing and changes no judgment. `evidence_enforced` records which regime each turn was graded under so the two are never pooled.
  - **Built — calibration (`runner.py` + new `scripts/harness/calibration.py`):** `--interactive` menu is now `[r/a/o/skip]`. `r` walks the checklist one criterion at a time (Enter keeps the judge's answer) and the verdict is re-derived from the corrected list by `derive_verdict()` — extracted and made public precisely so both sides of the comparison apply the identical M31 rule; if they diverged, the pass would measure the rule instead of the judge. `o` (direct verdict override) stays as the escape hatch for disagreeing with the derivation rule itself. A regrade that changes nothing records as the judge's own verdict — confirming a checklist is an affirmation, and counting it as human-sourced would understate the judge-alone up-rate M36 exists to report. The calibration printout now shows each criterion's quote and any evidence flags, which is what makes a lenient grade visible at a glance: `answer: L-2` marked met on the quote "the applicable rule is V-1" needs no deliberation. New `calibration.py` aggregates per-criterion agreement, the lenient/strict split, recorded-vs-judge-alone verdict mix, and evidence-flag counts into `calibration_summary.md` beside the checkpoint (written on abort too) — M36's lesson applied one layer down: a number nobody reports is a number nobody has. Pure aggregation over `TurnCheckpoint` rows, so it also recomputes after the fact from a finished pass's `checkpoint.json`.
  - **Why per-criterion is the point:** 3 overrides out of 20 turns is a rate with a 5–36% CI — uninformative, as the M36 audit itself noted. The *composition* was decisive. Regrading yields one observation per criterion (~2.4x the turns on the current bank) and each one names its criterion, so the next pass measures the composition directly instead of reconstructing it by hand afterwards.
  - **Two real defects found while writing the tests:** (1) `_normalize_for_evidence()` collapsed whitespace without a trailing strip, so a quote ending in an ellipsis retained a trailing space and failed the substring check — a false `unverified_quote` on a genuine quote, which would have made the flag noise instead of signal. (2) The enforcement bailout originally keyed on "no non-empty quote anywhere", conflating a *dropped* `quote` key with a *supplied-empty* one — which exempted **every single-criterion prompt** from the demotion rule, and single-criterion prompts are the entire `cluster_d` `answer: X` family, exactly where the audit's defect lives. The bailout now keys on the key's presence, so a dropped output field still cannot manufacture a `down` while a self-contradicting entry is still caught.
  - **Also:** the `_judge_and_post_verdict()` 5-tuple became a `VerdictOutcome` dataclass (M37 adds four more fields; nine values unpacked positionally is the shape that silently transposes two). `TurnCheckpoint` gains `judge_model_criteria_met` (the judge's own checklist, the per-criterion analogue of M36's `judge_model_verdict`), `criteria_evidence`, `evidence_flags`, `evidence_enforced`. `RunOutcome` gains `calibration_summary_path`. Per-criterion data stays harness-side — no backend schema change, same as M31.
  - **Note on `judge_model_verdict`/`judge_model_criteria_met` semantics:** both are recorded *after* the host-side evidence checks, because those run unattended too — they describe what an unattended run would store, which is what "judge alone" means. The model's raw pre-demotion claim stays recoverable from the `demoted_no_quote` entries in `evidence_flags`, and the summary states this explicitly.
  - **Tests:** 65 new/changed — `test_judge.py` (evidence layer: forced-false on self-contradiction, flag-not-demote on the synonym and unverifiable-quote cases, cosmetic-reformatting tolerance, the dropped-vs-supplied-empty distinction on a single-criterion prompt, key/shape drift acceptance, malformed payloads still direction-neutral, family classification, shared derivation truth table), `test_runner.py` (regrade derives the verdict, Enter keeps answers, no-change regrade stays model-sourced, typo re-prompts, quote+flags rendered, override still available, checkpoint records the evidence, summary written only when interactive), new `test_calibration.py` (11 tests over fabricated checkpoints). **1003 tests passing, no regressions.**
  - **Live end-to-end simulation** (fake backend/judge, real code path) reproduced both audit failure modes: the invented-synonym turn rendered with both flags and regraded `up`→`down`; the empty-quote turn was forced false before the operator saw it, and confirming it recorded as the judge's own verdict. Summary reported 50% per-criterion agreement, 1 lenient / 0 strict.
  - **AWAITING PAUL — the live 20-turn `--interactive` recalibration pass**, same constraint as M31's (real local Ollama + live human judgment on real judge output). Run: `python scripts/run_experiment_harness.py --plan plans/exp0_calibration_2026-08-05.json --dry-run --interactive`. The plan is a clone of the 08-02 one with the same `random_seed` and 20-turn slice, so the pass is directly comparable to M31's. Use `r`, not `a`/`o`, on every turn — that is what produces the measurement. Then: compare per-criterion agreement and the lenient/strict split against M31's 82.4% judge-alone / 3-override read, decide whether either evidence flag graduates to enforcement, and only then commit to exp0b.
  - Branch: `claude-judge-literal-match-per-criterion-calibration`.

- 2026-08-04: Milestone 36 — judge-source reporting (recorded vs. judge-alone verdicts) + override rationale integrity
  - **Trigger:** the M31 recalibration pass ran (`exp0-calibration-2026-08-02-m31_dryrun_learned`, 20 bulk turns, 0 failures). Paul overrode 3 verdicts but **the report showed no trace of them** — §5.2 aggregated `verdict` alone, so the headline 70.6% up-rate silently presented human-corrected verdicts as the judge's own calibration. Audit of the checkpoint + `feedback_events` found the 3 overrides (turn 5 `none`→`down`, turn 12 `up`→`down`, turn 19 `up`→`none`) and a second defect: `runner.py` posted the *model's* rationale on human-overridden rows, producing rows reading `judge_source="human"`, `verdict="down"`, rationale *"the response explicitly states 'V-1', which is a recognized synonym…"* — verdict and stored reasoning contradicting each other.
  - **Built:**
    - **`backend/memory/store.py`:** two additive nullable columns on `feedback_events` — `judge_model_verdict`, `judge_model_rationale` (standard `_migrate_db()` pattern, pre-existing rows keep NULLs). `get_verdict_counts_by_series()` now returns the judge-alone tallies (`judge_up`/`judge_down`/`judge_none`) and `human_sourced`/`human_unrecoverable` alongside the recorded `up`/`down`/`none`. Attribution rule: use `judge_model_verdict` when present; fall back to `verdict` for model-sourced rows (for a model row the two are the same thing — recovery, not guesswork); for **human** rows with no stored model verdict, count `human_unrecoverable` and exclude from judge-alone rather than attributing a human verdict to the judge.
    - **`backend/api/chat.py` + `backend/analytics/feedback_events.py`:** `VerdictRequest`/`FeedbackEvent` carry the two new optional fields; endpoint log line includes `judge_model_verdict`.
    - **`scripts/harness/runner.py` + `client.py`:** an override now writes a `rationale` describing the *recorded* verdict (`"operator override of judge verdict 'up' -> 'down' (--interactive calibration)"`) and preserves the judge's own account in `judge_model_rationale` — the evidence a calibration pass actually reads. `judge_model_verdict` is now POSTed, not just checkpointed: the reports read the DB, so without it there was no way to state a judge-alone rate from a calibration run's own data.
    - **`experiment_report.py` §5.2 + `markdown_renderer.py`:** recorded up-rate, human-sourced count, and judge-alone up-rate/counts, with a caveat block that renders only when overrides exist, and a ⚠️ coverage warning when pre-M36 human rows make judge-alone partial.
    - **`docs/harness_usage.md`:** new "Reading a calibration run's report" subsection under `--interactive`.
  - **Live-verified read-only against a copy of the real `memory/graph.db`** (the experiment DB was not mutated): the M31 calibration now reports `Human-sourced verdicts: 3`, judge-alone 12 up / 3 down = **80.0%** against the recorded 70.6%, plus the ⚠️ warning that 3 pre-M36 override rows have no recoverable pre-override verdict. Backfilling those 3 from the checkpoint (judge said `up`, `none`, `up`) would give the true judge-alone figure of **82.4%** — **not done, awaiting Paul's call**, since it mutates real experiment data.
  - Tests: +12 (store attribution incl. both legacy-row cases; `_aggregate_verdict_mix` recorded-vs-judge-alone, automated-run coincidence, unrecoverable coverage; two end-to-end report+render tests; two harness tests pinning the override-rationale fix and the accepted-verdict case; two API-boundary tests). **950 total** (938 baseline), no regressions.
  - **Science read for Paul (from the override audit, not the code):** 3/20 = 15% override rate, 95% CI ≈ 5–36% — as a *rate*, uninformative at n=20. The composition is the finding: all three overrides are the same defect — the judge marking a criterion `true` on a looser standard than the criterion states (turn 5's checklist contradicted its own rationale; turn 12 invented a synonym; turn 19 accepted a paraphrase) — and **all three are lenient in direction, none indict M31's mixed→none derivation rule**. M31 fixed the derivation layer; the per-criterion grading above it is now systematically permissive. Recommend tightening the judge prompt toward literal-match discipline and recalibrating with **per-criterion** grading before exp0b.
  - Branch: `claude-judge-source-reporting`

- 2026-08-02: /build — Milestone 31 code-complete (Judge Verdict Scheme: Partial Answers Abstain)
  - **Built, per BUILD-PLAN.md's "Judge Verdict Scheme — Milestone 31" spec, using the recommended per-criterion checklist protocol (option (b)):**
    - **`scripts/harness/judge.py`:** `_build_judge_prompt()` rewritten to ask for a per-criterion checklist (`criteria_met`, a list of exactly N true/false booleans in the same order as the prompt's criteria) plus `rationale`, instead of asking the judge to assert `verdict`/`criterion_triggered` itself. `_parse_verdict()` now **derives** the verdict host-side: all criteria met → `up`; none met → `down`, citing the *first* listed criterion (uninformative-by-construction once everything has failed, so no guessing is needed — the rationale carries the real detail); anything mixed → `none` (abstain, no weight update — matches the backend's existing M8 "absence of signal is an explicit observation" contract, no backend change needed). A malformed `criteria_met` (missing, wrong length, or containing a non-bool element — checked via `isinstance(v, bool)`, so JSON `1`/`0` are correctly rejected rather than truthy-coerced) raises `ValueError`, caught by `judge_turn()`'s existing retry-then-`JudgeUnparseableError` loop. `JudgeVerdict` gains `criteria_met`, drops `criterion_unresolved`/`raw_criterion_citation` — those fields, and the "unresolved citation" retry-and-keep-first logic they supported, described a failure mode (a free-text citation that couldn't be matched to a criterion) that no longer exists once criteria are identified by list position rather than by name. Because every parse failure is now equally direction-neutral (there's no verdict-but-bad-citation case left to special-case), `judge_turn()` itself got simpler: one `_parse_verdict()` call per attempt, no separate unresolved-verdict bookkeeping.
    - **`_normalize_criterion()`/`_resolve_criterion()` kept, per the milestone spec** ("stays for citation resolution wherever the judge names criteria in prose") even though the checklist protocol's primary parse path no longer calls them — now covered by direct unit tests (`test_judge.py`) rather than exercised indirectly through `judge_turn()`, since `judge_turn()` no longer has a citation-resolution code path to run them through.
    - **`scripts/harness/checkpoint.py`:** `TurnCheckpoint.criteria_met` (new, `Optional[list[bool]]`) — the milestone spec's "recorded for free" per-criterion data, populated whenever the judge produced a parseable verdict (interactive or not), null when no judging happened, the judge was unparseable, or an operator hand-graded an unparseable turn (no per-criterion judgment exists for those). Feeds future M33-style analysis without any backend/schema change.
    - **`scripts/harness/runner.py`:** `_judge_and_post_verdict()`/`_post_verdict()` thread `criteria_met` through as a 5th tuple element (was 4); the now-impossible unresolved-citation branches are removed — accepting a judge `down` no longer needs to ask the operator for a criterion, since a derived `down` always already carries one. `_print_calibration_context()` gained an optional `criteria_met` param and now renders each criterion with its own `[x]`/`[ ]` mark in `--interactive`, so the operator calibrates against the exact checklist the judge graded, not just the derived up/down/none — falls back to a plain list when there's no checklist (the judge-unparseable manual-recovery path). `none` was already a valid operator override target (`VALID_VERDICTS` already included it, and `apply_weight_update`'s `verdict in ("up", "down")` check already excluded it) — the milestone's "offer none as an operator choice" requirement turned out to already be satisfied by existing code; added explicit regression coverage rather than new logic.
    - **`plans/exp0_calibration_2026-08-02.json`:** clone of `plans/exp0_calibration_2026-07-27.json` with a new `plan_id` (`exp0-calibration-2026-08-02-m31`) and updated `plan_name`; `random_seed` and `dry_run_n=20` left identical so it resolves to the same 20-turn slice, per the spec. Validated against `RunPlan` directly (loads and parses cleanly).
    - **`docs/harness_usage.md`:** the "Judge verdicts vs. criterion citations" section rewritten as "Judge protocol: the per-criterion checklist (Milestone 31)", describing the derivation rule and why every parse failure is now direction-neutral; the `--interactive` calibration description updated to mention the `[x]`/`[ ]` checklist rendering and that `none` is a valid override target; the `criterion_unresolved` troubleshooting row replaced with two checklist-shaped failure modes (`none`-heavy runs are expected, not necessarily a bug; malformed `criteria_met` shapes indicate a judge model not following the schema).
  - **Out of scope, confirmed unaffected:** no backend change (verified 2026-08-02 per the milestone spec — `POST /feedback/verdict` already accepted `verdict="none"` and already skipped `process_verdict_for_learning()` for it); no change to the bank's criteria text (`content_hash` untouched, same reasoning as the 2026-07-27 normalization fix — the exp0 210-prompt bank stays valid).
  - Tests: `tests/test_harness/test_judge.py` rewritten for the checklist protocol (43 tests) — derivation truth table (all-met/none-met/each mixed combination, single-criterion prompts can only derive up/down never none), malformed-`criteria_met` cases (missing key, wrong length, non-bool elements including the `1`/`0`-vs-`True`/`False` distinction, non-list) all → `JudgeUnparseableError`, citation-on-derived-downs is always positional (`criteria[0]`, regardless of criteria order), `_build_judge_prompt()` regression (asks for `criteria_met`, never leaks the old `criterion_triggered` field), and `_normalize_criterion`/`_resolve_criterion` moved to direct unit tests. `tests/test_harness/test_runner.py`: `FakeOllamaJudgeClient`'s payload shape updated throughout (`checklists=[{"criteria_met": [...], "rationale": ...}]` replacing `verdicts=[{"verdict": ..., "criterion_triggered": ...}]`); the four tests that specifically exercised the now-impossible unresolved-citation code paths (citation normalization end-to-end, unresolved-down-keeps-direction, interactive-accept-asks-for-criterion, interactive-warns-on-unmatched-citation) removed as testing a path that no longer exists; new coverage added for mixed-checklist → `none` with no weight update, derived-down citing the first criterion end-to-end, `criteria_met` recorded on the checkpoint, interactive override to `none`, and the `[x]`/`[ ]` checklist rendering in calibration context. 938 tests passing (922 baseline), no regressions.
  - **AWAITING PAUL — the live 20-turn `--interactive` recalibration pass is a manual follow-up, not run this session.** It requires a real local Ollama instance and live human judgment calls on real judge output (accept/override decisions across 20 turns) — genuinely outside what an autonomous `/build` session can responsibly do unattended, unlike the report-layer M32–M35 milestones whose live verification was read-only against already-stored data. Run: `python scripts/run_experiment_harness.py --plan plans/exp0_calibration_2026-08-02.json --dry-run --interactive`. Record the resulting up/down/none mix in this file; exp0's 78-down/70-up/2-none-style ledger under the old rule predicts a large `none` share under the new one. Only after that pass should an exp0b re-baseline run be considered — this milestone was explicitly built last (per BUILD-PLAN.md's sequencing rule) because it changes the learning signal itself, so exp0 stops being a comparable verdict-driven baseline once it lands.
  - Branch: `milestone-31-judge-abstain-scheme`. This is the last of Milestones 31–35 (all of M32–35 already merged to main). Next: Paul reviews and merges this PR, runs the live recalibration pass, then decides on exp0b timing.

- 2026-08-02: /build — Milestone 35 complete (Instrumentation Fixes: Traversal Counter + Runaway Detector)
  - **Built, per BUILD-PLAN.md's "Instrumentation Fixes — Milestone 35" spec:** report-layer only, no schema or backend behavior change — both fixes derive correct numbers from data already being stored, per the same pattern as M32–34.
    - **§5.5 `traversals: 0` fix (`series_report.py`'s `_section_55_edge_weight_deltas()`):** traced the bug per the milestone's instruction ("analytics rows vs. weight_updates payloads"). Root cause: the function read `traversal_count` from `edges.traversal_count`, a DB column that defaults to 0 (`store.py:60`) and that nothing in the codebase ever increments — `update_edge_weight()` only touches `weight`, `update_edge_counters()` only touches `alpha`/`beta`/`evidence_updated_at_turn` (both grepped and confirmed with no other write site). Every edge reported `traversals: 0` regardless of real traversal count, including edges with `updates >= 1` — impossible, since `process_verdict_for_learning()` credits only edges on the traversal path. Fixed by counting each edge_id's occurrences across runs' `activated_edge_ids` (already stored per-turn on the `analytics` row) instead — `retrieval.py:328` confirms `activated_edge_ids=traversed_edge_ids`, so this is the correct source and required no backend change, just reading data that was already there. The old dead `traversal_count_map` built from `store.get_edges_by_ids()` is removed entirely.
    - **Runaway-detector redefinition (`experiment_report.py`'s `_compute_saturation_analysis()`):** the old heuristic (any 3 consecutive deltas that are each algebraically larger than the previous) fired on exp0's ordinary Beta(1,1) posterior-mean down-streaks (e.g. -0.1667 → -0.0833 → -0.05) because the values are negative and shrinking toward zero *is* an algebraic increase, even though the magnitude is diminishing — the update rule working exactly as designed. Split into two statistics: `same_direction_streaks` (informational only — any 3-consecutive same-sign-delta streak on one edge, renamed so it stops crying wolf) and `runaway_flags` (the real anomaly, a strict subset — fires only when `|delta|` is non-decreasing across all three steps, i.e. genuinely accelerating, OR the streak carries the edge's weight across `saturation_threshold` during the streak, which is dangerous even if each individual step is shrinking). Added a small epsilon (1e-9) to the non-decreasing-magnitude comparison since real float deltas meant to be equal can differ by a few ULPs. `markdown_renderer.py` renders both counts as separate lines (never conflated) and shows each runaway flag's reason(s) (`accelerating |delta|` and/or `crosses saturation threshold`).
  - **Live verification against real exp0 data** (`memory/graph.db`, read-only, `MEMORY_PATH` pointed at the host-mounted `./memory` before importing `memory.store` so `_DB_PATH` resolves correctly — same pattern as M33/M34's host-side verification):
    - **Runaway detector:** re-running `_section_61_learning_measures("exp0-2026-07-26_learned", store, config)` directly now returns `runaway_flag_count=0` (down from the stored report's 13) and `same_direction_streak_count=17`. Confirms the exp0 analysis finding exactly: all 13 originally-flagged edges were ordinary convergence, not genuine runaway reinforcement — the true count is zero.
    - **Traversal counter:** re-running `_section_55_edge_weight_deltas()` against series `exp0-2026-07-26_learned_s0`'s real analytics rows (42 runs) now shows nonzero, correct `traversal_count_total` for every one of the 75 activated edges — e.g. edge `ec000002r` (`update_count_in_series=4`) now reads `traversal_count_total=7` versus the stored report's `traversal_count_total=0`. This also surfaces 15 `zero_delta_high_traversal` flags in that one series alone — a signal that could never fire before since the traversal count it depends on (`> 5`) was always 0.
  - Tests: 5 new (917 baseline → 922, all passing, no regressions) — `tests/test_analytics/test_learning_measures.py`: the exact exp0 decaying-streak sequence (must NOT flag as runaway, must still count as a same-direction streak), a constructed non-decreasing-magnitude streak and a threshold-crossing-despite-shrinking-magnitude streak (both must flag runaway, with the correct `non_decreasing_magnitude`/`crosses_saturation_threshold` reason recorded), and an `empty_runs` regression covering the new `same_direction_streak_count` key. `tests/test_analytics/test_series_report.py`: a regression proving traversal count is derived from `activated_edge_ids` occurrences and not the (always-0) `edges.traversal_count` DB column, plus the existing zero-delta-high-traversal test rebuilt to activate an edge across 6 separate runs instead of stuffing the dead DB column directly. `tests/test_analytics/test_markdown_renderer.py`: the informational-streak-count line renders separately from the runaway-flag-count line, and each runaway entry's reason text renders.
  - Branch: `milestone-35-instrumentation-fixes`. This closes out Milestones 32–35 (M31, the judge-scheme change, is last per the sequencing rule in BUILD-PLAN.md's "Experiment-0 Follow-Ups" section, since it changes the learning signal itself). Next: Paul reviews and merges the PR, then M31 (judge abstain scheme + recalibration), then decide on an exp0b re-baseline.

- 2026-08-02: /build — Milestone 34 complete (§6.2 Control Shadow: Sample-Mode Comparison + Rendering)
  - **Built, per BUILD-PLAN.md's "§6.2 Control Shadow: Sample-Mode Comparison + Rendering — Milestone 34" spec:**
    - **`_compute_comparative_deltas()` (`control_shadow.py`)** now returns the raw learned/control values used to compute each of the four existing deltas (`probe_pass_rate_learned`/`_control`, `bridge_mu_rank_learned`/`_control`, `edge_weight_delta_magnitude_learned`/`_control`, `mean_gold_mu_rank_learned`/`_control`) alongside the delta keys already there — additive, nothing removed.
    - **New `_compute_sample_pass_rate_comparison()`:** per-probe, per-session learned-minus-control deltas matched on `session_index` from each condition's own `sample_pass_rates` (already computed by `_section_61_learning_measures()` for both the learned and control experiments via the existing M30/M33 machinery — no new query). Aggregated `by_phenomenon` into `mean_delta` (across every session of every probe in that phenomenon) and `final_session_mean_delta` (mean of each probe's last-session delta — the trajectory endpoint). A probe present in only one condition is excluded (can't be compared); a session with `pass_rate=None` (no non-flagged repeats) yields `delta=None` and is excluded from aggregation rather than crashing. Wired into `generate_control_shadow()` as a new `sample_pass_rate_comparison` key alongside `comparative_deltas`, per control condition.
    - **`markdown_renderer.py`'s `_render_s62()` — the two rendering bugs fixed:** the per-control "Comparative Deltas" table's Learned column previously hardcoded `—` on every row; it now reads the new `*_learned` keys. The cross-condition "Condition Comparison Summary" table previously hardcoded `(baseline)` for Learned; it now reads the same value from the first control's `comparative_deltas` (learned values are identical across every control_shadow entry since there's only one learned experiment). New `_S62_MEASURES` table-drives both renderers from one (delta_key, learned_key, control_key, label) list so the two tables can't drift out of sync with each other.
    - **`edge_weight_delta_magnitude`'s control column** shows the literal text `0 by design` instead of a bare `0.0000` when the control value is at/near zero (`abs(control_val) < 1e-9`) — per M24's finding that `process_verdict_for_learning()` only ever writes when `condition_label == "learned"`, so static/random controls structurally never accumulate `weight_updates`; presenting that as an empirically-near-zero "delta" implied a learning effect where the measure is true by construction. Still renders the real number if that invariant were ever violated (verified by a dedicated test with a fabricated nonzero control value) — the label communicates "expected", not "trust blindly".
    - New sample-mode comparison section rendered per control, clearly labeled `weight_mode=sample` and distinct from the existing "mean-mode" comparison table (relabeled `Comparative Deltas — mean-mode` so the two can't be conflated): a by-phenomenon summary table plus a collapsible `<details>` per-probe/per-session table (the full 28-probe x 5-session grid would otherwise dominate the report). Renders an explanatory "unavailable" message rather than an empty table when there's no sample-mode data to compare (pre-M30 experiments, or no probes match between conditions).
  - Tests: 17 new (900 baseline → 917, all passing, no regressions) — `tests/test_analytics/test_control_shadow.py`: raw learned/control value presence, per-session delta arithmetic, by-phenomenon aggregation (mean + final-session), missing-sample-data graceful degradation (empty dict, not a crash), probe-only-in-one-condition exclusion, `None` pass-rate → `None` delta handling. `tests/test_analytics/test_markdown_renderer.py`: Learned-column-not-dash regression test, `0 by design` labeling (both the by-design-zero and the would-be-a-bug-if-nonzero paths), condition-comparison-summary `(baseline)`-not-present regression test, sample-mode section rendering (by-phenomenon + per-probe), sample-mode-unavailable message, and the pre-existing `control_shadow=None` "Not computed" case.
  - **Live verification — the milestone's own open science question, run against real exp0 data (`generate_experiment_report()` called directly against `memory/graph.db`, read-only, both control shadows attached — this exercises the full report-generation path, not just `_section_61_learning_measures()` in isolation, now that M33's stored-report-path fix is merged to main):**
    - **vs_d4 (mistargeted, not a trained spoke) — the climb does NOT replicate in either control:** learned's session-by-session sample pass rate is 0.04 → 0.24 → 0.56 → 0.60 → 0.48 (the climb flagged in the exp0 analysis). Under `static_weight` — same graph growth/environment drift as `learned`, but zero weight updates — vs_d4 stays flat at exactly 0.0000 every session (delta = the raw learned value itself, since control is always 0). Under `random_weight` — weights re-randomized at every measurement point — vs_d4 is uncorrelated noise (1.00 → 0.00 → 0.00 → 1.00 → 1.00), tracking that session's independent re-randomization rather than any trend. **Reading: this is a real effect of the `learned` condition's own weight-update mechanism, not shared environment drift** — confirms Paul's framing (BUILD-PLAN.md, exp0 follow-ups) of vs_d4 as the run's first real (if mistargeted) learning signal, rather than leaving it as an open question.
    - **Second, unplanned finding — vs_c1 (a trained spoke):** near-ceiling under `learned` (0.92/1.00/0.92/0.96/0.96) and *also* near-ceiling under `static_weight` (flat 1.0000 every session) — but flat at 0.0000 under `random_weight`. Reading: vs_c1's high pass rate is explained by the graph's default static edge-weight structure already favoring correct retrieval for this probe, not by the RL update mechanism specifically — `random_weight`'s reshuffling is what actually breaks it, not the absence of learning. Worth keeping in mind when reading any future "learned condition passes probe X" result: check whether `static_weight` already passes it too before crediting the mechanism.
    - `edge_weight_delta_magnitude` confirmed structurally 0 for both controls (`0.110877` learned vs. `0.0` control, both conditions) — matches the "true by construction" framing, not a coincidence.
    - `probe_pass_rate_delta` (mean-mode) reproduced the motivating bug exactly: `0.0000` for both `static_weight` and `random_weight` — confirming mean-mode alone is blind here, and the new sample-mode comparison (`spoke_retrieval` phenomenon: `mean_delta=-0.0231` vs. `static_weight`, `-0.0231` vs. `random_weight`; `final_session_mean_delta=-0.0371` vs. `static_weight`, `-0.0014` vs. `random_weight`) is where the actual signal (and its absence, for most of the other 26 probes) is visible.
  - Branch: `milestone-34-control-shadow-sample-mode`. Next: Paul reviews and merges the PR, then Milestone 35, then M31 last.

- 2026-08-02: /build — Milestone 33 complete (Conversation-Edge Classification)
  - **Built, per BUILD-PLAN.md's "Conversation-Edge Classification — Milestone 33" spec:** derived entirely at report time from already-stored data, no schema change, no backend/retrieval behavior change.
    - **Seed adjacency (existing signal, unchanged):** `mu_info.cluster_id` from `store.get_mu_info_batch()` — non-null only for seed MUs, since the orchestrator never passes `cluster_id` when creating conversation MUs (user_input/prompt/output/tool_call/tool_result).
    - **Prompt lineage (new signal):** `store.get_prompt_ids_by_run(experiment_id)` — one query joining `feedback_events.prompt_id` to `analytics.run_id`, scoped by `experiment_id` — maps each bulk turn's `run_id` to its prompt_id. `experiment_report.py`'s `_cluster_from_prompt_id()` reads the target cluster straight off the ID string via regex (`f"{cluster_id}_{i:04d}"` is `prompt_gen.py`'s actual, current naming convention — e.g. `cluster_a_0007` → `cluster_a`), so no prompt-bank JSON file needs to still exist on disk at report time. `_build_mu_lineage_clusters()` then credits every MU a bulk turn created — `input_mu_id`, `prompt_mu_id`, `output_mu_id`, and every id in `tool_calls` (confirmed via `orchestrator.py`: `RunRecord.tool_calls` actually stores tool_call/tool_result MU ids, not structured call data) — with that turn's prompt's cluster.
    - **`_classify_edge_update(u)`:** classifies each weight update by edge class (`seed_seed`/`conv_seed`/`conv_conv`, from which endpoints are seed MUs) x cluster relation. `same_cluster`/`cross_cluster` apply when both endpoints resolve via the *same* signal type and agree/disagree; `mixed` is `conv_seed`-only, when the seed-adjacency cluster and the conversation endpoint's prompt-lineage cluster are two independent signals about the same edge and they disagree — kept distinct from `cross_cluster` because it is evidence the conversation content itself crossed a cluster boundary, not an artifact of a single segmentation scheme. `unresolvable` (the new, deliberately small residual) is a conv endpoint with no derivable prompt lineage at all (probe/dry-run turns, or a turn whose MUs aren't in the fixed input/prompt/output/tool_calls set) combined with no seed endpoint to fall back on.
    - **`_compute_edge_delta_ledger()`:** replaced the flat `within_cluster_count`/`cross_cluster_count`/`unknown_cluster_count`/`cross_cluster_deltas` keys with `edge_classification`: `by_class_relation` (3x4 grid of `{count, mean_abs_delta}`), `totals_by_relation`, `cross_domain_watch` (the `conv_seed`/`mixed` + `conv_conv`/`cross_cluster` edges — the operational definition of emergent cross-domain learning the milestone exists to surface), and `by_series` (per-series counts by edge class, in first-appearance order, for the longitudinal view the spec asked for).
    - `markdown_renderer.py`'s `_render_s61()` renders the grid, watch list (capped at 20, same convention as the old cross-cluster-deltas table), and per-series table; when `edge_classification` is absent (a pre-M33 stored report JSON, which only has the old flat keys), the whole block renders nothing rather than crashing.
  - **Verified live against the real exp0 data (done-criterion per the milestone spec):** called `_section_61_learning_measures("exp0-2026-07-26_learned", store, config)` directly against `memory/graph.db` — read-only, no files written, no DB rows inserted. Deliberately did **not** run the full `generate_experiment_report()`/`write_experiment_report()` path: that also recomputes §5 sections via `_load_stored_series_data()`, which failed to read the original stored series-report JSONs because `generated_reports.json_path` holds the container-absolute path (`/app/data/reports/...`) recorded at original generation time, which doesn't resolve on the host filesystem — a pre-existing, unrelated bug, not introduced by M33, and out of scope to fix here (the files exist on disk at the equivalent host path; only the stored path string is wrong for host-side re-reads). Results:
    - **`seed_seed` counts reproduced exactly:** 42 same-cluster, 7 cross-cluster — byte-for-byte the stored report's old `within_cluster_count`/`cross_cluster_count`, confirming M33 is a strict refinement of the existing classification, not a divergent recomputation.
    - **All 176 formerly-"unknown" updates resolved, 0 residual:** `conv_seed` 59 same-cluster + 8 mixed + 0 cross-cluster + 0 unresolvable = 67; `conv_conv` 109 same-cluster + 0 cross-cluster + 0 mixed + 0 unresolvable = 109; 67 + 109 = 176.
    - **The cross-domain-linkage watch list has exactly 8 entries** (all `conv_seed`/`mixed` — `conv_conv`/`cross_cluster` contributed none this run): 8 edges where a bulk turn's own prompt targeted one cluster but the edge it reinforced touched a *different* cluster's seed MU. Read: in exp0's learned condition, essentially all conversation-graph reinforcement stayed within the turn's own intended cluster (168/176 same-cluster) — genuine cross-domain linkage was rare (8/176, ~4.5%) but present, which is exactly the "measure it, don't assume it's zero or dominant" answer this milestone was built to give. Per-series counts (`by_series`) show `conv_seed`/`conv_conv` volume rising across series (s0: 7/14 → s4: 20/23) as accumulated conversation MUs win more retrieval slots — consistent with M32's seed-share-decline finding; the mixed/cross-domain share within that growing volume did not noticeably concentrate in any one series.
    - **Confirmed the graph-wipe-between-conditions concern does not corrupt this measurement:** seed_v3's MU ids are fixed in the fixture file (not random per reseed — e.g. `a0000001-aaaa-4aaa-8aaa-000000000001`), so even though `wipe_graph()` deletes all `memory_units` rows between exp0's three conditions, the *same* seed mu_ids (with the *same* `cluster_id`) get reinserted before each condition — meaning seed adjacency for the "learned" condition's weight updates still resolves correctly today, reading through whichever condition's reseed is currently live. Prompt lineage never touches `memory_units` at all (`analytics` + `feedback_events` only), so it's unaffected by wipes entirely. This is a property worth relying on for M34/M35's own live-data verification, not just a lucky one-off.
  - Tests: 30 new (870 baseline → 900, all passing, no regressions) — `_classify_edge_update()` truth table (all class x relation combinations, including both unresolvable paths), `_cluster_from_prompt_id()` (suffix stripping, None/empty/no-suffix cases), `_build_mu_lineage_clusters()` (input/prompt/output/tool_calls coverage, turns with no prompt_id contribute nothing), `_collect_all_weight_updates()` lineage wiring, `_compute_edge_delta_ledger()`'s grid/watch-list/by-series computations and confirmation the old flat keys are gone, markdown rendering (new grid + pre-M33 backward-compat), and three end-to-end store round-trip tests including an experiment-scoping contamination guard for `get_prompt_ids_by_run()` (mirrors the M32/§6.1 pattern — a different experiment's `prompt_id` must not leak into this one's lineage map even sharing a `series_id`).
  - **Also fixed in this PR (Paul's ask, after seeing it in the live-verification writeup): the container-absolute stored-report-path bug.** `write_series_report()`/`write_experiment_report()` store `json_path`/`md_path` in `generated_reports` as an absolute path built from `MEMORY_PATH` at write time — normally `/app/data/reports/...`, since the backend always runs in the container. Any reader whose own `MEMORY_PATH` differs (a host-side script pointed at the bind-mounted `./memory` directly, as the live-verification script above was) can't resolve that path even though the file exists on disk at the equivalent location. New `resolve_report_path()` (`series_report.py`) reconstructs the file location from the *current* `_REPORTS_DIR` plus only the stored path's basename, discarding the stale directory prefix entirely. Used by `experiment_report.py`'s `_load_stored_series_data()` (the exact function that logged "stored series report unreadable" during live verification above) and by `admin.py`'s two report-fetch-by-id endpoints (`GET /admin/reports/{id}` and `.../markdown`), which had the identical bug pattern. Confirmed fixed live: re-running the full `generate_experiment_report()` (not just `_section_61_learning_measures()`) against real exp0 data from the host now reads all 5 stored series reports cleanly (no "unreadable" warnings) and reproduces the documented per-series intra-series-rate decline (0.577→0.077) instead of silently recomputing from the wiped live graph. One existing test (`test_experiment_report_prefers_stored_series_reports_over_wiped_graph`) stored an arbitrary temp-dir path directly and relied on the old trust-the-literal-path behavior — updated to patch `_REPORTS_DIR` to that temp dir instead, plus a new regression test (`test_experiment_report_reads_stored_series_report_despite_stale_path_prefix`) that stores a `/app/data/reports/...`-style path while the real file lives elsewhere, proving the exact bug scenario is fixed. 4 more new tests for `resolve_report_path()` itself. Not a schema or write-path change — `generated_reports.json_path`/`md_path` still store the same absolute path as before; only the read side changed.
  - Branch: `milestone-33-conversation-edge-classification`. Next: Paul reviews and merges the PR, then Milestones 34–35 (any order), then M31 last.

- 2026-08-02: /build — Milestone 32 complete (Environment-Drift Metrics: Seed-Share + Verdict Mix)
  - **Built, per BUILD-PLAN.md's "Environment-Drift Metrics: Seed-Share + Verdict Mix — Milestone 32" spec:** report-layer only, no retrieval/wiping/MU-creation change, per the milestone's explicit scope.
    - `series_report.py`'s `_section_53_retrieval_rate_classification()` gained `seed_fixture_rate`/`cross_series_rate` fields (same `total_retrieved` denominator as the existing `intra_series_rate`), rendered as two new summary lines in the §5.3 Markdown block.
    - `experiment_report.py`'s `_aggregate_retrieval_rates()` carries `seed_share`/`cross_share` through per-series (read directly from the new series_report.py fields — never recomputed from raw counts, so the two layers can't silently disagree) and adds a `seed_share_drift` statement (first series vs. last series, in series order). A pre-M32 stored series report lacking the new fields renders `seed_share`/`cross_share` as `None` → `—` in Markdown rather than being backfilled from `seed_fixture_count`/`total_retrieved`, which were already present before M32 — deliberate, so the aggregation only ever states what series_report.py itself computed.
    - New verdict-mix subsection: `store.get_verdict_counts_by_series(experiment_id)` (new method — joins `feedback_events` to `analytics` on `run_id`, scoped by `experiment_id`, `is_probe_turn=0`) feeds `_aggregate_verdict_mix()` (per-series `up`/`down`/`none` counts, `up_rate = up/(up+down)`, `none` excluded from the denominator since it carries no learning signal). Wired into `series_summary["verdict_mix"]` and rendered in Markdown near §5.1 (per the spec: "it describes the run environment, not learning") — this is exactly the table hand-built from the console log during the 2026-08-02 exp0 analysis below.
  - **Backward-compat verified, not assumed:** `test_s53_experiment_markdown_missing_seed_share_renders_em_dash` and `test_aggregate_retrieval_rates_missing_fields_backward_compat` exercise a series_data shape with no `seed_fixture_rate`/`cross_series_rate` keys (the exact shape every pre-M32 stored series report on disk has) and confirm `None`/`—` rather than a crash or a silently recomputed value.
  - **Scoping verified:** `test_scoped_to_experiment_id_no_contamination` (store-level) and `test_generate_experiment_report_verdict_mix_scoped_to_experiment_id` (end-to-end) confirm a different experiment's feedback events don't leak into this experiment's verdict-mix counts even when both use the same `series_id` string — same contamination-guard pattern as the existing §6.1 probe-results scoping tests.
  - Tests: 14 new (rate arithmetic, aggregation carry-through, backward compat, verdict-mix scoping, zero-feedback-series rendering with `up_rate=None` rather than a fabricated `0.0`). 872 total passing, 3 skipped (pre-existing), no regressions.
  - Branch: `milestone-32-seed-share-verdict-mix`. Next: Paul reviews and merges the PR, then Milestones 33–35 (any order), then M31 last.

- 2026-08-02: experiment-0 analysis + follow-up milestones M31–M35 planned. No code changed.
  - **Run summary:** exp0 completed clean (630/630 turns, 3 conditions, ~12:30–14:57 UTC per the graph-wipe/report timestamps). Infrastructure verdict: success. Science verdict: null vs controls — §6.2 deltas exactly 0.0000, matching the M21 pilot.
  - **Finding 1 — environment drift.** Verdict tally from `harness_runs/exp0_console_2026-08-02.log` (UTF-16; converted before grepping): up-rate declines across series in **all three conditions** (learned 52→31→26→32→26%, static 62→52→21→31→38%, random 60→33→21→22→14%), so the decline cannot be a learning effect. Series reports show the cause: seed-fixture MUs' share of the 4 retrieval slots falls 42%→14% (learned s0→s4), 12% (static s4), 4% (random s4) as accumulated conversation MUs win BM25 slots — the graph is wiped between conditions but never between series. §5.3's "outlier" s0 (57.7% intra) is just the only series with no older neighbors. The calibration-vs-run up-rate gap is mostly this plus noise: the same 20 prompts scored 15up/5down at calibration and 11up/9down in the run (not significant at n=20); the 07-27 judge fix contributed ~1 recovered down per 20 turns, not the drop.
  - **Finding 2 — credit lands off-instrument.** §6.1 ledger: 176/225 updates (78%) on "unknown cluster" edges (conversation-MU edges). Only 49 touched classified seed edges, 7 cross-cluster. The probes measure none of this.
  - **Finding 3 — punishment-dominated regime.** 70 up / 138 down / 2 none in learned; ledger 81 increases / 144 decreases. The 13 "runaway-reinforcement" flags are all ordinary Beta(1,1) posterior-mean down-streaks (1/2→1/3→1/4→1/5, shrinking deltas) — detector heuristic (3 same-direction updates) mislabels convergence, and §5.5 also shows `traversals: 0` on every updated edge (counter broken).
  - **Finding 4 — trained spokes mostly didn't move; the interesting probe is untrained.** a1/b3 flat near zero, d5 declined; c1 passes 92–100% from session 1 (can't distinguish training from baseline without controls). vs_d4 (untrained) climbed 4→24→56→60→48% in sample mode — the only learning-shaped curve; §6.2 never compared it because it only reads mean-mode rows.
  - **Paul's decisions:** MU accumulation is a feature — measure (seed-share as first-class per-series metric), don't control. Build conversation-edge classification now (long-run cross-domain learning is expected there). Judge scheme change: up = all criteria met, down = none met, partial answers abstain (`none`, no update) — more realistic and un-skews the signal. Fix §6.2 sample-mode comparison.
  - **Planned:** BUILD-PLAN.md gains "Experiment-0 Follow-Ups — Milestones 31–35": M31 judge abstain scheme (build LAST + 20-turn recalibration; exp0b re-baseline decision follows), M32 seed-share + verdict-mix report metrics, M33 conversation-edge classification (derived at report time from prompt_id lineage + seed adjacency; done-criterion = resolving exp0's 176 unknowns), M34 §6.2 sample-mode comparison + Learned-column rendering fix (its live verification answers the vs_d4 drift-vs-learning question), M35 traversal counter + runaway-detector redefinition. Verified before planning: backend already records `verdict="none"` with no weight update (`chat.py:311`), so M31 needs no backend change.

- 2026-07-27 (later): judge criterion resolution + interactive recovery — from the completed calibration run. Branch `claude-fix-judge-criterion-normalization`.
  - **What prompted it:** the 20-turn `--interactive` calibration run completed (`harness_runs/exp0-calibration-2026-07-27_bb92d90280ba`, `final_status=complete`, 15 up / 5 down posted) with **one soft-continued turn** (index 4). The judge returned a perfectly good `down` citing `'local temporal readings/local time'` against a criterion written as `'answer: local temporal readings/local time'` — an exact-string match failure on a dropped label prefix. Both attempts failed identically, because `JudgeConfig.temperature` is capped at `le=0.1` and the omission is a stable reading, not a sampling accident: **the re-ask budget was being spent on a deterministic failure**.
  - **The finding that made this urgent, not cosmetic.** The old `_parse_verdict()` validated `criterion_triggered` *only* when `verdict == "down"` — an `up`/`none` citation was silently nulled. So this error mode could **only ever delete `down` verdicts, never `up` ones**. The run's true judge output was 15 up / 6 down; what got recorded was 15 up / 5 down. That is ~17% of the negative signal lost and 0% of the positive — in a calibration pass whose entire purpose is measuring judge up-skew, a failure that deletes only downs **biases the exact quantity being measured**. The headline 5% turn-level loss rate is the misleading number.
  - **Fix 1 — normalized criterion resolution (`scripts/harness/judge.py`).** `_resolve_criterion()` now tries exact match → normalized match → single-criterion deduction. `_normalize_criterion()` ignores case, whitespace runs, markdown emphasis, quotes, trailing punctuation, and leading `label:` prefixes (`answer:`, `criterion:`, `expected:`, …). The colon is **required**, so real criteria that open with those words (`states three`) are never truncated. Deliberately no fuzzy matching: the resolved value is what §6 groups failures by, so a too-eager match would attribute a failure to the wrong criterion. The resolved value is always the **bank's exact text**, never the judge's paraphrase. The observed exp0 turn now resolves on call 1.
  - **Fix 2 — the up/down asymmetry (`judge.py`).** `JudgeUnparseableError` is now reserved for failures that leave **no verdict at all** (bad JSON, unknown verdict value, HTTP failure) — all direction-neutral. An unresolvable citation returns the verdict with `criterion_unresolved=True` and the raw citation preserved. The re-ask still happens (a second sample occasionally cites cleanly, and it wins), but exhausting it now **degrades** the result instead of discarding it. In the residual non-interactive case the verdict still isn't posted (the backend 422s on a `down` with no criterion, and inventing one would corrupt per-criterion analysis) — but `judge_model_verdict` carries the `down` into the checkpoint, so the turn stays countable rather than vanishing. That is the actual balance: **the checkpoint always records what the judge said, in both directions.**
  - **Fix 3 — manual verdict recovery in `--interactive` (`scripts/harness/runner.py`).** On an unparseable judge response the operator was sitting right there with the full context on screen and was never asked. Now offered `Grade this turn yourself, or skip it? [g/skip]` after being shown the calibration context and the judge's failure; grading posts with `judge_source="human"` and a null `judge_model_verdict` (no model verdict exists). `skip` soft-continues as before. **`judge_unparseable: hard_stop` is still checked first** and aborts without prompting. Accepting an unresolved `down` in interactive mode now prompts for the criterion via the existing `_prompt_for_failed_criterion()`, and the judge's unmatched raw citation is shown so the operator knows why they're being asked.
  - Refactor: extracted `_post_verdict()` so the normal judged path and the manual-recovery path record identical metadata rather than drifting.
  - **856 tests passing** (was 820; +36). New coverage includes the exact exp0 citation as a parametrized regression, proof that `states three` survives prefix stripping, proof that distinct criteria are never conflated, and the four new runner paths.
  - **Not changed, on purpose:** the `answer: ` prefix in the bank's criteria. Normalizing the match is the robust fix — it also covers markdown, case, and punctuation drift the bank can't anticipate — whereas editing the bank would change its `content_hash` and invalidate the verified 210-prompt bank for a problem now handled in code.

- 2026-07-27: /smoketest — judge calibration setup. **4 real bugs found and fixed, 3 of which would have hit the experiment-0 run itself**
  - **Goal:** set up the `--dry-run --interactive` judge calibration Paul asked for. Baseline: 820 tests passing, Docker stack up 21h, `gemma4:e4b` reachable, backend healthy. Found in passing that a 2-turn `--dry-run` of `plans/exp0_2026-07-26.json` had **already run clean on 2026-07-26** (`harness_runs/exp0-2026-07-26_1473bdf7befc`, `final_status=complete`, both turns judged and posted, experiment report generated) — so the "confirm the stack end to end" step was already done; what had never been exercised at all was `--interactive`.
  - **Bug 1 (`04185f0`) — `--interactive` showed no basis for the decision.** Calibration mode printed the verdict and the judge's *own rationale* — the thing under audit — and nothing else. The operator never saw the prompt, the criteria, or the agent's response, so "accept or override?" was unanswerable and the only available action was to rubber-stamp the model. Both pre-existing interactive tests used a no-op `print_fn`, so nothing covered what the operator actually sees. `_print_calibration_context()` now shows all three, deliberately untruncated (a response clipped mid-sentence can flip a genuine "up" into an apparent "down", putting the wrong labels into the exact record the mode exists to produce). Also: any answer outside `a`/`o`/`skip` fell through to "accept", so a typo — or answering the accept/override question with "up" — silently recorded a verdict the operator was mid-way through rejecting; `_prompt_until_valid()` re-asks instead.
  - **Bug 2 (`192b7e7`) — overriding a verdict to "down" was rejected and silently dropped.** `POST /feedback/verdict` returns **422** when `verdict="down"` carries an empty `criterion_triggered` (`backend/api/chat.py:273`), and `_parse_verdict()` nulls that field on every non-"down" verdict. So overriding a model "up" to "down" posted `None`, was rejected, and soft-continued into a lost verdict — **silently discarding the single most valuable correction available on an up-skewed judge**, which is precisely what this calibration pass exists to catch. Confirmed live with a direct `curl` (422) before fixing, and confirmed fixed end to end afterwards. The operator now names the failed criterion by number. Also added `TurnCheckpoint.judge_model_verdict`: nothing on disk retained what the model said once an override replaced it, so a calibration pass could report *how often* the operator intervened but never the judge's **error direction** — the actual measurement.
  - **Bug 3 (`2fc3ecd`) — a report soft-continue crashed the whole run on Windows.** The soft-continue message carried `⚠` (U+26A0), which cp1252 (the default Windows console encoding) cannot encode. `print()` raised `UnicodeEncodeError` *from inside the soft-continue handler*, so the exception escaped it and took the run down with a traceback — the exact opposite of what `error_policy["report_generation"] = "soft_continue"` promises. Found by triggering it live via bug 4. The regression test asserts every line printed on that path is cp1252-encodable, rather than checking for the one character that happened to break.
  - **Bug 4 (`af542ef`) — seed and report calls exceeded the 60s client timeout mid-run.** Two call classes measured past the generic budget: **`seed-graph` at 63.8s** (seed_v3's 34 MUs + 64 edges; bulk graph writes go one row at a time through the connection-per-operation store, each insert paying a fresh connection + fsync, ~0.3–1.0s per edge on a Docker bind-mounted volume) and **`generate-experiment` at 55.7s** against a near-empty DB (286 analytics rows). The seed case is the dangerous one: seed failures are classified `connectivity`, which defaults to `hard_stop`, so **the harness aborted the entire run over a graph the backend had in fact seeded correctly** — and at 63.8s vs 60s it was a coin flip per condition, which is why earlier runs squeaked through. Both now get their own timeout, following `DEFAULT_CHAT_TIMEOUT`'s existing precedent; `wipe-graph` and `edges/randomize` share seed's budget (same bulk-write class, and randomize runs at the start of every `random_weight` series).
  - **Open finding, NOT fixed — §6.1's N+1 probe query.** `generate-experiment`'s 55.7s is almost entirely one step: `experiment_report.py:1153-1183` issues **one `query_probe_results()` call per probe definition, twice over** (mean rows, then sample rows since M30), each with a `condition_label` fallback pass — ~**172 sequential round-trips** on the 43-probe v3 battery, against a store that opens a connection per operation. It is round-trip-bound, not row-bound (the DB holds only 1114 `probe_results` rows today). Raising the timeout removes the failure; the waste remains. **Tracked as TODO-1 above, where a follow-up check established it does not block experiment 0** — `probe_results` is indexed on `experiment_id` and the query plan uses that index, and the 172 round-trips measured here only happen because both experiment-scoped loops came back empty and triggered both `condition_label` fallbacks, which will not happen on a real run. Fix it before the 10–15-run screening series, not before experiment 0.
  - **Calibration plan committed (`e433c95`): `plans/exp0_calibration_2026-07-27.json`.** A dedicated plan rather than `--dry-run` against the exp0 plan, because `dry_run_n` is part of the plan body and therefore of `config_hash` — the committed exp0 plan pins it at 2, and editing it to 20 would change the real run's own `run_id`. Everything feeding turn resolution and prompt assignment is copied verbatim, and **the 20 graded turns were verified to be exactly the prompts the real run's learned condition will see, in the same order**. All 20 are bulk turns (`probe_placement: "end"` puts the 28 probes at turns 42–69), spread 6/5/5/4 across clusters a/b/d/c. `run_identity_for()` suffixes a dry run's `experiment_id` with `_dryrun`, so it cannot contaminate the real run's analytics or feedback_events.
  - **Judge skew watch:** every bulk turn judged across all dry runs so far has come back `up` — 2 on 2026-07-26 and 4 this session, 6/6. Consistent with the 2026-07-06 pilot's ~71-up/5-down skew, and the reason the 20-turn pass is worth running before committing ~2.4h to the real thing.
  - Tests: 820 baseline → **836** passing (16 new), no regressions. Branch: `fix/smoketest-2026-07-27`.
  - **Next: Paul runs the calibration pass** (`python scripts/run_experiment_harness.py --plan plans/exp0_calibration_2026-07-27.json --interactive`), then the full three-condition experiment 0.

- 2026-07-26 (continued): keyword-window fix + **experiment-0 prompt bank generated and verified**
  - **Design decisions written up** in the new `docs/exp0_design_decisions_2026-07-26.md`, at Paul's request. Covers all three choices (`weight_mode`, sizing, `trained_spokes`) plus the fingerprint scoping change, with the alternatives and what would have gone wrong. The `weight_mode` section is the substantive one — it works through the mechanism (deterministic K2 argmax over tied `Beta(1,1)` edges + credit following only the traversal path ⇒ the initial tie-break winner is locked in on turn 1 and no other spoke can ever gain evidence) and tabulates which spoke would have been stranded in each cluster. **Paul's forward note is recorded there:** a future run should move to a non-spoke1 trained spoke in *all four* clusters, since under `sample` every spoke has equal ~1/8 odds and the current a1/b3/c1/d5 asymmetry is a `mean`-mode artifact carried into a `sample`-mode experiment; deferred for experiment 0 only because `trained_spokes` is part of the bank's `content_hash`.
  - **Blocker resolved — v3 fingerprint rule scoped to the BM25 keyword window** (`e61feab`). Tier 2 (other clusters' entry vocabulary) is now enforced over the first `retrieval_keyword_count` (5) non-stopword tokens instead of the whole prompt; tier 1 (answer tokens) stays whole-prompt and all-clusters. Justification is mechanical: `retrieve()` only ever uses those 5 tokens as BM25 keywords, so a term past the window cannot pull another cluster's hub into K1 — which is the only thing the rule protects. `bm25_keywords()`/`load_retrieval_keyword_count()` mirror `retrieval.py`; the tokenizer is duplicated rather than imported (host-side script) with 3 tests asserting the copies stay identical.
  - **Two further generation-path bugs found by a 12-prompt paid trial**, both consequences of adaptive thinking being on by default: (1) the v3 generation prompt inherited v2's blanket "must NOT repeat the cluster's distinctive vocabulary", which points the **wrong way** for v3 — a v3 prompt must front-load its own cluster's entry vocabulary or BM25 never reaches the hub (the 48/75 baseline-retrieval failure `docs/harness_usage.md` §4 records). It now states its actual rules, with `cluster_id` threaded through. (2) The flat 400-tokens-per-target budget broke both ways — a 3-target batch truncated mid-array, and the per-target retry path got 400, enough to be consumed entirely by thinking and return `""` (the coherence-gate failure again, on the generation path). Replaced by `generation_max_tokens()` with a floor, and generation now **streams**, since the real 53-target batch needs a budget large enough that a non-streaming request risks an SDK HTTP timeout.
  - **Bank generated: `prompt_bank_e2adf796...json`, 210 prompts, 0 exclusions.** 261 API calls, 30 retries (13 fingerprint leaks — `rule` ×9, `data` ×2, `structure`, `spine order` — and 17 coherence rejections), **every one recovered on the first retry**. Per-cluster 53/53/52/52, all validated. Compare the pre-fix attempt: 8 of the first 10 prompts failed, 3 already permanently excluded. `prompt_bank_path` stamped into the plan so the run path never re-enters generation.
  - **Bank verified against every requirement, all pass:** `content_hash` matches the plan's generation params; 210 entries with the correct per-cluster split; unique `prompt_id`s; every entry stamped `seed_fixture_version=v3`; 0 excluded and 0 placeholder entries; 1–3 non-empty criteria each; hop levels within `[0, 1]` and alternating evenly per cluster (27/26, 27/26, 26/26, 26/26); **0 fingerprint leaks** under the committed rule and **0 answer tokens anywhere** in any prompt under the stricter whole-prompt tier; **all 210 land ≥2 of their first 5 BM25 keywords on their target cluster's hub** (the multi-hit threshold `retrieve()` needs to admit a hub into K1); and **no prompt hits another cluster's hub harder than its own**. The last two checks are the ones worth keeping around — a bank can pass every gate `prompt_gen` applies and still be useless if BM25 never reaches the hub, and the generator does not check for that itself.
  - Tests: 798 → **820** passing (22 new across the two sessions), no regressions. Branch: `claude-fingerprint-keyword-window`.
  - **Experiment 0 is now ready to run.** Docker is up. Remaining pre-run steps are operational, not blocking: a `--dry-run` to confirm the stack end to end, and optionally `--dry-run --interactive` judge calibration on the new bank (recommended — 210 fresh criteria have never been graded by `gemma4:e4b`).


- 2026-07-26: /smoketest — experiment-0 prep. Plan + weight_mode decided, 2 real bugs found and fixed, bank generation **blocked** on a seed_v3 vocabulary collision
  - **Goal:** generate the experiment-0 prompt bank and identify anything else needed before the full run. Baseline: 791 tests passing, Ollama up with `gemma4:e4b`, `ANTHROPIC_API_KEY` set. **Docker daemon was down all session** — bank generation is host-side so this didn't block it, but it does block the run itself and any live probe battery.
  - **Three decisions made (Paul's, in-session) and committed as `plans/exp0_2026-07-26.json` + a `config.yaml` change (`9c3cfad`):**
    - **`edge_update.weight_mode: mean -> sample`.** This is a *requirement*, not a preference, and it resolves the question left open since M28. Under `mean` all 8 of a hub's edges read exactly 0.5, so K2's winner-take-all argmax is deterministic and the same neighbour wins every training turn; `process_verdict_for_learning()` credits only edges on the actual traversal path, so no other spoke can ever accumulate evidence. Under `mean` this plan would return a **structural null**: `b_spoke3`/`d_spoke5` would never be traversed, and `cluster_a`/`cluster_c` would credit only the bridge. Under `sample`, Thompson draws give each edge ~1/8 of turns, so the trained spoke gets traversed, earns credit when the judge grades its content "up", and climbs.
    - **Sizing: 5 series × 70 turns, `probe_ratio` 0.4.** Found while checking `assign_probe_schedule()`: `probe_ratio` derives probe turns from `turns_per_series`, and `probe_set_v3` is a **28**-probe battery, so a series needs exactly 28 probe turns for one full battery per session. The docs' v2-era reference shape (5×25, ratio 0.4) yields only **10** — and `assign_probe_schedule()` then walks the same leading 10 probes (`vs_a1`..`vs_b3`) every session, so **clusters c and d would never be probed at all**. 5×70 gives 210 bulk + 140 probe turns/condition, 28/session × 5 sessions, and a 210-prompt bank covering the bulk turns exactly once. ~2.4h for 3 conditions.
    - **`trained_spokes`: a1 / b3 / c1 / d5.** `b_spoke1`/`d_spoke1` are the only two spokes reading an already-passing baseline under `mean` (stable across M28, PR #50, PR #51), so training them starts the headline fail→pass observable with zero headroom. Clusters a/c surface no spoke at baseline (the bridge wins the hub tie), so spoke1 keeps full headroom there.
    - Deliberately **not** run: the higher-N sample-mode measurement (findings-doc option 1). Its own binomial analysis argues the 28 spokes are structurally symmetric under `sample`, so a larger N would most likely just re-confirm that — it isn't decision-relevant for `trained_spokes`, and the `mean`-mode baseline that *is* decision-relevant is already stable across 3 independent measurements.
  - **Bug 1 fixed (`6e8e08a`): declining the prompt-bank cost gate printed a raw traceback.** `run_experiment_harness.py` called `generate_bank_for_plan()` at two sites and caught neither `GenerationAborted` nor `MissingApiKeyError`. Both are expected operator outcomes, and a stack trace immediately after a cost-confirmation prompt reads as a crash mid-API-call — leaving the operator unsure whether spend or a file write happened (neither did). New `generate_bank_or_exit()` wraps both sites; 3 regression tests.
  - **Bug 2 fixed (`86742ce`) — the one that would have wasted the bank: the coherence gate could never pass.** `_validate_one()` asked Sonnet for a one-word YES/NO verdict with `max_tokens=10` and no `thinking` parameter. On `claude-sonnet-5` omitting `thinking` does **not** mean "no thinking" — adaptive thinking is on by default and `thinking.display` defaults to `"omitted"`. `max_tokens` caps thinking + response text together, so the whole budget went to an empty-text thinking block, `_call_sonnet` extracted `""`, and every prompt that got past the fingerprint scan was scored `coherence validation failed: ''`. Confirmed directly against the API: at `max_tokens=10` the response is `stop_reason='max_tokens'`, `block_types=['thinking']`, empty text — **and at 64 it was still all thinking**, so raising the budget alone does not fix it. Fixed with `thinking={"type": "disabled"}` on that call only (generation has a large budget and benefits from reasoning, so it keeps the model default) plus 10→64 tokens. Verified after the fix: a genuinely unanswerable prompt reads `'NO'`, an answerable one passes. 4 regression tests; `FakeMessages.create` now takes `**kwargs` so the fake accepts (and records) every optional request field.
  - **BLOCKER — bank generation stopped, no bank written, ~$0.02 spent.** Ran `--generate-bank` for real, watched the log, and killed it 10 prompts in: **8 of 10 failed validation at least once and 3 were already permanently excluded**. The dominant cause is not a code defect but a **seed_v3 design collision**: `fingerprint_scan()`'s v3 rule forbids a prompt from containing *any other cluster's* entry vocabulary, and all four clusters' entry vocabularies are generic English words drawn from one shared semantic domain — **27 of the 30 terms forbidden per cluster are ordinary English** (`data`, `type`, `source`, `version`, `format`, `column`, `range`, `validation`, …). Worst case: **`rule` is `cluster_c`'s entry vocab, so no `cluster_a`/`b`/`d` prompt may use the word "rule" at all** — while the entire fixture is a set of *rule families*. Observed leak frequency across the killed run: `rule` ×12, `data` ×7, `column` ×1, `violation` ×1. Extrapolated to 210 prompts that's ~60+ placeholder entries at roughly 4× the estimated spend — an unusable instrument.
  - **AWAITING PAUL — this needs a science call, not a patch.** The most promising fix, and the one I'd recommend: **scope the cross-cluster entry-vocab check to the BM25 keyword window** (the first `retrieval_keyword_count` = 5 non-stopword tokens) instead of the whole prompt, while keeping answer tokens forbidden everywhere. The justification is mechanical: `retrieve()` only ever takes the first 5 non-stopword tokens as keywords, so other-cluster vocabulary appearing *later* in the prompt is invisible to retrieval and cannot corrupt the hop-distance measurement — which is the only thing the cross-cluster rule exists to protect. Answer tokens stay whole-prompt because they'd also hand the agent the answer directly and corrupt the judge's verdict. Alternatives: regenerate `seed_v3` with invented (non-generic) entry vocabularies — correct but a large blast radius across fixture, probes, and bank; or accept the exclusions, which yields an unusable bank. **Nothing else blocks experiment 0** once this is settled: plan, `weight_mode`, `trained_spokes`, sizing, probe set, and fixture are all in place, and both harness bugs on the generation path are fixed.
  - Tests: 791 baseline → **798** passing (7 new), no regressions. Branch: `fix/smoketest-2026-07-26`.
  - Also still open from earlier sessions (unchanged, not touched this session): `docs/experiment_protocol.md`'s RunPlan field table still needs a `trained_spokes` row, and `docs/harness_usage.md` §3's sizing guidance still describes the v2-era 10-probe battery — both would now mislead an operator sizing a v3 run.

- 2026-07-26: /build — Milestone 30 Part B complete (§6.1 dual-mode battery report consumption), and folded the still-unmerged PR #51 into this same push
  - **Built, per BUILD-PLAN.md's "Dual-Mode Probe Battery — Milestone 30" Part B spec:** `experiment_report.py`'s `_section_61_learning_measures()` now passes `battery_mode="mean"` on both `query_probe_results()` calls (the `experiment_id`-scoped call and the `condition_label`-fallback call) — behavior-preserving for every experiment recorded before M30 (pre-M30 rows already default to `battery_mode='mean'` at the schema level), and closes the corruption path for real dual-mode data (every mean-only subsection — `state_transitions`, `bridge_mu_analysis`, `probe_pass_rates`, `mus_by_probe_outcome`, `cross_series_probe_mus` — would otherwise have silently received 1 mean row + K sample repeats per checkpoint instead of 1). New `_compute_sample_pass_rates()`, parallel in shape to `_compute_state_transitions()`: fetches `battery_mode="sample"` rows with the same `experiment_id`-first/`condition_label`-fallback pattern, groups by `measurement_session_index`, classifies each repeat via the existing `_classify_probe_outcome()` (reused, not reimplemented), and computes `pass_count`/`fail_count`/`repeats_completed`/`pass_rate`/`partial_battery` per session (`configured_repeats` read from `config["probe_measurement"]["sample_battery_repeats"]`, default 25 — same default `runner.py` uses). Wired into §6.1's returned dict as `sample_battery_repeats_configured` and `sample_pass_rates`. `markdown_renderer.py`'s `_render_s61()` gained a new "Sample-Repeat Pass Rate (`weight_mode=sample`)" table (probe_id/session/pass_rate/repeats_completed-over-configured/flag), placed after the State Transitions block, with a `⚠ PARTIAL BATTERY` flag on any session that stopped short of the configured repeat count — same visual convention as the existing orphan-update flag in the Edge Delta Ledger block.
  - Tests: 6 new (785 baseline + 6 — `test_learning_measures_ignores_sample_repeats_for_mean_sections` is the regression test for the actual bug, asserting a mixed mean+sample row set at one session index still produces exactly 1 mean-classified outcome; `test_sample_pass_rates_computed_from_repeats`, `test_sample_pass_rates_full_battery_not_flagged_partial`, `test_sample_pass_rates_scoped_to_experiment_id` (mirrors the existing mean-row contamination guard), `test_sample_pass_rates_empty_when_no_sample_rows` (every pre-M30 experiment still generates §6.1 cleanly); plus `test_s61_renders_sample_pass_rate_section` in `test_markdown_renderer.py`). 791 total, all passing, no regressions.
  - **PR consolidation (per Paul's explicit ask and BUILD-PLAN.md's own operational note):** PR #51 was still open and unmerged from the 2026-07-15/2026-07-26 sessions, carrying the tie-break finding (`b8c7632`), the `experiment_protocol.md` rewrite (`1f39c23`), and M30 Part A (`aea3a24`) — all on `fix/smoketest-2026-07-15`. Rather than opening a second PR for Part B, this session added its two commits (`_section_61_learning_measures`/`_render_s61` + tests, then this docs update) to that same branch and pushed, updating PR #51 in place — exactly the "fold it in" outcome Paul asked for. There was no separate PR to remove: PR #51 *is* the consolidated PR now, retitled/redescribed to cover the full arc (tie-break finding → dual-mode battery → report fix) rather than just its original narrower scope. Local history needed a force-push: the branch's first commit had been amended locally (`b8c7632`, identical tree/content to the already-pushed `5298454`, only the parent commit differs) after the initial push, so the branch had diverged from `origin/fix/smoketest-2026-07-15` by rewritten history, not just new commits on top — confirmed via `git diff` that the two versions of that commit are byte-identical before force-pushing.
  - Branch: `fix/smoketest-2026-07-15`. PR: https://github.com/PaulArthurMiller/local_corvus/pull/51 (retitled/redescribed, not a new PR).
  - Next: Paul reviews and merges PR #51. Then decide `weight_mode`/`trained_spokes` per the "Next step" note above.

- 2026-07-15 (backfilled 2026-07-26 — built via Claude Code in an unlogged session, discovered during that day's /catchup): Milestone 30 Part A complete (Dual-Mode Probe Battery — harness + backend dispatch)
  - **Built, same session as and immediately following the `learned`/`sample` tie-break finding below (commit `aea3a24`, 19:29 — the direct engineering response to that finding):** at every probe checkpoint, the harness now issues 1 deterministic `weight_mode="mean"` battery pass plus K independent `weight_mode="sample"` repeats (K = new `config.yaml` hyperparameter `probe_measurement.sample_battery_repeats`, default 25, host-read by `runner.py` with no backend restart needed to change it), identically across all three conditions. `retrieve()` gained a `weight_mode_override` parameter scoped to the single `POST /admin/run-probe` call path — real training turns never pass it, so training-turn behavior is unaffected. `RunProbeRequest`/`ProbeResult`/the `probe_results` table gained `series_id`, `battery_mode` (`"mean"|"sample"`), and `battery_repeat_index` columns.
  - **Real bug fixed:** `admin.py`'s `run_probe()` was passing the orchestrator's process-scoped `EDGE_RNG` into `retrieve()` — the same RNG stream real training turns use for `weight_mode: sample` draws. A probe checkpoint run under `sample` mode was silently perturbing the reproducibility of subsequent training turns' sample draws. Fixed by no longer passing `rng=` from `run_probe()`, matching `retrieval.py`'s own `_DEFAULT_RNG` docstring (which already documented probe callers as the intended user of the unseeded fallback).
  - Tests: 25 new (785 total) — dispatch/error-handling/config default (`test_runner.py`), `ProbeResult` new fields (`test_probes.py`), the admin.py endpoint incl. the RNG fix (`test_run_probe_dual_mode.py`), `weight_mode_override` (`test_retrieval.py`), `series_id`/`battery_mode` filtering + migration (`test_store.py`). All passing.
  - **Process gap this entry exists to close:** this work was code-complete and tested but never written up in BUILD-PLAN.md or PROGRESS.md at the time, and stayed local-only (unpushed) for 11 days until a 2026-07-26 /catchup surfaced it. Reconciled by adding a full "Dual-Mode Probe Battery — Milestone 30" section to BUILD-PLAN.md (which also specified Part B, the §6.1 report-consumption gap this session's own investigation found) before /build touched any code.
  - Branch: `fix/smoketest-2026-07-15` (unpushed at the time; pushed 2026-07-26 alongside Part B).

- 2026-07-26: /catchup then /planner — reconciled unlogged Milestone 30 work, planned Part B (§6.1 report consumption)
  - **Catchup surfaced a real state mismatch:** local `fix/smoketest-2026-07-15` had 2 commits neither pushed nor written up — `1f39c23` (Paul's long-pending `docs/experiment_protocol.md` rewrite, committed but never pushed) and `aea3a24` (`feat(milestone-30): dual-mode probe battery at every probe checkpoint` — real code, 25 new tests, 785 total passing, built via Claude Code in an unlogged session, mistakenly believed already committed/pushed).
  - **Resolved a related question:** PR #51 ("learned/sample tie-break overturns M28/M29's clean-fail premise") is chronologically *prior to* M30, not M30 itself — it's the finding (`b8c7632`/`5298454`, 15:50) that motivated building M30's dual-mode dispatch (`aea3a24`, 19:29) later the same day. Nothing to fold in on the PR #51 side; it's already fully written up.
  - **Investigated whether M30 was actually complete before backfilling docs, per Paul's explicit ask.** It wasn't: traced every consumer of `store.query_probe_results()`'s new `battery_mode` filter and found `experiment_report.py`'s `_section_61_learning_measures()` — the only report-layer consumer of probe results — never passes it. Since the mean pass and all K=25 sample repeats share one `measurement_session_index`, every §6.1 subsection (`state_transitions`, `bridge_mu_analysis`, `probe_pass_rates`, `mus_by_probe_outcome`, `cross_series_probe_mus`) would silently receive 26 rows per checkpoint instead of 1 the first time a real dual-mode battery ran through a real experiment — corrupting rank-trajectory/fail-to-pass detection. None of Part A's 25 tests touch `experiment_report.py`, so this wasn't caught. `series_report.py` confirmed unaffected (its own probe-related section, §5.6, only counts `feedback_events` rows, unrelated to `probe_results`).
  - **Planned (this session, /planner — no code written, per the skill's own rule) Milestone 30 Part B** to close that gap: `battery_mode="mean"` filter added to §6.1's two `query_probe_results()` calls (behavior-preserving — pre-M30 rows already default to `'mean'`), plus a new `_compute_sample_pass_rates()` subsection (pass_count/fail_count/repeats_completed/pass_rate per probe per checkpoint, with a partial-battery flag for soft-continued batteries that stopped short of the configured K) surfaced in both the JSON report and `markdown_renderer.py`'s `_render_s61()`. 6 new tests specified, including a regression test for the exact bug found (`test_learning_measures_ignores_sample_repeats_for_mean_sections`). Full spec (function names, exact call sites, test list) written to BUILD-PLAN.md's new "Dual-Mode Probe Battery — Milestone 30" section, plus a Milestone 30 entry in the milestone list and a Status block update — the retroactive documentation gap that let Part A go unrecorded shouldn't recur.
  - **Branch/PR guidance recorded for /build:** continue on the existing `fix/smoketest-2026-07-15` branch (not a fresh milestone branch off `main`, which doesn't have Part A's code) — pushing will update the already-open PR #51 with all three commits (tie-break finding, docs rewrite, M30 Part A) plus Part B's new work.
  - Tests: 785 passing throughout (no code changed this session — planning only).
  - Branch: `fix/smoketest-2026-07-15` (unpushed). Next: /build Milestone 30 Part B.

- 2026-07-15: /smoketest (2 passes) — re-confirmed the M28 baseline tie-break, then found it doesn't generalize to `weight_mode: sample` the way expected; no bugs found, no functional code changes (config.yaml toggled for measurement only, restored)
  - **Pass 1 — stability across reseeds (`static_weight`/`mean`, the accidental default):** pulled fresh `main` (fast-forwarded 6 commits, now includes merged PR #49 for M29). Baseline test suite: 760 passed, 0 failed. Fresh Docker rebuild, fresh wipe+reseed of `seed_v3` (34 MUs/64 edges, matches frozen fixture hash), all 28 `probe_set_v3.yaml` probes run individually via `POST /admin/run-probe`. **Exact reproduction of M28's recorded baseline:** 26/28 `gold_mu_rank=None`, `vs_b1→4`, `vs_d1→2`. Confirmed this measurement ran under `RUN_CONDITION` defaulting silently to `static_weight` (env var never set) — not `learned` as originally assumed. PR #50 opened at this point with the (later found to be incomplete) conclusion that the tie-break was safe to treat as a hard constraint.
  - **Pass 2 — same branch/PR, prompted by external review of pass 1's own report:** the review correctly flagged that neither M28's original check nor pass 1 ever exercised `condition="learned"`, and that `weight_mode: sample` (not yet tested at all) uses a fundamentally different mechanism (per-edge Thompson draws vs. a deterministic posterior mean) — "passes at baseline under mean" doesn't describe what the learned condition sees at session 0 if `sample` ends up being the real run's mode.
    - Restarted the backend with `RUN_CONDITION=learned` explicit, fresh wipe+reseed. **`learned`/`mean`: identical result to `static_weight`/`mean`** (`vs_b1→4`, `vs_d1→2`, 26/28 `None`) — expected, since `alpha=beta=1` on every seed edge makes `mean` compute 0.5 regardless of condition; confirms no bug.
    - Flipped `config.yaml`'s `edge_update.weight_mode` to `sample` (config is read fresh per-request, no restart needed) and ran the 28-probe battery 25× against the same seeded graph (probes are read-only, no reseed needed between repeats; the process-scoped `EDGE_RNG` naturally advances each call, giving 25 independent draws). **Result: every probe shows a noisy 0–36% pass rate — no clean deterministic split.** Only `vs_a3`/`vs_a6` read exactly 0/25, but a binomial check (assuming each of the 8 tied edges per hub has ~1/8 win probability) shows P(≥2 of 28 probes falsely read 0/25 by pure chance) ≈ 26% — statistically indistinguishable from noise at N=25. Full table and analysis in `docs/m28_tiebreak_learned_mode_findings_2026-07-15.md`.
    - **Re-verified the K2-traversal-only property under `learned`/`sample`** (M28's original trace inspection was `mean`-only, flagged as "inherited, not re-proven" in pass 1): direct `retrieve()` calls against the live graph confirmed 3 spot-checked sample-mode passes (`vs_a1`, `vs_b6`, `vs_d1`) all arrive at `hop_depth=1` (K2 traversal), never `hop_depth=0` (lexical) — property holds.
    - Restored `config.yaml` to `weight_mode: mean` and the backend to its default (no `RUN_CONDITION`) afterward — no permanent config change.
    - **Added a protocol note to `BUILD-PLAN.md`'s Status block:** any future baseline claim must record which `condition` and `weight_mode` it was measured under, since `RUN_CONDITION` silently defaulting to `static_weight` is exactly the class of bug the project's own probe-integrity principle already guards against elsewhere — both M28's original check and pass 1 fell into it unlabeled.
  - Not tested: full app functionality walkthrough, real training turns (both passes measured pure seed-graph retrieval only). Bugs found: none — the finding is a design-implication gap, not a code defect. Tests: 760 passing throughout (no functional code touched).
  - Branch: `fix/smoketest-2026-07-15`. PR: https://github.com/PaulArthurMiller/local_corvus/pull/50
  - Next: Paul decides the sample-mode spoke-selection path (bigger N vs. accept-and-handle-via-methodology) per the "Next step" note above.

- 2026-07-15: /build — Milestone 29 complete (Per-Cluster Trained-Spoke Selection)
  - **Blocker resolved at session start:** local `main` was stale by 10 commits — PR #48 (Milestone 28) had already been merged, but the harness's local checkout hadn't fetched it. `git fetch origin main` confirmed the merge (`d738930`); branched Milestone 29 off the fetched, up-to-date `main`, not off the old `claude-seed-v3-harness-wiring` branch. The M29 planning notes already written (uncommitted) to `BUILD-PLAN.md`/`PROGRESS.md` on that old branch were stashed, carried onto the new branch, and committed as their own docs-only commit — `docs/experiment_protocol.md`'s uncommitted in-flight edit (Paul's, not this session's) was stashed and restored the same way but deliberately left uncommitted, per the standing note not to touch it.
  - **Built:** `RunPlan.trained_spokes: dict[str, str]` (`schema.py`) — defaults to `{"cluster_a": "a_spoke1", ...}` (today's exact hardcoded behavior), shape-validated by a new `field_validator` (exact 4 cluster-id keys, values matching `^[abcd]_spoke[1-7]$`; `TRAINED_SPOKE_CLUSTERS`/`_TRAINED_SPOKE_KEY_RE` module constants mirror `prompt_gen.py`'s `CLUSTERS` rather than importing it, avoiding the circular-import problem flagged during planning). `read_cluster_content()` (`prompt_gen.py`) gains a `trained_spokes` parameter (default `_DEFAULT_TRAINED_SPOKES`, matching the schema default, for any direct caller that doesn't pass one), resolves `spoke_text` from `trained_spokes[cluster_id]` instead of the hardcoded `f"{letter}_spoke1"`, and raises a fixture-version/cluster-id/spoke-key-identifying `ValueError` when the key doesn't resolve to a real MU — closing the prior silent-failure path (`next(..., "")` returning an empty `spoke_text` on a bad key). `build_bank()`/`estimate_bank_cost_for_plan()` thread `plan.trained_spokes` through unchanged. `generation_params_for_plan()` now includes `trained_spokes` in its returned dict, so it's part of `compute_bank_hash()`'s hashed payload — two plans differing only in trained spoke choice now produce different `content_hash` values instead of silently sharing a bank. `queue.py` needed zero changes, confirmed by inspection (it only calls `generation_params_for_plan(plan)`, which already reads whatever `plan.trained_spokes` is).
  - Tests: 30 new — `test_schema.py` (7: default value, explicit override, missing-key/extra-key/malformed-value/wrong-cluster-id/bad-shape rejections, each field-identifying); `test_prompt_gen.py` (9: non-spoke1 override returns different content while untouched clusters keep spoke1, named `ValueError` on an unresolvable key, `generation_params_for_plan` includes `trained_spokes`, two plans differing only in `trained_spokes` produce different `content_hash`, `estimate_bank_cost_for_plan`/`build_bank` thread `plan.trained_spokes` into `read_cluster_content` — verified via a monkeypatched spy rather than a full mocked-Sonnet generation run, since satisfying `_finalize_entry`'s real validation/retry loop with canned responses was fragile and beside the point of what M29 needed to prove). 760 total (730 baseline + 30 new), all passing, no regressions.
  - Deliberately not done (per BUILD-PLAN.md's own scope): picking real spoke values for the first v3 `RunPlan` (needs a fresh wipe+reseed+probe pass to confirm the M28 tie-break is stable), `weight_mode`, turn/probe counts, `budget_cap`, and the `docs/experiment_protocol.md` RunPlan field-reference row (blocked on Paul's own uncommitted rewrite of that file landing first).
  - Branch: `milestone-29-trained-spoke-selection`.
  - Next: Paul reviews the PR. Once merged, proceed with the baseline-stability re-check and real spoke selection per this file's "Next step" note above.

- 2026-07-15: /planner — Milestone 29 (Per-Cluster Trained-Spoke Selection) added to BUILD-PLAN.md
  - **Source:** a design discussion (Paul + an external second opinion) working through M28's baseline finding — `b_spoke1`/`d_spoke1` already read as a passing probe at session 0 under the frozen `seed_v3.json.gz`, so training those two spokes would give the run's headline fail→pass observable zero headroom in half its clusters. Rejected hand-tuning the seed weights to force a fail (would confound the "learning did this, not the graph" claim); settled on making the trained spoke a per-cluster `RunPlan` field, selected by baseline headroom before generation.
  - **Design decisions made while reading the actual code (not just the spec/discussion):** (1) scope is contained to `schema.py` + `prompt_gen.py` — `read_cluster_content()` is only ever called from within `prompt_gen.py`, so `runner.py`/`queue.py`/both CLI entry points need zero changes; (2) `schema.py` cannot import the generator modules to validate spoke keys semantically (prompt_gen.py already imports `RunPlan` from `schema.py` — would be circular), so the new field gets shape-only validation at the schema layer and a loud runtime `ValueError` in `read_cluster_content()` — closing a real pre-existing silent-failure gap (`next(..., "")` currently returns an empty `spoke_text` on a bad key with no error); (3) `generation_params_for_plan()` must include `trained_spokes` in the hashed params, or two plans differing only in which spoke is trained would collide on `content_hash` and silently reuse each other's bank — this wasn't mentioned in the source discussion, found by reading `compute_bank_hash()`'s actual inputs.
  - **Explicitly out of scope, deferred to after M29 lands:** picking the real spoke values (needs a second wipe+reseed+probe pass to confirm the M28 tie-break is stable, not a one-snapshot artifact — flagged as a caveat in the source discussion, not yet done), `weight_mode`, turn/probe counts, `budget_cap`.
  - Full section written to BUILD-PLAN.md ("Per-Cluster Trained-Spoke Selection — Milestone 29"), including required test coverage (unit tests only — no live run needed, pure config-threading change). No code written this session — planning only, per the skill's own rule.
  - Noted but not acted on: `docs/experiment_protocol.md` has an uncommitted rewrite from another session (git status at session start) — left untouched since it's Paul's in-flight edit, not this session's to commit or continue.

- 2026-07-15: /build (ad hoc, not via the /build skill) — Milestone 28 complete (Seed Fixture v3 — Harness Wiring)
  - **Source:** deferred "v3 cascade" from `seed_v3_mu_spec.md` §6, planned this session (fast /planner pass, not written up separately in BUILD-PLAN.md beyond the milestone's own planning section) and built the same session. Paul corrected an initial planning overreach: `weight_mode` is a `config.yaml`/`RunPlan` choice for the actual experiment plan, not something this wiring milestone should decide — scoped it out accordingly.
  - **Built:** `RunPlan.seed_fixture_version` (`schema.py`, same free-string-resolved-at-runtime convention as `probe_set_version`); `runner.py`'s `seed_graph(version=plan.seed_fixture_version)` (was hardcoded `"v2"`); per-plan fixture-path resolution in `run_experiment_harness.py` and `run_experiment_queue.py`/`queue.py` (`SEED_FIXTURE_PATH` constants replaced with a `fixture_path_for_version()` helper in `prompt_gen.py`; `queue.py`'s `_resolve_bank_for_plan()` now takes a `fixtures_dir` and resolves per-plan, since different plans in one queue can request different fixture versions — a missing fixture is now a field-identifying `QueuePreflightError`, not a raw `FileNotFoundError`); `prompt_gen.py`'s version-dispatched two-tier `fingerprint_scan()` (v3: other-cluster entry vocabulary/family-name leak, plus any-cluster answer-token leak including the prompt's own target cluster — a stricter rule v2 never needed); a v3-specific Sonnet worked example nudging the `["names rule <ID>", "states <value token>"]` criteria template from spec §6; `probes/probe_set_v3.yaml` (28 `spoke_retrieval` probes, one per spoke per cluster); `config.yaml`'s `reporting.pass_rank_thresholds.spoke_retrieval: 4`. Committed `seed_fixtures/seed_v3.json.gz` (frozen in a prior session, previously untracked).
  - **Two real authoring bugs caught by actually running the probe battery live, not just unit tests:** (1) first-draft probe templates put the picked entry-vocab words *after* position 5 in the sentence — `retrieve()` only takes the first `retrieval_keyword_count` (5) non-stopword tokens as BM25 keywords, so every `cluster_d` probe returned zero candidates; fixed by front-loading the vocabulary as the first 3 tokens of every prompt (the same "front-load exact vocabulary" lesson the 2026-07-05 pilot bank already recorded once, now recorded a second time on the probe side). (2) `vs_a2` surfaced its own gold directly (`gold_mu_rank=2`) at baseline instead of only via traversal — excluding just each spoke's documented "anchor" word wasn't enough, since content gists are free-form prose and several spokes' real generated content uses more entry-vocab words than their one assigned anchor; fixed by excluding, per spoke, every entry-vocab word that actually appears in that spoke's real frozen content (read from `seed_v3.json.gz` directly), not just its anchor label.
  - **Live baseline check (real Docker backend, real frozen `seed_v3.json.gz`, fresh wipe+reseed, zero training turns, all 28 probes run for real):** 26/28 `gold_mu_rank=None`. The remaining 2 (`vs_b1`→4, `vs_d1`→2) are not defects — full retrieval-trace inspection confirmed both reach gold purely via K2 hub→spoke traversal, never a direct lexical hit. This is the "seven/eight-way tie" property itself: every hub has 7 spoke edges *and* 1 hub↔bridge edge, all identical `Beta(1,1)`/weight 0.5 at seed time, so something always deterministically wins that tie under `weight_mode: mean`. For `cluster_a`/`cluster_c` the winner happens to be the bridge (all 14 of those spoke probes read clean); for `cluster_b`/`cluster_d` the winner happens to be `b_spoke1`/`d_spoke1` specifically. Confirmed via `experiment_report.py` that `_classify_probe_outcome()` uses `rank <= threshold`, so this doesn't corrupt pass/fail classification — but it's a real, useful finding recorded in BUILD-PLAN.md for whoever designs the actual v3 RunPlan: picking `b_spoke1`/`d_spoke1` as the intentionally-trained spoke in those two clusters would start from an already-passing baseline, showing a weaker fail→pass contrast than picking a different spoke.
  - **Deliberately not done:** generating the real v3 prompt bank — that's a paid Sonnet API call Paul initiates himself (`--generate-bank`), not something to trigger unprompted.
  - Tests: 30 new (`test_schema.py` seed_fixture_version defaults/override; `test_runner.py` seed_graph version threading; `test_prompt_gen.py` v3 fingerprint scan two-tier rules, `fixture_path_for_version`, v3 fixture content reading, v3 worked-example dispatch, `_finalize_entry` version threading; `test_queue.py` v3 cached-bank resolution, unknown-fixture-version hard stop). 727 total (697 baseline + 30 new), all passing, no regressions.
  - Branch: `claude-seed-v3-harness-wiring`.
  - Next: Paul reviews the PR; once merged, initiate the v3 prompt-bank Sonnet generation and design the actual first v3 RunPlan (weight_mode, trained spoke per cluster, turn/probe counts, budget_cap — all deliberately left open by this milestone).

- 2026-07-11: /build — Milestone 27 complete (Queue Execution Loop + Resume + Summary — the second half of the Sequential Batch Queue Runner, M26+M27)
  - **Built:** `resolve_run_attempt()` extracted from `runner.py`'s `execute_run()` (behavior-preserving; 52/52 pre-existing runner tests pass unchanged) with a new `bump_on_complete` param — `True` (default) reproduces the exact prior inline behavior (a checkpoint already `"complete"` always advances to a fresh attempt); `False` is the new M27 mode that stops at the first attempt's checkpoint even if it's already complete, letting the queue peek at "did the harness already finish this?" without forking a second copy of the attempt-resolution logic. New `scripts/harness/queue_runner.py`: `run_queue()` calls `execute_run()` once per enabled plan with `on_plan_failure` continue/halt semantics, a post-failure `client.health()` recheck that can override "continue" (a failed plan's root cause might be a genuinely unhealthy backend), crash-resume reconciliation (every plan gets the same `resolve_run_attempt(bump_on_complete=False)` peek regardless of its queue-checkpoint status — a plan whose harness checkpoint already reached `final_status="complete"` is reconciled without ever re-invoking `execute_run()`, closing the exact crash window the planner flagged: the harness finishes but the queue's own status write is lost before the process dies), per-plan/per-condition duration capture from the harness checkpoint's own turn timestamps, `queue_plan_complete`/`queue_plan_failed`/`queue_complete`/`queue_aborted` notifications, and a `queue_summary.md` writer. `queue.py` gains `find_latest_launch_dir()` (lexicographic `{queue_id}_*` scan — `launch_dir_name()`'s timestamp format sorts chronologically) and `QueuePlanCheckpointEntry.report_paths`. `run_experiment_queue.py`'s non-dry-run path now resumes (auto-detected latest launch, or explicit `--resume-dir`, both guarded by an exact `queue_content_hash` match — a mismatch hard-stops rather than guessing which plans already ran) or preflights fresh, then runs to completion with a nonzero exit on any non-`"complete"` final status; new `--no-docker-restart` flag mirrors `run_experiment_harness.py`'s.
  - **Design choice made at build time (BUILD-PLAN.md's own open question):** `queue_plan_complete`/`queue_plan_failed` are distinct notification events sharing one payload shape, rather than a single event with a status field — matches the existing `harness_run_complete`/`harness_run_aborted` convention in `notify.py`.
  - Tests: 20 new — `tests/test_harness/test_queue_runner.py` (11: `on_plan_failure` continue/halt, the health-recheck override, an already-complete plan skipped without calling `execute_run`, an in-flight plan with an incomplete harness checkpoint re-entered, the running-but-harness-already-complete reconciliation, a queue-level unexpected exception marking the plan failed and halting, duration capture, `queue_summary.md` content, checkpoint atomicity); `test_runner.py` (4, `resolve_run_attempt`'s `bump_on_complete` param); `test_queue.py` (5, `find_latest_launch_dir` + `compute_queue_content_hash`). 730 total (710 baseline + 20 new), all passing, no regressions.
  - **Live verification — all 4 items run this session, the first time a session had a live Docker daemon** (`docker ps` worked; `gemma4:e4b` confirmed via Ollama's `/api/tags`) — brought the backend stack up with `docker compose up -d --build backend`:
    - (a) **2-plan micro-queue**, real Docker + real `gemma4:e4b`: both plans completed, `queue_summary.md` showed correct real durations (75.2s/50.5s wall-clock, per-condition breakdowns) and correct series+experiment report paths.
    - (b) **Crash-resume**: a 6-turn plan was `taskkill`'d mid-run at 5/6 turns recorded (confirmed via the harness checkpoint: `final_status=None`); re-invoking the same queue command auto-detected the matching launch dir (`queue_content_hash` matched), resumed exactly at turn index 5 (only the missing turn re-ran), correctly skipped wipe/seed for the already-in-progress condition, and the second plan ran a normal fresh wipe/seed and completed. No duplicate seeding beyond the harness's own.
    - (c) **Failure-continue**: Paul manually stopped Ollama mid-plan-1 and restarted it ~20s later. Plan 1 failed with a genuine `connectivity` error (a real `502` from the backend's own upstream Ollama call on the very first turn); the queue's post-failure health recheck — against the *backend*, which per `backend/main.py`'s `/health` only pings its own DB and never checks Ollama — passed, and the queue correctly continued to plan 2 rather than halting. Plan 2 then independently hit the tail end of Ollama's own restart window (its judge-availability preflight ran before Ollama had finished coming back up) and failed too — a real, honestly-observed timing overlap, not a queue-logic defect. Re-invoking a few minutes later (Ollama confirmed reachable) resumed both plans from their fully-aborted (zero-turn) checkpoints and both completed cleanly — incidentally also exercising the "aborted with 0 turns recorded" resume path, which the crash-resume test in (b) hadn't covered (that one had 5/6 turns already done).
    - (d) Full suite green throughout (730 passing).
  - All throwaway verification plan/queue files (`plans/verify_m27_*.json`, `queues/verify_m27_*.json`) were deleted after use — not part of the deliverable, same convention as prior sessions' scratch verification artifacts. `harness_runs/` output is gitignored.
  - Branch: `claude-queue-execution`. PR: https://github.com/PaulArthurMiller/local_corvus/pull/47
  - Next: Paul reviews the PR. Once merged, the Sequential Batch Queue Runner (M26+M27) is ready to drive the 10-15-run screening series unattended, and later the Karpathy loop's execution substrate.

- 2026-07-11: /build — Milestone 26 CODE COMPLETE (Sequential Batch Queue Runner: Queue Schema + Preflight + Dry-Run), in PR review
  - **Built:** `scripts/harness/queue.py` (QueueFile/QueuePlanEntry schema, QueueManifest/QueueCheckpointState + atomic `save_*`/`load_*` writers mirroring `checkpoint.py`'s temp-file+`os.replace` pattern, and `run_preflight()` implementing all 7 preflight steps from BUILD-PLAN.md), `scripts/run_experiment_queue.py` CLI (`--queue`, `--dry-run`, `--backend-url`, `--judge-host`, `--resume-dir` parsed-but-inert until M27), `queues/example_queue.json` (references the two already-committed plans/banks as living schema docs). Branch `milestone-26-queue-preflight`.
  - **Small public helper added per the plan's own risk note:** `prompt_gen.py`'s `_generation_params()` was private; added public `generation_params_for_plan()` (the private name now just calls it) so queue.py's cached-bank-hash resolution doesn't reach into a private function. Zero behavior change, existing `test_prompt_gen.py` untouched.
  - **Preflight ordering nuance:** duplicate-config-hash detection (step 3) runs on each plan's hash *before* bank-path stamping (step 4), matching the diagram's literal step order; the final manifest/checkpoint entries recompute config_hash/expected_run_id *after* stamping, since that's what `execute_run()` will actually see in M27. Also fixed an ordering bug caught while writing tests: manifest/checkpoint entries were being built "all disabled, then all enabled" instead of the queue file's original order — now assembled via a plan_path-keyed lookup so the CLI's printed execution order matches what a human wrote in the file.
  - **Budget-check testability:** local Ollama judge calls cost $0, so the aggregate-budget hard stop (step 6) would be permanently unreachable at the default rate — added an injectable `judge_cost_per_call` parameter to `run_preflight()`, same pattern as `execute_run()`'s own parameter, so a test (or a future hosted judge) can actually trigger it.
  - **Uncommitted planning docs found at session start:** BUILD-PLAN.md/PROGRESS.md had uncommitted M26/27 planner content from the prior `/planner` session (never committed). Committed those first as a docs-only commit before writing any code, per the branch-per-milestone convention — left the unrelated untracked `seed_fixtures/seed_v3.json.gz` (M25's freeze artifact) untouched as out-of-scope.
  - **Tests:** 30 new (`tests/test_harness/test_queue.py`) covering queue-file validation, `resolve_plan_path` relative/absolute, all 7 preflight steps (missing plan, invalid plan, duplicate hash, all 5 bank-resolution branches, health failure, judge-unavailable, budget breach + uncapped, disabled-plan-never-validated), manifest/checkpoint atomic round-trips, and `launch_dir_name`. Full suite: 710 passed (680 baseline + 30 new), no regressions.
  - **Live verification — partial.** All 3 required preflight hard-stops (missing plan file, fresh-bank-needed, duplicate config hash) were run live via the real CLI against real scratch files and recorded — each exits 1 with a plan-identifying message, before any backend call (confirmed for the missing-plan case via `client.health_calls == 0`). The "3-plan `--dry-run` with a passing health check" verification item could **not** be run live this session: no Docker daemon (`docker ps` failed — dockerDesktopLinuxEngine pipe not found) and no backend venv (`backend/.venv` doesn't exist; README's host-uvicorn fallback wasn't set up). Standing up a full backend stack felt like a heavier, riskier side-quest than the milestone needed given the identical success path is already covered by `test_preflight_success_manifest_and_checkpoint_shape` (fake client/judge) — flagged for Paul rather than silently skipped or force-built. Ollama itself *is* reachable natively (`gemma4:e4b` confirmed via `/api/tags`) — only the backend leg is missing.
  - Branch: `milestone-26-queue-preflight`. PR: https://github.com/PaulArthurMiller/local_corvus/pull/46
  - Next: Paul reviews the PR and runs a live `--dry-run` against a real backend before merge; then Milestone 27 (execution loop + resume + summary).

- 2026-07-11: /planner — Sequential Batch Queue Runner decomposed into Milestones 26–27 in BUILD-PLAN.md (source: Paul's planner document; design decisions there were settled — this session decided the four open questions and recorded rationale)
  - **What it is:** a "playlist player" — an ordered list of run-plan files executed one at a time through the existing `execute_run()`, unattended overnight, for the 10–15-run screening series and later as the Karpathy loop's execution substrate. No intelligence: never reads results, never edits plans. M26 = queue JSON schema + full preflight (everything validated before the first turn of the first plan) + `--dry-run` CLI; M27 = execution loop + crash-resume + failure semantics + `queue_summary.md`.
  - **Open questions decided (rationale in BUILD-PLAN):** (1) queue file is **JSON** — every execution artifact in the repo is JSON (plans/banks/manifests/checkpoints), YAML is reserved for human-edited config, and the Karpathy loop will machine-write queue files; (2) resume = **auto-detect latest `{queue_id}_*` dir guarded by a queue-content hash**, `--resume-dir` as explicit override, hash mismatch hard-stops rather than guessing; (3) disk space = projected-archive-count log line **plus** a `shutil.disk_usage` free-space WARN (stdlib one-liner, but archive-size prediction is guesswork so WARN not hard-stop); (4) **in-process** `execute_run()` confirmed — it returns a structured `RunOutcome`, never `sys.exit`s, and contains its own hard-stop unwinding, so subprocess would only add stdout parsing. Also: queue settings live in the queue file only, NOT `config.yaml` — operational choices, not experimental knobs.
  - **Mechanism correction found by reading `runner.py` (flagged, not silently patched):** the source doc predicted a byte-identical duplicate plan would "resume-as-complete"; actually a completed checkpoint **bumps to `_attempt2` and re-runs** (runner.py's attempt-resolution loop). Still wrong for replicates (same-seed pseudo-replicate under a suffixed identity), so the settled duplicate-config-hash preflight hard stop stands, with the "replicates need distinct random_seed values" message.
  - **Two resume subtleties designed in:** (a) a plan checkpointed `running` whose *harness* checkpoint says `complete` (crash in the window between harness finish and queue checkpoint write) must reconcile to `complete` without calling `execute_run()` — otherwise the attempt-bump silently re-runs ~2 hours; (b) locating the current attempt without forking resume logic = extract `runner.py`'s attempt-probe loop into a shared helper (M27 build item 1, behavior-preserving refactor).
  - **Bank preflight (the Sonnet gate must never fire mid-queue):** per plan, explicit `prompt_bank_path` must exist + pass `load_bank()`; else `subjects=="auto"` + `estimate_bank_cost_for_plan()` returning `None` resolves the cached bank by hash and stamps it onto the in-memory plan; anything needing fresh generation (or a plan with neither bank nor auto — placeholder turns overnight are wasted GPU-hours) hard-stops with a "run --generate-bank first" message. The queue makes zero Anthropic calls.
  - **Per-condition durations** derived post-hoc from the harness checkpoint's per-turn timestamps (min/max per condition) — zero `runner.py` changes, layer boundary respected.
  - No code written this session — planning only, per the skill's "never start coding" rule.

- 2026-07-11: /build — Milestone 25 CODE COMPLETE (Seed Fixture v3: Counterfactual Content Generation + Verification), in PR review awaiting Paul's content review + human freeze
  - **Built:** `scripts/generate_seed_v3.py`, `scripts/verify_seed_v3.py`, `tests/test_scripts/test_generate_seed_v3.py` (+ `tests/test_scripts/__init__.py`). Branch `milestone-25-seed-fixture-v3`.
  - **Generator design.** Copied v2's Ollama-generate-validate-retry machinery, replaced the single `CLUSTER_*_FP` lists with two-tier constants (`FAMILY_NAMES`/`ENTRY_VOCAB`/`ANSWER_TOKENS`/`ANCHOR_WORDS`/`RULE_IDS`) all transcribed verbatim from `seed_v3_mu_spec.md` §3–4. Required-terms are a **separate verbatim `REQUIRED` dict** (not derived from the token dicts) so a derivation bug can't silently diverge from the approved spec — hub rule-ID ellipses ("K-1…K-7") expanded. Forbidden terms ARE derived, per MU category: hub → own-cluster answer tokens; spoke → sibling answer tokens; bridge → both adjacent clusters' entry vocab + answer tokens + family names — all funnelled through v2's existing whole-word `_check_forbidden_terms`, so hub/sibling exclusion is just correctly-populated forbidden sets. Import-time self-checks (`_validate_answer_token_uniqueness`, `_validate_answer_token_formats`) run on the real table before any Ollama call. 64 reciprocal edges (`_build_edges` emits each v2 edge forward + `r`-suffixed reverse, weight 0.5). `fixture_version="v3"` → auto `series_id="seed:v3"` at registration (spec §7.3a — zero backend change).
  - **Manifest.** `generate_mu()` returns `(mu, failure_or_None)`; any MU exhausting `MAX_RETRIES` with an unresolved violation becomes a failure record (`mu_id`/`mu_key`/`cluster_id`/`attempts`/`missing_required`/`forbidden_leaked`/`last_attempted_text`), and `main()` always writes `seed_v3_generation_manifest.json` even with `"failures": []` (present-not-absent, matching the project's empty-result convention). Best-effort content is still kept in the draft. Draft + manifest are gitignored (patterns already present).
  - **Spec-vs-validation tension found and handled (minor deviation, documented in PR):** several §4 content gists name a *sibling* spoke's answer token because the rules are semantically interrelated — `a_spoke7`'s gist says "corrupts spine order" (`a_spoke1`'s token); `c_spoke4`'s says "three amber breaches → one crimson breach" (`c_spoke1`'s tokens); `c_spoke5`/`d_spoke4` reference `tally stamp`/`origin docket`. Sibling-exclusion forbids exactly that. Since the spec says prose is "to taste" and those sibling tokens aren't in the MUs' required lists, the generation prompts paraphrase the meaning without naming the sibling token. No token, rule ID, or required-term changed. Bridges likewise paraphrased to avoid entry-vocab words ("shape").
  - **Verifier.** 9 checks (v2 had 5): MU count, reciprocal edge wiring (64, every forward has a same-weight reverse; hub touches 16, each spoke 2), hub↔bridge both directions, bridge lexical distinctness (full two-tier), NEW hub-token exclusion, NEW sibling-token exclusion, NEW content-level global leakage scan (every MU vs. every *other* MU's tokens project-wide — the strongest check, validates frozen text so it also catches a bad hand-repair), NEW token-format check, and an extended probe-sanity check. Standalone copy of the vocab constants (verify-what's-frozen). `--draft`/`--frozen` flag defaults to `--draft` so the repair loop runs on the uncompressed draft before the (human) freeze — the deliberate workflow reversal from v2.
  - **Two-hit-bar check bug caught during the live run and fixed:** first draft queried check 9 sub-check C with *all* of a cluster's entry vocab — but every spoke's anchor word IS an entry-vocab word, so it handed each spoke its anchor hit and WARNed on nearly every spoke (noise). Placement rule 6's actual scenario is "all entry vocab but no spoke's anchor word" = `ENTRY_VOCAB` minus the cluster's anchor set. Fixed; now WARNs only on genuine cases.
  - **Live run (real Ollama `gemma4:e4b`, no Docker — same host-uvicorn pattern as prior sessions):** generated `seed_v3_draft.json` (34 MUs, 64 edges) with a clean manifest (`failures: []`) on the **first pass** — despite the spec predicting a higher retry rate, no MU needed hand-repair (the two-tier prompts plus the MUST-include instruction were enough). `verify_seed_v3.py --draft` → all 9 checks PASS; only remaining WARN is the two-hit bar on `a_spoke3`/`a_spoke5` (both say "structure" on top of the "output" that "Kestrel Output Conventions" injects into every cluster_a MU) — a content nit for Paul's review, not a defect.
  - **Sabotage proof (build-step 5 — the milestone's proof the manifest fires end to end):** ran a throwaway that validated one MU (`c_spoke3`) against an impossible required term. **First attempt failed instructively** — putting the impossible token in the MUST-include prompt line made Gemma copy the nonsense string verbatim, "passing" the check. Corrected by hiding the impossible token from the prompt (normal prompt, augmented validation): 3 real Ollama attempts exhausted, manifest record matched the BUILD-PLAN schema exactly. Lesson recorded: a required-term you want to force-fail must never appear in the prompt. (Throwaway lived in the scratchpad; real draft/manifest untouched.)
  - Tests: 680 total (649 baseline + 31 new), all passing, no regressions. The Ollama-dependent generation path stays manually validated (no mocked-Ollama pytest suite), matching v2's convention; the pure-Python validation surface is fully covered.
  - **Not done (Paul's steps, per the milestone's own structure):** content review of the draft prose, freeze `seed_v3_draft.json` → `seed_v3.json.gz`, and `verify_seed_v3.py --frozen`. The frozen `.gz` is the only committed fixture artifact and lands after the human freeze.

- 2026-07-11: /planner — Milestone 25 (Seed Fixture v3: Counterfactual Content Generation + Verification) added to BUILD-PLAN.md
  - **Source:** Paul provided a pre-reviewed/approved spec (`seed_v3_mu_spec.md` — invented rule-family content with two-tier vocabulary: entry vocab saturated in hubs, unguessable answer tokens confined to one spoke each; 64 reciprocal edges replacing v2's 32 one-directional edges; per §2, forward-only K2 traversal means a v2 hub-anchored query structurally cannot reach a spoke, which the reciprocal-edge layout fixes). Saved verbatim to the repo root as `seed_v3_mu_spec.md` (previously referenced but not yet committed — the M22–M24 Non-Goals section had flagged it as "a separate artifact" without it existing as a file).
  - **User's explicit ask, now part of the milestone's Done-when criteria:** generator failures (a MU that exhausts `MAX_RETRIES` with an unresolved missing-required/leaked-forbidden violation) must be written to a human-readable manifest (`seed_fixtures/seed_v3_generation_manifest.json`) with `mu_id`, missing required terms, leaked forbidden terms, and the last attempted text — so failed MUs can be identified and hand-authored rather than silently kept with a warning buried in a log (v2's behavior).
  - **Design decisions made while writing the milestone (read the actual codebase, not just the spec, before committing to these):**
    1. `POST /admin/seed-graph`'s fixture path is already generic (`seed_fixtures/seed_{version}.json.gz`, confirmed in `backend/api/admin.py` + `backend/analytics/seed_fixture.py`) — registering a v3 fixture needs zero backend changes. `series_id="seed:{fixture_version}"` is also derived automatically from the fixture's own `fixture_version` field — setting `"fixture_version": "v3"` in the draft JSON is sufficient for M23's `_source_kind_label()` seed-detection to keep working; no generator special-casing needed. Documented in spec §7.3a so this isn't re-investigated later.
    2. New required validation categories beyond v2 (which only forbade terms on bridge MUs): hub token exclusion (a hub must name rule IDs but contain zero value tokens from its own cluster) and spoke sibling-token exclusion (a spoke must not leak another spoke's token within the same cluster) — both are new per-MU-category forbidden-term logic, plus a global answer-token-uniqueness self-check at generator import time and a content-level cross-MU leakage scan in the verifier (the strongest of the 9 checks, since it validates frozen text rather than generation-time intent — also catches a bad hand-authored manifest repair).
    3. **Workflow order flipped from v2:** v2 froze to `.gz` before running `verify_seed_v2.py`. v3's `verify_seed_v3.py` gets a `--draft`/`--frozen` flag and defaults to `--draft`, so Paul can iterate (repair manifest failures, rerun verify) entirely on the uncompressed draft before ever gzipping — freeze becomes the last step, not the second-to-last. `--frozen` still exists as a final post-freeze sanity check.
    4. Added a required build step: run the real generator against Ollama, and if it doesn't naturally produce a manifest failure on the first try, deliberately sabotage one MU's required-terms list in a throwaway copy to prove the manifest mechanism fires end-to-end — the milestone's stated proof that "failures are explicitly documented," not just a unit-tested code path against synthetic data.
    5. v2's generator/verifier have zero pytest coverage (confirmed via grep — no test file references either script). M25 adds `tests/test_scripts/test_generate_seed_v3.py` for the pure-Python validation logic (required/forbidden checks, hub/sibling exclusion, uniqueness assertion, manifest serialization, token format check) since none of it depends on Ollama and the validation surface is meaningfully larger than v2's — the Ollama-dependent generation path itself stays manually-validated, matching the existing project convention.
  - **Explicitly out of scope for M25** (per spec §6/§7 and this project's existing Non-Goals convention — deferred to future milestones): `probe_set_v3.yaml`, prompt bank v3 regeneration, `prompt_gen.py`'s `SEED_FIXTURE_VERSION`/import switch to v3, the `weight_mode` (`sample` vs `mean`) decision for the v3 run, and wiring `prompt_gen.py` to consume the new two-tier fingerprint export the generator produces.
  - No code written this session — planning only, per the skill's "never start coding" rule.

- 2026-07-11: /smoketest — Milestone 24 complete (Verifications + Reports/Admin Audit + Smoketest — the final milestone in the M22–M24 Turn-Wiring Fixes decomposition)
  - **All 4 verification items confirmed clean by inspection, no fixes needed:**
    1. SimpleRL unreachable from the harness/experiment path — `POST /feedback/verdict` routes exclusively through `edge_update.process_verdict_for_learning()`, which internally refuses to write anything unless `condition_label == "learned"` regardless of the caller's flag; `scripts/harness/client.py` has no code path calling `/feedback` (the chat-UI thumbs endpoint) at all.
    2. Probe turns create no MUs/edges — `admin.py`'s `run_probe()` docstring states the invariant directly ("MUST NOT call run_interaction()"), and a grep for `run_interaction` in `admin.py` confirms only the docstring mentions it — the function body calls `retrieve()` only.
    3. Probe retrieval passes `condition=RUN_CONDITION` — confirmed at the same call site (`retrieve(..., condition=RUN_CONDITION, rng=EDGE_RNG)`), already true since before this milestone.
    4. `edges.direction`/`directional_edge_weight_multiplier` are inert — grepped every `create_edge()` call site across `orchestrator.py`, `extractor.py`, and `seed_fixture.py`, plus both seed fixture data files (`seed_v2.json.gz`, `seed_v2_draft.json`) directly: every edge everywhere is `direction="forward"`, nothing ever writes `"bidirectional"`. `retrieval.py`'s K2 traversal applies `directional_edge_weight_multiplier` uniformly to every traversed edge with no check against the `direction` column — a flat multiplier on all K2 scores, not the forward-vs-bidirectional bias the old comments implied. Documented this in `store.py`'s `edges` table schema comment and `config.yaml`'s hyperparameter comment (1 commit, doc-only, no behavior change).
  - **Report audit (items 5–6) confirmed clean, no changes needed:** `series_report.py`'s `_section_53_retrieval_rate_classification` classifies purely by comparing each `run["activated_mu_ids"]` entry's `series_id` against the current series and the `"seed:"` prefix — no `creation_trigger` filter anywhere, so it already picks up `co_activation`-sourced retrievals for free. `_section_54_cross_series_edges` → `store.get_cross_series_edges()` is a raw SQL query with no `creation_trigger` filter either — it already picks up the new `output->retrieved` edges. Grepped `admin/src` for `creation_trigger` — no matches, nothing there enumerates trigger values today.
  - **Required smoketest (item 7) — real backend, real Ollama `gemma4:e4b`, no Docker (daemon wasn't running this session, same deferral pattern as prior sessions — uvicorn run directly against a scratch `MEMORY_PATH`):** seeded the graph with `seed_v2` (34 MUs, 32 edges, all `series_id="seed:v2"`), then ran 8 real `/chat` turns in one series using seed_v2's structured-data-output vocabulary (delimiters, field ordering, encoding, quoting, whitespace, header rows).
    - **Confirmed live the exact cross-turn routing scenario M22/M23 targeted:** turn 3's `activated_mu_ids` included turn 1's *output* MU, retrieved via BM25 on synthesized response content — the "sanity narrative" from the M22-24 planning doc, now observed with real data.
    - Generated a series report (`POST /admin/reports/generate-series`) over the 8-turn series: §5.3 showed 31 total retrieved (16 intra-series, 15 seed-fixture, 0 cross-series, **0 unknown** — no unclassified-edge warnings), 51.6% intra-series rate. §5.4 showed 15 cross-series edges, every one an `output->retrieved` `co_activation` edge from a turn's output MU landing in `seed:v2` — exactly the "expected, not an anomaly" pattern BUILD-PLAN.md predicted.
    - Direct SQLite inspection of the edge population confirmed the predicted shape: every output MU has out-degree 3–4 (total degree N+1 — no longer the pre-M22 dead end), edge counts by `creation_trigger` were 31 `co_activation` (exactly matching the sum of `activated_mu_ids` across all 8 turns), 32 `seed`, and 17 `prompt_construction` (2/turn × 8 successful turns + 1 from an interrupted retry — down from the pre-M22 N+2/turn).
    - §5.1 (0 failed turns, 0 ghost-MU incidents), §5.6 (probe integrity PASS), §5.7 (0 Mode-3 triggers this run — condensed context never exceeded the 1500-token trigger in an 8-turn series, expected; M23's Mode-3 edge behavior already has full unit coverage) all reported clean.
    - **One transient issue, not filed as a bug:** a single turn's Ollama call took ~4.5 minutes before erroring with a connection error (well past the configured 60s timeout — the delay itself is unexplained, possibly a GPU/driver hiccup unrelated to this codebase); immediate retry of the identical request succeeded in 29s, and the backend recovered cleanly — the only artifact left behind was the expected orphaned prompt/input MU pair from the interrupted attempt (no output MU, no analytics row, since `capture_run()` only fires after a successful Ollama response). The series report generation was unaffected — it correctly used only the 8 completed runs' analytics rows. Not reproducible on retry, not related to M22/M23 code, so not chased further.
  - Tests: 649 total (unchanged from M23 baseline — no new unit tests, per the milestone's own scope: none were expected unless the smoketest surfaced a real bug, and it didn't), all passing, no regressions.
  - Branch: `fix/smoketest-2026-07-11`

- 2026-07-10: /build — Milestone 23 complete (Extractor: Summary Edges + Source-Kind Labels)
  - **Summary → source edges.** `backend/memory/extractor.py`: `extract()` and `_summarize()` both gain a `run_id` parameter (threaded from `orchestrator.py`'s existing `run_id`, already computed before the `extract()` call site). After a Mode-3 summary MU is created, `_summarize()` now creates one `summary->mu` edge per MU in the pre-summarization `mus` list, `creation_trigger="co_activation"`, reading priors directly from `extractor.py`'s own module-level `_CONFIG.get("edge_update", {})` (`prior_alpha`/`prior_beta`, default 1.0/1.0) rather than importing `validate_edge_update_config` — `orchestrator.py` already hard-fails startup on invalid `edge_update` config, so a second validation pass here would be redundant. Left the pre-existing `series_id`/`experiment_id` gap on the summary MU's `create_mu()` call untouched, as flagged in BUILD-PLAN.md — out of this milestone's scope.
  - **Source-kind content labels.** `_mode1_extract()`/`_mode2_extract()` now prefix each condensed chunk with a bracketed label from a new `_source_kind_label(mu)` helper, checking `series_id` (prefix `"seed:"`) before `source_type` — seed MUs are stamped `source_type="agent_output"` identically to a real turn's response, so `series_id` is the only field that actually distinguishes them (same mechanism `series_report.py` §5.3 already uses).
  - **Three deviations from BUILD-PLAN.md's draft label table, all directed by the user before implementation (not silently chosen):**
    1. **Failed-turn MUs are excluded from candidacy, not labeled.** The draft table's row folding `agent_output_failed_token_limit`/`agent_output_failed_empty_output` into `[memory — prior response]` is gone entirely — a failed turn has no meaningful content to inject as memory context. Instead, `hyperparameters.retrieval_exclude_source_types`'s default (both in `config.yaml` and its code-level fallback in `backend/memory/retrieval.py`) extends from `["prompt"]` to also include both failed-output source types, so they never reach the extractor at all.
    2. **`[memory — other]` renamed/narrowed.** `tool_call`/`tool_result` now get an explicit `[memory — tool record]` label; `[memory — other]` is reserved for genuinely unrecognized `source_type` values (there are none in the current schema — it's a forward-compatibility fallback).
    3. **The `series_id` `"seed:"` prefix is now a stated requirement for the future seed v3 generator**, not just a v2 fixture convention — documented in BUILD-PLAN.md's Non-Goals (M22–M24) section: since `_source_kind_label()` keys its seed-detection off this prefix with no validation, a v3 generator that stops stamping it would silently mislabel seed content as `[memory — prior response]` with no error raised.
  - Tests: `tests/test_memory/test_extractor.py` — `TestSourceKindLabel` (9: seed-first precedence, each source_type branch, missing/None `series_id` doesn't crash), `TestModeLabelPrefixes` (7: Mode 1/2 label prefixing across source kinds, multi-MU independent labels), `TestMilestone23SummaryEdges` (3: one edge per source MU with correct trigger/priors/run_id, zero edges when Mode 3 doesn't fire, missing `run_id` doesn't crash edge creation). Refactored `TestMode3Summarization._build_large_mus` into a module-level `_build_large_mus()` (now assigns unique `mu_id`s, needed to assert edge target sets) and updated the one exact-string-equality Mode 2 test to expect its new label prefix. `tests/test_memory/test_retrieval.py` — `TestFailedOutputExclusionFilter` (3: both failed-output types excluded by default, empty exclusion list restores candidacy for them too).
  - Tests: 649 total (626 baseline + 23 new), all passing, no regressions.
  - Branch: `milestone-23-extractor-summary-edges`

- 2026-07-10: /build — Milestone 22 complete (Output-MU Routing Edges + Retrieval Candidacy Exclusion)
  - `backend/core/orchestrator.py`: deleted the Step 5 `prompt->retrieved-MU` edge fan-out loop; the prompt MU is now a routing dead end (only `prompt->input`). Added a Step 9 `output->retrieved-MU` fan-out (one `create_edge()` per `activated_mu_ids` entry, `creation_trigger="co_activation"`) right after the `input->output` edge — `activated_mu_ids` already includes the Mode-3 summary MU when it fires, so `output->summary` arrives with no extra code. `activated_edge_ids` (the K1→K2 traversal-path field M20's credit assignment reads) is untouched — the new edges are co_activation, not traversal-path. Updated the Step 5 comment block and `_prompt_mu_content()`'s docstring, both of which claimed context linkage "lives in the prompt MU's edges" — it no longer does.
  - `config.yaml`: new `hyperparameters.retrieval_exclude_source_types: ["prompt"]` (matches the existing flat-`hyperparameters:`-with-`retrieval_`-prefix convention, not a new nested `retrieval:` block as the source spec assumed).
  - `backend/memory/retrieval.py`: `retrieve()` filters `all_mus` (not `all_edges`) by `source_type` immediately after `get_all_mus()` — excluded MUs never enter BM25 candidacy or K2 traversal targets (`mu_by_id` is built from the same filtered list), but their edges still contribute to a retrievable neighbor's `edge_weight_sum` centrality score, since `all_edges` stays unfiltered. Logs the excluded count.
  - **Bug found and fixed, discovered while writing M22's own required test** ("a turn with a failed-output source type produces the same edge shape"): `backend/gate/um.py`'s `SourceType` Literal never included `agent_output_failed_token_limit`/`agent_output_failed_empty_output` — the two failure-mode source types Milestone 11 introduced in `orchestrator.py`. Every real token-limit or empty-output failure has therefore crashed `create_um()` with a pydantic `ValidationError` since M11; no prior test drove an actual failure through the full `run_interaction()` pipeline (M11's own tests only exercised `_run_tool_loop()` in isolation), so it was never caught. Fixed by adding both literals to `SourceType`.
  - Tests: `tests/test_core/test_orchestrator.py` — new `TestMilestone22OutputRoutingEdges`, 5 tests running the full `run_interaction()` pipeline against a real temp-file SQLite `GraphStore` (retrieve()/extract() mocked to a known shape, Ollama mocked via httpx, same pattern as the M20 §8 trace test): N+2 edges with no summary, N+3 with a Mode-3 summary, prompt MU has exactly one outgoing edge, failed-output turns get the same edge shape, edge values (`weight`/`alpha`/`beta`) are identical across `learned`/`static_weight`/`random_weight`. `tests/test_memory/test_retrieval.py` — new `TestSourceTypeExclusionFilter`, 4 tests: prompt MUs excluded from `candidate_set_before_ranking`/`entry_point_mu_ids`/the retrieved set by default; a prompt-only-keyword query resolves to zero candidates; `retrieval_exclude_source_types: []` restores exact pre-M22 behavior; an excluded MU's edges still count toward a neighbor's `edge_weight_sum`.
  - Tests: 626 total (617 baseline + 9 new), all passing, no regressions.
  - **Manual live verification** (Docker wasn't running this session — backend run directly via uvicorn against a scratch `MEMORY_PATH`, real Ollama `gemma4:e4b` native on the host, same deferral pattern as prior sessions): sent 6 real `/chat` turns in one series. First 2-turn check showed the new `output->retrieved` edge existed and zero `prompt->retrieved` edges existed anywhere, but the specific MU retrieved on turn 2 was the turn's own just-created input MU rather than an earlier turn's — traced to `rank_bm25`'s classic Robertson–Sparck-Jones IDF formula (no `+1` smoothing) going *negative* for terms present in a majority of a tiny (3-document) corpus, a pre-existing library property unrelated to M22, not a bug. After 3 filler turns grew the corpus, a 5th turn's output MU gained a `co_activation` edge directly to turn 1's *output* MU — the exact cross-turn routing scenario M22's sanity narrative describes — confirmed via `/admin/graph`. Every prompt MU across all 6 turns had exactly one outgoing edge (`prompt->input`), zero `prompt->retrieved` edges anywhere in the run.
  - Branch: `milestone-22-output-mu-routing`

- 2026-07-09: /planner — Turn-Wiring Fixes decomposed into Milestones 22–24 in BUILD-PLAN.md (source: a planning doc on organic MU/edge topology, prerequisite for the full experimental run, separate from the not-yet-planned `seed_v3_mu_spec.md`)
  - **The problem:** the output MU is a routing dead-end — its only edge is incoming (`input→output`) — so a later turn that enters retrieval at a past response (BM25 match on synthesized content) can't traverse from there to the seed rules that response was built on. This blocks the run's secondary claim (the system routes through constructed, not just seed, knowledge).
  - M22 (Output-MU Routing Edges + Retrieval Candidacy Exclusion): relocates context-linkage edges from `prompt→retrieved` (prompt is now a routing dead-end instead, by design — it's an audit record, not a traversal node) to `output→retrieved` (+ `output→summary` when Mode 3 fires), `creation_trigger="co_activation"`; adds a `hyperparameters.retrieval_exclude_source_types: ["prompt"]` config knob so prompt MUs (near-duplicates of input MUs) stop wasting K1 slots now that they're no longer where context linkage lives.
  - M23 (Extractor: Summary Edges + Source-Kind Labels): fixes Mode-3 summary MUs being edge-orphans (`summary→source` edges); adds bracketed source-kind labels to condensed context chunks so mixed seed/interaction content stays legible to Gemma.
  - M24 (Verifications + Reports/Admin Audit + Smoketest): confirms 4 invariants (SimpleRL unreachable from the harness path, probes stay read-only, probe retrieval passes `condition=RUN_CONDITION` — already true, evidently fixed since the source doc was drafted, `edges.direction`/`directional_edge_weight_multiplier` are inert and now documented as such) and audits `series_report.py` §5.3/§5.4 against the new edge shape; required smoketest series report is the milestone's done-criterion.
  - **Two findings during planning that changed the spec, not just implementation detail:**
    1. Seed MUs are **not** distinguishable from real interaction MUs by `source_type` — both `seed_v2.json.gz` and `seed_v2_draft.json` stamp every seed MU `source_type: "agent_output"`, identical to a genuine turn's response. The spec's proposed label scheme for M23 (`source_type`-keyed) can't work as written; the only field that actually distinguishes seed-origin content is `series_id` (prefix `"seed:"`) — the same mechanism `series_report.py` §5.3 already uses. M23's label table now checks `series_id` first, `source_type` second. Flagged as a real spec deviation needing confirmation before implementation, not silently patched.
    2. `config.yaml` has no nested `retrieval:` block — hyperparameters are a flat `hyperparameters:` dict using a `retrieval_` prefix convention (`retrieval_keyword_pool_size`, `retrieval_level1_top_k`, ...). M22's new exclusion knob is named `hyperparameters.retrieval_exclude_source_types` to match, not a new nested section.
  - Also noted while reading code (no fix needed, confirmed by inspection): `admin.py`'s `POST /admin/run-probe` already passes `condition=RUN_CONDITION, rng=EDGE_RNG` to `retrieve()` — the source doc's T4.3 concern (probe retrieval silently defaulting to `static_weight`) is already resolved.
  - No code written this session — planning only, per the skill's "never start coding" rule.

- 2026-07-06: /smoketest — re-ran `plans/pilot_125_2026-07-05.json` on the real Ollama/gemma4:e4b box (the "next step" from M21). **First pilot with real model + real probe batteries: 375/375 turns, 0 failed, exit 0, ~75 min wall clock** (one laptop-sleep pause mid-run; per-turn checkpointing resumed cleanly, no turns lost).
  - **Harness + M21 wiring verified end to end on real data:** per-condition backend restarts stamped correct `EXPERIMENT_ID`/`RUN_CONDITION`/`CONDITION_LABEL`; 150 `probe_results` rows (3 conditions × 5 sessions × 10-probe v2 battery), `measurement_session_index` contiguous 1–5 per condition, all stamped with `experiment_id`; §6.3 all 3 integrity checks PASS; §6.2 all 4 control sanity checks PASS; M20 learning fired only in the learned condition (93 edge-update events, 88 up / 5 down, 0 orphans; verified 0 events attributable to control experiments); `random_weight` re-randomized once per series (32/118/205… edges). Judge verdict mix on real gemma4: heavily "up" (~71/5/rest-none in learned) vs the stub run's balanced mix.
  - **Bug 1 (observed in the run): experiment report §5 recomputed per-series sections from the live graph at experiment close** — but each later condition's `wipe_graph()` had deleted the earlier conditions' MUs, so §5.3 classified every non-seed MU "unknown" and reported 0.0% intra-series retrieval for all learned series while the series-close reports correctly said ~34%. Fix: `generate_experiment_report()` now prefers the series report JSON persisted at series close (authoritative snapshot) and only recomputes for series without a readable stored report. Rule: **any §5 section that reads `memory_units` is only trustworthy at series close — post-hoc regeneration must go through the stored reports.**
  - **Bug 2 (latent, found tracing bug 1): §6.2 comparative deltas queried probe results by `condition_label` only** — the third door into the same attempt/dry-run contamination class M19/M21 fixed twice already (§6.1 and §6.3 were fixed in M21; §6.2 was missed). Fix: new `_collect_probe_results_for_experiment()` scopes by `experiment_id` first, `condition_label` fallback for pre-M21 legacy rows.
  - **Bug 3 (report noise): §6.1 iterated all 15 synced probes (v1+v2)** when the experiment ran only the 10-probe v2 battery — 5 `insufficient_data` rows, probe count inflated, 6 "bridge probes" (2 with empty trajectories) instead of the 4 measured. Fix: §6.1 restricts to probes with ≥1 result for the experiment; keeps the full list when nothing has results (empty-section contract preserved).
  - Experiment report regenerated via `POST /admin/reports/generate-experiment` after the fixes: §5.3 mean intra-series rate 31.8% (0 unknowns), §6.1 probe_count 10 / 4 bridge probes, §6.2 deltas experiment-scoped. The 0.0 pass-rate/bridge-rank deltas vs both controls are genuine (identical final gold ranks across conditions), not artifacts — see the science note under Next step.
  - Tests: 617 total (611 baseline + 6 new: 2 stored-series-report, 2 §6.2 scoping, 2 §6.1 battery filtering), all passing, no regressions.
  - Branch: fix/smoketest-2026-07-06
  - PR: https://github.com/PaulArthurMiller/local_corvus/pull/41

- 2026-07-05: /build — Milestone 21 complete (Harness Probe Battery Wiring + prompt-MU nesting fix; the two gaps surfaced by the PR #39 stub pilot)
  - **Gap 1 (the milestone): the harness never called `POST /admin/run-probe`.** Probe turns were Phase-0 `/chat` placeholders — they mutated the graph, weren't stamped `is_probe_turn`, and recorded no `probe_results` rows, so §6.1 `probe_pass_rates` were all null and state-transition/bridge trajectories were empty.
  - Harness: `client.py` gains `run_probe()` (same connectivity/turn-error exception split as `chat()`, validates `result_id`); `turn_resolution.py` gains `assign_probe_schedule(sequence, probe_ids)` — purely positional, no RNG: within each series probe turns walk the battery in order, each completed walk is one measurement session, `measurement_session_index` is 1-based and globally contiguous (the §6.3 no-gaps requirement); stamped into the resolved sequence *before* the manifest is written so a resumed run replays the identical schedule (same guarantee `assign_prompt_ids` gives bulk turns). `schema.py` gains `RunPlan.probe_set_version` (default "v2"). `runner.py`: probe turns dispatch to `client.run_probe()` — never `/chat`, never judged; new probe-battery preflight hard-stops (unconditional, before any wipe) when a plan has probe turns but no battery loaded — silently running an experiment without its measurement instrument is never acceptable; `load_probe_prompts` renamed `load_probe_battery`. `checkpoint.py`: `TurnCheckpoint.probe_id`/`measurement_session_index`, `detail` = probe `result_id`.
  - **Contamination guard discovered during design (would have re-introduced the twice-fixed M19 bug class):** the report layer links `probe_results` to an experiment via `condition_label` — but `condition_label` is shared by every attempt/dry-run of a condition, so a second attempt's §6.1 would have aggregated the first attempt's probe rows (and a dry run's probe rows would contaminate the later real run, exactly the failure the `_dryrun` identity suffix exists to prevent). Fix: `probe_results.experiment_id` column (additive migration), stamped server-side in `/admin/run-probe` from the process-level `EXPERIMENT_ID` (mirrors analytics stamping); `query_probe_results()` gains an `experiment_id` filter + new `get_probe_session_indices_for_experiment()`; §6.1 and §6.3 scope by `experiment_id` first with a `condition_label` fallback for pre-M21 legacy rows. The harness restart env now also sets `CONDITION_LABEL={condition}` (harness vocabulary) so `get_condition_label_for_experiment()` returns per-condition labels instead of config.yaml's single default for all three conditions.
  - **Gap 2 (also from PR #39, "worth a look"): prompt-MU context nesting.** Prompt MUs stored the full assembled prompt including the `--- Context from memory ---` block; since MU content is retrievable, every retrieved MU's text (markers and all) was copied into a new node, and retrieving that node later injected old context inside new context, compounding per turn. Fix: `_prompt_mu_content()` in `orchestrator.py` — the stored prompt MU is now just the user-message portion (`[User message]\n{input}` when context was injected, bare input otherwise). The full assembled prompt remains audited in `analytics.assembled_prompt_json`; which MUs contributed context remains recorded structurally by the prompt→retrieved-MU edges. The prompt sent to Ollama is unchanged.
  - **Bug caught by the new migration test (would have broken every existing production DB):** the first draft put `CREATE INDEX idx_probe_results_experiment` in `_INIT_SQL` — but `_open_db()` runs `_INIT_SQL` *before* `_migrate_db()`, so on any pre-M21 database the index referenced a column that didn't exist yet and `executescript()` raised on every single open. Moved the index into `_migrate_db()` (after the ALTERs). Rule recorded: **indexes on migrated columns belong in `_migrate_db`, never `_INIT_SQL`.** The same failure also exposed a pre-existing leak: `_open_db()` didn't close the connection when init raised, and an unclosed aiosqlite connection leaves a non-daemon worker thread alive that keeps the whole process from exiting (this is what made every affected pytest run hang instead of fail) — `_open_db()` now closes on any init exception.
  - Tests: new — `assign_probe_schedule` (6), runner probe dispatch (8: run_probe-not-chat, checkpoint fields, no-battery hard stop before wipe, bulk-only needs no battery, cross-series session contiguity, turn-error soft-continue, connectivity hard-stop, resume replays identical schedule), store `experiment_id` (4, incl. the pre-M21-DB migration test that caught the index bug), probes experiment_id stamping (2), experiment-report scoping (3: §6.3 scoped, §6.1 scoped, §6.1 legacy fallback), `_prompt_mu_content` (3), schema `probe_set_version` (2). Updated: FakeBackendClient gains `run_probe`, 3 restart-env assertions gain `CONDITION_LABEL`, 3 series-report tests pass a battery, placeholder-probe-content test replaced by the real-dispatch suite.
  - Tests: 611 total, all passing, no regressions (584 post-PR#39 baseline + 28 new − 1 obsolete placeholder test)
  - Branch: `milestone-21-probe-battery-wiring`
- 2026-07-05: 125-prompt three-condition pilot plan created and executed end-to-end via the harness (remote sandbox session)
  - **Environment constraint, disclosed up front:** this session ran in a cloud sandbox whose egress policy blocks every model-weight host (ollama.com, registry.ollama.ai, huggingface.co, kaggle.com all 403 at the proxy), so the real `gemma4:e4b` could not be served. The run used a new `scripts/stub_ollama_server.py` — a deterministic, stdlib-only Ollama-API stand-in (`/api/tags`, `/v1/chat/completions`, `/api/chat`) whose "agent" echoes the backend-retrieved memory context (so retrieval quality flows to the judge along the pipeline's real causal path) and whose "judge" grades by criterion↔response content-word overlap. **This run is therefore a full-scale PIPELINE VALIDATION of the harness, not a model-learning result** — rerun the same plan on the real Ollama/gemma4:e4b box for science numbers.
  - Created: `plans/pilot_125_2026-07-05.json` — 5 series × 25 turns = 125 prompts per condition (≥100 per the M19 goal), probe_ratio 0.4 with placement "end" so every series closes with the full 10-probe v2 battery, conditions learned/static_weight/random_weight, judge pinned gemma4:e4b @ temp 0.0, budget_cap $25.
  - Created: `prompt_banks/prompt_bank_e75058b6….json` — 75 bulk prompts (19/19/19/18 per cluster, hop levels [0,1]), authored in-session (no ANTHROPIC_API_KEY exists in the sandbox; `generated_by: "session-authored-offline@2026-07-05"`, `prompt_gen.model: "session-authored-offline"` so the bank hash can never collide with a future real Sonnet bank). All 75 pass the harness's own fingerprint scan. **Authoring lesson recorded for future banks:** `retrieve()` takes the FIRST `retrieval_keyword_count` (5) non-stopword tokens of the message as BM25 keywords — prompts must front-load exact (inflection-sensitive) target-MU vocabulary or baseline retrieval finds nothing. First draft: 48/75 prompts failed a clean-graph baseline eval; after two anchor-word revision rounds: 46 up / 18 none / 11 down, where all 11 downs are the stub's deliberate low-effort slice.
  - **Pilot run — COMPLETE, real data end to end:** 375/375 turns (3 conditions × 125), 0 failed turns, 0 ghost MUs, 15 series reports + 1 experiment report generated, notification fired, harness exit 0. Verdict mix per condition ≈ 22-28 up / 26-31 down / 21-24 none (75 judged bulk turns each; probes never judged). §6.2 control shadow: all 4 sanity checks PASS (static: zero_weight_changes 0 violations, zero_feedback_edge_correlation 0; random: weight_std_dev 0.282 > 0.05 floor, correlation 0). §6.1 edge delta ledger: 86 updates, 36 up / 50 down, 0 orphan updates — M20 Beta-posterior learning fired only in the learned condition.
  - **Bug found & fixed (harness):** a dry run shared its experiment_id namespace with the real run of the same plan — `run_identity_for()` ignored `plan.dry_run`, and since `wipe_graph()` never deletes analytics/feedback_events, a dry run's turns would be silently aggregated into the real run's series/experiment reports. Same failure mode as the M19 attempt-collision fix, via a different door. Fix: `_dryrun` identity suffix (`manifest.py`); 12 dry-run-fixture test assertions updated, 1 new test.
  - **Bug found & fixed (backend):** §6.3 `graph_wipe_verification` FAILed on the pilot with `pre_experiment_foreign_mu_count: 34` — exactly the seed fixture. Seed MUs keep the fixture's original `created_at` but are stamped with whichever EXPERIMENT_ID the backend carried at load time, so in a multi-condition harness run the last condition's re-seed (`…_random_weight`) predates the learned condition's first run and read as an incomplete wipe. Fix: `count_pre_experiment_foreign_mus()` now also excludes `series_id LIKE 'seed:%'` (seed provenance is a separate §6.3 check); 1 new test. Report regenerated after the fix: all 3 integrity checks PASS, `overall_pass: true`.
  - **Gap surfaced, NOT built (needs planning):** the harness never calls `POST /admin/run-probe` — its "probe" turns are Phase-0-style `/chat` placeholder turns, which mutate the graph (input/output MUs) instead of being read-only measurements, are not stamped `is_probe_turn`, and record no `probe_results` rows. Consequence: §6.1 probe_pass_rates all null, state_transitions/bridge trajectories empty. Real probe-battery wiring (client method, per-series battery scheduling, `measurement_session_index` bookkeeping) is the biggest missing piece between "harness runs" and "harness measures learning" — recommend it as the next milestone before the real-model pilot.
  - Tests: 584 passing (582 pre-existing + 2 new), no regressions.
  - Branch: `claude/experiment-pilot-harness-2kv1u9`

- 2026-07-06: /build — Milestone 20 Phase 1 complete (Learned-Condition Edge Update Mechanism)
  - **Planning discovery (resolved before writing code):** the backend had no way to know which of the three harness conditions (learned/static_weight/random_weight) a given process was running — `CONDITION_LABEL`/`experiment.condition_label` is a *separate*, pre-existing axis (M9 probe/report vocabulary, `"learning"`/`"static-weight"`/`"random-weight"`), and `runner.py`'s per-condition backend restart only ever overrode `EXPERIMENT_ID`. Added a new, additive `RUN_CONDITION` env var (harness vocabulary), resolved once at orchestrator startup exactly like `EXPERIMENT_ID`. Default when unset is `"static_weight"` (the pre-M20 scalar-weight behavior) — deliberately *not* `"learned"` — so plain chat-UI/dev usage without the harness is behaviorally unchanged; only the harness (now passing `RUN_CONDITION` alongside `EXPERIMENT_ID` on its per-condition restart) opts into the new mechanism.
  - Created: `backend/learning/edge_update.py` — `validate_edge_update_config()` (hard-fails at import time on any invalid `edge_update`/`decay` value, including `decay.enabled: true`), `compute_effective_weight()` (sole condition-dispatched weight read path — Beta posterior mean/Thompson sample for "learned", unchanged scalar `weight` column otherwise), `compute_path_increments()` (pure geometric credit-decay math, hop_k counted backward from the terminal edge), `apply_evidence_cap()`, `get_path_edge_ids_for_mu()` (reconstructs a retrieved MU's incoming path edge(s) from `hop_depth_per_retrieved_mu` + `traversal_path` — empty for zero-hop/direct-BM25 MUs), `resolve_rng_seed()` (derives deterministically from `EXPERIMENT_ID` unless overridden), `process_verdict_for_learning()` (the verdict → counter-write → event-log orchestration; gates on `condition_label == "learned"` **internally**, not just trusting the caller's `apply_weight_update` flag — static/random conditions and probe turns are refused at this layer too, belt-and-suspenders).
  - Modified: `backend/memory/store.py` — `edges` table gains `alpha`/`beta` (`NOT NULL DEFAULT 1.0`) and `evidence_updated_at_turn`; new append-only `edge_update_events` table (survives `wipe_graph()`, same rationale as `feedback_events`/`analytics` — it's part of the experimental audit trail); `create_edge()` accepts `alpha`/`beta`; new `update_edge_counters()` (touches only alpha/beta/evidence_updated_at_turn, never the scalar `weight` column — the learned condition derives its weight, it doesn't maintain a denormalized copy), `insert_edge_update_event()`, `get_edge_update_events_for_edge()`, `get_edge_update_events_for_run()`; `analytics` table gains `edge_weights_used` (JSON) alongside the existing retrieval-decomposition columns.
  - Modified: `backend/memory/retrieval.py` — `retrieve()` gains `condition` (default `"static_weight"` for backward compatibility with every existing caller) and `rng` parameters; every raw `edge["weight"]` read (both K1's `edge_weight_sum` and K2's traversal scoring) now goes through a per-turn cache of `compute_effective_weight()` results, computed once per edge so a Thompson-mode draw stays consistent across every scoring site that reads the same edge within one turn; new `RetrievalResult.edge_weights_used` field logs `{alpha, beta, weight_used, mode}` for every edge in `traversal_path` (scoped to traversal-path edges only, not every edge touched by K1's centrality-sum scoring — a deliberate scope decision to keep mean-mode logging cheap while still fully covering what the trace test needs to reconstruct).
  - Modified: `backend/core/orchestrator.py` — `EDGE_UPDATE_CFG` (validated at import time, hard-fails startup on bad config), `RUN_CONDITION`, `EDGE_RNG` (process-scoped seeded `random.Random`, not reset per-turn — Thompson draws legitimately vary turn-to-turn while staying reproducible given the same `EXPERIMENT_ID`); `retrieve()` call now passes `condition=RUN_CONDITION, rng=EDGE_RNG`; all three `create_edge()` call sites now pass `alpha=EDGE_UPDATE_CFG["prior_alpha"], beta=EDGE_UPDATE_CFG["prior_beta"]`; `RunRecord` now carries `edge_weights_used`.
  - Modified: `backend/analytics/seed_fixture.py` — seed-loaded edges get `edge_update.seed_prior_alpha`/`seed_prior_beta` (falls back to Beta(1,1) uniform if config.yaml is unreadable — a data-loading path shouldn't hard-crash on a config problem the live request path already validates and hard-fails on).
  - Modified: `backend/api/chat.py` — `POST /feedback/verdict`'s `apply_weight_update` branch now calls `edge_update.process_verdict_for_learning()` instead of the M4-era `SimpleRL`+scalar-`weight` path. Response shape is unchanged (`edges_updated`, `weight_updates` with `edge_id`/`old_weight`/`new_weight`/`source_mu_id`/`target_mu_id`/`feedback_event_id`) so the M13–16 report generators that read `analytics.weight_updates` keep working unmodified — `old_weight`/`new_weight` are now the posterior mean before/after (plus new `alpha_before/after`, `beta_before/after`, `hop_k`, `increment` fields). `POST /feedback` (chat-UI thumbs, no condition concept) is untouched — explicitly out of this milestone's scope.
  - Modified: `backend/api/admin.py` — `POST /admin/run-probe` now passes `condition=RUN_CONDITION, rng=EDGE_RNG` to `retrieve()` so probe measurements taken under a `learned`-condition backend restart correctly reflect Beta-derived weights instead of silently falling back to the scalar-weight default.
  - Modified: `scripts/harness/runner.py` — per-condition backend restart now sets `RUN_CONDITION` alongside `EXPERIMENT_ID` (3 existing tests' `restart_calls` assertions updated to match the new env dict shape — a legitimate fixture update, not a design change, since the old dict shape reflected a real, now-closed gap).
  - Modified: `config.yaml` — new `edge_update:` block (all §6 knobs) and reserved `decay:` block; `docker-compose.yml` — `RUN_CONDITION` env passthrough.
  - Added: `tests/test_learning/test_edge_update.py` (44 — credit-decay math, evidence cap, `effective_weight` mean/sample dispatch, `get_path_edge_ids_for_mu`, RNG seed resolution, full config validation matrix incl. `decay.enabled: true`, and `process_verdict_for_learning` end-to-end against a fake store: up/down/none/zero-hop/static/random/probe-turn/order-independence/evidence-cap/multi-MU-independent-increments), `tests/test_learning/test_edge_update_trace.py` (1 — the §8 sign-off trace test: replays a real up-then-down verdict sequence on a real K1→K2 traversal edge against a real temp-file SQLite `GraphStore`, then reconstructs weight-as-of-turn/traversal-path/retrieved-set/rank **from persisted logs alone**, cross-checked against live DB state)
  - Extended: `tests/test_memory/test_store.py` (+9 — alpha/beta defaults and explicit values on `create_edge`, `update_edge_counters` leaves `weight` untouched, `edge_update_events` insert/fetch-by-edge/fetch-by-run/survives-wipe), `tests/test_memory/test_retrieval.py` (+4 — default-condition backward compatibility, learned-condition ignores the scalar `weight` column entirely, `edge_weights_used` scoped to traversal-path edges, Thompson-mode draws stable within one turn given the same seed), `tests/test_core/test_orchestrator.py` (+3 — `RUN_CONDITION` default, `EDGE_UPDATE_CFG` resolved, `EDGE_RNG` is a `random.Random`), `tests/test_analytics/test_seed_fixture.py` (+2 — seed priors stamped from config, fallback to uniform on bad config), `tests/test_api/test_feedback_verdict.py` (2 pre-existing tests updated — old fixtures had no `traversal_path`/`hop_depth_per_retrieved_mu`, so under the new path-based mechanism they correctly produced zero updates; fixtures now include a real K2-traversal shape, which is the spec-correct behavior the old SimpleRL-based fixtures never had to account for)
  - Tests: 62 new, 582 total — all passing, no regressions
  - Branch: `milestone-20-phase-1-edge-update`
  - Decay (counter discounting, ACT-R, usage-gating), pruning, competitive normalization, and the `negative_scope`/`weight_mode` ablation experiments remain explicitly deferred per the phase's own scope boundary — schema-reserved only.

- 2026-07-04: /planner — M19 Phases 1–3 decomposed into three milestones in BUILD-PLAN.md
  - Phase 1 (`milestone-19-phase-1-prompt-bank`): Sonnet-generated prompt bank + criteria, lexical-distinctness validation (pure-Python fingerprint scan reusing `generate_seed_v2.py` constants), versioned `prompt_banks/prompt_bank_{hash}.json` artifact, deterministic prompt_id assignment in turn resolution, cost-estimate confirmation gate. Model: `claude-sonnet-5` via `anthropic` SDK — host-side dev dependency only, never in Docker.
  - Phase 2 (`milestone-19-phase-2-judge`): Gemma judge module + verdict logging. **Planning discovery:** `feedback_events` table lacks all 8 judge fields the overview requires (`judge_source`, `condition_label`, `criterion_triggered`, etc.) — Phase 2 includes a DB migration and a new `POST /feedback/verdict` endpoint with an explicit `apply_weight_update` flag; the condition gate stays in `runner.py` (judge is condition-agnostic). Open decision flagged: verdict string vocabulary (`thumbs_up` vs `up`) — resolve by grepping report code at build time.
  - Phase 3 (`milestone-19-phase-3-pilot`): series/experiment report wire-up, notification channel dispatch (`log` only implemented), budget cap pre-run + mid-run, and the required full pilot smoke run. **Planning discovery:** `runner.py` never passes `series_id` on chat calls — series reports would query empty sets; fixed as Phase 3 build item 1. Open question flagged: how runs get stamped with `experiment_id` (highest-risk unknown; resolve before writing reporting calls).
  - Judge calls go to Ollama at `localhost:11434` from the host-side harness (not `host.docker.internal`, which is the container-side alias).
- 2026-07-05: /build — Milestone 19 Phase 3 complete (Full Pilot Execution + Reporting Wire-Up)
  - **Planning discovery, resolved before writing any Phase 3 code (highest-risk unknown):** confirmed live that `EXPERIMENT_ID` (`backend/core/orchestrator.py`) is resolved once at backend process startup from an env var and cached as a module-level constant for the process's lifetime — `/chat` has no per-request override, and `/admin/graph/wipe`'s `experiment_id` field only names the pre-wipe archive file, it does not change what subsequent runs get stamped with. Asked the user how to resolve this; chosen approach: the harness restarts the backend container with a new `EXPERIMENT_ID` between conditions rather than adding a backend override (keeps the backend server-side footprint at zero, matching the plan's intent).
  - **Second gap discovered only during live Docker verification (not caught by unit tests, since the fake `BackendClient` never exercises real session semantics):** `/chat`'s `series_id` field was being used as a session *lookup* key, not a caller-supplied stamp — passing a deterministic, never-before-seen `series_id` silently discarded it and minted a random UUID instead, on *every* call. This would have made every turn in a "series" land under a different random ID, and series-level reporting would have found nothing. Fixed with a new `get_or_create_session()` in `backend/core/session.py` (uses the caller-supplied ID as the session key on first use if not already registered, otherwise behaves exactly like `create_session()`); confirmed backward-compatible with the chat UI, which only ever resends an ID the backend already issued.
  - **Third gap, not in the original 5 build items:** the `random_weight` control condition needs edge weights re-randomized at the start of each series — this was never wired into `runner.py` (only ever done manually in the M17 smoketest). Added: `client.randomize_edges()` called once per series when `condition == "random_weight"`.
  - Created: `scripts/harness/cost.py` (`CostTracker`, `estimate_prerun_cost()`, `check_prerun_budget()`, `BudgetExceededError`; `JUDGE_COST_PER_CALL_USD` constant — $0 for local Ollama today, structure ready for a hosted judge), `scripts/harness/docker_lifecycle.py` (`restart_backend()` — `docker compose up -d --force-recreate backend` with env overrides; `wait_for_health()` — polls `/health`; `DockerLifecycleError`)
  - Modified: `scripts/harness/runner.py` (`execute_run()` gains `docker_project_root`/`restart_backend_fn`/`wait_for_health_fn` (opt-in, default `None` — every existing test/dry-run call site unaffected) and `bank_generation_cost_usd`/`judge_cost_per_call`; per-condition backend restart before wipe/seed when `docker_project_root` is set; deterministic `series_id` (`{plan_id}_{condition}_s{series_index}`) passed on every `client.chat()` call; series-close reporting keyed off `plan.turns_per_series` (not the dry-run-scoped sequence, so a short smoke dry-run never fires it prematurely); experiment-close reporting when `"learned"` is among the conditions that ran, with `control_experiment_ids` assembled deterministically; pre-run budget gate as the first check, before any graph mutation; mid-run budget check after every judge call; `random_weight` edge randomization once per series)
  - Modified: `scripts/harness/client.py` (`generate_series_report()`, `generate_experiment_report()`, `randomize_edges()`; new `ReportGenerationError` — deliberately distinct from `BackendConnectivityError`/`BackendTurnError` so report failures are always governed by the new `error_policy["report_generation"]` key, never the generic categories)
  - Modified: `scripts/harness/schema.py` (`DEFAULT_ERROR_POLICY` gains `"report_generation": "soft_continue"`; `NotificationConfig.channel: str = "log"` validated against `("log", "email", "webhook")`)
  - Modified: `scripts/harness/notify.py` (`notify()` is now a dispatch table keyed by `channel`; `"email"`/`"webhook"` log a warning and fall back to `"log"` verbatim — adding a real channel later is one new handler function)
  - Modified: `scripts/harness/checkpoint.py` (`SeriesReportRecord`, `ExperimentReportRecord`; `CheckpointState` gains `series_reports: list[SeriesReportRecord]` and `experiment_report: Optional[ExperimentReportRecord]`)
  - Modified: `scripts/harness/manifest.py` (`RunManifest.series_id_map: dict[str, str]` — full `{condition}:{series_index} -> series_id` map for every condition/series in the plan, computed up front)
  - Modified: `scripts/harness/prompt_gen.py` (`estimate_bank_cost_for_plan()` — pure pre-run cost estimate, no API calls, returns `None` when an identical-hash bank already exists)
  - Modified: `scripts/run_experiment_harness.py` (`--no-docker-restart` flag; pre-run budget gate before *any* Sonnet spend or graph mutation, ahead of the CLI's own bank-generation call)
  - Modified: `backend/core/session.py` (`get_or_create_session()`), `backend/api/chat.py` (uses it instead of the old get-then-create-random dance)
  - Added: `tests/test_harness/test_cost.py` (8 new), `tests/test_harness/test_docker_lifecycle.py` (4 new — fake subprocess/sleep/clock, no real Docker or wall-clock waiting), `tests/test_harness/test_notify.py` (4 new)
  - Extended: `tests/test_harness/test_runner.py` (24 new — series/experiment-close reporting incl. soft-continue and hard-stop-override, `random_weight` edge randomization, Docker restart wiring with injected fakes, budget_cap pre-run/none-never-stops/exceeded-by-judge-costs, deterministic `series_id` + manifest map), `tests/test_harness/test_schema.py` (+4), `tests/test_harness/test_prompt_gen.py` (+2), `tests/test_core/test_session.py` (+4)
  - Tests: 45 new, 510 total — all passing
  - Branch: `milestone-19-phase-3-pilot`
  - **Full pilot smoke run — the required done criterion — PASSED with real data:** Docker stack rebuilt and started (backend/frontend/admin, all healthy); ran `plans/phase3_pilot.json` (3 conditions × 1 series × 7 turns [5 bulk + 2 probe], real `gemma4:e4b` agent + judge via Ollama, existing validated Phase 1 prompt bank reused so $0 Sonnet spend). Confirmed live:
    1. Backend restarted correctly for all 3 conditions with the right `EXPERIMENT_ID` each time (verified via `/admin/runs` before the full pilot: a manual restart to `EXPERIMENT_ID=manual-test-123` produced a run stamped `experiment_id: "manual-test-123"`).
    2. All 21 turns completed (`COMPLETE: 21 turn(s) processed`); every bulk turn got a judge verdict (all `"up"` on this pilot's small prompt set).
    3. `random_weight` condition randomized 32 edges once at series start (never re-randomized mid-series, confirmed in the run log).
    4. 3 series reports generated (one per condition) + 1 experiment report (`experiment_report_phase3-pilot-2026-07-05_learned.md`), with `control_experiment_ids = {"static_weight": "...", "random_weight": "..."}` correctly assembled.
    5. **§6.2 control shadow — all 4 sanity checks PASS on real data:** `static_weight`: `zero_weight_changes` PASS (0 violations), `zero_feedback_edge_correlation` PASS (0 violations). `random_weight`: `weights_rerandomized` PASS (`weight_std_dev=0.1848` > `0.05` floor), `zero_feedback_edge_correlation` PASS (0 violations).
    6. Notification fired: `{"status": "complete", "report_path": "/app/data/reports/experiment_report_phase3-pilot-2026-07-05_learned.md"}`.
    7. `budget_cap` verified live in both directions: a plan needing a genuinely new (not-already-cached) bank with `budget_cap=0.01` hard-stopped with `pre-run estimated cost $0.5833 exceeds budget_cap $0.0100` before any Docker restart, wipe, or API call; the real pilot plan (`budget_cap=None`) proceeded normally.
  - Full test suite (510) passing, no regressions.
  - **Post-verification review (user-requested):** traced the EXPERIMENT_ID/series_id mechanism end-to-end for information-loss risk. Found one real gap, now fixed: on a resumed run, `execute_run()`'s condition loop unconditionally restarted/wiped/reseeded *every* condition, including ones already fully complete in the checkpoint — wasteful (an extra Docker restart + wipe/seed cycle) though never destructive (`wipe_graph()` never touches `analytics`/`feedback_events`, and the `already_reported` guard already prevented duplicate series reports). Fixed: `runner.py` now skips the restart/wipe/seed entirely when every turn for a condition is already checkpointed. 1 new test (`test_resume_skips_restart_wipe_seed_for_already_complete_condition`), 511 total passing. Separately noted (pre-existing, not changed by Phase 3): re-running an already-`complete` plan starts a fresh checkpoint but reuses the same deterministic `{plan_id}_{condition}` experiment_id, so two independent full trials of the same plan would mix their analytics rows under one experiment_id bucket — inherited from Phase 0's resume-vs-fresh logic, not introduced here; flagged for awareness, not fixed in this session.
  - Docker stack stopped (`docker compose down`) — data untouched (bind-mounted `./memory`, not a container volume).
  - **Follow-up (user-requested): fixed the pre-existing experiment_id collision issue.** Root cause: `run_id`/`experiment_id` were derived purely from `plan_id` (+ config hash for `run_id`), with no distinction between "resuming an incomplete run" and "starting fresh at an identity that already has data" — a `"complete"` checkpoint skipped the resume-prompt entirely and silently created a brand-new `CheckpointState` at the *same* `run_id`, and declining to resume an incomplete run did the same. Since `wipe_graph()` never deletes `analytics`/`feedback_events`, either path would write a second trial's rows into the same `experiment_id` bucket as the first (and silently overwrite the first run's `manifest.json`/`checkpoint.json` files on disk).
    - Fix: `manifest.py` — `compute_run_id()` gains an `attempt: int = 1` parameter (`attempt=1` reproduces the exact original `run_id` format, so a plan that only ever runs once is unaffected); new `run_identity_for(plan, attempt)` for building `experiment_id`/`series_id` strings (`"{plan_id}"` at attempt 1, `"{plan_id}_attemptN"` beyond).
    - `runner.py` — the resume-detection block is now a loop: it tries attempt 1, and only bumps to attempt 2+ when that identity's checkpoint is already `"complete"`, or the user declines to resume an incomplete one. Accepting a resume keeps attempt 1's identity unchanged (the common, everyday case). `experiment_id`/`series_id_map`/`control_experiment_ids` all now derive from `run_identity` instead of raw `plan.plan_id`.
    - 7 new tests (`test_manifest.py` — `compute_run_id`/`run_identity_for` attempt behavior; `test_runner.py` — re-running an already-complete plan gets a new run_id + experiment_id, declining a resume gets a new run_id + experiment_id, accepting a resume keeps the same one). 518 tests total, all passing.
  - **Second follow-up (user-requested): resuming a condition mid-flight no longer wipes/reseeds the graph.** The prior fix only skipped wipe/seed for conditions that were *fully* complete; a *partially* resumed condition (some turns done, some pending) still unconditionally called `wipe_graph()`+`seed_graph()` again before continuing — silently discarding the memory graph state (retrieved MUs, reinforced edges) that the already-completed turns had built up, and (for `random_weight`) re-randomizing edge weights mid-series, giving the turns before and after the interruption two different random baselines within what's supposed to be one comparable series.
    - `runner.py` — the per-condition guard now distinguishes "fully complete" (skip everything, as before) from "resumed mid-flight" (`any` turn done but not `all`): the backend restart still happens if configured (needed for correct `EXPERIMENT_ID` stamping on the remaining turns, and never touches the bind-mounted graph volume), but `wipe_graph()`/`seed_graph()` are skipped entirely. A new `series_previously_started` snapshot (taken once, before this invocation processes any new turns) prevents the `random_weight` re-randomization from firing again for a series that already has a completed turn from a prior session.
    - 2 new tests (`test_resume_mid_series_preserves_graph_and_skips_wipe_seed`, `test_resume_mid_series_does_not_re_randomize_edges`); updated one existing test's assertion to match the corrected (no-wipe) behavior. 520 tests total, all passing.
- 2026-07-05: /build — Milestone 19 Phase 2 complete (Judge Integration)
  - Created: `scripts/harness/judge.py` (`OllamaJudgeClient` — `chat_json()`, `model_available()`; `judge_turn()`; `JudgeVerdict` dataclass; `ProbeTurnJudgeError`, `JudgeUnparseableError`; closed-vocabulary verdict parsing with one re-ask)
  - Modified: `backend/memory/store.py` (feedback_events gains 8 nullable columns: `condition_label`, `rationale`, `criterion_triggered`, `judge_source`, `judge_model_name`, `judge_temperature`, `judge_pinned_snapshot`, `prompt_id` — additive `_migrate_db()` entries + `_INIT_SQL`; `insert_feedback_event()` writes all new fields)
  - Modified: `backend/analytics/feedback_events.py` (`FeedbackEvent` gains the same 8 optional fields)
  - Modified: `backend/api/chat.py` (new `POST /feedback/verdict` endpoint with `VerdictRequest`/`VerdictResponse`; extracted `_apply_weight_updates()` shared helper — used by both `/feedback` and `/feedback/verdict` so the §6.1 edge-delta-ledger orphan check audits one code path; `/feedback` now stamps `judge_source="human"`, contract otherwise unchanged)
  - Modified: `scripts/harness/client.py` (`BackendClient.post_verdict()` — POST /feedback/verdict wrapper, same connectivity/turn-error split as `chat()`)
  - Modified: `scripts/harness/schema.py` (`JudgeConfig.temperature` tightened `le=2.0` → `le=0.1`; `DEFAULT_ERROR_POLICY` gains `"judge_unparseable": "soft_continue"`)
  - Modified: `scripts/harness/checkpoint.py` (`TurnCheckpoint` gains `verdict`, `feedback_event_id`, `judge_error` — kept separate from `detail` so a verdict-posting hiccup never overwrites the chat call's own audit info)
  - Modified: `scripts/harness/runner.py` (judge-availability preflight — `GET /api/tags`, unconditional hard stop if the judge model is missing, runs before any wipe/seed call; `_judge_and_post_verdict()` helper: judges each successfully-completed bulk turn via the loaded prompt bank's criteria, computes `apply_weight_update = (condition == "learned" and verdict in ("up","down"))`, posts the verdict, supports `--interactive` accept/override/skip; `JudgeUnparseableError` routes through `error_policy["judge_unparseable"]`)
  - Modified: `scripts/run_experiment_harness.py` (`--interactive` and `--judge-host` CLI flags; constructs `OllamaJudgeClient` alongside `BackendClient`)
  - Added: `tests/test_harness/test_judge.py` (13 new — verdict parsing incl. closed-vocabulary `criterion_triggered` check, probe-turn guard, retry-then-raise, and `OllamaJudgeClient` request-shape tests against `httpx.MockTransport`)
  - Extended: `tests/test_harness/test_runner.py` (11 new — learned/static/none weight-update gating, no-judge-client no-op, unparseable soft-continue + checkpoint anomaly, hard-stop override, `--interactive` accept/override, judge preflight hard-stop, probe turns never judged)
  - Extended: `tests/test_harness/test_schema.py` (5 new — temperature validator, `judge_unparseable` policy default/override)
  - Extended: `tests/test_api/test_feedback_verdict.py` (new file, 12 tests), `tests/test_analytics/test_feedback_events.py` (+4), `tests/test_memory/test_store.py` (+1 migration test)
  - Tests: 44 new, 465 total — all passing
  - Branch: `milestone-19-phase-2-judge`
  - **Verdict vocabulary decision (build item 2, resolved by grep):** searched `series_report.py`, `experiment_report.py`, `control_shadow.py` for `"thumbs_up"`/`"thumbs_down"` string matching — found none; the one comment referencing "thumbs_up runs" in `experiment_report.py` actually keys off `analytics.feedback_signal == 1`, not the verdict string. Decision: the judge endpoint stores its own `"up"/"down"/"none"` vocabulary directly rather than mapping onto the older `"thumbs_up"/"thumbs_down"` strings; `judge_source` distinguishes origin, not the verdict string.
  - **Live verification (Docker not running this session — same deferral as Phase 0/1, Ollama was reachable natively):** ran `judge_turn()` against the real `gemma4:e4b` model for 5 real (prompt, criteria, response) cases spanning up/down verdicts and a criterion-triggered check — all 5 returned clean, correctly-classified JSON on the first attempt (0% unparseable rate, well under the 10% risk threshold flagged in BUILD-PLAN.md). Full Docker-backend dry-run (checks 1–2 of the Phase 2 verification protocol) deferred to a future `/smoketest` session, same as Phase 0/1's precedent — `execute_run()` was instead verified against a fake `BackendClient` in `test_runner.py`, exercising the identical code path.
  - **Pre-pilot calibration reminder (flagged in BUILD-PLAN.md):** before any large pilot run, spot-check 15–20 judged turns with `--interactive` against Gemma's verdicts to catch ambiguously-written criteria — schedule this between Phase 2 merge and the Phase 3 pilot.
- 2026-06-14: Architecture document drafted — `.agents/local_corvus_architecture_v0.1.md`
- 2026-06-15: Build plan created — `BUILD-PLAN.md`, 4 milestones defined
- 2026-06-15: /build — Milestone 1 complete
  - Created: `config.yaml`, `backend/gate/`, `backend/memory/store.py`, `backend/core/session.py`, `backend/api/chat.py`
  - Updated: `backend/main.py`, `backend/requirements.txt`
  - Added: `tests/test_gate/`, `tests/test_memory/`, `pytest.ini`
  - Tests: 32 new tests, all passing
  - Branch: milestone-1-foundation
  - PR: (see GitHub)
  - Bug found and fixed: aiosqlite `async with await connection` pattern starts the background thread twice on Python 3.9 — must use `db = await connect(...)` then explicit `await db.close()` instead
- 2026-06-15: /build — Milestone 2 complete
  - Created: `backend/memory/retrieval.py`, `backend/memory/extractor.py`, `backend/core/orchestrator.py`
  - Updated: `backend/memory/store.py` (added get_all_edges()), `backend/api/chat.py` (delegates to orchestrator), `backend/requirements.txt` (rank-bm25)
  - Added: `tests/test_memory/test_retrieval.py`, `tests/test_memory/test_extractor.py`
  - Tests: 36 new tests, 68 total — all passing
  - Branch: milestone-2-memory-engine
  - PR: (see GitHub)
  - Decision: BM25 index rebuilt on every retrieval call (simple; corpus is small for local research)
  - Decision: Multi-hit threshold is 2+ keyword hits for K1 (exact word matching, no stemming)
- 2026-06-17: /build — Milestone 3 complete
  - Created: `backend/tools/base.py`, `backend/tools/file_tool.py`, `backend/tools/registry.py`
  - Created: `backend/analytics/run_log.py`, `backend/memory/snapshot.py`
  - Created: `backend/api/admin.py` (GET /admin/runs, /admin/graph, /admin/config)
  - Updated: `backend/memory/store.py` (analytics table, insert_run, query_runs, get_run, update_run_feedback)
  - Updated: `backend/core/orchestrator.py` (tool loop with UM gating, analytics capture, graph snapshot)
  - Updated: `backend/api/chat.py` (exposes run_id and tool_call_mu_ids)
  - Updated: `backend/main.py` (registers admin router)
  - Added: `tests/test_tools/test_registry.py`, `tests/test_tools/test_file_tool.py`, `tests/test_analytics/test_run_log.py`
  - Tests: 40 new tests, 108 total — all passing
  - Branch: milestone-3-tool-system
  - PR: (see GitHub after push)
  - Fix: Python 3.9 incompatibility — `X | None` union syntax must be `Optional[X]` throughout

## First experiment task suite

**Task type chosen:** File creation + recall task (local-only; no external APIs)

**Task definition:**  
1. Send message: `"Please write the fact 'The boiling point of water is 100 degrees Celsius at sea level.' to a file called science_facts.txt"`
2. In a new session or after several unrelated turns, send: `"What temperature does water boil at sea level? Check your files if needed."`
3. Success: agent uses file_read tool to retrieve the file and includes the correct fact in its response.

**Why this task:**  
- Tests the full tool loop: detect tool call → gate → dispatch file_write → gate → MU → (later) detect file_read call → gate → dispatch → gate → MU
- Recall is observable: the agent either reads and cites the fact or it doesn't
- Fully local — no external APIs, no rate limits, deterministic file content

**Baseline experiment:** Run the task suite N=10 times with `initial_edge_weight: 0.5` and no learning active (feedback signal always None). Record latency_ms and whether the recall succeeded in PROGRESS.md after the baseline run.

**Success metric:** Fraction of recall turns where the agent uses file_read and returns the correct temperature. Baseline is expected to be ~0.8 (tool call works every time Gemma decides to use it; the question is whether it decides to).

## What exists

- `backend/main.py` — FastAPI app with /chat, /health, /ollama-health, /admin/* routes
- `backend/api/chat.py` — POST /chat: full pipeline including tool loop, returns run_id
- `backend/api/admin.py` — GET /admin/runs, /admin/graph, /admin/config
- `backend/gate/um.py` — UniversalMessage schema + create_um() factory
- `backend/gate/sanitize.py` — sanitize_in() / sanitize_out() (pure, stateless)
- `backend/gate/audit.py` — append_audit() → /app/memory/audit.log (JSONL, append-only)
- `backend/memory/store.py` — GraphStore: async CRUD for memory_units, edges, analytics tables
- `backend/memory/retrieval.py` — BM25 retrieval + traversal funnel
- `backend/memory/extractor.py` — 3-mode extractor (keyword, fallback, Gemma summarize)
- `backend/memory/snapshot.py` — take_snapshot() → /app/memory/snapshots/*.json.gz
- `backend/core/session.py` — create_session() / get_session() (in-memory)
- `backend/core/orchestrator.py` — full pipeline: gate → retrieve → extract → prompt → tool loop → analytics → snapshot
- `backend/tools/base.py` — Tool ABC + ToolResult schema
- `backend/tools/file_tool.py` — FileTool: file_read + file_write in /app/memory/workspace/
- `backend/tools/registry.py` — ToolRegistry: config-driven dispatch, ToolNotFoundError, ToolLoopError
- `backend/analytics/run_log.py` — RunRecord schema + capture_run() + query_runs()
- `backend/Dockerfile` + `docker-compose.yml` — backend service, ./memory:/app/memory volume
- `config.yaml` — full hyperparameter registry (single source of truth)

## Decisions made

- **Single edge type:** weight + directionality only, no semantic labels
- **No embeddings in v1:** BM25 keyword retrieval only
- **No external API calls in v1:** all tools are local
- **Ollama runs natively on Windows**, not in Docker — app container reaches it at `host.docker.internal:11434`
- **aiosqlite** for async-safe SQLite access (stdlib `sqlite3` is not async-safe)
- **Admin UI is a separate React app** (port 3001), not routes in the chat UI
- **flags field is free strings**, not an enum — patterns are embedded in the flag text for debugging visibility
- **Connection-per-operation** pattern for aiosqlite (not pooling) — appropriate for local research volume and avoids thread lifecycle bugs
- **Analytics table lives in graph.db** alongside memory_units and edges — one database for all persistent state, accessed only through GraphStore
- **Tool schemas passed on every Ollama call** — Gemma decides whether to invoke a tool per turn; no pre-filtering
- **First experiment task:** file creation + recall (see experiment section above)

## Open questions

1. **Weight initialization value** — flat 0.5 vs. source-type-based prior? Keeping 0.5 for baseline; revisit after first learning run.
2. **Decay function shape** — linear / exponential / sigmoid? Decide before Phase 4 results are meaningful.
3. **RL reward distribution along activated edges** — Build Milestone 4 with uniform distribution as default; revisit after first learning run.

- 2026-06-17: /build — Milestone 4 complete
  - Created: `backend/learning/interface.py` (LearningAlgorithm ABC)
  - Created: `backend/learning/simple_rl.py` (uniform weight update RL)
  - Updated: `backend/api/chat.py` (POST /feedback endpoint with SimpleRL)
  - Created: `admin/` — React Admin UI (port 3001): RunLogTable, LearningCurveChart, MemoryInspector
  - Created: `frontend/` — React Chat UI (port 3000): ChatWindow, MessageBubble, FeedbackButtons
  - Updated: `docker-compose.yml` (adds frontend and admin services)
  - Added: `tests/test_learning/test_simple_rl.py` (11 tests)
  - Tests: 11 new tests, 119 total — all passing
  - Branch: milestone-4-learning-admin-ui
  - Decision: Uniform RL reward distribution (open question #3) — all activated edges receive same delta
  - Decision: MemoryInspector uses paginated table (not force-directed graph) for reliability at scale

- 2026-06-19: /smoketest — full Docker stack tested, 4 bugs found and fixed
  - Bugs found: 4
  - Bugs fixed: 4
  - Branch: fix/smoketest-2026-06-19
  - PR: (see GitHub)
  - Fix 1: Missing CORS middleware (critical — browser blocked all API calls from port 3000 to 8000)
  - Fix 2: OLLAMA_HOST env var passing empty string when not set in shell (bypassed Python fallback)
  - Fix 3: config.yaml not accessible in Docker — path ../../config.yaml resolves to /config.yaml in container (fixed in orchestrator.py, chat.py, gate/sanitize.py, tools/registry.py, memory/extractor.py, api/admin.py)
  - Fix 4: gate/sanitize.py missing `import os` needed for the CONFIG_PATH fix
  - Status: all 3 containers start cleanly; /chat → /feedback pipeline works end-to-end; memory retrieval active on second message; admin endpoints return correct data; 119 tests passing

- 2026-06-19: /build — Milestone 5 complete
  - Modified: `config.yaml` (added model.context_window_tokens: 8192)
  - Modified: `backend/core/orchestrator.py` (extract Ollama usage tokens; return tokens_in/tokens_out from _run_tool_loop; populate RunRecord and InteractionResult)
  - Modified: `backend/api/chat.py` (tokens_in/tokens_out added to ChatResponse)
  - Modified: `frontend/src/components/ChatWindow.jsx` (token bar, New Session button, /admin/config fetch for context window size)
  - Modified: `frontend/src/components/MessageBubble.jsx` (Copy button with Copied! confirmation)
  - Tests: 119 existing tests — all passing; no new backend tests (frontend-heavy milestone; manual verification required)
  - Branch: milestone-5-chat-ui-quality
  - Note: navigator.clipboard requires secure context (HTTPS or localhost) — safe in development, note for production

- 2026-06-20: /build — Milestone 6 complete
  - Modified: `backend/core/orchestrator.py` — three changes in run_interaction():
    1. Step 4: updated prompt_text (prompt MU content) label to match new system message format
    2. Step 7a: replaced bare `[Context from memory]` tag with explicit natural-language label in system role
    3. Step 7b: fixed user role message from `prompt_gate.content` (duplicated context) to `input_result.content` (clean user input only)
    4. Extracted `_build_ollama_messages()` pure helper for testability
  - Created: `tests/test_core/test_orchestrator.py` (11 tests covering message construction with/without context)
  - Tests: 11 new tests, 130 total — all passing
  - Branch: milestone-6-prompt-clarity

- 2026-06-21: /build — Milestone 7 complete
  - Modified: `config.yaml` (added `allowed_attachment_mimetypes`, `max_attachment_size_bytes: 5242880`, `max_attachments_per_message: 4` under `gate:`)
  - Modified: `backend/gate/sanitize.py` (`AttachmentSanitizeResult` Pydantic model, `sanitize_attachment()`, `_check_magic_bytes()` — MIME allowlist, base64 decode, size limit, magic bytes validation; flags: `attachment_mime_rejected`, `attachment_base64_invalid`, `attachment_size_exceeded`, `attachment_magic_mismatch`; on any failure returns `data_bytes=b""`)
  - Modified: `backend/api/chat.py` (`AttachmentRequest` model; `attachments` field on `ChatRequest`; passes `AttachmentInput` list to `run_interaction()`)
  - Modified: `backend/core/orchestrator.py` (`AttachmentInput` dataclass; `run_interaction()` Step 1 gates each attachment, builds text-only MU content with `[Attached: …]` descriptions; `_build_ollama_messages()` builds content-parts list with images before text per Google spec)
  - Modified: `frontend/src/components/ChatWindow.jsx` (paperclip button + hidden file input, FileReader pipeline with `URL.createObjectURL()` thumbnails revoked on send, attachment preview strip, send enabled for attachments-only, New Session revokes objectURLs)
  - Modified: `frontend/src/api.js` (`sendMessage` sends `attachments` array in JSON body, strips `preview_url`)
  - Created: `tests/smoke_test_multimodal.py` (smoke test — not part of pytest suite)
  - Added: 11 new tests in `tests/test_gate/test_sanitize.py` (`TestSanitizeAttachment` — synthetic pure-Python image bytes, all MIME types, all rejection modes)
  - Tests: 11 new, 141 total — all passing
  - Branch: milestone-7-multimodal
  - ⚠ Known limitation: Ollama 0.30.x on Windows has a regression (GitHub Issue #16532) where `gemma4:e4b` decodes image tokens but does not interpret image content. Root cause: PR #16031 (merged 2026-05-07). Implementation is correct per OpenAI-compatible spec. End-to-end manual test blocked until Ollama fixes the regression. Workaround: roll back to Ollama v0.24.0.
  - Key decisions:
    - Binary attachment data never written to SQLite, audit.log, or snapshots — only text descriptions stored in MUs
    - Images are one-shot per message — dropped from session history to avoid unbounded RAM growth
    - Images placed BEFORE text in content-parts array per Google's Gemma multimodal spec
    - WEBP magic check uses RIFF header (bytes 0-3) + WEBP subtype (bytes 8-11) — different from other formats
    - `AttachmentSanitizeResult` needs `model_config = {"arbitrary_types_allowed": True}` for `bytes` field in Pydantic BaseModel

- 2026-06-22: /build — Milestone 9 complete
  - Created: `probes/probe_set_v1.yaml` (template: 5 probes — 3 phenomena, 2 bridge pairs)
  - Created: `backend/analytics/probes.py` (ProbeDefinition, ProbeResult, load_probe_set, compute_probe_result, record_probe_result)
  - Created: `backend/analytics/seed_fixture.py` (SeedFixtureManifest, compute_fixture_hash, register_fixture, load_and_verify_fixture)
  - Modified: `backend/memory/store.py` (3 new tables: probes, probe_results, seed_fixture_manifest; new methods: sync_probes, get_probe, insert_probe_result, query_probe_results, insert_seed_fixture_manifest, get_seed_fixture_manifest; migration for measurement_session_index + is_probe_turn)
  - Modified: `backend/memory/snapshot.py` (take_battery_snapshot, take_pre_pruning_snapshot)
  - Modified: `backend/analytics/run_log.py` (measurement_session_index + is_probe_turn added to RunRecord)
  - Modified: `backend/core/orchestrator.py` (CONDITION_LABEL env var overrides config.yaml condition_label)
  - Modified: `backend/api/admin.py` (POST /admin/run-probe, GET /admin/probe-results)
  - Modified: `backend/main.py` (startup probe sync)
  - Created: `admin/src/components/ProbeResults.jsx` (filter controls, per-phenomenon rank table, learning curves, bridge probe toggle)
  - Modified: `admin/src/App.jsx` (Probe Results tab)
  - Modified: `docker-compose.yml` (probes + seed_fixtures mounts, CONDITION_LABEL passthrough)
  - Added: 24 new tests (test_probes.py, test_seed_fixture.py, test_snapshot.py)
  - Tests: 24 new, 193 total — all passing
  - Branch: milestone-9-experiment-framework
  - Key decisions:
    - Probe content (prompt_text, gold_mu_ids) ships as template — filled in by experimenter after seed fixture generation
    - POST /admin/run-probe is strictly read-only: only calls retrieve(), no MU creation, no feedback events, run_id always None
    - CONDITION_LABEL env var takes priority over config.yaml (empty string falls through to config)
    - seed_fixtures directory mounted as Docker volume so fixtures are accessible without rebuilding the image

- 2026-06-22: /build — Milestone 8 complete
  - Modified: `backend/memory/retrieval.py` (RetrievalResult gains 6 new fields: bm25_scores_per_mu, entry_point_mu_ids, candidate_set_before_ranking, traversal_path, hop_depth_per_retrieved_mu, edge_contributions — all populated from values already computed in retrieve())
  - Modified: `backend/analytics/run_log.py` (RunRecord gains retrieval decomposition fields, assembled_prompt_json, config_hash, full_config_snapshot, condition_label, turn_index, session_turn_index)
  - Created: `backend/analytics/feedback_events.py` (FeedbackEvent model + record_feedback_event(); one DB row per /feedback call)
  - Modified: `backend/memory/store.py` (edges table: creation_run_id + creation_trigger; analytics table: all new RunRecord columns; feedback_events table added; _migrate_db() for idempotent ALTER TABLE on existing DBs; new methods: get_next_turn_index, get_session_turn_count, insert_feedback_event, get_feedback_events_for_run, get_edge)
  - Modified: `backend/core/orchestrator.py` (wires new RunRecord fields; passes creation_run_id + creation_trigger to create_edge; adds _scrub_prompt_for_storage() helper)
  - Modified: `backend/api/chat.py` (FeedbackRequest gains verdict_basis + is_probe_turn; feedback endpoint creates FeedbackEvent; weight_updates enriched with source_mu_id, target_mu_id, feedback_event_id)
  - Modified: `config.yaml` (adds experiment.condition_label: "learning")
  - Tests: 28 new tests, 169 total — all passing
  - Branch: milestone-8-core-instrumentation
  - PR: (see GitHub)
  - Key decision: _scrub_prompt_for_storage() strips base64 image data by estimating decoded size from b64 string length (len * 3/4) — avoids re-decoding just for size reporting

- 2026-06-26: /build — Milestone 10 complete
  - Modified: `config.yaml` (adds experiment.use_session_context: false)
  - Modified: `backend/core/orchestrator.py` (USE_SESSION_CONTEXT module-level constant resolved from env or config; conditional history injection in Step 7 with INFO log; use_session_context added to RunRecord)
  - Modified: `backend/analytics/run_log.py` (use_session_context: bool = False added to RunRecord)
  - Modified: `backend/memory/store.py` (use_session_context column in analytics table; migration entry; insert_run updated)
  - Modified: `docker-compose.yml` (USE_SESSION_CONTEXT env var passthrough)
  - Modified: `admin/src/components/RunLogTable.jsx` (session_ctx: on/off in expanded row detail)
  - Tests: 6 new tests, 199 total — all passing
  - Branch: milestone-10-session-context-flag
  - Key decisions:
    - USE_SESSION_CONTEXT is a module-level constant — changing config.yaml or env var requires a restart (consistent with all other module-level config)
    - Session history is still accumulated regardless of the flag (so a mid-session config change picks up full history on restart)
    - assembled_prompt_json in the run record is the authoritative audit — naturally reflects whether history was sent

- 2026-06-26: /build — Milestone 11 complete
  - Modified: `backend/core/orchestrator.py` (6 named constants: _OLLAMA_FINISH_REASON_LENGTH, FAILURE_REASON_TOKEN_LIMIT, FAILURE_REASON_EMPTY_OUTPUT, _MU_SOURCE_TYPE_FAILED_TOKEN_LIMIT, _MU_SOURCE_TYPE_FAILED_EMPTY_OUTPUT, FAILURE_UI_MESSAGES; _run_tool_loop now returns 5-tuple with failure_reason; run_interaction substitutes UI message on failure; output MU source_type encodes failure cause; RunRecord and InteractionResult gain failure_reason field)
  - Modified: `backend/analytics/run_log.py` (failure_reason: Optional[str] = None added to RunRecord)
  - Modified: `backend/memory/store.py` (failure_reason TEXT column in analytics table; migration entry; insert_run updated)
  - Modified: `backend/api/chat.py` (failure_reason: Optional[str] = None added to ChatResponse; populated from result.failure_reason)
  - Modified: `frontend/src/components/MessageBubble.jsx` (failureReason prop; warning visual treatment: amber border, warm background, italic text)
  - Modified: `frontend/src/components/ChatWindow.jsx` (failure_reason from API response threaded into message object; passed to MessageBubble)
  - Modified: `admin/src/components/RunLogTable.jsx` (Failure column in table header; failure indicator per row; failure_reason in expanded detail)
  - Created: `tests/test_core/test_failure_detection.py` (19 new tests)
  - Tests: 19 new tests, 218 total — all passing
  - Branch: milestone-11-empty-response-detection
  - Key decisions:
    - UI message substitution happens in run_interaction() BEFORE session history append, gate, and MU creation — so the audit trail reflects exactly what the user saw
    - output MU source_type is one of: "agent_output", "agent_output_failed_token_limit", "agent_output_failed_empty_output" — queryable by failure mode via SQL
    - finish_reason is guarded with `(finish_reason or "") == _OLLAMA_FINISH_REASON_LENGTH` to handle None from older Ollama versions
    - Max-iterations tool loop exhaustion uses FAILURE_REASON_EMPTY_OUTPUT (not a new constant — same bucket as other unexpected empties)

- 2026-06-27: /build — Milestone 13 complete
  - Created: `backend/analytics/reports/__init__.py`
  - Created: `backend/analytics/reports/series_report.py` (8-section §5 generator: failure summary, retrieval trace, retrieval rate, cross-series edges, edge weight deltas, probe integrity, Mode-3 count, num_ctx pressure)
  - Created: `backend/analytics/reports/markdown_renderer.py` (JSON → Markdown renderer for all 8 sections)
  - Modified: `backend/memory/store.py` (added `generated_reports` table + 10 new query methods for report support)
  - Modified: `backend/api/admin.py` (4 new endpoints: POST /admin/reports/generate-series, GET /admin/reports, GET /admin/reports/{id}, GET /admin/reports/{id}/markdown)
  - Modified: `config.yaml` (added `reporting.mode3_min_expected_length: 100`)
  - Created: `tests/test_analytics/test_series_report.py` (13 new tests)
  - Created: `tests/test_analytics/test_markdown_renderer.py` (8 new tests)
  - Tests: 21 new, 256 total — all passing
  - Branch: milestone-13-series-report-generator
  - Key decisions:
    - All DB access for report generation goes through new GraphStore methods — no direct SQLite in the report module
    - Ghost-MU check uses Python-side set intersection (activated_mu_ids is a JSON array — SQLite json_each() not needed for this query)
    - §5.2 trace digest truncates MU IDs to 8 chars for readability; full IDs in JSON
    - Markdown renderer caps table rows at 50 (§5.4) and 30 (§5.5) with a "…and N more" note for large datasets
    - `generated_reports` table uses CREATE TABLE IF NOT EXISTS — no ALTER TABLE migration needed (new table, not schema change to existing)

- 2026-06-27: /build — Milestone 16 complete
  - Created: `backend/analytics/reports/control_shadow.py` (§6.2: three sanity checks + comparative delta computation + generate_control_shadow() orchestrator)
  - Modified: `backend/analytics/reports/experiment_report.py` (generate_experiment_report() accepts optional control_experiment_ids; calls control shadow when provided; control_shadow key now populated)
  - Modified: `backend/analytics/reports/markdown_renderer.py` (_render_s62() replaces M14/M15 §6.2 placeholder; renders sanity checks + comparative delta table + cross-condition comparison)
  - Modified: `backend/api/admin.py` (GenerateExperimentReportRequest accepts optional control_experiment_ids dict)
  - Modified: `config.yaml` (adds reporting.random_control_variance_floor: 0.05)
  - Created: `admin/src/pages/Reports.jsx` (Reports page: list, generate form, viewer, comparison table)
  - Created: `admin/src/components/ReportsList.jsx` (filterable list with View + ↓ JSON buttons)
  - Created: `admin/src/components/ReportViewer.jsx` (react-markdown renderer for Markdown reports, GFM tables)
  - Created: `admin/src/components/ExperimentComparison.jsx` (auto-renders when control_shadow present; sanity check status badges + delta table)
  - Modified: `admin/src/App.jsx` (adds Reports tab)
  - Modified: `admin/package.json` (adds react-markdown + remark-gfm)
  - Created: `tests/test_analytics/test_control_shadow.py` (13 new tests)
  - Tests: 13 new, 315 total — all passing
  - Branch: milestone-16-control-shadow-admin-ui
  - Key decisions:
    - Control shadow embedded in the learned-condition report (not a separate file) — comparison can't drift out of sync
    - generate_control_shadow() runs _section_61_learning_measures() for each control — same code path as the learned condition, no separate implementation
    - react-markdown + remark-gfm renders GFM tables (| delimited) correctly in browser
    - Comparison table auto-renders when control_shadow is non-null in the fetched report JSON

- 2026-06-27: /build — Milestone 15 complete
  - Modified: `backend/analytics/reports/experiment_report.py` (§6.1 section: state-transition detection, MUs by probe outcome, edge delta ledger with orphan-update check, cross-series probe MUs, bridge-MU rank trajectory + transfer candidate detection, saturation/runaway-reinforcement flags; integrated into `generate_experiment_report()`)
  - Modified: `backend/analytics/reports/markdown_renderer.py` (`_render_s61()` — full Markdown renderer for all §6.1 sub-sections; replaces M14 placeholder)
  - Modified: `backend/memory/store.py` (`outcome_override TEXT` on `probe_results` schema + migration; 3 new methods: `get_all_runs_for_experiment`, `get_all_probes`, `get_mu_info_batch`)
  - Modified: `config.yaml` (`reporting.pass_rank_threshold: 3`, `reporting.saturation_threshold: 0.9`)
  - Modified: `tests/test_analytics/test_experiment_report.py` (updated M14 null-placeholder assertion to match M15 behavior)
  - Created: `tests/test_analytics/test_learning_measures.py` (33 new tests)
  - Tests: 33 new, 302 total — all passing
  - Branch: milestone-15-experiment-report-learning
  - PR: https://github.com/PaulArthurMiller/local_corvus/pull/19
  - Key decisions:
    - Weight update processing is Python-side (iterate over decoded `weight_updates` JSON arrays) to avoid SQLite `json_each()` version dependency
    - `outcome_override` is the correct mechanism for negative_flagged outcome — checked first in `_classify_probe_outcome()` before pass/fail rank logic
    - Bridge-MU transfer detection uses all reinforcement events for the experiment (no precise temporal windowing between probe sessions — probe results don't carry turn_index)
    - `learning_measures` key in the experiment report JSON is now a dict, not null

- 2026-06-27: /build — Milestone 14 complete
  - Created: `backend/analytics/reports/experiment_report.py` (§5 aggregation across series, §6.3 integrity checks, §6.4 statistical caveat, §6.1/§6.2 null placeholders)
  - Modified: `backend/analytics/reports/markdown_renderer.py` (added `render_experiment_report_markdown()`)
  - Modified: `backend/api/admin.py` (added `POST /admin/reports/generate-experiment` endpoint)
  - Modified: `backend/memory/store.py` (6 new M14 query methods: `get_all_series_for_experiment`, `get_earliest_run_timestamp_for_experiment`, `count_pre_experiment_foreign_mus`, `get_distinct_seed_series_ids`, `get_probe_session_indices_for_condition`, `get_condition_label_for_experiment`)
  - Created: `tests/test_analytics/test_experiment_report.py` (13 new tests)
  - Tests: 13 new, 269 total — all passing
  - Branch: milestone-14-experiment-report-nonlearning
  - Key decisions:
    - §5 aggregation re-queries the DB directly per-series rather than reading from prior series report files — always current, even for mid-experiment rollups
    - Outlier series in §5.3 flagged when intra-series rate differs from mean by >15 percentage points (configurable threshold)
    - §6.3 probe replay check links to experiment via `condition_label` (probe_results table has no experiment_id FK)
    - Graph-wipe check excludes NULL experiment_id MUs (pre-M12) — they're "unattributed", not foreign
    - `STATISTICAL_CAVEAT_TEXT` exported as a module-level constant so tests can assert verbatim inclusion
    - `learning_measures` and `control_shadow` keys present as `null` in JSON schema — stable for M15/M16 to fill in

- 2026-06-26: /build — Milestone 12 complete
  - Modified: `config.yaml` (adds `experiment.experiment_id: ""`)
  - Modified: `backend/gate/um.py` (`session_id` → `series_id` on UniversalMessage + create_um(); added `experiment_id: Optional[str]`)
  - Modified: `backend/analytics/run_log.py` (`session_id` → `series_id`; added `experiment_id` on RunRecord; `query_runs` param renamed)
  - Modified: `backend/memory/store.py` (`memory_units` table gains `series_id`, `experiment_id`, `cluster_id`; `analytics` table renames `session_id` → `series_id`, adds `experiment_id`; `_migrate_db` handles RENAME COLUMN; `get_session_turn_count` → `get_series_turn_count`; `create_mu` signature extended)
  - Modified: `backend/core/orchestrator.py` (`_resolve_experiment_id()` + `EXPERIMENT_ID` constant; full project rename `session_id` → `series_id` throughout; all MU + UM creation stamped with `series_id` + `experiment_id`)
  - Modified: `backend/analytics/seed_fixture.py` (seed MUs stamped with `series_id="seed:{version}"`, `experiment_id`, `cluster_id`)
  - Modified: `backend/api/chat.py` + `backend/api/admin.py` (`session_id` → `series_id` in request/response)
  - Modified: `docker-compose.yml` (adds `EXPERIMENT_ID` env var passthrough)
  - Modified: `frontend/src/api.js` + `frontend/src/components/ChatWindow.jsx` (series_id rename, "New Series" button)
  - Modified: `admin/src/api.js` + `admin/src/components/RunLogTable.jsx` (series_id rename, experiment_id in expanded detail)
  - Modified: `admin/src/components/MemoryInspector.jsx` (series_id, experiment_id, cluster_id in expanded MU detail)
  - Modified: `admin/src/components/ProbeResults.jsx` (placeholder clarified: "meas. session idx")
  - Updated: 7 existing test files (all `session_id` → `series_id`; SQL column renamed; `get_series_turn_count`)
  - Added: 17 new M12 tests (store, run_log, um, orchestrator)
  - Tests: 17 new, 235 total — all passing
  - Branch: milestone-12-series-id-prerequisite-gate
  - Key decisions:
    - EXPERIMENT_ID resolved at startup: env var → config.yaml → auto-generated `{condition_label}-{YYYY-MM-DD}` (logs WARNING if auto-generated)
    - SQLite RENAME COLUMN requires >= 3.25 (confirmed 3.38.2 in this environment)
    - `session.py` internal module NOT renamed — it stores UUIDs in `series_id` variables via the orchestrator
    - Seed MU registration is idempotent (duplicate on re-registration caught silently)

## Upcoming milestones

1. ~~**Foundation** — UM Gate + Graph Store + Session Management~~ ✅ COMPLETE
2. ~~**Memory Engine** — BM25 Retrieval + Traversal Funnel + Analyzer/Extractor~~ ✅ COMPLETE
3. ~~**Tool System** — Universal Tool Interface + File Tool + Analytics + Snapshots~~ ✅ COMPLETE
4. ~~**Learning** — Simple RL + Feedback Capture + Admin UI + Chat UI~~ ✅ COMPLETE
5. ~~**Chat UI Quality** — Token counter, copy button, New Session button~~ ✅ COMPLETE
6. ~~**Prompt Clarity** — Separate memory context from user message in Ollama call~~ ✅ COMPLETE
7. ~~**Multimodal Attachment Support** — Image attachments through the UM gate; binary transient only~~ ✅ COMPLETE
8. ~~**Core Instrumentation** — Retrieval decomposition, turn indexing, edge lifecycle, feedback-event log~~ ✅ COMPLETE
9. ~~**Experiment Framework** — Probe set, ground truth labels, seed fixture, battery snapshots~~ ✅ COMPLETE
10. ~~**Session Context Flag** — Config-driven flag to disable session history injection; off by default; per-run auditability in RunRecord and Admin UI~~ ✅ COMPLETE
11. ~~**Empty-Response Failure Detection** — Detect `finish_reason=length` + empty content; tag MU source_type with cause; thread `failure_reason` field through InteractionResult → ChatResponse → UI; `failure_reason` column in analytics; Admin UI failure indicator~~ ✅ COMPLETE
12. ~~**Series ID Prerequisite Gate** — Stamp `series_id` + `experiment_id` + `cluster_id` on `memory_units`; rename `session_id` → `series_id` project-wide; add `EXPERIMENT_ID` env var. Gates all reporting milestones.~~ ✅ COMPLETE
13. ~~**Series Report Generator (§5)** — All 8 series-report sections (failures, retrieval digest, intra/cross rate, cross-series edges, weight deltas, probe integrity, Mode-3 count, num_ctx pressure); JSON + Markdown output to `/app/memory/reports/`; admin endpoints.~~ ✅ COMPLETE
14. ~~**Experiment Report — Non-Learning Rollup (§5 aggregation + §6.3 + §6.4)** — §5 rolled up across all series; data-integrity / confound checks (seed provenance, graph-wipe, probe-replay-index normalization); statistical caveat text in template.~~ ✅ COMPLETE
15. ~~**Experiment Report — Learning Measures (§6.1)** — State-transition detection (fail→pass, pass→fail, flat); bridge-MU rank trajectory; cross-cluster transfer candidates; edge delta ledger with orphan-update check; saturation / runaway-reinforcement flags.~~ ✅ COMPLETE
16. ~~**Experiment Report — Control Shadow + Admin UI (§6.2 + dashboard)** — Control shadow embedded in learned-condition report; static/random control sanity checks; comparative deltas; Reports page with list, viewer, and comparison table.~~ ✅ COMPLETE

- 2026-06-27: /smoketest — Milestone 17 Phase 1 checks, 2 bugs found and fixed
  - Bugs found: 2
  - Bugs fixed: 2
  - Branch: fix/smoketest-2026-06-27
  - PR: https://github.com/PaulArthurMiller/local_corvus/pull/22
  - Fix 1: Python 3.12 strict `executescript()` blocked M12 migration — `_INIT_SQL` contained `CREATE INDEX idx_analytics_series ON analytics(series_id)` which fails on pre-M12 DBs (still have `session_id`). Python 3.12 made executescript raise on first failing statement, aborting `_open_db()` before `_migrate_db()` could run the RENAME COLUMN. Every admin endpoint returned 500 "no such column: series_id". Fixed by removing the index from `_INIT_SQL` and creating it in `_migrate_db()` outside the RENAME COLUMN try/except.
  - Fix 2: `memory/workspace/` and `memory/reports/` were missing from `.gitignore` — runtime files created by the file tool and report generator were being tracked by git.
  - Phase 1 status: All endpoints pass (/health, /ollama-health, all /admin/* tabs). Full pipeline verified: chat → tool use → feedback → weight updates. 315 tests passing.

- 2026-06-27: /smoketest — Milestone 17 Phase 2 (seed graph + probe setup), 4 gaps found and fixed
  - Gaps found: 4
  - Gaps fixed: 4
  - Branch: fix/smoketest-2026-06-27-2
  - PR: (see GitHub after push)
  - Fix 1: `POST /admin/seed-graph` was missing — added; reads `seed_fixtures/seed_{version}.json.gz`, calls `register_fixture()`, stamps all seed MUs with `series_id="seed:v1"` and `cluster_id`.
  - Fix 2: `GET /admin/graph/stats` was missing — added; returns `mu_count`, `mu_with_cluster_id`, `mu_without_cluster_id`, and `cluster_distribution`. Confirmed all 6 seed MUs have cluster_id.
  - Fix 3: `POST /admin/probes/snapshot` was missing — added; calls `take_battery_snapshot(battery_index, phase, condition_label)`. Confirmed snapshot file written at `battery_0_pre_seed_*.json.gz`.
  - Fix 4: `probe_set_v1.yaml` was an empty template — filled in all 5 probes with prompt text and gold_mu_ids keyed to seed fixture v1 MU IDs. `seed_fixtures/seed_v1.json.gz` created with 6 MUs: cluster_a×3, cluster_b×2, bridge_ab×1.
  - Config: `pass_rank_threshold` corrected from 3 to 1 (M15 deferred risk — K2=1 means at most 4 MUs retrieved; threshold=1 makes gold-rank-1 the meaningful pass criterion).
  - Probe baseline results (session_index=0, condition=seed, graph contaminated by Phase 1 MUs):
    - p01 (ranking_improvement, file_write): FAIL — Phase-1 MUs outscored seed MU; expected in polluted graph; will pass on clean graph
    - p02 (ranking_improvement, BM25 concepts): PASS rank=1
    - p03 (cross_cluster_transfer, bridge): PASS rank=1
    - p04 (cross_cluster_transfer, bridge): PASS rank=1
    - p05 (process_rule_adherence): PASS rank=1
  - Note: 47 non-seed MUs from Phase 1 lack cluster_id (NULL). These are regular interaction MUs. Cross-cluster analysis will correctly exclude them (they have no cluster_id). Real experiment conditions start from a wiped graph + seed fixture only.
  - Phase 2 status: All Phase 2 steps verified. Seed graph operational. Probe machinery confirmed working. 315 tests still passing.

- 2026-06-27: /smoketest — Milestone 17 Phase 3 (three-condition pilot), 2 bugs found and fixed
  - Bugs found: 2
  - Bugs fixed: 2
  - Branch: fix/smoketest-2026-06-27-3
  - PR: (see GitHub after push)
  - Fix 1: `POST /admin/edges/randomize` was missing — added to admin.py; `GraphStore.randomize_all_edge_weights()` added to store.py. Required for the random-weight control condition: sets all edge weights to uniform random [0, 1] before each series. Edge counts grew correctly (179 → 209 → 239 across 3 series).
  - Fix 2: `generate_experiment_report()` read `condition_label` from the CURRENT runtime config.yaml instead of querying the DB. In a multi-condition session (or when reports are generated after a config switch), all experiment report headers incorrectly showed the active condition rather than the experiment's actual condition. Fixed: now queries `get_condition_label_for_experiment()` first; falls back to config.yaml only when no runs exist.
  - Pilot results (3 series × 5 turns per condition, shared graph — no wipe between conditions):
    - Learned condition (exp-learned-2026-06-27): 15/15 turns PASS, 15 feedback events (+1), edges_updated active (0–8 per turn), 3/3 series reports generated
    - Static-weight control (exp-static-2026-06-27): 15/15 turns PASS, no feedback, 3/3 series reports generated
    - Random-weight control (exp-random-2026-06-27): 15/15 turns PASS, 3 edge randomizations (179/209/239 edges), no feedback, 3/3 series reports generated
  - Control shadow (all 4 sanity checks PASS on real data):
    - `zero_weight_changes` for static control: PASS (0 runs with weight_updates)
    - `zero_feedback_edge_correlation` for static control: PASS
    - `weights_rerandomized` for random control: PASS (std_dev > 0.05 floor)
    - `zero_feedback_edge_correlation` for random control: PASS
  - Probe results: all probes FAIL at session_index=2 (after pilot turns) because the shared graph is contaminated — interaction MUs from prior turns outrank seed MUs. Expected: real experiments must start from a wiped graph + re-seeded state per condition.
  - Phase 3 status: All pilot machinery verified end-to-end. 315 tests passing. Config restored to neutral (experiment_id="", condition_label="learning").

- 2026-06-28: /smoketest — Milestone 17 Phase 4 (full report generation and validation), 0 bugs found
  - Bugs found: 0
  - Bugs fixed: 0
  - Branch: fix/smoketest-2026-06-28
  - Phase 4 checks (all PASS):
    - Check 1: `POST /admin/reports/generate-experiment` for learned condition → `learning_measures` not null (11 keys populated: state_transitions, edge_delta_ledger, bridge_mu_analysis, saturation_analysis, etc.)
    - Check 2: Same endpoint with `control_experiment_ids: {static_weight: ..., random_weight: ...}` → `control_shadow` populated with both controls. Note: UI and code use `static_weight` / `random_weight` as key names (not `static` / `random`).
    - Check 3: All 4 sanity checks PASS — `zero_weight_changes` (static_weight, 0 violations), `zero_feedback_edge_correlation` (static_weight, 0 violations), `weights_rerandomized` (random_weight, std_dev=0.281, floor=0.05), `zero_feedback_edge_correlation` (random_weight, 0 violations).
    - Check 4: Admin UI Reports tab verified via API + code inspection: 17 reports appear in list (8 experiment + 9 series), Markdown endpoint returns full content with all sections (§5.1/§5.3/§5.5–§5.8, §6.1, §6.2, §6.3, §6.4), ↓JSON download implemented via Blob in ReportsList.jsx, ExperimentComparison.jsx reads control_shadow and renders comparative deltas. Browser extension unavailable — visual confirmation to be done by user.
    - Check 5: GFM table rendering — `remark-gfm` v4.0.1 installed in admin/package.json and wired up in ReportViewer.jsx with `remarkPlugins={[remarkGfm]}`. Markdown report contains 48 GFM table rows. M16 risk resolved.
  - Phase 4 status: All report generation machinery verified end-to-end. 315 tests passing. No code changes required.

- 2026-06-28: /smoketest — Milestone 17 Phase 5 (integration test suite), 3 gaps fixed
  - Gaps found: 3
  - Gaps fixed: 3
  - Branch: fix/smoketest-2026-06-28-2
  - PR: https://github.com/PaulArthurMiller/local_corvus/pull/26
  - Fix 1: `probe_pass_rates` missing from `learning_measures` — BUILD-PLAN §6.1 required it; added to `_section_61_learning_measures()` in `experiment_report.py`. Computed from `state_transitions.per_probe`, fraction of non-flagged outcomes that are "pass", rounded to 4 decimal places.
  - Fix 2: `tests/integration/test_e2e_pipeline.py` — created full integration test suite with 5 `pytest.mark.integration` tests using stdlib urllib only (no extra deps). Tests call real HTTP endpoints at `localhost:8000`. Tests 3 and 4 correctly call `GET /admin/reports/{report_id}` after the generate POST (the generate endpoints return only metadata). `_find_experiment_ids()` reads condition_label from `/admin/runs` (ground truth) not stale report metadata.
  - Fix 3: `pytest.ini` — added `--ignore=tests/integration` and registered `integration` marker so integration tests are excluded from the default `pytest` run.
  - Phase 5 integration test results (5/5 PASS):
    - test_chat_turn_creates_memory_units: PASS (run_id, input_mu_id, output_mu_id all distinct, run visible in /admin/runs)
    - test_feedback_event_triggers_weight_update: PASS (12 weight_updates returned, each with feedback_event_id; learning-condition run used to avoid contaminating control sanity checks)
    - test_series_report_generates_all_sections: PASS (all 8 §5 section keys present and non-null)
    - test_experiment_report_learning_measures_populated: PASS (probe_pass_rates + state_transitions present in learning_measures)
    - test_control_shadow_sanity_checks_pass: PASS (zero_weight_changes, zero_feedback_edge_correlation for static; weights_rerandomized, zero_feedback_edge_correlation for random — all PASS)
  - Note: test 2 was initially contaminating the random-weight control by sending feedback to a run in exp-random-2026-06-27 (selecting the first run with activated_edge_ids). Fixed by filtering to learning-condition runs only. The contaminating feedback_events row and weight_updates were cleaned up from the DB directly.
  - Phase 5 status: All 5 integration tests passing. 315 unit tests still passing. No regressions.

- 2026-06-28: /smoketest — Milestone 17 Phase 6 (hardening + documentation), 1 gap fixed
  - Gaps found: 1 (Docker healthcheck reflected process-up only, not DB availability)
  - Changes made: 3
  - Branch: fix/smoketest-2026-06-28-3
  - Change 1: Docker healthcheck — `GET /health` now executes a lightweight `SELECT 1` via a new `GraphStore.ping()` method. Returns `{"status":"ok","db":"ok"}` when the DB is reachable and `{"status":"degraded","db":"error"}` when it is not. Dockerfile `HEALTHCHECK` instruction added (30 s interval, 10 s timeout, 15 s start period, 3 retries). `docker-compose.yml` frontend/admin now use `condition: service_healthy` so they wait for a confirmed healthy backend before starting.
  - Change 2: `config.yaml` — comprehensive inline comments added to all hyperparameters. Each entry now documents: what the parameter does, the default value and its rationale, valid range, and any open questions or deferred decisions. File is now self-explaining as a research artifact.
  - Change 3: `docs/experiment_protocol.md` (new) — step-by-step runbook for running a three-condition experiment from scratch. Covers: environment setup and healthcheck verification; graph wipe and seed fixture registration; baseline probe battery; learned / static-weight / random-weight condition runs (with exact API calls and expected outputs); report generation; integration test validation; quick-reference endpoint table; troubleshooting guide for common failure modes.
  - Phase 6 status: All hardening and documentation tasks complete. 315 unit tests still passing. No regressions.

## Milestone 17 — Research Outcomes and Open Questions

### Probe pass-rates across conditions (pilot data, shared graph — not clean-condition results)

All probe results below were collected on a shared graph contaminated by interaction
MUs from prior phases. These are machinery-validation results, not experiment results.
A real experiment must start from a wiped graph + re-seeded state per condition.

- **Pilot (3 series × 5 turns, learned condition):** 15/15 turns PASS; feedback active;
  3–8 edges updated per turn; 3/3 series reports generated successfully.
- **Static-weight control:** 15/15 turns PASS; 0 weight updates confirmed; 3/3 series reports.
- **Random-weight control:** 15/15 turns PASS; edge variance std_dev=0.281 (floor=0.05 PASS);
  3/3 series reports.
- **Baseline probe battery (session_index=0, seed graph + Phase 1 MUs):** p01 FAIL
  (Phase-1 MUs outscored seed MU — expected on polluted graph); p02–p05 all PASS rank=1.
- **Post-pilot probe battery (session_index=2, polluted graph):** all 5 probes FAIL —
  interaction MUs dominate the retrieved set. Expected. Confirms clean-graph requirement.

### Anomalies observed

1. **Python 3.12 strict `executescript()` (Phase 1):** `CREATE INDEX` on a non-existent
   column aborted `_open_db()` before `_migrate_db()` could rename it. Fixed by moving
   the index creation to `_migrate_db()` outside the RENAME COLUMN try/except block.
2. **`generate_experiment_report()` reading condition_label from live config (Phase 3):**
   Multi-condition sessions showed the active condition, not the experiment's actual condition,
   in report headers. Fixed by querying `get_condition_label_for_experiment()` first.
3. **Integration test contaminating random-weight control (Phase 5):** Test 2 was selecting
   the first run with `activated_edge_ids` regardless of condition, sending feedback to
   a random-weight run and poisoning the `zero_feedback_edge_correlation` sanity check.
   Fixed by filtering to learning-condition runs only.

### Open questions for future work

1. **Full three-condition experiment:** The pilot used a shared graph and 3 series × 5 turns.
   A real experiment requires separate clean graphs per condition and at least 5 series × 10 turns
   each with probe batteries at start/mid/end. The `docs/experiment_protocol.md` describes
   the exact procedure.
2. **Weight initialization value (open since M1):** Flat 0.5 neutral prior. After the first
   real learning run, compare probe pass-rate trajectories against initial_edge_weight ∈ {0.1, 0.5, 0.9}.
3. **Decay function (open since M1):** `decay_rate: 0.01` is defined but the decay trigger
   is not yet implemented (deferred to Phase 2). Without decay, high-activation MUs accumulate
   weight indefinitely — the saturation check (threshold=0.9) is the current guard.
4. **RL reward distribution (resolved as uniform, open for improvement):** All activated edges
   receive the same delta. Backpropagation-style decay from the output MU would more accurately
   attribute learning to causal edges.
5. **Cross-cluster transfer signal:** The bridge-MU rank trajectory section requires probe
   batteries across multiple measurement sessions with a clean graph. The pilot did not produce
   meaningful transfer signal — graph contamination dominated retrieval.
6. **multimodal (Ollama regression, M7):** Ollama 0.30.x on Windows has a regression where
   gemma4:e4b decodes image tokens but does not interpret image content. Monitor the Ollama
   changelog for a fix to GitHub Issue #16532 before using attachment features in experiments.
7. **Output-compliance grading for `process_rule_adherence` probes:** The current probe pass
   criterion checks only retrieval rank (gold MU appears in top-K). It does not verify whether
   the agent's generated response actually follows the process rule described in the seed MU.
   A full compliance check would require a judge/evaluator step (LLM-as-judge or structured
   output parsing) that goes beyond rank-checking. Deferred as a candidate for a future
   automation-focused milestone — not addressed in M18.

- 2026-06-28: /build — Milestone 18 Phase 0 complete (infrastructure fixes)
  - Modified: `backend/analytics/seed_fixture.py` (Phase 0.1: `register_fixture()` now inserts edges from fixture; `SeedFixtureManifest` gains `edge_count: int = 0`; idempotent — duplicate edges are skipped with DEBUG log)
  - Modified: `backend/api/admin.py` (Phase 0.2: `RunProbeRequest` gains `probe_set_version: str = "v1"`; hardcoded "v1" replaced with `body.probe_set_version` in `run_probe`)
  - Modified: `backend/main.py` (Phase 0.3: startup probe sync now loops over ["v1", "v2"]; missing yaml logs INFO and continues; exceptions log WARNING and continue)
  - Modified: `config.yaml` (Phase 0.4: added `reporting.pass_rank_thresholds` dict with per-phenomenon overrides: `cross_cluster_transfer: 4`, `ranking_improvement: 1`, `process_rule_adherence: 1`)
  - Modified: `backend/analytics/reports/experiment_report.py` (Phase 0.4: `_classify_probe_outcome` gains `phenomenon_type` and `phenomenon_thresholds` optional params; `_compute_state_transitions` and `_compute_mus_by_probe_outcome` thread phenomenon-aware thresholds; `_section_61_learning_measures` reads and stores `phenomenon_thresholds` in return dict)
  - Modified: `backend/analytics/reports/control_shadow.py` (Phase 0.4: `_final_pass_rate` uses `_classify_probe_outcome` with thresholds from measures dict)
  - Modified: `backend/analytics/reports/markdown_renderer.py` (Phase 0.4: §6.1 header shows per-phenomenon thresholds when `phenomenon_thresholds` is non-empty)
  - Modified: `.gitignore` (added `seed_fixtures/*_draft.json` and `scripts/__pycache__/`)
  - Added: `tests/test_analytics/test_seed_fixture.py` extensions (3 new tests: edge insertion, edge_count=0 for no edges, idempotent duplicate)
  - Added: `tests/test_api/test_run_probe_versioned.py` (5 new tests: default version, v2 accepted, FileNotFoundError for missing version, v1 loads for v1, v2 loads for v2)
  - Added: `tests/test_startup/test_probe_sync.py` (4 new tests: both synced, missing v2, missing v1, exception non-fatal)
  - Added: `tests/test_analytics/test_learning_measures.py` extensions (6 new tests: phenomenon-aware classify — cross_cluster pass/fail, ranking_improvement override, fallback to default, gold_mu_rank untouched)
  - Tests: 17 new, 332 total — all passing
  - Branch: milestone-18-phase-0-infra-fixes
  - Gap A (cross_cluster_transfer pass criterion): RESOLVED — per-phenomenon threshold config added; cross_cluster_transfer uses threshold=4 (K1+K2); raw gold_mu_rank never modified
  - Gap B (process_rule_adherence output compliance): DOCUMENTED ONLY — noted in probe YAML comments; no code change needed

- 2026-06-28: /build — Milestone 18 Phase 1 complete (seed_v2 content generation script)
  - Created: `scripts/generate_seed_v2.py` — generates 34 MU contents via Gemma 4 (Ollama localhost:11434), assigns fixed UUIDs, wires 32 hub-and-spoke + bridge edges, writes `seed_fixtures/seed_v2_draft.json` (DRAFT — not committed)
  - `seed_fixtures/seed_v2_draft.json` written and awaiting Paul's review (excluded from git by .gitignore)
  - Script run successfully: 34 MUs + 32 edges generated; 1 retry (c_spoke1 missing 'enforce' on first attempt)
  - Validation: bridge_ab and bridge_cd forbidden-vocabulary checks PASS; hub MUs all contain all 5 cluster fingerprint terms; spoke MUs contain 3-5 of 5 fingerprints (BUILD-PLAN spec: "most MUs")
  - Tests: 332 existing tests — all passing; no new tests (script is a standalone dev tool, not part of the test suite)
  - Branch: milestone-18-phase-1-seed-v2-generation
  - Next step: Paul reviews seed_fixtures/seed_v2_draft.json content, approves or requests rewrites, then freezes to seed_v2.json.gz for Phase 3 verification

- 2026-06-28: M18 Phase 2 complete — Paul reviewed seed_v2_draft.json and froze to seed_fixtures/seed_v2.json.gz (commit 951d69f)

- 2026-06-29: /smoketest — Milestone 18 Phase 3 (verification script), 0 bugs found
  - Bugs found: 0
  - Gaps fixed: 0 (all verification checks passed on first run)
  - Branch: fix/smoketest-2026-06-29
  - Created: `scripts/verify_seed_v2.py` — 5-check verification script against the frozen seed_v2.json.gz
  - Phase 3 verification results (all PASS against seed_fixtures/seed_v2.json.gz):
    - Check 1 — MU count: 34 MUs, cluster_distribution exactly as specified (cluster_a/b/c/d×8 each, bridge_ab/cd×1 each): PASS
    - Check 2 — Edge wiring: 32 edges; each hub has 7 incoming spoke->hub edges and 1 outgoing hub->bridge edge; no edges between {a,b} group and {c,d} group: PASS
    - Check 3 — Connectivity: hub->bridge edge confirmed for all 4 clusters (cluster_a->bridge_ab, cluster_b->bridge_ab, cluster_c->bridge_cd, cluster_d->bridge_cd): PASS
    - Check 4 — Lexical-distinctness: bridge_ab contains none of CLUSTER_A_FP+CLUSTER_B_FP; bridge_cd contains none of CLUSTER_C_FP+CLUSTER_D_FP: PASS
    - Check 5 — BM25 probe sanity (simulated retrieval): for all 4 cross-cluster queries (p10–p13), K1=[3 in-cluster MUs], K2=[correct bridge]; bridge never at K1: PASS
  - Tests: 332 existing tests — all passing; no regressions
  - Note: seed_v2.json.gz is verified and ready for Phase 4 (probe_set_v2.yaml)

- 2026-06-29: /smoketest — Milestone 18 Phase 4 (probe set + final verification), 0 bugs found
  - Bugs found: 0
  - Gaps fixed: 0 (all 7 verification checks passed; 2 probe texts required BM25-driven revision before PASS)
  - Branch: fix/smoketest-2026-06-29
  - Created: `probes/probe_set_v2.yaml` — 10 probes (p06–p15); prompt_text filled in from seed_v2 MU content
  - Probe text design notes:
    - RETRIEVAL_KEYWORD_COUNT=5 means only the first 5 non-stopword tokens are used as BM25 query keywords
    - p08 initial text had 'what, relationship, between, system, rule' as top-5 tokens → hub only 1 hit, not in multi-hit pool
    - p12 initial text had 'how, system, rule, function, as' as top-5 tokens → hub not in multi-hit pool, bridge unreachable
    - Both fixed by leading with cluster_c fingerprint terms: p08 uses 'every, rule, exists, enforce, constraint'; p12 uses 'why, every, rule, enforce, constraint'
    - After fix: both probes achieve correct BM25 retrieval with hub dominant in K1
  - Phase 4 verification results (all 7 checks PASS, against freshly wiped + v2-seeded graph):
    - Check 1: POST /admin/seed-graph {"version":"v2"} → mu_count=34, edge_count=32: PASS
    - Check 2: GET /admin/graph/stats → {cluster_a:8, cluster_b:8, cluster_c:8, cluster_d:8, bridge_ab:1, bridge_cd:1}, mu_without_cluster_id=0: PASS
    - Check 3: Connectivity — hub→bridge edges confirmed for all 4 clusters (cluster_a→bridge_ab, cluster_b→bridge_ab, cluster_c→bridge_cd, cluster_d→bridge_cd): PASS
    - Check 4: Lexical-distinctness — bridge_ab contains none of CLUSTER_A_FP+CLUSTER_B_FP; bridge_cd contains none of CLUSTER_C_FP+CLUSTER_D_FP (confirmed in Phase 3; no new violations): PASS
    - Check 5a: Baseline probe run (session_index=0, clean v2 graph, ranking_improvement probes p06-p09): all PASS rank=1
      - p06 (cluster_a hub): rank=1, retrieved=[a0000001, ab000001, a0000006] ✓
      - p07 (cluster_b hub): rank=1, retrieved=[b0000001, b0000005, b0000004, ab000001] ✓
      - p08 (cluster_c hub): rank=1, retrieved=[c0000001, c0000007, c0000008, cd000001] ✓
      - p09 (cluster_d hub): rank=1, retrieved=[d0000001, a0000001, c0000001, cd000001] ✓
    - Check 5b: Baseline probe run (cross_cluster_transfer probes p10-p13): bridge at rank 4 (K2), NOT rank 1
      - p10 (cluster_a vocab, gold=bridge_ab): rank=4, K1=[a0000001, a0000005, a0000004], K2=[ab000001] ✓
      - p11 (cluster_b vocab, gold=bridge_ab): rank=4, K1=[b0000001, b0000005, b0000004], K2=[ab000001] ✓
      - p12 (cluster_c vocab, gold=bridge_cd): rank=4, K1=[c0000001, c0000007, c0000008], K2=[cd000001] ✓
      - p13 (cluster_d vocab, gold=bridge_cd): rank=4, K1=[d0000004, d0000001, a0000001], K2=[cd000001] ✓
    - Check 5c: Process rule adherence probes p14-p15: PASS rank=1
      - p14 (idempotency rule, c0000003): rank=1, retrieved=[c0000003, c0000007, c0000004, c0000001] ✓
      - p15 (multi-tenant access control, c0000005): rank=1, retrieved=[c0000005, c0000001] ✓
    - Check 6: Idempotency — second POST /admin/seed-graph {"version":"v2"} → mu_count=34 (no duplicates): PASS
    - Check 7: probe_set_v2.yaml loads at startup with no errors; 10 probes synced; all visible in admin (GET /admin/probe-results returns 20 results for p06-p15): PASS
  - Tests: 332 unit tests — all passing; no regressions
  - Gap A: RESOLVED (Phase 0.4) — cross_cluster_transfer uses threshold=4 (bridge structurally at K2/rank 4)
  - Gap B: DOCUMENTED in probe_set_v2.yaml comments — process_rule_adherence output compliance requires human review

- 2026-06-29: /smoketest — Milestone 18 Final Verification (all 7 checks, freshly wiped + re-seeded graph)
  - Bugs found: 0
  - Branch: fix/smoketest-2026-06-29
  - Setup: Deleted memory/graph.db, started backend fresh, backend health confirmed ({"status":"ok","db":"ok"})
  - Final verification results (all 7 checks PASS against freshly wiped + v2-seeded graph):
    - Check 1: POST /admin/seed-graph {"version":"v2"} → mu_count=34, edge_count=32: PASS
    - Check 2: GET /admin/graph/stats → {cluster_a:8, cluster_b:8, cluster_c:8, cluster_d:8, bridge_ab:1, bridge_cd:1}, mu_without_cluster_id=0: PASS
    - Check 3: Connectivity — hub→bridge edges confirmed for all 4 clusters (cluster_a→bridge_ab, cluster_b→bridge_ab, cluster_c→bridge_cd, cluster_d→bridge_cd): PASS
    - Check 4: Lexical-distinctness — bridge_ab contains none of CLUSTER_A_FP+CLUSTER_B_FP; bridge_cd contains none of CLUSTER_C_FP+CLUSTER_D_FP (verify_seed_v2.py PASS): PASS
    - Check 5a: Baseline probe run (session_index=0, clean v2 graph, ranking_improvement probes p06-p09): all PASS rank=1
      - p06 (cluster_a hub): rank=1, retrieved=[a0000001, ab000001, a0000006] ✓
      - p07 (cluster_b hub): rank=1, retrieved=[b0000001, b0000005, b0000004, ab000001] ✓
      - p08 (cluster_c hub): rank=1, retrieved=[c0000001, c0000007, c0000008, cd000001] ✓
      - p09 (cluster_d hub): rank=1, retrieved=[d0000001, a0000001, c0000001, cd000001] ✓
    - Check 5b: Baseline probe run (cross_cluster_transfer probes p10-p13): bridge at rank=4 (K2), NOT rank 1
      - p10 (cluster_a vocab, gold=bridge_ab): rank=4, K1=[a0000001, a0000005, a0000004], K2=[ab000001] ✓
      - p11 (cluster_b vocab, gold=bridge_ab): rank=4, K1=[b0000001, b0000005, b0000004], K2=[ab000001] ✓
      - p12 (cluster_c vocab, gold=bridge_cd): rank=4, K1=[c0000001, c0000007, c0000008], K2=[cd000001] ✓
      - p13 (cluster_d vocab, gold=bridge_cd): rank=4, K1=[d0000004, d0000001, a0000001], K2=[cd000001] ✓
    - Check 5c: Process rule adherence probes p14-p15: PASS rank=1
      - p14 (idempotency rule, c0000003): rank=1, retrieved=[c0000003, c0000007, c0000004, c0000001] ✓
      - p15 (multi-tenant access control, c0000005): rank=1, retrieved=[c0000005, c0000001] ✓
    - Check 6: Idempotency — second POST /admin/seed-graph {"version":"v2"} → mu_count=34 (no duplicates): PASS
    - Check 7: probe_set_v2.yaml loads at startup with no errors; 10 probes synced; GET /admin/probe-results returns 10 results for p06-p15: PASS
  - Tests: 332 unit tests — all passing; no regressions
  - Milestone 18 status: COMPLETE — all Done criteria met (seed_v2.json.gz committed, probe_set_v2.yaml committed, all 7 checks PASS with actual numbers, full test suite passing)

- 2026-07-03: /build — Milestone 19 Phase 0 complete (Experiment Harness: Run Plan Schema + Skeleton + Dry-Run Mode)
  - Created: `scripts/harness/__init__.py`, `scripts/harness/schema.py` (RunPlan pydantic model + all sub-schemas: ConceptualFields, DifficultyConfig, JudgeConfig, NotificationConfig; `format_validation_error()` for field-identifying CLI errors)
  - Created: `scripts/harness/turn_resolution.py` (`resolve_turn_sequence()` — deterministic probe/bulk resolution from probe_ratio + probe_placement + random_seed; supports `random`/`end`/`fixed_checkpoints`)
  - Created: `scripts/harness/manifest.py` (`compute_config_hash()`, `compute_run_id()` — deterministic run_id from plan_id + hash prefix, enabling resume; `RunManifest` persistence)
  - Created: `scripts/harness/checkpoint.py` (`CheckpointState`/`TurnCheckpoint` — atomic per-turn checkpoint writes via temp-file + `os.replace`; `is_turn_done()` for resume-skip logic)
  - Created: `scripts/harness/notify.py` (`notify()` stub — console + JSONL file, no network calls; `build_notification_payload()` for the `{run_id, status, error_summary, report_path}` shape)
  - Created: `scripts/harness/client.py` (`BackendClient` — httpx wrapper distinguishing `BackendConnectivityError` (hard-stop default) from `BackendTurnError` (soft-continue default); chat calls get a separate 120s timeout since Ollama inference can exceed a generic HTTP timeout on cold model loads)
  - Created: `scripts/harness/elicit.py` (`elicit_plan()` — full interactive walkthrough of every schema field with defaults, injectable `input_fn` for testing; `offer_to_save_plan()`)
  - Created: `scripts/harness/runner.py` (`execute_run()` — the testable core orchestration: manifest → resume detection → per-condition wipe+seed → turn loop → checkpoint → error policy → notify; injectable `BackendClient` and `resume_input_fn` so it's fully unit-testable without Docker)
  - Created: `scripts/run_experiment_harness.py` (CLI entrypoint: `--plan`, `--dry-run`, `--backend-url`; loads-or-elicits, validates, delegates to `execute_run()`)
  - Created: `tests/test_harness/` (44 tests: schema validation, turn-resolution determinism, elicitation with scripted input, checkpoint persistence/interrupt-simulation, and runner orchestration against a fake `BackendClient` — hard-stop, soft-continue, error-policy override, resume/decline-resume, wipe-before-seed ordering, probe placeholder content)
  - Modified: `.gitignore` (added `harness_runs/` — runtime manifests/checkpoints/notification logs)
  - Tests: 44 new (harness), 376 total at that point — all passing

  **Gap found and closed: no real "wipe" existed.** The plan's own text (§1, §3.2) assumed `POST /admin/seed-graph` already performed a combined wipe+seed. It doesn't — `register_fixture()` only ever does an idempotent merge (duplicate MUs/edges are silently skipped on re-registration; confirmed via M17/M18's own idempotency checks). The only prior "wipe" mechanism was manually deleting `memory/graph.db` and restarting the container (see M18 final-verification log, 2026-06-29). Without a real wipe, every condition in the future three-condition pilot would inherit MUs/edges from the previous condition — exactly the cross-condition graph contamination already flagged as a known issue in the M17 research-outcomes section. Asked Paul how to proceed; decision: **add a real wipe endpoint, with automatic pre-wipe archiving keyed by experiment_id** (not silent, irreversible deletion).
  - Modified: `backend/memory/store.py` — added `GraphStore.wipe_graph()`: deletes all rows from `edges` and `memory_units`, returns `{mus_deleted, edges_deleted}`. Leaves `analytics`, `probe_results`, `feedback_events`, `generated_reports`, `seed_fixture_manifest` untouched — the experimental audit trail survives a graph wipe.
  - Modified: `backend/memory/snapshot.py` — added `take_archive_snapshot(experiment_id)`: dumps the full pre-wipe graph to `archive_{experiment_id}_{ts}.json.gz` (sanitizes unsafe filename characters; `experiment_id=None` → `unlabeled`).
  - Modified: `backend/api/admin.py` — added `POST /admin/graph/wipe` (`WipeGraphRequest{experiment_id}`): archives first, then wipes; if archiving fails, the wipe is aborted (never wipes without a successful archive first) — verified by test.
  - Modified: `scripts/harness/client.py` / `scripts/harness/runner.py` — harness now calls `wipe_graph(experiment_id=f"{plan.plan_id}_{condition}")` before `seed_graph()` for every condition.
  - Added: 11 more tests (3 in `tests/test_memory/test_store.py::TestWipeGraph`, 4 in `tests/test_memory/test_snapshot.py` archive tests, 4 in new `tests/test_api/test_graph_wipe.py`) plus 2 more in `tests/test_harness/test_runner.py` (wipe-before-seed ordering, hard-stop-when-wipe-fails) — 55 new tests total this milestone.
  - Incidental fix while editing `tests/test_memory/test_store.py`: an `Edit` call misjudged the file's true last line (a truncated `Read` hid it) and orphaned the migration test's final assertion outside its function body — caught immediately by the failing test, corrected in the same session.
  - Tests: 55 new, 387 total — all passing. Integration suite (`tests/integration`, requires live Docker stack): 4 passed, 1 skipped (unrelated — no random-weight control experiment_id present this session), consistent with prior runs.

  **§3.4 verification protocol — all 7 checks PASS, run against the live Docker backend + real Ollama (gemma4:e4b) at `localhost:8000` / `localhost:11434`:**
  1. **Interactive elicitation → save → confirm match:** ran `python scripts/run_experiment_harness.py` with no `--plan`, scripted stdin answers through every field. Saved plan JSON matches every entered value exactly (plan_id, plan_name, conceptual_fields, series_count=1, turns_per_series=2, probe_ratio=0.0, hop_distance_levels=[0,1], judge fields, conditions=["learned"], notification.enabled=false, dry_run=false, random_seed=99). PASS.
  2. **Valid plan file loads cleanly, hash logged:** `--plan check2_plan.json` → `Plan loaded: plan_id='verify-check2' config_hash=b5bfe43aef34d4f31ec4e08a07e84fd2c1c39211f6b49aac607dcdaeeaa8a3c7`. PASS.
  3. **Malformed plan fails fast:** plan with `conditions=["bogus_condition"]`, `probe_placement="sometimes"` → exits 1 with two specific, field-identifying errors (`probe_placement: ... must be one of (...)`, `conditions: ... must be from (...)`) and zero HTTP calls made (graph untouched). PASS.
  4. **Dry-run scoping (real backend):** plan with `series_count=2, turns_per_series=5, conditions=["learned","static_weight"], dry_run=true, dry_run_n=2` → manifest's `resolved_turn_sequence` holds the full 10 turns (audit trail), but only 2 turns actually executed, only under `=== Condition: learned ===` (static_weight never ran). Checkpoint: exactly 2 entries, both `status="ok"`, `final_status="complete"`. `notifications.log`: `{"event": "harness_run_complete", "run_id": "verify-check4_cf96fb09c5da", "status": "complete", "error_summary": null, "report_path": null}`. PASS.
  5. **Simulated hard-stop:** `--backend-url http://localhost:59999` (unreachable) → `ABORTED: connectivity: health check failed: [WinError 10061] No connection could be made because the target machine actively refused it`, exit code 1. `checkpoint.json`: `{"turns": [], "final_status": "aborted"}`; `manifest.json` present. Clean abort before any graph mutation (health check runs before wipe/seed). PASS.
  6. **Simulated soft-continue:** `tests/test_harness/test_runner.py::test_soft_continue_on_ambiguous_turn_result_logs_and_proceeds -v -s` → turn checkpoints `[(0, 'ok', 'run-0'), (1, 'error_soft_continue', 'missing run_id'), (2, 'ok', 'run-2')]`, final status `complete` — the run logs the ambiguous turn and proceeds rather than aborting. PASS.
  7. **Full test suite:** 387 passed, 0 failed, no regressions (confirmed both before and after the wipe-gap fix). PASS.

  - Side effect of live verification: the shared dev graph now has 34 seed MUs + 9 interaction MUs from verification chat turns (expected — every wipe archived the prior state first; archives at `memory/snapshots/archive_*.json.gz`). Nothing was lost; re-seed or wipe again at any time.
  - Key decisions:
    - `error_policy: dict[str, str]` added to the RunPlan schema as a minor, documented deviation from the original field list — required by §3.2's "must be overridable from the run plan" language, which named the mechanism but not a schema field for it.
    - `run_id` is deterministic (`{plan_id}_{config_hash[:12]}`), not random — re-running the same plan file naturally resumes the same checkpoint; a materially different plan gets a fresh run directory rather than silently colliding with an unrelated one.
    - A checkpoint is resumable whenever `final_status != "complete"` (covers both a crash mid-run with no final_status, and a previously aborted run) — only a fully complete run has nothing left to do.
    - Turn content in Phase 0 is placeholder-only by design: probe turns cycle through `probe_set_v2.yaml` prompts; bulk turns get a labeled stub message. Real bulk-prompt generation is Phase 1's job.
  - Branch: `milestone-19-phase-0-harness-skeleton`
  - Milestone 19 Phase 0 status: COMPLETE — all Done criteria met (all deliverables built, all 7 §3.4 checks PASS with real recorded output, no regressions, wipe-gap closed with tests)

- 2026-07-05: /build — Milestone 19 Phase 1 complete (Prompt Bank Generation + Validation)
  - Created: `scripts/harness/prompt_bank.py` — `PromptBankEntry` (criteria constrained to 1-3 via pydantic `Field(min_length=1, max_length=3)`), `PromptBank` (+`validated_entries()`), `compute_bank_hash()`, `bank_path_for_hash()`, `save_bank()`/`load_bank()` (field-identifying validation errors, reusing `schema.format_validation_error()`)
  - Created: `scripts/harness/prompt_gen.py` — the only file that talks to the Anthropic API:
    - `read_cluster_content()` reads `seed_fixtures/seed_v2.json.gz` directly (host-side, no backend dependency); imports `CLUSTER_A_FP..D_FP` and `MU_IDS` straight from `generate_seed_v2.py` (no copy — single source of truth)
    - `fingerprint_scan()` — pure-Python lexical-distinctness check, whole-word matching
    - `distribute_counts()` / `distribute_hop_levels()` — deterministic round-robin distribution across the 4 clusters and hop-distance levels
    - `generate_cluster_prompts_safely()` — 1 Sonnet call per cluster; malformed JSON retries the batch call once, then falls back to per-prompt generation so one bad response never loses the whole cluster
    - `_finalize_entry()` — per-prompt validation (fingerprint scan + a second Sonnet coherence call) with up to `prompt_gen.max_retries` regenerations; persistent failures are excluded with a specific `exclusion_reason`, never silently dropped
    - `estimate_generation_cost()` / `confirm_cost()` — pure local token-count heuristic (chars/4), no API call, printed before the first Sonnet request; requires explicit `y`
    - `build_bank()` — hash-checks for an existing bank first (zero API calls on a re-run with identical params); `generate_bank_for_plan()` is the CLI-facing entry point (checks `ANTHROPIC_API_KEY` via `MissingApiKeyError` before constructing the client)
  - Modified: `scripts/harness/schema.py` — `PromptGenConfig` (`model: str = "claude-sonnet-5"`, `max_retries: int = 2`); `RunPlan` gains `prompt_bank_path: Optional[str]` and `prompt_gen: PromptGenConfig`
  - Modified: `scripts/harness/turn_resolution.py` — `assign_prompt_ids()`: deterministic draw-without-replacement (reshuffles once exhausted) from `random_seed`, assigns `prompt_id` to every bulk turn only
  - Modified: `scripts/harness/runner.py` — preflight loads the bank (if `plan.prompt_bank_path` is set) and calls `assign_prompt_ids()` before the manifest is written; a missing/invalid bank routes through the existing error-policy machinery as a "connectivity"-class hard-stop (before `client.health()`, so zero wipe/seed calls happen); `build_turn_message()` now looks up `prompt_id` in a `prompt_lookup` dict built from the loaded bank's validated entries, falling back to the Phase 0 placeholder stub only when no bank is loaded
  - Modified: `scripts/harness/elicit.py` — prompts for `prompt_bank_path`, `prompt_gen.model`, `prompt_gen.max_retries`
  - Modified: `scripts/run_experiment_harness.py` — `--generate-bank` flag (build/reuse a bank and exit without running an experiment); auto-generates a bank before executing when `prompt_bank_path` is absent and `conceptual_fields.subjects == "auto"`
  - Modified: `config.yaml` — new `prompt_gen:` section (`sonnet_price_per_mtok_in: 2.0`, `sonnet_price_per_mtok_out: 10.0` — Sonnet 5 introductory rate through 2026-08-31)
  - Added: `tests/test_harness/test_prompt_bank.py` (12 tests), `tests/test_harness/test_prompt_gen.py` (16 tests, all Sonnet calls mocked via `FakeAnthropicClient` — two tests use the real committed `seed_fixtures/seed_v2.json.gz` for fixture-reading/hash tests but make no network calls)
  - Extended: `tests/test_harness/test_turn_resolution.py` (+6 tests: `assign_prompt_ids` determinism, different-seed variation, bulk-only assignment, bank-smaller-than-turns reshuffle, empty-bank no-op), `tests/test_harness/test_runner.py` (+2 tests: bulk turn message equals bank `prompt_text`, missing bank hard-stops before any `wipe_graph` call), `tests/test_harness/test_elicit.py` (scripted answers extended for the 3 new prompts)
  - Tests: 34 new, 421 total — all passing, no regressions
  - Dependency: installed `anthropic` Python SDK (host-side only, `pip install anthropic` — never added to `backend/requirements.txt` or the Docker image; no tracked `requirements-dev.txt` exists yet for host-side scripts, so this is a manual local install for now)
  - **Verification protocol — all 4 checks PASS with real recorded output** (ANTHROPIC_API_KEY already exported; user confirmed proceeding with real, small Sonnet 5 API spend before this ran):
    1. `--generate-bank` against real `seed_v2.json.gz`, plan with `total_prompts=20, probe_ratio=0.2` (16 bulk prompts): cost estimate printed as `$0.0682 (~24075 input tokens, ~2000 output tokens)`, confirmed, bank written to `prompt_banks/prompt_bank_3a3c7eb69111a3891c7bc399758b539953164548a5b2db269c643bcb51c2c511.json`. Per-cluster counts: `cluster_a {generated:4, validated:4, excluded:0}`, `cluster_b {4,3,1}`, `cluster_c {4,3,1}`, `cluster_d {4,4,0}` — 14/16 validated; the 2 exclusions were real fingerprint leaks (`['field']`, `['data']`) caught correctly after exhausting retries. PASS.
    2. Re-ran the identical plan with `--generate-bank`: `Prompt bank already exists for hash=3a3c7eb6... — reusing (no API calls)` — zero HTTP requests logged. PASS.
    3. Dry-run (`dry_run_n=2`) against a fake backend client with the generated bank wired via `prompt_bank_path`: both bulk turns produced real Sonnet-generated prompt text (e.g. `"Why is it so important that fields always show up in the same sequence..."`), and the manifest's `resolved_turn_sequence` carried `prompt_id: "cluster_d_0000"` / `"cluster_a_0001"` on those turns. The `"[Phase 0 placeholder"` string did not appear anywhere in the output. PASS. (Full Docker-backend dry-run deferred — the backend container wasn't running this session; Ollama was reachable but `localhost:8000` was not — so this check used the same `execute_run()` entry point against a fake `BackendClient`, which is the same code path the real backend would exercise.)
    4. Full test suite: 421 passed, 0 failed, no regressions. PASS.
  - Key decisions:
    - Fingerprint-scan failures skip the (paid) Sonnet coherence call entirely — a lexical leak is a hard local fail, no reason to spend a second API call confirming it
    - Batch generation retries the *same* per-cluster call once on malformed JSON, then falls back to per-prompt generation — this means one bad batch response degrades gracefully to per-prompt retries instead of losing the whole cluster
    - `compute_fixture_manifest_hash()` reimplements the sha256-over-raw-bytes logic locally rather than importing `backend.analytics.seed_fixture.compute_fixture_hash` — that module pulls in the backend's async DB layer, which is wrong for a standalone host-side script
    - `prompt_bank_path` preflight failure is treated as "connectivity"-class severity in the error-policy dict (same override mechanism as backend-unreachable errors) rather than an unconditional hard-stop, per the build plan's explicit framing
    - The one 20-prompt verification bank (`prompt_banks/prompt_bank_3a3c...json`) is committed as evidence of a real, successful end-to-end run — `prompt_banks/` is not gitignored, consistent with `seed_fixtures/` being a committed, reusable artifact
  - Branch: `milestone-19-phase-1-prompt-bank`
  - Milestone 19 Phase 1 status: COMPLETE — all Done criteria met (all 4 verification checks PASS with real recorded output, criteria present on every validated entry, placeholder string confirmed absent from bank-backed dry-run output, no regressions)

- 2026-08-13: /build — Milestone 43 complete (Plan-Declared Config State — assertion + env-override automation)
  - See BUILD-PLAN.md's Milestone 43 status paragraph for the full technical
    record (files touched, per-file rationale, and the scope-expansion
    finding). Summary here:
  - **Phase 1 (assertion):** `GET /admin/effective-config` (new, additive —
    `GET /admin/config` untouched), `RunPlan.expected_config`,
    `scripts/harness/config_assert.py` (shared mismatch-finder), a
    per-condition preflight check in `runner.py` and a per-plan step-5
    check in `queue.py` (enforced only when `config_overrides` is empty, to
    avoid false-positiving on Phase 2's cross-config queueing), and
    `runtime_config_hash`/`resolved_config_snapshot` provenance recorded in
    both run and queue manifests unconditionally.
  - **Phase 2 (injection):** `RunPlan.config_overrides` rides the harness's
    existing per-condition backend restart as `CONFIG_OVERRIDES_JSON`
    (docker-compose.yml passthrough); `backend/core/config_overrides.py`
    (`apply_config_overrides()`) is called from `core/orchestrator.py`
    before `_CONFIG_HASH`/`EDGE_UPDATE_CFG`/every derived constant.
    `config.yaml` itself is never written.
  - **Real gap found and closed beyond the plan's own named risk:** the
    plan flagged only `memory/extractor.py` as needing the same override
    merge as `core/orchestrator.py`. Grepping every independent
    `config.yaml` loader found two more that read exactly the keys exp01's
    two configs sweep — `analytics/seed_fixture.py`'s `_load_seed_priors()`
    (cfgA's `edge_update.seed_prior_alpha/_beta`) and both
    `analytics/reports/experiment_report.py`/`series_report.py`'s
    `_load_config()` (cfgB's `reporting.pass_rank_thresholds.spoke_retrieval`).
    Left unfixed, cfgA's seed edges and cfgB's §6.1 pass/fail scoring would
    have silently used the un-overridden config.yaml value even with Phase
    2 fully wired everywhere else — exactly the "silent divergence between
    two modules' views of the config" class of bug the milestone exists to
    kill, just at more sites than the plan named. All four now apply the
    same merge (`tools/registry.py`/`gate/sanitize.py`/`api/chat.py`'s
    SimpleRL-only read were left alone — no hyperparameter sweep targets
    those sections).
  - The 4 exp01 plans (not yet run, so free to change per the plan's own
    scoping) got `expected_config` populated from their existing
    `plan_name` prose: cfgA → `edge_update.seed_prior_alpha/_beta: 3.0`;
    cfgB → `hyperparameters.retrieval_level2_top_k: 2` and
    `reporting.pass_rank_thresholds.spoke_retrieval: 5`.
  - 68 new tests: `test_config_overrides.py` (pure parse/merge logic),
    `test_config_overrides_wiring.py` (import-time reload tests — confirms
    `_CONFIG_HASH` differs with/without an override, a malformed override
    hard-fails at import, and `decay.enabled: true` still hard-rejects
    post-merge — the milestone's own flagged "cheapest guard"),
    `test_config_assert.py`, `test_client_effective_config.py`,
    `test_effective_config.py`, plus extensions to `test_schema.py`
    (expected_config/config_overrides participate in plan_config_hash),
    `test_manifest.py`, `test_runner.py` (per-condition assertion +
    provenance + config_overrides threading into the restart env dict —
    also required adding `effective_config()` to the existing
    `FakeBackendClient` and fixing two pre-existing tests that asserted an
    exact restart-env dict), and `test_queue.py` (step-5 enforcement
    skipped for override-carrying plans, still recorded). 1151 tests total
    (1083 baseline + 68 new), no regressions.
  - Docs: `docs/harness_usage.md` §7 rewritten — the "one queue = one
    config.yaml state" rule is now conditional on whether plans carry their
    own `config_overrides`; new "Plan-declared config" subsection; two new
    troubleshooting rows for the mismatch error message.
  - No live verification against a real Docker backend this session
    (Docker wasn't running) — the milestone's own "Done when" criteria
    (hard-stop on a real mismatch, pass after correction+restart, an
    edit-without-restart caught) are exercised by the test suite's fake
    `BackendClient` against the exact same code path `execute_run()` uses,
    matching the pattern several prior harness milestones used when Docker
    wasn't up (e.g. M19 Phase 1). **Still worth a live pass before exp01
    actually runs:** restart the real backend with a deliberately wrong
    `CONFIG_OVERRIDES_JSON`, confirm `GET /admin/effective-config` reports
    it, and run one of the exp01 plans with `--dry-run` to see the
    assertion fire against the real container.
  - Branch: `milestone-43-plan-declared-config-build`
  - Milestone 43 status: COMPLETE — both phases built, all Done criteria
    verified against a fake backend (real-Docker pass still recommended
    before the actual exp01 launch, noted above), no regressions. AWAITING
    PAUL: PR review + merge.

- 2026-08-13 (b): /review follow-up — Milestone 43 review fixes + full live verification
  - Paul reviewed the M43 PR against the BUILD-PLAN and asked for all four
    review findings implemented plus the live run. See BUILD-PLAN.md's
    "Milestone 43 (review pass, same day)" paragraph for the full record.
  - **The one real defect found:** `--no-docker-restart` silently disabled
    Phase 2 entirely. `config_overrides` are delivered *only* by the
    per-condition backend restart, so that flag (present on both CLIs)
    delivered none of them — and since `schema.py` explicitly allows a plan
    to supply without asserting, a `config_overrides`-only plan would have
    run to completion against the un-overridden `config.yaml` with nothing
    to detect it. That is the exact silent-wrong-config failure M43 exists
    to kill, reintroduced through a CLI flag. Now an unconditional hard stop
    in `execute_run()` and in `run_preflight()` (minute zero, before the
    backend is consulted at all).
  - Also fixed: the queue manifest now records `resolved_config_snapshot`
    alongside `runtime_config_hash` (Phase 1 step 5 asked for both, and the
    docs already claimed both); the dotted-key-collision open question is
    closed with `find_dotted_key_collisions()` asserted against the *live*
    config; and all 4 exp01 plans now carry `config_overrides` (not just
    `expected_config`), with their stale "REQUIRES config.yaml state ..."
    prose corrected.
  - **exp01 no longer needs a manual config switch.** Each plan supplies its
    own config and asserts it. The two queue files remain as an operational
    split (two ~5 h nights vs one ~10 h run), not a harness constraint —
    merge them into one 4-plan queue whenever that's preferable.
  - **Live verification, real Docker backend + real gemma4:e4b** (Docker was
    up this session): the edit-without-restart trap reproduced end to end
    (`/admin/config` fresh 3.0 vs `/admin/effective-config` stale 1.0, real
    preflight hard-stopping on both keys), the same plan passing after a
    restart, a real 2-plan cross-config queue completing with three distinct
    `runtime_config_hash` values and `expected_config verified` on both
    plans, `full_config_snapshot` on real run rows carrying the overridden
    values while `config.yaml` stayed committed-clean (`git status` clean
    throughout), malformed/unknown/typo'd overrides each hard-failing at
    container import, and `decay.enabled: true` still rejected by
    `validate_edge_update_config()` post-merge.
  - Environment left clean: backend restored to baseline
    `runtime_config_hash=c95248806877` / `overridden_keys: []`, `config.yaml`
    untouched. The graph is wiped/re-seeded to seed_v3 + one verification
    turn (37 MUs) — the state exp01 wipes from anyway — with the previous
    667-MU exp0b-rep3 graph archived by the harness's own wipe, and
    throwaway analytics rows under `m43-live-cfgA/cfgB_dryrun_learned`.
  - 1166 tests passing (15 new beyond the build session's 1151), no regressions.
  - Branch: `milestone-43-plan-declared-config-build` (same PR, updated)

- 2026-08-20: /build — Milestone 44 complete (Probe Measurement Reads the
  Resolved Config — Phase 1 fix + Phase 2 detection check)
  - See BUILD-PLAN.md's Milestone 44 section for the full technical
    record. Summary here:
  - **Phase 1 (the fix):** `POST /admin/run-probe` (`backend/api/admin.py`)
    dropped its own `with open(_CONFIG_PATH, ...)` disk read — a second,
    unrelated loader in the same file that M43's audit missed — and now
    uses the already-imported, already-resolved `_CONFIG` directly. One-line
    comments at both of admin.py's loader sites so the deliberately-disk
    `GET /admin/config` can't get "fixed" the same way by a future reader.
    `core/config_overrides.py`'s docstring inventory updated to name
    `api/admin.py` as a resolved-config consumer that inherits by import.
  - **New regression test** (`tests/test_api/test_run_probe_resolved_config.py`):
    real end-to-end test against a temp graph store — confirmed FAILING
    against the pre-fix code (retrieved slate stuck at K1(3)+K2(1)=4 under a
    K2=2 override) before the fix was applied, then passing after.
  - **New class fix** (`tests/test_config_loader_inventory.py`): turns M43's
    docstring-only loader inventory into an enforced allowlist scan of
    `backend/**/*.py` for `open(_CONFIG_PATH ...)` sites. Includes a live
    test that writes a throwaway loader file into the real `backend/` tree,
    confirms the guard flags it, then removes it.
  - **Phase 1 live verification (real Docker backend, real graph.db):**
    booted with `CONFIG_OVERRIDES_JSON` setting `retrieval_level2_top_k: 2`;
    `POST /admin/run-probe` returned a 5-MU retrieved set (baseline, no
    override: 4 MUs), and both calls' `probe_results` rows were confirmed
    written with matching slate sizes by direct query against `graph.db`.
    Backend restored to its original baseline `runtime_config_hash`
    afterward; `config.yaml` untouched throughout (`git status` clean).
  - **Phase 2 (the detection check):** `_check_probe_retrieval_geometry()`
    (`backend/analytics/reports/experiment_report.py`), a fourth §6.3
    integrity check, compares the maximum observed
    `probe_results.retrieved_mu_ids` slate size against K1+K2 read from a
    new `store.get_full_config_snapshot_for_experiment()` method — deliberately
    checking only the *maximum* observed slate, since short slates are
    normal and common early in a run. `markdown_renderer.py` gains the
    "Probe-Retrieval Geometry (M44)" §6.3 subsection.
  - **Phase 2 live acceptance check — the milestone's own Done-when
    criterion:** regenerated all 8 archived learned-run experiment reports
    (`exp0b`/`exp0b-rep2`/`exp0b-rep3` baselines, `exp01-cfgA-rep1/rep2/rep3`,
    `exp01-cfgB-rep1/rep2`) against the real `graph.db` via a real Docker
    backend. Result matched the prediction exactly: **FAIL for cfgB rep1 and
    rep2** (expected K1+K2=5, max observed 4 — the real defect, reproduced
    from archived production data, not a synthetic fixture) and **PASS for
    the other six** (all K1+K2=4, matching). Confirmed by direct SQL query
    against `graph.db` before running the endpoint, then confirmed again via
    the real endpoint's JSON and rendered Markdown output.
  - **Open question resolved:** `apply_config_overrides()`'s list-valued-key
    behavior (flagged unverified in the milestone's own risks section) is
    replace-not-merge — a plain `node[leaf] = value` assignment, confirmed
    by a new test (`test_apply_overrides_list_valued_leaf_replaces_not_merges`
    in `tests/test_core/test_config_overrides.py`). Relevant to any future
    item F knob run overriding `retrieval_exclude_source_types`.
  - 34 new tests total, full suite green (1196 passed, 2 pre-existing
    unrelated skips, no regressions).
  - Branch: `milestone-44-probe-resolved-config-build`
  - Milestone 44 status: COMPLETE — both phases built, all Done criteria
    met with live verification against a real Docker backend and the real
    `graph.db`. AWAITING PAUL: PR review + merge.

- 2026-08-24: /planner — Alpha item D scoped; Milestones 45 + 46 added to
  BUILD-PLAN.md. No code written.
  - M44 confirmed merged (cb39b85), `main` clean. Session opened as a read
    of `EXPERIMENT-PLAN.md` to pick the next step, with an explicit question
    about whether Alpha item E (judge calibration) should come next.
  - **Item E assessed and deferred, with its rationale changed.** Verified
    that probe pass rate is `gold_mu_rank <= threshold`
    (`experiment_report.py:767`, `_classify_probe_outcome`) — pure retrieval,
    no judge. So Gemma error has **no path into any headline read**: the 4/8,
    the 5/12, and item C's lock-in tables are all judge-independent, and E
    cannot retroactively correct anything. But verdicts drive edge updates,
    and item F's leading candidate is a write-side upvote multiplier, so E is
    properly a **pre-flight gate on F's write-side knob**, not a re-read of
    past runs — a promotion from where `EXPERIMENT-PLAN.md` has it. Also
    confirmed E is executable on archived data at zero run cost: all 1,680
    learned-condition responses recover via `memory/audit.log` joined on
    `analytics.output_mu_id` (210/210 for all 8 runs), verdict split
    611 up / 745 down / 324 none.
  - **Alpha D scope decisions** — see the 2026-08-24 current-state entry
    above for the composite metric design, horizon 2, archived-data scope,
    and the deferred cfgB disposition.
  - **Two directive premises corrected:** the `admin.py` fix was already
    shipped by M44 Phase 1 (no milestone created; item F's measurement gate
    is already clear), and the sensitivity/variance baseline doc did not
    exist (now created by M45 Phase 2 rather than appended to).
  - **`weight_mode: sample` caveat** (`config.yaml:357`) found during
    scoping; the single-criterion tie-back was split into three deterministic
    components as a result. Reconstructibility verified in detail:
    `edge_update_events` covers all 8 learned runs with 0 orphans and 100% of
    its `edge_id`s resolve via `analytics.traversal_path`; edge creation is
    deterministic at `orchestrator.py:517/601/621`; `audit.log` survived every
    graph wipe with 9,372 MUs indexed.
  - Milestones 45 and 46 written into BUILD-PLAN.md with full specs, ASCII
    diagrams, Done-when criteria, and risk lists. Status block updated.
  - Milestone 45 status: PROPOSED — awaiting build. Next action is Phase 1,
    `scripts/alpha_d_pathback.py`.

- 2026-08-26: /build — Milestone 45 Phase 1 complete (the path-back
  reconstructor and its three-component tie-back)
  - `scripts/alpha_d_pathback.py` rebuilds per-run graph topology and edge
    (alpha, beta) trajectories from sources that survive a graph wipe:
    `analytics` + `edge_update_events` in `memory/graph.db`, and
    `seed_fixtures/seed_v3.json.gz`. Deliberately never reads the live
    `edges` / `memory_units` tables — those hold only the most recent
    wipe+reseed's state (34 seed MUs today; none of the 8 archived runs'
    conversation topology survives there).
  - **Design point the milestone spec left open: conv edges have no
    recoverable `edge_id`.** `graph_store.create_edge()` for prompt→input,
    input→output, and output→activated-MU edges never passes an `edge_id`
    — the store assigns a random UUID, and that UUID is gone once the live
    edges table is wiped. The reconstructor tracks conv edges by
    `(source_mu_id, target_mu_id)` pair instead of by id — sound because
    every conv pair is created at most once ever (prompt/input/output MUs
    are unique to their own turn, never reused as a source later).
    Topology existence/completeness (component 2) is checked by pair; a
    turn's own recorded `traversal_path` (which carries the real edge_id
    alongside the pair) is then trusted as the enumeration for the K1→K2
    replay (component 3) — trusted because component 2 just validated that
    enumeration is topologically sound. Full rationale in the script's
    module docstring; recorded in BUILD-PLAN.md's Phase 1 build notes.
  - **All three tie-back components passed clean on the first full run
    against real data** — no debugging iteration needed once the pair-
    identity design was worked out. 1,680 real turns across all 8 archived
    learned runs (exp0b rep1/2/3, exp01-cfgA rep1/2/3, exp01-cfgB rep1/2),
    zero mismatches in alpha/beta, topology, or slate. Full run takes
    ~0.5s (`py -3 scripts/alpha_d_pathback.py`).
  - Confirmed empirically (not assumed): all 8 runs used seed fixture v3
    — `memory_units` currently holds only 34 "seed:v3" MUs (v2's rows are
    gone from a later wipe), and the v3 fixture's known MU ids (e.g.
    `a0000001-...`) appear directly in the runs' own `activated_mu_ids`.
  - `tests/test_scripts/test_alpha_d_reconstructor.py`: 30 tests — unit
    coverage of each reconstruction rule (seed vs. conv creation priors,
    conv-pair visibility timing, directedness — the schema's `direction`
    column is never read, only `source_mu_id` /`target_mu_id`) and every
    mismatch-detection branch of the three `verify_*` functions against
    synthetic data, plus real-data tests (skip if the archived DB/fixture
    aren't present in the checkout) including the full 8-run acceptance
    check that is the milestone's actual Done-when.
  - `memory/audit.log` (one of the six sources the milestone plan names)
    turned out not load-bearing for any of the three Phase 1 components —
    none need MU source_type or text, only ids and edge topology. Still
    built as `load_audit_registry()`, a reusable utility for Phase 2.
  - Full suite: 1,257 passed (1,227 pre-existing + 30 new), 2 pre-existing
    unrelated skips, no regressions.
  - Branch: `milestone-45-pathback-reconstructor` (fresh branch off main
    post-PR-#81-merge — the M45/M46 planning-docs branch/PR from
    2026-08-24 was separate and is already merged).
  - Milestone 45 Phase 1 status: COMPLETE. Phase 2 (metrics layer, first
    read, `docs/sensitivity_baseline.md`) not yet built. AWAITING PAUL: PR
    review + merge.

- 2026-08-26: /build — Milestone 47 complete (K1 Centrality Coefficient,
  R3-V6 knob)
  - `config.yaml` gained `hyperparameters.k1_centrality_weight` (default
    `1.0`, beside `retrieval_level1_top_k`); `_k1_score()` in
    `backend/memory/retrieval.py` reads it with the other `hp.get` calls
    and applies it only to the `edge_weight_sum` term.
    `candidate_set_before_ranking`/`edge_contributions` already read
    `edge_weight_sum` directly (not through `_k1_score()`), confirmed by a
    dedicated test to stay raw/unscaled rather than assumed.
  - 6 new tests. `tests/test_memory/test_retrieval.py`: a 3-candidate
    fixture (BM25 and `edge_weight_sum` deliberately disagree) proving the
    coefficient is a genuine scalar multiplier — three DISTINCT total K1
    orderings across 0.0/0.5/1.0, verified empirically against the real
    `BM25Okapi` scores before writing the assertions. A 2-item fixture
    would not have distinguished a real coefficient from a boolean gate at
    0.5 (the dominant-edge item wins either way); this one does.
    `tests/test_core/test_config_overrides_wiring.py`: the leaf resolves
    through the real `core.orchestrator` against the real, committed
    `config.yaml` (not a synthetic `apply_config_overrides()` call),
    matching the file's existing per-leaf pattern.
  - `scripts/verify_seed_v3.py`'s open question resolved as recommended
    (option a): its own copy of the K1 formula stays unmirrored; a comment
    now names M47 as the point the two formulas can diverge.
  - **§8 errata pass folded into `docs/experiment_protocol.md`, 6 of 7
    items** — documented `config_overrides`/`expected_config` (types,
    merge/assertion behavior), retired `tuning_knobs`, added
    `seed_fixture_version`/`config_overrides`/`expected_config` to the
    schema table, rewrote the v3-wiring section (v3 has been in use since
    exp0), added `random_seed`'s non-Thompson-seeding caveat, noted the
    two-condition Phase 1 default, replaced the worked example with an
    exp01-cfgA-rep1-style plan, and added a new "Sweepable hyperparameter
    knobs" reference table (V1–V6). The 7th item (analyst-consumed field
    list) stays open — depends on R0-1's field audit, not yet run;
    `phase_one_experiment_program.md` §8/§6 record the split explicitly.
  - Wrote (did not run) the R3-V6 plan file as two replicates —
    `plans/exp02-v6-rep1_2026-08-26.json` / `exp02-v6-rep2_2026-08-26.json`
    — identical `config_overrides`/`expected_config`
    (`hyperparameters.k1_centrality_weight: 0.0`), distinct `plan_id`/
    `random_seed`, otherwise matching the Round 1 baseline shape.
  - **Done-when text/code mismatch found and worked around, not silently
    fudged.** The milestone's own Done-when bullet describes
    `run_experiment_queue.py --dry-run` printing
    `expected_config verified … N key(s) matched` for the plan file.
    Tracing `queue.py`'s preflight step 5 shows that print only fires for
    a plan supplying `expected_config` *without* `config_overrides` — a
    plan with both (as required here) is deliberately exempted at the
    queue layer, since an override-carrying plan's config is only
    established by its own per-condition restart, and the message that
    actually verifies it (`runner.py`'s `check_and_record_effective_
    config()`) only runs during real per-condition execution, which
    mutates the graph — contradicting the same bullet's "zero mutation."
    Resolved with two checks instead of one forced-fit: `--dry-run`
    against both plans confirmed zero-mutation preflight passes with
    distinct `config_hash` (proving valid replicates); the real
    `find_config_mismatches()` assertion was then run directly against
    the plan's `expected_config` and a live, override-active backend's
    actual `GET /admin/effective-config` — same function, same message
    format, zero additional mutation — printing
    `expected_config verified: 1 key(s) matched`.
  - **Live verification (M44 pattern, real Docker backend, real
    `graph.db`, same graph state both times — 678 pre-existing MUs from
    prior archived runs):** baseline `POST /admin/run-probe` (`vs_a1`,
    `weight_mode: mean`) returned seed hub `a0000001-aaaa-…-000000000001`
    in the slate at `gold_mu_rank=4`. Restarted with
    `CONFIG_OVERRIDES_JSON={"hyperparameters.k1_centrality_weight": 0.0}`
    — `GET /admin/effective-config` reported `0.0` with
    `overridden_keys=["hyperparameters.k1_centrality_weight"]` and a
    distinct `runtime_config_hash`. The same probe on the same graph state
    then returned a **different** slate: the seed hub dropped out
    entirely and `gold_mu_rank` went from `4` to `None` — a direct,
    real-data confirmation of the planning pass's prediction that hub
    entry points would essentially vanish at `0.0`. Backend restarted a
    third time with no override; `GET /admin/effective-config` confirmed
    the original `runtime_config_hash` (`fd71fc92…`) and
    `overridden_keys: []`. `git status` clean throughout; `config.yaml`
    never touched on disk.
  - Full suite: 1266 passed (1260 pre-existing + 6 new), 2 pre-existing
    unrelated skips, no regressions.
  - Branch: `milestone-47-k1-centrality-coefficient`.
  - Milestone 47 status: COMPLETE — all Done-when criteria met (one with
    a documented substitution, see above) with live verification against
    a real Docker backend and the real `graph.db`. R3 remains blocked on
    R0/R1 per `phase_one_experiment_program.md` — this milestone delivers
    only the knob, per its own explicit non-scope. AWAITING PAUL: PR
    review + merge.

- 2026-08-26: /build (same session, Paul-directed follow-up) — closed the
  queue-preflight `config_overrides` shape gap
  - Paul flagged the "expected_config verified" Done-when mismatch from
    the entry above as a real hole, not just a text/code mismatch: queue
    preflight had zero validation of `config_overrides` itself, so a bad
    key would only crash hours into an unattended overnight queue.
  - Proposed a two-check design (offline key-resolution + offline
    `expected_config`-vs-`config_overrides` value consistency). **Paul
    scoped it down to key-resolution only** — `config_overrides` *is* the
    intended value, already provenance-recorded per run via
    `analytics.full_config_snapshot`; a wrong-but-resolvable value is a
    data-science question, not an infrastructure failure worth a second
    field to keep in sync. Agreed and built only the shape check.
  - `find_unresolvable_override_keys()` added to
    `scripts/harness/config_assert.py`, wired into `queue.py` step 5 —
    reuses the live config already fetched in that step (no new I/O).
    Regression-proven (reverted the fix, confirmed the new test failed —
    "DID NOT RAISE" — then restored it and confirmed it passes) and
    live-verified against a real Docker backend via the real
    `run_experiment_queue.py --dry-run` CLI: a deliberately-typo'd plan
    hard-stopped naming the exact bad key; both real committed R3-V6
    plans still pass cleanly.
  - 10 new tests, 1276 total, no regressions. Same branch/PR as the rest
    of Milestone 47 (`milestone-47-k1-centrality-coefficient`) — a direct
    fix to a gap that build's own notes flagged, not new scope.
