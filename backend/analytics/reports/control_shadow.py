"""
§6.2 Control Shadow — helpers for embedding control-condition analysis
in the learned-condition experiment report.

The control shadow is NOT a separate file. It's a sub-structure embedded
under the 'control_shadow' key in the learned-condition experiment report so
the comparison can't drift out of sync with the primary results.

Design note: the sanity checks in this module detect *bugs*, not results.
A non-zero `zero_weight_changes` count means the static control was
accidentally updated — that's a process violation, not a finding.
"""

import json
import logging
import math
from typing import Any, Optional

from memory.store import GraphStore

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Sanity checks
# ---------------------------------------------------------------------------


async def _check_static_zero_weight_changes(
    store: GraphStore,
    control_experiment_id: str,
) -> dict:
    """
    Static-control sanity check: no run in the control experiment should have
    any weight_updates entries.

    A non-zero count is a bug — the static control should never update weights.
    """
    all_runs = await store.get_all_runs_for_experiment(control_experiment_id)
    violation_count = 0
    violating_run_ids: list[str] = []

    malformed_run_ids: list[str] = []

    for run in all_runs:
        wupdates = run.get("weight_updates") or []
        if isinstance(wupdates, str):
            try:
                wupdates = json.loads(wupdates)
            except (ValueError, TypeError):
                # Unparseable weight_updates is corrupted evidence, not proof
                # of zero updates. Coercing it to [] would score corruption as
                # a clean static control — the exact bug this check exists to
                # catch.
                malformed_run_ids.append(run.get("run_id", ""))
                continue
        if wupdates:
            violation_count += 1
            violating_run_ids.append(run.get("run_id", ""))

    if malformed_run_ids:
        logger.error(
            "control_shadow static zero_weight_changes: experiment=%s FAIL "
            "malformed weight_updates in %d run(s)",
            control_experiment_id, len(malformed_run_ids),
        )
        return {
            "status": "FAIL",
            "violation_count": violation_count,
            "violating_run_ids": violating_run_ids,
            "malformed_run_ids": sorted(set(malformed_run_ids))[:20],
            "reason": (
                f"Malformed weight_updates in {len(malformed_run_ids)} run(s) — "
                "unparseable evidence cannot be scored as a passing control"
            ),
        }

    status = "PASS" if violation_count == 0 else "FAIL"
    logger.info(
        "control_shadow static zero_weight_changes: experiment=%s status=%s violations=%d",
        control_experiment_id, status, violation_count,
    )
    return {
        "status": status,
        "violation_count": violation_count,
        "violating_run_ids": violating_run_ids,
    }


