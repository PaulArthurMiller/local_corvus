"""
Tests for analytics/reports/series_report.py — §5 Series Report Generator.

Covers:
  §5.1 ghost-MU check identifies failed MU appearing in later turn's activated_mu_ids
  §5.1 clean series produces ghost_mu_count=0
  §5.3 correctly classifies intra-series, seed, cross-series, and unknown MUs
  §5.3 intra_series_rate matches manual calculation
  §5.5 start/end weight derived from weight_updates first/last entries
  §5.5 zero-delta high-traversal edges are flagged
  §5.6 probe integrity HARD_FAIL when feedback_events has is_probe_turn=1
  §5.6 probe integrity PASS on clean data
  §5.7 mode-3 anomaly flag fires when system MU content < min_expected_length
  §5.8 pressure flag fires when tokens_in > 0.8 * context_window_tokens
  §5.8 retrieval-volume attribution correct
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
import analytics.reports.series_report as series_report_module
from analytics.reports.series_report import (
    _section_51_failure_summary,
    _section_52_retrieval_trace_digest,
    _section_53_retrieval_rate_classification,
    _section_54_cross_series_edges,
    _section_55_edge_weight_deltas,
    _section_56_probe_integrity,
    _section_57_mode3_trigger_count,
    _section_58_num_ctx_pressure,
    resolve_report_path,
)


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


def _edge_id() -> str:
    return str(uuid.uuid4())


# Minimal config used by §5.7 and §5.8
_CONFIG = {
    "model": {"context_window_tokens": 1000},
    "reporting": {"mode3_min_expected_length": 100},
}


# ---------------------------------------------------------------------------
# §5.1 — Failure summary
# ---------------------------------------------------------------------------


def test_s51_ghost_mu_detected():
    """A failed MU that appears in a later run's activated_mu_ids is flagged."""
    with TempDB() as store:
        series_id = str(uuid.uuid4())
        failed_mu = _mu_id()

        # Create the failed MU in the DB
        run(store.create_mu(
            mu_id=failed_mu,
            source_type="agent_output_failed_token_limit",
            raw_content="failed output",
            series_id=series_id,
        ))

        runs = [
            {
                "run_id": _run_id(),
                "turn_index": 1,
                "failure_reason": "token_limit_exhausted",
                "activated_mu_ids": [],
            },
            {
                "run_id": _run_id(),
                "turn_index": 2,
                "failure_reason": None,
                "activated_mu_ids": [failed_mu, _mu_id()],
            },
        ]

        result = run(_section_51_failure_summary(runs, store, series_id))
        print(f"ghost_mu_count={result['ghost_mu_count']}, ghost_incidents={result['ghost_incidents']}")
        assert result["ghost_mu_count"] == 1
        assert result["ghost_incidents"][0]["ghost_mu_id"] == failed_mu
        assert result["ghost_incidents"][0]["appearing_at_turn_index"] == 2
        assert result["failed_turn_count"] == 1


def test_s51_clean_series_no_ghost_mus():
    """A series with no failed MUs produces ghost_mu_count=0."""
    with TempDB() as store:
        series_id = str(uuid.uuid4())
        runs = [
            {
                "run_id": _run_id(),
                "turn_index": 1,
                "failure_reason": None,
                "activated_mu_ids": [_mu_id()],
            },
        ]
        result = run(_section_51_failure_summary(runs, store, series_id))
        print(f"ghost_mu_count={result['ghost_mu_count']}")
        assert result["ghost_mu_count"] == 0
        assert result["failed_turn_count"] == 0


# ---------------------------------------------------------------------------
# §5.3 — Retrieval rate classification
# ---------------------------------------------------------------------------


