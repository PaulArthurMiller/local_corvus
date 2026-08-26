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

## 2026-08-26 — V6 pre-registration: what the K1 centrality term actually does

**Status: pre-registration, not a result.** No new experimental run stands
behind this. It is written *before* R3-V6 executes so the run is read against
a stated expectation rather than an assumption, per the Phase 1 program's
§1-8. Milestone 47 delivers the knob this entry describes.

**Source:** counterfactual replay over `analytics.candidate_set_before_ranking`
for the three Round 1 baseline learned runs (`exp0b`, `exp0b-rep2`,
`exp0b-rep3` — 630 real turns, 12,324 candidate rows), plus a code read of
`_k1_score()` in `backend/memory/retrieval.py` and the fixture design in
`seed_v3_mu_spec.md` §2. Re-runnable from `graph.db` alone; no backend
required. Script: `scripts/v6_centrality_counterfactual.py` (read-only;
`--runs` widens the run set as more baseline replicates land).

### The term

K1 ranks BM25 multi-hit candidates by

```
_k1_score(mu) = edge_weight_sum[mu] + cumulative_bm25[mu]
                └── undirected total degree ──┘   └─ lexical ─┘
```

`edge_weight_sum` accumulates on **both** endpoints of every edge in the
graph (`retrieval.py:229-236`), so it is total degree, not out-degree and
not a learned-direction quantity. Conv edges are created at ~6/turn, which
means a conv MU's K1 rank rises mechanically with age whether or not any
feedback ever endorsed it.

### The counterfactual is exact, not reconstructed

Setting the coefficient to 0.0 changes only the *ranking* of the multi-hit
candidate pool. The pool itself (BM25 ≥2-keyword hits) is untouched, and
`candidate_set_before_ranking` records every pool member with both its
`bm25_score` and its `edge_weight_sum`. Re-sorting that recorded list by
`bm25_score` alone yields the exact K1 slate V6 will produce. Nothing is
approximated at the K1 stage. (K2 is a different matter — see Limits.)

### Finding 1 — the term is small in magnitude and decisive in effect

| quantity | median | mean | p90 | max |
|---|---|---|---|---|
| `edge_weight_sum` | 1.63 | 2.55 | 4.62 | 28.96 |
| cumulative BM25 | 5.15 | 5.61 | 9.06 | 27.42 |

Centrality is a median **24.9%** of the composite score — the minority term.
Yet it changes the K1 set on **88.4%** of turns and the top-ranked entry
point on **71.3%**. BM25 scores among multi-hit candidates cluster tightly,
so a small additive term flips the order almost every time. **This is not a
marginal knob.** Whatever V6 shows, it will not be a null for lack of
leverage.

### Finding 2 — the standing finding is true per-MU and inverted in aggregate

The standing finding below ("undirected degree rewards conv-MU accumulation
independent of feedback") holds at the level of an individual MU. Its
aggregate effect on slate composition runs the other way:

| K1 slate composition | conv | seed hub | seed other |
|---|---|---|---|
| with centrality (today) | 88.8% | 9.4% | 1.9% |
| coefficient 0.0 (V6) | 95.9% | 1.3% | 2.9% |

Of the 908 MUs the term promotes into K1, 81.8% are conv — but 96.6% of the
908 it displaces are *also* conv. Nearly all of the churn is conv-vs-conv.
The term's one systematic effect is **holding seed hubs in K1 at all**: turn
it off and hub entry points essentially vanish. Turning the centrality term
off therefore makes K1 *more* conv-dominated, not less.

### Finding 3 — the effect is an early-session effect that decays

Per session, pooled across the three baselines (126 turns per cell):

| session | hub share of K1 (on) | hub share (off) | K1 set changed | top-1 changed | median max `edge_weight_sum` |
|---|---|---|---|---|---|
| 1 | 29.9% | 5.3% | 84.9% | 75.4% | 9.51 |
| 2 | 8.7% | 0.3% | 85.7% | 59.5% | 6.86 |
| 3 | 1.9% | 0.0% | 91.3% | 69.0% | 9.78 |
| 4 | 2.6% | 0.3% | 86.5% | 68.3% | 14.60 |
| 5 | 3.7% | 0.5% | 93.7% | 84.1% | 18.39 |

Seed hubs sit at a fixed `edge_weight_sum` of 8.0 (below). The highest-degree
conv MU climbs past them by session 4 (14.60) and is more than double a hub
by session 5 (18.39). So the term buys hub entry points in session 1, and by
session 3 has already spent that down to noise — after which it is pure
conv-vs-conv reordering. Churn stays high throughout; only its *character*
changes.

### The seed_v3 interaction — the part to understand before the run

`seed_v3_mu_spec.md` §2 gave every v2 edge a reciprocal partner (32 → 64
edges) with the stated intent that "hub K1 centrality roughly doubles,
further consolidating hubs as entry points (desired)." Measured on the
frozen fixture:

| MU class | `edge_weight_sum` at seed |
|---|---|
| hub (×4) | 8.0 |
| bridge (×2) | 2.0 |
| spoke (×28) | 1.0 |

An 8:1 hub:spoke ratio, deliberate, and it exists *only* through the term V6
switches off. **V6 does not merely remove a nuisance centrality signal — it
dismantles the entry-point structure seed_v3 was designed around.** Hubs
carry index prose and no answer tokens; spokes carry the golds. A result
showing V6 changes retrieval is therefore ambiguous between "undirected
degree was noise" and "the fixture's hub-mediated entry path was load-bearing,"
and Finding 3 says the two are separable mainly in session 1. Read V6
per-session; a pooled headline will dilute the one place the mechanisms differ.

### What this predicts for R3-V6

1. **Not a null.** 88% K1 churn guarantees movement in the headline partition.
2. **The movement is concentrated in sessions 1–2.** Expect the learned–static
   delta to separate early and converge late.
3. **Direction is genuinely open.** Losing hub entry points could either help
   (hubs hold no golds; their K1 slots are arguably wasted) or hurt (the hub is
   the only structural route from a cluster-topic query to its spokes via K2).
   M45's partition is the instrument: if V6 moves turns from OUTBID toward
   REACHED it helped; if DISCONNECTED rises, the hub route mattered.
4. **Do not read V6 as a verdict on "centrality" in general.** It is a verdict
   on *undirected total degree* on *this fixture*. An out-degree-only variant is
   the untested middle case and is deliberately not pre-registered — see
   Milestone 47's open question.

### Limits

- K1-stage counterfactual only. K2 depends on the topology reachable from the
  changed K1 set; computing the full slate offline needs M45's reconstructor,
  which was not run here. The *run* measures that directly.
- Three baseline learned runs. cfgA/cfgB not included — cfgA's seed prior does
  not touch this path, and cfgB's probes are invalid (M44).
- Hub identification uses the frozen fixture's seed `edge_weight_sum` ≥ 8.0,
  which selects exactly the four cluster hubs.

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
  **Refined 2026-08-26** by the V6 pre-registration entry at the top of this
  file: true per-MU, but the aggregate slate effect is the opposite — the
  term's one systematic contribution is keeping seed hubs in K1, and removing
  it makes K1 *more* conv-dominated (88.8% -> 95.9%). Read that entry before
  running V6.
