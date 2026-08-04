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
    _aggregate_verdict_mix,
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


def test_aggregate_retrieval_rates_seed_share_drift():
    """M32: seed_share/cross_share pass through from series_report.py's new
    fields, and the first-vs-last-series drift statement is computed."""
    series_data = {
        "s0": {
            "sections": {
                "s5_3": {
                    "intra_series_rate": 0.5,
                    "total_retrieved": 10,
                    "intra_series_count": 5,
                    "seed_fixture_count": 4,
                    "cross_series_count": 1,
                    "unknown_count": 0,
                    "seed_fixture_rate": 0.4,
                    "cross_series_rate": 0.1,
                },
            },
            "run_count": 5,
        },
        "s1": {
            "sections": {
                "s5_3": {
                    "intra_series_rate": 0.7,
                    "total_retrieved": 10,
                    "intra_series_count": 7,
                    "seed_fixture_count": 1,
                    "cross_series_count": 2,
                    "unknown_count": 0,
                    "seed_fixture_rate": 0.1,
                    "cross_series_rate": 0.2,
                },
            },
            "run_count": 5,
        },
    }

    result = _aggregate_retrieval_rates(series_data)
    print(f"per_series={result['per_series']} drift={result['seed_share_drift']}")
    assert result["per_series"][0]["seed_share"] == 0.4
    assert result["per_series"][1]["seed_share"] == 0.1
    assert result["seed_share_drift"] == {
        "first_series_id": "s0",
        "first_seed_share": 0.4,
        "last_series_id": "s1",
        "last_seed_share": 0.1,
        "delta": round(0.1 - 0.4, 4),
    }


def test_aggregate_retrieval_rates_missing_fields_backward_compat():
    """M32: a stored series report written before M32 lacks seed_fixture_rate
    / cross_series_rate — seed_share/cross_share must be None (not crash,
    not recomputed from raw counts) and the drift statement must be None."""
    series_data = {
        "s0": {
            "sections": {
                "s5_3": {
                    "intra_series_rate": 0.5,
                    "total_retrieved": 10,
                    "intra_series_count": 5,
                    "seed_fixture_count": 4,
                    "cross_series_count": 1,
                    "unknown_count": 0,
                    # no seed_fixture_rate / cross_series_rate keys — pre-M32 shape
                },
            },
            "run_count": 5,
        },
        "s1": {
            "sections": {
                "s5_3": {
                    "intra_series_rate": 0.7,
                    "total_retrieved": 10,
                    "intra_series_count": 7,
                    "seed_fixture_count": 1,
                    "cross_series_count": 2,
                    "unknown_count": 0,
                },
            },
            "run_count": 5,
        },
    }

    result = _aggregate_retrieval_rates(series_data)
    print(f"per_series={result['per_series']} drift={result['seed_share_drift']}")
    assert result["per_series"][0]["seed_share"] is None
    assert result["per_series"][0]["cross_share"] is None
    assert result["seed_share_drift"] is None


# ---------------------------------------------------------------------------
# Verdict mix (M32)
# ---------------------------------------------------------------------------


def test_aggregate_verdict_mix_up_rate_excludes_none():
    """up_rate = up / (up + down); 'none' verdicts don't affect it."""
    verdict_counts = {
        "s0": {"up": 3, "down": 1, "none": 4},
        "s1": {"up": 1, "down": 3, "none": 0},
    }
    result = _aggregate_verdict_mix(["s0", "s1"], verdict_counts)
    print(f"verdict_mix={result}")
    assert result["per_series"][0]["up_rate"] == 0.75
    assert result["per_series"][1]["up_rate"] == 0.25
    assert result["total_up"] == 4
    assert result["total_down"] == 4
    assert result["total_none"] == 4
    assert result["overall_up_rate"] == 0.5


def test_aggregate_verdict_mix_series_with_no_feedback_renders_none_up_rate():
    """A series absent from verdict_counts (all-probe series, dry run) gets
    zero counts and up_rate=None — distinct from a genuine 0% up-rate."""
    result = _aggregate_verdict_mix(["s0"], {})
    print(f"verdict_mix={result}")
    p = result["per_series"][0]
    assert (p["series_id"], p["up"], p["down"], p["none"], p["up_rate"]) == (
        "s0", 0, 0, 0, None,
    )
    assert result["overall_up_rate"] is None
    assert result["judge_alone_up_rate"] is None


# --- M36: recorded vs. judge-alone -----------------------------------------


def test_aggregate_verdict_mix_reports_judge_alone_rate_beside_recorded():
    """The headline up-rate is human-assisted when an operator overrode the
    judge; the judge-alone rate is what an unattended run would produce.
    Modelled on the 2026-08-04 calibration: 3 overrides, all judge "up"
    corrected downward, so judge-alone reads higher than recorded."""
    verdict_counts = {
        "s0": {
            # Recorded: 12 up / 5 down / 3 none -> 70.6%
            "up": 12, "down": 5, "none": 3,
            # Judge-alone: 14 up / 3 down / 3 none -> 82.4%
            "judge_up": 14, "judge_down": 3, "judge_none": 3,
            "human_sourced": 3, "human_unrecoverable": 0,
        },
    }
    result = _aggregate_verdict_mix(["s0"], verdict_counts)
    print(f"verdict_mix={result}")
    assert result["overall_up_rate"] == 0.7059
    assert result["judge_alone_up_rate"] == 0.8235
    assert result["total_human_sourced"] == 3
    assert result["judge_alone_unrecoverable"] == 0
    assert result["per_series"][0]["human_sourced"] == 3
    assert result["per_series"][0]["judge_alone_up_rate"] == 0.8235


def test_aggregate_verdict_mix_automated_run_rates_coincide():
    """With no operator in the loop the two rates are identical — the
    divergence, not the judge-alone figure itself, is the signal."""
    verdict_counts = {
        "s0": {
            "up": 8, "down": 2, "none": 1,
            "judge_up": 8, "judge_down": 2, "judge_none": 1,
            "human_sourced": 0, "human_unrecoverable": 0,
        },
    }
    result = _aggregate_verdict_mix(["s0"], verdict_counts)
    print(f"verdict_mix={result}")
    assert result["overall_up_rate"] == result["judge_alone_up_rate"] == 0.8
    assert result["total_human_sourced"] == 0


