"""
Tests for analytics/probes.py — probe loading, metric computation, and recording.
"""

import asyncio
import os
import sys
from pathlib import Path
from unittest.mock import AsyncMock, patch

import yaml

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "backend"))

from analytics.probes import (
    ProbeDefinition,
    ProbeResult,
    compute_probe_result,
    load_probe_set,
    record_probe_result,
)
from memory.retrieval import RetrievalResult


def run(coro):
    return asyncio.get_event_loop().run_until_complete(coro)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_probe_yaml(tmp_path: Path, probes: list, version: str = "v1") -> Path:
    """Write a minimal probe_set YAML file to a temp directory."""
    data = {
        "version": version,
        "phenomenon_map": {
            "ranking_improvement": {"description": "test"},
        },
        "probes": probes,
    }
    probe_file = tmp_path / f"probe_set_{version}.yaml"
    with open(probe_file, "w") as f:
        yaml.dump(data, f)
    return tmp_path


def _make_retrieval_result(
    activated_mu_ids: list,
    bm25_scores: dict = None,
    traversal_path: list = None,
    hop_depth_per_retrieved_mu: dict = None,
) -> RetrievalResult:
    """Build a minimal RetrievalResult with the given activated MU IDs."""
    return RetrievalResult(
        mus=[{"mu_id": mid, "raw_content": f"content of {mid}"} for mid in activated_mu_ids],
        keywords=["test"],
        activated_mu_ids=activated_mu_ids,
        activated_edge_ids=[],
        bm25_scores_per_mu=bm25_scores or {mid: 1.0 for mid in activated_mu_ids},
        traversal_path=traversal_path if traversal_path is not None else [],
        hop_depth_per_retrieved_mu=(
            hop_depth_per_retrieved_mu if hop_depth_per_retrieved_mu is not None else {}
        ),
    )


# ---------------------------------------------------------------------------
# load_probe_set tests
# ---------------------------------------------------------------------------

def test_load_probe_set_returns_probe_definitions(tmp_path):
    """load_probe_set() parses YAML and returns ProbeDefinition list."""
    probes_data = [
        {
            "probe_id": "p01",
            "phenomenon_type": "ranking_improvement",
            "prompt_text": "What is the boiling point of water?",
            "gold_mu_ids": ["mu-abc", "mu-def"],
            "is_bridge_probe": False,
            "bridge_pair_id": None,
        },
        {
            "probe_id": "p02",
            "phenomenon_type": "cross_cluster_transfer",
            "prompt_text": "Tell me about bridges.",
            "gold_mu_ids": [],
            "is_bridge_probe": True,
            "bridge_pair_id": "p03",
        },
    ]
    probes_dir = _make_probe_yaml(tmp_path, probes_data)

    with patch("analytics.probes._PROBES_PATH", probes_dir):
        result = load_probe_set(version="v1")

    assert len(result) == 2
    assert all(isinstance(p, ProbeDefinition) for p in result)

    p01 = result[0]
    assert p01.probe_id == "p01"
    assert p01.phenomenon_type == "ranking_improvement"
    assert p01.prompt_text == "What is the boiling point of water?"
    assert p01.gold_mu_ids == ["mu-abc", "mu-def"]
    assert p01.is_bridge_probe is False
    assert p01.probe_set_version == "v1"

    p02 = result[1]
    assert p02.is_bridge_probe is True
    assert p02.bridge_pair_id == "p03"

    print(f"[PASS] load_probe_set returned {len(result)} ProbeDefinition(s)")


def test_load_probe_set_handles_empty_gold_mu_ids(tmp_path):
    """load_probe_set() handles null/missing gold_mu_ids gracefully."""
    probes_data = [
        {
            "probe_id": "p01",
            "phenomenon_type": "ranking_improvement",
            "prompt_text": "empty gold",
            "gold_mu_ids": None,
            "is_bridge_probe": False,
            "bridge_pair_id": None,
        },
    ]
    probes_dir = _make_probe_yaml(tmp_path, probes_data)

    with patch("analytics.probes._PROBES_PATH", probes_dir):
        result = load_probe_set(version="v1")

    assert result[0].gold_mu_ids == []
    print("[PASS] load_probe_set handles None gold_mu_ids as empty list")


