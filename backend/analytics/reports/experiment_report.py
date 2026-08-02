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
import re
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
    resolve_report_path,
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


# Maps the short per-series section keys used by the §5 aggregators to the
# full section names used in persisted series report JSON files.
_SERIES_SECTION_KEY_MAP = {
    "s5_1": "s5_1_failure_summary",
    "s5_3": "s5_3_retrieval_rate_classification",
    "s5_5": "s5_5_edge_weight_deltas",
    "s5_6": "s5_6_probe_integrity",
    "s5_7": "s5_7_mode3_trigger_count",
    "s5_8": "s5_8_num_ctx_pressure",
}


async def _load_stored_series_data(
    experiment_id: str, store: GraphStore
) -> dict[str, dict]:
    """
    Load per-series §5 section data from series reports persisted at
    series-close time.

    The experiment report runs at the end of a multi-condition harness run,
    after later conditions' wipe_graph() calls have already deleted this
    condition's MUs — recomputing MU-dependent sections (§5.3 retrieval
    classification, §5.7 Mode-3 counts) from the live graph at that point
    misclassifies every deleted MU as "unknown". The stored series report is
    the snapshot taken while the series' MUs still existed, so it is the
    authoritative source; recomputation remains the fallback for any series
    without a readable stored report (e.g. ad-hoc report generation via the
    admin API before series reports exist).

    Returns {series_id: {"run_count": int, "sections": {short_key: data}}}
    for every series whose newest stored report is readable and complete.
    """
    stored: dict[str, dict] = {}
    rows = await store.query_generated_reports(
        report_type="series", experiment_id=experiment_id
    )

    # Rows are ordered generated_at DESC — the first row seen for a series
    # is its newest report and wins.
    for row in rows:
        sid = row.get("series_id")
        stored_json_path = row.get("json_path")
        if not sid or sid in stored or not stored_json_path:
            continue
        json_path = resolve_report_path(stored_json_path)
        try:
            with open(json_path, "r", encoding="utf-8") as f:
                report = json.load(f)
        except (OSError, ValueError) as exc:
            logger.warning(
                "experiment_report: stored series report unreadable, will "
                "recompute series=%s path=%s error=%s",
                sid, json_path, exc,
            )
            continue

        sections = report.get("sections") or {}
        if "run_count" not in report or not all(
            long_key in sections for long_key in _SERIES_SECTION_KEY_MAP.values()
        ):
            logger.warning(
                "experiment_report: stored series report incomplete, will "
                "recompute series=%s path=%s",
                sid, json_path,
            )
            continue

        stored[sid] = {
            "run_count": report["run_count"],
            "sections": {
                short: sections[long_key]
                for short, long_key in _SERIES_SECTION_KEY_MAP.items()
            },
        }

    logger.info(
        "experiment_report: loaded %d stored series report(s) for experiment_id=%s",
        len(stored), experiment_id,
    )
    return stored


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


def _aggregate_verdict_mix(
    series_ids: list[str], verdict_counts: dict[str, dict[str, int]]
) -> dict:
    """
    Verdict-mix subsection — per-series up/down/none counts and up-rate.

    up_rate = up / (up + down); `none` (partial-answer/abstain) verdicts
    are excluded from the denominator since they carry no learning signal
    to be right or wrong about. A series with no bulk-turn feedback at all
    (all-probe series, dry runs) reports up_rate=None rather than 0.0 —
    "no data" and "0% up" are different findings.
    """
    per_series = []
    total_up = total_down = total_none = 0

    for sid in series_ids:
        counts = verdict_counts.get(sid) or {"up": 0, "down": 0, "none": 0}
        up = counts.get("up", 0)
        down = counts.get("down", 0)
        none_ct = counts.get("none", 0)
        graded = up + down
        up_rate = round(up / graded, 4) if graded > 0 else None
        per_series.append(
            {
                "series_id": sid,
                "up": up,
                "down": down,
                "none": none_ct,
                "up_rate": up_rate,
            }
        )
        total_up += up
        total_down += down
        total_none += none_ct

    total_graded = total_up + total_down
    overall_up_rate = round(total_up / total_graded, 4) if total_graded > 0 else None

    logger.info(
        "experiment_report verdict_mix: total_up=%d total_down=%d total_none=%d overall_up_rate=%s",
        total_up, total_down, total_none, overall_up_rate,
    )
    return {
        "total_up": total_up,
        "total_down": total_down,
        "total_none": total_none,
        "overall_up_rate": overall_up_rate,
        "per_series": per_series,
    }


