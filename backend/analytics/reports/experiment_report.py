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


async def generate_experiment_report(
    experiment_id: str,
    store: GraphStore,
) -> dict:
    """
    Generate an Experiment Report for the given experiment_id.

    Covers:
    - §5 aggregated across all series in the experiment
    - §6.3 data-integrity / confound checks
    - §6.4 statistical caveat (static text embedded in Markdown)
    - §6.1 / §6.2 placeholder keys (null) for Milestones 15 and 16

    Returns the report as a dict (caller writes JSON + Markdown to disk).
    """
    config = _load_config()
    condition_label = config.get("experiment", {}).get("condition_label")
    logger.info(
        "experiment_report: starting generation experiment_id=%s condition=%s",
        experiment_id, condition_label,
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

    report = {
        "report_id": str(uuid.uuid4()),
        "report_type": "experiment",
        "experiment_id": experiment_id,
        "condition_label": condition_label,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "series_summary": series_summary,
        "integrity_checks": integrity_checks,
        "learning_measures": None,   # filled by Milestone 15
        "control_shadow": None,       # filled by Milestone 16
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
