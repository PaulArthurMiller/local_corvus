"""
Experiment Report Generator — §5 aggregation + §6.3 integrity checks + §6.4 caveat.

Reads from the DB (via GraphStore methods) and the series-level section functions
from series_report.py. Returns a structured dict; the caller writes JSON + Markdown
to disk and registers the paths in generated_reports.

§6.1 (learning measures) and §6.2 (control shadow) are added as null placeholder
keys — filled in by Milestones 15 and 16 respectively.

All database access goes through memory.store — no direct SQLite here.
"""

import json
import logging
import os
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

import yaml

from memory.store import GraphStore

# Re-use the per-series section functions rather than re-implementing them.
from analytics.reports.series_report import (
    _section_51_failure_summary,
    _section_53_retrieval_rate_classification,
    _section_55_edge_weight_deltas,
    _section_56_probe_integrity,
    _section_57_mode3_trigger_count,
    _section_58_num_ctx_pressure,
)

logger = logging.getLogger(__name__)

_CONFIG_PATH = os.getenv(
    "CONFIG_PATH",
    os.path.join(os.path.dirname(__file__), "..", "..", "..", "config.yaml"),
)

_REPORTS_DIR = Path(os.getenv("MEMORY_PATH", "/app/data")) / "reports"

STATISTICAL_CAVEAT_TEXT = """\
## §6.4 Statistical Note

At ~100+ prompts total per experiment, this report states raw counts and simple
deltas without implying statistical significance the sample size cannot support.
All reported differences are directional signals for hypothesis generation, not
confirmed effects."""


def _load_config() -> dict:
    with open(_CONFIG_PATH, "r", encoding="utf-8") as f:
        return yaml.safe_load(f)


# ---------------------------------------------------------------------------
# §5 aggregation helpers
# ---------------------------------------------------------------------------


def _aggregate_failures(series_data: dict[str, dict]) -> dict:
    """
    §5.1 aggregation — total failure count, per-series breakdown, all ghost incidents.
    """
    total_failed = 0
    total_ghost = 0
    per_series = []
    all_ghost_incidents = []

    for sid, data in series_data.items():
        s = data["sections"]["s5_1"]
        total_failed += s.get("failed_turn_count", 0)
        total_ghost += s.get("ghost_mu_count", 0)
        per_series.append(
            {
                "series_id": sid,
                "failed_turn_count": s.get("failed_turn_count", 0),
                "ghost_mu_count": s.get("ghost_mu_count", 0),
            }
        )
        all_ghost_incidents.extend(s.get("ghost_incidents") or [])

    return {
        "total_failed_turn_count": total_failed,
        "total_ghost_mu_incidents": total_ghost,
        "per_series": per_series,
        "all_ghost_incidents": all_ghost_incidents,
    }


def _aggregate_retrieval_rates(series_data: dict[str, dict]) -> dict:
    """
    §5.3 aggregation — mean intra-series rate ± range; flag outlier series.

    A series is flagged if its rate differs from the mean by more than 0.15
    (15 percentage points) — a heuristic threshold for a notable outlier.
    """
    rates = []
    per_series = []

    for sid, data in series_data.items():
        s = data["sections"]["s5_3"]
        rate = s.get("intra_series_rate", 0.0)
        rates.append(rate)
        per_series.append(
            {
                "series_id": sid,
                "intra_series_rate": rate,
                "total_retrieved": s.get("total_retrieved", 0),
                "intra_series_count": s.get("intra_series_count", 0),
                "seed_fixture_count": s.get("seed_fixture_count", 0),
                "cross_series_count": s.get("cross_series_count", 0),
                "unknown_count": s.get("unknown_count", 0),
            }
        )

    mean_rate = sum(rates) / len(rates) if rates else 0.0
    min_rate = min(rates) if rates else 0.0
    max_rate = max(rates) if rates else 0.0

    outlier_threshold = 0.15
    outlier_series = [
        p["series_id"]
        for p in per_series
        if abs(p["intra_series_rate"] - mean_rate) > outlier_threshold
    ]

    logger.info(
        "experiment_report §5.3: mean_rate=%.3f min=%.3f max=%.3f outliers=%s",
        mean_rate, min_rate, max_rate, outlier_series,
    )
    return {
        "mean_intra_series_rate": round(mean_rate, 4),
        "min_intra_series_rate": round(min_rate, 4),
        "max_intra_series_rate": round(max_rate, 4),
        "outlier_series": outlier_series,
        "outlier_threshold": outlier_threshold,
        "per_series": per_series,
    }


