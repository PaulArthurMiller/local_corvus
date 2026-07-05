"""
Experiment Run Plan schema (M19 Phase 0).

The harness executes plans; it does not design them. This module defines
the full validated schema for a run plan, including fields not yet acted
on by Phase 0 (judge pinning, tuning knobs, notification) so later phases
can build on a stable, already-validated shape.

Validation failures must be specific and field-identifying — never a raw
stack trace. format_validation_error() turns a pydantic ValidationError
into a human-readable, field-by-field message for CLI output.
"""

from typing import Optional, Union

from pydantic import BaseModel, Field, ValidationError, field_validator, model_validator

VALID_CONDITIONS = ("learned", "static_weight", "random_weight")
VALID_PROBE_PLACEMENTS = ("random", "end", "fixed_checkpoints")

# Error-handling policy keys understood by the harness in Phase 0.
# "connectivity" covers backend-unreachable / DB-failure conditions.
# "turn_ambiguous" covers a single per-turn result that is malformed or missing.
# "judge_unparseable" (M19 Phase 2) covers a judge response that fails to
# parse into a valid verdict even after one re-ask.
# "report_generation" (M19 Phase 3) covers a series/experiment report call
# failing — defaults to soft_continue regardless of the "connectivity"
# default, since a broken report is re-generatable later but an aborted
# run (that otherwise succeeded) is not.
DEFAULT_ERROR_POLICY = {
    "connectivity": "hard_stop",
    "turn_ambiguous": "soft_continue",
    "judge_unparseable": "soft_continue",
    "report_generation": "soft_continue",
}
VALID_ERROR_ACTIONS = ("hard_stop", "soft_continue")


class ConceptualFields(BaseModel):
    """Conceptual field count + subject list. 'auto' subject generation is Phase 1."""

    count: int = Field(gt=0, description="Number of conceptual fields (clusters) to draw prompts from")
    subjects: Union[list[str], str] = Field(
        default="auto",
        description="Explicit subject list, or the literal string 'auto' (Phase 1 generates it)",
    )

    @field_validator("subjects")
    @classmethod
    def _validate_subjects(cls, v: Union[list[str], str]) -> Union[list[str], str]:
        if isinstance(v, str) and v != "auto":
            raise ValueError("conceptual_fields.subjects must be the literal string 'auto' or a list of strings")
        if isinstance(v, list) and not v:
            raise ValueError("conceptual_fields.subjects list must not be empty (use 'auto' instead)")
        return v


class DifficultyConfig(BaseModel):
    """Hop-distance difficulty levels — the only difficulty mechanism in scope for M19."""

    hop_distance_levels: list[int] = Field(default_factory=lambda: [0, 1])

    @field_validator("hop_distance_levels")
    @classmethod
    def _validate_levels(cls, v: list[int]) -> list[int]:
        if not v:
            raise ValueError("difficulty.hop_distance_levels must not be empty")
        if any(level < 0 for level in v):
            raise ValueError("difficulty.hop_distance_levels values must all be >= 0")
        return v


class JudgeConfig(BaseModel):
    """Judge identity pinning + calls (M19 Phase 2 — see harness/judge.py)."""

    model: str = Field(default="gemma4:e4b", description="Judge model identity, config-swappable")
    # Tightened for M19 Phase 2: judge calls must be near-deterministic for
    # cross-condition verdict comparability to be meaningful — reproducibility
    # requires near-0, not merely "low", temperature.
    temperature: float = Field(
        default=0.0, ge=0.0, le=0.1,
        description="Must be <= 0.1 — judge calls need near-0 temperature for reproducibility",
    )
    pinned_snapshot: Optional[str] = Field(
        default=None, description="Optional pinned model snapshot/digest for reproducibility"
    )


VALID_NOTIFICATION_CHANNELS = ("log", "email", "webhook")


class NotificationConfig(BaseModel):
    enabled: bool = True
    # M19 Phase 3 — "email"/"webhook" are validated here but not yet
    # implemented; notify.py falls back to "log" with a logged warning so
    # wiring a real channel later is one new handler function, no refactor.
    channel: str = "log"

    @field_validator("channel")
    @classmethod
    def _validate_channel(cls, v: str) -> str:
        if v not in VALID_NOTIFICATION_CHANNELS:
            raise ValueError(
                f"notification.channel must be one of {VALID_NOTIFICATION_CHANNELS}, got {v!r}"
            )
        return v


class PromptGenConfig(BaseModel):
    """Sonnet-based prompt generation config (M19 Phase 1). Manual
    conceptual_fields.subjects strings (when provided) are passed into the
    generation prompt as topic constraints."""

    model: str = "claude-sonnet-5"
    max_retries: int = Field(default=2, ge=0)


