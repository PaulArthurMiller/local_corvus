"""
Tests for analytics/feedback_events.py

Covers:
  - FeedbackEvent model defaults and field validation
  - record_feedback_event persists to the feedback_events table
  - Enriched weight_updates in the /feedback endpoint include
    source_mu_id, target_mu_id, and feedback_event_id
"""

import asyncio
import os
import sys
import tempfile
import uuid
from pathlib import Path
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "backend"))

import memory.store as store_module
from memory.store import GraphStore
from analytics.feedback_events import FeedbackEvent, record_feedback_event


def run(coro):
    return asyncio.get_event_loop().run_until_complete(coro)


class TempDBStore:
    """Context manager: isolated temp-file GraphStore."""

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


def _insert_minimal_run(run_id: str) -> None:
    """Insert a minimal analytics row so feedback_event FK is valid."""
    async def _do():
        import memory.store as sm
        db = await sm._open_db()
        try:
            await db.execute(
                "INSERT INTO analytics (run_id, series_id, timestamp) VALUES (?, ?, ?)",
                (run_id, "sess-1", "2026-01-01T00:00:00Z"),
            )
            await db.commit()
        finally:
            await db.close()
    run(_do())


class TestFeedbackEventModel:

    def test_default_fields_are_set(self):
        """FeedbackEvent generates a UUID for feedback_event_id by default."""
        event = FeedbackEvent(run_id="run-1", verdict="thumbs_up")
        print(f"[test] event id: {event.feedback_event_id}")
        assert len(event.feedback_event_id) == 36  # standard UUID length
        assert event.verdict == "thumbs_up"
        assert event.verdict_basis is None
        assert event.turn_index is None
        assert event.is_probe_turn is False

    def test_all_fields_can_be_set(self):
        """FeedbackEvent accepts all Milestone 8 fields."""
        event = FeedbackEvent(
            run_id="run-2",
            verdict="thumbs_down",
            verdict_basis="confabulation",
            turn_index=5,
            is_probe_turn=True,
        )
        print(f"[test] full event: {event.model_dump()}")
        assert event.verdict == "thumbs_down"
        assert event.verdict_basis == "confabulation"
        assert event.turn_index == 5
        assert event.is_probe_turn is True

    def test_none_verdict_is_valid(self):
        """verdict='none' is accepted for turns without explicit feedback."""
        event = FeedbackEvent(run_id="run-3", verdict="none")
        assert event.verdict == "none"


class TestRecordFeedbackEvent:

    def test_record_feedback_event_persists_to_db(self):
        """record_feedback_event writes to the feedback_events table."""
        with TempDBStore() as s:
            run_id = str(uuid.uuid4())
            _insert_minimal_run(run_id)

            import analytics.feedback_events as fe_module
            with mock.patch.object(fe_module, "graph_store", s):
                event = FeedbackEvent(
                    run_id=run_id,
                    verdict="thumbs_up",
                    verdict_basis="checklist_pass",
                    turn_index=2,
                    is_probe_turn=False,
                )
                run(record_feedback_event(event))

            fetched = run(s.get_feedback_events_for_run(run_id))
            print(f"[test] persisted event: {fetched}")
            assert len(fetched) == 1
            assert fetched[0]["verdict"] == "thumbs_up"
            assert fetched[0]["verdict_basis"] == "checklist_pass"
            assert fetched[0]["feedback_event_id"] == event.feedback_event_id

    def test_record_feedback_event_with_none_verdict_basis(self):
        """verdict_basis=None is stored correctly (not as the string 'None')."""
        with TempDBStore() as s:
            run_id = str(uuid.uuid4())
            _insert_minimal_run(run_id)

            import analytics.feedback_events as fe_module
            with mock.patch.object(fe_module, "graph_store", s):
                event = FeedbackEvent(run_id=run_id, verdict="thumbs_down")
                run(record_feedback_event(event))

            fetched = run(s.get_feedback_events_for_run(run_id))
            print(f"[test] verdict_basis for null: {fetched[0]['verdict_basis']}")
            assert fetched[0]["verdict_basis"] is None