def test_load_probe_set_bridge_probe_filter(tmp_path):
    """ProbeDefinition.is_bridge_probe correctly distinguishes bridge vs non-bridge probes."""
    probes_data = [
        {"probe_id": "p01", "phenomenon_type": "ranking_improvement", "prompt_text": "a",
         "gold_mu_ids": [], "is_bridge_probe": False, "bridge_pair_id": None},
        {"probe_id": "p02", "phenomenon_type": "cross_cluster_transfer", "prompt_text": "b",
         "gold_mu_ids": [], "is_bridge_probe": True, "bridge_pair_id": "p01"},
    ]
    probes_dir = _make_probe_yaml(tmp_path, probes_data)

    with patch("analytics.probes._PROBES_PATH", probes_dir):
        probes = load_probe_set(version="v1")

    bridge_probes = [p for p in probes if p.is_bridge_probe]
    non_bridge = [p for p in probes if not p.is_bridge_probe]

    assert len(bridge_probes) == 1
    assert bridge_probes[0].probe_id == "p02"
    assert len(non_bridge) == 1
    assert non_bridge[0].probe_id == "p01"
    print("[PASS] is_bridge_probe filter correctly partitions probes")


# ---------------------------------------------------------------------------
# compute_probe_result tests
# ---------------------------------------------------------------------------

def test_compute_probe_result_gold_mu_at_rank_1():
    """compute_probe_result() returns gold_mu_rank=1 when gold MU is first in retrieved list."""
    probe = ProbeDefinition(
        probe_id="p01",
        phenomenon_type="ranking_improvement",
        prompt_text="test prompt",
        gold_mu_ids=["mu-gold"],
        is_bridge_probe=False,
        probe_set_version="v1",
    )
    retrieval_result = _make_retrieval_result(["mu-gold", "mu-other", "mu-third"])

    result = run(compute_probe_result(
        probe=probe,
        retrieval_result=retrieval_result,
        run_id=None,
        measurement_session_index=1,
        condition_label="learning",
    ))

    assert result.gold_mu_rank == 1
    assert abs(result.retrieval_precision - 1 / 3) < 1e-6
    assert abs(result.retrieval_recall - 1.0) < 1e-6
    assert result.probe_id == "p01"
    assert result.measurement_session_index == 1
    assert result.condition_label == "learning"
    print(f"[PASS] gold_mu_rank=1 precision={result.retrieval_precision:.3f} recall={result.retrieval_recall:.3f}")


def test_compute_probe_result_gold_mu_absent():
    """compute_probe_result() returns gold_mu_rank=None when gold MU not in retrieved set."""
    probe = ProbeDefinition(
        probe_id="p01",
        phenomenon_type="ranking_improvement",
        prompt_text="test prompt",
        gold_mu_ids=["mu-gold"],
        is_bridge_probe=False,
        probe_set_version="v1",
    )
    retrieval_result = _make_retrieval_result(["mu-other", "mu-third"])

    result = run(compute_probe_result(
        probe=probe,
        retrieval_result=retrieval_result,
        run_id=None,
        measurement_session_index=1,
        condition_label="learning",
    ))

    assert result.gold_mu_rank is None
    assert abs(result.retrieval_precision - 0.0) < 1e-9
    assert abs(result.retrieval_recall - 0.0) < 1e-9
    print("[PASS] gold_mu_rank=None when gold MU absent from retrieved set")


def test_compute_probe_result_gold_mu_at_rank_3():
    """compute_probe_result() returns correct rank when gold MU is not first."""
    probe = ProbeDefinition(
        probe_id="p01",
        phenomenon_type="ranking_improvement",
        prompt_text="test prompt",
        gold_mu_ids=["mu-gold"],
        is_bridge_probe=False,
        probe_set_version="v1",
    )
    retrieval_result = _make_retrieval_result(["mu-a", "mu-b", "mu-gold", "mu-c"])

    result = run(compute_probe_result(
        probe=probe,
        retrieval_result=retrieval_result,
        run_id=None,
        measurement_session_index=2,
        condition_label="static-weight",
    ))

    assert result.gold_mu_rank == 3
    assert abs(result.retrieval_precision - 1 / 4) < 1e-6
    assert abs(result.retrieval_recall - 1.0) < 1e-6
    print(f"[PASS] gold_mu_rank=3 (gold is third in list of 4)")


