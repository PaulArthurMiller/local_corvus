"""
Tests for Milestone 30: dual-mode probe battery support on POST /admin/run-probe.

Calls the route handler function directly (matching the existing convention
in tests/test_api/test_run_probe_versioned.py / test_graph_wipe.py) rather
than spinning up a FastAPI TestClient.
"""

import asyncio
import os
import sys
from unittest.mock import AsyncMock, patch

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "backend"))

from analytics.probes import ProbeDefinition, ProbeResult
from api.admin import RunProbeRequest, run_probe
from fastapi import HTTPException
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


def _retrieval_result():
    return RetrievalResult(
        mus=[{"mu_id": "mu-gold", "raw_content": "content"}],
        keywords=["test"],
        activated_mu_ids=["mu-gold"],
        activated_edge_ids=[],
        bm25_scores_per_mu={"mu-gold": 1.0},
    )


def _patched_run(body, retrieve_mock=None):
    """Run run_probe(body) with load_probe_set/retrieve/record_probe_result
    patched to fakes, returning (result_dict, retrieve_mock)."""
    retrieve_mock = retrieve_mock or AsyncMock(return_value=_retrieval_result())
    with patch("api.admin.load_probe_set", return_value=[_probe()]), \
         patch("api.admin.retrieve", retrieve_mock), \
         patch("api.admin.record_probe_result", AsyncMock()):
        result = run(run_probe(body))
    return result, retrieve_mock


# ---------------------------------------------------------------------------
# Request validation
# ---------------------------------------------------------------------------

def test_run_probe_rejects_invalid_weight_mode():
    body = RunProbeRequest(
        probe_id="p06", measurement_session_index=1, condition_label="learned",
        probe_set_version="v2", weight_mode="bogus",
    )
    with pytest.raises(HTTPException) as exc_info:
        run(run_probe(body))
    assert exc_info.value.status_code == 422
    assert "weight_mode" in exc_info.value.detail
    print("[PASS] run_probe rejects weight_mode='bogus' with 422")


def test_run_probe_rejects_invalid_battery_mode():
    body = RunProbeRequest(
        probe_id="p06", measurement_session_index=1, condition_label="learned",
        probe_set_version="v2", battery_mode="bogus",
    )
    with pytest.raises(HTTPException) as exc_info:
        run(run_probe(body))
    assert exc_info.value.status_code == 422
    assert "battery_mode" in exc_info.value.detail
    print("[PASS] run_probe rejects battery_mode='bogus' with 422")


def test_run_probe_weight_mode_none_is_valid():
    """weight_mode omitted (None) is the pre-M30 default — must not 422."""
    body = RunProbeRequest(
        probe_id="p06", measurement_session_index=1, condition_label="learned",
        probe_set_version="v2",
    )
    result, _ = _patched_run(body)
    assert result["probe_id"] == "p06"
    print("[PASS] run_probe accepts weight_mode=None (omitted)")


# ---------------------------------------------------------------------------
# M30 finding: probe measurement must not share EDGE_RNG with real turns
# ---------------------------------------------------------------------------

def test_run_probe_does_not_pass_orchestrator_edge_rng():
    """retrieve() must be called with no rng override from run_probe() — a
    probe measurement must never consume draws from the same RNG stream
    real training turns use for weight_mode: sample."""
    body = RunProbeRequest(
        probe_id="p06", measurement_session_index=1, condition_label="learned",
        probe_set_version="v2", weight_mode="sample",
    )
    retrieve_mock = AsyncMock(return_value=_retrieval_result())
    _patched_run(body, retrieve_mock=retrieve_mock)

    _, kwargs = retrieve_mock.call_args
    assert "rng" not in kwargs
    print("[PASS] run_probe never passes an rng= override to retrieve() (decoupled from EDGE_RNG)")


def test_run_probe_threads_weight_mode_override_into_retrieve():
    body = RunProbeRequest(
        probe_id="p06", measurement_session_index=1, condition_label="learned",
        probe_set_version="v2", weight_mode="sample",
    )
    retrieve_mock = AsyncMock(return_value=_retrieval_result())
    _patched_run(body, retrieve_mock=retrieve_mock)

    _, kwargs = retrieve_mock.call_args
    assert kwargs["weight_mode_override"] == "sample"
    print("[PASS] run_probe threads weight_mode='sample' into retrieve() as weight_mode_override")


# ---------------------------------------------------------------------------
# Dual-mode fields threaded into the recorded ProbeResult
# ---------------------------------------------------------------------------

def test_run_probe_threads_series_id_and_battery_fields_into_result():
    body = RunProbeRequest(
        probe_id="p06", measurement_session_index=1, condition_label="learned",
        probe_set_version="v2", series_id="run1_learned_s0",
        weight_mode="sample", battery_mode="sample", battery_repeat_index=4,
    )
    result, _ = _patched_run(body)

    assert result["series_id"] == "run1_learned_s0"
    assert result["battery_mode"] == "sample"
    assert result["battery_repeat_index"] == 4
    print("[PASS] run_probe threads series_id/battery_mode/battery_repeat_index into the ProbeResult")


def test_run_probe_defaults_battery_mode_to_mean():
    body = RunProbeRequest(
        probe_id="p06", measurement_session_index=1, condition_label="learned",
        probe_set_version="v2",
    )
    result, _ = _patched_run(body)

    assert result["battery_mode"] == "mean"
    assert result["battery_repeat_index"] is None
    assert result["series_id"] is None
    print("[PASS] run_probe defaults battery_mode='mean' with no series_id/repeat_index")
