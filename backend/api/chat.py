"""
Chat API router — POST /chat.

Thin HTTP layer. All pipeline logic lives in core/orchestrator.py.
This file's job:
  1. Parse the request
  2. Resolve or create the session
  3. Delegate to run_interaction()
  4. Return the response
"""

import logging
from typing import Optional

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from core.orchestrator import run_interaction
from core.session import create_session, get_session

logger = logging.getLogger(__name__)

router = APIRouter()


# ---------------------------------------------------------------------------
# Request / Response schemas
# ---------------------------------------------------------------------------


class ChatRequest(BaseModel):
    message: str
    session_id: Optional[str] = None


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
        "chat: session_id=%s message_len=%d", session_id, len(request.message)
    )

    # --- Delegate to orchestrator ---
    try:
        result = await run_interaction(request.message, session_id)
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
    )
