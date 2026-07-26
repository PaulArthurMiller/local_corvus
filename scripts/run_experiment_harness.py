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
from harness.cost import BudgetExceededError, check_prerun_budget, estimate_prerun_cost  # noqa: E402
from harness.elicit import elicit_plan, offer_to_save_plan  # noqa: E402
from harness.judge import DEFAULT_JUDGE_HOST, OllamaJudgeClient  # noqa: E402
from harness.prompt_bank import bank_path_for_hash  # noqa: E402
from harness.prompt_gen import (  # noqa: E402
    GenerationAborted,
    MissingApiKeyError,
    estimate_bank_cost_for_plan,
    fixture_path_for_version,
    generate_bank_for_plan,
)
from harness.runner import execute_run, load_probe_battery, scope_for_dry_run  # noqa: E402
from harness.schema import RunPlan, format_validation_error  # noqa: E402
from harness.turn_resolution import TURN_TYPE_PROBE, resolve_turn_sequence  # noqa: E402

logging.basicConfig(
    level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s"
)
logger = logging.getLogger("run_experiment_harness")

DEFAULT_BACKEND_URL = "http://localhost:8000"
RUNS_DIR = PROJECT_ROOT / "harness_runs"
PROBES_DIR = PROJECT_ROOT / "probes"
PROMPT_BANKS_DIR = PROJECT_ROOT / "prompt_banks"
SEED_FIXTURES_DIR = PROJECT_ROOT / "seed_fixtures"


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


def generate_bank_or_exit(plan: RunPlan, fixture_path: Path, output_dir: Path):
    """
    Wrap generate_bank_for_plan()'s two operator-facing failure modes so the
    CLI exits cleanly instead of surfacing a raw traceback — the same
    convention load_plan_from_file() already follows for plan validation.

    Both are expected operator outcomes, not defects: declining the cost gate
    is a deliberate choice, and a missing API key is a setup step. Printing a
    stack trace immediately after a cost-confirmation prompt is actively
    harmful — it reads as a crash mid-call, leaving the operator unsure
    whether any spend or file write happened (neither did).
    """
    try:
        return generate_bank_for_plan(plan, fixture_path, output_dir)
    except GenerationAborted:
        # confirm_cost() has already printed the "no API calls made, no
        # charges, no files written" line; don't restate it.
        print("Prompt bank generation declined — nothing generated.")
        sys.exit(1)
    except MissingApiKeyError as exc:
        print(f"ERROR: {exc}")
        sys.exit(1)


def main() -> None:
    parser = argparse.ArgumentParser(description="Local Corvus experiment harness (M19 Phase 0-1)")
    parser.add_argument("--plan", type=str, default=None, help="Path to a run plan JSON file")
    parser.add_argument("--dry-run", action="store_true", help="Force dry-run mode regardless of the plan")
    parser.add_argument("--backend-url", type=str, default=DEFAULT_BACKEND_URL)
    parser.add_argument("--judge-host", type=str, default=DEFAULT_JUDGE_HOST)
    parser.add_argument(
        "--generate-bank", action="store_true",
        help="Generate (or reuse) the prompt bank for the given plan, then exit without running an experiment",
    )
    parser.add_argument(
        "--interactive", action="store_true",
        help="After each judge verdict, prompt to accept or override it (calibration mode, not routine use)",
    )
    parser.add_argument(
        "--no-docker-restart", action="store_true",
        help=(
            "Skip restarting the backend container between conditions. Backend/core/"
            "orchestrator.py resolves EXPERIMENT_ID once at process startup with no "
            "per-request override, so without a restart every condition in a real "
            "multi-condition run would be stamped with the same experiment_id. Only "
            "safe for single-condition runs, or when EXPERIMENT_ID is being managed "
            "manually outside the harness."
        ),
    )
    args = parser.parse_args()

    if args.plan:
        plan = load_plan_from_file(Path(args.plan))
    else:
        plan = elicit_plan()
        offer_to_save_plan(plan)

    if args.dry_run:
        plan.dry_run = True

    # M28 — fixture path is resolved per plan (seed_fixture_version), not a
    # single hardcoded constant, so a plan can request seed_v2 or seed_v3.
    fixture_path = fixture_path_for_version(plan.seed_fixture_version, SEED_FIXTURES_DIR)

    if args.generate_bank:
        generate_bank_or_exit(plan, fixture_path, PROMPT_BANKS_DIR)
        return

    # M19 Phase 3 — pre-run budget gate, before ANY Sonnet spend (bank
    # generation's own confirm_cost prompt happens further down) or graph
    # mutation. Mirrors the same estimate execute_run() checks internally,
    # so a run launched directly via execute_run() (bypassing this CLI) is
    # still protected — this is the earliest possible exit for the CLI path.
    needs_bank_generation = not plan.prompt_bank_path and plan.conceptual_fields.subjects == "auto"
    bank_generation_cost_usd = 0.0
    if needs_bank_generation:
        bank_estimate = estimate_bank_cost_for_plan(plan, fixture_path, PROMPT_BANKS_DIR)
        bank_generation_cost_usd = (bank_estimate or {}).get("estimated_cost_usd", 0.0)

    full_sequence = resolve_turn_sequence(plan)
    conditions, scoped_sequence = scope_for_dry_run(plan, plan.condition_order, full_sequence)
    n_judge_calls = len(conditions) * sum(1 for t in scoped_sequence if t["turn_type"] != TURN_TYPE_PROBE)
    prerun_estimate = estimate_prerun_cost(bank_generation_cost_usd, n_judge_calls)
    print(
        f"Pre-run cost estimate: bank_generation=${prerun_estimate['bank_generation_cost_usd']:.4f} "
        f"judge_calls=${prerun_estimate['judge_calls_cost_usd']:.4f} "
        f"total=${prerun_estimate['estimated_total_cost_usd']:.4f} budget_cap={plan.budget_cap}"
    )
    try:
        check_prerun_budget(prerun_estimate, plan.budget_cap)
    except BudgetExceededError as exc:
        print(f"ERROR: {exc}")
        sys.exit(1)

    # If the plan doesn't name an existing bank and wants "auto" subjects,
    # generate one now (cost-confirmation gate happens inside prompt_gen.py)
    # before any wipe/seed call.
    if needs_bank_generation:
        bank = generate_bank_or_exit(plan, fixture_path, PROMPT_BANKS_DIR)
        plan.prompt_bank_path = str(bank_path_for_hash(bank.content_hash, PROMPT_BANKS_DIR))

    # M21 — the probe battery drives real POST /admin/run-probe measurements;
    # the version comes from the plan so the manifest records exactly which
    # battery the schedule was built against.
    probe_set_path = PROBES_DIR / f"probe_set_{plan.probe_set_version}.yaml"
    probes = load_probe_battery(probe_set_path)
    logger.info(
        "run_experiment_harness: loaded probe battery %s (%d probe(s))",
        probe_set_path.name, len(probes),
    )

    docker_project_root = None if args.no_docker_restart else PROJECT_ROOT

    with BackendClient(base_url=args.backend_url) as client, OllamaJudgeClient(host=args.judge_host) as judge_client:
        outcome = execute_run(
            plan, client, RUNS_DIR, probes=probes,
            judge_client=judge_client, interactive=args.interactive,
            docker_project_root=docker_project_root,
            bank_generation_cost_usd=bank_generation_cost_usd,
        )

    if outcome.status == "aborted":
        sys.exit(1)


if __name__ == "__main__":
    main()
