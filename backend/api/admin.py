"""
Admin API router — read-only endpoints for the Admin UI.

GET  /admin/runs                           — paginated run log
GET  /admin/graph                          — all MUs and edges (for memory inspector)
GET  /admin/graph/stats                   — MU count summary with cluster_id coverage
GET  /admin/config                         — current hyperparameter registry (re-reads config.yaml from disk)
GET  /admin/effective-config               — the orchestrator's actually-resolved config (M43)
POST /admin/graph/wipe                    — archive the current graph, then delete all MUs/edges
POST /admin/seed-graph                    — register a seed fixture and load MUs into the graph
POST /admin/run-probe                      — run a probe through retrieval (read-only; no MU creation)
GET  /admin/probe-results                  — query probe results with optional filters
POST /admin/probes/snapshot               — take a named battery snapshot of the current graph state
POST /admin/reports/generate-series       — generate §5 series report
POST /admin/reports/generate-experiment   — generate §5 aggregation + §6.3 + §6.4 experiment report
GET  /admin/reports                       — list all generated reports
GET  /admin/reports/{report_id}           — return report JSON
GET  /admin/reports/{report_id}/markdown  — return report Markdown

This is a thin HTTP layer; all data access goes through memory/store.py
via analytics/run_log.py and analytics/probes.py.
"""

import json
import logging
import os
from pathlib import Path
from typing import Optional

import yaml
from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import PlainTextResponse
from pydantic import BaseModel

from analytics.probes import (
    VALID_BATTERY_MODES,
    ProbeResult,
    compute_probe_result,
    load_probe_set,
    record_probe_result,
)
from analytics.reports.experiment_report import generate_experiment_report, write_experiment_report
from analytics.reports.series_report import generate_series_report, resolve_report_path, write_series_report
from analytics.run_log import query_runs
from analytics.seed_fixture import register_fixture
from core.orchestrator import _CONFIG, _CONFIG_HASH, _CONFIG_OVERRIDES, EXPERIMENT_ID, RUN_CONDITION, USE_SESSION_CONTEXT
from learning.edge_update import VALID_WEIGHT_MODES
from memory.retrieval import retrieve
from memory.snapshot import take_archive_snapshot, take_battery_snapshot
from memory.store import store as graph_store

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/admin")

_CONFIG_PATH = os.getenv(
    "CONFIG_PATH",
    os.path.join(os.path.dirname(__file__), "..", "..", "config.yaml"),
)


@router.get("/runs")
async def get_runs(
    series_id: Optional[str] = Query(default=None, description="Filter by series ID"),
    limit: int = Query(default=100, ge=1, le=1000, description="Max rows to return"),
    offset: int = Query(default=0, ge=0, description="Rows to skip"),
):
    """
    Return paginated run records from the analytics table.

    Optionally filter by series_id. Results ordered by timestamp DESC.
    """
    logger.info(
        "admin/runs: query series_id=%s limit=%d offset=%d", series_id, limit, offset
    )
    try:
        runs = await query_runs(series_id=series_id, limit=limit, offset=offset)
    except Exception as exc:
        logger.error("admin/runs: query failed — %s", exc)
        raise HTTPException(status_code=500, detail=str(exc))
    return {"runs": runs, "count": len(runs), "limit": limit, "offset": offset}


@router.get("/graph")
async def get_graph():
    """
    Return all memory units and edges for the memory inspector.

    Returns the full graph; callers should apply depth limits in the UI
    when MU counts are large.
    """
    logger.info("admin/graph: fetching full graph")
    try:
        mus = await graph_store.get_all_mus()
        edges = await graph_store.get_all_edges()
    except Exception as exc:
        logger.error("admin/graph: failed — %s", exc)
        raise HTTPException(status_code=500, detail=str(exc))
    return {
        "mu_count": len(mus),
        "edge_count": len(edges),
        "memory_units": mus,
        "edges": edges,
    }


