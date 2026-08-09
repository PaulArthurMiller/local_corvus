"""
Seed-fixture spoke membership — Milestone 39.

Maps each probe's gold MU to the spoke it targets ("a_spoke3") and the
cluster that spoke belongs to ("cluster_a"), and resolves a RunPlan's
trained_spokes dict (schema.py) to the probe id that measures each trained
spoke. This is the "MU -> spoke key" extension of M33's mu_cluster_info
lookup (which only ever resolved the coarse cluster_id, e.g. "cluster_a" —
see experiment_report.py's _build_mu_lineage_clusters/_collect_all_weight_updates).

Why this table is duplicated here rather than imported: scripts/generate_seed_v3.py
owns the canonical spoke_key -> mu_id table (MU_IDS), but scripts/ is
host-side harness tooling that is never mounted into the backend container —
docker-compose.yml's backend.volumes only crosses backend/, probes/, and
seed_fixtures/ into /app. This mirrors the M29 precedent in
scripts/harness/schema.py, where TRAINED_SPOKE_CLUSTERS is duplicated from
prompt_gen.py's CLUSTERS for the identical reason, one module boundary over.
The seed fixture is frozen and content_hash-guarded (M25/verify_seed_v3.py),
so this table cannot silently drift out from under the generator's own copy;
tests/test_analytics/test_spoke_membership.py cross-checks every mu_id here
against the real seed_fixtures/seed_v3.json.gz on disk as a regression guard.

No schema change: memory_units.cluster_id only ever stored the coarse
cluster (M33's mu_cluster_info); spoke-level membership only ever existed in
the frozen fixture generator's own literal table, so it has to be resolved
here at report time, not read back from the DB (M39 scope: report layer
only, per the milestone's "no schema change" rule).
"""

import logging
from typing import Optional

logger = logging.getLogger(__name__)

# spoke_key -> mu_id, duplicated from scripts/generate_seed_v3.py's MU_IDS
# (hub entries omitted — a hub is never a trainable spoke, and probes never
# target one). Keyed by seed_fixture_version below so a future v4 fixture
# adds a sibling table rather than overwriting this one.
_SEED_V3_SPOKE_MU_IDS: dict[str, str] = {
    "a_spoke1": "a0000002-aaaa-4aaa-8aaa-000000000002",
    "a_spoke2": "a0000003-aaaa-4aaa-8aaa-000000000003",
    "a_spoke3": "a0000004-aaaa-4aaa-8aaa-000000000004",
    "a_spoke4": "a0000005-aaaa-4aaa-8aaa-000000000005",
    "a_spoke5": "a0000006-aaaa-4aaa-8aaa-000000000006",
    "a_spoke6": "a0000007-aaaa-4aaa-8aaa-000000000007",
    "a_spoke7": "a0000008-aaaa-4aaa-8aaa-000000000008",
    "b_spoke1": "b0000002-bbbb-4bbb-8bbb-000000000002",
    "b_spoke2": "b0000003-bbbb-4bbb-8bbb-000000000003",
    "b_spoke3": "b0000004-bbbb-4bbb-8bbb-000000000004",
    "b_spoke4": "b0000005-bbbb-4bbb-8bbb-000000000005",
    "b_spoke5": "b0000006-bbbb-4bbb-8bbb-000000000006",
    "b_spoke6": "b0000007-bbbb-4bbb-8bbb-000000000007",
    "b_spoke7": "b0000008-bbbb-4bbb-8bbb-000000000008",
    "c_spoke1": "c0000002-cccc-4ccc-8ccc-000000000002",
    "c_spoke2": "c0000003-cccc-4ccc-8ccc-000000000003",
    "c_spoke3": "c0000004-cccc-4ccc-8ccc-000000000004",
    "c_spoke4": "c0000005-cccc-4ccc-8ccc-000000000005",
    "c_spoke5": "c0000006-cccc-4ccc-8ccc-000000000006",
    "c_spoke6": "c0000007-cccc-4ccc-8ccc-000000000007",
    "c_spoke7": "c0000008-cccc-4ccc-8ccc-000000000008",
    "d_spoke1": "d0000002-dddd-4ddd-8ddd-000000000002",
    "d_spoke2": "d0000003-dddd-4ddd-8ddd-000000000003",
    "d_spoke3": "d0000004-dddd-4ddd-8ddd-000000000004",
    "d_spoke4": "d0000005-dddd-4ddd-8ddd-000000000005",
    "d_spoke5": "d0000006-dddd-4ddd-8ddd-000000000006",
    "d_spoke6": "d0000007-dddd-4ddd-8ddd-000000000007",
    "d_spoke7": "d0000008-dddd-4ddd-8ddd-000000000008",
}

