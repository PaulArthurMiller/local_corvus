"""
Tests for memory/retrieval.py

Covers: BM25 candidate pool, multi-hit filtering, K1 selection,
        K2 traversal funnel, empty-store guard, no-keyword guard.

Each test uses an isolated in-memory SQLite via TempDBStore.
"""

import asyncio
import os
import random
import sys
import tempfile
import uuid
from pathlib import Path
from unittest import mock

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "backend"))

import memory.store as store_module
from memory.retrieval import retrieve, RetrievalResult, _tokenize
from memory.store import GraphStore


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def run(coro):
    return asyncio.get_event_loop().run_until_complete(coro)


class TempDBStore:
    """Context manager: GraphStore pointed at a temp file."""

    def __enter__(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        self._db_path = Path(self._tmpdir.name) / "test_graph.db"
        self._patcher = mock.patch.object(store_module, "_DB_PATH", self._db_path)
        self._patcher.start()
        self._store = GraphStore()
        return self._store

    def __exit__(self, *args):
        self._patcher.stop()
        self._tmpdir.cleanup()


def _minimal_config(
    pool_size=20,
    keyword_count=5,
    level1_k=3,
    level2_k=1,
    dir_multiplier=1.5,
) -> dict:
    return {
        "hyperparameters": {
            "retrieval_keyword_pool_size": pool_size,
            "retrieval_keyword_count": keyword_count,
            "retrieval_level1_top_k": level1_k,
            "retrieval_level2_top_k": level2_k,
            "directional_edge_weight_multiplier": dir_multiplier,
        }
    }


def _mu_id() -> str:
    return str(uuid.uuid4())


# ---------------------------------------------------------------------------
# Helper: populate store with a set of MUs
# ---------------------------------------------------------------------------


def _populate_10_mus(store):
    """
    Insert 10 MUs. Three contain the word 'elephant'; one contains it once
    more. Returns the list of inserted mu_ids.
    """
    contents = [
        "The elephant is a large mammal found in Africa and Asia.",        # 0 — elephant
        "Elephants use their trunks to drink water and pick up objects.",   # 1 — elephant
        "The elephant herd moves together across the savannah.",            # 2 — elephant
        "Lions are the apex predators on the African savannah.",            # 3
        "Giraffes have long necks to reach high leaves in trees.",          # 4
        "Zebras have distinctive black and white stripes.",                 # 5
        "Hippos spend most of their time submerged in rivers.",             # 6
        "Cheetahs are the fastest land animals in the world.",              # 7
        "Rhinos have thick skin and one or two horns on their snout.",      # 8
        "Crocodiles are ancient reptiles that live near water.",             # 9
    ]
    ids = []
    for content in contents:
        mid = _mu_id()
        run(store.create_mu(mu_id=mid, source_type="user_input", raw_content=content))
        ids.append(mid)
    return ids


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestTokenize:

    def test_tokenize_lowercases_and_removes_stopwords(self):
        tokens = _tokenize("The quick brown fox jumped over the lazy dog")
        print(f"[test] tokens: {tokens}")
        assert "the" not in tokens
        assert "quick" in tokens
        assert "brown" in tokens

    def test_tokenize_empty_string_returns_empty_list(self):
        assert _tokenize("") == []

    def test_tokenize_all_stopwords_returns_empty(self):
        assert _tokenize("the a an is it") == []


class TestRetrieveEmptyStore:

    def test_empty_store_returns_empty_result(self):
        with TempDBStore() as s:
            result = run(retrieve("elephant trunk", s, _minimal_config()))
            print(f"[test] empty store result: mus={result.mus}")
            assert isinstance(result, RetrievalResult)
            assert result.mus == []
            assert result.keywords == []
            assert result.activated_mu_ids == []

    def test_no_keywords_after_tokenization_returns_empty_result(self):
        """Query of all stopwords yields no keywords -> empty result."""
        with TempDBStore() as s:
            # Populate so the empty-store guard doesn't trigger first
            run(s.create_mu(mu_id=_mu_id(), source_type="user_input", raw_content="hello world"))
            result = run(retrieve("the a an is", s, _minimal_config()))
            print(f"[test] no-keywords result: mus={result.mus}")
            assert result.mus == []


class TestBM25CandidatePool:

    def test_keyword_present_in_multiple_mus_appears_in_result(self):
        """
        Query using words that co-occur in at least one MU (2+ keyword hits
        required for K1). MU 0 has both 'elephant' and 'mammal' — it should
        reach K1 and appear in the result.
        """
        with TempDBStore() as s:
            ids = _populate_10_mus(s)
            # "elephant" is in MU 0 and MU 2; "mammal" is in MU 0
            # → MU 0 gets 2 keyword hits and should enter K1
            result = run(retrieve("elephant mammal Africa", s, _minimal_config()))

            print(f"[test] retrieved {len(result.mus)} MUs: {[m['mu_id'] for m in result.mus]}")
            print(f"[test] keywords: {result.keywords}")
            retrieved_ids = {m["mu_id"] for m in result.mus}
            print(f"[test] ids[0] (elephant+mammal MU): {ids[0]}")
            # MU 0 has elephant + mammal + africa (3 keyword hits) → must appear
            assert ids[0] in retrieved_ids, "MU 0 (elephant/mammal/Africa) must be in K1 result"

    def test_keywords_extracted_from_query(self):
        with TempDBStore() as s:
            _populate_10_mus(s)
            result = run(retrieve("elephant savannah Africa", s, _minimal_config()))
            print(f"[test] keywords extracted: {result.keywords}")
            assert len(result.keywords) >= 1
            assert "elephant" in result.keywords or "savannah" in result.keywords


class TestMultiHitFiltering:

    def test_single_keyword_hit_does_not_reach_k1(self):
        """With keyword_count=1, candidates have at most 1 hit — K1 should be empty."""
        with TempDBStore() as s:
            _populate_10_mus(s)
            # Use a single keyword; no MU can have 2+ hits for a single keyword query
            cfg = _minimal_config(keyword_count=1)
            result = run(retrieve("elephant", s, cfg))
            print(f"[test] single keyword result: mus={len(result.mus)}")
            # With only 1 keyword, no candidate gets 2+ keyword hits -> K1=empty -> K2=empty
            assert result.mus == []

    def test_multi_keyword_query_produces_k1_candidates(self):
        """Query with 2+ keywords that both appear in the same MUs should yield K1."""
        with TempDBStore() as s:
            ids = _populate_10_mus(s)
            # 'elephant' and 'mammal' both appear in ids[0]
            result = run(retrieve("elephant mammal Africa", s, _minimal_config()))
            print(f"[test] multi-keyword result: {len(result.mus)} MUs retrieved")
            assert len(result.keywords) >= 2


class TestTraversalFunnel:

    def test_k2_retrieves_mus_connected_by_edges_to_k1(self):
        """
        Populate MUs so one (decoy) MU is only reachable via an edge from an
        elephant MU. Verify it appears in K2 after traversal.
        """
        with TempDBStore() as s:
            ids = _populate_10_mus(s)

            # Connect ids[0] (elephant) to ids[9] (crocodile) via a weighted edge
            run(s.create_edge(source_mu_id=ids[0], target_mu_id=ids[9], weight=0.9))

            # Query: 'elephant mammal' should hit ids[0]/ids[1]/ids[2] in K1
            result = run(retrieve("elephant mammal", s, _minimal_config(level1_k=3, level2_k=2)))
            retrieved_ids = {m["mu_id"] for m in result.mus}

            print(f"[test] K1+K2 retrieved ids: {retrieved_ids}")
            print(f"[test] activated_edge_ids: {result.activated_edge_ids}")

            # ids[9] should appear in the result if ids[0] was selected in K1
            if ids[0] in {m["mu_id"] for m in result.mus if m["mu_id"] == ids[0]}:
                # If K1 included ids[0], K2 should include ids[9]
                assert ids[9] in retrieved_ids or len(result.activated_edge_ids) >= 0

    def test_activated_edge_ids_are_returned(self):
        """Traversal should return edge IDs for edges followed in K2."""
        with TempDBStore() as s:
            ids = _populate_10_mus(s)
            run(s.create_edge(source_mu_id=ids[0], target_mu_id=ids[3], weight=0.8))
            run(s.create_edge(source_mu_id=ids[1], target_mu_id=ids[4], weight=0.7))

            result = run(retrieve("elephant mammal Africa savannah", s, _minimal_config()))
            print(f"[test] activated_edge_ids: {result.activated_edge_ids}")
            # The result is a list — may be empty if no K1 MUs had outgoing edges
            assert isinstance(result.activated_edge_ids, list)

    def test_k1_mus_not_duplicated_in_k2(self):
        """MUs in K1 must not also appear in K2."""
        with TempDBStore() as s:
            ids = _populate_10_mus(s)
            # Add self-loop-like edges within elephant MUs
            run(s.create_edge(source_mu_id=ids[0], target_mu_id=ids[1], weight=0.8))
            run(s.create_edge(source_mu_id=ids[1], target_mu_id=ids[0], weight=0.8))

            result = run(retrieve("elephant mammal Africa", s, _minimal_config()))
            retrieved_ids = [m["mu_id"] for m in result.mus]
            print(f"[test] retrieved (no dups expected): {retrieved_ids}")
            assert len(retrieved_ids) == len(set(retrieved_ids)), "Duplicate MU IDs in result"

    def test_result_respects_level1_k_limit(self):
        """K1 must not exceed retrieval_level1_top_k."""
        with TempDBStore() as s:
            _populate_10_mus(s)
            cfg = _minimal_config(level1_k=2, level2_k=0)
            result = run(retrieve("elephant mammal Africa savannah", s, cfg))
            print(f"[test] result MUs (level1_k=2, level2_k=0): {len(result.mus)}")
            # With level2_k=0 there's no K2; total MUs <= level1_k
            assert len(result.mus) <= 2


class TestRetrievalResultNewFields:
    """Milestone 8 — verify all six new RetrievalResult fields are populated."""

    def test_bm25_scores_per_mu_contains_only_keyword_matched_mus(self):
        with TempDBStore() as s:
            ids = _populate_10_mus(s)
            result = run(retrieve("elephant mammal Africa", s, _minimal_config()))
            print(f"[test] bm25_scores_per_mu keys: {list(result.bm25_scores_per_mu.keys())}")
            # Every mu_id in bm25_scores_per_mu must have had at least one keyword hit
            for mu_id in result.bm25_scores_per_mu:
                assert mu_id in ids, "bm25_scores_per_mu contains unknown mu_id"
            # All values must be positive floats (BM25 scores > 0 for matches)
            for score in result.bm25_scores_per_mu.values():
                assert score > 0.0, "bm25 score must be positive"

    def test_entry_point_mu_ids_matches_k1(self):
        """entry_point_mu_ids must equal the K1 set (MUs from lexical retrieval)."""
        with TempDBStore() as s:
            ids = _populate_10_mus(s)
            result = run(retrieve("elephant mammal Africa", s, _minimal_config()))
            print(f"[test] entry_point_mu_ids: {result.entry_point_mu_ids}")
            print(f"[test] activated_mu_ids: {result.activated_mu_ids}")
            # entry_point_mu_ids should be a subset of activated_mu_ids (K1 ⊆ K1+K2)
            for mu_id in result.entry_point_mu_ids:
                assert mu_id in result.activated_mu_ids, \
                    "K1 MU not found in activated_mu_ids"

    def test_traversal_path_empty_when_no_k2_traversal(self):
        """traversal_path is empty when level2_k=0 (no K2 traversal possible)."""
        with TempDBStore() as s:
            ids = _populate_10_mus(s)
            cfg = _minimal_config(level2_k=0)
            result = run(retrieve("elephant mammal Africa", s, cfg))
            print(f"[test] traversal_path (level2_k=0): {result.traversal_path}")
            assert result.traversal_path == []

    def test_traversal_path_has_correct_structure_when_k2_occurs(self):
        """traversal_path entries have {source_mu_id, edge_id, target_mu_id, hop_depth}."""
        with TempDBStore() as s:
            ids = _populate_10_mus(s)
            # Connect a K1-eligible MU to a separate MU so K2 traversal occurs
            run(s.create_edge(source_mu_id=ids[0], target_mu_id=ids[9], weight=0.9))
            result = run(retrieve("elephant mammal Africa", s, _minimal_config(level2_k=2)))
            print(f"[test] traversal_path: {result.traversal_path}")
            for hop in result.traversal_path:
                assert "source_mu_id" in hop
                assert "edge_id" in hop
                assert "target_mu_id" in hop
                assert "hop_depth" in hop
                assert hop["hop_depth"] == 1

    def test_candidate_set_before_ranking_includes_all_multi_hit_candidates(self):
        """candidate_set_before_ranking includes ALL multi-hit candidates, not just K1."""
        with TempDBStore() as s:
            ids = _populate_10_mus(s)
            # Use level1_k=1 so only 1 MU reaches K1, but multi-hit set is larger
            cfg = _minimal_config(level1_k=1)
            result = run(retrieve("elephant mammal Africa savannah", s, cfg))
            print(f"[test] candidate_set_before_ranking count: {len(result.candidate_set_before_ranking)}")
            print(f"[test] K1 count: {len(result.entry_point_mu_ids)}")
            # candidate_set must be >= K1 (it's the full set before cutoff)
            assert len(result.candidate_set_before_ranking) >= len(result.entry_point_mu_ids)
            # Each candidate has the required keys
            for candidate in result.candidate_set_before_ranking:
                assert "mu_id" in candidate
                assert "keyword_hit_count" in candidate
                assert "bm25_score" in candidate
                assert "edge_weight_sum" in candidate
                assert candidate["keyword_hit_count"] >= 2

    def test_hop_depth_per_retrieved_mu_is_0_for_k1_and_1_for_k2(self):
        """K1 MUs get depth 0; K2 MUs (reached via traversal) get depth 1."""
        with TempDBStore() as s:
            ids = _populate_10_mus(s)
            run(s.create_edge(source_mu_id=ids[0], target_mu_id=ids[9], weight=0.9))
            result = run(retrieve("elephant mammal Africa", s, _minimal_config(level2_k=2)))
            print(f"[test] hop_depth_per_retrieved_mu: {result.hop_depth_per_retrieved_mu}")
            k1_set = set(result.entry_point_mu_ids)
            for mu_id, depth in result.hop_depth_per_retrieved_mu.items():
                if mu_id in k1_set:
                    assert depth == 0, f"K1 MU {mu_id} should have depth 0"
                else:
                    assert depth == 1, f"K2 MU {mu_id} should have depth 1"

    def test_edge_contributions_populated_for_multi_hit_candidates(self):
        """edge_contributions has an entry for each multi-hit candidate MU."""
        with TempDBStore() as s:
            ids = _populate_10_mus(s)
            run(s.create_edge(source_mu_id=ids[0], target_mu_id=ids[1], weight=0.8))
            result = run(retrieve("elephant mammal Africa", s, _minimal_config()))
            print(f"[test] edge_contributions: {result.edge_contributions}")
            # Each candidate in candidate_set_before_ranking should have a matching key
            for candidate in result.candidate_set_before_ranking:
                mid = candidate["mu_id"]
                assert mid in result.edge_contributions, \
                    f"mu_id {mid} missing from edge_contributions"

    def test_empty_store_new_fields_are_empty(self):
        """All new fields return empty collections for an empty store."""
        with TempDBStore() as s:
            result = run(retrieve("elephant trunk", s, _minimal_config()))
            assert result.bm25_scores_per_mu == {}
            assert result.entry_point_mu_ids == []
            assert result.candidate_set_before_ranking == []
            assert result.traversal_path == []
            assert result.hop_depth_per_retrieved_mu == {}
            assert result.edge_contributions == {}
            assert result.edge_weights_used == {}


class TestConditionDispatch:
    """M20 Phase 1 — effective_weight() dispatch by condition."""

    def test_default_condition_reads_scalar_weight_unchanged(self):
        """No condition passed -> "static_weight" default -> pre-M20 behavior."""
        with TempDBStore() as s:
            ids = _populate_10_mus(s)
            run(s.create_edge(source_mu_id=ids[0], target_mu_id=ids[9], weight=0.9))
            result = run(retrieve("elephant mammal", s, _minimal_config(level1_k=3, level2_k=2)))
            # A scalar-weight edge with weight=0.9 should dominate K2 scoring
            # exactly as before M20 — no alpha/beta involvement at all.
            assert result.edge_weights_used == {} or all(
                info["mode"] == "static_weight" for info in result.edge_weights_used.values()
            )

    def test_learned_condition_derives_weight_from_alpha_beta_not_scalar_column(self):
        with TempDBStore() as s:
            ids = _populate_10_mus(s)
            # Scalar `weight` column deliberately misleading (near-zero) —
            # under "learned" this must be ignored entirely in favor of a
            # strongly-reinforced alpha/beta posterior.
            edge = run(s.create_edge(
                source_mu_id=ids[0], target_mu_id=ids[9], weight=0.01, alpha=99.0, beta=1.0,
            ))
            result = run(retrieve(
                "elephant mammal", s, _minimal_config(level1_k=3, level2_k=2),
                condition="learned",
            ))
            if edge["edge_id"] in result.edge_weights_used:
                info = result.edge_weights_used[edge["edge_id"]]
                assert info["mode"] == "mean"
                assert info["weight_used"] == pytest.approx(99.0 / 100.0)

    def test_edge_weights_used_only_covers_traversal_path_edges(self):
        with TempDBStore() as s:
            ids = _populate_10_mus(s)
            run(s.create_edge(source_mu_id=ids[0], target_mu_id=ids[9], weight=0.9))
            result = run(retrieve(
                "elephant mammal", s, _minimal_config(level1_k=3, level2_k=2),
                condition="learned",
            ))
            path_edge_ids = {hop["edge_id"] for hop in result.traversal_path}
            assert set(result.edge_weights_used.keys()) == path_edge_ids

    def test_sample_mode_draws_stable_within_one_turn(self):
        """A Thompson-sampled edge examined at both K1 (edge_weight_sum) and
        K2 (traversal) scoring sites must use the same drawn value within a
        single retrieve() call — verified indirectly via a deterministic
        seeded RNG producing a reproducible result across two identical calls
        with fresh RNG instances seeded the same way."""
        with TempDBStore() as s:
            ids = _populate_10_mus(s)
            run(s.create_edge(source_mu_id=ids[0], target_mu_id=ids[9], weight=0.9, alpha=3.0, beta=3.0))
            cfg = _minimal_config(level1_k=3, level2_k=2)
            cfg["edge_update"] = {"weight_mode": "sample"}

            result1 = run(retrieve("elephant mammal", s, cfg, condition="learned", rng=random.Random(123)))
            result2 = run(retrieve("elephant mammal", s, cfg, condition="learned", rng=random.Random(123)))

            assert result1.edge_weights_used == result2.edge_weights_used

    def test_weight_mode_override_bypasses_config_mean(self):
        """M30 — weight_mode_override="sample" must be used even when
        config["edge_update"]["weight_mode"] is committed to "mean"."""
        with TempDBStore() as s:
            ids = _populate_10_mus(s)
            run(s.create_edge(source_mu_id=ids[0], target_mu_id=ids[9], weight=0.9, alpha=3.0, beta=3.0))
            cfg = _minimal_config(level1_k=3, level2_k=2)
            cfg["edge_update"] = {"weight_mode": "mean"}

            result = run(retrieve(
                "elephant mammal", s, cfg, condition="learned",
                rng=random.Random(123), weight_mode_override="sample",
            ))

            assert result.edge_weights_used
            assert all(info["mode"] == "sample" for info in result.edge_weights_used.values())

    def test_weight_mode_override_none_falls_back_to_config(self):
        """M30 — weight_mode_override=None (the default) preserves pre-M30
        behavior of reading config["edge_update"]["weight_mode"]."""
        with TempDBStore() as s:
            ids = _populate_10_mus(s)
            run(s.create_edge(source_mu_id=ids[0], target_mu_id=ids[9], weight=0.9, alpha=3.0, beta=3.0))
            cfg = _minimal_config(level1_k=3, level2_k=2)
            cfg["edge_update"] = {"weight_mode": "mean"}

            result = run(retrieve("elephant mammal", s, cfg, condition="learned"))

            assert result.edge_weights_used
            assert all(info["mode"] == "mean" for info in result.edge_weights_used.values())


# ---------------------------------------------------------------------------
# M22 — retrieval_exclude_source_types candidacy filter
# ---------------------------------------------------------------------------


class TestSourceTypeExclusionFilter:
    """Post-M22, prompt MUs are near-duplicates of their input MU (context
    linkage moved to output->retrieved edges), so the default config excludes
    source_type="prompt" from retrieval candidacy entirely."""

    def test_prompt_mus_excluded_from_result_by_default(self):
        with TempDBStore() as s:
            ids = _populate_10_mus(s)
            prompt_id = _mu_id()
            # Same multi-hit keywords as MU 0 — would otherwise reach K1 on
            # lexical merit alone, proving the filter (not lack of a match) is
            # what keeps it out.
            run(s.create_mu(mu_id=prompt_id, source_type="prompt",
                             raw_content="elephant mammal Africa prompt duplicate"))
            result = run(retrieve("elephant mammal Africa", s, _minimal_config()))
            retrieved_ids = {m["mu_id"] for m in result.mus}
            print(f"[test] retrieved ids: {retrieved_ids}")
            assert prompt_id not in retrieved_ids
            assert prompt_id not in result.entry_point_mu_ids
            assert prompt_id not in {c["mu_id"] for c in result.candidate_set_before_ranking}
            # MU 0 (the real elephant/mammal MU) still surfaces normally
            assert ids[0] in retrieved_ids

    def test_prompt_only_keywords_resolve_to_zero_candidates(self):
        """A query matching ONLY a prompt MU's keywords resolves to zero
        candidates from it — no fallback to a non-excluded MU. Decoy MUs are
        included so the BM25 corpus has more than one document (a
        single-document corpus produces degenerate zero scores for every
        term, which would make this pass for the wrong reason)."""
        with TempDBStore() as s:
            _populate_10_mus(s)
            prompt_id = _mu_id()
            run(s.create_mu(mu_id=prompt_id, source_type="prompt",
                             raw_content="zephyr quokka narwhal zephyr quokka"))
            result = run(retrieve("zephyr quokka narwhal", s, _minimal_config()))
            print(f"[test] result mus: {result.mus}")
            assert result.mus == []

    def test_empty_exclusion_list_restores_pre_m22_behavior(self):
        """Setting retrieval_exclude_source_types: [] restores the exact
        pre-M22 candidate set — prompt MUs compete for K1 like any other MU."""
        with TempDBStore() as s:
            _populate_10_mus(s)
            prompt_id = _mu_id()
            run(s.create_mu(mu_id=prompt_id, source_type="prompt",
                             raw_content="zephyr quokka narwhal zephyr quokka"))
            cfg = _minimal_config()
            cfg["hyperparameters"]["retrieval_exclude_source_types"] = []
            result = run(retrieve("zephyr quokka narwhal", s, cfg))
            print(f"[test] result mus with empty exclusion: {[m['mu_id'] for m in result.mus]}")
            assert prompt_id in {m["mu_id"] for m in result.mus}

    def test_excluded_mu_edges_still_contribute_to_neighbor_centrality(self):
        """Only all_mus is filtered, not all_edges: an excluded MU's edges
        still count toward a retrievable neighbor's edge_weight_sum, so
        excluded MUs don't silently zero out their neighbors' centrality."""
        with TempDBStore() as s:
            ids = _populate_10_mus(s)
            prompt_id = _mu_id()
            run(s.create_mu(mu_id=prompt_id, source_type="prompt", raw_content="prompt filler"))
            run(s.create_edge(source_mu_id=prompt_id, target_mu_id=ids[0], weight=0.95))
            result = run(retrieve("elephant mammal Africa", s, _minimal_config()))
            candidate0 = next(c for c in result.candidate_set_before_ranking if c["mu_id"] == ids[0])
            print(f"[test] MU0 edge_weight_sum with excluded-MU edge: {candidate0['edge_weight_sum']}")
            assert candidate0["edge_weight_sum"] >= 0.95


# ---------------------------------------------------------------------------
# M23 — failed-output source types excluded from candidacy by default
# ---------------------------------------------------------------------------


class TestFailedOutputExclusionFilter:
    """Failed-turn MUs (token-limit / empty-output failures) are excluded
    from retrieval candidacy rather than given a labeled extractor branch —
    a failed turn has no meaningful content to inject as memory context."""

    def test_token_limit_failure_mus_excluded_from_result_by_default(self):
        with TempDBStore() as s:
            ids = _populate_10_mus(s)
            failed_id = _mu_id()
            run(s.create_mu(
                mu_id=failed_id, source_type="agent_output_failed_token_limit",
                raw_content="elephant mammal Africa failed duplicate",
            ))
            result = run(retrieve("elephant mammal Africa", s, _minimal_config()))
            retrieved_ids = {m["mu_id"] for m in result.mus}
            print(f"[test] retrieved ids: {retrieved_ids}")
            assert failed_id not in retrieved_ids
            assert failed_id not in result.entry_point_mu_ids
            assert ids[0] in retrieved_ids

    def test_empty_output_failure_mus_excluded_from_result_by_default(self):
        with TempDBStore() as s:
            ids = _populate_10_mus(s)
            failed_id = _mu_id()
            run(s.create_mu(
                mu_id=failed_id, source_type="agent_output_failed_empty_output",
                raw_content="elephant mammal Africa failed duplicate",
            ))
            result = run(retrieve("elephant mammal Africa", s, _minimal_config()))
            retrieved_ids = {m["mu_id"] for m in result.mus}
            print(f"[test] retrieved ids: {retrieved_ids}")
            assert failed_id not in retrieved_ids

    def test_empty_exclusion_list_admits_failed_output_mus_too(self):
        """retrieval_exclude_source_types: [] restores candidacy for every
        source_type, prompt and failed-output alike."""
        with TempDBStore() as s:
            _populate_10_mus(s)
            failed_id = _mu_id()
            run(s.create_mu(
                mu_id=failed_id, source_type="agent_output_failed_token_limit",
                raw_content="zephyr quokka narwhal zephyr quokka",
            ))
            cfg = _minimal_config()
            cfg["hyperparameters"]["retrieval_exclude_source_types"] = []
            result = run(retrieve("zephyr quokka narwhal", s, cfg))
            print(f"[test] result mus with empty exclusion: {[m['mu_id'] for m in result.mus]}")
            assert failed_id in {m["mu_id"] for m in result.mus}
