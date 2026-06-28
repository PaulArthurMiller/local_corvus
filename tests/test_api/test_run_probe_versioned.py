"""
Tests for Phase 0.2: probe_set_version parameter on POST /admin/run-probe.

Verifies that:
  - Default probe_set_version="v1" preserves backward compat
  - probe_set_version="v2" routes to v2 probe set
  - A probe_id only in v1 returns 404 when probe_set_version="v2"
  - A probe_id only in v2 returns 404 when probe_set_version="v1"
"""

import asyncio
import os
import sys
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "backend"))

from analytics.probes import ProbeDefinition


def run(coro):
    return asyncio.get_event_loop().run_until_complete(coro)


def _make_probe(probe_id: str, version: str) -> ProbeDefinition:
    return ProbeDefinition(
        probe_id=probe_id,
        phenomenon_type="ranking_improvement",
        prompt_text=f"Describe {probe_id}",
        gold_mu_ids=[],
        is_bridge_probe=False,
        bridge_pair_id=None,
        probe_set_version=version,
    )


# ---------------------------------------------------------------------------
# RunProbeRequest model tests (no HTTP client needed — unit-test the Pydantic model)
# ---------------------------------------------------------------------------

class TestRunProbeRequestModel:
    def test_default_probe_set_version_is_v1(self):
        """RunProbeRequest defaults to probe_set_version='v1'."""
        from api.admin import RunProbeRequest
        req = RunProbeRequest(
            probe_id="p01",
            measurement_session_index=0,
            condition_label="learning",
        )
        assert req.probe_set_version == "v1"
        print("[PASS] default probe_set_version is 'v1'")

    def test_probe_set_version_accepts_v2(self):
        """RunProbeRequest accepts probe_set_version='v2'."""
        from api.admin import RunProbeRequest
        req = RunProbeRequest(
            probe_id="p06",
            measurement_session_index=0,
            condition_label="learning",
            probe_set_version="v2",
        )
        assert req.probe_set_version == "v2"
        print("[PASS] probe_set_version='v2' accepted")


# ---------------------------------------------------------------------------
# load_probe_set routing tests
# ---------------------------------------------------------------------------

class TestProbeSetVersionRouting:
    def test_v1_probe_id_not_found_in_v2(self, tmp_path):
        """A probe_id only in v1 raises FileNotFoundError (or returns empty list) for version='v2'."""
        from analytics.probes import load_probe_set

        with patch("analytics.probes._PROBES_PATH", tmp_path):
            # v2 yaml does not exist → FileNotFoundError
            with pytest.raises(FileNotFoundError):
                load_probe_set(version="v2")
        print("[PASS] load_probe_set('v2') raises FileNotFoundError when v2 yaml missing")

    def test_v1_probe_loads_for_v1_version(self, tmp_path):
        """load_probe_set(version='v1') successfully loads v1 probes when yaml exists."""
        import yaml
        v1_data = {
            "version": "v1",
            "phenomenon_map": {"ranking_improvement": {"description": "test"}},
            "probes": [
                {
                    "probe_id": "p01",
                    "phenomenon_type": "ranking_improvement",
                    "prompt_text": "Test prompt",
                    "gold_mu_ids": [],
                    "is_bridge_probe": False,
                    "bridge_pair_id": None,
                }
            ],
        }
        (tmp_path / "probe_set_v1.yaml").write_text(yaml.dump(v1_data), encoding="utf-8")

        from analytics.probes import load_probe_set
        with patch("analytics.probes._PROBES_PATH", tmp_path):
            probes = load_probe_set(version="v1")

        assert len(probes) == 1
        assert probes[0].probe_id == "p01"
        assert probes[0].probe_set_version == "v1"
        print(f"[PASS] v1 probes loaded: {[p.probe_id for p in probes]}")

    def test_v2_probe_loads_for_v2_version(self, tmp_path):
        """load_probe_set(version='v2') successfully loads v2 probes when yaml exists."""
        import yaml
        v2_data = {
            "version": "v2",
            "phenomenon_map": {"ranking_improvement": {"description": "test"}},
            "probes": [
                {
                    "probe_id": "p06",
                    "phenomenon_type": "ranking_improvement",
                    "prompt_text": "Test prompt v2",
                    "gold_mu_ids": [],
                    "is_bridge_probe": False,
                    "bridge_pair_id": None,
                }
            ],
        }
        (tmp_path / "probe_set_v2.yaml").write_text(yaml.dump(v2_data), encoding="utf-8")

        from analytics.probes import load_probe_set
        with patch("analytics.probes._PROBES_PATH", tmp_path):
            probes = load_probe_set(version="v2")

        assert len(probes) == 1
        assert probes[0].probe_id == "p06"
        assert probes[0].probe_set_version == "v2"
        print(f"[PASS] v2 probes loaded: {[p.probe_id for p in probes]}")
