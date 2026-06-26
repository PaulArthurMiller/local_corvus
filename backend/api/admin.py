"""
Admin API router — read-only endpoints for the Admin UI.

GET  /admin/runs          — paginated run log
GET  /admin/graph         — all MUs and edges (for memory inspector)
GET  /admin/config        — current hyperparameter registry
POST /admin/run-probe     — run a probe through retrieval (read-only; no MU creation)
GET  /admin/probe-results — query probe results with optional filters

This is a thin HTTP layer; all data access goes through memory/store.py
via analytics/run_log.py and analytics/probes.py.
"""

import logging
import os
from typing import Optional

import yaml
from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel

from analytics.probes import ProbeResult, compute_probe_result, load_probe_set, record_probe_result
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