def _aggregate_edge_deltas(series_data: dict[str, dict]) -> dict:
    """
    §5.5 aggregation — total edges touched, count increased/decreased/unchanged,
    mean |delta|.
    """
    total_edges = 0
    increased = 0
    decreased = 0
    unchanged = 0
    abs_deltas: list[float] = []
    per_series = []

    for sid, data in series_data.items():
        s = data["sections"]["s5_5"]
        deltas = s.get("deltas") or []
        count = s.get("activated_edge_count", 0)
        total_edges += count

        s_increased = sum(1 for d in deltas if d.get("delta", 0.0) > 0)
        s_decreased = sum(1 for d in deltas if d.get("delta", 0.0) < 0)
        s_unchanged = sum(1 for d in deltas if d.get("delta", 0.0) == 0.0)
        increased += s_increased
        decreased += s_decreased
        unchanged += s_unchanged
        abs_deltas.extend(abs(d.get("delta", 0.0)) for d in deltas)

        per_series.append(
            {
                "series_id": sid,
                "activated_edge_count": count,
                "edges_increased": s_increased,
                "edges_decreased": s_decreased,
                "edges_unchanged": s_unchanged,
            }
        )

    mean_abs_delta = sum(abs_deltas) / len(abs_deltas) if abs_deltas else 0.0

    logger.info(
        "experiment_report §5.5: total_edges=%d increased=%d decreased=%d unchanged=%d mean_abs_delta=%.4f",
        total_edges, increased, decreased, unchanged, mean_abs_delta,
    )
    return {
        "total_activated_edge_count": total_edges,
        "edges_increased": increased,
        "edges_decreased": decreased,
        "edges_unchanged": unchanged,
        "mean_abs_delta": round(mean_abs_delta, 6),
        "per_series": per_series,
    }


def _aggregate_probe_integrity(series_data: dict[str, dict]) -> dict:
    """
    §5.6 aggregation — any HARD_FAIL in any series flags the whole experiment.
    """
    hard_fail_series = []
    total_violations = 0

    for sid, data in series_data.items():
        s = data["sections"]["s5_6"]
        if s.get("status") == "HARD_FAIL":
            hard_fail_series.append(sid)
            total_violations += s.get("violation_count", 0)

    experiment_status = "HARD_FAIL" if hard_fail_series else "PASS"
    logger.info(
        "experiment_report §5.6: status=%s hard_fail_series=%s",
        experiment_status, hard_fail_series,
    )
    return {
        "status": experiment_status,
        "total_violations": total_violations,
        "hard_fail_series": hard_fail_series,
    }


def _aggregate_mode3(series_data: dict[str, dict]) -> dict:
    """
    §5.7 aggregation — total Mode-3 trigger count; flag if later series trigger
    Mode-3 more often than earlier ones (retrieval growth → summarization pressure).
    """
    per_series = []
    for sid, data in series_data.items():
        s = data["sections"]["s5_7"]
        per_series.append(
            {
                "series_id": sid,
                "mode3_mu_count": s.get("mode3_mu_count", 0),
                "anomaly_count": s.get("anomaly_count", 0),
            }
        )

    total_mode3 = sum(p["mode3_mu_count"] for p in per_series)

    # Check if later series have more Mode-3 triggers than earlier ones
    # "increasing" means the last series has a higher count than the first
    increasing_pressure = False
    if len(per_series) >= 2:
        counts = [p["mode3_mu_count"] for p in per_series]
        # Flag if the last half average > first half average
        mid = len(counts) // 2
        first_half_avg = sum(counts[:mid]) / mid if mid > 0 else 0
        second_half_avg = sum(counts[mid:]) / (len(counts) - mid) if (len(counts) - mid) > 0 else 0
        increasing_pressure = second_half_avg > first_half_avg

    logger.info(
        "experiment_report §5.7: total_mode3=%d increasing_pressure=%s",
        total_mode3, increasing_pressure,
    )
    return {
        "total_mode3_mu_count": total_mode3,
        "increasing_pressure_flag": increasing_pressure,
        "per_series": per_series,
    }


def _aggregate_ctx_pressure(series_data: dict[str, dict]) -> dict:
    """
    §5.8 aggregation — turn count above num_ctx threshold per series; flag if growing.
    """
    per_series = []
    for sid, data in series_data.items():
        s = data["sections"]["s5_8"]
        per_series.append(
            {
                "series_id": sid,
                "pressure_turn_count": s.get("pressure_turn_count", 0),
                "context_window_tokens": s.get("context_window_tokens"),
            }
        )

    # Flag if the count is growing across series
    increasing_pressure = False
    if len(per_series) >= 2:
        counts = [p["pressure_turn_count"] for p in per_series]
        mid = len(counts) // 2
        first_half_avg = sum(counts[:mid]) / mid if mid > 0 else 0
        second_half_avg = sum(counts[mid:]) / (len(counts) - mid) if (len(counts) - mid) > 0 else 0
        increasing_pressure = second_half_avg > first_half_avg

    logger.info(
        "experiment_report §5.8: per_series=%s increasing_pressure=%s",
        [(p["series_id"], p["pressure_turn_count"]) for p in per_series],
        increasing_pressure,
    )
    return {
        "increasing_pressure_flag": increasing_pressure,
        "per_series": per_series,
    }


