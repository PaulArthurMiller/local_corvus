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
    """
    Structured record of one feedback signal (or explicit absence thereof).

    verdict vocabulary is source-dependent by design: the chat-UI /feedback
    path stores "thumbs_up"/"thumbs_down"/"none" (unchanged since M8); the
    judge-driven /feedback/verdict path (M19 Phase 2) stores "up"/"down"/
    "none" directly rather than mapping onto the older strings — a grep of
    the report generators (series_report.py, experiment_report.py,
    control_shadow.py) found nothing matching on the literal "thumbs_*"
    strings, so there was no reason to force one vocabulary. judge_source
    is what distinguishes origin, not the verdict string itself.
    """

    feedback_event_id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    run_id: str
    verdict: str  # "thumbs_up" | "thumbs_down" | "up" | "down" | "none"
    verdict_basis: Optional[str] = None  # e.g. "checklist_pass", "confabulation"
    turn_index: Optional[int] = None
    is_probe_turn: bool = False
    # M19 Phase 2 — judge verdict metadata. All optional: pre-existing rows
    # (and the chat-UI /feedback path prior to this milestone) keep NULLs.
    condition_label: Optional[str] = None
    rationale: Optional[str] = None
    criterion_triggered: Optional[str] = None
    judge_source: Optional[str] = None  # "model:<name>" | "human"
    judge_model_name: Optional[str] = None
    judge_temperature: Optional[float] = None
    judge_pinned_snapshot: Optional[str] = None
    prompt_id: Optional[str] = None


async def record_feedback_event(event: FeedbackEvent) -> None:
    """Write a feedback event to the feedback_events table via the graph store."""
    await graph_store.insert_feedback_event(event.model_dump())
    logger.info(
        "feedback_events: recorded event_id=%s run_id=%s verdict=%s",
        event.feedback_event_id,
        event.run_id,
        event.verdict,
    )
