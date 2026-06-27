"""
Markdown renderer for Series Reports.

Takes the structured dict produced by series_report.generate_series_report()
and renders a human-readable Markdown document. Each section corresponds to
a §5.N section of the experiment report spec.
"""

from typing import Any


def _truncate(value: Any, max_chars: int = 60) -> str:
    """Convert value to string and truncate for display."""
    s = str(value) if value is not None else "—"
    return s[:max_chars] + "…" if len(s) > max_chars else s


def _render_s51(section: dict) -> str:
    lines = [
        "## §5.1 Failure Summary\n",
        f"**Failed turns:** {section.get('failed_turn_count', 0)}  ",
        f"**Ghost-MU incidents:** {section.get('ghost_mu_count', 0)}\n",
    ]

    failed = section.get("failed_turns") or []
    if failed:
        lines.append("\n### Failed Turns\n")
        lines.append("| turn_index | run_id | failure_reason |")
        lines.append("|---|---|---|")
        for f in failed:
            lines.append(
                f"| {f.get('turn_index', '—')} "
                f"| {str(f.get('run_id', '—'))[:12]}… "
                f"| {f.get('failure_reason', '—')} |"
            )

    ghosts = section.get("ghost_incidents") or []
    if ghosts:
        lines.append("\n### Ghost-MU Incidents\n")
        lines.append("| ghost_mu_id | appearing_in_run | turn_index |")
        lines.append("|---|---|---|")
        for g in ghosts:
            lines.append(
                f"| {str(g.get('ghost_mu_id', '—'))[:12]}… "
                f"| {str(g.get('appearing_in_run_id', '—'))[:12]}… "
                f"| {g.get('appearing_at_turn_index', '—')} |"
            )
    else:
        lines.append("\n_No ghost-MU incidents detected._")

    return "\n".join(lines)


def _render_s52(section: dict) -> str:
    lines = [
        "## §5.2 Retrieval Trace Digest\n",
        f"**Turns:** {section.get('turn_count', 0)}\n",
        "| turn | bm25_candidates | entry_points | hops | retrieved |",
        "|---|---|---|---|---|",
    ]
    for trace in section.get("traces") or []:
        entry = ", ".join(trace.get("entry_point_mu_ids_short") or []) or "—"
        retrieved = ", ".join(trace.get("retrieved_mu_ids_short") or []) or "—"
        lines.append(
            f"| {trace.get('turn_index', '—')} "
            f"| {trace.get('bm25_candidate_count', 0)} "
            f"| {entry} "
            f"| {trace.get('traversal_hop_count', 0)} "
            f"| {retrieved} |"
        )
    return "\n".join(lines)


def _render_s53(section: dict) -> str:
    lines = [
        "## §5.3 Intra-Series vs. Cross-Series Retrieval Rate\n",
        f"**Total retrieved:** {section.get('total_retrieved', 0)}  ",
        f"**Intra-series:** {section.get('intra_series_count', 0)}  ",
        f"**Seed fixture:** {section.get('seed_fixture_count', 0)}  ",
        f"**Cross-series:** {section.get('cross_series_count', 0)}  ",
        f"**Unknown (pre-M12):** {section.get('unknown_count', 0)}  ",
        f"**Intra-series rate:** {section.get('intra_series_rate', 0.0):.1%}\n",
        "| turn | total | intra | seed | cross | unknown |",
        "|---|---|---|---|---|---|",
    ]
    for pt in section.get("per_turn") or []:
        lines.append(
            f"| {pt.get('turn_index', '—')} "
            f"| {pt.get('total_retrieved', 0)} "
            f"| {pt.get('intra_series', 0)} "
            f"| {pt.get('seed_fixture', 0)} "
            f"| {pt.get('cross_series', 0)} "
            f"| {pt.get('unknown', 0)} |"
        )
    return "\n".join(lines)


def _render_s54(section: dict) -> str:
    lines = [
        "## §5.4 Cross-Series Edges\n",
        f"**Cross-series edge count:** {section.get('cross_series_edge_count', 0)}\n",
    ]
    edges = section.get("edges") or []
    if edges:
        lines.append("| edge_id | weight | source_series | source_mu | target_series | target_mu |")
        lines.append("|---|---|---|---|---|---|")
        for e in edges[:50]:  # cap display at 50 rows
            lines.append(
                f"| {str(e.get('edge_id', ''))[:8]}… "
                f"| {e.get('weight', '—'):.3f} "
                f"| {e.get('source_series') or '—'} "
                f"| {str(e.get('source_mu', ''))[:8]}… "
                f"| {e.get('target_series') or '—'} "
                f"| {str(e.get('target_mu', ''))[:8]}… |"
            )
        if len(edges) > 50:
            lines.append(f"\n_…and {len(edges) - 50} more (see JSON for full list)_")
    else:
        lines.append("_No cross-series edges found._")
    return "\n".join(lines)