async def _check_random_weights_rerandomized(
    store: GraphStore,
    control_experiment_id: str,
    variance_floor: float,
) -> dict:
    """
    Random-control sanity check: each edge's weight should vary across the
    turns that used it (median per-edge std-dev >= variance_floor).

    Near-zero variance means weights were accidentally frozen (static, not
    random).

    Reads the weight actually used on each turn from the run's own
    `edge_weights_used` record. The live `edges` table must NOT be used here:
    it holds present-day weights, so a later condition wipe/reseed flattens
    every edge to its seed value and this check then reports FAIL for a
    control whose weights did vary. It also cannot answer the question at all
    — dispersion across *different* edges is not dispersion of *one* edge
    across turns, and only the latter is what "re-randomized" means.

    Malformed `edge_weights_used` JSON is an integrity failure, not an empty
    reading: silently treating unparseable evidence as absent evidence would
    let corruption pass as a clean result.
    """
    all_runs = await store.get_all_runs_for_experiment(control_experiment_id)

    # edge_id -> the weight used on each turn that traversed it
    weights_by_edge: dict[str, list[float]] = {}
    malformed_run_ids: list[str] = []

    for run in all_runs:
        raw = run.get("edge_weights_used") or {}
        if isinstance(raw, str):
            try:
                raw = json.loads(raw)
            except (ValueError, TypeError):
                malformed_run_ids.append(run.get("run_id", ""))
                continue
        if not isinstance(raw, dict):
            malformed_run_ids.append(run.get("run_id", ""))
            continue
        for edge_id, entry in raw.items():
            if not isinstance(entry, dict):
                malformed_run_ids.append(run.get("run_id", ""))
                continue
            weight_used = entry.get("weight_used")
            if weight_used is None:
                continue
            try:
                weights_by_edge.setdefault(edge_id, []).append(float(weight_used))
            except (TypeError, ValueError):
                malformed_run_ids.append(run.get("run_id", ""))

    if malformed_run_ids:
        logger.error(
            "control_shadow random weights_rerandomized: experiment=%s FAIL "
            "malformed edge_weights_used in %d run(s)",
            control_experiment_id, len(malformed_run_ids),
        )
        return {
            "status": "FAIL",
            "reason": (
                f"Malformed edge_weights_used in {len(malformed_run_ids)} run(s) — "
                "unparseable evidence cannot be scored as a passing control"
            ),
            "sample_size": 0,
            "weight_std_dev": None,
            "variance_floor": variance_floor,
            "malformed_run_ids": sorted(set(malformed_run_ids))[:20],
        }

    # Only edges traversed on 2+ turns can show across-turn variance at all.
    repeated = {e: w for e, w in weights_by_edge.items() if len(w) >= 2}

    if not weights_by_edge:
        logger.info(
            "control_shadow random weights_rerandomized: experiment=%s no_edges SKIP",
            control_experiment_id,
        )
        return {
            "status": "SKIP",
            "reason": "No edge weights recorded in control experiment",
            "sample_size": 0,
            "weight_std_dev": None,
            "variance_floor": variance_floor,
        }

    if not repeated:
        logger.info(
            "control_shadow random weights_rerandomized: experiment=%s "
            "no_repeated_edges SKIP observed_edges=%d",
            control_experiment_id, len(weights_by_edge),
        )
        return {
            "status": "SKIP",
            "reason": (
                "No edge was traversed on more than one turn, so across-turn "
                "re-randomization cannot be observed"
            ),
            "sample_size": 0,
            "observed_edge_count": len(weights_by_edge),
            "weight_std_dev": None,
            "variance_floor": variance_floor,
        }

    per_edge_std_devs = sorted(
        math.sqrt(
            sum((w - (sum(ws) / len(ws))) ** 2 for w in ws) / len(ws)
        )
        for ws in repeated.values()
    )
    mid = len(per_edge_std_devs) // 2
    median_std_dev = (
        per_edge_std_devs[mid]
        if len(per_edge_std_devs) % 2
        else (per_edge_std_devs[mid - 1] + per_edge_std_devs[mid]) / 2
    )

    status = "PASS" if median_std_dev >= variance_floor else "FAIL"
    logger.info(
        "control_shadow random weights_rerandomized: experiment=%s status=%s "
        "repeated_edges=%d observed_edges=%d median_std_dev=%.4f floor=%.4f",
        control_experiment_id, status, len(repeated), len(weights_by_edge),
        median_std_dev, variance_floor,
    )
    return {
        "status": status,
        "sample_size": len(repeated),
        "observed_edge_count": len(weights_by_edge),
        "weight_std_dev": round(median_std_dev, 6),
        "variance_floor": variance_floor,
    }


async def _check_zero_feedback_edge_correlation(
    store: GraphStore,
    control_experiment_id: str,
) -> dict:
    """
    Both-controls sanity check: for each feedback event in the control experiment,
    the associated run should have NO weight_updates (feedback is causally inert).

    Counts violations — should be 0 for both static and random controls.
    """
    all_runs = await store.get_all_runs_for_experiment(control_experiment_id)

    # Index runs by run_id for fast lookup
    runs_by_id = {run.get("run_id"): run for run in all_runs}

    # Fetch all feedback events for runs in this experiment
    violation_count = 0
    violating_feedback_event_ids: list[str] = []

    malformed_run_ids: list[str] = []

    for run in all_runs:
        run_id = run.get("run_id")
        if not run_id:
            continue
        # Parse once per run, not once per feedback event.
        wupdates = run.get("weight_updates") or []
        if isinstance(wupdates, str):
            try:
                wupdates = json.loads(wupdates)
            except (ValueError, TypeError):
                # See _check_static_zero_weight_changes: corrupted evidence
                # must not read as "no weight updates".
                malformed_run_ids.append(run_id)
                continue
        feedback_events = await store.get_feedback_events_for_run(run_id)
        for event in feedback_events:
            # If this run has a feedback event, it should have no weight_updates
            if wupdates:
                violation_count += 1
                violating_feedback_event_ids.append(
                    event.get("feedback_event_id", "")
                )

    if malformed_run_ids:
        logger.error(
            "control_shadow zero_feedback_edge_correlation: experiment=%s FAIL "
            "malformed weight_updates in %d run(s)",
            control_experiment_id, len(malformed_run_ids),
        )
        return {
            "status": "FAIL",
            "violation_count": violation_count,
            "violating_feedback_event_ids": violating_feedback_event_ids,
            "malformed_run_ids": sorted(set(malformed_run_ids))[:20],
            "reason": (
                f"Malformed weight_updates in {len(malformed_run_ids)} run(s) — "
                "unparseable evidence cannot be scored as a passing control"
            ),
        }

    status = "PASS" if violation_count == 0 else "FAIL"
    logger.info(
        "control_shadow zero_feedback_edge_correlation: experiment=%s status=%s violations=%d",
        control_experiment_id, status, violation_count,
    )
    return {
        "status": status,
        "violation_count": violation_count,
        "violating_feedback_event_ids": violating_feedback_event_ids,
    }


