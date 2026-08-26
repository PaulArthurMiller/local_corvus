"""
Sequential Batch Queue Runner — schema, manifest/checkpoint persistence, and
the full preflight protocol (Milestone 26).

The queue is a "playlist player": an ordered list of run-plan files fed one
at a time through harness.runner.execute_run(), unattended overnight. This
module contains no execution logic (that lands in M27's queue_runner.py) —
only the queue file schema, the resolved-launch artifacts (manifest +
checkpoint), and the preflight protocol that must pass in full before a
single turn of a single plan is ever run.

See BUILD-PLAN.md "Sequential Batch Queue Runner — Milestones 26-27" for
the full design record (open-question resolutions, the runner.py
attempt-resolution correction, and the layer-boundary rationale).
"""

import hashlib
import json
import logging
import os
import shutil
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Optional

from pydantic import BaseModel, Field, ValidationError, field_validator

from harness.client import BackendConnectivityError
from harness.config_assert import (
    find_config_mismatches,
    find_dotted_key_collisions,
    find_unresolvable_override_keys,
    format_config_mismatches,
    format_dotted_key_collisions,
    format_unresolvable_override_keys,
)
from harness.cost import JUDGE_COST_PER_CALL_USD, estimate_prerun_cost
from harness.manifest import compute_config_hash, compute_run_id, now_iso
from harness.prompt_bank import bank_path_for_hash, compute_bank_hash, load_bank
from harness.prompt_gen import (
    compute_fixture_manifest_hash,
    estimate_bank_cost_for_plan,
    fixture_path_for_version,
    generation_params_for_plan,
)
from harness.schema import NotificationConfig, RunPlan, format_validation_error
from harness.turn_resolution import TURN_TYPE_PROBE, resolve_turn_sequence

logger = logging.getLogger("harness.queue")

VALID_ON_PLAN_FAILURE = ("continue", "halt")

# Archive-size prediction is guesswork (see BUILD-PLAN.md open question #3),
# so this is a WARN threshold, not a hard stop. 5 GiB is a conservative
# floor for an overnight run's worth of wipe-archive snapshots.
DISK_SPACE_WARN_THRESHOLD_BYTES = 5 * 1024**3

STATUS_PENDING = "pending"
STATUS_RUNNING = "running"
STATUS_COMPLETE = "complete"
STATUS_FAILED = "failed"
STATUS_SKIPPED = "skipped"

MANIFEST_STATUS_RESOLVED = "resolved"
MANIFEST_STATUS_SKIPPED_DISABLED = "skipped_disabled"


class QueuePreflightError(Exception):
    """Raised on any preflight hard stop. Message is plan-identifying —
    never a raw stack trace. Every check in run_preflight() runs before any
    graph mutation or backend write."""


# ---------------------------------------------------------------------------
# Queue file schema
# ---------------------------------------------------------------------------

class QueuePlanEntry(BaseModel):
    """One line item in a queue file. `enabled` defaults True; disabled
    entries are recorded in the manifest but never validated or executed —
    the enabled:false flag substitutes for YAML's comment-out idiom."""

    plan_path: str = Field(min_length=1)
    enabled: bool = True


class QueueFile(BaseModel):
    """Full queue file schema. See BUILD-PLAN.md "Queue file schema" for
    the worked example. Queue settings (on_plan_failure, max_total_budget_usd)
    live here only, never in config.yaml — they are per-queue operational
    choices, not experimental knobs under study."""

    queue_id: str = Field(min_length=1)
    on_plan_failure: str = "continue"
    max_total_budget_usd: Optional[float] = Field(default=None, ge=0.0)
    notification: NotificationConfig = Field(default_factory=NotificationConfig)
    plans: list[QueuePlanEntry] = Field(min_length=1)

    @field_validator("on_plan_failure")
    @classmethod
    def _validate_on_plan_failure(cls, v: str) -> str:
        if v not in VALID_ON_PLAN_FAILURE:
            raise ValueError(
                f"on_plan_failure must be one of {VALID_ON_PLAN_FAILURE}, got {v!r}"
            )
        return v


def load_queue_file(path: Path) -> QueueFile:
    """Load and validate a queue file. Raises FileNotFoundError or ValueError
    with a specific, field-identifying message — mirrors harness.prompt_bank
    .load_bank()'s convention so the CLI never surfaces a raw stack trace."""
    path = Path(path)
    if not path.exists():
        raise FileNotFoundError(f"queue file not found: {path}")

    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ValueError(f"queue file is not valid JSON: {path}: {exc}") from exc

    try:
        return QueueFile(**raw)
    except ValidationError as exc:
        raise ValueError(
            f"queue file validation failed for {path}:\n{format_validation_error(exc)}"
        ) from exc


