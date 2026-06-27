"""
Tests for analytics/reports/experiment_report.py — §5 aggregation + §6.3 + §6.4.

Covers:
  §5 aggregation: total failure count is sum of per-series counts
  §5 aggregation: mean intra-series rate correct for synthetic multi-series data
  §6.3 seed-fixture provenance: PASS on one registered fixture version
  §6.3 seed-fixture provenance: FAIL when a fabricated MU has a mismatched series_id
  §6.3 graph-wipe: PASS on clean data
  §6.3 graph-wipe: FAIL when a pre-experiment MU from a different experiment_id exists
  §6.3 probe-replay-index: PASS on contiguous indices
  §6.3 probe-replay-index: FAIL on gap (e.g., 1, 2, 4 → gap at 3)
  §6.4 statistical caveat text appears verbatim in Markdown output
  JSON output has learning_measures=null and control_shadow=null placeholder keys
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
from analytics.reports.experiment_report import (
    STATISTICAL_CAVEAT_TEXT,
    _aggregate_failures,
    _aggregate_retrieval_rates,
    _check_graph_wipe,
    _check_probe_replay_index,
    _check_seed_provenance,
    generate_experiment_report,
)
from analytics.reports.markdown_renderer import render_experiment_report_markdown


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def run(coro):
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


def _run_id() -> str:
    return str(uuid.uuid4())


def _mu_id() -> str:
    return str(uuid.uuid4())


def _make_minimal_run(
    run_id: str = None,
    series_id: str = "series-A",
    experiment_id: str = "exp-1",
    turn_index: int = 1,
    failure_reason: str = None,
    activated_mu_ids: list = None,
    activated_edge_ids: list = None,
    tokens_in: int = 100,
    weight_updates: list = None,
    condition_label: str = "learning",
) -> dict:
    """Build a minimal run record dict for store.insert_run()."""
    return {
        "run_id": run_id or _run_id(),
        "series_id": series_id,
        "experiment_id": experiment_id,
        "timestamp": f"2026-01-01T00:{turn_index:02d}:00+00:00",
        "input_mu_id": _mu_id(),
        "output_mu_id": _mu_id(),
        "tool_calls": [],
        "activated_mu_ids": activated_mu_ids or [],
        "activated_edge_ids": activated_edge_ids or [],
        "prompt_mu_id": _mu_id(),
        "tokens_in": tokens_in,
        "tokens_out": 50,
        "latency_ms": 200,
        "feedback_signal": None,
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
        "failure_reason": failure_reason,
    }


_CONFIG = {
    "model": {"context_window_tokens": 1000},
    "reporting": {"mode3_min_expected_length": 100},
    "experiment": {"condition_label": "learning"},
}


# ---------------------------------------------------------------------------
# §5 aggregation — failure summary
# ---------------------------------------------------------------------------


def test_aggregate_failures_sums_per_series():
    """Total failure count is the sum of per-series counts."""
    series_data = {
        "series-A": {
            "sections": {
                "s5_1": {"failed_turn_count": 2, "ghost_mu_count": 1, "ghost_incidents": [], "failed_turns": []},
                "s5_3": {"intra_series_rate": 0.8, "total_retrieved": 10, "intra_series_count": 8,
                          "seed_fixture_count": 0, "cross_series_count": 2, "unknown_count": 0},
                "s5_5": {"activated_edge_count": 5, "deltas": [], "zero_delta_high_traversal_flags": []},
                "s5_6": {"status": "PASS", "violation_count": 0, "violations": []},
                "s5_7": {"mode3_mu_count": 1, "anomaly_count": 0},
                "s5_8": {"pressure_turn_count": 0, "context_window_tokens": 1000},
            },
            "run_count": 5,
        },
        "series-B": {
            "sections": {
                "s5_1": {"failed_turn_count": 3, "ghost_mu_count": 0, "ghost_incidents": [], "failed_turns": []},
                "s5_3": {"intra_series_rate": 0.6, "total_retrieved": 10, "intra_series_count": 6,
                          "seed_fixture_count": 0, "cross_series_count": 4, "unknown_count": 0},
                "s5_5": {"activated_edge_count": 3, "deltas": [], "zero_delta_high_traversal_flags": []},
                "s5_6": {"status": "PASS", "violation_count": 0, "violations": []},
                "s5_7": {"mode3_mu_count": 0, "anomaly_count": 0},
                "s5_8": {"pressure_turn_count": 1, "context_window_tokens": 1000},
            },
            "run_count": 6,
        },
    }

    result = _aggregate_failures(series_data)
    print(f"total_failed={result['total_failed_turn_count']} per_series={result['per_series']}")
    assert result["total_failed_turn_count"] == 5  # 2 + 3
    assert result["total_ghost_mu_incidents"] == 1
    assert len(result["per_series"]) == 2


# ---------------------------------------------------------------------------
# §5 aggregation — retrieval rate
# ---------------------------------------------------------------------------


def test_aggregate_retrieval_rates_mean_and_range():
    """Mean intra-series rate and range are computed correctly."""
    series_data = {
        "series-A": {
            "sections": {
                "s5_1": {"failed_turn_count": 0, "ghost_mu_count": 0, "ghost_incidents": [], "failed_turns": []},
                "s5_3": {
                    "intra_series_rate": 0.8,
                    "total_retrieved": 10,
                    "intra_series_count": 8,
                    "seed_fixture_count": 1,
                    "cross_series_count": 1,
                    "unknown_count": 0,
                },
                "s5_5": {"activated_edge_count": 0, "deltas": [], "zero_delta_high_traversal_flags": []},
                "s5_6": {"status": "PASS", "violation_count": 0, "violations": []},
                "s5_7": {"mode3_mu_count": 0, "anomaly_count": 0},
                "s5_8": {"pressure_turn_count": 0, "context_window_tokens": 1000},
            },
            "run_count": 5,
        },
        "series-B": {
            "sections": {
                "s5_1": {"failed_turn_count": 0, "ghost_mu_count": 0, "ghost_incidents": [], "failed_turns": []},
                "s5_3": {
                    "intra_series_rate": 0.4,
                    "total_retrieved": 10,
                    "intra_series_count": 4,
                    "seed_fixture_count": 3,
                    "cross_series_count": 3,
                    "unknown_count": 0,
                },
                "s5_5": {"activated_edge_count": 0, "deltas": [], "zero_delta_high_traversal_flags": []},
                "s5_6": {"status": "PASS", "violation_count": 0, "violations": []},
                "s5_7": {"mode3_mu_count": 0, "anomaly_count": 0},
                "s5_8": {"pressure_turn_count": 0, "context_window_tokens": 1000},
            },
            "run_count": 5,
        },
    }

    result = _aggregate_retrieval_rates(series_data)
    print(f"mean={result['mean_intra_series_rate']} min={result['min_intra_series_rate']} max={result['max_intra_series_rate']}")
    assert abs(result["mean_intra_series_rate"] - 0.6) < 0.001  # (0.8 + 0.4) / 2
    assert abs(result["min_intra_series_rate"] - 0.4) < 0.001
    assert abs(result["max_intra_series_rate"] - 0.8) < 0.001
    # series-B at 0.4 is 0.2 below the mean (0.6) → should be flagged as outlier
    assert "series-B" in result["outlier_series"]
    # series-A at 0.8 is 0.2 above the mean → should also be flagged
    assert "series-A" in result["outlier_series"]


# ---------------------------------------------------------------------------
# §6.3 seed-fixture provenance
# ---------------------------------------------------------------------------


def test_seed_provenance_pass_one_registered_version():
    """Provenance check passes when one fixture version is registered and matches."""
    with TempDB() as store:
        # Insert a seed MU with series_id="seed:v1"
        run(store.create_mu(
            mu_id=_mu_id(),
            source_type="user_input",
            raw_content="seed content",
            series_id="seed:v1",
        ))

        # Register the fixture manifest for v1
        run(store.insert_seed_fixture_manifest({
            "fixture_version": "v1",
            "manifest_hash": "abc123",
            "mu_count": 1,
            "type_distribution": {"user_input": 1},
            "generation_date": "2026-01-01",
            "generation_record": [],
        }))

        result = run(_check_seed_provenance("exp-1", store))
        print(f"seed_provenance: {result}")
        assert result["status"] == "PASS"
        assert "v1" in result["fixture_versions_found"]
        assert not result["unregistered_versions"]
        assert not result["hash_mismatch"]


def test_seed_provenance_fail_unregistered_version():
    """Provenance check fails when a seed MU's version has no registered manifest."""
    with TempDB() as store:
        # Insert a seed MU with series_id="seed:v99" — no manifest registered
        run(store.create_mu(
            mu_id=_mu_id(),
            source_type="user_input",
            raw_content="seed content",
            series_id="seed:v99",
        ))

        result = run(_check_seed_provenance("exp-1", store))
        print(f"seed_provenance: {result}")
        assert result["status"] == "FAIL"
        assert "v99" in result["unregistered_versions"]


