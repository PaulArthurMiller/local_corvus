"""
Run manifest — the resolved plan + a computed config hash + the resolved
turn sequence, persisted to disk so a run is auditable after the fact.

Mirrors the config-hash pattern already used in backend/core/orchestrator.py
(hashlib.sha256 over a sorted-keys JSON dump), reimplemented locally since
this is a standalone host-side script, not a backend module.
"""

import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from pydantic import BaseModel, Field

from harness.schema import RunPlan


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def compute_config_hash(plan: RunPlan) -> str:
    """Stable sha256 hash of the fully-resolved plan (sorted keys, JSON-serializable)."""
    payload = plan.model_dump(mode="json")
    return hashlib.sha256(json.dumps(payload, sort_keys=True).encode("utf-8")).hexdigest()


def compute_run_id(plan: RunPlan, config_hash: str, attempt: int = 1) -> str:
    """
    Deterministic run_id derived from plan_id + a hash prefix (+ an attempt
    suffix for attempt > 1).

    Re-running the same plan file produces the same run_id on the first
    attempt, which is what makes checkpoint-based resume possible — a
    materially different plan (different config_hash) intentionally gets a
    fresh run directory rather than silently resuming an unrelated
    checkpoint.

    attempt > 1 is for M19 Phase 3's experiment_id collision fix: if a
    run_id's checkpoint is already "complete" (or the user declines to
    resume an incomplete one), re-invoking execute_run() must not silently
    reuse that identity — wipe_graph() never deletes analytics/
    feedback_events, so a fresh checkpoint at the same run_id/experiment_id
    would mix a new trial's data with the old run's. See run_identity_for()
    for the matching experiment_id/series_id namespace.
    """
    suffix = "" if attempt <= 1 else f"_attempt{attempt}"
    return f"{plan.plan_id}_{config_hash[:12]}{suffix}"


def run_identity_for(plan: RunPlan, attempt: int = 1) -> str:
    """
    The plan_id-based identity used to build experiment_id/series_id
    strings (e.g. "{run_identity}_{condition}"). Matches compute_run_id()'s
    attempt-suffix convention: attempt=1 reproduces the original plain
    plan_id (fully backward compatible for the common case where a plan
    only ever runs once).

    A dry run gets a "_dryrun"-suffixed identity. Without it, a dry run of
    a plan writes analytics/feedback_events rows under the exact
    experiment_id the later real run of that plan uses — wipe_graph()
    deliberately never deletes those tables, so the real run's series/
    experiment reports would silently aggregate the dry run's turns. Same
    failure mode as the attempt-collision fix, via a different door.
    """
    base = plan.plan_id if attempt <= 1 else f"{plan.plan_id}_attempt{attempt}"
    return f"{base}_dryrun" if plan.dry_run else base


class RunManifest(BaseModel):
    run_id: str
    plan: dict
    config_hash: str
    resolved_turn_sequence: list[dict] = Field(default_factory=list)
    created_at: str = Field(default_factory=now_iso)
    # M19 Phase 3 — deterministic series_id per (condition, series_index),
    # keyed "{condition}:{series_index}" -> series_id. Recorded up front
    # (covers every condition/series in the plan, not just what a scoped
    # dry-run actually executes) so a run is fully auditable after the fact.
    series_id_map: dict[str, str] = Field(default_factory=dict)


def run_dir_for(run_id: str, base_dir: Path) -> Path:
    return base_dir / run_id


def manifest_path(run_id: str, base_dir: Path) -> Path:
    return run_dir_for(run_id, base_dir) / "manifest.json"


def save_manifest(manifest: RunManifest, base_dir: Path) -> Path:
    path = manifest_path(manifest.run_id, base_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(manifest.model_dump(mode="json"), indent=2), encoding="utf-8")
    return path


def load_manifest(run_id: str, base_dir: Path) -> Optional[RunManifest]:
    path = manifest_path(run_id, base_dir)
    if not path.exists():
        return None
    data = json.loads(path.read_text(encoding="utf-8"))
    return RunManifest(**data)
