"""
Tests for analytics/reports/spoke_membership.py — Milestone 39 trained-probe
derivation (RunPlan.trained_spokes -> probe ids via gold_mu_ids -> seed
fixture spoke membership).

Covers:
  resolve_probe_spoke_membership: probe -> {spoke_key, cluster_key} for
    known spoke MUs; probes with no/unknown gold MU excluded, not crashed on.
  resolve_trained_probe_ids: trained_spokes -> probe_id, including a
    trained spoke with no matching probe (rendered as None, not dropped)
    and an unknown spoke_key (mu_id resolves to None).
  trained_probe_id_set: extracts only the resolved probe_ids.
  Cross-check against the real seed_fixtures/seed_v3.json.gz and
  probes/probe_set_v3.yaml on disk — the duplicated table this module
  carries (see its module docstring) must never silently drift from the
  frozen fixture it mirrors.
"""

import gzip
import json
import os
import sys
from pathlib import Path

import yaml

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "backend"))

from analytics.reports.spoke_membership import (
    SPOKE_MU_IDS_BY_FIXTURE_VERSION,
    _cluster_key_from_spoke_key,
    resolve_probe_spoke_membership,
    resolve_trained_probe_ids,
    trained_probe_id_set,
)

FIXTURES_DIR = Path(__file__).parent.parent.parent / "seed_fixtures"
PROBES_DIR = Path(__file__).parent.parent.parent / "probes"


def _probe(probe_id: str, gold_mu_ids=None, phenomenon_type: str = "spoke_retrieval") -> dict:
    return {
        "probe_id": probe_id,
        "gold_mu_ids": gold_mu_ids or [],
        "phenomenon_type": phenomenon_type,
    }


# ---------------------------------------------------------------------------
# _cluster_key_from_spoke_key
# ---------------------------------------------------------------------------


class TestClusterKeyFromSpokeKey:
    def test_valid_spoke_key(self):
        assert _cluster_key_from_spoke_key("a_spoke3") == "cluster_a"
        assert _cluster_key_from_spoke_key("d_spoke7") == "cluster_d"

    def test_malformed_spoke_key_returns_none(self):
        assert _cluster_key_from_spoke_key("not-a-spoke") is None
        assert _cluster_key_from_spoke_key("") is None
        assert _cluster_key_from_spoke_key(None) is None
        assert _cluster_key_from_spoke_key("ab_spoke3") is None  # multi-letter cluster


# ---------------------------------------------------------------------------
# resolve_probe_spoke_membership
# ---------------------------------------------------------------------------


class TestResolveProbeSpokeMembership:
    def test_resolves_known_spoke_mu(self):
        probes = [_probe("vs_a3", ["a0000004-aaaa-4aaa-8aaa-000000000004"])]
        membership = resolve_probe_spoke_membership(probes)
        assert membership["vs_a3"] == {"spoke_key": "a_spoke3", "cluster_key": "cluster_a"}

    def test_probe_with_no_gold_mu_excluded(self):
        probes = [_probe("vs_x1", gold_mu_ids=[])]
        membership = resolve_probe_spoke_membership(probes)
        assert "vs_x1" not in membership

    def test_probe_with_unrecognized_gold_mu_excluded_not_crashed(self):
        """A hub MU or a MU from an unregistered fixture version must not
        appear in the membership map, and must not raise."""
        probes = [_probe("vs_a_hub", ["a0000001-aaaa-4aaa-8aaa-000000000001"])]
        membership = resolve_probe_spoke_membership(probes)
        assert "vs_a_hub" not in membership

    def test_unknown_fixture_version_yields_empty_membership(self):
        probes = [_probe("vs_a1", ["a0000002-aaaa-4aaa-8aaa-000000000002"])]
        membership = resolve_probe_spoke_membership(probes, seed_fixture_version="v99")
        assert membership == {}

    def test_uses_first_gold_mu_only(self):
        """spoke_retrieval probes carry exactly one gold MU; a probe with
        multiple resolves off the first."""
        probes = [_probe("vs_multi", [
            "a0000004-aaaa-4aaa-8aaa-000000000004",  # a_spoke3
            "a0000005-aaaa-4aaa-8aaa-000000000005",  # a_spoke4
        ])]
        membership = resolve_probe_spoke_membership(probes)
        assert membership["vs_multi"]["spoke_key"] == "a_spoke3"


# ---------------------------------------------------------------------------
# resolve_trained_probe_ids
# ---------------------------------------------------------------------------