def test_aggregate_verdict_mix_surfaces_unrecoverable_pre_m36_overrides():
    """Pre-M36 human rows can't contribute a judge-alone verdict. The count
    is surfaced so the partial coverage is stated, not silently absorbed."""
    verdict_counts = {
        "s0": {
            "up": 4, "down": 2, "none": 0,
            "judge_up": 4, "judge_down": 0, "judge_none": 0,
            "human_sourced": 2, "human_unrecoverable": 2,
        },
    }
    result = _aggregate_verdict_mix(["s0"], verdict_counts)
    print(f"verdict_mix={result}")
    assert result["judge_alone_unrecoverable"] == 2
    # Judge-alone is computed from the 4 recoverable rows only.
    assert result["judge_alone_up_rate"] == 1.0


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


def test_graph_wipe_ignores_seed_fixture_mus_reseeded_by_another_condition():
    """Seed-fixture MUs keep the fixture's original created_at but are stamped
    with whichever experiment_id the backend carried when they were loaded. In
    a multi-condition harness run, a later condition's re-seed (e.g.
    "..._random_weight") therefore predates an earlier condition's first run —
    that must not read as an incomplete wipe. Seed MUs are identified by
    series_id 'seed:%' and are verified by the seed-provenance check instead."""
    with TempDB() as store:
        experiment_id = "pilot_learned"

        run(store.insert_run(_make_minimal_run(
            series_id="series-A",
            experiment_id=experiment_id,
            turn_index=1,
        )))

        import aiosqlite

        async def _insert_reseeded_seed_mu():
            db = await aiosqlite.connect(store_module._DB_PATH)
            try:
                await db.execute(
                    """INSERT INTO memory_units
                       (mu_id, created_at, source_type, raw_content, experiment_id, series_id)
                       VALUES (?, ?, ?, ?, ?, ?)""",
                    (_mu_id(), "2025-12-31T00:00:00+00:00",
                     "agent_output", "seed fixture content",
                     "pilot_random_weight", "seed:v2"),
                )
                await db.commit()
            finally:
                await db.close()

        run(_insert_reseeded_seed_mu())

        result = run(_check_graph_wipe(experiment_id, store))
        print(f"graph_wipe: {result}")
        assert result["status"] == "PASS"
        assert result["pre_experiment_foreign_mu_count"] == 0


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


def _base_series_summary_for_markdown(s53_overrides: dict) -> dict:
    """Minimal series_summary dict, with an overridable §5.3 aggregation
    block, for markdown-rendering tests below."""
    return {
        "series_ids": ["s0", "s1"],
        "series_count": 2,
        "total_run_count": 0,
        "s5_1_failure_aggregation": {
            "total_failed_turn_count": 0, "total_ghost_mu_incidents": 0,
            "per_series": [], "all_ghost_incidents": [],
        },
        "verdict_mix": {
            "total_up": 0, "total_down": 0, "total_none": 0,
            "overall_up_rate": None, "per_series": [],
        },
        "s5_3_retrieval_rate_aggregation": s53_overrides,
        "s5_5_edge_weight_aggregation": {
            "total_activated_edge_count": 0, "edges_increased": 0,
            "edges_decreased": 0, "edges_unchanged": 0, "mean_abs_delta": 0.0,
            "per_series": [],
        },
        "s5_6_probe_integrity_aggregation": {
            "status": "PASS", "total_violations": 0, "hard_fail_series": [],
        },
        "s5_7_mode3_aggregation": {
            "total_mode3_mu_count": 0, "increasing_pressure_flag": False, "per_series": [],
        },
        "s5_8_ctx_pressure_aggregation": {
            "increasing_pressure_flag": False, "per_series": [],
        },
    }


def _base_report_for_markdown(s53_overrides: dict) -> dict:
    return {
        "report_id": "r1", "report_type": "experiment", "experiment_id": "exp-1",
        "condition_label": "learning", "generated_at": "2026-01-01T00:00:00+00:00",
        "series_summary": _base_series_summary_for_markdown(s53_overrides),
        "integrity_checks": {
            "overall_pass": True,
            "seed_fixture_provenance": {"status": "PASS", "fixture_versions_found": [], "unregistered_versions": [], "hash_mismatch": False},
            "graph_wipe_verification": {"status": "SKIP", "pre_experiment_foreign_mu_count": 0, "earliest_run_timestamp": None},
            "probe_replay_index_normalization": {"status": "PASS", "condition_label": None, "session_indices_found": [], "gaps": []},
        },
        "learning_measures": None,
        "control_shadow": None,
    }


def test_s53_experiment_markdown_renders_seed_share_and_drift():
    """M32: the experiment-report §5.3 table gains seed_share/cross_share
    columns and a drift statement line when the aggregation has them."""
    report = _base_report_for_markdown({
        "mean_intra_series_rate": 0.5, "min_intra_series_rate": 0.4,
        "max_intra_series_rate": 0.6, "outlier_series": [], "outlier_threshold": 0.15,
        "seed_share_drift": {
            "first_series_id": "s0", "first_seed_share": 0.42,
            "last_series_id": "s1", "last_seed_share": 0.14,
            "delta": round(0.14 - 0.42, 4),
        },
        "per_series": [
            {"series_id": "s0", "intra_series_rate": 0.5, "total_retrieved": 10,
             "intra_series_count": 5, "seed_fixture_count": 4, "cross_series_count": 1,
             "unknown_count": 0, "seed_share": 0.42, "cross_share": 0.1},
            {"series_id": "s1", "intra_series_rate": 0.5, "total_retrieved": 10,
             "intra_series_count": 5, "seed_fixture_count": 1, "cross_series_count": 4,
             "unknown_count": 0, "seed_share": 0.14, "cross_share": 0.4},
        ],
    })
    md = render_experiment_report_markdown(report)
    print(f"§5.3 experiment markdown:\n{md}")
    assert "Seed-share drift" in md
    assert "42.0%" in md
    assert "14.0%" in md


