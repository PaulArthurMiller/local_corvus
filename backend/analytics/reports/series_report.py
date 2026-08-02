"""
Series Report Generator — §5 of the Local Corvus experiment report spec.

Reads from the DB (via GraphStore methods) and returns a structured dict
with all 8 section payloads. The caller writes JSON + Markdown to disk
and registers the paths in generated_reports via store.insert_generated_report().

All database access goes through memory.store — no direct SQLite here.
"""

import json
import logging
import os
import uuid
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

import yaml

from memory.store import GraphStore

logger = logging.getLogger(__name__)

_CONFIG_PATH = os.getenv(
    "CONFIG_PATH",
    os.path.join(os.path.dirname(__file__), "..", "..", "..", "config.yaml"),
)

_REPORTS_DIR = Path(os.getenv("MEMORY_PATH", "/app/data")) / "reports"


def _load_config() -> dict:
    with open(_CONFIG_PATH, "r", encoding="utf-8") as f:
        return yaml.safe_load(f)


# ---------------------------------------------------------------------------
# Section generators
# ---------------------------------------------------------------------------


async def _section_51_failure_summary(
    runs: list[dict],
    store: GraphStore,
    series_id: str,
) -> dict:
    """
    §5.1 — Failure summary.

    Lists all failed turns and checks whether any failed MU was subsequently
    retrieved (ghost-MU incident).
    """
    failed_turns = [
        {
            "run_id": r["run_id"],
            "turn_index": r.get("turn_index"),
            "failure_reason": r["failure_reason"],
        }
        for r in runs
        if r.get("failure_reason")
    ]

    # Ghost-MU check: failed MUs that appeared in a later run's activated_mu_ids
    failed_mus = await store.get_mus_by_source_type_pattern(
        "agent_output_failed%", series_id=series_id
    )
    failed_mu_ids = {mu["mu_id"] for mu in failed_mus}

    ghost_incidents: list[dict] = []
    for run in runs:
        activated = run.get("activated_mu_ids") or []
        if not isinstance(activated, list):
            continue
        for mu_id in activated:
            if mu_id in failed_mu_ids:
                ghost_incidents.append(
                    {
                        "ghost_mu_id": mu_id,
                        "appearing_in_run_id": run["run_id"],
                        "appearing_at_turn_index": run.get("turn_index"),
                    }
                )

    logger.info(
        "series_report §5.1: failed_turns=%d ghost_incidents=%d",
        len(failed_turns),
        len(ghost_incidents),
    )
    return {
        "failed_turn_count": len(failed_turns),
        "failed_turns": failed_turns,
        "ghost_mu_count": len(ghost_incidents),
        "ghost_incidents": ghost_incidents,
    }


def _section_52_retrieval_trace_digest(runs: list[dict]) -> dict:
    """
    §5.2 — Retrieval trace digest.

    Renders a per-turn summary of retrieval keywords, entry points,
    traversal hops, and final retrieved set size.
    """
    traces = []
    for run in runs:
        turn = run.get("turn_index")
        keywords = run.get("entry_point_mu_ids") or []  # keywords used
        bm25 = run.get("bm25_scores_per_mu") or {}
        entry_points = run.get("entry_point_mu_ids") or []
        traversal = run.get("traversal_path") or []
        activated = run.get("activated_mu_ids") or []
        retrieved_count = len(activated) if isinstance(activated, list) else 0

        # Truncate MU IDs to 8 chars for readability
        entry_short = [eid[:8] for eid in (entry_points if isinstance(entry_points, list) else [])]
        activated_short = [mid[:8] for mid in (activated if isinstance(activated, list) else [])]

        traces.append(
            {
                "turn_index": turn,
                "run_id": run["run_id"],
                "bm25_candidate_count": len(bm25) if isinstance(bm25, dict) else 0,
                "entry_point_mu_ids_short": entry_short,
                "traversal_hop_count": len(traversal) if isinstance(traversal, list) else 0,
                "retrieved_mu_count": retrieved_count,
                "retrieved_mu_ids_short": activated_short,
            }
        )

    logger.info("series_report §5.2: %d turns in trace digest", len(traces))
    return {"turn_count": len(traces), "traces": traces}


