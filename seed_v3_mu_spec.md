# Seed Fixture v3 — MU Content Specification

Status: **APPROVED by Paul (2026-07-11).** This is the frozen source of truth
for Milestone 25 (`generate_seed_v3.py` / `verify_seed_v3.py`). Token choices
and content gists below are final unless a milestone build surfaces a problem
that requires revision — any such change should be made here first, then
re-approved, since this file is what `/build` implements against.

Counterfactual seed content for the full experimental run. Replaces seed_v2's
true-in-general content with invented, unguessable specifics so that response
quality — and therefore judge verdicts and edge updates — depends causally on
retrieval. Topology, MU IDs, cluster themes, and edge layout are carried over
from v2 unchanged unless the traversal-direction check (§2) says otherwise.

---

## 1. Design principles

### Two-tier vocabulary

Every cluster now has two strictly separated vocabulary tiers:

| Tier | What it is | Where it lives | Where it's allowed |
|---|---|---|---|
| **Entry vocabulary** | Natural thematic words (e.g. `format`, `delimiter`, `schema`) | Saturated in the **hub**; each spoke uses only its one subtopic anchor word | Prompts front-load these (BM25 entry). Freely usable in prompt text |
| **Answer tokens** | Invented rule IDs and value tokens (e.g. `K-2`, `TILDEBAR`) | Exactly **one spoke each** | Required in the agent's *response* (judge criteria). **Forbidden in prompt text.** Never in the hub |

The intended retrieval physics: a prompt front-loads entry vocabulary → BM25
lands on the hub (or a non-gold spoke) → only graph traversal reaches the gold
spoke → only the gold spoke's content contains the answer token → the judge
can verify retrieval success mechanically → verdicts correlate with path
quality → edge updates carry signal.

### Placement rules (enforced by generator validation)

1. The hub **names** every rule ID and its topic ("K-2 governs delimiters")
   but **never contains any value token**. The hub is an index, not an answer.
2. Each spoke contains: the family name, its own rule ID, its own value
   token(s), and its subtopic anchor word — and no other spoke's tokens.
3. No answer token appears in more than one cluster (global uniqueness).
4. Answer tokens are **word-like** (letters/digits/hyphens only). No tokens
   that depend on punctuation surviving tokenization (`~|`, `ty::`, etc.).
   Punctuation renderings may appear in content as color, but the checkable
   token must be the word.
5. Bridge MUs contain no entry vocabulary and no answer tokens from either
   adjacent cluster (unchanged from v2 discipline).
6. **Prompt-side rule (enforced in `prompt_gen.py` validation, recorded here
   because it constrains the vocabulary design):** a prompt targeting a gold
   spoke must not contain that spoke's **anchor word**, in addition to
   containing no answer tokens. K1 candidacy requires ≥2 distinct keyword
   hits; the family name gives every in-cluster MU one hit, so the anchor
   word would give the gold its second hit and put it into the K1 candidate
   set directly — destroying headroom at the lexical stage. Prompts reach the
   gold's concept *periphrastically* ("what separates the values in a
   record", not "delimiter") while front-loading the family name plus 1–2
   hub-saturated entry words so the hub clears the two-hit bar.

### Why rule-ID + value-token pairs

Individual words like "docket" or "ratchet" exist in English; what's
unguessable is the *pairing* — that Vessel rule V-4 defines a three-to-one
ratchet. Gemma has no parametric knowledge of any of these families, so any
correct specific in a response must have come through retrieval. Each spoke
carries both a rule ID and a value token so criteria can check two independent
specifics per prompt.

---

## 2. Traversal direction — RESOLVED (2026-07-09): forward-only

Confirmed by inspection of `retrieval.py`: Stage 3 walks an adjacency index
built from `source_mu_id` only, so K2 traversal moves strictly source→target.
(K1's `edge_weight_sum` centrality accumulates on both endpoints — the
undirected build intent survives in *scoring* but not in *traversal*.) Under
the v2 layout, entry at a hub can reach only its bridge; spokes are
unreachable from the hub.