@router.get("/graph/stats")
async def get_graph_stats():
    """
    Return a summary of graph coverage: total MU count broken down by
    cluster_id presence. Used in Phase 2 to verify seed MUs have cluster_id.
    """
    logger.info("admin/graph/stats: computing MU stats")
    try:
        mus = await graph_store.get_all_mus()
    except Exception as exc:
        logger.error("admin/graph/stats: failed — %s", exc)
        raise HTTPException(status_code=500, detail=str(exc))

    total = len(mus)
    with_cluster = sum(1 for m in mus if m.get("cluster_id"))
    without_cluster = total - with_cluster
    cluster_distribution: dict[str, int] = {}
    for m in mus:
        cid = m.get("cluster_id") or "NULL"
        cluster_distribution[cid] = cluster_distribution.get(cid, 0) + 1

    logger.info(
        "admin/graph/stats: total=%d with_cluster_id=%d without_cluster_id=%d",
        total, with_cluster, without_cluster,
    )
    return {
        "mu_count": total,
        "mu_with_cluster_id": with_cluster,
        "mu_without_cluster_id": without_cluster,
        "cluster_distribution": cluster_distribution,
    }


class WipeGraphRequest(BaseModel):
    experiment_id: Optional[str] = None


@router.post("/graph/wipe")
async def wipe_graph(body: WipeGraphRequest):
    """
    Archive the current graph state, then delete all memory_units and edges.

    Used by the experiment harness (M19) to reset the graph between
    conditions. The pre-wipe graph is always archived first (keyed by
    experiment_id) so nothing is silently lost — analytics, probe_results,
    feedback_events, and seed_fixture_manifest are untouched; only the
    live MU/edge graph resets.
    """
    logger.info("admin/graph/wipe: archiving before wipe experiment_id=%s", body.experiment_id)
    try:
        archive_path = await take_archive_snapshot(body.experiment_id)
    except Exception as exc:
        logger.error("admin/graph/wipe: archive failed, wipe aborted — %s", exc)
        raise HTTPException(status_code=500, detail=f"Archive failed, wipe aborted: {exc}")

    try:
        result = await graph_store.wipe_graph()
    except Exception as exc:
        logger.error("admin/graph/wipe: wipe failed — %s", exc)
        raise HTTPException(status_code=500, detail=str(exc))

    logger.info(
        "admin/graph/wipe: complete mus_deleted=%d edges_deleted=%d archive=%s",
        result["mus_deleted"], result["edges_deleted"], archive_path,
    )
    return {
        "mus_deleted": result["mus_deleted"],
        "edges_deleted": result["edges_deleted"],
        "archive_path": archive_path,
    }


class SeedGraphRequest(BaseModel):
    version: str = "v1"


@router.post("/seed-graph")
async def seed_graph(body: SeedGraphRequest):
    """
    Register the seed fixture and load its MUs into the graph store.

    The fixture file must exist at seed_fixtures/seed_{version}.json.gz
    (mounted at /app/seed_fixtures inside Docker). Re-registration is
    idempotent — existing MUs are silently skipped.
    """
    logger.info("admin/seed-graph: registering fixture version=%s", body.version)

    _seed_fixtures_dir = Path(os.getenv("SEED_FIXTURES_PATH", "/app/seed_fixtures"))
    fixture_path = _seed_fixtures_dir / f"seed_{body.version}.json.gz"

    try:
        manifest = await register_fixture(fixture_path=fixture_path, version=body.version)
    except FileNotFoundError as exc:
        logger.error("admin/seed-graph: fixture not found — %s", exc)
        raise HTTPException(status_code=404, detail=str(exc))
    except Exception as exc:
        logger.error("admin/seed-graph: registration failed — %s", exc)
        raise HTTPException(status_code=500, detail=str(exc))

    logger.info(
        "admin/seed-graph: done version=%s mu_count=%d hash=%s",
        manifest.fixture_version, manifest.mu_count, manifest.manifest_hash[:16],
    )
    return manifest.model_dump()


class ProbeSnapshotRequest(BaseModel):
    battery_index: int = 0
    phase: str = "pre"
    condition_label: str = "seed"


@router.post("/probes/snapshot")
async def create_probe_snapshot(body: ProbeSnapshotRequest):
    """
    Take a named battery snapshot of the current graph state.

    Used before and after each probe measurement battery to bound the
    graph state. phase should be 'pre' or 'post'. battery_index is the
    sequential measurement session number (0-based).
    """
    logger.info(
        "admin/probes/snapshot: battery_index=%d phase=%s condition=%s",
        body.battery_index, body.phase, body.condition_label,
    )
    try:
        filepath = await take_battery_snapshot(
            battery_index=body.battery_index,
            phase=body.phase,
            condition_label=body.condition_label,
        )
    except Exception as exc:
        logger.error("admin/probes/snapshot: failed — %s", exc)
        raise HTTPException(status_code=500, detail=str(exc))

    logger.info("admin/probes/snapshot: wrote %s", filepath)
    return {
        "snapshot_path": filepath,
        "battery_index": body.battery_index,
        "phase": body.phase,
        "condition_label": body.condition_label,
    }


