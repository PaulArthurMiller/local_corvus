"""
Shared plan.expected_config vs. GET /admin/effective-config comparison
(Milestone 43 Phase 1).

Pure functions only — no harness-specific exception types, no I/O. Both
runner.py (per-condition, before wipe/seed) and queue.py (step 5, per plan)
call find_config_mismatches() and wrap the result in their own hard-stop
exception with a specific, field-identifying message; this module stays a
single source of truth for what "mismatch" means so the two call sites
can never silently drift apart on the comparison itself.
"""

from typing import Any

# Sentinel distinguishing "the live config has this key set to None" from
# "the live config doesn't have this key at all" — both are real, different
# situations a mismatch message should describe differently.
_MISSING = object()


def get_dotted(config: dict, dotted_key: str) -> Any:
    """
    Resolve a dotted path ("edge_update.seed_prior_alpha") into a nested
    dict. Returns the _MISSING sentinel (never raises) if any segment is
    absent or the path runs into a non-dict — a declared key with no
    matching live value is a mismatch to report, not a crash.
    """
    node: Any = config
    for part in dotted_key.split("."):
        if not isinstance(node, dict) or part not in node:
            return _MISSING
        node = node[part]
    return node


def find_config_mismatches(expected: dict[str, Any], live_config: dict) -> list[str]:
    """
    Compare every declared key in `expected` (a RunPlan.expected_config
    dict) against `live_config` (the "config" key of a GET
    /admin/effective-config response). Returns one human-readable line per
    mismatch, "key: expected=X actual=Y" (actual="<missing>" when the
    declared key doesn't resolve at all) — empty list means every declared
    key matched.

    A declared key absent from the live config is a mismatch, not a skip
    (BUILD-PLAN.md Milestone 43, Phase 1 build item 4) — a plan that
    declares a key that no longer exists in config.yaml (e.g. after a
    later refactor renamed it) must fail loudly, not be silently ignored.
    """
    mismatches = []
    for key, expected_value in expected.items():
        actual_value = get_dotted(live_config, key)
        if actual_value is _MISSING:
            mismatches.append(f"{key}: expected={expected_value!r} actual=<missing>")
        elif actual_value != expected_value:
            mismatches.append(f"{key}: expected={expected_value!r} actual={actual_value!r}")
    return mismatches


def format_config_mismatches(mismatches: list[str]) -> str:
    """Render find_config_mismatches()'s output as one hard-stop message."""
    detail = "; ".join(mismatches)
    return f"expected_config mismatch — {detail}"