# ---------------------------------------------------------------------------
# §6.3 graph-wipe verification
# ---------------------------------------------------------------------------


def test_graph_wipe_pass_clean_data():
    """Graph-wipe check passes when no foreign experiment MUs predate this experiment."""
    with TempDB() as store:
        experiment_id = "exp-clean"
        run_id = _run_id()

        # Insert one run for the current experiment
        run(store.insert_run(_make_minimal_run(
            run_id=run_id,
            series_id="series-A",
            experiment_id=experiment_id,
            turn_index=1,
        )))

        result = run(_check_graph_wipe(experiment_id, store))
        print(f"graph_wipe: {result}")
        assert result["status"] == "PASS"
        assert result["pre_experiment_foreign_mu_count"] == 0


def test_graph_wipe_fail_foreign_mu_predates_experiment():
    """Graph-wipe check fires when a pre-experiment MU from a different experiment exists."""
    with TempDB() as store:
        experiment_id = "exp-new"
        other_experiment_id = "exp-old"

        # Insert a run for the NEW experiment (timestamp later)
        run(store.insert_run(_make_minimal_run(
            series_id="series-A",
            experiment_id=experiment_id,
            turn_index=1,
        )))

        # Insert a MU from a DIFFERENT experiment with an EARLIER timestamp
        # Memory units have their own created_at — we insert directly via create_mu
        # then manually update the created_at to be before the experiment's earliest run
        foreign_mu_id = _mu_id()
        import aiosqlite
        from pathlib import Path

        async def _insert_foreign_mu():
            db = await aiosqlite.connect(store_module._DB_PATH)
            try:
                await db.execute(
                    """INSERT INTO memory_units
                       (mu_id, created_at, source_type, raw_content, experiment_id)
                       VALUES (?, ?, ?, ?, ?)""",
                    (foreign_mu_id, "2025-12-31T00:00:00+00:00",
                     "user_input", "foreign mu content", other_experiment_id),
                )
                await db.commit()
            finally:
                await db.close()

        run(_insert_foreign_mu())

        result = run(_check_graph_wipe(experiment_id, store))
        print(f"graph_wipe: {result}")
        assert result["status"] == "FAIL"
        assert result["pre_experiment_foreign_mu_count"] == 1


