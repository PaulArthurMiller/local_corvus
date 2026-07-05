"""
M20 Phase 1 §8 sign-off gate — trace test.

Replays a seeded run segment containing an up verdict and a down verdict on
a graph-traversal (K2) edge, then demonstrates — from persisted logs alone
(the analytics run record + edge_update_events), with NO re-execution of
retrieval or the update mechanism — the full chain:

    feedback event -> per-edge increment -> weight-as-of-turn ->
    traversal path -> retrieved set -> (gold-MU) rank

Uses a real GraphStore against a temp SQLite file (no Docker) so every
number below comes from an actual database round-trip, not a hand-built dict.
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
from analytics.run_log import RunRecord, capture_run
from learning.edge_update import process_verdict_for_learning
from memory.retrieval import retrieve
from memory.store import GraphStore


def run(coro):
    return asyncio.get_event_loop().run_until_complete(coro)


class TempDBStore:
    def __enter__(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        self._db_path = Path(self._tmpdir.name) / "trace_test.db"
        self._patcher = mock.patch.object(store_module, "_DB_PATH", self._db_path)
        self._patcher.start()
        self._store = GraphStore()
        return self._store

    def __exit__(self, *args):
        self._patcher.stop()
        self._tmpdir.cleanup()


def _cfg() -> dict:
    return {
        "hyperparameters": {
            "retrieval_keyword_pool_size": 20,
            "retrieval_keyword_count": 5,
            "retrieval_level1_top_k": 1,
            "retrieval_level2_top_k": 1,
            "directional_edge_weight_multiplier": 1.5,
        },
        "edge_update": {"weight_mode": "mean"},
    }


EDGE_UPDATE_RUNTIME_CFG = {
    "credit_scale": 1.0,
    "credit_lambda": 0.5,
    "negative_scope": "path",
    "weight_mode": "mean",
    "evidence_cap": {"enabled": False, "n_max": 50.0},
}


def test_trace_reconstructs_full_chain_from_logs_alone():
    with TempDBStore() as s:
        # --- Seed a small graph: an entry MU (K1, reached by BM25) with a
        # traversal edge to a bridge/gold MU (K2), plus decoys so the K1
        # selection is non-trivial. ---
        entry_mu = str(uuid.uuid4())
        gold_mu = str(uuid.uuid4())
        decoy_mu = str(uuid.uuid4())
        run(s.create_mu(mu_id=entry_mu, source_type="user_input",
                         raw_content="clusterphrase alpha topic clusterphrase beta"))
        run(s.create_mu(mu_id=gold_mu, source_type="agent_output",
                         raw_content="the bridge memory unit content"))
        run(s.create_mu(mu_id=decoy_mu, source_type="user_input",
                         raw_content="unrelated giraffe savannah content"))

        bridge_edge = run(s.create_edge(
            source_mu_id=entry_mu, target_mu_id=gold_mu, weight=0.5, alpha=1.0, beta=1.0,
        ))

        # --- Turn 1: retrieval surfaces entry_mu (K1) and gold_mu (K2 via bridge_edge) ---
        retrieval = run(retrieve("clusterphrase alpha beta topic", s, _cfg(), condition="learned"))
        assert gold_mu in retrieval.activated_mu_ids, "bridge/gold MU must surface via K2 traversal"
        assert retrieval.hop_depth_per_retrieved_mu[gold_mu] == 1
        assert any(hop["edge_id"] == bridge_edge["edge_id"] for hop in retrieval.traversal_path)

        turn1_run_id = str(uuid.uuid4())
        turn1_record = RunRecord(
            run_id=turn1_run_id,
            series_id="series-1",
            experiment_id="exp-trace",
            activated_mu_ids=retrieval.activated_mu_ids,
            activated_edge_ids=retrieval.activated_edge_ids,
            traversal_path=retrieval.traversal_path,
            hop_depth_per_retrieved_mu=retrieval.hop_depth_per_retrieved_mu,
            edge_weights_used=retrieval.edge_weights_used,
            turn_index=1,
            condition_label="learned",
            is_probe_turn=False,
        )
        run(capture_run(turn1_record))

        # --- Verdict 1: "up" on turn 1 (gold MU's response satisfied criteria) ---
        persisted_turn1 = run(s.get_run(turn1_run_id))
        results_up = run(process_verdict_for_learning(
            persisted_turn1, "up", "learned", "fb-up-1", s, EDGE_UPDATE_RUNTIME_CFG,
        ))
        assert len(results_up) == 1
        assert results_up[0]["edge_id"] == bridge_edge["edge_id"]

        # --- Turn 2: same retrieval shape (weight has moved since turn 1) ---
        retrieval2 = run(retrieve("clusterphrase alpha beta topic", s, _cfg(), condition="learned"))
        turn2_run_id = str(uuid.uuid4())
        turn2_record = RunRecord(
            run_id=turn2_run_id,
            series_id="series-1",
            experiment_id="exp-trace",
            activated_mu_ids=retrieval2.activated_mu_ids,
            activated_edge_ids=retrieval2.activated_edge_ids,
            traversal_path=retrieval2.traversal_path,
            hop_depth_per_retrieved_mu=retrieval2.hop_depth_per_retrieved_mu,
            edge_weights_used=retrieval2.edge_weights_used,
            turn_index=2,
            condition_label="learned",
            is_probe_turn=False,
        )
        run(capture_run(turn2_record))

        # --- Verdict 2: "down" on turn 2 ---
        persisted_turn2 = run(s.get_run(turn2_run_id))
        results_down = run(process_verdict_for_learning(
            persisted_turn2, "down", "learned", "fb-down-1", s, EDGE_UPDATE_RUNTIME_CFG,
        ))
        assert len(results_down) == 1

        # =====================================================================
        # RECONSTRUCTION — from logs alone, no re-execution of retrieve() or
        # process_verdict_for_learning() beyond what already ran above.
        # =====================================================================

        # 1. feedback event -> per-edge increment: the edge_update_events log
        #    has exactly the two increments we just applied, in order.
        events = run(s.get_edge_update_events_for_edge(bridge_edge["edge_id"]))
        assert len(events) == 2
        assert events[0]["verdict"] == "up" and events[0]["verdict_id"] == "fb-up-1"
        assert events[1]["verdict"] == "down" and events[1]["verdict_id"] == "fb-down-1"
        assert events[0]["alpha_before"] == 1.0 and events[0]["alpha_after"] == 2.0
        assert events[1]["beta_before"] == 1.0 and events[1]["beta_after"] == 2.0

        # 2. weight-as-of-turn: for any turn_index, the weight is
        #    alpha_after/(alpha_after+beta_after) of the latest event at or
        #    before that turn (or the init prior if none exist) — reconstructed
        #    purely by scanning the event log, no DB "current state" needed.
        def weight_as_of_turn(turn_index: int) -> float:
            applicable = [e for e in events if (e["turn_index"] or 0) <= turn_index]
            if not applicable:
                return 0.5  # Beta(1,1) prior
            latest = applicable[-1]
            return latest["alpha_after"] / (latest["alpha_after"] + latest["beta_after"])

        assert weight_as_of_turn(0) == pytest.approx(0.5)  # before any verdict
        assert weight_as_of_turn(1) == pytest.approx(2.0 / 3.0)  # after the "up" verdict
        assert weight_as_of_turn(2) == pytest.approx(2.0 / 4.0)  # after the "down" verdict too

        # Cross-check against the actual current DB state (proves the log
        # reconstruction and the live counters agree).
        live_edge = run(s.get_edge(bridge_edge["edge_id"]))
        live_mean = live_edge["alpha"] / (live_edge["alpha"] + live_edge["beta"])
        assert weight_as_of_turn(2) == pytest.approx(live_mean)

        # 3. traversal path -> retrieved set -> gold-MU rank: reconstructed
        #    entirely from the persisted analytics rows (turn1/turn2 records),
        #    not from re-running retrieve().
        fetched_turn1 = run(s.get_run(turn1_run_id))
        assert fetched_turn1["traversal_path"] == retrieval.traversal_path
        assert gold_mu in fetched_turn1["activated_mu_ids"]
        # "Rank" here: gold_mu's position in the retrieved set (K1 entries
        # first, then K2) — reconstructable from activated_mu_ids order alone.
        gold_rank = fetched_turn1["activated_mu_ids"].index(gold_mu) + 1
        assert gold_rank == len(fetched_turn1["activated_mu_ids"])  # K2 MU ranks last
