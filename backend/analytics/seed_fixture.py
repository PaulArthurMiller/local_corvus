"""
Seed fixture provenance — register and verify seed fixture files.

Fixtures are gzip-compressed JSON snapshots (same format as graph snapshots)
used to initialize the graph store to a known state before an experiment run.
The manifest stored in the DB lets you verify that the fixture file hasn't
been replaced or tampered with between experiment conditions.

Fixture files live in seed_fixtures/seed_{version}.json.gz at the project root.

When a fixture is registered, each MU is stamped with:
  series_id   = "seed:{fixture_version}"
  experiment_id = resolved from EXPERIMENT_ID env var (same as orchestrator)
  cluster_id  = from per-MU metadata in the fixture file (if present)
"""

import gzip
import hashlib
import json
import logging
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import yaml
from pydantic import BaseModel

from core.config_overrides import apply_config_overrides, load_config_overrides_from_env
from learning.edge_update import validate_edge_update_config
from memory.store import store as graph_store

logger = logging.getLogger(__name__)

_SEED_FIXTURES_PATH = Path(os.getenv(
    "SEED_FIXTURES_PATH",
    os.path.join(os.path.dirname(__file__), "..", "..", "seed_fixtures"),
))

_CONFIG_PATH = os.getenv(
    "CONFIG_PATH",
    os.path.join(os.path.dirname(__file__), "..", "..", "config.yaml"),
)


def _resolve_experiment_id() -> str:
    """Read EXPERIMENT_ID from env, falling back to empty string."""
    return os.getenv("EXPERIMENT_ID", "").strip()


def _load_seed_priors() -> tuple[float, float]:
    """
    Read edge_update.seed_prior_alpha/seed_prior_beta from config.yaml.

    Falls back to the Beta(1,1) uniform prior if config.yaml is missing or
    invalid — register_fixture() is a data-loading path, not the place to
    hard-fail the whole app on a config problem (backend/core/orchestrator.py
    already validates and hard-fails at startup for the live request path).

    M43 Phase 2 — applies CONFIG_OVERRIDES_JSON on top of the on-disk file,
    same as core/orchestrator.py's own resolution. This is the read path a
    seed-prior sweep (e.g. exp01 cfgA's edge_update.seed_prior_alpha/_beta)
    actually depends on: register_fixture() calls this at wipe+seed time, so
    without the override applied here, seed edges would silently seed at the
    un-overridden config.yaml value regardless of what CONFIG_OVERRIDES_JSON
    says — the exact "config lived, run silently used the wrong value"
    failure this milestone exists to close, just one layer deeper than
    core/orchestrator.py's own _CONFIG.
    """
    try:
        with open(_CONFIG_PATH, "r", encoding="utf-8") as f:
            cfg = yaml.safe_load(f) or {}
        overrides = load_config_overrides_from_env()
        if overrides:
            cfg = apply_config_overrides(cfg, overrides)
        resolved = validate_edge_update_config(cfg)
        return resolved["seed_prior_alpha"], resolved["seed_prior_beta"]
    except Exception as exc:
        logger.warning("seed_fixture: could not resolve seed priors from config — %s", exc)
        return 1.0, 1.0


class SeedFixtureManifest(BaseModel):
    fixture_version: str
    manifest_hash: str                     # SHA-256 of the fixture file's raw bytes
    mu_count: int
    edge_count: int = 0                    # number of edges inserted from this fixture
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
    Compute hash and type distribution from a fixture file, store the manifest
    in the DB, and insert each MU into memory_units with proper stamps.

    The fixture file must be in gzip-compressed JSON format matching
    the graph snapshot schema: {memory_units: [...], edges: [...], ...}

    Each MU is stamped:
      series_id   = "seed:{version}"
      experiment_id = EXPERIMENT_ID from env
      cluster_id  = from per-MU "cluster_id" field if present
    """
    fixture_path = Path(fixture_path)
    if not fixture_path.exists():
        raise FileNotFoundError(f"Fixture file not found: {fixture_path}")

    manifest_hash = compute_fixture_hash(fixture_path)
    experiment_id = _resolve_experiment_id() or None
    series_id = f"seed:{version}"

    with gzip.open(fixture_path, "rt", encoding="utf-8") as f:
        data = json.load(f)

    memory_units = data.get("memory_units", [])
    mu_count = len(memory_units)

    type_distribution: dict[str, int] = {}
    generation_record: list[dict] = []
    for mu in memory_units:
        source_type = mu.get("source_type", "unknown")
        cluster_id = mu.get("cluster_id") or None
        type_distribution[source_type] = type_distribution.get(source_type, 0) + 1
        generation_record.append({
            "mu_id": mu.get("mu_id", ""),
            "source_type": source_type,
            "cluster_id": cluster_id,
        })

        # Insert MU into the graph store with seed stamps
        mu_id = mu.get("mu_id")
        if mu_id:
            try:
                await graph_store.create_mu(
                    mu_id=mu_id,
                    source_type=source_type,
                    raw_content=mu.get("raw_content", ""),
                    created_at=mu.get("created_at"),
                    series_id=series_id,
                    experiment_id=experiment_id,
                    cluster_id=cluster_id,
                )
            except Exception as exc:
                # MU already exists (idempotent re-registration) — skip
                logger.debug(
                    "seed_fixture: MU mu_id=%s already in DB (skipping): %s", mu_id, exc
                )

    seed_prior_alpha, seed_prior_beta = _load_seed_priors()

    edges = data.get("edges", [])
    edge_count = 0
    for edge in edges:
        edge_id = edge.get("edge_id")
        if not edge_id:
            continue
        try:
            await graph_store.create_edge(
                edge_id=edge_id,
                source_mu_id=edge["source_mu_id"],
                target_mu_id=edge["target_mu_id"],
                weight=float(edge.get("weight", 0.5)),
                direction=edge.get("direction", "forward"),
                creation_run_id=None,
                creation_trigger="seed",
                alpha=seed_prior_alpha,
                beta=seed_prior_beta,
            )
            edge_count += 1
        except Exception as exc:
            # Edge already exists (idempotent re-registration) — skip
            logger.debug(
                "seed_fixture: edge %s already in DB (skipping): %s", edge_id, exc
            )

    manifest = SeedFixtureManifest(
        fixture_version=version,
        manifest_hash=manifest_hash,
        mu_count=mu_count,
        edge_count=edge_count,
        type_distribution=type_distribution,
        generation_date=datetime.now(timezone.utc).isoformat(),
        generation_record=generation_record,
    )

    await graph_store.insert_seed_fixture_manifest(manifest.model_dump())
    logger.info(
        "seed_fixture: registered version=%s hash=%s mu_count=%d edge_count=%d series_id=%s",
        version,
        manifest_hash[:16],
        mu_count,
        edge_count,
        series_id,
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