def resolve_plan_path(queue_file_path: Path, plan_path: str) -> Path:
    """plan_path resolves relative to the queue file's own directory when
    not absolute — machine-writer friendly: the Karpathy loop writes both
    the plan and the queue entry without knowing the invoker's CWD."""
    p = Path(plan_path)
    if p.is_absolute():
        return p
    return (Path(queue_file_path).parent / p).resolve()


# ---------------------------------------------------------------------------
# Manifest — resolved queue: plan list, hashes, expected run_ids, preflight
# ---------------------------------------------------------------------------

class QueuePlanManifestEntry(BaseModel):
    plan_path: str
    enabled: bool
    status: str  # "resolved" | "skipped_disabled"
    plan_id: Optional[str] = None
    config_hash: Optional[str] = None
    expected_run_id: Optional[str] = None
    bank_path: Optional[str] = None
    conditions: Optional[list[str]] = None
    # M43 Phase 1 — the backend's runtime_config_hash at preflight time (the
    # single GET /admin/effective-config call step 5 makes, shared across
    # every plan in the queue — see run_preflight()'s step 5 comment for why
    # a plan with config_overrides doesn't get its expected_config enforced
    # here). None for a disabled plan, or if preflight never reached step 5.
    runtime_config_hash: Optional[str] = None
    # The full resolved config behind that hash, recorded per plan so the
    # queue manifest alone answers "what was the backend actually running
    # when this queue was resolved?" without opening each plan's own run
    # manifest. Same value for every plan in one launch (there is exactly
    # one live backend at preflight time) — stored per entry anyway so an
    # entry stays self-describing if a future preflight ever resolves plans
    # against different backends.
    resolved_config_snapshot: Optional[dict] = None


class QueueManifest(BaseModel):
    queue_id: str
    queue_file_path: str
    queue_content_hash: str
    created_at: str = Field(default_factory=now_iso)
    plans: list[QueuePlanManifestEntry] = Field(default_factory=list)
    preflight: dict = Field(default_factory=dict)


def compute_queue_content_hash(queue_file: QueueFile) -> str:
    """Stable sha256 of the queue file's resolved content — M27's resume
    auto-detect guard compares this against a candidate directory's stored
    hash before deciding to resume it."""
    payload = queue_file.model_dump(mode="json")
    return hashlib.sha256(json.dumps(payload, sort_keys=True).encode("utf-8")).hexdigest()


def queue_manifest_path(launch_dir: Path) -> Path:
    return launch_dir / "queue_manifest.json"


