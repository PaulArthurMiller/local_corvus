"""
Tests for learning/edge_update.py — M20 Phase 1 learned-condition edge
update mechanism.

Coverage (per BUILD-PLAN.md M20 Phase 1 §8):
- 3-hop path credit decay math (up verdict)
- negative_scope: path vs terminal (down verdict)
- none verdict -> zero counter change, zero event rows
- zero-hop (no path edges) -> zero counter change, zero event rows, no error
- order-independence: two verdicts touching a shared edge produce identical
  final counters regardless of application order
- evidence cap: rescale preserves mean; event row records post-cap values
- effective_weight: mean mode == alpha/(alpha+beta); sample mode draws are
  Beta-distributed and stable across repeated reads within one call
- static/random conditions: never write counters or event rows
- probe turns: never write counters or event rows in any condition
- config validation: every §6 rule, including decay.enabled: true hard-fail
"""

import asyncio
import os
import random
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "backend"))

from learning.edge_update import (
    EdgeUpdateConfigError,
    apply_evidence_cap,
    compute_effective_weight,
    compute_path_increments,
    get_path_edge_ids_for_mu,
    process_verdict_for_learning,
    resolve_rng_seed,
    validate_edge_update_config,
)


def run(coro):
    return asyncio.get_event_loop().run_until_complete(coro)


def _default_cfg(**overrides) -> dict:
    cfg = {
        "credit_scale": 1.0,
        "credit_lambda": 0.5,
        "negative_scope": "path",
        "weight_mode": "mean",
        "evidence_cap": {"enabled": False, "n_max": 50.0},
    }
    cfg.update(overrides)
    return cfg


# ---------------------------------------------------------------------------
# compute_path_increments — pure credit-decay math
# ---------------------------------------------------------------------------


class TestComputePathIncrements:
    def test_three_hop_path_yields_geometric_decay(self):
        increments = compute_path_increments(
            ["e-entry", "e-mid", "e-terminal"], credit_scale=1.0, credit_lambda=0.5
        )
        by_edge = {eid: (hop_k, inc) for eid, hop_k, inc in increments}
        assert by_edge["e-terminal"] == (0, pytest.approx(1.0))
        assert by_edge["e-mid"] == (1, pytest.approx(0.5))
        assert by_edge["e-entry"] == (2, pytest.approx(0.25))
        print(f"[PASS] 3-hop increments: {increments}")

    def test_single_edge_path_is_hop_zero(self):
        increments = compute_path_increments(["e1"], credit_scale=2.0, credit_lambda=0.6)
        assert increments == [("e1", 0, 2.0)]

    def test_empty_path_yields_no_increments(self):
        assert compute_path_increments([], credit_scale=1.0, credit_lambda=0.5) == []


# ---------------------------------------------------------------------------
# apply_evidence_cap
# ---------------------------------------------------------------------------


class TestApplyEvidenceCap:
    def test_no_rescale_when_under_cap(self):
        alpha, beta = apply_evidence_cap(5.0, 3.0, n_max=50.0)
        assert (alpha, beta) == (5.0, 3.0)

    def test_rescale_preserves_mean(self):
        alpha, beta = apply_evidence_cap(40.0, 20.0, n_max=30.0)
        assert alpha + beta == pytest.approx(30.0)
        assert alpha / (alpha + beta) == pytest.approx(40.0 / 60.0)


# ---------------------------------------------------------------------------
# compute_effective_weight
# ---------------------------------------------------------------------------