@router.get("/config")
async def get_config():
    """Return the current hyperparameter registry from config.yaml."""
    try:
        with open(_CONFIG_PATH, "r", encoding="utf-8") as f:
            cfg = yaml.safe_load(f)
    except Exception as exc:
        logger.error("admin/config: could not read config.yaml — %s", exc)
        raise HTTPException(status_code=500, detail=str(exc))
    return cfg


@router.get("/effective-config")
async def get_effective_config():
    """
    Return what the orchestrator actually resolved at process startup —
    config.yaml merged with CONFIG_OVERRIDES_JSON (M43 Phase 2), the exact
    values every /chat and /admin/run-probe call is running on right now.

    Deliberately NOT a re-read of config.yaml (that's GET /admin/config,
    unchanged): re-reading the file would certify the exact bug this
    endpoint exists to catch — a config.yaml edited on disk but not yet
    picked up by a backend restart. Importing _CONFIG/_CONFIG_HASH from
    core.orchestrator instead of re-parsing the file is the whole point
    (see BUILD-PLAN.md Milestone 43's "the trap that shapes Phase 1").

    `overridden_keys` (M43 Phase 2) lists which dotted keys in `config`
    came from CONFIG_OVERRIDES_JSON rather than the on-disk file — this is
    what lets a plan know which knobs it owns vs. inherits.
    """
    return {
        "config": _CONFIG,
        "runtime_config_hash": _CONFIG_HASH,
        "resolved_env": {
            "EXPERIMENT_ID": EXPERIMENT_ID,
            "RUN_CONDITION": RUN_CONDITION,
            "CONDITION_LABEL": os.getenv("CONDITION_LABEL") or None,
            "USE_SESSION_CONTEXT": USE_SESSION_CONTEXT,
        },
        "overridden_keys": sorted(_CONFIG_OVERRIDES.keys()),
    }


class RunProbeRequest(BaseModel):
    probe_id: str
    measurement_session_index: int
    condition_label: str
    probe_set_version: str = "v1"   # default "v1" preserves backward compat
    # M30 — dual-mode probe battery. series_id lets the result be scoped to
    # one series by series_report.py (probe turns create no analytics row,
    # so this is the only way to join a probe result back to a series).
    # weight_mode overrides config.yaml's committed edge_update.weight_mode
    # for this call only — None (default) preserves the pre-M30 behavior of
    # reading whatever is currently committed. battery_mode/
    # battery_repeat_index are recorded alongside the result for reporting;
    # they do not themselves affect retrieval (weight_mode does).
    series_id: Optional[str] = None
    weight_mode: Optional[str] = None
    battery_mode: str = "mean"
    battery_repeat_index: Optional[int] = None


