"""
Tests for scripts/harness/runner.py — core orchestration, using a fake
BackendClient so no real Docker stack is required.
"""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "scripts"))

from harness.checkpoint import STATUS_OK, STATUS_SOFT_CONTINUE, load_checkpoint
from harness.client import BackendConnectivityError, BackendTurnError
from harness.runner import execute_run
from harness.schema import RunPlan


class FakeBackendClient:
    """Duck-typed stand-in for harness.client.BackendClient."""

    def __init__(self, health_error=None, wipe_error=None, seed_error=None, chat_errors=None):
        self.health_error = health_error
        self.wipe_error = wipe_error
        self.seed_error = seed_error
        self.chat_errors = chat_errors or {}  # call-index -> exception instance
        self.chat_call_count = 0
        self.chat_messages = []
        self.wipe_calls = []
        self.seed_calls = 0

    def health(self):
        if self.health_error:
            raise self.health_error
        return {"status": "ok", "db": "ok"}

    def wipe_graph(self, experiment_id=None):
        self.wipe_calls.append(experiment_id)
        if self.wipe_error:
            raise self.wipe_error
        return {"mus_deleted": 34, "edges_deleted": 32, "archive_path": "archive_test.json.gz"}

    def seed_graph(self, version="v2"):
        self.seed_calls += 1
        if self.seed_error:
            raise self.seed_error
        return {"mu_count": 34, "edge_count": 32}

    def graph_stats(self):
        return {"mu_count": 34}

    def chat(self, message, series_id=None):
        idx = self.chat_call_count
        self.chat_call_count += 1
        self.chat_messages.append(message)
        if idx in self.chat_errors:
            raise self.chat_errors[idx]
        return {"run_id": f"run-{idx}", "response": "placeholder response"}


def _plan(**overrides):
    kwargs = dict(
        plan_id="p1",
        plan_name="Test plan",
        conceptual_fields={"count": 4, "subjects": "auto"},
        series_count=2,
        turns_per_series=5,
        probe_ratio=0.2,
        probe_placement="end",
        random_seed=42,
        conditions=["learned", "static_weight"],
    )
    kwargs.update(overrides)
    return RunPlan(**kwargs)


def _noop_print(*_a, **_k):
    pass


def test_dry_run_scopes_to_one_condition_and_dry_run_n_turns(tmp_path):
    plan = _plan(dry_run=True, dry_run_n=2)
    client = FakeBackendClient()

    outcome = execute_run(plan, client, tmp_path, print_fn=_noop_print)

    assert outcome.status == "complete"
    assert outcome.turns_processed == 2
    assert len(outcome.checkpoint.turns) == 2
    assert all(t.condition == "learned" for t in outcome.checkpoint.turns)
    assert all(t.series_index == 0 for t in outcome.checkpoint.turns)
    assert outcome.manifest_path.exists()
    assert outcome.notification_log_path.exists()


def test_hard_stop_on_unreachable_backend_preserves_manifest_and_checkpoint(tmp_path):
    plan = _plan(dry_run=True, dry_run_n=2)
    client = FakeBackendClient(health_error=BackendConnectivityError("connection refused"))

    outcome = execute_run(plan, client, tmp_path, print_fn=_noop_print)

    assert outcome.status == "aborted"
    assert "connectivity" in outcome.error_summary
    assert outcome.manifest_path.exists()
    assert outcome.checkpoint.final_status == "aborted"

    reloaded = load_checkpoint(outcome.run_id, tmp_path)
    assert reloaded is not None
    assert reloaded.final_status == "aborted"


def test_soft_continue_on_ambiguous_turn_result_logs_and_proceeds(tmp_path):
    plan = _plan(dry_run=True, dry_run_n=3)
    # Second chat call (index 1) returns an ambiguous/malformed result.
    client = FakeBackendClient(chat_errors={1: BackendTurnError("missing run_id")})

    outcome = execute_run(plan, client, tmp_path, print_fn=_noop_print)

    print(f"[test] final status: {outcome.status}")
    print(f"[test] turn checkpoints: {[(t.turn_index, t.status, t.detail) for t in outcome.checkpoint.turns]}")

    assert outcome.status == "complete"
    assert outcome.turns_processed == 3
    statuses = [t.status for t in outcome.checkpoint.turns]
    assert statuses.count(STATUS_OK) == 2
    assert statuses.count(STATUS_SOFT_CONTINUE) == 1


