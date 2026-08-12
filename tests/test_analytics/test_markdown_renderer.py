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
    _render_s62,
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


def test_s61_renders_same_direction_streak_count_and_runaway_reasons():
    """
    M35: the informational same-direction streak count must render
    separately from (and never be conflated with) the runaway flag count,
    and each runaway entry must show WHY it flagged (accelerating |delta|
    and/or a saturation-threshold crossing) rather than just the raw deltas.
    """
    section = _base_s61_section({})
    section["saturation_analysis"] = {
        "saturation_threshold": 0.9,
        "total_unique_edges": 2,
        "saturation_flag_count": 0,
        "saturation_flags": [],
        "same_direction_streak_count": 2,
        "same_direction_streaks": [
            {"edge_id": "edge-decay", "consecutive_deltas": [-0.1667, -0.0833, -0.05], "starting_at_turn": 1},
            {"edge_id": "edge-accel", "consecutive_deltas": [0.05, 0.07, 0.10], "starting_at_turn": 1},
        ],
        "runaway_flag_count": 1,
        "runaway_flags": [
            {
                "edge_id": "edge-accel",
                "consecutive_deltas": [0.05, 0.07, 0.10],
                "starting_at_turn": 1,
                "non_decreasing_magnitude": True,
                "crosses_saturation_threshold": False,
            },
        ],
    }
    md = _render_s61(section)
    print(f"§6.1 saturation/runaway section:\n{md}")
    assert "Same-direction streaks (informational" in md
    assert "**Same-direction streaks (informational — not anomalies):** 2" in md
    assert "**Runaway flags:** 1" in md
    assert "accelerating |delta|" in md
    # The decaying (non-runaway) edge must not appear in the runaway list.
    runaway_section = md.split("Runaway-Reinforcement Edges")[-1]
    assert "edge-decay" not in runaway_section


# ---------------------------------------------------------------------------
# §6.2 Control Shadow — M34 rendering fixes
# ---------------------------------------------------------------------------


def _make_control_shadow_entry(
    control_name: str,
    condition_label: str,
    edge_weight_control: float = 0.0,
    sample_by_phenomenon: dict = None,
    sample_per_probe: list = None,
) -> dict:
    return {
        "experiment_id": f"exp-{control_name}",
        "condition_label": condition_label,
        "learning_measures": {},
        "sanity_checks": {
            "zero_weight_changes": {"status": "PASS", "violation_count": 0, "violating_run_ids": []},
        },
        "comparative_deltas": {
            "probe_pass_rate_delta": 0.5,
            "probe_pass_rate_learned": 0.8,
            "probe_pass_rate_control": 0.3,
            "bridge_mu_rank_delta": -1.2,
            "bridge_mu_rank_learned": 2.0,
            "bridge_mu_rank_control": 3.2,
            "edge_weight_delta_magnitude": 0.1109,
            "edge_weight_delta_magnitude_learned": 0.1109,
            "edge_weight_delta_magnitude_control": edge_weight_control,
            "mean_gold_mu_rank_delta": -0.5,
            "mean_gold_mu_rank_learned": 1.5,
            "mean_gold_mu_rank_control": 2.0,
        },
        "sample_pass_rate_comparison": {
            "per_probe": sample_per_probe or [],
            "by_phenomenon": sample_by_phenomenon or {},
        },
    }


def test_s62_comparative_deltas_shows_learned_value_not_dash():
    """M34 bug: the Learned column previously hardcoded '—' on every row.
    It must now show the actual learned value."""
    control_shadow = {
        "static_weight": _make_control_shadow_entry("static_weight", "static-weight"),
    }
    md = _render_s62(control_shadow)
    print(f"§6.2 comparative deltas:\n{md}")
    assert "0.8000" in md  # probe_pass_rate_learned
    assert "2.0000" in md  # bridge_mu_rank_learned


