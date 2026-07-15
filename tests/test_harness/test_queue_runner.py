"""
Tests for scripts/harness/queue_runner.py — the M27 execution loop:
on_plan_failure continue/halt semantics, the post-failure health recheck,
crash-resume reconciliation, duration capture, and queue_summary.md.

execute_run() itself is faked throughout (a FakeExecuteRun records calls and
returns a scripted RunOutcome) — this module's own behavior (per M26) is
already covered by test_queue.py and test_runner.py.
"""

import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "scripts"))

from harness.checkpoint import CheckpointState, TurnCheckpoint, save_checkpoint
from harness.client import BackendConnectivityError
from harness.manifest import compute_config_hash, compute_run_id
from harness.prompt_bank import PromptBank, save_bank
from harness.queue import (
    MANIFEST_STATUS_RESOLVED,
    STATUS_COMPLETE,
    STATUS_FAILED,
    STATUS_PENDING,
    STATUS_RUNNING,
    STATUS_SKIPPED,
    QueueCheckpointState,
    QueueManifest,
    QueuePlanCheckpointEntry,
    QueuePlanManifestEntry,
)
from harness.queue_runner import compute_per_condition_durations, run_queue
from harness.runner import RunOutcome
from harness.schema import RunPlan


# ---------------------------------------------------------------------------
# Test helpers
# ---------------------------------------------------------------------------

class FakeBackendClient:
    def __init__(self, health_errors=None):
        # health_errors: list of exceptions to raise on successive health()
        # calls; once exhausted, health() succeeds.
        self.health_errors = list(health_errors or [])
        self.health_calls = 0

    def health(self):
        self.health_calls += 1
        if self.health_errors:
            raise self.health_errors.pop(0)
        return {"status": "ok"}


class FakeExecuteRun:
    """Records every call and returns/raises according to a per-plan_id
    script. `outcomes` maps plan_id -> RunOutcome | Exception instance."""

    def __init__(self, outcomes: dict):
        self.outcomes = outcomes
        self.calls = []

    def __call__(self, plan, client, run_base_dir, **kwargs):
        self.calls.append(plan.plan_id)
        result = self.outcomes[plan.plan_id]
        if isinstance(result, Exception):
            raise result
        return result


def _dummy_bank_path(tmp_path, name="bank") -> Path:
    bank = PromptBank(entries=[], generation_params={}, content_hash="a" * 64)
    banks_dir = tmp_path / f"{name}_dir"
    banks_dir.mkdir(exist_ok=True)
    return save_bank(bank, banks_dir)


def _write_plan(path: Path, plan_id: str, bank_path: Path, **overrides) -> RunPlan:
    kwargs = dict(
        plan_id=plan_id,
        plan_name=f"Test plan {plan_id}",
        conceptual_fields={"count": 4, "subjects": "auto"},
        series_count=1,
        turns_per_series=2,
        probe_ratio=0.0,
        conditions=["learned"],
        random_seed=1,
        prompt_bank_path=str(bank_path),
    )
    kwargs.update(overrides)
    path.write_text(json.dumps(kwargs), encoding="utf-8")
    return RunPlan(**kwargs)


def _turn(condition, idx, ts) -> TurnCheckpoint:
    return TurnCheckpoint(
        condition=condition, series_index=0, turn_index=idx, turn_type="bulk",
        status="ok", detail=f"run-{idx}", timestamp=ts,
    )


def _manifest_and_checkpoint(plan_paths: list) -> tuple:
    manifest = QueueManifest(
        queue_id="q1", queue_file_path="q.json", queue_content_hash="irrelevant",
        plans=[
            QueuePlanManifestEntry(plan_path=p, enabled=True, status=MANIFEST_STATUS_RESOLVED)
            for p in plan_paths
        ],
    )
    checkpoint = QueueCheckpointState(
        queue_id="q1",
        plans=[QueuePlanCheckpointEntry(plan_path=p, status=STATUS_PENDING) for p in plan_paths],
    )
    return manifest, checkpoint


def _outcome(plan_id: str, status: str, turns: list, error_summary=None) -> RunOutcome:
    ckpt = CheckpointState(run_id=f"{plan_id}_run", turns=turns, final_status=status)
    return RunOutcome(
        run_id=f"{plan_id}_run", status=status, error_summary=error_summary,
        checkpoint=ckpt, manifest_path=Path("unused_manifest.json"),
        notification_log_path=Path("unused_notify.log"),
        turns_processed=len(turns),
    )


def _noop_print(_msg):
    pass