async def _section_53_retrieval_rate_classification(
    runs: list[dict],
    current_series_id: str,
    store: GraphStore,
) -> dict:
    """
    §5.3 — Intra-series vs. cross-series retrieval rate.

    For each activated MU, classifies as intra_series / seed_fixture /
    cross_series / unknown. Requires M12 (series_id on MUs).
    """
    all_activated_mu_ids: list[str] = []
    for run in runs:
        activated = run.get("activated_mu_ids") or []
        if isinstance(activated, list):
            all_activated_mu_ids.extend(activated)

    # Deduplicate for the lookup, but keep counts per occurrence
    unique_ids = list(set(all_activated_mu_ids))
    series_map = await store.get_mu_series_ids(unique_ids)

    # Per-turn breakdown
    per_turn: list[dict] = []
    total_intra = 0
    total_seed = 0
    total_cross = 0
    total_unknown = 0
    total_retrieved = 0

    for run in runs:
        activated = run.get("activated_mu_ids") or []
        if not isinstance(activated, list):
            activated = []

        turn_counts = {"intra_series": 0, "seed_fixture": 0, "cross_series": 0, "unknown": 0}
        for mu_id in activated:
            mu_series = series_map.get(mu_id)
            if mu_series is None:
                turn_counts["unknown"] += 1
            elif mu_series == current_series_id:
                turn_counts["intra_series"] += 1
            elif mu_series.startswith("seed:"):
                turn_counts["seed_fixture"] += 1
            else:
                turn_counts["cross_series"] += 1

        total = len(activated)
        per_turn.append(
            {
                "turn_index": run.get("turn_index"),
                "run_id": run["run_id"],
                "total_retrieved": total,
                **turn_counts,
            }
        )
        total_intra += turn_counts["intra_series"]
        total_seed += turn_counts["seed_fixture"]
        total_cross += turn_counts["cross_series"]
        total_unknown += turn_counts["unknown"]
        total_retrieved += total

    intra_rate = total_intra / total_retrieved if total_retrieved > 0 else 0.0
    seed_rate = total_seed / total_retrieved if total_retrieved > 0 else 0.0
    cross_rate = total_cross / total_retrieved if total_retrieved > 0 else 0.0

    logger.info(
        "series_report §5.3: total_retrieved=%d intra=%d seed=%d cross=%d unknown=%d "
        "intra_rate=%.3f seed_rate=%.3f cross_rate=%.3f",
        total_retrieved, total_intra, total_seed, total_cross, total_unknown,
        intra_rate, seed_rate, cross_rate,
    )
    return {
        "total_retrieved": total_retrieved,
        "intra_series_count": total_intra,
        "seed_fixture_count": total_seed,
        "cross_series_count": total_cross,
        "unknown_count": total_unknown,
        "intra_series_rate": round(intra_rate, 4),
        "seed_fixture_rate": round(seed_rate, 4),
        "cross_series_rate": round(cross_rate, 4),
        "per_turn": per_turn,
    }


async def _section_54_cross_series_edges(
    series_id: str, store: GraphStore
) -> dict:
    """
    §5.4 — Cross-series edges.

    Returns edges where at least one endpoint belongs to series_id and
    the other belongs to a different (or NULL) series.
    """
    edges = await store.get_cross_series_edges(series_id)
    logger.info("series_report §5.4: %d cross-series edges found", len(edges))
    return {"cross_series_edge_count": len(edges), "edges": edges}