def test_s53_experiment_markdown_missing_seed_share_renders_em_dash():
    """M32: a pre-M32 stored series report (no seed_share/cross_share)
    renders '—' in those columns rather than raising or fabricating a value."""
    report = _base_report_for_markdown({
        "mean_intra_series_rate": 0.5, "min_intra_series_rate": 0.5,
        "max_intra_series_rate": 0.5, "outlier_series": [], "outlier_threshold": 0.15,
        "seed_share_drift": None,
        "per_series": [
            {"series_id": "s0", "intra_series_rate": 0.5, "total_retrieved": 10,
             "intra_series_count": 5, "seed_fixture_count": 4, "cross_series_count": 1,
             "unknown_count": 0, "seed_share": None, "cross_share": None},
        ],
    })
    md = render_experiment_report_markdown(report)
    print(f"§5.3 backward-compat markdown:\n{md}")
    assert "Seed-share drift" not in md
    assert "| s0 | 50.0% | — | — | 10 |" in md


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


# ---------------------------------------------------------------------------
# Verdict mix (M32) — end-to-end through generate_experiment_report
# ---------------------------------------------------------------------------


def test_generate_experiment_report_includes_verdict_mix():
    """verdict_mix is wired into series_summary and reflects real
    feedback_events rows for this experiment's series, in series order."""
    with TempDB() as store:
        experiment_id = "exp-verdict-e2e"
        run_a = _run_id()
        run_b = _run_id()
        run(store.insert_run(_make_minimal_run(
            run_id=run_a, series_id="series-A", experiment_id=experiment_id, turn_index=1,
        )))
        run(store.insert_run(_make_minimal_run(
            run_id=run_b, series_id="series-B", experiment_id=experiment_id, turn_index=2,
        )))
        run(store.insert_feedback_event({
            "feedback_event_id": str(uuid.uuid4()), "run_id": run_a,
            "verdict": "up", "is_probe_turn": False,
        }))
        run(store.insert_feedback_event({
            "feedback_event_id": str(uuid.uuid4()), "run_id": run_b,
            "verdict": "down", "is_probe_turn": False,
        }))

        with mock.patch(
            "analytics.reports.experiment_report._load_config", return_value=_CONFIG
        ):
            report = run(generate_experiment_report(experiment_id, store))

        verdict_mix = report["series_summary"]["verdict_mix"]
        print(f"verdict_mix={verdict_mix}")
        assert verdict_mix["total_up"] == 1
        assert verdict_mix["total_down"] == 1
        assert verdict_mix["overall_up_rate"] == 0.5
        by_series = {p["series_id"]: p for p in verdict_mix["per_series"]}
        assert by_series["series-A"]["up"] == 1
        assert by_series["series-B"]["down"] == 1


def test_generate_experiment_report_surfaces_overrides_end_to_end():
    """Regression for the 2026-08-04 calibration gap: three operator
    overrides were recorded in the DB and appeared nowhere in the report,
    and the headline up-rate silently presented human-corrected verdicts as
    the judge's own calibration. The report must now show both the override
    count and the judge-alone rate, and the rendered markdown must say so."""
    with TempDB() as store:
        experiment_id = "exp-override-e2e"
        # Two accepted judge verdicts (up, up) and one override (judge up ->
        # operator down) — recorded 2/3 up, judge-alone 3/3 up.
        specs = [
            ("up", "model:gemma4:e4b", "up"),
            ("up", "model:gemma4:e4b", "up"),
            ("down", "human", "up"),
        ]
        for i, (verdict, source, model_verdict) in enumerate(specs):
            rid = _run_id()
            run(store.insert_run(_make_minimal_run(
                run_id=rid, series_id="series-A",
                experiment_id=experiment_id, turn_index=i,
            )))
            run(store.insert_feedback_event({
                "feedback_event_id": str(uuid.uuid4()), "run_id": rid,
                "verdict": verdict, "is_probe_turn": False,
                "judge_source": source, "judge_model_verdict": model_verdict,
            }))

        with mock.patch(
            "analytics.reports.experiment_report._load_config", return_value=_CONFIG
        ):
            report = run(generate_experiment_report(experiment_id, store))

        verdict_mix = report["series_summary"]["verdict_mix"]
        print(f"override verdict_mix={verdict_mix}")
        assert verdict_mix["overall_up_rate"] == round(2 / 3, 4)
        assert verdict_mix["judge_alone_up_rate"] == 1.0
        assert verdict_mix["total_human_sourced"] == 1
        assert verdict_mix["judge_alone_unrecoverable"] == 0

        md = render_experiment_report_markdown(report)
        print(md[md.index("### Verdict Mix"):][:700])
        assert "Human-sourced verdicts" in md
        assert "Judge-alone up-rate" in md
        assert "human-assisted" in md, "the caveat must appear when overrides exist"


def test_render_verdict_mix_omits_caveat_for_fully_automated_run():
    """The human-assisted caveat is noise on a run with no operator in the
    loop — it must appear only when there is actually a divergence to warn
    about."""
    with TempDB() as store:
        experiment_id = "exp-no-override"
        rid = _run_id()
        run(store.insert_run(_make_minimal_run(
            run_id=rid, series_id="series-A", experiment_id=experiment_id, turn_index=0,
        )))
        run(store.insert_feedback_event({
            "feedback_event_id": str(uuid.uuid4()), "run_id": rid,
            "verdict": "up", "is_probe_turn": False,
            "judge_source": "model:gemma4:e4b", "judge_model_verdict": "up",
        }))

        with mock.patch(
            "analytics.reports.experiment_report._load_config", return_value=_CONFIG
        ):
            report = run(generate_experiment_report(experiment_id, store))

        md = render_experiment_report_markdown(report)
        assert "Judge-alone up-rate" in md
        assert "human-assisted" not in md
        assert "⚠️" not in md.split("### §5.3")[0], "no unrecoverable-coverage warning expected"


def test_generate_experiment_report_verdict_mix_scoped_to_experiment_id():
    """A different experiment's feedback events must not leak into this
    experiment's verdict-mix counts, even under a shared series_id."""
    with TempDB() as store:
        run_own = _run_id()
        run_other = _run_id()
        run(store.insert_run(_make_minimal_run(
            run_id=run_own, series_id="series-A", experiment_id="exp-own", turn_index=1,
        )))
        run(store.insert_run(_make_minimal_run(
            run_id=run_other, series_id="series-A", experiment_id="exp-other", turn_index=1,
        )))
        run(store.insert_feedback_event({
            "feedback_event_id": str(uuid.uuid4()), "run_id": run_own,
            "verdict": "up", "is_probe_turn": False,
        }))
        run(store.insert_feedback_event({
            "feedback_event_id": str(uuid.uuid4()), "run_id": run_other,
            "verdict": "down", "is_probe_turn": False,
        }))

        with mock.patch(
            "analytics.reports.experiment_report._load_config", return_value=_CONFIG
        ):
            report = run(generate_experiment_report("exp-own", store))

        verdict_mix = report["series_summary"]["verdict_mix"]
        print(f"scoped verdict_mix={verdict_mix}")
        assert verdict_mix["total_up"] == 1
        assert verdict_mix["total_down"] == 0


