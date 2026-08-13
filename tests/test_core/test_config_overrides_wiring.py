"""
Import-time wiring tests: confirm core/orchestrator.py and the other
independent config.yaml loaders (memory/extractor.py, analytics/seed_fixture.py,
analytics/reports/experiment_report.py, analytics/reports/series_report.py)
actually apply CONFIG_OVERRIDES_JSON, not just that core/config_overrides.py's
merge logic works in isolation (see test_config_overrides.py for that).

Reloads each module with the env var set/unset via importlib.reload() —
these modules' import-time side effects (ToolRegistry init, etc.) are cheap
and idempotent, and every assertion here only touches module-level
constants, so reloading in-process is safe and matches the existing
test_orchestrator.py convention of importing the real module directly
against the real, committed config.yaml.
"""

import importlib
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "backend"))


def test_orchestrator_config_hash_differs_with_override(monkeypatch):
    """The cheapest guard against the milestone's own flagged risk: 'import-
    time merge ordering is the whole correctness surface of Phase 2' — if
    the merge ever moved after _CONFIG_HASH's computation, this would stop
    detecting a difference and fail."""
    import core.orchestrator as orchestrator

    monkeypatch.delenv("CONFIG_OVERRIDES_JSON", raising=False)
    importlib.reload(orchestrator)
    baseline_hash = orchestrator._CONFIG_HASH
    baseline_top_k = orchestrator._HP.get("retrieval_level2_top_k")

    monkeypatch.setenv(
        "CONFIG_OVERRIDES_JSON",
        '{"hyperparameters.retrieval_level2_top_k": 999}',
    )
    try:
        importlib.reload(orchestrator)
        assert orchestrator._HP["retrieval_level2_top_k"] == 999
        assert orchestrator._CONFIG_HASH != baseline_hash
        assert orchestrator._CONFIG_OVERRIDES == {"hyperparameters.retrieval_level2_top_k": 999}
    finally:
        # Restore process-wide module state for any test that runs after
        # this one in the same session and imports core.orchestrator fresh.
        monkeypatch.delenv("CONFIG_OVERRIDES_JSON", raising=False)
        importlib.reload(orchestrator)
        assert orchestrator._HP.get("retrieval_level2_top_k") == baseline_top_k


def test_extractor_applies_config_overrides(monkeypatch):
    import memory.extractor as extractor

    monkeypatch.delenv("CONFIG_OVERRIDES_JSON", raising=False)
    importlib.reload(extractor)
    baseline = extractor._HP.get("extractor_min_sentences")

    monkeypatch.setenv(
        "CONFIG_OVERRIDES_JSON",
        '{"hyperparameters.extractor_min_sentences": 999}',
    )
    try:
        importlib.reload(extractor)
        assert extractor._HP["extractor_min_sentences"] == 999
    finally:
        monkeypatch.delenv("CONFIG_OVERRIDES_JSON", raising=False)
        importlib.reload(extractor)
        assert extractor._HP.get("extractor_min_sentences") == baseline


def test_seed_fixture_seed_priors_apply_override(monkeypatch):
    """Critical for exp01 cfgA specifically: register_fixture() calls
    _load_seed_priors() at wipe+seed time, so without this, a seed-prior
    sweep would silently seed edges at the un-overridden config.yaml value
    regardless of what CONFIG_OVERRIDES_JSON says."""
    import analytics.seed_fixture as seed_fixture

    monkeypatch.delenv("CONFIG_OVERRIDES_JSON", raising=False)
    baseline_alpha, baseline_beta = seed_fixture._load_seed_priors()
    assert (baseline_alpha, baseline_beta) == (1.0, 1.0)  # config.yaml's committed default

    monkeypatch.setenv(
        "CONFIG_OVERRIDES_JSON",
        '{"edge_update.seed_prior_alpha": 3.0, "edge_update.seed_prior_beta": 3.0}',
    )
    alpha, beta = seed_fixture._load_seed_priors()
    assert (alpha, beta) == (3.0, 3.0)


def test_experiment_report_load_config_applies_override(monkeypatch):
    import analytics.reports.experiment_report as experiment_report

    monkeypatch.delenv("CONFIG_OVERRIDES_JSON", raising=False)
    baseline = experiment_report._load_config()
    assert baseline["reporting"]["pass_rank_thresholds"]["spoke_retrieval"] == 4  # config.yaml's committed default

    monkeypatch.setenv(
        "CONFIG_OVERRIDES_JSON",
        '{"reporting.pass_rank_thresholds.spoke_retrieval": 999}',
    )
    overridden = experiment_report._load_config()
    assert overridden["reporting"]["pass_rank_thresholds"]["spoke_retrieval"] == 999


def test_orchestrator_malformed_override_hard_fails_at_import(monkeypatch):
    """Malformed CONFIG_OVERRIDES_JSON must crash the backend at import
    (before any run state exists), not silently ignore the override or
    start with a half-applied config."""
    import core.orchestrator as orchestrator
    from core.config_overrides import ConfigOverrideError

    monkeypatch.setenv("CONFIG_OVERRIDES_JSON", "{not valid json")
    try:
        with pytest.raises(ConfigOverrideError):
            importlib.reload(orchestrator)
    finally:
        monkeypatch.delenv("CONFIG_OVERRIDES_JSON", raising=False)
        importlib.reload(orchestrator)


def test_orchestrator_override_does_not_bypass_decay_enabled_rejection(monkeypatch):
    """The merge must precede validate_edge_update_config() — an override
    that sets decay.enabled: true must still hard-fail the same way a
    committed config.yaml value would (BUILD-PLAN.md M43 risk: 'verify the
    merge does not route around the validator')."""
    import core.orchestrator as orchestrator
    from learning.edge_update import EdgeUpdateConfigError

    monkeypatch.setenv("CONFIG_OVERRIDES_JSON", '{"decay.enabled": true}')
    try:
        with pytest.raises(EdgeUpdateConfigError):
            importlib.reload(orchestrator)
    finally:
        monkeypatch.delenv("CONFIG_OVERRIDES_JSON", raising=False)
        importlib.reload(orchestrator)


def test_series_report_load_config_applies_override(monkeypatch):
    import analytics.reports.series_report as series_report

    monkeypatch.delenv("CONFIG_OVERRIDES_JSON", raising=False)
    baseline = series_report._load_config()
    assert baseline["hyperparameters"]["retrieval_level2_top_k"] == 1  # config.yaml's committed default

    monkeypatch.setenv(
        "CONFIG_OVERRIDES_JSON",
        '{"hyperparameters.retrieval_level2_top_k": 2}',
    )
    overridden = series_report._load_config()
    assert overridden["hyperparameters"]["retrieval_level2_top_k"] == 2
