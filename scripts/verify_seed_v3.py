"""
Verification checks for the seed_v3 fixture.

Run against the uncompressed draft while iterating (the default), then again
against the frozen .gz as a final post-freeze sanity check:

  python scripts/verify_seed_v3.py            # --draft (seed_v3_draft.json)
  python scripts/verify_seed_v3.py --draft
  python scripts/verify_seed_v3.py --frozen   # seed_v3.json.gz

This --draft-first workflow is the deliberate deviation from v2 (which froze
to .gz BEFORE verifying): it makes the manifest-driven repair loop practical —
edit seed_v3_draft.json, rerun --draft, repeat, freeze last.

9 checks (v2 had 5):
  1. MU count / cluster distribution (34 MUs)                    [FAIL]
  2. Edge count / reciprocal wiring (64 edges)                   [FAIL]
  3. Hub<->bridge connectivity, BOTH directions                 [FAIL]
  4. Lexical-distinctness of bridges (full two-tier vocab)       [FAIL]
  5. NEW Hub token exclusion (no own-cluster value tokens)       [FAIL]
  6. NEW Spoke sibling-token exclusion                           [FAIL]
  7. NEW Global answer-token leakage scan (content-level)        [FAIL]
  8. NEW Answer-token format check (^[A-Za-z0-9-]+$)             [FAIL]
  9. Baseline probe sanity (BM25 simulation):                    [FAIL/WARN]
       - bridges NOT at K1                                       [FAIL]
       - hub query reaches a same-cluster spoke at K2            [WARN]
       - two-hit bar: entry-vocab-only query surfaces no spoke   [WARN]
         at K1

This file duplicates its own copy of the two-tier vocabulary constants
rather than importing them from generate_seed_v3.py — the same "verify what
is frozen, not what was intended" convention v2 uses.
"""

import argparse
import gzip
import json
import re
import sys
from pathlib import Path

# Force UTF-8 output on Windows (console default is cp1252, lacks arrow chars).
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

# rank_bm25 is a backend dependency — install if not present:
#   pip install rank-bm25
try:
    from rank_bm25 import BM25Okapi
except ImportError:
    print("ERROR: rank_bm25 not installed. Run: pip install rank-bm25")
    sys.exit(1)

SCRIPT_DIR = Path(__file__).parent
PROJECT_ROOT = SCRIPT_DIR.parent
DRAFT_PATH = PROJECT_ROOT / "seed_fixtures" / "seed_v3_draft.json"
FROZEN_PATH = PROJECT_ROOT / "seed_fixtures" / "seed_v3.json.gz"

# ---------------------------------------------------------------------------
# Constants — match retrieval.py exactly (tokenizer + hyperparameters)
# ---------------------------------------------------------------------------

_STOPWORDS = frozenset({
    "a", "an", "the", "is", "it", "in", "on", "at", "to", "for",
    "of", "and", "or", "but", "not", "with", "this", "that", "i",
    "you", "he", "she", "we", "they", "be", "was", "were", "are",
    "has", "have", "had", "do", "did", "does", "will", "would",
    "could", "should", "may", "might", "can",
})
_WORD_RE = re.compile(r"\b\w+\b")

RETRIEVAL_KEYWORD_POOL_SIZE = 20
RETRIEVAL_KEYWORD_COUNT = 5
LEVEL1_TOP_K = 3  # K1
LEVEL2_TOP_K = 1  # K2
DIR_MULTIPLIER = 1.5

# ---------------------------------------------------------------------------
# Two-tier vocabulary — standalone copy of generate_seed_v3.py's constants
# (verify what is frozen, not what was intended).
# ---------------------------------------------------------------------------

FAMILY_NAMES = {
    "cluster_a": "Kestrel", "cluster_b": "Marrow",
    "cluster_c": "Vessel",  "cluster_d": "Ledger",
}