def test_generate_experiment_report_verdict_mix_empty_series_renders_cleanly():
    """An experiment with runs but no feedback_events (e.g. an all-probe
    series or a dry run) produces a zero-filled verdict_mix, not a crash."""
    with TempDB() as store:
        experiment_id = "exp-no-feedback"
        run(store.insert_run(_make_minimal_run(
            series_id="series-A", experiment_id=experiment_id, turn_index=1,
        )))

        with mock.patch(
            "analytics.reports.experiment_report._load_config", return_value=_CONFIG
        ):
            report = run(generate_experiment_report(experiment_id, store))

        verdict_mix = report["series_summary"]["verdict_mix"]
        print(f"empty verdict_mix={verdict_mix}")
        assert verdict_mix["total_up"] == 0
        assert verdict_mix["overall_up_rate"] is None
        p = verdict_mix["per_series"][0]
        assert (p["series_id"], p["up"], p["down"], p["none"], p["up_rate"]) == (
            "series-A", 0, 0, 0, None,
        )

        md = render_experiment_report_markdown(report)
        assert "Verdict Mix" in md


# ---------------------------------------------------------------------------
# M21 — probe results scoped by experiment_id (contamination guard)
# ---------------------------------------------------------------------------


def _insert_probe_row(
    store, probe_id, session_idx, condition_label, experiment_id=None,
    gold_mu_rank=None, battery_mode="mean", battery_repeat_index=None,
):
    run(store.insert_probe_result({
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
        "battery_mode": battery_mode,
        "battery_repeat_index": battery_repeat_index,
    }))


def test_probe_replay_index_scoped_to_experiment_id():
    """§6.3 must not see another attempt's probe sessions: two experiments
    share condition_label 'learned', but only this experiment's indices count."""
    with TempDB() as store:
        experiment_id = "plan1_learned"
        run(store.insert_run(_make_minimal_run(
            series_id="series-A", experiment_id=experiment_id,
            condition_label="learned", turn_index=1,
        )))
        run(store.sync_probes([{
            "probe_id": "p01", "probe_set_version": "v2",
            "phenomenon_type": "ranking_improvement", "prompt_text": "test probe",
            "gold_mu_ids": [], "is_bridge_probe": False, "bridge_pair_id": None,
        }]))

        for session_idx in [1, 2]:
            _insert_probe_row(store, "p01", session_idx, "learned", experiment_id=experiment_id)
        # A different attempt of the same condition — session 9 would create a
        # bogus gap (3..8) if condition_label were the only filter.
        _insert_probe_row(store, "p01", 9, "learned", experiment_id="plan1_attempt2_learned")

        result = run(_check_probe_replay_index(experiment_id, store))
        print(f"probe_replay scoped: {result}")
        assert result["status"] == "PASS"
        assert result["session_indices_found"] == [1, 2]


def test_learning_measures_probe_results_scoped_to_experiment_id():
    """§6.1 probe_pass_rates must only aggregate this experiment's probe rows,
    even when another run shares the condition_label."""
    from analytics.reports.experiment_report import _section_61_learning_measures

    with TempDB() as store:
        experiment_id = "plan1_learned"
        run(store.insert_run(_make_minimal_run(
            series_id="series-A", experiment_id=experiment_id,
            condition_label="learned", turn_index=1,
        )))
        run(store.sync_probes([{
            "probe_id": "p01", "probe_set_version": "v2",
            "phenomenon_type": "ranking_improvement", "prompt_text": "test probe",
            "gold_mu_ids": ["mu-gold"], "is_bridge_probe": False, "bridge_pair_id": None,
        }]))

        # This experiment: 2 sessions, both passing (rank 1).
        _insert_probe_row(store, "p01", 1, "learned", experiment_id=experiment_id, gold_mu_rank=1)
        _insert_probe_row(store, "p01", 2, "learned", experiment_id=experiment_id, gold_mu_rank=1)
        # Contaminating attempt: same condition_label, all failing (rank None).
        _insert_probe_row(store, "p01", 1, "learned", experiment_id="plan1_attempt2_learned")
        _insert_probe_row(store, "p01", 2, "learned", experiment_id="plan1_attempt2_learned")

        measures = run(_section_61_learning_measures(experiment_id, store, {"reporting": {}}))

        p01 = next(
            p for p in measures["state_transitions"]["per_probe"] if p["probe_id"] == "p01"
        )
        assert p01["session_indices"] == [1, 2]  # not 4 rows — other attempt excluded
        assert p01["outcome_sequence"] == ["pass", "pass"]
        assert measures["probe_pass_rates"]["p01"] == 1.0  # would be 0.5 if contaminated
        print(f"[PASS] §6.1 scoped to experiment_id: {p01}")


def test_learning_measures_fall_back_to_condition_label_for_legacy_rows():
    """Pre-M21 probe rows (experiment_id=NULL) are still found via the
    condition_label fallback when no experiment-stamped rows exist."""
    from analytics.reports.experiment_report import _section_61_learning_measures

    with TempDB() as store:
        experiment_id = "legacy_experiment"
        run(store.insert_run(_make_minimal_run(
            series_id="series-A", experiment_id=experiment_id,
            condition_label="learning", turn_index=1,
        )))
        run(store.sync_probes([{
            "probe_id": "p01", "probe_set_version": "v1",
            "phenomenon_type": "ranking_improvement", "prompt_text": "test probe",
            "gold_mu_ids": ["mu-gold"], "is_bridge_probe": False, "bridge_pair_id": None,
        }]))
        _insert_probe_row(store, "p01", 1, "learning", gold_mu_rank=1)  # no experiment_id

        measures = run(_section_61_learning_measures(experiment_id, store, {"reporting": {}}))

        p01 = next(
            p for p in measures["state_transitions"]["per_probe"] if p["probe_id"] == "p01"
        )
        assert p01["session_indices"] == [1]
        assert measures["probe_pass_rates"]["p01"] == 1.0
        print(f"[PASS] §6.1 legacy condition_label fallback: {p01}")


