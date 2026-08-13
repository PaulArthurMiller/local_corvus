"""Tests for scripts/harness/schema.py — Run Plan validation."""

import os
import sys

import pytest
from pydantic import ValidationError

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "scripts"))

from harness.schema import RunPlan, format_validation_error


def _base_kwargs(**overrides):
    kwargs = dict(
        plan_id="p1",
        plan_name="Test plan",
        conceptual_fields={"count": 4, "subjects": "auto"},
        series_count=5,
        turns_per_series=10,
    )
    kwargs.update(overrides)
    return kwargs


def test_valid_plan_loads_and_derives_total_prompts():
    plan = RunPlan(**_base_kwargs())
    assert plan.total_prompts == 50
    assert plan.condition_order == plan.conditions


def test_valid_plan_with_all_three_turn_counts_consistent():
    plan = RunPlan(**_base_kwargs(total_prompts=50))
    assert plan.total_prompts == 50


def test_inconsistent_turn_counts_raises_field_identifying_error():
    with pytest.raises(ValidationError) as exc_info:
        RunPlan(**_base_kwargs(total_prompts=999))
    message = format_validation_error(exc_info.value)
    assert "total_prompts" in message


def test_only_one_turn_count_field_raises_error():
    with pytest.raises(ValidationError) as exc_info:
        RunPlan(**_base_kwargs(series_count=5, turns_per_series=None))
    message = format_validation_error(exc_info.value)
    assert "total_prompts" in message or "turns_per_series" in message


def test_invalid_condition_raises_field_identifying_error():
    with pytest.raises(ValidationError) as exc_info:
        RunPlan(**_base_kwargs(conditions=["bogus_condition"]))
    message = format_validation_error(exc_info.value)
    assert "conditions" in message


def test_condition_order_not_a_permutation_raises_error():
    with pytest.raises(ValidationError) as exc_info:
        RunPlan(**_base_kwargs(
            conditions=["learned", "static_weight"],
            condition_order=["learned"],
        ))
    message = format_validation_error(exc_info.value)
    assert "condition_order" in message


def test_invalid_probe_placement_raises_error():
    with pytest.raises(ValidationError) as exc_info:
        RunPlan(**_base_kwargs(probe_placement="sometimes"))
    message = format_validation_error(exc_info.value)
    assert "probe_placement" in message


def test_fixed_checkpoints_without_positions_raises_error():
    with pytest.raises(ValidationError) as exc_info:
        RunPlan(**_base_kwargs(probe_placement="fixed_checkpoints"))
    message = format_validation_error(exc_info.value)
    assert "probe_checkpoint_positions" in message


def test_fixed_checkpoints_out_of_range_position_raises_error():
    with pytest.raises(ValidationError) as exc_info:
        RunPlan(**_base_kwargs(
            probe_placement="fixed_checkpoints",
            probe_checkpoint_positions=[999],
        ))
    message = format_validation_error(exc_info.value)
    assert "probe_checkpoint_positions" in message


def test_invalid_subjects_string_raises_error():
    with pytest.raises(ValidationError) as exc_info:
        RunPlan(**_base_kwargs(conceptual_fields={"count": 4, "subjects": "not_auto"}))
    message = format_validation_error(exc_info.value)
    assert "subjects" in message


def test_probe_ratio_out_of_range_raises_error():
    with pytest.raises(ValidationError) as exc_info:
        RunPlan(**_base_kwargs(probe_ratio=1.5))
    message = format_validation_error(exc_info.value)
    assert "probe_ratio" in message


def test_invalid_error_policy_action_raises_error():
    with pytest.raises(ValidationError) as exc_info:
        RunPlan(**_base_kwargs(error_policy={"connectivity": "shrug"}))
    message = format_validation_error(exc_info.value)
    assert "error_policy" in message


def test_error_policy_valid_override_accepted():
    plan = RunPlan(**_base_kwargs(error_policy={"turn_ambiguous": "hard_stop"}))
    assert plan.error_policy == {"turn_ambiguous": "hard_stop"}


def test_format_validation_error_is_not_a_raw_stack_trace():
    with pytest.raises(ValidationError) as exc_info:
        RunPlan(**_base_kwargs(conditions=[]))
    message = format_validation_error(exc_info.value)
    assert "Traceback" not in message
    assert "conditions" in message


# ---------------------------------------------------------------------------
# M19 Phase 2 — judge.temperature tightened to <= 0.1 (reproducibility)
# ---------------------------------------------------------------------------

def test_judge_temperature_above_0_1_raises_field_identifying_error():
    with pytest.raises(ValidationError) as exc_info:
        RunPlan(**_base_kwargs(judge={"model": "gemma4:e4b", "temperature": 0.5}))
    message = format_validation_error(exc_info.value)
    assert "temperature" in message