ENTRY_VOCAB = {
    "cluster_a": ["format", "structure", "output", "field", "delimiter",
                  "header", "quoting", "encoding", "padding"],
    "cluster_b": ["schema", "data", "shape", "type", "validation",
                  "column", "nesting", "version", "range"],
    "cluster_c": ["rule", "constraint", "violation", "enforce", "comply",
                  "severity", "exemption", "escalation", "deadline"],
    "cluster_d": ["source", "provenance", "cite", "origin", "traceable",
                  "attribution", "timestamp", "custody", "retraction"],
}

ANSWER_TOKENS = {
    "a_hub": [],
    "a_spoke1": ["spine order"], "a_spoke2": ["TILDEBAR"],
    "a_spoke3": ["KAPLINE"],     "a_spoke4": ["P-fault"],
    "a_spoke5": ["grove quotes"], "a_spoke6": ["ENCK9"],
    "a_spoke7": ["NULLMARK"],
    "b_hub": [],
    "b_spoke1": ["WHOLENUM", "SPANTEXT", "FLAGBIT"],
    "b_spoke2": ["girth line"],  "b_spoke3": ["trident limit"],
    "b_spoke4": ["COBALT numbering"], "b_spoke5": ["lowmark case"],
    "b_spoke6": ["BOUNDSPAN"],   "b_spoke7": ["marrow order"],
    "c_hub": [],
    "c_spoke1": ["amber breach", "crimson breach"],
    "c_spoke2": ["tally stamp"], "c_spoke3": ["WAIVERKEY"],
    "c_spoke4": ["three-to-one ratchet"], "c_spoke5": ["nine clear days"],
    "c_spoke6": ["warden note"], "c_spoke7": ["echo penalty"],
    "d_hub": [],
    "d_spoke1": ["SRCLINE"],     "d_spoke2": ["origin docket"],
    "d_spoke3": ["relay chain"], "d_spoke4": ["GREYMARK"],
    "d_spoke5": ["ledger time"], "d_spoke6": ["quill code"],
    "d_spoke7": ["UNWRITE notice"],
}

ANCHOR_WORDS = {
    "a_spoke1": "field", "a_spoke2": "delimiter", "a_spoke3": "header",
    "a_spoke4": "padding", "a_spoke5": "quoting", "a_spoke6": "encoding",
    "a_spoke7": "field",
    "b_spoke1": "type", "b_spoke2": "column", "b_spoke3": "nesting",
    "b_spoke4": "version", "b_spoke5": "schema", "b_spoke6": "range",
    "b_spoke7": "validation",
    "c_spoke1": "severity", "c_spoke2": "violation", "c_spoke3": "exemption",
    "c_spoke4": "escalation", "c_spoke5": "deadline", "c_spoke6": "rule",
    "c_spoke7": "comply",
    "d_spoke1": "cite", "d_spoke2": "origin", "d_spoke3": "custody",
    "d_spoke4": "source", "d_spoke5": "timestamp", "d_spoke6": "attribution",
    "d_spoke7": "retraction",
}