# ---------------------------------------------------------------------------
# Comparative delta computation
# ---------------------------------------------------------------------------


def _compute_comparative_deltas(
    learned_measures: dict,
    control_measures: dict,
    probe_results_by_condition: dict[str, list[dict]],
    learned_condition: str,
    control_condition: str,
) -> dict:
    """
    Compute comparative deltas between learned and control conditions for each
    learning measure that can be compared numerically.

    Deltas: learned_value - control_value. Positive = learned is higher/better.
    """

    def _final_pass_rate(measures: dict, condition: str, results_by_probe: list[dict]) -> Optional[float]:
        """Fraction of probes with 'pass' outcome at the final measurement session."""
        if not results_by_probe:
            return None
        # Group by probe_id, get last session for each
        from collections import defaultdict
        from analytics.reports.experiment_report import _classify_probe_outcome
        default_threshold = measures.get("pass_rank_threshold", 3)
        phenomenon_thresholds = measures.get("phenomenon_thresholds") or {}
        by_probe: dict[str, list] = defaultdict(list)
        for r in results_by_probe:
            by_probe[r.get("probe_id", "")].append(r)
        pass_count = 0
        total = 0
        for pid, results in by_probe.items():
            sorted_r = sorted(results, key=lambda x: x.get("measurement_session_index", 0))
            if not sorted_r:
                continue
            last = sorted_r[-1]
            total += 1
            # phenomenon_type is not available in the flat result — falls back to default
            outcome = _classify_probe_outcome(
                last, default_threshold,
                phenomenon_type="",
                phenomenon_thresholds=phenomenon_thresholds,
            )
            if outcome == "pass":
                pass_count += 1
        return pass_count / total if total > 0 else None

    def _final_bridge_mu_rank(measures: dict) -> Optional[float]:
        """Mean gold_mu_rank of bridge probes at their final measurement session."""
        bridge = measures.get("bridge_mu_analysis") or {}
        results = bridge.get("bridge_probe_results") or []
        if not results:
            return None
        final_ranks = []
        for bp in results:
            traj = bp.get("rank_trajectory") or []
            if traj:
                _, last_rank = traj[-1]
                if last_rank is not None:
                    final_ranks.append(last_rank)
        return sum(final_ranks) / len(final_ranks) if final_ranks else None

    def _mean_edge_delta_magnitude(measures: dict) -> Optional[float]:
        ledger = measures.get("edge_delta_ledger") or {}
        val = ledger.get("mean_abs_delta")
        return val if val is not None else None

    def _mean_gold_mu_rank_final(results_by_probe: list[dict]) -> Optional[float]:
        from collections import defaultdict
        by_probe: dict[str, list] = defaultdict(list)
        for r in results_by_probe:
            by_probe[r.get("probe_id", "")].append(r)
        ranks = []
        for pid, results in by_probe.items():
            sorted_r = sorted(results, key=lambda x: x.get("measurement_session_index", 0))
            if not sorted_r:
                continue
            last = sorted_r[-1]
            rank = last.get("gold_mu_rank")
            if rank is not None:
                ranks.append(rank)
        return sum(ranks) / len(ranks) if ranks else None

    learned_results = probe_results_by_condition.get(learned_condition, [])
    control_results = probe_results_by_condition.get(control_condition, [])

    l_pass = _final_pass_rate(learned_measures, learned_condition, learned_results)
    c_pass = _final_pass_rate(control_measures, control_condition, control_results)
    probe_pass_rate_delta = (
        round(l_pass - c_pass, 4)
        if (l_pass is not None and c_pass is not None) else None
    )

    l_bridge = _final_bridge_mu_rank(learned_measures)
    c_bridge = _final_bridge_mu_rank(control_measures)
    bridge_mu_rank_delta = (
        round(l_bridge - c_bridge, 4)
        if (l_bridge is not None and c_bridge is not None) else None
    )

    l_edge = _mean_edge_delta_magnitude(learned_measures)
    c_edge = _mean_edge_delta_magnitude(control_measures)
    edge_weight_delta_magnitude = (
        round(l_edge - c_edge, 6)
        if (l_edge is not None and c_edge is not None) else None
    )

    l_rank = _mean_gold_mu_rank_final(learned_results)
    c_rank = _mean_gold_mu_rank_final(control_results)
    mean_gold_mu_rank_delta = (
        round(l_rank - c_rank, 4)
        if (l_rank is not None and c_rank is not None) else None
    )

    logger.info(
        "control_shadow comparative_deltas: learned=%s control=%s "
        "pass_rate_delta=%s bridge_rank_delta=%s edge_mag_delta=%s rank_delta=%s",
        learned_condition, control_condition,
        probe_pass_rate_delta, bridge_mu_rank_delta,
        edge_weight_delta_magnitude, mean_gold_mu_rank_delta,
    )
    # Raw learned/control values are returned alongside each delta (not just
    # the delta itself) so the rendered table can show "learned's own value,
    # the control's, and the delta" instead of hiding the baseline behind a
    # bare "—" (the M34 rendering bug).
    return {
        "probe_pass_rate_delta": probe_pass_rate_delta,
        "probe_pass_rate_learned": l_pass,
        "probe_pass_rate_control": c_pass,
        "bridge_mu_rank_delta": bridge_mu_rank_delta,
        "bridge_mu_rank_learned": l_bridge,
        "bridge_mu_rank_control": c_bridge,
        "edge_weight_delta_magnitude": edge_weight_delta_magnitude,
        "edge_weight_delta_magnitude_learned": l_edge,
        "edge_weight_delta_magnitude_control": c_edge,
        "mean_gold_mu_rank_delta": mean_gold_mu_rank_delta,
        "mean_gold_mu_rank_learned": l_rank,
        "mean_gold_mu_rank_control": c_rank,
    }


