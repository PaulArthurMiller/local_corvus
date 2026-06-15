"""
Session management — lightweight in-memory session registry.

Sessions are keyed by UUID. For Milestone 1 the only session state is
an ID and a start timestamp; the orchestrator adds interaction tracking
in Milestone 2.

In-memory storage is intentional for v1 — sessions are ephemeral by
design. If the server restarts, the active session ends and the user
starts a new one. Memory units written to SQLite during the session persist.
"""

import logging
import uuid
from datetime import datetime, timezone
from typing import Optional
from pydantic import BaseModel

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Session schema
# ---------------------------------------------------------------------------


class Session(BaseModel):
    session_id: str
    started_at: str


# ---------------------------------------------------------------------------
# In-memory registry
# ---------------------------------------------------------------------------

_sessions: dict[str, Session] = {}


def create_session() -> str:
    """
    Create a new session, register it, and return its ID.
    """
    sid = str(uuid.uuid4())
    session = Session(
        session_id=sid,
        started_at=datetime.now(timezone.utc).isoformat(),
    )
    _sessions[sid] = session
    logger.info("session: created session_id=%s", sid)
    return sid


def get_session(session_id: str) -> Optional[Session]:
    """
    Look up an existing session by ID. Returns None if not found.
    """
    session = _sessions.get(session_id)
    if session is None:
        logger.debug("session: session_id=%s not found", session_id)
    return session
