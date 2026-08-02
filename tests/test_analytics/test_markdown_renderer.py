"""
Tests for analytics/reports/markdown_renderer.py.

Covers:
  - All 8 section headers appear in the Markdown output
  - render_series_report_markdown returns a non-empty string
  - Each section function produces non-empty Markdown
  - Section with empty data renders gracefully (no exception)
"""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "backend"))

from analytics.reports.markdown_renderer import (
    render_series_report_markdown,
    _render_s51,
    _render_s52,
    _render_s53,
    _render_s54,
    _render_s55,
    _render_s56,
    _render_s57,
    _render_s58,
    _render_s61,
)

SECTION_HEADERS = [
    "§5.1 Failure Summary",
    "§5.2 Retrieval Trace Digest",
    "§5.3 Intra-Series vs. Cross-Series Retrieval Rate",
    "§5.4 Cross-Series Edges",
    "§5.5 Edge Weight Deltas",
    "§5.6 Probe Integrity Check",
    "§5.7 Mode-3 Trigger Count",
    "§5.8 num_ctx Pressure",
]


def _make_report() -> dict:
    """Minimal but structurally valid report dict for rendering tests."""
    return {
        "report_id": "report-abc123",
        "report_type": "series",
        "experiment_id": "exp-2026-07-01",
        "series_id": "series-1",
        "generated_at": "2026-07-01T12:00:00+00:00",
        "run_count": 3,
        "sections": {
            "s5_1_failure_summary": {
                "failed_turn_count": 1,
                "failed_turns": [
                    {"run_id": "run-abc", "turn_index": 2, "failure_reason": "token_limit_exhausted"}
                ],
                "ghost_mu_count": 0,
                "ghost_incidents": [],
            },
            "s5_2_retrieval_trace_digest": {
                "turn_count": 3,
                "traces": [
                    {
                        "turn_index": 1,
                        "run_id": "run-001",
                        "bm25_candidate_count": 5,
                        "entry_point_mu_ids_short": ["abc12345", "def67890"],
                        "traversal_hop_count": 1,
                        "retrieved_mu_count": 2,
                        "retrieved_mu_ids_short": ["abc12345", "def67890"],
                    }
                ],
            },
            "s5_3_retrieval_rate_classification": {
                "total_retrieved": 10,
                "intra_series_count": 7,
                "seed_fixture_count": 2,
                "cross_series_count": 1,
                "unknown_count": 0,
                "intra_series_rate": 0.7,
                "per_turn": [
                    {
                        "turn_index": 1,
                        "run_id": "run-001",
                        "total_retrieved": 10,
                        "intra_series": 7,
                        "seed_fixture": 2,
                        "cross_series": 1,
                        "unknown": 0,
                    }
                ],
            },
            "s5_4_cross_series_edges": {
                "cross_series_edge_count": 2,
                "edges": [
                    {
                        "edge_id": "edge-abc123",
                        "weight": 0.6,
                        "source_series": "series-1",
                        "source_mu": "mu-src",
                        "target_series": "series-2",
                        "target_mu": "mu-tgt",
                    }
                ],
            },
            "s5_5_edge_weight_deltas": {
                "activated_edge_count": 3,
                "deltas": [
                    {
                        "edge_id": "edge-001",
                        "start_weight": 0.5,
                        "end_weight": 0.7,
                        "delta": 0.2,
                        "traversal_count_total": 4,
                        "update_count_in_series": 2,
                    }
                ],
                "zero_delta_high_traversal_flags": [],
            },
            "s5_6_probe_integrity": {
                "status": "PASS",
                "violation_count": 0,
                "violations": [],
            },
            "s5_7_mode3_trigger_count": {
                "mode3_mu_count": 2,
                "min_expected_length": 100,
                "anomaly_count": 0,
                "anomalies": [],
            },
            "s5_8_num_ctx_pressure": {
                "context_window_tokens": 8192,
                "pressure_threshold_ratio": 0.8,
                "series_mean_retrieved_count": 2.5,
                "pressure_turn_count": 0,
                "pressure_turns": [],
            },
        },
    }


def test_all_section_headers_present():
    """render_series_report_markdown includes all 8 §5.N headers."""
    report = _make_report()
    md = render_series_report_markdown(report)
    print(f"Markdown length: {len(md)} chars")
    for header in SECTION_HEADERS:
        assert header in md, f"Missing section header: {header}"


def test_render_returns_nonempty_string():
    """render_series_report_markdown returns a non-empty string."""
    report = _make_report()
    md = render_series_report_markdown(report)
    assert isinstance(md, str)
    assert len(md) > 0