class TestResolveTrainedProbeIds:
    def test_resolves_trained_spoke_to_probe_id(self):
        probes = [_probe("vs_a3", ["a0000004-aaaa-4aaa-8aaa-000000000004"])]
        resolved = resolve_trained_probe_ids({"cluster_a": "a_spoke3"}, probes)
        assert resolved["cluster_a"] == {
            "spoke_key": "a_spoke3",
            "mu_id": "a0000004-aaaa-4aaa-8aaa-000000000004",
            "probe_id": "vs_a3",
        }

    def test_trained_spoke_with_no_matching_probe_rendered_not_dropped(self):
        """A trained spoke this probe set never battery-tested still gets a
        row — mu_id resolves, probe_id is explicitly None, not absent."""
        resolved = resolve_trained_probe_ids({"cluster_a": "a_spoke3"}, probes=[])
        assert "cluster_a" in resolved
        assert resolved["cluster_a"]["mu_id"] == "a0000004-aaaa-4aaa-8aaa-000000000004"
        assert resolved["cluster_a"]["probe_id"] is None

    def test_unknown_spoke_key_resolves_mu_id_to_none(self):
        resolved = resolve_trained_probe_ids({"cluster_a": "a_spoke99"}, probes=[])
        assert resolved["cluster_a"]["mu_id"] is None
        assert resolved["cluster_a"]["probe_id"] is None

    def test_all_four_clusters_resolve_independently(self):
        probes = [
            _probe("vs_a1", ["a0000002-aaaa-4aaa-8aaa-000000000002"]),
            _probe("vs_b3", ["b0000004-bbbb-4bbb-8bbb-000000000004"]),
            _probe("vs_c1", ["c0000002-cccc-4ccc-8ccc-000000000002"]),
            _probe("vs_d5", ["d0000006-dddd-4ddd-8ddd-000000000006"]),
        ]
        trained_spokes = {
            "cluster_a": "a_spoke1", "cluster_b": "b_spoke3",
            "cluster_c": "c_spoke1", "cluster_d": "d_spoke5",
        }
        resolved = resolve_trained_probe_ids(trained_spokes, probes)
        assert resolved["cluster_a"]["probe_id"] == "vs_a1"
        assert resolved["cluster_b"]["probe_id"] == "vs_b3"
        assert resolved["cluster_c"]["probe_id"] == "vs_c1"
        assert resolved["cluster_d"]["probe_id"] == "vs_d5"

    def test_empty_trained_spokes_yields_empty_resolution(self):
        assert resolve_trained_probe_ids({}, probes=[]) == {}
        assert resolve_trained_probe_ids(None, probes=[]) == {}


# ---------------------------------------------------------------------------
# trained_probe_id_set
# ---------------------------------------------------------------------------


class TestTrainedProbeIdSet:
    def test_extracts_only_resolved_probe_ids(self):
        resolved = {
            "cluster_a": {"spoke_key": "a_spoke1", "mu_id": "mu-a", "probe_id": "vs_a1"},
            "cluster_b": {"spoke_key": "b_spoke1", "mu_id": "mu-b", "probe_id": None},
        }
        assert trained_probe_id_set(resolved) == {"vs_a1"}

    def test_empty_input_yields_empty_set(self):
        assert trained_probe_id_set({}) == set()
        assert trained_probe_id_set(None) == set()


# ---------------------------------------------------------------------------
# Cross-check against the real frozen fixture + probe set on disk — the
# duplicated table must never silently drift from what it mirrors.
# ---------------------------------------------------------------------------


class TestSpokeTableMatchesFrozenFixture:
    def test_every_spoke_mu_id_exists_in_the_real_fixture_with_matching_cluster(self):
        fixture_path = FIXTURES_DIR / "seed_v3.json.gz"
        if not fixture_path.exists():
            import pytest
            pytest.skip(f"seed_v3 fixture not present at {fixture_path}")

        with gzip.open(fixture_path, "rt", encoding="utf-8") as f:
            fixture = json.load(f)
        mu_cluster = {mu["mu_id"]: mu["cluster_id"] for mu in fixture["memory_units"]}

        spoke_table = SPOKE_MU_IDS_BY_FIXTURE_VERSION["v3"]
        assert len(spoke_table) == 28, "expected exactly 7 spokes x 4 clusters"
        for spoke_key, mu_id in spoke_table.items():
            assert mu_id in mu_cluster, f"{spoke_key}'s mu_id {mu_id} not in the real fixture"
            assert mu_cluster[mu_id] == _cluster_key_from_spoke_key(spoke_key), (
                f"{spoke_key} resolves to cluster {_cluster_key_from_spoke_key(spoke_key)} "
                f"but the fixture's mu_id {mu_id} is stamped {mu_cluster[mu_id]!r}"
            )

    def test_every_probe_set_v3_gold_mu_matches_its_documented_spoke(self):
        probe_set_path = PROBES_DIR / "probe_set_v3.yaml"
        if not probe_set_path.exists():
            import pytest
            pytest.skip(f"probe_set_v3.yaml not present at {probe_set_path}")

        with open(probe_set_path, "r", encoding="utf-8") as f:
            data = yaml.safe_load(f)

        probes = [
            {"probe_id": p["probe_id"], "gold_mu_ids": p["gold_mu_ids"]}
            for p in data["probes"]
        ]
        membership = resolve_probe_spoke_membership(probes)
        assert len(membership) == 28

        for p in data["probes"]:
            # probe_id convention: "vs_{letter}{n}" -> "{letter}_spoke{n}"
            letter = p["probe_id"][3]
            num = p["probe_id"][4:]
            expected_spoke = f"{letter}_spoke{num}"
            assert membership[p["probe_id"]]["spoke_key"] == expected_spoke