# ---------------------------------------------------------------------------
# Sample-mode (weight_mode=sample) pass-rate comparison
# ---------------------------------------------------------------------------


def _aggregate_deltas_by_phenomenon(per_probe: list[dict]) -> dict[str, dict]:
    """
    Aggregate per-probe/per-session deltas (the `per_probe` shape produced by
    `_compute_sample_pass_rate_comparison`) into per-phenomenon mean deltas
    (across all sessions) and final-session mean deltas.

    Extracted (M39) so the unrestricted by-phenomenon mean and the
    pin-adjusted excluding-pinned mean (`_compute_pin_adjusted_mean_deltas`)
    compute the mean identically — never two slightly-different definitions
    of "mean delta" living in the same report.
    """
    deltas_by_phenom: dict[str, list[float]] = {}
    finals_by_phenom: dict[str, list[float]] = {}
    counts: dict[str, int] = {}

    for p in per_probe:
        phenomenon = p.get("phenomenon_type", "unknown")
        counts[phenomenon] = counts.get(phenomenon, 0) + 1
        sessions = p.get("sessions") or []
        probe_deltas = [s["delta"] for s in sessions if s.get("delta") is not None]
        deltas_by_phenom.setdefault(phenomenon, []).extend(probe_deltas)
        if sessions:
            final_delta = sessions[-1].get("delta")
            if final_delta is not None:
                finals_by_phenom.setdefault(phenomenon, []).append(final_delta)

    by_phenomenon: dict[str, dict] = {}
    for phenomenon, count in counts.items():
        deltas = deltas_by_phenom.get(phenomenon, [])
        finals = finals_by_phenom.get(phenomenon, [])
        by_phenomenon[phenomenon] = {
            "probe_count": count,
            "mean_delta": round(sum(deltas) / len(deltas), 4) if deltas else None,
            "final_session_mean_delta": (
                round(sum(finals) / len(finals), 4) if finals else None
            ),
        }
    return by_phenomenon


