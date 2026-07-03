"""
Interactive elicitation flow — walks the user through every Run Plan field
with sensible defaults, then offers to save the result as a reusable plan
file. Used when the harness CLI is invoked without --plan.

input_fn is injectable so tests can script a sequence of answers instead of
reading from a real terminal.
"""

import json
import uuid
from pathlib import Path
from typing import Callable, Optional

from harness.schema import (
    VALID_CONDITIONS,
    VALID_PROBE_PLACEMENTS,
    ConceptualFields,
    DifficultyConfig,
    JudgeConfig,
    NotificationConfig,
    RunPlan,
)

InputFn = Callable[[str], str]


def _ask(input_fn: InputFn, prompt: str, default: str) -> str:
    raw = input_fn(f"{prompt} [{default}]: ").strip()
    return raw if raw else default


def _ask_int(input_fn: InputFn, prompt: str, default: int) -> int:
    raw = _ask(input_fn, prompt, str(default))
    return int(raw)


def _ask_float(input_fn: InputFn, prompt: str, default: float) -> float:
    raw = _ask(input_fn, prompt, str(default))
    return float(raw)


def _ask_bool(input_fn: InputFn, prompt: str, default: bool) -> bool:
    default_str = "y" if default else "n"
    raw = _ask(input_fn, f"{prompt} (y/n)", default_str).strip().lower()
    return raw in ("y", "yes", "true", "1")


def _ask_optional(input_fn: InputFn, prompt: str, default: Optional[str] = None) -> Optional[str]:
    shown_default = default if default is not None else "none"
    raw = input_fn(f"{prompt} [{shown_default}]: ").strip()
    if not raw and default is None:
        return None
    return raw or default


def _ask_csv(input_fn: InputFn, prompt: str, default: list) -> list:
    default_str = ",".join(str(x) for x in default)
    raw = _ask(input_fn, prompt, default_str)
    return [item.strip() for item in raw.split(",") if item.strip()]


def _ask_csv_int(input_fn: InputFn, prompt: str, default: list) -> list:
    return [int(x) for x in _ask_csv(input_fn, prompt, default)]


def _ask_kv_pairs(input_fn: InputFn, prompt: str) -> dict:
    """Parse 'key=value,key2=value2' into a dict[str, float]. Blank -> {}."""
    raw = _ask(input_fn, f"{prompt} (key=value,...; blank for none)", "")
    if not raw:
        return {}
    result = {}
    for pair in raw.split(","):
        pair = pair.strip()
        if not pair:
            continue
        key, _, value = pair.partition("=")
        result[key.strip()] = float(value.strip())
    return result


def _ask_error_policy(input_fn: InputFn, prompt: str) -> dict:
    """Parse 'key=hard_stop,key2=soft_continue' into a dict[str, str]. Blank -> {}."""
    raw = _ask(input_fn, f"{prompt} (key=hard_stop|soft_continue,...; blank for defaults)", "")
    if not raw:
        return {}
    result = {}
    for pair in raw.split(","):
        pair = pair.strip()
        if not pair:
            continue
        key, _, value = pair.partition("=")
        result[key.strip()] = value.strip()
    return result


