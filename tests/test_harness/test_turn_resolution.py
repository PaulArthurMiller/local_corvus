"""Tests for scripts/harness/turn_resolution.py — deterministic turn-type resolution."""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "scripts"))

from harness.schema import RunPlan
from harness.turn_resolution import resolve_turn_sequence


def _plan(**overrides):
    kwargs = dict(
        plan_id="p1",
        plan_name="Test plan",
        conceptual_fields={"count": 4, "subjects": "auto"},
        series_count=3,
        turns_per_series=10,
        probe_ratio=0.2,
        probe_placement="random",
        random_seed=42,
    )
    kwargs.update(overrides)
    return RunPlan(**kwargs)


def test_same_seed_and_plan_produces_same_sequence():
    plan = _plan()
    seq_1 = resolve_turn_sequence(plan)
    seq_2 = resolve_turn_sequence(plan)
    assert seq_1 == seq_2


def test_different_seed_can_produce_different_sequence():
    seq_seed_a = resolve_turn_sequence(_plan(random_seed=1))
    seq_seed_b = resolve_turn_sequence(_plan(random_seed=2))
    assert seq_seed_a != seq_seed_b


def test_sequence_covers_every_series_and_turn_exactly_once():
    plan = _plan(series_count=3, turns_per_series=10)
    seq = resolve_turn_sequence(plan)
    keys = {(t["series_index"], t["turn_index"]) for t in seq}
    expected = {(s, t) for s in range(3) for t in range(10)}
    assert keys == expected


def test_probe_ratio_changes_proportion_of_probe_turns():
    low_ratio_seq = resolve_turn_sequence(_plan(probe_ratio=0.1, turns_per_series=20))
    high_ratio_seq = resolve_turn_sequence(_plan(probe_ratio=0.5, turns_per_series=20))

    low_probe_count = sum(1 for t in low_ratio_seq if t["turn_type"] == "probe")
    high_probe_count = sum(1 for t in high_ratio_seq if t["turn_type"] == "probe")

    assert low_probe_count < high_probe_count


def test_end_placement_puts_probes_at_the_tail_of_each_series():
    plan = _plan(probe_placement="end", probe_ratio=0.3, turns_per_series=10, series_count=2)
    seq = resolve_turn_sequence(plan)
    for series_index in range(2):
        series_turns = [t for t in seq if t["series_index"] == series_index]
        series_turns.sort(key=lambda t: t["turn_index"])
        types = [t["turn_type"] for t in series_turns]
        # last 3 (10 * 0.3) should be probe, everything before bulk
        assert types[-3:] == ["probe"] * 3
        assert all(t == "bulk" for t in types[:-3])


def test_fixed_checkpoints_placement_uses_exact_positions():
    plan = _plan(
        probe_placement="fixed_checkpoints",
        probe_checkpoint_positions=[2, 5],
        turns_per_series=10,
        series_count=1,
    )
    seq = resolve_turn_sequence(plan)
    probe_positions = sorted(t["turn_index"] for t in seq if t["turn_type"] == "probe")
    assert probe_positions == [2, 5]


def test_probe_turns_never_flagged_for_feedback_by_construction():
    # This is a documentation-level assertion: turn_type == "probe" is the
    # single signal callers must check before submitting feedback.
    plan = _plan(probe_placement="end", probe_ratio=0.2, turns_per_series=10)
    seq = resolve_turn_sequence(plan)
    probe_turns = [t for t in seq if t["turn_type"] == "probe"]
    bulk_turns = [t for t in seq if t["turn_type"] == "bulk"]
    assert probe_turns and bulk_turns
    assert set(t["turn_type"] for t in seq) == {"probe", "bulk"}
