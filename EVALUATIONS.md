# Local Corvus — Evaluations Log

The run-evaluation log. `EXPERIMENT-PLAN.md` records what we decided to run and
why; this file records what a run (or a read across several runs) actually
showed and what we concluded from it — the discussion layer the reports
themselves can't capture, since a report is data and this is the reading of
that data.

**Format (Plan Concept Alpha, item A.a, agreed 2026-08-16):**

- One file, **newest entry first**. One entry per read/discussion session —
  a session may cover several runs at once (the first entry below reads all
  four exp01 runs together).
- Fixed headings per entry, in this order: **What ran** · **What it showed**
  · **Comparison to prior runs** · **Interpretation** · **Implications** ·
  **Next steps** · **Longer-term notes**. Omit a heading only if it's
  genuinely empty for that entry (e.g., a first-ever run has nothing to put
  under "Comparison to prior runs").
- **Next steps** names plan items where one exists (e.g. "Alpha B") rather
  than restating the item's content — `EXPERIMENT-PLAN.md` is the source of
  truth for what a plan item actually says.
- Entries stay around a page. When a discussion goes deeper than that
  (mechanism arguments, rejected alternatives, design debates worth
  preserving), the deep dive becomes its own file under `docs/evaluations/`
  and the entry links to it with one line of context — don't inline the deep
  dive here.
- **Guardrail against bloat: entries record conclusions and reasoning, not
  data.** Numbers that support a conclusion may appear inline (a pass rate, a
  delta, a count) as evidence for the sentence making the claim, but tables,
  full per-probe breakdowns, and anything reconstructable from a report
  belong in the linked report, not copied in here. If you're pasting more
  than a line of numbers, it's report content — link it instead.

---

## 2026-08-20 — exp01-cfgA-rep3 read: 1/4, cfgA lands ambiguous, priors look like a flattener

**What ran.** `exp01-cfgA-rep3` alone (plan `plans/exp01-cfgA-rep3_2026-08-17.json`,
setup-and-goals `plans/exp01-cfgA-rep3_2026-08-17.md`, report
`memory/reports/experiment_report_exp01-cfgA-rep3-2026-08-17_learned.md`) — the
third cfgA replicate (seed priors 3.0/3.0), at seed 20260726, the one Round 1
baseline seed cfgA had not yet used. Read against the decision rule
pre-registered in `EXPERIMENT-PLAN.md` item B.c and the setup doc, and against
its quasi-paired baseline `exp0b-2026-08-06`. First read to use the A.c
setup-and-goals convention.

**What it showed.** **1/4 final-session cluster-slots aligned → pooled cfgA
5/12 → the pre-registered *ambiguous* outcome**, which routes the config
decision to item C. The single aligned slot is degenerate and should not be
read as a win: `vs_a1` is control-pinned at 1.0000 in every session under
static_weight, ran 0.04–0.12 in the learned condition (final Δ −0.88), and
"won" cluster_a only because the whole cluster collapsed to the floor — 3
passes against 2 out of 25 at session 5, and a coin-flip tie at session 4. The
secondary champion watch fails outright: no probe reaches the Round 1 champion
band (≥0.44) in any session except untrained `vs_a2` at 0.48 in session 1,
decaying to zero, and the best trained-spoke value anywhere in the run is
`vs_d5` at 0.16. All three paired predictions missed — `vs_b3` never started
rather than decaying (0.04 → 0.00 and flat), `vs_d5` took cluster_d in sessions
2–3 and lost it back, and `vs_d3` produced no third climb. Trained-subset final
Δ of −0.19 sits inside the Round 1 envelope (+0.04 excluding the pin). The run
itself is clean: every §6.3 integrity check PASS, `expected_config` asserted
3.0/3.0, zero failures / ghost MUs / pressure / saturation, and all the
universal dynamics present exactly as pre-registered non-outcomes.