def elicit_plan(input_fn: InputFn = input, print_fn=print) -> RunPlan:
    """Walk the user through every Run Plan field and return a validated RunPlan."""
    print_fn("=== Experiment Run Plan — interactive elicitation ===")
    print_fn("Press Enter to accept the bracketed default for any field.\n")

    plan_id = _ask(input_fn, "plan_id", f"plan-{uuid.uuid4().hex[:8]}")
    plan_name = _ask(input_fn, "plan_name", "Unnamed experiment")

    print_fn("\n--- conceptual_fields ---")
    cf_count = _ask_int(input_fn, "conceptual_fields.count", 4)
    subjects_raw = _ask(input_fn, "conceptual_fields.subjects (comma-separated, or 'auto')", "auto")
    subjects = subjects_raw if subjects_raw == "auto" else [
        s.strip() for s in subjects_raw.split(",") if s.strip()
    ]
    conceptual_fields = ConceptualFields(count=cf_count, subjects=subjects)

    print_fn("\n--- turn counts (provide at least two; the third is derived) ---")
    series_count = _ask_int(input_fn, "series_count", 5)
    turns_per_series = _ask_int(input_fn, "turns_per_series", 10)
    total_prompts = series_count * turns_per_series

    print_fn("\n--- probes ---")
    probe_ratio = _ask_float(input_fn, "probe_ratio (0-1)", 0.2)
    probe_placement = _ask(
        input_fn, f"probe_placement ({'/'.join(VALID_PROBE_PLACEMENTS)})", "end"
    )
    probe_checkpoint_positions = None
    if probe_placement == "fixed_checkpoints":
        probe_checkpoint_positions = _ask_csv_int(
            input_fn, "probe_checkpoint_positions (comma-separated turn indices)", [turns_per_series - 1]
        )

    print_fn("\n--- difficulty ---")
    hop_levels = _ask_csv_int(input_fn, "difficulty.hop_distance_levels", [0, 1])
    difficulty = DifficultyConfig(hop_distance_levels=hop_levels)

    print_fn("\n--- judge ---")
    judge_model = _ask(input_fn, "judge.model", "gemma4:e4b")
    judge_temperature = _ask_float(input_fn, "judge.temperature", 0.0)
    judge_snapshot = _ask_optional(input_fn, "judge.pinned_snapshot", None)
    judge = JudgeConfig(model=judge_model, temperature=judge_temperature, pinned_snapshot=judge_snapshot)

    print_fn("\n--- conditions ---")
    conditions = _ask_csv(input_fn, "conditions", list(VALID_CONDITIONS))
    condition_order_raw = _ask(input_fn, "condition_order (blank = same as conditions)", "")
    condition_order = (
        [c.strip() for c in condition_order_raw.split(",") if c.strip()]
        if condition_order_raw else None
    )

    print_fn("\n--- tuning knobs / error policy / notification ---")
    tuning_knobs = _ask_kv_pairs(input_fn, "tuning_knobs")
    error_policy = _ask_error_policy(input_fn, "error_policy overrides")
    notification_enabled = _ask_bool(input_fn, "notification.enabled", True)
    notification = NotificationConfig(enabled=notification_enabled)

    budget_cap_raw = _ask_optional(input_fn, "budget_cap (blank = none)", None)
    budget_cap = float(budget_cap_raw) if budget_cap_raw else None

    print_fn("\n--- execution ---")
    dry_run = _ask_bool(input_fn, "dry_run", False)
    dry_run_n = _ask_int(input_fn, "dry_run_n", 2)
    random_seed = _ask_int(input_fn, "random_seed", 42)

    plan = RunPlan(
        plan_id=plan_id,
        plan_name=plan_name,
        conceptual_fields=conceptual_fields,
        total_prompts=total_prompts,
        series_count=series_count,
        turns_per_series=turns_per_series,
        probe_ratio=probe_ratio,
        probe_placement=probe_placement,
        probe_checkpoint_positions=probe_checkpoint_positions,
        difficulty=difficulty,
        judge=judge,
        conditions=conditions,
        condition_order=condition_order,
        tuning_knobs=tuning_knobs,
        error_policy=error_policy,
        notification=notification,
        budget_cap=budget_cap,
        dry_run=dry_run,
        dry_run_n=dry_run_n,
        random_seed=random_seed,
    )

    print_fn("\n=== Plan collected and validated ===")
    return plan


def offer_to_save_plan(
    plan: RunPlan, input_fn: InputFn = input, print_fn=print
) -> Optional[Path]:
    """Ask whether to save the plan to a file; return the saved path, or None if declined."""
    should_save = _ask_bool(input_fn, "Save this plan to a file?", True)
    if not should_save:
        return None
    default_path = f"plans/{plan.plan_id}.json"
    path_str = _ask(input_fn, "Path to save plan", default_path)
    path = Path(path_str)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(plan.model_dump(mode="json"), indent=2), encoding="utf-8")
    print_fn(f"Plan saved to {path}")
    return path
