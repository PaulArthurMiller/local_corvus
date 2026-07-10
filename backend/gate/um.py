"""
Universal Message (UM) schema and factory.

Every piece of data that crosses a trust boundary in Corvus is wrapped in a
UniversalMessage before any other processing happens. This file owns the schema
and the factory; no other module should construct a UM directly.
"""

import uuid
from datetime import datetime, timezone
from typing import Literal, Optional
from pydantic import BaseModel, Field

SourceType = Literal[
    "user_input",
    "agent_output",
    "agent_output_failed_token_limit",
    "agent_output_failed_empty_output",
    "tool_call",
    "tool_result",
    "prompt",
    "system",
]

Direction = Literal["in", "out"]


class UniversalMessage(BaseModel):
    mu_id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    created_at: str = Field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )
    source_type: SourceType
    raw_content: str
    boundary: str
    direction: Direction
    series_id: str
    experiment_id: Optional[str] = None
    channel_id: Optional[str] = None
    # Free-string flags raised by sanitize_in / sanitize_out
    flags: list[str] = Field(default_factory=list)


def create_um(
    content: str,
    source_type: SourceType,
    boundary: str,
    direction: Direction,
    series_id: str,
    experiment_id: Optional[str] = None,
    channel_id: Optional[str] = None,
    flags: Optional[list[str]] = None,
) -> UniversalMessage:
    """Create a UniversalMessage with generated mu_id and timestamp."""
    return UniversalMessage(
        source_type=source_type,
        raw_content=content,
        boundary=boundary,
        direction=direction,
        series_id=series_id,
        experiment_id=experiment_id,
        channel_id=channel_id,
        flags=flags or [],
    )
