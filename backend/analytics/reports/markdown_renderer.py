"""
Markdown renderer for Series Reports.

Takes the structured dict produced by series_report.generate_series_report()
and renders a human-readable Markdown document. Each section corresponds to
a §5.N section of the experiment report spec.
"""

from typing import Any, Optional


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
        f"**Intra-series rate:** {section.get('intra_series_rate', 0.0):.1%}  ",
        f"**Seed-fixture rate:** {section.get('seed_fixture_rate', 0.0):.1%}  ",
        f"**Cross-series rate:** {section.get('cross_series_rate', 0.0):.1%}\n",
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


def _render_s61(section: dict) -> str:
    """Render §6.1 Learning Measures as Markdown."""
    if section is None:
        return "## §6.1 Learning Measures\n\n_Not yet computed._"

    # Build threshold display: per-phenomenon if available, else global
    phenomenon_thresholds = section.get("phenomenon_thresholds") or {}
    if phenomenon_thresholds:
        threshold_parts = ", ".join(
            f"{ph}={t}" for ph, t in sorted(phenomenon_thresholds.items())
        )
        threshold_str = f"**Pass-rank thresholds:** {threshold_parts}  "
    else:
        threshold_str = f"**Pass-rank threshold:** top-{section.get('pass_rank_threshold', 3)}  "

    lines = [
        "## §6.1 Learning Measures\n",
        f"**Condition:** `{section.get('condition_label', '—')}`  ",
        threshold_str,
        f"**Probes:** {section.get('probe_count', 0)}  ",
        f"**Runs with weight updates:** {section.get('run_count', 0)}\n",
    ]

    # State transitions
    st = section.get("state_transitions") or {}
    lines += [
        "### State Transitions\n",
        "| probe_id | phenomenon | pattern | fail→pass | pass→fail |",
        "|---|---|---|---|---|",
    ]
    for p in st.get("per_probe") or []:
        lines.append(
            f"| {p.get('probe_id', '—')} "
            f"| {p.get('phenomenon_type', '—')} "
            f"| {p.get('pattern', '—')} "
            f"| {p.get('fail_to_pass_count', 0)} "
            f"| {p.get('pass_to_fail_count', 0)} |"
        )

    by_phenom = st.get("by_phenomenon") or {}
    if by_phenom:
        lines += [
            "\n#### By Phenomenon\n",
            "| phenomenon | probes | fail→pass | pass→fail | flat_fail | flat_pass |",
            "|---|---|---|---|---|---|",
        ]
        for phenom, counts in by_phenom.items():
            lines.append(
                f"| {phenom} "
                f"| {counts.get('probe_count', 0)} "
                f"| {counts.get('fail_to_pass', 0)} "
                f"| {counts.get('pass_to_fail', 0)} "
                f"| {counts.get('flat_fail', 0)} "
                f"| {counts.get('flat_pass', 0)} |"
            )

    # Sample-repeat pass rate (weight_mode=sample battery repeats, M30)
    sample_pass_rates = section.get("sample_pass_rates") or {}
    configured_repeats = section.get("sample_battery_repeats_configured", 25)
    lines += [
        "\n### Sample-Repeat Pass Rate (`weight_mode=sample`)\n",
        "| probe_id | session | pass_rate | repeats_completed/configured | flag |",
        "|---|---|---|---|---|",
    ]
    for p in sample_pass_rates.get("per_probe") or []:
        for s in p.get("sessions") or []:
            pass_rate = s.get("pass_rate")
            pass_rate_str = f"{pass_rate:.4f}" if pass_rate is not None else "—"
            flag = " ⚠ PARTIAL BATTERY" if s.get("partial_battery") else ""
            lines.append(
                f"| {p.get('probe_id', '—')} "
                f"| {s.get('session_index', '—')} "
                f"| {pass_rate_str} "
                f"| {s.get('repeats_completed', 0)}/{configured_repeats} "
                f"|{flag} |"
            )

    # Edge delta ledger
    ledger = section.get("edge_delta_ledger") or {}
    orphan_count = ledger.get("orphan_update_count", 0)
    orphan_flag = " ⚠ **PROCESS VIOLATION**" if orphan_count > 0 else ""
    lines += [
        "\n### Edge Weight Delta Ledger\n",
        f"**Total updates:** {ledger.get('total_updates', 0)}  ",
        f"**Increased:** {ledger.get('increased_count', 0)} | "
        f"**Decreased:** {ledger.get('decreased_count', 0)} | "
        f"**Unchanged:** {ledger.get('unchanged_count', 0)}  ",
        f"**Mean |delta|:** {ledger.get('mean_abs_delta', 0.0):.6f}  ",
        f"**Orphan updates (no feedback_event_id):** {orphan_count}{orphan_flag}\n",
    ]

    # Edge classification (M33) — edge class x cluster relation, replacing
    # the earlier flat within/cross/unknown counts. A pre-M33 stored report
    # JSON has no "edge_classification" key, so this whole block simply
    # renders nothing rather than crashing (see test_s61_edge_classification
    # backward-compat coverage).
    classification = ledger.get("edge_classification")
    if classification:

        def _fmt_cell(cell: dict) -> str:
            count = cell.get("count", 0)
            if count == 0:
                return "0"
            return f"{count} (Δ̄{cell.get('mean_abs_delta', 0.0):.4f})"

        by_class_relation = classification.get("by_class_relation") or {}
        class_labels = {
            "seed_seed": "seed↔seed",
            "conv_seed": "conv→seed",
            "conv_conv": "conv↔conv",
        }
        relations = ["same_cluster", "cross_cluster", "mixed", "unresolvable"]
        lines += [
            "#### Edge Classification\n",
            "| edge class | same-cluster | cross-cluster | mixed | unresolvable |",
            "|---|---|---|---|---|",
        ]
        for cls, label in class_labels.items():
            cells = by_class_relation.get(cls) or {}
            row = " | ".join(_fmt_cell(cells.get(rel) or {}) for rel in relations)
            lines.append(f"| {label} | {row} |")

        totals = classification.get("totals_by_relation") or {}
        lines.append(
            "\n**Totals by relation:** "
            f"same-cluster: {totals.get('same_cluster', 0)} | "
            f"cross-cluster: {totals.get('cross_cluster', 0)} | "
            f"mixed: {totals.get('mixed', 0)} | "
            f"unresolvable: {totals.get('unresolvable', 0)}\n"
        )

        watch = classification.get("cross_domain_watch") or []
        if watch:
            lines += [
                "#### Cross-Domain Linkage Watch List\n",
                "_conv→seed/mixed and conv↔conv/cross-cluster edges — the "
                "operational definition of emergent cross-domain learning._\n",
                "| edge_id | class | relation | delta | source | target |",
                "|---|---|---|---|---|---|",
            ]
            for w in watch[:20]:
                lines.append(
                    f"| {str(w.get('edge_id', ''))[:8]}… "
                    f"| {class_labels.get(w.get('edge_class'), w.get('edge_class'))} "
                    f"| {w.get('cluster_relation', '—')} "
                    f"| {w.get('delta', 0.0):+.4f} "
                    f"| {w.get('source_cluster', '—')} "
                    f"| {w.get('target_cluster', '—')} |"
                )
            if len(watch) > 20:
                lines.append(f"\n_…and {len(watch) - 20} more (see JSON)_")

        by_series = classification.get("by_series") or []
        if by_series:
            lines += [
                "\n#### Edge Classification by Series (longitudinal)\n",
                "| series | seed↔seed | conv→seed | conv↔conv |",
                "|---|---|---|---|",
            ]
            for s in by_series:
                by_class = s.get("by_class") or {}
                lines.append(
                    f"| {s.get('series_id', '—')} "
                    f"| {_fmt_cell(by_class.get('seed_seed') or {})} "
                    f"| {_fmt_cell(by_class.get('conv_seed') or {})} "
                    f"| {_fmt_cell(by_class.get('conv_conv') or {})} |"
                )

    # Bridge-MU analysis
    bridge = section.get("bridge_mu_analysis") or {}
    lines += [
        "\n### Bridge-MU / Cross-Cluster Transfer\n",
        f"**Bridge probes:** {bridge.get('bridge_probe_count', 0)}  ",
        f"**Reinforcement events (thumbs-up runs):** {bridge.get('reinforcement_event_count', 0)}\n",
    ]
    for bp in bridge.get("bridge_probe_results") or []:
        trajectory = bp.get("rank_trajectory") or []
        traj_str = " → ".join(
            f"s{s}:{'r'+str(r) if r is not None else 'absent'}"
            for s, r in trajectory
        )
        lines.append(f"**{bp.get('probe_id')}** rank trajectory: {traj_str or '—'}")
        for tc in bp.get("transfer_candidates") or []:
            flag = "🔵 TRANSFER CANDIDATE" if tc.get("cross_cluster_transfer_flag") else ""
            lines.append(
                f"  - Session {tc.get('session_n')}→{tc.get('session_n1')}: "
                f"rank {tc.get('rank_n')}→{tc.get('rank_n1')} {flag}"
            )
        lines.append("")

    # Saturation
    sat = section.get("saturation_analysis") or {}
    lines += [
        "### Saturation / Runaway-Reinforcement\n",
        f"**Saturation threshold:** {sat.get('saturation_threshold', 0.9)}  ",
        f"**Unique edges tracked:** {sat.get('total_unique_edges', 0)}  ",
        f"**Saturation flags:** {sat.get('saturation_flag_count', 0)}  ",
        f"**Same-direction streaks (informational — not anomalies):** "
        f"{sat.get('same_direction_streak_count', 0)}  ",
        f"**Runaway flags:** {sat.get('runaway_flag_count', 0)}\n",
    ]
    sat_flags = sat.get("saturation_flags") or []
    if sat_flags:
        lines += [
            "#### Saturated Edges\n",
            "| edge_id | final_weight | update_count |",
            "|---|---|---|",
        ]
        for sf in sat_flags:
            lines.append(
                f"| {str(sf.get('edge_id', ''))[:8]}… "
                f"| {sf.get('final_weight', '—'):.4f} "
                f"| {sf.get('update_count', 0)} |"
            )
    runaway_flags = sat.get("runaway_flags") or []
    if runaway_flags:
        lines.append("\n#### Runaway-Reinforcement Edges\n")
        for rf in runaway_flags:
            reasons = []
            if rf.get("non_decreasing_magnitude"):
                reasons.append("accelerating |delta|")
            if rf.get("crosses_saturation_threshold"):
                reasons.append("crosses saturation threshold")
            reason_str = ", ".join(reasons) or "—"
            lines.append(
                f"- `{str(rf.get('edge_id', ''))[:8]}…` "
                f"consecutive deltas: {rf.get('consecutive_deltas')} "
                f"(from turn {rf.get('starting_at_turn', '—')}) — {reason_str}"
            )

    return "\n".join(lines)


