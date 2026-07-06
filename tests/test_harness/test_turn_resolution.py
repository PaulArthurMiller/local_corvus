"""Tests for scripts/harness/turn_resolution.py — deterministic turn-type resolution."""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "scripts"))

from harness.schema import RunPlan
from harness.turn_resolution import (
    TURN_TYPE_BULK,
    TURN_TYPE_PROBE,
    assign_probe_schedule,
    assign_prompt_ids,
    resolve_turn_sequence,
)


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


# ---------------------------------------------------------------------------
# assign_prompt_ids — M19 Phase 1 deterministic prompt_id assignment
# ---------------------------------------------------------------------------

def _bank_ids(n):
    return [f"cluster_a_{i:04d}" for i in range(n)]


def test_assign_prompt_ids_same_seed_produces_same_sequence():
    plan = _plan(probe_placement="end", probe_ratio=0.2, turns_per_series=10)
    seq = resolve_turn_sequence(plan)
    prompt_ids = _bank_ids(5)

    assigned_1 = assign_prompt_ids(seq, prompt_ids, random_seed=42)
    assigned_2 = assign_prompt_ids(seq, prompt_ids, random_seed=42)

    assert assigned_1 == assigned_2


def test_assign_prompt_ids_different_seed_produces_different_sequence():
    plan = _plan(probe_placement="end", probe_ratio=0.2, turns_per_series=10)
    seq = resolve_turn_sequence(plan)
    prompt_ids = _bank_ids(10)

    assigned_a = assign_prompt_ids(seq, prompt_ids, random_seed=1)
    assigned_b = assign_prompt_ids(seq, prompt_ids, random_seed=2)

    ids_a = [t.get("prompt_id") for t in assigned_a if t["turn_type"] == TURN_TYPE_BULK]
    ids_b = [t.get("prompt_id") for t in assigned_b if t["turn_type"] == TURN_TYPE_BULK]
    assert ids_a != ids_b


def test_assign_prompt_ids_only_assigns_bulk_turns():
    plan = _plan(probe_placement="end", probe_ratio=0.2, turns_per_series=10)
    seq = resolve_turn_sequence(plan)
    assigned = assign_prompt_ids(seq, _bank_ids(5), random_seed=42)

    for turn in assigned:
        if turn["turn_type"] == TURN_TYPE_BULK:
            assert "prompt_id" in turn
        else:
            assert "prompt_id" not in turn


def test_assign_prompt_ids_bank_smaller_than_bulk_turns_reshuffles_without_crashing():
    plan = _plan(probe_placement="end", probe_ratio=0.1, turns_per_series=20, series_count=1)
    seq = resolve_turn_sequence(plan)
    bulk_turn_count = sum(1 for t in seq if t["turn_type"] == TURN_TYPE_BULK)
    prompt_ids = _bank_ids(3)
    assert bulk_turn_count > len(prompt_ids)

    assigned = assign_prompt_ids(seq, prompt_ids, random_seed=42)
    assigned_ids = [t["prompt_id"] for t in assigned if t["turn_type"] == TURN_TYPE_BULK]

    assert len(assigned_ids) == bulk_turn_count
    assert set(assigned_ids) == set(prompt_ids)  # every id used at least once via reshuffling


def test_assign_prompt_ids_empty_bank_returns_sequence_unchanged():
    plan = _plan(probe_placement="end", probe_ratio=0.2, turns_per_series=10)
    seq = resolve_turn_sequence(plan)
    assigned = assign_prompt_ids(seq, [], random_seed=42)
    assert assigned == seq


# ---------------------------------------------------------------------------
# M21 — assign_probe_schedule: probe_id + measurement_session_index stamping
# ---------------------------------------------------------------------------

def test_assign_probe_schedule_stamps_battery_in_order_per_series():
    plan = _plan(series_count=2, turns_per_series=5, probe_ratio=0.4, probe_placement="end")
    seq = assign_probe_schedule(resolve_turn_sequence(plan), ["p06", "p07"])

    probe_turns = [t for t in seq if t["turn_type"] == TURN_TYPE_PROBE]
    assert [t["probe_id"] for t in probe_turns] == ["p06", "p07", "p06", "p07"]
    assert [t["measurement_session_index"] for t in probe_turns] == [1, 1, 2, 2]


def test_assign_probe_schedule_session_indices_are_contiguous_with_partial_battery():
    # 3 probe slots per series against a 2-probe battery: two sessions per
    # series (the second one partial), still gap-free across the experiment.
    plan = _plan(series_count=2, turns_per_series=5, probe_ratio=0.6, probe_placement="end")
    seq = assign_probe_schedule(resolve_turn_sequence(plan), ["p06", "p07"])

    probe_turns = [t for t in seq if t["turn_type"] == TURN_TYPE_PROBE]
    assert [t["probe_id"] for t in probe_turns] == ["p06", "p07", "p06", "p06", "p07", "p06"]
    sessions = [t["measurement_session_index"] for t in probe_turns]
    assert sessions == [1, 1, 2, 3, 3, 4]
    assert sorted(set(sessions)) == list(range(1, max(sessions) + 1))  # no gaps (§6.3)


def test_assign_probe_schedule_battery_larger_than_series_probe_count_runs_leading_subset():
    plan = _plan(series_count=2, turns_per_series=5, probe_ratio=0.2, probe_placement="end")
    seq = assign_probe_schedule(resolve_turn_sequence(plan), ["p06", "p07", "p08"])

    probe_turns = [t for t in seq if t["turn_type"] == TURN_TYPE_PROBE]
    # Every series runs the same leading subset — one session per series.
    assert [t["probe_id"] for t in probe_turns] == ["p06", "p06"]
    assert [t["measurement_session_index"] for t in probe_turns] == [1, 2]


def test_assign_probe_schedule_is_deterministic_and_does_not_mutate_input():
    plan = _plan(probe_placement="random")
    base = resolve_turn_sequence(plan)
    stamped_1 = assign_probe_schedule(base, ["p06", "p07"])
    stamped_2 = assign_probe_schedule(base, ["p06", "p07"])

    assert stamped_1 == stamped_2
    assert all("probe_id" not in t for t in base)  # input untouched


def test_assign_probe_schedule_leaves_bulk_turns_untouched():
    plan = _plan(series_count=1, turns_per_series=5, probe_ratio=0.4, probe_placement="end")
    seq = assign_probe_schedule(resolve_turn_sequence(plan), ["p06"])

    bulk_turns = [t for t in seq if t["turn_type"] == TURN_TYPE_BULK]
    assert bulk_turns and all("probe_id" not in t for t in bulk_turns)
    assert all("measurement_session_index" not in t for t in bulk_turns)


def test_assign_probe_schedule_empty_battery_returns_sequence_unchanged():
    plan = _plan()
    base = resolve_turn_sequence(plan)
    assert assign_probe_schedule(base, []) == base
