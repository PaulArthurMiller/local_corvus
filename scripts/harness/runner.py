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
from typing import Callable, Optional

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
from harness.manifest import RunManifest, compute_config_hash, compute_run_id, now_iso, save_manifest
from harness.notify import build_notification_payload, notify
from harness.schema import DEFAULT_ERROR_POLICY, RunPlan
from harness.turn_resolution import TURN_TYPE_PROBE, resolve_turn_sequence

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


def build_turn_message(probes: list, turn: dict, condition: str) -> str:
    """Resolve placeholder content for a turn (probe prompt or a labeled stub)."""
    if turn["turn_type"] == TURN_TYPE_PROBE and probes:
        probe = probes[turn["turn_index"] % len(probes)]
        return probe["prompt_text"]
    return _placeholder_bulk_message(condition, turn)


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
) -> RunOutcome:
    """
    Run the full harness skeleton against `client` and return a RunOutcome.

    `client` is injected so tests can pass a fake/stub BackendClient without
    a real Docker stack. probes is an optional pre-loaded list of
    {probe_id, prompt_text} used as placeholder content for probe turns.
    """
    probes = probes or []
    config_hash = compute_config_hash(plan)
    run_id = compute_run_id(plan, config_hash)
    print_fn(f"Plan loaded: plan_id={plan.plan_id!r} config_hash={config_hash}")
    logger.info("runner: run_id=%s config_hash=%s", run_id, config_hash)

    full_sequence = resolve_turn_sequence(plan)

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

    try:
        try:
            client.health()
        except BackendConnectivityError as exc:
            error_summary = f"connectivity: {exc}"
            raise HardStopError(error_summary) from exc

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

                message = build_turn_message(probes, turn, condition)
                status = STATUS_OK
                detail = None
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

                checkpoint.turns.append(TurnCheckpoint(
                    condition=condition,
                    series_index=turn["series_index"],
                    turn_index=turn["turn_index"],
                    turn_type=turn["turn_type"],
                    status=status,
                    detail=detail,
                ))
                save_checkpoint(checkpoint, run_base_dir)
                turns_processed += 1
                print_fn(f"  turn {key} type={turn['turn_type']} -> {status}")

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
