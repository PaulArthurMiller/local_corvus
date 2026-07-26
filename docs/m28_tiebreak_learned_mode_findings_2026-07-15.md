# M28 Tie-Break — Learned-Condition Follow-Up Findings (2026-07-15)

## Why this doc exists

The M28 baseline check and its first re-confirmation (see PR #50) only ever
measured the tie-break under `RUN_CONDITION` defaulting to `static_weight`
(the env var was never set, and `orchestrator.py` silently defaults to
`static_weight` when `RUN_CONDITION` is unset or invalid). Neither run ever
exercised `condition="learned"`, and `weight_mode: sample` — the mode the
Beta-counter learning mechanism actually depends on for exploration — was
never tested at all. This is the same class of hazard as the project's
probe-integrity principle (`retrieve()` must run with `condition=RUN_CONDITION`,
not silently default): **any baseline claim must record which `condition` and
`weight_mode` it was measured under, or the exact same silent-default mistake
recurs.** See the note added to `BUILD-PLAN.md`'s Status block.

This doc records the follow-up run that closes that gap.

## Run log

| # | Condition | weight_mode | Runs | Fixture | Result |
|---|---|---|---|---|---|
| 1 | `static_weight` (env unset, default) | `mean` | 1× (M28 original) | `seed_v3` fresh reseed | 26/28 `None`; `vs_b1`→4, `vs_d1`→2 |
| 2 | `static_weight` (env unset, default) | `mean` | 1× (PR #50, independent reseed) | `seed_v3` fresh reseed | identical to #1 |
| 3 | `learned` (`RUN_CONDITION=learned` explicit) | `mean` | 1× | `seed_v3` fresh reseed | identical to #1/#2 — `vs_b1`→4, `vs_d1`→2, 26/28 `None` |
| 4 | `learned` (`RUN_CONDITION=learned` explicit) | `sample` | 25× (no reseed between repeats — probes are read-only) | `seed_v3`, same graph as run #3 | see table below — **no probe is a clean, reproducible fail** |

`EXPERIMENT_ID=smoketest-2026-07-15-learned-check` (runs #3–4), backend
freshly rebuilt via `docker compose down` / `docker compose up -d --build
backend` before run #3, no code changes at any point — `config.yaml`'s
`edge_update.weight_mode` was toggled `mean`→`sample`→`mean` for the
duration of this check only and is back at its committed value (`mean`).

**Run #3 confirms CC's prediction exactly:** with `alpha=beta=1` on every
seed edge, `weight_mode: mean` computes `alpha/(alpha+beta) = 0.5` for every
edge regardless of `condition` — so `learned`/`mean` is numerically identical
to the `static_weight` path, and the result reproduces bit-for-bit. Not a bug.

## Run #4 — pass-frequency table (learned / sample, N=25, threshold=rank≤4)

```
probe    pass_freq   passes/N
vs_a1      0.04        1/25
vs_a2      0.16        4/25
vs_a3      0.00        0/25   *
vs_a4      0.28        7/25
vs_a5      0.04        1/25
vs_a6      0.00        0/25   *
vs_a7      0.08        2/25
vs_b1      0.04        1/25
vs_b2      0.20        5/25
vs_b3      0.20        5/25
vs_b4      0.24        6/25
vs_b5      0.16        4/25
vs_b6      0.36        9/25
vs_b7      0.16        4/25
vs_c1      0.12        3/25
vs_c2      0.08        2/25
vs_c3      0.12        3/25
vs_c4      0.32        8/25
vs_c5      0.16        4/25
vs_c6      0.12        3/25
vs_c7      0.08        2/25
vs_d1      0.16        4/25
vs_d2      0.04        1/25
vs_d3      0.20        5/25
vs_d4      0.08        2/25
vs_d5      0.12        3/25
vs_d6      0.08        2/25
vs_d7      0.16        4/25
```

`*` = the only two probes reading exactly 0/25.

## The headline finding: sample mode is a fundamentally different regime, not just "the same tie, randomized"

CC's advice framed sample-mode headroom selection as: measure baseline pass
*probability* across repeats, and pick spokes whose probability is ≈0
("genuinely unreachable", not just an unlucky coin flip). The data doesn't
support that framing for this fixture. Under `mean`, the tie-break is a
**hard, deterministic split**: 26 probes read `None` on every measurement,
ever, and exactly 2 (`vs_b1`, `vs_d1`) read a fixed passing rank on every
measurement, ever. Under `sample`, **every single probe** shows a non-zero
or near-zero pass rate scattered across a noisy 0–36% band — there is no
clean split into a reliably-failing set and a reliably-passing set. The
mechanism: every hub has 8 outgoing edges (7 spokes + 1 bridge) all seeded
`Beta(1,1)`; under `weight_mode: sample`, each `retrieve()` call draws an
independent Thompson sample per edge, so which neighbor wins the K2 argmax
— and even whether the hub survives into the K1 top-3 at all, since K1's
own `edge_weight_sum` ranking is *also* computed from sampled weights — is
close to a fresh ~1-in-8 coin flip every single call, for every cluster.

**Statistical check on the two apparent "clean" spokes.** If every spoke has
a true win probability of `p ≈ 1/8`, the chance any one of them reads 0/25
by pure sampling noise is `(7/8)^25 ≈ 3.6%`. Across 28 independently-tested
probes, the expected count of probes that read 0/25 *purely by chance* is
`28 × 0.036 ≈ 0.99` — and the chance of seeing **two or more** such
false negatives by chance alone is **~26%** (Poisson approximation). In
other words: observing exactly `vs_a3` and `vs_a6` at 0/25 is **not
statistically distinguishable from noise at N=25** — it is roughly what
you'd expect to see even if all 28 spokes are structurally identical in
their tie-break odds. **N=25 is underpowered to identify a genuinely
"always-fails" spoke under `weight_mode: sample`; the apparent clean-fail
set should not be trusted as-is.**

**What this means for M29's `trained_spokes` selection:** the entire premise
that motivated M29 — "some spokes deterministically already pass at session
0, so pick a different one" — is a `weight_mode: mean`-specific problem. It
does not carry over to `sample` in the form CC's advice anticipated (a
stable subset of always-fail spokes to select from). Under `sample`, *every*
spoke starts with roughly the same non-trivial (~10–20%) chance of an
already-passing session-0 draw purely from tie-break luck, and that is
inherent measurement noise in the experimental design, not something spoke
pre-selection can route around by picking "the right" spoke.

## K2-traversal-only property — re-verified under learned/sample

M28's original trace inspection (confirming `vs_b1`/`vs_d1`'s baseline
passes arrive only via K2 hub→spoke traversal, never a direct BM25 lexical
hit) was done under the `mean` path and never re-checked under `sample` —
flagged as "inherited, not re-proven" in the first smoketest pass. Bundled a
direct trace check into this run: called `retrieve()` against the live
`seed_v3` graph under `condition="learned"`, `weight_mode="sample"` in a
loop until each of `vs_a1`, `vs_b6`, `vs_d1` produced a pass, then inspected
`RetrievalResult.hop_depth_per_retrieved_mu` for the gold MU.

```
vs_d1: found on attempt 0,  rank=2, hop_depth=1 (K2 traversal)
vs_b6: found on attempt 11, rank=4, hop_depth=1 (K2 traversal)
vs_a1: found on attempt 29, rank=4, hop_depth=1 (K2 traversal)
```

All three arrive at `hop_depth=1` (K2), never `hop_depth=0` (a direct BM25
lexical hit). The property holds under `learned`/`sample` too — entry
vocabulary isn't leaking into a lexical shortcut even when the tie-break
mechanism changes; the pass, when it happens, is always the intended
traversal-only mechanism.

## Open question for Paul — how to proceed

1. **Run a much larger N under `sample`** (cheap — no LLM calls; 25 repeats
   of the 28-probe battery took ~66s, so N=200–500 is a few minutes) to get
   enough power to actually distinguish a genuinely-low-probability spoke
   from the ~12% noise floor, if one exists. Given the data so far, it's not
   obvious one does — the whole cluster of 28 probes may simply be
   structurally symmetric under `sample`.
2. **Accept that under `sample`, spoke pre-selection doesn't remove
   session-0 tie-break noise** — treat the ~10–20% baseline-pass rate as
   inherent to the design (same for whichever spoke is trained) and handle
   it via measurement methodology instead (e.g., averaging pass/fail over
   multiple session-0 probe batteries in the real RunPlan, rather than a
   single measurement) rather than via `trained_spokes` selection.
3. Some combination: still avoid any spoke that looks like an outlier at
   higher N, but stop expecting a clean zero-probability set to exist.

This doc doesn't pick trained spokes or touch `--generate-bank` — both
remain Paul's call, now with the full picture instead of a `mean`-only one.