# ---------------------------------------------------------------------------
# §6.3 integrity checks
# ---------------------------------------------------------------------------


async def _check_seed_provenance(experiment_id: str, store: GraphStore) -> dict:
    """
    §6.3 — Seed-fixture provenance check.

    Verifies that all 'seed:*' series_ids in the DB correspond to registered
    fixture versions, and checks for any fixture version inconsistencies.
    """
    seed_series_ids = await store.get_distinct_seed_series_ids()

    # Extract fixture versions from "seed:{version}" format
    fixture_versions = set()
    for sid in seed_series_ids:
        if sid.startswith("seed:"):
            fixture_versions.add(sid[len("seed:"):])

    # Check each fixture version against the manifest
    registered = {}
    unregistered_versions = []
    for version in fixture_versions:
        manifest = await store.get_seed_fixture_manifest(version)
        if manifest is not None:
            registered[version] = manifest.get("manifest_hash")
        else:
            unregistered_versions.append(version)

    # If multiple fixture versions are present, check hash consistency
    hashes = list(registered.values())
    hash_mismatch = len(set(hashes)) > 1 if hashes else False

    status = "PASS"
    if unregistered_versions:
        status = "FAIL"
    elif hash_mismatch:
        status = "FAIL"

    logger.info(
        "experiment_report §6.3 seed_provenance: status=%s versions=%s unregistered=%s hash_mismatch=%s",
        status, list(fixture_versions), unregistered_versions, hash_mismatch,
    )
    return {
        "status": status,
        "seed_series_ids_found": seed_series_ids,
        "fixture_versions_found": list(fixture_versions),
        "registered_versions": list(registered.keys()),
        "unregistered_versions": unregistered_versions,
        "hash_mismatch": hash_mismatch,
        "registered_hashes": registered,
    }


async def _check_graph_wipe(experiment_id: str, store: GraphStore) -> dict:
    """
    §6.3 — Graph-wipe verification.

    Checks that no MUs from a different experiment existed before this experiment's
    first run (would indicate an incomplete graph wipe between experiments).
    """
    earliest_ts = await store.get_earliest_run_timestamp_for_experiment(experiment_id)
    if earliest_ts is None:
        return {
            "status": "SKIP",
            "reason": "No runs found for this experiment_id",
            "pre_experiment_foreign_mu_count": 0,
            "earliest_run_timestamp": None,
        }

    foreign_count = await store.count_pre_experiment_foreign_mus(experiment_id, earliest_ts)
    status = "PASS" if foreign_count == 0 else "FAIL"

    logger.info(
        "experiment_report §6.3 graph_wipe: status=%s foreign_mu_count=%d earliest_ts=%s",
        status, foreign_count, earliest_ts,
    )
    return {
        "status": status,
        "pre_experiment_foreign_mu_count": foreign_count,
        "earliest_run_timestamp": earliest_ts,
    }


async def _check_probe_replay_index(experiment_id: str, store: GraphStore) -> dict:
    """
    §6.3 — Probe-replay-index normalization check.

    Verifies measurement_session_index values are contiguous (no gaps).
    Uses the experiment's condition_label to filter probe_results.
    """
    condition_label = await store.get_condition_label_for_experiment(experiment_id)
    if condition_label is None:
        return {
            "status": "SKIP",
            "reason": "No condition_label found for this experiment_id",
            "condition_label": None,
            "session_indices_found": [],
            "gaps": [],
        }

    indices = await store.get_probe_session_indices_for_condition(condition_label)

    if not indices:
        return {
            "status": "PASS",
            "reason": "No probe results found (probe set may not have been run yet)",
            "condition_label": condition_label,
            "session_indices_found": [],
            "gaps": [],
        }

    # Find gaps in the sequence
    expected = set(range(indices[0], indices[-1] + 1))
    actual = set(indices)
    gaps = sorted(expected - actual)

    status = "PASS" if not gaps else "FAIL"

    logger.info(
        "experiment_report §6.3 probe_replay_index: status=%s indices=%s gaps=%s condition=%s",
        status, indices, gaps, condition_label,
    )
    return {
        "status": status,
        "condition_label": condition_label,
        "session_indices_found": indices,
        "gaps": gaps,
    }


async def _section_63_integrity_checks(
    experiment_id: str,
    store: GraphStore,
) -> dict:
    """§6.3 — All three data-integrity / confound checks."""
    seed_provenance = await _check_seed_provenance(experiment_id, store)
    graph_wipe = await _check_graph_wipe(experiment_id, store)
    probe_replay = await _check_probe_replay_index(experiment_id, store)

    overall_pass = all(
        c.get("status") in ("PASS", "SKIP")
        for c in (seed_provenance, graph_wipe, probe_replay)
    )

    return {
        "overall_pass": overall_pass,
        "seed_fixture_provenance": seed_provenance,
        "graph_wipe_verification": graph_wipe,
        "probe_replay_index_normalization": probe_replay,
    }


