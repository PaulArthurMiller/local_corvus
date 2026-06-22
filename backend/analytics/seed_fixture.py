"""
Seed fixture provenance — register and verify seed fixture files.

Fixtures are gzip-compressed JSON snapshots (same format as graph snapshots)
used to initialize the graph store to a known state before an experiment run.
The manifest stored in the DB lets you verify that the fixture file hasn't
been replaced or tampered with between experiment conditions.

Fixture files live in seed_fixtures/seed_{version}.json.gz at the project root.
"""

import gzip
import hashlib
import json
import logging
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from pydantic import BaseModel

from memory.store import store as graph_store

logger = logging.getLogger(__name__)

_SEED_FIXTURES_PATH = Path(os.getenv(
    "SEED_FIXTURES_PATH",
    os.path.join(os.path.dirname(__file__), "..", "..", "seed_fixtures"),
))


class SeedFixtureManifest(BaseModel):
    fixture_version: str
    manifest_hash: str                     # SHA-256 of the fixture file's raw bytes
    mu_count: int
    type_distribution: dict[str, int]      # {source_type: count}
    generation_date: str
    generation_record: list[dict]          # [{mu_id, source_type}] per seed MU


def compute_fixture_hash(fixture_path: Path) -> str:
    """Return the SHA-256 hex digest of a fixture file's raw bytes."""
    sha = hashlib.sha256()
    with open(fixture_path, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            sha.update(chunk)
    return sha.hexdigest()


async def register_fixture(fixture_path: Path, version: str) -> SeedFixtureManifest:
    """
    Compute hash and type distribution from a fixture file, then store
    the manifest in the DB.

    The fixture file must be in gzip-compressed JSON format matching
    the graph snapshot schema: {memory_units: [...], edges: [...], ...}
    """
    fixture_path = Path(fixture_path)
    if not fixture_path.exists():
        raise FileNotFoundError(f"Fixture file not found: {fixture_path}")

    manifest_hash = compute_fixture_hash(fixture_path)

    with gzip.open(fixture_path, "rt", encoding="utf-8") as f:
        data = json.load(f)

    memory_units = data.get("memory_units", [])
    mu_count = len(memory_units)

    type_distribution: dict[str, int] = {}
    generation_record: list[dict] = []
    for mu in memory_units:
        source_type = mu.get("source_type", "unknown")
        type_distribution[source_type] = type_distribution.get(source_type, 0) + 1
        generation_record.append({
            "mu_id": mu.get("mu_id", ""),
            "source_type": source_type,
        })

    manifest = SeedFixtureManifest(
        fixture_version=version,
        manifest_hash=manifest_hash,
        mu_count=mu_count,
        type_distribution=type_distribution,
        generation_date=datetime.now(timezone.utc).isoformat(),
        generation_record=generation_record,
    )

    await graph_store.insert_seed_fixture_manifest(manifest.model_dump())
    logger.info(
        "seed_fixture: registered version=%s hash=%s mu_count=%d",
        version,
        manifest_hash[:16],
        mu_count,
    )
    return manifest


async def load_and_verify_fixture(version: str) -> SeedFixtureManifest:
    """
    Load the manifest from the DB, recompute the hash from the fixture file,
    and raise ValueError if the hashes don't match.

    A hash mismatch means the file was modified after registration — the
    experiment baseline cannot be trusted.
    """
    row = await graph_store.get_seed_fixture_manifest(version)
    if row is None:
        raise ValueError(f"No seed fixture manifest found for version={version}")

    manifest = SeedFixtureManifest(**row)

    fixture_path = _SEED_FIXTURES_PATH / f"seed_{version}.json.gz"
    if not fixture_path.exists():
        raise FileNotFoundError(f"Fixture file not found: {fixture_path}")

    current_hash = compute_fixture_hash(fixture_path)
    if current_hash != manifest.manifest_hash:
        raise ValueError(
            f"Fixture file tampered for version={version}: "
            f"registered={manifest.manifest_hash[:16]}... "
            f"current={current_hash[:16]}..."
        )

    logger.info(
        "seed_fixture: verified version=%s hash=%s", version, current_hash[:16]
    )
    return manifest