# (delta_key, learned_key, control_key, display_label) — the raw learned/
# control values ride alongside each delta in `comparative_deltas` (see
# `_compute_comparative_deltas`) so the table can show all three columns
# instead of hiding the baseline behind a bare "—" (the M34 rendering bug).
_S62_MEASURES = [
    ("probe_pass_rate_delta", "probe_pass_rate_learned",
     "probe_pass_rate_control", "probe_pass_rate_delta (mean-mode)"),
    ("bridge_mu_rank_delta", "bridge_mu_rank_learned",
     "bridge_mu_rank_control", "bridge_mu_rank_delta"),
    ("edge_weight_delta_magnitude", "edge_weight_delta_magnitude_learned",
     "edge_weight_delta_magnitude_control", "edge_weight_delta_magnitude"),
    ("mean_gold_mu_rank_delta", "mean_gold_mu_rank_learned",
     "mean_gold_mu_rank_control", "mean_gold_mu_rank_delta"),
]


def _s62_value_str(val: Optional[float]) -> str:
    return f"{val:.4f}" if val is not None else "—"


def _s62_control_display(measure_key: str, control_val: Optional[float]) -> str:
    """
    `edge_weight_delta_magnitude`'s control value is 0 by construction — the
    static/random controls never receive weight_updates (M24: feedback only
    ever writes through `process_verdict_for_learning()`, which refuses
    writes outside `condition_label == "learned"`), not because of any
    empirical result. Labeling it plainly avoids reading a true-by-design
    zero as a finding.
    """
    if measure_key == "edge_weight_delta_magnitude" and control_val is not None and abs(control_val) < 1e-9:
        return "0 by design"
    return _s62_value_str(control_val)


