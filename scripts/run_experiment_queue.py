"""
Sequential Batch Queue Runner CLI (Milestone 26).

Feeds an ordered list of run-plan files through harness.runner.execute_run()
one at a time, unattended. This is a host-side script, same pattern as
scripts/run_experiment_harness.py — it does not run inside Docker.

M26 ships the queue file schema, the full preflight protocol, and
--dry-run. The execution loop itself (actually calling execute_run() for
each plan, crash-resume, queue_summary.md) lands in M27 — without
--dry-run, this CLI runs preflight, writes the launch manifest + initial
checkpoint, and stops with an explicit message.

Usage:
  python scripts/run_experiment_queue.py --queue queues/screening_a.json --dry-run
  python scripts/run_experiment_queue.py --queue queues/screening_a.json
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
    launch_dir_name,
    load_queue_file,
    run_preflight,
    save_queue_checkpoint,
    save_queue_manifest,
)

logging.basicConfig(
    level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s"
)
logger = logging.getLogger("run_experiment_queue")

DEFAULT_BACKEND_URL = "http://localhost:8000"
QUEUES_RUNS_DIR = PROJECT_ROOT / "harness_runs" / "queues"
PROBES_DIR = PROJECT_ROOT / "probes"
PROMPT_BANKS_DIR = PROJECT_ROOT / "prompt_banks"
SEED_FIXTURE_PATH = PROJECT_ROOT / "seed_fixtures" / "seed_v2.json.gz"


def main() -> None:
    parser = argparse.ArgumentParser(description="Local Corvus sequential batch queue runner (M26)")
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
        help="Explicit launch directory to resume. Parsed now; the resume execution "
             "path lands in M27 — this flag is a no-op until then.",
    )
    args = parser.parse_args()

    try:
        queue_file = load_queue_file(Path(args.queue))
    except (FileNotFoundError, ValueError) as exc:
        print(f"ERROR: {exc}")
        sys.exit(1)

    if args.resume_dir:
        print(f"Note: --resume-dir={args.resume_dir!r} is parsed but not yet acted on — resume lands in M27.")

    with BackendClient(base_url=args.backend_url) as client, OllamaJudgeClient(host=args.judge_host) as judge_client:
        try:
            resolution = run_preflight(
                queue_file, Path(args.queue), client, judge_client,
                SEED_FIXTURE_PATH, PROMPT_BANKS_DIR, PROJECT_ROOT,
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

    if args.dry_run:
        print("\nDry run complete — zero mutation calls made, nothing written to disk.")
        sys.exit(0)

    launch_dir = QUEUES_RUNS_DIR / launch_dir_name(queue_file.queue_id)
    save_queue_manifest(resolution.manifest, launch_dir)
    save_queue_checkpoint(resolution.checkpoint, launch_dir)
    print(
        f"\nPreflight complete. Manifest + initial checkpoint written to {launch_dir}. "
        f"Execution loop lands in M27."
    )


if __name__ == "__main__":
    main()
