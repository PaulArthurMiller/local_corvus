"""
Per-call-class HTTP timeouts on BackendClient.

Report generation is a long aggregation rather than a request/response call:
experiment_report.py's §6.1 issues one query per probe definition, twice over
(mean and sample rows since M30), each with a condition_label fallback pass.
On the 43-probe v3 battery that is ~172 round-trips against the
connection-per-operation store, measured at 55.7s against a near-empty
database on 2026-07-27 — already past the 60s generic timeout before any real
run's data exists. These tests pin the resulting timeouts to the call classes
that need them.
"""

import os
import sys

import httpx
import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "scripts"))

from harness.client import BackendClient, ReportGenerationError


class _RecordingTransport(httpx.BaseTransport):
    """Captures the effective timeout httpx resolved for each request."""

    def __init__(self):
        self.read_timeouts = []

    def handle_request(self, request):
        self.read_timeouts.append(request.extensions.get("timeout", {}).get("read"))
        return httpx.Response(200, json={"report_id": "r1"})


def _client_with(transport, **kwargs):
    client = BackendClient(base_url="http://backend", **kwargs)
    client._client = httpx.Client(
        base_url="http://backend", transport=transport, timeout=kwargs.get("timeout", 60.0)
    )
    return client


def test_series_report_uses_the_report_timeout_not_the_generic_one():
    transport = _RecordingTransport()
    client = _client_with(transport)

    client.generate_series_report("exp-1", "series-1")

    assert transport.read_timeouts == [BackendClient.DEFAULT_REPORT_TIMEOUT]


def test_experiment_report_uses_the_report_timeout_not_the_generic_one():
    transport = _RecordingTransport()
    client = _client_with(transport)

    client.generate_experiment_report("exp-1", {})

    assert transport.read_timeouts == [BackendClient.DEFAULT_REPORT_TIMEOUT]


def test_report_timeout_is_generous_enough_for_the_measured_duration():
    """55.7s measured on a near-empty DB; the budget must leave real headroom
    for a run that has accumulated data, not merely clear the observation."""
    assert BackendClient.DEFAULT_REPORT_TIMEOUT >= 300.0


def test_seed_graph_uses_the_graph_op_timeout():
    """
    Regression: seeding seed_v3 took 63.8s against the 60s generic timeout
    while completing successfully server-side. seed_graph failures are
    classified as connectivity, and connectivity defaults to hard_stop, so
    the harness aborted the whole run over a correctly-seeded graph.
    """
    transport = _RecordingTransport()
    client = _client_with(transport)

    client.seed_graph("v3")

    assert transport.read_timeouts == [BackendClient.DEFAULT_GRAPH_OP_TIMEOUT]


def test_wipe_graph_uses_the_graph_op_timeout():
    transport = _RecordingTransport()
    client = _client_with(transport)

    client.wipe_graph("exp-1")

    assert transport.read_timeouts == [BackendClient.DEFAULT_GRAPH_OP_TIMEOUT]


def test_randomize_edges_uses_the_graph_op_timeout():
    """Same bulk-write class: one connection + fsync per edge, run at the
    start of every random_weight series."""
    transport = _RecordingTransport()
    client = _client_with(transport)

    client.randomize_edges()

    assert transport.read_timeouts == [BackendClient.DEFAULT_GRAPH_OP_TIMEOUT]


def test_graph_op_timeout_leaves_headroom_over_the_measured_seed():
    """63.8s measured; the budget must not merely clear that observation,
    since per-write cost scales with the graph and the host's disk."""
    assert BackendClient.DEFAULT_GRAPH_OP_TIMEOUT >= 300.0


def test_health_still_uses_the_generic_timeout():
    """Behavior-preserving guard: only the two report calls were re-budgeted."""
    transport = _RecordingTransport()
    client = _client_with(transport, timeout=60.0)

    client.health()

    assert transport.read_timeouts == [60.0]


def test_report_timeout_failure_still_raises_report_generation_error():
    """A timed-out report must stay in the report_generation error category so
    error_policy governs it, rather than surfacing as raw connectivity."""

    class _TimingOutTransport(httpx.BaseTransport):
        def handle_request(self, request):
            raise httpx.ReadTimeout("timed out", request=request)

    client = _client_with(_TimingOutTransport())

    with pytest.raises(ReportGenerationError):
        client.generate_experiment_report("exp-1", {})