async def _section_55_edge_weight_deltas(
    runs: list[dict], store: GraphStore
) -> dict:
    """
    §5.5 — Edge weight deltas (series-scoped).

    Computes start/end weight for each edge activated in this series.
    Flags zero-delta edges traversed > 5 times.
    """
    # Collect all edge IDs activated in this series
    activated_edge_ids: set[str] = set()
    for run in runs:
        eids = run.get("activated_edge_ids") or []
        if isinstance(eids, list):
            activated_edge_ids.update(eids)

    # Collect weight updates from each run, keyed by edge_id
    # {edge_id: [{turn_index, old_weight, new_weight}]}
    edge_updates: dict[str, list[dict]] = defaultdict(list)
    for run in runs:
        turn_idx = run.get("turn_index", 0)
        updates = run.get("weight_updates") or []
        if not isinstance(updates, list):
            continue
        for upd in updates:
            if isinstance(upd, dict) and "edge_id" in upd:
                edge_updates[upd["edge_id"]].append(
                    {
                        "turn_index": turn_idx,
                        "old_weight": upd.get("old_weight"),
                        "new_weight": upd.get("new_weight"),
                    }
                )

    # Get current weights for all activated edges
    edge_id_list = list(activated_edge_ids)
    current_edges = await store.get_edges_by_ids(edge_id_list)
    current_weight_map = {e["edge_id"]: e["weight"] for e in current_edges}
    traversal_count_map = {e["edge_id"]: e.get("traversal_count", 0) for e in current_edges}

    deltas: list[dict] = []
    zero_delta_high_traversal: list[dict] = []

    for edge_id in activated_edge_ids:
        current_weight = current_weight_map.get(edge_id)
        if current_weight is None:
            # Edge deleted between activation and report generation
            continue

        updates_for_edge = sorted(edge_updates.get(edge_id, []), key=lambda u: u["turn_index"])
        if updates_for_edge:
            start_weight = updates_for_edge[0]["old_weight"]
            end_weight = updates_for_edge[-1]["new_weight"]
        else:
            # Activated but never updated in this series — no delta
            start_weight = current_weight
            end_weight = current_weight

        delta = (end_weight or 0.0) - (start_weight or 0.0)
        traversal_count = traversal_count_map.get(edge_id, 0)

        entry: dict = {
            "edge_id": edge_id,
            "start_weight": start_weight,
            "end_weight": end_weight,
            "delta": round(delta, 6),
            "traversal_count_total": traversal_count,
            "update_count_in_series": len(updates_for_edge),
        }
        deltas.append(entry)

        if delta == 0.0 and traversal_count > 5:
            zero_delta_high_traversal.append(
                {
                    "edge_id": edge_id,
                    "traversal_count": traversal_count,
                    "weight": current_weight,
                }
            )

    # Sort by abs(delta) descending
    deltas.sort(key=lambda d: abs(d["delta"]), reverse=True)

    logger.info(
        "series_report §5.5: %d activated edges, %d with deltas, %d zero-delta high-traversal",
        len(activated_edge_ids),
        sum(1 for d in deltas if d["delta"] != 0.0),
        len(zero_delta_high_traversal),
    )
    return {
        "activated_edge_count": len(activated_edge_ids),
        "deltas": deltas,
        "zero_delta_high_traversal_flags": zero_delta_high_traversal,
    }


async def _section_56_probe_integrity(
    series_id: str, store: GraphStore
) -> dict:
    """
    §5.6 — Probe integrity check.

    HARD FAIL if any feedback_events row has is_probe_turn=1 for a run
    in this series. Any violation is a data-integrity flag.
    """
    count, events = await store.count_probe_feedback_events_for_series(series_id)
    status = "PASS" if count == 0 else "HARD_FAIL"
    logger.info(
        "series_report §5.6: probe_integrity=%s violation_count=%d", status, count
    )
    return {
        "status": status,
        "violation_count": count,
        "violations": events,
    }


async def _section_57_mode3_trigger_count(
    series_id: str, store: GraphStore, config: dict
) -> dict:
    """
    §5.7 — Mode-3 trigger count.

    Counts system MUs (Mode-3 summarization outputs) for the series.
    Flags any that are suspiciously short per reporting.mode3_min_expected_length.
    """
    min_len = config.get("reporting", {}).get("mode3_min_expected_length", 100)
    system_mus = await store.get_mus_by_source_type_pattern("system", series_id=series_id)

    flagged: list[dict] = []
    for mu in system_mus:
        content = mu.get("raw_content") or ""
        if len(content) < min_len:
            flagged.append(
                {
                    "mu_id": mu["mu_id"],
                    "created_at": mu.get("created_at"),
                    "content_length": len(content),
                    "min_expected_length": min_len,
                }
            )

    logger.info(
        "series_report §5.7: mode3_count=%d flagged_anomalous=%d",
        len(system_mus),
        len(flagged),
    )
    return {
        "mode3_mu_count": len(system_mus),
        "min_expected_length": min_len,
        "anomaly_count": len(flagged),
        "anomalies": flagged,
    }


