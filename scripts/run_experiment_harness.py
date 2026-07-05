"""
Experiment Harness CLI (Milestone 19, Phase 0).

Loads or elicits a Run Plan, validates it against the schema, wipes+seeds
the graph per condition, resolves turn types deterministically, runs the
turn loop with per-turn checkpointing, applies an error-handling policy,
and calls the notification stub.

This is a host-side script that calls the Dockerized backend's API over
HTTP — it does not run inside the container. Same pattern as
scripts/generate_seed_v2.py and scripts/verify_seed_v2.py.

Usage:
  python scripts/run_experiment_harness.py                        # interactive elicitation
  python scripts/run_experiment_harness.py --plan plans/foo.json
  python scripts/run_experiment_harness.py --plan plans/foo.json --dry-run
  python scripts/run_experiment_harness.py --backend-url http://localhost:8000
"""

import argparse
import json
import logging
import sys
from pathlib import Path

from pydantic import ValidationError

SCRIPT_DIR = Path(__file__).parent
PROJECT_ROOT = SCRIPT_DIR.parent
sys.path.insert(0, str(SCRIPT_DIR))

from harness.client import BackendClient  # noqa: E402
from harness.elicit import elicit_plan, offer_to_save_plan  # noqa: E402
from harness.prompt_bank import bank_path_for_hash  # noqa: E402
from harness.prompt_gen import generate_bank_for_plan  # noqa: E402
from harness.runner import execute_run, load_probe_prompts  # noqa: E402
from harness.schema import RunPlan, format_validation_error  # noqa: E402

logging.basicConfig(
    level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s"
)
logger = logging.getLogger("run_experiment_harness")

DEFAULT_BACKEND_URL = "http://localhost:8000"
RUNS_DIR = PROJECT_ROOT / "harness_runs"
PROBE_SET_PATH = PROJECT_ROOT / "probes" / "probe_set_v2.yaml"
PROMPT_BANKS_DIR = PROJECT_ROOT / "prompt_banks"
SEED_FIXTURE_PATH = PROJECT_ROOT / "seed_fixtures" / "seed_v2.json.gz"


def load_plan_from_file(plan_path: Path) -> RunPlan:
    """Load and validate a plan file. Exits non-zero with a field-identifying
    error on any failure — never touches the graph on a bad plan."""
    if not plan_path.exists():
        print(f"ERROR: plan file not found: {plan_path}")
        sys.exit(1)

    try:
        raw = json.loads(plan_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        print(f"ERROR: plan file is not valid JSON: {exc}")
        sys.exit(1)

    try:
        return RunPlan(**raw)
    except ValidationError as exc:
        print("ERROR: plan validation failed:")
        print(format_validation_error(exc))
        sys.exit(1)


def main() -> None:
    parser = argparse.ArgumentParser(description="Local Corvus experiment harness (M19 Phase 0-1)")
    parser.add_argument("--plan", type=str, default=None, help="Path to a run plan JSON file")
    parser.add_argument("--dry-run", action="store_true", help="Force dry-run mode regardless of the plan")
    parser.add_argument("--backend-url", type=str, default=DEFAULT_BACKEND_URL)
    parser.add_argument(
        "--generate-bank", action="store_true",
        help="Generate (or reuse) the prompt bank for the given plan, then exit without running an experiment",
    )
    args = parser.parse_args()

    if args.plan:
        plan = load_plan_from_file(Path(args.plan))
    else:
        plan = elicit_plan()
        offer_to_save_plan(plan)

    if args.dry_run:
        plan.dry_run = True

    if args.generate_bank:
        generate_bank_for_plan(plan, SEED_FIXTURE_PATH, PROMPT_BANKS_DIR)
        return

    # If the plan doesn't name an existing bank and wants "auto" subjects,
    # generate one now (cost-confirmation gate happens inside prompt_gen.py)
    # before any wipe/seed call.
    if not plan.prompt_bank_path and plan.conceptual_fields.subjects == "auto":
        bank = generate_bank_for_plan(plan, SEED_FIXTURE_PATH, PROMPT_BANKS_DIR)
        plan.prompt_bank_path = str(bank_path_for_hash(bank.content_hash, PROMPT_BANKS_DIR))

    probes = load_probe_prompts(PROBE_SET_PATH)
    logger.info("run_experiment_harness: loaded %d probe prompt(s) for placeholder content", len(probes))

    with BackendClient(base_url=args.backend_url) as client:
        outcome = execute_run(plan, client, RUNS_DIR, probes=probes)

    if outcome.status == "aborted":
        sys.exit(1)


if __name__ == "__main__":
    main()
