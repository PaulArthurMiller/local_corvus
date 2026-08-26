# Sensitivity Baseline

A running record of **structural facts about the retrieval + learning
mechanism**, established from archived-data analysis rather than from a
single experimental read. `EVALUATIONS.md` records what a *run* showed;
this file records what the *mechanism itself* does — knob sensitivity,
dead zones, and other facts that hold across runs and conditions, and
that any future config search (the Karpathy loop, item F) should already
know before spending an overnight on a knob.

Forward-referenced by `BUILD-PLAN.md`'s Milestone 27 (queue infrastructure)
as the destination for downstream result interpretation; created here by
Milestone 45 Phase 2, the first analysis to actually need it.

Format: newest entry first, one entry per finding-producing session. Each
finding names the script/data it came from so it can be re-run against
new data rather than trusted as a one-off number.

---

## 2026-08-26 — M45 Phase 2: path-back partition, K2 knob pricing, anchor strength

**Source:** `scripts/alpha_d_pathback.py --phase2`, all 8 archived learned
runs (baseline x3, cfgA x3, cfgB x2), 1,120 `battery_mode="mean"` probe
measurements (28 probes x 5 sessions x 8 runs). Full tables in
`docs/evaluations/alpha_d_pathback_analysis.md`. Cross-validated against
`probe_results.gold_mu_rank`: the REACHED bucket count matches the
recorded non-NULL `gold_mu_rank` count exactly, in all 8 runs.

**Finding 1 — the stepping stones are overwhelmingly present, not absent.**
Pooled across every run and condition, **80–82% of probe turns land in
OUTBID**: gold was a real K2 candidate — one directed hop from a retrieved
MU — and lost a one-slot argmax. DISCONNECTED (no path to gold at all,
within 2 hops, from anything retrieved) is rare: 0% in both cfgB runs, 3%
in baseline and cfgA. **This settles item D.c's diagnosis: late-session
seed-share collapse is overwhelmingly a read-side problem, not a write-side
one.** The graph is building the stepping stones back to gold; retrieval's
single K2 slot is what's discarding them. This directly informs item F:
a read-side chase-down candidate has far more raw material to work with
than a write-side strengthening candidate does, at least for the already-
one-hop-away majority.

**Finding 2 — chase-down yield is low even where a path structurally
exists (COUNTERFACTUAL).** Within OUTBID+OUT_OF_DEPTH turns, greedily
following the single highest-mean-weight outgoing edge from each retrieved
MU reaches gold only **6% of the time pooled** (64/989; per-run range
3–12%), despite ~81% of those turns having gold one hop away. The
stepping stone being *present* does not mean the *strongest* edge points
to it — competing conv edges usually outweigh it. This is evidence
against a naive "always take the top edge" chase-down design and toward
either widening the chase-down's own candidate set or biasing edge
strength toward gold-productive paths specifically (ties back to item F's
write-side candidate, not just its read-side one). Labeled COUNTERFACTUAL
per the risk item in BUILD-PLAN.md's Milestone 45 — `retrieve()` has never
performed this walk.

**Finding 3 — retrieval_level2_top_k is a cheap, well-behaved knob to
widen, priced without a new run.** Of turns where gold entered the K2
candidate pool, P(rank <= 2) = 24%, P(rank <= 3) = 36%, P(rank <= 5) = 60%
pooled — meaning widening `retrieval_level2_top_k` from 1 to 5 would
roughly **quintuple** the REACHED rate on turns where gold is a real
candidate (the current top_k=1 rate is P(rank <= 1), i.e. today's observed
11%/7%/10% REACHED-by-condition). This is the retrospective pricing item
D.a proposed specifically because cfgB's declared `retrieval_level2_top_k:
2` never actually reached the probe path (Milestone 44) — this number is
computed from what probes actually saw, at every value of K, not just the
one cfgB declared. **13% of pool-eligible turns never had gold enter the
pool at all** (146/1120) regardless of K — no top_k widening rescues those;
they need a chase-down stage or better topology.

**Finding 4 — one trained spoke's anchor never engages, across every run
in scope.** `cluster_c`'s trained spoke (`c_spoke1`) shows a flat
alpha/beta = 1.0/1.0 (mean 0.500) hub-edge trajectory in **every one of
the 8 runs**, with 0–1 output→gold co-activation edges ever created and
never more than 1 ever credited. Every other trained spoke shows nonzero
co-activation traffic and a moving mean weight by session 2. This is
consistent with the already-established finding (`docs/evaluations/
alpha_c_lockin_analysis.md`) that `vs_c1` never wins a single session
across all 8 runs — the anchor-strength instrument confirms the mechanism
behind that: `c_spoke1` isn't losing a close contest, it is structurally
never retrieved during real turns at all, so its hub edge never receives
so much as one write-side touch. Worth checking whether this is a probe/
prompt-bank content issue specific to `c_spoke1` before reading it as a
retrieval-mechanism finding.

**Standing findings, established during the 2026-08-24 Alpha D scoping
(from archived data, restated here as this doc's first entries rather than
re-derived):**

- **~11% of created edges are ever traversed; ~6% ever receive a weight
  update.** Dead consistent across all 8 runs and both configs (~1,260
  edges created per run at ~6/turn, ~143–171 ever traversed, ~70–112 ever
  updated; cfgB ~1,470 created at 11.0/11.6% and 7.6/6.9%). Co-activation
  edges are structurally near-frozen — the graph builds stepping stones
  roughly nine times faster than the learning signal can touch them.
  **The write side is therefore co-binding with `retrieval_level2_top_k`
  for item F:** widening the K2 slot without also widening what gets
  credit only samples more of a near-uniform prior. Finding 1 above sharpens
  this: most of that untouched 89–94% is still directionally *useful*
  (present, one hop away) — it's under-credited, not absent.
- **K1's `edge_weight_sum` is undirected total degree** — a centrality
  term, not a learned-direction term. A conv MU gains K1 rank mechanically
  at ~6 edges per turn, independent of whether any feedback ever endorsed
  it. Flagged as a knob candidate with this rationale attached; not
  changed now — it is a plausible explanation for concentration-without-
  alignment (the untrained-spoke `vs_d3` curves that motivated item C) and
  should be tested deliberately rather than adjusted in passing.
