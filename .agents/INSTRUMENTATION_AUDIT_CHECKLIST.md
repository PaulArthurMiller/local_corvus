# Local Corvus — Instrumentation Audit Checklist (v2)

**Purpose:** A pre-experiment gate. For each field below, the question is not "do we log something" but **"can the claim it supports be reconstructed after the run without re-executing?"** Walk the codebase against this list before smoketest sign-off.

**How to use:** For each row, CC should fill in three things — (1) does the field exist, (2) where it is written / how it is extracted, (3) where/how it is stored. Anything marked *MISSING* or *PARTIAL* is a gate item to resolve before the small experiment.

**Organizing principle:** Log the *intermediate* quantities, not just inputs and final outputs. Any value computed and discarded — entry-point scores, pre-decay weights, the candidate set before ranking, the traversal path through the graph — is a value you'll wish you had when a curve does something surprising. Storage is cheap; re-running a battery to recover a discarded number is not.

**v2 changes:** Added Claim 2b (recalled-MU chains / traversal path), edge-creation events alongside edge-updates, per-probe phenomenon tagging, bridge-MU tracking, a feedback-event log, and seed-fixture provenance. These reflect the experiment design finalized after the original audit: three separable learning phenomena, two-pair conceptual-area structure with named bridge MUs, Gemma-generated frozen seed fixtures, and sparse decisive feedback with probes held read-only.

**Gap analysis completed:** 2026-06-21. See Milestones 8 and 9 in BUILD-PLAN.md for the remediation plan.

---

## Claim 1 — "Learning occurred (vs. didn't)"

*Requires the three condition lines to be strictly comparable against the same probes at matched intervals.*

| Field | Why it's needed | Exists? | Where written / extracted | Where stored |
|---|---|---|---|---|
| `condition_label` (learning / static-weight / random-weight) | The learning curve is meaningless without the control lines beside it. Every record must be attributable to a condition. | MISSING | Not implemented | — |
| `test_prompt_set_version` | Proves the probes were identical across conditions. If the set drifted, no cross-condition comparison is valid. | MISSING | Not implemented | — |
| `measurement_session_index` (count, not wall-clock) | Measurement points must align by session-count across conditions, not by time. | MISSING | Not implemented | — |
| `score_per_test_prompt` (per-probe, not just aggregate) | You need to see *which* probes moved, not an averaged-away aggregate — that's where cross-cluster signal hides. | MISSING | Not implemented | — |

---

## Claim 2 — "This specific retrieval was caused by learning"

*The traceability path. At each retrieval, capture the decomposition, not just the final ranked list.*

| Field | Why it's needed | Exists? | Where written / extracted | Where stored |
|---|---|---|---|---|
| `retrieved_mu_set` (with final scores) | The result you're explaining. | PARTIAL | `activated_mu_ids` written in `core/orchestrator.py:316–330`. Scores computed inside `memory/retrieval.py:130` (`_k1_score`) and `k2_scores` dict but not returned from `retrieve()`. | `analytics.activated_mu_ids` (JSON list of IDs only; per-MU final scores are discarded) |
| `bm25_entry_score_per_mu` | Separates lexical-match contribution from learned contribution. | MISSING | Computed as `candidate_scores` dict in `memory/retrieval.py:106–107` inside `retrieve()`, but not included in `RetrievalResult` and not passed to the orchestrator. | — |
| `edge_contribution_per_candidate` | The lift/suppression each edge applied — the learned signal itself. | MISSING | Computed as `edge_weight_sum.get(mu_id, 0.0)` inside `_k1_score()` in `memory/retrieval.py:129–131`, but not returned or stored. | — |
| `edge_weights_as_of_this_turn` | Final scores are uninterpretable without the weights that produced them at that moment. | PARTIAL | Full graph snapshots written after every interaction via `memory/snapshot.py:take_snapshot()` called from `core/orchestrator.py:334`. No explicit link from a `run_id` to the snapshot file taken at that turn. | `/app/memory/snapshots/*.json.gz` (full graph state per interaction; not indexed by `run_id`) |
| `assembled_prompt_sent_to_gemma` | Your prior failure mode (LC reworking its own output) was a prompt-construction bug invisible in any metric — only catchable by inspecting the actual assembled context. | PARTIAL | A `prompt_mu_id` in `RunRecord` links to a prompt MU in `memory_units` containing `prompt_text`. But `prompt_text` is the context+user string — not the full Ollama `messages` array (system role, session history turns, user content-parts) actually sent to the model. `_build_ollama_messages()` in `core/orchestrator.py:352` constructs the full array but does not persist it. | `memory_units.raw_content` for the prompt MU (context block + user message text; not the full messages array with history and roles) |

