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
                        "INSERT INTO analytics (run_id, series_id, timestamp) VALUES (?, ?, ?)",
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

    def test_get_series_turn_count_returns_0_for_unknown_series(self):
        """series_turn_count for an unknown series returns 0."""
        with TempDBStore() as s:
            count = run(s.get_series_turn_count("no-such-session"))
            print(f"[test] unknown session count: {count}")
            assert count == 0


class TestFeedbackEvents:

    def _make_run_and_insert(self, s, run_id, series_id="sess-1"):
        """Insert a minimal analytics row so feedback_event FK is valid."""
        async def _insert():
            import memory.store as sm
            db = await sm._open_db()
            try:
                await db.execute(
                    "INSERT INTO analytics (run_id, series_id, timestamp) VALUES (?, ?, ?)",
                    (run_id, series_id, "2026-01-01T00:00:00Z"),
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


# ---------------------------------------------------------------------------
# Milestone 32 — verdict-mix counts per series
# ---------------------------------------------------------------------------


class TestVerdictCountsBySeries:

    def _make_run_and_insert(self, s, run_id, series_id, experiment_id):
        """Insert a minimal analytics row with series_id + experiment_id."""
        async def _insert():
            import memory.store as sm
            db = await sm._open_db()
            try:
                await db.execute(
                    "INSERT INTO analytics (run_id, series_id, experiment_id, timestamp) "
                    "VALUES (?, ?, ?, ?)",
                    (run_id, series_id, experiment_id, "2026-01-01T00:00:00Z"),
                )
                await db.commit()
            finally:
                await db.close()
        run(_insert())

    def _insert_feedback(
        self, s, run_id, verdict, is_probe_turn=False,
        judge_source=None, judge_model_verdict=None,
    ):
        run(s.insert_feedback_event({
            "feedback_event_id": str(uuid.uuid4()),
            "run_id": run_id,
            "verdict": verdict,
            "verdict_basis": None,
            "turn_index": None,
            "is_probe_turn": is_probe_turn,
            "judge_source": judge_source,
            "judge_model_verdict": judge_model_verdict,
        }))

    def test_counts_up_down_none_per_series(self):
        """Bulk-turn verdicts are tallied per series_id."""
        with TempDBStore() as s:
            exp_id = "exp-verdict-mix"
            run_a1, run_a2, run_b1 = str(uuid.uuid4()), str(uuid.uuid4()), str(uuid.uuid4())
            self._make_run_and_insert(s, run_a1, "series-A", exp_id)
            self._make_run_and_insert(s, run_a2, "series-A", exp_id)
            self._make_run_and_insert(s, run_b1, "series-B", exp_id)
            self._insert_feedback(s, run_a1, "up")
            self._insert_feedback(s, run_a2, "down")
            self._insert_feedback(s, run_b1, "none")

            counts = run(s.get_verdict_counts_by_series(exp_id))
            print(f"[test] verdict counts: {counts}")
            assert counts["series-A"]["up"] == 1
            assert counts["series-A"]["down"] == 1
            assert counts["series-A"]["none"] == 0
            assert counts["series-B"]["none"] == 1

    def test_excludes_probe_turn_feedback(self):
        """A feedback_events row with is_probe_turn=1 is not counted — probe
        turns are never graded on this path (an accidental probe grading is
        an integrity violation the §5.6 check flags separately)."""
        with TempDBStore() as s:
            exp_id = "exp-verdict-mix-probe"
            run_id = str(uuid.uuid4())
            self._make_run_and_insert(s, run_id, "series-A", exp_id)
            self._insert_feedback(s, run_id, "up", is_probe_turn=True)

            counts = run(s.get_verdict_counts_by_series(exp_id))
            print(f"[test] counts with probe-turn feedback excluded: {counts}")
            assert counts == {}

    def test_scoped_to_experiment_id_no_contamination(self):
        """A different experiment's feedback events must not leak into this
        experiment's counts, even when both use the same series_id string."""
        with TempDBStore() as s:
            run_own, run_other = str(uuid.uuid4()), str(uuid.uuid4())
            self._make_run_and_insert(s, run_own, "series-A", "exp-own")
            self._make_run_and_insert(s, run_other, "series-A", "exp-other")
            self._insert_feedback(s, run_own, "up")
            self._insert_feedback(s, run_other, "down")

            counts = run(s.get_verdict_counts_by_series("exp-own"))
            print(f"[test] scoped counts: {counts}")
            assert list(counts) == ["series-A"]
            assert counts["series-A"]["up"] == 1
            assert counts["series-A"]["down"] == 0

    # --- M36: judge-alone split -------------------------------------------

    def test_override_splits_recorded_from_judge_alone(self):
        """An operator override records the human verdict but tallies the
        judge's own verdict separately — the divergence the report needs."""
        with TempDBStore() as s:
            exp_id = "exp-override"
            r_ok, r_override = str(uuid.uuid4()), str(uuid.uuid4())
            self._make_run_and_insert(s, r_ok, "series-A", exp_id)
            self._make_run_and_insert(s, r_override, "series-A", exp_id)
            self._insert_feedback(
                s, r_ok, "up", judge_source="model:gemma4:e4b", judge_model_verdict="up",
            )
            # The judge said "up"; the operator overrode to "down".
            self._insert_feedback(
                s, r_override, "down", judge_source="human", judge_model_verdict="up",
            )

            counts = run(s.get_verdict_counts_by_series(exp_id))["series-A"]
            print(f"[test] override split: {counts}")
            # Recorded: what drove learning.
            assert counts["up"] == 1
            assert counts["down"] == 1
            # Judge-alone: both turns were "up" before the operator stepped in.
            assert counts["judge_up"] == 2
            assert counts["judge_down"] == 0
            assert counts["human_sourced"] == 1
            assert counts["human_unrecoverable"] == 0

    def test_pre_m36_model_rows_fall_back_to_recorded_verdict(self):
        """A model-sourced row with no judge_model_verdict (pre-M36) counts
        toward judge-alone using its recorded verdict — for a model row the
        two are the same thing, so this is recovery, not guesswork."""
        with TempDBStore() as s:
            exp_id = "exp-legacy-model"
            run_id = str(uuid.uuid4())
            self._make_run_and_insert(s, run_id, "series-A", exp_id)
            self._insert_feedback(
                s, run_id, "down", judge_source="model:gemma4:e4b", judge_model_verdict=None,
            )

            counts = run(s.get_verdict_counts_by_series(exp_id))["series-A"]
            print(f"[test] legacy model row: {counts}")
            assert counts["down"] == 1
            assert counts["judge_down"] == 1
            assert counts["human_unrecoverable"] == 0

    def test_pre_m36_human_rows_are_unrecoverable_not_guessed(self):
        """A human row with no judge_model_verdict has no recoverable
        pre-override verdict. It must be flagged, never folded into the
        judge-alone tallies as if the judge had said it."""
        with TempDBStore() as s:
            exp_id = "exp-legacy-human"
            run_id = str(uuid.uuid4())
            self._make_run_and_insert(s, run_id, "series-A", exp_id)
            self._insert_feedback(
                s, run_id, "down", judge_source="human", judge_model_verdict=None,
            )

            counts = run(s.get_verdict_counts_by_series(exp_id))["series-A"]
            print(f"[test] legacy human row: {counts}")
            assert counts["down"] == 1
            assert counts["judge_down"] == 0, "must not attribute a human verdict to the judge"
            assert counts["judge_up"] == 0
            assert counts["human_sourced"] == 1
            assert counts["human_unrecoverable"] == 1

    def test_no_feedback_events_returns_empty_dict(self):
        """An experiment with no bulk-turn feedback (all-probe or dry-run
        series) returns an empty dict rather than raising."""
        with TempDBStore() as s:
            counts = run(s.get_verdict_counts_by_series("exp-empty"))
            print(f"[test] empty counts: {counts}")
            assert counts == {}


# ---------------------------------------------------------------------------
# M33 — get_prompt_ids_by_run (conversation-edge classification's prompt
# lineage signal)
# ---------------------------------------------------------------------------


class TestGetPromptIdsByRun:

    def _make_run_and_insert(self, s, run_id, series_id, experiment_id):
        async def _insert():
            import memory.store as sm
            db = await sm._open_db()
            try:
                await db.execute(
                    "INSERT INTO analytics (run_id, series_id, experiment_id, timestamp) "
                    "VALUES (?, ?, ?, ?)",
                    (run_id, series_id, experiment_id, "2026-01-01T00:00:00Z"),
                )
                await db.commit()
            finally:
                await db.close()
        run(_insert())

    def _insert_feedback(self, s, run_id, prompt_id, verdict="up"):
        run(s.insert_feedback_event({
            "feedback_event_id": str(uuid.uuid4()),
            "run_id": run_id,
            "verdict": verdict,
            "verdict_basis": None,
            "turn_index": None,
            "is_probe_turn": False,
            "prompt_id": prompt_id,
        }))

    def test_maps_run_id_to_prompt_id(self):
        with TempDBStore() as s:
            exp_id = "exp-lineage"
            run_a = str(uuid.uuid4())
            self._make_run_and_insert(s, run_a, "series-A", exp_id)
            self._insert_feedback(s, run_a, "cluster_a_0003")

            mapping = run(s.get_prompt_ids_by_run(exp_id))
            print(f"[test] prompt id mapping: {mapping}")
            assert mapping == {run_a: "cluster_a_0003"}

    def test_excludes_runs_with_no_prompt_id(self):
        """Probe turns and dry runs never carry a prompt_id — they're
        excluded rather than mapped to None."""
        with TempDBStore() as s:
            exp_id = "exp-lineage-probe"
            run_a = str(uuid.uuid4())
            self._make_run_and_insert(s, run_a, "series-A", exp_id)
            self._insert_feedback(s, run_a, prompt_id=None)

            mapping = run(s.get_prompt_ids_by_run(exp_id))
            print(f"[test] mapping with null prompt_id: {mapping}")
            assert mapping == {}

    def test_scoped_to_experiment_id_no_contamination(self):
        """A different experiment's feedback events must not leak into this
        experiment's prompt-id mapping, even sharing a series_id string."""
        with TempDBStore() as s:
            run_own, run_other = str(uuid.uuid4()), str(uuid.uuid4())
            self._make_run_and_insert(s, run_own, "series-A", "exp-own")
            self._make_run_and_insert(s, run_other, "series-A", "exp-other")
            self._insert_feedback(s, run_own, "cluster_a_0001")
            self._insert_feedback(s, run_other, "cluster_b_0002")

            mapping = run(s.get_prompt_ids_by_run("exp-own"))
            print(f"[test] scoped mapping: {mapping}")
            assert mapping == {run_own: "cluster_a_0001"}

    def test_no_feedback_events_returns_empty_dict(self):
        with TempDBStore() as s:
            mapping = run(s.get_prompt_ids_by_run("exp-empty"))
            print(f"[test] empty mapping: {mapping}")
            assert mapping == {}


# ---------------------------------------------------------------------------
# Milestone 10 — use_session_context round-trip
# ---------------------------------------------------------------------------


class TestUseSessionContextField:

    def _insert_run_with_flag(self, s, run_id, use_session_context: bool):
        """Insert a run record with the use_session_context flag."""
        async def _insert():
            import memory.store as sm
            from analytics.run_log import RunRecord
            record = RunRecord(
                run_id=run_id,
                series_id="sess-m10",
                use_session_context=use_session_context,
            )
            await s.insert_run(record.model_dump())
        run(_insert())

    def test_use_session_context_false_stores_as_0(self):
        """use_session_context=False round-trips as integer 0 in SQLite."""
        with TempDBStore() as s:
            run_id = str(uuid.uuid4())
            self._insert_run_with_flag(s, run_id, use_session_context=False)

            fetched = run(s.get_run(run_id))
            print(f"[test] use_session_context (False): {fetched['use_session_context']}")
            assert fetched is not None
            assert fetched["use_session_context"] == 0

    def test_use_session_context_true_stores_as_1(self):
        """use_session_context=True round-trips as integer 1 in SQLite."""
        with TempDBStore() as s:
            run_id = str(uuid.uuid4())
            self._insert_run_with_flag(s, run_id, use_session_context=True)

            fetched = run(s.get_run(run_id))
            print(f"[test] use_session_context (True): {fetched['use_session_context']}")
            assert fetched is not None
            assert fetched["use_session_context"] == 1


# ---------------------------------------------------------------------------
# Milestone 12 — series_id, experiment_id, cluster_id on memory_units
# ---------------------------------------------------------------------------


class TestMilestone12MuFields:

    def test_create_mu_stores_series_id(self):
        """create_mu() with series_id round-trips through SQLite."""
        with TempDBStore() as s:
            mid = str(uuid.uuid4())
            run(s.create_mu(mu_id=mid, source_type="user_input", raw_content="x",
                            series_id="series-abc"))
            row = run(s.get_mu(mid))
            print(f"[test] series_id: {row['series_id']}")
            assert row["series_id"] == "series-abc"

    def test_create_mu_stores_experiment_id(self):
        """create_mu() with experiment_id round-trips through SQLite."""
        with TempDBStore() as s:
            mid = str(uuid.uuid4())
            run(s.create_mu(mu_id=mid, source_type="agent_output", raw_content="y",
                            experiment_id="exp-2026-06-26"))
            row = run(s.get_mu(mid))
            print(f"[test] experiment_id: {row['experiment_id']}")
            assert row["experiment_id"] == "exp-2026-06-26"

    def test_create_mu_stores_cluster_id(self):
        """create_mu() with cluster_id round-trips through SQLite."""
        with TempDBStore() as s:
            mid = str(uuid.uuid4())
            run(s.create_mu(mu_id=mid, source_type="prompt", raw_content="z",
                            cluster_id="cluster-42"))
            row = run(s.get_mu(mid))
            print(f"[test] cluster_id: {row['cluster_id']}")
            assert row["cluster_id"] == "cluster-42"

    def test_create_mu_all_three_null_by_default(self):
        """series_id, experiment_id, cluster_id default to None when not supplied."""
        with TempDBStore() as s:
            mid = str(uuid.uuid4())
            run(s.create_mu(mu_id=mid, source_type="system", raw_content="w"))
            row = run(s.get_mu(mid))
            print(f"[test] defaults: {row['series_id']} {row['experiment_id']} {row['cluster_id']}")
            assert row["series_id"] is None
            assert row["experiment_id"] is None
            assert row["cluster_id"] is None

    def test_get_series_turn_count_returns_correct_count(self):
        """get_series_turn_count() counts runs matching the given series_id."""
        from analytics.run_log import RunRecord, capture_run
        import analytics.run_log as rl_module

        with TempDBStore() as s:
            # Patch the run_log singleton so capture_run uses our temp store
            with mock.patch.object(rl_module, "graph_store", s):
                sid_a = str(uuid.uuid4())
                sid_b = str(uuid.uuid4())
                r1 = RunRecord(series_id=sid_a)
                r2 = RunRecord(series_id=sid_a)
                r3 = RunRecord(series_id=sid_b)
                run(capture_run(r1))
                run(capture_run(r2))
                run(capture_run(r3))

            count_a = run(s.get_series_turn_count(sid_a))
            count_b = run(s.get_series_turn_count(sid_b))
            count_unknown = run(s.get_series_turn_count("no-such-series"))
            print(f"[test] count_a={count_a} count_b={count_b} count_unknown={count_unknown}")
            assert count_a == 2
            assert count_b == 1
            assert count_unknown == 0

    def test_migration_renames_session_id_to_series_id(self):
        """_migrate_db() renames analytics.session_id → series_id and preserves data."""
        import aiosqlite
        import tempfile
        from memory.store import _migrate_db

        async def _run_migration_test():
            with tempfile.TemporaryDirectory() as tmpdir:
                db_path = Path(tmpdir) / "old_schema.db"
                db = await aiosqlite.connect(str(db_path))
                db.row_factory = aiosqlite.Row
                # Create OLD analytics schema with session_id (pre-M12)
                await db.executescript("""
                    CREATE TABLE analytics (
                        run_id TEXT PRIMARY KEY,
                        session_id TEXT NOT NULL,
                        timestamp TEXT NOT NULL
                    );
                    CREATE TABLE memory_units (
                        mu_id TEXT PRIMARY KEY,
                        created_at TEXT NOT NULL,
                        source_type TEXT NOT NULL,
                        raw_content TEXT NOT NULL
                    );
                """)
                await db.commit()
                # Insert a row under the OLD column name
                await db.execute(
                    "INSERT INTO analytics (run_id, session_id, timestamp) VALUES (?, ?, ?)",
                    ("run-1", "series-old-value", "2026-01-01T00:00:00Z"),
                )
                await db.commit()
                # Run migration
                await _migrate_db(db)
                # Verify: old data accessible via new column name
                async with db.execute("SELECT series_id FROM analytics WHERE run_id=?", ("run-1",)) as cur:
                    row = await cur.fetchone()
                await db.close()
                return row

        row = asyncio.get_event_loop().run_until_complete(_run_migration_test())
        print(f"[test] migrated series_id: {row['series_id']}")
        assert row is not None
        assert row["series_id"] == "series-old-value"

    def test_migration_adds_judge_columns_and_preserves_existing_rows(self):
        """
        M19 Phase 2: _migrate_db() adds the 8 judge-verdict columns to an old
        feedback_events table (pre-Phase-2 schema) without disturbing
        existing rows.
        """
        import aiosqlite
        import tempfile
        from memory.store import _migrate_db

        async def _run_migration_test():
            with tempfile.TemporaryDirectory() as tmpdir:
                db_path = Path(tmpdir) / "old_feedback_events.db"
                db = await aiosqlite.connect(str(db_path))
                db.row_factory = aiosqlite.Row
                # Pre-Phase-2 feedback_events schema (M8/M9 — 6 columns only)
                await db.executescript("""
                    CREATE TABLE analytics (
                        run_id TEXT PRIMARY KEY,
                        series_id TEXT NOT NULL,
                        timestamp TEXT NOT NULL
                    );
                    CREATE TABLE feedback_events (
                        feedback_event_id TEXT PRIMARY KEY,
                        run_id TEXT NOT NULL,
                        verdict TEXT NOT NULL,
                        verdict_basis TEXT,
                        turn_index INTEGER,
                        is_probe_turn INTEGER NOT NULL DEFAULT 0
                    );
                """)
                await db.commit()
                await db.execute(
                    "INSERT INTO feedback_events "
                    "(feedback_event_id, run_id, verdict, is_probe_turn) VALUES (?, ?, ?, ?)",
                    ("fbevt-1", "run-1", "thumbs_up", 0),
                )
                await db.commit()

                await _migrate_db(db)

                async with db.execute(
                    "SELECT * FROM feedback_events WHERE feedback_event_id=?", ("fbevt-1",)
                ) as cur:
                    row = await cur.fetchone()
                await db.close()
                return row

        row = asyncio.get_event_loop().run_until_complete(_run_migration_test())
        print(f"[test] migrated feedback_events row: {dict(row)}")
        assert row is not None
        # Pre-existing data untouched
        assert row["verdict"] == "thumbs_up"
        assert row["run_id"] == "run-1"
        # New columns exist and default to NULL for pre-existing rows
        assert row["condition_label"] is None
        assert row["rationale"] is None
        assert row["criterion_triggered"] is None
        assert row["judge_source"] is None
        assert row["judge_model_name"] is None
        assert row["judge_temperature"] is None
        assert row["judge_pinned_snapshot"] is None
        assert row["prompt_id"] is None


# ---------------------------------------------------------------------------
# Milestone 19 Phase 0 — wipe_graph
# ---------------------------------------------------------------------------


class TestWipeGraph:

    def test_wipe_graph_deletes_all_mus_and_edges(self):
        with TempDBStore() as s:
            mu1 = str(uuid.uuid4())
            mu2 = str(uuid.uuid4())
            run(s.create_mu(mu_id=mu1, source_type="user_input", raw_content="a"))
            run(s.create_mu(mu_id=mu2, source_type="agent_output", raw_content="b"))
            run(s.create_edge(source_mu_id=mu1, target_mu_id=mu2, weight=0.5))

            result = run(s.wipe_graph())
            print(f"[test] wipe_graph result: {result}")
            assert result == {"mus_deleted": 2, "edges_deleted": 1}

            assert run(s.get_all_mus()) == []
            assert run(s.get_all_edges()) == []

    def test_wipe_graph_on_empty_graph_returns_zero_counts(self):
        with TempDBStore() as s:
            result = run(s.wipe_graph())
            print(f"[test] wipe_graph on empty DB: {result}")
            assert result == {"mus_deleted": 0, "edges_deleted": 0}

    def test_wipe_graph_leaves_analytics_table_untouched(self):
        with TempDBStore() as s:
            mu1 = str(uuid.uuid4())
            run(s.create_mu(mu_id=mu1, source_type="user_input", raw_content="a"))

            async def _insert_run():
                import memory.store as sm
                db = await sm._open_db()
                try:
                    await db.execute(
                        "INSERT INTO analytics (run_id, series_id, timestamp) VALUES (?, ?, ?)",
                        ("run-preserved", "series-1", "2026-01-01T00:00:00Z"),
                    )
                    await db.commit()
                finally:
                    await db.close()
            run(_insert_run())

            run(s.wipe_graph())

            preserved_run = run(s.get_run("run-preserved"))
            print(f"[test] analytics row survives wipe: {preserved_run is not None}")
            assert preserved_run is not None


class TestEdgeEvidenceCounters:
    """M20 Phase 1 — alpha/beta evidence counters on edges."""

    def test_create_edge_defaults_alpha_beta_to_one(self):
        with TempDBStore() as s:
            mu1, mu2 = str(uuid.uuid4()), str(uuid.uuid4())
            run(s.create_mu(mu_id=mu1, source_type="user_input", raw_content="a"))
            run(s.create_mu(mu_id=mu2, source_type="agent_output", raw_content="b"))
            edge = run(s.create_edge(source_mu_id=mu1, target_mu_id=mu2, weight=0.5))
            assert edge["alpha"] == 1.0
            assert edge["beta"] == 1.0
            assert edge["evidence_updated_at_turn"] is None

    def test_create_edge_accepts_explicit_alpha_beta(self):
        with TempDBStore() as s:
            mu1, mu2 = str(uuid.uuid4()), str(uuid.uuid4())
            run(s.create_mu(mu_id=mu1, source_type="user_input", raw_content="a"))
            run(s.create_mu(mu_id=mu2, source_type="agent_output", raw_content="b"))
            edge = run(s.create_edge(
                source_mu_id=mu1, target_mu_id=mu2, weight=0.5, alpha=3.0, beta=7.0,
            ))
            assert edge["alpha"] == 3.0
            assert edge["beta"] == 7.0

            fetched = run(s.get_edge(edge["edge_id"]))
            assert fetched["alpha"] == 3.0
            assert fetched["beta"] == 7.0

    def test_update_edge_counters_does_not_touch_weight_column(self):
        with TempDBStore() as s:
            mu1, mu2 = str(uuid.uuid4()), str(uuid.uuid4())
            run(s.create_mu(mu_id=mu1, source_type="user_input", raw_content="a"))
            run(s.create_mu(mu_id=mu2, source_type="agent_output", raw_content="b"))
            edge = run(s.create_edge(source_mu_id=mu1, target_mu_id=mu2, weight=0.42))

            updated = run(s.update_edge_counters(
                edge_id=edge["edge_id"], alpha=5.0, beta=2.0, evidence_updated_at_turn=17,
            ))
            assert updated is True

            fetched = run(s.get_edge(edge["edge_id"]))
            assert fetched["alpha"] == 5.0
            assert fetched["beta"] == 2.0
            assert fetched["evidence_updated_at_turn"] == 17
            assert fetched["weight"] == pytest.approx(0.42)  # untouched

    def test_update_edge_counters_returns_false_for_unknown_edge(self):
        with TempDBStore() as s:
            updated = run(s.update_edge_counters(edge_id="does-not-exist", alpha=1.0, beta=1.0))
            assert updated is False


class TestEdgeUpdateEvents:
    """M20 Phase 1 — append-only edge_update_events log."""

    def _sample_event(self, **overrides) -> dict:
        event = {
            "event_id": str(uuid.uuid4()),
            "run_id": "run-1",
            "turn_index": 3,
            "verdict_id": "fb-1",
            "mu_id": "mu-2",
            "edge_id": "edge-1",
            "hop_k": 0,
            "verdict": "up",
            "increment": 1.0,
            "alpha_before": 1.0,
            "alpha_after": 2.0,
            "beta_before": 1.0,
            "beta_after": 1.0,
            "created_at": "2026-07-06T00:00:00+00:00",
        }
        event.update(overrides)
        return event

    def test_insert_and_fetch_by_edge(self):
        with TempDBStore() as s:
            run(s.insert_edge_update_event(self._sample_event()))
            events = run(s.get_edge_update_events_for_edge("edge-1"))
            assert len(events) == 1
            assert events[0]["verdict"] == "up"
            assert events[0]["alpha_before"] == 1.0
            assert events[0]["alpha_after"] == 2.0

    def test_insert_and_fetch_by_run(self):
        with TempDBStore() as s:
            run(s.insert_edge_update_event(self._sample_event(event_id=str(uuid.uuid4()))))
            run(s.insert_edge_update_event(self._sample_event(
                event_id=str(uuid.uuid4()), edge_id="edge-2",
            )))
            events = run(s.get_edge_update_events_for_run("run-1"))
            assert len(events) == 2

    def test_multiple_events_for_same_edge_are_independently_stored(self):
        """One (edge, verdict) increment per row — additivity, no dedup."""
        with TempDBStore() as s:
            run(s.insert_edge_update_event(self._sample_event(
                event_id=str(uuid.uuid4()), alpha_before=1.0, alpha_after=2.0,
            )))
            run(s.insert_edge_update_event(self._sample_event(
                event_id=str(uuid.uuid4()), alpha_before=2.0, alpha_after=3.0,
            )))
            events = run(s.get_edge_update_events_for_edge("edge-1"))
            assert len(events) == 2

    def test_survives_graph_wipe(self):
        """Like feedback_events/analytics, edge_update_events is part of the
        experimental audit trail and must survive wipe_graph()."""
        with TempDBStore() as s:
            mu1 = str(uuid.uuid4())
            run(s.create_mu(mu_id=mu1, source_type="user_input", raw_content="a"))
            run(s.insert_edge_update_event(self._sample_event()))

            run(s.wipe_graph())

            events = run(s.get_edge_update_events_for_edge("edge-1"))
            assert len(events) == 1


# ---------------------------------------------------------------------------
# M21: experiment_id on probe_results
# ---------------------------------------------------------------------------


class TestProbeResultExperimentId:
    """probe_results rows carry the experiment they were measured under, so
    reports never mix probe data across attempts/dry-runs that share a
    condition_label."""

    @staticmethod
    def _probe_result(result_id, session, condition="learned", experiment_id=None):
        return {
            "result_id": result_id,
            "probe_id": "p06",
            "run_id": None,
            "measurement_session_index": session,
            "condition_label": condition,
            "retrieved_mu_ids": ["mu-a"],
            "gold_mu_rank": 1,
            "retrieval_precision": 1.0,
            "retrieval_recall": 1.0,
            "bm25_scores_per_mu": {"mu-a": 2.0},
            "experiment_id": experiment_id,
        }

    @staticmethod
    def _sync_probe(store):
        run(store.sync_probes([{
            "probe_id": "p06",
            "probe_set_version": "v2",
            "phenomenon_type": "ranking_improvement",
            "prompt_text": "test",
            "gold_mu_ids": ["mu-a"],
            "is_bridge_probe": False,
            "bridge_pair_id": None,
        }]))

    def test_insert_and_query_by_experiment_id(self):
        with TempDBStore() as store:
            self._sync_probe(store)
            run(store.insert_probe_result(self._probe_result("r1", 1, experiment_id="plan1_learned")))
            run(store.insert_probe_result(self._probe_result("r2", 1, experiment_id="plan1_attempt2_learned")))

            results = run(store.query_probe_results(experiment_id="plan1_learned"))
            assert [r["result_id"] for r in results] == ["r1"]
            assert results[0]["experiment_id"] == "plan1_learned"
            print("[PASS] query_probe_results filters by experiment_id")

    def test_experiment_id_filter_excludes_legacy_null_rows(self):
        with TempDBStore() as store:
            self._sync_probe(store)
            run(store.insert_probe_result(self._probe_result("legacy", 1)))  # experiment_id None
            run(store.insert_probe_result(self._probe_result("new", 2, experiment_id="plan1_learned")))

            results = run(store.query_probe_results(experiment_id="plan1_learned"))
            assert [r["result_id"] for r in results] == ["new"]
            # No filter still returns both
            all_results = run(store.query_probe_results())
            assert {r["result_id"] for r in all_results} == {"legacy", "new"}
            print("[PASS] experiment_id filter excludes legacy NULL rows; unfiltered query returns all")

    def test_get_probe_session_indices_for_experiment(self):
        with TempDBStore() as store:
            self._sync_probe(store)
            run(store.insert_probe_result(self._probe_result("r1", 1, experiment_id="plan1_learned")))
            run(store.insert_probe_result(self._probe_result("r2", 2, experiment_id="plan1_learned")))
            run(store.insert_probe_result(self._probe_result("r3", 2, experiment_id="plan1_learned")))
            run(store.insert_probe_result(self._probe_result("other", 9, experiment_id="other_experiment")))

            indices = run(store.get_probe_session_indices_for_experiment("plan1_learned"))
            assert indices == [1, 2]  # distinct, sorted, scoped to one experiment
            print("[PASS] get_probe_session_indices_for_experiment returns distinct scoped indices")

    def test_migration_adds_experiment_id_to_existing_probe_results_table(self):
        """A pre-M21 DB (probe_results without experiment_id) gains the column
        on next open, and its existing rows read back with experiment_id=None."""
        import aiosqlite

        async def _build_old_db(db_path):
            db = await aiosqlite.connect(db_path)
            await db.execute("""
                CREATE TABLE probes (
                    probe_id TEXT PRIMARY KEY,
                    probe_set_version TEXT NOT NULL,
                    phenomenon_type TEXT NOT NULL,
                    prompt_text TEXT NOT NULL,
                    gold_mu_ids TEXT NOT NULL DEFAULT '[]',
                    is_bridge_probe INTEGER NOT NULL DEFAULT 0,
                    bridge_pair_id TEXT
                )
            """)
            await db.execute("""
                CREATE TABLE probe_results (
                    result_id TEXT PRIMARY KEY,
                    probe_id TEXT NOT NULL REFERENCES probes(probe_id),
                    run_id TEXT,
                    measurement_session_index INTEGER NOT NULL,
                    condition_label TEXT NOT NULL,
                    retrieved_mu_ids TEXT NOT NULL DEFAULT '[]',
                    gold_mu_rank INTEGER,
                    retrieval_precision REAL,
                    retrieval_recall REAL,
                    bm25_scores_per_mu TEXT NOT NULL DEFAULT '{}',
                    outcome_override TEXT
                )
            """)
            await db.execute(
                "INSERT INTO probe_results (result_id, probe_id, measurement_session_index, condition_label) "
                "VALUES ('old-row', 'p06', 1, 'learning')"
            )
            await db.commit()
            await db.close()

        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "old_graph.db"
            run(_build_old_db(db_path))

            with mock.patch.object(store_module, "_DB_PATH", db_path):
                store = GraphStore()
                results = run(store.query_probe_results(probe_id="p06"))

            assert len(results) == 1
            assert results[0]["result_id"] == "old-row"
            assert results[0]["experiment_id"] is None
            print("[PASS] migration adds probe_results.experiment_id; legacy rows read back as None")


# ---------------------------------------------------------------------------
# M30: series_id / battery_mode / battery_repeat_index on probe_results
# ---------------------------------------------------------------------------


class TestProbeResultDualModeBattery:
    """probe_results rows carry series_id (so series_report.py can scope
    dual-mode probe measurements to one series) and battery_mode/
    battery_repeat_index (so the deterministic "mean" checkpoint pass is
    separable from the K independent "sample" repeats)."""

    @staticmethod
    def _probe_result(result_id, session, series_id=None, battery_mode="mean", battery_repeat_index=None):
        return {
            "result_id": result_id,
            "probe_id": "p06",
            "run_id": None,
            "measurement_session_index": session,
            "condition_label": "learned",
            "retrieved_mu_ids": ["mu-a"],
            "gold_mu_rank": 1,
            "retrieval_precision": 1.0,
            "retrieval_recall": 1.0,
            "bm25_scores_per_mu": {"mu-a": 2.0},
            "experiment_id": "plan1_learned",
            "series_id": series_id,
            "battery_mode": battery_mode,
            "battery_repeat_index": battery_repeat_index,
        }

    @staticmethod
    def _sync_probe(store):
        run(store.sync_probes([{
            "probe_id": "p06",
            "probe_set_version": "v2",
            "phenomenon_type": "ranking_improvement",
            "prompt_text": "test",
            "gold_mu_ids": ["mu-a"],
            "is_bridge_probe": False,
            "bridge_pair_id": None,
        }]))

    def test_insert_and_query_by_series_id(self):
        with TempDBStore() as store:
            self._sync_probe(store)
            run(store.insert_probe_result(self._probe_result("r1", 1, series_id="run1_learned_s0")))
            run(store.insert_probe_result(self._probe_result("r2", 1, series_id="run1_learned_s1")))

            results = run(store.query_probe_results(series_id="run1_learned_s0"))
            assert [r["result_id"] for r in results] == ["r1"]
            assert results[0]["series_id"] == "run1_learned_s0"
            print("[PASS] query_probe_results filters by series_id")

    def test_insert_and_query_by_battery_mode(self):
        with TempDBStore() as store:
            self._sync_probe(store)
            run(store.insert_probe_result(self._probe_result(
                "mean-pass", 1, series_id="run1_learned_s0", battery_mode="mean",
            )))
            for i in range(3):
                run(store.insert_probe_result(self._probe_result(
                    f"sample-{i}", 1, series_id="run1_learned_s0",
                    battery_mode="sample", battery_repeat_index=i,
                )))

            mean_results = run(store.query_probe_results(series_id="run1_learned_s0", battery_mode="mean"))
            sample_results = run(store.query_probe_results(series_id="run1_learned_s0", battery_mode="sample"))

            assert [r["result_id"] for r in mean_results] == ["mean-pass"]
            assert mean_results[0]["battery_repeat_index"] is None
            assert {r["result_id"] for r in sample_results} == {"sample-0", "sample-1", "sample-2"}
            assert sorted(r["battery_repeat_index"] for r in sample_results) == [0, 1, 2]
            print("[PASS] query_probe_results filters by battery_mode, repeat_index round-trips")

    def test_battery_mode_defaults_to_mean_when_omitted(self):
        with TempDBStore() as store:
            self._sync_probe(store)
            result = self._probe_result("r1", 1)
            del result["battery_mode"]  # omitted entirely, like a pre-M30 caller
            run(store.insert_probe_result(result))

            results = run(store.query_probe_results(probe_id="p06"))
            assert results[0]["battery_mode"] == "mean"
            print("[PASS] battery_mode defaults to 'mean' in the DB when omitted from the insert dict")

    def test_migration_adds_dual_mode_columns_to_existing_probe_results_table(self):
        """A pre-M30 DB (probe_results without series_id/battery_mode/
        battery_repeat_index) gains the columns on next open; existing rows
        read back with series_id=None, battery_mode='mean' (the column
        default), battery_repeat_index=None."""
        import aiosqlite

        async def _build_old_db(db_path):
            db = await aiosqlite.connect(db_path)
            await db.execute("""
                CREATE TABLE probes (
                    probe_id TEXT PRIMARY KEY,
                    probe_set_version TEXT NOT NULL,
                    phenomenon_type TEXT NOT NULL,
                    prompt_text TEXT NOT NULL,
                    gold_mu_ids TEXT NOT NULL DEFAULT '[]',
                    is_bridge_probe INTEGER NOT NULL DEFAULT 0,
                    bridge_pair_id TEXT
                )
            """)
            await db.execute("""
                CREATE TABLE probe_results (
                    result_id TEXT PRIMARY KEY,
                    probe_id TEXT NOT NULL REFERENCES probes(probe_id),
                    run_id TEXT,
                    measurement_session_index INTEGER NOT NULL,
                    condition_label TEXT NOT NULL,
                    retrieved_mu_ids TEXT NOT NULL DEFAULT '[]',
                    gold_mu_rank INTEGER,
                    retrieval_precision REAL,
                    retrieval_recall REAL,
                    bm25_scores_per_mu TEXT NOT NULL DEFAULT '{}',
                    outcome_override TEXT,
                    experiment_id TEXT
                )
            """)
            await db.execute(
                "INSERT INTO probe_results (result_id, probe_id, measurement_session_index, condition_label) "
                "VALUES ('old-row', 'p06', 1, 'learning')"
            )
            await db.commit()
            await db.close()

        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "old_graph.db"
            run(_build_old_db(db_path))

            with mock.patch.object(store_module, "_DB_PATH", db_path):
                store = GraphStore()
                results = run(store.query_probe_results(probe_id="p06"))

            assert len(results) == 1
            assert results[0]["result_id"] == "old-row"
            assert results[0]["series_id"] is None
            assert results[0]["battery_mode"] == "mean"
            assert results[0]["battery_repeat_index"] is None
            print("[PASS] migration adds probe_results dual-mode columns; legacy rows default correctly")
