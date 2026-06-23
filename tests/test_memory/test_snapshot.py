"""
Tests for memory/snapshot.py — regular, battery, and pre-pruning snapshot functions.
"""

import asyncio
import gzip
import json
import os
import sys
from pathlib import Path
from unittest.mock import AsyncMock, patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "backend"))

from memory.snapshot import take_battery_snapshot, take_pre_pruning_snapshot, take_snapshot


def run(coro):
    return asyncio.get_event_loop().run_until_complete(coro)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _mock_store(mus=None, edges=None):
    """Return a mock GraphStore with configurable get_all_mus / get_all_edges."""
    mock = AsyncMock()
    mock.get_all_mus = AsyncMock(return_value=mus or [])
    mock.get_all_edges = AsyncMock(return_value=edges or [])
    return mock


def _read_gz(path: str) -> dict:
    with gzip.open(path, "rt", encoding="utf-8") as f:
        return json.load(f)


# ---------------------------------------------------------------------------
# take_snapshot tests
# ---------------------------------------------------------------------------

def test_take_snapshot_writes_file(tmp_path):
    """take_snapshot() writes a gzip-JSON file to the snapshots directory."""
    mock_store = _mock_store(
        mus=[{"mu_id": "mu-001", "raw_content": "hello"}],
        edges=[{"edge_id": "e-001", "weight": 0.5}],
    )
    snapshots_dir = tmp_path / "snapshots"

    with patch("memory.snapshot.graph_store", mock_store):
        with patch("memory.snapshot._SNAPSHOTS_DIR", snapshots_dir):
            result_path = run(take_snapshot())

    assert Path(result_path).exists()
    data = _read_gz(result_path)
    assert data["mu_count"] == 1
    assert data["edge_count"] == 1
    assert len(data["memory_units"]) == 1
    print(f"[PASS] take_snapshot wrote {Path(result_path).name}")


# ---------------------------------------------------------------------------
# take_battery_snapshot tests
# ---------------------------------------------------------------------------

def test_take_battery_snapshot_writes_correct_filename(tmp_path):
    """take_battery_snapshot() writes a file with the battery naming convention."""
    mock_store = _mock_store()
    snapshots_dir = tmp_path / "snapshots"

    with patch("memory.snapshot.graph_store", mock_store):
        with patch("memory.snapshot._SNAPSHOTS_DIR", snapshots_dir):
            result_path = run(take_battery_snapshot(
                battery_index=3,
                phase="pre",
                condition_label="learning",
            ))

    assert Path(result_path).exists()
    filename = Path(result_path).name
    assert filename.startswith("battery_3_pre_learning_")
    assert filename.endswith(".json.gz")
    print(f"[PASS] battery snapshot filename: {filename}")


def test_take_battery_snapshot_post_phase(tmp_path):
    """take_battery_snapshot() correctly names post-battery snapshots."""
    mock_store = _mock_store()
    snapshots_dir = tmp_path / "snapshots"

    with patch("memory.snapshot.graph_store", mock_store):
        with patch("memory.snapshot._SNAPSHOTS_DIR", snapshots_dir):
            result_path = run(take_battery_snapshot(
                battery_index=1,
                phase="post",
                condition_label="static-weight",
            ))

    filename = Path(result_path).name
    assert filename.startswith("battery_1_post_static-weight_")
    print(f"[PASS] post-battery snapshot filename: {filename}")


def test_take_battery_snapshot_payload_has_metadata(tmp_path):
    """take_battery_snapshot() payload includes battery_index, phase, condition_label."""
    mock_store = _mock_store(
        mus=[{"mu_id": "mu-1"}],
        edges=[],
    )
    snapshots_dir = tmp_path / "snapshots"

    with patch("memory.snapshot.graph_store", mock_store):
        with patch("memory.snapshot._SNAPSHOTS_DIR", snapshots_dir):
            result_path = run(take_battery_snapshot(
                battery_index=2,
                phase="pre",
                condition_label="random-weight",
            ))

    data = _read_gz(result_path)
    assert data["snapshot_type"] == "battery"
    assert data["battery_index"] == 2
    assert data["phase"] == "pre"
    assert data["condition_label"] == "random-weight"
    assert data["mu_count"] == 1
    print(f"[PASS] battery snapshot payload has correct metadata")


# ---------------------------------------------------------------------------
# take_pre_pruning_snapshot tests
# ---------------------------------------------------------------------------

def test_take_pre_pruning_snapshot_writes_correct_path(tmp_path):
    """take_pre_pruning_snapshot() writes to the correct pre_prune_{ts}.json.gz path."""
    mock_store = _mock_store()
    snapshots_dir = tmp_path / "snapshots"

    with patch("memory.snapshot.graph_store", mock_store):
        with patch("memory.snapshot._SNAPSHOTS_DIR", snapshots_dir):
            result_path = run(take_pre_pruning_snapshot())

    assert Path(result_path).exists()
    filename = Path(result_path).name
    assert filename.startswith("pre_prune_")
    assert filename.endswith(".json.gz")
    print(f"[PASS] pre-pruning snapshot filename: {filename}")


def test_take_pre_pruning_snapshot_payload(tmp_path):
    """take_pre_pruning_snapshot() payload includes snapshot_type = pre_prune."""
    mock_store = _mock_store(
        mus=[{"mu_id": "mu-1"}, {"mu_id": "mu-2"}],
        edges=[{"edge_id": "e-1"}],
    )
    snapshots_dir = tmp_path / "snapshots"

    with patch("memory.snapshot.graph_store", mock_store):
        with patch("memory.snapshot._SNAPSHOTS_DIR", snapshots_dir):
            result_path = run(take_pre_pruning_snapshot())

    data = _read_gz(result_path)
    assert data["snapshot_type"] == "pre_prune"
    assert data["mu_count"] == 2
    assert data["edge_count"] == 1
    print(f"[PASS] pre-pruning snapshot has snapshot_type=pre_prune mu_count=2 edge_count=1")
