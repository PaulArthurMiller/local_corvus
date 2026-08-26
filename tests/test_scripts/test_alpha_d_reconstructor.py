"""
Tests for the Alpha D Phase 1 path-back reconstructor.

Two tiers, matching test_alpha_c_lockin.py's pattern:

  - Unit tests against synthetic Turn/edge data — no DB, no fixture files,
    exercise the reconstruction rules in isolation (visibility timing,
    directedness, alpha/beta event-sourcing) and each mismatch-detection
    branch of the three verify_* functions.
  - Real-data tests against the actual memory/graph.db and seed_fixtures/
    (skipped if absent in this checkout) — including the milestone's own
    Done-when: all three tie-back components pass, zero mismatches, across
    all 8 archived learned runs.
"""

import importlib.util
import json
import sqlite3
import sys
from pathlib import Path

import pytest

_SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "alpha_d_pathback.py"
_spec = importlib.util.spec_from_file_location("alpha_d_pathback", _SCRIPT)
alpha_d = importlib.util.module_from_spec(_spec)
sys.modules["alpha_d_pathback"] = alpha_d
_spec.loader.exec_module(alpha_d)


def make_seed(edges: dict[str, tuple[str, str]], mu_ids: set[str] = None) -> "alpha_d.SeedFixture":
    """Build a SeedFixture from a plain {edge_id: (source, target)} mapping."""
    edge_dicts = {
        eid: {"source_mu_id": s, "target_mu_id": t} for eid, (s, t) in edges.items()
    }
    edge_by_pair = {(s, t): eid for eid, (s, t) in edges.items()}
    return alpha_d.SeedFixture(
        version="test",
        mu_ids=mu_ids or set(),
        edges=edge_dicts,
        edge_by_pair=edge_by_pair,
    )


def make_turn(**overrides) -> "alpha_d.Turn":
    defaults = dict(
        run_id="run-1",
        turn_index=10,
        timestamp="2026-08-06T10:00:00+00:00",
        prompt_mu_id=None,
        input_mu_id=None,
        output_mu_id=None,
        activated_mu_ids=[],
        entry_point_mu_ids=[],
        candidate_set_before_ranking=[],
        traversal_path=[],
        hop_depth_per_retrieved_mu={},
        edge_weights_used={},
        hyperparameters_snapshot={
            "retrieval_level1_top_k": 3,
            "retrieval_level2_top_k": 1,
            "directional_edge_weight_multiplier": 1.5,
        },
        edge_update_cfg={
            "prior_alpha": 1.0, "prior_beta": 1.0,
            "seed_prior_alpha": 1.0, "seed_prior_beta": 1.0,
        },
    )
    defaults.update(overrides)
    return alpha_d.Turn(**defaults)


class TestSeedDirectedness:
    """The schema's `direction` column must never be consulted — see the
    module docstring and BUILD-PLAN.md's directed-reachability risk item."""

    def test_forward_and_reverse_seed_pairs_are_distinct_edges(self):
        seed = make_seed({"ea1": ("a1", "a2"), "ea1r": ("a2", "a1")})
        assert seed.edge_by_pair[("a1", "a2")] == "ea1"
        assert seed.edge_by_pair[("a2", "a1")] == "ea1r"

    def test_outgoing_pairs_only_returns_edges_sourced_at_the_queried_node(self):
        seed = make_seed({"ea1": ("a1", "a2"), "ea1r": ("a2", "a1")})
        recon = alpha_d.GraphReconstructor(seed, turns=[], edge_events={})
        assert recon.outgoing_pairs_at("a1", turn_index=1000) == {("a1", "a2")}
        assert recon.outgoing_pairs_at("a2", turn_index=1000) == {("a2", "a1")}


class TestConvTopologyVisibility:
    def test_seed_pair_is_always_visible(self):
        seed = make_seed({"ea1": ("a1", "a2")})
        recon = alpha_d.GraphReconstructor(seed, turns=[], edge_events={})
        assert recon.edge_exists_at("a1", "a2", turn_index=1) is True

    def test_conv_pair_not_visible_at_its_own_creation_turn(self):
        """Retrieval for turn T runs before T's own edges are written."""
        seed = make_seed({})
        turn = make_turn(turn_index=5, prompt_mu_id="p5", input_mu_id="i5")
        recon = alpha_d.GraphReconstructor(seed, turns=[turn], edge_events={})
        assert recon.edge_exists_at("p5", "i5", turn_index=5) is False

    def test_conv_pair_visible_starting_the_next_turn(self):
        seed = make_seed({})
        turn = make_turn(turn_index=5, prompt_mu_id="p5", input_mu_id="i5")
        recon = alpha_d.GraphReconstructor(seed, turns=[turn], edge_events={})
        assert recon.edge_exists_at("p5", "i5", turn_index=6) is True

    def test_co_activation_edges_created_from_output_to_every_activated_mu(self):
        seed = make_seed({})
        turn = make_turn(
            turn_index=7, output_mu_id="o7", activated_mu_ids=["m1", "m2"]
        )
        recon = alpha_d.GraphReconstructor(seed, turns=[turn], edge_events={})
        assert recon.outgoing_pairs_at("o7", turn_index=8) == {("o7", "m1"), ("o7", "m2")}
        assert recon.outgoing_pairs_at("o7", turn_index=7) == set()

    def test_unknown_pair_does_not_exist(self):
        seed = make_seed({})
        recon = alpha_d.GraphReconstructor(seed, turns=[], edge_events={})
        assert recon.edge_exists_at("x", "y", turn_index=999) is False


