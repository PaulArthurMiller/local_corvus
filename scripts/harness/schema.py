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

import re
from typing import Any, Optional, Union

from pydantic import BaseModel, Field, ValidationError, field_validator, model_validator

VALID_CONDITIONS = ("learned", "static_weight", "random_weight")
VALID_PROBE_PLACEMENTS = ("random", "end", "fixed_checkpoints")

# M29 — mirrors prompt_gen.py's CLUSTERS. Duplicated rather than imported:
# prompt_gen.py already imports RunPlan from this module, so importing back
# would be circular. Only the 4 cluster ids are needed here for shape
# validation of trained_spokes; the full spoke-content resolution stays in
# prompt_gen.py.
TRAINED_SPOKE_CLUSTERS = ("cluster_a", "cluster_b", "cluster_c", "cluster_d")
_TRAINED_SPOKE_KEY_RE = re.compile(r"^[abcd]_spoke[1-7]$")

# Error-handling policy keys understood by the harness in Phase 0.
# "connectivity" covers backend-unreachable / DB-failure conditions.
# "turn_ambiguous" covers a single per-turn result that is malformed or missing.
# "judge_unparseable" (M19 Phase 2) covers a judge response that fails to
# parse into a valid verdict even after one re-ask.
# "report_generation" (M19 Phase 3) covers a series/experiment report call
# failing — defaults to soft_continue regardless of the "connectivity"
# default, since a broken report is re-generatable later but an aborted
# run (that otherwise succeeded) is not.
#
# "record_none" (2026-08-06) is judge_unparseable-only: post a "none" verdict
# naming the parse failure instead of recording nothing at all. It replaces
# soft_continue as that key's default. The 2026-08-06 calibration lost 1 turn
# in 20 this way — at exp0's 1,050 turns that is ~50 turns silently absent
# from the denominator, which is indistinguishable in the data from turns
# that were never attempted. Both actions leave learning untouched (a "none"
# verdict triggers no weight update, exactly like recording nothing), so the
# change is to the record, not to the experiment.
ERROR_ACTION_RECORD_NONE = "record_none"
DEFAULT_ERROR_POLICY = {
    "connectivity": "hard_stop",
    "turn_ambiguous": "soft_continue",
    "judge_unparseable": ERROR_ACTION_RECORD_NONE,
    "report_generation": "soft_continue",
}
VALID_ERROR_ACTIONS = ("hard_stop", "soft_continue")
# Per-key extensions to VALID_ERROR_ACTIONS. Kept a whitelist rather than
# widening VALID_ERROR_ACTIONS for every key: "connectivity": "record_none"
# has no meaning, and a plan asking for it should fail validation rather than
# be quietly ignored at run time.
VALID_ERROR_ACTIONS_BY_KEY = {
    "judge_unparseable": VALID_ERROR_ACTIONS + (ERROR_ACTION_RECORD_NONE,),
}


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
    # M21 — which probes/probe_set_{version}.yaml battery the probe turns run.
    # Passed through to POST /admin/run-probe; the backend loads its own copy
    # of the same file (both sides read probes/ — one mounted, one local).
    probe_set_version: str = Field(default="v2", min_length=1)
    # M28 — which seed_fixtures/seed_{version}.json.gz the harness wipes+seeds
    # per condition (POST /admin/seed-graph) and reads cluster content from
    # for prompt-bank generation. Same convention as probe_set_version: a
    # free string resolved at runtime, not an enum — a bad value fails
    # loudly (missing fixture file / missing generator module) rather than
    # at schema-validation time.
    seed_fixture_version: str = Field(default="v2", min_length=1)
    # M29 — which spoke in each cluster is the single trained target that
    # build_bank()/read_cluster_content() draw "spoke_text" from. Defaults to
    # spoke1 in every cluster, preserving the pre-M29 hardcoded behavior for
    # any plan that doesn't set this field. Shape-validated only (keys are
    # the 4 known cluster ids, values match "{letter}_spokeN") — schema.py
    # cannot check a value resolves to real content in a given fixture
    # version without importing prompt_gen.py, which would be circular
    # (prompt_gen.py already imports RunPlan from here). A bad value fails
    # loudly at runtime in read_cluster_content(), not at schema-validation
    # time — same convention as seed_fixture_version above.
    trained_spokes: dict[str, str] = Field(
        default_factory=lambda: {
            "cluster_a": "a_spoke1",
            "cluster_b": "b_spoke1",
            "cluster_c": "c_spoke1",
            "cluster_d": "d_spoke1",
        }
    )

    # M43 Phase 1 — declared config-state assertion. Keys are dotted paths
    # into config.yaml ("edge_update.seed_prior_alpha"); values are the
    # expected leaf values. Checked against GET /admin/effective-config
    # before any graph mutation (runner.py preflight, queue.py step 5) —
    # a mismatch (including a declared key absent from the live config) is
    # an unconditional hard stop, not subject to error_policy override.
    # Shape-validated only (dict[str, Any]) — schema.py cannot know which
    # keys exist in config.yaml without importing backend modules, same
    # convention as seed_fixture_version/trained_spokes above: a bad value
    # fails loudly at runtime (a "mismatch" against the live config), not
    # at schema-validation time. Default {} — every existing plan is
    # unaffected and behaves exactly as before M43.
    #
    # Deliberately kept independent from config_overrides below rather than
    # auto-derived from it (BUILD-PLAN.md M43 open question, resolved:
    # "default to keeping them independent") — a plan can assert without
    # supplying, or supply without asserting.
    expected_config: dict[str, Any] = Field(default_factory=dict)

    # M43 Phase 2 — per-condition config injection. Same dotted-key shape as
    # expected_config. Rides the harness's existing per-condition backend
    # restart (docker_lifecycle.restart_backend) as CONFIG_OVERRIDES_JSON —
    # never writes config.yaml itself (git-tracked, human-edited,
    # bind-mounted; a harness that wrote it would also need to revert
    # reliably on crash/abort). Per-plan, not per-queue: the plan is already
    # the unit of identity, and config_overrides participates in
    # plan_config_hash (see compute_config_hash()) exactly like
    # expected_config does — the root-cause fix this milestone exists to
    # deliver is that a plan's config state is no longer external to its
    # own identity. Default {} — every existing plan is unaffected.
    config_overrides: dict[str, Any] = Field(default_factory=dict)

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

    @field_validator("trained_spokes")
    @classmethod
    def _validate_trained_spokes(cls, v: dict[str, str]) -> dict[str, str]:
        missing = [c for c in TRAINED_SPOKE_CLUSTERS if c not in v]
        extra = [c for c in v if c not in TRAINED_SPOKE_CLUSTERS]
        if missing or extra:
            raise ValueError(
                f"trained_spokes keys must be exactly {list(TRAINED_SPOKE_CLUSTERS)}; "
                f"missing {missing}, unexpected {extra}"
            )
        bad = {c: s for c, s in v.items() if not _TRAINED_SPOKE_KEY_RE.match(s)}
        if bad:
            raise ValueError(
                f"trained_spokes has malformed spoke key(s) {bad}; "
                f"each value must match '{{letter}}_spoke{{1-7}}', e.g. 'b_spoke3'"
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
        bad_actions = {
            k: a for k, a in v.items()
            if a not in VALID_ERROR_ACTIONS_BY_KEY.get(k, VALID_ERROR_ACTIONS)
        }
        if bad_actions:
            allowed = {
                k: VALID_ERROR_ACTIONS_BY_KEY.get(k, VALID_ERROR_ACTIONS) for k in bad_actions
            }
            raise ValueError(
                f"error_policy has invalid action(s) {bad_actions}; allowed per key: {allowed}"
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