def save_queue_manifest(manifest: QueueManifest, launch_dir: Path) -> Path:
    """Atomic write: temp file in the same directory, then os.replace —
    same pattern as harness.checkpoint.save_checkpoint()."""
    path = queue_manifest_path(launch_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_suffix(".json.tmp")
    tmp_path.write_text(json.dumps(manifest.model_dump(mode="json"), indent=2), encoding="utf-8")
    os.replace(tmp_path, path)
    return path


def load_queue_manifest(launch_dir: Path) -> Optional[QueueManifest]:
    path = queue_manifest_path(launch_dir)
    if not path.exists():
        return None
    data = json.loads(path.read_text(encoding="utf-8"))
    return QueueManifest(**data)


# ---------------------------------------------------------------------------
# Checkpoint — per-plan execution status (M27 writes/reads this during the
# execution loop; M26 only writes the initial all-pending/skipped state)
# ---------------------------------------------------------------------------

class QueuePlanCheckpointEntry(BaseModel):
    plan_path: str
    plan_id: Optional[str] = None
    config_hash: Optional[str] = None
    run_id: Optional[str] = None
    status: str = STATUS_PENDING  # pending | running | complete | failed | skipped
    started_at: Optional[str] = None
    ended_at: Optional[str] = None
    wall_clock_duration_seconds: Optional[float] = None
    per_condition_durations_seconds: dict[str, float] = Field(default_factory=dict)
    error_summary: Optional[str] = None
    # M27 — series/experiment report md_paths generated by this plan's run,
    # for queue_summary.md. Populated from the harness checkpoint's
    # series_reports/experiment_report, whether from a fresh execute_run()
    # call or a crash-reconciled already-complete harness checkpoint.
    report_paths: list[str] = Field(default_factory=list)


class QueueCheckpointState(BaseModel):
    queue_id: str
    plans: list[QueuePlanCheckpointEntry] = Field(default_factory=list)
    # None while the queue is running; "complete" | "aborted" once it ends (M27).
    final_status: Optional[str] = None


def queue_checkpoint_path(launch_dir: Path) -> Path:
    return launch_dir / "queue_checkpoint.json"


def save_queue_checkpoint(state: QueueCheckpointState, launch_dir: Path) -> Path:
    path = queue_checkpoint_path(launch_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_suffix(".json.tmp")
    tmp_path.write_text(json.dumps(state.model_dump(mode="json"), indent=2), encoding="utf-8")
    os.replace(tmp_path, path)
    return path


def load_queue_checkpoint(launch_dir: Path) -> Optional[QueueCheckpointState]:
    path = queue_checkpoint_path(launch_dir)
    if not path.exists():
        return None
    data = json.loads(path.read_text(encoding="utf-8"))
    return QueueCheckpointState(**data)


def launch_dir_name(queue_id: str, launch_ts: Optional[str] = None) -> str:
    ts = launch_ts or datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    return f"{queue_id}_{ts}"


def find_latest_launch_dir(queues_runs_dir: Path, queue_id: str) -> Optional[Path]:
    """
    M27 resume auto-detect: the lexicographically-last `{queue_id}_*`
    directory under queues_runs_dir. launch_dir_name()'s timestamp format
    (%Y%m%dT%H%M%SZ) sorts lexicographically in chronological order, so
    this is the most recent launch of this queue_id. None if the queue has
    never been launched (or queues_runs_dir doesn't exist yet).

    Caller is responsible for the queue-content-hash guard (see
    compute_queue_content_hash) before treating this as a resume target —
    finding a directory is not the same as confirming it matches the
    current queue file.
    """
    if not queues_runs_dir.exists():
        return None
    prefix = f"{queue_id}_"
    candidates = sorted(
        p.name for p in queues_runs_dir.iterdir() if p.is_dir() and p.name.startswith(prefix)
    )
    return (queues_runs_dir / candidates[-1]) if candidates else None


# ---------------------------------------------------------------------------
# Preflight protocol (steps 1-7, BUILD-PLAN.md "Preflight protocol")
# ---------------------------------------------------------------------------

class PreflightResolution(BaseModel):
    """Everything preflight resolved, for the CLI to print and (on a real
    launch) persist as the queue manifest + initial checkpoint."""

    manifest: QueueManifest
    checkpoint: QueueCheckpointState
    projected_archive_count: int
    aggregate_estimated_cost_usd: float
    disk_free_bytes: int
    disk_warn: bool


def _load_and_validate_plan(resolved_path: Path, plan_path_label: str) -> RunPlan:
    if not resolved_path.exists():
        raise QueuePreflightError(f"plan {plan_path_label!r} not found at {resolved_path}")
    try:
        raw = json.loads(resolved_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise QueuePreflightError(f"plan {plan_path_label!r} is not valid JSON: {exc}") from exc
    try:
        return RunPlan(**raw)
    except ValidationError as exc:
        raise QueuePreflightError(
            f"plan {plan_path_label!r} failed validation:\n{format_validation_error(exc)}"
        ) from exc


def _resolve_bank_for_plan(
    plan: RunPlan, plan_path_label: str, fixtures_dir: Path, prompt_banks_dir: Path,
) -> None:
    """Step 4 — mutates plan.prompt_bank_path in place when a cached bank is
    found. Raises QueuePreflightError for every case that would otherwise
    need fresh Sonnet generation or run placeholder turns unattended.

    fixtures_dir is a directory, not a single file — different plans in the
    same queue can request different seed_fixture_version values (M28), so
    the actual fixture path is resolved per plan, not shared across the
    whole queue."""
    fixture_path = fixture_path_for_version(plan.seed_fixture_version, fixtures_dir)

    if plan.prompt_bank_path:
        try:
            load_bank(Path(plan.prompt_bank_path))
        except (FileNotFoundError, ValueError) as exc:
            raise QueuePreflightError(
                f"plan {plan_path_label!r}: prompt_bank_path {plan.prompt_bank_path!r} "
                f"is invalid or missing: {exc}"
            ) from exc
        return

    if plan.conceptual_fields.subjects == "auto":
        if not fixture_path.exists():
            raise QueuePreflightError(
                f"plan {plan_path_label!r}: seed_fixture_version "
                f"{plan.seed_fixture_version!r} has no fixture at {fixture_path} — "
                f"freeze it or fix seed_fixture_version before this plan can run"
            )
        estimate = estimate_bank_cost_for_plan(plan, fixture_path, prompt_banks_dir)
        if estimate is not None:
            raise QueuePreflightError(
                f"plan {plan_path_label!r} needs fresh Sonnet prompt-bank generation "
                f"(estimated ${estimate['estimated_cost_usd']:.4f}) — run --generate-bank "
                f"for this plan interactively first"
            )
        fixture_hash = compute_fixture_manifest_hash(fixture_path)
        params = generation_params_for_plan(plan)
        content_hash = compute_bank_hash(fixture_hash, params)
        plan.prompt_bank_path = str(bank_path_for_hash(content_hash, prompt_banks_dir))
        return

    raise QueuePreflightError(
        f"plan {plan_path_label!r} has neither an existing prompt_bank_path nor "
        f"subjects=='auto' — placeholder-message turns in an unattended overnight "
        f"run are wasted GPU-hours"
    )


def run_preflight(
    queue_file: QueueFile,
    queue_file_path: Path,
    client,
    judge_client,
    fixtures_dir: Path,
    prompt_banks_dir: Path,
    project_root: Path,
    print_fn: Callable[[str], None] = print,
    disk_usage_fn: Callable[[str], "shutil._ntuple_diskusage"] = shutil.disk_usage,
    judge_cost_per_call: float = JUDGE_COST_PER_CALL_USD,
    docker_restart_enabled: bool = True,
) -> PreflightResolution:
    """
    Run the full 7-step preflight protocol. Raises QueuePreflightError on
    the first hard stop encountered, in protocol order — nothing after a
    failed step runs. `client` needs .health() and .effective_config()
    (M43); `judge_client` needs .model_available(model). Both are
    duck-typed exactly like execute_run()'s own injected dependencies, so
    tests can pass fakes.

    docker_restart_enabled mirrors the invoking CLI's --no-docker-restart
    flag (M43): config_overrides can only be delivered by the per-condition
    backend restart, so a queue containing an override-carrying plan is
    unrunnable with restarts disabled and must fail at minute zero rather
    than hours in. Defaults True — the normal invocation, and what every
    pre-M43 test call site assumes.

    judge_cost_per_call defaults to $0 (today's local Ollama judge) —
    injectable, same pattern as execute_run()'s own parameter, so a test
    (or a future hosted-judge config) can exercise the aggregate budget
    hard stop without it being permanently unreachable at $0/call.
    """
    # --- Step 2: every enabled plan loads + validates ---
    # disabled_paths preserves the queue file's original ordering so the
    # final manifest/checkpoint lists (built after step 7) can be assembled
    # in file order rather than "all disabled, then all enabled".
    loaded: list[dict] = []  # {entry, resolved_path, plan, raw_config_hash}
    disabled_paths: list[str] = []

    for entry in queue_file.plans:
        if not entry.enabled:
            disabled_paths.append(entry.plan_path)
            continue

        resolved_path = resolve_plan_path(queue_file_path, entry.plan_path)
        plan = _load_and_validate_plan(resolved_path, entry.plan_path)
        loaded.append({
            "entry": entry,
            "resolved_path": resolved_path,
            "plan": plan,
            "raw_config_hash": compute_config_hash(plan),
        })

    # --- Step 3: duplicate config_hash detection across enabled plans ---
    by_hash: dict = {}
    for item in loaded:
        by_hash.setdefault(item["raw_config_hash"], []).append(item["entry"].plan_path)
    duplicates = {h: paths for h, paths in by_hash.items() if len(paths) > 1}
    if duplicates:
        detail = "; ".join(f"{h[:12]}: {paths}" for h, paths in duplicates.items())
        raise QueuePreflightError(
            f"duplicate config_hash across plans ({detail}) — replicates need "
            f"distinct random_seed values"
        )

    # --- Step 4: per-plan effective-bank resolution ---
    for item in loaded:
        _resolve_bank_for_plan(item["plan"], item["entry"].plan_path, fixtures_dir, prompt_banks_dir)

    # --- Step 5: backend health + judge model availability ---
    try:
        client.health()
    except BackendConnectivityError as exc:
        raise QueuePreflightError(f"backend health check failed: {exc}") from exc

    # M43 Phase 2 — a plan's config_overrides ride the per-condition backend
    # restart, so --no-docker-restart makes an override-carrying plan
    # unrunnable as declared. Caught here at minute zero (the queue layer's
    # whole reason for existing) rather than when execute_run() reaches that
    # plan hours into an overnight queue; runner.py carries the same guard
    # for the single-plan CLI. Ahead of the effective-config call below
    # because it needs no backend at all — a contradiction between the plan
    # and this invocation's own flags, not between the plan and the backend.
    if not docker_restart_enabled:
        override_plans = [
            item["entry"].plan_path for item in loaded if item["plan"].config_overrides
        ]
        if override_plans:
            raise QueuePreflightError(
                f"{len(override_plans)} plan(s) declare config_overrides but the backend "
                f"restart is disabled (--no-docker-restart): {override_plans} — overrides "
                f"ride the per-condition restart as CONFIG_OVERRIDES_JSON, so they would "
                f"never reach the backend and those plans would silently run against "
                f"config.yaml's committed values"
            )

    # M43 Phase 1 — one GET /admin/effective-config call, shared by every
    # loaded plan (there is exactly one live backend at preflight time,
    # before any plan's own execute_run() has restarted it for its own
    # condition/config_overrides). Record runtime_config_hash into every
    # plan's manifest entry unconditionally (provenance), but only ENFORCE
    # expected_config for a plan whose config_overrides is empty — a plan
    # that supplies config_overrides will get its own config established by
    # its own per-condition restart once execute_run() actually starts it,
    # so asserting against *this* backend's current state (which reflects
    # whatever the previous plan/queue left running, not this plan's own
    # config) would false-positive on exactly the scenario Phase 2's
    # cross-config queueing done-criterion requires to work. runner.py's
    # own per-condition check (check_and_record_effective_config) is what
    # actually verifies an override-carrying plan once it runs.
    try:
        effective = client.effective_config()
    except BackendConnectivityError as exc:
        raise QueuePreflightError(f"effective-config check failed: {exc}") from exc
    runtime_config_hash = effective.get("runtime_config_hash")
    live_config = effective.get("config") or {}

    # M43 open question, now closed — a live-config key with a literal "."
    # in its name makes every dotted path ambiguous. Checked once per
    # preflight, and only when some plan actually uses dotted paths.
    if any(item["plan"].expected_config or item["plan"].config_overrides for item in loaded):
        collisions = find_dotted_key_collisions(live_config)
        if collisions:
            raise QueuePreflightError(format_dotted_key_collisions(collisions))

    for item in loaded:
        item["runtime_config_hash"] = runtime_config_hash
        item["resolved_config_snapshot"] = live_config
        plan = item["plan"]
        if plan.expected_config and not plan.config_overrides:
            mismatches = find_config_mismatches(plan.expected_config, live_config)
            if mismatches:
                raise QueuePreflightError(
                    f"plan {item['entry'].plan_path!r}: {format_config_mismatches(mismatches)}"
                )
        # Milestone 47 follow-up — a plan's config_overrides is exempt from
        # the expected_config comparison above (its config only becomes real
        # once its own restart runs), but that left NO preflight check at all
        # for config_overrides itself: a typo'd or renamed dotted key would
        # sail through --dry-run and only surface as a ConfigOverrideError
        # crash hours into an unattended overnight queue. This is a shape
        # check only (does the key resolve), not a value check — see
        # find_unresolvable_override_keys()'s docstring for why a wrong-but-
        # resolvable value is deliberately left unchecked here.
        if plan.config_overrides:
            unresolvable = find_unresolvable_override_keys(plan.config_overrides, live_config)
            if unresolvable:
                raise QueuePreflightError(
                    f"plan {item['entry'].plan_path!r}: {format_unresolvable_override_keys(unresolvable)}"
                )

    distinct_judge_models = sorted({item["plan"].judge.model for item in loaded})
    for model in distinct_judge_models:
        if not judge_client.model_available(model):
            raise QueuePreflightError(f"judge model unavailable: {model!r} not found on Ollama")

    # --- Step 6: aggregate budget check ---
    aggregate_cost = 0.0
    for item in loaded:
        plan = item["plan"]
        full_sequence = resolve_turn_sequence(plan)
        n_judge_calls = len(plan.condition_order) * sum(
            1 for t in full_sequence if t["turn_type"] != TURN_TYPE_PROBE
        )
        # Banks pre-exist by this point (step 4 either confirmed or resolved
        # one) — bank_generation_cost_usd is always 0 here.
        estimate = estimate_prerun_cost(0.0, n_judge_calls, judge_cost_per_call)
        aggregate_cost += estimate["estimated_total_cost_usd"]
    aggregate_cost = round(aggregate_cost, 6)

    if queue_file.max_total_budget_usd is not None and aggregate_cost > queue_file.max_total_budget_usd:
        raise QueuePreflightError(
            f"aggregate estimated cost ${aggregate_cost:.4f} exceeds max_total_budget_usd "
            f"${queue_file.max_total_budget_usd:.4f}"
        )
    print_fn(
        f"Preflight: aggregate estimated cost ${aggregate_cost:.4f} "
        f"(max_total_budget_usd={queue_file.max_total_budget_usd})"
    )

    # --- Step 7: disk-space note (WARN, never a hard stop) ---
    projected_archive_count = sum(len(item["plan"].condition_order) for item in loaded)
    print_fn(
        f"Preflight: projected archive count = {projected_archive_count} "
        f"({len(loaded)} plan(s) x their condition counts)"
    )
    usage = disk_usage_fn(str(project_root))
    disk_warn = usage.free < DISK_SPACE_WARN_THRESHOLD_BYTES
    if disk_warn:
        print_fn(
            f"WARNING: free disk space ({usage.free} bytes) is below the "
            f"{DISK_SPACE_WARN_THRESHOLD_BYTES} byte warning threshold"
        )

    # --- Build final manifest/checkpoint entries (post bank-stamping, so
    # config_hash/expected_run_id match what execute_run() will actually
    # produce in M27), assembled in the queue file's original plan order —
    # not "all disabled, then all enabled" — so the CLI's printed execution
    # order matches what a human wrote in the queue file.
    manifest_by_path: dict = {p: QueuePlanManifestEntry(
        plan_path=p, enabled=False, status=MANIFEST_STATUS_SKIPPED_DISABLED,
    ) for p in disabled_paths}
    checkpoint_by_path: dict = {p: QueuePlanCheckpointEntry(
        plan_path=p, status=STATUS_SKIPPED,
    ) for p in disabled_paths}

    for item in loaded:
        plan = item["plan"]
        plan_path_label = item["entry"].plan_path
        final_hash = compute_config_hash(plan)
        expected_run_id = compute_run_id(plan, final_hash, attempt=1)
        manifest_by_path[plan_path_label] = QueuePlanManifestEntry(
            plan_path=plan_path_label, enabled=True, status=MANIFEST_STATUS_RESOLVED,
            plan_id=plan.plan_id, config_hash=final_hash, expected_run_id=expected_run_id,
            bank_path=plan.prompt_bank_path, conditions=plan.condition_order,
            runtime_config_hash=item.get("runtime_config_hash"),
            resolved_config_snapshot=item.get("resolved_config_snapshot"),
        )
        checkpoint_by_path[plan_path_label] = QueuePlanCheckpointEntry(
            plan_path=plan_path_label, plan_id=plan.plan_id, config_hash=final_hash,
            run_id=expected_run_id, status=STATUS_PENDING,
        )

    manifest_entries = [manifest_by_path[e.plan_path] for e in queue_file.plans]
    checkpoint_entries = [checkpoint_by_path[e.plan_path] for e in queue_file.plans]

    manifest = QueueManifest(
        queue_id=queue_file.queue_id,
        queue_file_path=str(queue_file_path),
        queue_content_hash=compute_queue_content_hash(queue_file),
        plans=manifest_entries,
        preflight={
            "projected_archive_count": projected_archive_count,
            "aggregate_estimated_cost_usd": aggregate_cost,
            "disk_free_bytes": usage.free,
            "disk_warn": disk_warn,
        },
    )
    checkpoint = QueueCheckpointState(queue_id=queue_file.queue_id, plans=checkpoint_entries)

    return PreflightResolution(
        manifest=manifest,
        checkpoint=checkpoint,
        projected_archive_count=projected_archive_count,
        aggregate_estimated_cost_usd=aggregate_cost,
        disk_free_bytes=usage.free,
        disk_warn=disk_warn,
    )