def _aggregate_retrieval_rates(series_data: dict[str, dict]) -> dict:
    """
    §5.3 aggregation — mean intra-series rate ± range; flag outlier series.

    A series is flagged if its rate differs from the mean by more than 0.15
    (15 percentage points) — a heuristic threshold for a notable outlier.

    M32: also carries seed_share/cross_share (the M32 seed_fixture_rate /
    cross_series_rate fields from series_report.py's §5.3) through to the
    per-series rows, plus a first-series-vs-last-series seed_share drift
    statement — MU-accumulation drift (seed-fixture MUs losing retrieval
    share to accumulated conversation MUs across series) is a permanent,
    measured-not-controlled feature of the system (Paul's call, 2026-08-02).
    Stored series reports written before M32 don't have these two fields;
    `seed_share`/`cross_share` are None in that case rather than recomputed
    from the raw counts, so the aggregation never silently disagrees with
    what series_report.py itself would report for the same series.
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
                "seed_share": s.get("seed_fixture_rate"),
                "cross_share": s.get("cross_series_rate"),
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

    # Seed-share drift: first series vs. last series, in series order. Only
    # meaningful with >= 2 series and only computed when both endpoints
    # carry the M32 field — a pre-M32 stored report at either end means the
    # drift can't be honestly stated, not that it's zero.
    seed_share_drift = None
    if len(per_series) >= 2:
        first, last = per_series[0], per_series[-1]
        if first["seed_share"] is not None and last["seed_share"] is not None:
            seed_share_drift = {
                "first_series_id": first["series_id"],
                "first_seed_share": first["seed_share"],
                "last_series_id": last["series_id"],
                "last_seed_share": last["seed_share"],
                "delta": round(last["seed_share"] - first["seed_share"], 4),
            }

    logger.info(
        "experiment_report §5.3: mean_rate=%.3f min=%.3f max=%.3f outliers=%s seed_share_drift=%s",
        mean_rate, min_rate, max_rate, outlier_series, seed_share_drift,
    )
    return {
        "mean_intra_series_rate": round(mean_rate, 4),
        "min_intra_series_rate": round(min_rate, 4),
        "max_intra_series_rate": round(max_rate, 4),
        "outlier_series": outlier_series,
        "outlier_threshold": outlier_threshold,
        "seed_share_drift": seed_share_drift,
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

    M21: probe_results are scoped by experiment_id first — condition_label
    is shared across every attempt/dry-run of a condition, so filtering by
    it alone would mix measurement sessions from unrelated runs. The
    condition_label path remains as a fallback for rows recorded before
    experiment_id stamping existed.
    """
    condition_label = await store.get_condition_label_for_experiment(experiment_id)
    indices = await store.get_probe_session_indices_for_experiment(experiment_id)

    if not indices and condition_label is not None:
        # Legacy fallback: pre-M21 probe rows carry no experiment_id.
        indices = await store.get_probe_session_indices_for_condition(condition_label)

    if not indices:
        if condition_label is None:
            return {
                "status": "SKIP",
                "reason": "No condition_label found for this experiment_id",
                "condition_label": None,
                "session_indices_found": [],
                "gaps": [],
            }
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


