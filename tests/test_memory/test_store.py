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


# ---------------------------------------------------------------------------
# Milestone 8 — new store methods
# ---------------------------------------------------------------------------


class TestEdgeCreationMetadata:

    def test_create_edge_stores_creation_run_id_and_trigger(self):
        """Edges created with Milestone 8 fields persist creation_run_id and creation_trigger."""
        with TempDBStore() as s:
            mu1 = str(uuid.uuid4())
            mu2 = str(uuid.uuid4())
            run(s.create_mu(mu_id=mu1, source_type="user_input", raw_content="a"))
            run(s.create_mu(mu_id=mu2, source_type="agent_output", raw_content="b"))

            run_id = str(uuid.uuid4())
            edge = run(s.create_edge(
                source_mu_id=mu1,
                target_mu_id=mu2,
                weight=0.5,
                creation_run_id=run_id,
                creation_trigger="prompt_construction",
            ))
            print(f"[test] created edge with metadata: {edge}")
            assert edge["creation_run_id"] == run_id
            assert edge["creation_trigger"] == "prompt_construction"

    def test_create_edge_without_metadata_stores_none(self):
        """Edges created without Milestone 8 fields store None for those columns."""
        with TempDBStore() as s:
            mu1 = str(uuid.uuid4())
            mu2 = str(uuid.uuid4())
            run(s.create_mu(mu_id=mu1, source_type="user_input", raw_content="a"))
            run(s.create_mu(mu_id=mu2, source_type="agent_output", raw_content="b"))

            edge = run(s.create_edge(source_mu_id=mu1, target_mu_id=mu2, weight=0.5))
            print(f"[test] edge without metadata: {edge}")
            assert edge["creation_run_id"] is None
            assert edge["creation_trigger"] is None


class TestTurnIndexMethods:

    def test_get_next_turn_index_returns_1_for_empty_db(self):
        """First turn index is 1 when analytics table is empty."""
        with TempDBStore() as s:
            idx = run(s.get_next_turn_index())
            print(f"[test] turn index for empty DB: {idx}")
            assert idx == 1

    def test_get_next_turn_index_increments_with_each_run(self):
        """turn index increments monotonically as runs are inserted."""
        import json
        import aiosqlite
        with TempDBStore() as s:
            # Insert a minimal row directly to avoid RunRecord dependency
            async def insert_minimal_run(run_id):
                import memory.store as sm
                db = await sm._open_db()
                try:
                    await db.execute(
                        "INSERT INTO analytics (run_id, session_id, timestamp) VALUES (?, ?, ?)",
                        (run_id, "sess-1", "2026-01-01T00:00:00Z"),
                    )
                    await db.commit()
                finally:
                    await db.close()

            idx1 = run(s.get_next_turn_index())
            assert idx1 == 1
            run(insert_minimal_run(str(uuid.uuid4())))
            idx2 = run(s.get_next_turn_index())
            print(f"[test] turn indices: {idx1}, {idx2}")
            assert idx2 == 2

    def test_get_session_turn_count_returns_0_for_unknown_session(self):
        """session_turn_count for an unknown session returns 0."""
        with TempDBStore() as s:
            count = run(s.get_session_turn_count("no-such-session"))
            print(f"[test] unknown session count: {count}")
            assert count == 0


class TestFeedbackEvents:

    def _make_run_and_insert(self, s, run_id, session_id="sess-1"):
        """Insert a minimal analytics row so feedback_event FK is valid."""
        async def _insert():
            import memory.store as sm
            db = await sm._open_db()
            try:
                await db.execute(
                    "INSERT INTO analytics (run_id, session_id, timestamp) VALUES (?, ?, ?)",
                    (run_id, session_id, "2026-01-01T00:00:00Z"),
                )
                await db.commit()
            finally:
                await db.close()
        run(_insert())

    def test_insert_and_get_feedback_event_round_trips(self):
        """insert_feedback_event and get_feedback_events_for_run round-trip correctly."""
        with TempDBStore() as s:
            run_id = str(uuid.uuid4())
            self._make_run_and_insert(s, run_id)

            event = {
                "feedback_event_id": str(uuid.uuid4()),
                "run_id": run_id,
                "verdict": "thumbs_up",
                "verdict_basis": "checklist_pass",
                "turn_index": 3,
                "is_probe_turn": False,
            }
            run(s.insert_feedback_event(event))

            fetched = run(s.get_feedback_events_for_run(run_id))
            print(f"[test] fetched feedback events: {fetched}")
            assert len(fetched) == 1
            assert fetched[0]["verdict"] == "thumbs_up"
            assert fetched[0]["verdict_basis"] == "checklist_pass"
            assert fetched[0]["turn_index"] == 3
            assert fetched[0]["is_probe_turn"] == 0  # SQLite stores bool as int

    def test_get_feedback_events_for_run_returns_empty_for_unknown_run(self):
        """get_feedback_events_for_run returns [] for a run_id with no events."""
        with TempDBStore() as s:
            fetched = run(s.get_feedback_events_for_run("no-such-run"))
            print(f"[test] empty feedback events: {fetched}")
            assert fetched == []

    def test_multiple_feedback_events_for_same_run_all_returned(self):
        """Multiple events for the same run_id are all returned."""
        with TempDBStore() as s:
            run_id = str(uuid.uuid4())
            self._make_run_and_insert(s, run_id)

            for verdict in ("thumbs_up", "thumbs_down"):
                event = {
                    "feedback_event_id": str(uuid.uuid4()),
                    "run_id": run_id,
                    "verdict": verdict,
                    "verdict_basis": None,
                    "turn_index": None,
                    "is_probe_turn": False,
                }
                run(s.insert_feedback_event(event))

            fetched = run(s.get_feedback_events_for_run(run_id))
            print(f"[test] two events: {[e['verdict'] for e in fetched]}")
            assert len(fetched) == 2
