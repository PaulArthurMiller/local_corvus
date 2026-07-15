"""
Core harness orchestration — separated from CLI argument parsing/elicitation
so it can be unit-tested with a fake BackendClient (no real backend needed).

execute_run() is the single entry point: given a validated plan and a
BackendClient, it computes the manifest, resolves turn types, wipes+seeds
per condition, runs the turn loop with per-turn checkpointing, applies the
error-handling policy, and calls the notification stub.
"""

import logging
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Optional

import yaml

from harness.checkpoint import (
    STATUS_OK,
    STATUS_SOFT_CONTINUE,
    CheckpointState,
    ExperimentReportRecord,
    SeriesReportRecord,
    TurnCheckpoint,
    is_turn_done,
    load_checkpoint,
    save_checkpoint,
)
from harness.client import (
    BackendClient,
    BackendConnectivityError,
    BackendTurnError,
    ReportGenerationError,
)
from harness.cost import (
    JUDGE_COST_PER_CALL_USD,
    BudgetExceededError,
    CostTracker,
    check_prerun_budget,
    estimate_prerun_cost,
)
from harness.docker_lifecycle import (
    DockerLifecycleError,
    restart_backend,
    wait_for_health,
)
from harness.judge import JudgeUnparseableError, judge_turn
from harness.manifest import (
    RunManifest,
    compute_config_hash,
    compute_run_id,
    now_iso,
    run_identity_for,
    save_manifest,
)
from harness.notify import build_notification_payload, notify
from harness.prompt_bank import PromptBank, PromptBankEntry, load_bank
from harness.schema import DEFAULT_ERROR_POLICY, RunPlan
from harness.turn_resolution import (
    TURN_TYPE_PROBE,
    assign_probe_schedule,
    assign_prompt_ids,
    resolve_turn_sequence,
)

logger = logging.getLogger("harness.runner")


class HardStopError(Exception):
    """Raised internally to unwind the turn loop on a hard-stop condition."""


@dataclass
class RunOutcome:
    run_id: str
    status: str  # "complete" | "aborted"
    error_summary: Optional[str]
    checkpoint: CheckpointState
    manifest_path: Path
    notification_log_path: Path
    turns_processed: int = 0


def resolve_error_policy(plan: RunPlan) -> dict:
    policy = dict(DEFAULT_ERROR_POLICY)
    policy.update(plan.error_policy or {})
    return policy


def load_probe_battery(probe_set_path: Path) -> list:
    """
    Load the probe battery — ordered {probe_id, prompt_text} entries — from
    a probe_set_*.yaml file.

    Standalone parse (no backend import) — mirrors the existing convention
    of scripts/generate_seed_v2.py and scripts/verify_seed_v2.py, which read
    fixture/probe files directly rather than importing backend internals.
    M21: the battery's ordering drives assign_probe_schedule(); the backend
    loads its own copy of the same file when POST /admin/run-probe executes,
    so only probe_id (not prompt_text) crosses the API boundary.
    """
    if not probe_set_path.exists():
        logger.warning("runner: probe set not found at %s — probe turns cannot run", probe_set_path)
        return []
    with open(probe_set_path, "r", encoding="utf-8") as f:
        data = yaml.safe_load(f) or {}
    probes = []
    for entry in data.get("probes", []):
        prompt_text = entry.get("prompt_text")
        if prompt_text:
            probes.append({"probe_id": entry.get("probe_id"), "prompt_text": prompt_text})
    return probes


def _placeholder_bulk_message(condition: str, turn: dict) -> str:
    return (
        f"[Phase 0 placeholder — real bulk content arrives in Phase 1] "
        f"condition={condition} series={turn['series_index']} turn={turn['turn_index']}. "
        f"Please respond briefly to confirm the pipeline is working."
    )


def build_turn_message(turn: dict, condition: str, prompt_lookup: Optional[dict] = None) -> str:
    """
    Resolve message content for a bulk turn: a real bank prompt (via
    turn["prompt_id"] -> prompt_lookup), or — with no bank loaded — the
    Phase 0 labeled placeholder stub. Probe turns never build a chat
    message (M21 — they go through client.run_probe(), not client.chat()).
    """
    if prompt_lookup and turn.get("prompt_id") in prompt_lookup:
        return prompt_lookup[turn["prompt_id"]]
    return _placeholder_bulk_message(condition, turn)


