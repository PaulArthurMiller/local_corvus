"""Tests for BackendClient.effective_config() (M43 Phase 1)."""

import os
import sys

import httpx

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "scripts"))

import pytest

from harness.client import BackendClient, BackendConnectivityError


class _RecordingTransport(httpx.BaseTransport):
    def __init__(self, response_json=None, status_code=200):
        self.requests = []
        self._response_json = response_json or {}
        self._status_code = status_code

    def handle_request(self, request):
        self.requests.append(request)
        return httpx.Response(self._status_code, json=self._response_json)


def _client_with(transport):
    client = BackendClient(base_url="http://backend")
    client._client = httpx.Client(base_url="http://backend", transport=transport)
    return client


def test_effective_config_gets_the_right_endpoint():
    transport = _RecordingTransport(response_json={"config": {}, "runtime_config_hash": "h"})
    client = _client_with(transport)

    client.effective_config()

    assert len(transport.requests) == 1
    assert transport.requests[0].url.path == "/admin/effective-config"
    assert transport.requests[0].method == "GET"


def test_effective_config_returns_parsed_body():
    body = {"config": {"edge_update": {"seed_prior_alpha": 3.0}}, "runtime_config_hash": "abc123"}
    transport = _RecordingTransport(response_json=body)
    client = _client_with(transport)

    result = client.effective_config()

    assert result == body


def test_effective_config_5xx_raises_backend_connectivity_error():
    transport = _RecordingTransport(response_json={"detail": "boom"}, status_code=500)
    client = _client_with(transport)

    with pytest.raises(BackendConnectivityError):
        client.effective_config()


def test_effective_config_connect_error_raises_backend_connectivity_error():
    class _FailingTransport(httpx.BaseTransport):
        def handle_request(self, request):
            raise httpx.ConnectError("refused")

    client = _client_with(_FailingTransport())

    with pytest.raises(BackendConnectivityError):
        client.effective_config()