# Fixed MU IDs (copied from generate_seed_v3.py / seed_v2 — same UUIDs).
MU_IDS = {
    "a_hub":    "a0000001-aaaa-4aaa-8aaa-000000000001",
    "a_spoke1": "a0000002-aaaa-4aaa-8aaa-000000000002",
    "a_spoke2": "a0000003-aaaa-4aaa-8aaa-000000000003",
    "a_spoke3": "a0000004-aaaa-4aaa-8aaa-000000000004",
    "a_spoke4": "a0000005-aaaa-4aaa-8aaa-000000000005",
    "a_spoke5": "a0000006-aaaa-4aaa-8aaa-000000000006",
    "a_spoke6": "a0000007-aaaa-4aaa-8aaa-000000000007",
    "a_spoke7": "a0000008-aaaa-4aaa-8aaa-000000000008",
    "b_hub":    "b0000001-bbbb-4bbb-8bbb-000000000001",
    "b_spoke1": "b0000002-bbbb-4bbb-8bbb-000000000002",
    "b_spoke2": "b0000003-bbbb-4bbb-8bbb-000000000003",
    "b_spoke3": "b0000004-bbbb-4bbb-8bbb-000000000004",
    "b_spoke4": "b0000005-bbbb-4bbb-8bbb-000000000005",
    "b_spoke5": "b0000006-bbbb-4bbb-8bbb-000000000006",
    "b_spoke6": "b0000007-bbbb-4bbb-8bbb-000000000007",
    "b_spoke7": "b0000008-bbbb-4bbb-8bbb-000000000008",
    "c_hub":    "c0000001-cccc-4ccc-8ccc-000000000001",
    "c_spoke1": "c0000002-cccc-4ccc-8ccc-000000000002",
    "c_spoke2": "c0000003-cccc-4ccc-8ccc-000000000003",
    "c_spoke3": "c0000004-cccc-4ccc-8ccc-000000000004",
    "c_spoke4": "c0000005-cccc-4ccc-8ccc-000000000005",
    "c_spoke5": "c0000006-cccc-4ccc-8ccc-000000000006",
    "c_spoke6": "c0000007-cccc-4ccc-8ccc-000000000007",
    "c_spoke7": "c0000008-cccc-4ccc-8ccc-000000000008",
    "d_hub":    "d0000001-dddd-4ddd-8ddd-000000000001",
    "d_spoke1": "d0000002-dddd-4ddd-8ddd-000000000002",
    "d_spoke2": "d0000003-dddd-4ddd-8ddd-000000000003",
    "d_spoke3": "d0000004-dddd-4ddd-8ddd-000000000004",
    "d_spoke4": "d0000005-dddd-4ddd-8ddd-000000000005",
    "d_spoke5": "d0000006-dddd-4ddd-8ddd-000000000006",
    "d_spoke6": "d0000007-dddd-4ddd-8ddd-000000000007",
    "d_spoke7": "d0000008-dddd-4ddd-8ddd-000000000008",
    "bridge_ab": "ab000001-abab-4aba-8aba-000000000001",
    "bridge_cd": "cd000001-cdcd-4cdc-8cdc-000000000001",
}
ID_TO_KEY = {v: k for k, v in MU_IDS.items()}

HUB_IDS = {
    "cluster_a": MU_IDS["a_hub"], "cluster_b": MU_IDS["b_hub"],
    "cluster_c": MU_IDS["c_hub"], "cluster_d": MU_IDS["d_hub"],
}
BRIDGE_IDS = {
    "bridge_ab": MU_IDS["bridge_ab"], "bridge_cd": MU_IDS["bridge_cd"],
}
HUB_TO_BRIDGE = {
    "cluster_a": "bridge_ab", "cluster_b": "bridge_ab",
    "cluster_c": "bridge_cd", "cluster_d": "bridge_cd",
}
BRIDGE_CLUSTERS = {
    "bridge_ab": ("cluster_a", "cluster_b"),
    "bridge_cd": ("cluster_c", "cluster_d"),
}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _tokenize(text: str) -> list[str]:
    """Lowercase word tokens with stopwords removed (matches retrieval.py)."""
    return [w for w in _WORD_RE.findall(text.lower()) if w not in _STOPWORDS]


def _contains_terms(content: str, terms: list[str]) -> list[str]:
    """Return terms present in content using whole-word/whole-phrase matching."""
    lower = content.lower()
    return [t for t in terms
            if re.search(r"\b" + re.escape(t.lower()) + r"\b", lower)]


def _answer_tokens_for_cluster(cluster_letter: str) -> list[str]:
    """Every answer token belonging to any spoke in the given cluster."""
    tokens: list[str] = []
    for key, toks in ANSWER_TOKENS.items():
        if key.startswith(cluster_letter + "_"):
            tokens.extend(toks)
    return tokens


def _all_answer_tokens() -> list[str]:
    """Every answer token across all clusters (project-wide)."""
    return [tok for toks in ANSWER_TOKENS.values() for tok in toks]