def test_s62_edge_weight_delta_control_labeled_zero_by_design():
    """M34: a structurally-zero control value (static/random never receive
    weight_updates) is labeled '0 by design', not a bare 0.0000 that reads
    as an empirical near-zero result."""
    control_shadow = {
        "static_weight": _make_control_shadow_entry("static_weight", "static-weight", edge_weight_control=0.0),
    }
    md = _render_s62(control_shadow)
    print(f"§6.2 edge weight labeling:\n{md}")
    assert "0 by design" in md


def test_s62_edge_weight_delta_nonzero_control_shows_actual_value():
    """If the control's edge_weight value were ever genuinely nonzero (a
    bug), the renderer must show the real number, not silently relabel it."""
    control_shadow = {
        "random_weight": _make_control_shadow_entry("random_weight", "random-weight", edge_weight_control=0.042),
    }
    md = _render_s62(control_shadow)
    print(f"§6.2 nonzero control edge weight:\n{md}")
    assert "0.0420" in md
    assert "0 by design" not in md


def test_s62_condition_comparison_summary_shows_learned_value():
    """M34: the cross-condition summary table previously hardcoded
    '(baseline)' for Learned. It must show the actual learned value."""
    control_shadow = {
        "static_weight": _make_control_shadow_entry("static_weight", "static-weight"),
        "random_weight": _make_control_shadow_entry("random_weight", "random-weight"),
    }
    md = _render_s62(control_shadow)
    print(f"§6.2 condition comparison summary:\n{md}")
    assert "(baseline)" not in md
    assert "Condition Comparison Summary" in md


def test_s62_sample_pass_rate_comparison_renders_by_phenomenon():
    """M34: the new sample-mode comparison section renders its by-phenomenon
    table, labeled distinctly from the mean-mode comparison."""
    control_shadow = {
        "static_weight": _make_control_shadow_entry(
            "static_weight", "static-weight",
            sample_by_phenomenon={
                "spoke_retrieval": {
                    "probe_count": 2,
                    "mean_delta": 0.13,
                    "final_session_mean_delta": 0.26,
                },
            },
            sample_per_probe=[
                {
                    "probe_id": "vs_d4",
                    "phenomenon_type": "spoke_retrieval",
                    "sessions": [
                        {"session_index": 1, "learned_pass_rate": 0.04,
                         "control_pass_rate": 0.04, "delta": 0.0},
                        {"session_index": 5, "learned_pass_rate": 0.60,
                         "control_pass_rate": 0.08, "delta": 0.52},
                    ],
                },
            ],
        ),
    }
    md = _render_s62(control_shadow)
    print(f"§6.2 sample-mode comparison:\n{md}")
    assert "Sample-Mode Pass-Rate Comparison" in md
    assert "weight_mode=sample" in md
    assert "spoke_retrieval" in md
    assert "0.13" in md
    assert "vs_d4" in md
    assert "0.52" in md


def test_s62_sample_pass_rate_comparison_absent_renders_message_not_crash():
    """Pre-M30 experiments (or a mismatched probe set) have no sample-mode
    data — must render an explanatory message, not crash."""
    control_shadow = {
        "static_weight": _make_control_shadow_entry("static_weight", "static-weight"),
    }
    md = _render_s62(control_shadow)
    print(f"§6.2 no sample data:\n{md}")
    assert "unavailable" in md.lower()


def test_s62_no_control_shadow_renders_not_computed():
    md = _render_s62(None)
    assert "Not computed" in md


# ---------------------------------------------------------------------------
# M39 — trained probes, cluster winner table, trained-subset comparison,
# pin-adjusted mean deltas
# ---------------------------------------------------------------------------


def test_s61_renders_trained_probes_and_cluster_winner_table():
    section = _base_s61_section({})
    section["trained_probes"] = {
        "cluster_a": {"spoke_key": "a_spoke1", "mu_id": "a0000002-aaaa-4aaa-8aaa-000000000002", "probe_id": "vs_a1"},
        "cluster_b": {"spoke_key": "b_spoke1", "mu_id": "b0000002-bbbb-4bbb-8bbb-000000000002", "probe_id": None},
    }
    section["cluster_winner_table"] = {
        "cluster_a": {
            "sessions": [
                {"session_index": 0, "winner_probe_id": "vs_a1", "winner_pass_rate": 0.9,
                 "trained": True, "tied_with": []},
            ],
            "final_session_winner": {
                "session_index": 0, "winner_probe_id": "vs_a1", "winner_pass_rate": 0.9,
                "trained": True, "tied_with": [],
            },
        },
    }

    md = _render_s61(section)
    print(f"§6.1 trained probes + cluster winner table:\n{md}")
    assert "Trained Probes (M39)" in md
    assert "a_spoke1" in md
    assert "vs_a1" in md
    assert "no matching probe" in md  # cluster_b's unresolved probe_id
    assert "Per-Cluster Winner Table (M39)" in md
    assert "✅ trained" in md