def test_graph_wipe_skip_no_runs():
    """Graph-wipe check returns SKIP when no runs exist for the experiment."""
    with TempDB() as store:
        result = run(_check_graph_wipe("nonexistent-experiment", store))
        print(f"graph_wipe: {result}")
        assert result["status"] == "SKIP"


# ---------------------------------------------------------------------------
# §6.3 probe-replay-index normalization
# ---------------------------------------------------------------------------


def test_probe_replay_index_pass_contiguous():
    """Probe-replay-index check passes for contiguous indices (1, 2, 3)."""
    with TempDB() as store:
        experiment_id = "exp-probes"
        condition_label = "learning"

        # Insert a run so get_condition_label_for_experiment returns something
        run(store.insert_run(_make_minimal_run(
            series_id="series-A",
            experiment_id=experiment_id,
            condition_label=condition_label,
            turn_index=1,
        )))

        # Insert a probe
        run(store.sync_probes([{
            "probe_id": "p01",
            "probe_set_version": "v1",
            "phenomenon_type": "ranking_improvement",
            "prompt_text": "test probe",
            "gold_mu_ids": [],
            "is_bridge_probe": False,
            "bridge_pair_id": None,
        }]))

        # Insert probe results for sessions 1, 2, 3
        for session_idx in [1, 2, 3]:
            run(store.insert_probe_result({
                "result_id": str(uuid.uuid4()),
                "probe_id": "p01",
                "run_id": None,
                "measurement_session_index": session_idx,
                "condition_label": condition_label,
                "retrieved_mu_ids": [],
                "gold_mu_rank": None,
                "retrieval_precision": None,
                "retrieval_recall": None,
                "bm25_scores_per_mu": {},
            }))

        result = run(_check_probe_replay_index(experiment_id, store))
        print(f"probe_replay: {result}")
        assert result["status"] == "PASS"
        assert result["gaps"] == []
        assert result["session_indices_found"] == [1, 2, 3]


def test_probe_replay_index_fail_with_gap():
    """Probe-replay-index check fails when there's a gap (1, 2, 4 — missing 3)."""
    with TempDB() as store:
        experiment_id = "exp-gap"
        condition_label = "learning"

        # Insert a run so condition_label is found
        run(store.insert_run(_make_minimal_run(
            series_id="series-A",
            experiment_id=experiment_id,
            condition_label=condition_label,
            turn_index=1,
        )))

        # Insert a probe
        run(store.sync_probes([{
            "probe_id": "p01",
            "probe_set_version": "v1",
            "phenomenon_type": "ranking_improvement",
            "prompt_text": "test probe",
            "gold_mu_ids": [],
            "is_bridge_probe": False,
            "bridge_pair_id": None,
        }]))

        # Sessions 1, 2, 4 — gap at 3
        for session_idx in [1, 2, 4]:
            run(store.insert_probe_result({
                "result_id": str(uuid.uuid4()),
                "probe_id": "p01",
                "run_id": None,
                "measurement_session_index": session_idx,
                "condition_label": condition_label,
                "retrieved_mu_ids": [],
                "gold_mu_rank": None,
                "retrieval_precision": None,
                "retrieval_recall": None,
                "bm25_scores_per_mu": {},
            }))

        result = run(_check_probe_replay_index(experiment_id, store))
        print(f"probe_replay: {result}")
        assert result["status"] == "FAIL"
        assert 3 in result["gaps"]
        assert result["session_indices_found"] == [1, 2, 4]