---

## Claim 2b — Recalled-MU chains (traversal path) **[NEW]**

*Claim 2 captures the retrieved set and its scores; this captures the **walk** that produced it. Without the path, a chain of recalled MUs is reconstructed by inference rather than read directly — and chain structure is exactly where edge-driven behavior lives.*

| Field | Why it's needed | Exists? | Where written / extracted | Where stored |
|---|---|---|---|---|
| `entry_point_mu_ids` (which MUs the BM25 funnel seeded the walk from) | The chain's roots. Distinguishes "surfaced via lexical entry" from "surfaced via edge traversal." | MISSING | K1 IDs computed as `k1_ids` in `memory/retrieval.py:133–138` but not included in `RetrievalResult` or passed to the orchestrator. | — |
| `traversal_path` (ordered: source MU → edge_id → target MU, per hop) | The actual chain. Lets you see *how* a distant MU was reached, not just that it appeared in the set. | MISSING | K2 traversal loop in `memory/retrieval.py:145–153` iterates `(mu_id, edge, target)` triples but records only `edge_id` in `traversed_edge_ids` — no structured `{source, edge_id, target, depth}` record. | — |
| `edge_id_per_hop` (tied to the per-turn edge-weight snapshot) | Links each hop to the weight that justified taking it — the join key between the chain and the weight-change record. | PARTIAL | `traversed_edge_ids` is collected in `memory/retrieval.py:153` and returned as `activated_edge_ids` in `RetrievalResult`. Included in `RunRecord.activated_edge_ids`. But stored as an unordered flat list with no source/target/depth annotation — not as a hop record. | `analytics.activated_edge_ids` (JSON list of edge IDs; hop structure and order are lost) |
| `hop_depth_per_retrieved_mu` | Cross-cluster transfer should surface MUs at greater traversal depth; depth is the quantitative fingerprint of transfer vs. direct lexical hit. | MISSING | K1 vs K2 is implicitly known (K1 = BM25 entry, K2 = one traversal hop) but not stored as a numeric depth per retrieved MU. | — |
| `candidate_set_before_ranking` (pre-cutoff funnel contents) | The MUs considered and dropped. "Why didn't the gold MU surface" is unanswerable without the rejected candidates. | MISSING | `multi_hit` list computed in `memory/retrieval.py:116` contains all candidates with ≥2 keyword hits before K1 cutoff, but not returned from `retrieve()` or stored anywhere. | — |

---

## Claim 3 — "Component X of the algorithm drove the learning"

*An experimental-design requirement, not solvable by logging alone. Each knob's setting must be recorded and tied to the config hash.*

| Field | Why it's needed | Exists? | Where written / extracted | Where stored |
|---|---|---|---|---|
| `config_hash` (or full resolved config.yaml, not the file path) | Proves two runs were comparable and makes either reproducible. The *resolved* config, after defaults applied — not just the file. | MISSING | Not implemented | — |
| `knob_settings` (decay on/off, edge-update rule id, reinforcement magnitude, etc., each named) | Lets you later ask "did decay-on runs differ from decay-off" even if the small run wasn't designed as a clean contrast. | PARTIAL | `hyperparameters_snapshot` written in `core/orchestrator.py:329` captures the `hyperparameters` section of `config.yaml`. Missing: `model`, `gate`, and `tools` sections. No explicit per-knob annotation or on/off flags. | `analytics.hyperparameters_snapshot` (JSON; partial config — `hyperparameters` section only) |
| `learning_algo_id` | Identifies which algorithm produced this run for the multi-algo comparison. | YES | Set from `config.hyperparameters.algorithm` in `core/orchestrator.py:330`. | `analytics.algorithm` (TEXT column) |

---

## Per-edge-update record (the "why did retrieval change" raw material)

*Each time an edge weight changes, capture the transition. Without this, edge-weight evolution is a black box.*