class TestCreationPrior:
    def test_seed_edge_uses_seed_prior(self):
        seed = make_seed({"ea1": ("a1", "a2")})
        recon = alpha_d.GraphReconstructor(seed, turns=[], edge_events={})
        turn = make_turn(edge_update_cfg={
            "prior_alpha": 1.0, "prior_beta": 1.0,
            "seed_prior_alpha": 2.5, "seed_prior_beta": 3.5,
        })
        assert recon.creation_prior("ea1", turn) == (2.5, 3.5)

    def test_conv_edge_uses_conv_prior(self):
        seed = make_seed({})
        recon = alpha_d.GraphReconstructor(seed, turns=[], edge_events={})
        turn = make_turn(edge_update_cfg={
            "prior_alpha": 1.0, "prior_beta": 1.0,
            "seed_prior_alpha": 2.5, "seed_prior_beta": 3.5,
        })
        assert recon.creation_prior("some-random-uuid", turn) == (1.0, 1.0)


class TestAlphaBetaReconstruction:
    def test_falls_back_to_creation_prior_with_no_events(self):
        seed = make_seed({"ea1": ("a1", "a2")})
        recon = alpha_d.GraphReconstructor(seed, turns=[], edge_events={})
        turn = make_turn()
        assert recon.reconstructed_alpha_beta("ea1", "2026-08-06T10:00:00", turn) == (1.0, 1.0)

    def test_uses_latest_event_strictly_before_the_timestamp(self):
        seed = make_seed({})
        events = {
            "e1": [
                alpha_d.EdgeUpdateEvent("e1", "2026-08-06T10:00:00", 2.0, 1.0),
                alpha_d.EdgeUpdateEvent("e1", "2026-08-06T11:00:00", 3.0, 1.0),
            ]
        }
        recon = alpha_d.GraphReconstructor(seed, turns=[], edge_events=events)
        turn = make_turn()
        # Strictly between the two events -> the first one applies.
        assert recon.reconstructed_alpha_beta("e1", "2026-08-06T10:30:00", turn) == (2.0, 1.0)
        # After both -> the second (latest) applies.
        assert recon.reconstructed_alpha_beta("e1", "2026-08-06T12:00:00", turn) == (3.0, 1.0)
        # Before both -> falls back to the creation prior.
        assert recon.reconstructed_alpha_beta("e1", "2026-08-06T09:00:00", turn) == (1.0, 1.0)

    def test_event_at_exactly_the_query_timestamp_does_not_count(self):
        """Strict '<' — a turn must not see its own concurrently-recorded update."""
        seed = make_seed({})
        events = {"e1": [alpha_d.EdgeUpdateEvent("e1", "2026-08-06T10:00:00", 5.0, 1.0)]}
        recon = alpha_d.GraphReconstructor(seed, turns=[], edge_events=events)
        turn = make_turn()
        assert recon.reconstructed_alpha_beta("e1", "2026-08-06T10:00:00", turn) == (1.0, 1.0)


class TestVerifyAlphaBetaTieback:
    def test_passes_when_reconstructed_matches_recorded(self):
        seed = make_seed({"ea1": ("a1", "a2")})
        turn = make_turn(
            turn_index=10,
            edge_weights_used={"ea1": {"alpha": 1.0, "beta": 1.0, "weight_used": 0.5}},
        )
        recon = alpha_d.GraphReconstructor(seed, turns=[turn], edge_events={})
        mismatches = alpha_d.verify_alpha_beta_tieback(recon, [turn], "test run")
        assert mismatches == []

    def test_flags_a_mismatch(self):
        seed = make_seed({"ea1": ("a1", "a2")})
        turn = make_turn(
            turn_index=10,
            edge_weights_used={"ea1": {"alpha": 9.0, "beta": 9.0, "weight_used": 0.5}},
        )
        recon = alpha_d.GraphReconstructor(seed, turns=[turn], edge_events={})
        mismatches = alpha_d.verify_alpha_beta_tieback(recon, [turn], "test run")
        assert len(mismatches) == 1
        assert mismatches[0].component == "alpha_beta"

    def test_ignores_edges_with_no_recorded_alpha(self):
        """Non-learned conditions record weight_used with alpha/beta absent."""
        seed = make_seed({"ea1": ("a1", "a2")})
        turn = make_turn(edge_weights_used={"ea1": {"weight_used": 0.5}})
        recon = alpha_d.GraphReconstructor(seed, turns=[turn], edge_events={})
        assert alpha_d.verify_alpha_beta_tieback(recon, [turn], "test run") == []


