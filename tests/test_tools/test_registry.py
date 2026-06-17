"""
Tests for tools/registry.py

Covers:
  - dispatch to a registered tool returns ToolResult
  - dispatch to an unknown tool raises ToolNotFoundError
  - exceeding max_iterations raises ToolLoopError
  - get_all_schemas returns OpenAI-format list
"""

import sys
import os
import asyncio

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "backend"))

from tools.base import Tool, ToolResult
from tools.registry import ToolRegistry, ToolNotFoundError, ToolLoopError


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def run(coro):
    return asyncio.get_event_loop().run_until_complete(coro)


class _EchoTool(Tool):
    """Minimal test tool that echoes its 'message' param back."""

    @property
    def name(self) -> str:
        return "echo"

    @property
    def description(self) -> str:
        return "Echoes the message param."

    @property
    def parameters_schema(self) -> dict:
        return {
            "type": "object",
            "properties": {"message": {"type": "string"}},
            "required": ["message"],
        }

    async def execute(self, params: dict) -> ToolResult:
        return ToolResult(
            tool_name=self.name,
            params_used=params,
            output=params.get("message", ""),
        )


def _make_registry(max_iterations: int = 3) -> ToolRegistry:
    """Build a ToolRegistry with the echo tool pre-registered."""
    registry = ToolRegistry.__new__(ToolRegistry)
    registry._config = {}
    registry._max_iterations = max_iterations
    registry._tools = {"echo": _EchoTool()}
    return registry


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_dispatch_registered_tool_returns_tool_result():
    """Dispatching to a registered tool returns a ToolResult with output."""
    registry = _make_registry()
    result = run(registry.dispatch("echo", {"message": "hello world"}, iteration=0))

    print(f"ToolResult: tool_name={result.tool_name} output={result.output}")
    assert isinstance(result, ToolResult)
    assert result.tool_name == "echo"
    assert result.output == "hello world"
    assert result.error is None


def test_dispatch_unknown_tool_raises_tool_not_found_error():
    """Dispatching to an unregistered tool name raises ToolNotFoundError."""
    registry = _make_registry()
    with pytest.raises(ToolNotFoundError) as exc_info:
        run(registry.dispatch("nonexistent_tool", {}, iteration=0))

    print(f"ToolNotFoundError: {exc_info.value}")
    assert "nonexistent_tool" in str(exc_info.value)


def test_dispatch_raises_tool_loop_error_when_iteration_at_limit():
    """dispatch() with iteration >= max_iterations raises ToolLoopError."""
    registry = _make_registry(max_iterations=3)
    with pytest.raises(ToolLoopError) as exc_info:
        run(registry.dispatch("echo", {"message": "x"}, iteration=3))

    print(f"ToolLoopError: {exc_info.value}")
    assert "max_iterations" in str(exc_info.value)


def test_dispatch_raises_tool_loop_error_well_above_limit():
    """ToolLoopError is raised regardless of how far above the limit."""
    registry = _make_registry(max_iterations=2)
    with pytest.raises(ToolLoopError):
        run(registry.dispatch("echo", {"message": "x"}, iteration=10))


def test_dispatch_succeeds_at_iteration_below_limit():
    """dispatch succeeds when iteration is strictly less than max_iterations."""
    registry = _make_registry(max_iterations=5)
    result = run(registry.dispatch("echo", {"message": "ok"}, iteration=4))

    print(f"ToolResult at iteration=4: output={result.output}")
    assert result.output == "ok"


def test_get_all_schemas_returns_openai_format():
    """get_all_schemas() returns a list with correctly structured function definitions."""
    registry = _make_registry()
    schemas = registry.get_all_schemas()

    print(f"schemas: {schemas}")
    assert len(schemas) == 1
    schema = schemas[0]
    assert schema["type"] == "function"
    assert "function" in schema
    assert schema["function"]["name"] == "echo"
    assert "description" in schema["function"]
    assert "parameters" in schema["function"]


def test_is_registered_returns_true_for_known_tool():
    registry = _make_registry()
    assert registry.is_registered("echo") is True


def test_is_registered_returns_false_for_unknown_tool():
    registry = _make_registry()
    assert registry.is_registered("ghost") is False


def test_registry_loads_file_tool_from_config():
    """ToolRegistry can load FileTool from a config dict matching config.yaml structure."""
    config = {
        "hyperparameters": {"tool_loop_max_iterations": 5},
        "tools": {
            "registered": [
                {
                    "name": "file_read",
                    "module": "tools.file_tool",
                    "class": "FileTool",
                    "operation": "read",
                },
                {
                    "name": "file_write",
                    "module": "tools.file_tool",
                    "class": "FileTool",
                    "operation": "write",
                },
            ]
        },
    }
    registry = ToolRegistry(config=config)
    print(f"Registered tools: {list(registry._tools.keys())}")
    assert registry.is_registered("file_read")
    assert registry.is_registered("file_write")
    assert registry.max_iterations == 5