**Comparison to prior runs.** rep3 is the **flattest of the eight learned runs
to date** — its best final-session cluster winner is 0.24, where every other run
has at least one at 0.28 or above and most have one in the champion band.
Nothing consolidated in any cluster, trained or untrained. Its own baseline pair
at this seed was more decisive, not less: `exp0b-2026-08-06` handed cluster_d to
untrained `vs_d6` at 0.84. So the honest paired statement is that cfgA aligned
1/4 where baseline aligned 0/4, at a sequence where baseline nonetheless
produced far stronger concentration. Pooled cfgA (5/12) still nominally exceeds
the Round 1 baseline (2/12), but that margin is carried entirely by two slots —
rep1's `vs_a1` and rep2's `vs_d5`.

**Interpretation.** The working reading of cfgA changes: 3.0/3.0 priors are
behaving as a **flattener**, not as a discriminator. They do what they were
designed to do — dilute early evidence and keep exploration alive past session 1
— and the consequence in rep3 is that *nothing* consolidates, rather than that
the trained spoke gets a fairer chance. A preliminary pass at item C's
preservation question supports this: session-1 winner survives to session 5 in
roughly two-thirds of baseline slots but only about a fifth of exp01-config
slots, with rep3 preserving none of its four, against a chance rate near
one-in-seven. Lock-in therefore looks real in the program *and* substantially
suppressed by both configs we have tried — which makes rep1's and rep2's
successes read more like early lottery wins that happened to survive the
flattening than like the config selecting for the trained spoke. That
preliminary pass is a quick count with crude tie handling and a different run
scope than item C specifies; it sharpens item C's hypothesis, it does not
answer it.

**Implications.** cfgA is **not confirmed** and should stop being treated as the
presumptive base config for item F's knob search; that choice now waits on item
C, as B.c pre-registered. More alignment replicates will not settle this — four
binary slots per run cannot distinguish "the config worked" from "one slot got
lucky," which makes the bottleneck a measurement problem and raises the priority
of item D's per-turn instrumentation ahead of any further knob runs. Item E also
gains relevance: a run this flat, with up-rate sliding 54.5% → 28.1%, is exactly
where a 10–20% judge error rate correlated with context composition would most
plausibly swamp the edge-update signal, so rep3 is a good re-grade candidate.
One process gap surfaced: the setup doc predicted the wrong control-pinned
probe, and the pin landed on the very slot that decided the primary outcome —
checking which probes are control-pinned belongs *before* reading the winner
table, since a pinned probe can win a cluster in the learned condition while
scoring −0.88 against its control.

**Next steps.** Alpha C, now, and extended to cover the baseline-vs-config
preservation split above — it is the pre-registered tiebreaker for this exact
outcome and the gate on Alpha F's base config. Alpha D before any further knob
replicates. Alpha E, with rep3 as a re-grade candidate. Alpha F stays gated.

**Longer-term notes.** Cross-cluster passive tracking: 7 cross-cluster and 5
mixed edges, deltas in both directions, including two updates to a single
`cluster_a → bridge_ab` edge — bridge-targeted edges have now appeared in three
runs (`exp0b-2026-08-06`, cfgA rep2, cfgA rep3), still with zero bridge probes
and no bridge activity in the probe results, so this remains an observational
note rather than a signal. Mean-mode headline metrics read 0.0000 / 4.0 for the
eighth consecutive run — the dead-instrument finding is now as well-evidenced as
it is going to get.

---

## 2026-08-16 — exp01 (Round 2) read: cfgA confirms, cfgB nulls, lock-in hypothesis opens

**What ran.** All four exp01 Round 2 experiments, read together in one
session: `exp01-cfgA-rep1`, `exp01-cfgA-rep2`, `exp01-cfgB-rep1`,
`exp01-cfgB-rep2` (plans `plans/exp01-cfg{A,B}-rep{1,2}_2026-08-12.json`,
reports `memory/reports/experiment_report_exp01-cfg{A,B}-rep{1,2}-2026-08-12_learned.md`).
cfgA = seed priors `seed_prior_alpha`/`seed_prior_beta` 3.0/3.0 (vs. baseline
1.0/1.0); cfgB = retrieval `retrieval_level2_top_k` 4→5 (with
`pass_rank_thresholds.spoke_retrieval` adjusted to 5 to match). Read against
the three Round 1 baselines (`exp0b-2026-08-06`, `exp0b-rep2-2026-08-09`,
`exp0b-rep3-2026-08-09`) and the pre-registered M42 decision rules.

