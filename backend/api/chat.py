"""
Chat API router — POST /chat and POST /feedback.

Thin HTTP layer. All pipeline logic lives in core/orchestrator.py and
learning/simple_rl.py.
"""

import logging
import os
from typing import Optional

import yaml
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from analytics.feedback_events import FeedbackEvent, record_feedback_event
from core.orchestrator import AttachmentInput, run_interaction
from core.session import create_session, get_session
from learning.simple_rl import SimpleRL
from memory.store import store as graph_store

logger = logging.getLogger(__name__)

router = APIRouter()

_CONFIG_PATH = os.getenv(
    "CONFIG_PATH",
    os.path.join(os.path.dirname(__file__), "..", "..", "config.yaml"),
)


def _load_weight_update_rate() -> float:
    try:
        with open(_CONFIG_PATH, "r", encoding="utf-8") as f:
            cfg = yaml.safe_load(f)
        return float(cfg.get("hyperparameters", {}).get("weight_update_rate", 0.1))
    except Exception:
        return 0.1


# ---------------------------------------------------------------------------
# Request / Response schemas
# ---------------------------------------------------------------------------


class AttachmentRequest(BaseModel):
    filename: str
    mime_type: str
    data_b64: str


class ChatRequest(BaseModel):
    message: str
    session_id: Optional[str] = None
    attachments: Optional[list[AttachmentRequest]] = None


class ChatResponse(BaseModel):
    response: str
    session_id: str
    run_id: str
    input_mu_id: str
    output_mu_id: str
    prompt_mu_id: str
    activated_mu_ids: list[str]
    activated_edge_ids: list[str]
    tool_call_mu_ids: list[str]
    flags: list[str]
    tokens_in: Optional[int] = None
    tokens_out: Optional[int] = None
    failure_reason: Optional[str] = None


# ---------------------------------------------------------------------------
# Route
# ---------------------------------------------------------------------------


@router.post("/chat", response_model=ChatResponse)
async def chat(request: ChatRequest) -> ChatResponse:
    """
    Process a chat message through the full agent pipeline.

    Session is resolved or created here; all other logic is in
    core/orchestrator.py.
    """
    # --- Session resolution ---
    if request.session_id:
        session = get_session(request.session_id)
        if session is None:
            logger.warning(
                "chat: unknown session_id=%s — creating new session",
                request.session_id,
            )
            session_id = create_session()
        else:
            session_id = session.session_id
    else:
        session_id = create_session()

    logger.info(
        "chat: session_id=%s message_len=%d attachments=%d",
        session_id,
        len(request.message),
        len(request.attachments) if request.attachments else 0,
    )

    # Convert API attachment models to orchestrator inputs
    attachment_inputs = [
        AttachmentInput(
            filename=a.filename,
            mime_type=a.mime_type,
            data_b64=a.data_b64,
        )
        for a in (request.attachments or [])
    ]

    # --- Delegate to orchestrator ---
    try:
        result = await run_interaction(
            request.message,
            session_id,
            attachments=attachment_inputs or None,
        )
    except Exception as exc:
        logger.error("chat: orchestrator error — %s", exc)
        raise HTTPException(status_code=502, detail=str(exc))

    return ChatResponse(
        response=result.response,
        session_id=result.session_id,
        run_id=result.run_id,
        input_mu_id=result.input_mu_id,
        output_mu_id=result.output_mu_id,
        prompt_mu_id=result.prompt_mu_id,
        activated_mu_ids=result.activated_mu_ids,
        activated_edge_ids=result.activated_edge_ids,
        tool_call_mu_ids=result.tool_call_mu_ids,
        flags=result.flags,
        tokens_in=result.tokens_in,
        tokens_out=result.tokens_out,
        failure_reason=result.failure_reason,
    )


# ---------------------------------------------------------------------------
# Feedback
# ---------------------------------------------------------------------------


class FeedbackRequest(BaseModel):
    run_id: str
    signal: int  # +1 or -1
    verdict_basis: Optional[str] = None  # e.g. "checklist_pass", "confabulation"
    is_probe_turn: bool = False


class FeedbackResponse(BaseModel):
    run_id: str
    signal: int
    edges_updated: int
    weight_updates: list[dict]


