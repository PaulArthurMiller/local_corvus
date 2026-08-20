"""
Milestone 44 Phase 1 regression test.

POST /admin/run-probe must resolve retrieval hyperparameters from
core.orchestrator._CONFIG (the config CONFIG_OVERRIDES_JSON — M43 Phase 2 —
already merges into) rather than re-opening config.yaml from disk. Before
this fix, the endpoint's own `with open(_CONFIG_PATH, ...)` read the raw
committed file, so a plan's config_overrides reached every real /chat turn
but never a single probe measurement — see BUILD-PLAN.md's Milestone 44 for
the exp01-cfgB blast radius this was found from (3,500 probe rows measured
at K2=1 while the run's real turns were trained at the declared K2=2).

Real end-to-end test against a temp SQLite graph store (same TempDBStore
pattern as tests/test_memory/test_retrieval.py), reloading
core.orchestrator + api.admin with CONFIG_OVERRIDES_JSON set — the same
reload convention tests/test_core/test_config_overrides_wiring.py already
uses for the other M43 loaders. api.admin binds `_CONFIG` by `from
core.orchestrator import _CONFIG` at import time, so both modules must be
reloaded in that order for the override to actually reach admin's namespace.

Fixture: 3 MUs share two keywords ("elephant", "mammal") and all reach K1
(retrieval_level1_top_k=3, config.yaml's committed default — all three
have 2 keyword hits and no other MU competes). Two more MUs are decoys,
each reachable by exactly one outgoing edge from a different K1 MU, at
different weights: A->D at 0.9, B->E at 0.5. With retrieval_level2_top_k=1
(config.yaml's committed default) only D — the higher-weight target —
enters K2, for a retrieved slate of 3+1=4. Raising the override to 2 lets
both D and E enter K2, for a slate of 3+2=5. This is exactly the K1(3)+K2
geometry difference BUILD-PLAN.md's Milestone 44 measured in the real
exp01-cfgB graph.db (declared K2=2 giving a slate of 5; the pre-fix
endpoint actually measuring K2=1's slate of 4).

MUST FAIL against pre-fix code: verified manually (git stash the fix,
rerun this file — retrieved_mu_ids stays length 4 under the override
because the pre-fix admin.py never sees CONFIG_OVERRIDES_JSON at all, it
re-reads the untouched file) before applying backend/api/admin.py's fix.
"""

import asyncio
import importlib
import os
import sys
import tempfile
import uuid
from pathlib import Path
from unittest import mock

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "backend"))


def run(coro):
    return asyncio.get_event_loop().run_until_complete(coro)


def _mu_id() -> str:
    return str(uuid.uuid4())


@pytest.fixture
def temp_graph_db(monkeypatch):
    """
    Point memory.store._DB_PATH at a temp file for the test's duration.

    GraphStore methods read the module-level _DB_PATH global fresh on every
    call (see memory/store.py's _open_db()) rather than storing it on the
    instance, so patching the module attribute redirects every caller that
    imported the `store` singleton — api.admin and analytics.probes both
    do — without needing to also patch those imports individually.
    """
    import memory.store as store_module

    tmpdir = tempfile.TemporaryDirectory()
    db_path = Path(tmpdir.name) / "test_graph.db"
    monkeypatch.setattr(store_module, "_DB_PATH", db_path)
    yield store_module.store
    tmpdir.cleanup()


def _populate_k1_k2_fixture(store) -> dict[str, str]:
    """3 K1 candidates sharing 'elephant'+'mammal', 2 decoy MUs reachable by
    one differently-weighted edge each from a different K1 MU — see module
    docstring for why this makes K2=1 vs K2=2 select a different-sized set."""
    ids = {name: _mu_id() for name in ("a", "b", "c", "d", "e")}
    run(store.create_mu(mu_id=ids["a"], source_type="user_input",
                         raw_content="The elephant is a large mammal in the savannah."))
    run(store.create_mu(mu_id=ids["b"], source_type="user_input",
                         raw_content="An elephant is also a mammal near the ocean."))
    run(store.create_mu(mu_id=ids["c"], source_type="user_input",
                         raw_content="Every elephant is a mammal living in the forest."))
    run(store.create_mu(mu_id=ids["d"], source_type="user_input",
                         raw_content="Giraffes live at the zoo."))
    run(store.create_mu(mu_id=ids["e"], source_type="user_input",
                         raw_content="Lions live at the zoo too."))
    run(store.create_edge(source_mu_id=ids["a"], target_mu_id=ids["d"], weight=0.9))
    run(store.create_edge(source_mu_id=ids["b"], target_mu_id=ids["e"], weight=0.5))
    return ids


