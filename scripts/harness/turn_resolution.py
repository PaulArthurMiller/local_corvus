"""
Deterministic turn-type (probe / bulk) resolution.

Regardless of probe_placement strategy, every turn's type must be resolved
before any turn executes, and the same (plan, random_seed) must always
produce the same resolved sequence. Probe turns are never eligible for
feedback/weight updates — callers must check turn_type before submitting
feedback.
"""

import random

from harness.schema import RunPlan

TURN_TYPE_PROBE = "probe"
TURN_TYPE_BULK = "bulk"


def _clamp(value: int, low: int, high: int) -> int:
    return max(low, min(high, value))


def _resolve_series_turn_types(
    turns_per_series: int,
    n_probes: int,
    placement: str,
    checkpoint_positions: list,
    rng: random.Random,
) -> list:
    """Resolve one series' turn-type list ('bulk' by default, 'probe' at chosen slots)."""
    types = [TURN_TYPE_BULK] * turns_per_series

    if placement == "end":
        for i in range(turns_per_series - n_probes, turns_per_series):
            types[i] = TURN_TYPE_PROBE
    elif placement == "fixed_checkpoints":
        for pos in checkpoint_positions:
            if 0 <= pos < turns_per_series:
                types[pos] = TURN_TYPE_PROBE
    elif placement == "random":
        indices = list(range(turns_per_series))
        rng.shuffle(indices)
        for idx in indices[:n_probes]:
            types[idx] = TURN_TYPE_PROBE
    else:
        raise ValueError(f"unknown probe_placement {placement!r}")

    return types


def resolve_turn_sequence(plan: RunPlan) -> list:
    """
    Resolve the full (series_index, turn_index, turn_type) sequence for a plan.

    Deterministic given (plan, plan.random_seed): the same inputs always
    produce the same output, which is what allows the sequence to be
    persisted in the run manifest and audited after the fact.

    For probe_placement == "random", a single seeded RNG is shared across
    all series (advanced in series order) so different series get distinct
    but reproducible probe placements.

    Returns a flat list of dicts: {"series_index", "turn_index", "turn_type"}.
    """
    if plan.series_count is None or plan.turns_per_series is None:
        raise ValueError(
            "plan.series_count and plan.turns_per_series must be resolved "
            "(via schema validation) before turn-type resolution"
        )

    n_series = plan.series_count
    n_turns = plan.turns_per_series
    n_probes = _clamp(round(n_turns * plan.probe_ratio), 0, n_turns)
    checkpoint_positions = sorted(set(plan.probe_checkpoint_positions or []))
    rng = random.Random(plan.random_seed)

    sequence = []
    for series_index in range(n_series):
        types = _resolve_series_turn_types(
            turns_per_series=n_turns,
            n_probes=n_probes,
            placement=plan.probe_placement,
            checkpoint_positions=checkpoint_positions,
            rng=rng,
        )
        for turn_index, turn_type in enumerate(types):
            sequence.append({
                "series_index": series_index,
                "turn_index": turn_index,
                "turn_type": turn_type,
            })

    return sequence