def test_learning_measures_exclude_probes_from_batteries_not_run():
    """Probes synced from another probe-set version, with no results for this
    experiment, must not appear in §6.1 (they render as insufficient_data
    noise and inflate probe/bridge counts)."""
    from analytics.reports.experiment_report import _section_61_learning_measures

    with TempDB() as store:
        experiment_id = "plan1_learned"
        run(store.insert_run(_make_minimal_run(
            series_id="series-A", experiment_id=experiment_id,
            condition_label="learned", turn_index=1,
        )))
        # Two synced probe sets: only the v2 probe recorded measurements.
        run(store.sync_probes([
            {"probe_id": "p01", "probe_set_version": "v1",
             "phenomenon_type": "ranking_improvement", "prompt_text": "stale v1 probe",
             "gold_mu_ids": [], "is_bridge_probe": True, "bridge_pair_id": "pair1"},
            {"probe_id": "p06", "probe_set_version": "v2",
             "phenomenon_type": "ranking_improvement", "prompt_text": "active v2 probe",
             "gold_mu_ids": ["mu-gold"], "is_bridge_probe": False, "bridge_pair_id": None},
        ]))
        _insert_probe_row(store, "p06", 1, "learned", experiment_id=experiment_id, gold_mu_rank=1)

        measures = run(_section_61_learning_measures(experiment_id, store, {"reporting": {}}))

        probe_ids = [p["probe_id"] for p in measures["state_transitions"]["per_probe"]]
        print(f"probes in §6.1: {probe_ids}")
        assert probe_ids == ["p06"]
        assert measures["probe_count"] == 1
        # The stale v1 bridge probe must not inflate the bridge analysis either.
        assert measures["bridge_mu_analysis"]["bridge_probe_count"] == 0
        assert "p01" not in measures["probe_pass_rates"]


def test_learning_measures_keep_all_probes_when_no_results_exist():
    """With zero probe results anywhere, §6.1 still generates with the full
    probe list and empty data — the pre-existing empty-section contract."""
    from analytics.reports.experiment_report import _section_61_learning_measures

    with TempDB() as store:
        experiment_id = "exp-no-probes-run"
        run(store.insert_run(_make_minimal_run(
            series_id="series-A", experiment_id=experiment_id,
            condition_label="learned", turn_index=1,
        )))
        run(store.sync_probes([
            {"probe_id": "p01", "probe_set_version": "v1",
             "phenomenon_type": "ranking_improvement", "prompt_text": "probe",
             "gold_mu_ids": [], "is_bridge_probe": False, "bridge_pair_id": None},
        ]))

        measures = run(_section_61_learning_measures(experiment_id, store, {"reporting": {}}))

        print(f"probe_count with no results: {measures['probe_count']}")
        assert measures["probe_count"] == 1
        assert measures["probe_pass_rates"] == {"p01": None}


# ---------------------------------------------------------------------------
# M30 Part B — §6.1 dual-mode battery consumption (battery_mode filtering +
# sample_pass_rates)
# ---------------------------------------------------------------------------


def test_learning_measures_ignores_sample_repeats_for_mean_sections():
    """Regression test for the actual M30 bug: mean-section subsections must
    only ever see battery_mode='mean' rows. Before the fix, a dual-mode
    battery's mean pass + K sample repeats shared one measurement_session_index
    and query_probe_results() had no battery_mode filter, so every mean-only
    subsection silently received 1 + K rows per checkpoint instead of 1."""
    from analytics.reports.experiment_report import _section_61_learning_measures

    with TempDB() as store:
        experiment_id = "plan1_learned"
        run(store.insert_run(_make_minimal_run(
            series_id="series-A", experiment_id=experiment_id,
            condition_label="learned", turn_index=1,
        )))
        run(store.sync_probes([{
            "probe_id": "p01", "probe_set_version": "v3",
            "phenomenon_type": "spoke_retrieval", "prompt_text": "test probe",
            "gold_mu_ids": ["mu-gold"], "is_bridge_probe": False, "bridge_pair_id": None,
        }]))

        # 1 mean row at session 0, passing.
        _insert_probe_row(
            store, "p01", 0, "learned", experiment_id=experiment_id,
            gold_mu_rank=1, battery_mode="mean",
        )
        # 5 sample repeats at the same session index, mixed pass/fail —
        # these must never reach state_transitions/probe_pass_rates.
        for i, rank in enumerate([1, None, None, 1, None]):
            _insert_probe_row(
                store, "p01", 0, "learned", experiment_id=experiment_id,
                gold_mu_rank=rank, battery_mode="sample", battery_repeat_index=i,
            )

        measures = run(_section_61_learning_measures(experiment_id, store, {"reporting": {}}))

        p01 = next(
            p for p in measures["state_transitions"]["per_probe"] if p["probe_id"] == "p01"
        )
        print(f"state_transitions p01: {p01}")
        assert len(p01["session_indices"]) == 1  # not 6
        assert p01["outcome_sequence"] == ["pass"]
        assert measures["probe_pass_rates"]["p01"] == 1.0


def test_sample_pass_rates_computed_from_repeats():
    """A known pass/fail mix of sample repeats at one session must produce
    exact pass_count/fail_count/repeats_completed/pass_rate."""
    from analytics.reports.experiment_report import _section_61_learning_measures

    with TempDB() as store:
        experiment_id = "plan1_learned"
        run(store.insert_run(_make_minimal_run(
            series_id="series-A", experiment_id=experiment_id,
            condition_label="learned", turn_index=1,
        )))
        run(store.sync_probes([{
            "probe_id": "p01", "probe_set_version": "v3",
            "phenomenon_type": "spoke_retrieval", "prompt_text": "test probe",
            "gold_mu_ids": ["mu-gold"], "is_bridge_probe": False, "bridge_pair_id": None,
        }]))
        _insert_probe_row(
            store, "p01", 0, "learned", experiment_id=experiment_id,
            gold_mu_rank=1, battery_mode="mean",
        )
        # 4 sample repeats: 3 pass (rank 1), 1 fail (rank None).
        for i, rank in enumerate([1, 1, 1, None]):
            _insert_probe_row(
                store, "p01", 0, "learned", experiment_id=experiment_id,
                gold_mu_rank=rank, battery_mode="sample", battery_repeat_index=i,
            )

        measures = run(_section_61_learning_measures(experiment_id, store, {"reporting": {}}))

        p01 = next(
            p for p in measures["sample_pass_rates"]["per_probe"] if p["probe_id"] == "p01"
        )
        session = p01["sessions"][0]
        print(f"sample_pass_rates session: {session}")
        assert session["pass_count"] == 3
        assert session["fail_count"] == 1
        assert session["repeats_completed"] == 4
        assert session["pass_rate"] == 0.75