@router.post("/run-probe")
async def run_probe(body: RunProbeRequest):
    """
    Run a probe through the retrieval engine and record the result.

    INVARIANT: This endpoint MUST NOT call run_interaction(). It only calls
    retrieve() — no MUs are created, no edges are written, no feedback events
    are generated. run_id in the ProbeResult is always None. This holds
    identically regardless of weight_mode override (M30) — compute_effective_
    weight() is a pure read in both "mean" and "sample" modes; no counter
    (alpha/beta) is ever written by this endpoint or by retrieve() itself.
    """
    logger.info(
        "admin/run-probe: probe_id=%s session_index=%d condition=%s weight_mode=%s "
        "battery_mode=%s battery_repeat_index=%s",
        body.probe_id,
        body.measurement_session_index,
        body.condition_label,
        body.weight_mode,
        body.battery_mode,
        body.battery_repeat_index,
    )

    if body.weight_mode is not None and body.weight_mode not in VALID_WEIGHT_MODES:
        raise HTTPException(
            status_code=422,
            detail=f"weight_mode must be one of {VALID_WEIGHT_MODES} or omitted, got {body.weight_mode!r}",
        )
    if body.battery_mode not in VALID_BATTERY_MODES:
        raise HTTPException(
            status_code=422,
            detail=f"battery_mode must be one of {VALID_BATTERY_MODES}, got {body.battery_mode!r}",
        )

    # Load probe definitions from YAML to find the requested probe
    try:
        probes = load_probe_set(version=body.probe_set_version)
    except Exception as exc:
        logger.error("admin/run-probe: could not load probe set — %s", exc)
        raise HTTPException(status_code=500, detail=f"Could not load probe set: {exc}")

    probe = next((p for p in probes if p.probe_id == body.probe_id), None)
    if probe is None:
        raise HTTPException(
            status_code=404,
            detail=f"Probe '{body.probe_id}' not found in probe set version '{body.probe_set_version}'",
        )

    # Load config for retrieval hyperparameters
    try:
        with open(_CONFIG_PATH, "r", encoding="utf-8") as f:
            config = yaml.safe_load(f)
    except Exception as exc:
        logger.error("admin/run-probe: could not load config — %s", exc)
        raise HTTPException(status_code=500, detail=str(exc))

    # Run retrieval — read-only; no MU creation, no edges written. rng is
    # deliberately NOT the orchestrator's process-scoped EDGE_RNG (real turns'
    # RNG) — M30 finding: probe measurement must never consume draws from the
    # same stream real training turns use for weight_mode: sample, or a probe
    # checkpoint would silently perturb the reproducibility of subsequent
    # training turns' sample draws even though it writes no counters. Passing
    # no rng here lets retrieve() fall back to its own unseeded module-level
    # RNG (see retrieval.py's _DEFAULT_RNG docstring, which already documented
    # this as the intended behavior for read-only probe callers — this was
    # previously not followed). weight_mode_override lets one checkpoint run
    # the deterministic "mean" pass and K independent "sample" repeats
    # regardless of what edge_update.weight_mode is currently committed for
    # training turns.
    try:
        retrieval_result = await retrieve(
            probe.prompt_text, graph_store, config,
            condition=RUN_CONDITION, weight_mode_override=body.weight_mode,
        )
    except Exception as exc:
        logger.error("admin/run-probe: retrieval failed — %s", exc)
        raise HTTPException(status_code=500, detail=f"Retrieval failed: {exc}")

    # Compute probe metrics. experiment_id is stamped server-side from the
    # process-level EXPERIMENT_ID (the harness restarts the backend with the
    # right one per condition) — mirrors how analytics rows are stamped, and
    # keeps probe measurements from different attempts/dry-runs separable
    # even though they share a condition_label.
    probe_result = await compute_probe_result(
        probe=probe,
        retrieval_result=retrieval_result,
        run_id=None,
        measurement_session_index=body.measurement_session_index,
        condition_label=body.condition_label,
        experiment_id=EXPERIMENT_ID,
        series_id=body.series_id,
        battery_mode=body.battery_mode,
        battery_repeat_index=body.battery_repeat_index,
    )

    # Persist the result
    try:
        await record_probe_result(probe_result)
    except Exception as exc:
        logger.error("admin/run-probe: could not record result — %s", exc)
        raise HTTPException(status_code=500, detail=f"Could not record probe result: {exc}")

    logger.info(
        "admin/run-probe: complete probe_id=%s rank=%s precision=%s recall=%s",
        probe_result.probe_id,
        probe_result.gold_mu_rank,
        probe_result.retrieval_precision,
        probe_result.retrieval_recall,
    )
    return probe_result.model_dump()


@router.get("/probe-results")
async def get_probe_results(
    probe_id: Optional[str] = Query(default=None, description="Filter by probe ID"),
    condition_label: Optional[str] = Query(default=None, description="Filter by condition label"),
    measurement_session_index: Optional[int] = Query(default=None, description="Filter by session index"),
    series_id: Optional[str] = Query(default=None, description="Filter by series ID"),
    battery_mode: Optional[str] = Query(default=None, description="Filter by battery mode (mean|sample)"),
):
    """
    Return probe results with optional filters.

    Results ordered by measurement_session_index ASC.
    """
    logger.info(
        "admin/probe-results: probe_id=%s condition=%s session=%s series_id=%s battery_mode=%s",
        probe_id,
        condition_label,
        measurement_session_index,
        series_id,
        battery_mode,
    )
    try:
        results = await graph_store.query_probe_results(
            probe_id=probe_id,
            condition_label=condition_label,
            measurement_session_index=measurement_session_index,
            series_id=series_id,
            battery_mode=battery_mode,
        )
    except Exception as exc:
        logger.error("admin/probe-results: query failed — %s", exc)
        raise HTTPException(status_code=500, detail=str(exc))
    return {"probe_results": results, "count": len(results)}