def _reload_admin_with_override(monkeypatch, override_json):
    """
    Reload core.orchestrator (which parses CONFIG_OVERRIDES_JSON and merges
    it at import time — M43 Phase 2) then api.admin (which binds `_CONFIG`
    by value from core.orchestrator at its own import time, so it must be
    reloaded AFTER orchestrator to pick up the new merge).
    """
    if override_json is None:
        monkeypatch.delenv("CONFIG_OVERRIDES_JSON", raising=False)
    else:
        monkeypatch.setenv("CONFIG_OVERRIDES_JSON", override_json)

    import core.orchestrator as orchestrator
    importlib.reload(orchestrator)
    import api.admin as admin
    importlib.reload(admin)
    return admin


def test_run_probe_slate_size_reflects_level2_top_k_override(monkeypatch, temp_graph_db):
    """The regression test: raising retrieval_level2_top_k via
    CONFIG_OVERRIDES_JSON must change what POST /admin/run-probe actually
    retrieves, not just what GET /admin/effective-config reports."""
    ids = _populate_k1_k2_fixture(temp_graph_db)

    admin = _reload_admin_with_override(
        monkeypatch, '{"hyperparameters.retrieval_level2_top_k": 2}'
    )
    try:
        # Sanity check the override actually reached admin's resolved config
        # before trusting the retrieval-level assertion below — if this
        # fails, the test would otherwise silently degenerate into testing
        # nothing (the fixture would just reproduce the K2=1 baseline).
        assert admin._CONFIG["hyperparameters"]["retrieval_level2_top_k"] == 2

        from analytics.probes import ProbeDefinition

        probe = ProbeDefinition(
            probe_id="m44-k2-probe",
            phenomenon_type="ranking_improvement",
            prompt_text="elephant mammal",
            gold_mu_ids=[ids["a"]],
            is_bridge_probe=False,
            probe_set_version="v2",
        )
        body = admin.RunProbeRequest(
            probe_id="m44-k2-probe",
            measurement_session_index=1,
            condition_label="static_weight",
            probe_set_version="v2",
        )
        with mock.patch("api.admin.load_probe_set", return_value=[probe]):
            result = run(admin.run_probe(body))

        retrieved = set(result["retrieved_mu_ids"])
        print(f"[test] retrieved_mu_ids (K2 override=2): {retrieved}")
        assert {ids["a"], ids["b"], ids["c"]} <= retrieved, "all 3 K1 candidates must be retrieved"
        assert ids["d"] in retrieved and ids["e"] in retrieved, (
            "both decoys must enter K2 once retrieval_level2_top_k=2 — "
            "this is what fails pre-fix, since the disk-read config.yaml "
            "commits retrieval_level2_top_k=1 regardless of the override"
        )
        assert len(retrieved) == 5, f"expected K1(3)+K2(2)=5, got {len(retrieved)}"
    finally:
        # Restore process-wide module state so later tests importing
        # core.orchestrator / api.admin fresh see the committed config.
        _reload_admin_with_override(monkeypatch, None)


def test_run_probe_slate_size_without_override_stays_at_committed_default(monkeypatch, temp_graph_db):
    """Control case: no override set, config.yaml's committed
    retrieval_level2_top_k=1 still yields a 4-MU slate (only the
    higher-weight decoy). Confirms the fixture itself is sound and that
    the fix doesn't change behavior when there's nothing to override."""
    ids = _populate_k1_k2_fixture(temp_graph_db)

    admin = _reload_admin_with_override(monkeypatch, None)
    try:
        assert admin._CONFIG["hyperparameters"]["retrieval_level2_top_k"] == 1

        from analytics.probes import ProbeDefinition

        probe = ProbeDefinition(
            probe_id="m44-k2-probe",
            phenomenon_type="ranking_improvement",
            prompt_text="elephant mammal",
            gold_mu_ids=[ids["a"]],
            is_bridge_probe=False,
            probe_set_version="v2",
        )
        body = admin.RunProbeRequest(
            probe_id="m44-k2-probe",
            measurement_session_index=1,
            condition_label="static_weight",
            probe_set_version="v2",
        )
        with mock.patch("api.admin.load_probe_set", return_value=[probe]):
            result = run(admin.run_probe(body))

        retrieved = set(result["retrieved_mu_ids"])
        print(f"[test] retrieved_mu_ids (no override): {retrieved}")
        assert ids["d"] in retrieved and ids["e"] not in retrieved, (
            "only the higher-weight decoy (0.9) should reach K2=1"
        )
        assert len(retrieved) == 4, f"expected K1(3)+K2(1)=4, got {len(retrieved)}"
    finally:
        _reload_admin_with_override(monkeypatch, None)