| Field | Why it's needed | Exists? | Where written / extracted | Where stored |
|---|---|---|---|---|
| `edge_id` (source MU, target MU) | Identifies which connection changed. | PARTIAL | `edge_id` included in the `weight_updates` list built in `backend/api/chat.py:223–228`. `source_mu_id` and `target_mu_id` are not included in the update record — you must join to the `edges` table to recover them. | `analytics.weight_updates` JSON: `[{edge_id, old_weight, new_weight}]` — endpoints missing |
| `old_weight` → `new_weight` | The transition itself. | YES | Captured in the `weight_updates` list in `backend/api/chat.py:223–228`. `old_weight` fetched from the edge record before `update_edge_weight()` is called. | `analytics.weight_updates` JSON |
| `update_trigger` (what feedback caused it; FK to feedback-event id) | Distinguishes reinforcement from decay from negative correction — and ties the change to the specific feedback that caused it. | MISSING | No `feedback_events` entity exists. Weight updates are tied to `run_id` implicitly (they're stored on the run record), but there is no feedback event FK or trigger-type field. | — |
| `turn_index` / `session_index` | Lets you replay edge evolution against the learning curve. | MISSING | Only wall-clock `timestamp` exists on each run record. No sequential turn or session index is tracked. | — |

---

## Per-edge-creation record **[NEW]**

*Edge births are a distinct event type from weight changes. In an accretion-plus-reweighting system you must be able to ask "when did this connection first exist," separately from "how did its weight evolve."*

| Field | Why it's needed | Exists? | Where written / extracted | Where stored |
|---|---|---|---|---|
| `edge_id` + `creation_turn_index` / `session_index` | When the connection first appeared. Separates accretion from reweighting in the timeline. | PARTIAL | `edge_id` and `created_at` (wall-clock timestamp) stored at creation via `memory/store.py:GraphStore.create_edge()`. No turn index or session index at creation time. | `edges.created_at` (ISO-8601 wall-clock; no sequential index) |
| `creation_trigger` (co-activation, feedback, traversal, etc.) | Why the edge was created — the accretion rule in action. | MISSING | All three edge-creation sites in `core/orchestrator.py` (Steps 5 and 9: prompt→input, prompt→retrieved, input→output) call `graph_store.create_edge()` with no trigger label. | — |
| `initial_weight` | The weight-init value actually applied (one of your flagged open questions). | PARTIAL | The `weight` column in the `edges` table is the weight at creation time, which equals `initial_edge_weight` from config (0.5). This recovers the initial value as long as no `update_edge_weight()` call has run yet — but there is no explicit "initial_weight" field frozen at creation. | `edges.weight` (current weight, equals initial before first update; no separate initial_weight column) |
| `endpoints_source_type` (user_input / agent_output / tool_call / tool_result for each end) | Lets you see which MU-type pairings the system tends to connect — a structural learning signal independent of weights. | MISSING | Not stored in the `edges` table. Recoverable by joining `edges.source_mu_id` → `memory_units.source_type` and `edges.target_mu_id` → `memory_units.source_type`, but not denormalized. | — (reconstructable via join, not stored directly) |

---

## Feedback-event log **[NEW]**

*The reinforcement signal must be a recorded, replayable event — not a live judgment — or "identical runs" aren't identical. Sparse and decisive by design: pass / named-violation / silence.*

| Field | Why it's needed | Exists? | Where written / extracted | Where stored |
|---|---|---|---|---|
| `feedback_event_id` | The FK that edge-updates point back to via `update_trigger`. | MISSING | No `feedback_events` table or entity exists. Feedback is stored only as a scalar `feedback_signal` integer on the run record. | — |
| `verdict` (thumbs_up / thumbs_down / none) | The three-valued signal; "none" (the ambiguous middle) must be logged explicitly, not left absent. | PARTIAL | `feedback_signal` in `RunRecord` / `analytics` table stores +1 (thumbs_up) or -1 (thumbs_down). The "none" state is represented by NULL — absence, not an explicit value. The `POST /feedback` endpoint in `backend/api/chat.py:160` only fires when the user clicks a button; silence is never recorded. | `analytics.feedback_signal` (INTEGER: 1 / -1 / NULL — not a three-valued explicit record) |
| `verdict_basis` (checklist-pass / confabulation / rule-violation, naming the item) | Makes the signal objective and replayable; names *which* non-negotiable triggered it. | MISSING | Not captured anywhere. `FeedbackRequest` in `backend/api/chat.py:148` only accepts `run_id` and `signal`. | — |
| `turn_index` + `is_probe_turn` (bool) | Enforces the read-only-probe rule: probe turns must show no feedback event. A probe with a feedback event is a contamination bug. | MISSING | No probe turn concept. No `is_probe_turn` flag anywhere in the codebase. | — |

---

## Per-probe phenomenon & ground-truth labels **[EXPANDED]**

*Capture the negative space (which MUs you WANTED retrieved but weren't) AND tag which of the three learning phenomena each probe tests, so the headline metrics don't blur into one composite signal.*

| Field | Why it's needed | Exists? | Where written / extracted | Where stored |
|---|---|---|---|---|
| `gold_relevant_mus_per_test_prompt` (hand-labeled for the small frozen test set) | Without a relevance ground truth, retrieval quality stays an eyeball judgment. With it, precision/recall per probe. | MISSING | No probe set, no gold labels. | — |
| `retrieval_precision` / `retrieval_recall` (per measurement) | The precise metric the gold labels enable. | MISSING | Not computed or stored anywhere. | — |
| `phenomenon_type` (ranking_improvement / cross_cluster_transfer / process_rule_adherence) | The three phenomena have different measurement loci (retrieval vs. output). Tagging keeps "ranking improved but adherence flat" legible as a finding instead of an averaged blur. | MISSING | No phenomenon tagging. | — |
| `gold_mu_rank` (rank of gold MU in the retrieved list, per probe per measurement) | Improvement = this number descending over the session. The cleanest within-cluster learning metric. | MISSING | Not tracked. | — |
| `is_bridge_mu` + `bridge_pair_id` (flags the cross-cluster bridge nodes) | Cross-cluster transfer is measured as a *specific* bridge MU's rank moving on an *adjacent-cluster* probe. The bridge node must be individually trackable, not buried in the gold set. | MISSING | Not in any schema. | — |

---

## Graph snapshots

*Snapshots keep counterfactuals answerable and protect reproducibility.*

| Field | Why it's needed | Exists? | Where written / extracted | Where stored |
|---|---|---|---|---|
| `snapshot_before_each_session_battery` | Reproducible starting state per battery. | MISSING | No battery concept. `take_snapshot()` in `memory/snapshot.py` fires after every interaction (`core/orchestrator.py:334`), not at battery boundaries. | — |
| `snapshot_after_each_session_battery` | Diff target for what a battery changed. | MISSING | Same as above — no battery-level snapshot exists. | — |
| `snapshot_before_any_pruning` | Pruning is destructive; without a pre-prune snapshot, "would this MU have been retrieved in the control" becomes unanswerable and breaks cross-condition comparability. | MISSING | Pruning not yet implemented. No pre-pruning snapshot hook exists. | — |

---

## Seed-fixture provenance **[NEW]**

*The Gemma-generated, context-free seed MUs are the shared starting state for every run in a class. Each run must prove it started from the identical frozen fixture.*

| Field | Why it's needed | Exists? | Where written / extracted | Where stored |
|---|---|---|---|---|
| `seed_fixture_version` / `seed_manifest_hash` | Proves every run (learning, static, random) began from the same seed graph. A drifted fixture invalidates cross-condition comparison as surely as a drifted probe set. | MISSING | No seed fixture concept in the codebase. | — |
| `seed_mu_generation_record` (prompt → tool_call → tool_result → response, per seed) | Documents that seeds are true Gemma-class outputs with no extra context — the fidelity guarantee. Lets you regenerate or audit the fixture. | MISSING | Not implemented. | — |
| `seed_mu_type_distribution` (counts per source_type, incl. tool MUs) | Confirms the graph is representative (heterogeneous MU types present), not text-only. | MISSING | Not implemented. | — |

---

## Sign-off gate

The smoketest is not complete until:

1. Every *Exists?* cell is filled, with no gate item left *MISSING* or *PARTIAL* on:
   - condition labels + frozen probe-set version
   - per-turn score decomposition + assembled prompt
   - traversal path (Claim 2b)
   - edge-update **and** edge-creation records
   - feedback-event log with probe-turns showing no feedback
   - gold MUs + phenomenon tags + bridge-MU flags
   - graph snapshots (incl. pre-prune)
   - seed-fixture version/manifest
2. A single replayed probe can be walked end-to-end from logs alone: feedback event → edge update/creation → weight-as-of-turn → traversal path → retrieved set → gold-MU rank — with no step requiring re-execution.
3. A probe turn is confirmed to produce a measurement record but **no** feedback event.

**Current status (2026-06-21):** Gate items are MISSING or PARTIAL across all eight categories above. Milestones 8 and 9 in BUILD-PLAN.md address these gaps. The sign-off gate cannot be satisfied until both milestones are complete and a populated probe set / seed fixture exists.
