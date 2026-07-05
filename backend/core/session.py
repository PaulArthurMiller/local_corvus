"""
Session management — lightweight in-memory session registry.

Sessions are keyed by UUID. Each session accumulates a message history
(the clean conversation without memory-injection context) that is sent
to Ollama on every turn to maintain multi-turn coherence.

In-memory storage is intentional for v1 — sessions are ephemeral by
design. If the server restarts, the active session ends and the user
starts a new one. Memory units written to SQLite during the session persist.
"""

import logging
import uuid
from datetime import datetime, timezone
from typing import Optional

from pydantic import BaseModel, Field

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Session schema
# ---------------------------------------------------------------------------


class Session(BaseModel):
    session_id: str
    started_at: str
    # Accumulated chat history in OpenAI message format.
    # Each entry is {"role": "user" | "assistant", "content": "..."}.
    # Sent to Ollama on every turn so the model sees the full conversation.
    messages: list[dict] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# In-memory registry
# ---------------------------------------------------------------------------

_sessions: dict[str, Session] = {}


def create_session() -> str:
    """Create a new session, register it, and return its ID."""
    sid = str(uuid.uuid4())
    _sessions[sid] = Session(
        session_id=sid,
        started_at=datetime.now(timezone.utc).isoformat(),
    )
    logger.info("session: created session_id=%s", sid)
    return sid


def get_session(session_id: str) -> Optional[Session]:
    """Look up an existing session by ID. Returns None if not found."""
    session = _sessions.get(session_id)
    if session is None:
        logger.debug("session: session_id=%s not found", session_id)
    return session


def get_or_create_session(requested_id: Optional[str] = None) -> str:
    """
    Return `requested_id` if it's already a registered session; otherwise
    create a new session and return its ID.

    When `requested_id` is given but not yet registered, the new session
    is created *using that exact ID* rather than a fresh UUID — this is
    what lets a caller (e.g. the M19 experiment harness's deterministic
    `{plan_id}_{condition}_s{series_index}`) supply its own series
    identifier and have every subsequent call with that same ID land in
    the same session, instead of silently minting a new random UUID on
    every call because the ID was never seen before.

    When `requested_id` is None, behaves exactly like create_session().
    """
    if requested_id and requested_id in _sessions:
        return requested_id
    sid = requested_id or str(uuid.uuid4())
    _sessions[sid] = Session(
        session_id=sid,
        started_at=datetime.now(timezone.utc).isoformat(),
    )
    logger.info("session: created session_id=%s (caller_supplied=%s)", sid, bool(requested_id))
    return sid


def get_session_messages(session_id: str) -> list[dict]:
    """
    Return a copy of the message history for the session.

    Returns an empty list if the session does not exist.
    """
    session = _sessions.get(session_id)
    if session is None:
        return []
    return list(session.messages)


def append_session_message(session_id: str, message: dict) -> bool:
    """
    Append one message to the session's history.

    message must be {"role": "user"|"assistant", "content": str}.
    Returns False if the session does not exist.
    """
    session = _sessions.get(session_id)
    if session is None:
        logger.warning(
            "session: append_session_message called for unknown session_id=%s",
            session_id,
        )
        return False
    session.messages.append(message)
    logger.debug(
        "session: appended role=%s session_id=%s history_len=%d",
        message.get("role"),
        session_id,
        len(session.messages),
    )
    return True
