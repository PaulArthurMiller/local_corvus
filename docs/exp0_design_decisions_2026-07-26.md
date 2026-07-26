# Experiment 0 — Design Decisions (2026-07-26)

Decision record for the first three-condition run on `seed_v3`'s counterfactual
content. Every choice below was open going into the 2026-07-26 `/smoketest`
session and is now committed in `plans/exp0_2026-07-26.json` and `config.yaml`.

This file exists because none of these choices is recoverable from the files
they live in. A plan JSON records *that* `weight_mode` is `sample` and *that*
`cluster_b`'s trained spoke is `b_spoke3`; it records nothing about why, what
the alternatives were, or what would have gone wrong otherwise. Future runs
will want to revisit these, and the reasoning is what makes that possible.

Related: `docs/m28_tiebreak_learned_mode_findings_2026-07-15.md` (the
measurements these decisions rest on), `BUILD-PLAN.md` Milestones 28–30,
`PROGRESS.md` 2026-07-26.

---

## Decision 1 — `edge_update.weight_mode: sample`

**Committed:** `config.yaml`, `edge_update.weight_mode`, changed from `mean`.

**Status: a requirement, not a preference.** This is the important one. Under
`mean` this experiment cannot produce a result at all, for mechanical reasons
that have nothing to do with whether learning works.

### The mechanism

Three facts about the system compose into the constraint:

1. **Every hub in `seed_v3` has 8 outgoing edges** — 7 spokes plus 1 bridge —
   and all of them are seeded `Beta(1, 1)`.
2. **K2 traversal is winner-take-all.** `retrieval_level2_top_k` is 1, so
   exactly one neighbour is followed from each K1 hub.
3. **Credit follows the traversal path.**
   `edge_update.process_verdict_for_learning()` walks `activated_mu_ids` and
   updates only the edges actually on the path that retrieved them. An edge
   that was never traversed receives no evidence, ever.

Under `weight_mode: mean`, `effective_weight()` computes
`alpha / (alpha + beta)` — exactly `0.5` for every seed edge. The K2 argmax
over 8 identical values is therefore **deterministic**: the same neighbour
wins on turn 1 and on every turn after it. And because that winner is the only
edge collecting evidence, its posterior mean rises above `0.5` while the other
seven stay pinned there — the initial tie-break outcome is locked in
permanently by the first turn.

### What that does to this specific plan

