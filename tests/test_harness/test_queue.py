"""
Tests for scripts/harness/queue.py — queue file schema, manifest/checkpoint
persistence, and the 7-step preflight protocol. Fixture-reading tests (the
"fresh bank generation needed" path) use the real, committed
seed_fixtures/seed_v2.json.gz (no network, no Docker required) — same
convention as test_prompt_gen.py.
"""

import json
import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "scripts"))

from harness.client import BackendConnectivityError
from harness.prompt_bank import PromptBank, save_bank
from harness.prompt_gen import compute_fixture_manifest_hash, generation_params_for_plan
from harness.prompt_bank import bank_path_for_hash, compute_bank_hash
from harness.queue import (
    MANIFEST_STATUS_RESOLVED,
    MANIFEST_STATUS_SKIPPED_DISABLED,
    QueueCheckpointState,
    QueueFile,
    QueueManifest,
    QueuePlanCheckpointEntry,
    QueuePlanEntry,
    QueuePlanManifestEntry,
    QueuePreflightError,
    STATUS_PENDING,
    STATUS_SKIPPED,
    launch_dir_name,
    load_queue_checkpoint,
    load_queue_file,
    load_queue_manifest,
    resolve_plan_path,
    run_preflight,
    save_queue_checkpoint,
    save_queue_manifest,
)
from harness.schema import RunPlan
from pydantic import ValidationError

REAL_FIXTURE_PATH = Path(__file__).parent.parent.parent / "seed_fixtures" / "seed_v2.json.gz"


# ---------------------------------------------------------------------------
# Test helpers
# ---------------------------------------------------------------------------

class FakeBackendClient:
    def __init__(self, health_error=None):
        self.health_error = health_error
        self.health_calls = 0

    def health(self):
        self.health_calls += 1
        if self.health_error:
            raise self.health_error
        return {"status": "ok", "db": "ok"}


class FakeJudgeClient:
    def __init__(self, unavailable_models=None):
        self.unavailable_models = set(unavailable_models or [])
        self.checked = []

    def model_available(self, model):
        self.checked.append(model)
        return model not in self.unavailable_models


def _dummy_bank_path(tmp_path, name="dummy_bank.json") -> Path:
    """A minimal, validly-loadable PromptBank file — zero entries is fine
    (PromptBank.entries has no min_length)."""
    bank = PromptBank(entries=[], generation_params={}, content_hash="a" * 64)
    banks_dir = tmp_path / "explicit_banks"
    banks_dir.mkdir(exist_ok=True)
    return save_bank(bank, banks_dir)


def _plan_dict(**overrides):
    kwargs = dict(
        plan_id="p1",
        plan_name="Test plan",
        conceptual_fields={"count": 4, "subjects": "auto"},
        series_count=1,
        turns_per_series=4,
        probe_ratio=0.25,
        conditions=["learned"],
        random_seed=1,
    )
    kwargs.update(overrides)
    return kwargs


def _write_plan(path: Path, **overrides):
    path.write_text(json.dumps(_plan_dict(**overrides)), encoding="utf-8")
    return path


def _write_queue(path: Path, plans: list, **overrides):
    body = {
        "queue_id": "q1",
        "on_plan_failure": "continue",
        "plans": plans,
    }
    body.update(overrides)
    path.write_text(json.dumps(body), encoding="utf-8")
    return path


def _preflight_deps(tmp_path):
    """Standard (client, judge_client, fixture_path, prompt_banks_dir,
    project_root) tuple for run_preflight() calls that don't need to
    exercise the real fixture-reading path."""
    return (
        FakeBackendClient(),
        FakeJudgeClient(),
        tmp_path / "unused_fixture.json.gz",
        tmp_path / "prompt_banks",
        tmp_path,
    )


# ---------------------------------------------------------------------------
# Queue-file validation
# ---------------------------------------------------------------------------

def test_queue_file_missing_required_field_raises_value_error(tmp_path):
    path = tmp_path / "q.json"
    path.write_text(json.dumps({"on_plan_failure": "continue", "plans": [{"plan_path": "a.json"}]}), encoding="utf-8")
    with pytest.raises(ValueError, match="queue_id"):
        load_queue_file(path)