class RunPlan(BaseModel):
    """
    Full Experiment Run Plan schema.

    total_prompts / series_count / turns_per_series: exactly two of these
    three must be supplied; the third is derived and cross-checked for
    consistency if all three are given.
    """

    plan_id: str = Field(min_length=1)
    plan_name: str = Field(min_length=1)

    conceptual_fields: ConceptualFields

    total_prompts: Optional[int] = Field(default=None, gt=0)
    series_count: Optional[int] = Field(default=None, gt=0)
    turns_per_series: Optional[int] = Field(default=None, gt=0)

    probe_ratio: float = Field(default=0.2, ge=0.0, le=1.0)
    probe_placement: str = Field(default="end")
    probe_checkpoint_positions: Optional[list[int]] = None

    difficulty: DifficultyConfig = Field(default_factory=DifficultyConfig)
    judge: JudgeConfig = Field(default_factory=JudgeConfig)

    conditions: list[str] = Field(default_factory=lambda: list(VALID_CONDITIONS))
    condition_order: Optional[list[str]] = None

    tuning_knobs: dict[str, float] = Field(default_factory=dict)

    # Overrides on top of DEFAULT_ERROR_POLICY. Not enumerated in the original
    # architecture note's field list, but required by §3.2's error-handling
    # policy requirement ("must be overridable from the run plan, not
    # hardcoded") — added as a minor, documented deviation.
    error_policy: dict[str, str] = Field(default_factory=dict)

    notification: NotificationConfig = Field(default_factory=NotificationConfig)

    budget_cap: Optional[float] = Field(default=None, ge=0.0)

    # M19 Phase 1 — path to an existing prompt bank. If absent and
    # conceptual_fields.subjects == "auto", the harness generates and saves
    # one before executing (with the cost-confirmation gate in prompt_gen.py).
    prompt_bank_path: Optional[str] = None
    prompt_gen: PromptGenConfig = Field(default_factory=PromptGenConfig)

    dry_run: bool = False
    dry_run_n: int = Field(default=2, gt=0)

    random_seed: int = 0

    @field_validator("probe_placement")
    @classmethod
    def _validate_placement(cls, v: str) -> str:
        if v not in VALID_PROBE_PLACEMENTS:
            raise ValueError(
                f"probe_placement must be one of {VALID_PROBE_PLACEMENTS}, got {v!r}"
            )
        return v

    @field_validator("conditions")
    @classmethod
    def _validate_conditions(cls, v: list[str]) -> list[str]:
        if not v:
            raise ValueError("conditions must not be empty")
        bad = [c for c in v if c not in VALID_CONDITIONS]
        if bad:
            raise ValueError(
                f"conditions contains invalid value(s) {bad}; must be from {VALID_CONDITIONS}"
            )
        return v

    @field_validator("error_policy")
    @classmethod
    def _validate_error_policy(cls, v: dict[str, str]) -> dict[str, str]:
        bad_actions = {k: a for k, a in v.items() if a not in VALID_ERROR_ACTIONS}
        if bad_actions:
            raise ValueError(
                f"error_policy has invalid action(s) {bad_actions}; "
                f"each value must be one of {VALID_ERROR_ACTIONS}"
            )
        return v

    @model_validator(mode="after")
    def _validate_plan(self) -> "RunPlan":
        # condition_order must be a permutation of conditions
        if self.condition_order is None:
            self.condition_order = list(self.conditions)
        elif sorted(self.condition_order) != sorted(self.conditions):
            raise ValueError(
                f"condition_order {self.condition_order} must be a permutation of "
                f"conditions {self.conditions}"
            )

        # --- Derive/cross-check total_prompts / series_count / turns_per_series ---
        given = [
            x for x in (self.total_prompts, self.series_count, self.turns_per_series)
            if x is not None
        ]
        if len(given) < 2:
            raise ValueError(
                "at least two of total_prompts, series_count, turns_per_series must be "
                "provided so the third can be derived"
            )

        if self.series_count is not None and self.turns_per_series is not None:
            derived_total = self.series_count * self.turns_per_series
            if self.total_prompts is None:
                self.total_prompts = derived_total
            elif self.total_prompts != derived_total:
                raise ValueError(
                    f"total_prompts ({self.total_prompts}) != series_count * turns_per_series "
                    f"({self.series_count} * {self.turns_per_series} = {derived_total})"
                )
        elif self.total_prompts is not None and self.series_count is not None:
            if self.total_prompts % self.series_count != 0:
                raise ValueError(
                    f"total_prompts ({self.total_prompts}) is not evenly divisible by "
                    f"series_count ({self.series_count})"
                )
            self.turns_per_series = self.total_prompts // self.series_count
        elif self.total_prompts is not None and self.turns_per_series is not None:
            if self.total_prompts % self.turns_per_series != 0:
                raise ValueError(
                    f"total_prompts ({self.total_prompts}) is not evenly divisible by "
                    f"turns_per_series ({self.turns_per_series})"
                )
            self.series_count = self.total_prompts // self.turns_per_series

        # --- fixed_checkpoints requires explicit, in-range positions ---
        if self.probe_placement == "fixed_checkpoints":
            if not self.probe_checkpoint_positions:
                raise ValueError(
                    "probe_placement is 'fixed_checkpoints' but probe_checkpoint_positions "
                    "is empty — provide explicit turn indices"
                )
            max_pos = self.turns_per_series or 0
            out_of_range = [
                p for p in self.probe_checkpoint_positions if p < 0 or p >= max_pos
            ]
            if out_of_range:
                raise ValueError(
                    f"probe_checkpoint_positions {out_of_range} out of range for "
                    f"turns_per_series={max_pos} (valid range: 0..{max_pos - 1})"
                )

        return self


def format_validation_error(exc: ValidationError) -> str:
    """
    Render a pydantic ValidationError as field-identifying lines, e.g.:
      - conditions: contains invalid value(s) ['bogus']; must be from (...)
      - turns_per_series: input should be greater than 0

    Used by the CLI so plan-loading failures never surface a raw stack trace.
    """
    lines = []
    for err in exc.errors():
        loc = ".".join(str(p) for p in err["loc"]) or "(top level)"
        lines.append(f"  - {loc}: {err['msg']}")
    return "\n".join(lines)
