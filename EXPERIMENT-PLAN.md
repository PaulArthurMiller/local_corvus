# EXPERIMENT-PLAN.md — Experiment Planning

**Purpose:** the experimental counterpart to `BUILD-PLAN.md`. Build milestones
track *code*; the plans here track *runs, analyses, and experiment process*.
When a plan item needs new code (a knob, a metric, a report section), that
code work still becomes a BUILD-PLAN milestone — this doc records why we want
it and what experiment it serves.

**Conventions:**

- Each planning session produces one named plan: **Plan Concept Alpha**,
  **Beta**, **Gamma**, … in session order. Plans are independent of each
  other, the way BUILD-PLAN milestones are — a new discussion session opens a
  new plan rather than extending an old one.
- Within a plan, top-level items are outlined **A**, **B**, **C**, … and are
  listed in their expected time sequence: item A is expected to complete
  before item B **unless the plan specifically notes otherwise**.
- Subitems are outlined **a**, **b**, **c**, …; anything below that is
  double-lettered (**aa**, **ab**, …). Non-sequential collections
  (alternatives, watch lists, candidate ideas) are bulleted, not lettered.
- These are experiment plan items, not milestones, and they are deliberately
  not numbered — experiment IDs (exp0, exp01, …) and planning items will
  never line up, so we don't pretend they do.
- Each item carries a status: `proposed` → `agreed` → `in progress` → `done`,
  with a date.

---

## Plan Concept Alpha — post-exp01 read (2026-08-16)

**Context.** The exp01 (Round 2) read against the three Round 1 baselines:
**cfgA** (seed priors 3.0/3.0) landed exactly on the pre-registered "clear
signal" line at **4/8** final-session cluster-slots aligned to trained spokes
(baseline: 2/12), and produced the two strongest champions of the program
(rep1 vs_a1 at 0.92–1.00 every session; rep2 vs_d5 climbing 0.40→0.96).
**cfgB** (retrieval K 4→5) came in at **2/8 = pre-registered null** and is not
extended. Caveats: replicate variance dwarfs the config effect (vs_a1 was a
1.00 champion in rep1 and flat 0.00 in rep2), and two of cfgA's four aligned
slots were weak winners (0.16, 0.36). Universal dynamics held in every run:
seed-share collapse (~45–55% → 4–17%), within-run up-rate decay, only a
trickle of cross-cluster/mixed edges, zero failures/pressure/saturation.

### A. Adopt the run-documentation process — *agreed 2026-08-16*

The two standing process pieces, established first because item B's run is
the first consumer of both.

**a. Create `EVALUATIONS.md`** — the run-evaluation log, at repo root beside
BUILD-PLAN.md and PROGRESS.md. The durable record of what each run produced
and what we decided it meant — the discussion layer reports can't capture.
Working format (deliberately simple):

- One file, newest entry first; one entry per read/discussion session (which
  may cover several runs, as the exp01 four-report read did).
- Fixed headings per entry: **What ran** (links to reports + plan files) ·
  **What it showed** · **Comparison to prior runs** · **Interpretation** ·
  **Implications** · **Next steps** (naming plan items, e.g. "Alpha B") ·
  **Longer-term notes**.
- Entries stay around a page. When a discussion goes deeper than that
  (mechanism arguments, rejected alternatives, design debates worth keeping),
  the deep dive becomes its own file in `docs/evaluations/` and the entry
  links to it with one line of context.
- Guardrail against bloat: entries record *conclusions and reasoning*, not
  data — numbers live in the linked reports, never copied in.

**b. Backfill the first entry:** the 2026-08-16 exp01 read (cfgA 4/8, cfgB
null, the lock-in hypothesis, the path-back reframe).

**c. Define the per-experiment setup-and-goals doc convention** — every
experiment run from `exp01-cfgA-rep3` forward gets a brief pre-run statement
of intent, so results are always read against what we said we were looking
for. Shape: a markdown file in `plans/` with the same basename as the plan
file (e.g. `plans/exp01-cfgA-rep3_<date>.md` beside its `.json`), short and
fixed-format:

- **What we adjusted** — the knob(s) and values, vs. what baseline.
- **What we're watching for** — expected phenomena if the idea is right, and
  the signature we'd see if it's wrong.
- **How we'll measure** — which report sections/metrics carry the answer,
  plus the pre-registered decision rule, written before the run starts.