def _render_s62(control_shadow: Optional[dict]) -> str:
    """Render §6.2 Control Shadow as Markdown."""
    if not control_shadow:
        return "## §6.2 Control Shadow\n\n_Not computed — no control_experiment_ids provided._"

    lines = ["## §6.2 Control Shadow\n"]

    for control_name, cs in control_shadow.items():
        condition = cs.get("condition_label") or control_name
        exp_id = cs.get("experiment_id", "—")
        lines += [
            f"### {control_name} (`{condition}`)\n",
            f"**Experiment ID:** `{exp_id}`\n",
        ]

        # Sanity checks
        sanity = cs.get("sanity_checks") or {}
        lines.append("#### Sanity Checks\n")
        for check_name, check_result in sanity.items():
            status = check_result.get("status", "UNKNOWN")
            status_display = "✅ PASS" if status == "PASS" else ("⏭ SKIP" if status == "SKIP" else "❌ FAIL")
            lines.append(f"**{check_name}:** {status_display}")
            if status == "FAIL":
                violations = check_result.get("violation_count", check_result.get("violating_run_ids", ""))
                lines.append(f"  - Violations: {violations}")
        lines.append("")

        # Comparative deltas table (mean-mode probe results)
        deltas = cs.get("comparative_deltas") or {}
        lines += [
            "#### Comparative Deltas — mean-mode (learned − control)\n",
            "| Measure | Learned | Control | Delta |",
            "|---|---|---|---|",
        ]
        for delta_key, learned_key, control_key, label in _S62_MEASURES:
            learned_val = deltas.get(learned_key)
            control_val = deltas.get(control_key)
            delta_val = deltas.get(delta_key)
            delta_str = f"{delta_val:+.4f}" if delta_val is not None else "—"
            lines.append(
                f"| {label} | {_s62_value_str(learned_val)} "
                f"| {_s62_control_display(delta_key, control_val)} | {delta_str} |"
            )
        lines.append("")

        # Sample-mode pass-rate comparison (M34) — additive to the mean-mode
        # table above; blind spot it exists to close is a flat mean-mode
        # comparison next to a real sample-mode trajectory (e.g. exp0's
        # vs_d4, 4%→60% under weight_mode=sample).
        sample_cmp = cs.get("sample_pass_rate_comparison") or {}
        by_phenomenon = sample_cmp.get("by_phenomenon") or {}
        if by_phenomenon:
            lines += [
                "#### Sample-Mode Pass-Rate Comparison — `weight_mode=sample` (learned − control)\n",
                "| Phenomenon | Probes | Mean Δ (all sessions) | Mean Δ (final session) |",
                "|---|---|---|---|",
            ]
            for phenomenon, stats in by_phenomenon.items():
                lines.append(
                    f"| {phenomenon} | {stats.get('probe_count', 0)} "
                    f"| {_s62_value_str(stats.get('mean_delta'))} "
                    f"| {_s62_value_str(stats.get('final_session_mean_delta'))} |"
                )
            lines.append("")

            per_probe = sample_cmp.get("per_probe") or []
            if per_probe:
                lines += [
                    "<details><summary>Per-probe sample-mode sessions</summary>\n",
                    "| Probe | Session | Learned | Control | Δ |",
                    "|---|---|---|---|---|",
                ]
                for p in per_probe:
                    for s in p.get("sessions") or []:
                        lines.append(
                            f"| {p.get('probe_id', '—')} | {s.get('session_index', '—')} "
                            f"| {_s62_value_str(s.get('learned_pass_rate'))} "
                            f"| {_s62_value_str(s.get('control_pass_rate'))} "
                            f"| {_s62_value_str(s.get('delta'))} |"
                        )
                lines.append("\n</details>\n")
        else:
            lines.append(
                "_Sample-mode comparison unavailable — no `sample_pass_rates` "
                "data for this experiment (pre-M30) or no matching probes "
                "between conditions._\n"
            )

    # Comparison table (cross-condition, if multiple controls)
    if len(control_shadow) >= 1:
        first_deltas = next(iter(control_shadow.values())).get("comparative_deltas") or {}
        lines += [
            "### Condition Comparison Summary\n",
            "| Measure | Learned | " + " | ".join(cs.get("condition_label") or n for n, cs in control_shadow.items()) + " |",
            "|---|---|" + "---|" * len(control_shadow),
        ]
        for delta_key, learned_key, control_key, label in _S62_MEASURES:
            learned_val = _s62_value_str(first_deltas.get(learned_key))
            control_vals = []
            for cs in control_shadow.values():
                d = (cs.get("comparative_deltas") or {}).get(delta_key)
                control_vals.append(f"{d:+.4f}" if d is not None else "—")
            lines.append(f"| {label} | {learned_val} | " + " | ".join(control_vals) + " |")

    return "\n".join(lines)


