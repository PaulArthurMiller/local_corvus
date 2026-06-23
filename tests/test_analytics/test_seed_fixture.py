"""
Tests for analytics/seed_fixture.py — fixture registration, hash verification,
and tamper detection.
"""

import asyncio
import gzip
import json
import os
import sys
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "backend"))

from analytics.seed_fixture import (
    SeedFixtureManifest,
    compute_fixture_hash,
    load_and_verify_fixture,
    register_fixture,
)


def run(coro):
    return asyncio.get_event_loop().run_until_complete(coro)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _write_fixture(path: Path, memory_units: list = None) -> Path:
    """Write a gzip-JSON fixture file in graph snapshot format."""
    data = {
        "timestamp": "2026-06-22T00:00:00Z",
        "mu_count": len(memory_units or []),
        "edge_count": 0,
        "memory_units": memory_units or [],
        "edges": [],
    }
    with gzip.open(path, "wt", encoding="utf-8") as f:
        json.dump(data, f)
    return path


def _make_fixture(tmp_path: Path, version: str = "v1", mus: list = None) -> Path:
    fixture_path = tmp_path / f"seed_{version}.json.gz"
    _write_fixture(fixture_path, mus or [
        {"mu_id": "mu-001", "source_type": "user_input", "raw_content": "hello"},
        {"mu_id": "mu-002", "source_type": "agent_output", "raw_content": "world"},
    ])
    return fixture_path


# ---------------------------------------------------------------------------
# compute_fixture_hash tests
# ---------------------------------------------------------------------------

def test_compute_fixture_hash_is_consistent(tmp_path):
    """compute_fixture_hash() returns the same hash for the same file."""
    fixture_path = _make_fixture(tmp_path)
    hash1 = compute_fixture_hash(fixture_path)
    hash2 = compute_fixture_hash(fixture_path)
    assert hash1 == hash2
    assert len(hash1) == 64  # SHA-256 hex digest
    print(f"[PASS] consistent hash: {hash1[:16]}...")


def test_compute_fixture_hash_differs_for_different_files(tmp_path):
    """compute_fixture_hash() returns different hashes for different files."""
    path_a = tmp_path / "a.json.gz"
    path_b = tmp_path / "b.json.gz"
    _write_fixture(path_a, [{"mu_id": "mu-001", "source_type": "user_input", "raw_content": "aaa"}])
    _write_fixture(path_b, [{"mu_id": "mu-001", "source_type": "user_input", "raw_content": "bbb"}])
    hash_a = compute_fixture_hash(path_a)
    hash_b = compute_fixture_hash(path_b)
    assert hash_a != hash_b
    print(f"[PASS] different content produces different hashes: {hash_a[:8]}... vs {hash_b[:8]}...")


# ---------------------------------------------------------------------------
# register_fixture tests
# ---------------------------------------------------------------------------

def test_register_fixture_builds_correct_manifest(tmp_path):
    """register_fixture() builds manifest with correct mu_count and type_distribution."""
    fixture_path = _make_fixture(tmp_path, mus=[
        {"mu_id": "mu-001", "source_type": "user_input", "raw_content": "a"},
        {"mu_id": "mu-002", "source_type": "user_input", "raw_content": "b"},
        {"mu_id": "mu-003", "source_type": "agent_output", "raw_content": "c"},
    ])

    with patch("analytics.seed_fixture.graph_store") as mock_store:
        mock_store.insert_seed_fixture_manifest = AsyncMock()
        manifest = run(register_fixture(fixture_path=fixture_path, version="v1"))

    assert isinstance(manifest, SeedFixtureManifest)
    assert manifest.fixture_version == "v1"
    assert manifest.mu_count == 3
    assert manifest.type_distribution == {"user_input": 2, "agent_output": 1}
    assert len(manifest.generation_record) == 3
    assert len(manifest.manifest_hash) == 64
    mock_store.insert_seed_fixture_manifest.assert_called_once()
    print(f"[PASS] manifest: mu_count={manifest.mu_count} types={manifest.type_distribution}")


