"""
Tests for run_interaction's failed-turn rollback (orchestrator.py).

The bug these pin: the orchestrator writes the input and prompt MUs *before*
calling Ollama, and its httpx.HTTPStatusError path re-raises without undoing
them. The 2026-08-06 harness abort left exactly such an orphan behind, and a
later verification /chat retrieved it — so this is an observed failure, not a
theoretical one. Once the harness retries transient failures, every retry
compounds it.

Why it matters more than "an extra row": retrieval reads at K1=3 + K2=1, a
four-slot window. An orphaned near-duplicate of the user's message *evicts* a
real seed MU from that turn's context rather than merely adding noise, and it
carries no seed edges, so traversals that reach it earn evidence on edges the
experiment never intended to train.

These tests exercise the wrapper and the rollback helper directly. The store's
own delete semantics are covered in tests/test_memory/test_delete_turn_artifacts.py.
"""

import asyncio
import os
import sys
from unittest.mock import AsyncMock, patch

import httpx
import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "backend"))

import core.orchestrator as orch


def run(coro):
    return asyncio.get_event_loop().run_until_complete(coro)


# ---------------------------------------------------------------------------
# run_interaction wrapper — when the turn fails, the graph is restored
# ---------------------------------------------------------------------------


class TestRunInteractionRollsBackOnFailure:

    def _patch_inner(self, side_effect, records=("mu-input", "mu-prompt")):
        """
        Replace the pipeline body with a stub that writes MUs (by recording
        their ids in the tracker, exactly as the real one does) and then fails.
        """
        async def fake_inner(*, message, series_id, attachments, run_id, created_mu_ids):
            created_mu_ids.extend(records)
            raise side_effect

        return patch.object(orch, "_run_interaction_inner", side_effect=fake_inner)

    def test_ollama_500_rolls_back_the_mus_the_turn_wrote(self):
        """The exact 2026-08-06 fault: Ollama 500 after the MUs are persisted."""
        request = httpx.Request("POST", "http://host.docker.internal:11434/v1/chat/completions")
        error = httpx.HTTPStatusError(
            "Server error '500 Internal Server Error'",
            request=request,
            response=httpx.Response(500, request=request),
        )
        delete = AsyncMock(return_value={"mus_deleted": 2, "edges_deleted": 1})

        with self._patch_inner(error), patch.object(
            orch.graph_store, "delete_turn_artifacts", delete
        ):
            with pytest.raises(httpx.HTTPStatusError):
                run(orch.run_interaction("the question", "series-1"))

        print(f"[test] delete called with: {delete.call_args}")
        delete.assert_awaited_once()
        assert delete.call_args.args[0] == ["mu-input", "mu-prompt"]

    def test_the_original_error_still_propagates(self):
        """Rollback must not swallow the fault — the harness classifies on it."""
        error = httpx.ConnectError("connection refused")
        with self._patch_inner(error), patch.object(
            orch.graph_store, "delete_turn_artifacts", AsyncMock(return_value={})
        ):
            with pytest.raises(httpx.ConnectError):
                run(orch.run_interaction("msg", "series-1"))

    def test_rollback_is_scoped_to_this_turns_run_id(self):
        delete = AsyncMock(return_value={"mus_deleted": 2, "edges_deleted": 1})
        with self._patch_inner(RuntimeError("boom")), patch.object(
            orch.graph_store, "delete_turn_artifacts", delete
        ):
            with pytest.raises(RuntimeError):
                run(orch.run_interaction("msg", "series-1"))

        run_id = delete.call_args.kwargs["run_id"]
        print(f"[test] rollback run_id: {run_id}")
        assert run_id is not None and len(run_id) > 0

    def test_any_failure_rolls_back_not_just_http_ones(self):
        """'Regardless of source of failure' — a gate or DB error strands MUs too."""
        delete = AsyncMock(return_value={"mus_deleted": 2, "edges_deleted": 0})
        with self._patch_inner(ValueError("unexpected pipeline bug")), patch.object(
            orch.graph_store, "delete_turn_artifacts", delete
        ):
            with pytest.raises(ValueError):
                run(orch.run_interaction("msg", "series-1"))

        delete.assert_awaited_once()

    def test_cancellation_also_rolls_back(self):
        """
        CancelledError is a BaseException, not an Exception. A client that
        disconnects mid-turn strands MUs exactly like a crash does, so the
        wrapper catches BaseException deliberately.
        """
        delete = AsyncMock(return_value={"mus_deleted": 2, "edges_deleted": 0})
        with self._patch_inner(asyncio.CancelledError()), patch.object(
            orch.graph_store, "delete_turn_artifacts", delete
        ):
            with pytest.raises(asyncio.CancelledError):
                run(orch.run_interaction("msg", "series-1"))

        delete.assert_awaited_once()

    def test_nothing_is_deleted_when_the_turn_wrote_nothing(self):
        """Failing before the first MU write must not issue a pointless delete."""
        delete = AsyncMock()
        with self._patch_inner(RuntimeError("early"), records=()), patch.object(
            orch.graph_store, "delete_turn_artifacts", delete
        ):
            with pytest.raises(RuntimeError):
                run(orch.run_interaction("msg", "series-1"))

        delete.assert_not_awaited()