# ---------------------------------------------------------------------------
# on_plan_failure semantics
# ---------------------------------------------------------------------------

def test_on_plan_failure_continue_runs_next_plan_after_failure(tmp_path):
    bank = _dummy_bank_path(tmp_path)
    _write_plan(tmp_path / "a.json", "planA", bank)
    _write_plan(tmp_path / "b.json", "planB", bank)
    manifest, checkpoint = _manifest_and_checkpoint(["a.json", "b.json"])

    fake_execute = FakeExecuteRun({
        "planA": _outcome("planA", "aborted", [], error_summary="turn error: boom"),
        "planB": _outcome("planB", "complete", [_turn("learned", 0, "2026-01-01T00:00:00+00:00")]),
    })
    client = FakeBackendClient()

    result = run_queue(
        manifest, checkpoint, tmp_path / "q.json", "continue",
        client, None, tmp_path / "harness_runs", tmp_path / "launch", tmp_path / "probes",
        execute_run_fn=fake_execute, print_fn=_noop_print,
    )

    assert fake_execute.calls == ["planA", "planB"]
    statuses = {e.plan_path: e.status for e in result.plans}
    assert statuses == {"a.json": STATUS_FAILED, "b.json": STATUS_COMPLETE}
    assert result.final_status == "complete"  # continue mode never halts on its own


def test_on_plan_failure_halt_skips_remaining_plans(tmp_path):
    bank = _dummy_bank_path(tmp_path)
    _write_plan(tmp_path / "a.json", "planA", bank)
    _write_plan(tmp_path / "b.json", "planB", bank)
    manifest, checkpoint = _manifest_and_checkpoint(["a.json", "b.json"])

    fake_execute = FakeExecuteRun({
        "planA": _outcome("planA", "aborted", [], error_summary="turn error: boom"),
        "planB": _outcome("planB", "complete", []),
    })
    client = FakeBackendClient()

    result = run_queue(
        manifest, checkpoint, tmp_path / "q.json", "halt",
        client, None, tmp_path / "harness_runs", tmp_path / "launch", tmp_path / "probes",
        execute_run_fn=fake_execute, print_fn=_noop_print,
    )

    assert fake_execute.calls == ["planA"]  # planB never invoked
    statuses = {e.plan_path: e.status for e in result.plans}
    assert statuses == {"a.json": STATUS_FAILED, "b.json": STATUS_SKIPPED}
    assert result.final_status == "aborted"


def test_health_recheck_failure_overrides_continue(tmp_path):
    bank = _dummy_bank_path(tmp_path)
    _write_plan(tmp_path / "a.json", "planA", bank)
    _write_plan(tmp_path / "b.json", "planB", bank)
    manifest, checkpoint = _manifest_and_checkpoint(["a.json", "b.json"])

    fake_execute = FakeExecuteRun({
        "planA": _outcome("planA", "aborted", [], error_summary="connectivity: dropped"),
        "planB": _outcome("planB", "complete", []),
    })
    client = FakeBackendClient(health_errors=[BackendConnectivityError("still down")])

    result = run_queue(
        manifest, checkpoint, tmp_path / "q.json", "continue",
        client, None, tmp_path / "harness_runs", tmp_path / "launch", tmp_path / "probes",
        execute_run_fn=fake_execute, print_fn=_noop_print,
    )

    assert fake_execute.calls == ["planA"]  # planB never invoked despite "continue"
    statuses = {e.plan_path: e.status for e in result.plans}
    assert statuses == {"a.json": STATUS_FAILED, "b.json": STATUS_SKIPPED}
    assert result.final_status == "aborted"


# ---------------------------------------------------------------------------
# Crash-resume reconciliation
# ---------------------------------------------------------------------------

def test_already_complete_plan_is_skipped_without_calling_execute_run(tmp_path):
    bank = _dummy_bank_path(tmp_path)
    _write_plan(tmp_path / "a.json", "planA", bank)
    manifest, checkpoint = _manifest_and_checkpoint(["a.json"])
    checkpoint.plans[0].status = STATUS_COMPLETE

    fake_execute = FakeExecuteRun({})  # would KeyError if ever called
    client = FakeBackendClient()

    result = run_queue(
        manifest, checkpoint, tmp_path / "q.json", "continue",
        client, None, tmp_path / "harness_runs", tmp_path / "launch", tmp_path / "probes",
        execute_run_fn=fake_execute, print_fn=_noop_print,
    )

    assert fake_execute.calls == []
    assert result.plans[0].status == STATUS_COMPLETE


