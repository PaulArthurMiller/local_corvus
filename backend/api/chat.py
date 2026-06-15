"""
Chat API router — POST /chat.

Thin HTTP layer. Business logic lives in the gate and store modules.
The only job here is:
  1. Parse the request
  2. Resolve or create the session
  3. Gate the input
  4. Call Ollama (orchestrator stub for Milestone 1)
  5. Gate the response
  6. Persist both MUs to SQLite + audit log
  7. Return the response

The orchestrator.py module (Milestone 2) will replace step 4-6 here
with a full pipeline call. For now this is an intentional stub that
proves the gate and store wiring is correct before the retrieval layer
is added.
"""

import logging
import os
from typing import Optional

import httpx
import yaml
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from core.session import create_session, get_session
from gate.audit import append_audit
from gate.sanitize import sanitize_in, sanitize_out
from gate.um import create_um
from memory.store import store

logger = logging.getLogger(__name__)

router = APIRouter()

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

_CONFIG_PATH = os.path.join(os.path.dirname(__file__), "..", "..", "config.yaml")


def _load_config() -> dict:
    try:
        with open(_CONFIG_PATH, "r", encoding="utf-8") as f:
            return yaml.safe_load(f)
    except Exception as exc:
        logger.warning("Could not load config.yaml: %s — using defaults", exc)
        return {}


_CONFIG = _load_config()
_MODEL_NAME: str = _CONFIG.get("model", {}).get("name", "gemma4:e4b")
_OLLAMA_TIMEOUT: int = _CONFIG.get("model", {}).get("timeout_seconds", 60)
_OLLAMA_HOST: str = os.getenv(
    "OLLAMA_HOST",
    _CONFIG.get("model", {}).get("ollama_host", "http://host.docker.internal:11434"),
)


# ---------------------------------------------------------------------------
# Request / Response schemas
# ---------------------------------------------------------------------------


class ChatRequest(BaseModel):
    message: str
    session_id: Optional[str] = None


class ChatResponse(BaseModel):
    response: str
    session_id: str
    input_mu_id: str
    output_mu_id: str
    flags: list[str]


# ---------------------------------------------------------------------------
# Route
# ---------------------------------------------------------------------------


@router.post("/chat", response_model=ChatResponse)
async def chat(request: ChatRequest) -> ChatResponse:
    """
    Process a chat message through the full UM gate pipeline.

    On each call:
    - Resolves or creates a session
    - sanitize_in on user message
    - Calls Ollama for a response (orchestrator stub)
    - sanitize_in on LLM response (inbound because it enters the agent)
    - Stores user_input MU + agent_output MU in SQLite
    - Appends both MUs to audit.log
    """

    # --- Session ---
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

    logger.info("chat: session_id=%s message_len=%d", session_id, len(request.message))

    # --- Gate: sanitize user input ---
    input_result = sanitize_in(request.message, boundary="user_input→agent")

    all_flags: list[str] = list(input_result.flags)

    # --- Create input UM ---
    input_um = create_um(
        content=input_result.content,
        source_type="user_input",
        boundary="user_input→agent",
        direction="in",
        session_id=session_id,
        flags=input_result.flags,
    )

    # --- Gate: sanitize_out on the prompt before sending to Ollama ---
    prompt_result = sanitize_out(input_result.content, boundary="agent→llm")
    all_flags.extend(prompt_result.flags)

    # --- Call Ollama ---
    ollama_payload = {
        "model": _MODEL_NAME,
        "messages": [{"role": "user", "content": prompt_result.content}],
        "stream": False,
    }
    try:
        async with httpx.AsyncClient() as client:
            ollama_response = await client.post(
                f"{_OLLAMA_HOST}/v1/chat/completions",
                json=ollama_payload,
                timeout=float(_OLLAMA_TIMEOUT),
            )
            ollama_response.raise_for_status()
        response_data = ollama_response.json()
        llm_content: str = response_data["choices"][0]["message"]["content"]
        logger.info(
            "chat: Ollama response received session_id=%s len=%d",
            session_id,
            len(llm_content),
        )
    except httpx.HTTPStatusError as exc:
        logger.error("chat: Ollama HTTP error %s — %s", exc.response.status_code, exc)
        raise HTTPException(
            status_code=502, detail=f"LLM service returned {exc.response.status_code}"
        )
    except httpx.RequestError as exc:
        logger.error("chat: Ollama connection error — %s", exc)
        raise HTTPException(
            status_code=503, detail="LLM service unreachable"
        )

    # --- Gate: sanitize_in on LLM response (entering the agent boundary) ---
    output_result = sanitize_in(llm_content, boundary="llm→agent")
    all_flags.extend(output_result.flags)

    # --- Create output UM ---
    output_um = create_um(
        content=output_result.content,
        source_type="agent_output",
        boundary="llm→agent",
        direction="in",
        session_id=session_id,
        flags=output_result.flags,
    )

    # --- Persist MUs to SQLite ---
    await store.create_mu(
        mu_id=input_um.mu_id,
        source_type=input_um.source_type,
        raw_content=input_um.raw_content,
        created_at=input_um.created_at,
    )
    await store.create_mu(
        mu_id=output_um.mu_id,
        source_type=output_um.source_type,
        raw_content=output_um.raw_content,
        created_at=output_um.created_at,
    )

    # --- Append to audit log ---
    append_audit(input_um)
    append_audit(output_um)

    # --- Gate: sanitize_out on final response leaving the agent ---
    final_result = sanitize_out(output_result.content, boundary="agent→user")
    all_flags.extend(final_result.flags)

    logger.info(
        "chat: complete session_id=%s input_mu=%s output_mu=%s flags=%d",
        session_id,
        input_um.mu_id,
        output_um.mu_id,
        len(all_flags),
    )

    return ChatResponse(
        response=final_result.content,
        session_id=session_id,
        input_mu_id=input_um.mu_id,
        output_mu_id=output_um.mu_id,
        flags=all_flags,
    )
