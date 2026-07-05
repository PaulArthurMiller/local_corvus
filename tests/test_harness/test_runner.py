"""
Tests for scripts/harness/runner.py — core orchestration, using a fake
BackendClient so no real Docker stack is required.
"""

import json
import os
import sys
from pathlib import Path

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "scripts"))

from harness.checkpoint import STATUS_OK, STATUS_SOFT_CONTINUE, load_checkpoint
from harness.client import BackendConnectivityError, BackendTurnError, ReportGenerationError
from harness.docker_lifecycle import DockerLifecycleError
from harness.manifest import load_manifest
from harness.prompt_bank import PromptBank, PromptBankEntry, save_bank
from harness.runner import execute_run
from harness.schema import RunPlan


class FakeBackendClient:
    """Duck-typed stand-in for harness.client.BackendClient."""

    def __init__(
        self, health_error=None, wipe_error=None, seed_error=None, chat_errors=None,
        verdict_errors=None, series_report_error=None, experiment_report_error=None,
        randomize_error=None,
    ):
        self.health_error = health_error
        self.wipe_error = wipe_error
        self.seed_error = seed_error
        self.chat_errors = chat_errors or {}  # call-index -> exception instance
        self.verdict_errors = verdict_errors or {}  # call-index -> exception instance
        self.series_report_error = series_report_error
        self.experiment_report_error = experiment_report_error
        self.randomize_error = randomize_error
        self.chat_call_count = 0
        self.chat_messages = []
        self.chat_series_ids = []
        self.wipe_calls = []
        self.seed_calls = 0
        self.verdict_calls = []
        self.series_report_calls = []
        self.experiment_report_calls = []
        self.randomize_calls = 0

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
        self.chat_series_ids.append(series_id)
        if idx in self.chat_errors:
            raise self.chat_errors[idx]
        return {"run_id": f"run-{idx}", "response": "placeholder response"}

    def post_verdict(self, **kwargs):
        idx = len(self.verdict_calls)
        self.verdict_calls.append(kwargs)
        if idx in self.verdict_errors:
            raise self.verdict_errors[idx]
        return {"feedback_event_id": f"fbevent-{idx}"}

    def generate_series_report(self, experiment_id, series_id):
        self.series_report_calls.append((experiment_id, series_id))
        if self.series_report_error:
            raise self.series_report_error
        idx = len(self.series_report_calls)
        return {
            "report_id": f"series-report-{idx}",
            "json_path": f"/reports/series-{idx}.json",
            "md_path": f"/reports/series-{idx}.md",
            "run_count": 5,
        }

    def generate_experiment_report(self, experiment_id, control_experiment_ids=None):
        self.experiment_report_calls.append((experiment_id, control_experiment_ids))
        if self.experiment_report_error:
            raise self.experiment_report_error
        idx = len(self.experiment_report_calls)
        return {
            "report_id": f"experiment-report-{idx}",
            "json_path": f"/reports/experiment-{idx}.json",
            "md_path": f"/reports/experiment-{idx}.md",
            "series_count": 2,
        }

    def randomize_edges(self):
        self.randomize_calls += 1
        if self.randomize_error:
            raise self.randomize_error
        return {"edges_randomized": 32}


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


def test_resume_skips_restart_wipe_seed_for_already_complete_condition(tmp_path):
    """M19 Phase 3 — a resumed run must not restart/wipe/seed a condition
    whose turns are all already checkpointed; only the still-pending
    condition should touch the graph again."""
    plan = _plan(series_count=1, turns_per_series=2, probe_ratio=0.0, conditions=["learned", "static_weight"])

    # First run: "learned" completes both its turns (chat indices 0, 1);
    # "static_weight"'s first turn (chat index 2) hits a connectivity error
    # and aborts the run before any of its turns are checkpointed.
    failing_client = FakeBackendClient(chat_errors={2: BackendConnectivityError("dropped")})
    first_outcome = execute_run(plan, failing_client, tmp_path, print_fn=_noop_print)

    assert first_outcome.status == "aborted"
    assert failing_client.wipe_calls == ["p1_learned", "p1_static_weight"]
    conditions_done = {t.condition for t in first_outcome.checkpoint.turns}
    assert conditions_done == {"learned"}

    # Second run: resume. "learned" is fully done — its restart/wipe/seed
    # must not be repeated. "static_weight" resumes normally.
    healthy_client = FakeBackendClient()
    restart_calls = []

    second_outcome = execute_run(
        plan, healthy_client, tmp_path,
        resume_input_fn=lambda _p: "y", print_fn=_noop_print,
        docker_project_root=Path("/fake/project"),
        restart_backend_fn=lambda root, env: restart_calls.append(env),
    )

    assert second_outcome.status == "complete"
    assert healthy_client.wipe_calls == ["p1_static_weight"]  # "learned" skipped entirely
    assert healthy_client.seed_calls == 1
    assert restart_calls == [{"EXPERIMENT_ID": "p1_static_weight", "RUN_CONDITION": "static_weight"}]
    assert healthy_client.chat_call_count == 2  # only static_weight's 2 turns


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


