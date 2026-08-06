"""
Tests for GraphStore.delete_turn_artifacts — turn-scoped rollback.

Exists because a failed turn used to strand its input and prompt MUs in the
graph permanently: the orchestrator writes both *before* calling Ollama and its
HTTPStatusError path re-raises without undoing them. Retrieval reads at
K1=3 + K2=1, a four-slot window, so a stranded near-duplicate of the user's
message evicts a real seed MU rather than merely adding noise — and it carries
no seed edges, so traversals reaching it train edges the experiment never
intended to touch.

The interesting case is the co_activation edge: it points from the turn's
output MU at a *surviving* seed MU, so deleting by MU id alone leaves it
dangling. That is why run_id is a separate argument.
"""

import asyncio
import os
import sys
import tempfile
import uuid
from pathlib import Path
from unittest import mock

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "backend"))

import memory.store as store_module
from memory.store import GraphStore


def run(coro):
    return asyncio.get_event_loop().run_until_complete(coro)


class TempDBStore:
    """Context manager that gives a GraphStore pointed at a temp file."""

    def __enter__(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        self._db_path = Path(self._tmpdir.name) / "test_graph.db"
        self._patcher = mock.patch.object(store_module, "_DB_PATH", self._db_path)
        self._patcher.start()
        self._store = GraphStore()
        return self._store

    def __exit__(self, *args):
        self._patcher.stop()
        self._tmpdir.cleanup()


def _seed_turn(store, run_id):
    """
    Build the graph shape a real turn leaves behind: a surviving seed MU, the
    turn's own input/prompt/output MUs, the prompt->input edge, and a
    co_activation edge from the output MU to the surviving seed MU.

    Returns (seed_mu_id, turn_mu_ids).
    """
    seed_mu = str(uuid.uuid4())
    input_mu = str(uuid.uuid4())
    prompt_mu = str(uuid.uuid4())
    output_mu = str(uuid.uuid4())

    run(store.create_mu(mu_id=seed_mu, source_type="seed", raw_content="seed fact"))
    run(store.create_mu(mu_id=input_mu, source_type="user_input", raw_content="the question"))
    run(store.create_mu(mu_id=prompt_mu, source_type="prompt", raw_content="assembled prompt"))
    run(store.create_mu(mu_id=output_mu, source_type="agent_output", raw_content="the answer"))

    run(store.create_edge(
        source_mu_id=prompt_mu, target_mu_id=input_mu, weight=0.5,
        direction="forward", creation_run_id=run_id,
        creation_trigger="prompt_construction",
    ))
    run(store.create_edge(
        source_mu_id=output_mu, target_mu_id=seed_mu, weight=0.5,
        direction="forward", creation_run_id=run_id,
        creation_trigger="co_activation",
    ))

    return seed_mu, [input_mu, prompt_mu, output_mu]


class TestDeleteTurnArtifacts:

    def test_deletes_the_turns_mus(self):
        with TempDBStore() as s:
            run_id = str(uuid.uuid4())
            _, turn_mus = _seed_turn(s, run_id)

            result = run(s.delete_turn_artifacts(turn_mus, run_id=run_id))

            print(f"[test] result: {result}")
            assert result["mus_deleted"] == 3
            for mu_id in turn_mus:
                assert run(s.get_mu(mu_id)) is None

    def test_leaves_surviving_seed_mus_alone(self):
        """Rollback must undo the turn, not damage the graph it ran against."""
        with TempDBStore() as s:
            run_id = str(uuid.uuid4())
            seed_mu, turn_mus = _seed_turn(s, run_id)

            run(s.delete_turn_artifacts(turn_mus, run_id=run_id))

            surviving = run(s.get_mu(seed_mu))
            print(f"[test] surviving seed MU: {surviving}")
            assert surviving is not None
            assert surviving["raw_content"] == "seed fact"

    def test_deletes_the_co_activation_edge_pointing_at_a_surviving_mu(self):
        """
        The case MU-id deletion alone cannot reach: output_mu -> seed_mu. The
        source is deleted, the target survives, so without the run_id sweep this
        edge would dangle against a nonexistent source MU.
        """
        with TempDBStore() as s:
            run_id = str(uuid.uuid4())
            seed_mu, turn_mus = _seed_turn(s, run_id)

            run(s.delete_turn_artifacts(turn_mus, run_id=run_id))

            edges = run(s.get_edges_for_mu(seed_mu))
            print(f"[test] edges left on surviving seed MU: {edges}")
            assert edges == []

    def test_deletes_every_edge_the_turn_created(self):
        with TempDBStore() as s:
            run_id = str(uuid.uuid4())
            _, turn_mus = _seed_turn(s, run_id)

            result = run(s.delete_turn_artifacts(turn_mus, run_id=run_id))

            print(f"[test] edges_deleted: {result['edges_deleted']}")
            assert result["edges_deleted"] == 2

    def test_does_not_touch_edges_from_other_runs(self):
        """A concurrent or prior turn's edges must survive this turn's rollback."""
        with TempDBStore() as s:
            keep_run_id = str(uuid.uuid4())
            seed_mu, _ = _seed_turn(s, keep_run_id)

            fail_run_id = str(uuid.uuid4())
            _, failed_mus = _seed_turn(s, fail_run_id)

            run(s.delete_turn_artifacts(failed_mus, run_id=fail_run_id))

            kept = run(s.get_edges_for_mu(seed_mu))
            print(f"[test] edges surviving from the earlier run: {kept}")
            assert len(kept) == 1
            assert kept[0]["creation_run_id"] == keep_run_id

    def test_empty_inputs_are_a_no_op(self):
        with TempDBStore() as s:
            run_id = str(uuid.uuid4())
            seed_mu, _ = _seed_turn(s, run_id)

            result = run(s.delete_turn_artifacts([], run_id=None))

            print(f"[test] no-op result: {result}")
            assert result == {"mus_deleted": 0, "edges_deleted": 0}
            assert run(s.get_mu(seed_mu)) is not None

    def test_unknown_mu_ids_are_tolerated(self):
        """Rollback runs in an exception handler — it must not blow up on drift."""
        with TempDBStore() as s:
            result = run(s.delete_turn_artifacts(
                [str(uuid.uuid4()), str(uuid.uuid4())], run_id=str(uuid.uuid4())
            ))

            print(f"[test] unknown-id result: {result}")
            assert result["mus_deleted"] == 0
            assert result["edges_deleted"] == 0

    def test_rollback_leaves_the_graph_exactly_as_it_was(self):
        """The whole point: a failed turn is indistinguishable from one never run."""
        with TempDBStore() as s:
            seed_mu = str(uuid.uuid4())
            run(s.create_mu(mu_id=seed_mu, source_type="seed", raw_content="seed fact"))
            before = run(s.get_all_mus())

            run_id = str(uuid.uuid4())
            turn_mus = []
            for source_type in ("user_input", "prompt", "agent_output"):
                mu_id = str(uuid.uuid4())
                run(s.create_mu(mu_id=mu_id, source_type=source_type, raw_content="x"))
                turn_mus.append(mu_id)
            run(s.create_edge(
                source_mu_id=turn_mus[2], target_mu_id=seed_mu, weight=0.5,
                direction="forward", creation_run_id=run_id,
                creation_trigger="co_activation",
            ))

            run(s.delete_turn_artifacts(turn_mus, run_id=run_id))

            after = run(s.get_all_mus())
            print(f"[test] before={len(before)} after={len(after)}")
            assert [m["mu_id"] for m in after] == [m["mu_id"] for m in before]