def _simulate_retrieval(query: str, mus: list[dict], edges: list[dict]) -> dict:
    """Stripped-down BM25 + traversal funnel (same logic as retrieval.py).
    Returns k1_ids, k2_ids, and the multi-hit candidate set."""
    mu_ids = [m["mu_id"] for m in mus]
    corpus = [_tokenize(m["raw_content"]) for m in mus]
    bm25 = BM25Okapi([t if t else ["_empty_"] for t in corpus])

    query_tokens = _tokenize(query)[:RETRIEVAL_KEYWORD_COUNT]

    candidate_hits: dict[str, set] = {}
    candidate_scores: dict[str, float] = {}
    for kw in query_tokens:
        scores = bm25.get_scores([kw])
        indexed = sorted(enumerate(scores), key=lambda x: x[1], reverse=True)
        for idx, score in indexed[:RETRIEVAL_KEYWORD_POOL_SIZE]:
            if score <= 0.0:
                break
            mid = mu_ids[idx]
            candidate_hits.setdefault(mid, set()).add(kw)
            candidate_scores[mid] = candidate_scores.get(mid, 0.0) + score

    multi_hit = [mid for mid, hits in candidate_hits.items() if len(hits) >= 2]

    edge_weight_sum: dict[str, float] = {}
    outgoing_edges: dict[str, list] = {}
    for edge in edges:
        s, t, w = edge["source_mu_id"], edge["target_mu_id"], edge["weight"]
        edge_weight_sum[s] = edge_weight_sum.get(s, 0.0) + w
        edge_weight_sum[t] = edge_weight_sum.get(t, 0.0) + w
        outgoing_edges.setdefault(s, []).append(edge)

    def k1_score(mid: str) -> float:
        # M47 divergence point: backend/memory/retrieval.py's _k1_score()
        # gained a hyperparameters.k1_centrality_weight coefficient on the
        # edge_weight_sum term (default 1.0, so this formula still matches
        # bit-for-bit at the committed default). Deliberately NOT mirrored
        # here — this function verifies a frozen fixture under fixed
        # assumptions (BM25 + unweighted centrality), not a runtime sweep;
        # see BUILD-PLAN.md Milestone 47's open questions for the reasoning.
        # If a future change moves the default off 1.0, this comment is the
        # place that would need to change too.
        return edge_weight_sum.get(mid, 0.0) + candidate_scores.get(mid, 0.0)

    sorted_cands = sorted(multi_hit, key=k1_score, reverse=True)
    k1_ids = sorted_cands[:LEVEL1_TOP_K]

    k2_scores: dict[str, float] = {}
    k1_set = set(k1_ids)
    for mid in k1_ids:
        for edge in outgoing_edges.get(mid, []):
            tgt = edge["target_mu_id"]
            if tgt in k1_set:
                continue
            k2_scores[tgt] = k2_scores.get(tgt, 0.0) + edge["weight"] * DIR_MULTIPLIER
    k2_sorted = sorted(k2_scores.items(), key=lambda x: x[1], reverse=True)
    k2_ids = [mid for mid, _ in k2_sorted[:LEVEL2_TOP_K]]

    return {"k1_ids": k1_ids, "k2_ids": k2_ids, "multi_hit": multi_hit}


# ---------------------------------------------------------------------------
# Check functions
# ---------------------------------------------------------------------------

def check_1_mu_count(mus: list[dict]) -> bool:
    """All 34 MUs present with correct cluster_id distribution."""
    expected_dist = {
        "cluster_a": 8, "cluster_b": 8, "cluster_c": 8, "cluster_d": 8,
        "bridge_ab": 1, "bridge_cd": 1,
    }
    actual_dist: dict[str, int] = {}
    for mu in mus:
        cid = mu.get("cluster_id", "NONE")
        actual_dist[cid] = actual_dist.get(cid, 0) + 1

    passed = True
    if len(mus) != 34:
        print(f"  FAIL: expected 34 MUs, got {len(mus)}")
        passed = False
    else:
        print(f"  OK: mu_count = {len(mus)}")

    if actual_dist != expected_dist:
        print("  FAIL: cluster distribution mismatch")
        print(f"    expected: {expected_dist}")
        print(f"    actual:   {actual_dist}")
        passed = False
    else:
        print(f"  OK: cluster_distribution = {actual_dist}")

    return passed