def _classify_probe_outcome(
    result: dict,
    pass_rank_threshold: int,
    phenomenon_type: str = "",
    phenomenon_thresholds: Optional[dict] = None,
) -> str:
    """
    Classify a single probe result as 'pass', 'fail', or 'negative_flagged'.

    threshold is looked up per phenomenon_type first, then falls back to
    pass_rank_threshold (the global default). gold_mu_rank is always the raw
    rank — never modified here.

    'negative_flagged' requires an explicit outcome_override on the result.
    'pass' requires gold_mu_rank to be set and within the effective threshold.
    """
    if result.get("outcome_override") == "negative_flagged":
        return "negative_flagged"
    rank = result.get("gold_mu_rank")
    threshold = (phenomenon_thresholds or {}).get(phenomenon_type, pass_rank_threshold)
    if rank is not None and rank <= threshold:
        return "pass"
    return "fail"


def _compute_state_transitions(
    probes: list[dict],
    results_by_probe: dict[str, list[dict]],
    pass_rank_threshold: int,
    phenomenon_thresholds: Optional[dict] = None,
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

        outcomes = [
            _classify_probe_outcome(
                r, pass_rank_threshold,
                phenomenon_type=phenomenon,
                phenomenon_thresholds=phenomenon_thresholds,
            )
            for r in results
        ]
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


def _compute_sample_pass_rates(
    probes: list[dict],
    sample_results_by_probe: dict[str, list[dict]],
    pass_rank_threshold: int,
    configured_repeats: int,
    phenomenon_thresholds: Optional[dict] = None,
) -> dict:
    """
    For each probe, group its weight_mode=sample battery repeats by
    measurement_session_index and compute a pass_rate per session.

    Parallel in shape to _compute_state_transitions, but a "session" here is
    a set of K independent Thompson-sample repeats rather than a single
    deterministic read — pass_rate is the fraction of non-flagged repeats
    that passed, not a single pass/fail outcome. partial_battery flags a
    session that stopped short of configured_repeats (a soft-continued
    battery), so a truncated read is never silently treated as a full one.
    """
    by_phenomenon: dict[str, dict] = {}
    per_probe: list[dict] = []

    for probe in probes:
        probe_id = probe["probe_id"]
        phenomenon = probe.get("phenomenon_type", "unknown")
        results = sample_results_by_probe.get(probe_id, [])

        by_session: dict[int, list[dict]] = {}
        for r in results:
            idx = r.get("measurement_session_index")
            by_session.setdefault(idx, []).append(r)

        sessions: list[dict] = []
        for session_idx in sorted(by_session.keys()):
            rows = by_session[session_idx]
            outcomes = [
                _classify_probe_outcome(
                    r, pass_rank_threshold,
                    phenomenon_type=phenomenon,
                    phenomenon_thresholds=phenomenon_thresholds,
                )
                for r in rows
            ]
            non_flagged = [o for o in outcomes if o != "negative_flagged"]
            pass_count = sum(1 for o in non_flagged if o == "pass")
            fail_count = len(non_flagged) - pass_count
            pass_rate = (
                round(pass_count / len(non_flagged), 4) if non_flagged else None
            )
            sessions.append({
                "session_index": session_idx,
                "pass_count": pass_count,
                "fail_count": fail_count,
                "repeats_completed": len(rows),
                "pass_rate": pass_rate,
                "partial_battery": len(rows) < configured_repeats,
            })

        per_probe.append({
            "probe_id": probe_id,
            "phenomenon_type": phenomenon,
            "sessions": sessions,
        })

        if phenomenon not in by_phenomenon:
            by_phenomenon[phenomenon] = {
                "probe_count": 0,
                "session_count": 0,
                "partial_battery_count": 0,
            }
        by_phenomenon[phenomenon]["probe_count"] += 1
        by_phenomenon[phenomenon]["session_count"] += len(sessions)
        by_phenomenon[phenomenon]["partial_battery_count"] += sum(
            1 for s in sessions if s["partial_battery"]
        )

    logger.info(
        "experiment_report §6.1 sample_pass_rates: probes=%d configured_repeats=%d",
        len(per_probe), configured_repeats,
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
    phenomenon_thresholds: Optional[dict] = None,
) -> list[dict]:
    """
    For each probe, group retrieved MUs by classified outcome (pass/fail/negative_flagged).

    Enables "what was retrieved when it worked vs. when it didn't" comparison.
    """
    per_probe: list[dict] = []

    for probe in probes:
        probe_id = probe["probe_id"]
        phenomenon = probe.get("phenomenon_type", "unknown")
        results = sorted(
            results_by_probe.get(probe_id, []),
            key=lambda r: r.get("measurement_session_index", 0),
        )

        by_outcome: dict[str, list[dict]] = {"pass": [], "fail": [], "negative_flagged": []}
        for result in results:
            outcome = _classify_probe_outcome(
                result, pass_rank_threshold,
                phenomenon_type=phenomenon,
                phenomenon_thresholds=phenomenon_thresholds,
            )
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


# M33: prompt_gen.py's generator names every bulk prompt
# f"{cluster_id}_{i:04d}" (e.g. "cluster_a_0007") — the target cluster is
# baked into the ID string itself. Reading it back with a regex means the
# "prompt lineage" signal below works directly off feedback_events.prompt_id
# (already-stored data) without needing the prompt bank JSON file to still
# exist on disk at report time.
_PROMPT_ID_CLUSTER_RE = re.compile(r"^(.+)_\d+$")


def _cluster_from_prompt_id(prompt_id: Optional[str]) -> Optional[str]:
    """Derive a bulk prompt's target cluster from its prompt_id, or None."""
    if not prompt_id:
        return None
    match = _PROMPT_ID_CLUSTER_RE.match(prompt_id)
    return match.group(1) if match else None


def _build_mu_lineage_clusters(
    all_runs: list[dict],
    run_prompt_ids: dict[str, str],
) -> dict[str, str]:
    """
    Map conversation MU id -> the cluster of the bulk prompt whose turn
    created it (M33 "prompt lineage" signal).

    Every MU a turn creates — input, prompt, output, and any tool_call/
    tool_result MUs — inherits that turn's prompt's target cluster. Turns
    with no prompt_id (probe turns, dry runs, turns predating M19 Phase 2)
    contribute nothing here; their MUs stay unresolved unless a seed
    endpoint classifies the edge instead.
    """
    lineage: dict[str, str] = {}
    for run in all_runs:
        run_id = run.get("run_id")
        prompt_id = run_prompt_ids.get(run_id) if run_id else None
        cluster = _cluster_from_prompt_id(prompt_id)
        if cluster is None:
            continue
        mu_ids = [
            run.get("input_mu_id"),
            run.get("prompt_mu_id"),
            run.get("output_mu_id"),
            *(run.get("tool_calls") or []),
        ]
        for mu_id in mu_ids:
            if mu_id:
                lineage[mu_id] = cluster
    return lineage


def _collect_all_weight_updates(
    all_runs: list[dict],
    mu_cluster_info: dict[str, dict],
    mu_lineage_clusters: Optional[dict[str, str]] = None,
) -> list[dict]:
    """
    Collect all weight_updates entries across all runs, enriched with cluster_id
    for source and target MUs.

    `mu_lineage_clusters` (M33) supplies the prompt-lineage cluster for
    conversation MUs — the seed-adjacency cluster (`mu_cluster_info`) is
    None for those, so the two signals never collide on the same MU.

    Returns a flat list ordered by turn_index.
    """
    mu_lineage_clusters = mu_lineage_clusters or {}
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
                "source_lineage_cluster": mu_lineage_clusters.get(src_id),
                "target_lineage_cluster": mu_lineage_clusters.get(tgt_id),
                "turn_index": run.get("turn_index"),
                "run_id": run.get("run_id"),
                "series_id": run.get("series_id"),
            })
    return collected