def test_in_flight_plan_with_incomplete_harness_checkpoint_is_re_entered(tmp_path):
    """Simulates a kill mid-plan: the queue checkpoint says 'running' and a
    real harness checkpoint exists but never reached final_status. Must
    call execute_run() again (which itself resumes the harness run)."""
    bank = _dummy_bank_path(tmp_path)
    plan_path = tmp_path / "a.json"
    plan = _write_plan(plan_path, "planA", bank)
    manifest, checkpoint = _manifest_and_checkpoint(["a.json"])
    checkpoint.plans[0].status = STATUS_RUNNING

    harness_dir = tmp_path / "harness_runs"
    config_hash = compute_config_hash(plan)
    run_id = compute_run_id(plan, config_hash, attempt=1)
    save_checkpoint(
        CheckpointState(run_id=run_id, turns=[_turn("learned", 0, "2026-01-01T00:00:00+00:00")], final_status=None),
        harness_dir,
    )

    fake_execute = FakeExecuteRun({
        "planA": _outcome("planA", "complete", [
            _turn("learned", 0, "2026-01-01T00:00:00+00:00"),
            _turn("learned", 1, "2026-01-01T00:01:00+00:00"),
        ]),
    })
    client = FakeBackendClient()

    result = run_queue(
        manifest, checkpoint, tmp_path / "q.json", "continue",
        client, None, harness_dir, tmp_path / "launch", tmp_path / "probes",
        execute_run_fn=fake_execute, print_fn=_noop_print,
    )

    assert fake_execute.calls == ["planA"]  # execute_run() WAS invoked — resumed, not skipped
    assert result.plans[0].status == STATUS_COMPLETE


def test_running_but_harness_already_complete_is_reconciled_without_rerunning(tmp_path):
    """The exact crash window the design doc flags: harness finished
    (final_status='complete' in ITS OWN checkpoint) but the queue's own
    status write never happened before the crash. Must NOT re-invoke
    execute_run() — that would silently burn a wasted fresh attempt."""
    bank = _dummy_bank_path(tmp_path)
    plan_path = tmp_path / "a.json"
    plan = _write_plan(plan_path, "planA", bank)
    manifest, checkpoint = _manifest_and_checkpoint(["a.json"])
    checkpoint.plans[0].status = STATUS_RUNNING

    harness_dir = tmp_path / "harness_runs"
    config_hash = compute_config_hash(plan)
    run_id = compute_run_id(plan, config_hash, attempt=1)
    save_checkpoint(
        CheckpointState(
            run_id=run_id,
            turns=[_turn("learned", 0, "2026-01-01T00:00:00+00:00"), _turn("learned", 1, "2026-01-01T00:01:00+00:00")],
            final_status="complete",
        ),
        harness_dir,
    )

    fake_execute = FakeExecuteRun({})  # would KeyError if ever called
    client = FakeBackendClient()

    result = run_queue(
        manifest, checkpoint, tmp_path / "q.json", "continue",
        client, None, harness_dir, tmp_path / "launch", tmp_path / "probes",
        execute_run_fn=fake_execute, print_fn=_noop_print,
    )

    assert fake_execute.calls == []  # never re-invoked
    assert result.plans[0].status == STATUS_COMPLETE
    assert result.plans[0].run_id == run_id
    assert result.plans[0].per_condition_durations_seconds == {"learned": 60.0}


# ---------------------------------------------------------------------------
# Queue-level unexpected exception
# ---------------------------------------------------------------------------

def test_unexpected_exception_marks_plan_failed_halts_and_writes_checkpoint(tmp_path):
    bank = _dummy_bank_path(tmp_path)
    _write_plan(tmp_path / "a.json", "planA", bank)
    _write_plan(tmp_path / "b.json", "planB", bank)
    manifest, checkpoint = _manifest_and_checkpoint(["a.json", "b.json"])
    launch_dir = tmp_path / "launch"

    fake_execute = FakeExecuteRun({
        "planA": RuntimeError("segfault in some unrelated library"),
        "planB": _outcome("planB", "complete", []),
    })
    client = FakeBackendClient()

    result = run_queue(
        manifest, checkpoint, tmp_path / "q.json", "continue",
        client, None, tmp_path / "harness_runs", launch_dir, tmp_path / "probes",
        execute_run_fn=fake_execute, print_fn=_noop_print,
    )

    assert fake_execute.calls == ["planA"]  # planB never invoked
    assert result.plans[0].status == STATUS_FAILED
    assert "unexpected exception" in result.plans[0].error_summary
    assert result.plans[1].status == STATUS_SKIPPED
    assert result.final_status == "aborted"
    # Checkpoint was actually persisted, not just held in memory.
    assert (launch_dir / "queue_checkpoint.json").exists()


