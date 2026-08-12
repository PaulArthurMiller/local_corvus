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
  Sample-mode pass-rate comparison (M34): per-probe/per-session deltas,
    by-phenomenon aggregation, graceful degradation on missing sample data.
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
    _collect_probe_results_for_experiment,
    _compute_comparative_deltas,
    _compute_sample_pass_rate_comparison,
    _compute_pin_adjusted_mean_deltas,
    _compute_pinned_probes,
    _compute_trained_subset_comparison,
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

    def test_raw_learned_and_control_values_present(self):
        """M34: the raw learned/control values ride alongside each delta so
        the rendered table can show all three columns instead of hiding the
        baseline behind a bare '—'."""
        learned_measures = self._make_measures(mean_abs_delta=0.18)
        control_measures = self._make_measures(mean_abs_delta=0.0)

        deltas = _compute_comparative_deltas(
            learned_measures=learned_measures,
            control_measures=control_measures,
            probe_results_by_condition={
                "learning": [self._make_probe_result("p1", 1, gold_rank=1)],
                "static-weight": [self._make_probe_result("p1", 1, gold_rank=None)],
            },
            learned_condition="learning",
            control_condition="static-weight",
        )

        assert deltas["probe_pass_rate_learned"] == 1.0
        assert deltas["probe_pass_rate_control"] == 0.0
        assert deltas["edge_weight_delta_magnitude_learned"] == 0.18
        assert deltas["edge_weight_delta_magnitude_control"] == 0.0


# ---------------------------------------------------------------------------
# _compute_sample_pass_rate_comparison — M34 sample-mode comparison
# ---------------------------------------------------------------------------


def _make_sample_pass_rates(probe_sessions: dict[str, list[tuple]]) -> dict:
    """
    probe_sessions: {probe_id: [(session_index, phenomenon_type, pass_rate), ...]}
    Builds a `sample_pass_rates` dict matching
    `_compute_sample_pass_rates()`'s real output shape.
    """
    per_probe = []
    for probe_id, sessions in probe_sessions.items():
        phenomenon = sessions[0][1] if sessions else "unknown"
        per_probe.append({
            "probe_id": probe_id,
            "phenomenon_type": phenomenon,
            "sessions": [
                {
                    "session_index": idx,
                    "pass_count": 0,
                    "fail_count": 0,
                    "repeats_completed": 25,
                    "pass_rate": rate,
                    "partial_battery": False,
                }
                for idx, _, rate in sessions
            ],
        })
    return {"per_probe": per_probe, "by_phenomenon": {}}