def test_s61_trained_probes_absent_renders_without_crash():
    """Pre-M39 stored report JSON has neither key — must render cleanly,
    not crash (M32 backward-compat rule)."""
    section = _base_s61_section({})
    md = _render_s61(section)
    assert "Trained Probes (M39)" not in md
    assert "Per-Cluster Winner Table (M39)" not in md


def test_s62_trained_subset_comparison_renders():
    entry = _make_control_shadow_entry("static_weight", "static-weight")
    entry["trained_subset_comparison"] = {
        "trained_probe_count": 1,
        "matched_probe_count": 1,
        "per_probe": [],
        "mean_delta_by_session": [
            {"session_index": 0, "mean_delta": -0.75, "probe_count": 1},
            {"session_index": 4, "mean_delta": -0.94, "probe_count": 1},
        ],
        "final_session_mean_delta": -0.94,
    }
    control_shadow = {"static_weight": entry}

    md = _render_s62(control_shadow)
    print(f"§6.2 trained-subset comparison:\n{md}")
    assert "Trained-Subset Comparison" in md
    assert "-0.94" in md


def test_s62_trained_subset_comparison_absent_renders_message_not_crash():
    control_shadow = {"static_weight": _make_control_shadow_entry("static_weight", "static-weight")}
    md = _render_s62(control_shadow)
    assert "no `trained_spokes` was supplied" in md


# ---------------------------------------------------------------------------
# M41 — pin-adjusted trained-subset delta rendering
# ---------------------------------------------------------------------------


def test_s62_trained_subset_pin_adjustment_renders_side_by_side():
    """The rep3-shaped case: two trained probes, one control-pinned -- both
    the raw and excluding-pinned means render, each with its own probe
    count, and the block is retitled (M39/M41)."""
    entry = _make_control_shadow_entry("static_weight", "static-weight")
    entry["trained_subset_comparison"] = {
        "trained_probe_count": 2,
        "matched_probe_count": 2,
        "per_probe": [],
        "mean_delta_by_session": [
            {"session_index": 4, "mean_delta": -0.1, "probe_count": 2},
        ],
        "final_session_mean_delta": -0.1,
        "pinned_trained_probe_ids": ["vs_a1"],
        "matched_probe_count_excluding_pinned": 1,
        "mean_delta_by_session_excluding_pinned": [
            {"session_index": 4, "mean_delta": 0.76, "probe_count": 1},
        ],
        "final_session_mean_delta_excluding_pinned": 0.76,
    }
    control_shadow = {"static_weight": entry}

    md = _render_s62(control_shadow)
    print(f"§6.2 trained-subset pin-adjustment:\n{md}")
    assert "(M39/M41)" in md
    assert "vs_a1" in md
    assert "-0.1000" in md
    assert "0.7600" in md
    assert "(n=2)" in md
    assert "(n=1)" in md


def test_s62_trained_subset_all_pinned_renders_dash_and_note_not_zero():
    """The all-pinned hazard: '-' plus the explicit note, never '0.0000'
    (which would misread as 'no effect')."""
    entry = _make_control_shadow_entry("static_weight", "static-weight")
    entry["trained_subset_comparison"] = {
        "trained_probe_count": 2,
        "matched_probe_count": 2,
        "per_probe": [],
        "mean_delta_by_session": [
            {"session_index": 4, "mean_delta": -0.96, "probe_count": 2},
        ],
        "final_session_mean_delta": -0.96,
        "pinned_trained_probe_ids": ["vs_a1", "vs_c1"],
        "matched_probe_count_excluding_pinned": 0,
        "mean_delta_by_session_excluding_pinned": [],
        "final_session_mean_delta_excluding_pinned": None,
    }
    control_shadow = {"static_weight": entry}

    md = _render_s62(control_shadow)
    print(f"§6.2 trained-subset all-pinned:\n{md}")
    assert "all trained probes" in md.lower()
    assert "control-pinned" in md.lower()
    assert "0.0000" not in md
    # the excl-pinned headline figure renders as the dash, not a fabricated 0
    assert "excl. pinned:** —" in md