def _compute_sample_pass_rate_comparison(
    learned_measures: dict,
    control_measures: dict,
) -> dict:
    """
    Sample-mode pass-rate comparison — per-probe, per-session
    learned-minus-control deltas, aggregated by phenomenon.

    The existing `_compute_comparative_deltas` only compares
    `battery_mode="mean"` probe results, which are flat nearly everywhere —
    it is blind precisely where the signal lives (`sample_pass_rates`, M30
    Part B). This is a separate, additive comparison over each condition's
    own `sample_pass_rates` (already computed by `_section_61_learning_measures`
    for both the learned and control experiments); it does not replace or
    redefine the mean-mode comparison above.

    Degrades to an empty comparison (not a crash) when either condition has
    no sample-mode data — true for every pre-M30 experiment, since
    `sample_pass_rates["per_probe"]` is simply empty for those.
    """
    learned_per_probe = {
        p["probe_id"]: p
        for p in (learned_measures.get("sample_pass_rates") or {}).get("per_probe") or []
    }
    control_per_probe = {
        p["probe_id"]: p
        for p in (control_measures.get("sample_pass_rates") or {}).get("per_probe") or []
    }

    per_probe: list[dict] = []

    for probe_id, l_probe in learned_per_probe.items():
        c_probe = control_per_probe.get(probe_id)
        if c_probe is None:
            continue

        phenomenon = l_probe.get("phenomenon_type", "unknown")
        c_sessions_by_idx = {
            s.get("session_index"): s for s in c_probe.get("sessions") or []
        }

        sessions: list[dict] = []
        for l_session in l_probe.get("sessions") or []:
            idx = l_session.get("session_index")
            c_session = c_sessions_by_idx.get(idx)
            l_rate = l_session.get("pass_rate")
            c_rate = c_session.get("pass_rate") if c_session else None
            delta = (
                round(l_rate - c_rate, 4)
                if (l_rate is not None and c_rate is not None) else None
            )
            sessions.append({
                "session_index": idx,
                "learned_pass_rate": l_rate,
                "control_pass_rate": c_rate,
                "delta": delta,
            })

        if not sessions:
            continue

        per_probe.append({
            "probe_id": probe_id,
            "phenomenon_type": phenomenon,
            "sessions": sessions,
        })

    by_phenomenon = _aggregate_deltas_by_phenomenon(per_probe)

    logger.info(
        "control_shadow sample_pass_rate_comparison: probes=%d phenomena=%s",
        len(per_probe), list(by_phenomenon.keys()),
    )
    return {
        "per_probe": per_probe,
        "by_phenomenon": by_phenomenon,
    }


# ---------------------------------------------------------------------------
# Trained-subset comparison + pin-adjusted mean deltas (M39)
# ---------------------------------------------------------------------------


def _compute_session_mean_deltas(
    per_probe: list[dict],
) -> tuple[list[dict], Optional[float]]:
    """
    Shared arithmetic (M41) for turning a `per_probe` delta list (the shape
    `_compute_sample_pass_rate_comparison`/`_compute_trained_subset_comparison`
    both produce) into a per-session mean delta and a final-session mean
    delta.

    Extracted so `_compute_trained_subset_comparison()`'s raw mean and its
    excluding-pinned mean run through identical code — the same rationale as
    M39's `_aggregate_deltas_by_phenomenon` extraction: two slightly
    different definitions of "trained-subset mean delta" must not be able to
    coexist in the same report.

    Returns `(mean_delta_by_session, final_session_mean_delta)`.
    `final_session_mean_delta` is `None` (never `0.0`) when `per_probe` is
    empty or has no final-session deltas — an empty input (e.g. every
    trained probe control-pinned) must read as "nothing to average," not as
    "averaged to zero."
    """
    session_deltas: dict[int, list[float]] = {}
    for p in per_probe:
        for s in p.get("sessions") or []:
            if s.get("delta") is not None:
                session_deltas.setdefault(s["session_index"], []).append(s["delta"])

    mean_delta_by_session = [
        {
            "session_index": idx,
            "mean_delta": round(sum(deltas) / len(deltas), 4),
            "probe_count": len(deltas),
        }
        for idx, deltas in sorted(session_deltas.items())
    ]

    final_session_deltas = [
        p["sessions"][-1]["delta"]
        for p in per_probe
        if p.get("sessions") and p["sessions"][-1].get("delta") is not None
    ]
    final_session_mean_delta = (
        round(sum(final_session_deltas) / len(final_session_deltas), 4)
        if final_session_deltas else None
    )
    return mean_delta_by_session, final_session_mean_delta


