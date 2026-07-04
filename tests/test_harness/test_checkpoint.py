"""Tests for scripts/harness/checkpoint.py — per-turn checkpoint persistence."""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "scripts"))

from harness.checkpoint import (
    CheckpointState,
    TurnCheckpoint,
    is_turn_done,
    load_checkpoint,
    save_checkpoint,
)


def test_save_and_load_checkpoint_round_trips(tmp_path):
    state = CheckpointState(run_id="run-1")
    state.turns.append(TurnCheckpoint(
        condition="learned", series_index=0, turn_index=0, turn_type="bulk", status="ok",
    ))
    save_checkpoint(state, tmp_path)

    loaded = load_checkpoint("run-1", tmp_path)
    assert loaded is not None
    assert len(loaded.turns) == 1
    assert loaded.turns[0].condition == "learned"
    assert loaded.turns[0].status == "ok"


def test_load_checkpoint_returns_none_when_missing(tmp_path):
    assert load_checkpoint("nonexistent_run", tmp_path) is None


def test_is_turn_done_true_for_recorded_turn():
    state = CheckpointState(run_id="run-1")
    state.turns.append(TurnCheckpoint(
        condition="learned", series_index=0, turn_index=2, turn_type="probe", status="ok",
    ))
    assert is_turn_done(state, "learned", 0, 2) is True


def test_is_turn_done_false_for_unrecorded_turn():
    state = CheckpointState(run_id="run-1")
    assert is_turn_done(state, "learned", 0, 0) is False


def test_checkpoint_reflects_last_completed_turn_after_simulated_interrupt(tmp_path):
    """
    Simulates a crash mid-run: write turns 0 and 1, then "interrupt" (stop
    writing). Reloading from disk must show exactly those two turns — no
    silent loss, no phantom later turns.
    """
    state = CheckpointState(run_id="run-1")

    state.turns.append(TurnCheckpoint(
        condition="learned", series_index=0, turn_index=0, turn_type="bulk", status="ok",
    ))
    save_checkpoint(state, tmp_path)

    state.turns.append(TurnCheckpoint(
        condition="learned", series_index=0, turn_index=1, turn_type="bulk", status="ok",
    ))
    save_checkpoint(state, tmp_path)

    # Simulated interrupt: no further save_checkpoint call happens.

    reloaded = load_checkpoint("run-1", tmp_path)
    assert reloaded is not None
    assert reloaded.final_status is None  # run was never marked complete/aborted
    assert len(reloaded.turns) == 2
    assert [t.turn_index for t in reloaded.turns] == [0, 1]
    assert is_turn_done(reloaded, "learned", 0, 0)
    assert is_turn_done(reloaded, "learned", 0, 1)
    assert not is_turn_done(reloaded, "learned", 0, 2)


def test_soft_continue_status_is_preserved_across_reload(tmp_path):
    state = CheckpointState(run_id="run-1")
    state.turns.append(TurnCheckpoint(
        condition="learned", series_index=0, turn_index=0, turn_type="bulk",
        status="error_soft_continue", detail="turn error: malformed response",
    ))
    save_checkpoint(state, tmp_path)

    reloaded = load_checkpoint("run-1", tmp_path)
    assert reloaded.turns[0].status == "error_soft_continue"
    assert "malformed response" in reloaded.turns[0].detail
