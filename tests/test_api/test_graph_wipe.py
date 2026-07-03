"""
Tests for Milestone 19 Phase 0: POST /admin/graph/wipe.

Calls the route handler function directly (matching the existing convention
in tests/test_api/test_run_probe_versioned.py) rather than spinning up a
FastAPI TestClient.
"""

import asyncio
import os
import sys
from unittest.mock import AsyncMock, patch

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "backend"))

from api.admin import WipeGraphRequest, wipe_graph
from fastapi import HTTPException


def run(coro):
    return asyncio.get_event_loop().run_until_complete(coro)


def test_wipe_graph_archives_then_wipes_in_order():
    """The archive snapshot must be taken before the graph store is wiped."""
    call_order = []

    async def fake_archive(experiment_id):
        call_order.append(("archive", experiment_id))
        return "/app/data/snapshots/archive_exp-1_20260701T000000Z.json.gz"

    async def fake_wipe():
        call_order.append(("wipe", None))
        return {"mus_deleted": 34, "edges_deleted": 32}

    with patch("api.admin.take_archive_snapshot", side_effect=fake_archive), \
         patch("api.admin.graph_store.wipe_graph", side_effect=fake_wipe):
        result = run(wipe_graph(WipeGraphRequest(experiment_id="exp-1")))

    assert call_order == [("archive", "exp-1"), ("wipe", None)]
    assert result["mus_deleted"] == 34
    assert result["edges_deleted"] == 32
    assert result["archive_path"].endswith(".json.gz")
    print(f"[PASS] wipe_graph result: {result}")


def test_wipe_graph_defaults_experiment_id_to_none():
    async def fake_archive(experiment_id):
        assert experiment_id is None
        return "/app/data/snapshots/archive_unlabeled_20260701T000000Z.json.gz"

    async def fake_wipe():
        return {"mus_deleted": 0, "edges_deleted": 0}

    with patch("api.admin.take_archive_snapshot", side_effect=fake_archive), \
         patch("api.admin.graph_store.wipe_graph", side_effect=fake_wipe):
        result = run(wipe_graph(WipeGraphRequest()))

    assert result["mus_deleted"] == 0
    print("[PASS] wipe_graph defaults experiment_id to None")


def test_wipe_graph_aborts_wipe_if_archive_fails():
    """If archiving fails, the graph must NOT be wiped — data loss must never
    happen silently."""
    async def failing_archive(experiment_id):
        raise IOError("disk full")

    wipe_mock = AsyncMock()

    with patch("api.admin.take_archive_snapshot", side_effect=failing_archive), \
         patch("api.admin.graph_store.wipe_graph", wipe_mock):
        with pytest.raises(HTTPException) as exc_info:
            run(wipe_graph(WipeGraphRequest(experiment_id="exp-1")))

    assert exc_info.value.status_code == 500
    assert "Archive failed" in exc_info.value.detail
    wipe_mock.assert_not_called()
    print("[PASS] wipe aborted (not called) when archive fails")


def test_wipe_graph_raises_500_when_store_wipe_fails():
    async def fake_archive(experiment_id):
        return "/app/data/snapshots/archive_exp-1.json.gz"

    async def failing_wipe():
        raise RuntimeError("db locked")

    with patch("api.admin.take_archive_snapshot", side_effect=fake_archive), \
         patch("api.admin.graph_store.wipe_graph", side_effect=failing_wipe):
        with pytest.raises(HTTPException) as exc_info:
            run(wipe_graph(WipeGraphRequest(experiment_id="exp-1")))

    assert exc_info.value.status_code == 500
    print("[PASS] wipe_graph raises 500 when store.wipe_graph() fails")