EDGE_CLASSES = ("seed_seed", "conv_seed", "conv_conv")
CLUSTER_RELATIONS = ("same_cluster", "cross_cluster", "mixed", "unresolvable")

# The cross-domain-linkage watch list (M33): a conv_seed edge whose two
# independent signals (seed adjacency, prompt lineage) disagree, or a
# conv_conv edge whose two prompt-lineage-derived clusters disagree, is the
# operational definition of emergent cross-domain learning outside the seed
# fixture — the phenomenon the classification exists to surface.
_CROSS_DOMAIN_CELLS = frozenset({("conv_seed", "mixed"), ("conv_conv", "cross_cluster")})


def _classify_edge_update(u: dict) -> tuple[str, str]:
    """
    Classify one weight update's edge by endpoint provenance (M33).

    Edge class depends only on whether each endpoint is a seed MU (has a
    cluster_id via seed adjacency) or a conversation MU:
      - seed_seed: both endpoints are seed MUs
      - conv_seed: one seed, one conversation
      - conv_conv: both endpoints are conversation MUs

    Cluster relation compares whichever cluster each endpoint resolves to:
      - same_cluster:  both endpoints resolve to the same cluster
      - cross_cluster: both endpoints resolved via the SAME signal type
                       (seed_seed, or conv_conv with both lineages known)
                       and disagree
      - mixed:         conv_seed only — the seed-adjacency cluster and the
                       conversation endpoint's prompt-lineage cluster are
                       two independent signals about the same edge, and
                       they disagree. Kept separate from cross_cluster
                       because it is evidence the conversation content
                       itself crossed a cluster boundary, not an artifact
                       of a single segmentation scheme.
      - unresolvable:  at least one endpoint has no cluster from either
                       signal (e.g. a tool_call/tool_result MU whose turn
                       had no prompt_id, or a probe/dry-run turn) — the
                       "small residual" the flat unknown-cluster count
                       should shrink to (was 78% of exp0's updates).
    """
    src_seed = u.get("source_cluster")
    tgt_seed = u.get("target_cluster")
    src_lineage = u.get("source_lineage_cluster")
    tgt_lineage = u.get("target_lineage_cluster")

    src_is_seed = src_seed is not None
    tgt_is_seed = tgt_seed is not None

    if src_is_seed and tgt_is_seed:
        return "seed_seed", ("same_cluster" if src_seed == tgt_seed else "cross_cluster")

    if src_is_seed or tgt_is_seed:
        seed_cluster = src_seed if src_is_seed else tgt_seed
        conv_lineage = tgt_lineage if src_is_seed else src_lineage
        if conv_lineage is None:
            return "conv_seed", "unresolvable"
        return "conv_seed", ("same_cluster" if conv_lineage == seed_cluster else "mixed")

    # Both endpoints are conversation MUs.
    if src_lineage is None or tgt_lineage is None:
        return "conv_conv", "unresolvable"
    return "conv_conv", ("same_cluster" if src_lineage == tgt_lineage else "cross_cluster")


