"""
Tests for analytics/reports/control_shadow.py — §6.2 control shadow checks.

Covers:
  Static-control zero_weight_changes: PASS on clean data; FAIL when a
    fabricated weight_updates entry is present for a static-control run.
  Random-control weights_rerandomized: FAIL on near-zero variance data;
    PASS when variance > floor.
  zero_feedback_edge_correlation: FAIL when a feedback event for a control
    run has non-empty weight_updates; PASS on clean data.
  Comparative delta computation: correct values for synthetic learned/control pairs.
"""

import asyncio
import json
import os
import sys
import tempfile
import uuid
from pathlib import Path
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "backend"))

import memory.store as store_module
from memory.store import GraphStore
from analytics.reports.control_shadow import (
    _check_static_zero_weight_changes,
    _check_random_weights_rerandomized,
    _check_zero_feedback_edge_correlation,
    _compute_comparative_deltas,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def async_run(coro):
    return asyncio.get_event_loop().run_until_complete(coro)


class TempDB:
    """Context manager: GraphStore pointed at a temp file."""

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


def _mu_id() -> str:
    return str(uuid.uuid4())


def _run_id() -> str:
    return str(uuid.uuid4())


def _edge_id() -> str:
    return str(uuid.uuid4())


def _make_record(
    run_id: str = None,
    experiment_id: str = "exp-ctrl",
    series_id: str = "series-ctrl",
    weight_updates=None,
    feedback_signal: int = None,
    activated_edge_ids=None,
    turn_index: int = 1,
    condition_label: str = "static-weight",
) -> dict:
    """Build a minimal run record dict ready for store.insert_run()."""
    return {
        "run_id": run_id or _run_id(),
        "series_id": series_id,
        "experiment_id": experiment_id,
        "timestamp": f"2026-01-01T00:{turn_index:02d}:00+00:00",
        "input_mu_id": _mu_id(),
        "output_mu_id": _mu_id(),
        "tool_calls": [],
        "activated_mu_ids": [],
        "activated_edge_ids": activated_edge_ids or [],
        "prompt_mu_id": _mu_id(),
        "tokens_in": 100,
        "tokens_out": 50,
        "latency_ms": 200,
        "feedback_signal": feedback_signal,
        "weight_updates": weight_updates or [],
        "hyperparameters_snapshot": {},
        "algorithm": "simple_rl",
        "bm25_scores_per_mu": {},
        "entry_point_mu_ids": [],
        "candidate_set_before_ranking": [],
        "traversal_path": [],
        "hop_depth_per_retrieved_mu": {},
        "edge_contributions": {},
        "assembled_prompt_json": [],
        "config_hash": None,
        "full_config_snapshot": {},
        "condition_label": condition_label,
        "turn_index": turn_index,
        "session_turn_index": turn_index,
        "measurement_session_index": None,
        "is_probe_turn": False,
        "use_session_context": False,
        "failure_reason": None,
    }


# ---------------------------------------------------------------------------
# _check_static_zero_weight_changes
# ---------------------------------------------------------------------------


class TestCheckStaticZeroWeightChanges:
    def test_pass_on_clean_static_control(self):
        """Static control runs with no weight_updates → PASS."""
        with TempDB() as store:
            rec1 = _make_record(experiment_id="exp-static", weight_updates=[], turn_index=1)
            rec2 = _make_record(experiment_id="exp-static", weight_updates=[], turn_index=2)
            async_run(store.insert_run(rec1))
            async_run(store.insert_run(rec2))

            result = async_run(_check_static_zero_weight_changes(store, "exp-static"))
            assert result["status"] == "PASS"
            assert result["violation_count"] == 0
            assert result["violating_run_ids"] == []

    def test_fail_when_run_has_weight_updates(self):
        """Static control with a non-empty weight_updates list → FAIL."""
        with TempDB() as store:
            wu = [{"edge_id": _edge_id(), "old_weight": 0.5, "new_weight": 0.6,
                   "source_mu_id": _mu_id(), "target_mu_id": _mu_id(),
                   "feedback_event_id": _run_id()}]
            bad_rec = _make_record(experiment_id="exp-static", weight_updates=wu)
            async_run(store.insert_run(bad_rec))

            result = async_run(_check_static_zero_weight_changes(store, "exp-static"))
            assert result["status"] == "FAIL"
            assert result["violation_count"] == 1
            assert bad_rec["run_id"] in result["violating_run_ids"]

    def test_fail_counts_multiple_violating_runs(self):
        """Multiple runs with weight_updates → all counted."""
        with TempDB() as store:
            wu = [{"edge_id": _edge_id(), "old_weight": 0.5, "new_weight": 0.6,
                   "source_mu_id": None, "target_mu_id": None,
                   "feedback_event_id": None}]
            for i in range(3):
                rec = _make_record(
                    experiment_id="exp-static", weight_updates=wu, turn_index=i + 1
                )
                async_run(store.insert_run(rec))

            result = async_run(_check_static_zero_weight_changes(store, "exp-static"))
            assert result["status"] == "FAIL"
            assert result["violation_count"] == 3


# ---------------------------------------------------------------------------
# _check_random_weights_rerandomized
# ---------------------------------------------------------------------------


class TestCheckRandomWeightsRerandomized:
    def test_skip_when_no_edges(self):
        """No activated edges → SKIP (can't compute variance)."""
        with TempDB() as store:
            rec = _make_record(
                experiment_id="exp-random",
                activated_edge_ids=[],
                condition_label="random-weight",
            )
            async_run(store.insert_run(rec))

            result = async_run(
                _check_random_weights_rerandomized(store, "exp-random", 0.05)
            )
            assert result["status"] == "SKIP"

    def test_pass_when_variance_above_floor(self):
        """
        Edges with varied weights (std-dev ≈ 0.28 > 0.05 floor) → PASS.
        Weights [0.1, 0.3, 0.5, 0.7, 0.9] have std-dev ≈ 0.283.
        """
        with TempDB() as store:
            src = _mu_id()
            tgt = _mu_id()
            async_run(store.create_mu(src, "user_input", "source content"))
            async_run(store.create_mu(tgt, "agent_output", "target content"))

            weights = [0.1, 0.3, 0.5, 0.7, 0.9]
            edge_ids = []
            for w in weights:
                eid = _edge_id()
                edge_ids.append(eid)
                async_run(
                    store.create_edge(src, tgt, w, "forward", edge_id=eid)
                )

            rec = _make_record(
                experiment_id="exp-random",
                activated_edge_ids=edge_ids,
                condition_label="random-weight",
            )
            async_run(store.insert_run(rec))

            result = async_run(
                _check_random_weights_rerandomized(store, "exp-random", 0.05)
            )
            assert result["status"] == "PASS"
            assert result["weight_std_dev"] > 0.05

    def test_fail_when_all_weights_identical(self):
        """All edges at weight 0.5 → zero variance → FAIL."""
        with TempDB() as store:
            src = _mu_id()
            tgt = _mu_id()
            async_run(store.create_mu(src, "user_input", "source"))
            async_run(store.create_mu(tgt, "agent_output", "target"))

            edge_ids = []
            for _ in range(5):
                eid = _edge_id()
                edge_ids.append(eid)
                async_run(
                    store.create_edge(src, tgt, 0.5, "forward", edge_id=eid)
                )

            rec = _make_record(
                experiment_id="exp-random",
                activated_edge_ids=edge_ids,
                condition_label="random-weight",
            )
            async_run(store.insert_run(rec))

            result = async_run(
                _check_random_weights_rerandomized(store, "exp-random", 0.05)
            )
            assert result["status"] == "FAIL"
            assert result["weight_std_dev"] == 0.0


# ---------------------------------------------------------------------------
# _check_zero_feedback_edge_correlation
# ---------------------------------------------------------------------------


class TestCheckZeroFeedbackEdgeCorrelation:
    def test_pass_on_clean_control(self):
        """Feedback event present but run has no weight_updates → PASS."""
        with TempDB() as store:
            rid = _run_id()
            rec = _make_record(
                run_id=rid,
                experiment_id="exp-ctrl",
                weight_updates=[],
                condition_label="static-weight",
            )
            async_run(store.insert_run(rec))

            # Feedback event for this run — but weight_updates is empty
            fe = {
                "feedback_event_id": str(uuid.uuid4()),
                "run_id": rid,
                "verdict": "thumbs_up",
                "verdict_basis": None,
                "turn_index": 1,
                "is_probe_turn": False,
            }
            async_run(store.insert_feedback_event(fe))

            result = async_run(
                _check_zero_feedback_edge_correlation(store, "exp-ctrl")
            )
            assert result["status"] == "PASS"
            assert result["violation_count"] == 0

    def test_fail_when_feedback_run_has_weight_updates(self):
        """Feedback event AND non-empty weight_updates on the same run → FAIL."""
        with TempDB() as store:
            rid = _run_id()
            wu = [{"edge_id": _edge_id(), "old_weight": 0.5, "new_weight": 0.6,
                   "source_mu_id": None, "target_mu_id": None,
                   "feedback_event_id": None}]
            rec = _make_record(
                run_id=rid,
                experiment_id="exp-ctrl",
                weight_updates=wu,
                condition_label="static-weight",
            )
            async_run(store.insert_run(rec))

            fe = {
                "feedback_event_id": str(uuid.uuid4()),
                "run_id": rid,
                "verdict": "thumbs_up",
                "verdict_basis": None,
                "turn_index": 1,
                "is_probe_turn": False,
            }
            async_run(store.insert_feedback_event(fe))

            result = async_run(
                _check_zero_feedback_edge_correlation(store, "exp-ctrl")
            )
            assert result["status"] == "FAIL"
            assert result["violation_count"] == 1

    def test_pass_when_no_feedback_events(self):
        """No feedback events at all → PASS (nothing to correlate)."""
        with TempDB() as store:
            rec = _make_record(
                experiment_id="exp-ctrl",
                weight_updates=[],
                condition_label="static-weight",
            )
            async_run(store.insert_run(rec))

            result = async_run(
                _check_zero_feedback_edge_correlation(store, "exp-ctrl")
            )
            assert result["status"] == "PASS"
            assert result["violation_count"] == 0


# ---------------------------------------------------------------------------
# _compute_comparative_deltas
# ---------------------------------------------------------------------------


class TestComputeComparativeDeltas:
    def _make_measures(self, mean_abs_delta: float = 0.1) -> dict:
        """Minimal learning_measures dict for delta computation."""
        return {
            "pass_rank_threshold": 3,
            "edge_delta_ledger": {
                "total_updates": 5,
                "increased_count": 5,
                "decreased_count": 0,
                "unchanged_count": 0,
                "mean_abs_delta": mean_abs_delta,
                "orphan_update_count": 0,
                "within_cluster_count": 0,
                "cross_cluster_count": 0,
                "unknown_cluster_count": 5,
            },
            "bridge_mu_analysis": {
                "bridge_probe_count": 0,
                "bridge_probe_results": [],
            },
        }

    def _make_probe_result(self, probe_id: str, session: int, gold_rank=None) -> dict:
        return {
            "probe_id": probe_id,
            "measurement_session_index": session,
            "gold_mu_rank": gold_rank,
            "retrieved_mu_ids": [],
            "condition_label": "learning",
            "outcome_override": None,
        }

    def test_probe_pass_rate_delta_positive_when_learned_passes(self):
        """Learned condition passes; control fails → positive delta."""
        learned_measures = self._make_measures(0.15)
        control_measures = self._make_measures(0.0)

        learned_results = [self._make_probe_result("p1", 1, gold_rank=1)]  # pass
        control_results = [self._make_probe_result("p1", 1, gold_rank=None)]  # fail

        deltas = _compute_comparative_deltas(
            learned_measures=learned_measures,
            control_measures=control_measures,
            probe_results_by_condition={
                "learning": learned_results,
                "static-weight": control_results,
            },
            learned_condition="learning",
            control_condition="static-weight",
        )

        # Learned pass rate = 1.0, control = 0.0 → delta = +1.0
        assert deltas["probe_pass_rate_delta"] == 1.0

    def test_edge_weight_delta_magnitude_computed(self):
        """Mean edge delta magnitude difference computed correctly."""
        learned_measures = self._make_measures(mean_abs_delta=0.18)
        control_measures = self._make_measures(mean_abs_delta=0.0)

        deltas = _compute_comparative_deltas(
            learned_measures=learned_measures,
            control_measures=control_measures,
            probe_results_by_condition={
                "learning": [],
                "static-weight": [],
            },
            learned_condition="learning",
            control_condition="static-weight",
        )

        assert abs(deltas["edge_weight_delta_magnitude"] - 0.18) < 1e-6

    def test_none_delta_when_no_probe_results(self):
        """When both conditions have no probe results, pass-rate delta is None."""
        deltas = _compute_comparative_deltas(
            learned_measures=self._make_measures(),
            control_measures=self._make_measures(),
            probe_results_by_condition={"learning": [], "static-weight": []},
            learned_condition="learning",
            control_condition="static-weight",
        )

        assert deltas["probe_pass_rate_delta"] is None
        assert deltas["mean_gold_mu_rank_delta"] is None

    def test_all_delta_keys_present(self):
        """Returned dict always has all four delta keys."""
        deltas = _compute_comparative_deltas(
            learned_measures=self._make_measures(),
            control_measures=self._make_measures(),
            probe_results_by_condition={"learning": [], "static-weight": []},
            learned_condition="learning",
            control_condition="static-weight",
        )
        assert "probe_pass_rate_delta" in deltas
        assert "bridge_mu_rank_delta" in deltas
        assert "edge_weight_delta_magnitude" in deltas
        assert "mean_gold_mu_rank_delta" in deltas
