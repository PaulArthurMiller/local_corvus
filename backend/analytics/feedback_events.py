"""
Feedback event log — one record per /feedback call.

Records the structured verdict for every feedback signal, including turns
where the user provided no feedback (verdict="none"). The feedback_events
table is separate from the analytics table so that absence of feedback is
an explicit observation rather than a null field.
"""

import logging
import uuid
from typing import Optional

from pydantic import BaseModel, Field

from memory.store import store as graph_store

logger = logging.getLogger(__name__)


class FeedbackEvent(BaseModel):
    """Structured record of one feedback signal (or explicit absence thereof)."""

    feedback_event_id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    run_id: str
    verdict: str  # "thumbs_up" | "thumbs_down" | "none"
    verdict_basis: Optional[str] = None  # e.g. "checklist_pass", "confabulation"
    turn_index: Optional[int] = None
    is_probe_turn: bool = False


async def record_feedback_event(event: FeedbackEvent) -> None:
    """Write a feedback event to the feedback_events table via the graph store."""
    await graph_store.insert_feedback_event(event.model_dump())
    logger.info(
        "feedback_events: recorded event_id=%s run_id=%s verdict=%s",
        event.feedback_event_id,
        event.run_id,
        event.verdict,
    )