def test_resume_declined_gets_a_new_run_id_and_experiment_id(tmp_path):
    """M19 Phase 3 collision fix: declining to resume an incomplete run must
    not reuse its run_id/experiment_id — wipe_graph() never deletes
    analytics/feedback_events, so reusing the identity would mix the
    abandoned attempt's data with the fresh one's."""
    plan = _plan(dry_run=True, dry_run_n=2, conditions=["learned"])

    failing_client = FakeBackendClient(chat_errors={1: BackendConnectivityError("dropped")})
    first_outcome = execute_run(plan, failing_client, tmp_path, print_fn=_noop_print)
    assert first_outcome.status == "aborted"
    assert failing_client.wipe_calls == ["p1_learned"]

    healthy_client = FakeBackendClient()
    second_outcome = execute_run(
        plan, healthy_client, tmp_path,
        resume_input_fn=lambda _p: "n", print_fn=_noop_print,
    )

    assert second_outcome.status == "complete"
    assert second_outcome.run_id != first_outcome.run_id
    assert second_outcome.run_id.endswith("_attempt2")
    # The fresh attempt wipes under a distinct experiment_id, not "p1_learned".
    assert healthy_client.wipe_calls == ["p1_attempt2_learned"]


def test_rerunning_an_already_complete_plan_gets_a_new_run_id_and_experiment_id(tmp_path):
    """M19 Phase 3 collision fix: re-invoking execute_run() for a plan that
    already ran to completion must not silently reuse its run_id/
    experiment_id (the pre-Phase-3 behavior — the resume-prompt block was
    skipped entirely for a "complete" checkpoint, so a fresh checkpoint
    was created at the *same* run_id, and new chat calls would have landed
    under the same experiment_id as the completed run's data)."""
    plan = _plan(dry_run=True, dry_run_n=1, conditions=["learned"])

    first_client = FakeBackendClient()
    first_outcome = execute_run(plan, first_client, tmp_path, print_fn=_noop_print)
    assert first_outcome.status == "complete"
    assert first_client.wipe_calls == ["p1_learned"]

    second_client = FakeBackendClient()
    second_outcome = execute_run(plan, second_client, tmp_path, print_fn=_noop_print)

    assert second_outcome.status == "complete"
    assert second_outcome.run_id != first_outcome.run_id
    assert second_outcome.run_id.endswith("_attempt2")
    assert second_client.wipe_calls == ["p1_attempt2_learned"]
    # The completed first attempt's own checkpoint/manifest files are untouched.
    reloaded_first = load_checkpoint(first_outcome.run_id, tmp_path)
    assert reloaded_first is not None
    assert reloaded_first.final_status == "complete"
    assert len(reloaded_first.turns) == 1


def test_resuming_an_incomplete_run_keeps_the_same_run_id_and_experiment_id(tmp_path):
    """Accepting a resume must NOT bump the attempt number — this is the
    common, everyday case and has to stay byte-for-byte the original
    run_id/experiment_id convention. wipe/seed must NOT be repeated for a
    condition resumed mid-flight (see
    test_resume_mid_series_preserves_graph_and_skips_wipe_seed for the
    graph-preservation rationale)."""
    plan = _plan(dry_run=True, dry_run_n=2, conditions=["learned"])

    failing_client = FakeBackendClient(chat_errors={1: BackendConnectivityError("dropped")})
    first_outcome = execute_run(plan, failing_client, tmp_path, print_fn=_noop_print)
    assert first_outcome.status == "aborted"

    healthy_client = FakeBackendClient()
    second_outcome = execute_run(
        plan, healthy_client, tmp_path,
        resume_input_fn=lambda _p: "y", print_fn=_noop_print,
    )

    assert second_outcome.status == "complete"
    assert second_outcome.run_id == first_outcome.run_id
    assert healthy_client.wipe_calls == []
    assert healthy_client.seed_calls == 0


