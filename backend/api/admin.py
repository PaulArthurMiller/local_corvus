"""
Admin API router — read-only endpoints for the Admin UI.

GET  /admin/runs                           — paginated run log
GET  /admin/graph                          — all MUs and edges (for memory inspector)
GET  /admin/config                         — current hyperparameter registry
POST /admin/run-probe                      — run a probe through retrieval (read-only; no MU creation)
GET  /admin/probe-results                  — query probe results with optional filters
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

from analytics.probes import ProbeResult, compute_probe_result, load_probe_set, record_probe_result
from analytics.reports.experiment_report import generate_experiment_report, write_experiment_report
from analytics.reports.series_report import generate_series_report, write_series_report
from analytics.run_log import query_runs
from memory.retrieval import retrieve
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


class RunProbeRequest(BaseModel):
    probe_id: str
    measurement_session_index: int
    condition_label: str


@router.post("/run-probe")
async def run_probe(body: RunProbeRequest):
    """
    Run a probe through the retrieval engine and record the result.

    INVARIANT: This endpoint MUST NOT call run_interaction(). It only calls
    retrieve() — no MUs are created, no edges are written, no feedback events
    are generated. run_id in the ProbeResult is always None.
    """
    logger.info(
        "admin/run-probe: probe_id=%s session_index=%d condition=%s",
        body.probe_id,
        body.measurement_session_index,
        body.condition_label,
    )

    # Load probe definitions from YAML to find the requested probe
    try:
        probes = load_probe_set(version="v1")
    except Exception as exc:
        logger.error("admin/run-probe: could not load probe set — %s", exc)
        raise HTTPException(status_code=500, detail=f"Could not load probe set: {exc}")

    probe = next((p for p in probes if p.probe_id == body.probe_id), None)
    if probe is None:
        raise HTTPException(
            status_code=404,
            detail=f"Probe '{body.probe_id}' not found in probe set",
        )

    # Load config for retrieval hyperparameters
    try:
        with open(_CONFIG_PATH, "r", encoding="utf-8") as f:
            config = yaml.safe_load(f)
    except Exception as exc:
        logger.error("admin/run-probe: could not load config — %s", exc)
        raise HTTPException(status_code=500, detail=str(exc))

    # Run retrieval — read-only; no MU creation, no edges written
    try:
        retrieval_result = await retrieve(probe.prompt_text, graph_store, config)
    except Exception as exc:
        logger.error("admin/run-probe: retrieval failed — %s", exc)
        raise HTTPException(status_code=500, detail=f"Retrieval failed: {exc}")

    # Compute probe metrics
    probe_result = await compute_probe_result(
        probe=probe,
        retrieval_result=retrieval_result,
        run_id=None,
        measurement_session_index=body.measurement_session_index,
        condition_label=body.condition_label,
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
):
    """
    Return probe results with optional filters.

    Results ordered by measurement_session_index ASC.
    """
    logger.info(
        "admin/probe-results: probe_id=%s condition=%s session=%s",
        probe_id,
        condition_label,
        measurement_session_index,
    )
    try:
        results = await graph_store.query_probe_results(
            probe_id=probe_id,
            condition_label=condition_label,
            measurement_session_index=measurement_session_index,
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


@router.post("/reports/generate-experiment")
async def generate_experiment_report_endpoint(body: GenerateExperimentReportRequest):
    """
    Generate an Experiment Report for the given experiment_id.

    Covers §5 aggregation across all series, §6.3 data-integrity checks, and
    §6.4 statistical caveat. §6.1 (learning measures) and §6.2 (control shadow)
    are null placeholder keys until Milestones 15 and 16 are built.

    Writes JSON + Markdown to /app/memory/reports/, registers the paths in
    generated_reports, and returns the report_id plus file paths.
    """
    logger.info(
        "admin/reports/generate-experiment: experiment_id=%s", body.experiment_id
    )
    try:
        report = await generate_experiment_report(
            experiment_id=body.experiment_id,
            store=graph_store,
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

    json_path = Path(record["json_path"])
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

    md_path = Path(record["md_path"])
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
