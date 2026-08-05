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
    # --interactive calibration only: the verdict the judge model returned,
    # recorded alongside `verdict` (which holds the FINAL value — the
    # operator's, if they overrode). Without both, an override is
    # unrecoverable after the fact: nothing else on disk retains what the
    # model said, so a calibration pass could report how often the operator
    # intervened but never the judge's error direction, which is the whole
    # measurement. None outside interactive mode, and on every pre-existing
    # checkpoint.
    judge_model_verdict: Optional[str] = None
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
    # Milestone 31 — the per-criterion checklist for this turn (same order as
    # the prompt's criteria), recorded whenever the judge produced a parseable
    # verdict. None when no judging happened, the judge was unparseable, or
    # the verdict came from an operator's manual grade in --interactive (no
    # per-criterion judgment exists for those). Since M37 this holds the
    # FINAL checklist — the operator's, if they regraded it in --interactive.
    criteria_met: Optional[list[bool]] = None
    # Milestone 37 — the judge model's own checklist, before any operator
    # regrade, and the exact analogue of judge_model_verdict one layer down.
    # This is what makes calibration per-criterion instead of per-verdict:
    # comparing it against criteria_met yields one agreement observation per
    # criterion (~2.4x the turns on the current bank) and, more importantly,
    # names which criterion the judge got wrong rather than only that the
    # turn's verdict moved. Equal to criteria_met on every unregraded turn.
    #
    # Recorded AFTER the host-side evidence checks, matching judge_model_verdict:
    # both describe what an unattended run would have stored, and those checks
    # run unattended too. The model's raw pre-demotion claim stays recoverable —
    # an EVIDENCE_DEMOTED_NO_QUOTE entry in evidence_flags marks each criterion
    # the model asserted and the host forced false.
    judge_model_criteria_met: Optional[list[bool]] = None
    # Milestone 37 — the judge's supporting quote per criterion, and the
    # host-side evidence-check outcomes over them (lists of the judge module's
    # EVIDENCE_* constants). None when the judge answered with a bare boolean
    # list; evidence_enforced records whether the quote rules were applied at
    # all, so bare-list turns are never pooled with quoted ones in analysis.
    criteria_evidence: Optional[list[str]] = None
    evidence_flags: Optional[list[list[str]]] = None
    evidence_enforced: Optional[bool] = None


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