def test_error_policy_override_promotes_turn_ambiguous_to_hard_stop(tmp_path):
    plan = _plan(
        dry_run=True, dry_run_n=3,
        error_policy={"turn_ambiguous": "hard_stop"},
    )
    client = FakeBackendClient(chat_errors={0: BackendTurnError("missing run_id")})

    outcome = execute_run(plan, client, tmp_path, print_fn=_noop_print)

    assert outcome.status == "aborted"
    assert "turn error" in outcome.error_summary


def test_resume_skips_already_completed_turns_after_hard_stop(tmp_path):
    plan = _plan(dry_run=True, dry_run_n=3)

    # First run: connectivity failure on the second chat call aborts the run
    # after turn 0 has already been checkpointed.
    failing_client = FakeBackendClient(chat_errors={1: BackendConnectivityError("dropped")})
    first_outcome = execute_run(plan, failing_client, tmp_path, print_fn=_noop_print)
    assert first_outcome.status == "aborted"
    assert len(first_outcome.checkpoint.turns) == 1

    # Second run: same plan (same run_id), a healthy client, and the user
    # chooses to resume. Only the remaining turns should be processed.
    healthy_client = FakeBackendClient()

    def scripted_resume_input(_prompt):
        return "y"

    second_outcome = execute_run(
        plan, healthy_client, tmp_path,
        resume_input_fn=scripted_resume_input, print_fn=_noop_print,
    )

    assert second_outcome.status == "complete"
    assert second_outcome.run_id == first_outcome.run_id
    # Total recorded turns across the resumed checkpoint == dry_run_n, no duplicates.
    assert len(second_outcome.checkpoint.turns) == 3
    keys = [(t.condition, t.series_index, t.turn_index) for t in second_outcome.checkpoint.turns]
    assert len(keys) == len(set(keys))
    # Only 2 new chat calls were made by the healthy client (turn 0 was skipped).
    assert healthy_client.chat_call_count == 2


def test_resume_declined_starts_fresh(tmp_path):
    plan = _plan(dry_run=True, dry_run_n=2)

    failing_client = FakeBackendClient(chat_errors={1: BackendConnectivityError("dropped")})
    first_outcome = execute_run(plan, failing_client, tmp_path, print_fn=_noop_print)
    assert first_outcome.status == "aborted"
    assert len(first_outcome.checkpoint.turns) == 1

    healthy_client = FakeBackendClient()

    def scripted_decline_input(_prompt):
        return "n"

    second_outcome = execute_run(
        plan, healthy_client, tmp_path,
        resume_input_fn=scripted_decline_input, print_fn=_noop_print,
    )

    assert second_outcome.status == "complete"
    # Starting fresh means both turns are re-run from scratch.
    assert len(second_outcome.checkpoint.turns) == 2
    assert healthy_client.chat_call_count == 2


def test_wipe_is_called_before_seed_for_each_condition(tmp_path):
    plan = _plan(dry_run=True, dry_run_n=1)
    client = FakeBackendClient()

    outcome = execute_run(plan, client, tmp_path, print_fn=_noop_print)

    assert outcome.status == "complete"
    # Dry-run scopes to exactly one condition ("learned", first in condition_order).
    assert client.wipe_calls == ["p1_learned"]
    assert client.seed_calls == 1


def test_hard_stop_when_wipe_fails(tmp_path):
    plan = _plan(dry_run=True, dry_run_n=2)
    client = FakeBackendClient(wipe_error=BackendConnectivityError("wipe endpoint 500"))

    outcome = execute_run(plan, client, tmp_path, print_fn=_noop_print)

    assert outcome.status == "aborted"
    assert "wipe/seed" in outcome.error_summary
    assert client.seed_calls == 0  # seed must never run if wipe failed


def test_probe_placeholder_content_used_for_probe_turns(tmp_path):
    plan = _plan(dry_run=True, dry_run_n=5, probe_ratio=1.0, probe_placement="end")
    client = FakeBackendClient()
    probes = [{"probe_id": "p06", "prompt_text": "What is the format rule?"}]

    outcome = execute_run(plan, client, tmp_path, probes=probes, print_fn=_noop_print)

    assert outcome.status == "complete"
    assert any("What is the format rule?" in m for m in client.chat_messages)
