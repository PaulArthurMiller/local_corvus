"""
Analytics run log — capture and query per-interaction run records.

Delegates all database access to memory/store.py (the only file allowed to
touch SQLite). This module owns the RunRecord schema and the capture/query
interface.
"""

import logging
import uuid
from datetime import datetime, timezone
from typing import Any, Optional

from pydantic import BaseModel, Field

from memory.store import store as graph_store

logger = logging.getLogger(__name__)


class RunRecord(BaseModel):
    """Complete record of one agent interaction."""

    run_id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    session_id: str
    timestamp: str = Field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )
    input_mu_id: Optional[str] = None
    output_mu_id: Optional[str] = None
    tool_calls: list[str] = Field(default_factory=list)
    activated_mu_ids: list[str] = Field(default_factory=list)
    activated_edge_ids: list[str] = Field(default_factory=list)
    prompt_mu_id: Optional[str] = None
    tokens_in: Optional[int] = None
    tokens_out: Optional[int] = None
    latency_ms: Optional[int] = None
    feedback_signal: Optional[int] = None
    weight_updates: list[dict[str, Any]] = Field(default_factory=list)
    hyperparameters_snapshot: dict[str, Any] = Field(default_factory=dict)
    algorithm: Optional[str] = None

    # Retrieval decomposition — all values populated from RetrievalResult
    bm25_scores_per_mu: dict[str, Any] = Field(default_factory=dict)
    entry_point_mu_ids: list[str] = Field(default_factory=list)
    candidate_set_before_ranking: list[dict] = Field(default_factory=list)
    traversal_path: list[dict] = Field(default_factory=list)
    hop_depth_per_retrieved_mu: dict[str, Any] = Field(default_factory=dict)
    edge_contributions: dict[str, Any] = Field(default_factory=dict)

    # Full Ollama messages array with base64 image data replaced by placeholder strings
    assembled_prompt_json: list[dict] = Field(default_factory=list)

    # Config traceability
    config_hash: Optional[str] = None
    full_config_snapshot: dict[str, Any] = Field(default_factory=dict)
    condition_label: Optional[str] = None

    # Turn indexing
    turn_index: Optional[int] = None
    session_turn_index: Optional[int] = None

    # Probe / experiment session tracking
    measurement_session_index: Optional[int] = None
    is_probe_turn: bool = False

    # Session context flag — whether get_session_messages() history was injected
    use_session_context: bool = False

    # Failure detection — None for normal turns; set to a FAILURE_REASON_* constant when the
    # model returned empty content (token_limit_exhausted or empty_output)
    failure_reason: Optional[str] = None


async def capture_run(run: RunRecord) -> None:
    """
    Persist a run record to the analytics table.

    Called by the orchestrator after every interaction completes.
    """
    await graph_store.insert_run(run.model_dump())
    logger.info(
        "run_log: captured run_id=%s session_id=%s latency_ms=%s",
        run.run_id,
        run.session_id,
        run.latency_ms,
    )


async def query_runs(
    session_id: Optional[str] = None,
    limit: int = 100,
    offset: int = 0,
) -> list[dict]:
    """
    Retrieve run records for the Admin UI.

    Optionally filter by session_id. Returns rows ordered by timestamp DESC.
    """
    rows = await graph_store.query_runs(
        session_id=session_id,
        limit=limit,
        offset=offset,
    )
    logger.info(
        "run_log: query returned %d run(s) session_id=%s limit=%d offset=%d",
        len(rows),
        session_id,
        limit,
        offset,
    )
    return rows