def _compute_trained_subset_comparison(
    sample_pass_rate_comparison: dict,
    trained_probe_ids: set[str],
    pinned_probe_ids: Optional[set[str]] = None,
) -> dict:
    """
    §6.2 trained-subset comparison (M39/M41) — the primary config-comparison
    endpoint. Restricts the sample-mode learned/control comparison
    (`_compute_sample_pass_rate_comparison`'s per_probe output) to just the
    trained probes, and reports a trained-subset mean delta per session and
    for the final session.

    Every config round's central question — "did learning move the TRAINED
    spokes?" — has no home in the existing by-phenomenon aggregation, which
    averages all 28 spoke_retrieval probes together (4 trained + 24 untrained
    neighbors). exp0b's aggregate mean read ~0.00 while individual untrained
    neighbors climbed as high as 84% — this is the number that isolates
    whether the trained spokes themselves moved. State it even when
    (especially when) it's zero or negative.

    Degrades to an empty comparison, not a crash, when trained_probe_ids is
    empty (no trained_spokes supplied to generate_experiment_report) or none
    of the trained probes have sample-mode data.

    M41: `pinned_probe_ids` (control-specific, from `_compute_pinned_probes`)
    adds a second mean computed over the trained probes EXCLUDING any that
    are control-pinned at a degenerate 1.0 tie-break ceiling the mechanism
    can never move. The raw mean above cancels genuine trained-spoke wins
    against those ceilings — Round 1 rep3's real data: raw final Δ = −0.12
    (two +0.7-ish trained wins cancelled by two −0.96 pinned ceilings) vs.
    +0.72 excluding {vs_a1, vs_c1}. Defaults to an empty set, so every
    pre-M41 call site is unaffected and the raw mean/keys are untouched
    (M34's never-silently-redefine rule) — the new keys are purely additive.
    """
    pinned_probe_ids = pinned_probe_ids or set()
    all_per_probe = sample_pass_rate_comparison.get("per_probe") or []
    # Shallow-copy each matched probe's dict rather than mutating the shared
    # `all_per_probe` list in place — that list is also consumed elsewhere
    # (e.g. `_compute_pin_adjusted_mean_deltas`), and tagging it with a
    # trained-subset-only `control_pinned` flag would leak this function's
    # bookkeeping into an unrelated caller's data.
    trained_per_probe = [
        {**p, "control_pinned": p["probe_id"] in pinned_probe_ids}
        for p in all_per_probe
        if p["probe_id"] in trained_probe_ids
    ]
    unpinned_trained_per_probe = [
        p for p in trained_per_probe if not p["control_pinned"]
    ]
    pinned_trained_probe_ids = sorted(
        p["probe_id"] for p in trained_per_probe if p["control_pinned"]
    )

    mean_delta_by_session, final_session_mean_delta = _compute_session_mean_deltas(
        trained_per_probe
    )
    (
        mean_delta_by_session_excluding_pinned,
        final_session_mean_delta_excluding_pinned,
    ) = _compute_session_mean_deltas(unpinned_trained_per_probe)

    logger.info(
        "control_shadow trained_subset_comparison: trained_probes=%d matched=%d "
        "pinned=%d matched_excluding_pinned=%d final_session_mean_delta=%s "
        "final_session_mean_delta_excluding_pinned=%s",
        len(trained_probe_ids), len(trained_per_probe), len(pinned_trained_probe_ids),
        len(unpinned_trained_per_probe), final_session_mean_delta,
        final_session_mean_delta_excluding_pinned,
    )
    return {
        "trained_probe_count": len(trained_probe_ids),
        "matched_probe_count": len(trained_per_probe),
        "per_probe": trained_per_probe,
        "mean_delta_by_session": mean_delta_by_session,
        "final_session_mean_delta": final_session_mean_delta,
        "pinned_trained_probe_ids": pinned_trained_probe_ids,
        "matched_probe_count_excluding_pinned": len(unpinned_trained_per_probe),
        "mean_delta_by_session_excluding_pinned": mean_delta_by_session_excluding_pinned,
        "final_session_mean_delta_excluding_pinned": final_session_mean_delta_excluding_pinned,
    }


def _compute_pinned_probes(control_sample_pass_rates: dict) -> set[str]:
    """
    A probe is "control-pinned" (M39) when its sample-mode pass rate is 1.0
    in EVERY session recorded for this control condition — a static
    tie-break ceiling the mechanism can never move, not evidence of
    learning. A single 1.0 session does not qualify (all sessions must hit
    the ceiling) — this is what correctly excludes vs_b1's one-off 1.0 while
    catching vs_a1/vs_c1/vs_d1 under static_weight and vs_d5 under
    random_weight on exp0b.

    A probe with zero recorded sessions is never pinned (nothing to pin on).
    """
    pinned: set[str] = set()
    for p in control_sample_pass_rates.get("per_probe") or []:
        sessions = p.get("sessions") or []
        rates = [s.get("pass_rate") for s in sessions]
        if rates and all(r == 1.0 for r in rates):
            pinned.add(p["probe_id"])
    return pinned


