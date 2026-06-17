"""
Admin API router — read-only endpoints for the Admin UI.

GET /admin/runs    — paginated run log
GET /admin/graph   — all MUs and edges (for memory inspector)
GET /admin/config  — current hyperparameter registry

This is a thin HTTP layer; all data access goes through memory/store.py
via analytics/run_log.py.
"""

import logging
import os
from typing import Optional

import yaml
from fastapi import APIRouter, HTTPException, Query

from analytics.run_log import query_runs
from memory.store import store as graph_store

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/admin")

_CONFIG_PATH = os.path.join(os.path.dirname(__file__), "..", "..", "config.yaml")


@router.get("/runs")
async def get_runs(
    session_id: Optional[str] = Query(default=None, description="Filter by session ID"),
    limit: int = Query(default=100, ge=1, le=1000, description="Max rows to return"),
    offset: int = Query(default=0, ge=0, description="Rows to skip"),
):
    """
    Return paginated run records from the analytics table.

    Optionally filter by session_id. Results ordered by timestamp DESC.
    """
    logger.info(
        "admin/runs: query session_id=%s limit=%d offset=%d", session_id, limit, offset
    )
    try:
        runs = await query_runs(session_id=session_id, limit=limit, offset=offset)
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
