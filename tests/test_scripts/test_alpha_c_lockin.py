"""
Tests for the Alpha C lock-in analysis slot logic.

The preservation/degenerate/pinned rules decide the headline number, and each
has a case where the naive reading is wrong (ties, zero-pass winners, probes
the control already solves for free), so each gets an explicit test.
"""

import importlib.util
import sys
from pathlib import Path

import pytest

_SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "alpha_c_lockin.py"
_spec = importlib.util.spec_from_file_location("alpha_c_lockin", _SCRIPT)
alpha_c = importlib.util.module_from_spec(_spec)
# Register before exec: the script uses `from __future__ import annotations`, so
# @dataclass resolves its string annotations via sys.modules[cls.__module__].
sys.modules["alpha_c_lockin"] = alpha_c
_spec.loader.exec_module(alpha_c)


def make_slot(**overrides):
    """Build a Slot with sensible defaults, overriding only what a test cares about."""
    defaults = dict(
        run_label="test run",
        condition="cfgA",
        cluster="cluster_a",
        trained_probe="vs_a1",
        s1_winner="vs_a1",
        s1_pass_rate=0.5,
        s1_tied=[],
        final_winner="vs_a1",
        final_pass_rate=0.5,
        final_tied=[],
        pinned_probes=frozenset(),
        first_upvoted=None,
    )
    defaults.update(overrides)
    return alpha_c.Slot(**defaults)


class TestPreservation:
    def test_same_winner_no_ties_is_preserved_strictly(self):
        slot = make_slot(s1_winner="vs_a3", final_winner="vs_a3")
        assert slot.preserved_strict is True
        assert slot.preserved_lenient is True

    def test_different_winner_is_not_preserved(self):
        slot = make_slot(s1_winner="vs_a3", final_winner="vs_a5")
        assert slot.preserved_strict is False
        assert slot.preserved_lenient is False

    def test_session_1_tie_blocks_strict_but_allows_lenient(self):
        """An argmax pick among equals is not evidence the winner was decided."""
        slot = make_slot(
            s1_winner="vs_a3", s1_tied=["vs_a3", "vs_a5"], final_winner="vs_a5"
        )
        assert slot.preserved_strict is False
        assert slot.preserved_lenient is True

    def test_final_tie_blocks_strict(self):
        slot = make_slot(
            s1_winner="vs_a3", final_winner="vs_a3", final_tied=["vs_a3", "vs_a5"]
        )
        assert slot.preserved_strict is False


class TestDegenerate:
    def test_zero_pass_winner_is_degenerate(self):
        """cfgA rep1 cluster_b: everything scored 0.00, so nothing won."""
        slot = make_slot(final_pass_rate=0.0)
        assert slot.is_degenerate is True

    def test_wide_tie_is_degenerate(self):
        slot = make_slot(final_tied=["vs_a1", "vs_a2", "vs_a3", "vs_a4"])
        assert slot.is_degenerate is True

    def test_narrow_tie_with_passes_is_not_degenerate(self):
        slot = make_slot(final_pass_rate=0.08, final_tied=["vs_a1", "vs_a4"])
        assert slot.is_degenerate is False


class TestAlignment:
    def test_aligned_when_final_winner_is_trained_spoke(self):
        slot = make_slot(trained_probe="vs_a1", final_winner="vs_a1")
        assert slot.final_aligned is True
        assert slot.final_aligned_solid is True

    def test_pinned_winner_is_aligned_but_not_solid(self):
        """rep3 cluster_a: vs_a1 pinned at 1.00 under control, 0.12 learned."""
        slot = make_slot(
            trained_probe="vs_a1",
            final_winner="vs_a1",
            final_pass_rate=0.12,
            pinned_probes=frozenset({"vs_a1"}),
        )
        assert slot.final_aligned is True
        assert slot.winner_is_pinned is True
        assert slot.final_aligned_solid is False

    def test_untrained_winner_is_not_aligned(self):
        slot = make_slot(trained_probe="vs_a1", final_winner="vs_a2")
        assert slot.final_aligned is False


class TestFirstUpvoted:
    def test_missing_first_upvoted_never_counts_as_a_match(self):
        """A slot with no recorded first-upvoted spoke must not score as a hit."""
        slot = make_slot(first_upvoted=None, final_winner="vs_a1")
        assert slot.final_matches_first_upvoted is False
        assert slot.first_upvoted_is_trained is False

    def test_first_upvoted_winning_the_cluster_is_a_match(self):
        slot = make_slot(first_upvoted="vs_a3", final_winner="vs_a3")
        assert slot.final_matches_first_upvoted is True

    def test_first_upvoted_losing_the_cluster_is_not_a_match(self):
        slot = make_slot(first_upvoted="vs_a3", final_winner="vs_a5")
        assert slot.final_matches_first_upvoted is False

    def test_first_upvoted_trained_is_independent_of_who_won(self):
        slot = make_slot(
            trained_probe="vs_a1", first_upvoted="vs_a1", final_winner="vs_a5"
        )
        assert slot.first_upvoted_is_trained is True
        assert slot.final_matches_first_upvoted is False


class TestMuToProbeMapping:
    def test_maps_gold_mus_to_probe_ids(self):
        mapping = alpha_c.load_mu_to_probe()
        if not mapping:
            pytest.skip("probe set not present in this checkout")
        # Deterministic v3 fixture id for cluster_a spoke 1.
        assert mapping["a0000002-aaaa-4aaa-8aaa-000000000002"] == "vs_a1"
        assert len(mapping) == 28


class TestFirstUpvotedQuery:
    """Exercises the read-only graph.db path when the DB is present."""

    def test_rep3_resolves_a_spoke_for_every_cluster(self):
        if not alpha_c.GRAPH_DB.exists():
            pytest.skip("graph.db not present")
        mapping = alpha_c.load_mu_to_probe()
        first = alpha_c.load_first_upvoted_spokes(
            alpha_c.GRAPH_DB, "exp01-cfgA-rep3-2026-08-17_learned", mapping
        )
        assert set(first) == {"cluster_a", "cluster_b", "cluster_c", "cluster_d"}
        for cluster, probe in first.items():
            assert probe.startswith(f"vs_{cluster[-1]}")

    def test_unknown_experiment_returns_empty(self):
        if not alpha_c.GRAPH_DB.exists():
            pytest.skip("graph.db not present")
        first = alpha_c.load_first_upvoted_spokes(
            alpha_c.GRAPH_DB, "no-such-experiment_learned", alpha_c.load_mu_to_probe()
        )
        assert first == {}


class TestRateFormatting:
    def test_rate_formats_as_fraction_and_percent(self):
        assert alpha_c.rate(3, 12) == "3/12 (25%)"

    def test_empty_denominator_does_not_divide_by_zero(self):
        assert alpha_c.rate(0, 0) == "0/0 (n/a)"


class TestRealDataLoad:
    """The analysis is only meaningful if it reproduces known pre-registered counts."""

    def test_rep3_reproduces_one_of_four_aligned(self):
        slots = alpha_c.load_slots("exp01-cfgA-rep3-2026-08-17", "cfgA", "cfgA rep3")
        if not slots:
            pytest.skip("rep3 report not present in this checkout")
        assert len(slots) == 4
        assert sum(1 for s in slots if s.final_aligned) == 1

    def test_baseline_rep1_reproduces_zero_of_four_aligned(self):
        slots = alpha_c.load_slots("exp0b-2026-08-06", "baseline", "exp0b rep1")
        if not slots:
            pytest.skip("baseline report not present in this checkout")
        assert sum(1 for s in slots if s.final_aligned) == 0