**v3 edge layout: every v2 edge gets a reciprocal partner — 32 → 64 edges,
all weights 0.5.** Spoke↔hub pairs (56 edges) and hub↔bridge pairs (8 edges).
Rationale: zero backend code change; consistent with M20's directed
Beta-counter edges (each direction independently learnable, which is
principled — asymmetric association); avoids muddying
`directional_edge_weight_multiplier` semantics the way an undirected-traversal
code change would. Side effect: hub K1 centrality roughly doubles, further
consolidating hubs as entry points (desired).

---

## 3. Convention families (one per cluster)

| Cluster | Theme (unchanged) | Invented family | Rule IDs |
|---|---|---|---|
| cluster_a | Structured file output / format discipline | **Kestrel Output Conventions** | K-1 … K-7 |
| cluster_b | Schema and data-shape reasoning | **Marrow Schema Standard** | M-1 … M-7 |
| cluster_c | Rule-following under constraint | **Vessel Constraint Code** | V-1 … V-7 |
| cluster_d | Self-sourcing / provenance discipline | **Ledger Provenance Protocol** | L-1 … L-7 |

Family names are invented proper nouns and double as strong BM25 anchors:
a prompt beginning "Under the Kestrel conventions, …" reliably enters
cluster A. Every MU in a cluster (hub and spokes) contains its family name.

### Entry vocabulary per cluster

Hub is saturated with all of these; each spoke uses only the one marked as its
anchor in §4. These also remain the cross-cluster fingerprint sets for prompt
leakage scanning (a cluster-A prompt must not contain cluster-B entry terms
**or** any answer token from any cluster).

- **cluster_a:** format, structure, output, field, delimiter, header, quoting, encoding, padding
- **cluster_b:** schema, data, shape, type, validation, column, nesting, version, range
- **cluster_c:** rule, constraint, violation, enforce, comply, severity, exemption, escalation, deadline
- **cluster_d:** source, provenance, cite, origin, traceable, attribution, timestamp, custody, retraction

---

## 4. Per-MU specifications

Column key — **Anchor:** the one entry-vocabulary word this spoke may use.
**Required terms:** exact-match validation list for the generator (family
name + rule ID + value token + anchor). **Content gist:** what the 2–3
sentences must assert; final prose is generated (or authored) to taste.

### cluster_a — Kestrel Output Conventions

| MU | Rule | Anchor | Value token(s) | Content gist | Required terms |
|---|---|---|---|---|---|
| a_hub | — | (all) | *none — index only* | The Kestrel Output Conventions govern structured file output: seven numbered rules, K-1 field order, K-2 delimiters, K-3 headers, K-4 padding, K-5 quoting, K-6 encoding, K-7 missing values. States that specific values are defined in the individual rules. | Kestrel, K-1…K-7, format, structure, output, field, delimiter, header, quoting, encoding, padding |
| a_spoke1 | K-1 | field | **spine order** | Kestrel K-1: fields appear in spine order — the fixed sequence declared on the file's first line; reordering is a K-1 violation. | Kestrel, K-1, spine order, field |
| a_spoke2 | K-2 | delimiter | **TILDEBAR** | Kestrel K-2: the delimiter is the TILDEBAR token; it is never escaped inline — values containing it must be quoted per K-5. | Kestrel, K-2, TILDEBAR, delimiter |
| a_spoke3 | K-3 | header | **KAPLINE** | Kestrel K-3: the header row begins with the KAPLINE marker and must mirror the data rows position-for-position. | Kestrel, K-3, KAPLINE, header |
| a_spoke4 | K-4 | padding | **P-fault** | Kestrel K-4: interior padding is forbidden; any padded value is logged as a P-fault. | Kestrel, K-4, P-fault, padding |
| a_spoke5 | K-5 | quoting | **grove quotes** | Kestrel K-5: values containing the delimiter are wrapped in grove quotes; inconsistent quoting breaks parsing. | Kestrel, K-5, grove quotes, quoting |
| a_spoke6 | K-6 | encoding | **ENCK9** | Kestrel K-6: every file declares the ENCK9 encoding tag on its second line; mixed encodings are rejected outright. | Kestrel, K-6, ENCK9, encoding |
| a_spoke7 | K-7 | field | **NULLMARK** | Kestrel K-7: missing values use the NULLMARK placeholder, never an empty field; silent omission corrupts spine order. | Kestrel, K-7, NULLMARK |