def test_compute_probe_result_multiple_gold_mus():
    """compute_probe_result() computes precision/recall correctly with multiple gold MUs."""
    probe = ProbeDefinition(
        probe_id="p01",
        phenomenon_type="ranking_improvement",
        prompt_text="test prompt",
        gold_mu_ids=["mu-gold-1", "mu-gold-2"],
        is_bridge_probe=False,
        probe_set_version="v1",
    )
    # Retrieved: gold-1 at rank 1, gold-2 not retrieved; one non-gold retrieved
    retrieval_result = _make_retrieval_result(["mu-gold-1", "mu-other"])

    result = run(compute_probe_result(
        probe=probe,
        retrieval_result=retrieval_result,
        run_id=None,
        measurement_session_index=1,
        condition_label="learning",
    ))

    assert result.gold_mu_rank == 1
    assert abs(result.retrieval_precision - 0.5) < 1e-6   # 1 of 2 retrieved is gold
    assert abs(result.retrieval_recall - 0.5) < 1e-6      # 1 of 2 gold was retrieved
    print(f"[PASS] multiple gold MUs: precision={result.retrieval_precision:.3f} recall={result.retrieval_recall:.3f}")


def test_compute_probe_result_empty_retrieved_set():
    """compute_probe_result() returns None precision when no MUs retrieved."""
    probe = ProbeDefinition(
        probe_id="p01",
        phenomenon_type="ranking_improvement",
        prompt_text="test prompt",
        gold_mu_ids=["mu-gold"],
        is_bridge_probe=False,
        probe_set_version="v1",
    )
    retrieval_result = _make_retrieval_result([])

    result = run(compute_probe_result(
        probe=probe,
        retrieval_result=retrieval_result,
        run_id=None,
        measurement_session_index=1,
        condition_label="learning",
    ))

    assert result.gold_mu_rank is None
    assert result.retrieval_precision is None
    assert abs(result.retrieval_recall - 0.0) < 1e-9
    print("[PASS] empty retrieved set returns gold_mu_rank=None precision=None")


def test_compute_probe_result_bm25_scores_passed_through():
    """compute_probe_result() includes bm25_scores_per_mu from the retrieval result."""
    probe = ProbeDefinition(
        probe_id="p01",
        phenomenon_type="ranking_improvement",
        prompt_text="test",
        gold_mu_ids=["mu-gold"],
        is_bridge_probe=False,
        probe_set_version="v1",
    )
    bm25 = {"mu-gold": 2.5, "mu-other": 1.1}
    retrieval_result = _make_retrieval_result(["mu-gold"], bm25_scores=bm25)

    result = run(compute_probe_result(
        probe=probe,
        retrieval_result=retrieval_result,
        run_id=None,
        measurement_session_index=1,
        condition_label="learning",
    ))

    assert result.bm25_scores_per_mu == bm25
    print(f"[PASS] bm25_scores_per_mu passed through: {result.bm25_scores_per_mu}")


# ---------------------------------------------------------------------------
# record_probe_result test
# ---------------------------------------------------------------------------

def test_record_probe_result_calls_store():
    """record_probe_result() calls graph_store.insert_probe_result with the correct data."""
    probe_result = ProbeResult(
        probe_id="p01",
        run_id=None,
        measurement_session_index=1,
        condition_label="learning",
        retrieved_mu_ids=["mu-a"],
        gold_mu_rank=1,
        retrieval_precision=1.0,
        retrieval_recall=1.0,
    )

    with patch("analytics.probes.graph_store") as mock_store:
        mock_store.insert_probe_result = AsyncMock()
        run(record_probe_result(probe_result))
        mock_store.insert_probe_result.assert_called_once()
        call_arg = mock_store.insert_probe_result.call_args[0][0]
        assert call_arg["probe_id"] == "p01"
        assert call_arg["gold_mu_rank"] == 1

    print("[PASS] record_probe_result calls graph_store.insert_probe_result with correct data")


# ---------------------------------------------------------------------------
# M21 — experiment_id stamping on probe results
# ---------------------------------------------------------------------------

def test_compute_probe_result_stamps_experiment_id():
    """compute_probe_result() carries the experiment_id through to the ProbeResult
    so reports can scope probe measurements to one (plan, attempt, condition) run."""
    probe = ProbeDefinition(
        probe_id="p01",
        phenomenon_type="ranking_improvement",
        prompt_text="test prompt",
        gold_mu_ids=["mu-gold"],
        is_bridge_probe=False,
        probe_set_version="v1",
    )
    retrieval_result = _make_retrieval_result(["mu-gold"])

    result = run(compute_probe_result(
        probe=probe,
        retrieval_result=retrieval_result,
        run_id=None,
        measurement_session_index=1,
        condition_label="learned",
        experiment_id="plan1_learned",
    ))

    assert result.experiment_id == "plan1_learned"
    assert result.model_dump()["experiment_id"] == "plan1_learned"
    print("[PASS] compute_probe_result stamps experiment_id into the ProbeResult")