# ---------------------------------------------------------------------------
# Main generator
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# §6.1 learning measures — pure helpers
# ---------------------------------------------------------------------------


def _classify_probe_outcome(result: dict, pass_rank_threshold: int) -> str:
    """
    Classify a single probe result as 'pass', 'fail', or 'negative_flagged'.

    'negative_flagged' requires an explicit outcome_override on the result.
    'pass' requires gold_mu_rank to be set and within the threshold.
    """
    if result.get("outcome_override") == "negative_flagged":
        return "negative_flagged"
    rank = result.get("gold_mu_rank")
    if rank is not None and rank <= pass_rank_threshold:
        return "pass"
    return "fail"


def _compute_state_transitions(
    probes: list[dict],
    results_by_probe: dict[str, list[dict]],
    pass_rank_threshold: int,
) -> dict:
    """
    For each probe, classify results as pass/fail/negative_flagged, derive a
    state sequence, and identify fail_to_pass / pass_to_fail transitions.

    Returns per-probe detail and per-phenomenon aggregates.
    """
    by_phenomenon: dict[str, dict] = {}
    per_probe: list[dict] = []

    for probe in probes:
        probe_id = probe["probe_id"]
        phenomenon = probe.get("phenomenon_type", "unknown")
        results = sorted(
            results_by_probe.get(probe_id, []),
            key=lambda r: r.get("measurement_session_index", 0),
        )

        outcomes = [_classify_probe_outcome(r, pass_rank_threshold) for r in results]
        session_indices = [r.get("measurement_session_index") for r in results]
        gold_ranks = [r.get("gold_mu_rank") for r in results]

        fail_to_pass = 0
        pass_to_fail = 0
        for i in range(len(outcomes) - 1):
            cur, nxt = outcomes[i], outcomes[i + 1]
            if cur == "fail" and nxt == "pass":
                fail_to_pass += 1
            elif cur == "pass" and nxt == "fail":
                pass_to_fail += 1

        non_flagged = [o for o in outcomes if o != "negative_flagged"]
        if not non_flagged:
            pattern = "insufficient_data"
        elif all(o == "fail" for o in non_flagged):
            pattern = "flat_fail"
        elif all(o == "pass" for o in non_flagged):
            pattern = "flat_pass"
        else:
            pattern = "mixed"

        per_probe.append({
            "probe_id": probe_id,
            "phenomenon_type": phenomenon,
            "is_bridge_probe": probe.get("is_bridge_probe", False),
            "bridge_pair_id": probe.get("bridge_pair_id"),
            "outcome_sequence": outcomes,
            "session_indices": session_indices,
            "gold_mu_ranks": gold_ranks,
            "fail_to_pass_count": fail_to_pass,
            "pass_to_fail_count": pass_to_fail,
            "pattern": pattern,
        })

        if phenomenon not in by_phenomenon:
            by_phenomenon[phenomenon] = {
                "probe_count": 0,
                "fail_to_pass": 0,
                "pass_to_fail": 0,
                "flat_fail": 0,
                "flat_pass": 0,
                "mixed": 0,
                "insufficient_data": 0,
            }
        by_phenomenon[phenomenon]["probe_count"] += 1
        by_phenomenon[phenomenon]["fail_to_pass"] += fail_to_pass
        by_phenomenon[phenomenon]["pass_to_fail"] += pass_to_fail
        by_phenomenon[phenomenon][pattern] = by_phenomenon[phenomenon].get(pattern, 0) + 1

    logger.info(
        "experiment_report §6.1 state_transitions: probes=%d phenomena=%s",
        len(per_probe),
        list(by_phenomenon.keys()),
    )
    return {
        "per_probe": per_probe,
        "by_phenomenon": by_phenomenon,
    }


def _compute_mus_by_probe_outcome(
    probes: list[dict],
    results_by_probe: dict[str, list[dict]],
    pass_rank_threshold: int,
    mu_info: dict[str, dict],
) -> list[dict]:
    """
    For each probe, group retrieved MUs by classified outcome (pass/fail/negative_flagged).

    Enables "what was retrieved when it worked vs. when it didn't" comparison.
    """
    per_probe: list[dict] = []

    for probe in probes:
        probe_id = probe["probe_id"]
        results = sorted(
            results_by_probe.get(probe_id, []),
            key=lambda r: r.get("measurement_session_index", 0),
        )

        by_outcome: dict[str, list[dict]] = {"pass": [], "fail": [], "negative_flagged": []}
        for result in results:
            outcome = _classify_probe_outcome(result, pass_rank_threshold)
            retrieved_mus = []
            for mu_id in result.get("retrieved_mu_ids") or []:
                info = mu_info.get(mu_id) or {}
                retrieved_mus.append({
                    "mu_id": mu_id,
                    "series_id": info.get("series_id"),
                    "cluster_id": info.get("cluster_id"),
                    "source_type": info.get("source_type"),
                })
            by_outcome[outcome].append({
                "measurement_session_index": result.get("measurement_session_index"),
                "retrieved_mus": retrieved_mus,
            })

        per_probe.append({
            "probe_id": probe_id,
            "phenomenon_type": probe.get("phenomenon_type"),
            "by_outcome": by_outcome,
        })

    return per_probe