def test_s53_classification_all_categories():
    """
    All four classifications (intra_series, seed_fixture, cross_series, unknown)
    are correctly counted and the intra_series_rate matches manual calculation.
    """
    with TempDB() as store:
        series_id = "series-A"
        other_series = "series-B"

        intra_mu = _mu_id()
        seed_mu = _mu_id()
        cross_mu = _mu_id()
        unknown_mu = _mu_id()  # will not be inserted to DB → series_id=None won't exist

        run(store.create_mu(mu_id=intra_mu, source_type="agent_output", raw_content="x", series_id=series_id))
        run(store.create_mu(mu_id=seed_mu, source_type="agent_output", raw_content="y", series_id="seed:v1"))
        run(store.create_mu(mu_id=cross_mu, source_type="agent_output", raw_content="z", series_id=other_series))
        # unknown_mu intentionally not created in DB

        runs = [
            {
                "run_id": _run_id(),
                "turn_index": 1,
                "activated_mu_ids": [intra_mu, seed_mu, cross_mu, unknown_mu],
            }
        ]

        result = run(_section_53_retrieval_rate_classification(runs, series_id, store))
        print(f"classification: {result}")
        assert result["total_retrieved"] == 4
        assert result["intra_series_count"] == 1
        assert result["seed_fixture_count"] == 1
        assert result["cross_series_count"] == 1
        assert result["unknown_count"] == 1
        assert result["intra_series_rate"] == round(1 / 4, 4)
        # M32: seed_fixture_rate / cross_series_rate mirror the same
        # denominator (total_retrieved) as intra_series_rate.
        assert result["seed_fixture_rate"] == round(1 / 4, 4)
        assert result["cross_series_rate"] == round(1 / 4, 4)


def test_s53_rate_computation_all_intra():
    """When all retrieved MUs are intra-series, rate == 1.0."""
    with TempDB() as store:
        series_id = "series-only"
        mu1 = _mu_id()
        mu2 = _mu_id()
        run(store.create_mu(mu_id=mu1, source_type="user_input", raw_content="a", series_id=series_id))
        run(store.create_mu(mu_id=mu2, source_type="agent_output", raw_content="b", series_id=series_id))

        runs = [
            {
                "run_id": _run_id(),
                "turn_index": 1,
                "activated_mu_ids": [mu1, mu2],
            }
        ]
        result = run(_section_53_retrieval_rate_classification(runs, series_id, store))
        print(f"intra_rate={result['intra_series_rate']}")
        assert result["intra_series_rate"] == 1.0
        assert result["cross_series_count"] == 0
        # M32: with no seed/cross MUs retrieved, both new rates are 0.0.
        assert result["seed_fixture_rate"] == 0.0
        assert result["cross_series_rate"] == 0.0


# ---------------------------------------------------------------------------
# §5.5 — Edge weight deltas
# ---------------------------------------------------------------------------


def test_s55_start_end_weight_from_weight_updates():
    """start/end weight is read from first/last weight_updates entry in turn_index order."""
    with TempDB() as store:
        series_id = "series-delta"
        eid = _edge_id()
        mu_a = _mu_id()
        mu_b = _mu_id()

        run(store.create_mu(mu_id=mu_a, source_type="agent_output", raw_content="a"))
        run(store.create_mu(mu_id=mu_b, source_type="agent_output", raw_content="b"))
        run(store.create_edge(source_mu_id=mu_a, target_mu_id=mu_b, weight=0.5, edge_id=eid))

        runs = [
            {
                "run_id": _run_id(),
                "turn_index": 1,
                "activated_edge_ids": [eid],
                "weight_updates": [{"edge_id": eid, "old_weight": 0.5, "new_weight": 0.6}],
            },
            {
                "run_id": _run_id(),
                "turn_index": 2,
                "activated_edge_ids": [eid],
                "weight_updates": [{"edge_id": eid, "old_weight": 0.6, "new_weight": 0.7}],
            },
        ]

        result = run(_section_55_edge_weight_deltas(runs, store))
        print(f"deltas: {result['deltas']}")
        assert len(result["deltas"]) == 1
        d = result["deltas"][0]
        assert d["edge_id"] == eid
        assert d["start_weight"] == 0.5  # first entry old_weight
        assert d["end_weight"] == 0.7    # last entry new_weight
        assert abs(d["delta"] - 0.2) < 1e-9