def check_2_edge_wiring(mus: list[dict], edges: list[dict]) -> bool:
    """64 reciprocal edges: every forward (s->t, w) has a reverse (t->s, w)
    with the same weight; hub touches 16 edges, each spoke touches 2; no
    cross-group edges."""
    passed = True

    if len(edges) != 64:
        print(f"  FAIL: expected 64 edges, got {len(edges)}")
        passed = False
    else:
        print(f"  OK: edge_count = {len(edges)}")

    # Reciprocity: for every (s, t, w) an (t, s, w) must also exist.
    edge_set = {(e["source_mu_id"], e["target_mu_id"], e["weight"]) for e in edges}
    missing_reverse = [
        (s, t) for (s, t, w) in edge_set if (t, s, w) not in edge_set
    ]
    if missing_reverse:
        print(f"  FAIL: {len(missing_reverse)} edge(s) have no matching reverse")
        for s, t in missing_reverse[:5]:
            print(f"    {s[:12]} -> {t[:12]} has no reverse with same weight")
        passed = False
    else:
        print("  OK: every edge has a reciprocal partner (same weight)")

    # Degree counts: each hub touches 16 edges, each spoke touches 2.
    touch: dict[str, int] = {}
    for e in edges:
        touch[e["source_mu_id"]] = touch.get(e["source_mu_id"], 0) + 1
        touch[e["target_mu_id"]] = touch.get(e["target_mu_id"], 0) + 1

    for cluster, hub_id in HUB_IDS.items():
        if touch.get(hub_id, 0) != 16:
            print(f"  FAIL: {cluster} hub touches {touch.get(hub_id, 0)} edges (expected 16)")
            passed = False
        else:
            print(f"  OK: {cluster} hub touches 16 edges (7 spoke pairs + 1 bridge pair)")

    spoke_bad = []
    for key, mu_id in MU_IDS.items():
        if "spoke" in key and touch.get(mu_id, 0) != 2:
            spoke_bad.append((key, touch.get(mu_id, 0)))
    if spoke_bad:
        print(f"  FAIL: {len(spoke_bad)} spoke(s) do not touch exactly 2 edges: {spoke_bad[:5]}")
        passed = False
    else:
        print("  OK: every spoke touches exactly 2 edges (1 in + 1 out)")

    # No edges between {a,b} clusters and {c,d} clusters.
    ab_ids = {m["mu_id"] for m in mus if m.get("cluster_id") in ("cluster_a", "cluster_b")}
    cd_ids = {m["mu_id"] for m in mus if m.get("cluster_id") in ("cluster_c", "cluster_d")}
    cross_edges = [
        e for e in edges
        if (e["source_mu_id"] in ab_ids and e["target_mu_id"] in cd_ids)
        or (e["source_mu_id"] in cd_ids and e["target_mu_id"] in ab_ids)
    ]
    if cross_edges:
        print(f"  FAIL: {len(cross_edges)} forbidden cross-group edges found")
        passed = False
    else:
        print("  OK: no edges between {a,b} group and {c,d} group")

    return passed


def check_3_connectivity(mus: list[dict], edges: list[dict]) -> bool:
    """Each cluster hub has an edge to its bridge AND the bridge back to the
    hub (v2 only checked hub->bridge; v3 requires both directions)."""
    passed = True
    directed = {(e["source_mu_id"], e["target_mu_id"]) for e in edges}

    for cluster, hub_id in HUB_IDS.items():
        bridge_id = BRIDGE_IDS[HUB_TO_BRIDGE[cluster]]
        fwd = (hub_id, bridge_id) in directed
        rev = (bridge_id, hub_id) in directed
        if fwd and rev:
            print(f"  OK: {cluster} hub <-> {HUB_TO_BRIDGE[cluster]} (both directions)")
        else:
            print(f"  FAIL: {cluster} hub <-> {HUB_TO_BRIDGE[cluster]} "
                  f"(hub->bridge={fwd}, bridge->hub={rev})")
            passed = False

    return passed