class TestComputeSamplePassRateComparison:
    def test_per_session_delta_matches_expected(self):
        """learned - control at each matching session_index."""
        learned = {"sample_pass_rates": _make_sample_pass_rates({
            "vs_d4": [(1, "spoke_retrieval", 0.04), (5, "spoke_retrieval", 0.60)],
        })}
        control = {"sample_pass_rates": _make_sample_pass_rates({
            "vs_d4": [(1, "spoke_retrieval", 0.04), (5, "spoke_retrieval", 0.08)],
        })}

        result = _compute_sample_pass_rate_comparison(learned, control)

        probe = result["per_probe"][0]
        assert probe["probe_id"] == "vs_d4"
        sessions = {s["session_index"]: s for s in probe["sessions"]}
        assert sessions[1]["delta"] == 0.0
        assert sessions[5]["delta"] == 0.52

    def test_aggregated_by_phenomenon(self):
        """Mean and final-session deltas aggregate correctly across probes
        sharing a phenomenon_type."""
        learned = {"sample_pass_rates": _make_sample_pass_rates({
            "vs_c1": [(1, "spoke_retrieval", 1.0), (2, "spoke_retrieval", 1.0)],
            "vs_d4": [(1, "spoke_retrieval", 0.04), (2, "spoke_retrieval", 0.60)],
        })}
        control = {"sample_pass_rates": _make_sample_pass_rates({
            "vs_c1": [(1, "spoke_retrieval", 1.0), (2, "spoke_retrieval", 1.0)],
            "vs_d4": [(1, "spoke_retrieval", 0.04), (2, "spoke_retrieval", 0.08)],
        })}

        result = _compute_sample_pass_rate_comparison(learned, control)

        by_phenom = result["by_phenomenon"]["spoke_retrieval"]
        assert by_phenom["probe_count"] == 2
        # deltas: vs_c1 [0.0, 0.0], vs_d4 [0.0, 0.52] -> mean = 0.13
        assert by_phenom["mean_delta"] == 0.13
        # final-session deltas: vs_c1 0.0, vs_d4 0.52 -> mean = 0.26
        assert by_phenom["final_session_mean_delta"] == 0.26

    def test_missing_sample_data_degrades_without_crashing(self):
        """Pre-M30 experiments have no sample_pass_rates at all — the
        comparison must degrade to empty, not crash."""
        learned = {"sample_pass_rates": {"per_probe": [], "by_phenomenon": {}}}
        control = {}  # no sample_pass_rates key whatsoever

        result = _compute_sample_pass_rate_comparison(learned, control)

        assert result == {"per_probe": [], "by_phenomenon": {}}

    def test_probe_only_in_one_condition_is_excluded(self):
        """A probe present in learned but absent from control (or vice
        versa) can't be compared — it must not appear in the output."""
        learned = {"sample_pass_rates": _make_sample_pass_rates({
            "vs_d4": [(1, "spoke_retrieval", 0.04)],
        })}
        control = {"sample_pass_rates": _make_sample_pass_rates({
            "vs_c1": [(1, "spoke_retrieval", 1.0)],
        })}

        result = _compute_sample_pass_rate_comparison(learned, control)

        assert result["per_probe"] == []
        assert result["by_phenomenon"] == {}

    def test_none_pass_rate_produces_none_delta(self):
        """A session with no non-flagged repeats (pass_rate=None) yields a
        None delta rather than a crash, and is excluded from aggregation."""
        learned = {"sample_pass_rates": _make_sample_pass_rates({
            "vs_x1": [(1, "phenom", None)],
        })}
        control = {"sample_pass_rates": _make_sample_pass_rates({
            "vs_x1": [(1, "phenom", 0.5)],
        })}

        result = _compute_sample_pass_rate_comparison(learned, control)

        sessions = result["per_probe"][0]["sessions"]
        assert sessions[0]["delta"] is None
        assert result["by_phenomenon"]["phenom"]["mean_delta"] is None


# ---------------------------------------------------------------------------
# _collect_probe_results_for_experiment — §6.2 experiment_id scoping
# ---------------------------------------------------------------------------


def _insert_probe_row(store, probe_id, session_idx, condition_label,
                      experiment_id=None, gold_mu_rank=None):
    async_run(store.insert_probe_result({
        "result_id": str(uuid.uuid4()),
        "probe_id": probe_id,
        "run_id": None,
        "measurement_session_index": session_idx,
        "condition_label": condition_label,
        "retrieved_mu_ids": [],
        "gold_mu_rank": gold_mu_rank,
        "retrieval_precision": None,
        "retrieval_recall": None,
        "bm25_scores_per_mu": {},
        "experiment_id": experiment_id,
    }))


_PROBE_DEF = {
    "probe_id": "p01", "probe_set_version": "v2",
    "phenomenon_type": "ranking_improvement", "prompt_text": "test probe",
    "gold_mu_ids": ["mu-gold"], "is_bridge_probe": False, "bridge_pair_id": None,
}