def test_judge_temperature_at_0_1_is_accepted():
    plan = RunPlan(**_base_kwargs(judge={"model": "gemma4:e4b", "temperature": 0.1}))
    assert plan.judge.temperature == 0.1


def test_judge_temperature_default_is_accepted():
    plan = RunPlan(**_base_kwargs())
    assert plan.judge.temperature == 0.0


# ---------------------------------------------------------------------------
# M19 Phase 2 — judge_unparseable error policy key
# ---------------------------------------------------------------------------

def test_judge_unparseable_defaults_to_record_none():
    """
    Changed from soft_continue on 2026-08-06. A dropped verdict is invisible
    in the data — indistinguishable from a turn that never ran — and the
    calibration pass that day lost one turn in twenty this way, which is ~50
    turns across exp0. Recording "none" costs nothing: it triggers no weight
    update, exactly like recording nothing did.
    """
    from harness.schema import DEFAULT_ERROR_POLICY
    assert DEFAULT_ERROR_POLICY["judge_unparseable"] == "record_none"


def test_judge_unparseable_error_policy_override_accepted():
    plan = RunPlan(**_base_kwargs(error_policy={"judge_unparseable": "hard_stop"}))
    assert plan.error_policy == {"judge_unparseable": "hard_stop"}


def test_record_none_is_accepted_for_judge_unparseable_only():
    """
    record_none is meaningless for the other keys — there is no verdict to
    record when the backend is unreachable — so a plan asking for it should
    fail validation rather than be silently ignored at run time.
    """
    plan = RunPlan(**_base_kwargs(error_policy={"judge_unparseable": "record_none"}))
    assert plan.error_policy == {"judge_unparseable": "record_none"}

    with pytest.raises(ValidationError) as exc_info:
        RunPlan(**_base_kwargs(error_policy={"connectivity": "record_none"}))
    assert "error_policy" in format_validation_error(exc_info.value)


# ---------------------------------------------------------------------------
# M19 Phase 3 — notification channel + report_generation error policy key
# ---------------------------------------------------------------------------

def test_report_generation_defaults_to_soft_continue():
    from harness.schema import DEFAULT_ERROR_POLICY
    assert DEFAULT_ERROR_POLICY["report_generation"] == "soft_continue"


def test_report_generation_error_policy_override_accepted():
    plan = RunPlan(**_base_kwargs(error_policy={"report_generation": "hard_stop"}))
    assert plan.error_policy == {"report_generation": "hard_stop"}


def test_notification_channel_defaults_to_log():
    plan = RunPlan(**_base_kwargs())
    assert plan.notification.channel == "log"


def test_notification_channel_accepts_email_and_webhook():
    plan_email = RunPlan(**_base_kwargs(notification={"channel": "email"}))
    plan_webhook = RunPlan(**_base_kwargs(notification={"channel": "webhook"}))
    assert plan_email.notification.channel == "email"
    assert plan_webhook.notification.channel == "webhook"


def test_invalid_notification_channel_raises_field_identifying_error():
    with pytest.raises(ValidationError) as exc_info:
        RunPlan(**_base_kwargs(notification={"channel": "carrier_pigeon"}))
    message = format_validation_error(exc_info.value)
    assert "channel" in message
    assert "carrier_pigeon" in message


# ---------------------------------------------------------------------------
# M21 — probe_set_version
# ---------------------------------------------------------------------------

def test_probe_set_version_defaults_to_v2():
    plan = RunPlan(**_base_kwargs())
    assert plan.probe_set_version == "v2"


def test_probe_set_version_accepts_explicit_value():
    plan = RunPlan(**_base_kwargs(probe_set_version="v3"))
    assert plan.probe_set_version == "v3"


# ---------------------------------------------------------------------------
# M28 — seed_fixture_version
# ---------------------------------------------------------------------------

def test_seed_fixture_version_defaults_to_v2():
    plan = RunPlan(**_base_kwargs())
    assert plan.seed_fixture_version == "v2"


def test_seed_fixture_version_accepts_explicit_value():
    plan = RunPlan(**_base_kwargs(seed_fixture_version="v3"))
    assert plan.seed_fixture_version == "v3"


# ---------------------------------------------------------------------------
# M29 — trained_spokes
# ---------------------------------------------------------------------------

def test_trained_spokes_defaults_to_spoke1_per_cluster():
    plan = RunPlan(**_base_kwargs())
    assert plan.trained_spokes == {
        "cluster_a": "a_spoke1",
        "cluster_b": "b_spoke1",
        "cluster_c": "c_spoke1",
        "cluster_d": "d_spoke1",
    }