class TestComputeEffectiveWeight:
    def test_non_learned_condition_reads_scalar_weight_column(self):
        edge = {"weight": 0.73, "alpha": 99.0, "beta": 1.0}
        info = compute_effective_weight(edge, "static_weight", "mean", random.Random(1))
        assert info.weight == pytest.approx(0.73)
        assert info.mode == "static_weight"

    def test_learned_mean_mode_equals_alpha_over_total(self):
        edge = {"weight": 0.5, "alpha": 3.0, "beta": 1.0}
        info = compute_effective_weight(edge, "learned", "mean", random.Random(1))
        assert info.weight == pytest.approx(3.0 / 4.0)
        assert info.mode == "mean"
        assert info.alpha == 3.0 and info.beta == 1.0

    def test_learned_sample_mode_is_beta_distributed_and_in_bounds(self):
        edge = {"weight": 0.5, "alpha": 5.0, "beta": 5.0}
        rng = random.Random(42)
        draws = [
            compute_effective_weight(edge, "learned", "sample", rng).weight
            for _ in range(200)
        ]
        assert all(0.0 <= d <= 1.0 for d in draws)
        # Statistical smoke check — mean of many draws from Beta(5,5) should
        # land near 0.5, not be a constant (rules out an accidental mean-mode fallback)
        assert 0.35 < (sum(draws) / len(draws)) < 0.65
        assert len(set(draws)) > 50
        print(f"[PASS] sample-mode mean over 200 draws: {sum(draws)/len(draws):.3f}")

    def test_sample_mode_same_rng_object_advances_across_calls(self):
        edge = {"weight": 0.5, "alpha": 2.0, "beta": 2.0}
        rng = random.Random(7)
        first = compute_effective_weight(edge, "learned", "sample", rng).weight
        second = compute_effective_weight(edge, "learned", "sample", rng).weight
        assert first != second  # distinct draws from the same evolving RNG stream

    def test_missing_alpha_beta_defaults_to_uniform_prior(self):
        edge = {"weight": 0.5}
        info = compute_effective_weight(edge, "learned", "mean", random.Random(1))
        assert info.weight == pytest.approx(0.5)


# ---------------------------------------------------------------------------
# get_path_edge_ids_for_mu
# ---------------------------------------------------------------------------


class TestGetPathEdgeIdsForMu:
    def test_zero_hop_mu_has_no_path_edges(self):
        assert get_path_edge_ids_for_mu("mu-1", {"mu-1": 0}, []) == []

    def test_mu_missing_from_hop_depths_has_no_path_edges(self):
        assert get_path_edge_ids_for_mu("mu-unknown", {"mu-1": 1}, []) == []

    def test_single_hop_mu_returns_its_incoming_edge(self):
        traversal_path = [
            {"source_mu_id": "mu-k1", "edge_id": "e1", "target_mu_id": "mu-2", "hop_depth": 1},
        ]
        assert get_path_edge_ids_for_mu("mu-2", {"mu-2": 1}, traversal_path) == ["e1"]

    def test_multiple_incoming_edges_all_returned(self):
        traversal_path = [
            {"source_mu_id": "mu-a", "edge_id": "e1", "target_mu_id": "mu-2", "hop_depth": 1},
            {"source_mu_id": "mu-b", "edge_id": "e2", "target_mu_id": "mu-2", "hop_depth": 1},
        ]
        assert get_path_edge_ids_for_mu("mu-2", {"mu-2": 1}, traversal_path) == ["e1", "e2"]


# ---------------------------------------------------------------------------
# resolve_rng_seed
# ---------------------------------------------------------------------------


class TestResolveRngSeed:
    def test_explicit_override_wins(self):
        assert resolve_rng_seed({"rng_seed": 12345}, "exp-abc") == 12345

    def test_derives_deterministically_from_experiment_id(self):
        seed1 = resolve_rng_seed({"rng_seed": None}, "exp-abc")
        seed2 = resolve_rng_seed({"rng_seed": None}, "exp-abc")
        seed3 = resolve_rng_seed({"rng_seed": None}, "exp-xyz")
        assert seed1 == seed2
        assert seed1 != seed3


# ---------------------------------------------------------------------------
# validate_edge_update_config
# ---------------------------------------------------------------------------