def _collect_all_weight_updates(
    all_runs: list[dict],
    mu_cluster_info: dict[str, dict],
) -> list[dict]:
    """
    Collect all weight_updates entries across all runs, enriched with cluster_id
    for source and target MUs.

    Returns a flat list ordered by turn_index.
    """
    collected: list[dict] = []
    for run in all_runs:
        wupdates = run.get("weight_updates") or []
        if isinstance(wupdates, str):
            try:
                wupdates = json.loads(wupdates)
            except (ValueError, TypeError):
                wupdates = []
        for wu in wupdates:
            src_id = wu.get("source_mu_id") or ""
            tgt_id = wu.get("target_mu_id") or ""
            old_w = wu.get("old_weight") or 0.0
            new_w = wu.get("new_weight") or 0.0
            src_info = mu_cluster_info.get(src_id) or {}
            tgt_info = mu_cluster_info.get(tgt_id) or {}
            collected.append({
                "edge_id": wu.get("edge_id"),
                "old_weight": old_w,
                "new_weight": new_w,
                "delta": new_w - old_w,
                "feedback_event_id": wu.get("feedback_event_id"),
                "source_mu_id": src_id or None,
                "target_mu_id": tgt_id or None,
                "source_cluster": src_info.get("cluster_id"),
                "target_cluster": tgt_info.get("cluster_id"),
                "turn_index": run.get("turn_index"),
                "run_id": run.get("run_id"),
            })
    return collected


def _compute_edge_delta_ledger(all_updates: list[dict]) -> dict:
    """
    Aggregate weight update statistics for the §6.1 edge delta ledger.

    Includes orphan-update check (updates without a feedback_event_id) and
    within-cluster vs. cross-cluster segmentation.
    """
    increased = sum(1 for u in all_updates if u["delta"] > 0)
    decreased = sum(1 for u in all_updates if u["delta"] < 0)
    unchanged = sum(1 for u in all_updates if u["delta"] == 0)
    abs_deltas = [abs(u["delta"]) for u in all_updates]
    mean_abs_delta = sum(abs_deltas) / len(abs_deltas) if abs_deltas else 0.0

    orphan_count = sum(1 for u in all_updates if not u.get("feedback_event_id"))

    within_cluster = [
        u for u in all_updates
        if u.get("source_cluster") is not None
        and u.get("source_cluster") == u.get("target_cluster")
    ]
    cross_cluster = [
        u for u in all_updates
        if u.get("source_cluster") is not None
        and u.get("target_cluster") is not None
        and u.get("source_cluster") != u.get("target_cluster")
    ]

    logger.info(
        "experiment_report §6.1 edge_delta_ledger: total=%d orphans=%d cross_cluster=%d",
        len(all_updates), orphan_count, len(cross_cluster),
    )
    return {
        "total_updates": len(all_updates),
        "increased_count": increased,
        "decreased_count": decreased,
        "unchanged_count": unchanged,
        "mean_abs_delta": round(mean_abs_delta, 6),
        "orphan_update_count": orphan_count,
        "within_cluster_count": len(within_cluster),
        "cross_cluster_count": len(cross_cluster),
        "unknown_cluster_count": len(all_updates) - len(within_cluster) - len(cross_cluster),
        "cross_cluster_deltas": [
            {
                "edge_id": u["edge_id"],
                "delta": u["delta"],
                "source_cluster": u["source_cluster"],
                "target_cluster": u["target_cluster"],
                "feedback_event_id": u.get("feedback_event_id"),
            }
            for u in cross_cluster
        ],
    }


def _compute_cross_series_probe_mus(
    probes: list[dict],
    results_by_probe: dict[str, list[dict]],
    mu_info: dict[str, dict],
) -> list[dict]:
    """
    For each probe result, report the series_id and cluster_id of each retrieved MU.

    Answers: "which series contributed to each retrieval?"
    """
    per_probe: list[dict] = []

    for probe in probes:
        probe_id = probe["probe_id"]
        results = sorted(
            results_by_probe.get(probe_id, []),
            key=lambda r: r.get("measurement_session_index", 0),
        )

        per_session: list[dict] = []
        for result in results:
            session_mus = []
            for mu_id in result.get("retrieved_mu_ids") or []:
                info = mu_info.get(mu_id) or {}
                session_mus.append({
                    "mu_id": mu_id,
                    "series_id": info.get("series_id"),
                    "cluster_id": info.get("cluster_id"),
                })
            per_session.append({
                "measurement_session_index": result.get("measurement_session_index"),
                "retrieved_mus": session_mus,
            })

        per_probe.append({
            "probe_id": probe_id,
            "phenomenon_type": probe.get("phenomenon_type"),
            "per_session": per_session,
        })

    return per_probe