def test_queue_file_bad_on_plan_failure_value_raises_value_error(tmp_path):
    path = _write_queue(tmp_path / "q.json", [{"plan_path": "a.json"}], on_plan_failure="retry_forever")
    with pytest.raises(ValueError, match="on_plan_failure"):
        load_queue_file(path)


def test_queue_file_empty_plans_list_raises_value_error(tmp_path):
    path = _write_queue(tmp_path / "q.json", [])
    with pytest.raises(ValueError):
        load_queue_file(path)


def test_queue_file_not_found_raises_file_not_found_error(tmp_path):
    with pytest.raises(FileNotFoundError):
        load_queue_file(tmp_path / "nonexistent.json")


def test_queue_file_not_valid_json_raises_value_error(tmp_path):
    path = tmp_path / "q.json"
    path.write_text("{not json", encoding="utf-8")
    with pytest.raises(ValueError, match="not valid JSON"):
        load_queue_file(path)


def test_queue_file_defaults_enabled_true_and_on_plan_failure_continue(tmp_path):
    path = _write_queue(tmp_path / "q.json", [{"plan_path": "a.json"}])
    qf = load_queue_file(path)
    assert qf.plans[0].enabled is True
    assert qf.on_plan_failure == "continue"


# ---------------------------------------------------------------------------
# resolve_plan_path — relative vs absolute
# ---------------------------------------------------------------------------

def test_resolve_plan_path_relative_resolves_against_queue_file_dir(tmp_path):
    queue_dir = tmp_path / "queues"
    queue_dir.mkdir()
    queue_file_path = queue_dir / "q.json"
    resolved = resolve_plan_path(queue_file_path, "../plans/foo.json")
    assert resolved == (tmp_path / "plans" / "foo.json").resolve()


def test_resolve_plan_path_absolute_passes_through(tmp_path):
    abs_path = str((tmp_path / "plans" / "foo.json").resolve())
    resolved = resolve_plan_path(tmp_path / "queues" / "q.json", abs_path)
    assert resolved == Path(abs_path)


# ---------------------------------------------------------------------------
# Preflight — missing plan file
# ---------------------------------------------------------------------------

def test_preflight_missing_plan_file_hard_stops(tmp_path):
    queue_path = _write_queue(tmp_path / "q.json", [{"plan_path": "does_not_exist.json"}])
    qf = load_queue_file(queue_path)
    client, judge, fixture, banks_dir, root = _preflight_deps(tmp_path)
    with pytest.raises(QueuePreflightError, match="not found"):
        run_preflight(qf, queue_path, client, judge, fixture, banks_dir, root)
    assert client.health_calls == 0  # never reached — hard stop is before any backend call


# ---------------------------------------------------------------------------
# Preflight — invalid plan (schema error identifies which plan)
# ---------------------------------------------------------------------------

def test_preflight_invalid_plan_identifies_the_plan(tmp_path):
    plan_path = tmp_path / "bad_plan.json"
    _write_plan(plan_path, turns_per_series=-1)  # gt=0 violation
    queue_path = _write_queue(tmp_path / "q.json", [{"plan_path": "bad_plan.json"}])
    qf = load_queue_file(queue_path)
    client, judge, fixture, banks_dir, root = _preflight_deps(tmp_path)
    with pytest.raises(QueuePreflightError, match="bad_plan.json"):
        run_preflight(qf, queue_path, client, judge, fixture, banks_dir, root)


# ---------------------------------------------------------------------------
# Preflight — duplicate config_hash hard stop
# ---------------------------------------------------------------------------

def test_preflight_duplicate_config_hash_hard_stops(tmp_path):
    bank_path = _dummy_bank_path(tmp_path)
    _write_plan(tmp_path / "planA.json", prompt_bank_path=str(bank_path), conceptual_fields={"count": 4, "subjects": "auto"})
    _write_plan(tmp_path / "planB.json", prompt_bank_path=str(bank_path), conceptual_fields={"count": 4, "subjects": "auto"})
    queue_path = _write_queue(tmp_path / "q.json", [{"plan_path": "planA.json"}, {"plan_path": "planB.json"}])
    qf = load_queue_file(queue_path)
    client, judge, fixture, banks_dir, root = _preflight_deps(tmp_path)
    with pytest.raises(QueuePreflightError, match="replicates need distinct random_seed values"):
        run_preflight(qf, queue_path, client, judge, fixture, banks_dir, root)