# ---------------------------------------------------------------------------
# M13: Report endpoints
# ---------------------------------------------------------------------------


class GenerateSeriesReportRequest(BaseModel):
    experiment_id: str
    series_id: str


@router.post("/reports/generate-series")
async def generate_series_report_endpoint(body: GenerateSeriesReportRequest):
    """
    Generate a §5 Series Report for the given experiment_id + series_id.

    Writes JSON + Markdown to /app/memory/reports/, registers the paths
    in generated_reports, and returns the report_id plus file paths.
    """
    logger.info(
        "admin/reports/generate-series: experiment_id=%s series_id=%s",
        body.experiment_id,
        body.series_id,
    )
    try:
        report = await generate_series_report(
            experiment_id=body.experiment_id,
            series_id=body.series_id,
            store=graph_store,
        )
        json_path, md_path = await write_series_report(report)
    except Exception as exc:
        logger.error("admin/reports/generate-series: generation failed — %s", exc)
        raise HTTPException(status_code=500, detail=str(exc))

    report_record = {
        "report_id": report["report_id"],
        "report_type": "series",
        "experiment_id": body.experiment_id,
        "series_id": body.series_id,
        "generated_at": report["generated_at"],
        "json_path": json_path,
        "md_path": md_path,
    }
    try:
        await graph_store.insert_generated_report(report_record)
    except Exception as exc:
        logger.error("admin/reports/generate-series: could not register report — %s", exc)
        raise HTTPException(status_code=500, detail=f"Report generated but not registered: {exc}")

    logger.info(
        "admin/reports/generate-series: complete report_id=%s json=%s md=%s",
        report["report_id"], json_path, md_path,
    )
    return {
        "report_id": report["report_id"],
        "json_path": json_path,
        "md_path": md_path,
        "run_count": report.get("run_count", 0),
    }


class GenerateExperimentReportRequest(BaseModel):
    experiment_id: str
    control_experiment_ids: Optional[dict] = None
    # M39 — RunPlan.trained_spokes ({"cluster_a": "a_spoke3", ...}) from the
    # plan this experiment was run under. Not persisted anywhere reachable
    # from the DB (see spoke_membership.py's module docstring), so the
    # caller supplies it explicitly. Omit for a report with no trained-probe
    # / cluster-winner data (degrades to "—" in the rendered Markdown).
    trained_spokes: Optional[dict] = None
    seed_fixture_version: str = "v3"


@router.post("/reports/generate-experiment")
async def generate_experiment_report_endpoint(body: GenerateExperimentReportRequest):
    """
    Generate an Experiment Report for the given experiment_id.

    Covers §5 aggregation across all series, §6.3 data-integrity checks, §6.4
    statistical caveat, §6.1 learning measures, and (when control_experiment_ids
    is provided) §6.2 control shadow.

    control_experiment_ids: optional dict mapping control condition name to
    experiment_id, e.g. {"static_weight": "exp-static-2026-07-10",
    "random_weight": "exp-random-2026-07-10"}.

    Writes JSON + Markdown to /app/memory/reports/, registers the paths in
    generated_reports, and returns the report_id plus file paths.
    """
    logger.info(
        "admin/reports/generate-experiment: experiment_id=%s control_ids=%s",
        body.experiment_id,
        list(body.control_experiment_ids.keys()) if body.control_experiment_ids else None,
    )
    try:
        report = await generate_experiment_report(
            experiment_id=body.experiment_id,
            store=graph_store,
            control_experiment_ids=body.control_experiment_ids or None,
            trained_spokes=body.trained_spokes or None,
            seed_fixture_version=body.seed_fixture_version,
        )
        json_path, md_path = await write_experiment_report(report)
    except Exception as exc:
        logger.error("admin/reports/generate-experiment: generation failed — %s", exc)
        raise HTTPException(status_code=500, detail=str(exc))

    report_record = {
        "report_id": report["report_id"],
        "report_type": "experiment",
        "experiment_id": body.experiment_id,
        "series_id": None,
        "generated_at": report["generated_at"],
        "json_path": json_path,
        "md_path": md_path,
    }
    try:
        await graph_store.insert_generated_report(report_record)
    except Exception as exc:
        logger.error(
            "admin/reports/generate-experiment: could not register report — %s", exc
        )
        raise HTTPException(
            status_code=500, detail=f"Report generated but not registered: {exc}"
        )

    logger.info(
        "admin/reports/generate-experiment: complete report_id=%s json=%s md=%s",
        report["report_id"], json_path, md_path,
    )
    return {
        "report_id": report["report_id"],
        "json_path": json_path,
        "md_path": md_path,
        "series_count": report.get("series_summary", {}).get("series_count", 0),
    }