class TestVerifyTopologyTieback:
    def test_passes_on_consistent_seed_traversal(self):
        seed = make_seed({"ea1": ("a1", "a2")})
        turn = make_turn(
            turn_index=10,
            entry_point_mu_ids=["a1"],
            traversal_path=[{"source_mu_id": "a1", "edge_id": "ea1", "target_mu_id": "a2", "hop_depth": 1}],
        )
        recon = alpha_d.GraphReconstructor(seed, turns=[turn], edge_events={})
        assert alpha_d.verify_topology_tieback(recon, [turn], "test run") == []

    def test_flags_traversed_pair_that_does_not_yet_exist(self):
        """A conv pair recorded as traversed at its own creation turn — impossible."""
        seed = make_seed({})
        turn = make_turn(
            turn_index=5,
            prompt_mu_id="p5", input_mu_id="i5",
            entry_point_mu_ids=["p5"],
            traversal_path=[{"source_mu_id": "p5", "edge_id": "some-uuid", "target_mu_id": "i5", "hop_depth": 1}],
        )
        recon = alpha_d.GraphReconstructor(seed, turns=[turn], edge_events={})
        mismatches = alpha_d.verify_topology_tieback(recon, [turn], "test run")
        assert any(m.component == "topology_existence" for m in mismatches)

    def test_flags_wrong_edge_id_for_a_seed_pair(self):
        seed = make_seed({"ea1": ("a1", "a2")})
        turn = make_turn(
            turn_index=10,
            entry_point_mu_ids=["a1"],
            traversal_path=[{"source_mu_id": "a1", "edge_id": "wrong-id", "target_mu_id": "a2", "hop_depth": 1}],
        )
        recon = alpha_d.GraphReconstructor(seed, turns=[turn], edge_events={})
        mismatches = alpha_d.verify_topology_tieback(recon, [turn], "test run")
        assert any(m.component == "topology_seed_id" for m in mismatches)

    def test_flags_missing_reconstructed_edge_not_in_traversal_path(self):
        """Completeness: a K1-outgoing edge that should have been walked, wasn't recorded."""
        seed = make_seed({"ea1": ("a1", "a2")})
        turn = make_turn(turn_index=10, entry_point_mu_ids=["a1"], traversal_path=[])
        recon = alpha_d.GraphReconstructor(seed, turns=[turn], edge_events={})
        mismatches = alpha_d.verify_topology_tieback(recon, [turn], "test run")
        assert any(m.component == "topology_completeness" for m in mismatches)

    def test_k1_to_k1_edges_are_not_required_in_traversal_path(self):
        """retrieve() skips targets already in the K1 set — must not false-flag those."""
        seed = make_seed({"ea1": ("a1", "a2")})
        turn = make_turn(turn_index=10, entry_point_mu_ids=["a1", "a2"], traversal_path=[])
        recon = alpha_d.GraphReconstructor(seed, turns=[turn], edge_events={})
        assert alpha_d.verify_topology_tieback(recon, [turn], "test run") == []