def _compute_bridge_mu_analysis(
    bridge_probes: list[dict],
    results_by_probe: dict[str, list[dict]],
    all_runs: list[dict],
    mu_cluster_info: dict[str, dict],
    pass_rank_threshold: int,
) -> dict:
    """
    Bridge-MU / cross-cluster transfer analysis.

    For each bridge probe, tracks gold_mu_rank trajectory across sessions and
    identifies rank improvements. Checks whether rank improvements follow
    cluster-A-only reinforcement events as evidence of cross-cluster transfer.

    Cross-cluster transfer candidate: a bridge probe shows rank improvement AND
    all reinforcement events (feedback_signal=1) in the experiment had source
    MUs exclusively in cluster_a.
    """
    # Build reinforcement events (thumbs_up runs) with their source clusters
    reinforcement_events: list[dict] = []
    for run in all_runs:
        if run.get("feedback_signal") == 1:
            wupdates = run.get("weight_updates") or []
            if isinstance(wupdates, str):
                try:
                    wupdates = json.loads(wupdates)
                except (ValueError, TypeError):
                    wupdates = []
            src_clusters: set[str] = set()
            for wu in wupdates:
                src_id = wu.get("source_mu_id") or ""
                cluster = (mu_cluster_info.get(src_id) or {}).get("cluster_id")
                if cluster:
                    src_clusters.add(cluster)
            reinforcement_events.append({
                "run_id": run.get("run_id"),
                "turn_index": run.get("turn_index"),
                "source_clusters": sorted(src_clusters),
            })

    bridge_probe_results: list[dict] = []
    for probe in bridge_probes:
        probe_id = probe["probe_id"]
        results = sorted(
            results_by_probe.get(probe_id, []),
            key=lambda r: r.get("measurement_session_index", 0),
        )

        rank_trajectory = [
            (r.get("measurement_session_index"), r.get("gold_mu_rank"))
            for r in results
        ]

        transfer_candidates: list[dict] = []
        for i in range(len(rank_trajectory) - 1):
            sess_n, rank_n = rank_trajectory[i]
            sess_n1, rank_n1 = rank_trajectory[i + 1]

            # Rank improvement: lower numeric rank is better; None = not found = worst
            rank_n_val = rank_n if rank_n is not None else 9999
            rank_n1_val = rank_n1 if rank_n1 is not None else 9999
            if rank_n1_val >= rank_n_val:
                continue  # no improvement this step

            # Check if all reinforcement events are cluster-A-only
            non_empty_events = [
                e for e in reinforcement_events if e["source_clusters"]
            ]
            cluster_a_only = bool(non_empty_events) and all(
                set(e["source_clusters"]) == {"cluster_a"}
                for e in non_empty_events
            )

            transfer_candidates.append({
                "session_n": sess_n,
                "session_n1": sess_n1,
                "rank_n": rank_n,
                "rank_n1": rank_n1,
                "reinforcement_event_run_ids": [e["run_id"] for e in reinforcement_events],
                "cross_cluster_transfer_flag": cluster_a_only,
            })

        bridge_probe_results.append({
            "probe_id": probe_id,
            "bridge_pair_id": probe.get("bridge_pair_id"),
            "phenomenon_type": probe.get("phenomenon_type"),
            "rank_trajectory": rank_trajectory,
            "rank_improvement_count": len(transfer_candidates),
            "transfer_candidates": transfer_candidates,
        })

    logger.info(
        "experiment_report §6.1 bridge_mu: bridge_probes=%d reinforcement_events=%d",
        len(bridge_probes), len(reinforcement_events),
    )
    return {
        "bridge_probe_count": len(bridge_probes),
        "reinforcement_event_count": len(reinforcement_events),
        "bridge_probe_results": bridge_probe_results,
    }


