# Local Corvus — Instrumentation Audit Checklist

**Purpose:** A pre-experiment gate. For each field below, the question is not "do we log something" but **"can the claim it supports be reconstructed after the run without re-executing?"** Walk the codebase against this list before smoketest sign-off.

**How to use:** For each row, CC should fill in three things — (1) does the field exist, (2) where it is written / how it is extracted, (3) where/how it is stored. Anything marked *MISSING* or *PARTIAL* is a gate item to resolve before the small experiment.

**Organizing principle:** Log the *intermediate* quantities, not just inputs and final outputs. Any value computed and discarded — entry-point scores, pre-decay weights, the candidate set before ranking — is a value you'll wish you had when a curve does something surprising. Storage is cheap; re-running a battery to recover a discarded number is not.

---

## Claim 1 — "Learning occurred (vs. didn't)"

*Requires the three condition lines to be strictly comparable against the same probes at matched intervals.*

| Field | Why it's needed | Exists? | Where written / extracted | Where stored |
|---|---|---|---|---|
| `condition_label` (learning / no-change / sham-control) | The learning curve is meaningless without the control lines beside it. Every record must be attributable to a condition. | | | |
| `test_prompt_set_version` | Proves the probes were identical across conditions. If the set drifted, no cross-condition comparison is valid. | | | |
| `measurement_session_index` (count, not wall-clock) | Measurement points must align by session-count across conditions, not by time. | | | |
| `score_per_test_prompt` (per-probe, not just aggregate) | You need to see *which* probes moved, not an averaged-away aggregate — that's where cross-cluster signal hides. | | | |
| `condition_shares_starting_graph` (snapshot id) | All three lines must start from the identical wiped graph state for comparability. | | | |

---

## Claim 2 — "This specific retrieval was caused by learning"

*The traceability path — most likely to be under-instrumented. If you only log the final ranked list, you see THAT ranking changed but not WHY.*

| Field | Why it's needed | Exists? | Where written / extracted | Where stored |
|---|---|---|---|---|
| `retrieved_mu_set` (per turn) | The candidate list actually returned. | | | |
| `bm25_entrypoint_score` (per candidate) | The retrieval-substrate contribution, isolated from edge effects. | | | |
| `edge_contributions` (per candidate: which edges lifted/suppressed it, and by how much) | This is the raw material for attributing a ranking shift to reweighting vs. BM25 noise vs. decay. | | | |
| `edge_weights_at_retrieval_time` (snapshot of weights as they stood that turn) | Final weights logged later can't reconstruct what the ranking saw at the moment. | | | |
| `final_rank_and_score` (per candidate) | The output of the ranking layer, to diff against the decomposition above. | | | |
| `assembled_prompt_sent_to_gemma` (verbatim) | Your prior failure mode (LC reworking prior output instead of answering) was a prompt-construction bug invisible to any metric — only catchable by inspecting the actual assembled context. | | | |
| `gemma_raw_output` (verbatim) | Needed to separate retrieval quality from generation behavior. | | | |

---

## Claim 3 — "Component X of the algorithm drove the learning"

*Not solvable by logging alone — an experimental-design requirement. You cannot decompose contributions post-hoc from a single run where everything was on. For the SMALL experiment, the requirement is just that each knob's setting is recorded and tied to the config hash, so contrasts can be asked of the data later.*

| Field | Why it's needed | Exists? | Where written / extracted | Where stored |
|---|---|---|---|---|
| `config_hash` (or full resolved config.yaml, not the file path) | Proves two runs were comparable and makes either reproducible. The *resolved* config, after defaults applied — not just the file. | | | |
| `knob_settings` (decay on/off, edge-update rule id, reinforcement magnitude, etc., each named) | Lets you later ask "did decay-on runs differ from decay-off" even if the small run wasn't designed as a clean contrast. | | | |
| `learning_algo_id` | Identifies which algorithm produced this run for the multi-algo comparison. | | | |

---

## Per-edge-update record (the "why did retrieval change" raw material)

*Each time an edge weight changes, capture the transition. Without this, edge-weight evolution is a black box.*

| Field | Why it's needed | Exists? | Where written / extracted | Where stored |
|---|---|---|---|---|
| `edge_id` (source MU, target MU) | Identifies which connection changed. | | | |
| `old_weight` → `new_weight` | The transition itself. | | | |
| `update_trigger` (what feedback caused it) | Distinguishes reinforcement from decay from negative correction. | | | |
| `turn_index` / `session_index` | Lets you replay edge evolution against the learning curve. | | | |

---

## Graph snapshots

*Snapshots keep counterfactuals answerable and protect reproducibility.*

| Field | Why it's needed | Exists? | Where written / extracted | Where stored |
|---|---|---|---|---|
| `snapshot_before_each_session_battery` | Reproducible starting state per battery. | | | |
| `snapshot_after_each_session_battery` | Diff target for what a battery changed. | | | |
| `snapshot_before_any_pruning` | Pruning is destructive; without a pre-prune snapshot, "would this MU have been retrieved in the control" becomes unanswerable and breaks cross-condition comparability. | | | |

---

## Retrieval ground-truth (the highest-value, easy-to-miss item)

*Capture the negative space: for each test prompt, which MUs you WANTED retrieved but weren't. Turns "retrieval got better" from a judgment call into precision/recall on retrieval itself — the cleanest operationalization of the core research question, and what separates cross-cluster transfer from lookup.*

| Field | Why it's needed | Exists? | Where written / extracted | Where stored |
|---|---|---|---|---|
| `gold_relevant_mus_per_test_prompt` (hand-labeled for the small frozen test set) | Without a relevance ground truth, retrieval quality stays an eyeball judgment. With it, you get precision/recall per probe. Labeling cost is real but bounded for a small frozen set. | | | |
| `retrieval_precision` / `retrieval_recall` (computed per measurement) | The precise metric the gold labels enable; the direct measure of whether the right prior context surfaced. | | | |

---

## Sign-off gate

The smoketest is not complete until:

1. Every *Exists?* cell above reads **YES** or has an explicit, justified **DEFER** with a note on which claim it weakens.
2. The three condition lines are confirmed to run against a frozen, identical test-prompt set at matched session-counts.
3. A trial micro-run (a handful of turns) has been inspected end-to-end: pick one retrieval and manually walk it back from final rank → edge contributions → edge weights at that turn → assembled prompt. If that walk-back is possible by hand, the traceability path is real. If it isn't, instrumentation is incomplete regardless of what the table says.

---

## Next step (for CC, after confirmation)

Once the above is verified, produce the operations doc: for every field, record the canonical **variable name**, **how it is tracked or extracted** (the code path that produces it), and **how/where it is stored** (table, column, file, format). That document becomes the shared reference for analyzing results so naming stays consistent across the analytics layer, the experiment scripts, and result interpretation.