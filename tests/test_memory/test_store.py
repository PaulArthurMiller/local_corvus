"""
Tests for memory/store.py

Covers: create_mu round-trip, create_edge schema, update_edge_weight,
        update_mu_activation, get_edges_for_mu.

Each test creates a fresh in-memory SQLite database by patching _DB_PATH
to a temp file, avoiding cross-test pollution.
"""

import sys
import os
import asyncio
import tempfile
import uuid
from pathlib import Path
from unittest import mock

import pytest

# Add backend to path
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "backend"))

import memory.store as store_module
from memory.store import GraphStore


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def run(coro):
    """Synchronous wrapper for async calls in tests."""
    return asyncio.get_event_loop().run_until_complete(coro)


class TempDBStore:
    """Context manager that gives a GraphStore pointed at a temp file."""

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


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestCreateMu:

    def test_create_mu_round_trips_to_sqlite(self):
        with TempDBStore() as s:
            mid = str(uuid.uuid4())
            run(s.create_mu(mu_id=mid, source_type="user_input", raw_content="hello world"))

            row = run(s.get_mu(mid))
            print(f"[test] fetched MU: {row}")
            assert row is not None
            assert row["mu_id"] == mid
            assert row["source_type"] == "user_input"
            assert row["raw_content"] == "hello world"
            assert row["activation_count"] == 0

    def test_get_mu_returns_none_for_unknown_id(self):
        with TempDBStore() as s:
            row = run(s.get_mu("does-not-exist"))
            print(f"[test] missing MU: {row}")
            assert row is None

    def test_create_mu_default_fields_are_correct(self):
        with TempDBStore() as s:
            mid = str(uuid.uuid4())
            run(s.create_mu(mu_id=mid, source_type="agent_output", raw_content="response"))

            row = run(s.get_mu(mid))
            print(f"[test] default fields: {row}")
            assert row["activation_count"] == 0
            assert row["reference_count"] == 0
            assert row["pruning_flag"] == 0
            assert row["last_activated_at"] is None


class TestCreateEdge:

    def test_create_edge_with_correct_schema(self):
        with TempDBStore() as s:
            mu1 = str(uuid.uuid4())
            mu2 = str(uuid.uuid4())
            run(s.create_mu(mu_id=mu1, source_type="user_input", raw_content="a"))
            run(s.create_mu(mu_id=mu2, source_type="agent_output", raw_content="b"))

            edge = run(s.create_edge(
                source_mu_id=mu1,
                target_mu_id=mu2,
                weight=0.5,
                direction="forward",
            ))
            print(f"[test] created edge: {edge}")

            assert edge["source_mu_id"] == mu1
            assert edge["target_mu_id"] == mu2
            assert edge["weight"] == 0.5
            assert edge["direction"] == "forward"
            assert edge["traversal_count"] == 0

    def test_get_edges_for_mu_returns_connected_edges(self):
        with TempDBStore() as s:
            mu1 = str(uuid.uuid4())
            mu2 = str(uuid.uuid4())
            run(s.create_mu(mu_id=mu1, source_type="user_input", raw_content="a"))
            run(s.create_mu(mu_id=mu2, source_type="agent_output", raw_content="b"))
            run(s.create_edge(source_mu_id=mu1, target_mu_id=mu2, weight=0.5))

            edges_from_mu1 = run(s.get_edges_for_mu(mu1))
            edges_from_mu2 = run(s.get_edges_for_mu(mu2))

            print(f"[test] edges for mu1: {len(edges_from_mu1)}, mu2: {len(edges_from_mu2)}")
            assert len(edges_from_mu1) == 1
            assert len(edges_from_mu2) == 1  # target also matches

    def test_get_edges_for_mu_with_no_edges_returns_empty_list(self):
        with TempDBStore() as s:
            mid = str(uuid.uuid4())
            run(s.create_mu(mu_id=mid, source_type="system", raw_content="x"))

            edges = run(s.get_edges_for_mu(mid))
            print(f"[test] isolated MU edges: {edges}")
            assert edges == []


class TestUpdateEdgeWeight:

    def test_update_edge_weight_changes_only_weight_field(self):
        with TempDBStore() as s:
            mu1 = str(uuid.uuid4())
            mu2 = str(uuid.uuid4())
            run(s.create_mu(mu_id=mu1, source_type="user_input", raw_content="a"))
            run(s.create_mu(mu_id=mu2, source_type="agent_output", raw_content="b"))

            edge = run(s.create_edge(
                source_mu_id=mu1,
                target_mu_id=mu2,
                weight=0.5,
            ))
            eid = edge["edge_id"]

            updated = run(s.update_edge_weight(eid, 0.8))
            print(f"[test] update result: {updated}")
            assert updated is True

            edges = run(s.get_edges_for_mu(mu1))
            updated_edge = next(e for e in edges if e["edge_id"] == eid)
            print(f"[test] new weight: {updated_edge['weight']}")
            assert updated_edge["weight"] == pytest.approx(0.8)
            # Direction unchanged
            assert updated_edge["direction"] == "forward"

    def test_update_edge_weight_clamps_above_one(self):
        with TempDBStore() as s:
            mu1 = str(uuid.uuid4())
            mu2 = str(uuid.uuid4())
            run(s.create_mu(mu_id=mu1, source_type="user_input", raw_content="a"))
            run(s.create_mu(mu_id=mu2, source_type="agent_output", raw_content="b"))

            edge = run(s.create_edge(source_mu_id=mu1, target_mu_id=mu2, weight=0.5))
            run(s.update_edge_weight(edge["edge_id"], 1.5))

            edges = run(s.get_edges_for_mu(mu1))
            w = edges[0]["weight"]
            print(f"[test] clamped weight: {w}")
            assert w == pytest.approx(1.0)

    def test_update_edge_weight_clamps_below_zero(self):
        with TempDBStore() as s:
            mu1 = str(uuid.uuid4())
            mu2 = str(uuid.uuid4())
            run(s.create_mu(mu_id=mu1, source_type="user_input", raw_content="a"))
            run(s.create_mu(mu_id=mu2, source_type="agent_output", raw_content="b"))

            edge = run(s.create_edge(source_mu_id=mu1, target_mu_id=mu2, weight=0.5))
            run(s.update_edge_weight(edge["edge_id"], -0.3))

            edges = run(s.get_edges_for_mu(mu1))
            w = edges[0]["weight"]
            print(f"[test] clamped to zero: {w}")
            assert w == pytest.approx(0.0)

    def test_update_nonexistent_edge_returns_false(self):
        with TempDBStore() as s:
            result = run(s.update_edge_weight("no-such-edge", 0.7))
            print(f"[test] missing edge update: {result}")
            assert result is False


class TestUpdateMuActivation:

    def test_update_mu_activation_increments_count(self):
        with TempDBStore() as s:
            mid = str(uuid.uuid4())
            run(s.create_mu(mu_id=mid, source_type="user_input", raw_content="hello"))

            run(s.update_mu_activation(mid))
            row = run(s.get_mu(mid))
            print(f"[test] activation_count after 1: {row['activation_count']}")
            assert row["activation_count"] == 1
            assert row["last_activated_at"] is not None

            run(s.update_mu_activation(mid))
            row = run(s.get_mu(mid))
            print(f"[test] activation_count after 2: {row['activation_count']}")
            assert row["activation_count"] == 2