def check_4_lexical_distinctness(mus: list[dict]) -> bool:
    """Bridge MUs must contain none of either adjacent cluster's entry
    vocabulary, answer tokens, or family name (full two-tier upgrade from
    v2's 5-term fingerprint)."""
    passed = True
    mu_by_cluster = {m["cluster_id"]: m for m in mus
                     if m["cluster_id"] in ("bridge_ab", "bridge_cd")}

    for bridge_key, (cl1, cl2) in BRIDGE_CLUSTERS.items():
        bridge = mu_by_cluster.get(bridge_key)
        if not bridge:
            print(f"  FAIL: {bridge_key} MU not found")
            passed = False
            continue
        forbidden = (
            ENTRY_VOCAB[cl1] + ENTRY_VOCAB[cl2]
            + _answer_tokens_for_cluster(_letter(cl1))
            + _answer_tokens_for_cluster(_letter(cl2))
            + [FAMILY_NAMES[cl1], FAMILY_NAMES[cl2]]
        )
        found = _contains_terms(bridge["raw_content"], forbidden)
        if found:
            print(f"  FAIL: {bridge_key} contains forbidden terms: {found}")
            passed = False
        else:
            print(f"  OK: {bridge_key} contains none of {cl1}/{cl2} vocab, "
                  f"tokens, or family names")

    return passed


def _letter(cluster_id: str) -> str:
    return cluster_id.split("_")[1]


def check_5_hub_token_exclusion(mus: list[dict]) -> bool:
    """Each hub's content contains zero answer tokens from its own cluster
    (a hub names rule IDs but defines no values — spec §4 rule 1)."""
    passed = True
    mu_by_id = {m["mu_id"]: m for m in mus}

    for cluster, hub_id in HUB_IDS.items():
        hub = mu_by_id.get(hub_id)
        if not hub:
            print(f"  FAIL: {cluster} hub MU not found")
            passed = False
            continue
        own_tokens = _answer_tokens_for_cluster(_letter(cluster))
        leaked = _contains_terms(hub["raw_content"], own_tokens)
        if leaked:
            print(f"  FAIL: {cluster} hub contains own value token(s): {leaked}")
            passed = False
        else:
            print(f"  OK: {cluster} hub contains none of its own value tokens")

    return passed


def check_6_sibling_token_exclusion(mus: list[dict]) -> bool:
    """Each spoke's content contains none of its sibling spokes' answer tokens
    within the same cluster (spec §4 rule 2)."""
    passed = True
    mu_by_id = {m["mu_id"]: m for m in mus}

    for key, mu_id in MU_IDS.items():
        if "spoke" not in key:
            continue
        spoke = mu_by_id.get(mu_id)
        if not spoke:
            print(f"  FAIL: {key} MU not found")
            passed = False
            continue
        letter = key[0]
        sibling_tokens = [
            tok for sib_key, toks in ANSWER_TOKENS.items()
            if sib_key.startswith(letter + "_") and sib_key != key
            for tok in toks
        ]
        leaked = _contains_terms(spoke["raw_content"], sibling_tokens)
        if leaked:
            print(f"  FAIL: {key} contains sibling token(s): {leaked}")
            passed = False

    if passed:
        print("  OK: no spoke contains a sibling's answer token")
    return passed


def check_7_global_leakage(mus: list[dict]) -> bool:
    """For every MU, scan its content for every OTHER MU's answer tokens
    project-wide (whole-phrase, case-insensitive). The strongest check: it
    validates the frozen text itself, so it also catches a hand-authored
    manifest repair that pasted in the wrong token."""
    passed = True
    # token -> owning mu_key
    token_owner: dict[str, str] = {}
    for key, toks in ANSWER_TOKENS.items():
        for tok in toks:
            token_owner[tok] = key

    for mu in mus:
        this_key = ID_TO_KEY.get(mu["mu_id"], "?")
        foreign = [tok for tok, owner in token_owner.items() if owner != this_key]
        leaked = _contains_terms(mu["raw_content"], foreign)
        if leaked:
            owners = {tok: token_owner[tok] for tok in leaked}
            print(f"  FAIL: {this_key} leaks foreign answer token(s): {owners}")
            passed = False

    if passed:
        print("  OK: no MU contains any other MU's answer token")
    return passed