def test_resume_mid_series_preserves_graph_and_skips_wipe_seed(tmp_path):
    """A condition resumed mid-flight (some turns already done, some
    pending) must not wipe/reseed the graph — doing so would erase the
    context (retrieved MUs, reinforced edges) the completed turns built up
    before the interruption. The backend restart still happens when
    configured, since that's needed for correct EXPERIMENT_ID stamping on
    the remaining turns and never touches the graph itself."""
    plan = _plan(dry_run=True, dry_run_n=3, conditions=["learned"])

    failing_client = FakeBackendClient(chat_errors={1: BackendConnectivityError("dropped")})
    first_outcome = execute_run(plan, failing_client, tmp_path, print_fn=_noop_print)
    assert first_outcome.status == "aborted"
    assert len(first_outcome.checkpoint.turns) == 1
    assert failing_client.wipe_calls == ["p1_learned"]  # first attempt: brand new, wipes normally

    healthy_client = FakeBackendClient()
    restart_calls = []
    second_outcome = execute_run(
        plan, healthy_client, tmp_path,
        resume_input_fn=lambda _p: "y", print_fn=_noop_print,
        docker_project_root=Path("/fake/project"),
        restart_backend_fn=lambda root, env: restart_calls.append(env),
    )

    assert second_outcome.status == "complete"
    assert healthy_client.wipe_calls == []  # graph preserved, not reset
    assert healthy_client.seed_calls == 0
    assert restart_calls == [{"EXPERIMENT_ID": "p1_learned", "RUN_CONDITION": "learned"}]  # still restarted for correct stamping
    assert healthy_client.chat_call_count == 2  # only the 2 remaining turns


def test_resume_mid_series_does_not_re_randomize_edges(tmp_path):
    """The random_weight control's re-randomization must happen exactly
    once per series — resuming mid-series must not re-randomize, or the
    turns before and after the interruption would see different random
    baselines within what's supposed to be one comparable series."""
    plan = _plan(dry_run=True, dry_run_n=3, conditions=["random_weight"])

    failing_client = FakeBackendClient(chat_errors={1: BackendConnectivityError("dropped")})
    first_outcome = execute_run(plan, failing_client, tmp_path, print_fn=_noop_print)
    assert first_outcome.status == "aborted"
    assert failing_client.randomize_calls == 1

    healthy_client = FakeBackendClient()
    second_outcome = execute_run(
        plan, healthy_client, tmp_path,
        resume_input_fn=lambda _p: "y", print_fn=_noop_print,
    )

    assert second_outcome.status == "complete"
    assert healthy_client.randomize_calls == 0


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


# ---------------------------------------------------------------------------
# M19 Phase 1 — prompt bank wiring
# ---------------------------------------------------------------------------

def _write_bank(tmp_path, n=5):
    entries = [
        PromptBankEntry(
            prompt_id=f"cluster_a_{i:04d}", cluster_id="cluster_a",
            prompt_text=f"real bank prompt {i}", hop_distance=0,
            criteria=["c1"], seed_fixture_version="v2",
            generated_by="claude-sonnet-5@2026-07-04", validated=True,
        )
        for i in range(n)
    ]
    bank = PromptBank(entries=entries, generation_params={}, content_hash="testhash123")
    return save_bank(bank, tmp_path)


def test_bulk_turn_message_equals_bank_entrys_prompt_text(tmp_path):
    bank_path = _write_bank(tmp_path / "banks")
    plan = _plan(dry_run=True, dry_run_n=3, probe_ratio=0.0, prompt_bank_path=str(bank_path))
    client = FakeBackendClient()

    outcome = execute_run(plan, client, tmp_path, print_fn=_noop_print)

    assert outcome.status == "complete"
    assert all(m.startswith("real bank prompt ") for m in client.chat_messages)