@router.post("/edges/randomize")
async def randomize_edge_weights():
    """
    Set all edge weights to uniform random values in [0.0, 1.0].

    Called before each series in the random-weight control condition.
    Returns the count of edges updated.
    """
    logger.info("admin/edges/randomize: randomizing all edge weights")
    try:
        count = await graph_store.randomize_all_edge_weights()
    except Exception as exc:
        logger.error("admin/edges/randomize: failed — %s", exc)
        raise HTTPException(status_code=500, detail=str(exc))
    logger.info("admin/edges/randomize: updated %d edges", count)
    return {"edges_randomized": count}


@router.get("/reports")
async def list_reports(
    report_type: Optional[str] = Query(default=None, description="Filter by type: 'series' or 'experiment'"),
    experiment_id: Optional[str] = Query(default=None, description="Filter by experiment ID"),
):
    """Return all generated reports, optionally filtered."""
    logger.info(
        "admin/reports: list report_type=%s experiment_id=%s", report_type, experiment_id
    )
    try:
        reports = await graph_store.query_generated_reports(
            report_type=report_type,
            experiment_id=experiment_id,
        )
    except Exception as exc:
        logger.error("admin/reports: list failed — %s", exc)
        raise HTTPException(status_code=500, detail=str(exc))
    return {"reports": reports, "count": len(reports)}


@router.get("/reports/{report_id}")
async def get_report_json(report_id: str):
    """Return the full report JSON for the given report_id."""
    logger.info("admin/reports/%s: fetch JSON", report_id)
    try:
        record = await graph_store.get_generated_report(report_id)
    except Exception as exc:
        logger.error("admin/reports/%s: lookup failed — %s", report_id, exc)
        raise HTTPException(status_code=500, detail=str(exc))

    if record is None:
        raise HTTPException(status_code=404, detail=f"Report '{report_id}' not found")

    json_path = resolve_report_path(record["json_path"])
    if not json_path.exists():
        raise HTTPException(
            status_code=404,
            detail=f"Report file not found on disk: {json_path}",
        )
    try:
        content = json.loads(json_path.read_text(encoding="utf-8"))
    except Exception as exc:
        logger.error("admin/reports/%s: could not read JSON — %s", report_id, exc)
        raise HTTPException(status_code=500, detail=str(exc))
    return content


@router.get("/reports/{report_id}/markdown", response_class=PlainTextResponse)
async def get_report_markdown(report_id: str):
    """Return the Markdown file for the given report_id as plain text."""
    logger.info("admin/reports/%s/markdown: fetch", report_id)
    try:
        record = await graph_store.get_generated_report(report_id)
    except Exception as exc:
        logger.error("admin/reports/%s/markdown: lookup failed — %s", report_id, exc)
        raise HTTPException(status_code=500, detail=str(exc))

    if record is None:
        raise HTTPException(status_code=404, detail=f"Report '{report_id}' not found")

    md_path = resolve_report_path(record["md_path"])
    if not md_path.exists():
        raise HTTPException(
            status_code=404,
            detail=f"Markdown file not found on disk: {md_path}",
        )
    try:
        content = md_path.read_text(encoding="utf-8")
    except Exception as exc:
        logger.error("admin/reports/%s/markdown: could not read file — %s", report_id, exc)
        raise HTTPException(status_code=500, detail=str(exc))
    return content