class TestCollectProbeResultsForExperiment:
    def test_scoped_to_experiment_id_not_condition_label(self):
        """Rows from another attempt sharing the condition_label are excluded —
        comparative deltas must only see this experiment's measurements."""
        with TempDB() as store:
            async_run(store.sync_probes([_PROBE_DEF]))
            _insert_probe_row(store, "p01", 1, "learned",
                              experiment_id="plan1_learned", gold_mu_rank=1)
            # Same condition_label, different attempt — must not appear.
            _insert_probe_row(store, "p01", 1, "learned",
                              experiment_id="plan1_attempt2_learned", gold_mu_rank=9)

            all_probes = async_run(store.get_all_probes())
            results = async_run(_collect_probe_results_for_experiment(
                store, all_probes, "plan1_learned", "learned"
            ))

            print(f"scoped results: {[(r['experiment_id'], r['gold_mu_rank']) for r in results]}")
            assert len(results) == 1
            assert results[0]["experiment_id"] == "plan1_learned"
            assert results[0]["gold_mu_rank"] == 1

    def test_falls_back_to_condition_label_for_legacy_rows(self):
        """Pre-M21 rows (experiment_id=NULL) are still found via the
        condition_label fallback when no experiment-stamped rows exist."""
        with TempDB() as store:
            async_run(store.sync_probes([_PROBE_DEF]))
            _insert_probe_row(store, "p01", 1, "learning", gold_mu_rank=2)

            all_probes = async_run(store.get_all_probes())
            results = async_run(_collect_probe_results_for_experiment(
                store, all_probes, "legacy_experiment", "learning"
            ))

            print(f"legacy fallback results: {len(results)}")
            assert len(results) == 1
            assert results[0]["gold_mu_rank"] == 2


# ---------------------------------------------------------------------------
# _compute_trained_subset_comparison — M39 §2 trained-subset comparison
# ---------------------------------------------------------------------------


