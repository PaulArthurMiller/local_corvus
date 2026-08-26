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


def find_unresolvable_override_keys(overrides: dict[str, Any], live_config: dict) -> list[str]:
    """
    Return the dotted keys in a RunPlan.config_overrides dict that do NOT
    resolve to an existing leaf in `live_config` (the "config" key of a GET
    /admin/effective-config response, fetched once at queue preflight,
    before any plan's own restart) — a shape check, not a value check.

    Mirrors, on the host and offline, the resolution rule
    backend/core/config_overrides.py's apply_config_overrides() enforces at
    container startup: an unresolvable key is a hard fail there too, but
    only once that plan's own per-condition restart actually runs — which
    for a queue can be hours into an unattended overnight session. Catching
    it here instead means a typo'd or renamed key is a preflight hard stop
    at minute zero, not a crash mid-run (Milestone 47 follow-up: the queue
    preflight had no check at all for config_overrides, only for
    expected_config on override-empty plans).

    Deliberately does NOT check values, only key existence: config_overrides
    IS the plan's own intended value at that key, and its provenance is
    already recorded on every real run row via analytics.full_config_snapshot
    — a wrong-but-resolvable value is a data-science correctness question the
    scientist owns and can audit after the fact, not an infrastructure
    failure a preflight step should try to adjudicate. Empty list means every
    key resolves.
    """
    return [key for key in overrides if get_dotted(live_config, key) is _MISSING]


def format_unresolvable_override_keys(keys: list[str]) -> str:
    """Render find_unresolvable_override_keys()'s output as one hard-stop message."""
    detail = ", ".join(repr(k) for k in keys)
    return (
        f"config_overrides declares {len(keys)} key(s) that do not resolve to an "
        f"existing config.yaml leaf ({detail}) — this would hard-fail at backend "
        f"startup once this plan's own restart runs, hours into an unattended "
        f"queue; fix the key(s) (or config.yaml, if a leaf was renamed) before launching"
    )


def find_dotted_key_collisions(live_config: dict, _prefix: str = "") -> list[str]:
    """
    Return the dotted paths of any live-config key that contains a literal
    "." in its own name — the one input that breaks the dotted-path scheme
    silently.

    BUILD-PLAN.md Milestone 43 flagged this as an open question: no
    config.yaml key contains a dot today (57 leaves, verified 2026-08-13),
    but if one is ever added, "a.b" becomes ambiguous between the nested
    path a→b and the flat key "a.b", and both expected_config and
    config_overrides would silently resolve the wrong one. Cheap to assert,
    impossible to debug from the symptom — so runner.py and queue.py both
    check it against the *live* config on the same call they already make,
    rather than assuming config.yaml never changes shape.

    Returns the offending paths (empty list = the scheme is unambiguous).
    """
    offenders: list[str] = []
    for key, value in live_config.items():
        path = f"{_prefix}{key}"
        if "." in str(key):
            offenders.append(path)
        if isinstance(value, dict):
            offenders.extend(find_dotted_key_collisions(value, f"{path}."))
    return offenders


def format_dotted_key_collisions(offenders: list[str]) -> str:
    """Render find_dotted_key_collisions()'s output as one hard-stop message."""
    detail = ", ".join(repr(o) for o in offenders)
    return (
        f"config.yaml contains {len(offenders)} key(s) with a literal '.' in the "
        f"key name ({detail}) — this makes every dotted expected_config/"
        f"config_overrides path ambiguous. Rename the key(s) before running."
    )
