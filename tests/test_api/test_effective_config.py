"""
Tests for GET /admin/effective-config (M43 Phase 1).

Calls the route handler function directly (matching test_graph_wipe.py's
convention) and patches api.admin's module-level names bound at import
time from core.orchestrator — this is what the endpoint must NOT re-read
config.yaml to get right (see BUILD-PLAN.md M43's "the trap that shapes
Phase 1": GET /admin/config re-reads the file and would certify the exact
"edited but not restarted" bug this endpoint exists to catch).
"""

import asyncio
import os
import sys
from unittest.mock import patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "backend"))


def run(coro):
    return asyncio.get_event_loop().run_until_complete(coro)


def test_effective_config_reports_orchestrator_state_not_disk():
    from api.admin import get_effective_config

    fake_config = {"edge_update": {"seed_prior_alpha": 3.0}}
    with patch("api.admin._CONFIG", fake_config), \
         patch("api.admin._CONFIG_HASH", "fake-hash-123"), \
         patch("api.admin._CONFIG_OVERRIDES", {"edge_update.seed_prior_alpha": 3.0}), \
         patch("api.admin.EXPERIMENT_ID", "exp-test"), \
         patch("api.admin.RUN_CONDITION", "learned"), \
         patch("api.admin.USE_SESSION_CONTEXT", False), \
         patch.dict(os.environ, {"CONDITION_LABEL": "learned"}):
        result = run(get_effective_config())

    assert result["config"] == fake_config
    assert result["runtime_config_hash"] == "fake-hash-123"
    assert result["overridden_keys"] == ["edge_update.seed_prior_alpha"]
    assert result["resolved_env"]["EXPERIMENT_ID"] == "exp-test"
    assert result["resolved_env"]["RUN_CONDITION"] == "learned"
    assert result["resolved_env"]["CONDITION_LABEL"] == "learned"
    assert result["resolved_env"]["USE_SESSION_CONTEXT"] is False


def test_effective_config_overridden_keys_empty_when_no_overrides():
    from api.admin import get_effective_config

    with patch("api.admin._CONFIG", {}), \
         patch("api.admin._CONFIG_HASH", "h"), \
         patch("api.admin._CONFIG_OVERRIDES", {}):
        result = run(get_effective_config())

    assert result["overridden_keys"] == []


def test_effective_config_condition_label_none_when_unset():
    from api.admin import get_effective_config

    with patch("api.admin._CONFIG", {}), \
         patch("api.admin._CONFIG_HASH", "h"), \
         patch("api.admin._CONFIG_OVERRIDES", {}), \
         patch.dict(os.environ, {}, clear=False):
        os.environ.pop("CONDITION_LABEL", None)
        result = run(get_effective_config())

    assert result["resolved_env"]["CONDITION_LABEL"] is None