def test_trained_spokes_accepts_explicit_override():
    override = {
        "cluster_a": "a_spoke3",
        "cluster_b": "b_spoke5",
        "cluster_c": "c_spoke1",
        "cluster_d": "d_spoke7",
    }
    plan = RunPlan(**_base_kwargs(trained_spokes=override))
    assert plan.trained_spokes == override


def test_trained_spokes_missing_cluster_key_raises_field_identifying_error():
    with pytest.raises(ValidationError) as exc_info:
        RunPlan(**_base_kwargs(trained_spokes={
            "cluster_a": "a_spoke1",
            "cluster_b": "b_spoke1",
            "cluster_c": "c_spoke1",
            # cluster_d missing
        }))
    message = format_validation_error(exc_info.value)
    assert "trained_spokes" in message


def test_trained_spokes_extra_cluster_key_raises_field_identifying_error():
    with pytest.raises(ValidationError) as exc_info:
        RunPlan(**_base_kwargs(trained_spokes={
            "cluster_a": "a_spoke1",
            "cluster_b": "b_spoke1",
            "cluster_c": "c_spoke1",
            "cluster_d": "d_spoke1",
            "cluster_e": "e_spoke1",
        }))
    message = format_validation_error(exc_info.value)
    assert "trained_spokes" in message


def test_trained_spokes_malformed_spoke_key_raises_field_identifying_error():
    with pytest.raises(ValidationError) as exc_info:
        RunPlan(**_base_kwargs(trained_spokes={
            "cluster_a": "a_spoke1",
            "cluster_b": "b_spoke9",  # out of range — spokes are 1-7
            "cluster_c": "c_spoke1",
            "cluster_d": "d_spoke1",
        }))
    message = format_validation_error(exc_info.value)
    assert "trained_spokes" in message


def test_trained_spokes_wrong_cluster_id_key_raises_field_identifying_error():
    with pytest.raises(ValidationError) as exc_info:
        RunPlan(**_base_kwargs(trained_spokes={
            "cluster_a": "a_spoke1",
            "cluster_b": "b_spoke1",
            "cluster_c": "c_spoke1",
            "cluster_e": "d_spoke1",  # not a real cluster id
        }))
    message = format_validation_error(exc_info.value)
    assert "trained_spokes" in message


def test_trained_spokes_malformed_value_shape_raises_field_identifying_error():
    with pytest.raises(ValidationError) as exc_info:
        RunPlan(**_base_kwargs(trained_spokes={
            "cluster_a": "spoke1",  # missing the letter prefix
            "cluster_b": "b_spoke1",
            "cluster_c": "c_spoke1",
            "cluster_d": "d_spoke1",
        }))
    message = format_validation_error(exc_info.value)
    assert "trained_spokes" in message


# ---------------------------------------------------------------------------
# M43 Phase 1/2 — expected_config / config_overrides
# ---------------------------------------------------------------------------

def test_expected_config_defaults_to_empty_dict():
    plan = RunPlan(**_base_kwargs())
    assert plan.expected_config == {}


def test_config_overrides_defaults_to_empty_dict():
    plan = RunPlan(**_base_kwargs())
    assert plan.config_overrides == {}


def test_expected_config_accepts_arbitrary_dotted_keys():
    """Shape-validated only — schema.py cannot know which config.yaml keys
    exist without importing backend modules (same convention as
    seed_fixture_version/trained_spokes); a bad key fails loudly at
    preflight time, not at schema-validation time."""
    plan = RunPlan(**_base_kwargs(expected_config={
        "edge_update.seed_prior_alpha": 3.0,
        "edge_update.seed_prior_beta": 3.0,
    }))
    assert plan.expected_config == {
        "edge_update.seed_prior_alpha": 3.0,
        "edge_update.seed_prior_beta": 3.0,
    }


def test_config_overrides_accepts_arbitrary_dotted_keys():
    plan = RunPlan(**_base_kwargs(config_overrides={
        "hyperparameters.retrieval_level2_top_k": 2,
    }))
    assert plan.config_overrides == {"hyperparameters.retrieval_level2_top_k": 2}


def test_expected_config_and_config_overrides_participate_in_plan_config_hash():
    """M43's whole point: declaring config state changes the plan's
    identity — a plan that declares its config *is* a different plan."""
    from harness.manifest import compute_config_hash

    base = RunPlan(**_base_kwargs())
    with_expected = RunPlan(**_base_kwargs(expected_config={"model.timeout_seconds": 90}))
    with_override = RunPlan(**_base_kwargs(config_overrides={"model.timeout_seconds": 90}))

    assert compute_config_hash(base) != compute_config_hash(with_expected)
    assert compute_config_hash(base) != compute_config_hash(with_override)
    assert compute_config_hash(with_expected) != compute_config_hash(with_override)
