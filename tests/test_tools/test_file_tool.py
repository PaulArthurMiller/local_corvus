"""
Tests for tools/file_tool.py

Covers:
  - file_write creates a file with the correct content
  - file_read returns the content of an existing file
  - file_read raises an error for a missing file
  - path traversal is rejected for both operations
  - file_write creates parent directories as needed
"""

import sys
import os
import asyncio
import tempfile
from pathlib import Path
from unittest import mock

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "backend"))

import tools.file_tool as ft_module
from tools.file_tool import FileTool


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def run(coro):
    return asyncio.get_event_loop().run_until_complete(coro)


class TempWorkspace:
    """Context manager that patches _WORKSPACE to a temp directory."""

    def __enter__(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        self._workspace = Path(self._tmpdir.name) / "workspace"
        self._workspace.mkdir()
        self._patcher = mock.patch.object(ft_module, "_WORKSPACE", self._workspace)
        self._patcher.start()
        return self._workspace

    def __exit__(self, *args):
        self._patcher.stop()
        self._tmpdir.cleanup()


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_file_write_creates_file_with_correct_content():
    """file_write writes the given content to the workspace file."""
    tool = FileTool(operation="write")
    with TempWorkspace() as ws:
        result = run(tool.execute({"path": "notes.txt", "content": "hello world"}))
        print(f"write result: {result}")
        assert result.error is None
        assert result.output is not None
        assert (ws / "notes.txt").read_text() == "hello world"


def test_file_read_returns_file_content():
    """file_read returns the content of an existing workspace file."""
    tool_w = FileTool(operation="write")
    tool_r = FileTool(operation="read")
    with TempWorkspace() as ws:
        (ws / "fact.txt").write_text("the sky is blue", encoding="utf-8")
        result = run(tool_r.execute({"path": "fact.txt"}))
        print(f"read result: {result}")
        assert result.error is None
        assert result.output == "the sky is blue"


def test_file_read_returns_error_for_missing_file():
    """file_read returns a ToolResult with error field for a non-existent file."""
    tool = FileTool(operation="read")
    with TempWorkspace():
        result = run(tool.execute({"path": "does_not_exist.txt"}))
        print(f"missing file result: {result}")
        assert result.error is not None
        assert result.output is None
        assert "not found" in result.error.lower() or "no such" in result.error.lower()


def test_file_write_creates_parent_directories():
    """file_write creates intermediate directories when path includes subdirectories."""
    tool = FileTool(operation="write")
    with TempWorkspace() as ws:
        result = run(tool.execute({"path": "subdir/nested/data.txt", "content": "deep"}))
        print(f"nested write result: {result}")
        assert result.error is None
        assert (ws / "subdir" / "nested" / "data.txt").read_text() == "deep"


def test_file_write_path_traversal_returns_error():
    """file_write rejects paths that would escape the workspace."""
    tool = FileTool(operation="write")
    with TempWorkspace():
        result = run(tool.execute({"path": "../../etc/passwd", "content": "x"}))
        print(f"traversal write result: {result}")
        assert result.error is not None
        assert result.output is None


def test_file_read_path_traversal_returns_error():
    """file_read rejects paths that would escape the workspace."""
    tool = FileTool(operation="read")
    with TempWorkspace():
        result = run(tool.execute({"path": "../../../etc/hostname"}))
        print(f"traversal read result: {result}")
        assert result.error is not None
        assert result.output is None


def test_file_write_overwrites_existing_file():
    """file_write replaces the content of an existing file."""
    tool = FileTool(operation="write")
    with TempWorkspace() as ws:
        run(tool.execute({"path": "overwrite.txt", "content": "original"}))
        run(tool.execute({"path": "overwrite.txt", "content": "updated"}))
        assert (ws / "overwrite.txt").read_text() == "updated"


def test_file_tool_name_matches_operation():
    """Tool name is file_read or file_write depending on operation."""
    assert FileTool(operation="read").name == "file_read"
    assert FileTool(operation="write").name == "file_write"


def test_file_tool_invalid_operation_raises():
    """FileTool raises ValueError for an invalid operation."""
    with pytest.raises(ValueError):
        FileTool(operation="delete")


def test_file_write_missing_path_param_returns_error():
    """file_write returns error when 'path' param is absent."""
    tool = FileTool(operation="write")
    with TempWorkspace():
        result = run(tool.execute({"content": "no path given"}))
        print(f"missing path result: {result}")
        assert result.error is not None


def test_file_write_missing_content_param_returns_error():
    """file_write returns error when 'content' param is absent."""
    tool = FileTool(operation="write")
    with TempWorkspace():
        result = run(tool.execute({"path": "somefile.txt"}))
        print(f"missing content result: {result}")
        assert result.error is not None
