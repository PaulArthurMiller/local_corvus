"""
Backend container lifecycle control (M19 Phase 3).

EXPERIMENT_ID is resolved once at backend process startup (see
backend/core/orchestrator.py: _resolve_experiment_id — env var -> config.yaml
-> auto-generated) and cached as a module-level constant for the life of
the process; there is no per-request override. Since the harness must
stamp each condition (learned/static_weight/random_weight) with its own
experiment_id so the M13-16 report generators query disjoint data, the
only way to change it between conditions is to recreate the backend
container with a new EXPERIMENT_ID env var. This module does exactly that
and nothing else — it does not manage the frontend/admin services or any
other part of the Docker stack's lifecycle.

Verified confirmed gap (M19 Phase 3 planning): docker-compose.yml passes
EXPERIMENT_ID=${EXPERIMENT_ID:-} through to the backend service; /chat and
/admin/graph/wipe accept no per-request experiment_id override that
changes what subsequent runs/MUs are stamped with. Restarting the
container is the only way to change it without a backend code change.
"""

import logging
import os
import subprocess
import time
from pathlib import Path
from typing import Callable, Optional

from harness.client import BackendClient, BackendConnectivityError

logger = logging.getLogger("harness.docker_lifecycle")

BACKEND_SERVICE_NAME = "backend"
DEFAULT_RESTART_TIMEOUT_SECONDS = 90.0
DEFAULT_HEALTH_POLL_INTERVAL_SECONDS = 2.0


class DockerLifecycleError(Exception):
    """The backend container could not be restarted, or did not become
    healthy again within the timeout."""


def restart_backend(
    project_root: Path,
    env_overrides: dict,
    runner_fn: Callable[..., "subprocess.CompletedProcess"] = subprocess.run,
) -> None:
    """
    Recreate the backend service with `env_overrides` merged into the
    current environment, so docker compose's ${VAR:-} interpolation picks
    up the new values. --force-recreate guarantees a fresh process (and
    therefore a freshly re-resolved EXPERIMENT_ID) even if compose's own
    service-diffing would otherwise consider the definition unchanged.
    """
    env = {**os.environ, **{k: str(v) for k, v in env_overrides.items()}}
    command = ["docker", "compose", "up", "-d", "--force-recreate", BACKEND_SERVICE_NAME]
    logger.info(
        "docker_lifecycle: restarting %s with env overrides %s",
        BACKEND_SERVICE_NAME, list(env_overrides.keys()),
    )
    result = runner_fn(command, cwd=str(project_root), env=env, capture_output=True, text=True)
    if result.returncode != 0:
        raise DockerLifecycleError(
            f"docker compose up failed (exit {result.returncode}): "
            f"{(result.stderr or '').strip()[:500]}"
        )


def wait_for_health(
    client: BackendClient,
    timeout: float = DEFAULT_RESTART_TIMEOUT_SECONDS,
    poll_interval: float = DEFAULT_HEALTH_POLL_INTERVAL_SECONDS,
    sleep_fn: Callable[[float], None] = time.sleep,
    clock_fn: Callable[[], float] = time.monotonic,
) -> None:
    """Poll client.health() until it succeeds or `timeout` seconds elapse."""
    deadline = clock_fn() + timeout
    last_error: Optional[Exception] = None
    while clock_fn() < deadline:
        try:
            client.health()
            return
        except BackendConnectivityError as exc:
            last_error = exc
            sleep_fn(poll_interval)
    raise DockerLifecycleError(
        f"backend did not become healthy within {timeout}s after restart: {last_error}"
    )