def test_compute_probe_result_experiment_id_defaults_to_none():
    """Callers that predate M21 (no experiment_id argument) still work — the field
    defaults to None, matching pre-M21 rows in the probe_results table."""
    probe = ProbeDefinition(
        probe_id="p01",
        phenomenon_type="ranking_improvement",
        prompt_text="test prompt",
        gold_mu_ids=["mu-gold"],
        is_bridge_probe=False,
        probe_set_version="v1",
    )
    retrieval_result = _make_retrieval_result(["mu-gold"])

    result = run(compute_probe_result(
        probe=probe,
        retrieval_result=retrieval_result,
        run_id=None,
        measurement_session_index=1,
        condition_label="learning",
    ))

    assert result.experiment_id is None
    print("[PASS] experiment_id defaults to None for legacy callers")


# ---------------------------------------------------------------------------
# M30 — dual-mode probe battery: series_id/battery_mode/battery_repeat_index
# ---------------------------------------------------------------------------

def test_compute_probe_result_stamps_series_id_battery_mode_and_repeat_index():
    """compute_probe_result() carries series_id/battery_mode/battery_repeat_index
    through to the ProbeResult so series_report.py can scope + separate the
    dual-mode battery's mean vs. sample rows."""
    probe = ProbeDefinition(
        probe_id="p01",
        phenomenon_type="ranking_improvement",
        prompt_text="test prompt",
        gold_mu_ids=["mu-gold"],
        is_bridge_probe=False,
        probe_set_version="v1",
    )
    retrieval_result = _make_retrieval_result(["mu-gold"])

    result = run(compute_probe_result(
        probe=probe,
        retrieval_result=retrieval_result,
        run_id=None,
        measurement_session_index=1,
        condition_label="learned",
        series_id="run1_learned_s0",
        battery_mode="sample",
        battery_repeat_index=7,
    ))

    assert result.series_id == "run1_learned_s0"
    assert result.battery_mode == "sample"
    assert result.battery_repeat_index == 7
    print("[PASS] compute_probe_result stamps series_id/battery_mode/battery_repeat_index")


def test_compute_probe_result_battery_mode_defaults_to_mean():
    """Callers that predate M30 (no battery_mode argument) still work — defaults
    to 'mean', matching a single-pass measurement with no dual-mode context."""
    probe = ProbeDefinition(
        probe_id="p01",
        phenomenon_type="ranking_improvement",
        prompt_text="test prompt",
        gold_mu_ids=["mu-gold"],
        is_bridge_probe=False,
        probe_set_version="v1",
    )
    retrieval_result = _make_retrieval_result(["mu-gold"])

    result = run(compute_probe_result(
        probe=probe,
        retrieval_result=retrieval_result,
        run_id=None,
        measurement_session_index=1,
        condition_label="learning",
    ))

    assert result.battery_mode == "mean"
    assert result.battery_repeat_index is None
    assert result.series_id is None
    print("[PASS] battery_mode defaults to 'mean', series_id/battery_repeat_index default to None")


def test_probe_result_rejects_invalid_battery_mode():
    """ProbeResult's battery_mode field_validator rejects anything outside
    VALID_BATTERY_MODES ('mean', 'sample')."""
    import pytest

    with pytest.raises(ValueError):
        ProbeResult(
            probe_id="p01",
            measurement_session_index=1,
            condition_label="learning",
            battery_mode="bogus",
        )
    print("[PASS] ProbeResult rejects battery_mode='bogus'")