class TestVerifySlateTieback:
    def test_passes_on_consistent_k1_and_k2(self):
        turn = make_turn(
            turn_index=10,
            entry_point_mu_ids=["a1"],
            candidate_set_before_ranking=[{"mu_id": "a1", "bm25_score": 5.0, "edge_weight_sum": 1.0}],
            activated_mu_ids=["a1", "a2"],
            traversal_path=[{"source_mu_id": "a1", "edge_id": "ea1", "target_mu_id": "a2", "hop_depth": 1}],
            edge_weights_used={"ea1": {"alpha": 1.0, "beta": 1.0, "weight_used": 0.6}},
        )
        assert alpha_d.verify_slate_tieback([turn], "test run") == []

    def test_flags_k1_mismatch(self):
        turn = make_turn(
            turn_index=10,
            entry_point_mu_ids=["WRONG"],
            candidate_set_before_ranking=[{"mu_id": "a1", "bm25_score": 5.0, "edge_weight_sum": 1.0}],
        )
        mismatches = alpha_d.verify_slate_tieback([turn], "test run")
        assert len(mismatches) == 1
        assert mismatches[0].component == "slate_k1"

    def test_flags_k2_mismatch_when_wrong_target_wins(self):
        turn = make_turn(
            turn_index=10,
            entry_point_mu_ids=["a1"],
            candidate_set_before_ranking=[{"mu_id": "a1", "bm25_score": 5.0, "edge_weight_sum": 1.0}],
            activated_mu_ids=["a1", "WRONG_TARGET"],
            traversal_path=[{"source_mu_id": "a1", "edge_id": "ea1", "target_mu_id": "a2", "hop_depth": 1}],
            edge_weights_used={"ea1": {"alpha": 1.0, "beta": 1.0, "weight_used": 0.6}},
        )
        mismatches = alpha_d.verify_slate_tieback([turn], "test run")
        assert len(mismatches) == 1
        assert mismatches[0].component == "slate_k2"

    def test_k2_ranks_targets_by_summed_injected_weight_times_dir_multiplier(self):
        """Two K1 sources both point at the same target — scores must sum."""
        turn = make_turn(
            turn_index=10,
            entry_point_mu_ids=["a1", "a2"],
            candidate_set_before_ranking=[
                {"mu_id": "a1", "bm25_score": 5.0, "edge_weight_sum": 1.0},
                {"mu_id": "a2", "bm25_score": 4.0, "edge_weight_sum": 1.0},
            ],
            activated_mu_ids=["a1", "a2", "shared_target"],
            traversal_path=[
                {"source_mu_id": "a1", "edge_id": "e1", "target_mu_id": "shared_target", "hop_depth": 1},
                {"source_mu_id": "a2", "edge_id": "e2", "target_mu_id": "shared_target", "hop_depth": 1},
                {"source_mu_id": "a1", "edge_id": "e3", "target_mu_id": "other_target", "hop_depth": 1},
            ],
            edge_weights_used={
                "e1": {"alpha": 1.0, "beta": 1.0, "weight_used": 0.1},
                "e2": {"alpha": 1.0, "beta": 1.0, "weight_used": 0.1},
                "e3": {"alpha": 1.0, "beta": 1.0, "weight_used": 0.9},
            },
        )
        # shared_target: (0.1+0.1)*1.5=0.3 ; other_target: 0.9*1.5=1.35 -> other_target should win
        # but recorded activated_mu_ids says shared_target won -> mismatch expected.
        mismatches = alpha_d.verify_slate_tieback([turn], "test run")
        assert len(mismatches) == 1
        assert mismatches[0].component == "slate_k2"

    def test_flags_missing_injected_weight(self):
        turn = make_turn(
            turn_index=10,
            entry_point_mu_ids=["a1"],
            candidate_set_before_ranking=[{"mu_id": "a1", "bm25_score": 5.0, "edge_weight_sum": 1.0}],
            traversal_path=[{"source_mu_id": "a1", "edge_id": "ea1", "target_mu_id": "a2", "hop_depth": 1}],
            edge_weights_used={},  # ea1 missing entirely
        )
        mismatches = alpha_d.verify_slate_tieback([turn], "test run")
        assert any(m.component == "slate_k2_missing_weight" for m in mismatches)


class TestLoadSeedFixtureRealFile:
    def test_v3_matches_known_mu_and_edge_counts(self):
        if not (alpha_d.SEED_FIXTURES_DIR / "seed_v3.json.gz").exists():
            pytest.skip("seed_v3.json.gz not present in this checkout")
        seed = alpha_d.load_seed_fixture("v3")
        assert len(seed.mu_ids) == 34
        assert len(seed.edges) == 64

    def test_v3_seed_edges_are_directed_pairs(self):
        if not (alpha_d.SEED_FIXTURES_DIR / "seed_v3.json.gz").exists():
            pytest.skip("seed_v3.json.gz not present in this checkout")
        seed = alpha_d.load_seed_fixture("v3")
        # Known deterministic v3 pair (see BUILD-PLAN.md / probe_set_v3.yaml)
        assert ("a0000002-aaaa-4aaa-8aaa-000000000002", "a0000001-aaaa-4aaa-8aaa-000000000001") in seed.edge_by_pair
        assert ("a0000001-aaaa-4aaa-8aaa-000000000001", "a0000002-aaaa-4aaa-8aaa-000000000002") in seed.edge_by_pair


class TestLoadAuditRegistryRealFile:
    def test_registry_loads_and_is_nonempty(self):
        if not alpha_d.AUDIT_LOG.exists():
            pytest.skip("audit.log not present in this checkout")
        registry = alpha_d.load_audit_registry()
        assert len(registry) > 0
        assert all(isinstance(v, str) for v in registry.values())

    def test_missing_file_returns_empty_dict_not_an_error(self, tmp_path):
        registry = alpha_d.load_audit_registry(tmp_path / "does_not_exist.log")
        assert registry == {}


