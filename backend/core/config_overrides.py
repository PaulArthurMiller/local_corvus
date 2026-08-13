"""
CONFIG_OVERRIDES_JSON parsing + deep-merge (Milestone 43 Phase 2).

The harness never mutates config.yaml — it is git-tracked, human-edited,
and bind-mounted (see BUILD-PLAN.md "Plan-Declared Config State — Milestone
43" for why: a harness that wrote it would also need to revert reliably on
crash/abort, which M42's own step 7 already flagged as a rollback hazard).
Instead, a per-plan RunPlan.config_overrides dict rides through
docker_lifecycle.py's existing per-condition restart as a single JSON blob
in the CONFIG_OVERRIDES_JSON env var, and every backend module that reads
config.yaml applies it on top at import/load time via this module.

Every module that independently loads config.yaml from disk (see the
"multiple independent loaders" note below) must call
load_config_overrides_from_env() + apply_config_overrides() right after its
own _load_config(), before deriving anything from the result — silent
divergence between two modules' views of the config is exactly the class of
bug this milestone exists to kill. As of Milestone 43 that is:
  - core/orchestrator.py (the primary resolved config: model/hyperparameters/
    edge_update/experiment)
  - memory/extractor.py (its own hyperparameters/model read)
  - analytics/seed_fixture.py (edge_update.seed_prior_alpha/beta — read at
    seed time, critical for a seed-prior sweep to actually take effect)
  - analytics/reports/experiment_report.py and series_report.py (reporting.*
    thresholds — read at report-generation time, critical for a
    pass_rank_thresholds sweep to score correctly)

tools/registry.py, gate/sanitize.py, and api/chat.py's SimpleRL-only
weight_update_rate read config.yaml too, but only sections no experiment
sweep targets (tool registration, injection-pattern regexes, and the
harness-unreachable SimpleRL path — see BUILD-PLAN.md Milestone 24's
finding that SimpleRL is unreachable from the harness). Left untouched;
revisit if a future override ever targets those sections.
"""

import copy
import json
import logging
import os
from typing import Any

logger = logging.getLogger(__name__)

DEFAULT_OVERRIDES_ENV_VAR = "CONFIG_OVERRIDES_JSON"


class ConfigOverrideError(Exception):
    """
    Malformed CONFIG_OVERRIDES_JSON, or an override key that does not
    resolve to an existing config.yaml leaf.

    Raised at import/load time — before any run state exists — matching
    validate_edge_update_config()'s existing hard-fail-at-startup posture
    for a config.yaml the backend cannot safely run with.
    """


def parse_config_overrides_json(raw: str) -> dict[str, Any]:
    """
    Parse the CONFIG_OVERRIDES_JSON env var's raw string value.

    An empty/unset value (the common case — every process not opted into an
    override) returns {} rather than raising, so every existing call site
    that doesn't set the env var is unaffected.
    """
    if not raw or not raw.strip():
        return {}
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ConfigOverrideError(
            f"CONFIG_OVERRIDES_JSON is not valid JSON: {exc}"
        ) from exc
    if not isinstance(data, dict):
        raise ConfigOverrideError(
            f"CONFIG_OVERRIDES_JSON must be a JSON object of dotted-key -> "
            f"value pairs, got {type(data).__name__}"
        )
    return data


def load_config_overrides_from_env(env_var: str = DEFAULT_OVERRIDES_ENV_VAR) -> dict[str, Any]:
    """Read + parse the overrides env var. {} when unset, matching every
    existing config.yaml-reading module's unaffected-by-default behavior."""
    return parse_config_overrides_json(os.getenv(env_var, ""))


def apply_config_overrides(config: dict, overrides: dict[str, Any]) -> dict:
    """
    Deep-merge dotted-path `overrides` onto a *copy* of `config` (never
    mutates the input). Returns the merged copy.

    Every dotted key must already resolve to an existing leaf in `config` —
    an unknown key is a hard fail, not a silent no-op or a newly-created
    key. This is deliberate: a typo'd key (or a key that assumes a
    config.yaml shape that has since changed) must never look like it took
    effect when it didn't.
    """
    if not overrides:
        return config

    merged = copy.deepcopy(config)
    for dotted_key, value in overrides.items():
        parts = dotted_key.split(".")
        node = merged
        for depth, part in enumerate(parts[:-1]):
            if not isinstance(node, dict) or part not in node:
                raise ConfigOverrideError(
                    f"CONFIG_OVERRIDES_JSON key {dotted_key!r} does not resolve to an "
                    f"existing config.yaml leaf — no {'.'.join(parts[:depth + 1])!r} found"
                )
            node = node[part]
        leaf = parts[-1]
        if not isinstance(node, dict) or leaf not in node:
            raise ConfigOverrideError(
                f"CONFIG_OVERRIDES_JSON key {dotted_key!r} does not resolve to an "
                f"existing config.yaml leaf"
            )
        node[leaf] = value
        logger.info("config_overrides: applied %s = %r", dotted_key, value)

    return merged