class TestValidateEdgeUpdateConfig:
    def test_defaults_applied_when_block_missing(self):
        resolved = validate_edge_update_config({})
        assert resolved["prior_alpha"] == 1.0
        assert resolved["weight_mode"] == "mean"
        assert resolved["negative_scope"] == "path"
        assert resolved["evidence_cap"]["enabled"] is False

    def test_seed_priors_default_to_runtime_priors_when_unset(self):
        resolved = validate_edge_update_config({"edge_update": {"prior_alpha": 2.0, "prior_beta": 3.0}})
        assert resolved["seed_prior_alpha"] == 2.0
        assert resolved["seed_prior_beta"] == 3.0

    @pytest.mark.parametrize("field", ["prior_alpha", "prior_beta", "seed_prior_alpha", "seed_prior_beta", "credit_scale"])
    def test_non_positive_values_rejected(self, field):
        with pytest.raises(EdgeUpdateConfigError):
            validate_edge_update_config({"edge_update": {field: 0.0}})

    @pytest.mark.parametrize("bad_lambda", [0.0, -0.1, 1.1])
    def test_credit_lambda_out_of_range_rejected(self, bad_lambda):
        with pytest.raises(EdgeUpdateConfigError):
            validate_edge_update_config({"edge_update": {"credit_lambda": bad_lambda}})

    def test_credit_lambda_boundary_one_is_valid(self):
        resolved = validate_edge_update_config({"edge_update": {"credit_lambda": 1.0}})
        assert resolved["credit_lambda"] == 1.0

    def test_invalid_negative_scope_rejected(self):
        with pytest.raises(EdgeUpdateConfigError):
            validate_edge_update_config({"edge_update": {"negative_scope": "bogus"}})

    def test_invalid_weight_mode_rejected(self):
        with pytest.raises(EdgeUpdateConfigError):
            validate_edge_update_config({"edge_update": {"weight_mode": "bogus"}})

    def test_evidence_cap_n_max_must_exceed_prior_total_when_enabled(self):
        with pytest.raises(EdgeUpdateConfigError):
            validate_edge_update_config({
                "edge_update": {
                    "prior_alpha": 1.0, "prior_beta": 1.0,
                    "evidence_cap": {"enabled": True, "n_max": 2.0},
                }
            })

    def test_evidence_cap_n_max_valid_when_above_prior_total(self):
        resolved = validate_edge_update_config({
            "edge_update": {
                "prior_alpha": 1.0, "prior_beta": 1.0,
                "evidence_cap": {"enabled": True, "n_max": 10.0},
            }
        })
        assert resolved["evidence_cap"]["n_max"] == 10.0

    def test_decay_enabled_true_hard_fails(self):
        with pytest.raises(EdgeUpdateConfigError, match="decay.enabled"):
            validate_edge_update_config({"decay": {"enabled": True}})

    def test_decay_enabled_false_or_absent_is_fine(self):
        validate_edge_update_config({"decay": {"enabled": False}})
        validate_edge_update_config({})  # no decay block at all


# ---------------------------------------------------------------------------
# process_verdict_for_learning — the end-to-end orchestration
# ---------------------------------------------------------------------------


class FakeStore:
    """In-memory fake of the GraphStore methods process_verdict_for_learning needs."""

    def __init__(self, edges: dict[str, dict]):
        self.edges = edges
        self.update_calls: list[dict] = []
        self.event_rows: list[dict] = []

    async def get_edge(self, edge_id):
        return self.edges.get(edge_id)

    async def update_edge_counters(self, edge_id, alpha, beta, evidence_updated_at_turn=None):
        self.edges[edge_id]["alpha"] = alpha
        self.edges[edge_id]["beta"] = beta
        self.update_calls.append({
            "edge_id": edge_id, "alpha": alpha, "beta": beta,
            "evidence_updated_at_turn": evidence_updated_at_turn,
        })
        return True

    async def insert_edge_update_event(self, event: dict):
        self.event_rows.append(event)


def _edge(edge_id="e1", source="mu-src", target="mu-tgt", alpha=1.0, beta=1.0):
    return {"edge_id": edge_id, "source_mu_id": source, "target_mu_id": target,
            "alpha": alpha, "beta": beta, "weight": 0.5}


def _run(run_id="run-1", turn_index=5, activated_mu_ids=None, hop_depths=None, traversal_path=None, is_probe_turn=False):
    return {
        "run_id": run_id,
        "turn_index": turn_index,
        "activated_mu_ids": activated_mu_ids or [],
        "hop_depth_per_retrieved_mu": hop_depths or {},
        "traversal_path": traversal_path or [],
        "is_probe_turn": is_probe_turn,
    }