def _compute_saturation_analysis(all_runs: list[dict], saturation_threshold: float) -> dict:
    """
    Saturation and runaway-reinforcement detection.

    Saturation: edge reaches weight > saturation_threshold.
    Runaway: three consecutive deltas are each strictly larger than the previous
    (accelerating reinforcement, not diminishing). Fires for [0.05, 0.07, 0.10];
    does not fire for [0.10, 0.07, 0.05].
    """
    # Collect per-edge update sequences, ordered by turn_index
    edge_updates: dict[str, list[dict]] = {}
    for run in sorted(all_runs, key=lambda r: r.get("turn_index") or 0):
        wupdates = run.get("weight_updates") or []
        if isinstance(wupdates, str):
            try:
                wupdates = json.loads(wupdates)
            except (ValueError, TypeError):
                wupdates = []
        for wu in wupdates:
            eid = wu.get("edge_id")
            if not eid:
                continue
            if eid not in edge_updates:
                edge_updates[eid] = []
            old_w = wu.get("old_weight") or 0.0
            new_w = wu.get("new_weight") or 0.0
            edge_updates[eid].append({
                "old_weight": old_w,
                "new_weight": new_w,
                "delta": new_w - old_w,
                "turn_index": run.get("turn_index"),
            })

    saturation_flags: list[dict] = []
    runaway_flags: list[dict] = []

    for edge_id, updates in edge_updates.items():
        final_weight = updates[-1]["new_weight"] if updates else None

        # Saturation: final weight above threshold
        if final_weight is not None and final_weight > saturation_threshold:
            saturation_flags.append({
                "edge_id": edge_id,
                "final_weight": final_weight,
                "update_count": len(updates),
                "delta_sequence": [u["delta"] for u in updates],
            })

        # Runaway: any 3 consecutive deltas are strictly increasing
        deltas = [u["delta"] for u in updates]
        for i in range(len(deltas) - 2):
            d0, d1, d2 = deltas[i], deltas[i + 1], deltas[i + 2]
            if d1 > d0 and d2 > d1:
                runaway_flags.append({
                    "edge_id": edge_id,
                    "consecutive_deltas": [d0, d1, d2],
                    "starting_at_turn": updates[i]["turn_index"],
                })
                break  # one flag per edge is sufficient

    logger.info(
        "experiment_report §6.1 saturation: unique_edges=%d saturation=%d runaway=%d",
        len(edge_updates), len(saturation_flags), len(runaway_flags),
    )
    return {
        "saturation_threshold": saturation_threshold,
        "total_unique_edges": len(edge_updates),
        "saturation_flag_count": len(saturation_flags),
        "runaway_flag_count": len(runaway_flags),
        "saturation_flags": saturation_flags,
        "runaway_flags": runaway_flags,
    }


async def _section_61_learning_measures(
    experiment_id: str,
    store: GraphStore,
    config: dict,
) -> dict:
    """
    §6.1 — Full learning measures section.

    Requires the experiment to have probe results and feedback-signal runs.
    If no probe results exist, the section still generates with empty data.
    """
    reporting_cfg = config.get("reporting", {})
    pass_rank_threshold = reporting_cfg.get("pass_rank_threshold", 3)
    saturation_threshold = reporting_cfg.get("saturation_threshold", 0.9)

    condition_label = await store.get_condition_label_for_experiment(experiment_id)
    logger.info(
        "experiment_report §6.1: experiment_id=%s condition=%s "
        "pass_rank_threshold=%d saturation_threshold=%.2f",
        experiment_id, condition_label, pass_rank_threshold, saturation_threshold,
    )

    # All runs for this experiment (weight_updates, feedback_signal, turn_index)
    all_runs = await store.get_all_runs_for_experiment(experiment_id)
    logger.info("experiment_report §6.1: %d runs loaded", len(all_runs))

    # All probe definitions
    probes = await store.get_all_probes()

    # Probe results grouped by probe_id (filter by condition_label)
    results_by_probe: dict[str, list[dict]] = {}
    for probe in probes:
        pid = probe["probe_id"]
        results_by_probe[pid] = await store.query_probe_results(
            probe_id=pid,
            condition_label=condition_label,
        )

    # Collect all MU IDs referenced in probe results and weight_updates for info lookup
    all_mu_ids: set[str] = set()
    for results in results_by_probe.values():
        for r in results:
            all_mu_ids.update(r.get("retrieved_mu_ids") or [])
    for run in all_runs:
        wupdates = run.get("weight_updates") or []
        if isinstance(wupdates, str):
            try:
                wupdates = json.loads(wupdates)
            except (ValueError, TypeError):
                wupdates = []
        for wu in wupdates:
            if wu.get("source_mu_id"):
                all_mu_ids.add(wu["source_mu_id"])
            if wu.get("target_mu_id"):
                all_mu_ids.add(wu["target_mu_id"])

    mu_info = await store.get_mu_info_batch(list(all_mu_ids))
    logger.info("experiment_report §6.1: %d unique MU IDs looked up", len(mu_info))

    # --- Sub-sections ---
    state_transitions = _compute_state_transitions(probes, results_by_probe, pass_rank_threshold)
    mus_by_probe_outcome = _compute_mus_by_probe_outcome(
        probes, results_by_probe, pass_rank_threshold, mu_info
    )

    all_updates = _collect_all_weight_updates(all_runs, mu_info)
    edge_delta_ledger = _compute_edge_delta_ledger(all_updates)

    cross_series_probe_mus = _compute_cross_series_probe_mus(
        probes, results_by_probe, mu_info
    )

    bridge_probes = [p for p in probes if p.get("is_bridge_probe")]
    bridge_mu_analysis = _compute_bridge_mu_analysis(
        bridge_probes, results_by_probe, all_runs, mu_info, pass_rank_threshold
    )

    saturation_analysis = _compute_saturation_analysis(all_runs, saturation_threshold)

    return {
        "pass_rank_threshold": pass_rank_threshold,
        "saturation_threshold": saturation_threshold,
        "condition_label": condition_label,
        "probe_count": len(probes),
        "run_count": len(all_runs),
        "state_transitions": state_transitions,
        "mus_by_probe_outcome": mus_by_probe_outcome,
        "edge_delta_ledger": edge_delta_ledger,
        "cross_series_probe_mus": cross_series_probe_mus,
        "bridge_mu_analysis": bridge_mu_analysis,
        "saturation_analysis": saturation_analysis,
    }