@router.post("/feedback", response_model=FeedbackResponse)
async def feedback(request: FeedbackRequest) -> FeedbackResponse:
    """
    Apply a feedback signal to the edges activated during a prior run.

    Loads the run record, fetches activated edges, runs SimpleRL.update(),
    writes new weights to the graph store, and appends weight_updates to
    the run record.
    """
    if request.signal not in (1, -1):
        raise HTTPException(
            status_code=422,
            detail="signal must be +1 or -1",
        )

    logger.info(
        "feedback: run_id=%s signal=%+d", request.run_id, request.signal
    )

    # Load the run record to get activated_edge_ids
    run = await graph_store.get_run(request.run_id)
    if run is None:
        raise HTTPException(
            status_code=404,
            detail=f"run_id {request.run_id!r} not found",
        )

    activated_edge_ids: list[str] = run.get("activated_edge_ids") or []
    activated_mu_ids: list[str] = run.get("activated_mu_ids") or []

    logger.info(
        "feedback: run_id=%s has %d activated edge(s) and %d activated MU(s)",
        request.run_id,
        len(activated_edge_ids),
        len(activated_mu_ids),
    )

    # Fetch full edge records for all activated edges
    activated_edges = await _fetch_edges_by_ids(activated_edge_ids)

    # Fetch full MU records for all activated MUs (SimpleRL ignores them,
    # but the interface requires them for future algorithm compatibility)
    activated_mus: list[dict] = []
    for mu_id in activated_mu_ids:
        mu = await graph_store.get_mu(mu_id)
        if mu is not None:
            activated_mus.append(mu)

    # Run the learning algorithm
    weight_update_rate = _load_weight_update_rate()
    algorithm = SimpleRL(weight_update_rate=weight_update_rate)
    updates = algorithm.update(
        activated_edges=activated_edges,
        activated_mus=activated_mus,
        feedback=request.signal,
    )

    # Build a feedback event before writing weights (need its ID for weight_updates)
    verdict = "thumbs_up" if request.signal == 1 else "thumbs_down"
    feedback_event = FeedbackEvent(
        run_id=request.run_id,
        verdict=verdict,
        verdict_basis=request.verdict_basis,
        turn_index=run.get("turn_index"),
        is_probe_turn=request.is_probe_turn,
    )

    # Build an edge lookup for source/target enrichment
    edge_by_id = {e["edge_id"]: e for e in activated_edges}

    # Write new weights to graph store and build enriched weight_updates record
    weight_updates: list[dict] = []
    for edge_id, new_weight in updates:
        edge = edge_by_id.get(edge_id, {})
        old_weight = edge.get("weight")
        await graph_store.update_edge_weight(edge_id, new_weight)
        weight_updates.append({
            "edge_id": edge_id,
            "old_weight": old_weight,
            "new_weight": new_weight,
            "source_mu_id": edge.get("source_mu_id"),
            "target_mu_id": edge.get("target_mu_id"),
            "feedback_event_id": feedback_event.feedback_event_id,
        })

    # Persist feedback event
    await record_feedback_event(feedback_event)

    # Persist feedback signal + weight updates back to the run record
    await graph_store.update_run_feedback(
        run_id=request.run_id,
        feedback_signal=request.signal,
        weight_updates=weight_updates,
    )

    logger.info(
        "feedback: complete run_id=%s signal=%+d edges_updated=%d event_id=%s",
        request.run_id,
        request.signal,
        len(weight_updates),
        feedback_event.feedback_event_id,
    )

    return FeedbackResponse(
        run_id=request.run_id,
        signal=request.signal,
        edges_updated=len(weight_updates),
        weight_updates=weight_updates,
    )


async def _fetch_edges_by_ids(edge_ids: list[str]) -> list[dict]:
    """
    Fetch edge records by their IDs.

    GraphStore doesn't have a get_edge_by_id method; we use get_all_edges()
    and filter. This is efficient enough for local research volume.
    """
    if not edge_ids:
        return []
    all_edges = await graph_store.get_all_edges()
    id_set = set(edge_ids)
    return [e for e in all_edges if e["edge_id"] in id_set]