def _judge_and_post_verdict(
    client: BackendClient,
    judge_client: Any,
    plan: RunPlan,
    condition: str,
    run_id: Optional[str],
    prompt_entry: PromptBankEntry,
    response_text: str,
    turn_type: str,
    error_policy: dict,
    interactive: bool,
    resume_input_fn: Callable[[str], str],
    print_fn: Callable[[str], None],
) -> tuple:
    """
    Judge one bulk turn's response and post the verdict to the backend.

    Returns (verdict, feedback_event_id, judge_error) — verdict and
    feedback_event_id are None when judging soft-continues (judge_error
    holds the reason instead). Raises HardStopError only for the
    judge_unparseable hard_stop policy case. BackendConnectivityError /
    BackendTurnError raised by client.post_verdict() propagate to the
    caller, which applies the standard connectivity/turn_ambiguous policy —
    the same categories every other backend call in the loop uses.
    """
    try:
        verdict_obj = judge_turn(
            judge_client, prompt_entry.prompt_text, prompt_entry.criteria,
            response_text, plan.judge, turn_type,
        )
    except JudgeUnparseableError as exc:
        policy = error_policy.get("judge_unparseable", "soft_continue")
        if policy == "hard_stop":
            raise HardStopError(f"judge_unparseable: {exc}") from exc
        print_fn(f"  judge verdict unparseable — soft-continuing, no verdict recorded: {exc}")
        logger.warning("runner: judge_unparseable (soft_continue): %s", exc)
        return None, None, f"judge_unparseable: {exc}"

    verdict = verdict_obj.verdict
    judge_source = f"model:{plan.judge.model}"

    if interactive:
        print_fn(
            f"  Judge verdict: {verdict} rationale={verdict_obj.rationale!r} "
            f"criterion_triggered={verdict_obj.criterion_triggered!r}"
        )
        answer = resume_input_fn("Accept judge verdict, or override? [a/o/skip]: ").strip().lower()
        if answer == "skip":
            return None, None, "verdict skipped by operator (--interactive)"
        if answer == "o":
            override = resume_input_fn("Override verdict (up/down/none): ").strip().lower()
            if override in ("up", "down", "none"):
                verdict = override
                judge_source = "human"

    apply_weight_update = condition == "learned" and verdict in ("up", "down")
    verdict_response = client.post_verdict(
        run_id=run_id,
        verdict=verdict,
        condition_label=condition,
        judge_source=judge_source,
        apply_weight_update=apply_weight_update,
        rationale=verdict_obj.rationale,
        criterion_triggered=verdict_obj.criterion_triggered,
        judge_model_name=plan.judge.model,
        judge_temperature=plan.judge.temperature,
        judge_pinned_snapshot=plan.judge.pinned_snapshot,
        prompt_id=prompt_entry.prompt_id,
    )
    return verdict, verdict_response.get("feedback_event_id"), None


def _generate_series_report(
    client: BackendClient,
    error_policy: dict,
    print_fn: Callable[[str], None],
    condition: str,
    series_index: int,
    experiment_id: str,
    series_id: str,
) -> SeriesReportRecord:
    """
    Call generate-series for one completed (condition, series_index) and
    return a SeriesReportRecord regardless of outcome. Failures are
    governed by error_policy["report_generation"] (soft_continue by
    default, prominently logged) rather than the generic
    connectivity/turn_ambiguous categories — a broken report is
    re-generatable later; an aborted run that otherwise succeeded is not.
    """
    try:
        result = client.generate_series_report(experiment_id, series_id)
    except ReportGenerationError as exc:
        policy = error_policy.get("report_generation", "soft_continue")
        message = (
            f"series report generation failed for condition={condition} "
            f"series_index={series_index} (experiment_id={experiment_id} "
            f"series_id={series_id}): {exc}"
        )
        if policy == "hard_stop":
            raise HardStopError(f"report_generation: {message}") from exc
        logger.error("runner: %s (soft_continue per error_policy)", message)
        print_fn(f"  ⚠ SERIES REPORT GENERATION FAILED (soft-continue): {message}")
        return SeriesReportRecord(
            condition=condition, series_index=series_index, experiment_id=experiment_id,
            series_id=series_id, status=STATUS_SOFT_CONTINUE, error=str(exc),
        )

    print_fn(
        f"  Series report generated: condition={condition} series_index={series_index} "
        f"report_id={result.get('report_id')} md={result.get('md_path')}"
    )
    return SeriesReportRecord(
        condition=condition, series_index=series_index, experiment_id=experiment_id,
        series_id=series_id, status=STATUS_OK, report_id=result.get("report_id"),
        json_path=result.get("json_path"), md_path=result.get("md_path"),
    )