# ---------------------------------------------------------------------------
# Duration capture
# ---------------------------------------------------------------------------

def test_compute_per_condition_durations_derives_min_max_per_condition():
    ckpt = CheckpointState(run_id="r1", turns=[
        _turn("learned", 0, "2026-01-01T00:00:00+00:00"),
        _turn("learned", 1, "2026-01-01T00:02:30+00:00"),
        _turn("static_weight", 0, "2026-01-01T01:00:00+00:00"),
    ])
    durations = compute_per_condition_durations(ckpt)
    assert durations == {"learned": 150.0, "static_weight": 0.0}


def test_plan_duration_fields_populated_from_execute_run_outcome(tmp_path):
    bank = _dummy_bank_path(tmp_path)
    _write_plan(tmp_path / "a.json", "planA", bank)
    manifest, checkpoint = _manifest_and_checkpoint(["a.json"])

    fake_execute = FakeExecuteRun({
        "planA": _outcome("planA", "complete", [
            _turn("learned", 0, "2026-01-01T00:00:00+00:00"),
            _turn("learned", 1, "2026-01-01T00:05:00+00:00"),
        ]),
    })
    client = FakeBackendClient()

    result = run_queue(
        manifest, checkpoint, tmp_path / "q.json", "continue",
        client, None, tmp_path / "harness_runs", tmp_path / "launch", tmp_path / "probes",
        execute_run_fn=fake_execute, print_fn=_noop_print,
    )

    entry = result.plans[0]
    assert entry.per_condition_durations_seconds == {"learned": 300.0}
    assert entry.wall_clock_duration_seconds is not None
    assert entry.wall_clock_duration_seconds >= 0.0


# ---------------------------------------------------------------------------
# queue_summary.md content
# ---------------------------------------------------------------------------

def test_queue_summary_written_with_plan_statuses_and_run_ids(tmp_path):
    bank = _dummy_bank_path(tmp_path)
    _write_plan(tmp_path / "a.json", "planA", bank)
    launch_dir = tmp_path / "launch"
    manifest, checkpoint = _manifest_and_checkpoint(["a.json"])

    ckpt = CheckpointState(
        run_id="planA_run", final_status="complete",
        turns=[_turn("learned", 0, "2026-01-01T00:00:00+00:00")],
    )
    from harness.checkpoint import SeriesReportRecord
    ckpt.series_reports.append(SeriesReportRecord(
        condition="learned", series_index=0, experiment_id="exp1", series_id="series1",
        status="ok", md_path="harness_runs/planA_run/series_report.md",
    ))

    fake_execute = FakeExecuteRun({
        "planA": RunOutcome(
            run_id="planA_run", status="complete", error_summary=None, checkpoint=ckpt,
            manifest_path=Path("m.json"), notification_log_path=Path("n.log"), turns_processed=1,
        ),
    })
    client = FakeBackendClient()

    run_queue(
        manifest, checkpoint, tmp_path / "q.json", "continue",
        client, None, tmp_path / "harness_runs", launch_dir, tmp_path / "probes",
        execute_run_fn=fake_execute, print_fn=_noop_print,
    )

    summary_path = launch_dir / "queue_summary.md"
    assert summary_path.exists()
    text = summary_path.read_text(encoding="utf-8")
    assert "a.json" in text
    assert STATUS_COMPLETE in text
    assert "planA_run" in text
    assert "series_report.md" in text


def test_queue_summary_atomic_write_leaves_no_tmp_file(tmp_path):
    bank = _dummy_bank_path(tmp_path)
    _write_plan(tmp_path / "a.json", "planA", bank)
    launch_dir = tmp_path / "launch"
    manifest, checkpoint = _manifest_and_checkpoint(["a.json"])
    fake_execute = FakeExecuteRun({"planA": _outcome("planA", "complete", [])})
    client = FakeBackendClient()

    run_queue(
        manifest, checkpoint, tmp_path / "q.json", "continue",
        client, None, tmp_path / "harness_runs", launch_dir, tmp_path / "probes",
        execute_run_fn=fake_execute, print_fn=_noop_print,
    )

    assert (launch_dir / "queue_checkpoint.json").exists()
    assert not (launch_dir / "queue_checkpoint.json.tmp").exists()