def _section_58_num_ctx_pressure(runs: list[dict], config: dict) -> dict:
    """
    §5.8 — num_ctx pressure.

    Flags turns where tokens_in > 0.8 * context_window_tokens. For flagged
    turns, notes whether retrieved_count is above the series mean.
    """
    context_window = config.get("model", {}).get("context_window_tokens", 8192)
    threshold = 0.8 * context_window

    # Compute series mean retrieved count
    retrieved_counts = []
    for run in runs:
        activated = run.get("activated_mu_ids") or []
        if isinstance(activated, list):
            retrieved_counts.append(len(activated))
    mean_retrieved = sum(retrieved_counts) / len(retrieved_counts) if retrieved_counts else 0.0

    pressure_turns: list[dict] = []
    for run in runs:
        tokens_in = run.get("tokens_in") or 0
        if tokens_in > threshold:
            activated = run.get("activated_mu_ids") or []
            retrieved_count = len(activated) if isinstance(activated, list) else 0
            above_mean = retrieved_count > mean_retrieved

            pressure_turns.append(
                {
                    "turn_index": run.get("turn_index"),
                    "run_id": run["run_id"],
                    "tokens_in": tokens_in,
                    "context_window_tokens": context_window,
                    "pressure_ratio": round(tokens_in / context_window, 4),
                    "retrieved_count": retrieved_count,
                    "series_mean_retrieved": round(mean_retrieved, 2),
                    "attributed_to_retrieval_volume": above_mean,
                }
            )

    logger.info(
        "series_report §5.8: pressure_turns=%d / %d total",
        len(pressure_turns), len(runs),
    )
    return {
        "context_window_tokens": context_window,
        "pressure_threshold_ratio": 0.8,
        "series_mean_retrieved_count": round(mean_retrieved, 2),
        "pressure_turn_count": len(pressure_turns),
        "pressure_turns": pressure_turns,
    }


# ---------------------------------------------------------------------------
# Main generator
# ---------------------------------------------------------------------------


async def generate_series_report(
    experiment_id: str,
    series_id: str,
    store: GraphStore,
) -> dict:
    """
    Generate a complete §5 Series Report for the given series_id.

    Returns the report as a dict (caller writes JSON + Markdown to disk).
    """
    config = _load_config()
    logger.info(
        "series_report: starting generation experiment_id=%s series_id=%s",
        experiment_id, series_id,
    )

    runs = await store.get_all_runs_for_series(series_id)
    logger.info("series_report: loaded %d runs", len(runs))

    report = {
        "report_id": str(uuid.uuid4()),
        "report_type": "series",
        "experiment_id": experiment_id,
        "series_id": series_id,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "run_count": len(runs),
        "sections": {
            "s5_1_failure_summary": await _section_51_failure_summary(runs, store, series_id),
            "s5_2_retrieval_trace_digest": _section_52_retrieval_trace_digest(runs),
            "s5_3_retrieval_rate_classification": await _section_53_retrieval_rate_classification(
                runs, series_id, store
            ),
            "s5_4_cross_series_edges": await _section_54_cross_series_edges(series_id, store),
            "s5_5_edge_weight_deltas": await _section_55_edge_weight_deltas(runs, store),
            "s5_6_probe_integrity": await _section_56_probe_integrity(series_id, store),
            "s5_7_mode3_trigger_count": await _section_57_mode3_trigger_count(
                series_id, store, config
            ),
            "s5_8_num_ctx_pressure": _section_58_num_ctx_pressure(runs, config),
        },
    }

    logger.info("series_report: generation complete report_id=%s", report["report_id"])
    return report


async def write_series_report(
    report: dict,
) -> tuple[str, str]:
    """
    Write the report dict to JSON + Markdown files in /app/memory/reports/.

    Returns (json_path, md_path) as strings.
    """
    from analytics.reports.markdown_renderer import render_series_report_markdown

    _REPORTS_DIR.mkdir(parents=True, exist_ok=True)

    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    exp_id = report["experiment_id"].replace("/", "-")
    ser_id = report["series_id"].replace("/", "-")
    base_name = f"series_report_{exp_id}_{ser_id}_{ts}"

    json_path = _REPORTS_DIR / f"{base_name}.json"
    md_path = _REPORTS_DIR / f"{base_name}.md"

    json_path.write_text(json.dumps(report, indent=2, default=str), encoding="utf-8")
    md_path.write_text(render_series_report_markdown(report), encoding="utf-8")

    logger.info("series_report: wrote %s and %s", json_path, md_path)
    return str(json_path), str(md_path)