class TestComputeTrainedSubsetComparison:
    def _make_comparison(self, per_probe: list[dict]) -> dict:
        return {"per_probe": per_probe, "by_phenomenon": {}}

    def test_restricts_to_trained_probes_only(self):
        cmp = self._make_comparison([
            {"probe_id": "vs_d5", "phenomenon_type": "spoke_retrieval",
             "sessions": [{"session_index": 0, "delta": -0.5}, {"session_index": 4, "delta": -0.94}]},
            {"probe_id": "vs_d6", "phenomenon_type": "spoke_retrieval",
             "sessions": [{"session_index": 0, "delta": 0.38}, {"session_index": 4, "delta": 0.72}]},
        ])
        result = _compute_trained_subset_comparison(cmp, trained_probe_ids={"vs_d5"})

        assert result["matched_probe_count"] == 1
        assert [p["probe_id"] for p in result["per_probe"]] == ["vs_d5"]
        assert result["final_session_mean_delta"] == -0.94

    def test_mean_delta_by_session_averages_across_trained_probes(self):
        cmp = self._make_comparison([
            {"probe_id": "vs_a1", "phenomenon_type": "spoke_retrieval",
             "sessions": [{"session_index": 0, "delta": -1.0}]},
            {"probe_id": "vs_d5", "phenomenon_type": "spoke_retrieval",
             "sessions": [{"session_index": 0, "delta": -0.5}]},
        ])
        result = _compute_trained_subset_comparison(cmp, trained_probe_ids={"vs_a1", "vs_d5"})

        assert result["mean_delta_by_session"] == [
            {"session_index": 0, "mean_delta": -0.75, "probe_count": 2}
        ]

    def test_empty_trained_probe_ids_degrades_without_crashing(self):
        """No trained_spokes supplied to generate_experiment_report — the
        endpoint must render '—', never crash."""
        cmp = self._make_comparison([
            {"probe_id": "vs_a1", "phenomenon_type": "spoke_retrieval",
             "sessions": [{"session_index": 0, "delta": -1.0}]},
        ])
        result = _compute_trained_subset_comparison(cmp, trained_probe_ids=set())

        assert result["trained_probe_count"] == 0
        assert result["matched_probe_count"] == 0
        assert result["per_probe"] == []
        assert result["mean_delta_by_session"] == []
        assert result["final_session_mean_delta"] is None

    def test_none_deltas_excluded_from_the_mean(self):
        cmp = self._make_comparison([
            {"probe_id": "vs_d5", "phenomenon_type": "spoke_retrieval",
             "sessions": [{"session_index": 0, "delta": None}]},
        ])
        result = _compute_trained_subset_comparison(cmp, trained_probe_ids={"vs_d5"})
        assert result["mean_delta_by_session"] == []
        assert result["final_session_mean_delta"] is None

    def test_trained_spoke_with_no_matching_probe_counted_but_excluded_from_means(self):
        """M39 behavior, re-asserted post-M41: a trained_probe_id with no
        sample-mode data stays in trained_probe_count but never enters
        matched_probe_count or either mean (raw or excluding-pinned)."""
        cmp = self._make_comparison([
            {"probe_id": "vs_d5", "phenomenon_type": "spoke_retrieval",
             "sessions": [{"session_index": 0, "delta": 0.5}]},
        ])
        # vs_z9 is "trained" but has no probe results at all.
        result = _compute_trained_subset_comparison(
            cmp, trained_probe_ids={"vs_d5", "vs_z9"}, pinned_probe_ids=set()
        )
        assert result["trained_probe_count"] == 2
        assert result["matched_probe_count"] == 1
        assert result["matched_probe_count_excluding_pinned"] == 1
        assert [p["probe_id"] for p in result["per_probe"]] == ["vs_d5"]

    # -- M41: pin-adjustment --------------------------------------------

    def test_no_trained_probe_pinned_adjusted_equals_raw_exactly(self):
        """Scope §9: with nothing pinned, the excluding-pinned output must
        equal the raw output exactly (assert equality, not closeness) --
        this is what catches two divergent mean definitions."""
        cmp = self._make_comparison([
            {"probe_id": "vs_b3", "phenomenon_type": "spoke_retrieval",
             "sessions": [{"session_index": 0, "delta": 0.2}, {"session_index": 4, "delta": 0.76}]},
            {"probe_id": "vs_d5", "phenomenon_type": "spoke_retrieval",
             "sessions": [{"session_index": 0, "delta": 0.1}, {"session_index": 4, "delta": 0.68}]},
        ])
        result = _compute_trained_subset_comparison(
            cmp, trained_probe_ids={"vs_b3", "vs_d5"}, pinned_probe_ids=set()
        )
        assert result["pinned_trained_probe_ids"] == []
        assert result["matched_probe_count_excluding_pinned"] == result["matched_probe_count"]
        assert result["mean_delta_by_session_excluding_pinned"] == result["mean_delta_by_session"]
        assert result["final_session_mean_delta_excluding_pinned"] == result["final_session_mean_delta"]

    def test_one_of_two_trained_probes_pinned_adjusted_mean_is_survivor(self):
        """The rep3-shaped case: two trained probes, one control-pinned --
        the excluding-pinned mean is exactly the surviving probe's delta."""
        cmp = self._make_comparison([
            {"probe_id": "vs_a1", "phenomenon_type": "spoke_retrieval",
             "sessions": [{"session_index": 4, "delta": -0.96}]},
            {"probe_id": "vs_b3", "phenomenon_type": "spoke_retrieval",
             "sessions": [{"session_index": 4, "delta": 0.76}]},
        ])
        result = _compute_trained_subset_comparison(
            cmp, trained_probe_ids={"vs_a1", "vs_b3"}, pinned_probe_ids={"vs_a1"}
        )
        assert result["pinned_trained_probe_ids"] == ["vs_a1"]
        assert result["matched_probe_count_excluding_pinned"] == 1
        assert result["final_session_mean_delta_excluding_pinned"] == 0.76
        # raw mean is unchanged and still averages both probes
        assert result["final_session_mean_delta"] == -0.1

    def test_all_trained_probes_pinned_yields_none_never_zero(self):
        """The all-pinned hazard: None + count 0, never 0.0 (0.0 would read
        as 'no effect,' indistinguishable from a real null), no crash."""
        cmp = self._make_comparison([
            {"probe_id": "vs_a1", "phenomenon_type": "spoke_retrieval",
             "sessions": [{"session_index": 4, "delta": -0.96}]},
            {"probe_id": "vs_c1", "phenomenon_type": "spoke_retrieval",
             "sessions": [{"session_index": 4, "delta": -0.96}]},
        ])
        result = _compute_trained_subset_comparison(
            cmp, trained_probe_ids={"vs_a1", "vs_c1"}, pinned_probe_ids={"vs_a1", "vs_c1"}
        )
        assert result["pinned_trained_probe_ids"] == ["vs_a1", "vs_c1"]
        assert result["matched_probe_count_excluding_pinned"] == 0
        assert result["mean_delta_by_session_excluding_pinned"] == []
        assert result["final_session_mean_delta_excluding_pinned"] is None
        # raw mean is untouched by the all-pinned condition
        assert result["final_session_mean_delta"] == -0.96

    def test_same_probe_pinned_under_one_control_not_the_other(self):
        """exp0b's vs_d5-shaped case: pinned status is control-specific --
        the same per_probe input produces different adjusted deltas
        depending on which control's pinned set is supplied."""
        cmp = self._make_comparison([
            {"probe_id": "vs_d5", "phenomenon_type": "spoke_retrieval",
             "sessions": [{"session_index": 4, "delta": 0.68}]},
            {"probe_id": "vs_b3", "phenomenon_type": "spoke_retrieval",
             "sessions": [{"session_index": 4, "delta": 0.76}]},
        ])
        under_static = _compute_trained_subset_comparison(
            cmp, trained_probe_ids={"vs_d5", "vs_b3"}, pinned_probe_ids=set()
        )
        under_random = _compute_trained_subset_comparison(
            cmp, trained_probe_ids={"vs_d5", "vs_b3"}, pinned_probe_ids={"vs_d5"}
        )
        assert under_static["final_session_mean_delta_excluding_pinned"] == 0.72
        assert under_random["final_session_mean_delta_excluding_pinned"] == 0.76
        assert under_static["final_session_mean_delta_excluding_pinned"] != (
            under_random["final_session_mean_delta_excluding_pinned"]
        )

    def test_missing_session_gives_each_row_its_own_denominator(self):
        """A trained probe missing one session must not distort that
        session's mean -- probe_count differs per session row, both raw and
        excluding-pinned."""
        cmp = self._make_comparison([
            {"probe_id": "vs_b3", "phenomenon_type": "spoke_retrieval",
             "sessions": [
                 {"session_index": 0, "delta": 0.2},
                 {"session_index": 4, "delta": 0.76},
             ]},
            {"probe_id": "vs_d5", "phenomenon_type": "spoke_retrieval",
             # vs_d5 has no session_index=0 result at all.
             "sessions": [{"session_index": 4, "delta": 0.68}]},
        ])
        result = _compute_trained_subset_comparison(
            cmp, trained_probe_ids={"vs_b3", "vs_d5"}, pinned_probe_ids=set()
        )
        by_session = {row["session_index"]: row for row in result["mean_delta_by_session"]}
        assert by_session[0]["probe_count"] == 1
        assert by_session[0]["mean_delta"] == 0.2
        assert by_session[4]["probe_count"] == 2
        assert by_session[4]["mean_delta"] == 0.72

    def test_no_pinned_probe_ids_argument_defaults_to_unpinned(self):
        """Every pre-M41 call site omits pinned_probe_ids -- the new keys
        must still appear with sane empty-pin values, not raise."""
        cmp = self._make_comparison([
            {"probe_id": "vs_d5", "phenomenon_type": "spoke_retrieval",
             "sessions": [{"session_index": 0, "delta": 0.5}]},
        ])
        result = _compute_trained_subset_comparison(cmp, trained_probe_ids={"vs_d5"})
        assert result["pinned_trained_probe_ids"] == []
        assert result["matched_probe_count_excluding_pinned"] == 1
        assert result["final_session_mean_delta_excluding_pinned"] == 0.5


