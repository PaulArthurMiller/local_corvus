"""
Queue execution loop (Milestone 27) — orchestrates harness.runner.execute_run()
across an already-preflighted queue: on_plan_failure continue/halt semantics,
a post-failure health recheck that can override "continue", crash-resume
reconciliation against the harness's own per-run checkpoint, per-plan and
per-condition duration capture, queue-level notifications, and the
queue_summary.md writer.

Layer boundary (per BUILD-PLAN.md): this module knows nothing about graph/
turn semantics — it only knows QueueManifest/QueueCheckpointState entries
and how to call execute_run() once per enabled plan, exactly like a
playlist player advancing between tracks.
"""

import json
import logging
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Optional

from harness.checkpoint import CheckpointState
from harness.client import BackendConnectivityError
from harness.manifest import compute_config_hash, now_iso
from harness.notify import build_notification_payload, notify
from harness.queue import (
    STATUS_COMPLETE,
    STATUS_FAILED,
    STATUS_RUNNING,
    STATUS_SKIPPED,
    QueueCheckpointState,
    QueueManifest,
    QueuePlanManifestEntry,
    resolve_plan_path,
    save_queue_checkpoint,
)
from harness.runner import RunOutcome, execute_run, load_probe_battery, resolve_run_attempt
from harness.schema import RunPlan

logger = logging.getLogger("harness.queue_runner")

# Non-interactive: the queue runs unattended overnight, so every resume
# prompt execute_run()/resolve_run_attempt() would otherwise raise is
# answered "yes" automatically — skip already-done turns, never block.
_AUTO_YES_RESUME_INPUT: Callable[[str], str] = lambda _prompt: "y"  # noqa: E731


def _load_plan_for_execution(resolved_path: Path, manifest_entry: QueuePlanManifestEntry) -> RunPlan:
    """Reload the plan from disk and re-apply preflight's bank-path
    stamping (manifest_entry.bank_path) — the mutation run_preflight()
    made to the in-memory plan object never touched the file on disk."""
    raw = json.loads(resolved_path.read_text(encoding="utf-8"))
    plan = RunPlan(**raw)
    if manifest_entry.bank_path:
        plan.prompt_bank_path = manifest_entry.bank_path
    return plan


def compute_per_condition_durations(checkpoint: CheckpointState) -> dict:
    """Per-condition wall-clock duration derived from the harness
    checkpoint's own per-turn timestamps (max - min within each condition,
    seconds) — zero runner.py changes needed, matching the layer-boundary
    call made during planning. A condition with a single turn reports 0.0."""
    groups: dict = {}
    for turn in checkpoint.turns:
        groups.setdefault(turn.condition, []).append(turn.timestamp)
    durations = {}
    for condition, timestamps in groups.items():
        parsed = sorted(datetime.fromisoformat(ts) for ts in timestamps)
        durations[condition] = (parsed[-1] - parsed[0]).total_seconds()
    return durations


def _collect_report_paths(checkpoint: CheckpointState) -> list:
    """Series report md_paths (in series_index order) followed by the
    experiment report's md_path, if present — the terminal rollup lands
    last, matching how a human would want to read the list."""
    paths = [r.md_path for r in checkpoint.series_reports if r.md_path]
    if checkpoint.experiment_report is not None and checkpoint.experiment_report.md_path:
        paths.append(checkpoint.experiment_report.md_path)
    return paths