def test_record_probe_result_includes_m30_fields():
    """record_probe_result() passes series_id/battery_mode/battery_repeat_index
    through to graph_store.insert_probe_result."""
    probe_result = ProbeResult(
        probe_id="p01",
        run_id=None,
        measurement_session_index=1,
        condition_label="learning",
        retrieved_mu_ids=["mu-a"],
        gold_mu_rank=1,
        retrieval_precision=1.0,
        retrieval_recall=1.0,
        series_id="run1_learned_s0",
        battery_mode="sample",
        battery_repeat_index=3,
    )

    with patch("analytics.probes.graph_store") as mock_store:
        mock_store.insert_probe_result = AsyncMock()
        run(record_probe_result(probe_result))
        call_arg = mock_store.insert_probe_result.call_args[0][0]
        assert call_arg["series_id"] == "run1_learned_s0"
        assert call_arg["battery_mode"] == "sample"
        assert call_arg["battery_repeat_index"] == 3

    print("[PASS] record_probe_result includes M30 dual-mode battery fields")


# ---------------------------------------------------------------------------
# M46 — probe traversal instrumentation: traversal_path / hop_depth_per_retrieved_mu
# ---------------------------------------------------------------------------

def test_compute_probe_result_carries_traversal_path_and_hop_depth():
    """compute_probe_result() carries traversal_path/hop_depth_per_retrieved_mu
    straight through from the RetrievalResult — nothing new computed, only
    stored, per BUILD-PLAN.md's Milestone 46 scope."""
    probe = ProbeDefinition(
        probe_id="p01",
        phenomenon_type="ranking_improvement",
        prompt_text="test prompt",
        gold_mu_ids=["mu-gold"],
        is_bridge_probe=False,
        probe_set_version="v1",
    )
    traversal_path = [
        {"source_mu_id": "mu-k1", "edge_id": "edge-1", "target_mu_id": "mu-gold", "hop_depth": 1},
    ]
    hop_depth = {"mu-k1": 0, "mu-gold": 1}
    retrieval_result = _make_retrieval_result(
        ["mu-k1", "mu-gold"],
        traversal_path=traversal_path,
        hop_depth_per_retrieved_mu=hop_depth,
    )

    result = run(compute_probe_result(
        probe=probe,
        retrieval_result=retrieval_result,
        run_id=None,
        measurement_session_index=1,
        condition_label="learning",
    ))

    assert result.traversal_path == traversal_path
    assert result.hop_depth_per_retrieved_mu == hop_depth
    print("[PASS] compute_probe_result carries traversal_path/hop_depth_per_retrieved_mu through")


def test_compute_probe_result_traversal_fields_default_empty():
    """A RetrievalResult with no traversal (e.g. gold reached at K1, no K2 hop
    ever taken) yields empty traversal_path/hop_depth_per_retrieved_mu, not
    an error — matches RetrievalResult's own defaults."""
    probe = ProbeDefinition(
        probe_id="p01",
        phenomenon_type="ranking_improvement",
        prompt_text="test prompt",
        gold_mu_ids=["mu-gold"],
        is_bridge_probe=False,
        probe_set_version="v1",
    )
    retrieval_result = _make_retrieval_result(["mu-gold"])

    result = run(compute_probe_result(
        probe=probe,
        retrieval_result=retrieval_result,
        run_id=None,
        measurement_session_index=1,
        condition_label="learning",
    ))

    assert result.traversal_path == []
    assert result.hop_depth_per_retrieved_mu == {}
    print("[PASS] traversal_path/hop_depth_per_retrieved_mu default to empty when the RetrievalResult carries none")


def test_record_probe_result_includes_m46_fields():
    """record_probe_result() passes traversal_path/hop_depth_per_retrieved_mu
    through to graph_store.insert_probe_result."""
    traversal_path = [
        {"source_mu_id": "mu-a", "edge_id": "edge-1", "target_mu_id": "mu-b", "hop_depth": 1},
    ]
    hop_depth = {"mu-a": 0, "mu-b": 1}
    probe_result = ProbeResult(
        probe_id="p01",
        run_id=None,
        measurement_session_index=1,
        condition_label="learning",
        retrieved_mu_ids=["mu-a", "mu-b"],
        gold_mu_rank=2,
        retrieval_precision=1.0,
        retrieval_recall=1.0,
        traversal_path=traversal_path,
        hop_depth_per_retrieved_mu=hop_depth,
    )

    with patch("analytics.probes.graph_store") as mock_store:
        mock_store.insert_probe_result = AsyncMock()
        run(record_probe_result(probe_result))
        call_arg = mock_store.insert_probe_result.call_args[0][0]
        assert call_arg["traversal_path"] == traversal_path
        assert call_arg["hop_depth_per_retrieved_mu"] == hop_depth

    print("[PASS] record_probe_result includes M46 traversal instrumentation fields")