def test_report_metadata_in_header():
    """Experiment ID, series ID, and run count appear in the rendered document."""
    report = _make_report()
    md = render_series_report_markdown(report)
    assert "exp-2026-07-01" in md
    assert "series-1" in md
    assert "3" in md  # run_count


def test_each_section_function_nonempty():
    """Each individual section render function produces non-empty Markdown."""
    assert len(_render_s51({})) > 0
    assert len(_render_s52({})) > 0
    assert len(_render_s53({})) > 0
    assert len(_render_s54({})) > 0
    assert len(_render_s55({})) > 0
    assert len(_render_s56({})) > 0
    assert len(_render_s57({})) > 0
    assert len(_render_s58({})) > 0


def test_render_with_empty_sections_no_exception():
    """render_series_report_markdown handles an empty sections dict without raising."""
    report = {
        "report_id": "r1",
        "report_type": "series",
        "experiment_id": "exp",
        "series_id": "series",
        "generated_at": "2026-01-01T00:00:00+00:00",
        "run_count": 0,
        "sections": {},
    }
    md = render_series_report_markdown(report)
    print(f"Empty-sections MD length: {len(md)}")
    assert isinstance(md, str)
    assert len(md) > 0


def test_probe_integrity_pass_shows_correct_status():
    """PASS status renders clearly in the probe integrity section."""
    section = {"status": "PASS", "violation_count": 0, "violations": []}
    md = _render_s56(section)
    assert "PASS" in md
    assert "0" in md


def test_probe_integrity_hard_fail_shows_status():
    """HARD FAIL status and violations are visible in the rendered section."""
    section = {
        "status": "HARD_FAIL",
        "violation_count": 2,
        "violations": [
            {"feedback_event_id": "fe-abc123456789", "run_id": "run-abc123456789", "verdict": "thumbs_up", "turn_index": 3},
        ],
    }
    md = _render_s56(section)
    print(f"Hard fail section:\n{md}")
    assert "HARD FAIL" in md
    assert "thumbs_up" in md


def test_failure_summary_with_ghost_mus():
    """Ghost-MU incidents are visible in §5.1 when present."""
    section = {
        "failed_turn_count": 1,
        "failed_turns": [{"run_id": "run-abc", "turn_index": 1, "failure_reason": "empty_output"}],
        "ghost_mu_count": 1,
        "ghost_incidents": [
            {"ghost_mu_id": "mu-ghost-abc", "appearing_in_run_id": "run-def", "appearing_at_turn_index": 3}
        ],
    }
    md = _render_s51(section)
    print(f"Ghost MU section:\n{md}")
    assert "Ghost-MU" in md
    assert "empty_output" in md


def test_s53_renders_seed_and_cross_rate():
    """M32: §5.3's new seed_fixture_rate / cross_series_rate render as
    percentage lines alongside the existing intra-series rate."""
    section = {
        "total_retrieved": 10,
        "intra_series_count": 5,
        "seed_fixture_count": 4,
        "cross_series_count": 1,
        "unknown_count": 0,
        "intra_series_rate": 0.5,
        "seed_fixture_rate": 0.4,
        "cross_series_rate": 0.1,
        "per_turn": [],
    }
    md = _render_s53(section)
    print(f"§5.3 with seed/cross rates:\n{md}")
    assert "Seed-fixture rate" in md
    assert "40.0%" in md
    assert "Cross-series rate" in md
    assert "10.0%" in md


def test_s61_renders_sample_pass_rate_section():
    """M30: §6.1's new Sample-Repeat Pass Rate block renders its header and
    flags a partial-battery session with the ⚠ PARTIAL BATTERY marker."""
    section = {
        "condition_label": "learned",
        "pass_rank_threshold": 3,
        "probe_count": 1,
        "run_count": 1,
        "state_transitions": {"per_probe": [], "by_phenomenon": {}},
        "edge_delta_ledger": {},
        "bridge_mu_analysis": {},
        "saturation_analysis": {},
        "sample_battery_repeats_configured": 25,
        "sample_pass_rates": {
            "per_probe": [
                {
                    "probe_id": "vs_a1",
                    "phenomenon_type": "spoke_retrieval",
                    "sessions": [
                        {
                            "session_index": 0,
                            "pass_count": 20,
                            "fail_count": 5,
                            "repeats_completed": 25,
                            "pass_rate": 0.8,
                            "partial_battery": False,
                        },
                        {
                            "session_index": 1,
                            "pass_count": 5,
                            "fail_count": 5,
                            "repeats_completed": 10,
                            "pass_rate": 0.5,
                            "partial_battery": True,
                        },
                    ],
                },
            ],
            "by_phenomenon": {},
        },
    }

    md = _render_s61(section)
    print(f"§6.1 sample pass rate section:\n{md}")
    assert "Sample-Repeat Pass Rate" in md
    assert "weight_mode=sample" in md
    assert "vs_a1" in md
    assert "0.8" in md
    assert "⚠ PARTIAL BATTERY" in md