def test_preflight_distinct_random_seed_is_not_a_duplicate(tmp_path):
    bank_path = _dummy_bank_path(tmp_path)
    _write_plan(tmp_path / "planA.json", prompt_bank_path=str(bank_path), random_seed=1)
    _write_plan(tmp_path / "planB.json", prompt_bank_path=str(bank_path), random_seed=2)
    queue_path = _write_queue(tmp_path / "q.json", [{"plan_path": "planA.json"}, {"plan_path": "planB.json"}])
    qf = load_queue_file(queue_path)
    client, judge, fixture, banks_dir, root = _preflight_deps(tmp_path)
    resolution = run_preflight(qf, queue_path, client, judge, fixture, banks_dir, root)
    assert len(resolution.manifest.plans) == 2


# ---------------------------------------------------------------------------
# Preflight — bank resolution (step 4)
# ---------------------------------------------------------------------------

def test_preflight_explicit_bank_path_missing_hard_stops(tmp_path):
    _write_plan(tmp_path / "planA.json", prompt_bank_path=str(tmp_path / "nope.json"))
    queue_path = _write_queue(tmp_path / "q.json", [{"plan_path": "planA.json"}])
    qf = load_queue_file(queue_path)
    client, judge, fixture, banks_dir, root = _preflight_deps(tmp_path)
    with pytest.raises(QueuePreflightError, match="invalid or missing"):
        run_preflight(qf, queue_path, client, judge, fixture, banks_dir, root)


def test_preflight_explicit_bank_path_valid_passes(tmp_path):
    bank_path = _dummy_bank_path(tmp_path)
    _write_plan(tmp_path / "planA.json", prompt_bank_path=str(bank_path))
    queue_path = _write_queue(tmp_path / "q.json", [{"plan_path": "planA.json"}])
    qf = load_queue_file(queue_path)
    client, judge, fixture, banks_dir, root = _preflight_deps(tmp_path)
    resolution = run_preflight(qf, queue_path, client, judge, fixture, banks_dir, root)
    assert resolution.manifest.plans[0].bank_path == str(bank_path)


def test_preflight_cached_hash_resolution_stamps_the_plan(tmp_path):
    """subjects=='auto', no explicit prompt_bank_path, but a bank matching
    this plan's (fixture, generation_params) hash already exists on disk —
    preflight must resolve and stamp its path without any Sonnet call."""
    plan_path = _write_plan(tmp_path / "planA.json", conceptual_fields={"count": 4, "subjects": "auto"})
    plan = RunPlan(**json.loads(plan_path.read_text()))

    banks_dir = tmp_path / "prompt_banks"
    banks_dir.mkdir()
    fixture_hash = compute_fixture_manifest_hash(REAL_FIXTURE_PATH)
    params = generation_params_for_plan(plan)
    content_hash = compute_bank_hash(fixture_hash, params)
    existing_bank = PromptBank(entries=[], generation_params=params, content_hash=content_hash)
    save_bank(existing_bank, banks_dir)

    queue_path = _write_queue(tmp_path / "q.json", [{"plan_path": "planA.json"}])
    qf = load_queue_file(queue_path)
    client, judge, _, _, root = _preflight_deps(tmp_path)
    resolution = run_preflight(qf, queue_path, client, judge, REAL_FIXTURE_PATH, banks_dir, root)
    assert resolution.manifest.plans[0].bank_path == str(bank_path_for_hash(content_hash, banks_dir))


def test_preflight_fresh_bank_generation_needed_hard_stops(tmp_path):
    """subjects=='auto', no explicit bank, and no cached bank exists —
    must hard-stop naming --generate-bank, never fall through to a live
    Sonnet call."""
    _write_plan(tmp_path / "planA.json", conceptual_fields={"count": 4, "subjects": "auto"})
    queue_path = _write_queue(tmp_path / "q.json", [{"plan_path": "planA.json"}])
    qf = load_queue_file(queue_path)
    banks_dir = tmp_path / "empty_banks"
    client, judge = FakeBackendClient(), FakeJudgeClient()
    with pytest.raises(QueuePreflightError, match="--generate-bank"):
        run_preflight(qf, queue_path, client, judge, REAL_FIXTURE_PATH, banks_dir, tmp_path)