SPOKE_MU_IDS_BY_FIXTURE_VERSION: dict[str, dict[str, str]] = {
    "v3": _SEED_V3_SPOKE_MU_IDS,
}


def _cluster_key_from_spoke_key(spoke_key: str) -> Optional[str]:
    """'a_spoke3' -> 'cluster_a'. None if spoke_key isn't a '{letter}_spokeN' shape."""
    if not spoke_key or "_spoke" not in spoke_key:
        return None
    letter = spoke_key.split("_spoke", 1)[0]
    if len(letter) != 1 or not letter.isalpha():
        return None
    return f"cluster_{letter}"


def resolve_probe_spoke_membership(
    probes: list[dict],
    seed_fixture_version: str = "v3",
) -> dict[str, dict]:
    """
    For every probe whose gold MU resolves against the known fixture's spoke
    table, return {probe_id: {"spoke_key": ..., "cluster_key": ...}}.

    A probe is included only when its first gold_mu_id matches a spoke in
    this fixture version's table (spoke_retrieval probes carry exactly one
    gold MU each). Probes with no gold MU, or a gold MU this fixture version
    doesn't recognize (e.g. a hub MU, or a different probe_set/fixture
    pairing), are silently excluded — they have no cluster-winner role to
    play in the per-cluster tables this module feeds.
    """
    spoke_table = SPOKE_MU_IDS_BY_FIXTURE_VERSION.get(seed_fixture_version) or {}
    mu_id_to_spoke = {mu_id: spoke_key for spoke_key, mu_id in spoke_table.items()}

    membership: dict[str, dict] = {}
    for probe in probes:
        gold_ids = probe.get("gold_mu_ids") or []
        if not gold_ids:
            continue
        spoke_key = mu_id_to_spoke.get(gold_ids[0])
        if spoke_key is None:
            continue
        membership[probe["probe_id"]] = {
            "spoke_key": spoke_key,
            "cluster_key": _cluster_key_from_spoke_key(spoke_key),
        }
    logger.info(
        "spoke_membership: resolved %d/%d probe(s) to a spoke (fixture_version=%s)",
        len(membership), len(probes), seed_fixture_version,
    )
    return membership


def resolve_trained_probe_ids(
    trained_spokes: dict[str, str],
    probes: list[dict],
    seed_fixture_version: str = "v3",
) -> dict[str, dict]:
    """
    Map a RunPlan's trained_spokes ({"cluster_a": "a_spoke3", ...}, schema.py)
    to the probe that measures each trained spoke.

    Returns {cluster_key: {"spoke_key": ..., "mu_id": ..., "probe_id": ...}}.
    mu_id/probe_id are None (rendered as such, never dropped from the map)
    when the trained spoke isn't found in this fixture version's table, or
    has no matching probe in the synced probe set.
    """
    spoke_table = SPOKE_MU_IDS_BY_FIXTURE_VERSION.get(seed_fixture_version) or {}

    # First probe seen wins for a given gold MU — spoke_retrieval probes are
    # 1:1 with their gold MU, so no real collision is expected.
    probe_id_by_gold_mu: dict[str, str] = {}
    for probe in probes:
        for gold_id in probe.get("gold_mu_ids") or []:
            probe_id_by_gold_mu.setdefault(gold_id, probe["probe_id"])

    resolved: dict[str, dict] = {}
    for cluster_key, spoke_key in (trained_spokes or {}).items():
        mu_id = spoke_table.get(spoke_key)
        probe_id = probe_id_by_gold_mu.get(mu_id) if mu_id else None
        resolved[cluster_key] = {
            "spoke_key": spoke_key,
            "mu_id": mu_id,
            "probe_id": probe_id,
        }
        if mu_id is None:
            logger.warning(
                "spoke_membership: trained spoke %r (cluster %s) not found in "
                "fixture_version=%s spoke table",
                spoke_key, cluster_key, seed_fixture_version,
            )
        elif probe_id is None:
            logger.warning(
                "spoke_membership: trained spoke %r (cluster %s, mu_id=%s) has "
                "no matching probe in the synced probe set",
                spoke_key, cluster_key, mu_id,
            )
    return resolved


def trained_probe_id_set(resolved_trained_spokes: dict[str, dict]) -> set[str]:
    """Extract the set of non-None probe_ids from resolve_trained_probe_ids()'s output."""
    return {
        info["probe_id"]
        for info in (resolved_trained_spokes or {}).values()
        if info.get("probe_id")
    }