class TestFullReconstructorAcceptance:
    """
    The milestone's own Done-when: all three tie-back components pass, with
    zero mismatches, across all 8 archived learned runs (1,680 real turns).
    This is the real value of Phase 1 — see the BUILD-PLAN's "why this
    ordering is the whole point" note. Skipped only if the archived DB or
    seed fixture isn't present in this checkout.
    """

    def test_zero_mismatches_across_all_8_archived_runs(self):
        if not alpha_d.GRAPH_DB.exists():
            pytest.skip("graph.db not present in this checkout")
        if not (alpha_d.SEED_FIXTURES_DIR / "seed_v3.json.gz").exists():
            pytest.skip("seed_v3.json.gz not present in this checkout")

        seed = alpha_d.load_seed_fixture()
        total_turns = 0
        all_mismatches = []
        for basename, _condition, label in alpha_d.RUNS:
            turns, mismatches = alpha_d.run_tieback_for_experiment(
                alpha_d.GRAPH_DB, seed, basename, label
            )
            if not turns:
                pytest.skip(f"run {label} not present in this checkout's graph.db")
            total_turns += len(turns)
            all_mismatches += mismatches

        assert total_turns == 1680, f"expected 1,680 real turns across 8 runs, got {total_turns}"
        assert all_mismatches == [], (
            f"{len(all_mismatches)} tie-back mismatches: "
            f"{[m.detail for m in all_mismatches[:5]]}"
        )


# ===========================================================================
# Phase 2 — metrics layer
# ===========================================================================


class TestComputeSessionCutoffs:
    def test_derives_cutoffs_from_session_turn_index_resets(self):
        turns = [
            make_turn(turn_index=100, session_turn_index=0),
            make_turn(turn_index=101, session_turn_index=1),
            make_turn(turn_index=102, session_turn_index=0),  # session 2 starts
            make_turn(turn_index=103, session_turn_index=1),
            make_turn(turn_index=104, session_turn_index=2),
        ]
        cutoffs = alpha_d.compute_session_cutoffs(turns)
        # session 1 ends at turn_index 101 (last of session 1) -> cutoff is
        # session 2's first turn_index (102); session 2 is the last session
        # -> cutoff is one past the run's very last turn_index (105).
        assert cutoffs == {1: 102, 2: 105}

    def test_empty_turns_returns_empty(self):
        assert alpha_d.compute_session_cutoffs([]) == {}

    def test_single_session_run(self):
        turns = [make_turn(turn_index=5 + i, session_turn_index=i) for i in range(4)]
        cutoffs = alpha_d.compute_session_cutoffs(turns)
        assert cutoffs == {1: 9}  # one past the last turn_index (8)


class TestMinHopDistance:
    def test_direct_hit_is_distance_one(self):
        seed = make_seed({"e1": ("a", "gold")})
        recon = alpha_d.GraphReconstructor(seed, turns=[], edge_events={})
        assert alpha_d.min_hop_distance(recon, {"a"}, "gold", as_of_turn_index=1) == 1

    def test_two_hop_path_is_distance_two(self):
        seed = make_seed({"e1": ("a", "mid"), "e2": ("mid", "gold")})
        recon = alpha_d.GraphReconstructor(seed, turns=[], edge_events={})
        assert alpha_d.min_hop_distance(recon, {"a"}, "gold", as_of_turn_index=1) == 2

    def test_unreachable_within_horizon_is_none(self):
        seed = make_seed({"e1": ("a", "mid"), "e2": ("mid", "far"), "e3": ("far", "gold")})
        recon = alpha_d.GraphReconstructor(seed, turns=[], edge_events={})
        assert alpha_d.min_hop_distance(recon, {"a"}, "gold", as_of_turn_index=1, max_hops=2) is None

    def test_reachable_from_any_source_in_the_set(self):
        seed = make_seed({"e1": ("a", "gold")})
        recon = alpha_d.GraphReconstructor(seed, turns=[], edge_events={})
        assert alpha_d.min_hop_distance(recon, {"unrelated", "a"}, "gold", as_of_turn_index=1) == 1

    def test_no_outgoing_edges_is_unreachable(self):
        seed = make_seed({})
        recon = alpha_d.GraphReconstructor(seed, turns=[], edge_events={})
        assert alpha_d.min_hop_distance(recon, {"a"}, "gold", as_of_turn_index=1) is None


class TestClassifyBucket:
    def test_gold_in_retrieved_set_is_reached(self):
        seed = make_seed({})
        recon = alpha_d.GraphReconstructor(seed, turns=[], edge_events={})
        bucket = alpha_d.classify_bucket(recon, ["a", "gold"], "gold", as_of_turn_index=1)
        assert bucket == alpha_d.BUCKET_REACHED

    def test_one_hop_from_a_retrieved_mu_is_outbid(self):
        seed = make_seed({"e1": ("a", "gold")})
        recon = alpha_d.GraphReconstructor(seed, turns=[], edge_events={})
        bucket = alpha_d.classify_bucket(recon, ["a", "b"], "gold", as_of_turn_index=1)
        assert bucket == alpha_d.BUCKET_OUTBID

    def test_two_hops_from_a_retrieved_mu_is_out_of_depth(self):
        seed = make_seed({"e1": ("a", "mid"), "e2": ("mid", "gold")})
        recon = alpha_d.GraphReconstructor(seed, turns=[], edge_events={})
        bucket = alpha_d.classify_bucket(recon, ["a"], "gold", as_of_turn_index=1)
        assert bucket == alpha_d.BUCKET_OUT_OF_DEPTH

    def test_no_path_within_horizon_is_disconnected(self):
        seed = make_seed({})
        recon = alpha_d.GraphReconstructor(seed, turns=[], edge_events={})
        bucket = alpha_d.classify_bucket(recon, ["a"], "gold", as_of_turn_index=1)
        assert bucket == alpha_d.BUCKET_DISCONNECTED