def test_preflight_neither_bank_nor_auto_hard_stops(tmp_path):
    _write_plan(tmp_path / "planA.json", conceptual_fields={"count": 4, "subjects": ["manual subject"]})
    queue_path = _write_queue(tmp_path / "q.json", [{"plan_path": "planA.json"}])
    qf = load_queue_file(queue_path)
    client, judge, fixture, banks_dir, root = _preflight_deps(tmp_path)
    with pytest.raises(QueuePreflightError, match="wasted GPU-hours"):
        run_preflight(qf, queue_path, client, judge, fixture, banks_dir, root)


# ---------------------------------------------------------------------------
# Preflight — health check + judge availability (step 5)
# ---------------------------------------------------------------------------

def test_preflight_health_check_failure_hard_stops(tmp_path):
    bank_path = _dummy_bank_path(tmp_path)
    _write_plan(tmp_path / "planA.json", prompt_bank_path=str(bank_path))
    queue_path = _write_queue(tmp_path / "q.json", [{"plan_path": "planA.json"}])
    qf = load_queue_file(queue_path)
    client = FakeBackendClient(health_error=BackendConnectivityError("backend down"))
    judge = FakeJudgeClient()
    with pytest.raises(QueuePreflightError, match="health check failed"):
        run_preflight(qf, queue_path, client, judge, tmp_path / "fx", tmp_path / "banks", tmp_path)


def test_preflight_judge_model_unavailable_hard_stops(tmp_path):
    bank_path = _dummy_bank_path(tmp_path)
    _write_plan(tmp_path / "planA.json", prompt_bank_path=str(bank_path), judge={"model": "missing-model", "temperature": 0.0})
    queue_path = _write_queue(tmp_path / "q.json", [{"plan_path": "planA.json"}])
    qf = load_queue_file(queue_path)
    client = FakeBackendClient()
    judge = FakeJudgeClient(unavailable_models={"missing-model"})
    with pytest.raises(QueuePreflightError, match="judge model unavailable"):
        run_preflight(qf, queue_path, client, judge, tmp_path / "fx", tmp_path / "banks", tmp_path)


# ---------------------------------------------------------------------------
# Preflight — aggregate budget (step 6)
# ---------------------------------------------------------------------------

def test_preflight_aggregate_budget_breach_hard_stops(tmp_path):
    bank_path = _dummy_bank_path(tmp_path)
    _write_plan(tmp_path / "planA.json", prompt_bank_path=str(bank_path), series_count=2, turns_per_series=5, probe_ratio=0.0)
    queue_path = _write_queue(tmp_path / "q.json", [{"plan_path": "planA.json"}], max_total_budget_usd=0.01)
    qf = load_queue_file(queue_path)
    client, judge, fixture, banks_dir, root = _preflight_deps(tmp_path)
    with pytest.raises(QueuePreflightError, match="exceeds max_total_budget_usd"):
        run_preflight(qf, queue_path, client, judge, fixture, banks_dir, root, judge_cost_per_call=1.0)


def test_preflight_aggregate_budget_none_is_uncapped(tmp_path):
    bank_path = _dummy_bank_path(tmp_path)
    _write_plan(tmp_path / "planA.json", prompt_bank_path=str(bank_path), series_count=2, turns_per_series=5, probe_ratio=0.0)
    queue_path = _write_queue(tmp_path / "q.json", [{"plan_path": "planA.json"}])  # max_total_budget_usd omitted -> None
    qf = load_queue_file(queue_path)
    client, judge, fixture, banks_dir, root = _preflight_deps(tmp_path)
    resolution = run_preflight(qf, queue_path, client, judge, fixture, banks_dir, root, judge_cost_per_call=1000.0)
    assert resolution.aggregate_estimated_cost_usd > 0


# ---------------------------------------------------------------------------
# Preflight — disabled plans skipped, never validated
# ---------------------------------------------------------------------------