def test_register_fixture_raises_for_missing_file(tmp_path):
    """register_fixture() raises FileNotFoundError for a non-existent file."""
    missing_path = tmp_path / "nonexistent.json.gz"

    with patch("analytics.seed_fixture.graph_store"):
        with pytest.raises(FileNotFoundError, match="not found"):
            run(register_fixture(fixture_path=missing_path, version="v1"))

    print("[PASS] FileNotFoundError raised for missing fixture file")


# ---------------------------------------------------------------------------
# load_and_verify_fixture tests
# ---------------------------------------------------------------------------

def test_load_and_verify_fixture_succeeds_when_hash_matches(tmp_path):
    """load_and_verify_fixture() succeeds when the file hash matches the stored manifest."""
    fixture_path = _make_fixture(tmp_path, version="v1")
    stored_hash = compute_fixture_hash(fixture_path)

    stored_manifest = {
        "fixture_version": "v1",
        "manifest_hash": stored_hash,
        "mu_count": 2,
        "type_distribution": {"user_input": 1, "agent_output": 1},
        "generation_date": "2026-06-22T00:00:00Z",
        "generation_record": [],
    }

    with patch("analytics.seed_fixture.graph_store") as mock_store:
        mock_store.get_seed_fixture_manifest = AsyncMock(return_value=stored_manifest)
        with patch("analytics.seed_fixture._SEED_FIXTURES_PATH", tmp_path):
            manifest = run(load_and_verify_fixture(version="v1"))

    assert manifest.fixture_version == "v1"
    assert manifest.manifest_hash == stored_hash
    print(f"[PASS] hash verified successfully: {stored_hash[:16]}...")


def test_load_and_verify_fixture_raises_on_hash_mismatch(tmp_path):
    """load_and_verify_fixture() raises ValueError when fixture file has been modified."""
    fixture_path = _make_fixture(tmp_path, version="v1")
    original_hash = compute_fixture_hash(fixture_path)

    stored_manifest = {
        "fixture_version": "v1",
        "manifest_hash": original_hash,
        "mu_count": 2,
        "type_distribution": {},
        "generation_date": "2026-06-22T00:00:00Z",
        "generation_record": [],
    }

    # Modify the fixture file after registration — simulates tampering
    _write_fixture(fixture_path, [
        {"mu_id": "mu-TAMPERED", "source_type": "system", "raw_content": "tampered content"},
    ])

    with patch("analytics.seed_fixture.graph_store") as mock_store:
        mock_store.get_seed_fixture_manifest = AsyncMock(return_value=stored_manifest)
        with patch("analytics.seed_fixture._SEED_FIXTURES_PATH", tmp_path):
            with pytest.raises(ValueError, match="tampered"):
                run(load_and_verify_fixture(version="v1"))

    print("[PASS] ValueError raised when fixture file hash does not match stored hash")


def test_load_and_verify_fixture_raises_when_manifest_missing():
    """load_and_verify_fixture() raises ValueError when no manifest is found in DB."""
    with patch("analytics.seed_fixture.graph_store") as mock_store:
        mock_store.get_seed_fixture_manifest = AsyncMock(return_value=None)
        with pytest.raises(ValueError, match="No seed fixture manifest"):
            run(load_and_verify_fixture(version="missing-version"))

    print("[PASS] ValueError raised when no manifest found for version")


def test_load_and_verify_fixture_raises_when_file_missing(tmp_path):
    """load_and_verify_fixture() raises FileNotFoundError when fixture file is gone."""
    stored_manifest = {
        "fixture_version": "v1",
        "manifest_hash": "abc123",
        "mu_count": 1,
        "type_distribution": {},
        "generation_date": "2026-06-22T00:00:00Z",
        "generation_record": [],
    }

    with patch("analytics.seed_fixture.graph_store") as mock_store:
        mock_store.get_seed_fixture_manifest = AsyncMock(return_value=stored_manifest)
        with patch("analytics.seed_fixture._SEED_FIXTURES_PATH", tmp_path):
            # seed_v1.json.gz does not exist in tmp_path
            with pytest.raises(FileNotFoundError):
                run(load_and_verify_fixture(version="v1"))

    print("[PASS] FileNotFoundError raised when fixture file is missing from disk")
