"""
Sequential Batch Queue Runner CLI (Milestones 26-27).

Feeds an ordered list of run-plan files through harness.runner.execute_run()
one at a time, unattended overnight. This is a host-side script, same
pattern as scripts/run_experiment_harness.py — it does not run inside
Docker.

M26 shipped the queue file schema, the full preflight protocol, and
--dry-run. M27 adds the real execution loop: on_plan_failure continue/halt
semantics (with a post-failure health recheck that can override
"continue"), crash-resume (auto-detected via the latest `{queue_id}_*`
launch directory, guarded by a queue-content-hash match — a mismatch hard-
stops rather than guessing which plans already ran), per-plan/per-condition
duration capture, queue_summary.md, and queue-level notifications.

Usage:
  python scripts/run_experiment_queue.py --queue queues/screening_a.json --dry-run
  python scripts/run_experiment_queue.py --queue queues/screening_a.json
  python scripts/run_experiment_queue.py --queue queues/screening_a.json --resume-dir screening_a_20260712T030000Z
  python scripts/run_experiment_queue.py --queue queues/screening_a.json --backend-url http://localhost:8000
"""

import argparse
import logging
import sys
from pathlib import Path

SCRIPT_DIR = Path(__file__).parent
PROJECT_ROOT = SCRIPT_DIR.parent
sys.path.insert(0, str(SCRIPT_DIR))

from harness.client import BackendClient  # noqa: E402
from harness.judge import DEFAULT_JUDGE_HOST, OllamaJudgeClient  # noqa: E402
from harness.queue import (  # noqa: E402
    QueuePreflightError,
    compute_queue_content_hash,
    find_latest_launch_dir,
    launch_dir_name,
    load_queue_checkpoint,
    load_queue_file,
    load_queue_manifest,
    run_preflight,
    save_queue_checkpoint,
    save_queue_manifest,
)
from harness.queue_runner import run_queue  # noqa: E402

logging.basicConfig(
    level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s"
)
logger = logging.getLogger("run_experiment_queue")

DEFAULT_BACKEND_URL = "http://localhost:8000"
HARNESS_RUNS_DIR = PROJECT_ROOT / "harness_runs"
QUEUES_RUNS_DIR = PROJECT_ROOT / "harness_runs" / "queues"
PROBES_DIR = PROJECT_ROOT / "probes"
PROMPT_BANKS_DIR = PROJECT_ROOT / "prompt_banks"
SEED_FIXTURES_DIR = PROJECT_ROOT / "seed_fixtures"


def _resolve_resume_dir(explicit: str) -> Path:
    p = Path(explicit)
    return p if p.is_absolute() else (QUEUES_RUNS_DIR / p)