def test_sample_pass_rates_full_battery_not_flagged_partial():
    """Exactly configured_repeats rows must not be flagged partial_battery."""
    from analytics.reports.experiment_report import _section_61_learning_measures

    with TempDB() as store:
        experiment_id = "plan1_learned"
        run(store.insert_run(_make_minimal_run(
            series_id="series-A", experiment_id=experiment_id,
            condition_label="learned", turn_index=1,
        )))
        run(store.sync_probes([{
            "probe_id": "p01", "probe_set_version": "v3",
            "phenomenon_type": "spoke_retrieval", "prompt_text": "test probe",
            "gold_mu_ids": ["mu-gold"], "is_bridge_probe": False, "bridge_pair_id": None,
        }]))
        _insert_probe_row(
            store, "p01", 0, "learned", experiment_id=experiment_id,
            gold_mu_rank=1, battery_mode="mean",
        )
        configured_repeats = 3
        for i in range(configured_repeats):
            _insert_probe_row(
                store, "p01", 0, "learned", experiment_id=experiment_id,
                gold_mu_rank=1, battery_mode="sample", battery_repeat_index=i,
            )

        measures = run(_section_61_learning_measures(
            experiment_id, store,
            {"reporting": {}, "probe_measurement": {"sample_battery_repeats": configured_repeats}},
        ))

        p01 = next(
            p for p in measures["sample_pass_rates"]["per_probe"] if p["probe_id"] == "p01"
        )
        session = p01["sessions"][0]
        print(f"full-battery session: {session}")
        assert session["repeats_completed"] == configured_repeats
        assert session["partial_battery"] is False


def test_sample_pass_rates_scoped_to_experiment_id():
    """A contaminating attempt under a different experiment_id must not leak
    into this experiment's sample pass rate — mirrors the existing mean-row
    scoping test."""
    from analytics.reports.experiment_report import _section_61_learning_measures

    with TempDB() as store:
        experiment_id = "plan1_learned"
        run(store.insert_run(_make_minimal_run(
            series_id="series-A", experiment_id=experiment_id,
            condition_label="learned", turn_index=1,
        )))
        run(store.sync_probes([{
            "probe_id": "p01", "probe_set_version": "v3",
            "phenomenon_type": "spoke_retrieval", "prompt_text": "test probe",
            "gold_mu_ids": ["mu-gold"], "is_bridge_probe": False, "bridge_pair_id": None,
        }]))
        _insert_probe_row(
            store, "p01", 0, "learned", experiment_id=experiment_id,
            gold_mu_rank=1, battery_mode="mean",
        )
        # This experiment: both sample repeats pass.
        _insert_probe_row(
            store, "p01", 0, "learned", experiment_id=experiment_id,
            gold_mu_rank=1, battery_mode="sample", battery_repeat_index=0,
        )
        _insert_probe_row(
            store, "p01", 0, "learned", experiment_id=experiment_id,
            gold_mu_rank=1, battery_mode="sample", battery_repeat_index=1,
        )
        # Contaminating attempt, same condition_label, all failing.
        _insert_probe_row(
            store, "p01", 0, "learned", experiment_id="plan1_attempt2_learned",
            gold_mu_rank=None, battery_mode="sample", battery_repeat_index=0,
        )

        measures = run(_section_61_learning_measures(experiment_id, store, {"reporting": {}}))

        p01 = next(
            p for p in measures["sample_pass_rates"]["per_probe"] if p["probe_id"] == "p01"
        )
        session = p01["sessions"][0]
        print(f"scoped sample session: {session}")
        assert session["repeats_completed"] == 2  # not 3 — other attempt excluded
        assert session["pass_rate"] == 1.0


def test_sample_pass_rates_empty_when_no_sample_rows():
    """An experiment with only mean-mode rows (every pre-M30 experiment)
    still generates §6.1 cleanly — sample_pass_rates.per_probe present with
    empty sessions, no crash."""
    from analytics.reports.experiment_report import _section_61_learning_measures

    with TempDB() as store:
        experiment_id = "plan1_learned"
        run(store.insert_run(_make_minimal_run(
            series_id="series-A", experiment_id=experiment_id,
            condition_label="learned", turn_index=1,
        )))
        run(store.sync_probes([{
            "probe_id": "p01", "probe_set_version": "v2",
            "phenomenon_type": "ranking_improvement", "prompt_text": "test probe",
            "gold_mu_ids": ["mu-gold"], "is_bridge_probe": False, "bridge_pair_id": None,
        }]))
        _insert_probe_row(
            store, "p01", 1, "learned", experiment_id=experiment_id,
            gold_mu_rank=1, battery_mode="mean",
        )

        measures = run(_section_61_learning_measures(experiment_id, store, {"reporting": {}}))

        p01 = next(
            p for p in measures["sample_pass_rates"]["per_probe"] if p["probe_id"] == "p01"
        )
        print(f"empty sample_pass_rates p01: {p01}")
        assert p01["sessions"] == []


# ---------------------------------------------------------------------------
# M33 — §6.1 edge classification, end-to-end through the real store
# ---------------------------------------------------------------------------


def _make_run_with_conv_mu(
    run_id: str, conv_mu: str, series_id: str, experiment_id: str,
    weight_updates: list,
) -> dict:
    """A minimal run dict whose input_mu_id is pinned to conv_mu, so a
    weight update referencing conv_mu resolves through the run's own
    prompt-lineage signal."""
    run_dict = _make_minimal_run(
        run_id=run_id, series_id=series_id, experiment_id=experiment_id,
        turn_index=1, weight_updates=weight_updates,
    )
    run_dict["input_mu_id"] = conv_mu
    return run_dict