class TestSuccessfulTurnIsNotRolledBack:

    def test_a_completed_turn_keeps_its_mus(self):
        """The obvious regression risk in adding a rollback: over-eager deletion."""
        sentinel = object()

        async def fake_inner(*, message, series_id, attachments, run_id, created_mu_ids):
            created_mu_ids.extend(["mu-input", "mu-prompt", "mu-output"])
            return sentinel

        delete = AsyncMock()
        with patch.object(orch, "_run_interaction_inner", side_effect=fake_inner), \
                patch.object(orch.graph_store, "delete_turn_artifacts", delete):
            result = run(orch.run_interaction("msg", "series-1"))

        assert result is sentinel
        delete.assert_not_awaited()


# ---------------------------------------------------------------------------
# _rollback_failed_turn — must never mask the fault it is cleaning up after
# ---------------------------------------------------------------------------


class TestRollbackNeverRaises:

    def test_a_failing_delete_is_swallowed(self):
        """
        Rollback runs inside an exception handler. If it raised, a diagnosable
        Ollama fault would be replaced by a confusing SQLite one.
        """
        delete = AsyncMock(side_effect=RuntimeError("database is locked"))
        with patch.object(orch.graph_store, "delete_turn_artifacts", delete):
            run(orch._rollback_failed_turn(
                ["mu-1"], "run-1", httpx.ConnectError("original fault")
            ))

    def test_orphan_ids_are_logged_when_rollback_fails(self, caplog):
        """If cleanup fails the ids must be recoverable by hand from the log."""
        delete = AsyncMock(side_effect=RuntimeError("database is locked"))
        with patch.object(orch.graph_store, "delete_turn_artifacts", delete):
            with caplog.at_level("ERROR"):
                run(orch._rollback_failed_turn(
                    ["mu-orphan-1"], "run-1", httpx.ConnectError("original")
                ))

        print(f"[test] captured: {caplog.text}")
        assert "ROLLBACK FAILED" in caplog.text
        assert "mu-orphan-1" in caplog.text

    def test_successful_rollback_logs_what_it_deleted(self, caplog):
        delete = AsyncMock(return_value={"mus_deleted": 2, "edges_deleted": 1})
        with patch.object(orch.graph_store, "delete_turn_artifacts", delete):
            with caplog.at_level("WARNING"):
                run(orch._rollback_failed_turn(
                    ["mu-1", "mu-2"], "run-1", httpx.ConnectError("original")
                ))

        print(f"[test] captured: {caplog.text}")
        assert "rolled back failed turn" in caplog.text
        assert "run-1" in caplog.text