def test_missing_prompt_bank_file_hard_stops_before_wipe_graph_is_called(tmp_path):
    plan = _plan(dry_run=True, dry_run_n=2, prompt_bank_path=str(tmp_path / "does_not_exist.json"))
    client = FakeBackendClient()

    outcome = execute_run(plan, client, tmp_path, print_fn=_noop_print)

    assert outcome.status == "aborted"
    assert "prompt bank" in outcome.error_summary
    assert client.wipe_calls == []


# ---------------------------------------------------------------------------
# M19 Phase 2 — judge wiring
# ---------------------------------------------------------------------------


class FakeOllamaJudgeClient:
    """Duck-typed stand-in for harness.judge.OllamaJudgeClient."""

    def __init__(self, verdicts=None, available=True, chat_json_fn=None):
        self._verdicts = list(verdicts) if verdicts is not None else None
        self._default_verdict = {"verdict": "up", "rationale": "ok", "criterion_triggered": None}
        self.available = available
        self.calls = []
        if chat_json_fn is not None:
            self.chat_json = chat_json_fn

    def chat_json(self, model, temperature, prompt):
        self.calls.append(prompt)
        v = self._verdicts.pop(0) if self._verdicts else self._default_verdict
        return json.dumps(v)

    def model_available(self, model):
        return self.available


def _write_bank_with_criteria(tmp_path, n=5, cluster_id="cluster_a"):
    entries = [
        PromptBankEntry(
            prompt_id=f"{cluster_id}_{i:04d}", cluster_id=cluster_id,
            prompt_text=f"real bank prompt {i}", hop_distance=0,
            criteria=["mentions X"], seed_fixture_version="v2",
            generated_by="claude-sonnet-5@2026-07-04", validated=True,
        )
        for i in range(n)
    ]
    bank = PromptBank(entries=entries, generation_params={}, content_hash="testhash789")
    return save_bank(bank, tmp_path)


def test_learned_condition_up_verdict_applies_weight_update(tmp_path):
    bank_path = _write_bank_with_criteria(tmp_path / "banks")
    plan = _plan(
        dry_run=True, dry_run_n=2, probe_ratio=0.0, prompt_bank_path=str(bank_path),
        conditions=["learned"],
    )
    client = FakeBackendClient()
    judge_client = FakeOllamaJudgeClient(verdicts=[{"verdict": "up", "rationale": "good", "criterion_triggered": None}])

    outcome = execute_run(plan, client, tmp_path, print_fn=_noop_print, judge_client=judge_client)

    assert outcome.status == "complete"
    assert len(client.verdict_calls) == 2
    assert client.verdict_calls[0]["apply_weight_update"] is True
    assert client.verdict_calls[0]["condition_label"] == "learned"
    assert client.verdict_calls[0]["judge_source"] == "model:gemma4:e4b"
    assert outcome.checkpoint.turns[0].verdict == "up"
    assert outcome.checkpoint.turns[0].feedback_event_id == "fbevent-0"


def test_static_condition_up_verdict_does_not_apply_weight_update(tmp_path):
    bank_path = _write_bank_with_criteria(tmp_path / "banks")
    plan = _plan(
        dry_run=True, dry_run_n=1, probe_ratio=0.0, prompt_bank_path=str(bank_path),
        conditions=["static_weight"],
    )
    client = FakeBackendClient()
    judge_client = FakeOllamaJudgeClient(verdicts=[{"verdict": "up", "rationale": "good", "criterion_triggered": None}])

    execute_run(plan, client, tmp_path, print_fn=_noop_print, judge_client=judge_client)

    assert client.verdict_calls[0]["apply_weight_update"] is False
    assert client.verdict_calls[0]["condition_label"] == "static_weight"


def test_learned_condition_none_verdict_does_not_apply_weight_update(tmp_path):
    bank_path = _write_bank_with_criteria(tmp_path / "banks")
    plan = _plan(
        dry_run=True, dry_run_n=1, probe_ratio=0.0, prompt_bank_path=str(bank_path),
        conditions=["learned"],
    )
    client = FakeBackendClient()
    judge_client = FakeOllamaJudgeClient(verdicts=[{"verdict": "none", "rationale": "ambiguous", "criterion_triggered": None}])

    execute_run(plan, client, tmp_path, print_fn=_noop_print, judge_client=judge_client)

    assert client.verdict_calls[0]["apply_weight_update"] is False
    assert client.verdict_calls[0]["verdict"] == "none"


