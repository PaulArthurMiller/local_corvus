"""
Tests for Milestone 46: Probe Traversal Instrumentation.

POST /admin/run-probe must persist the traversal_path/hop_depth_per_
retrieved_mu that retrieve() has always returned on its RetrievalResult but
that probe measurement never stored — the gap BUILD-PLAN.md's Milestone 46
section describes ("Probe measurement records *what was retrieved* and
nothing about *how*"). Nothing new is computed here; this only checks the
already-returned fields reach the recorded ProbeResult, mirroring the
existing test_run_probe_dual_mode.py convention (call the route handler
function directly, with load_probe_set/retrieve/record_probe_result
patched).
"""

import asyncio
import os
import sys
from unittest.mock import AsyncMock, patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "backend"))

from analytics.probes import ProbeDefinition
from api.admin import RunProbeRequest, run_probe
from memory.retrieval import RetrievalResult


def run(coro):
    return asyncio.get_event_loop().run_until_complete(coro)


def _probe():
    return ProbeDefinition(
        probe_id="p06",
        phenomenon_type="ranking_improvement",
        prompt_text="test prompt",
        gold_mu_ids=["mu-gold"],
        is_bridge_probe=False,
        probe_set_version="v2",
    )


def _retrieval_result_with_traversal():
    """A RetrievalResult where the gold MU is reached via one K2 hop from a
    K1 entry point — the case the pre-M46 endpoint silently dropped."""
    traversal_path = [
        {"source_mu_id": "mu-k1", "edge_id": "edge-1", "target_mu_id": "mu-gold", "hop_depth": 1},
    ]
    hop_depth = {"mu-k1": 0, "mu-gold": 1}
    return RetrievalResult(
        mus=[
            {"mu_id": "mu-k1", "raw_content": "content"},
            {"mu_id": "mu-gold", "raw_content": "content"},
        ],
        keywords=["test"],
        activated_mu_ids=["mu-k1", "mu-gold"],
        activated_edge_ids=["edge-1"],
        bm25_scores_per_mu={"mu-k1": 1.0},
        traversal_path=traversal_path,
        hop_depth_per_retrieved_mu=hop_depth,
    )


def _patched_run(body, retrieve_mock=None, record_mock=None):
    """Run run_probe(body) with load_probe_set/retrieve/record_probe_result
    patched to fakes, returning (result_dict, retrieve_mock, record_mock)."""
    retrieve_mock = retrieve_mock or AsyncMock(return_value=_retrieval_result_with_traversal())
    record_mock = record_mock or AsyncMock()
    with patch("api.admin.load_probe_set", return_value=[_probe()]), \
         patch("api.admin.retrieve", retrieve_mock), \
         patch("api.admin.record_probe_result", record_mock):
        result = run(run_probe(body))
    return result, retrieve_mock, record_mock


def test_run_probe_response_includes_traversal_path_and_hop_depth():
    """The response body (ProbeResult.model_dump()) carries traversal_path
    and hop_depth_per_retrieved_mu straight from the RetrievalResult."""
    body = RunProbeRequest(
        probe_id="p06", measurement_session_index=1, condition_label="learned",
        probe_set_version="v2",
    )
    result, _, _ = _patched_run(body)

    assert result["traversal_path"] == [
        {"source_mu_id": "mu-k1", "edge_id": "edge-1", "target_mu_id": "mu-gold", "hop_depth": 1},
    ]
    assert result["hop_depth_per_retrieved_mu"] == {"mu-k1": 0, "mu-gold": 1}
    print("[PASS] run_probe response includes traversal_path/hop_depth_per_retrieved_mu")


def test_run_probe_persists_traversal_path_and_hop_depth():
    """record_probe_result() — the function that writes to probe_results —
    is called with a ProbeResult whose traversal_path/hop_depth_per_
    retrieved_mu are populated, not left at the empty defaults."""
    body = RunProbeRequest(
        probe_id="p06", measurement_session_index=1, condition_label="learned",
        probe_set_version="v2",
    )
    record_mock = AsyncMock()
    _patched_run(body, record_mock=record_mock)

    record_mock.assert_called_once()
    (recorded_result,), _ = record_mock.call_args
    assert recorded_result.traversal_path == [
        {"source_mu_id": "mu-k1", "edge_id": "edge-1", "target_mu_id": "mu-gold", "hop_depth": 1},
    ]
    assert recorded_result.hop_depth_per_retrieved_mu == {"mu-k1": 0, "mu-gold": 1}
    print("[PASS] run_probe passes populated traversal_path/hop_depth_per_retrieved_mu to record_probe_result")


def test_run_probe_traversal_fields_empty_when_no_k2_hop_taken():
    """A probe whose gold MU is reached at K1 alone (no traversal) records
    empty traversal_path/hop_depth_per_retrieved_mu — not an error, and not
    silently omitted from the response."""
    body = RunProbeRequest(
        probe_id="p06", measurement_session_index=1, condition_label="learned",
        probe_set_version="v2",
    )
    retrieve_mock = AsyncMock(return_value=RetrievalResult(
        mus=[{"mu_id": "mu-gold", "raw_content": "content"}],
        keywords=["test"],
        activated_mu_ids=["mu-gold"],
        activated_edge_ids=[],
        bm25_scores_per_mu={"mu-gold": 1.0},
    ))
    result, _, _ = _patched_run(body, retrieve_mock=retrieve_mock)

    assert result["traversal_path"] == []
    assert result["hop_depth_per_retrieved_mu"] == {}
    print("[PASS] run_probe records empty traversal_path/hop_depth_per_retrieved_mu when no K2 hop was taken")