class TestMeanWeightForPair:
    def test_seed_pair_uses_event_sourced_alpha_beta(self):
        seed = make_seed({"e1": ("a", "b")})
        events = {"e1": [alpha_d.EdgeUpdateEvent("e1", "2026-01-01T00:00:00", 3.0, 1.0, turn_index=5)]}
        recon = alpha_d.GraphReconstructor(seed, turns=[], edge_events=events)
        turn = make_turn()
        assert recon.mean_weight_for_pair("a", "b", as_of_turn_index=10, turn=turn) == 0.75

    def test_conv_pair_never_traversed_falls_back_to_creation_prior(self):
        """An untraversed conv pair has no recoverable edge_id — provably
        still at its creation prior (see mean_weight_for_pair's docstring)."""
        seed = make_seed({})
        recon = alpha_d.GraphReconstructor(seed, turns=[], edge_events={})
        turn = make_turn(edge_update_cfg={
            "prior_alpha": 1.0, "prior_beta": 3.0,
            "seed_prior_alpha": 1.0, "seed_prior_beta": 1.0,
        })
        # prior 1.0/3.0 -> mean 0.25, never touched since the pair was never
        # traversed by any real turn's traversal_path.
        assert recon.mean_weight_for_pair("out1", "gold", as_of_turn_index=10, turn=turn) == 0.25

    def test_conv_pair_traversed_uses_its_resolved_edge_id(self):
        seed = make_seed({})
        turn_with_hop = make_turn(
            turn_index=3,
            traversal_path=[{"source_mu_id": "out1", "edge_id": "conv-e1", "target_mu_id": "gold", "hop_depth": 1}],
        )
        events = {"conv-e1": [alpha_d.EdgeUpdateEvent("conv-e1", "t", 4.0, 4.0, turn_index=3)]}
        recon = alpha_d.GraphReconstructor(seed, turns=[turn_with_hop], edge_events=events)
        assert recon.mean_weight_for_pair("out1", "gold", as_of_turn_index=10, turn=turn_with_hop) == 0.5


class TestChaseDownReachesGold:
    def test_reaches_gold_in_one_hop(self):
        seed = make_seed({"e1": ("a", "gold")})
        recon = alpha_d.GraphReconstructor(seed, turns=[], edge_events={})
        turn = make_turn()
        assert alpha_d.chase_down_reaches_gold(recon, "a", "gold", as_of_turn_index=1, turn=turn) is True

    def test_follows_the_highest_weight_edge_and_reaches_gold_in_two_hops(self):
        seed = make_seed({
            "e_weak": ("a", "decoy"),
            "e_strong": ("a", "mid"),
            "e_final": ("mid", "gold"),
        })
        events = {
            "e_weak": [alpha_d.EdgeUpdateEvent("e_weak", "t", 1.0, 9.0, turn_index=1)],  # low mean
            "e_strong": [alpha_d.EdgeUpdateEvent("e_strong", "t", 9.0, 1.0, turn_index=1)],  # high mean
        }
        recon = alpha_d.GraphReconstructor(seed, turns=[], edge_events=events)
        turn = make_turn()
        assert alpha_d.chase_down_reaches_gold(recon, "a", "gold", as_of_turn_index=10, turn=turn) is True

    def test_dead_end_fails(self):
        seed = make_seed({"e1": ("a", "dead_end")})
        recon = alpha_d.GraphReconstructor(seed, turns=[], edge_events={})
        turn = make_turn()
        assert alpha_d.chase_down_reaches_gold(recon, "a", "gold", as_of_turn_index=1, turn=turn) is False

    def test_gold_beyond_the_horizon_fails(self):
        seed = make_seed({"e1": ("a", "mid"), "e2": ("mid", "far"), "e3": ("far", "gold")})
        recon = alpha_d.GraphReconstructor(seed, turns=[], edge_events={})
        turn = make_turn()
        assert alpha_d.chase_down_reaches_gold(
            recon, "a", "gold", as_of_turn_index=1, turn=turn, max_hops=2
        ) is False