def main() -> None:
    parser = argparse.ArgumentParser(description="Local Corvus sequential batch queue runner (M26-27)")
    parser.add_argument("--queue", type=str, required=True, help="Path to a queue file JSON")
    parser.add_argument(
        "--dry-run", action="store_true",
        help="Run full preflight and print the resolved execution order, then exit 0 — "
             "zero HTTP mutation calls, zero graph mutation, zero files written.",
    )
    parser.add_argument("--backend-url", type=str, default=DEFAULT_BACKEND_URL)
    parser.add_argument("--judge-host", type=str, default=DEFAULT_JUDGE_HOST)
    parser.add_argument(
        "--resume-dir", type=str, default=None,
        help="Explicit launch directory to resume (relative to harness_runs/queues/, or "
             "absolute). Its queue_content_hash must match the current queue file exactly, "
             "or this hard-stops rather than guessing. Omit to auto-detect the latest "
             "launch of this queue_id.",
    )
    parser.add_argument(
        "--no-docker-restart", action="store_true",
        help="Skip restarting the backend container between conditions — same caveat as "
             "run_experiment_harness.py's flag of the same name (only safe for "
             "single-condition plans, or EXPERIMENT_ID managed manually).",
    )
    args = parser.parse_args()

    try:
        queue_file = load_queue_file(Path(args.queue))
    except (FileNotFoundError, ValueError) as exc:
        print(f"ERROR: {exc}")
        sys.exit(1)

    docker_project_root = None if args.no_docker_restart else PROJECT_ROOT

    with BackendClient(base_url=args.backend_url) as client, OllamaJudgeClient(host=args.judge_host) as judge_client:
        if args.dry_run:
            try:
                resolution = run_preflight(
                    queue_file, Path(args.queue), client, judge_client,
                    SEED_FIXTURES_DIR, PROMPT_BANKS_DIR, PROJECT_ROOT,
                    # M43 — an override-carrying plan is unrunnable with
                    # restarts disabled; preflight says so at minute zero.
                    docker_restart_enabled=docker_project_root is not None,
                )
            except QueuePreflightError as exc:
                print(f"ERROR: preflight failed: {exc}")
                sys.exit(1)

            print(f"\nPreflight passed for queue_id={queue_file.queue_id!r}. Resolved execution order:")
            for entry in resolution.manifest.plans:
                if entry.status != "resolved":
                    print(f"  [skipped_disabled] {entry.plan_path}")
                    continue
                print(
                    f"  plan_id={entry.plan_id!r} config_hash={entry.config_hash} "
                    f"expected_run_id={entry.expected_run_id!r} bank_path={entry.bank_path!r} "
                    f"conditions={entry.conditions}"
                )
            print("\nDry run complete — zero mutation calls made, nothing written to disk.")
            sys.exit(0)

        # --- Resume detection: explicit --resume-dir, else auto-detect the
        # latest {queue_id}_* launch directory. Either way, a mismatched
        # queue_content_hash hard-stops — the queue file changed since that
        # launch, so silently resuming would execute against a stale
        # resolved plan list (bank paths, config hashes) from before the edit.
        candidate_dir = (
            _resolve_resume_dir(args.resume_dir) if args.resume_dir
            else find_latest_launch_dir(QUEUES_RUNS_DIR, queue_file.queue_id)
        )

        resuming = False
        if candidate_dir is not None and candidate_dir.exists():
            manifest = load_queue_manifest(candidate_dir)
            checkpoint = load_queue_checkpoint(candidate_dir)
            if manifest is None or checkpoint is None:
                print(f"ERROR: {candidate_dir} is missing queue_manifest.json or queue_checkpoint.json")
                sys.exit(1)
            current_hash = compute_queue_content_hash(queue_file)
            if manifest.queue_content_hash != current_hash:
                print(
                    f"ERROR: {candidate_dir} was launched from a different queue file "
                    f"(queue_content_hash mismatch: {manifest.queue_content_hash} != "
                    f"{current_hash}) — the queue file changed since that launch. "
                    f"Start a fresh launch, or point --resume-dir at the correct directory."
                )
                sys.exit(1)
            launch_dir = candidate_dir
            resuming = True
            print(f"Resuming launch {launch_dir} (queue_content_hash matches).")
        else:
            try:
                resolution = run_preflight(
                    queue_file, Path(args.queue), client, judge_client,
                    SEED_FIXTURES_DIR, PROMPT_BANKS_DIR, PROJECT_ROOT,
                    # M43 — an override-carrying plan is unrunnable with
                    # restarts disabled; preflight says so at minute zero.
                    docker_restart_enabled=docker_project_root is not None,
                )
            except QueuePreflightError as exc:
                print(f"ERROR: preflight failed: {exc}")
                sys.exit(1)

            launch_dir = QUEUES_RUNS_DIR / launch_dir_name(queue_file.queue_id)
            save_queue_manifest(resolution.manifest, launch_dir)
            save_queue_checkpoint(resolution.checkpoint, launch_dir)
            manifest = resolution.manifest
            checkpoint = resolution.checkpoint
            print(f"Preflight complete. Manifest + initial checkpoint written to {launch_dir}.")

        print(f"\n{'Resuming' if resuming else 'Starting'} queue_id={queue_file.queue_id!r} "
              f"on_plan_failure={queue_file.on_plan_failure!r}")
        final_checkpoint = run_queue(
            manifest, checkpoint, Path(args.queue), queue_file.on_plan_failure,
            client, judge_client, HARNESS_RUNS_DIR, launch_dir, PROBES_DIR,
            docker_project_root=docker_project_root,
            notification_channel=queue_file.notification.channel,
        )

    print(f"\nQueue {final_checkpoint.final_status.upper()}. Summary: {launch_dir / 'queue_summary.md'}")
    if final_checkpoint.final_status != "complete":
        sys.exit(1)


if __name__ == "__main__":
    main()