def _compute_pin_adjusted_mean_deltas(
    sample_pass_rate_comparison: dict,
    pinned_probe_ids: set[str],
) -> dict:
    """
    §6.2 pin-adjusted mean delta (M39) — the same by-phenomenon sample-mode
    mean delta `_compute_sample_pass_rate_comparison` already produces,
    computed a second way with control-pinned probes excluded.

    Static weights deterministically pin some probes at pass rate 1.0 in
    every session; learned's real gains elsewhere get cancelled in the mean
    by -0.92..-1.0 deltas against those degenerate ceilings (how exp0b's
    sample-mode mean read ~0.00 over data containing a 0->84% climb). Both
    means are always reported side by side — `by_phenomenon_all_probes` is
    the exact, unmodified value from the unrestricted comparison, never
    silently redefined (M34's rule): a reader who wants the old number still
    has it.
    """
    per_probe = sample_pass_rate_comparison.get("per_probe") or []
    unpinned = [p for p in per_probe if p["probe_id"] not in pinned_probe_ids]

    logger.info(
        "control_shadow pin_adjusted_mean_deltas: pinned=%d all_probe_count=%d "
        "excluding_pinned_count=%d",
        len(pinned_probe_ids), len(per_probe), len(unpinned),
    )
    return {
        "pinned_probe_ids": sorted(pinned_probe_ids),
        "by_phenomenon_all_probes": sample_pass_rate_comparison.get("by_phenomenon") or {},
        "by_phenomenon_excluding_pinned": _aggregate_deltas_by_phenomenon(unpinned),
    }


async def _collect_probe_results_for_experiment(
    store: GraphStore,
    all_probes: list[dict],
    experiment_id: str,
    condition_label: Optional[str],
) -> list[dict]:
    """
    Collect every probe result belonging to one experiment, for the
    comparative-delta computation.

    Scoped by experiment_id first (M21): condition_label is shared by every
    attempt/dry-run of a condition, so filtering by it alone would mix probe
    rows from unrelated runs of the same plan into the deltas — the same
    contamination class §6.1/§6.3 already guard against. condition_label
    remains as a fallback for rows recorded before experiment_id stamping.
    """
    collected: list[dict] = []
    for probe in all_probes:
        results = await store.query_probe_results(
            probe_id=probe["probe_id"],
            experiment_id=experiment_id,
        )
        collected.extend(results)
    if not collected and condition_label:
        for probe in all_probes:
            results = await store.query_probe_results(
                probe_id=probe["probe_id"],
                condition_label=condition_label,
            )
            collected.extend(results)
    return collected


# ---------------------------------------------------------------------------
# Main control shadow generator
# ---------------------------------------------------------------------------