class TestK2RunnerUpRank:
    def test_gold_absent_from_pool_is_none(self):
        seed = make_seed({"e1": ("a", "other")})
        recon = alpha_d.GraphReconstructor(seed, turns=[], edge_events={})
        turn = make_turn()
        rank = alpha_d.k2_runner_up_rank(
            recon, ["a", "b", "c"], "gold", as_of_turn_index=1, turn=turn, level1_k=3,
        )
        assert rank is None

    def test_ranks_by_summed_weight_across_k1_sources(self):
        seed = make_seed({
            "e_to_gold_1": ("a", "gold"),
            "e_to_gold_2": ("b", "gold"),
            "e_to_rival": ("a", "rival"),
        })
        events = {
            "e_to_gold_1": [alpha_d.EdgeUpdateEvent("e_to_gold_1", "t", 1.0, 9.0, turn_index=1)],  # mean 0.1
            "e_to_gold_2": [alpha_d.EdgeUpdateEvent("e_to_gold_2", "t", 1.0, 9.0, turn_index=1)],  # mean 0.1
            "e_to_rival": [alpha_d.EdgeUpdateEvent("e_to_rival", "t", 9.0, 1.0, turn_index=1)],   # mean 0.9
        }
        recon = alpha_d.GraphReconstructor(seed, turns=[], edge_events=events)
        turn = make_turn()
        # gold: (0.1+0.1)*1.5=0.3 ; rival: 0.9*1.5=1.35 -> rival ranks above gold
        rank = alpha_d.k2_runner_up_rank(
            recon, ["a", "b", "c"], "gold", as_of_turn_index=10, turn=turn, level1_k=3,
        )
        assert rank == 2

    def test_k1_to_k1_targets_are_excluded_from_the_pool(self):
        seed = make_seed({"e1": ("a", "b")})  # b is itself in the K1 set
        recon = alpha_d.GraphReconstructor(seed, turns=[], edge_events={})
        turn = make_turn()
        rank = alpha_d.k2_runner_up_rank(
            recon, ["a", "b"], "b", as_of_turn_index=1, turn=turn, level1_k=2,
        )
        assert rank is None


class TestHubMuIdForGold:
    def test_resolves_the_unique_hub_by_naming_convention(self):
        seed = make_seed({}, mu_ids={
            "a0000001-aaaa-4aaa-8aaa-000000000001",
            "a0000002-aaaa-4aaa-8aaa-000000000002",
        })
        hub = alpha_d.hub_mu_id_for_gold(seed, "a0000002-aaaa-4aaa-8aaa-000000000002")
        assert hub == "a0000001-aaaa-4aaa-8aaa-000000000001"

    def test_no_matching_hub_returns_none(self):
        seed = make_seed({}, mu_ids={"a0000002-aaaa-4aaa-8aaa-000000000002"})
        assert alpha_d.hub_mu_id_for_gold(seed, "a0000002-aaaa-4aaa-8aaa-000000000002") is None

    def test_empty_gold_returns_none(self):
        seed = make_seed({}, mu_ids=set())
        assert alpha_d.hub_mu_id_for_gold(seed, "") is None


class TestGoldCoActivationCensus:
    def test_counts_created_traversed_and_credited(self):
        seed = make_seed({})
        turns = [
            # output1 -> gold created at turn 1, never traversed later.
            make_turn(turn_index=1, output_mu_id="output1", activated_mu_ids=["gold"]),
            # output2 -> gold created at turn 2, traversed at turn 3 (hop
            # recorded there), and that edge_id receives a credit event.
            make_turn(turn_index=2, output_mu_id="output2", activated_mu_ids=["gold"]),
            make_turn(
                turn_index=3,
                entry_point_mu_ids=["output2"],
                traversal_path=[{"source_mu_id": "output2", "edge_id": "conv-e2", "target_mu_id": "gold", "hop_depth": 1}],
            ),
        ]
        events = {"conv-e2": [alpha_d.EdgeUpdateEvent("conv-e2", "t", 2.0, 1.0, turn_index=4)]}
        recon = alpha_d.GraphReconstructor(seed, turns=turns, edge_events=events)
        census = alpha_d.gold_co_activation_census(recon, "gold")
        assert census.created == 2
        assert census.traversed == 1
        assert census.credited == 1

    def test_no_co_activation_edges_at_all(self):
        seed = make_seed({})
        recon = alpha_d.GraphReconstructor(seed, turns=[], edge_events={})
        census = alpha_d.gold_co_activation_census(recon, "gold")
        assert census == alpha_d.CoActivationCensus(created=0, traversed=0, credited=0)


