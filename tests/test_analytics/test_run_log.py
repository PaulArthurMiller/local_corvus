"""
Tests for analytics/run_log.py and the analytics table in memory/store.py

Covers:
  - capture_run persists a RunRecord to the analytics table
  - query_runs returns the inserted record
  - query_runs filters correctly by series_id
  - update_run_feedback updates feedback_signal and weight_updates
  - get_run fetches a single record by ID
"""

import sys
import os
import asyncio
import tempfile
import uuid
from pathlib import Path
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "backend"))

import memory.store as store_module
from memory.store import GraphStore
from analytics.run_log import RunRecord, capture_run, query_runs


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def run(coro):
    return asyncio.get_event_loop().run_until_complete(coro)


class TempDBStore:
    """Context manager: GraphStore + capture_run/query_runs pointed at a temp file."""

    def __enter__(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        self._db_path = Path(self._tmpdir.name) / "test_graph.db"
        self._patcher = mock.patch.object(store_module, "_DB_PATH", self._db_path)
        self._patcher.start()
        # Patch the singleton used by analytics.run_log
        import analytics.run_log as rl_module
        self._store = GraphStore()
        self._rl_patcher = mock.patch.object(rl_module, "graph_store", self._store)
        self._rl_patcher.start()
        return self._store

    def __exit__(self, *args):
        self._rl_patcher.stop()
        self._patcher.stop()
        self._tmpdir.cleanup()


def _make_run(series_id=None) -> RunRecord:
    return RunRecord(
        series_id=series_id or str(uuid.uuid4()),
        input_mu_id=str(uuid.uuid4()),
        output_mu_id=str(uuid.uuid4()),
        prompt_mu_id=str(uuid.uuid4()),
        tool_calls=[str(uuid.uuid4())],
        activated_mu_ids=[str(uuid.uuid4()), str(uuid.uuid4())],
        activated_edge_ids=[str(uuid.uuid4())],
        latency_ms=123,
        hyperparameters_snapshot={"initial_edge_weight": 0.5},
        algorithm="simple_rl",
    )


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_capture_run_persists_record():
    """capture_run writes a RunRecord and query_runs retrieves it."""
    with TempDBStore() as store:
        rec = _make_run()
        run(capture_run(rec))

        rows = run(query_runs())
        print(f"rows after capture: {rows}")
        assert len(rows) == 1
        assert rows[0]["run_id"] == rec.run_id
        assert rows[0]["series_id"] == rec.series_id
        assert rows[0]["latency_ms"] == 123
        assert rows[0]["algorithm"] == "simple_rl"


def test_query_runs_returns_all_runs_when_no_filter():
    """query_runs returns all records when series_id is not specified."""
    with TempDBStore():
        for _ in range(3):
            run(capture_run(_make_run()))
        rows = run(query_runs())
        print(f"total rows: {len(rows)}")
        assert len(rows) == 3


def test_query_runs_filters_by_series_id():
    """query_runs returns only records matching the given series_id."""
    session_a = str(uuid.uuid4())
    session_b = str(uuid.uuid4())
    with TempDBStore():
        run(capture_run(_make_run(series_id=session_a)))
        run(capture_run(_make_run(series_id=session_a)))
        run(capture_run(_make_run(series_id=session_b)))

        rows_a = run(query_runs(series_id=session_a, limit=100))
        rows_b = run(query_runs(series_id=session_b, limit=100))
        print(f"session_a rows: {len(rows_a)}, session_b rows: {len(rows_b)}")
        assert len(rows_a) == 2
        assert len(rows_b) == 1


def test_captured_run_has_list_fields_decoded():
    """JSON list fields are decoded back to Python lists, not raw strings."""
    with TempDBStore():
        rec = _make_run()
        run(capture_run(rec))
        rows = run(query_runs())
        row = rows[0]

        print(f"tool_calls type: {type(row['tool_calls'])}")
        assert isinstance(row["tool_calls"], list)
        assert isinstance(row["activated_mu_ids"], list)
        assert isinstance(row["activated_edge_ids"], list)
        assert isinstance(row["hyperparameters_snapshot"], dict)


def test_captured_run_hyperparameters_snapshot_matches():
    """The hyperparameters_snapshot field round-trips correctly."""
    with TempDBStore():
        hp = {"initial_edge_weight": 0.5, "weight_update_rate": 0.1}
        rec = RunRecord(
            series_id=str(uuid.uuid4()),
            hyperparameters_snapshot=hp,
        )
        run(capture_run(rec))
        rows = run(query_runs())
        assert rows[0]["hyperparameters_snapshot"] == hp


def test_get_run_returns_single_record():
    """get_run fetches a single run record by ID."""
    with TempDBStore() as store:
        rec = _make_run()
        run(capture_run(rec))
        fetched = run(store.get_run(rec.run_id))
        print(f"fetched run: {fetched}")
        assert fetched is not None
        assert fetched["run_id"] == rec.run_id


def test_get_run_returns_none_for_unknown_id():
    """get_run returns None for a run_id that doesn't exist."""
    with TempDBStore() as store:
        fetched = run(store.get_run(str(uuid.uuid4())))
        print(f"fetched unknown: {fetched}")
        assert fetched is None


def test_update_run_feedback_sets_signal_and_weight_updates():
    """update_run_feedback persists feedback_signal and weight_updates."""
    with TempDBStore() as store:
        rec = _make_run()
        run(capture_run(rec))

        weight_updates = [{"edge_id": str(uuid.uuid4()), "old_weight": 0.5, "new_weight": 0.6}]
        updated = run(store.update_run_feedback(rec.run_id, +1, weight_updates))
        print(f"updated: {updated}")
        assert updated is True

        fetched = run(store.get_run(rec.run_id))
        assert fetched["feedback_signal"] == 1
        assert isinstance(fetched["weight_updates"], list)
        assert fetched["weight_updates"][0]["new_weight"] == 0.6


# ---------------------------------------------------------------------------
# Milestone 8 — new RunRecord fields round-trip through SQLite
# ---------------------------------------------------------------------------


def _make_full_run(series_id=None) -> RunRecord:
    """RunRecord with all Milestone 8 fields populated."""
    return RunRecord(
        series_id=series_id or str(uuid.uuid4()),
        input_mu_id=str(uuid.uuid4()),
        output_mu_id=str(uuid.uuid4()),
        prompt_mu_id=str(uuid.uuid4()),
        latency_ms=200,
        hyperparameters_snapshot={"initial_edge_weight": 0.5},
        algorithm="simple_rl",
        bm25_scores_per_mu={"mu-abc": 1.23, "mu-def": 0.45},
        entry_point_mu_ids=["mu-abc"],
        candidate_set_before_ranking=[
            {"mu_id": "mu-abc", "keyword_hit_count": 2, "bm25_score": 1.23, "edge_weight_sum": 0.9},
        ],
        traversal_path=[
            {"source_mu_id": "mu-abc", "edge_id": "e-1", "target_mu_id": "mu-xyz", "hop_depth": 1},
        ],
        hop_depth_per_retrieved_mu={"mu-abc": 0, "mu-xyz": 1},
        edge_contributions={"mu-abc": 0.9},
        assembled_prompt_json=[{"role": "system", "content": "context"}, {"role": "user", "content": "hello"}],
        config_hash="a" * 64,
        full_config_snapshot={"model": {"name": "gemma4:e4b"}},
        condition_label="learning",
        turn_index=1,
        session_turn_index=0,
    )


def test_new_fields_round_trip_through_sqlite():
    """All Milestone 8 RunRecord fields persist and decode correctly."""
    with TempDBStore() as store:
        rec = _make_full_run()
        run(capture_run(rec))
        fetched = run(store.get_run(rec.run_id))

        print(f"[test] fetched run keys: {list(fetched.keys())}")
        assert fetched["bm25_scores_per_mu"] == {"mu-abc": 1.23, "mu-def": 0.45}
        assert fetched["entry_point_mu_ids"] == ["mu-abc"]
        assert len(fetched["candidate_set_before_ranking"]) == 1
        assert fetched["candidate_set_before_ranking"][0]["keyword_hit_count"] == 2
        assert fetched["traversal_path"][0]["hop_depth"] == 1
        assert fetched["hop_depth_per_retrieved_mu"] == {"mu-abc": 0, "mu-xyz": 1}
        assert fetched["edge_contributions"] == {"mu-abc": 0.9}
        assert len(fetched["assembled_prompt_json"]) == 2
        assert fetched["config_hash"] == "a" * 64
        assert fetched["full_config_snapshot"] == {"model": {"name": "gemma4:e4b"}}
        assert fetched["condition_label"] == "learning"
        assert fetched["turn_index"] == 1
        assert fetched["session_turn_index"] == 0


def test_config_hash_is_consistent_for_same_config():
    """The same config dict always produces the same config_hash."""
    import hashlib
    import json

    cfg = {"model": {"name": "gemma4:e4b"}, "hyperparameters": {"initial_edge_weight": 0.5}}
    hash1 = hashlib.sha256(json.dumps(cfg, sort_keys=True).encode()).hexdigest()
    hash2 = hashlib.sha256(json.dumps(cfg, sort_keys=True).encode()).hexdigest()
    print(f"[test] hash: {hash1}")
    assert hash1 == hash2
    assert len(hash1) == 64  # SHA-256 hex digest is 64 chars


# ---------------------------------------------------------------------------
# Milestone 12 — series_id and experiment_id on RunRecord
# ---------------------------------------------------------------------------


def test_run_record_series_id_is_populated():
    """RunRecord.series_id is set from the constructor argument."""
    sid = str(uuid.uuid4())
    record = RunRecord(series_id=sid)
    print(f"[test] series_id: {record.series_id}")
    assert record.series_id == sid
    assert record.model_dump()["series_id"] == sid


def test_run_record_experiment_id_can_be_set():
    """RunRecord.experiment_id accepts a non-None value and round-trips."""
    record = RunRecord(series_id=str(uuid.uuid4()), experiment_id="learning-2026-06-26")
    print(f"[test] experiment_id: {record.experiment_id}")
    assert record.experiment_id == "learning-2026-06-26"
    assert record.model_dump()["experiment_id"] == "learning-2026-06-26"


def test_run_record_experiment_id_defaults_to_none():
    """RunRecord.experiment_id is None when not supplied."""
    record = RunRecord(series_id=str(uuid.uuid4()))
    print(f"[test] default experiment_id: {record.experiment_id!r}")
    assert record.experiment_id is None


def test_capture_run_persists_experiment_id():
    """capture_run stores experiment_id and query_runs returns it."""
    with TempDBStore():
        rec = RunRecord(series_id=str(uuid.uuid4()), experiment_id="exp-m12-test")
        run(capture_run(rec))
        rows = run(query_runs())
        print(f"[test] experiment_id from db: {rows[0].get('experiment_id')}")
        assert rows[0]["experiment_id"] == "exp-m12-test"