def test_s55_zero_delta_high_traversal_flagged():
    """An edge traversed > 5 times with zero delta is flagged."""
    with TempDB() as store:
        eid = _edge_id()
        mu_a = _mu_id()
        mu_b = _mu_id()

        run(store.create_mu(mu_id=mu_a, source_type="agent_output", raw_content="a"))
        run(store.create_mu(mu_id=mu_b, source_type="agent_output", raw_content="b"))
        # Insert edge with traversal_count = 6 (above the threshold of 5)
        db_path = store_module._DB_PATH
        import aiosqlite

        async def _set_traversal():
            db = await aiosqlite.connect(str(db_path))
            now = "2026-01-01T00:00:00+00:00"
            await db.execute(
                "INSERT INTO edges (edge_id, source_mu_id, target_mu_id, created_at, weight, direction, traversal_count) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (eid, mu_a, mu_b, now, 0.5, "forward", 6),
            )
            await db.commit()
            await db.close()

        run(_set_traversal())

        runs = [
            {
                "run_id": _run_id(),
                "turn_index": 1,
                "activated_edge_ids": [eid],
                "weight_updates": [],  # no updates → zero delta
            },
        ]
        result = run(_section_55_edge_weight_deltas(runs, store))
        print(f"zero_delta_flags: {result['zero_delta_high_traversal_flags']}")
        assert len(result["zero_delta_high_traversal_flags"]) == 1
        assert result["zero_delta_high_traversal_flags"][0]["edge_id"] == eid


# ---------------------------------------------------------------------------
# §5.6 — Probe integrity
# ---------------------------------------------------------------------------


def test_s56_probe_integrity_hard_fail():
    """HARD_FAIL when feedback_events has is_probe_turn=1 for a run in the series."""
    with TempDB() as store:
        series_id = str(uuid.uuid4())
        rid = _run_id()
        mu_a = _mu_id()
        mu_b = _mu_id()

        # Create MUs needed for edge FK in analytics
        run(store.create_mu(mu_id=mu_a, source_type="user_input", raw_content="q"))
        run(store.create_mu(mu_id=mu_b, source_type="agent_output", raw_content="a"))

        import json
        import aiosqlite

        async def _insert_run_and_event():
            db = await aiosqlite.connect(str(store_module._DB_PATH))
            await db.executescript(store_module._INIT_SQL)
            await db.commit()
            await db.execute(
                "INSERT INTO analytics (run_id, series_id, timestamp, tool_calls, activated_mu_ids, activated_edge_ids, "
                "weight_updates, hyperparameters_snapshot, assembled_prompt_json, full_config_snapshot) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (rid, series_id, "2026-01-01T00:00:00+00:00", "[]", "[]", "[]", "[]", "{}", "[]", "{}"),
            )
            await db.execute(
                "INSERT INTO feedback_events (feedback_event_id, run_id, verdict, is_probe_turn) "
                "VALUES (?, ?, ?, ?)",
                (str(uuid.uuid4()), rid, "thumbs_up", 1),
            )
            await db.commit()
            await db.close()

        run(_insert_run_and_event())

        result = run(_section_56_probe_integrity(series_id, store))
        print(f"probe_integrity: {result}")
        assert result["status"] == "HARD_FAIL"
        assert result["violation_count"] == 1


def test_s56_probe_integrity_pass():
    """PASS when no probe-turn feedback events exist for the series."""
    with TempDB() as store:
        series_id = str(uuid.uuid4())
        result = run(_section_56_probe_integrity(series_id, store))
        print(f"probe_integrity: {result}")
        assert result["status"] == "PASS"
        assert result["violation_count"] == 0


# ---------------------------------------------------------------------------
# §5.7 — Mode-3 trigger count
# ---------------------------------------------------------------------------


def test_s57_anomaly_fires_for_short_system_mu():
    """system MU with content shorter than mode3_min_expected_length is flagged."""
    with TempDB() as store:
        series_id = str(uuid.uuid4())
        short_mu = _mu_id()
        run(store.create_mu(
            mu_id=short_mu,
            source_type="system",
            raw_content="short",  # 5 chars < 100 threshold
            series_id=series_id,
        ))

        result = run(_section_57_mode3_trigger_count(series_id, store, _CONFIG))
        print(f"mode3: {result}")
        assert result["mode3_mu_count"] == 1
        assert result["anomaly_count"] == 1
        assert result["anomalies"][0]["mu_id"] == short_mu


def test_s57_no_anomaly_for_long_system_mu():
    """system MU with content >= mode3_min_expected_length is not flagged."""
    with TempDB() as store:
        series_id = str(uuid.uuid4())
        long_mu = _mu_id()
        run(store.create_mu(
            mu_id=long_mu,
            source_type="system",
            raw_content="x" * 200,  # 200 chars ≥ 100 threshold
            series_id=series_id,
        ))

        result = run(_section_57_mode3_trigger_count(series_id, store, _CONFIG))
        print(f"mode3: {result}")
        assert result["mode3_mu_count"] == 1
        assert result["anomaly_count"] == 0