async def generate_experiment_report(
    experiment_id: str,
    store: GraphStore,
    control_experiment_ids: Optional[dict[str, str]] = None,
) -> dict:
    """
    Generate an Experiment Report for the given experiment_id.

    Covers:
    - §5 aggregated across all series in the experiment
    - §6.3 data-integrity / confound checks
    - §6.4 statistical caveat (static text embedded in Markdown)
    - §6.1 learning measures
    - §6.2 control shadow (when control_experiment_ids is provided)

    Returns the report as a dict (caller writes JSON + Markdown to disk).
    """
    config = _load_config()
    condition_label = config.get("experiment", {}).get("condition_label")
    logger.info(
        "experiment_report: starting generation experiment_id=%s condition=%s control_ids=%s",
        experiment_id, condition_label, list(control_experiment_ids.keys()) if control_experiment_ids else None,
    )

    # Enumerate all series in this experiment
    series_ids = await store.get_all_series_for_experiment(experiment_id)
    logger.info("experiment_report: found %d series: %s", len(series_ids), series_ids)

    # Gather per-series section data (re-query directly — always current)
    series_data: dict[str, dict] = {}
    for sid in series_ids:
        runs = await store.get_all_runs_for_series(sid)
        series_data[sid] = {
            "run_count": len(runs),
            "sections": {
                "s5_1": await _section_51_failure_summary(runs, store, sid),
                "s5_3": await _section_53_retrieval_rate_classification(runs, sid, store),
                "s5_5": await _section_55_edge_weight_deltas(runs, store),
                "s5_6": await _section_56_probe_integrity(sid, store),
                "s5_7": await _section_57_mode3_trigger_count(sid, store, config),
                "s5_8": _section_58_num_ctx_pressure(runs, config),
            },
        }

    # §5 aggregation
    series_summary = {
        "series_ids": series_ids,
        "series_count": len(series_ids),
        "total_run_count": sum(d["run_count"] for d in series_data.values()),
        "s5_1_failure_aggregation": _aggregate_failures(series_data),
        "s5_3_retrieval_rate_aggregation": _aggregate_retrieval_rates(series_data),
        "s5_5_edge_weight_aggregation": _aggregate_edge_deltas(series_data),
        "s5_6_probe_integrity_aggregation": _aggregate_probe_integrity(series_data),
        "s5_7_mode3_aggregation": _aggregate_mode3(series_data),
        "s5_8_ctx_pressure_aggregation": _aggregate_ctx_pressure(series_data),
    }

    # §6.3 integrity checks
    integrity_checks = await _section_63_integrity_checks(experiment_id, store)

    # §6.1 learning measures
    learning_measures = await _section_61_learning_measures(experiment_id, store, config)

    # §6.2 control shadow (optional — only when control_experiment_ids is provided)
    control_shadow = None
    if control_experiment_ids:
        from analytics.reports.control_shadow import generate_control_shadow
        control_shadow = await generate_control_shadow(
            learned_experiment_id=experiment_id,
            learned_measures=learning_measures,
            learned_condition=condition_label,
            control_experiment_ids=control_experiment_ids,
            store=store,
            config=config,
        )
        logger.info(
            "experiment_report: control_shadow computed for %d conditions",
            len(control_shadow),
        )

    report = {
        "report_id": str(uuid.uuid4()),
        "report_type": "experiment",
        "experiment_id": experiment_id,
        "condition_label": condition_label,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "series_summary": series_summary,
        "integrity_checks": integrity_checks,
        "learning_measures": learning_measures,
        "control_shadow": control_shadow,
    }

    logger.info("experiment_report: generation complete report_id=%s", report["report_id"])
    return report


async def write_experiment_report(report: dict) -> tuple[str, str]:
    """
    Write the experiment report dict to JSON + Markdown files in /app/memory/reports/.

    Returns (json_path, md_path) as strings.
    """
    from analytics.reports.markdown_renderer import render_experiment_report_markdown

    _REPORTS_DIR.mkdir(parents=True, exist_ok=True)

    exp_id = report["experiment_id"].replace("/", "-")
    base_name = f"experiment_report_{exp_id}"

    json_path = _REPORTS_DIR / f"{base_name}.json"
    md_path = _REPORTS_DIR / f"{base_name}.md"

    json_path.write_text(json.dumps(report, indent=2, default=str), encoding="utf-8")
    md_path.write_text(render_experiment_report_markdown(report), encoding="utf-8")

    logger.info("experiment_report: wrote %s and %s", json_path, md_path)
    return str(json_path), str(md_path)