# ---------------------------------------------------------------------------
# _compute_pinned_probes — M39 §4 control-pinned classification
# ---------------------------------------------------------------------------


class TestComputePinnedProbes:
    def _spr(self, probe_sessions: dict) -> dict:
        per_probe = []
        for probe_id, rates in probe_sessions.items():
            per_probe.append({
                "probe_id": probe_id,
                "sessions": [{"session_index": i, "pass_rate": r} for i, r in enumerate(rates)],
            })
        return {"per_probe": per_probe}

    def test_pinned_when_every_session_is_1_0(self):
        spr = self._spr({"vs_a1": [1.0, 1.0, 1.0]})
        assert _compute_pinned_probes(spr) == {"vs_a1"}

    def test_not_pinned_when_only_one_session_hits_1_0(self):
        """vs_b1-shaped case: a single 1.0 session must not qualify."""
        spr = self._spr({"vs_b1": [0.2, 1.0, 0.4]})
        assert _compute_pinned_probes(spr) == set()

    def test_not_pinned_when_no_sessions(self):
        spr = self._spr({"vs_x1": []})
        assert _compute_pinned_probes(spr) == set()

    def test_multiple_pinned_probes_all_caught(self):
        """exp0b-shaped case: vs_a1/vs_c1/vs_d1 pinned under static_weight."""
        spr = self._spr({
            "vs_a1": [1.0, 1.0], "vs_c1": [1.0, 1.0], "vs_d1": [1.0, 1.0],
            "vs_b1": [1.0, 0.2],
        })
        assert _compute_pinned_probes(spr) == {"vs_a1", "vs_c1", "vs_d1"}


