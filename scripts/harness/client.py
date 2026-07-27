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


class ReportGenerationError(Exception):
    """
    A series/experiment report call failed (M19 Phase 3).

    Deliberately not a BackendConnectivityError/BackendTurnError — report
    failures are governed by the dedicated error_policy["report_generation"]
    key (soft_continue by default) regardless of what the "connectivity"
    default is set to, since a broken report can be re-generated later but
    an aborted run cannot be re-run for free.
    """


class BackendClient:
    # Chat calls go through Ollama inference and can run well past a generic
    # HTTP timeout, especially on a cold model load — give them their own,
    # more generous budget (matches TIMEOUT_CHAT in tests/integration).
    DEFAULT_CHAT_TIMEOUT = 120.0

    # Report generation is a long aggregation, not a request/response call.
    # §6.1 alone issues one query per probe definition, twice over (mean and
    # sample rows since M30), each with a condition_label fallback pass — on
    # a 43-probe battery that is ~172 round-trips against the connection-per-
    # operation store. Measured at 55.7s on a near-empty database, i.e.
    # already past the 60s generic timeout before any real run's data exists.
    # Same reasoning as DEFAULT_CHAT_TIMEOUT: a call class whose honest
    # duration exceeds the generic budget gets its own, rather than having
    # every run soft-continue into a missing report.
    DEFAULT_REPORT_TIMEOUT = 600.0

    # Bulk graph writes (wipe, seed, randomize-edges) go one row at a time
    # through the connection-per-operation store, each insert paying a fresh
    # connection plus an fsync — on a Docker bind-mounted volume that is
    # ~0.3-1.0s per edge. Seeding seed_v3's 34 MUs + 64 edges was measured at
    # 63.8s on 2026-07-27: past the 60s generic timeout, while the write
    # itself completed successfully server-side. Since wipe/seed failures are
    # classified as connectivity, and connectivity defaults to hard_stop,
    # that combination aborts an entire multi-hour run over a graph that was
    # in fact seeded correctly. These calls get a budget matched to what they
    # actually cost.
    DEFAULT_GRAPH_OP_TIMEOUT = 600.0

    def __init__(
        self,
        base_url: str,
        timeout: float = 60.0,
        chat_timeout: float = DEFAULT_CHAT_TIMEOUT,
        report_timeout: float = DEFAULT_REPORT_TIMEOUT,
        graph_op_timeout: float = DEFAULT_GRAPH_OP_TIMEOUT,
    ):
        self._client = httpx.Client(base_url=base_url, timeout=timeout)
        self._chat_timeout = chat_timeout
        self._report_timeout = report_timeout
        self._graph_op_timeout = graph_op_timeout

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
            resp = self._client.post(
                "/admin/graph/wipe",
                json={"experiment_id": experiment_id},
                timeout=self._graph_op_timeout,
            )
            resp.raise_for_status()
            return resp.json()
        except httpx.HTTPError as exc:
            raise BackendConnectivityError(f"graph/wipe failed: {exc}") from exc

    def seed_graph(self, version: str = "v2") -> dict:
        try:
            resp = self._client.post(
                "/admin/seed-graph",
                json={"version": version},
                timeout=self._graph_op_timeout,
            )
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

    def run_probe(
        self,
        probe_id: str,
        measurement_session_index: int,
        condition_label: str,
        probe_set_version: str = "v2",
        series_id: Optional[str] = None,
        weight_mode: Optional[str] = None,
        battery_mode: str = "mean",
        battery_repeat_index: Optional[int] = None,
    ) -> dict:
        """POST /admin/run-probe (M21) — run one probe through the backend's
        read-only retrieval path and record a probe_results row. No MUs are
        created and no edges are written (the endpoint enforces this), so a
        probe turn never mutates the graph the way a /chat turn does.

        M30 — series_id lets the result be scoped to one series by
        series_report.py. weight_mode overrides whatever edge_update.
        weight_mode is currently committed in config.yaml for this call only
        (None preserves pre-M30 behavior); battery_mode/battery_repeat_index
        record which pass of the dual-mode battery this call belongs to."""
        body = {
            "probe_id": probe_id,
            "measurement_session_index": measurement_session_index,
            "condition_label": condition_label,
            "probe_set_version": probe_set_version,
            "series_id": series_id,
            "weight_mode": weight_mode,
            "battery_mode": battery_mode,
            "battery_repeat_index": battery_repeat_index,
        }
        try:
            resp = self._client.post("/admin/run-probe", json=body)
        except (httpx.ConnectError, httpx.TimeoutException, httpx.NetworkError) as exc:
            raise BackendConnectivityError(f"run-probe call failed (connectivity): {exc}") from exc
        except httpx.HTTPError as exc:
            raise BackendConnectivityError(f"run-probe call failed: {exc}") from exc

        if resp.status_code >= 500:
            raise BackendConnectivityError(
                f"run-probe failed with server error {resp.status_code}: {resp.text[:200]}"
            )
        if resp.status_code >= 400:
            raise BackendTurnError(
                f"run-probe failed with client error {resp.status_code}: {resp.text[:200]}"
            )

        try:
            data = resp.json()
        except Exception as exc:
            raise BackendTurnError(f"run-probe response was not valid JSON: {exc}") from exc

        if "result_id" not in data:
            raise BackendTurnError(f"run-probe response missing result_id: {data}")

        return data

    def post_verdict(
        self,
        run_id: str,
        verdict: str,
        condition_label: str,
        judge_source: str,
        apply_weight_update: bool,
        rationale: Optional[str] = None,
        criterion_triggered: Optional[str] = None,
        judge_model_name: Optional[str] = None,
        judge_temperature: Optional[float] = None,
        judge_pinned_snapshot: Optional[str] = None,
        prompt_id: Optional[str] = None,
    ) -> dict:
        """POST /feedback/verdict (M19 Phase 2) — record a judge or human-override
        verdict, applying the weight update only when apply_weight_update is True."""
        body = {
            "run_id": run_id,
            "verdict": verdict,
            "condition_label": condition_label,
            "judge_source": judge_source,
            "apply_weight_update": apply_weight_update,
            "rationale": rationale,
            "criterion_triggered": criterion_triggered,
            "judge_model_name": judge_model_name,
            "judge_temperature": judge_temperature,
            "judge_pinned_snapshot": judge_pinned_snapshot,
            "prompt_id": prompt_id,
        }
        try:
            resp = self._client.post("/feedback/verdict", json=body)
        except (httpx.ConnectError, httpx.TimeoutException, httpx.NetworkError) as exc:
            raise BackendConnectivityError(f"feedback/verdict call failed (connectivity): {exc}") from exc
        except httpx.HTTPError as exc:
            raise BackendConnectivityError(f"feedback/verdict call failed: {exc}") from exc

        if resp.status_code >= 500:
            raise BackendConnectivityError(
                f"feedback/verdict failed with server error {resp.status_code}: {resp.text[:200]}"
            )
        if resp.status_code >= 400:
            raise BackendTurnError(
                f"feedback/verdict failed with client error {resp.status_code}: {resp.text[:200]}"
            )

        try:
            data = resp.json()
        except Exception as exc:
            raise BackendTurnError(f"feedback/verdict response was not valid JSON: {exc}") from exc

        if "feedback_event_id" not in data:
            raise BackendTurnError(f"feedback/verdict response missing feedback_event_id: {data}")

        return data

    def generate_series_report(self, experiment_id: str, series_id: str) -> dict:
        """POST /admin/reports/generate-series (M13). Raises ReportGenerationError
        on any failure — never BackendConnectivityError/BackendTurnError, so the
        caller applies error_policy["report_generation"] instead of the generic
        connectivity/turn_ambiguous categories."""
        try:
            resp = self._client.post(
                "/admin/reports/generate-series",
                json={"experiment_id": experiment_id, "series_id": series_id},
                timeout=self._report_timeout,
            )
            resp.raise_for_status()
            return resp.json()
        except httpx.HTTPError as exc:
            raise ReportGenerationError(
                f"generate-series report failed (experiment_id={experiment_id!r} "
                f"series_id={series_id!r}): {exc}"
            ) from exc

    def generate_experiment_report(
        self, experiment_id: str, control_experiment_ids: Optional[dict] = None
    ) -> dict:
        """POST /admin/reports/generate-experiment (M14-16). See generate_series_report
        for why this raises ReportGenerationError rather than the generic categories."""
        try:
            resp = self._client.post(
                "/admin/reports/generate-experiment",
                json={
                    "experiment_id": experiment_id,
                    "control_experiment_ids": control_experiment_ids,
                },
                timeout=self._report_timeout,
            )
            resp.raise_for_status()
            return resp.json()
        except httpx.HTTPError as exc:
            raise ReportGenerationError(
                f"generate-experiment report failed (experiment_id={experiment_id!r}): {exc}"
            ) from exc

    def randomize_edges(self) -> dict:
        """POST /admin/edges/randomize — resets all edge weights to uniform
        random [0, 1]. Called at the start of each series in the
        random_weight control condition."""
        try:
            resp = self._client.post("/admin/edges/randomize", timeout=self._graph_op_timeout)
            resp.raise_for_status()
            return resp.json()
        except httpx.HTTPError as exc:
            raise BackendConnectivityError(f"edges/randomize failed: {exc}") from exc