### cluster_b — Marrow Schema Standard

| MU | Rule | Anchor | Value token(s) | Content gist | Required terms |
|---|---|---|---|---|---|
| b_hub | — | (all) | *none — index only* | The Marrow Schema Standard governs data shape: M-1 type declarations, M-2 column counts, M-3 nesting, M-4 versioning, M-5 naming, M-6 numeric ranges, M-7 validation order. Values defined in the individual rules. | Marrow, M-1…M-7, schema, data, shape, type, validation, column, nesting, version, range |
| b_spoke1 | M-1 | type | **WHOLENUM, SPANTEXT, FLAGBIT** | Marrow M-1: every column declares one of the three base types — WHOLENUM, SPANTEXT, or FLAGBIT. | Marrow, M-1, WHOLENUM, SPANTEXT, FLAGBIT, type |
| b_spoke2 | M-2 | column | **girth line** | Marrow M-2: every table declares its column count on a girth line; a mismatch fails the table before any other check. | Marrow, M-2, girth line, column |
| b_spoke3 | M-3 | nesting | **trident limit** | Marrow M-3: nesting is capped at depth three — the trident limit; deeper structures must be flattened. | Marrow, M-3, trident limit, nesting |
| b_spoke4 | M-4 | version | **COBALT numbering** | Marrow M-4: schema versions use COBALT numbering (COBALT-1, COBALT-2, …); consumers pin to a COBALT number, never "latest". | Marrow, M-4, COBALT, version |
| b_spoke5 | M-5 | schema | **lowmark case** | Marrow M-5: all names in a schema are written in lowmark case; mixed casing is a naming violation. | Marrow, M-5, lowmark case |
| b_spoke6 | M-6 | range | **BOUNDSPAN** | Marrow M-6: numeric columns declare their range with the BOUNDSPAN keyword; values outside it are rejected, not clamped. | Marrow, M-6, BOUNDSPAN, range |
| b_spoke7 | M-7 | validation | **marrow order** | Marrow M-7: validation runs in marrow order — types first, then ranges, then names; running checks out of order masks failures. | Marrow, M-7, marrow order, validation |

### cluster_c — Vessel Constraint Code

| MU | Rule | Anchor | Value token(s) | Content gist | Required terms |
|---|---|---|---|---|---|
| c_hub | — | (all) | *none — index only* | The Vessel Constraint Code governs rule-following: V-1 severity classes, V-2 violation logging, V-3 exemptions, V-4 escalation, V-5 correction deadlines, V-6 overrides, V-7 repeat violations. Values defined in the individual rules. | Vessel, V-1…V-7, rule, constraint, violation, enforce, comply, severity, exemption, escalation, deadline |
| c_spoke1 | V-1 | severity | **amber breach, crimson breach** | Vessel V-1: every violation is classed as either an amber breach or a crimson breach; there is no third class. | Vessel, V-1, amber breach, crimson breach, severity |
| c_spoke2 | V-2 | violation | **tally stamp** | Vessel V-2: every violation receives a tally stamp at the moment it is logged; an unstamped violation cannot be enforced. | Vessel, V-2, tally stamp, violation |
| c_spoke3 | V-3 | exemption | **WAIVERKEY** | Vessel V-3: exemptions require a WAIVERKEY issued *before* the act; retroactive WAIVERKEYs are void. | Vessel, V-3, WAIVERKEY, exemption |
| c_spoke4 | V-4 | escalation | **three-to-one ratchet** | Vessel V-4: three amber breaches escalate to one crimson breach — the three-to-one ratchet; the count resets after escalation. | Vessel, V-4, ratchet, escalation |
| c_spoke5 | V-5 | deadline | **nine clear days** | Vessel V-5: corrections are due within nine clear days of the tally stamp; day one is the day after stamping. | Vessel, V-5, nine clear days, deadline |
| c_spoke6 | V-6 | rule | **warden note** | Vessel V-6: only a warden note can override a constraint, and it must name the specific rule it overrides. | Vessel, V-6, warden note |
| c_spoke7 | V-7 | comply | **echo penalty** | Vessel V-7: a repeat of the same violation within one cycle incurs the echo penalty — the sanction doubles. | Vessel, V-7, echo penalty |