def _compute_edge_delta_ledger(all_updates: list[dict]) -> dict:
    """
    Aggregate weight update statistics for the §6.1 edge delta ledger.

    Includes an orphan-update check (updates without a feedback_event_id)
    and the M33 edge classification — edge class (seed_seed / conv_seed /
    conv_conv) x cluster relation (same_cluster / cross_cluster / mixed /
    unresolvable) — which replaces the earlier flat within/cross/unknown
    counts. Those counted every conv_seed and conv_conv edge alike as
    "unknown cluster" — 78% of exp0's 225 updates — because they only ever
    checked seed adjacency; M33 adds the prompt-lineage signal so most of
    that 78% resolves to a real class instead of a residual.
    """
    increased = sum(1 for u in all_updates if u["delta"] > 0)
    decreased = sum(1 for u in all_updates if u["delta"] < 0)
    unchanged = sum(1 for u in all_updates if u["delta"] == 0)
    abs_deltas = [abs(u["delta"]) for u in all_updates]
    mean_abs_delta = sum(abs_deltas) / len(abs_deltas) if abs_deltas else 0.0

    orphan_count = sum(1 for u in all_updates if not u.get("feedback_event_id"))

    # Classify once per update; every downstream grouping reads from this.
    classified = [(u, *_classify_edge_update(u)) for u in all_updates]

    def _cell(updates: list[dict]) -> dict:
        deltas = [abs(x["delta"]) for x in updates]
        return {
            "count": len(updates),
            "mean_abs_delta": round(sum(deltas) / len(deltas), 6) if deltas else 0.0,
        }

    classification = {
        cls: {
            rel: _cell([u for u, c, r in classified if c == cls and r == rel])
            for rel in CLUSTER_RELATIONS
        }
        for cls in EDGE_CLASSES
    }
    totals_by_relation = {
        rel: sum(classification[cls][rel]["count"] for cls in EDGE_CLASSES)
        for rel in CLUSTER_RELATIONS
    }

    cross_domain_watch = [
        {
            "edge_id": u["edge_id"],
            "edge_class": edge_class,
            "cluster_relation": relation,
            "delta": u["delta"],
            "source_cluster": u.get("source_cluster") or u.get("source_lineage_cluster"),
            "target_cluster": u.get("target_cluster") or u.get("target_lineage_cluster"),
            "feedback_event_id": u.get("feedback_event_id"),
        }
        for u, edge_class, relation in classified
        if (edge_class, relation) in _CROSS_DOMAIN_CELLS
    ]

    # Per-series longitudinal view (§6.1 spec item 3) — series ordered by
    # first appearance, matching all_runs' own turn_index ASC ordering.
    series_order: list[str] = []
    seen_series: set[str] = set()
    for u, _c, _r in classified:
        sid = u.get("series_id")
        if sid and sid not in seen_series:
            seen_series.add(sid)
            series_order.append(sid)

    by_series = [
        {
            "series_id": sid,
            "by_class": {
                cls: _cell([u for u, c, _r in classified if u.get("series_id") == sid and c == cls])
                for cls in EDGE_CLASSES
            },
        }
        for sid in series_order
    ]

    logger.info(
        "experiment_report §6.1 edge_delta_ledger: total=%d orphans=%d "
        "seed_seed=%d conv_seed=%d conv_conv=%d unresolvable=%d",
        len(all_updates), orphan_count,
        sum(classification["seed_seed"][r]["count"] for r in CLUSTER_RELATIONS),
        sum(classification["conv_seed"][r]["count"] for r in CLUSTER_RELATIONS),
        sum(classification["conv_conv"][r]["count"] for r in CLUSTER_RELATIONS),
        totals_by_relation["unresolvable"],
    )
    return {
        "total_updates": len(all_updates),
        "increased_count": increased,
        "decreased_count": decreased,
        "unchanged_count": unchanged,
        "mean_abs_delta": round(mean_abs_delta, 6),
        "orphan_update_count": orphan_count,
        "edge_classification": {
            "by_class_relation": classification,
            "totals_by_relation": totals_by_relation,
            "cross_domain_watch": cross_domain_watch,
            "by_series": by_series,
        },
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
    phenomenon_thresholds = reporting_cfg.get("pass_rank_thresholds", {})
    saturation_threshold = reporting_cfg.get("saturation_threshold", 0.9)

    condition_label = await store.get_condition_label_for_experiment(experiment_id)
    logger.info(
        "experiment_report §6.1: experiment_id=%s condition=%s "
        "pass_rank_threshold=%d phenomenon_thresholds=%s saturation_threshold=%.2f",
        experiment_id, condition_label, pass_rank_threshold, phenomenon_thresholds, saturation_threshold,
    )

    # All runs for this experiment (weight_updates, feedback_signal, turn_index)
    all_runs = await store.get_all_runs_for_experiment(experiment_id)
    logger.info("experiment_report §6.1: %d runs loaded", len(all_runs))

    # All probe definitions
    probes = await store.get_all_probes()

    # Probe results grouped by probe_id — scoped by experiment_id (M21).
    # condition_label is shared across every attempt/dry-run of a condition,
    # so filtering by it alone would mix probe measurements from unrelated
    # runs into this experiment's learning curves. The condition_label path
    # remains as a fallback for rows recorded before experiment_id stamping.
    results_by_probe: dict[str, list[dict]] = {}
    for probe in probes:
        pid = probe["probe_id"]
        results_by_probe[pid] = await store.query_probe_results(
            probe_id=pid,
            experiment_id=experiment_id,
            battery_mode="mean",
        )
    if not any(results_by_probe.values()) and condition_label is not None:
        for probe in probes:
            pid = probe["probe_id"]
            results_by_probe[pid] = await store.query_probe_results(
                probe_id=pid,
                condition_label=condition_label,
                battery_mode="mean",
            )

    # Sample-mode battery repeats (M30) — same experiment_id-first,
    # condition_label-fallback pattern as the mean rows above, but filtered
    # to battery_mode="sample" instead. Kept as a separate lookup rather than
    # merged into results_by_probe, since mean and sample rows must never be
    # classified together (that's the exact bug this milestone fixes).
    sample_results_by_probe: dict[str, list[dict]] = {}
    for probe in probes:
        pid = probe["probe_id"]
        sample_results_by_probe[pid] = await store.query_probe_results(
            probe_id=pid,
            experiment_id=experiment_id,
            battery_mode="sample",
        )
    if not any(sample_results_by_probe.values()) and condition_label is not None:
        for probe in probes:
            pid = probe["probe_id"]
            sample_results_by_probe[pid] = await store.query_probe_results(
                probe_id=pid,
                condition_label=condition_label,
                battery_mode="sample",
            )

    # Restrict §6.1 to probes that actually recorded measurements for this
    # experiment. The probes table accumulates every synced probe-set version
    # (v1 + v2 + ...), so probes from batteries this experiment never ran
    # would otherwise render as insufficient_data noise and inflate the
    # probe/bridge counts. When no probe has results at all, keep the full
    # list — the section still generates with empty data (existing contract).
    if any(results_by_probe.values()):
        probes = [p for p in probes if results_by_probe.get(p["probe_id"])]
        logger.info(
            "experiment_report §6.1: %d probe(s) with results for this "
            "experiment (of %d synced probe definitions)",
            len(probes), len(results_by_probe),
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

    # M33 prompt-lineage signal: run_id -> bulk-turn prompt_id, then the
    # conversation MUs that turn created inherit the prompt's cluster.
    run_prompt_ids = await store.get_prompt_ids_by_run(experiment_id)
    mu_lineage_clusters = _build_mu_lineage_clusters(all_runs, run_prompt_ids)
    logger.info(
        "experiment_report §6.1: %d run(s) with a bulk prompt_id, "
        "%d conversation MU(s) resolved via prompt lineage",
        len(run_prompt_ids), len(mu_lineage_clusters),
    )

    # --- Sub-sections ---
    state_transitions = _compute_state_transitions(
        probes, results_by_probe, pass_rank_threshold, phenomenon_thresholds
    )
    mus_by_probe_outcome = _compute_mus_by_probe_outcome(
        probes, results_by_probe, pass_rank_threshold, mu_info, phenomenon_thresholds
    )

    all_updates = _collect_all_weight_updates(all_runs, mu_info, mu_lineage_clusters)
    edge_delta_ledger = _compute_edge_delta_ledger(all_updates)

    cross_series_probe_mus = _compute_cross_series_probe_mus(
        probes, results_by_probe, mu_info
    )

    bridge_probes = [p for p in probes if p.get("is_bridge_probe")]
    bridge_mu_analysis = _compute_bridge_mu_analysis(
        bridge_probes, results_by_probe, all_runs, mu_info, pass_rank_threshold
    )

    saturation_analysis = _compute_saturation_analysis(all_runs, saturation_threshold)

    configured_repeats = config.get("probe_measurement", {}).get(
        "sample_battery_repeats", 25
    )
    sample_pass_rates = _compute_sample_pass_rates(
        probes, sample_results_by_probe, pass_rank_threshold,
        configured_repeats, phenomenon_thresholds,
    )

    # Per-probe pass rate: fraction of non-flagged sessions where the probe passed.
    # Derived from state_transitions so it stays in sync with pass/fail classification.
    probe_pass_rates: dict[str, Optional[float]] = {}
    for probe_data in state_transitions.get("per_probe", []):
        pid = probe_data["probe_id"]
        outcomes = probe_data.get("outcome_sequence", [])
        non_flagged = [o for o in outcomes if o != "negative_flagged"]
        if non_flagged:
            probe_pass_rates[pid] = round(
                sum(1 for o in non_flagged if o == "pass") / len(non_flagged), 4
            )
        else:
            probe_pass_rates[pid] = None

    return {
        "pass_rank_threshold": pass_rank_threshold,
        "phenomenon_thresholds": phenomenon_thresholds,
        "saturation_threshold": saturation_threshold,
        "condition_label": condition_label,
        "probe_count": len(probes),
        "run_count": len(all_runs),
        "probe_pass_rates": probe_pass_rates,
        "state_transitions": state_transitions,
        "mus_by_probe_outcome": mus_by_probe_outcome,
        "edge_delta_ledger": edge_delta_ledger,
        "cross_series_probe_mus": cross_series_probe_mus,
        "bridge_mu_analysis": bridge_mu_analysis,
        "saturation_analysis": saturation_analysis,
        "sample_battery_repeats_configured": configured_repeats,
        "sample_pass_rates": sample_pass_rates,
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
    # Condition label comes from the DB (the runs stamped when the experiment
    # was actually run). Falling back to config.yaml is wrong when reports are
    # generated after a config switch (e.g. multi-condition smoketest sessions).
    condition_label = await store.get_condition_label_for_experiment(experiment_id)
    if condition_label is None:
        condition_label = config.get("experiment", {}).get("condition_label")
        logger.warning(
            "experiment_report: no runs found for experiment_id=%s, "
            "using config condition_label=%s",
            experiment_id, condition_label,
        )
    logger.info(
        "experiment_report: starting generation experiment_id=%s condition=%s control_ids=%s",
        experiment_id, condition_label, list(control_experiment_ids.keys()) if control_experiment_ids else None,
    )

    # Enumerate all series in this experiment
    series_ids = await store.get_all_series_for_experiment(experiment_id)
    logger.info("experiment_report: found %d series: %s", len(series_ids), series_ids)

    # Gather per-series section data. Prefer the series reports persisted at
    # series-close time: recomputing from the live graph here is wrong for
    # any condition that isn't the last one run, because each subsequent
    # condition's wipe_graph() deletes this condition's MUs and every
    # MU-dependent classification silently degrades to "unknown".
    stored_series_data = await _load_stored_series_data(experiment_id, store)
    series_data: dict[str, dict] = {}
    for sid in series_ids:
        if sid in stored_series_data:
            series_data[sid] = stored_series_data[sid]
            continue
        logger.info(
            "experiment_report: no stored series report for series=%s — "
            "recomputing from the live graph", sid,
        )
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

    # Verdict mix (M32) — read live from feedback_events rather than stored
    # series reports, since no pre-M32 series report captured verdict data.
    verdict_counts = await store.get_verdict_counts_by_series(experiment_id)
    verdict_mix = _aggregate_verdict_mix(series_ids, verdict_counts)

    # §5 aggregation
    series_summary = {
        "series_ids": series_ids,
        "series_count": len(series_ids),
        "total_run_count": sum(d["run_count"] for d in series_data.values()),
        "s5_1_failure_aggregation": _aggregate_failures(series_data),
        "verdict_mix": verdict_mix,
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