# ---------------------------------------------------------------------------
# §6.4 statistical caveat
# ---------------------------------------------------------------------------


def test_statistical_caveat_in_markdown():
    """§6.4 statistical caveat text appears verbatim in the rendered Markdown."""
    minimal_report = {
        "report_id": "r1",
        "report_type": "experiment",
        "experiment_id": "exp-1",
        "condition_label": "learning",
        "generated_at": "2026-01-01T00:00:00+00:00",
        "series_summary": {
            "series_ids": [],
            "series_count": 0,
            "total_run_count": 0,
            "s5_1_failure_aggregation": {
                "total_failed_turn_count": 0,
                "total_ghost_mu_incidents": 0,
                "per_series": [],
                "all_ghost_incidents": [],
            },
            "s5_3_retrieval_rate_aggregation": {
                "mean_intra_series_rate": 0.0,
                "min_intra_series_rate": 0.0,
                "max_intra_series_rate": 0.0,
                "outlier_series": [],
                "outlier_threshold": 0.15,
                "per_series": [],
            },
            "s5_5_edge_weight_aggregation": {
                "total_activated_edge_count": 0,
                "edges_increased": 0,
                "edges_decreased": 0,
                "edges_unchanged": 0,
                "mean_abs_delta": 0.0,
                "per_series": [],
            },
            "s5_6_probe_integrity_aggregation": {
                "status": "PASS",
                "total_violations": 0,
                "hard_fail_series": [],
            },
            "s5_7_mode3_aggregation": {
                "total_mode3_mu_count": 0,
                "increasing_pressure_flag": False,
                "per_series": [],
            },
            "s5_8_ctx_pressure_aggregation": {
                "increasing_pressure_flag": False,
                "per_series": [],
            },
        },
        "integrity_checks": {
            "overall_pass": True,
            "seed_fixture_provenance": {"status": "PASS", "fixture_versions_found": [], "unregistered_versions": [], "hash_mismatch": False},
            "graph_wipe_verification": {"status": "SKIP", "pre_experiment_foreign_mu_count": 0, "earliest_run_timestamp": None},
            "probe_replay_index_normalization": {"status": "PASS", "condition_label": None, "session_indices_found": [], "gaps": []},
        },
        "learning_measures": None,
        "control_shadow": None,
    }

    md = render_experiment_report_markdown(minimal_report)
    print(f"Markdown length: {len(md)}")
    # The caveat text key lines must appear verbatim in the output
    assert "§6.4 Statistical Note" in md
    assert "directional signals for hypothesis generation" in md
    assert "statistical significance the sample size cannot support" in md


# ---------------------------------------------------------------------------
# JSON structure — placeholder keys
# ---------------------------------------------------------------------------


def test_json_has_learning_measures_populated():
    """The generated report JSON has learning_measures populated (not null) by M15."""
    with TempDB() as store:
        with mock.patch(
            "analytics.reports.experiment_report._load_config", return_value=_CONFIG
        ):
            report = run(generate_experiment_report("exp-empty", store))

    assert "learning_measures" in report
    # M15 now fills learning_measures — it is a dict, not None
    assert report["learning_measures"] is not None
    assert isinstance(report["learning_measures"], dict)
    assert "state_transitions" in report["learning_measures"]
    assert "edge_delta_ledger" in report["learning_measures"]


def test_json_has_control_shadow_null():
    """The generated report JSON has control_shadow=null."""
    with TempDB() as store:
        with mock.patch(
            "analytics.reports.experiment_report._load_config", return_value=_CONFIG
        ):
            report = run(generate_experiment_report("exp-empty", store))

    assert "control_shadow" in report
    assert report["control_shadow"] is None


def test_json_structure_complete():
    """The generated report JSON has all required top-level keys."""
    with TempDB() as store:
        with mock.patch(
            "analytics.reports.experiment_report._load_config", return_value=_CONFIG
        ):
            report = run(generate_experiment_report("exp-structure", store))

    required_keys = {
        "report_id",
        "report_type",
        "experiment_id",
        "condition_label",
        "generated_at",
        "series_summary",
        "integrity_checks",
        "learning_measures",
        "control_shadow",
    }
    missing = required_keys - set(report.keys())
    print(f"missing keys: {missing}")
    assert not missing
    assert report["report_type"] == "experiment"
    assert report["experiment_id"] == "exp-structure"
