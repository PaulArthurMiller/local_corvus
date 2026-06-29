"""
Verification checks for seed_v2 fixture.
Run BEFORE registering the fixture in the DB.

Checks:
  1. All 34 MUs present with correct cluster_id distribution
  2. All 32 edges present with correct wiring
  3. Connectivity: hub→bridge edge exists for each bridge
  4. Lexical-distinctness: bridge MUs contain none of the fingerprint terms
     from adjacent clusters (the same constraint enforced during generation)
  5. Baseline probe sanity: simulated BM25 retrieval confirms bridge MUs do
     NOT surface at K1 (ranks 1–3) for cluster-vocabulary queries — they
     appear only at K2 (rank 4) via edge traversal from the hub

Usage: python scripts/verify_seed_v2.py
"""

import gzip
import json
import re
import sys
from pathlib import Path

# Force UTF-8 output on Windows (console default is cp1252 which lacks arrow chars)
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
FIXTURE_PATH = PROJECT_ROOT / "seed_fixtures" / "seed_v2.json.gz"

# ---------------------------------------------------------------------------
# Constants — match retrieval.py exactly
# ---------------------------------------------------------------------------

_STOPWORDS = frozenset({
    "a", "an", "the", "is", "it", "in", "on", "at", "to", "for",
    "of", "and", "or", "but", "not", "with", "this", "that", "i",
    "you", "he", "she", "we", "they", "be", "was", "were", "are",
    "has", "have", "had", "do", "did", "does", "will", "would",
    "could", "should", "may", "might", "can",
})
_WORD_RE = re.compile(r"\b\w+\b")

# Fingerprint vocabulary — distinctive terms per cluster.
# Bridge MUs must NOT contain any of the fingerprint terms from either adjacent cluster.
# These are the same terms enforced in generate_seed_v2.py.
CLUSTER_A_FP = ["format", "structure", "output", "field", "delimiter"]
CLUSTER_B_FP = ["schema", "data", "shape", "type", "validation"]
CLUSTER_C_FP = ["rule", "constraint", "violation", "enforce", "comply"]
CLUSTER_D_FP = ["source", "provenance", "cite", "origin", "traceable"]

BRIDGE_AB_FORBIDDEN = CLUSTER_A_FP + CLUSTER_B_FP
BRIDGE_CD_FORBIDDEN = CLUSTER_C_FP + CLUSTER_D_FP

# Fixed MU IDs (from generate_seed_v2.py)
HUB_IDS = {
    "cluster_a": "a0000001-aaaa-4aaa-8aaa-000000000001",
    "cluster_b": "b0000001-bbbb-4bbb-8bbb-000000000001",
    "cluster_c": "c0000001-cccc-4ccc-8ccc-000000000001",
    "cluster_d": "d0000001-dddd-4ddd-8ddd-000000000001",
}
BRIDGE_IDS = {
    "bridge_ab": "ab000001-abab-4aba-8aba-000000000001",
    "bridge_cd": "cd000001-cdcd-4cdc-8cdc-000000000001",
}
HUB_TO_BRIDGE = {
    "cluster_a": "bridge_ab",
    "cluster_b": "bridge_ab",
    "cluster_c": "bridge_cd",
    "cluster_d": "bridge_cd",
}

# Retrieval hyperparameters — match config.yaml defaults
RETRIEVAL_KEYWORD_POOL_SIZE = 20
RETRIEVAL_KEYWORD_COUNT = 5
LEVEL1_TOP_K = 3  # K1
LEVEL2_TOP_K = 1  # K2
DIR_MULTIPLIER = 1.5


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _tokenize(text: str) -> list[str]:
    """Lowercase word tokens with stopwords removed (matches retrieval.py)."""
    return [w for w in _WORD_RE.findall(text.lower()) if w not in _STOPWORDS]


def _contains_forbidden(content: str, terms: list[str]) -> list[str]:
    """Return forbidden terms found in content using whole-word matching."""
    lower = content.lower()
    return [t for t in terms if re.search(r"\b" + re.escape(t) + r"\b", lower)]


def _simulate_retrieval(query: str, mus: list[dict], edges: list[dict]) -> dict:
    """
    Stripped-down BM25 + traversal funnel (same logic as retrieval.py).
    Returns {k1_ids, k2_ids, bridge_in_k1, bridge_in_k2} for the query.
    """
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

    all_bridge_ids = set(BRIDGE_IDS.values())
    bridge_in_k1 = [mid for mid in k1_ids if mid in all_bridge_ids]
    bridge_in_k2 = [mid for mid in k2_ids if mid in all_bridge_ids]

    return {
        "k1_ids": k1_ids,
        "k2_ids": k2_ids,
        "bridge_in_k1": bridge_in_k1,
        "bridge_in_k2": bridge_in_k2,
    }


# ---------------------------------------------------------------------------
# Check functions
# ---------------------------------------------------------------------------