# ---------------------------------------------------------------------------
# _compute_pin_adjusted_mean_deltas — M39 §4
# ---------------------------------------------------------------------------


class TestComputePinAdjustedMeanDeltas:
    def _per_probe(self) -> list[dict]:
        return [
            {"probe_id": "vs_a1", "phenomenon_type": "spoke_retrieval",
             "sessions": [{"session_index": 0, "delta": -1.0}, {"session_index": 1, "delta": -0.88}]},
            {"probe_id": "vs_d6", "phenomenon_type": "spoke_retrieval",
             "sessions": [{"session_index": 0, "delta": 0.38}, {"session_index": 1, "delta": 0.72}]},
        ]

    def test_all_probes_mean_is_unchanged_from_the_unrestricted_comparison(self):
        """M34's rule: the existing all-probe mean is never silently
        redefined — it must be the exact by_phenomenon dict passed in."""
        from analytics.reports.control_shadow import _aggregate_deltas_by_phenomenon
        per_probe = self._per_probe()
        cmp = {"per_probe": per_probe, "by_phenomenon": _aggregate_deltas_by_phenomenon(per_probe)}

        result = _compute_pin_adjusted_mean_deltas(cmp, pinned_probe_ids=set())
        assert result["by_phenomenon_all_probes"] == cmp["by_phenomenon"]

    def test_excluding_pinned_removes_pinned_probes_from_the_mean(self):
        per_probe = self._per_probe()
        from analytics.reports.control_shadow import _aggregate_deltas_by_phenomenon
        cmp = {"per_probe": per_probe, "by_phenomenon": _aggregate_deltas_by_phenomenon(per_probe)}

        result = _compute_pin_adjusted_mean_deltas(cmp, pinned_probe_ids={"vs_a1"})

        # excluding vs_a1 leaves only vs_d6: mean = (0.38+0.72)/2 = 0.55
        excl = result["by_phenomenon_excluding_pinned"]["spoke_retrieval"]
        assert excl["mean_delta"] == 0.55
        assert excl["probe_count"] == 1
        assert result["pinned_probe_ids"] == ["vs_a1"]

    def test_all_pinned_yields_empty_excluding_pinned_mean(self):
        per_probe = self._per_probe()
        from analytics.reports.control_shadow import _aggregate_deltas_by_phenomenon
        cmp = {"per_probe": per_probe, "by_phenomenon": _aggregate_deltas_by_phenomenon(per_probe)}

        result = _compute_pin_adjusted_mean_deltas(cmp, pinned_probe_ids={"vs_a1", "vs_d6"})

        assert result["by_phenomenon_excluding_pinned"] == {}
