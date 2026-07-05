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
    TurnCheckpoint,
    is_turn_done,
    load_checkpoint,
    save_checkpoint,
)
from harness.client import BackendClient, BackendConnectivityError, BackendTurnError
from harness.judge import JudgeUnparseableError, judge_turn
from harness.manifest import RunManifest, compute_config_hash, compute_run_id, now_iso, save_manifest
from harness.notify import build_notification_payload, notify
from harness.prompt_bank import PromptBank, PromptBankEntry, load_bank
from harness.schema import DEFAULT_ERROR_POLICY, RunPlan
from harness.turn_resolution import TURN_TYPE_PROBE, assign_prompt_ids, resolve_turn_sequence

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


def load_probe_prompts(probe_set_path: Path) -> list:
    """
    Load {probe_id, prompt_text} pairs from a probe_set_*.yaml file.

    Standalone parse (no backend import) — mirrors the existing convention
    of scripts/generate_seed_v2.py and scripts/verify_seed_v2.py, which read
    fixture/probe files directly rather than importing backend internals.
    Used only as placeholder turn content in Phase 0 (real bulk-prompt
    generation is Phase 1).
    """
    if not probe_set_path.exists():
        logger.warning("runner: probe set not found at %s — no placeholder content available", probe_set_path)
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


def build_turn_message(probes: list, turn: dict, condition: str, prompt_lookup: Optional[dict] = None) -> str:
    """
    Resolve message content for a turn: probe prompt, a real bank prompt
    (via turn["prompt_id"] -> prompt_lookup), or — with no bank loaded — the
    Phase 0 labeled placeholder stub.
    """
    if turn["turn_type"] == TURN_TYPE_PROBE and probes:
        probe = probes[turn["turn_index"] % len(probes)]
        return probe["prompt_text"]
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
) -> RunOutcome:
    """
    Run the full harness skeleton against `client` and return a RunOutcome.

    `client` is injected so tests can pass a fake/stub BackendClient without
    a real Docker stack. probes is an optional pre-loaded list of
    {probe_id, prompt_text} used as placeholder content for probe turns.

    judge_client (M19 Phase 2) is an optional injected OllamaJudgeClient (or
    fake). When None, no judging happens — this keeps every Phase 0/1 test
    and call site working unchanged. When present and a prompt bank is
    loaded, every successfully-completed bulk turn is judged and the verdict
    is posted to the backend; interactive=True additionally prompts to
    accept or override each verdict (calibration mode — see --interactive).
    """
    probes = probes or []
    config_hash = compute_config_hash(plan)
    run_id = compute_run_id(plan, config_hash)
    print_fn(f"Plan loaded: plan_id={plan.plan_id!r} config_hash={config_hash}")
    logger.info("runner: run_id=%s config_hash=%s", run_id, config_hash)

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

    manifest = RunManifest(
        run_id=run_id,
        plan=plan.model_dump(mode="json"),
        config_hash=config_hash,
        resolved_turn_sequence=full_sequence,
    )
    manifest_path = save_manifest(manifest, run_base_dir)
    print_fn(f"Manifest saved: {manifest_path}")

    # --- Resume detection ---
    # A run counts as "incomplete" (and therefore resumable) if it was never
    # finished OR it was previously aborted — only a "complete" run has
    # nothing left to do.
    existing = load_checkpoint(run_id, run_base_dir)
    resume = False
    if existing is not None and existing.final_status != "complete":
        print_fn(
            f"Found incomplete checkpoint for run_id={run_id} with "
            f"{len(existing.turns)} turn(s) already recorded "
            f"(final_status={existing.final_status!r})."
        )
        answer = resume_input_fn("Resume this run? (y/n) [y]: ").strip().lower()
        resume = answer in ("", "y", "yes")
        if not resume:
            print_fn("Starting fresh — previous checkpoint will be overwritten.")

    checkpoint = existing if (existing is not None and resume) else CheckpointState(run_id=run_id)

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

    try:
        if prompt_bank_error and error_policy.get("connectivity") == "hard_stop":
            error_summary = prompt_bank_error
            raise HardStopError(error_summary)
        elif prompt_bank_error:
            logger.warning("runner: %s (soft_continue per error_policy)", prompt_bank_error)

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
            print_fn(f"\n=== Condition: {condition} ===")
            try:
                wipe_result = client.wipe_graph(experiment_id=f"{plan.plan_id}_{condition}")
                seed_result = client.seed_graph(version="v2")
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

            for turn in scoped_sequence:
                key = (condition, turn["series_index"], turn["turn_index"])
                if is_turn_done(checkpoint, *key):
                    continue

                message = build_turn_message(probes, turn, condition, prompt_lookup)
                status = STATUS_OK
                detail = None
                response = None
                try:
                    response = client.chat(message)
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
                ))
                save_checkpoint(checkpoint, run_base_dir)
                turns_processed += 1
                verdict_note = f" verdict={verdict_str}" if verdict_str else ""
                print_fn(f"  turn {key} type={turn['turn_type']} -> {status}{verdict_note}")

    except HardStopError:
        aborted = True

    checkpoint.final_status = "aborted" if aborted else "complete"
    save_checkpoint(checkpoint, run_base_dir)

    payload = build_notification_payload(
        run_id=run_id,
        status=checkpoint.final_status,
        error_summary=error_summary,
        report_path=None,
    )
    notification_log_path = notify(
        "harness_run_aborted" if aborted else "harness_run_complete", payload, run_base_dir
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