def _generate_experiment_report(
    client: BackendClient,
    error_policy: dict,
    print_fn: Callable[[str], None],
    experiment_id: str,
    control_experiment_ids: Optional[dict],
) -> ExperimentReportRecord:
    """Call generate-experiment at experiment close. Same soft-continue-by-default
    policy as _generate_series_report, via the same error_policy key."""
    try:
        result = client.generate_experiment_report(experiment_id, control_experiment_ids)
    except ReportGenerationError as exc:
        policy = error_policy.get("report_generation", "soft_continue")
        message = f"experiment report generation failed (experiment_id={experiment_id}): {exc}"
        if policy == "hard_stop":
            raise HardStopError(f"report_generation: {message}") from exc
        logger.error("runner: %s (soft_continue per error_policy)", message)
        print_fn(f"  ⚠ EXPERIMENT REPORT GENERATION FAILED (soft-continue): {message}")
        return ExperimentReportRecord(
            experiment_id=experiment_id, status=STATUS_SOFT_CONTINUE, error=str(exc),
        )

    print_fn(
        f"  Experiment report generated: experiment_id={experiment_id} "
        f"report_id={result.get('report_id')} md={result.get('md_path')}"
    )
    return ExperimentReportRecord(
        experiment_id=experiment_id, status=STATUS_OK, report_id=result.get("report_id"),
        json_path=result.get("json_path"), md_path=result.get("md_path"),
    )


def scope_for_dry_run(plan: RunPlan, conditions: list, full_sequence: list) -> tuple:
    """
    Dry-run mode limits execution to dry_run_n turns, 1 series, and the
    first condition in condition_order (learned, by default ordering).
    """
    if not plan.dry_run:
        return conditions, full_sequence

    scoped_conditions = conditions[:1] if conditions else ["learned"]
    scoped_sequence = [t for t in full_sequence if t["series_index"] == 0][: plan.dry_run_n]
    return scoped_conditions, scoped_sequence