def test_s62_trained_subset_pre_m41_data_omits_pinned_line_no_false_note():
    """Backward compat (M32 rule): a pre-M41 trained_subset_comparison dict
    (M39 shape, no pin-adjustment keys) must render '-' in the new columns,
    omit the pinned-probes line entirely, and must NOT trigger the
    all-pinned note just because the defaulted excluding-pinned count is 0."""
    entry = _make_control_shadow_entry("static_weight", "static-weight")
    entry["trained_subset_comparison"] = {
        "trained_probe_count": 1,
        "matched_probe_count": 1,
        "per_probe": [],
        "mean_delta_by_session": [
            {"session_index": 4, "mean_delta": -0.94, "probe_count": 1},
        ],
        "final_session_mean_delta": -0.94,
        # no pinned_trained_probe_ids / matched_probe_count_excluding_pinned /
        # mean_delta_by_session_excluding_pinned / final_session_mean_delta_excluding_pinned
    }
    control_shadow = {"static_weight": entry}

    md = _render_s62(control_shadow)
    print(f"§6.2 pre-M41 trained-subset data:\n{md}")
    assert "Control-pinned trained probes" not in md
    assert "all trained probes" not in md.lower()
    assert "-0.9400" in md
    assert "excl. pinned:** —" in md


def test_s62_pin_adjusted_mean_deltas_renders():
    entry = _make_control_shadow_entry("static_weight", "static-weight")
    entry["pin_adjusted_mean_deltas"] = {
        "pinned_probe_ids": ["vs_a1", "vs_d5"],
        "by_phenomenon_all_probes": {
            "spoke_retrieval": {"probe_count": 4, "mean_delta": -0.2163, "final_session_mean_delta": -0.165},
        },
        "by_phenomenon_excluding_pinned": {
            "spoke_retrieval": {"probe_count": 2, "mean_delta": 0.4125, "final_session_mean_delta": 0.61},
        },
    }
    control_shadow = {"static_weight": entry}

    md = _render_s62(control_shadow)
    print(f"§6.2 pin-adjusted mean deltas:\n{md}")
    assert "Pin-Adjusted Mean Deltas (M39)" in md
    assert "vs_a1, vs_d5" in md
    assert "0.4125" in md


def test_s62_cluster_winner_table_control_side_renders():
    entry = _make_control_shadow_entry("static_weight", "static-weight")
    entry["learning_measures"] = {
        "cluster_winner_table": {
            "cluster_a": {
                "sessions": [],
                "final_session_winner": {
                    "session_index": 4, "winner_probe_id": "vs_a1",
                    "winner_pass_rate": 1.0, "trained": True, "tied_with": [],
                },
            },
        },
    }
    control_shadow = {"static_weight": entry}

    md = _render_s62(control_shadow)
    print(f"§6.2 control-side cluster winner table:\n{md}")
    assert "Per-Cluster Winner Table — static_weight (M39)" in md
    assert "vs_a1" in md


def test_s62_m39_sections_all_absent_render_without_crash():
    """Pre-M39 control_shadow entries have none of trained_subset_comparison,
    pin_adjusted_mean_deltas, or a cluster_winner_table under learning_measures.
    The whole section must still render cleanly."""
    control_shadow = {"static_weight": _make_control_shadow_entry("static_weight", "static-weight")}
    md = _render_s62(control_shadow)
    assert "Per-Cluster Winner Table — static_weight (M39)" not in md
    assert "Pin-Adjusted Mean Deltas (M39)" not in md
