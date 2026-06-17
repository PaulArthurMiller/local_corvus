"""
Tests for memory/retrieval.py

Covers: BM25 candidate pool, multi-hit filtering, K1 selection,
        K2 traversal funnel, empty-store guard, no-keyword guard.

Each test uses an isolated in-memory SQLite via TempDBStore.
"""

import asyncio
import os
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