def _render_s55(section: dict) -> str:
    lines = [
        "## §5.5 Edge Weight Deltas (Series-Scoped)\n",
        f"**Activated edge count:** {section.get('activated_edge_count', 0)}\n",
    ]
    deltas = section.get("deltas") or []
    if deltas:
        lines.append("| edge_id | start_weight | end_weight | delta | traversals | updates |")
        lines.append("|---|---|---|---|---|---|")
        for d in deltas[:30]:  # top 30 by |delta|
            lines.append(
                f"| {str(d.get('edge_id', ''))[:8]}… "
                f"| {d.get('start_weight', '—')} "
                f"| {d.get('end_weight', '—')} "
                f"| {d.get('delta', 0.0):+.4f} "
                f"| {d.get('traversal_count_total', 0)} "
                f"| {d.get('update_count_in_series', 0)} |"
            )
        if len(deltas) > 30:
            lines.append(f"\n_…and {len(deltas) - 30} more (see JSON for full list)_")

    flags = section.get("zero_delta_high_traversal_flags") or []
    if flags:
        lines.append("\n### Zero-Delta, High-Traversal Flags\n")
        lines.append("| edge_id | traversal_count | weight |")
        lines.append("|---|---|---|")
        for f in flags:
            lines.append(
                f"| {str(f.get('edge_id', ''))[:8]}… "
                f"| {f.get('traversal_count', 0)} "
                f"| {f.get('weight', '—')} |"
            )
    return "\n".join(lines)


def _render_s56(section: dict) -> str:
    status = section.get("status", "UNKNOWN")
    status_display = "✅ PASS" if status == "PASS" else "❌ HARD FAIL"
    lines = [
        "## §5.6 Probe Integrity Check\n",
        f"**Status:** {status_display}  ",
        f"**Violation count:** {section.get('violation_count', 0)}\n",
    ]
    violations = section.get("violations") or []
    if violations:
        lines.append("| feedback_event_id | run_id | verdict | turn_index |")
        lines.append("|---|---|---|---|")
        for v in violations:
            lines.append(
                f"| {str(v.get('feedback_event_id', '—'))[:12]}… "
                f"| {str(v.get('run_id', '—'))[:12]}… "
                f"| {v.get('verdict', '—')} "
                f"| {v.get('turn_index', '—')} |"
            )
    return "\n".join(lines)


def _render_s57(section: dict) -> str:
    lines = [
        "## §5.7 Mode-3 Trigger Count\n",
        f"**Mode-3 MUs (summarization outputs):** {section.get('mode3_mu_count', 0)}  ",
        f"**Min expected length:** {section.get('min_expected_length', 100)} chars  ",
        f"**Anomaly count (suspiciously short):** {section.get('anomaly_count', 0)}\n",
    ]
    anomalies = section.get("anomalies") or []
    if anomalies:
        lines.append("### Anomalous Mode-3 MUs\n")
        lines.append("| mu_id | content_length | created_at |")
        lines.append("|---|---|---|")
        for a in anomalies:
            lines.append(
                f"| {str(a.get('mu_id', '—'))[:12]}… "
                f"| {a.get('content_length', '—')} "
                f"| {a.get('created_at', '—')} |"
            )
    else:
        lines.append("_No anomalies detected._")
    return "\n".join(lines)


def _render_s58(section: dict) -> str:
    lines = [
        "## §5.8 num_ctx Pressure\n",
        f"**Context window:** {section.get('context_window_tokens', 0):,} tokens  ",
        f"**Pressure threshold:** {section.get('pressure_threshold_ratio', 0.8):.0%}  ",
        f"**Series mean retrieved count:** {section.get('series_mean_retrieved_count', 0.0):.1f}  ",
        f"**Pressure turns:** {section.get('pressure_turn_count', 0)}\n",
    ]
    turns = section.get("pressure_turns") or []
    if turns:
        lines.append("| turn | tokens_in | pressure_ratio | retrieved | above_mean_retrieval |")
        lines.append("|---|---|---|---|---|")
        for t in turns:
            lines.append(
                f"| {t.get('turn_index', '—')} "
                f"| {t.get('tokens_in', 0):,} "
                f"| {t.get('pressure_ratio', 0.0):.1%} "
                f"| {t.get('retrieved_count', 0)} "
                f"| {t.get('attributed_to_retrieval_volume', False)} |"
            )
    else:
        lines.append("_No pressure turns detected._")
    return "\n".join(lines)


def render_series_report_markdown(report: dict) -> str:
    """
    Render a series report dict as a Markdown document.

    All 8 §5 sections are rendered in order. The caller writes the result
    to disk; this function is pure (no I/O).
    """
    sections = report.get("sections") or {}
    generated_at = report.get("generated_at", "unknown")
    experiment_id = report.get("experiment_id", "unknown")
    series_id = report.get("series_id", "unknown")
    run_count = report.get("run_count", 0)

    header = (
        f"# Series Report\n\n"
        f"**Experiment:** `{experiment_id}`  \n"
        f"**Series:** `{series_id}`  \n"
        f"**Generated:** {generated_at}  \n"
        f"**Runs in series:** {run_count}\n"
    )

    body_parts = [
        header,
        _render_s51(sections.get("s5_1_failure_summary") or {}),
        _render_s52(sections.get("s5_2_retrieval_trace_digest") or {}),
        _render_s53(sections.get("s5_3_retrieval_rate_classification") or {}),
        _render_s54(sections.get("s5_4_cross_series_edges") or {}),
        _render_s55(sections.get("s5_5_edge_weight_deltas") or {}),
        _render_s56(sections.get("s5_6_probe_integrity") or {}),
        _render_s57(sections.get("s5_7_mode3_trigger_count") or {}),
        _render_s58(sections.get("s5_8_num_ctx_pressure") or {}),
    ]

    return "\n\n---\n\n".join(body_parts) + "\n"