def check_8_token_format(mus: list[dict]) -> bool:
    """Every single-word answer token is word-like (^[A-Za-z0-9-]+$).
    Multi-word phrase tokens are exempt (spec §4 rule 4). This validates the
    constant table itself, so it is independent of generated content."""
    token_re = re.compile(r"^[A-Za-z0-9-]+$")
    bad = []
    for key, toks in ANSWER_TOKENS.items():
        for tok in toks:
            if " " in tok:
                continue
            if not token_re.match(tok):
                bad.append((key, tok))
    if bad:
        print(f"  FAIL: malformed single-word answer token(s): {bad}")
        return False
    print("  OK: all single-word answer tokens match ^[A-Za-z0-9-]+$")
    return True


def check_9_probe_sanity(mus: list[dict], edges: list[dict]) -> bool:
    """Baseline probe sanity via BM25 simulation. Hard FAIL only on a bridge
    surfacing at K1; the two new v3 sub-checks (hub->spoke K2 reach, two-hit
    bar) are WARN-not-FAIL because BM25 scoring on real generated prose has
    edge cases the fixed word lists don't (BUILD-PLAN.md M25 risk note)."""
    passed = True
    mu_by_id = {m["mu_id"]: m for m in mus}

    def cluster_of(mid: str) -> str:
        return mu_by_id.get(mid, {}).get("cluster_id", "?")

    # --- Sub-check A: bridges must NOT surface at K1 (hard FAIL, as v2) ---
    for cluster in ("cluster_a", "cluster_b", "cluster_c", "cluster_d"):
        bridge_id = BRIDGE_IDS[HUB_TO_BRIDGE[cluster]]
        query = " ".join(ENTRY_VOCAB[cluster])
        result = _simulate_retrieval(query, mus, edges)
        if bridge_id in result["k1_ids"]:
            print(f"  FAIL: {cluster} query surfaces bridge at K1 "
                  f"(BM25 finds it trivially)")
            passed = False
        else:
            print(f"  OK: {cluster} query does not surface bridge at K1")

    # --- Sub-check B: hub query reaches a same-cluster spoke at K2 (WARN) ---
    for cluster in ("cluster_a", "cluster_b", "cluster_c", "cluster_d"):
        hub_id = HUB_IDS[cluster]
        query = " ".join(ENTRY_VOCAB[cluster])
        result = _simulate_retrieval(query, mus, edges)
        k2_clusters = [cluster_of(m) for m in result["k2_ids"]]
        hub_in_k1 = hub_id in result["k1_ids"]
        spoke_in_k2 = any(
            cluster_of(m) == cluster and "spoke" in ID_TO_KEY.get(m, "")
            for m in result["k2_ids"]
        )
        if hub_in_k1 and spoke_in_k2:
            print(f"  OK: {cluster} hub query -> same-cluster spoke at K2")
        else:
            print(f"  WARN: {cluster} hub query did not reach a same-cluster "
                  f"spoke at K2 (hub_in_k1={hub_in_k1}, K2={k2_clusters}) — "
                  f"BM25 edge case, review generated content")

    # --- Sub-check C: two-hit bar — a query of hub-saturated entry vocabulary
    #     that omits every spoke's anchor word should surface no spoke in the
    #     K1 multi-hit set (WARN). Placement rule 6: without its anchor word a
    #     spoke has only the family-name hit, so it cannot clear the two-hit
    #     bar. Every anchor IS an entry-vocab word, so the "no anchor word"
    #     query is exactly ENTRY_VOCAB minus the cluster's anchor set. ---
    for cluster in ("cluster_a", "cluster_b", "cluster_c", "cluster_d"):
        letter = _letter(cluster)
        cluster_anchors = {
            w for key, w in ANCHOR_WORDS.items() if key.startswith(letter + "_")
        }
        non_anchor_entry = [w for w in ENTRY_VOCAB[cluster]
                            if w not in cluster_anchors]
        query = " ".join(non_anchor_entry)
        result = _simulate_retrieval(query, mus, edges)
        spokes_in_multi = [
            ID_TO_KEY.get(m, m) for m in result["multi_hit"]
            if cluster_of(m) == cluster and "spoke" in ID_TO_KEY.get(m, "")
        ]
        if spokes_in_multi:
            print(f"  WARN: {cluster} entry-vocab-only query put spoke(s) in the "
                  f"K1 multi-hit set: {spokes_in_multi} — headroom risk, review "
                  f"generated content")
        else:
            print(f"  OK: {cluster} entry-vocab-only query surfaces no spoke at "
                  f"the K1 two-hit bar")

    return passed


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def _load_fixture(use_frozen: bool) -> dict:
    if use_frozen:
        if not FROZEN_PATH.exists():
            print(f"ERROR: frozen fixture not found at {FROZEN_PATH}")
            sys.exit(1)
        with gzip.open(FROZEN_PATH, "rt", encoding="utf-8") as f:
            return json.load(f)
    if not DRAFT_PATH.exists():
        print(f"ERROR: draft fixture not found at {DRAFT_PATH}")
        print("Run: python scripts/generate_seed_v3.py")
        sys.exit(1)
    with open(DRAFT_PATH, "r", encoding="utf-8") as f:
        return json.load(f)