def test_no_judge_client_means_no_judging_happens(tmp_path):
    """judge_client=None (the default) — Phase 0/1 behavior is unchanged."""
    bank_path = _write_bank_with_criteria(tmp_path / "banks")
    plan = _plan(dry_run=True, dry_run_n=1, probe_ratio=0.0, prompt_bank_path=str(bank_path))
    client = FakeBackendClient()

    outcome = execute_run(plan, client, tmp_path, print_fn=_noop_print)

    assert outcome.status == "complete"
    assert client.verdict_calls == []
    assert outcome.checkpoint.turns[0].verdict is None


def test_unparseable_judge_soft_continues_and_checkpoint_notes_anomaly(tmp_path):
    bank_path = _write_bank_with_criteria(tmp_path / "banks")
    plan = _plan(
        dry_run=True, dry_run_n=1, probe_ratio=0.0, prompt_bank_path=str(bank_path),
        conditions=["learned"],
    )
    client = FakeBackendClient()
    judge_client = FakeOllamaJudgeClient(chat_json_fn=lambda model, temperature, prompt: "not json")

    outcome = execute_run(plan, client, tmp_path, print_fn=_noop_print, judge_client=judge_client)

    assert outcome.status == "complete"  # the chat turn itself succeeded
    assert client.verdict_calls == []  # no verdict posted
    assert outcome.checkpoint.turns[0].status == STATUS_OK
    assert outcome.checkpoint.turns[0].verdict is None
    assert outcome.checkpoint.turns[0].judge_error is not None
    assert "judge_unparseable" in outcome.checkpoint.turns[0].judge_error


def test_judge_unparseable_hard_stop_when_policy_overridden(tmp_path):
    bank_path = _write_bank_with_criteria(tmp_path / "banks")
    plan = _plan(
        dry_run=True, dry_run_n=1, probe_ratio=0.0, prompt_bank_path=str(bank_path),
        conditions=["learned"], error_policy={"judge_unparseable": "hard_stop"},
    )
    client = FakeBackendClient()
    judge_client = FakeOllamaJudgeClient(chat_json_fn=lambda model, temperature, prompt: "not json")

    outcome = execute_run(plan, client, tmp_path, print_fn=_noop_print, judge_client=judge_client)

    assert outcome.status == "aborted"
    assert "judge_unparseable" in outcome.error_summary


def test_interactive_override_sets_human_judge_source(tmp_path):
    bank_path = _write_bank_with_criteria(tmp_path / "banks")
    plan = _plan(
        dry_run=True, dry_run_n=1, probe_ratio=0.0, prompt_bank_path=str(bank_path),
        conditions=["learned"],
    )
    client = FakeBackendClient()
    judge_client = FakeOllamaJudgeClient(verdicts=[{"verdict": "up", "rationale": "good", "criterion_triggered": None}])

    scripted_answers = iter(["o", "down"])

    def scripted_input(_prompt):
        return next(scripted_answers)

    execute_run(
        plan, client, tmp_path, print_fn=_noop_print,
        judge_client=judge_client, interactive=True, resume_input_fn=scripted_input,
    )

    assert client.verdict_calls[0]["verdict"] == "down"
    assert client.verdict_calls[0]["judge_source"] == "human"


def test_interactive_accept_keeps_model_judge_source(tmp_path):
    bank_path = _write_bank_with_criteria(tmp_path / "banks")
    plan = _plan(
        dry_run=True, dry_run_n=1, probe_ratio=0.0, prompt_bank_path=str(bank_path),
        conditions=["learned"],
    )
    client = FakeBackendClient()
    judge_client = FakeOllamaJudgeClient(verdicts=[{"verdict": "up", "rationale": "good", "criterion_triggered": None}])

    def scripted_accept(_prompt):
        return "a"

    execute_run(
        plan, client, tmp_path, print_fn=_noop_print,
        judge_client=judge_client, interactive=True, resume_input_fn=scripted_accept,
    )

    assert client.verdict_calls[0]["verdict"] == "up"
    assert client.verdict_calls[0]["judge_source"] == "model:gemma4:e4b"