async def generate_control_shadow(
    learned_experiment_id: str,
    learned_measures: dict,
    learned_condition: str,
    control_experiment_ids: dict[str, str],
    store: GraphStore,
    config: dict,
    trained_spokes: Optional[dict[str, str]] = None,
    seed_fixture_version: str = "v3",
) -> dict:
    """
    Generate the §6.2 control shadow sub-structure.

    For each control condition in control_experiment_ids:
    1. Run §6.1 learning measures analysis on the control experiment.
    2. Run control-specific sanity checks.
    3. Compute comparative deltas against the learned condition.
    4. (M39/M41) Compute the control-pinned probe set, then the trained-subset
       comparison (pin-adjusted, M41) and the all-probe pin-adjusted mean
       deltas (M39), both against the learned condition.

    trained_spokes/seed_fixture_version: forwarded to the control
    experiment's own `_section_61_learning_measures` call so its
    `trained_probes`/`cluster_winner_table` resolve identically to the
    learned condition's (same plan, same fixture — see
    experiment_report.py's `_section_61_learning_measures` docstring).

    Returns a dict keyed by control condition name (e.g. 'static_weight',
    'random_weight'), suitable for embedding as report['control_shadow'].
    """
    reporting_cfg = config.get("reporting", {})
    variance_floor = reporting_cfg.get("random_control_variance_floor", 0.05)

    # Probe-set version match check: all conditions must use the same probes
    probe_set_versions: dict[str, str] = {}
    all_probes = await store.get_all_probes()
    # The DB does not track probe_set_version per condition — use the YAML version.
    # For now we just check that probes exist; version mismatch guard is a warning.

    # Collect probe results per condition for comparative deltas — scoped by
    # experiment_id (with a legacy condition_label fallback) so another
    # attempt/dry-run of the same plan can never contaminate the deltas.
    probe_results_by_condition: dict[str, list[dict]] = {}
    probe_results_by_condition[learned_condition] = (
        await _collect_probe_results_for_experiment(
            store, all_probes, learned_experiment_id, learned_condition
        )
    )

    control_shadow: dict[str, dict] = {}

    for control_name, control_eid in control_experiment_ids.items():
        logger.info(
            "control_shadow: processing control=%s experiment_id=%s",
            control_name, control_eid,
        )

        # Fetch control condition label
        control_condition = await store.get_condition_label_for_experiment(control_eid)

        # Run §6.1 analysis for the control experiment
        from analytics.reports.experiment_report import _section_61_learning_measures
        control_measures = await _section_61_learning_measures(
            control_eid, store, config,
            trained_spokes=trained_spokes, seed_fixture_version=seed_fixture_version,
        )

        # Probe results for this control experiment (experiment_id-scoped,
        # legacy condition_label fallback)
        probe_results_by_condition[control_condition or control_name] = (
            await _collect_probe_results_for_experiment(
                store, all_probes, control_eid, control_condition
            )
        )

        # Sanity checks
        sanity_checks: dict[str, Any] = {}

        if control_name == "static_weight":
            sanity_checks["zero_weight_changes"] = await _check_static_zero_weight_changes(
                store, control_eid
            )
        elif control_name == "random_weight":
            sanity_checks["weights_rerandomized"] = await _check_random_weights_rerandomized(
                store, control_eid, variance_floor
            )
        else:
            # Unknown control type — run both generic checks
            sanity_checks["zero_weight_changes"] = await _check_static_zero_weight_changes(
                store, control_eid
            )

        sanity_checks["zero_feedback_edge_correlation"] = (
            await _check_zero_feedback_edge_correlation(store, control_eid)
        )

        # Comparative deltas (mean-mode probe results)
        comparative_deltas = _compute_comparative_deltas(
            learned_measures=learned_measures,
            control_measures=control_measures,
            probe_results_by_condition=probe_results_by_condition,
            learned_condition=learned_condition,
            control_condition=control_condition or control_name,
        )

        # Sample-mode (weight_mode=sample) pass-rate comparison — additive,
        # does not replace the mean-mode comparison above (M34).
        sample_pass_rate_comparison = _compute_sample_pass_rate_comparison(
            learned_measures=learned_measures,
            control_measures=control_measures,
        )

        # Control-pinned probe set (M39 §4 / M41) — pinned status is
        # control-specific (static_weight and random_weight pin different
        # probes), and is computed *before* the trained-subset comparison
        # below (M41) so that call can carry the pin-adjustment: a probe
        # pinned at a degenerate 1.0 ceiling under this control feeds both
        # the all-probe pin-adjusted mean (M39) and the trained-subset
        # pin-adjusted mean (M41) from the same computation.
        pinned_probe_ids = _compute_pinned_probes(control_measures.get("sample_pass_rates") or {})

        # Trained-subset comparison (M39 §2, pin-adjusted M41) — trained_probes
        # resolves identically off learned_measures or control_measures (same
        # trained_spokes/seed_fixture_version passed to both); using
        # learned_measures here since it's always computed, control-
        # independent of which control is currently being processed.
        from analytics.reports.spoke_membership import trained_probe_id_set
        trained_probe_ids = trained_probe_id_set(learned_measures.get("trained_probes") or {})
        trained_subset_comparison = _compute_trained_subset_comparison(
            sample_pass_rate_comparison, trained_probe_ids, pinned_probe_ids=pinned_probe_ids
        )

        # Pin-adjusted mean deltas (M39 §4) — all-probe by-phenomenon version.
        pin_adjusted_mean_deltas = _compute_pin_adjusted_mean_deltas(
            sample_pass_rate_comparison, pinned_probe_ids
        )

        control_shadow[control_name] = {
            "experiment_id": control_eid,
            "condition_label": control_condition,
            "learning_measures": control_measures,
            "sanity_checks": sanity_checks,
            "comparative_deltas": comparative_deltas,
            "sample_pass_rate_comparison": sample_pass_rate_comparison,
            "trained_subset_comparison": trained_subset_comparison,
            "pin_adjusted_mean_deltas": pin_adjusted_mean_deltas,
        }

        logger.info(
            "control_shadow: %s complete sanity_check_keys=%s",
            control_name, list(sanity_checks.keys()),
        )

    return control_shadow
