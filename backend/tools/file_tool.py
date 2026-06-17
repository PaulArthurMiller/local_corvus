"""
FileTool — read and write files within /app/memory/workspace/.

Path traversal outside the workspace directory is rejected. Both operations
are registered separately in config.yaml so the LLM sees distinct tool names
("file_read" and "file_write") even though they share an implementation class.
"""

import logging
import os
import time
from pathlib import Path
from typing import Any

from tools.base import Tool, ToolResult

logger = logging.getLogger(__name__)

_WORKSPACE = Path(os.getenv("MEMORY_PATH", "/app/memory")) / "workspace"


def _safe_path(relative_path: str) -> Path:
    """
    Resolve relative_path under the workspace and reject traversal attempts.

    Raises ValueError if the resolved path escapes the workspace.
    """
    _WORKSPACE.mkdir(parents=True, exist_ok=True)
    resolved = (_WORKSPACE / relative_path).resolve()
    workspace_resolved = _WORKSPACE.resolve()
    if not str(resolved).startswith(str(workspace_resolved)):
        raise ValueError(
            f"path traversal rejected: '{relative_path}' resolves outside workspace"
        )
    return resolved


class FileTool(Tool):
    """
    Read or write files inside /app/memory/workspace/.

    The operation ("read" or "write") is bound at registration time via the
    config.yaml entry, so one class serves both file_read and file_write.
    """

    def __init__(self, operation: str) -> None:
        if operation not in ("read", "write"):
            raise ValueError(f"FileTool operation must be 'read' or 'write', got '{operation}'")
        self._operation = operation

    @property
    def name(self) -> str:
        return f"file_{self._operation}"

    @property
    def description(self) -> str:
        if self._operation == "read":
            return (
                "Read the contents of a file from the memory workspace. "
                "Provide the relative path from the workspace root."
            )
        return (
            "Write text content to a file in the memory workspace. "
            "Creates the file if it does not exist; overwrites if it does. "
            "Provide the relative path from the workspace root and the content to write."
        )

    @property
    def parameters_schema(self) -> dict:
        if self._operation == "read":
            return {
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string",
                        "description": "Relative path from the workspace root (e.g. 'notes.txt' or 'subdir/data.txt')",
                    }
                },
                "required": ["path"],
            }
        return {
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "Relative path from the workspace root (e.g. 'notes.txt' or 'subdir/data.txt')",
                },
                "content": {
                    "type": "string",
                    "description": "Text content to write to the file.",
                },
            },
            "required": ["path", "content"],
        }

    async def execute(self, params: dict[str, Any]) -> ToolResult:
        start = time.monotonic()
        tool_name = self.name

        try:
            if self._operation == "read":
                result = await self._read(params)
            else:
                result = await self._write(params)
        except Exception as exc:
            latency_ms = int((time.monotonic() - start) * 1000)
            logger.error("%s: error — %s", tool_name, exc)
            return ToolResult(
                tool_name=tool_name,
                params_used=params,
                error=str(exc),
                latency_ms=latency_ms,
            )

        latency_ms = int((time.monotonic() - start) * 1000)
        logger.info("%s: success latency_ms=%d", tool_name, latency_ms)
        return ToolResult(
            tool_name=tool_name,
            params_used=params,
            output=result,
            latency_ms=latency_ms,
        )

    async def _read(self, params: dict[str, Any]) -> str:
        path_str = params.get("path")
        if not path_str:
            raise ValueError("'path' parameter is required for file_read")
        resolved = _safe_path(path_str)
        if not resolved.exists():
            raise FileNotFoundError(f"file not found: '{path_str}'")
        content = resolved.read_text(encoding="utf-8")
        logger.info("file_read: read %d chars from '%s'", len(content), path_str)
        return content

    async def _write(self, params: dict[str, Any]) -> str:
        path_str = params.get("path")
        content = params.get("content")
        if not path_str:
            raise ValueError("'path' parameter is required for file_write")
        if content is None:
            raise ValueError("'content' parameter is required for file_write")
        resolved = _safe_path(path_str)
        resolved.parent.mkdir(parents=True, exist_ok=True)
        resolved.write_text(content, encoding="utf-8")
        logger.info("file_write: wrote %d chars to '%s'", len(content), path_str)
        return f"wrote {len(content)} characters to '{path_str}'"
