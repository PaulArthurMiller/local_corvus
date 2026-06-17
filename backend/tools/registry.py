"""
ToolRegistry — config-driven registration and dispatch for all tools.

Reads the `tools.registered` list from config.yaml at startup. The
orchestrator calls dispatch(); the registry finds the right tool, executes it,
and returns a ToolResult. The orchestrator never imports tool files directly.

Errors raised here are part of the public contract:
  ToolNotFoundError  — dispatch called with an unknown tool name
  ToolLoopError      — tool loop exceeded tool_loop_max_iterations
"""

import importlib
import logging
import os
from typing import Any

import yaml

from tools.base import Tool, ToolResult

logger = logging.getLogger(__name__)

_CONFIG_PATH = os.path.join(os.path.dirname(__file__), "..", "..", "config.yaml")


class ToolNotFoundError(Exception):
    """Raised when dispatch() is called with an unregistered tool name."""


class ToolLoopError(Exception):
    """Raised when the tool call loop exceeds the configured maximum iterations."""


class ToolRegistry:
    """
    Loads tools from config.yaml and dispatches calls by name.

    Tool instances are created once at load time and reused — they must be
    stateless across calls.
    """

    def __init__(self, config: dict | None = None) -> None:
        if config is None:
            config = self._load_config()
        self._config = config
        self._max_iterations: int = int(
            config.get("hyperparameters", {}).get("tool_loop_max_iterations", 5)
        )
        self._tools: dict[str, Tool] = {}
        self._load_tools(config)
        logger.info(
            "registry: loaded %d tool(s): %s",
            len(self._tools),
            list(self._tools.keys()),
        )

    # -----------------------------------------------------------------------
    # Public API
    # -----------------------------------------------------------------------

    @property
    def max_iterations(self) -> int:
        return self._max_iterations

    def get_all_schemas(self) -> list[dict]:
        """Return OpenAI-compatible function schemas for all registered tools."""
        return [tool.to_openai_schema() for tool in self._tools.values()]

    async def dispatch(
        self,
        tool_name: str,
        params: dict[str, Any],
        iteration: int = 0,
    ) -> ToolResult:
        """
        Dispatch a tool call by name.

        iteration is the 0-indexed call number within the current tool loop.
        Raises ToolNotFoundError if tool_name is not registered.
        Raises ToolLoopError if iteration >= max_iterations.
        """
        if iteration >= self._max_iterations:
            logger.error(
                "registry: tool_loop_max_iterations=%d exceeded at iteration=%d",
                self._max_iterations,
                iteration,
            )
            raise ToolLoopError(
                f"tool loop exceeded max_iterations={self._max_iterations}"
            )

        if tool_name not in self._tools:
            logger.error("registry: unknown tool '%s'", tool_name)
            raise ToolNotFoundError(f"tool '{tool_name}' is not registered")

        tool = self._tools[tool_name]
        logger.info(
            "registry: dispatching tool='%s' iteration=%d params=%s",
            tool_name,
            iteration,
            {k: str(v)[:80] for k, v in params.items()},
        )
        result = await tool.execute(params)
        logger.info(
            "registry: tool='%s' completed error=%s latency_ms=%d",
            tool_name,
            result.error,
            result.latency_ms,
        )
        return result

    def is_registered(self, tool_name: str) -> bool:
        return tool_name in self._tools

    # -----------------------------------------------------------------------
    # Internal loading
    # -----------------------------------------------------------------------

    def _load_tools(self, config: dict) -> None:
        tool_entries = config.get("tools", {}).get("registered", [])
        for entry in tool_entries:
            name = entry.get("name")
            module_path = entry.get("module")
            class_name = entry.get("class")
            operation = entry.get("operation")

            if not (name and module_path and class_name):
                logger.warning("registry: skipping incomplete tool entry: %s", entry)
                continue

            try:
                module = importlib.import_module(module_path)
                cls = getattr(module, class_name)
                instance: Tool = cls(operation=operation) if operation else cls()
                self._tools[name] = instance
                logger.debug("registry: registered tool='%s' from %s.%s", name, module_path, class_name)
            except Exception as exc:
                logger.error(
                    "registry: failed to load tool '%s' from %s.%s — %s",
                    name,
                    module_path,
                    class_name,
                    exc,
                )

    @staticmethod
    def _load_config() -> dict:
        try:
            with open(_CONFIG_PATH, "r", encoding="utf-8") as f:
                return yaml.safe_load(f)
        except Exception as exc:
            logger.warning("registry: could not load config.yaml — %s", exc)
            return {}