def execute_run(
    plan: RunPlan,
    client: BackendClient,
    run_base_dir: Path,
    probes: Optional[list] = None,
    resume_input_fn: Callable[[str], str] = input,
    print_fn: Callable[[str], None] = print,
    judge_client: Optional[Any] = None,
    interactive: bool = False,
    docker_project_root: Optional[Path] = None,
    restart_backend_fn: Callable[[Path, dict], None] = restart_backend,
    wait_for_health_fn: Callable[..., None] = wait_for_health,
    bank_generation_cost_usd: float = 0.0,
    judge_cost_per_call: float = JUDGE_COST_PER_CALL_USD,
) -> RunOutcome:
    """
    Run the full harness skeleton against `client` and return a RunOutcome.

    `client` is injected so tests can pass a fake/stub BackendClient without
    a real Docker stack. probes is the pre-loaded probe battery (ordered
    {probe_id, prompt_text} entries from load_probe_battery()) — M21: probe
    turns run through POST /admin/run-probe (read-only retrieval, records a
    probe_results row, never mutates the graph), scheduled deterministically
    by assign_probe_schedule(). A plan whose resolved sequence contains
    probe turns hard-stops before any graph mutation if no battery is
    loaded — silently running an experiment without its measurement
    instrument is never acceptable.

    judge_client (M19 Phase 2) is an optional injected OllamaJudgeClient (or
    fake). When None, no judging happens — this keeps every Phase 0/1 test
    and call site working unchanged. When present and a prompt bank is
    loaded, every successfully-completed bulk turn is judged and the verdict
    is posted to the backend; interactive=True additionally prompts to
    accept or override each verdict (calibration mode — see --interactive).

    docker_project_root (M19 Phase 3) opts into restarting the backend
    container with a per-condition EXPERIMENT_ID before each condition's
    wipe/seed — required for real multi-condition runs, since
    backend/core/orchestrator.py resolves EXPERIMENT_ID once at process
    startup with no per-request override. None (the default) skips this
    entirely, which is what every dry-run/unit-test call site wants.

    bank_generation_cost_usd is the known/estimated Sonnet cost already
    incurred (or about to be incurred) for this run's prompt bank — 0.0
    when an existing bank is reused. Combined with a judge-call estimate
    and checked against plan.budget_cap before any graph mutation.

    experiment_id/series_id collision note: run_id/experiment_id are
    derived from plan_id (+ an "_attemptN" suffix beyond the first attempt
    at a given identity). Accepting a resume of an incomplete run keeps
    attempt=1's identity; declining a resume, or invoking execute_run()
    again for a plan that already completed, bumps the attempt number
    instead of silently reusing an identity that already has real data —
    wipe_graph() never deletes analytics/feedback_events, so reuse would
    otherwise mix an old trial's rows into a new one's experiment_id.
    """
    probes = probes or []
    config_hash = compute_config_hash(plan)

    # --- Resume / attempt-identity detection ---
    # A checkpoint counts as "incomplete" (and therefore resumable) if it
    # was never finished OR it was previously aborted — only a "complete"
    # checkpoint has nothing left to do. Re-invoking execute_run() for the
    # exact same plan after it already completed (or after the user
    # declines to resume an incomplete one) must NOT reuse that run_id:
    # wipe_graph() never deletes analytics/feedback_events, so a fresh
    # checkpoint at the same run_id/experiment_id would silently mix a new
    # trial's data with the old run's. Each attempt beyond the first gets
    # its own run_id/experiment_id (compute_run_id / run_identity_for) —
    # attempt 1 is byte-for-byte the original convention, so this is fully
    # backward compatible for a plan that only ever runs once.
    attempt = 1
    resume = False
    existing: Optional[CheckpointState] = None
    while True:
        run_id = compute_run_id(plan, config_hash, attempt)
        existing = load_checkpoint(run_id, run_base_dir)
        if existing is None:
            break
        if existing.final_status != "complete":
            print_fn(
                f"Found incomplete checkpoint for run_id={run_id} with "
                f"{len(existing.turns)} turn(s) already recorded "
                f"(final_status={existing.final_status!r})."
            )
            answer = resume_input_fn("Resume this run? (y/n) [y]: ").strip().lower()
            resume = answer in ("", "y", "yes")
            if resume:
                break
            print_fn(
                f"Starting fresh under a new run identity — run_id={run_id}'s data "
                f"is preserved, not overwritten."
            )
            attempt += 1
            continue
        print_fn(
            f"run_id={run_id} already completed — starting a new attempt under a "
            f"distinct identity so its data is never mixed with the completed run."
        )
        attempt += 1

    run_identity = run_identity_for(plan, attempt)
    print_fn(f"Plan loaded: plan_id={plan.plan_id!r} config_hash={config_hash} run_id={run_id!r}")
    logger.info("runner: run_id=%s config_hash=%s attempt=%d", run_id, config_hash, attempt)

    full_sequence = resolve_turn_sequence(plan)

    # M19 Phase 1 — preflight: load the prompt bank (if the plan names one)
    # and assign prompt_id to every bulk turn before the manifest is written,
    # so a resumed run always replays identical prompts. A missing/invalid
    # bank is deferred to the error-policy check below rather than raised
    # here, so the manifest/checkpoint are still written for the aborted run.
    prompt_bank: Optional[PromptBank] = None
    prompt_bank_error: Optional[str] = None
    if plan.prompt_bank_path:
        try:
            prompt_bank = load_bank(Path(plan.prompt_bank_path))
        except (FileNotFoundError, ValueError) as exc:
            prompt_bank_error = f"connectivity: prompt bank invalid or missing: {exc}"
        else:
            full_sequence = assign_prompt_ids(
                full_sequence,
                [e.prompt_id for e in prompt_bank.validated_entries()],
                plan.random_seed,
            )

    # M21 — stamp probe_id + measurement_session_index onto every probe turn
    # before the manifest is written, so a resumed run always replays the
    # identical probe schedule (same guarantee assign_prompt_ids gives bulk
    # turns). Purely positional and deterministic — no RNG involved.
    full_sequence = assign_probe_schedule(
        full_sequence, [p["probe_id"] for p in probes]
    )

    # M19 Phase 3 — deterministic series_id per (condition, series_index),
    # e.g. "{run_identity}_{condition}_s{series_index}". Covers every
    # condition in the plan (not just what a scoped dry-run executes) so
    # the manifest is always a complete map, matching
    # resolved_turn_sequence's use of full_sequence rather than the scoped
    # one.
    series_id_map = {
        f"{condition}:{series_index}": f"{run_identity}_{condition}_s{series_index}"
        for condition in plan.condition_order
        for series_index in range(plan.series_count)
    }

    manifest = RunManifest(
        run_id=run_id,
        plan=plan.model_dump(mode="json"),
        config_hash=config_hash,
        resolved_turn_sequence=full_sequence,
        series_id_map=series_id_map,
    )
    manifest_path = save_manifest(manifest, run_base_dir)
    print_fn(f"Manifest saved: {manifest_path}")

    checkpoint = existing if resume else CheckpointState(run_id=run_id)

    # M19 Phase 3 — snapshot, before this invocation processes a single new
    # turn, which (condition, series_index) pairs already have at least one
    # turn recorded. Used below to tell "this series is brand new" from
    # "this series is being resumed mid-flight" — resuming must never wipe
    # the graph or re-randomize edges out from under a series that's
    # already in progress, since that would erase the experimental context
    # (retrieved MUs, reinforced edges, the random_weight baseline) that
    # the remaining turns are supposed to continue building on.
    series_previously_started = {(t.condition, t.series_index) for t in checkpoint.turns}

    error_policy = resolve_error_policy(plan)
    conditions, scoped_sequence = scope_for_dry_run(plan, plan.condition_order, full_sequence)

    error_summary: Optional[str] = None
    aborted = False
    turns_processed = 0

    prompt_lookup = (
        {e.prompt_id: e.prompt_text for e in prompt_bank.validated_entries()} if prompt_bank else None
    )
    # M19 Phase 2 — full entry lookup (prompt_text + criteria) for judging;
    # prompt_lookup above stays text-only since build_turn_message never
    # needs criteria.
    entry_by_prompt_id = (
        {e.prompt_id: e for e in prompt_bank.validated_entries()} if prompt_bank else None
    )

    # M19 Phase 3 — pre-run budget gate, the very first check, before any
    # graph mutation or judge preflight (see the Phase 3 run-lifecycle
    # diagram in BUILD-PLAN.md). n_judge_calls only counts bulk turns —
    # probes are never judged — across every condition actually scoped to
    # run (respects dry_run's single-condition scoping).
    n_judge_calls = (
        len(conditions) * sum(1 for t in scoped_sequence if t["turn_type"] != TURN_TYPE_PROBE)
        if judge_client is not None else 0
    )
    prerun_estimate = estimate_prerun_cost(bank_generation_cost_usd, n_judge_calls, judge_cost_per_call)
    print_fn(
        f"Pre-run cost estimate: bank_generation=${prerun_estimate['bank_generation_cost_usd']:.4f} "
        f"judge_calls=${prerun_estimate['judge_calls_cost_usd']:.4f} "
        f"total=${prerun_estimate['estimated_total_cost_usd']:.4f} budget_cap={plan.budget_cap}"
    )
    cost_tracker = CostTracker(budget_cap=plan.budget_cap, cumulative_cost_usd=bank_generation_cost_usd)

    try:
        try:
            check_prerun_budget(prerun_estimate, plan.budget_cap)
        except BudgetExceededError as exc:
            error_summary = f"budget_cap: {exc}"
            raise HardStopError(error_summary) from exc

        if prompt_bank_error and error_policy.get("connectivity") == "hard_stop":
            error_summary = prompt_bank_error
            raise HardStopError(error_summary)
        elif prompt_bank_error:
            logger.warning("runner: %s (soft_continue per error_policy)", prompt_bank_error)

        # M21 — probe battery preflight. Unconditional hard stop (not subject
        # to error_policy override), before any graph mutation: executing a
        # plan's probe turns as anything other than real probe measurements
        # would silently produce an experiment with no learning data — the
        # exact gap this milestone closes.
        if not probes and any(t["turn_type"] == TURN_TYPE_PROBE for t in scoped_sequence):
            error_summary = (
                f"probe battery required: the resolved sequence contains probe "
                f"turns but no probe set was loaded (probe_set_version="
                f"{plan.probe_set_version!r})"
            )
            raise HardStopError(error_summary)

        try:
            client.health()
        except BackendConnectivityError as exc:
            error_summary = f"connectivity: {exc}"
            raise HardStopError(error_summary) from exc

        # M19 Phase 2 — judge availability preflight. Runs before any graph
        # mutation (ahead of the wipe/seed loop below); a missing judge model
        # is an unconditional hard stop, not subject to error_policy override.
        if judge_client is not None and prompt_bank is not None:
            if not judge_client.model_available(plan.judge.model):
                error_summary = f"judge model unavailable: {plan.judge.model!r} not found on Ollama"
                raise HardStopError(error_summary)

        for condition in conditions:
            condition_turn_done_flags = [
                is_turn_done(checkpoint, condition, t["series_index"], t["turn_index"])
                for t in scoped_sequence
            ]
            # M19 Phase 3 — resume optimization: if every turn for this
            # condition is already recorded in the checkpoint, skip the
            # restart/wipe/seed entirely rather than churning the graph for
            # a condition with nothing left to do. Not just a speed-up: it
            # also means a resumed run never re-wipes a condition whose
            # series report has already been generated (wipe_graph() itself
            # never touches analytics/feedback_events, so no data was ever
            # at risk here — this only removes wasted work).
            if condition_turn_done_flags and all(condition_turn_done_flags):
                print_fn(f"\n=== Condition: {condition} — already complete, skipping ===")
                continue

            # A condition with SOME (but not all) turns already done is
            # being resumed mid-flight: wipe_graph()/seed_graph() must be
            # skipped so the remaining turns see the same graph state
            # (retrieved MUs, reinforced edges) the completed turns left
            # behind, not a freshly reset one. The backend still gets
            # restarted (if configured) so EXPERIMENT_ID is correct for the
            # remaining turns' analytics rows — that restart never touches
            # the graph itself (bind-mounted volume, untouched by a
            # container recreate).
            resuming_condition = any(condition_turn_done_flags)

            print_fn(f"\n=== Condition: {condition} ===")
            experiment_id = f"{run_identity}_{condition}"

            # M19 Phase 3 — restart the backend with this condition's
            # EXPERIMENT_ID before touching the graph. Unconditional hard
            # stop (not subject to error_policy override) — nothing
            # downstream is trustworthy if the restart itself fails.
            # docker_project_root=None (the default) skips this entirely.
            # M20 Phase 1 — RUN_CONDITION rides alongside EXPERIMENT_ID so
            # the backend's effective_weight() dispatch (learned vs.
            # static_weight vs. random_weight) matches the condition
            # actually being run; the backend has no other way to know.
            # M21 — CONDITION_LABEL too: it stamps every analytics row, and
            # the experiment report resolves an experiment's condition from
            # those rows to label/filter probe data — without it, all three
            # conditions inherit config.yaml's single default label.
            if docker_project_root is not None:
                try:
                    restart_backend_fn(
                        docker_project_root,
                        {
                            "EXPERIMENT_ID": experiment_id,
                            "RUN_CONDITION": condition,
                            "CONDITION_LABEL": condition,
                        },
                    )
                    wait_for_health_fn(client)
                except DockerLifecycleError as exc:
                    error_summary = f"docker restart failed for condition {condition}: {exc}"
                    raise HardStopError(error_summary) from exc
                print_fn(f"Backend restarted for condition={condition} EXPERIMENT_ID={experiment_id}")

            if resuming_condition:
                print_fn(
                    f"Resuming condition={condition} mid-flight — wipe/seed skipped so "
                    f"the graph context built by already-completed turns is preserved."
                )
            else:
                try:
                    wipe_result = client.wipe_graph(experiment_id=experiment_id)
                    seed_result = client.seed_graph(version=plan.seed_fixture_version)
                    stats = client.graph_stats()
                except BackendConnectivityError as exc:
                    error_summary = f"connectivity during wipe/seed for condition {condition}: {exc}"
                    raise HardStopError(error_summary) from exc

                print_fn(
                    f"Wiped graph for condition={condition}: "
                    f"mus_deleted={wipe_result.get('mus_deleted')} "
                    f"edges_deleted={wipe_result.get('edges_deleted')} "
                    f"archive={wipe_result.get('archive_path')}"
                )
                print_fn(
                    f"Seeded graph for condition={condition}: "
                    f"mu_count={seed_result.get('mu_count')} stats_mu_count={stats.get('mu_count')}"
                )

            # M19 Phase 3 — random_weight control: re-randomize edge weights
            # once at the start of each series. series_previously_started
            # (snapshotted before this invocation's own turns) is what
            # actually prevents re-randomizing a series resumed mid-flight —
            # randomized_series_this_condition only dedupes within this
            # same loop pass, it doesn't know about prior invocations.
            randomized_series_this_condition: set = set()

            for turn in scoped_sequence:
                key = (condition, turn["series_index"], turn["turn_index"])
                if is_turn_done(checkpoint, *key):
                    continue

                if (
                    condition == "random_weight"
                    and turn["series_index"] not in randomized_series_this_condition
                ):
                    randomized_series_this_condition.add(turn["series_index"])
                    if (condition, turn["series_index"]) in series_previously_started:
                        print_fn(
                            f"  Skipping edge randomization for condition={condition} "
                            f"series_index={turn['series_index']} — series already in "
                            f"progress from a prior session."
                        )
                    else:
                        try:
                            randomize_result = client.randomize_edges()
                            print_fn(
                                f"  Randomized edge weights for condition={condition} "
                                f"series_index={turn['series_index']}: "
                                f"edges_randomized={randomize_result.get('edges_randomized')}"
                            )
                        except BackendConnectivityError as exc:
                            if error_policy.get("connectivity") == "hard_stop":
                                error_summary = (
                                    f"connectivity during edge randomization for condition="
                                    f"{condition} series_index={turn['series_index']}: {exc}"
                                )
                                raise HardStopError(error_summary) from exc
                            print_fn(f"  edge randomization failed (soft-continue): {exc}")

                series_id = series_id_map[f"{condition}:{turn['series_index']}"]
                status = STATUS_OK
                detail = None
                response = None
                if turn["turn_type"] == TURN_TYPE_PROBE:
                    # M21 — probe turns are real measurements: read-only
                    # retrieval via POST /admin/run-probe, recording a
                    # probe_results row. Never /chat (which would create MUs
                    # and edges), never judged (guarded again below).
                    try:
                        probe_response = client.run_probe(
                            probe_id=turn["probe_id"],
                            measurement_session_index=turn["measurement_session_index"],
                            condition_label=condition,
                            probe_set_version=plan.probe_set_version,
                        )
                        detail = probe_response.get("result_id")
                    except BackendConnectivityError as exc:
                        if error_policy.get("connectivity") == "hard_stop":
                            error_summary = f"connectivity during probe turn {key}: {exc}"
                            raise HardStopError(error_summary) from exc
                        status, detail = STATUS_SOFT_CONTINUE, str(exc)
                    except BackendTurnError as exc:
                        if error_policy.get("turn_ambiguous") == "hard_stop":
                            error_summary = f"probe turn error {key}: {exc}"
                            raise HardStopError(error_summary) from exc
                        status, detail = STATUS_SOFT_CONTINUE, str(exc)
                else:
                    message = build_turn_message(turn, condition, prompt_lookup)
                    try:
                        response = client.chat(message, series_id=series_id)
                        detail = response.get("run_id")
                    except BackendConnectivityError as exc:
                        if error_policy.get("connectivity") == "hard_stop":
                            error_summary = f"connectivity during turn {key}: {exc}"
                            raise HardStopError(error_summary) from exc
                        status, detail = STATUS_SOFT_CONTINUE, str(exc)
                    except BackendTurnError as exc:
                        if error_policy.get("turn_ambiguous") == "hard_stop":
                            error_summary = f"turn error {key}: {exc}"
                            raise HardStopError(error_summary) from exc
                        status, detail = STATUS_SOFT_CONTINUE, str(exc)

                # M19 Phase 2 — judge the response and post the verdict.
                # Bulk turns only (guard #1 — probes are never judged); the
                # judge module itself refuses probe turns too (guard #2).
                verdict_str: Optional[str] = None
                feedback_event_id: Optional[str] = None
                judge_error: Optional[str] = None
                if (
                    status == STATUS_OK
                    and turn["turn_type"] != TURN_TYPE_PROBE
                    and judge_client is not None
                    and entry_by_prompt_id is not None
                ):
                    prompt_entry = entry_by_prompt_id.get(turn.get("prompt_id"))
                    if prompt_entry is not None:
                        try:
                            verdict_str, feedback_event_id, judge_error = _judge_and_post_verdict(
                                client, judge_client, plan, condition, detail, prompt_entry,
                                (response or {}).get("response", ""), turn["turn_type"],
                                error_policy, interactive, resume_input_fn, print_fn,
                            )
                        except HardStopError as exc:
                            # judge_unparseable hard_stop — the helper already
                            # composed the message; just surface it here.
                            error_summary = str(exc)
                            raise
                        except BackendConnectivityError as exc:
                            if error_policy.get("connectivity") == "hard_stop":
                                error_summary = f"connectivity during verdict post {key}: {exc}"
                                raise HardStopError(error_summary) from exc
                            judge_error = f"verdict post connectivity error: {exc}"
                        except BackendTurnError as exc:
                            if error_policy.get("turn_ambiguous") == "hard_stop":
                                error_summary = f"verdict post turn error {key}: {exc}"
                                raise HardStopError(error_summary) from exc
                            judge_error = f"verdict post turn error: {exc}"

                        # M19 Phase 3 — a judge call was made regardless of
                        # outcome (verdict, unparseable, or a soft-continued
                        # posting error); local Ollama costs $0/call today
                        # but the ledger entry + mid-run budget check are
                        # real, so a hosted judge later is a config change.
                        # Only reached when the call above didn't hard-stop.
                        cost_tracker.add_cost("judge_call", judge_cost_per_call)
                        try:
                            cost_tracker.check_budget(f"mid-run after turn {key}")
                        except BudgetExceededError as exc:
                            error_summary = f"budget_cap: {exc}"
                            raise HardStopError(error_summary) from exc

                checkpoint.turns.append(TurnCheckpoint(
                    condition=condition,
                    series_index=turn["series_index"],
                    turn_index=turn["turn_index"],
                    turn_type=turn["turn_type"],
                    status=status,
                    detail=detail,
                    verdict=verdict_str,
                    feedback_event_id=feedback_event_id,
                    judge_error=judge_error,
                    probe_id=turn.get("probe_id"),
                    measurement_session_index=turn.get("measurement_session_index"),
                ))
                save_checkpoint(checkpoint, run_base_dir)
                turns_processed += 1
                verdict_note = f" verdict={verdict_str}" if verdict_str else ""
                probe_note = (
                    f" probe={turn['probe_id']} session={turn['measurement_session_index']}"
                    if turn["turn_type"] == TURN_TYPE_PROBE else ""
                )
                print_fn(f"  turn {key} type={turn['turn_type']} -> {status}{verdict_note}{probe_note}")

                # M19 Phase 3 — series-close reporting. "Complete" is judged
                # against the plan's true turns_per_series boundary, not the
                # dry-run-scoped sequence, so a short smoke dry_run_n never
                # fires this prematurely; a dry run spanning full series
                # legitimately does (verification protocol item 1).
                if turn["turn_index"] == plan.turns_per_series - 1:
                    already_reported = any(
                        r.condition == condition and r.series_index == turn["series_index"]
                        for r in checkpoint.series_reports
                    )
                    if not already_reported:
                        try:
                            report_record = _generate_series_report(
                                client, error_policy, print_fn,
                                condition, turn["series_index"], experiment_id, series_id,
                            )
                        except HardStopError as exc:
                            error_summary = str(exc)
                            raise
                        checkpoint.series_reports.append(report_record)
                        save_checkpoint(checkpoint, run_base_dir)

        # M19 Phase 3 — experiment-close reporting. Only meaningful once the
        # learned condition has actually run this session; skips cleanly
        # (no call, no error) when scope_for_dry_run limited execution to a
        # control-only condition list. control_experiment_ids is assembled
        # deterministically from the manifest's own naming convention, never
        # asked of the user.
        if "learned" in conditions:
            learned_experiment_id = f"{run_identity}_learned"
            control_experiment_ids = {
                c: f"{run_identity}_{c}" for c in conditions if c != "learned"
            } or None
            try:
                experiment_report_record = _generate_experiment_report(
                    client, error_policy, print_fn, learned_experiment_id, control_experiment_ids,
                )
            except HardStopError as exc:
                error_summary = str(exc)
                raise
            checkpoint.experiment_report = experiment_report_record
            save_checkpoint(checkpoint, run_base_dir)

    except HardStopError:
        aborted = True

    checkpoint.final_status = "aborted" if aborted else "complete"
    save_checkpoint(checkpoint, run_base_dir)

    # M19 Phase 3 — the experiment-close report's md_path becomes report_path
    # in the notification payload, exactly the field the Phase 0 stub
    # already defined. None when aborted, skipped, or itself soft-continued.
    report_path = None
    if checkpoint.experiment_report is not None and checkpoint.experiment_report.status == STATUS_OK:
        report_path = checkpoint.experiment_report.md_path

    payload = build_notification_payload(
        run_id=run_id,
        status=checkpoint.final_status,
        error_summary=error_summary,
        report_path=report_path,
    )
    notification_log_path = notify(
        "harness_run_aborted" if aborted else "harness_run_complete", payload, run_base_dir,
        channel=plan.notification.channel,
    )

    if aborted:
        print_fn(f"\nABORTED: {error_summary}")
    else:
        print_fn(f"\nCOMPLETE: {turns_processed} turn(s) processed.")

    return RunOutcome(
        run_id=run_id,
        status=checkpoint.final_status,
        error_summary=error_summary,
        checkpoint=checkpoint,
        manifest_path=manifest_path,
        notification_log_path=notification_log_path,
        turns_processed=turns_processed,
    )
