"""
/chat retry behaviour on BackendClient.

Motivated by the 2026-08-06 run abort: Ollama's llama-server hit a CUDA OOM
while evicting a prompt-cache entry, died with 0xc0000409, and returned a bare
500. The backend turned that into a 502 and the harness's connectivity
hard_stop killed a 1050-turn run on its first turn — despite the fault being
self-healing (Ollama expires the OOMed model and the next call reloads it).

These tests pin both halves of the fix: that a server-side failure is retried,
and that a timeout is NOT, because /chat is not idempotent (the orchestrator
persists the input and prompt MUs before calling Ollama and does not roll them
back on failure, so retrying an in-flight turn double-writes).
"""

import os
import sys

import httpx
import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "scripts"))

from harness.client import BackendClient, BackendConnectivityError, BackendTurnError


class _ScriptedTransport(httpx.BaseTransport):
    """Replays a scripted list of responses/exceptions, one per request."""

    def __init__(self, steps):
        self._steps = list(steps)
        self.request_count = 0

    def handle_request(self, request):
        self.request_count += 1
        step = self._steps.pop(0)
        if isinstance(step, Exception):
            raise step
        return step


def _ok_response():
    return httpx.Response(200, json={"run_id": "run-1", "response": "hi"})


def _client_with(steps, **kwargs):
    """BackendClient wired to a scripted transport, with sleeps recorded not taken."""
    slept = []
    client = BackendClient(
        base_url="http://backend",
        sleep_fn=slept.append,
        **kwargs,
    )
    transport = _ScriptedTransport(steps)
    client._client = httpx.Client(base_url="http://backend", transport=transport)
    return client, transport, slept


def test_chat_retries_a_500_and_succeeds_on_the_second_attempt():
    """The exact 2026-08-06 fault: one 500 from a crashed model runner, then recovery."""
    client, transport, slept = _client_with(
        [httpx.Response(500, text="Server error 500 for host.docker.internal:11434"), _ok_response()]
    )

    result = client.chat("what is the kestrel rule?", series_id="s0")

    assert result["run_id"] == "run-1"
    assert transport.request_count == 2
    assert slept == [BackendClient.DEFAULT_CHAT_RETRY_BACKOFF[0]]


def test_chat_makes_three_attempts_total_before_giving_up():
    client, transport, slept = _client_with([httpx.Response(502, text="bad gateway")] * 3)

    with pytest.raises(BackendConnectivityError) as exc_info:
        client.chat("msg")

    assert transport.request_count == 3
    assert "502" in str(exc_info.value)
    assert slept == list(BackendClient.DEFAULT_CHAT_RETRY_BACKOFF)


def test_chat_backoff_grows_between_attempts():
    """A cold model reload takes ~15s; the schedule must not burn retries too early."""
    client, _, slept = _client_with([httpx.Response(500)] * 3)

    with pytest.raises(BackendConnectivityError):
        client.chat("msg")

    assert slept == [5.0, 15.0]
    assert slept[1] > slept[0]


def test_chat_does_not_retry_a_timeout():
    """A timed-out turn may still be in flight server-side — retrying double-writes."""
    client, transport, slept = _client_with(
        [httpx.ReadTimeout("timed out"), _ok_response()]
    )

    with pytest.raises(BackendConnectivityError) as exc_info:
        client.chat("msg")

    assert transport.request_count == 1
    assert slept == []
    assert "not retried" in str(exc_info.value)


def test_chat_retries_a_connect_error():
    """Nothing reached the backend, so no MU was written — safe to retry."""
    client, transport, _ = _client_with(
        [httpx.ConnectError("connection refused"), _ok_response()]
    )

    result = client.chat("msg")

    assert result["run_id"] == "run-1"
    assert transport.request_count == 2


def test_chat_does_not_retry_a_4xx():
    """Client errors are deterministic — a retry would fail identically."""
    client, transport, slept = _client_with([httpx.Response(422, text="bad payload")])

    with pytest.raises(BackendTurnError):
        client.chat("msg")

    assert transport.request_count == 1
    assert slept == []


def test_chat_succeeds_without_sleeping_when_the_first_attempt_works():
    client, transport, slept = _client_with([_ok_response()])

    result = client.chat("msg")

    assert result["run_id"] == "run-1"
    assert transport.request_count == 1
    assert slept == []


def test_retry_budget_is_configurable_to_zero_for_the_old_behaviour():
    client, transport, _ = _client_with([httpx.Response(500)], chat_max_retries=0)

    with pytest.raises(BackendConnectivityError):
        client.chat("msg")

    assert transport.request_count == 1


def test_backoff_schedule_shorter_than_budget_reuses_its_last_delay():
    """Defensive indexing: a 1-entry backoff with 2 retries must not IndexError."""
    client, transport, slept = _client_with(
        [httpx.Response(500)] * 3, chat_retry_backoff=(3.0,)
    )

    with pytest.raises(BackendConnectivityError):
        client.chat("msg")

    assert transport.request_count == 3
    assert slept == [3.0, 3.0]
