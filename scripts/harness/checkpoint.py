"""
Per-turn checkpointing.

Progress is never silently lost: after every turn, the full checkpoint
state is rewritten to disk (atomic write via temp file + rename). On
startup, an incomplete checkpoint for the same run_id is surfaced so the
caller can decide whether to resume (skip already-completed turns) or
start fresh. Resume logic is intentionally minimal per the Phase 0 spec —
it is not full crash-resume automation.
"""

import json
import os
from pathlib import Path
from typing import Optional

from pydantic import BaseModel, Field

from harness.manifest import now_iso, run_dir_for

STATUS_OK = "ok"
STATUS_SOFT_CONTINUE = "error_soft_continue"


class TurnCheckpoint(BaseModel):
    condition: str
    series_index: int
    turn_index: int
    turn_type: str
    status: str  # "ok" | "error_soft_continue"
    timestamp: str = Field(default_factory=now_iso)
    detail: Optional[str] = None


class CheckpointState(BaseModel):
    run_id: str
    turns: list[TurnCheckpoint] = Field(default_factory=list)
    # None while running; "complete" | "aborted" once the run ends.
    final_status: Optional[str] = None


def checkpoint_path(run_id: str, base_dir: Path) -> Path:
    return run_dir_for(run_id, base_dir) / "checkpoint.json"


def load_checkpoint(run_id: str, base_dir: Path) -> Optional[CheckpointState]:
    path = checkpoint_path(run_id, base_dir)
    if not path.exists():
        return None
    data = json.loads(path.read_text(encoding="utf-8"))
    return CheckpointState(**data)


def save_checkpoint(state: CheckpointState, base_dir: Path) -> Path:
    """Atomic write: write to a temp file in the same directory, then rename."""
    path = checkpoint_path(state.run_id, base_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_suffix(".json.tmp")
    tmp_path.write_text(json.dumps(state.model_dump(mode="json"), indent=2), encoding="utf-8")
    os.replace(tmp_path, path)
    return path


def is_turn_done(
    state: CheckpointState, condition: str, series_index: int, turn_index: int
) -> bool:
    """A turn counts as done if it has any recorded checkpoint entry (ok or soft-continue)."""
    return any(
        t.condition == condition
        and t.series_index == series_index
        and t.turn_index == turn_index
        for t in state.turns
    )