# ---------------------------------------------------------------------------
# §5.8 — num_ctx pressure
# ---------------------------------------------------------------------------


def test_s58_pressure_flag_fires_above_threshold():
    """Turn with tokens_in > 0.8 * context_window_tokens is flagged."""
    runs = [
        {
            "run_id": _run_id(),
            "turn_index": 1,
            "tokens_in": 850,  # > 0.8 * 1000 = 800
            "activated_mu_ids": [_mu_id(), _mu_id(), _mu_id()],  # 3 retrieved
        },
        {
            "run_id": _run_id(),
            "turn_index": 2,
            "tokens_in": 400,  # below threshold
            "activated_mu_ids": [_mu_id()],  # 1 retrieved
        },
    ]

    result = _section_58_num_ctx_pressure(runs, _CONFIG)
    print(f"pressure: {result}")
    assert result["pressure_turn_count"] == 1
    assert result["pressure_turns"][0]["turn_index"] == 1
    assert result["context_window_tokens"] == 1000


def test_s58_retrieval_volume_attribution_above_mean():
    """
    When the pressure turn has retrieved_count above the series mean,
    attributed_to_retrieval_volume is True.
    """
    # Turn 1: 10 MUs (above mean of 5.5), tokens_in=900 (flagged)
    # Turn 2: 1 MU, tokens_in=100 (not flagged)
    runs = [
        {
            "run_id": _run_id(),
            "turn_index": 1,
            "tokens_in": 900,
            "activated_mu_ids": [_mu_id() for _ in range(10)],
        },
        {
            "run_id": _run_id(),
            "turn_index": 2,
            "tokens_in": 100,
            "activated_mu_ids": [_mu_id()],
        },
    ]

    result = _section_58_num_ctx_pressure(runs, _CONFIG)
    print(f"pressure attribution: {result['pressure_turns']}")
    assert result["pressure_turn_count"] == 1
    # Series mean = (10 + 1) / 2 = 5.5, retrieved_count = 10 > 5.5
    assert result["pressure_turns"][0]["attributed_to_retrieval_volume"] is True


def test_s58_no_pressure_when_all_turns_below_threshold():
    """No pressure turns when all tokens_in are below 0.8 * context_window."""
    runs = [
        {
            "run_id": _run_id(),
            "turn_index": 1,
            "tokens_in": 500,
            "activated_mu_ids": [_mu_id()],
        },
        {
            "run_id": _run_id(),
            "turn_index": 2,
            "tokens_in": 200,
            "activated_mu_ids": [],
        },
    ]
    result = _section_58_num_ctx_pressure(runs, _CONFIG)
    print(f"no pressure: {result}")
    assert result["pressure_turn_count"] == 0


# ---------------------------------------------------------------------------
# resolve_report_path — reconstructs from the current _REPORTS_DIR rather
# than trusting a stored path verbatim (fixes host-side reads of a
# container-absolute path stored in generated_reports)
# ---------------------------------------------------------------------------


class TestResolveReportPath:
    def test_reconstructs_container_path_against_current_reports_dir(self):
        """A path stored as the in-container absolute path (the normal case
        — write and read happen in the same container) resolves against
        whatever _REPORTS_DIR currently is, using only the filename."""
        with mock.patch.object(series_report_module, "_REPORTS_DIR", Path("/host/memory/reports")):
            resolved = resolve_report_path("/app/data/reports/series_report_x.json")
        assert resolved == Path("/host/memory/reports/series_report_x.json")

    def test_ignores_stored_directory_entirely(self):
        """Only the basename of the stored path is trusted — any directory
        component (container or otherwise) is discarded."""
        with mock.patch.object(series_report_module, "_REPORTS_DIR", Path("/current/reports")):
            resolved = resolve_report_path("/completely/different/place/report.md")
        assert resolved == Path("/current/reports/report.md")

    def test_bare_filename_resolves_the_same_as_a_full_path(self):
        with mock.patch.object(series_report_module, "_REPORTS_DIR", Path("/current/reports")):
            assert resolve_report_path("report.json") == resolve_report_path(
                "/app/data/reports/report.json"
            )
