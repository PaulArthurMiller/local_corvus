"""
Tests for M19 Phase 2: POST /feedback/verdict, and the /feedback regression
covering its new judge_source="human" stamp.

Calls the route handler functions directly (matching the existing convention
in tests/test_api/test_graph_wipe.py) rather than spinning up a FastAPI
TestClient.
"""

import asyncio
import os
import sys
from unittest.mock import AsyncMock, patch

import pytest
from fastapi import HTTPException

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "backend"))

from api.chat import FeedbackRequest, VerdictRequest, feedback, feedback_verdict


def run(coro):
    return asyncio.get_event_loop().run_until_complete(coro)


def _run_record(run_id="run-1", activated_edge_ids=None, activated_mu_ids=None, turn_index=3):
    # M20 Phase 1: process_verdict_for_learning() credits a MU's *path*
    # edges (reconstructed from hop_depth_per_retrieved_mu + traversal_path),
    # not the flat activated_edge_ids list SimpleRL used to iterate over.
    # "mu-1" here is a K2 (hop_depth=1) MU reached via "edge-1", matching the
    # single-hop retrieval design (see edge_update.get_path_edge_ids_for_mu).
    mu_ids = activated_mu_ids if activated_mu_ids is not None else ["mu-1"]
    edge_ids = activated_edge_ids if activated_edge_ids is not None else ["edge-1"]
    return {
        "run_id": run_id,
        "activated_edge_ids": edge_ids,
        "activated_mu_ids": mu_ids,
        "hop_depth_per_retrieved_mu": {mu_id: 1 for mu_id in mu_ids},
        "traversal_path": [
            {"source_mu_id": "mu-src", "edge_id": edge_id, "target_mu_id": mu_id, "hop_depth": 1}
            for edge_id, mu_id in zip(edge_ids, mu_ids)
        ],
        "turn_index": turn_index,
    }


def _edge(edge_id="edge-1", weight=0.5, alpha=1.0, beta=1.0):
    return {
        "edge_id": edge_id,
        "source_mu_id": "mu-src",
        "target_mu_id": "mu-tgt",
        "weight": weight,
        "alpha": alpha,
        "beta": beta,
    }


def _verdict_request(**overrides):
    kwargs = dict(
        run_id="run-1",
        verdict="up",
        condition_label="learned",
        judge_source="model:gemma4:e4b",
        apply_weight_update=True,
    )
    kwargs.update(overrides)
    return VerdictRequest(**kwargs)


class TestVerdictValidation:

    def test_down_without_criterion_triggered_returns_422(self):
        request = _verdict_request(verdict="down", criterion_triggered=None)
        with pytest.raises(HTTPException) as exc_info:
            run(feedback_verdict(request))
        assert exc_info.value.status_code == 422
        print(f"[PASS] down without criterion_triggered rejected: {exc_info.value.detail}")

    def test_down_with_blank_criterion_triggered_returns_422(self):
        request = _verdict_request(verdict="down", criterion_triggered="   ")
        with pytest.raises(HTTPException) as exc_info:
            run(feedback_verdict(request))
        assert exc_info.value.status_code == 422

    def test_invalid_verdict_value_returns_422(self):
        request = _verdict_request(verdict="maybe")
        with pytest.raises(HTTPException) as exc_info:
            run(feedback_verdict(request))
        assert exc_info.value.status_code == 422

    def test_unknown_run_id_returns_404(self):
        request = _verdict_request(verdict="none", apply_weight_update=False)
        with patch("api.chat.graph_store.get_run", AsyncMock(return_value=None)):
            with pytest.raises(HTTPException) as exc_info:
                run(feedback_verdict(request))
        assert exc_info.value.status_code == 404