def write_queue_summary(checkpoint: QueueCheckpointState, launch_dir: Path) -> Path:
    """Human-readable queue_summary.md: one row per plan with its final
    status, run_id, durations, and generated report paths."""
    lines = [
        f"# Queue Summary — {checkpoint.queue_id}",
        "",
        f"Final status: {checkpoint.final_status}",
        "",
        "| Plan | Status | Run ID | Duration (s) | Per-condition durations (s) | Reports | Error |",
        "|---|---|---|---|---|---|---|",
    ]
    for entry in checkpoint.plans:
        duration_str = (
            f"{entry.wall_clock_duration_seconds:.1f}"
            if entry.wall_clock_duration_seconds is not None else "—"
        )
        per_condition = ", ".join(
            f"{c}={d:.1f}" for c, d in entry.per_condition_durations_seconds.items()
        ) or "—"
        reports = "<br>".join(entry.report_paths) or "—"
        error = entry.error_summary or "—"
        lines.append(
            f"| {entry.plan_path} | {entry.status} | {entry.run_id or '—'} | "
            f"{duration_str} | {per_condition} | {reports} | {error} |"
        )
    path = launch_dir / "queue_summary.md"
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


def run_queue(
    manifest: QueueManifest,
    checkpoint: QueueCheckpointState,
    queue_file_path: Path,
    on_plan_failure: str,
    client: Any,
    judge_client: Any,
    harness_run_base_dir: Path,
    launch_dir: Path,
    probes_dir: Path,
    docker_project_root: Optional[Path] = None,
    load_probe_battery_fn: Callable[[Path], list] = load_probe_battery,
    execute_run_fn: Callable[..., RunOutcome] = execute_run,
    print_fn: Callable[[str], None] = print,
    notify_fn: Callable[..., Path] = notify,
    notification_channel: str = "log",
) -> QueueCheckpointState:
    """
    Run every not-yet-terminal plan in `checkpoint` in order, mutating and
    atomically saving `checkpoint` after every state transition (crash
    between any two writes loses at most the in-flight plan's own status,
    which the crash-resume peek below recovers on the next launch).

    Every plan gets the same crash-reconciliation peek regardless of its
    current queue-checkpoint status: resolve_run_attempt(bump_on_complete=
    False) locates the harness's own attempt-1 checkpoint for this plan's
    identity, without forking a second copy of the attempt-resolution
    logic. If that checkpoint is already "complete" — whether because a
    prior queue session crashed between the harness finishing and this
    checkpoint recording it, or the identity was already run manually
    outside this queue — the plan is reconciled as complete without ever
    calling execute_run() again (which would otherwise treat "already
    complete" as a signal to burn a fresh, wasted attempt).
    """
    manifest_by_path = {e.plan_path: e for e in manifest.plans}
    halted = False

    for entry in checkpoint.plans:
        if entry.status in (STATUS_COMPLETE, STATUS_SKIPPED):
            print_fn(f"  [{entry.status}] {entry.plan_path} — already done, skipping")
            continue

        if halted:
            entry.status = STATUS_SKIPPED
            save_queue_checkpoint(checkpoint, launch_dir)
            print_fn(f"  [skipped] {entry.plan_path} — remaining queue halted")
            continue

        manifest_entry = manifest_by_path[entry.plan_path]
        resolved_path = resolve_plan_path(queue_file_path, entry.plan_path)
        plan = _load_plan_for_execution(resolved_path, manifest_entry)
        config_hash = compute_config_hash(plan)
        # Each plan names its own probe_set_version, so the battery is
        # loaded per plan rather than once for the whole queue.
        probes = load_probe_battery_fn(probes_dir / f"probe_set_{plan.probe_set_version}.yaml")

        run_id, _attempt, _resume, existing = resolve_run_attempt(
            plan, config_hash, harness_run_base_dir,
            resume_input_fn=_AUTO_YES_RESUME_INPUT, print_fn=print_fn,
            bump_on_complete=False,
        )

        if existing is not None and existing.final_status == "complete":
            print_fn(
                f"  Reconciling {entry.plan_path}: harness run {run_id!r} already "
                f"completed — not re-running."
            )
            timestamps = sorted(t.timestamp for t in existing.turns)
            entry.status = STATUS_COMPLETE
            entry.run_id = run_id
            if timestamps:
                entry.started_at = timestamps[0]
                entry.ended_at = timestamps[-1]
                entry.wall_clock_duration_seconds = (
                    datetime.fromisoformat(timestamps[-1]) - datetime.fromisoformat(timestamps[0])
                ).total_seconds()
            entry.per_condition_durations_seconds = compute_per_condition_durations(existing)
            entry.report_paths = _collect_report_paths(existing)
            save_queue_checkpoint(checkpoint, launch_dir)
            continue

        entry.status = STATUS_RUNNING
        entry.run_id = run_id
        entry.started_at = now_iso()
        save_queue_checkpoint(checkpoint, launch_dir)
        print_fn(f"\n=== Plan: {entry.plan_path} (run_id={run_id!r}) ===")

        try:
            outcome = execute_run_fn(
                plan, client, harness_run_base_dir,
                probes=probes, resume_input_fn=_AUTO_YES_RESUME_INPUT, print_fn=print_fn,
                judge_client=judge_client, docker_project_root=docker_project_root,
            )
        except Exception as exc:  # noqa: BLE001 — queue-level catch-all; execute_run()'s
            # own hard-stop unwinding already returns a normal "aborted"
            # RunOutcome, so anything reaching here is a genuinely
            # unexpected failure. Never let it kill the whole queue silently.
            logger.exception("queue_runner: unexpected exception running plan %s", entry.plan_path)
            entry.status = STATUS_FAILED
            entry.error_summary = f"unexpected exception: {exc}"
            entry.ended_at = now_iso()
            entry.wall_clock_duration_seconds = (
                datetime.fromisoformat(entry.ended_at) - datetime.fromisoformat(entry.started_at)
            ).total_seconds()
            save_queue_checkpoint(checkpoint, launch_dir)
            print_fn(f"  Plan FAILED (unexpected exception): {entry.plan_path}: {exc}")
            halted = True
            continue

        entry.ended_at = now_iso()
        entry.wall_clock_duration_seconds = (
            datetime.fromisoformat(entry.ended_at) - datetime.fromisoformat(entry.started_at)
        ).total_seconds()
        entry.per_condition_durations_seconds = compute_per_condition_durations(outcome.checkpoint)
        entry.report_paths = _collect_report_paths(outcome.checkpoint)

        if outcome.status == "complete":
            entry.status = STATUS_COMPLETE
            save_queue_checkpoint(checkpoint, launch_dir)
            print_fn(f"  Plan complete: {entry.plan_path} run_id={outcome.run_id}")
            notify_fn(
                "queue_plan_complete",
                build_notification_payload(
                    run_id=outcome.run_id, status="complete",
                    report_path=(entry.report_paths[-1] if entry.report_paths else None),
                ),
                harness_run_base_dir, channel=notification_channel,
            )
            continue

        entry.status = STATUS_FAILED
        entry.error_summary = outcome.error_summary
        save_queue_checkpoint(checkpoint, launch_dir)
        print_fn(f"  Plan FAILED: {entry.plan_path}: {outcome.error_summary}")
        notify_fn(
            "queue_plan_failed",
            build_notification_payload(
                run_id=outcome.run_id, status="failed", error_summary=outcome.error_summary,
            ),
            harness_run_base_dir, channel=notification_channel,
        )

        if on_plan_failure == "halt":
            halted = True
            continue

        # on_plan_failure == "continue" — but a failed plan's root cause
        # (backend crash, etc.) might mean the backend is still unhealthy;
        # a health recheck here overrides "continue" so the remaining
        # plans don't all fail identically against a dead backend.
        try:
            client.health()
        except BackendConnectivityError as exc:
            print_fn(f"  Health recheck failed after plan failure — halting remaining queue: {exc}")
            halted = True

    checkpoint.final_status = "aborted" if halted else "complete"
    save_queue_checkpoint(checkpoint, launch_dir)
    write_queue_summary(checkpoint, launch_dir)

    notify_fn(
        "queue_aborted" if halted else "queue_complete",
        build_notification_payload(run_id=checkpoint.queue_id, status=checkpoint.final_status),
        launch_dir, channel=notification_channel,
    )

    return checkpoint