def test_edge_classification_resolves_conv_seed_same_cluster_via_prompt_lineage():
    """A weight update between a seed MU (cluster_a) and the conversation MU
    a bulk turn created is conv_seed/same_cluster once that turn's
    feedback_event carries a prompt_id landing on the same cluster."""
    from analytics.reports.experiment_report import _section_61_learning_measures

    with TempDB() as store:
        experiment_id = "exp-m33"
        seed_mu = _mu_id()
        run(store.create_mu(
            mu_id=seed_mu, source_type="user_input", raw_content="seed",
            series_id="seed:v3", cluster_id="cluster_a",
        ))

        run_id = _run_id()
        conv_mu = _mu_id()
        run(store.insert_run(_make_run_with_conv_mu(
            run_id, conv_mu, "series-A", experiment_id,
            weight_updates=[{
                "edge_id": "edge-1", "old_weight": 0.5, "new_weight": 0.6,
                "feedback_event_id": "fe-1",
                "source_mu_id": seed_mu, "target_mu_id": conv_mu,
            }],
        )))
        run(store.insert_feedback_event({
            "feedback_event_id": "fe-1",
            "run_id": run_id,
            "verdict": "up",
            "is_probe_turn": False,
            "prompt_id": "cluster_a_0005",
        }))

        measures = run(_section_61_learning_measures(experiment_id, store, {"reporting": {}}))

        grid = measures["edge_delta_ledger"]["edge_classification"]["by_class_relation"]
        print(f"edge classification grid: {grid}")
        assert grid["conv_seed"]["same_cluster"]["count"] == 1


def test_edge_classification_conv_seed_mixed_when_lineage_disagrees():
    """Same setup, but the bulk turn's prompt targets a different cluster
    than the seed endpoint — a real cross-domain link, classified 'mixed'."""
    from analytics.reports.experiment_report import _section_61_learning_measures

    with TempDB() as store:
        experiment_id = "exp-m33-mixed"
        seed_mu = _mu_id()
        run(store.create_mu(
            mu_id=seed_mu, source_type="user_input", raw_content="seed",
            series_id="seed:v3", cluster_id="cluster_a",
        ))

        run_id = _run_id()
        conv_mu = _mu_id()
        run(store.insert_run(_make_run_with_conv_mu(
            run_id, conv_mu, "series-A", experiment_id,
            weight_updates=[{
                "edge_id": "edge-1", "old_weight": 0.5, "new_weight": 0.6,
                "feedback_event_id": "fe-1",
                "source_mu_id": seed_mu, "target_mu_id": conv_mu,
            }],
        )))
        run(store.insert_feedback_event({
            "feedback_event_id": "fe-1",
            "run_id": run_id,
            "verdict": "up",
            "is_probe_turn": False,
            "prompt_id": "cluster_b_0005",
        }))

        measures = run(_section_61_learning_measures(experiment_id, store, {"reporting": {}}))

        grid = measures["edge_delta_ledger"]["edge_classification"]["by_class_relation"]
        print(f"edge classification grid: {grid}")
        assert grid["conv_seed"]["mixed"]["count"] == 1
        watch = measures["edge_delta_ledger"]["edge_classification"]["cross_domain_watch"]
        assert any(w["edge_id"] == "edge-1" for w in watch)


def test_edge_classification_lineage_scoped_to_experiment_id():
    """A prompt_id belonging to a different experiment's feedback event must
    not resolve this experiment's conversation MU lineage."""
    from analytics.reports.experiment_report import _section_61_learning_measures

    with TempDB() as store:
        seed_mu = _mu_id()
        run(store.create_mu(
            mu_id=seed_mu, source_type="user_input", raw_content="seed",
            series_id="seed:v3", cluster_id="cluster_a",
        ))

        own_run_id = _run_id()
        own_conv_mu = _mu_id()
        run(store.insert_run(_make_run_with_conv_mu(
            own_run_id, own_conv_mu, "series-A", "exp-own",
            weight_updates=[{
                "edge_id": "edge-1", "old_weight": 0.5, "new_weight": 0.6,
                "feedback_event_id": "fe-own",
                "source_mu_id": seed_mu, "target_mu_id": own_conv_mu,
            }],
        )))
        # No feedback event at all for "exp-own"'s run — its conv endpoint
        # has no prompt lineage available.

        # A different experiment's run whose prompt_id must not leak in.
        other_run_id = _run_id()
        run(store.insert_run(_make_minimal_run(
            run_id=other_run_id, series_id="series-A", experiment_id="exp-other",
            turn_index=1,
        )))
        run(store.insert_feedback_event({
            "feedback_event_id": "fe-other",
            "run_id": other_run_id,
            "verdict": "up",
            "is_probe_turn": False,
            "prompt_id": "cluster_a_0009",
        }))

        measures = run(_section_61_learning_measures("exp-own", store, {"reporting": {}}))

        grid = measures["edge_delta_ledger"]["edge_classification"]["by_class_relation"]
        print(f"edge classification grid: {grid}")
        assert grid["conv_seed"]["unresolvable"]["count"] == 1
        assert grid["conv_seed"]["same_cluster"]["count"] == 0
        assert grid["conv_seed"]["mixed"]["count"] == 0


# ---------------------------------------------------------------------------
# §5 aggregation must use stored series reports, not the wiped live graph
# ---------------------------------------------------------------------------


def _make_stored_series_report(experiment_id: str, series_id: str, s5_3: dict) -> dict:
    """Build a persisted-series-report dict with the section shape
    write_series_report() produces (full section names under 'sections')."""
    return {
        "report_id": str(uuid.uuid4()),
        "report_type": "series",
        "experiment_id": experiment_id,
        "series_id": series_id,
        "generated_at": "2026-07-06T09:00:00+00:00",
        "run_count": 1,
        "sections": {
            "s5_1_failure_summary": {
                "failed_turn_count": 0, "ghost_mu_count": 0,
                "failed_turns": [], "ghost_incidents": [],
            },
            "s5_2_retrieval_trace_digest": {"turn_count": 1, "traces": []},
            "s5_3_retrieval_rate_classification": s5_3,
            "s5_4_cross_series_edges": {"cross_series_edge_count": 0, "edges": []},
            "s5_5_edge_weight_deltas": {"activated_edge_count": 0, "deltas": []},
            "s5_6_probe_integrity": {"status": "PASS", "violation_count": 0},
            "s5_7_mode3_trigger_count": {"mode3_mu_count": 0, "anomaly_count": 0},
            "s5_8_num_ctx_pressure": {"pressure_turn_count": 0, "context_window_tokens": 8192},
        },
    }