def test_preflight_disabled_plan_never_validated(tmp_path):
    """A disabled entry pointing at a nonexistent/invalid file must not
    trip any preflight check — it's recorded as skipped_disabled only."""
    bank_path = _dummy_bank_path(tmp_path)
    _write_plan(tmp_path / "planA.json", prompt_bank_path=str(bank_path))
    queue_path = _write_queue(tmp_path / "q.json", [
        {"plan_path": "planA.json"},
        {"plan_path": "totally_missing.json", "enabled": False},
    ])
    qf = load_queue_file(queue_path)
    client, judge, fixture, banks_dir, root = _preflight_deps(tmp_path)
    resolution = run_preflight(qf, queue_path, client, judge, fixture, banks_dir, root)
    statuses = {e.plan_path: e.status for e in resolution.manifest.plans}
    assert statuses["totally_missing.json"] == MANIFEST_STATUS_SKIPPED_DISABLED
    assert statuses["planA.json"] == MANIFEST_STATUS_RESOLVED

    ckpt_statuses = {e.plan_path: e.status for e in resolution.checkpoint.plans}
    assert ckpt_statuses["totally_missing.json"] == STATUS_SKIPPED
    assert ckpt_statuses["planA.json"] == STATUS_PENDING


# ---------------------------------------------------------------------------
# Preflight — full success shape (manifest + checkpoint content)
# ---------------------------------------------------------------------------

def test_preflight_success_manifest_and_checkpoint_shape(tmp_path):
    bank_path = _dummy_bank_path(tmp_path)
    _write_plan(tmp_path / "planA.json", prompt_bank_path=str(bank_path), conditions=["learned", "static_weight"])
    queue_path = _write_queue(tmp_path / "q.json", [{"plan_path": "planA.json"}])
    qf = load_queue_file(queue_path)
    client, judge, fixture, banks_dir, root = _preflight_deps(tmp_path)
    resolution = run_preflight(qf, queue_path, client, judge, fixture, banks_dir, root)

    entry = resolution.manifest.plans[0]
    assert entry.plan_id == "p1"
    assert entry.config_hash is not None
    assert entry.expected_run_id is not None
    assert entry.conditions == ["learned", "static_weight"]
    assert resolution.projected_archive_count == 2  # 1 plan x 2 conditions

    ckpt_entry = resolution.checkpoint.plans[0]
    assert ckpt_entry.status == STATUS_PENDING
    assert ckpt_entry.run_id == entry.expected_run_id
    assert client.health_calls == 1


# ---------------------------------------------------------------------------
# Manifest / checkpoint atomic-write round-trip
# ---------------------------------------------------------------------------

def test_queue_manifest_round_trips(tmp_path):
    manifest = QueueManifest(
        queue_id="q1", queue_file_path="q.json", queue_content_hash="abc123",
        plans=[QueuePlanManifestEntry(plan_path="a.json", enabled=True, status=MANIFEST_STATUS_RESOLVED, plan_id="p1")],
    )
    launch_dir = tmp_path / "q1_20260101T000000Z"
    save_queue_manifest(manifest, launch_dir)
    loaded = load_queue_manifest(launch_dir)
    assert loaded is not None
    assert loaded.queue_id == "q1"
    assert loaded.plans[0].plan_id == "p1"


def test_queue_manifest_load_returns_none_when_missing(tmp_path):
    assert load_queue_manifest(tmp_path / "nope") is None


def test_queue_checkpoint_round_trips(tmp_path):
    state = QueueCheckpointState(
        queue_id="q1",
        plans=[QueuePlanCheckpointEntry(plan_path="a.json", status=STATUS_PENDING)],
    )
    launch_dir = tmp_path / "q1_20260101T000000Z"
    save_queue_checkpoint(state, launch_dir)
    loaded = load_queue_checkpoint(launch_dir)
    assert loaded is not None
    assert loaded.plans[0].status == STATUS_PENDING


def test_queue_checkpoint_load_returns_none_when_missing(tmp_path):
    assert load_queue_checkpoint(tmp_path / "nope") is None


def test_queue_checkpoint_atomic_write_leaves_no_tmp_file(tmp_path):
    state = QueueCheckpointState(queue_id="q1", plans=[])
    launch_dir = tmp_path / "q1_20260101T000000Z"
    path = save_queue_checkpoint(state, launch_dir)
    assert path.exists()
    assert not (launch_dir / "queue_checkpoint.json.tmp").exists()


# ---------------------------------------------------------------------------
# launch_dir_name
# ---------------------------------------------------------------------------

def test_launch_dir_name_uses_explicit_timestamp():
    assert launch_dir_name("screening_a", launch_ts="20260712T0300Z") == "screening_a_20260712T0300Z"


def test_launch_dir_name_default_timestamp_is_nonempty():
    name = launch_dir_name("screening_a")
    assert name.startswith("screening_a_")
    assert len(name) > len("screening_a_")