### cluster_d — Ledger Provenance Protocol

| MU | Rule | Anchor | Value token(s) | Content gist | Required terms |
|---|---|---|---|---|---|
| d_hub | — | (all) | *none — index only* | The Ledger Provenance Protocol governs sourcing: L-1 citation markers, L-2 origin records, L-3 custody chains, L-4 unverified content, L-5 timestamps, L-6 attribution, L-7 retractions. Values defined in the individual rules. | Ledger, L-1…L-7, source, provenance, cite, origin, traceable, attribution, timestamp, custody, retraction |
| d_spoke1 | L-1 | cite | **SRCLINE** | Ledger L-1: every citation carries a SRCLINE marker; a claim without one is treated as unsourced. | Ledger, L-1, SRCLINE |
| d_spoke2 | L-2 | origin | **origin docket** | Ledger L-2: every claim links to an origin docket recording where the content first entered the system. | Ledger, L-2, origin docket |
| d_spoke3 | L-3 | custody | **relay chain** | Ledger L-3: custody is recorded as a relay chain — every hand-off logged in order; a gap breaks traceability. | Ledger, L-3, relay chain, custody |
| d_spoke4 | L-4 | source | **GREYMARK** | Ledger L-4: unverified content is flagged GREYMARK and must keep the flag until its origin docket is completed. | Ledger, L-4, GREYMARK |
| d_spoke5 | L-5 | timestamp | **ledger time** | Ledger L-5: all timestamps are recorded in ledger time — UTC at minute precision; local time never appears in records. | Ledger, L-5, ledger time, timestamp |
| d_spoke6 | L-6 | attribution | **quill code** | Ledger L-6: authors are identified by quill codes, never by name, so attribution survives renames. | Ledger, L-6, quill code, attribution |
| d_spoke7 | L-7 | retraction | **UNWRITE notice** | Ledger L-7: a retraction issues an UNWRITE notice; the original record is never deleted, only marked. | Ledger, L-7, UNWRITE notice, retraction |

### Bridges (unchanged role, refreshed content)

| MU | Connects | Content gist | Forbidden |
|---|---|---|---|
| bridge_ab | a_hub ↔ b_hub | The two convention families for how records are written and how they are described are maintained by the same working group and reviewed together each quarter; a change to one triggers review of the other. | All cluster_a + cluster_b entry vocabulary and answer tokens (incl. family names Kestrel, Marrow) |
| bridge_cd | c_hub ↔ d_hub | Accountability and traceability practices share one review board; findings from either practice area are filed to the same registry. | All cluster_c + cluster_d entry vocabulary and answer tokens (incl. family names Vessel, Ledger) |

---

## 5. Generation approach

Keep the existing Gemma-via-Ollama pipeline in `generate_seed_v2.py` (copied
to `generate_seed_v3.py`) with its required/forbidden-term retry validation —
the invented tokens are exact-match checkable, so the machinery fits as-is.
Expect a higher retry rate: MUST-include lists now contain unusual tokens.
If a spec repeatedly fails after `MAX_RETRIES`, hand-author that MU's 2–3
sentences directly rather than loosening the requirement — the token's exact
presence is the point, and there is no research reason the *seed* prose must
be Gemma-written (the learning object is edge weights, not seed authorship).

Generator changes required (implemented in Milestone 25, see BUILD-PLAN.md):

1. **New validation: hub token exclusion.** Hub MUs must contain zero value
   tokens from their own cluster (placement rule 1). Currently only
   forbidden-terms-from-other-clusters is checked.
2. **Fingerprint export update.** `CLUSTER_*_FP` (imported by
   `prompt_gen.py`) becomes two exports per cluster: entry vocabulary and
   answer tokens. Prompt leakage scanning must reject a prompt containing
   any *other* cluster's entry terms (as today) **or any answer token from
   any cluster, including its own** — answer tokens in a prompt would let
   BM25 find the gold spoke directly and destroy headroom. (Consuming this
   export in `prompt_gen.py` itself is a future milestone — see BUILD-PLAN.md
   Non-Goals.)
3. **Family names join the fingerprint sets** for cross-cluster scans.

---

## 6. Downstream impacts (hash cascade)

