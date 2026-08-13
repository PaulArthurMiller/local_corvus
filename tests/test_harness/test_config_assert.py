"""Tests for scripts/harness/config_assert.py — plan.expected_config vs.
GET /admin/effective-config comparison (M43 Phase 1)."""

import os
import sys
from pathlib import Path

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "scripts"))

from harness.config_assert import (
    find_config_mismatches,
    find_dotted_key_collisions,
    format_config_mismatches,
    format_dotted_key_collisions,
    get_dotted,
)


# ---------------------------------------------------------------------------
# get_dotted
# ---------------------------------------------------------------------------

def test_get_dotted_resolves_nested_leaf():
    config = {"edge_update": {"seed_prior_alpha": 3.0}}
    assert get_dotted(config, "edge_update.seed_prior_alpha") == 3.0


def test_get_dotted_missing_top_level_key_returns_missing_sentinel():
    config = {"edge_update": {"seed_prior_alpha": 3.0}}
    result = get_dotted(config, "nonexistent.key")
    assert result != 3.0
    # Compare against a second independent lookup on a genuinely missing key
    # rather than importing the private sentinel — both must agree it's absent.
    assert get_dotted({}, "nonexistent.key") is result


def test_get_dotted_missing_leaf_key():
    config = {"edge_update": {"seed_prior_alpha": 3.0}}
    result = get_dotted(config, "edge_update.nonexistent_leaf")
    assert result is get_dotted({}, "anything.at.all")  # both resolve to the same MISSING sentinel


def test_get_dotted_path_runs_into_a_non_dict():
    config = {"edge_update": {"seed_prior_alpha": 3.0}}
    result = get_dotted(config, "edge_update.seed_prior_alpha.too_deep")
    assert result is get_dotted({}, "x")


# ---------------------------------------------------------------------------
# find_config_mismatches
# ---------------------------------------------------------------------------

def test_no_mismatches_when_expected_is_empty():
    assert find_config_mismatches({}, {"edge_update": {"seed_prior_alpha": 1.0}}) == []


def test_no_mismatches_when_every_declared_key_matches():
    expected = {"edge_update.seed_prior_alpha": 3.0, "edge_update.seed_prior_beta": 3.0}
    live_config = {"edge_update": {"seed_prior_alpha": 3.0, "seed_prior_beta": 3.0}}
    assert find_config_mismatches(expected, live_config) == []


def test_mismatch_reports_expected_and_actual():
    expected = {"edge_update.seed_prior_alpha": 3.0}
    live_config = {"edge_update": {"seed_prior_alpha": 1.0}}
    mismatches = find_config_mismatches(expected, live_config)
    assert len(mismatches) == 1
    assert "edge_update.seed_prior_alpha" in mismatches[0]
    assert "expected=3.0" in mismatches[0]
    assert "actual=1.0" in mismatches[0]


def test_missing_key_is_a_mismatch_not_a_skip():
    """A declared key absent from the live config must be reported, per
    BUILD-PLAN.md M43 Phase 1 build item 4 — not silently skipped."""
    expected = {"edge_update.seed_prior_alpha": 3.0}
    live_config = {"edge_update": {}}
    mismatches = find_config_mismatches(expected, live_config)
    assert len(mismatches) == 1
    assert "actual=<missing>" in mismatches[0]


def test_multiple_mismatches_all_reported():
    expected = {
        "hyperparameters.retrieval_level2_top_k": 2,
        "reporting.pass_rank_thresholds.spoke_retrieval": 5,
    }
    live_config = {
        "hyperparameters": {"retrieval_level2_top_k": 1},
        "reporting": {"pass_rank_thresholds": {"spoke_retrieval": 4}},
    }
    mismatches = find_config_mismatches(expected, live_config)
    assert len(mismatches) == 2


def test_format_config_mismatches_joins_with_semicolons():
    formatted = format_config_mismatches(["a: expected=1 actual=2", "b: expected=3 actual=4"])
    assert "a: expected=1 actual=2" in formatted
    assert "b: expected=3 actual=4" in formatted
    assert "expected_config mismatch" in formatted


# ---------------------------------------------------------------------------
# M43 open question, now closed — dotted-key collision detection
# ---------------------------------------------------------------------------

def test_no_collisions_in_a_normal_nested_config():
    config = {"edge_update": {"seed_prior_alpha": 3.0}, "reporting": {"thresholds": {"a": 1}}}
    assert find_dotted_key_collisions(config) == []


def test_top_level_key_containing_a_literal_dot_is_reported():
    config = {"edge_update.seed_prior_alpha": 3.0, "model": {"name": "gemma4:e4b"}}
    assert find_dotted_key_collisions(config) == ["edge_update.seed_prior_alpha"]


def test_nested_key_containing_a_literal_dot_reports_its_full_path():
    config = {"reporting": {"pass_rank.thresholds": {"spoke_retrieval": 4}}}
    assert find_dotted_key_collisions(config) == ["reporting.pass_rank.thresholds"]


def test_multiple_collisions_all_reported():
    config = {"a.b": 1, "c": {"d.e": 2}}
    assert sorted(find_dotted_key_collisions(config)) == ["a.b", "c.d.e"]


def test_real_config_yaml_has_no_dotted_keys():
    """The premise the whole dotted-path scheme rests on, asserted against
    the committed config.yaml rather than assumed — if someone ever adds a
    key with a dot in it, this fails here instead of silently resolving the
    wrong leaf at run time."""
    import yaml
    config_path = Path(__file__).resolve().parents[2] / "config.yaml"
    config = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    assert find_dotted_key_collisions(config) == []


def test_format_dotted_key_collisions_names_every_offender():
    formatted = format_dotted_key_collisions(["a.b", "c.d.e"])
    assert "'a.b'" in formatted
    assert "'c.d.e'" in formatted
    assert "literal '.'" in formatted