The tie-break winner per cluster is already known from three independent
measurements (M28, PR #50, PR #51):

| Cluster | K2 winner under `mean` | Consequence for this plan |
|---|---|---|
| `cluster_a` | the **bridge** | `a_spoke1` never traversed → flat forever |
| `cluster_b` | `b_spoke1` | `b_spoke3` (our trained spoke) never traversed → flat forever |
| `cluster_c` | the **bridge** | `c_spoke1` never traversed → flat forever |
| `cluster_d` | `d_spoke1` | `d_spoke5` (our trained spoke) never traversed → flat forever |

So under `mean`, **all four trained spokes would be structurally unreachable**,
every probe would read flat across all five sessions, and the experiment would
report a clean null. That null would say nothing about whether edge learning
works — it would only say that a deterministic argmax over tied values never
explores. A null result we cannot distinguish from "the mechanism is broken" is
worse than no result.

### Why `sample` fixes it

Under `weight_mode: sample`, `effective_weight()` draws an independent Thompson
sample per edge per `retrieve()` call. Each of a hub's 8 edges therefore wins
the K2 argmax roughly **1 turn in 8**. The trained spoke gets traversed on
about 12% of training turns; on those turns the bank's prompt targets that
spoke's content, so the judge grades the response "up", the spoke's edge gains
`alpha`, its draws shift upward, and it wins more often. That positive feedback
loop *is* the learning mechanism the experiment is built to observe. The six
untrained spokes are traversed too, but their content doesn't satisfy the
prompt's criteria, so they collect "down" evidence and fall away — which is the
discriminative signal.

### What this does not affect

**Probe measurement is unaffected either way.** Milestone 30's dual-mode
battery passes `weight_mode_override` on every `POST /admin/run-probe` call and
reads *both* regimes at every checkpoint — 1 deterministic `mean` pass plus
K=25 `sample` repeats — regardless of what is committed here. This value
governs **training turns only**. Choosing `sample` costs no measurement.

**The control conditions are unaffected.** `static_weight` and `random_weight`
read the scalar `weight` column and never consult `weight_mode` at all.

### Consequence to keep in view

`sample` means every spoke, trained or not, has a non-trivial chance of an
already-passing session-0 probe read purely from tie-break luck (measured at
0–36% across the 28 probes at N=25). That is inherent noise in the design, not
something spoke selection can remove — see Decision 3 and the M28 follow-up
findings doc. `sample_pass_rates` in report §6.1 exists to read through it by
averaging K=25 repeats per checkpoint rather than trusting one draw.

### Alternative considered

Running `mean` and `sample` as two plans in a queue, to demonstrate the
contrast empirically rather than argue it. Rejected for experiment 0: it
roughly doubles wall clock to produce a `mean` arm whose outcome is already
derivable from the mechanism above. Worth doing later if the argument itself
needs empirical backing for write-up.

---

## Decision 2 — Sizing: 5 series × 70 turns, `probe_ratio` 0.4

**Committed:** `plans/exp0_2026-07-26.json`.

| | Per condition |
|---|---|
| Total turns | 350 |
| Bulk turns | 210 |
| Probe turns | 140 (28 per series) |
| Measurement sessions | 5, contiguous |
| Prompt bank size | 210 |
| Wall clock | ~48 min/condition → ~2.4 h for all three |

### The trap this avoids

`probe_ratio` does **not** select a battery — it derives a *count* of probe
turns from `turns_per_series` (`round(turns_per_series × probe_ratio)`).
`assign_probe_schedule()` then walks the battery positionally: the k-th probe
turn of a series runs `probe_ids[k % B]`.

`probe_set_v3.yaml` is a **28-probe** battery (7 spokes × 4 clusters). If a
series has fewer than 28 probe turns, the schedule runs *the same leading
subset every session* — the function's own docstring calls this "a partial but
consistent battery".

The reference shape in `docs/harness_usage.md` (5 × 25 turns at
`probe_ratio` 0.4) is a **seed_v2-era** shape sized for v2's 10-probe battery.
Applied to v3 it yields 10 probe turns per series, which runs only
`vs_a1`…`vs_b3` — **clusters c and d would never be probed at all**, including
`c_spoke1` and `d_spoke5`, two of the four trained spokes. The run would have
completed, generated reports, and silently measured nothing in half the graph.

5 × 70 at 0.4 gives exactly 28 probe turns per series: one complete battery per
measurement session, five sessions, `measurement_session_index` contiguous 1–5.

### Why these numbers specifically

- **210 bulk turns/condition** clears the M19 science floor (≥100 bulk prompts)
  with margin. The margin matters under `sample`: a trained spoke is traversed
  only ~1 turn in 8, so ~26 of its cluster's ~53 bulk turns actually land on it
  across the run. Fewer bulk turns would leave too little evidence to move a
  posterior.
- **The 210-prompt bank covers the bulk turns exactly once.**
  `assign_prompt_ids()` draws without replacement and reshuffles only on
  exhaustion, so 210 prompts over 210 bulk turns means every prompt is seen
  once per condition, with no repetition asymmetry between conditions.
- **`probe_placement: "end"`** closes each series with the full battery, so
  each measurement session reads the graph after a complete block of 42 bulk
  turns.

### Alternatives considered

- **5 × 56 @ 0.5** — full battery, but only 140 bulk turns/condition. Viable;
  rejected for the evidence-per-edge reason above.
- **5 × 100 @ 0.28** — 360 bulk turns/condition, strongest evidence, ~3.9 h.
  The better choice if experiment 0's result is ambiguous for lack of training
  signal.

---

## Decision 3 — `trained_spokes`: `a_spoke1` / `b_spoke3` / `c_spoke1` / `d_spoke5`

**Committed:** `plans/exp0_2026-07-26.json`. Threaded into bank generation by
M29, and part of the bank's `content_hash`.

### Reasoning

Under `weight_mode: mean`, two spokes read as an **already-passing baseline**:
`vs_b1` at rank 4 and `vs_d1` at rank 2, stable across three independent
measurements. Training `b_spoke1` or `d_spoke1` would start the headline
fail→pass observable with zero headroom in half the clusters — the M28/M29
finding that motivated making this a plan field at all.

Clusters `a` and `c` have no such problem: the bridge wins their hub tie, so
*no* spoke surfaces at baseline and spoke1 keeps full headroom. Hence the
asymmetry — change `b` and `d`, leave `a` and `c` alone.

### The caveat, and the likely future change

**That reasoning is `mean`-specific, and experiment 0 runs under `sample`.**
Under `sample` there is no deterministic tie-break winner: all 8 of a hub's
edges are drawn independently, so **every spoke shares the same ~1/8 chance**
of winning K2 on any given call. The M28 follow-up measured this directly —
all 28 probes show a noisy 0–36% pass rate at N=25, with no clean split into
reliably-passing and reliably-failing sets, and a binomial check showing the
two probes that read 0/25 are statistically indistinguishable from noise.

So under the regime this experiment actually runs in, the a1/b3/c1/d5
asymmetry buys **nothing mechanically**. What it does buy is a cleaner
*`mean`-mode readout*: the dual-mode battery reports both regimes at every
checkpoint, and the `mean` half of the session-0 report will show the trained
spoke failing in all four clusters rather than passing in two. That is a
presentational benefit, not a causal one, and it costs nothing — which is why
it was kept.

> **Noted for a future run (Paul, 2026-07-26): move to the third option
> considered — a non-spoke1 trained spoke in all four clusters.** The
> justification is exactly the ~1/8 symmetry above: since every spoke has equal
> odds under `sample`, treating all four clusters identically is the more
> defensible design, and the current asymmetry is a `mean`-mode artifact
> carried into a `sample`-mode experiment. Deferred rather than adopted for
> experiment 0 only because changing `trained_spokes` changes the bank's
> `content_hash` and would require regenerating the prompt bank.

### Other alternative considered

All-spoke1 (the pre-M29 hardcoded default). Rejected: it is the one option that
is worse under *both* weight modes — no mechanical advantage under `sample`,
and the already-passing baseline problem under `mean`.

---

## Decision 4 — v3 fingerprint rule scoped to the BM25 keyword window

**Committed:** `scripts/harness/prompt_gen.py`, `fingerprint_scan()`.

Not one of the three questions above, but a design change made in the same
session and load-bearing for the bank, so recorded here.

`fingerprint_scan()`'s v3 tier-2 rule — a prompt must not contain any *other*
cluster's entry vocabulary — was enforced over the whole prompt. That is
stricter than the mechanism it protects, and unsatisfiable in practice.

**Unsatisfiable:** `seed_v3`'s four entry vocabularies are all ordinary English
words drawn from one shared semantic domain, so **27 of the 30 terms forbidden
per cluster are generic** — `data`, `type`, `source`, `format`, `version`,
`column`, `range`, `validation`, and so on. The sharpest case: `rule` belongs
to `cluster_c`, so no `cluster_a`/`b`/`d` prompt could contain the word "rule"
— while the entire fixture is a set of *rule families*. A real generation run
failed 8 of its first 10 prompts on this, with `rule` and `data` accounting for
19 of the 21 observed leaks.

**Stricter than necessary:** the rule exists to stop a prompt pulling another
cluster's hub into K1 and corrupting the hop-distance measurement. `retrieve()`
takes only the first `hyperparameters.retrieval_keyword_count` (5) non-stopword
tokens as BM25 keywords. A term past that window is never a keyword, so it
cannot pull in anything — it is invisible to retrieval.

**The rule now:**

| Tier | Scope | Rationale |
|---|---|---|
| Answer tokens (all clusters, incl. the prompt's own) | **whole prompt** | Position-independent: an answer token anywhere hands the agent the answer without retrieval, corrupting the judge's verdict — separately from any BM25 effect |
| Other clusters' entry vocabulary + family name | **first 5 non-stopword tokens** | Exactly the window `retrieve()` reads |
| Own cluster's entry vocabulary | unrestricted | This is how BM25 is *meant* to reach the hub |

Two supporting changes fell out of the same trial run:

- **The v3 generation prompt now states these rules.** It previously inherited
  v2's blanket "must NOT quote or repeat the cluster's distinctive vocabulary",
  which points the wrong way for v3 — a v3 prompt *must* front-load its own
  cluster's entry vocabulary or BM25 never reaches the hub and every condition
  flatlines. This is the failure `docs/harness_usage.md` §4 records from the
  first 75-prompt draft (48/75 found nothing at baseline).
- **`generation_max_tokens()` and streaming.** The flat 400-tokens-per-target
  budget broke both ways once adaptive thinking became the model default: batch
  calls truncated mid-array, and the per-target retry path could be consumed
  entirely by thinking and return empty. Generation now has a floor plus
  headroom, and streams — a 53-target batch needs a budget large enough that a
  non-streaming request risks an SDK HTTP timeout.

**Verification:** a 12-prompt paid trial produced 12/12 validated, 0 excluded,
and 12/12 with at least 2 of their first 5 BM25 keywords hitting the target
cluster's hub — the ≥2 multi-hit threshold `retrieve()` requires to admit a hub
into K1 at all.

---

## Open, deliberately not decided here

- **Higher-N `sample`-mode spoke measurement** (option 1 in the M28 follow-up).
  Not run: that doc's own binomial analysis argues the 28 spokes are
  structurally symmetric under `sample`, so a larger N would most likely just
  re-confirm it. Not decision-relevant for `trained_spokes`.
- **`budget_cap`** is set to 25.0 by convention, carried over from the pilot
  plan. Local Ollama judge calls cost $0, so it only ever binds on Sonnet bank
  generation.
- **`evidence_cap`** remains disabled (`config.yaml`), unchanged from the pilot.
- **`credit_scale` / `credit_lambda`** unchanged at 1.0 / 0.6. Untuned; a
  plausible thing to sweep if experiment 0's learning signal is weak.
