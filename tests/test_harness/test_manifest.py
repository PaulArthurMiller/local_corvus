"""Tests for scripts/harness/manifest.py — config hash + manifest persistence."""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "scripts"))

from harness.manifest import (
    RunManifest,
    compute_config_hash,
    compute_run_id,
    load_manifest,
    run_identity_for,
    save_manifest,
)
from harness.schema import RunPlan


def _plan(**overrides):
    kwargs = dict(
        plan_id="p1",
        plan_name="Test plan",
        conceptual_fields={"count": 4, "subjects": "auto"},
        series_count=3,
        turns_per_series=10,
    )
    kwargs.update(overrides)
    return RunPlan(**kwargs)


def test_config_hash_is_stable_for_identical_plans():
    plan_a = _plan()
    plan_b = _plan()
    assert compute_config_hash(plan_a) == compute_config_hash(plan_b)


def test_config_hash_changes_when_plan_changes():
    hash_a = compute_config_hash(_plan(random_seed=1))
    hash_b = compute_config_hash(_plan(random_seed=2))
    assert hash_a != hash_b


def test_run_id_is_deterministic_for_same_plan():
    plan = _plan()
    config_hash = compute_config_hash(plan)
    run_id_a = compute_run_id(plan, config_hash)
    run_id_b = compute_run_id(plan, config_hash)
    assert run_id_a == run_id_b
    assert run_id_a.startswith("p1_")


def test_save_and_load_manifest_round_trips(tmp_path):
    plan = _plan()
    config_hash = compute_config_hash(plan)
    run_id = compute_run_id(plan, config_hash)
    manifest = RunManifest(
        run_id=run_id,
        plan=plan.model_dump(mode="json"),
        config_hash=config_hash,
        resolved_turn_sequence=[{"series_index": 0, "turn_index": 0, "turn_type": "bulk"}],
    )
    save_manifest(manifest, tmp_path)

    loaded = load_manifest(run_id, tmp_path)
    assert loaded is not None
    assert loaded.run_id == run_id
    assert loaded.config_hash == config_hash
    assert loaded.resolved_turn_sequence == manifest.resolved_turn_sequence


def test_load_manifest_returns_none_when_missing(tmp_path):
    assert load_manifest("nonexistent_run", tmp_path) is None


# ---------------------------------------------------------------------------
# M19 Phase 3 — attempt-suffix disambiguation (experiment_id collision fix)
# ---------------------------------------------------------------------------

def test_compute_run_id_attempt_1_matches_original_convention():
    plan = _plan()
    config_hash = compute_config_hash(plan)
    assert compute_run_id(plan, config_hash) == compute_run_id(plan, config_hash, attempt=1)
    assert compute_run_id(plan, config_hash, attempt=1) == f"p1_{config_hash[:12]}"


def test_compute_run_id_attempt_2_is_distinct_and_stable():
    plan = _plan()
    config_hash = compute_config_hash(plan)
    run_id_1 = compute_run_id(plan, config_hash, attempt=1)
    run_id_2a = compute_run_id(plan, config_hash, attempt=2)
    run_id_2b = compute_run_id(plan, config_hash, attempt=2)

    assert run_id_2a != run_id_1
    assert run_id_2a == run_id_2b  # deterministic given the same attempt number
    assert run_id_2a == f"p1_{config_hash[:12]}_attempt2"


def test_run_identity_for_attempt_1_is_plain_plan_id():
    plan = _plan()
    assert run_identity_for(plan) == "p1"
    assert run_identity_for(plan, attempt=1) == "p1"


def test_run_identity_for_attempt_2_is_suffixed():
    plan = _plan()
    assert run_identity_for(plan, attempt=2) == "p1_attempt2"


def test_run_identity_for_dry_run_gets_its_own_namespace():
    """A dry run must never share an experiment_id namespace with the real
    run of the same plan: analytics/feedback_events survive wipe_graph(), so
    a shared identity would mix dry-run turns into the real run's reports."""
    plan = _plan()
    plan.dry_run = True
    assert run_identity_for(plan) == "p1_dryrun"
    assert run_identity_for(plan, attempt=2) == "p1_attempt2_dryrun"
