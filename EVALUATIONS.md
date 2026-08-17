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