def _base_s61_section(edge_delta_ledger: dict) -> dict:
    return {
        "condition_label": "learned",
        "pass_rank_threshold": 3,
        "probe_count": 1,
        "run_count": 1,
        "state_transitions": {"per_probe": [], "by_phenomenon": {}},
        "edge_delta_ledger": edge_delta_ledger,
        "bridge_mu_analysis": {},
        "saturation_analysis": {},
        "sample_pass_rates": {"per_probe": [], "by_phenomenon": {}},
    }


def test_s61_renders_edge_classification():
    """M33: the typed edge-class x cluster-relation breakdown renders as a
    table, plus the cross-domain watch list and per-series longitudinal view."""
    ledger = {
        "total_updates": 3,
        "increased_count": 2,
        "decreased_count": 1,
        "unchanged_count": 0,
        "mean_abs_delta": 0.05,
        "orphan_update_count": 0,
        "edge_classification": {
            "by_class_relation": {
                "seed_seed": {
                    "same_cluster": {"count": 1, "mean_abs_delta": 0.02},
                    "cross_cluster": {"count": 0, "mean_abs_delta": 0.0},
                    "mixed": {"count": 0, "mean_abs_delta": 0.0},
                    "unresolvable": {"count": 0, "mean_abs_delta": 0.0},
                },
                "conv_seed": {
                    "same_cluster": {"count": 0, "mean_abs_delta": 0.0},
                    "cross_cluster": {"count": 0, "mean_abs_delta": 0.0},
                    "mixed": {"count": 1, "mean_abs_delta": 0.1},
                    "unresolvable": {"count": 0, "mean_abs_delta": 0.0},
                },
                "conv_conv": {
                    "same_cluster": {"count": 0, "mean_abs_delta": 0.0},
                    "cross_cluster": {"count": 1, "mean_abs_delta": 0.03},
                    "mixed": {"count": 0, "mean_abs_delta": 0.0},
                    "unresolvable": {"count": 0, "mean_abs_delta": 0.0},
                },
            },
            "totals_by_relation": {
                "same_cluster": 1, "cross_cluster": 1, "mixed": 1, "unresolvable": 0,
            },
            "cross_domain_watch": [
                {
                    "edge_id": "edge-mixed-1",
                    "edge_class": "conv_seed",
                    "cluster_relation": "mixed",
                    "delta": 0.1,
                    "source_cluster": "cluster_a",
                    "target_cluster": "cluster_b",
                    "feedback_event_id": "fe-1",
                },
            ],
            "by_series": [
                {
                    "series_id": "series-0",
                    "by_class": {
                        "seed_seed": {"count": 1, "mean_abs_delta": 0.02},
                        "conv_seed": {"count": 1, "mean_abs_delta": 0.1},
                        "conv_conv": {"count": 1, "mean_abs_delta": 0.03},
                    },
                },
            ],
        },
    }
    md = _render_s61(_base_s61_section(ledger))
    print(f"§6.1 edge classification section:\n{md}")
    assert "Edge Classification" in md
    assert "seed↔seed" in md
    assert "conv→seed" in md
    assert "conv↔conv" in md
    assert "Totals by relation" in md
    assert "Cross-Domain Linkage Watch List" in md
    assert "edge-mix" in md
    assert "Edge Classification by Series" in md
    assert "series-0" in md
    # Old flat within/cross/unknown keys must not appear anywhere.
    assert "Within-cluster" not in md
    assert "Unknown cluster" not in md


def test_s61_edge_classification_absent_renders_without_crash():
    """A pre-M33 stored report JSON has no 'edge_classification' key on its
    ledger (only the old within/cross/unknown flat counts, since removed).
    The renderer must degrade gracefully rather than crash."""
    old_shape_ledger = {
        "total_updates": 3,
        "increased_count": 2,
        "decreased_count": 1,
        "unchanged_count": 0,
        "mean_abs_delta": 0.05,
        "orphan_update_count": 0,
        "within_cluster_count": 1,
        "cross_cluster_count": 1,
        "unknown_cluster_count": 1,
        "cross_cluster_deltas": [],
    }
    md = _render_s61(_base_s61_section(old_shape_ledger))
    print(f"§6.1 pre-M33 ledger shape:\n{md}")
    assert "Edge Weight Delta Ledger" in md
    assert "Edge Classification" not in md
