"""Tests for scripts/harness/elicit.py — scripted interactive elicitation."""

import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "scripts"))

from harness.elicit import elicit_plan, offer_to_save_plan


class ScriptedInput:
    """Feeds a fixed sequence of answers, like a scripted terminal session."""

    def __init__(self, answers):
        self._answers = list(answers)

    def __call__(self, prompt: str) -> str:
        if not self._answers:
            raise AssertionError(f"ScriptedInput exhausted; unexpected prompt: {prompt!r}")
        return self._answers.pop(0)


# Answers in the exact order elicit_plan() asks, using blank ("") to accept
# defaults except where we want to confirm a specific override is honored.
_ANSWERS = [
    "my-plan",          # plan_id
    "My Test Experiment",  # plan_name
    "3",                # conceptual_fields.count
    "auto",             # conceptual_fields.subjects
    "4",                # series_count
    "8",                # turns_per_series
    "0.25",             # probe_ratio
    "end",              # probe_placement
    "0,1",              # difficulty.hop_distance_levels
    "gemma4:e4b",       # judge.model
    "0.0",              # judge.temperature
    "",                 # judge.pinned_snapshot
    "learned,static_weight",  # conditions
    "",                 # condition_order
    "",                 # tuning_knobs
    "",                 # error_policy overrides
    "n",                # notification.enabled
    "",                 # budget_cap
    "",                 # prompt_bank_path
    "",                 # prompt_gen.model
    "",                 # prompt_gen.max_retries
    "y",                # dry_run
    "3",                # dry_run_n
    "7",                # random_seed
]


def test_elicit_plan_produces_validated_plan_matching_scripted_answers():
    input_fn = ScriptedInput(_ANSWERS)
    plan = elicit_plan(input_fn=input_fn, print_fn=lambda *_a, **_k: None)

    assert plan.plan_id == "my-plan"
    assert plan.plan_name == "My Test Experiment"
    assert plan.conceptual_fields.count == 3
    assert plan.conceptual_fields.subjects == "auto"
    assert plan.series_count == 4
    assert plan.turns_per_series == 8
    assert plan.total_prompts == 32
    assert plan.probe_ratio == 0.25
    assert plan.probe_placement == "end"
    assert plan.difficulty.hop_distance_levels == [0, 1]
    assert plan.judge.model == "gemma4:e4b"
    assert plan.judge.pinned_snapshot is None
    assert plan.conditions == ["learned", "static_weight"]
    assert plan.condition_order == ["learned", "static_weight"]
    assert plan.notification.enabled is False
    assert plan.budget_cap is None
    assert plan.prompt_bank_path is None
    assert plan.prompt_gen.model == "claude-sonnet-5"
    assert plan.prompt_gen.max_retries == 2
    assert plan.dry_run is True
    assert plan.dry_run_n == 3
    assert plan.random_seed == 7


def test_offer_to_save_plan_writes_file_matching_entered_plan(tmp_path):
    input_fn = ScriptedInput(_ANSWERS)
    plan = elicit_plan(input_fn=input_fn, print_fn=lambda *_a, **_k: None)

    save_path = tmp_path / "saved_plan.json"
    save_input = ScriptedInput(["y", str(save_path)])
    result_path = offer_to_save_plan(plan, input_fn=save_input, print_fn=lambda *_a, **_k: None)

    assert result_path == save_path
    saved = json.loads(save_path.read_text(encoding="utf-8"))
    assert saved["plan_id"] == "my-plan"
    assert saved["series_count"] == 4
    assert saved["turns_per_series"] == 8
    assert saved["dry_run_n"] == 3
    assert saved["random_seed"] == 7


def test_offer_to_save_plan_declines_when_answered_no(tmp_path):
    input_fn = ScriptedInput(_ANSWERS)
    plan = elicit_plan(input_fn=input_fn, print_fn=lambda *_a, **_k: None)

    decline_input = ScriptedInput(["n"])
    result_path = offer_to_save_plan(plan, input_fn=decline_input, print_fn=lambda *_a, **_k: None)
    assert result_path is None