class TestVerdictWeightUpdateGating:

    def test_apply_weight_update_false_makes_zero_weight_changes(self):
        request = _verdict_request(verdict="up", apply_weight_update=False)
        update_mock = AsyncMock()
        with patch("api.chat.graph_store.get_run", AsyncMock(return_value=_run_record())), \
             patch("api.chat.graph_store.update_edge_weight", update_mock), \
             patch("api.chat.record_feedback_event", AsyncMock()):
            result = run(feedback_verdict(request))

        update_mock.assert_not_called()
        assert result.edges_updated == 0
        assert result.weight_updates == []
        print("[PASS] apply_weight_update=false makes zero weight changes")

    def test_apply_weight_update_true_up_verdict_updates_weights_with_fk(self):
        request = _verdict_request(verdict="up", apply_weight_update=True, condition_label="learned")
        with patch("api.chat.graph_store.get_run", AsyncMock(return_value=_run_record())), \
             patch("api.chat.graph_store.get_edge", AsyncMock(return_value=_edge())), \
             patch("api.chat.graph_store.update_edge_counters", AsyncMock()) as update_mock, \
             patch("api.chat.graph_store.insert_edge_update_event", AsyncMock()), \
             patch("api.chat.graph_store.update_run_feedback", AsyncMock()) as update_run_mock, \
             patch("api.chat.record_feedback_event", AsyncMock()) as record_mock:
            result = run(feedback_verdict(request))

        assert result.edges_updated == 1
        assert result.weight_updates[0]["feedback_event_id"] == result.feedback_event_id
        assert result.weight_updates[0]["source_mu_id"] == "mu-src"
        assert result.weight_updates[0]["target_mu_id"] == "mu-tgt"
        assert result.weight_updates[0]["new_weight"] > result.weight_updates[0]["old_weight"]
        update_mock.assert_called_once()
        update_run_mock.assert_called_once()
        recorded_event = record_mock.call_args[0][0]
        assert recorded_event.verdict == "up"
        assert recorded_event.judge_source == "model:gemma4:e4b"
        assert recorded_event.condition_label == "learned"
        print(f"[PASS] up verdict applied weight update with FK: {result.weight_updates}")

    def test_apply_weight_update_true_down_verdict_updates_weights(self):
        request = _verdict_request(
            verdict="down", apply_weight_update=True, criterion_triggered="mentions X",
        )
        with patch("api.chat.graph_store.get_run", AsyncMock(return_value=_run_record())), \
             patch("api.chat.graph_store.get_edge", AsyncMock(return_value=_edge())), \
             patch("api.chat.graph_store.update_edge_counters", AsyncMock()) as update_mock, \
             patch("api.chat.graph_store.insert_edge_update_event", AsyncMock()), \
             patch("api.chat.graph_store.update_run_feedback", AsyncMock()), \
             patch("api.chat.record_feedback_event", AsyncMock()):
            result = run(feedback_verdict(request))

        assert result.edges_updated == 1
        assert result.weight_updates[0]["new_weight"] < result.weight_updates[0]["old_weight"]
        update_mock.assert_called_once()

    def test_apply_weight_update_true_none_verdict_makes_zero_changes(self):
        """Silence is not a signal — 'none' never applies a weight update even if requested."""
        request = _verdict_request(verdict="none", apply_weight_update=True, criterion_triggered=None)
        update_mock = AsyncMock()
        with patch("api.chat.graph_store.get_run", AsyncMock(return_value=_run_record())), \
             patch("api.chat.graph_store.update_edge_weight", update_mock), \
             patch("api.chat.record_feedback_event", AsyncMock()):
            result = run(feedback_verdict(request))

        update_mock.assert_not_called()
        assert result.edges_updated == 0
        print("[PASS] verdict=none makes zero weight changes even with apply_weight_update=true")


class TestVerdictAlwaysPersisted:

    def test_none_verdict_is_explicitly_stored_not_null(self):
        request = _verdict_request(
            verdict="none", apply_weight_update=False, condition_label="static_weight",
        )
        with patch("api.chat.graph_store.get_run", AsyncMock(return_value=_run_record())), \
             patch("api.chat.record_feedback_event", AsyncMock()) as record_mock:
            result = run(feedback_verdict(request))

        recorded_event = record_mock.call_args[0][0]
        assert recorded_event.verdict == "none"
        assert result.verdict == "none"
        print("[PASS] verdict='none' is explicitly stored, never NULL")

    def test_verdict_metadata_fields_pass_through(self):
        request = _verdict_request(
            verdict="up",
            apply_weight_update=False,
            rationale="covers all points",
            judge_model_name="gemma4:e4b",
            judge_temperature=0.0,
            judge_pinned_snapshot="sha256:abcdef",
            prompt_id="cluster_a_0001",
        )
        with patch("api.chat.graph_store.get_run", AsyncMock(return_value=_run_record())), \
             patch("api.chat.record_feedback_event", AsyncMock()) as record_mock:
            run(feedback_verdict(request))

        recorded_event = record_mock.call_args[0][0]
        assert recorded_event.rationale == "covers all points"
        assert recorded_event.judge_model_name == "gemma4:e4b"
        assert recorded_event.judge_temperature == 0.0
        assert recorded_event.judge_pinned_snapshot == "sha256:abcdef"
        assert recorded_event.prompt_id == "cluster_a_0001"
        assert recorded_event.is_probe_turn is False


class TestOldFeedbackEndpointUnchanged:

    def test_old_feedback_endpoint_stamps_judge_source_human(self):
        request = FeedbackRequest(run_id="run-1", signal=1)
        with patch("api.chat.graph_store.get_run", AsyncMock(return_value=_run_record())), \
             patch("api.chat.graph_store.get_all_edges", AsyncMock(return_value=[_edge()])), \
             patch("api.chat.graph_store.get_mu", AsyncMock(return_value={"mu_id": "mu-1"})), \
             patch("api.chat.graph_store.update_edge_weight", AsyncMock()), \
             patch("api.chat.graph_store.update_run_feedback", AsyncMock()), \
             patch("api.chat.record_feedback_event", AsyncMock()) as record_mock:
            result = run(feedback(request))

        recorded_event = record_mock.call_args[0][0]
        assert recorded_event.judge_source == "human"
        assert recorded_event.verdict == "thumbs_up"
        assert result.edges_updated == 1
        print("[PASS] /feedback (old endpoint) stamps judge_source='human', behavior unchanged")

    def test_old_feedback_endpoint_signal_validation_unchanged(self):
        request = FeedbackRequest(run_id="run-1", signal=0)
        with pytest.raises(HTTPException) as exc_info:
            run(feedback(request))
        assert exc_info.value.status_code == 422
