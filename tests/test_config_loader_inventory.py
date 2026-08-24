"""
Milestone 44 Phase 1 — the class fix.

core/config_overrides.py's module docstring has enumerated every backend
module that independently loads config.yaml from disk since M43, with a
warning that a missed one is "silent divergence between two modules' views
of the config." That inventory caught it out for `api/admin.py`'s
POST /admin/run-probe anyway — because a docstring is tribal knowledge with
no enforcement, and nobody re-checked it when a second, unrelated disk
loader turned up a hundred lines below the one M43's own trap diagram was
built around (see BUILD-PLAN.md's Milestone 44, "Why M43 missed it").

This test turns the inventory into something that fails CI instead of
something a reviewer has to remember to reread. It scans backend/**/*.py
for the `open(_CONFIG_PATH ...)` pattern every existing loader uses and
checks each hit against an explicit allowlist naming why it's there and
whether it participates in the CONFIG_OVERRIDES_JSON merge — mirroring
core/config_overrides.py's own docstring inventory, kept in sync with it
by hand (both are short; a diff reviewer can eyeball them together).
"""

import re
from pathlib import Path

BACKEND_ROOT = Path(__file__).resolve().parent.parent / "backend"

# Matches every existing loader's `with open(_CONFIG_PATH, ...)` line. Not
# a general "reads config.yaml" detector — deliberately narrow to the one
# concrete pattern every current loader shares, so it stays cheap and does
# not need to understand indirection (a module that reads config.yaml some
# other way won't be caught here; broaden the pattern if that happens).
_LOAD_SITE_PATTERN = re.compile(r"open\(\s*_CONFIG_PATH\b")

# backend-relative path (posix separators) -> one-line reason. Keep this in
# sync with core/config_overrides.py's module docstring inventory by hand.
ALLOWLIST: dict[str, str] = {
    "core/orchestrator.py": (
        "applies overrides — the primary resolved config (model/"
        "hyperparameters/edge_update/experiment), M43"
    ),
    "memory/extractor.py": "applies overrides — its own hyperparameters/model read, M43",
    "analytics/seed_fixture.py": (
        "applies overrides — edge_update.seed_prior_alpha/beta at seed time, M43"
    ),
    "analytics/reports/experiment_report.py": (
        "applies overrides — reporting.* thresholds at report-generation time, M43"
    ),
    "analytics/reports/series_report.py": (
        "applies overrides — reporting.* thresholds at report-generation time, M43"
    ),
    "api/admin.py": (
        "deliberately disk for GET /admin/config (the Admin UI's editable "
        "registry, M43); POST /admin/run-probe no longer loads here — it "
        "reads the already-resolved _CONFIG imported from core.orchestrator "
        "instead (M44 fix)"
    ),
    "api/chat.py": (
        "deliberately disk — SimpleRL-only weight_update_rate, a section no "
        "experiment sweep targets (M43; SimpleRL is unreachable from the "
        "harness per Milestone 24)"
    ),
    "gate/sanitize.py": (
        "deliberately disk — injection-pattern regexes / max_input_length, a "
        "section no experiment sweep targets (M43)"
    ),
    "tools/registry.py": (
        "deliberately disk — tool registration, a section no experiment "
        "sweep targets (M43)"
    ),
}


def _scan_for_disk_config_loaders(root: Path) -> set[str]:
    """Return backend-relative (posix) paths of every .py file containing
    an `open(_CONFIG_PATH ...)` disk-config load site."""
    found: set[str] = set()
    for py_file in root.rglob("*.py"):
        if "__pycache__" in py_file.parts:
            continue
        text = py_file.read_text(encoding="utf-8")
        if _LOAD_SITE_PATTERN.search(text):
            found.add(py_file.relative_to(root).as_posix())
    return found


def test_every_disk_config_loader_is_in_the_allowlist():
    """The enforcement this milestone exists to add: a new, unlisted
    open(_CONFIG_PATH ...) site fails the suite rather than silently
    joining the docstring-only inventory nobody rereads."""
    found = _scan_for_disk_config_loaders(BACKEND_ROOT)
    unlisted = found - set(ALLOWLIST)
    assert not unlisted, (
        f"New config.yaml disk loader(s) with no ALLOWLIST entry: {sorted(unlisted)} — "
        "add a one-line reason here (and to core/config_overrides.py's module "
        "docstring) naming whether it needs apply_config_overrides()."
    )


def test_allowlist_has_no_stale_entries():
    """Catches drift the other direction — an ALLOWLIST entry for a loader
    that moved or was removed, which would otherwise silently stop being
    checked at all."""
    found = _scan_for_disk_config_loaders(BACKEND_ROOT)
    stale = set(ALLOWLIST) - found
    assert not stale, (
        f"ALLOWLIST entries with no matching loader: {sorted(stale)} — "
        "the file moved, was rewritten to not use _CONFIG_PATH, or was deleted; "
        "update ALLOWLIST to match."
    )


def test_adding_a_throwaway_loader_to_backend_fails_the_inventory_guard():
    """Proves the scanner actually catches a new site rather than merely
    passing today because nothing new happens to exist — 'Adding a
    throwaway loader to a backend module fails the inventory test' per
    BUILD-PLAN.md's Milestone 44 Done-when criteria. Writes a real file
    into backend/ and removes it in the finally block regardless of outcome."""
    throwaway = BACKEND_ROOT / "_m44_throwaway_loader_for_test.py"
    assert not throwaway.exists(), "throwaway test file already exists — leftover from a prior failed run?"
    throwaway.write_text(
        'import os\n'
        '\n'
        '_CONFIG_PATH = "config.yaml"\n'
        '\n'
        'with open(_CONFIG_PATH, "r", encoding="utf-8") as f:\n'
        '    pass\n',
        encoding="utf-8",
    )
    try:
        found = _scan_for_disk_config_loaders(BACKEND_ROOT)
        assert "_m44_throwaway_loader_for_test.py" in found, (
            "scanner failed to detect the throwaway loader — pattern regressed"
        )
        unlisted = found - set(ALLOWLIST)
        assert "_m44_throwaway_loader_for_test.py" in unlisted, (
            "throwaway loader was somehow already allowlisted — test fixture bug"
        )
    finally:
        throwaway.unlink()