def test_experiment_report_prefers_stored_series_reports_over_wiped_graph():
    """Regression (2026-07-06 pilot): the experiment report runs after later
    conditions have wiped this condition's MUs. Recomputing §5.3 from the
    live graph then classifies every deleted MU as 'unknown' and reports a
    0.0 intra-series rate — the stored series-close report must win."""
    with TempDB() as store:
        experiment_id = "exp-wiped"
        series_id = "series-W"
        # Run whose activated MUs no longer exist in memory_units — the state
        # a non-final condition is in once a later condition has wiped the graph.
        run(store.insert_run(_make_minimal_run(
            series_id=series_id, experiment_id=experiment_id,
            activated_mu_ids=[_mu_id(), _mu_id(), _mu_id()],
        )))

        stored = _make_stored_series_report(experiment_id, series_id, s5_3={
            "total_retrieved": 3, "intra_series_count": 1, "seed_fixture_count": 2,
            "cross_series_count": 0, "unknown_count": 0,
            "intra_series_rate": 0.3333, "per_turn": [],
        })

        with tempfile.TemporaryDirectory() as reports_dir:
            json_path = Path(reports_dir) / f"series_report_{series_id}.json"
            json_path.write_text(json.dumps(stored), encoding="utf-8")
            # The stored path can be anything (M33: resolve_report_path()
            # only trusts the basename, reconstructing the directory from
            # the current _REPORTS_DIR) — patch _REPORTS_DIR to this test's
            # temp dir rather than needing the stored path itself to match.
            run(store.insert_generated_report({
                "report_id": stored["report_id"], "report_type": "series",
                "experiment_id": experiment_id, "series_id": series_id,
                "generated_at": stored["generated_at"],
                "json_path": str(json_path), "md_path": str(json_path),
            }))

            with mock.patch(
                "analytics.reports.experiment_report._load_config", return_value=_CONFIG
            ), mock.patch(
                "analytics.reports.series_report._REPORTS_DIR", Path(reports_dir)
            ):
                report = run(generate_experiment_report(experiment_id, store))

    agg = report["series_summary"]["s5_3_retrieval_rate_aggregation"]
    per = {p["series_id"]: p for p in agg["per_series"]}
    print(f"stored-report aggregation: {per[series_id]}")
    # Values come from the stored snapshot — NOT the live-graph recompute,
    # which would produce intra=0 / unknown=3 here.
    assert per[series_id]["intra_series_rate"] == 0.3333
    assert per[series_id]["intra_series_count"] == 1
    assert per[series_id]["unknown_count"] == 0
    assert agg["mean_intra_series_rate"] == 0.3333


def test_experiment_report_reads_stored_series_report_despite_stale_path_prefix():
    """The stored json_path may carry a directory prefix from a different
    environment than the one reading it now (e.g. the in-container
    /app/data/reports/... path, read back from a host-side script pointed
    at the bind-mounted directory directly). resolve_report_path() must
    still find the file by reconstructing against the current _REPORTS_DIR
    and trusting only the stored filename."""
    with TempDB() as store:
        experiment_id = "exp-stale-prefix"
        series_id = "series-P"
        run(store.insert_run(_make_minimal_run(
            series_id=series_id, experiment_id=experiment_id,
            activated_mu_ids=[_mu_id(), _mu_id(), _mu_id()],
        )))

        stored = _make_stored_series_report(experiment_id, series_id, s5_3={
            "total_retrieved": 3, "intra_series_count": 1, "seed_fixture_count": 2,
            "cross_series_count": 0, "unknown_count": 0,
            "intra_series_rate": 0.3333, "per_turn": [],
        })

        with tempfile.TemporaryDirectory() as reports_dir:
            filename = f"series_report_{series_id}.json"
            (Path(reports_dir) / filename).write_text(json.dumps(stored), encoding="utf-8")
            # A stored path that points nowhere real on this machine — only
            # its basename should ever be used.
            stale_path = f"/app/data/reports/{filename}"
            run(store.insert_generated_report({
                "report_id": stored["report_id"], "report_type": "series",
                "experiment_id": experiment_id, "series_id": series_id,
                "generated_at": stored["generated_at"],
                "json_path": stale_path, "md_path": stale_path,
            }))

            with mock.patch(
                "analytics.reports.experiment_report._load_config", return_value=_CONFIG
            ), mock.patch(
                "analytics.reports.series_report._REPORTS_DIR", Path(reports_dir)
            ):
                report = run(generate_experiment_report(experiment_id, store))

    agg = report["series_summary"]["s5_3_retrieval_rate_aggregation"]
    per = {p["series_id"]: p for p in agg["per_series"]}
    print(f"stale-prefix aggregation: {per[series_id]}")
    assert per[series_id]["intra_series_rate"] == 0.3333


def test_experiment_report_recomputes_when_stored_report_unreadable():
    """A registered series report whose JSON file is missing must not break
    generation — the section falls back to the live-graph recompute."""
    with TempDB() as store:
        experiment_id = "exp-fallback"
        series_id = "series-F"
        run(store.insert_run(_make_minimal_run(
            series_id=series_id, experiment_id=experiment_id,
        )))
        run(store.insert_generated_report({
            "report_id": str(uuid.uuid4()), "report_type": "series",
            "experiment_id": experiment_id, "series_id": series_id,
            "generated_at": "2026-07-06T09:00:00+00:00",
            "json_path": "/nonexistent/path/series_report.json",
            "md_path": "/nonexistent/path/series_report.md",
        }))

        with mock.patch(
            "analytics.reports.experiment_report._load_config", return_value=_CONFIG
        ):
            report = run(generate_experiment_report(experiment_id, store))

    agg = report["series_summary"]["s5_3_retrieval_rate_aggregation"]
    per = {p["series_id"]: p for p in agg["per_series"]}
    print(f"fallback aggregation: {per[series_id]}")
    # Recomputed from the live graph (no activated MUs → 0 retrieved), and
    # generation completed without raising.
    assert per[series_id]["total_retrieved"] == 0