def test_judge_preflight_hard_stops_when_model_unavailable(tmp_path):
    bank_path = _write_bank_with_criteria(tmp_path / "banks")
    plan = _plan(dry_run=True, dry_run_n=1, probe_ratio=0.0, prompt_bank_path=str(bank_path))
    client = FakeBackendClient()
    judge_client = FakeOllamaJudgeClient(available=False)

    outcome = execute_run(plan, client, tmp_path, print_fn=_noop_print, judge_client=judge_client)

    assert outcome.status == "aborted"
    assert "judge model" in outcome.error_summary
    assert client.wipe_calls == []


def test_probe_turns_are_never_judged(tmp_path):
    bank_path = _write_bank_with_criteria(tmp_path / "banks")
    plan = _plan(
        dry_run=True, dry_run_n=3, probe_ratio=1.0, probe_placement="end",
        prompt_bank_path=str(bank_path), conditions=["learned"],
    )
    client = FakeBackendClient()
    judge_client = FakeOllamaJudgeClient()
    probes = [{"probe_id": "p06", "prompt_text": "What is the format rule?"}]

    outcome = execute_run(
        plan, client, tmp_path, probes=probes, print_fn=_noop_print, judge_client=judge_client,
    )

    assert outcome.status == "complete"
    assert client.verdict_calls == []
    assert judge_client.calls == []
    assert all(t.verdict is None for t in outcome.checkpoint.turns)


# ---------------------------------------------------------------------------
# M19 Phase 3 — series_id wiring + manifest series_id_map
# ---------------------------------------------------------------------------

def test_chat_receives_deterministic_series_id(tmp_path):
    plan = _plan(dry_run=True, dry_run_n=2, probe_ratio=0.0)
    client = FakeBackendClient()

    outcome = execute_run(plan, client, tmp_path, print_fn=_noop_print)

    assert outcome.status == "complete"
    assert client.chat_series_ids == ["p1_learned_s0", "p1_learned_s0"]


def test_manifest_records_series_id_map_for_all_conditions_and_series(tmp_path):
    plan = _plan(dry_run=True, dry_run_n=1)
    client = FakeBackendClient()

    outcome = execute_run(plan, client, tmp_path, print_fn=_noop_print)

    manifest = load_manifest(outcome.run_id, tmp_path)
    assert manifest.series_id_map["learned:0"] == "p1_learned_s0"
    assert manifest.series_id_map["learned:1"] == "p1_learned_s1"
    assert manifest.series_id_map["static_weight:0"] == "p1_static_weight_s0"
    assert manifest.series_id_map["static_weight:1"] == "p1_static_weight_s1"


# ---------------------------------------------------------------------------
# M19 Phase 3 — series-close + experiment-close reporting
# ---------------------------------------------------------------------------

def test_series_completion_triggers_series_report_with_correct_ids(tmp_path):
    # dry_run_n == turns_per_series (5): the full series_index=0 completes,
    # unlike the shorter dry runs elsewhere which never reach the last turn.
    plan = _plan(dry_run=True, dry_run_n=5)
    client = FakeBackendClient()

    outcome = execute_run(plan, client, tmp_path, print_fn=_noop_print)

    assert outcome.status == "complete"
    assert client.series_report_calls == [("p1_learned", "p1_learned_s0")]
    assert len(outcome.checkpoint.series_reports) == 1
    record = outcome.checkpoint.series_reports[0]
    assert record.condition == "learned"
    assert record.series_index == 0
    assert record.status == STATUS_OK
    assert record.report_id == "series-report-1"
    assert record.md_path == "/reports/series-1.md"


def test_multi_series_full_run_generates_one_series_report_per_series(tmp_path):
    plan = _plan(series_count=2, turns_per_series=2, probe_ratio=0.0, conditions=["learned"])
    client = FakeBackendClient()

    outcome = execute_run(plan, client, tmp_path, print_fn=_noop_print)

    assert outcome.status == "complete"
    assert client.series_report_calls == [
        ("p1_learned", "p1_learned_s0"),
        ("p1_learned", "p1_learned_s1"),
    ]
    assert len(outcome.checkpoint.series_reports) == 2