- Backfilling older runs is not required; their intent lives in PROGRESS.md
  and BUILD-PLAN.md.

### B. cfgA rep3 — settle the 4/8 — *agreed 2026-08-16*

**Goal:** determine whether cfgA's 4/8 is a real effect or replicate noise.
One more replicate is the cheapest way to move from "promising" to
"confirmed": 6/12 vs. 4/12 is the difference between "cfgA works" and
"cfgA is baseline-plus-noise."

**a. Write the setup-and-goals doc** (first use of the A.c convention).
The one open design decision it must record: seed choice — fresh seed vs.
reusing a Round 1 seed for another quasi-paired winner-identity read.

**b. Run `exp01-cfgA-rep3`** — overnight queue, same plan pattern as
rep1/rep2 (plan-declared `expected_config` + `config_overrides`, no manual
config.yaml edit).

**c. Read against the pre-registered decision rule** (fixed here, before the
run). Complete item C's analysis before this read if at all possible — an
ambiguous outcome needs it as tiebreaker. Outcomes:

- rep3 ≥2/4 aligned → cfgA treated as a confirmed directional signal; it
  becomes the base config for item F's knob experiments.
- rep3 1/4 aligned → ambiguous (5/12 overall); consult item C's lock-in
  analysis before choosing the next round's configs.
- rep3 0/4 aligned → 4/12 overall; treat cfgA as baseline-plus-noise.
- Secondary watch, all outcomes: does rep3 produce at least one champion at
  or above the Round 1 champion band (pass rate ≥ 0.44)? cfgA's value may
  lie less in average alignment than in enabling strong champions.
- Note: cfgA's pre-registered "too slow to concentrate" signature did NOT
  appear in rep1/rep2 (champions reached and exceeded the baseline band), so
  the previously sketched redirect to 2.0/2.0 priors is not triggered.

### C. Early-feedback lottery / lock-in analysis — *agreed 2026-08-16*

Existing data only, no new runs. **May run in parallel with item B** (the
queue runs overnight anyway); should land before B.c's read.

**Goal:** test whether the eventual cluster winner is effectively decided in
the first session — an early-feedback lottery whose winner the
rich-get-richer edge loop amplifies, with config only shifting the odds.
Motivated by the rep1/rep2 vs_a1 split (1.00 vs. 0.00 under identical
config) and by rising learned-looking curves on *untrained* spokes (vs_d3 in
two separate runs) — concentration without alignment.

**a. Build the table:** across all 7 learned runs to date (exp0b ×3,
exp01 ×4), per run × cluster: session-1 sample-mode winner; first spoke to
receive an upvote-driven edge increase (from the edge delta ledger);
final-session winner; whether each matches the trained spoke.

**b. Headline number:** P(final winner = session-1 winner) across the 28
run×cluster slots, vs. the ~1/7 chance level. Secondary:
P(final winner = first-upvoted spoke).

**c. Read:** a strong result (say >50%) confirms lock-in — future configs
get judged on *session-1* alignment, not just final, and config search aims
at the session-0/1 window. A weak result means late sessions still matter
and the variance lives elsewhere.

### D. Instrument path-back behavior — *agreed 2026-08-16 (metric definitions to be settled)*

**Context — the reframe of "seed-share collapse" (applies to D and F):**
conversational MUs are not noise to be suppressed — they are a permanent and
intended part of the memory infrastructure. In a deployed system, "seed" MUs
won't be designated at all; they'll be **enthroned or dis-enthroned over time
by the strength of the weights connected to them**, acquired conversationally.
The problem with late-session retrieval isn't that conv MUs dominate; it's
that they don't yet act as **stepping stones back** to the MU that actually
held the answer — a "conversation history" path that doesn't replay
conversational context but chases down the nugget of knowledge behind it.
Decay mechanisms are deferred; the near-term lever is strengthening and
following positive paths. Current experiments hunt for knob settings that
produce a learning signal; the enthronement dynamic is the larger goal they
feed.

**Goal:** before tuning anything, make path-back visible. Reports show
seed-share collapsing to ~4–17% by session 5, but can't distinguish "conv
MUs replaced the seed" from "conv MUs are one hop from the seed and would
lead back to it if retrieval followed edges."

**a. Settle metric definitions.** Proposed starting set:

