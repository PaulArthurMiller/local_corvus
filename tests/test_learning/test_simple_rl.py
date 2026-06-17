"""
Tests for learning/simple_rl.py — SimpleRL weight update algorithm.

Coverage:
- Thumbs up increases edge weights by weight_update_rate
- Thumbs down decreases edge weights by weight_update_rate
- Weights clamp at 1.0 (no overflow above max)
- Weights clamp at 0.0 (no underflow below min)
- Non-activated edges are unchanged (returned list only contains activated edges)
- Invalid feedback value produces empty result
- Activated MUs are accepted but not used (interface compatibility)
"""

import sys
import os

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "backend"))

import pytest
from learning.simple_rl import SimpleRL


def make_edge(edge_id: str, weight: float) -> dict:
    return {
        "edge_id": edge_id,
        "source_mu_id": "mu-source",
        "target_mu_id": "mu-target",
        "created_at": "2026-01-01T00:00:00+00:00",
        "weight": weight,
        "direction": "forward",
        "last_traversed_at": None,
        "traversal_count": 0,
    }


def make_mu(mu_id: str) -> dict:
    return {
        "mu_id": mu_id,
        "source_type": "user_input",
        "raw_content": "test content",
        "activation_count": 1,
    }


# ---------------------------------------------------------------------------
# Basic weight update
# ---------------------------------------------------------------------------


def test_thumbs_up_increases_weight():
    """Positive feedback moves edge weight up by weight_update_rate."""
    rl = SimpleRL(weight_update_rate=0.1)
    edges = [make_edge("e1", 0.5)]

    updates = rl.update(activated_edges=edges, activated_mus=[], feedback=1)

    print(f"updates: {updates}")
    assert len(updates) == 1
    edge_id, new_weight = updates[0]
    assert edge_id == "e1"
    assert abs(new_weight - 0.6) < 1e-9, f"expected 0.6, got {new_weight}"


def test_thumbs_down_decreases_weight():
    """Negative feedback moves edge weight down by weight_update_rate."""
    rl = SimpleRL(weight_update_rate=0.1)
    edges = [make_edge("e1", 0.5)]

    updates = rl.update(activated_edges=edges, activated_mus=[], feedback=-1)

    print(f"updates: {updates}")
    assert len(updates) == 1
    edge_id, new_weight = updates[0]
    assert edge_id == "e1"
    assert abs(new_weight - 0.4) < 1e-9, f"expected 0.4, got {new_weight}"


def test_multiple_edges_all_updated():
    """All activated edges receive the same weight delta."""
    rl = SimpleRL(weight_update_rate=0.1)
    edges = [
        make_edge("e1", 0.3),
        make_edge("e2", 0.5),
        make_edge("e3", 0.7),
    ]

    updates = rl.update(activated_edges=edges, activated_mus=[], feedback=1)
    print(f"updates: {updates}")

    assert len(updates) == 3
    result = {eid: w for eid, w in updates}
    assert abs(result["e1"] - 0.4) < 1e-9
    assert abs(result["e2"] - 0.6) < 1e-9
    assert abs(result["e3"] - 0.8) < 1e-9


# ---------------------------------------------------------------------------
# Clamping
# ---------------------------------------------------------------------------


def test_weight_clamps_at_1_0_on_thumbs_up():
    """Weight at 0.95 + 0.1 = 1.05 → clamped to 1.0."""
    rl = SimpleRL(weight_update_rate=0.1)
    edges = [make_edge("e1", 0.95)]

    updates = rl.update(activated_edges=edges, activated_mus=[], feedback=1)
    print(f"updates (clamp upper): {updates}")

    _, new_weight = updates[0]
    assert new_weight == 1.0, f"expected 1.0, got {new_weight}"


def test_weight_clamps_at_0_0_on_thumbs_down():
    """Weight at 0.05 - 0.1 = -0.05 → clamped to 0.0."""
    rl = SimpleRL(weight_update_rate=0.1)
    edges = [make_edge("e1", 0.05)]

    updates = rl.update(activated_edges=edges, activated_mus=[], feedback=-1)
    print(f"updates (clamp lower): {updates}")

    _, new_weight = updates[0]
    assert new_weight == 0.0, f"expected 0.0, got {new_weight}"


def test_weight_already_at_max_stays_at_max():
    """Weight at 1.0 with thumbs up stays at 1.0."""
    rl = SimpleRL(weight_update_rate=0.1)
    edges = [make_edge("e1", 1.0)]

    updates = rl.update(activated_edges=edges, activated_mus=[], feedback=1)
    _, new_weight = updates[0]
    assert new_weight == 1.0


def test_weight_already_at_min_stays_at_min():
    """Weight at 0.0 with thumbs down stays at 0.0."""
    rl = SimpleRL(weight_update_rate=0.1)
    edges = [make_edge("e1", 0.0)]

    updates = rl.update(activated_edges=edges, activated_mus=[], feedback=-1)
    _, new_weight = updates[0]
    assert new_weight == 0.0


# ---------------------------------------------------------------------------
# Empty / invalid inputs
# ---------------------------------------------------------------------------


def test_no_activated_edges_returns_empty():
    """No activated edges → no updates."""
    rl = SimpleRL(weight_update_rate=0.1)
    updates = rl.update(activated_edges=[], activated_mus=[], feedback=1)
    print(f"empty updates: {updates}")
    assert updates == []


def test_invalid_feedback_returns_empty():
    """feedback=0 is not +1 or -1; no update should be applied."""
    rl = SimpleRL(weight_update_rate=0.1)
    edges = [make_edge("e1", 0.5)]

    updates = rl.update(activated_edges=edges, activated_mus=[], feedback=0)
    print(f"invalid feedback updates: {updates}")
    assert updates == []


def test_activated_mus_accepted_but_unused():
    """Passing MUs does not change the weight calculation."""
    rl = SimpleRL(weight_update_rate=0.1)
    edges = [make_edge("e1", 0.5)]
    mus = [make_mu("mu1"), make_mu("mu2")]

    updates_with_mus = rl.update(activated_edges=edges, activated_mus=mus, feedback=1)
    updates_without_mus = rl.update(activated_edges=edges, activated_mus=[], feedback=1)

    print(f"with_mus: {updates_with_mus}, without_mus: {updates_without_mus}")
    assert updates_with_mus == updates_without_mus


# ---------------------------------------------------------------------------
# Configurable rate
# ---------------------------------------------------------------------------


def test_custom_weight_update_rate():
    """weight_update_rate is configurable; large rate applies correctly."""
    rl = SimpleRL(weight_update_rate=0.25)
    edges = [make_edge("e1", 0.5)]

    updates = rl.update(activated_edges=edges, activated_mus=[], feedback=1)
    _, new_weight = updates[0]
    print(f"custom rate new_weight: {new_weight}")
    assert abs(new_weight - 0.75) < 1e-9
