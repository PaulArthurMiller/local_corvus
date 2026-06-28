"""
Tests for Phase 0.3: multi-version probe sync at startup.

Verifies that:
  - Both v1 and v2 are synced when both yaml files exist
  - A missing v2 yaml logs INFO and does not crash (non-fatal)
  - A missing v1 yaml logs INFO and does not crash (non-fatal)
  - Other exceptions log WARNING and do not crash
"""

import asyncio
import os
import sys
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "backend"))


def run(coro):
    return asyncio.get_event_loop().run_until_complete(coro)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_probe_def(probe_id: str, version: str):
    from analytics.probes import ProbeDefinition
    return ProbeDefinition(
        probe_id=probe_id,
        phenomenon_type="ranking_improvement",
        prompt_text="Test",
        gold_mu_ids=[],
        is_bridge_probe=False,
        bridge_pair_id=None,
        probe_set_version=version,
    )


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

class TestMultiVersionProbeSync:
    def test_both_versions_synced_when_both_exist(self):
        """Both v1 and v2 probe sets are synced when both yaml files exist."""
        v1_probes = [_make_probe_def("p01", "v1"), _make_probe_def("p02", "v1")]
        v2_probes = [_make_probe_def("p06", "v2")]

        mock_store = MagicMock()
        mock_store.sync_probes = AsyncMock()

        def _load_probe_set(version: str):
            if version == "v1":
                return v1_probes
            elif version == "v2":
                return v2_probes
            raise FileNotFoundError(f"probe_set_{version}.yaml not found")

        # Patch the imports inside the function at their source modules
        with patch("analytics.probes.load_probe_set", side_effect=_load_probe_set), \
             patch("memory.store.store", mock_store):
            from main import _sync_probes_on_startup
            run(_sync_probes_on_startup())

        assert mock_store.sync_probes.call_count == 2
        print("[PASS] sync_probes called twice — once per version")

    def test_missing_v2_does_not_crash(self):
        """Missing v2 yaml logs INFO and does not raise an exception."""
        v1_probes = [_make_probe_def("p01", "v1")]

        mock_store = MagicMock()
        mock_store.sync_probes = AsyncMock()

        def _load_probe_set(version: str):
            if version == "v1":
                return v1_probes
            raise FileNotFoundError(f"probe_set_{version}.yaml not found")

        with patch("analytics.probes.load_probe_set", side_effect=_load_probe_set), \
             patch("memory.store.store", mock_store):
            from main import _sync_probes_on_startup
            # Should not raise
            run(_sync_probes_on_startup())

        # Only v1 was synced
        assert mock_store.sync_probes.call_count == 1
        print("[PASS] missing v2 yaml does not crash; v1 still synced")

    def test_missing_v1_does_not_crash(self):
        """Missing v1 yaml logs INFO and does not raise an exception."""
        v2_probes = [_make_probe_def("p06", "v2")]

        mock_store = MagicMock()
        mock_store.sync_probes = AsyncMock()

        def _load_probe_set(version: str):
            if version == "v2":
                return v2_probes
            raise FileNotFoundError(f"probe_set_{version}.yaml not found")

        with patch("analytics.probes.load_probe_set", side_effect=_load_probe_set), \
             patch("memory.store.store", mock_store):
            from main import _sync_probes_on_startup
            run(_sync_probes_on_startup())

        # Only v2 was synced
        assert mock_store.sync_probes.call_count == 1
        print("[PASS] missing v1 yaml does not crash; v2 still synced")

    def test_sync_exception_does_not_crash(self):
        """A non-FileNotFoundError during sync logs WARNING and does not raise."""
        v1_probes = [_make_probe_def("p01", "v1")]

        mock_store = MagicMock()
        mock_store.sync_probes = AsyncMock(side_effect=RuntimeError("DB connection failed"))

        def _load_probe_set(version: str):
            if version == "v1":
                return v1_probes
            raise FileNotFoundError(f"probe_set_{version}.yaml not found")

        with patch("analytics.probes.load_probe_set", side_effect=_load_probe_set), \
             patch("memory.store.store", mock_store):
            from main import _sync_probes_on_startup
            # Should not raise even when sync_probes throws
            run(_sync_probes_on_startup())

        print("[PASS] sync exception does not crash startup")