- **Path-back rate:** fraction of probe-turn retrievals where a retrieved
  conv MU has an edge path (1 hop, and 2 hops as a separate number) to the
  gold seed MU for that probe.
- **Terminal-seed rate:** fraction of turns where following the
  highest-weight edge chain from each retrieved conv MU terminates at the
  gold seed MU.
- **Anchor strength:** for each seed MU that centered an upvoted answer, the
  weight trajectory of its seed↔prompt and seed↔response edges across
  sessions. (The M33 conv→seed rows in the edge ledger are the raw material —
  this extends that classification, it doesn't replace it.)

**b. Implement** — a report section or standalone analysis script at first;
becomes a small BUILD-PLAN milestone once a's definitions are settled.

**c. First read:** turn late-session seed-share from symptom into diagnosis —
are the stepping stones present but unused, or absent entirely?

### E. Strong-judge post-run calibration loop — *agreed 2026-08-16 (mechanism TBD)*

**May overlap items C/D** — independent of the rep3 sequence.

**Goal:** bound how much of the verdict signal is Gemma judge error vs. real
answer degradation, using a stronger model (Sonnet or Opus) to re-grade
samples after runs complete — larger samples than hand-grading allows, at
zero cost to run time. Prior calibration passes put Gemma's failure rate at
roughly 10–20%; every one of the ~1,470 learned-condition verdicts to date is
judge-alone.

The leading explanation for the universal within-run up-rate decay is *not*
judge drift: retrieved conv MUs present incomplete information for the probe
question, so answers genuinely get worse — the judge may be correctly
reporting a real retrieval problem (exactly the item D problem). A
strong-judge re-grade distinguishes the two: if Sonnet/Opus agrees with the
down-slope, it's real degradation; if not, Gemma noise is correlated with
context composition and contaminating the edge-update signal.

**a. Design the sample:** stratified (early vs. late sessions, up vs. down
verdicts) from one baseline run and one exp01 run; report agreement rates by
session. Paul may also do a manual pass alongside.

**b. Decide on standing adoption:** if disagreement is material, decide
whether the strong-judge re-grade becomes a standing post-run step.

### F. Positive-path knob experiments — *proposed (gated on B and D)*

**Goal:** find knob settings under which an upvoted, seed-centered answer
durably strengthens the path back to that seed, and retrieval actually
follows it. Runs only after item B confirms/denies cfgA as the base config
and item D provides the instruments to see path-back at all; design should
also be informed by item C's lock-in findings.

Candidate knobs (examples, not commitments):

- **Write side:** an upvote multiplier that asymmetrically boosts
  seed↔prompt and seed↔response edges when a seed MU centered the upvoted
  answer — much stronger than the generic edge update.
- **Read side:** a chase-down stage in retrieval — from retrieved conv MUs,
  follow the highest-weight edges toward a base MU; and/or hub scoring
  (weighted degree, edge count) so the MU that most often provided the
  correct answer in the past outranks its own echoes.

Measurement: item D's metrics become the success criteria, plus a reframed
seed-share reading — late-session *gold-seed terminal retrieval* going up is
success even if raw seed-share stays low. The exp01-style alignment tables
remain the cross-check that path-back helps rather than just recirculates.
Each knob run gets its own setup doc (A.c) with a pre-registered rule before
launch.

### Standing items (not sequenced)

- **Cross-cluster transfer: track passively, test later.** It gets its own
  experiment series in time; nothing is designed around it now. The
  Cross-Domain Linkage Watch List stays on, and any incidental signals (so
  far: 2–9 mixed conv→seed edges per run, deltas in both directions, no
  bridge activity) get noted in the relevant EVALUATIONS.md entry, building
  the observational base for the eventual dedicated series.
- **Mean-mode headline metrics are a dead instrument** *(assistant
  suggestion, not yet discussed in depth)*: mean-mode pass-rate delta and
  gold-MU rank have read 0.0000 / 4.0 in every run of every round; all
  observed signal lives in sample mode. Either drop mean-mode from the
  headline tables or replace it with a rank-sensitive measure. Low priority;
  small reporting change.
- **Decay is deferred, not rejected.** A decay mechanism for conversational
  edges will likely matter eventually, but the near-term work (items D and
  F) is about strengthening positive paths, and decay would add a confound
  while those knobs are being tuned.
