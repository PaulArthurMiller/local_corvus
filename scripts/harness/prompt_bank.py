"""
Prompt bank artifact layer (M19 Phase 1) — no API calls in this module.

A PromptBank is the immutable, reusable output of prompt_gen.py: one Sonnet
generation + validation pass over seed_v2's clusters. The harness only ever
draws `validated: true` entries at runtime; excluded entries are retained
with an `exclusion_reason` so nothing is silently dropped.

Validation errors use the same field-identifying convention as schema.py —
see format_validation_error() in that module.
"""

import hashlib
import json
from pathlib import Path
from typing import Optional

from pydantic import BaseModel, Field, ValidationError, field_validator

from harness.schema import format_validation_error


class PromptBankEntry(BaseModel):
    """One bulk-interaction prompt, with its grading criteria and provenance."""

    prompt_id: str = Field(min_length=1)
    cluster_id: str = Field(min_length=1)
    prompt_text: str = Field(min_length=1)
    hop_distance: int = Field(ge=0)
    criteria: list[str] = Field(min_length=1, max_length=3)
    seed_fixture_version: str = Field(min_length=1)
    generated_by: str = Field(min_length=1)  # e.g. "claude-sonnet-5@2026-07-04"
    validated: bool = False
    exclusion_reason: Optional[str] = None

    @field_validator("criteria")
    @classmethod
    def _validate_criteria_count(cls, v: list[str]) -> list[str]:
        if not (1 <= len(v) <= 3):
            raise ValueError(f"criteria must have 1-3 entries, got {len(v)}")
        return v


class PromptBank(BaseModel):
    """The full generated bank: entries + the params that produced them + a content hash."""

    entries: list[PromptBankEntry] = Field(default_factory=list)
    generation_params: dict = Field(default_factory=dict)
    content_hash: str = Field(min_length=1)

    def validated_entries(self) -> list[PromptBankEntry]:
        """Entries the runner is allowed to draw from — excluded ones are audit-only."""
        return [e for e in self.entries if e.validated]


def compute_bank_hash(fixture_manifest_hash: str, params: dict) -> str:
    """
    Deterministic hash identifying a (seed fixture version + generation params)
    pair. Same fixture + same params -> same hash -> the harness reuses the
    existing bank file instead of calling Sonnet again.
    """
    payload = {"fixture_manifest_hash": fixture_manifest_hash, "params": params}
    return hashlib.sha256(json.dumps(payload, sort_keys=True).encode("utf-8")).hexdigest()


def bank_path_for_hash(content_hash: str, base_dir: Path) -> Path:
    return base_dir / f"prompt_bank_{content_hash}.json"


def save_bank(bank: PromptBank, base_dir: Path) -> Path:
    base_dir.mkdir(parents=True, exist_ok=True)
    path = bank_path_for_hash(bank.content_hash, base_dir)
    path.write_text(json.dumps(bank.model_dump(mode="json"), indent=2), encoding="utf-8")
    return path


def load_bank(path: Path) -> PromptBank:
    """Load and validate a prompt bank file. Raises FileNotFoundError or ValueError
    with a specific, field-identifying message — never a raw stack trace."""
    path = Path(path)
    if not path.exists():
        raise FileNotFoundError(f"prompt bank not found: {path}")

    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ValueError(f"prompt bank file is not valid JSON: {path}: {exc}") from exc

    try:
        return PromptBank(**raw)
    except ValidationError as exc:
        raise ValueError(
            f"prompt bank validation failed for {path}:\n{format_validation_error(exc)}"
        ) from exc
