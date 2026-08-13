"""Tests for core/config_overrides.py — CONFIG_OVERRIDES_JSON parse/merge (M43 Phase 2)."""

import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "backend"))

from core.config_overrides import (
    ConfigOverrideError,
    apply_config_overrides,
    load_config_overrides_from_env,
    parse_config_overrides_json,
)


# ---------------------------------------------------------------------------
# parse_config_overrides_json
# ---------------------------------------------------------------------------

def test_empty_string_parses_to_empty_dict():
    assert parse_config_overrides_json("") == {}


def test_whitespace_only_parses_to_empty_dict():
    assert parse_config_overrides_json("   ") == {}


def test_valid_json_object_parses():
    result = parse_config_overrides_json('{"edge_update.seed_prior_alpha": 3.0}')
    assert result == {"edge_update.seed_prior_alpha": 3.0}


def test_malformed_json_raises_config_override_error():
    with pytest.raises(ConfigOverrideError, match="not valid JSON"):
        parse_config_overrides_json("{not valid json")


def test_non_object_json_raises_config_override_error():
    with pytest.raises(ConfigOverrideError, match="must be a JSON object"):
        parse_config_overrides_json("[1, 2, 3]")


# ---------------------------------------------------------------------------
# apply_config_overrides
# ---------------------------------------------------------------------------

def test_apply_overrides_sets_nested_leaf():
    config = {"edge_update": {"seed_prior_alpha": 1.0, "seed_prior_beta": 1.0}}
    merged = apply_config_overrides(config, {"edge_update.seed_prior_alpha": 3.0})
    assert merged["edge_update"]["seed_prior_alpha"] == 3.0
    assert merged["edge_update"]["seed_prior_beta"] == 1.0  # untouched


def test_apply_overrides_does_not_mutate_input():
    config = {"edge_update": {"seed_prior_alpha": 1.0}}
    apply_config_overrides(config, {"edge_update.seed_prior_alpha": 3.0})
    assert config["edge_update"]["seed_prior_alpha"] == 1.0


def test_apply_empty_overrides_is_a_noop():
    config = {"edge_update": {"seed_prior_alpha": 1.0}}
    merged = apply_config_overrides(config, {})
    assert merged == config


def test_apply_overrides_unknown_top_level_key_raises():
    config = {"edge_update": {"seed_prior_alpha": 1.0}}
    with pytest.raises(ConfigOverrideError, match="does not resolve to an existing config.yaml leaf"):
        apply_config_overrides(config, {"nonexistent_section.some_key": 1.0})


def test_apply_overrides_unknown_leaf_key_raises():
    config = {"edge_update": {"seed_prior_alpha": 1.0}}
    with pytest.raises(ConfigOverrideError, match="does not resolve to an existing config.yaml leaf"):
        apply_config_overrides(config, {"edge_update.nonexistent_leaf": 1.0})


def test_apply_overrides_multiple_keys_across_sections():
    config = {
        "hyperparameters": {"retrieval_level2_top_k": 1},
        "reporting": {"pass_rank_thresholds": {"spoke_retrieval": 4}},
    }
    merged = apply_config_overrides(config, {
        "hyperparameters.retrieval_level2_top_k": 2,
        "reporting.pass_rank_thresholds.spoke_retrieval": 5,
    })
    assert merged["hyperparameters"]["retrieval_level2_top_k"] == 2
    assert merged["reporting"]["pass_rank_thresholds"]["spoke_retrieval"] == 5


# ---------------------------------------------------------------------------
# load_config_overrides_from_env
# ---------------------------------------------------------------------------

def test_load_from_env_missing_var_returns_empty(monkeypatch):
    monkeypatch.delenv("CONFIG_OVERRIDES_JSON", raising=False)
    assert load_config_overrides_from_env() == {}


def test_load_from_env_reads_set_var(monkeypatch):
    monkeypatch.setenv("CONFIG_OVERRIDES_JSON", '{"model.timeout_seconds": 90}')
    assert load_config_overrides_from_env() == {"model.timeout_seconds": 90}


def test_load_from_env_malformed_var_raises(monkeypatch):
    monkeypatch.setenv("CONFIG_OVERRIDES_JSON", "not json")
    with pytest.raises(ConfigOverrideError):
        load_config_overrides_from_env()
