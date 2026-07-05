"""Tests for scripts/harness/docker_lifecycle.py — backend container
restart + health-poll, using fake subprocess/sleep/clock functions so no
real Docker or wall-clock waiting is needed (M19 Phase 3)."""

import os
import sys
from pathlib import Path

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "scripts"))

import pytest

from harness.client import BackendConnectivityError
from harness.docker_lifecycle import DockerLifecycleError, restart_backend, wait_for_health


class _FakeCompletedProcess:
    def __init__(self, returncode=0, stderr=""):
        self.returncode = returncode
        self.stderr = stderr


def test_restart_backend_invokes_docker_compose_up_force_recreate_with_env():
    calls = []

    def fake_runner(command, cwd, env, capture_output, text):
        calls.append({"command": command, "cwd": cwd, "env": env})
        return _FakeCompletedProcess(returncode=0)

    project_root = Path("/fake/project")
    restart_backend(project_root, {"EXPERIMENT_ID": "p1_learned"}, runner_fn=fake_runner)

    assert len(calls) == 1
    assert calls[0]["command"] == ["docker", "compose", "up", "-d", "--force-recreate", "backend"]
    assert calls[0]["cwd"] == str(project_root)
    assert calls[0]["env"]["EXPERIMENT_ID"] == "p1_learned"


def test_restart_backend_raises_on_nonzero_exit():
    def failing_runner(command, cwd, env, capture_output, text):
        return _FakeCompletedProcess(returncode=1, stderr="no such service: backend")

    with pytest.raises(DockerLifecycleError, match="docker compose up failed"):
        restart_backend(Path("/fake/project"), {"EXPERIMENT_ID": "x"}, runner_fn=failing_runner)


class _FakeHealthClient:
    def __init__(self, fail_n_times=0):
        self.fail_n_times = fail_n_times
        self.calls = 0

    def health(self):
        self.calls += 1
        if self.calls <= self.fail_n_times:
            raise BackendConnectivityError("not up yet")
        return {"status": "ok", "db": "ok"}


def test_wait_for_health_returns_once_healthy():
    client = _FakeHealthClient(fail_n_times=2)
    sleeps = []

    wait_for_health(
        client, timeout=10.0, poll_interval=1.0,
        sleep_fn=lambda s: sleeps.append(s),
        clock_fn=_make_fake_clock(),
    )

    assert client.calls == 3
    assert len(sleeps) == 2


def _make_fake_clock():
    """Fake monotonic clock that advances by 0.1s per read — fast tests,
    no real sleeping, but still exercises the timeout-comparison logic."""
    state = {"t": 0.0}

    def _clock():
        state["t"] += 0.1
        return state["t"]

    return _clock


def test_wait_for_health_raises_after_timeout():
    client = _FakeHealthClient(fail_n_times=10_000)  # never becomes healthy

    with pytest.raises(DockerLifecycleError, match="did not become healthy"):
        wait_for_health(
            client, timeout=1.0, poll_interval=1.0,
            sleep_fn=lambda s: None,
            clock_fn=_make_fake_clock(),
        )