def render_experiment_report_markdown(report: dict) -> str:
    """
    Render an experiment report dict as a Markdown document.

    Covers §5 aggregation across series, §6.3 integrity checks, and §6.4
    statistical caveat. Pure function (no I/O).
    """
    from analytics.reports.experiment_report import STATISTICAL_CAVEAT_TEXT

    experiment_id = report.get("experiment_id", "unknown")
    condition_label = report.get("condition_label", "unknown")
    generated_at = report.get("generated_at", "unknown")
    ss = report.get("series_summary") or {}
    ic = report.get("integrity_checks") or {}

    header = (
        f"# Experiment Report\n\n"
        f"**Experiment:** `{experiment_id}`  \n"
        f"**Condition:** `{condition_label}`  \n"
        f"**Generated:** {generated_at}  \n"
        f"**Series:** {ss.get('series_count', 0)} | "
        f"**Total runs:** {ss.get('total_run_count', 0)}\n"
    )

    # §5 Aggregated Series Summary
    s51 = ss.get("s5_1_failure_aggregation") or {}
    verdict_mix = ss.get("verdict_mix") or {}
    s53 = ss.get("s5_3_retrieval_rate_aggregation") or {}
    s55 = ss.get("s5_5_edge_weight_aggregation") or {}
    s56 = ss.get("s5_6_probe_integrity_aggregation") or {}
    s57 = ss.get("s5_7_mode3_aggregation") or {}
    s58 = ss.get("s5_8_ctx_pressure_aggregation") or {}

    series_section_lines = [
        "## §5 Series Summary (Aggregated)\n",
        f"**Series:** {', '.join(ss.get('series_ids') or [])}  \n",
    ]

    # §5.1
    series_section_lines += [
        "### §5.1 Failure Summary\n",
        f"**Total failed turns:** {s51.get('total_failed_turn_count', 0)}  ",
        f"**Total ghost-MU incidents:** {s51.get('total_ghost_mu_incidents', 0)}\n",
        "| series | failed_turns | ghost_mus |",
        "|---|---|---|",
    ]
    for p in s51.get("per_series") or []:
        series_section_lines.append(
            f"| {p.get('series_id', '—')} | {p.get('failed_turn_count', 0)} | {p.get('ghost_mu_count', 0)} |"
        )

    # Verdict Mix (M32) — bulk-turn judge verdicts, per series.
    # M36 — recorded vs. judge-alone, so operator overrides are visible in the
    # report rather than silently folded into the headline up-rate.
    overall_up_rate = verdict_mix.get("overall_up_rate")
    judge_alone_rate = verdict_mix.get("judge_alone_up_rate")
    human_sourced = verdict_mix.get("total_human_sourced", 0)
    unrecoverable = verdict_mix.get("judge_alone_unrecoverable", 0)
    series_section_lines += [
        "\n### Verdict Mix (Bulk Turns)\n",
        f"**Total up:** {verdict_mix.get('total_up', 0)} | "
        f"**Total down:** {verdict_mix.get('total_down', 0)} | "
        f"**Total none:** {verdict_mix.get('total_none', 0)}  ",
        f"**Overall up-rate (up / (up+down)):** "
        f"{f'{overall_up_rate:.1%}' if overall_up_rate is not None else '—'}\n",
        f"**Human-sourced verdicts (operator overrides / hand-grades):** {human_sourced}  ",
        f"**Judge-alone up-rate (overrides reverted):** "
        f"{f'{judge_alone_rate:.1%}' if judge_alone_rate is not None else '—'}  ",
        f"**Judge-alone counts:** up {verdict_mix.get('total_judge_alone_up', 0)} | "
        f"down {verdict_mix.get('total_judge_alone_down', 0)} | "
        f"none {verdict_mix.get('total_judge_alone_none', 0)}\n",
    ]
    if human_sourced:
        series_section_lines.append(
            "> The overall up-rate above is **human-assisted**. An unattended run "
            "has no operator to override the judge, so the judge-alone rate is the "
            "one that describes it — treat any gap between the two as the judge's "
            "measured miscalibration, and its direction as the direction of bias.\n"
        )
    if unrecoverable:
        series_section_lines.append(
            f"> ⚠️ {unrecoverable} human-sourced verdict(s) predate judge_model_verdict "
            "recording and have no recoverable pre-override verdict; they are excluded "
            "from the judge-alone tallies, which are therefore a partial view.\n"
        )
    series_section_lines += [
        "| series | up | down | none | up_rate | human | judge_alone_up_rate |",
        "|---|---|---|---|---|---|---|",
    ]
    for p in verdict_mix.get("per_series") or []:
        p_rate = p.get("up_rate")
        p_j_rate = p.get("judge_alone_up_rate")
        series_section_lines.append(
            f"| {p.get('series_id', '—')} | {p.get('up', 0)} | {p.get('down', 0)} "
            f"| {p.get('none', 0)} | {f'{p_rate:.1%}' if p_rate is not None else '—'} "
            f"| {p.get('human_sourced', 0)} "
            f"| {f'{p_j_rate:.1%}' if p_j_rate is not None else '—'} |"
        )

    # §5.3
    outliers = s53.get("outlier_series") or []
    series_section_lines += [
        "\n### §5.3 Retrieval Rate\n",
        f"**Mean intra-series rate:** {s53.get('mean_intra_series_rate', 0.0):.1%}  ",
        f"**Range:** {s53.get('min_intra_series_rate', 0.0):.1%} – {s53.get('max_intra_series_rate', 0.0):.1%}  ",
    ]
    if outliers:
        series_section_lines.append(f"**⚠ Outlier series (>15% from mean):** {', '.join(outliers)}")
    drift = s53.get("seed_share_drift")
    if drift:
        series_section_lines.append(
            f"**Seed-share drift ({drift.get('first_series_id', '—')} → "
            f"{drift.get('last_series_id', '—')}):** "
            f"{drift.get('first_seed_share', 0.0):.1%} → {drift.get('last_seed_share', 0.0):.1%} "
            f"({drift.get('delta', 0.0):+.1%})"
        )
    series_section_lines += [
        "\n| series | intra_rate | seed_share | cross_share | total_retrieved |",
        "|---|---|---|---|---|",
    ]
    for p in s53.get("per_series") or []:
        seed_share = p.get("seed_share")
        cross_share = p.get("cross_share")
        series_section_lines.append(
            f"| {p.get('series_id', '—')} | {p.get('intra_series_rate', 0.0):.1%} "
            f"| {f'{seed_share:.1%}' if seed_share is not None else '—'} "
            f"| {f'{cross_share:.1%}' if cross_share is not None else '—'} "
            f"| {p.get('total_retrieved', 0)} |"
        )

    # §5.5
    series_section_lines += [
        "\n### §5.5 Edge Weight Deltas\n",
        f"**Total activated edges:** {s55.get('total_activated_edge_count', 0)}  ",
        f"**Increased:** {s55.get('edges_increased', 0)} | **Decreased:** {s55.get('edges_decreased', 0)} | **Unchanged:** {s55.get('edges_unchanged', 0)}  ",
        f"**Mean |delta|:** {s55.get('mean_abs_delta', 0.0):.4f}\n",
    ]

    # §5.6
    s56_status = s56.get("status", "UNKNOWN")
    s56_display = "✅ PASS" if s56_status == "PASS" else "❌ HARD FAIL"
    series_section_lines += [
        "### §5.6 Probe Integrity\n",
        f"**Experiment-level status:** {s56_display}  ",
        f"**Total violations:** {s56.get('total_violations', 0)}  ",
    ]
    if s56.get("hard_fail_series"):
        series_section_lines.append(
            f"**HARD FAIL in series:** {', '.join(s56['hard_fail_series'])}"
        )

    # §5.7
    series_section_lines += [
        "\n### §5.7 Mode-3 Triggers\n",
        f"**Total Mode-3 MUs:** {s57.get('total_mode3_mu_count', 0)}  ",
    ]
    if s57.get("increasing_pressure_flag"):
        series_section_lines.append("**⚠ Increasing Mode-3 pressure detected (later series trigger more summarization)**")

    # §5.8
    series_section_lines += [
        "\n### §5.8 Context Pressure\n",
    ]
    if s58.get("increasing_pressure_flag"):
        series_section_lines.append("**⚠ Increasing context pressure detected (growing number of pressure turns)**\n")
    series_section_lines += [
        "| series | pressure_turns |",
        "|---|---|",
    ]
    for p in s58.get("per_series") or []:
        series_section_lines.append(
            f"| {p.get('series_id', '—')} | {p.get('pressure_turn_count', 0)} |"
        )

    series_section = "\n".join(series_section_lines)

    # §6.3 Integrity Checks
    overall = "✅ PASS" if ic.get("overall_pass") else "❌ ISSUES FOUND"
    sp = ic.get("seed_fixture_provenance") or {}
    gw = ic.get("graph_wipe_verification") or {}
    pr = ic.get("probe_replay_index_normalization") or {}

    sp_display = "✅ PASS" if sp.get("status") == "PASS" else ("⏭ SKIP" if sp.get("status") == "SKIP" else "❌ FAIL")
    gw_display = "✅ PASS" if gw.get("status") == "PASS" else ("⏭ SKIP" if gw.get("status") == "SKIP" else "❌ FAIL")
    pr_display = "✅ PASS" if pr.get("status") == "PASS" else ("⏭ SKIP" if pr.get("status") == "SKIP" else "❌ FAIL")

    integrity_lines = [
        "## §6.3 Data-Integrity / Confound Checks\n",
        f"**Overall:** {overall}\n",
        "### Seed-Fixture Provenance\n",
        f"**Status:** {sp_display}  ",
        f"**Fixture versions found:** {', '.join(sp.get('fixture_versions_found') or []) or '—'}  ",
        f"**Unregistered versions:** {', '.join(sp.get('unregistered_versions') or []) or '—'}  ",
        f"**Hash mismatch:** {sp.get('hash_mismatch', False)}\n",
        "### Graph-Wipe Verification\n",
        f"**Status:** {gw_display}  ",
        f"**Pre-experiment foreign MUs:** {gw.get('pre_experiment_foreign_mu_count', 0)}  ",
        f"**Earliest run timestamp:** {gw.get('earliest_run_timestamp') or '—'}\n",
        "### Probe-Replay-Index Normalization\n",
        f"**Status:** {pr_display}  ",
        f"**Condition:** `{pr.get('condition_label') or '—'}`  ",
        f"**Session indices found:** {pr.get('session_indices_found') or []}  ",
        f"**Gaps:** {pr.get('gaps') or 'none'}\n",
    ]
    integrity_section = "\n".join(integrity_lines)

    # §6.1 learning measures
    learning_measures = report.get("learning_measures")
    s61_section = _render_s61(learning_measures)

    # §6.2 control shadow
    control_shadow = report.get("control_shadow")
    s62_section = _render_s62(control_shadow)

    body_parts = [
        header,
        series_section,
        integrity_section,
        STATISTICAL_CAVEAT_TEXT,
        s61_section,
        s62_section,
    ]
    return "\n\n---\n\n".join(body_parts) + "\n"


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