def main() -> None:
    parser = argparse.ArgumentParser(description="Verify the seed_v3 fixture.")
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--draft", action="store_true",
                       help="verify seed_v3_draft.json (default)")
    group.add_argument("--frozen", action="store_true",
                       help="verify the frozen seed_v3.json.gz")
    args = parser.parse_args()
    use_frozen = args.frozen
    source = FROZEN_PATH if use_frozen else DRAFT_PATH

    print("=" * 60)
    print("verify_seed_v3.py — seed fixture verification")
    print(f"Source: {source}  ({'FROZEN' if use_frozen else 'DRAFT'})")
    print("=" * 60)

    data = _load_fixture(use_frozen)
    mus = data.get("memory_units", [])
    edges = data.get("edges", [])
    version = data.get("fixture_version", "?")

    print(f"\nFixture version: {version}")
    print(f"MUs: {len(mus)}  Edges: {len(edges)}")

    checks = [
        ("Check 1: MU count and cluster distribution", check_1_mu_count, (mus,)),
        ("Check 2: Edge count and reciprocal wiring", check_2_edge_wiring, (mus, edges)),
        ("Check 3: Hub<->bridge connectivity (both directions)", check_3_connectivity, (mus, edges)),
        ("Check 4: Lexical-distinctness of bridges (two-tier)", check_4_lexical_distinctness, (mus,)),
        ("Check 5: Hub token exclusion", check_5_hub_token_exclusion, (mus,)),
        ("Check 6: Spoke sibling-token exclusion", check_6_sibling_token_exclusion, (mus,)),
        ("Check 7: Global answer-token leakage scan", check_7_global_leakage, (mus,)),
        ("Check 8: Answer-token format check", check_8_token_format, (mus,)),
        ("Check 9: Baseline probe sanity (BM25 simulation)", check_9_probe_sanity, (mus, edges)),
    ]

    results = {}
    for i, (title, fn, fn_args) in enumerate(checks, start=1):
        print(f"\n--- {title} ---")
        results[f"check_{i}"] = fn(*fn_args)

    print("\n" + "=" * 60)
    print("RESULTS SUMMARY")
    print("=" * 60)
    all_passed = True
    for check, passed in results.items():
        status = "PASS" if passed else "FAIL"
        print(f"  {check}: {status}")
        if not passed:
            all_passed = False

    print()
    if all_passed:
        target = "seed_v3.json.gz" if use_frozen else "seed_v3_draft.json"
        print(f"ALL CHECKS PASS — {target} verified.")
        if not use_frozen:
            print("Next: Paul reviews content, freezes to seed_v3.json.gz, then "
                  "runs `python scripts/verify_seed_v3.py --frozen`.")
    else:
        print("ONE OR MORE CHECKS FAILED — fix content and rerun.")
        sys.exit(1)


if __name__ == "__main__":
    main()
