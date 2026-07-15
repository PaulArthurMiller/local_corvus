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
    # M19 Phase 2 — judge verdict outcome for this turn. `detail` above
    # always describes the chat-call result (run_id, or the chat error);
    # these three describe the independent judge/verdict-posting outcome,
    # so a verdict-posting hiccup never overwrites the chat call's audit info.
    verdict: Optional[str] = None
    feedback_event_id: Optional[str] = None
    judge_error: Optional[str] = None
    # M21 — probe turns only: which probe ran and which measurement session
    # it belonged to. `detail` holds the probe result_id (the probe-turn
    # analogue of a chat turn's run_id).
    probe_id: Optional[str] = None
    measurement_session_index: Optional[int] = None
    # M30 — dual-mode probe battery. `detail` above is repurposed to hold the
    # deterministic weight_mode="mean" pass's result_id for a probe turn
    # (unchanged meaning — still "the probe result_id"); these two fields
    # additionally record the K weight_mode="sample" repeats' result_ids and
    # how many were actually completed, so a soft-continued partial battery
    # (e.g. a connectivity blip mid-sweep) is visible in the checkpoint
    # rather than silently looking identical to a full K-repeat pass.
    probe_sample_result_ids: list[str] = Field(default_factory=list)
    probe_sample_repeats_completed: int = 0


class SeriesReportRecord(BaseModel):
    """M19 Phase 3 — outcome of one generate-series report call, recorded
    when the last turn of a (condition, series_index) completes."""

    condition: str
    series_index: int
    experiment_id: str
    series_id: str
    status: str  # "ok" | "error_soft_continue"
    timestamp: str = Field(default_factory=now_iso)
    report_id: Optional[str] = None
    json_path: Optional[str] = None
    md_path: Optional[str] = None
    error: Optional[str] = None


class ExperimentReportRecord(BaseModel):
    """M19 Phase 3 — outcome of the single generate-experiment report call
    made at experiment close (only when the learned condition ran)."""

    experiment_id: str
    status: str  # "ok" | "error_soft_continue"
    timestamp: str = Field(default_factory=now_iso)
    report_id: Optional[str] = None
    json_path: Optional[str] = None
    md_path: Optional[str] = None
    error: Optional[str] = None


class CheckpointState(BaseModel):
    run_id: str
    turns: list[TurnCheckpoint] = Field(default_factory=list)
    series_reports: list[SeriesReportRecord] = Field(default_factory=list)
    experiment_report: Optional[ExperimentReportRecord] = None
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