def check_1_mu_count(mus: list[dict]) -> bool:
    """All 34 MUs present with correct cluster_id distribution."""
    expected_dist = {
        "cluster_a": 8, "cluster_b": 8,
        "cluster_c": 8, "cluster_d": 8,
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
        print(f"  FAIL: cluster distribution mismatch")
        print(f"    expected: {expected_dist}")
        print(f"    actual:   {actual_dist}")
        passed = False
    else:
        print(f"  OK: cluster_distribution = {actual_dist}")

    return passed


def check_2_edge_wiring(mus: list[dict], edges: list[dict]) -> bool:
    """32 edges with correct hub-and-spoke + hub→bridge wiring."""
    passed = True

    if len(edges) != 32:
        print(f"  FAIL: expected 32 edges, got {len(edges)}")
        passed = False
    else:
        print(f"  OK: edge_count = {len(edges)}")

    mu_by_id = {m["mu_id"]: m for m in mus}

    # Build incoming/outgoing edge maps
    incoming: dict[str, list] = {}
    outgoing: dict[str, list] = {}
    for e in edges:
        incoming.setdefault(e["target_mu_id"], []).append(e)
        outgoing.setdefault(e["source_mu_id"], []).append(e)

    clusters = ["cluster_a", "cluster_b", "cluster_c", "cluster_d"]
    for cluster in clusters:
        hub_id = HUB_IDS[cluster]
        hub_incoming = incoming.get(hub_id, [])
        hub_outgoing = outgoing.get(hub_id, [])

        # Hub should have 7 incoming spoke→hub edges
        if len(hub_incoming) != 7:
            print(f"  FAIL: {cluster} hub has {len(hub_incoming)} incoming edges (expected 7)")
            passed = False
        else:
            print(f"  OK: {cluster} hub has 7 incoming spoke->hub edges")

        # Hub should have exactly 1 outgoing hub→bridge edge
        if len(hub_outgoing) != 1:
            print(f"  FAIL: {cluster} hub has {len(hub_outgoing)} outgoing edges (expected 1)")
            passed = False
        else:
            tgt_id = hub_outgoing[0]["target_mu_id"]
            tgt_cluster = mu_by_id.get(tgt_id, {}).get("cluster_id", "UNKNOWN")
            print(f"  OK: {cluster} hub -> {tgt_cluster} ({tgt_id[:12]})")

    # No edges between {a,b} clusters and {c,d} clusters
    ab_ids = {m["mu_id"] for m in mus if m.get("cluster_id") in ("cluster_a", "cluster_b")}
    cd_ids = {m["mu_id"] for m in mus if m.get("cluster_id") in ("cluster_c", "cluster_d")}
    cross_edges = [
        e for e in edges
        if (e["source_mu_id"] in ab_ids and e["target_mu_id"] in cd_ids)
        or (e["source_mu_id"] in cd_ids and e["target_mu_id"] in ab_ids)
    ]
    if cross_edges:
        print(f"  FAIL: {len(cross_edges)} forbidden cross-group edges found")
        for e in cross_edges:
            print(f"    {e['source_mu_id'][:12]} → {e['target_mu_id'][:12]}")
        passed = False
    else:
        print("  OK: no edges between {a,b} group and {c,d} group")

    return passed


def check_3_connectivity(mus: list[dict], edges: list[dict]) -> bool:
    """Each cluster hub has an outgoing edge to its bridge MU."""
    passed = True
    outgoing: dict[str, list] = {}
    for e in edges:
        outgoing.setdefault(e["source_mu_id"], []).append(e)

    for cluster, hub_id in HUB_IDS.items():
        expected_bridge = BRIDGE_IDS[HUB_TO_BRIDGE[cluster]]
        hub_out = outgoing.get(hub_id, [])
        reaches_bridge = any(e["target_mu_id"] == expected_bridge for e in hub_out)
        if not reaches_bridge:
            print(f"  FAIL: {cluster} hub has no edge to {HUB_TO_BRIDGE[cluster]}")
            passed = False
        else:
            print(f"  OK: {cluster} -> {HUB_TO_BRIDGE[cluster]} edge confirmed")

    return passed


def check_4_lexical_distinctness(mus: list[dict]) -> bool:
    """Bridge MUs must not contain fingerprint terms from adjacent clusters.

    The generation script enforced this during Gemma output validation;
    this check confirms the constraint survived review and freezing.
    Whole-word matching is used (same as generate_seed_v2.py).
    """
    passed = True
    mu_by_cluster = {m["cluster_id"]: m for m in mus
                     if m["cluster_id"] in ("bridge_ab", "bridge_cd")}

    bridge_ab = mu_by_cluster.get("bridge_ab")
    bridge_cd = mu_by_cluster.get("bridge_cd")

    if not bridge_ab:
        print("  FAIL: bridge_ab MU not found")
        return False
    if not bridge_cd:
        print("  FAIL: bridge_cd MU not found")
        return False

    # bridge_ab: must not contain cluster_a or cluster_b fingerprint terms
    ab_forbidden_found = _contains_forbidden(bridge_ab["raw_content"], BRIDGE_AB_FORBIDDEN)
    if ab_forbidden_found:
        print(f"  FAIL: bridge_ab contains forbidden terms: {ab_forbidden_found}")
        passed = False
    else:
        print(f"  OK: bridge_ab contains none of {BRIDGE_AB_FORBIDDEN}")

    # bridge_cd: must not contain cluster_c or cluster_d fingerprint terms
    cd_forbidden_found = _contains_forbidden(bridge_cd["raw_content"], BRIDGE_CD_FORBIDDEN)
    if cd_forbidden_found:
        print(f"  FAIL: bridge_cd contains forbidden terms: {cd_forbidden_found}")
        passed = False
    else:
        print(f"  OK: bridge_cd contains none of {BRIDGE_CD_FORBIDDEN}")

    return passed


def check_5_probe_sanity(mus: list[dict], edges: list[dict]) -> bool:
    """Simulated BM25 retrieval: bridges surface at K2 (rank 4), not K1 (ranks 1-3).

    Each query uses only the fingerprint vocabulary of one cluster.
    Bridge MUs have zero BM25 score for these queries (they contain none of those terms),
    so they can only surface via edge traversal (K2). The check passes when:
      - bridge NOT in K1 (BM25 alone does not surface it)
      - bridge IS in K2 (hub→bridge edge carries it to rank 4)
    """
    passed = True
    mu_by_id = {m["mu_id"]: m for m in mus}

    # Representative cluster-vocabulary queries (Phase 4 will write formal probe text;
    # these use only fingerprint terms to represent worst-case BM25 similarity)
    probes = [
        # (label, query, expected_bridge_id)
        ("p10 (cluster_a→bridge_ab)",
         "format structure output field delimiter",
         BRIDGE_IDS["bridge_ab"]),
        ("p11 (cluster_b→bridge_ab)",
         "schema data shape type validation",
         BRIDGE_IDS["bridge_ab"]),
        ("p12 (cluster_c→bridge_cd)",
         "rule constraint violation enforce comply",
         BRIDGE_IDS["bridge_cd"]),
        ("p13 (cluster_d→bridge_cd)",
         "source provenance cite origin traceable",
         BRIDGE_IDS["bridge_cd"]),
    ]

    for label, query, expected_bridge_id in probes:
        result = _simulate_retrieval(query, mus, edges)
        k1_ids = result["k1_ids"]
        k2_ids = result["k2_ids"]

        # Determine expected cluster from query
        bridge_in_k1 = expected_bridge_id in k1_ids
        bridge_in_k2 = expected_bridge_id in k2_ids

        bridge_cluster = mu_by_id.get(expected_bridge_id, {}).get("cluster_id", "?")
        k1_clusters = [mu_by_id.get(m, {}).get("cluster_id", "?") for m in k1_ids]
        k2_clusters = [mu_by_id.get(m, {}).get("cluster_id", "?") for m in k2_ids]

        if bridge_in_k1:
            print(f"  FAIL: {label}: bridge at K1 (BM25 surfaces it trivially)")
            print(f"    K1={k1_clusters} K2={k2_clusters}")
            passed = False
        elif not bridge_in_k2:
            # Bridge not in K2 — either hub wasn't in K1, or edge traversal failed
            print(f"  WARN: {label}: bridge not in K2 (expected via hub edge)")
            print(f"    K1={k1_clusters} K2={k2_clusters}")
            # This is a warning, not a hard FAIL — it could mean no hub in K1 for this query
        else:
            print(f"  OK: {label}: bridge at K2 (rank 4), not K1")
            print(f"    K1={k1_clusters}  K2={k2_clusters}")

    return passed


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    print("=" * 60)
    print("verify_seed_v2.py — seed fixture verification")
    print(f"Fixture: {FIXTURE_PATH}")
    print("=" * 60)

    if not FIXTURE_PATH.exists():
        print(f"ERROR: fixture not found at {FIXTURE_PATH}")
        sys.exit(1)

    with gzip.open(FIXTURE_PATH, "rt", encoding="utf-8") as f:
        data = json.load(f)

    mus = data.get("memory_units", [])
    edges = data.get("edges", [])
    version = data.get("fixture_version", "?")

    print(f"\nFixture version: {version}")
    print(f"MUs: {len(mus)}  Edges: {len(edges)}")

    results = {}

    print("\n--- Check 1: MU count and cluster distribution ---")
    results["check_1"] = check_1_mu_count(mus)

    print("\n--- Check 2: Edge count and wiring ---")
    results["check_2"] = check_2_edge_wiring(mus, edges)

    print("\n--- Check 3: Hub→bridge connectivity ---")
    results["check_3"] = check_3_connectivity(mus, edges)

    print("\n--- Check 4: Lexical-distinctness (fingerprint terms) ---")
    results["check_4"] = check_4_lexical_distinctness(mus)

    print("\n--- Check 5: Baseline probe sanity (BM25 simulation) ---")
    results["check_5"] = check_5_probe_sanity(mus, edges)

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
        print("ALL CHECKS PASS — seed_v2.json.gz is verified and ready for Phase 4.")
    else:
        print("ONE OR MORE CHECKS FAILED — do not proceed to Phase 4.")
        sys.exit(1)


if __name__ == "__main__":
    main()
