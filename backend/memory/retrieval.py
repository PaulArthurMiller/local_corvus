"""
BM25 keyword retrieval + two-level traversal funnel.

Stage 1 — BM25 scan:
  Tokenize all MU raw_content, build an in-memory BM25Okapi index,
  query with the top-N keywords extracted from the user message,
  collect the top `retrieval_keyword_pool_size` candidates per keyword.

Stage 2 — K1 selection:
  Retain candidates with >= 2 keyword hits. Score each by summing the
  weights of all edges connected to it (graph-centrality proxy) plus the
  cumulative BM25 score. Take top `retrieval_level1_top_k`.

Stage 3 — K2 traversal:
  From K1, follow outgoing directed edges. Score each reachable target by
  summing incoming edge weights * `directional_edge_weight_multiplier`.
  Take top `retrieval_level2_top_k` (excluding MUs already in K1).

Returns the union of K1 + K2 as the context set for the extractor.
The BM25 index is rebuilt on every call — simple and correct for the
small MU counts of a local research session.
"""

import logging
import re
from dataclasses import dataclass, field

from rank_bm25 import BM25Okapi

logger = logging.getLogger(__name__)

_WORD_RE = re.compile(r"\b\w+\b")

_STOPWORDS = frozenset({
    "a", "an", "the", "is", "it", "in", "on", "at", "to", "for",
    "of", "and", "or", "but", "not", "with", "this", "that", "i",
    "you", "he", "she", "we", "they", "be", "was", "were", "are",
    "has", "have", "had", "do", "did", "does", "will", "would",
    "could", "should", "may", "might", "can",
})


def _tokenize(text: str) -> list[str]:
    """Lowercase word tokens with stopwords removed."""
    return [w for w in _WORD_RE.findall(text.lower()) if w not in _STOPWORDS]


@dataclass
class RetrievalResult:
    mus: list[dict] = field(default_factory=list)
    keywords: list[str] = field(default_factory=list)
    activated_mu_ids: list[str] = field(default_factory=list)
    activated_edge_ids: list[str] = field(default_factory=list)

    # Cumulative BM25 score per candidate MU that matched at least one keyword
    bm25_scores_per_mu: dict[str, float] = field(default_factory=dict)

    # K1 MU IDs — BM25 funnel entry points before traversal.
    # Distinguishes "surfaced via lexical match" from "surfaced via edge traversal."
    entry_point_mu_ids: list[str] = field(default_factory=list)

    # All multi-hit (>=2 keyword) candidates before K1 cutoff.
    # Each entry: {mu_id, keyword_hit_count, bm25_score, edge_weight_sum}
    candidate_set_before_ranking: list[dict] = field(default_factory=list)

    # Ordered hops K1→K2. Each entry: {source_mu_id, edge_id, target_mu_id, hop_depth}
    # hop_depth=1 for the current single-hop design; field is future-proof.
    traversal_path: list[dict] = field(default_factory=list)

    # {mu_id: depth} — 0 for K1 (lexical entry), 1 for K2 (traversal hop).
    hop_depth_per_retrieved_mu: dict[str, int] = field(default_factory=dict)

    # Total edge weight contribution per candidate MU (sum of connected edge weights).
    # Separates edge-learned signal from BM25 lexical signal in the K1 composite score.
    edge_contributions: dict[str, float] = field(default_factory=dict)