Everything below re-derives from the frozen `seed_v3.json.gz`. None of this
is in scope for Milestone 25 — it is listed here so future milestones don't
have to re-derive it:

- **Probe set v3** — golds are spoke MUs (IDs unchanged from v2 where
  convenient); probe queries front-load entry vocabulary only, obey placement
  rule 6 (no gold anchor word), and every probe must pass a baseline check on
  a clean seeded graph (gold NOT already surfaced) before the set is frozen.
  **Metric semantics RESOLVED (probes.py):** `gold_mu_rank` is position
  within the retrieved set only (K1 then K2 ordering), so a traversal-reached
  gold is structurally rank 4 and an unretrieved gold is None — the
  trajectory is a None↔4 flip. Primary metric: **gold-retrieval rate per
  measurement session**. Pass threshold for v3 hop-1 probes must be 4
  (precedent: `pass_rank_thresholds.cross_cluster_transfer: 4`, same
  structural reason) — either override `ranking_improvement` or define a new
  phenomenon type for v3. Secondary continuous metric: trained hub→gold edge
  posterior mean per session, queried from `edges` /
  `edge_update_events` (analysis-side; no schema change).
- **Prompt bank** — regenerated against v3; per-prompt criteria follow the
  template: `["names rule <ID>", "states <value token>"]` — two
  binary-checkable criteria per prompt, judge-gradeable as a checklist.
  Same baseline gate applied bank-wide. **One trained target spoke per
  cluster:** K2 selection (`retrieval_level2_top_k: 1`) is winner-take-all
  and query-independent, so global edge weights can route each hub to only
  one favored spoke; training multiple spokes per cluster makes their edges
  fight over the single slot. Concentrate bulk prompts on one designated
  spoke per cluster; the six untrained spokes are **negative controls**
  (probes on them should stay flat — discriminative learning evidence).
  Document query-independent traversal as an architectural property;
  query-conditioned traversal scoring is future work.
- **`weight_mode` decision (M20)** — `sample` (Thompson) breaks the
  seven-way initial tie stochastically and explores; `mean` gives
  deterministic fixture-order elimination until the gold locks in. Both
  defensible; choose deliberately and record it in the plan, since it shapes
  the learning curve being interpreted.
- **Harness/config fixture path** — `POST /admin/seed-graph` already accepts
  `{"version": "v3"}` with zero code changes (confirmed during M25 planning
  by reading `backend/api/admin.py` and `backend/analytics/seed_fixture.py`
  — the fixture path and `series_id` prefix are both derived from `version`
  generically). What still hardcodes `v2` is harness-side:
  `scripts/harness/prompt_gen.py`'s `SEED_FIXTURE_VERSION` constant and its
  `from generate_seed_v2 import ...` — updating those is part of the future
  prompt-bank-v3 milestone, not M25.
- **`prompt_gen.py` imports** — fingerprint import surface changes per §5.

## 7. Pre-freeze checklist

1. ~~§2 traversal-direction check~~ RESOLVED: forward-only; v3 ships 64
   reciprocal edges per §2.
2. ~~Probe rank-metric question~~ RESOLVED from probes.py: primary metric is
   gold-retrieval rate (None↔4 flip); v3 hop-1 pass threshold = 4; edge
   posterior mean as the continuous secondary (see §6).
3. Paul reviews/edits all value tokens and gists above. **DONE — approved
   2026-07-11, this file is the frozen record of that approval.**
3a. v3 generator preserves the `series_id` `"seed:"` prefix convention on
   every seed MU — both `series_report.py` §5.3 and the M23 extractor label
   derivation classify seed content by this prefix. (Confirmed automatic:
   `register_fixture()` derives `series_id = f"seed:{fixture_version}"` from
   the fixture's own `fixture_version` field, so setting
   `"fixture_version": "v3"` in the draft/frozen JSON is sufficient — no
   generator code has to special-case this.)
4. Draft generated; every MU passes required/forbidden validation, OR its
   failure is recorded in `seed_v3_generation_manifest.json` and hand-repaired
   before freeze.
5. Manual read of the draft: hubs contain no value tokens; each token appears
   in exactly one MU; bridges clean. (`verify_seed_v3.py` automates this —
   see Milestone 25.)
6. Freeze to `seed_v3.json.gz`; record fixture hash.