**What it showed.** cfgA: **4/8 final-session cluster-slots aligned to
trained spokes** (rep1: `vs_a1` only, dominant at 0.92–1.00 every session;
rep2: `vs_b3` 0.36, `vs_c1` 0.16, `vs_d5` climbing 0.40→0.96) — lands exactly
on the pre-registered "clear signal" line, against a 2/12 Round 1 baseline.
cfgB: **2/8 aligned** (rep1: `vs_a1` 0.12, `vs_c1` 0.40; rep2: none) — the
pre-registered null, not extended. Raw trained-subset final Δ: only cfgA
rep1 (0.00) escapes the Round 1 envelope of [−0.46, −0.12]; the other three
runs land inside it. Universal dynamics held in all four runs: seed-share
collapse (~45–55% → 4–17% by session 5), within-run judge up-rate decay,
only a trickle of cross-cluster/mixed edges (2–9), zero failures / pressure
/ saturation. Mean-mode headline metrics stayed flat at 0.0000/4.0 in every
run — still a dead instrument (see Standing items below).

**Comparison to prior runs.** cfgA's 4/8 roughly doubles the Round 1
baseline rate (2/12 ≈ 0.17 → 4/8 = 0.50). cfgB's 2/8 (0.25) is not
distinguishable from baseline. cfgA's pre-registered "too slow to
concentrate" failure signature — the reason a redirect to 2.0/2.0 priors was
sketched as a fallback — did **not** appear in either replicate; champions
reached and exceeded the Round 1 champion band (pass rate ≥ 0.44), so that
fallback is not triggered.

**Interpretation.** cfgA is a real, if modest, directional signal — one more
replicate (Alpha B) is needed before treating it as confirmed rather than
promising, because replicate variance is large enough to matter on its own:
`vs_a1` was a 1.00 champion in rep1 and flat 0.00 in rep2 under an
*identical* config. That same split, plus rising learned-looking curves on
an untrained spoke (`vs_d3`, seen in two separate runs now) — concentration
without alignment — motivates a new working hypothesis: the eventual
cluster winner may be decided by an early-feedback lottery in session 0–1,
amplified by the rich-get-richer edge-update loop, with config only shifting
the odds rather than picking the winner. This reframes what "config search"
is even measuring if true. Separately, Paul reframed seed-share collapse
itself this session: conversational MUs are not noise to be suppressed, they
are permanent, intended memory infrastructure that will eventually be
enthroned/dis-enthroned by edge weight rather than a seed/conv label — the
near-term problem is that they don't yet act as stepping stones back to the
MU that held the answer, not that they exist at all.

**Implications.** cfgA becomes the base config candidate for future knob
work, pending rep3. The lock-in hypothesis, if it holds, changes what a
config run should be judged on (session-1 alignment, not just final-session)
and where knob search should aim (the session-0/1 window). The seed-share
reframe redirects instrumentation work toward path-back visibility
(does a retrieved conv MU lead back to its gold seed?) rather than toward
suppressing conv-MU growth, and redirects write-side ideas toward
strengthening positive paths rather than decay.

**Next steps.** Alpha B (cfgA rep3, settle the 4/8) and Alpha C (lock-in
analysis on existing data, run in parallel, land before B's read) are next.
Alpha D (path-back instrumentation) and Alpha E (strong-judge calibration)
follow; Alpha F (positive-path knob experiments) is gated on B and D.

**Longer-term notes.** Two items to keep tracking passively rather than act
on now: cross-cluster transfer (2–9 mixed conv→seed edges per run so far,
both directions, no bridge activity — its own series eventually, per
`EXPERIMENT-PLAN.md`'s standing items) and the mean-mode dead-instrument
finding (four runs running now with mean-mode headline metrics reading
identically flat — worth dropping or replacing with a rank-sensitive
measure at some point, but low priority).
