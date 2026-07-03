"""
Thin HTTP client for the harness to talk to the Dockerized backend.

Two exception types drive the error-handling policy:
  - BackendConnectivityError: connection failures, timeouts, or server-side
    (5xx) / DB-level failures. Hard-stop by default.
  - BackendTurnError: a single ambiguous or malformed per-turn result
    (4xx response, unparseable body, missing expected field). Soft-continue
    by default.
"""

import logging
from typing import Optional

import httpx

logger = logging.getLogger("harness.client")


class BackendConnectivityError(Exception):
    """Connection-level or server/DB failure — hard-stop by default."""


class BackendTurnError(Exception):
    """A single ambiguous/missing per-turn result — soft-continue by default."""


class BackendClient:
    # Chat calls go through Ollama inference and can run well past a generic
    # HTTP timeout, especially on a cold model load — give them their own,
    # more generous budget (matches TIMEOUT_CHAT in tests/integration).
    DEFAULT_CHAT_TIMEOUT = 120.0

    def __init__(self, base_url: str, timeout: float = 60.0, chat_timeout: float = DEFAULT_CHAT_TIMEOUT):
        self._client = httpx.Client(base_url=base_url, timeout=timeout)
        self._chat_timeout = chat_timeout

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> "BackendClient":
        return self

    def __exit__(self, *exc_info) -> None:
        self.close()

    def health(self) -> dict:
        try:
            resp = self._client.get("/health")
            resp.raise_for_status()
            data = resp.json()
        except httpx.HTTPError as exc:
            raise BackendConnectivityError(f"health check failed: {exc}") from exc
        if data.get("db") not in (None, "ok"):
            raise BackendConnectivityError(f"health check reports degraded DB: {data}")
        return data

    def wipe_graph(self, experiment_id: Optional[str] = None) -> dict:
        """Archive the current graph (keyed by experiment_id), then delete all MUs/edges."""
        try:
            resp = self._client.post("/admin/graph/wipe", json={"experiment_id": experiment_id})
            resp.raise_for_status()
            return resp.json()
        except httpx.HTTPError as exc:
            raise BackendConnectivityError(f"graph/wipe failed: {exc}") from exc

    def seed_graph(self, version: str = "v2") -> dict:
        try:
            resp = self._client.post("/admin/seed-graph", json={"version": version})
            resp.raise_for_status()
            return resp.json()
        except httpx.HTTPError as exc:
            raise BackendConnectivityError(f"seed-graph failed: {exc}") from exc

    def graph_stats(self) -> dict:
        try:
            resp = self._client.get("/admin/graph/stats")
            resp.raise_for_status()
            return resp.json()
        except httpx.HTTPError as exc:
            raise BackendConnectivityError(f"graph/stats failed: {exc}") from exc

    def chat(self, message: str, series_id: Optional[str] = None) -> dict:
        body: dict = {"message": message}
        if series_id:
            body["series_id"] = series_id

        try:
            resp = self._client.post("/chat", json=body, timeout=self._chat_timeout)
        except (httpx.ConnectError, httpx.TimeoutException, httpx.NetworkError) as exc:
            raise BackendConnectivityError(f"chat call failed (connectivity): {exc}") from exc
        except httpx.HTTPError as exc:
            raise BackendConnectivityError(f"chat call failed: {exc}") from exc

        if resp.status_code >= 500:
            raise BackendConnectivityError(
                f"chat call failed with server error {resp.status_code}: {resp.text[:200]}"
            )
        if resp.status_code >= 400:
            raise BackendTurnError(
                f"chat call failed with client error {resp.status_code}: {resp.text[:200]}"
            )

        try:
            data = resp.json()
        except Exception as exc:
            raise BackendTurnError(f"chat response was not valid JSON: {exc}") from exc

        if "run_id" not in data:
            raise BackendTurnError(f"chat response missing run_id: {data}")

        return data