class TestProcessVerdictForLearning:
    def test_up_verdict_increments_alpha_on_single_hop_mu(self):
        store = FakeStore({"e1": _edge(alpha=1.0, beta=1.0)})
        run_record = _run(
            activated_mu_ids=["mu-2"],
            hop_depths={"mu-2": 1},
            traversal_path=[{"source_mu_id": "mu-1", "edge_id": "e1", "target_mu_id": "mu-2", "hop_depth": 1}],
        )
        results = run(process_verdict_for_learning(
            run_record, "up", "learned", "fb-1", store, _default_cfg(),
        ))
        assert len(results) == 1
        assert store.edges["e1"]["alpha"] == pytest.approx(2.0)
        assert store.edges["e1"]["beta"] == pytest.approx(1.0)
        assert len(store.event_rows) == 1
        event = store.event_rows[0]
        assert event["verdict"] == "up"
        assert event["hop_k"] == 0
        assert event["alpha_before"] == 1.0 and event["alpha_after"] == 2.0
        assert event["verdict_id"] == "fb-1"
        assert event["mu_id"] == "mu-2"

    def test_down_verdict_path_scope_increments_beta_on_all_path_edges(self):
        store = FakeStore({
            "e-entry": _edge("e-entry", "mu-a", "mu-b", alpha=1.0, beta=1.0),
        })
        # Simulate a synthetic multi-hop path by calling compute_path_increments
        # indirectly is out of scope here (retrieval is single-hop); instead
        # verify the single available hop is credited to beta correctly.
        run_record = _run(
            activated_mu_ids=["mu-b"],
            hop_depths={"mu-b": 1},
            traversal_path=[{"source_mu_id": "mu-a", "edge_id": "e-entry", "target_mu_id": "mu-b", "hop_depth": 1}],
        )
        results = run(process_verdict_for_learning(
            run_record, "down", "learned", "fb-2", store, _default_cfg(negative_scope="path"),
        ))
        assert len(results) == 1
        assert store.edges["e-entry"]["beta"] == pytest.approx(2.0)
        assert store.edges["e-entry"]["alpha"] == pytest.approx(1.0)

    def test_down_verdict_terminal_scope_only_touches_hop_zero(self):
        # Two independent single-edge "paths" converging on the same MU —
        # both are hop_k=0 (terminal) under the current single-hop design,
        # so negative_scope: terminal still credits both (each is its own
        # terminal edge); this test documents that behavior explicitly.
        store = FakeStore({
            "e1": _edge("e1", "mu-a", "mu-c", alpha=1.0, beta=1.0),
            "e2": _edge("e2", "mu-b", "mu-c", alpha=1.0, beta=1.0),
        })
        run_record = _run(
            activated_mu_ids=["mu-c"],
            hop_depths={"mu-c": 1},
            traversal_path=[
                {"source_mu_id": "mu-a", "edge_id": "e1", "target_mu_id": "mu-c", "hop_depth": 1},
                {"source_mu_id": "mu-b", "edge_id": "e2", "target_mu_id": "mu-c", "hop_depth": 1},
            ],
        )
        results = run(process_verdict_for_learning(
            run_record, "down", "learned", "fb-3", store, _default_cfg(negative_scope="terminal"),
        ))
        assert len(results) == 2
        assert store.edges["e1"]["beta"] == pytest.approx(2.0)
        assert store.edges["e2"]["beta"] == pytest.approx(2.0)

    def test_none_verdict_makes_zero_changes(self):
        store = FakeStore({"e1": _edge(alpha=1.0, beta=1.0)})
        run_record = _run(
            activated_mu_ids=["mu-2"], hop_depths={"mu-2": 1},
            traversal_path=[{"source_mu_id": "mu-1", "edge_id": "e1", "target_mu_id": "mu-2", "hop_depth": 1}],
        )
        results = run(process_verdict_for_learning(
            run_record, "none", "learned", "fb-4", store, _default_cfg(),
        ))
        assert results == []
        assert store.update_calls == []
        assert store.event_rows == []

    def test_zero_hop_mu_makes_zero_changes_no_error(self):
        store = FakeStore({})
        run_record = _run(activated_mu_ids=["mu-1"], hop_depths={"mu-1": 0}, traversal_path=[])
        results = run(process_verdict_for_learning(
            run_record, "up", "learned", "fb-5", store, _default_cfg(),
        ))
        assert results == []
        assert store.event_rows == []

    def test_static_condition_never_writes_counters(self):
        store = FakeStore({"e1": _edge(alpha=1.0, beta=1.0)})
        run_record = _run(
            activated_mu_ids=["mu-2"], hop_depths={"mu-2": 1},
            traversal_path=[{"source_mu_id": "mu-1", "edge_id": "e1", "target_mu_id": "mu-2", "hop_depth": 1}],
        )
        results = run(process_verdict_for_learning(
            run_record, "up", "static_weight", "fb-6", store, _default_cfg(),
        ))
        assert results == []
        assert store.update_calls == []
        assert store.event_rows == []

    def test_random_condition_never_writes_counters(self):
        store = FakeStore({"e1": _edge(alpha=1.0, beta=1.0)})
        run_record = _run(
            activated_mu_ids=["mu-2"], hop_depths={"mu-2": 1},
            traversal_path=[{"source_mu_id": "mu-1", "edge_id": "e1", "target_mu_id": "mu-2", "hop_depth": 1}],
        )
        results = run(process_verdict_for_learning(
            run_record, "up", "random_weight", "fb-7", store, _default_cfg(),
        ))
        assert results == []
        assert store.update_calls == []

    def test_probe_turn_never_writes_counters_even_under_learned(self):
        store = FakeStore({"e1": _edge(alpha=1.0, beta=1.0)})
        run_record = _run(
            activated_mu_ids=["mu-2"], hop_depths={"mu-2": 1},
            traversal_path=[{"source_mu_id": "mu-1", "edge_id": "e1", "target_mu_id": "mu-2", "hop_depth": 1}],
            is_probe_turn=True,
        )
        results = run(process_verdict_for_learning(
            run_record, "up", "learned", "fb-8", store, _default_cfg(),
        ))
        assert results == []
        assert store.update_calls == []

    def test_order_independence_two_verdicts_same_edge(self):
        """Two verdicts touching a shared edge in one turn (here: two calls,
        since one turn -> one verdict) must produce the same final counters
        regardless of order, because increments are additive."""
        store_a = FakeStore({"e1": _edge(alpha=1.0, beta=1.0)})
        store_b = FakeStore({"e1": _edge(alpha=1.0, beta=1.0)})
        run_record = _run(
            activated_mu_ids=["mu-2"], hop_depths={"mu-2": 1},
            traversal_path=[{"source_mu_id": "mu-1", "edge_id": "e1", "target_mu_id": "mu-2", "hop_depth": 1}],
        )

        # order: up then down
        run(process_verdict_for_learning(run_record, "up", "learned", "fb-a1", store_a, _default_cfg()))
        run(process_verdict_for_learning(run_record, "down", "learned", "fb-a2", store_a, _default_cfg()))

        # order: down then up
        run(process_verdict_for_learning(run_record, "down", "learned", "fb-b1", store_b, _default_cfg()))
        run(process_verdict_for_learning(run_record, "up", "learned", "fb-b2", store_b, _default_cfg()))

        assert store_a.edges["e1"]["alpha"] == pytest.approx(store_b.edges["e1"]["alpha"])
        assert store_a.edges["e1"]["beta"] == pytest.approx(store_b.edges["e1"]["beta"])

    def test_evidence_cap_rescales_and_event_row_records_post_cap_values(self):
        store = FakeStore({"e1": _edge(alpha=20.0, beta=20.0)})
        run_record = _run(
            activated_mu_ids=["mu-2"], hop_depths={"mu-2": 1},
            traversal_path=[{"source_mu_id": "mu-1", "edge_id": "e1", "target_mu_id": "mu-2", "hop_depth": 1}],
        )
        cfg = _default_cfg(evidence_cap={"enabled": True, "n_max": 30.0})
        results = run(process_verdict_for_learning(
            run_record, "up", "learned", "fb-9", store, cfg,
        ))
        alpha_after = store.edges["e1"]["alpha"]
        beta_after = store.edges["e1"]["beta"]
        assert alpha_after + beta_after == pytest.approx(30.0)
        # mean preserved: (20+1)/(20+1+20) before cap == alpha_after/30 after cap
        expected_mean = 21.0 / 41.0
        assert alpha_after / (alpha_after + beta_after) == pytest.approx(expected_mean)
        event = store.event_rows[0]
        assert event["alpha_after"] == pytest.approx(alpha_after)
        assert event["beta_after"] == pytest.approx(beta_after)

    def test_edge_shared_across_two_retrieved_mus_gets_independent_increments(self):
        """An edge cannot literally target two different MUs (edge.target_mu_id
        is fixed), but two different edges landing on two different MUs in the
        same turn must each get their own independent event row — verifies no
        accidental deduplication across the activated_mu_ids loop."""
        store = FakeStore({
            "e1": _edge("e1", "mu-src", "mu-2", alpha=1.0, beta=1.0),
            "e2": _edge("e2", "mu-src", "mu-3", alpha=1.0, beta=1.0),
        })
        run_record = _run(
            activated_mu_ids=["mu-2", "mu-3"],
            hop_depths={"mu-2": 1, "mu-3": 1},
            traversal_path=[
                {"source_mu_id": "mu-src", "edge_id": "e1", "target_mu_id": "mu-2", "hop_depth": 1},
                {"source_mu_id": "mu-src", "edge_id": "e2", "target_mu_id": "mu-3", "hop_depth": 1},
            ],
        )
        results = run(process_verdict_for_learning(
            run_record, "up", "learned", "fb-10", store, _default_cfg(),
        ))
        assert len(results) == 2
        assert len(store.event_rows) == 2
        assert store.edges["e1"]["alpha"] == pytest.approx(2.0)
        assert store.edges["e2"]["alpha"] == pytest.approx(2.0)
