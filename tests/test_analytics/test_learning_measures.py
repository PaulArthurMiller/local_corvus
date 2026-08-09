"""
Tests for §6.1 learning measures — experiment_report.py pure helper functions.

Covers:
  State-transition detection:
    [fail, fail, pass, pass] → 1 fail_to_pass, 0 pass_to_fail
    [pass, fail, pass] → 1 fail_to_pass, 1 pass_to_fail
    [fail, fail, fail] → flat_fail
  Probe aggregation by phenomenon type
  Orphan update count: 0 for fully-linked updates; N for fabricated orphans
  Cross-cluster edge segmentation
  Saturation check fires at weight > 0.9; does not fire at 0.85
  Runaway-reinforcement fires for [0.05, 0.07, 0.10]; not for [0.10, 0.07, 0.05]
  Bridge-MU rank trajectory correctly identified
  outcome_override='negative_flagged' → classified as negative_flagged, not pass or fail
  DB round-trip: outcome_override stored and retrieved correctly
"""

import asyncio
import os
import sys
import tempfile
import uuid
from pathlib import Path
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "backend"))

import memory.store as store_module
from memory.store import GraphStore
from analytics.reports.experiment_report import (
    _build_mu_lineage_clusters,
    _classify_edge_update,
    _classify_probe_outcome,
    _cluster_from_prompt_id,
    _compute_bridge_mu_analysis,
    _compute_cluster_winner_table,
    _compute_cross_series_probe_mus,
    _compute_edge_delta_ledger,
    _compute_mus_by_probe_outcome,
    _compute_saturation_analysis,
    _compute_state_transitions,
    _collect_all_weight_updates,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def run(coro):
    return asyncio.get_event_loop().run_until_complete(coro)


class TempDB:
    """Context manager: GraphStore pointed at a temp file."""

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


def _mu_id() -> str:
    return str(uuid.uuid4())


def _run_id() -> str:
    return str(uuid.uuid4())


def _make_probe_result(
    probe_id: str,
    measurement_session_index: int,
    gold_mu_rank=None,
    retrieved_mu_ids=None,
    condition_label: str = "learning",
    outcome_override: str = None,
) -> dict:
    return {
        "result_id": str(uuid.uuid4()),
        "probe_id": probe_id,
        "run_id": None,
        "measurement_session_index": measurement_session_index,
        "condition_label": condition_label,
        "retrieved_mu_ids": retrieved_mu_ids or [],
        "gold_mu_rank": gold_mu_rank,
        "retrieval_precision": None,
        "retrieval_recall": None,
        "bm25_scores_per_mu": {},
        "outcome_override": outcome_override,
    }


def _make_run_with_updates(
    weight_updates: list,
    feedback_signal: int = None,
    turn_index: int = 1,
    experiment_id: str = "exp-1",
    series_id: str = "series-A",
) -> dict:
    return {
        "run_id": _run_id(),
        "series_id": series_id,
        "experiment_id": experiment_id,
        "timestamp": f"2026-01-01T00:{turn_index:02d}:00+00:00",
        "input_mu_id": _mu_id(),
        "output_mu_id": _mu_id(),
        "tool_calls": [],
        "activated_mu_ids": [],
        "activated_edge_ids": [],
        "prompt_mu_id": _mu_id(),
        "tokens_in": 100,
        "tokens_out": 50,
        "latency_ms": 200,
        "feedback_signal": feedback_signal,
        "weight_updates": weight_updates,
        "hyperparameters_snapshot": {},
        "algorithm": "simple_rl",
        "bm25_scores_per_mu": {},
        "entry_point_mu_ids": [],
        "candidate_set_before_ranking": [],
        "traversal_path": [],
        "hop_depth_per_retrieved_mu": {},
        "edge_contributions": {},
        "assembled_prompt_json": [],
        "config_hash": None,
        "full_config_snapshot": {},
        "condition_label": "learning",
        "turn_index": turn_index,
        "session_turn_index": turn_index,
        "measurement_session_index": None,
        "is_probe_turn": False,
        "use_session_context": False,
        "failure_reason": None,
    }


# ---------------------------------------------------------------------------
# §6.1 — _classify_probe_outcome
# ---------------------------------------------------------------------------


class TestClassifyProbeOutcome:
    def test_pass_when_rank_within_threshold(self):
        result = _make_probe_result("p1", 1, gold_mu_rank=2)
        assert _classify_probe_outcome(result, pass_rank_threshold=3) == "pass"

    def test_pass_at_threshold_boundary(self):
        result = _make_probe_result("p1", 1, gold_mu_rank=3)
        assert _classify_probe_outcome(result, pass_rank_threshold=3) == "pass"

    def test_fail_when_rank_exceeds_threshold(self):
        result = _make_probe_result("p1", 1, gold_mu_rank=4)
        assert _classify_probe_outcome(result, pass_rank_threshold=3) == "fail"

    def test_fail_when_rank_is_none(self):
        result = _make_probe_result("p1", 1, gold_mu_rank=None)
        assert _classify_probe_outcome(result, pass_rank_threshold=3) == "fail"

    def test_negative_flagged_overrides_pass(self):
        result = _make_probe_result("p1", 1, gold_mu_rank=1, outcome_override="negative_flagged")
        assert _classify_probe_outcome(result, pass_rank_threshold=3) == "negative_flagged"

    def test_negative_flagged_overrides_fail(self):
        result = _make_probe_result("p1", 1, gold_mu_rank=None, outcome_override="negative_flagged")
        assert _classify_probe_outcome(result, pass_rank_threshold=3) == "negative_flagged"

    # --- Phase 0.4: phenomenon-aware pass criteria ---

    def test_cross_cluster_transfer_passes_at_rank4_with_threshold4(self):
        """cross_cluster_transfer with rank=4 should PASS when phenomenon threshold is 4."""
        result = _make_probe_result("p10", 0, gold_mu_rank=4)
        thresholds = {"cross_cluster_transfer": 4, "ranking_improvement": 1}
        outcome = _classify_probe_outcome(
            result, pass_rank_threshold=1,
            phenomenon_type="cross_cluster_transfer",
            phenomenon_thresholds=thresholds,
        )
        assert outcome == "pass", f"Expected pass, got {outcome}"
        print("[PASS] cross_cluster_transfer rank=4 → PASS with threshold=4")

    def test_cross_cluster_transfer_fails_at_rank5_with_threshold4(self):
        """cross_cluster_transfer with rank=5 should FAIL when phenomenon threshold is 4."""
        result = _make_probe_result("p10", 0, gold_mu_rank=5)
        thresholds = {"cross_cluster_transfer": 4, "ranking_improvement": 1}
        outcome = _classify_probe_outcome(
            result, pass_rank_threshold=1,
            phenomenon_type="cross_cluster_transfer",
            phenomenon_thresholds=thresholds,
        )
        assert outcome == "fail", f"Expected fail, got {outcome}"
        print("[PASS] cross_cluster_transfer rank=5 → FAIL with threshold=4")

    def test_ranking_improvement_fails_at_rank2_even_with_cross_cluster_threshold(self):
        """ranking_improvement with rank=2 should FAIL even when cross_cluster threshold is 4."""
        result = _make_probe_result("p06", 0, gold_mu_rank=2)
        thresholds = {"cross_cluster_transfer": 4, "ranking_improvement": 1}
        outcome = _classify_probe_outcome(
            result, pass_rank_threshold=1,
            phenomenon_type="ranking_improvement",
            phenomenon_thresholds=thresholds,
        )
        assert outcome == "fail", f"Expected fail, got {outcome}"
        print("[PASS] ranking_improvement rank=2 → FAIL with threshold=1")

    def test_falls_back_to_default_when_no_phenomenon_thresholds(self):
        """No phenomenon_thresholds arg → falls back to pass_rank_threshold."""
        result = _make_probe_result("p01", 0, gold_mu_rank=3)
        outcome = _classify_probe_outcome(
            result, pass_rank_threshold=3,
            phenomenon_type="ranking_improvement",
            phenomenon_thresholds=None,
        )
        assert outcome == "pass", f"Expected pass, got {outcome}"
        print("[PASS] fallback to default threshold when phenomenon_thresholds=None")

    def test_gold_mu_rank_not_modified_by_classify(self):
        """gold_mu_rank in probe_results row is untouched by _classify_probe_outcome."""
        result = _make_probe_result("p10", 0, gold_mu_rank=4)
        original_rank = result["gold_mu_rank"]
        thresholds = {"cross_cluster_transfer": 4}
        _classify_probe_outcome(
            result, pass_rank_threshold=1,
            phenomenon_type="cross_cluster_transfer",
            phenomenon_thresholds=thresholds,
        )
        # The raw rank must remain unchanged
        assert result["gold_mu_rank"] == original_rank
        print(f"[PASS] gold_mu_rank={original_rank} unchanged after classification")


# ---------------------------------------------------------------------------
# §6.1 — _compute_state_transitions
# ---------------------------------------------------------------------------


class TestComputeStateTransitions:
    def _run_transitions(self, outcome_seq: list, phenomenon: str = "ranking_improvement"):
        """Helper: synthesise probe + results for the given outcome sequence."""
        probe = {
            "probe_id": "p1",
            "phenomenon_type": phenomenon,
            "is_bridge_probe": False,
            "bridge_pair_id": None,
        }
        # Map "pass" → rank=1, "fail" → rank=None
        results_by_probe = {
            "p1": [
                _make_probe_result(
                    "p1",
                    session_idx,
                    gold_mu_rank=(1 if o == "pass" else None),
                )
                for session_idx, o in enumerate(outcome_seq, start=1)
            ]
        }
        return _compute_state_transitions([probe], results_by_probe, pass_rank_threshold=3)

    def test_fail_fail_pass_pass_gives_one_transition(self):
        result = self._run_transitions(["fail", "fail", "pass", "pass"])
        p = result["per_probe"][0]
        assert p["fail_to_pass_count"] == 1
        assert p["pass_to_fail_count"] == 0

    def test_pass_fail_pass_gives_one_each(self):
        result = self._run_transitions(["pass", "fail", "pass"])
        p = result["per_probe"][0]
        assert p["fail_to_pass_count"] == 1
        assert p["pass_to_fail_count"] == 1

    def test_flat_fail(self):
        result = self._run_transitions(["fail", "fail", "fail"])
        p = result["per_probe"][0]
        assert p["pattern"] == "flat_fail"
        assert p["fail_to_pass_count"] == 0

    def test_flat_pass(self):
        result = self._run_transitions(["pass", "pass", "pass"])
        p = result["per_probe"][0]
        assert p["pattern"] == "flat_pass"

    def test_mixed_pattern(self):
        result = self._run_transitions(["fail", "pass", "fail"])
        p = result["per_probe"][0]
        assert p["pattern"] == "mixed"

    def test_probe_aggregation_by_phenomenon(self):
        # Two probes in same phenomenon: one has 1 fail_to_pass, one has none
        probes = [
            {"probe_id": "p1", "phenomenon_type": "ranking_improvement",
             "is_bridge_probe": False, "bridge_pair_id": None},
            {"probe_id": "p2", "phenomenon_type": "ranking_improvement",
             "is_bridge_probe": False, "bridge_pair_id": None},
        ]
        results_by_probe = {
            "p1": [
                _make_probe_result("p1", 1, gold_mu_rank=None),
                _make_probe_result("p1", 2, gold_mu_rank=1),
            ],
            "p2": [
                _make_probe_result("p2", 1, gold_mu_rank=None),
                _make_probe_result("p2", 2, gold_mu_rank=None),
            ],
        }
        result = _compute_state_transitions(probes, results_by_probe, pass_rank_threshold=3)
        phenom = result["by_phenomenon"].get("ranking_improvement", {})
        assert phenom["probe_count"] == 2
        assert phenom["fail_to_pass"] == 1   # p1 contributes one
        assert phenom["flat_fail"] == 1      # p2 is flat_fail

    def test_two_phenomena_counted_separately(self):
        probes = [
            {"probe_id": "p1", "phenomenon_type": "ranking_improvement",
             "is_bridge_probe": False, "bridge_pair_id": None},
            {"probe_id": "p2", "phenomenon_type": "cross_cluster_transfer",
             "is_bridge_probe": False, "bridge_pair_id": None},
        ]
        results_by_probe = {
            "p1": [_make_probe_result("p1", 1, gold_mu_rank=1)],
            "p2": [_make_probe_result("p2", 1, gold_mu_rank=None)],
        }
        result = _compute_state_transitions(probes, results_by_probe, pass_rank_threshold=3)
        assert "ranking_improvement" in result["by_phenomenon"]
        assert "cross_cluster_transfer" in result["by_phenomenon"]


# ---------------------------------------------------------------------------
# §6.1 — _compute_edge_delta_ledger (orphan updates + cluster segmentation)
# ---------------------------------------------------------------------------


class TestEdgeDeltaLedger:
    def _make_update(
        self,
        edge_id="edge-1",
        old_weight=0.5,
        new_weight=0.6,
        feedback_event_id="fe-1",
        source_mu_id=None,
        target_mu_id=None,
        source_cluster=None,
        target_cluster=None,
        source_lineage_cluster=None,
        target_lineage_cluster=None,
        series_id="series-A",
    ) -> dict:
        return {
            "edge_id": edge_id,
            "old_weight": old_weight,
            "new_weight": new_weight,
            "delta": new_weight - old_weight,
            "feedback_event_id": feedback_event_id,
            "source_mu_id": source_mu_id or _mu_id(),
            "target_mu_id": target_mu_id or _mu_id(),
            "source_cluster": source_cluster,
            "target_cluster": target_cluster,
            "source_lineage_cluster": source_lineage_cluster,
            "target_lineage_cluster": target_lineage_cluster,
            "turn_index": 1,
            "run_id": _run_id(),
            "series_id": series_id,
        }

    def test_orphan_count_zero_for_fully_linked(self):
        updates = [
            {**self._make_update(), "feedback_event_id": "fe-1"},
            {**self._make_update(), "feedback_event_id": "fe-2"},
        ]
        ledger = _compute_edge_delta_ledger(updates)
        assert ledger["orphan_update_count"] == 0

    def test_orphan_count_nonzero_for_fabricated_orphans(self):
        updates = [
            {**self._make_update(), "feedback_event_id": None},
            {**self._make_update(), "feedback_event_id": "fe-1"},
            {**self._make_update(), "feedback_event_id": None},
        ]
        ledger = _compute_edge_delta_ledger(updates)
        assert ledger["orphan_update_count"] == 2

    def test_increased_decreased_unchanged_counts(self):
        updates = [
            {**self._make_update(old_weight=0.5, new_weight=0.6)},   # increased
            {**self._make_update(old_weight=0.6, new_weight=0.5)},   # decreased
            {**self._make_update(old_weight=0.5, new_weight=0.5)},   # unchanged
        ]
        for u in updates:
            u["delta"] = u["new_weight"] - u["old_weight"]
        ledger = _compute_edge_delta_ledger(updates)
        assert ledger["increased_count"] == 1
        assert ledger["decreased_count"] == 1
        assert ledger["unchanged_count"] == 1

    def test_edge_classification_grid_counts(self):
        """M33: seed_seed/conv_seed/conv_conv x same/cross/mixed/unresolvable."""
        updates = [
            self._make_update(source_cluster="cluster_a", target_cluster="cluster_a"),
            self._make_update(source_cluster="cluster_a", target_cluster="cluster_b"),
            self._make_update(
                source_cluster="cluster_a", target_lineage_cluster="cluster_a",
            ),
            self._make_update(
                source_cluster="cluster_a", target_lineage_cluster="cluster_b",
            ),
            self._make_update(source_cluster="cluster_a"),  # conv endpoint unresolved
            self._make_update(
                source_lineage_cluster="cluster_a", target_lineage_cluster="cluster_a",
            ),
            self._make_update(
                source_lineage_cluster="cluster_a", target_lineage_cluster="cluster_b",
            ),
            self._make_update(),  # both endpoints fully unresolved
        ]
        ledger = _compute_edge_delta_ledger(updates)
        grid = ledger["edge_classification"]["by_class_relation"]
        assert grid["seed_seed"]["same_cluster"]["count"] == 1
        assert grid["seed_seed"]["cross_cluster"]["count"] == 1
        assert grid["conv_seed"]["same_cluster"]["count"] == 1
        assert grid["conv_seed"]["mixed"]["count"] == 1
        assert grid["conv_seed"]["unresolvable"]["count"] == 1
        assert grid["conv_conv"]["same_cluster"]["count"] == 1
        assert grid["conv_conv"]["cross_cluster"]["count"] == 1
        assert grid["conv_conv"]["unresolvable"]["count"] == 1

        totals = ledger["edge_classification"]["totals_by_relation"]
        assert totals["same_cluster"] == 3
        assert totals["cross_cluster"] == 2
        assert totals["mixed"] == 1
        assert totals["unresolvable"] == 2

    def test_cross_domain_watch_list_contains_mixed_and_cross_conv(self):
        """The watch list is conv_seed/mixed + conv_conv/cross_cluster only —
        the operational definition of emergent cross-domain linkage."""
        mixed_update = self._make_update(
            edge_id="edge-mixed",
            source_cluster="cluster_a", target_lineage_cluster="cluster_b",
        )
        cross_conv_update = self._make_update(
            edge_id="edge-cross-conv",
            source_lineage_cluster="cluster_a", target_lineage_cluster="cluster_b",
        )
        same_cluster_update = self._make_update(
            edge_id="edge-same",
            source_cluster="cluster_a", target_cluster="cluster_a",
        )
        ledger = _compute_edge_delta_ledger(
            [mixed_update, cross_conv_update, same_cluster_update]
        )
        watch = ledger["edge_classification"]["cross_domain_watch"]
        watch_ids = {w["edge_id"] for w in watch}
        assert watch_ids == {"edge-mixed", "edge-cross-conv"}

    def test_edge_classification_by_series_longitudinal(self):
        """Per-series breakdown, ordered by first appearance."""
        updates = [
            self._make_update(
                series_id="series-0",
                source_cluster="cluster_a", target_cluster="cluster_a",
            ),
            self._make_update(
                series_id="series-1",
                source_lineage_cluster="cluster_a", target_lineage_cluster="cluster_b",
            ),
            self._make_update(
                series_id="series-0",
                source_lineage_cluster="cluster_a", target_lineage_cluster="cluster_a",
            ),
        ]
        ledger = _compute_edge_delta_ledger(updates)
        by_series = ledger["edge_classification"]["by_series"]
        assert [s["series_id"] for s in by_series] == ["series-0", "series-1"]
        assert by_series[0]["by_class"]["seed_seed"]["count"] == 1
        assert by_series[0]["by_class"]["conv_conv"]["count"] == 1
        assert by_series[1]["by_class"]["conv_conv"]["count"] == 1

    def test_no_old_flat_keys_in_ledger_output(self):
        """The old within/cross/unknown_cluster_count keys are gone (M33
        replaces them with edge_classification)."""
        ledger = _compute_edge_delta_ledger([self._make_update()])
        assert "within_cluster_count" not in ledger
        assert "cross_cluster_count" not in ledger
        assert "unknown_cluster_count" not in ledger
        assert "cross_cluster_deltas" not in ledger


# ---------------------------------------------------------------------------
# M33 — _classify_edge_update
# ---------------------------------------------------------------------------


class TestClassifyEdgeUpdate:
    def _u(self, **kwargs) -> dict:
        base = {
            "source_cluster": None, "target_cluster": None,
            "source_lineage_cluster": None, "target_lineage_cluster": None,
        }
        base.update(kwargs)
        return base

    def test_seed_seed_same_cluster(self):
        u = self._u(source_cluster="cluster_a", target_cluster="cluster_a")
        assert _classify_edge_update(u) == ("seed_seed", "same_cluster")

    def test_seed_seed_cross_cluster(self):
        u = self._u(source_cluster="cluster_a", target_cluster="cluster_b")
        assert _classify_edge_update(u) == ("seed_seed", "cross_cluster")

    def test_conv_seed_same_cluster_source_is_seed(self):
        u = self._u(source_cluster="cluster_a", target_lineage_cluster="cluster_a")
        assert _classify_edge_update(u) == ("conv_seed", "same_cluster")

    def test_conv_seed_same_cluster_target_is_seed(self):
        u = self._u(target_cluster="cluster_a", source_lineage_cluster="cluster_a")
        assert _classify_edge_update(u) == ("conv_seed", "same_cluster")

    def test_conv_seed_mixed_on_disagreement(self):
        u = self._u(source_cluster="cluster_a", target_lineage_cluster="cluster_b")
        assert _classify_edge_update(u) == ("conv_seed", "mixed")

    def test_conv_seed_unresolvable_when_lineage_missing(self):
        """Missing prompt_id on the conv endpoint still yields edge_class
        conv_seed via the seed-adjacency fallback — just an unresolvable
        relation, not a fully-unknown edge."""
        u = self._u(source_cluster="cluster_a")
        assert _classify_edge_update(u) == ("conv_seed", "unresolvable")

    def test_conv_conv_same_cluster(self):
        u = self._u(source_lineage_cluster="cluster_a", target_lineage_cluster="cluster_a")
        assert _classify_edge_update(u) == ("conv_conv", "same_cluster")

    def test_conv_conv_cross_cluster(self):
        u = self._u(source_lineage_cluster="cluster_a", target_lineage_cluster="cluster_b")
        assert _classify_edge_update(u) == ("conv_conv", "cross_cluster")

    def test_conv_conv_unresolvable_when_one_lineage_missing(self):
        u = self._u(source_lineage_cluster="cluster_a")
        assert _classify_edge_update(u) == ("conv_conv", "unresolvable")

    def test_conv_conv_unresolvable_when_both_missing(self):
        """The residual bucket: no prompt_id and no seed endpoint at all."""
        u = self._u()
        assert _classify_edge_update(u) == ("conv_conv", "unresolvable")


# ---------------------------------------------------------------------------
# M33 — _cluster_from_prompt_id / _build_mu_lineage_clusters
# ---------------------------------------------------------------------------


class TestPromptLineage:
    def test_cluster_from_prompt_id_strips_numeric_suffix(self):
        assert _cluster_from_prompt_id("cluster_a_0007") == "cluster_a"

    def test_cluster_from_prompt_id_none_for_none(self):
        assert _cluster_from_prompt_id(None) is None

    def test_cluster_from_prompt_id_none_for_empty_string(self):
        assert _cluster_from_prompt_id("") is None

    def test_cluster_from_prompt_id_none_when_no_numeric_suffix(self):
        assert _cluster_from_prompt_id("cluster_a") is None

    def test_build_mu_lineage_covers_input_prompt_output_and_tool_mus(self):
        run = _make_run_with_updates([], turn_index=1)
        run["tool_calls"] = ["tc-mu-1", "tr-mu-1"]
        run_prompt_ids = {run["run_id"]: "cluster_c_0002"}
        lineage = _build_mu_lineage_clusters([run], run_prompt_ids)
        assert lineage[run["input_mu_id"]] == "cluster_c"
        assert lineage[run["prompt_mu_id"]] == "cluster_c"
        assert lineage[run["output_mu_id"]] == "cluster_c"
        assert lineage["tc-mu-1"] == "cluster_c"
        assert lineage["tr-mu-1"] == "cluster_c"

    def test_build_mu_lineage_skips_runs_with_no_prompt_id(self):
        """Probe turns and dry runs never carry a bulk prompt_id — their
        MUs contribute nothing to the lineage map."""
        run = _make_run_with_updates([], turn_index=1)
        lineage = _build_mu_lineage_clusters([run], run_prompt_ids={})
        assert lineage == {}


# ---------------------------------------------------------------------------
# M33 — _collect_all_weight_updates carries the lineage signal through
# ---------------------------------------------------------------------------


class TestCollectAllWeightUpdatesLineage:
    def test_lineage_cluster_populated_for_conversation_mu(self):
        src_mu = _mu_id()
        tgt_mu = _mu_id()
        run = _make_run_with_updates(
            weight_updates=[
                {
                    "edge_id": "edge-1", "old_weight": 0.5, "new_weight": 0.6,
                    "feedback_event_id": "fe-1",
                    "source_mu_id": src_mu, "target_mu_id": tgt_mu,
                }
            ],
        )
        mu_info = {}  # neither MU is a seed MU
        mu_lineage = {src_mu: "cluster_a", tgt_mu: "cluster_b"}
        updates = _collect_all_weight_updates([run], mu_info, mu_lineage)
        assert updates[0]["source_lineage_cluster"] == "cluster_a"
        assert updates[0]["target_lineage_cluster"] == "cluster_b"
        assert updates[0]["source_cluster"] is None
        assert updates[0]["target_cluster"] is None

    def test_lineage_defaults_to_none_when_not_supplied(self):
        run = _make_run_with_updates(
            weight_updates=[
                {
                    "edge_id": "edge-1", "old_weight": 0.5, "new_weight": 0.6,
                    "feedback_event_id": "fe-1",
                    "source_mu_id": _mu_id(), "target_mu_id": _mu_id(),
                }
            ],
        )
        updates = _collect_all_weight_updates([run], mu_cluster_info={})
        assert updates[0]["source_lineage_cluster"] is None
        assert updates[0]["target_lineage_cluster"] is None


# ---------------------------------------------------------------------------
# §6.1 — _compute_saturation_analysis
# ---------------------------------------------------------------------------


class TestSaturationAnalysis:
    def _make_runs_for_edge(self, weight_sequence: list, edge_id: str = "edge-1") -> list[dict]:
        """Build minimal run dicts producing the given weight sequence for one edge."""
        runs = []
        for i, (old_w, new_w) in enumerate(zip(weight_sequence, weight_sequence[1:]), start=1):
            runs.append(
                _make_run_with_updates(
                    weight_updates=[{
                        "edge_id": edge_id,
                        "old_weight": old_w,
                        "new_weight": new_w,
                        "source_mu_id": "mu-src",
                        "target_mu_id": "mu-tgt",
                        "feedback_event_id": "fe-1",
                    }],
                    turn_index=i,
                )
            )
        return runs

    def test_saturation_fires_above_threshold(self):
        # Weights: 0.5 → 0.8 → 0.95 (final > 0.9)
        runs = self._make_runs_for_edge([0.5, 0.8, 0.95])
        result = _compute_saturation_analysis(runs, saturation_threshold=0.9)
        assert result["saturation_flag_count"] == 1
        assert result["saturation_flags"][0]["edge_id"] == "edge-1"
        assert result["saturation_flags"][0]["final_weight"] == 0.95

    def test_saturation_does_not_fire_below_threshold(self):
        # Final weight = 0.85, which is below 0.9
        runs = self._make_runs_for_edge([0.5, 0.7, 0.85])
        result = _compute_saturation_analysis(runs, saturation_threshold=0.9)
        assert result["saturation_flag_count"] == 0

    def test_runaway_fires_for_accelerating_deltas(self):
        # Deltas: 0.05, 0.07, 0.10 — each larger than previous
        weights = [0.5, 0.55, 0.62, 0.72]
        runs = self._make_runs_for_edge(weights)
        result = _compute_saturation_analysis(runs, saturation_threshold=0.9)
        assert result["runaway_flag_count"] == 1

    def test_runaway_does_not_fire_for_diminishing_deltas(self):
        # Deltas: 0.10, 0.07, 0.05 — each smaller than previous (diminishing)
        weights = [0.5, 0.60, 0.67, 0.72]
        runs = self._make_runs_for_edge(weights)
        result = _compute_saturation_analysis(runs, saturation_threshold=0.9)
        assert result["runaway_flag_count"] == 0

    def test_empty_runs_returns_zeros(self):
        result = _compute_saturation_analysis([], saturation_threshold=0.9)
        assert result["saturation_flag_count"] == 0
        assert result["runaway_flag_count"] == 0
        assert result["total_unique_edges"] == 0
        assert result["same_direction_streak_count"] == 0

    def test_decaying_down_streak_is_not_runaway_but_is_informational(self):
        """
        Regression (M35): exp0's exact sequence — a Beta(1,1) posterior-mean
        down-streak with SHRINKING magnitude each step (1/2 -> 1/3 -> 1/4 ->
        1/5, deltas -0.1667/-0.0833/-0.05) — is ordinary convergence, not
        runaway reinforcement. The old heuristic (any 3 consecutive
        algebraically-increasing deltas) flagged this because -0.05 > -0.0833
        > -0.1667 numerically, even though |delta| is shrinking. It must not
        appear in runaway_flags, but it IS a same-direction streak and must
        still be counted in the informational statistic.
        """
        weights = [1 / 2, 1 / 3, 1 / 4, 1 / 5]
        runs = self._make_runs_for_edge(weights)
        result = _compute_saturation_analysis(runs, saturation_threshold=0.9)
        assert result["runaway_flag_count"] == 0
        assert result["same_direction_streak_count"] == 1
        assert result["same_direction_streaks"][0]["edge_id"] == "edge-1"

    def test_non_decreasing_magnitude_streak_flagged_as_runaway(self):
        """A same-direction streak with non-decreasing |delta| (not just strictly
        increasing) is still genuinely accelerating and must flag."""
        # Deltas: 0.05, 0.05, 0.05 — constant magnitude, not diminishing.
        weights = [0.5, 0.55, 0.60, 0.65]
        runs = self._make_runs_for_edge(weights)
        result = _compute_saturation_analysis(runs, saturation_threshold=0.9)
        assert result["runaway_flag_count"] == 1
        assert result["runaway_flags"][0]["non_decreasing_magnitude"] is True

    def test_threshold_crossing_streak_flagged_despite_shrinking_magnitude(self):
        """
        A same-direction streak whose |delta| is shrinking (would not flag on
        magnitude alone) must still flag as runaway if it carries the edge's
        weight across saturation_threshold during the streak — dangerous
        regardless of whether each step is individually diminishing.
        """
        # Deltas: 0.07, 0.03, 0.02 (shrinking) but weight crosses 0.9 (0.85 -> 0.97).
        weights = [0.85, 0.92, 0.95, 0.97]
        runs = self._make_runs_for_edge(weights)
        result = _compute_saturation_analysis(runs, saturation_threshold=0.9)
        assert result["runaway_flag_count"] == 1
        flag = result["runaway_flags"][0]
        assert flag["non_decreasing_magnitude"] is False
        assert flag["crosses_saturation_threshold"] is True


# ---------------------------------------------------------------------------
# §6.1 — _compute_bridge_mu_analysis (rank trajectory)
# ---------------------------------------------------------------------------


class TestBridgeMuAnalysis:
    def test_rank_improvement_identified(self):
        bridge_probe = {
            "probe_id": "bridge-1",
            "phenomenon_type": "cross_cluster_transfer",
            "is_bridge_probe": True,
            "bridge_pair_id": "pair-A",
        }
        results_by_probe = {
            "bridge-1": [
                _make_probe_result("bridge-1", 1, gold_mu_rank=5),
                _make_probe_result("bridge-1", 2, gold_mu_rank=2),  # improvement: 5 → 2
            ]
        }
        result = _compute_bridge_mu_analysis(
            [bridge_probe], results_by_probe, [], {}, pass_rank_threshold=3
        )
        bp = result["bridge_probe_results"][0]
        assert bp["rank_improvement_count"] == 1
        tc = bp["transfer_candidates"][0]
        assert tc["session_n"] == 1
        assert tc["session_n1"] == 2
        assert tc["rank_n"] == 5
        assert tc["rank_n1"] == 2

    def test_no_improvement_produces_no_candidates(self):
        bridge_probe = {
            "probe_id": "bridge-1",
            "phenomenon_type": "cross_cluster_transfer",
            "is_bridge_probe": True,
            "bridge_pair_id": "pair-A",
        }
        results_by_probe = {
            "bridge-1": [
                _make_probe_result("bridge-1", 1, gold_mu_rank=2),
                _make_probe_result("bridge-1", 2, gold_mu_rank=4),  # regression, not improvement
            ]
        }
        result = _compute_bridge_mu_analysis(
            [bridge_probe], results_by_probe, [], {}, pass_rank_threshold=3
        )
        bp = result["bridge_probe_results"][0]
        assert bp["rank_improvement_count"] == 0

    def test_cross_cluster_transfer_flag_set_when_cluster_a_only(self):
        bridge_probe = {
            "probe_id": "bridge-1",
            "phenomenon_type": "cross_cluster_transfer",
            "is_bridge_probe": True,
            "bridge_pair_id": "pair-A",
        }
        results_by_probe = {
            "bridge-1": [
                _make_probe_result("bridge-1", 1, gold_mu_rank=5),
                _make_probe_result("bridge-1", 2, gold_mu_rank=2),
            ]
        }
        src_mu = _mu_id()
        tgt_mu = _mu_id()
        # Run with feedback_signal=1, source MU in cluster_a
        reinforcement_run = _make_run_with_updates(
            weight_updates=[{
                "edge_id": "edge-1",
                "old_weight": 0.5,
                "new_weight": 0.6,
                "source_mu_id": src_mu,
                "target_mu_id": tgt_mu,
                "feedback_event_id": "fe-1",
            }],
            feedback_signal=1,
            turn_index=1,
        )
        mu_cluster_info = {
            src_mu: {"cluster_id": "cluster_a", "series_id": "s1", "source_type": "agent_output"},
            tgt_mu: {"cluster_id": "cluster_a", "series_id": "s1", "source_type": "agent_output"},
        }
        result = _compute_bridge_mu_analysis(
            [bridge_probe], results_by_probe, [reinforcement_run], mu_cluster_info,
            pass_rank_threshold=3
        )
        tc = result["bridge_probe_results"][0]["transfer_candidates"][0]
        assert tc["cross_cluster_transfer_flag"] is True

    def test_cross_cluster_transfer_flag_false_when_mixed_clusters(self):
        bridge_probe = {
            "probe_id": "bridge-1",
            "phenomenon_type": "cross_cluster_transfer",
            "is_bridge_probe": True,
            "bridge_pair_id": "pair-A",
        }
        results_by_probe = {
            "bridge-1": [
                _make_probe_result("bridge-1", 1, gold_mu_rank=5),
                _make_probe_result("bridge-1", 2, gold_mu_rank=2),
            ]
        }
        src_mu = _mu_id()
        tgt_mu = _mu_id()
        reinforcement_run = _make_run_with_updates(
            weight_updates=[
                {
                    "edge_id": "edge-1",
                    "old_weight": 0.5,
                    "new_weight": 0.6,
                    "source_mu_id": src_mu,
                    "target_mu_id": tgt_mu,
                    "feedback_event_id": "fe-1",
                }
            ],
            feedback_signal=1,
            turn_index=1,
        )
        mu_cluster_info = {
            src_mu: {"cluster_id": "cluster_b", "series_id": "s1", "source_type": "agent_output"},
            tgt_mu: {"cluster_id": "cluster_a", "series_id": "s1", "source_type": "agent_output"},
        }
        result = _compute_bridge_mu_analysis(
            [bridge_probe], results_by_probe, [reinforcement_run], mu_cluster_info,
            pass_rank_threshold=3
        )
        tc = result["bridge_probe_results"][0]["transfer_candidates"][0]
        assert tc["cross_cluster_transfer_flag"] is False


# ---------------------------------------------------------------------------
# §6.1 — DB round-trip for outcome_override
# ---------------------------------------------------------------------------


class TestOutcomeOverrideRoundTrip:
    def test_outcome_override_stored_and_retrieved(self):
        with TempDB() as store:
            # Insert a probe definition first (FK constraint)
            run(store.sync_probes([{
                "probe_id": "p1",
                "probe_set_version": "v1",
                "phenomenon_type": "ranking_improvement",
                "prompt_text": "test",
                "gold_mu_ids": [],
                "is_bridge_probe": False,
                "bridge_pair_id": None,
            }]))
            # Insert probe result with outcome_override
            run(store.insert_probe_result({
                "result_id": str(uuid.uuid4()),
                "probe_id": "p1",
                "run_id": None,
                "measurement_session_index": 1,
                "condition_label": "learning",
                "retrieved_mu_ids": [],
                "gold_mu_rank": 1,
                "retrieval_precision": 1.0,
                "retrieval_recall": 1.0,
                "bm25_scores_per_mu": {},
                "outcome_override": "negative_flagged",
            }))
            results = run(store.query_probe_results(probe_id="p1"))
            assert len(results) == 1
            assert results[0]["outcome_override"] == "negative_flagged"

    def test_outcome_override_none_when_not_set(self):
        with TempDB() as store:
            run(store.sync_probes([{
                "probe_id": "p2",
                "probe_set_version": "v1",
                "phenomenon_type": "ranking_improvement",
                "prompt_text": "test",
                "gold_mu_ids": [],
                "is_bridge_probe": False,
                "bridge_pair_id": None,
            }]))
            run(store.insert_probe_result({
                "result_id": str(uuid.uuid4()),
                "probe_id": "p2",
                "run_id": None,
                "measurement_session_index": 1,
                "condition_label": "learning",
                "retrieved_mu_ids": [],
                "gold_mu_rank": None,
                "retrieval_precision": None,
                "retrieval_recall": None,
                "bm25_scores_per_mu": {},
                "outcome_override": None,
            }))
            results = run(store.query_probe_results(probe_id="p2"))
            assert len(results) == 1
            assert results[0]["outcome_override"] is None


# ---------------------------------------------------------------------------
# §6.1 — get_mu_info_batch
# ---------------------------------------------------------------------------


class TestGetMuInfoBatch:
    def test_returns_series_cluster_and_source_type(self):
        with TempDB() as store:
            mu_id_val = _mu_id()
            run(store.create_mu(
                mu_id=mu_id_val,
                source_type="agent_output",
                raw_content="test content",
                series_id="series-A",
                experiment_id="exp-1",
                cluster_id="cluster_a",
            ))
            info = run(store.get_mu_info_batch([mu_id_val]))
            assert mu_id_val in info
            assert info[mu_id_val]["series_id"] == "series-A"
            assert info[mu_id_val]["cluster_id"] == "cluster_a"
            assert info[mu_id_val]["source_type"] == "agent_output"

    def test_missing_mu_excluded_from_result(self):
        with TempDB() as store:
            info = run(store.get_mu_info_batch(["nonexistent-mu-id"]))
            assert "nonexistent-mu-id" not in info

    def test_empty_list_returns_empty_dict(self):
        with TempDB() as store:
            info = run(store.get_mu_info_batch([]))
            assert info == {}


# ---------------------------------------------------------------------------
# §6.1 — get_all_runs_for_experiment
# ---------------------------------------------------------------------------


class TestGetAllRunsForExperiment:
    def _make_minimal_run(
        self,
        run_id=None,
        experiment_id="exp-1",
        series_id="series-A",
        turn_index=1,
    ) -> dict:
        return {
            "run_id": run_id or _run_id(),
            "series_id": series_id,
            "experiment_id": experiment_id,
            "timestamp": f"2026-01-01T00:{turn_index:02d}:00+00:00",
            "input_mu_id": _mu_id(),
            "output_mu_id": _mu_id(),
            "tool_calls": [],
            "activated_mu_ids": [],
            "activated_edge_ids": [],
            "prompt_mu_id": _mu_id(),
            "tokens_in": 100,
            "tokens_out": 50,
            "latency_ms": 200,
            "feedback_signal": None,
            "weight_updates": [],
            "hyperparameters_snapshot": {},
            "algorithm": "simple_rl",
            "bm25_scores_per_mu": {},
            "entry_point_mu_ids": [],
            "candidate_set_before_ranking": [],
            "traversal_path": [],
            "hop_depth_per_retrieved_mu": {},
            "edge_contributions": {},
            "assembled_prompt_json": [],
            "config_hash": None,
            "full_config_snapshot": {},
            "condition_label": "learning",
            "turn_index": turn_index,
            "session_turn_index": turn_index,
            "measurement_session_index": None,
            "is_probe_turn": False,
            "use_session_context": False,
            "failure_reason": None,
        }

    def test_returns_all_runs_for_experiment(self):
        with TempDB() as store:
            run(store.insert_run(self._make_minimal_run(experiment_id="exp-1", turn_index=1)))
            run(store.insert_run(self._make_minimal_run(experiment_id="exp-1", turn_index=2)))
            run(store.insert_run(self._make_minimal_run(experiment_id="exp-99", turn_index=1)))

            runs = run(store.get_all_runs_for_experiment("exp-1"))
            assert len(runs) == 2
            assert all(r["experiment_id"] == "exp-1" for r in runs)

    def test_ordered_by_turn_index(self):
        with TempDB() as store:
            run(store.insert_run(self._make_minimal_run(experiment_id="exp-2", turn_index=3)))
            run(store.insert_run(self._make_minimal_run(experiment_id="exp-2", turn_index=1)))
            run(store.insert_run(self._make_minimal_run(experiment_id="exp-2", turn_index=2)))

            runs = run(store.get_all_runs_for_experiment("exp-2"))
            indices = [r["turn_index"] for r in runs]
            assert indices == sorted(indices)


# ---------------------------------------------------------------------------
# _compute_cluster_winner_table — M39 per-cluster argmax/alignment table
# ---------------------------------------------------------------------------


def _sample_pass_rates(probe_sessions: dict) -> dict:
    """probe_sessions: {probe_id: [(session_index, pass_rate), ...]}"""
    per_probe = []
    for probe_id, sessions in probe_sessions.items():
        per_probe.append({
            "probe_id": probe_id,
            "phenomenon_type": "spoke_retrieval",
            "sessions": [
                {"session_index": idx, "pass_rate": rate, "pass_count": 0,
                 "fail_count": 0, "repeats_completed": 25, "partial_battery": False}
                for idx, rate in sessions
            ],
        })
    return {"per_probe": per_probe, "by_phenomenon": {}}


_MEMBERSHIP = {
    "vs_a1": {"spoke_key": "a_spoke1", "cluster_key": "cluster_a"},
    "vs_a2": {"spoke_key": "a_spoke2", "cluster_key": "cluster_a"},
    "vs_d5": {"spoke_key": "d_spoke5", "cluster_key": "cluster_d"},
    "vs_d6": {"spoke_key": "d_spoke6", "cluster_key": "cluster_d"},
}


class TestComputeClusterWinnerTable:
    def test_argmax_picks_highest_pass_rate(self):
        """exp0b-shaped case: an untrained neighbor (vs_d6) climbs past the
        trained spoke (vs_d5) — the table's winner must reflect that, not
        the trained flag."""
        spr = _sample_pass_rates({
            "vs_d5": [(0, 0.5), (4, 0.0)],
            "vs_d6": [(0, 0.48), (4, 0.84)],
        })
        table = _compute_cluster_winner_table(spr, _MEMBERSHIP, trained_probe_ids={"vs_d5"})

        final = table["cluster_d"]["final_session_winner"]
        assert final["winner_probe_id"] == "vs_d6"
        assert final["winner_pass_rate"] == 0.84
        assert final["trained"] is False

    def test_trained_flag_true_when_trained_spoke_wins(self):
        spr = _sample_pass_rates({
            "vs_a1": [(0, 0.9)],
            "vs_a2": [(0, 0.1)],
        })
        table = _compute_cluster_winner_table(spr, _MEMBERSHIP, trained_probe_ids={"vs_a1"})

        winner = table["cluster_a"]["sessions"][0]
        assert winner["winner_probe_id"] == "vs_a1"
        assert winner["trained"] is True

    def test_tie_recorded_and_broken_by_probe_id_ascending(self):
        spr = _sample_pass_rates({
            "vs_d6": [(0, 0.5)],
            "vs_d5": [(0, 0.5)],
        })
        table = _compute_cluster_winner_table(spr, _MEMBERSHIP, trained_probe_ids=set())

        row = table["cluster_d"]["sessions"][0]
        assert row["winner_probe_id"] == "vs_d5"  # alphabetically first of the tie
        assert row["tied_with"] == ["vs_d5", "vs_d6"]

    def test_no_tie_leaves_tied_with_empty(self):
        spr = _sample_pass_rates({"vs_a1": [(0, 0.5)], "vs_a2": [(0, 0.2)]})
        table = _compute_cluster_winner_table(spr, _MEMBERSHIP, trained_probe_ids=set())
        assert table["cluster_a"]["sessions"][0]["tied_with"] == []

    def test_probes_with_no_membership_are_excluded(self):
        """A probe whose gold MU doesn't resolve to a known spoke (e.g. a
        bridge probe) plays no role in the cluster tables."""
        spr = _sample_pass_rates({"vs_bridge1": [(0, 0.9)]})
        table = _compute_cluster_winner_table(spr, probe_spoke_membership={}, trained_probe_ids=set())
        assert table == {}

    def test_sessions_with_none_pass_rate_are_skipped(self):
        per_probe = [{
            "probe_id": "vs_a1", "phenomenon_type": "spoke_retrieval",
            "sessions": [{"session_index": 0, "pass_rate": None}],
        }]
        table = _compute_cluster_winner_table(
            {"per_probe": per_probe}, _MEMBERSHIP, trained_probe_ids=set()
        )
        assert table == {}

    def test_empty_sample_pass_rates_yields_empty_table(self):
        table = _compute_cluster_winner_table({"per_probe": []}, _MEMBERSHIP, trained_probe_ids=set())
        assert table == {}
