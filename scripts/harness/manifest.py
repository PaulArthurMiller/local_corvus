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


def compute_run_id(plan: RunPlan, config_hash: str) -> str:
    """
    Deterministic run_id derived from plan_id + a hash prefix.

    Re-running the same plan file produces the same run_id, which is what
    makes checkpoint-based resume possible — a materially different plan
    (different config_hash) intentionally gets a fresh run directory rather
    than silently resuming an unrelated checkpoint.
    """
    return f"{plan.plan_id}_{config_hash[:12]}"


class RunManifest(BaseModel):
    run_id: str
    plan: dict
    config_hash: str
    resolved_turn_sequence: list[dict] = Field(default_factory=list)
    created_at: str = Field(default_factory=now_iso)


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