def test_series_report_failure_soft_continues_and_checkpoint_records_error(tmp_path):
    plan = _plan(dry_run=True, dry_run_n=5)
    client = FakeBackendClient(series_report_error=ReportGenerationError("bogus series_id"))

    outcome = execute_run(plan, client, tmp_path, print_fn=_noop_print)

    assert outcome.status == "complete"  # the run itself still completes
    assert len(outcome.checkpoint.series_reports) == 1
    record = outcome.checkpoint.series_reports[0]
    assert record.status == STATUS_SOFT_CONTINUE
    assert "bogus series_id" in record.error


def test_series_report_failure_hard_stops_when_policy_overridden(tmp_path):
    plan = _plan(dry_run=True, dry_run_n=5, error_policy={"report_generation": "hard_stop"})
    client = FakeBackendClient(series_report_error=ReportGenerationError("boom"))

    outcome = execute_run(plan, client, tmp_path, print_fn=_noop_print)

    assert outcome.status == "aborted"
    assert "report_generation" in outcome.error_summary


def test_experiment_close_calls_generate_experiment_with_control_ids(tmp_path):
    plan = _plan(
        series_count=1, turns_per_series=1, probe_ratio=0.0,
        conditions=["learned", "static_weight", "random_weight"],
    )
    client = FakeBackendClient()

    outcome = execute_run(plan, client, tmp_path, print_fn=_noop_print)

    assert outcome.status == "complete"
    assert client.experiment_report_calls == [
        ("p1_learned", {"static_weight": "p1_static_weight", "random_weight": "p1_random_weight"})
    ]
    assert outcome.checkpoint.experiment_report.status == STATUS_OK
    assert outcome.checkpoint.experiment_report.md_path == "/reports/experiment-1.md"


def test_experiment_close_skipped_when_learned_condition_did_not_run(tmp_path):
    plan = _plan(dry_run=True, dry_run_n=1, conditions=["static_weight"])
    client = FakeBackendClient()

    outcome = execute_run(plan, client, tmp_path, print_fn=_noop_print)

    assert outcome.status == "complete"
    assert client.experiment_report_calls == []
    assert outcome.checkpoint.experiment_report is None


def test_hard_stop_mid_run_skips_experiment_close_but_still_notifies_aborted(tmp_path):
    plan = _plan(dry_run=True, dry_run_n=2)
    client = FakeBackendClient(health_error=BackendConnectivityError("down"))

    outcome = execute_run(plan, client, tmp_path, print_fn=_noop_print)

    assert outcome.status == "aborted"
    assert client.experiment_report_calls == []
    assert outcome.checkpoint.experiment_report is None
    assert outcome.notification_log_path.exists()
    lines = outcome.notification_log_path.read_text(encoding="utf-8").strip().splitlines()
    last_record = json.loads(lines[-1])
    assert last_record["status"] == "aborted"
    assert last_record["report_path"] is None


# ---------------------------------------------------------------------------
# M19 Phase 3 — random_weight edge randomization
# ---------------------------------------------------------------------------

def test_random_weight_condition_randomizes_edges_once_per_series(tmp_path):
    plan = _plan(series_count=2, turns_per_series=2, probe_ratio=0.0, conditions=["random_weight"])
    client = FakeBackendClient()

    outcome = execute_run(plan, client, tmp_path, print_fn=_noop_print)

    assert outcome.status == "complete"
    assert client.randomize_calls == 2  # once per series, not once per turn


def test_non_random_weight_conditions_never_randomize_edges(tmp_path):
    plan = _plan(dry_run=True, dry_run_n=2)
    client = FakeBackendClient()

    execute_run(plan, client, tmp_path, print_fn=_noop_print)

    assert client.randomize_calls == 0


# ---------------------------------------------------------------------------
# M19 Phase 3 — Docker lifecycle (per-condition EXPERIMENT_ID restart)
# ---------------------------------------------------------------------------

def test_docker_restart_called_once_per_condition_when_project_root_given(tmp_path):
    plan = _plan(dry_run=True, dry_run_n=1)  # dry-run scopes to one condition ("learned")
    client = FakeBackendClient()
    restart_calls = []
    health_calls = []

    def fake_restart(project_root, env_overrides):
        restart_calls.append((project_root, env_overrides))

    def fake_wait(client_arg):
        health_calls.append(client_arg)

    outcome = execute_run(
        plan, client, tmp_path, print_fn=_noop_print,
        docker_project_root=Path("/fake/project"),
        restart_backend_fn=fake_restart, wait_for_health_fn=fake_wait,
    )

    assert outcome.status == "complete"
    assert restart_calls == [
        (Path("/fake/project"), {"EXPERIMENT_ID": "p1_learned", "RUN_CONDITION": "learned"})
    ]
    assert len(health_calls) == 1