class TestLoadProbeSet:
    def _write_probe_set(self, tmp_path, probes):
        path = tmp_path / "probe_set_test.yaml"
        import yaml as _yaml
        path.write_text(_yaml.safe_dump({"probes": probes}), encoding="utf-8")
        return path

    def test_extracts_single_gold_per_probe(self, tmp_path):
        path = self._write_probe_set(tmp_path, [
            {"probe_id": "vs_a1", "gold_mu_ids": ["gold-a1"]},
            {"probe_id": "vs_a2", "gold_mu_ids": ["gold-a2"]},
        ])
        gold_map = alpha_d.load_probe_set(path=path)
        assert gold_map == {"vs_a1": "gold-a1", "vs_a2": "gold-a2"}

    def test_skips_probes_with_zero_or_multiple_golds(self, tmp_path):
        path = self._write_probe_set(tmp_path, [
            {"probe_id": "vs_a1", "gold_mu_ids": ["gold-a1"]},
            {"probe_id": "vs_bridge", "gold_mu_ids": ["g1", "g2"]},
            {"probe_id": "vs_empty", "gold_mu_ids": []},
        ])
        gold_map = alpha_d.load_probe_set(path=path)
        assert gold_map == {"vs_a1": "gold-a1"}


class TestPlanPathForBasename:
    def test_derives_underscore_before_date(self):
        assert alpha_d.plan_path_for_basename("exp0b-2026-08-06") == alpha_d.PLANS_DIR / "exp0b_2026-08-06.json"
        assert alpha_d.plan_path_for_basename("exp0b-rep2-2026-08-09") == alpha_d.PLANS_DIR / "exp0b-rep2_2026-08-09.json"
        assert (
            alpha_d.plan_path_for_basename("exp01-cfgA-rep1-2026-08-12")
            == alpha_d.PLANS_DIR / "exp01-cfgA-rep1_2026-08-12.json"
        )


class TestLoadTrainedSpokeGolds:
    def test_resolves_cluster_to_gold_via_plan_and_probe_set(self, tmp_path, monkeypatch):
        plan_path = tmp_path / "myrun_2026-01-01.json"
        plan_path.write_text(json.dumps({"trained_spokes": {"cluster_a": "a_spoke1"}}), encoding="utf-8")
        monkeypatch.setattr(alpha_d, "PLANS_DIR", tmp_path)
        gold_map = {"vs_a1": "a0000002-aaaa-4aaa-8aaa-000000000002"}
        result = alpha_d.load_trained_spoke_golds("myrun-2026-01-01", gold_map)
        assert result == {"a": "a0000002-aaaa-4aaa-8aaa-000000000002"}

    def test_missing_plan_file_returns_empty(self, tmp_path, monkeypatch):
        monkeypatch.setattr(alpha_d, "PLANS_DIR", tmp_path)
        assert alpha_d.load_trained_spoke_golds("nonexistent-2026-01-01", {}) == {}


class TestPhase2RealDataCrossCheck:
    """
    The bucket partition's REACHED count must exactly equal the count of
    battery_mode="mean" probe_results rows with a non-NULL gold_mu_rank —
    both describe "gold was in the retrieved slate," computed two different
    ways (this script's own retrieved_mu_ids membership check vs. the
    backend's own recorded rank at measurement time). Agreement across all
    8 runs is a strong correctness signal for classify_bucket(), independent
    of Phase 1's tie-back (which never touches probe data at all).
    """

    def test_reached_bucket_matches_recorded_gold_mu_rank_across_all_runs(self):
        if not alpha_d.GRAPH_DB.exists():
            pytest.skip("graph.db not present in this checkout")
        if not (alpha_d.SEED_FIXTURES_DIR / "seed_v3.json.gz").exists():
            pytest.skip("seed_v3.json.gz not present in this checkout")

        seed = alpha_d.load_seed_fixture()
        gold_map = alpha_d.load_probe_set()
        total_probes = 0
        for basename, condition, label in alpha_d.RUNS:
            metrics = alpha_d.compute_run_metrics(alpha_d.GRAPH_DB, seed, gold_map, basename, condition, label)
            if metrics is None:
                pytest.skip(f"run {label} not present in this checkout's graph.db")

            con = sqlite3.connect(alpha_d.GRAPH_DB)
            recorded_reached = con.execute(
                "SELECT COUNT(*) FROM probe_results "
                "WHERE experiment_id = ? AND battery_mode = 'mean' AND gold_mu_rank IS NOT NULL",
                (f"{basename}_learned",),
            ).fetchone()[0]
            con.close()

            n = sum(metrics.bucket_counts.values())
            total_probes += n
            assert metrics.bucket_counts[alpha_d.BUCKET_REACHED] == recorded_reached, (
                f"{label}: reconstructed REACHED={metrics.bucket_counts[alpha_d.BUCKET_REACHED]} "
                f"!= recorded gold_mu_rank-not-null={recorded_reached}"
            )
            # Every bucket-eligible probe lands in exactly one bucket.
            assert n == 140, f"{label}: expected 140 mean-mode probe measurements, got {n}"

        assert total_probes == 1120, f"expected 28 probes x 5 sessions x 8 runs = 1120, got {total_probes}"