async def retrieve(query: str, store, config: dict) -> RetrievalResult:
    """
    Run BM25 + traversal funnel against all MUs in the store.

    Returns a RetrievalResult with the retrieved MUs and traversal metadata.
    Returns an empty result if the store has no MUs or no keywords match.
    """
    hp = config.get("hyperparameters", {})
    pool_size: int = hp.get("retrieval_keyword_pool_size", 20)
    keyword_count: int = hp.get("retrieval_keyword_count", 5)
    level1_k: int = hp.get("retrieval_level1_top_k", 3)
    level2_k: int = hp.get("retrieval_level2_top_k", 1)
    dir_multiplier: float = float(hp.get("directional_edge_weight_multiplier", 1.5))

    all_mus = await store.get_all_mus()
    all_edges = await store.get_all_edges()

    if not all_mus:
        logger.debug("retrieve: store is empty — skipping retrieval")
        return RetrievalResult()

    # --- Stage 1: BM25 index ---
    mu_ids = [m["mu_id"] for m in all_mus]
    tokenized_corpus = [_tokenize(m["raw_content"]) for m in all_mus]

    # BM25Okapi requires at least one token per document; skip empty docs gracefully
    bm25 = BM25Okapi(
        [tokens if tokens else ["_empty_"] for tokens in tokenized_corpus]
    )

    query_tokens = _tokenize(query)
    keywords = query_tokens[:keyword_count]

    if not keywords:
        logger.debug("retrieve: no keywords after tokenization")
        return RetrievalResult()

    logger.info("retrieve: query keywords=%s pool_size=%d", keywords, pool_size)

    # Accumulate keyword hits and BM25 scores per candidate MU
    candidate_hits: dict[str, set[str]] = {}
    candidate_scores: dict[str, float] = {}

    for keyword in keywords:
        scores = bm25.get_scores([keyword])
        indexed = sorted(enumerate(scores), key=lambda x: x[1], reverse=True)
        for idx, score in indexed[:pool_size]:
            if score <= 0.0:
                break
            mu_id = mu_ids[idx]
            candidate_hits.setdefault(mu_id, set()).add(keyword)
            candidate_scores[mu_id] = candidate_scores.get(mu_id, 0.0) + score

    logger.info(
        "retrieve: BM25 produced %d candidates (pool_size=%d per keyword)",
        len(candidate_hits),
        pool_size,
    )

    # --- Stage 2: K1 — filter >= 2 keyword hits, rank by edge weight + BM25 score ---
    multi_hit = [mid for mid, hits in candidate_hits.items() if len(hits) >= 2]

    # Build in-memory index of edge weights and outgoing edges per MU
    edge_weight_sum: dict[str, float] = {}
    outgoing_edges: dict[str, list[dict]] = {}
    for edge in all_edges:
        src = edge["source_mu_id"]
        tgt = edge["target_mu_id"]
        w = edge["weight"]
        edge_weight_sum[src] = edge_weight_sum.get(src, 0.0) + w
        edge_weight_sum[tgt] = edge_weight_sum.get(tgt, 0.0) + w
        outgoing_edges.setdefault(src, []).append(edge)

    def _k1_score(mu_id: str) -> float:
        return edge_weight_sum.get(mu_id, 0.0) + candidate_scores.get(mu_id, 0.0)

    sorted_candidates = sorted(multi_hit, key=_k1_score, reverse=True)
    k1_ids = sorted_candidates[:level1_k]

    # Build candidate_set_before_ranking: all multi-hit candidates before K1 cutoff
    candidate_set_before_ranking = [
        {
            "mu_id": mid,
            "keyword_hit_count": len(candidate_hits[mid]),
            "bm25_score": candidate_scores.get(mid, 0.0),
            "edge_weight_sum": edge_weight_sum.get(mid, 0.0),
        }
        for mid in sorted_candidates
    ]

    logger.info(
        "retrieve: K1=%d MUs selected (from %d multi-hit candidates)",
        len(k1_ids),
        len(multi_hit),
    )

    # --- Stage 3: K2 — follow outgoing edges from K1 ---
    k2_scores: dict[str, float] = {}
    traversed_edge_ids: list[str] = []
    traversal_path: list[dict] = []
    k1_set = set(k1_ids)

    for mu_id in k1_ids:
        for edge in outgoing_edges.get(mu_id, []):
            target = edge["target_mu_id"]
            if target in k1_set:
                continue
            k2_scores[target] = (
                k2_scores.get(target, 0.0) + edge["weight"] * dir_multiplier
            )
            traversed_edge_ids.append(edge["edge_id"])
            traversal_path.append({
                "source_mu_id": mu_id,
                "edge_id": edge["edge_id"],
                "target_mu_id": target,
                "hop_depth": 1,
            })

    sorted_k2 = sorted(k2_scores.items(), key=lambda x: x[1], reverse=True)
    k2_ids = [mu_id for mu_id, _ in sorted_k2[:level2_k]]

    logger.info("retrieve: K2=%d MUs selected", len(k2_ids))

    activated_mu_ids = k1_ids + k2_ids

    # Build hop_depth_per_retrieved_mu: 0 for K1, 1 for K2
    hop_depth_per_retrieved_mu: dict[str, int] = {}
    for mid in k1_ids:
        hop_depth_per_retrieved_mu[mid] = 0
    for mid in k2_ids:
        hop_depth_per_retrieved_mu[mid] = 1

    mu_by_id = {m["mu_id"]: m for m in all_mus}
    retrieved_mus = [mu_by_id[mid] for mid in activated_mu_ids if mid in mu_by_id]

    logger.info(
        "retrieve: returning %d MUs (K1=%d K2=%d traversed_edges=%d)",
        len(retrieved_mus),
        len(k1_ids),
        len(k2_ids),
        len(traversed_edge_ids),
    )

    return RetrievalResult(
        mus=retrieved_mus,
        keywords=keywords,
        activated_mu_ids=activated_mu_ids,
        activated_edge_ids=traversed_edge_ids,
        bm25_scores_per_mu=dict(candidate_scores),
        entry_point_mu_ids=list(k1_ids),
        candidate_set_before_ranking=candidate_set_before_ranking,
        traversal_path=traversal_path,
        hop_depth_per_retrieved_mu=hop_depth_per_retrieved_mu,
        edge_contributions={mid: edge_weight_sum.get(mid, 0.0) for mid in multi_hit},
    )