def test_docker_restart_skipped_by_default(tmp_path):
    plan = _plan(dry_run=True, dry_run_n=1)
    client = FakeBackendClient()
    calls = []

    execute_run(
        plan, client, tmp_path, print_fn=_noop_print,
        restart_backend_fn=lambda *a, **k: calls.append(a),
    )

    assert calls == []  # docker_project_root defaults to None


def test_docker_restart_failure_hard_stops_before_wipe(tmp_path):
    plan = _plan(dry_run=True, dry_run_n=1)
    client = FakeBackendClient()

    def failing_restart(project_root, env_overrides):
        raise DockerLifecycleError("docker compose up failed")

    outcome = execute_run(
        plan, client, tmp_path, print_fn=_noop_print,
        docker_project_root=Path("/fake/project"), restart_backend_fn=failing_restart,
    )

    assert outcome.status == "aborted"
    assert "docker restart failed" in outcome.error_summary
    assert client.wipe_calls == []


# ---------------------------------------------------------------------------
# M19 Phase 3 — budget_cap enforcement
# ---------------------------------------------------------------------------

def test_budget_cap_prerun_over_estimate_hard_stops_with_zero_client_calls(tmp_path):
    plan = _plan(dry_run=True, dry_run_n=2, budget_cap=0.01)
    client = FakeBackendClient()

    outcome = execute_run(
        plan, client, tmp_path, print_fn=_noop_print, bank_generation_cost_usd=5.0,
    )

    assert outcome.status == "aborted"
    assert "budget_cap" in outcome.error_summary
    assert client.wipe_calls == []
    assert client.chat_call_count == 0


def test_budget_cap_none_never_stops_even_with_high_bank_cost(tmp_path):
    plan = _plan(dry_run=True, dry_run_n=1, budget_cap=None)
    client = FakeBackendClient()

    outcome = execute_run(
        plan, client, tmp_path, print_fn=_noop_print, bank_generation_cost_usd=99999.0,
    )

    assert outcome.status == "complete"


def test_budget_cap_exceeded_by_projected_judge_costs_hard_stops_before_any_turns(tmp_path):
    bank_path = _write_bank_with_criteria(tmp_path / "banks", n=3)
    plan = _plan(
        dry_run=True, dry_run_n=3, probe_ratio=0.0, prompt_bank_path=str(bank_path),
        conditions=["learned"], budget_cap=0.03,
    )
    client = FakeBackendClient()
    judge_client = FakeOllamaJudgeClient(
        verdicts=[{"verdict": "up", "rationale": "ok", "criterion_triggered": None} for _ in range(3)]
    )

    outcome = execute_run(
        plan, client, tmp_path, print_fn=_noop_print, judge_client=judge_client,
        judge_cost_per_call=0.02,  # 3 bulk turns x $0.02 = $0.06 > $0.03 cap
    )

    assert outcome.status == "aborted"
    assert "budget_cap" in outcome.error_summary
    assert client.chat_call_count == 0  # caught before any graph mutation or turn


def test_budget_cap_within_estimate_allows_judge_calls_to_proceed(tmp_path):
    bank_path = _write_bank_with_criteria(tmp_path / "banks", n=2)
    plan = _plan(
        dry_run=True, dry_run_n=2, probe_ratio=0.0, prompt_bank_path=str(bank_path),
        conditions=["learned"], budget_cap=1.0,
    )
    client = FakeBackendClient()
    judge_client = FakeOllamaJudgeClient(
        verdicts=[{"verdict": "up", "rationale": "ok", "criterion_triggered": None} for _ in range(2)]
    )

    outcome = execute_run(
        plan, client, tmp_path, print_fn=_noop_print, judge_client=judge_client,
        judge_cost_per_call=0.01,
    )

    assert outcome.status == "complete"
    assert len(client.verdict_calls) == 2
