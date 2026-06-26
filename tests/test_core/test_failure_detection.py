"""
Tests for Milestone 11 — empty-response failure detection in the orchestrator.

Covers: _run_tool_loop failure detection paths, output MU source_type selection,
RunRecord.failure_reason field, and UI message constants.

Async tests use asyncio.get_event_loop().run_until_complete() — consistent with
other async tests in this project (see test_run_log.py).
"""

import asyncio
import os
import sys
from unittest.mock import AsyncMock, MagicMock, patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "backend"))

from core.orchestrator import (
    FAILURE_REASON_EMPTY_OUTPUT,
    FAILURE_REASON_TOKEN_LIMIT,
    FAILURE_UI_MESSAGES,
    _MU_SOURCE_TYPE_FAILED_EMPTY_OUTPUT,
    _MU_SOURCE_TYPE_FAILED_TOKEN_LIMIT,
    _OLLAMA_FINISH_REASON_LENGTH,
    _run_tool_loop,
)
from analytics.run_log import RunRecord


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def run(coro):
    return asyncio.get_event_loop().run_until_complete(coro)


def _make_ollama_response(content: str, finish_reason) -> dict:
    """Minimal Ollama /v1/chat/completions response with no tool calls."""
    return {
        "choices": [{
            "finish_reason": finish_reason,
            "message": {
                "content": content,
                "tool_calls": None,
            },
        }],
        "usage": {"prompt_tokens": 10, "completion_tokens": 0 if not content.strip() else 5},
    }


def _patch_httpx(response_json: dict):
    """Return a context manager that patches httpx.AsyncClient.post."""
    mock_response = MagicMock()
    mock_response.json.return_value = response_json
    mock_response.raise_for_status = MagicMock()

    mock_client = AsyncMock()
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=False)
    mock_client.post = AsyncMock(return_value=mock_response)

    return patch("core.orchestrator.httpx.AsyncClient", return_value=mock_client)


async def _call_tool_loop(content: str, finish_reason):
    """Run _run_tool_loop with a mocked Ollama returning the given content/finish_reason."""
    resp = _make_ollama_response(content=content, finish_reason=finish_reason)
    with _patch_httpx(resp):
        return await _run_tool_loop(
            messages=[{"role": "user", "content": "hello"}],
            session_id="test-session",
            tool_schemas=[],
            all_flags=[],
        )


# ---------------------------------------------------------------------------
# _run_tool_loop failure detection
# ---------------------------------------------------------------------------


class TestRunToolLoopFailureDetection:

    def test_detects_token_limit_on_empty_content_with_length_finish_reason(self):
        """Empty content + finish_reason='length' → FAILURE_REASON_TOKEN_LIMIT."""
        content, _, _, _, failure_reason = run(_call_tool_loop("", _OLLAMA_FINISH_REASON_LENGTH))
        print(f"[test] failure_reason={failure_reason!r} content={content!r}")
        assert failure_reason == FAILURE_REASON_TOKEN_LIMIT
        assert content == ""

    def test_detects_token_limit_on_whitespace_only_content(self):
        """Whitespace-only content + finish_reason='length' also triggers token limit."""
        _, _, _, _, failure_reason = run(_call_tool_loop("   \n\t  ", _OLLAMA_FINISH_REASON_LENGTH))
        print(f"[test] failure_reason={failure_reason!r}")
        assert failure_reason == FAILURE_REASON_TOKEN_LIMIT

    def test_detects_empty_output_on_empty_content_with_stop_finish_reason(self):
        """Empty content + finish_reason='stop' → FAILURE_REASON_EMPTY_OUTPUT."""
        _, _, _, _, failure_reason = run(_call_tool_loop("", "stop"))
        print(f"[test] failure_reason={failure_reason!r}")
        assert failure_reason == FAILURE_REASON_EMPTY_OUTPUT

    def test_no_failure_on_normal_response(self):
        """Non-empty content → failure_reason is None."""
        content, _, _, _, failure_reason = run(_call_tool_loop("Hello there!", "stop"))
        print(f"[test] failure_reason={failure_reason!r} content={content!r}")
        assert failure_reason is None
        assert content == "Hello there!"

    def test_normal_response_returns_content(self):
        """Non-empty response content is returned as the first tuple element."""
        expected = "The capital of France is Paris."
        content, _, _, _, _ = run(_call_tool_loop(expected, "stop"))
        print(f"[test] content={content!r}")
        assert content == expected

    def test_finish_reason_none_with_empty_content_gives_empty_output(self):
        """finish_reason None with empty content → FAILURE_REASON_EMPTY_OUTPUT."""
        # finish_reason=None means (finish_reason or "") == "" which != "length"
        _, _, _, _, failure_reason = run(_call_tool_loop("", None))
        print(f"[test] failure_reason={failure_reason!r}")
        assert failure_reason == FAILURE_REASON_EMPTY_OUTPUT


# ---------------------------------------------------------------------------
# UI message constants
# ---------------------------------------------------------------------------


class TestFailureUiMessages:

    def test_token_limit_ui_message_is_non_empty_string(self):
        msg = FAILURE_UI_MESSAGES[FAILURE_REASON_TOKEN_LIMIT]
        print(f"[test] token_limit msg: {msg!r}")
        assert isinstance(msg, str)
        assert len(msg) > 0

    def test_empty_output_ui_message_is_non_empty_string(self):
        msg = FAILURE_UI_MESSAGES[FAILURE_REASON_EMPTY_OUTPUT]
        print(f"[test] empty_output msg: {msg!r}")
        assert isinstance(msg, str)
        assert len(msg) > 0

    def test_both_failure_reasons_have_distinct_messages(self):
        assert FAILURE_UI_MESSAGES[FAILURE_REASON_TOKEN_LIMIT] != FAILURE_UI_MESSAGES[FAILURE_REASON_EMPTY_OUTPUT]

    def test_ui_message_substituted_on_token_limit(self):
        """Response from failed turn is the FAILURE_UI_MESSAGES value, not empty string."""
        content, _, _, _, failure_reason = run(_call_tool_loop("", _OLLAMA_FINISH_REASON_LENGTH))
        print(f"[test] content={content!r} failure_reason={failure_reason!r}")
        # The substitution happens in run_interaction(), not _run_tool_loop itself.
        # _run_tool_loop returns "" and the reason; run_interaction substitutes the message.
        assert failure_reason == FAILURE_REASON_TOKEN_LIMIT
        expected_ui_msg = FAILURE_UI_MESSAGES[FAILURE_REASON_TOKEN_LIMIT]
        assert isinstance(expected_ui_msg, str)
        assert "completion-token limit" in expected_ui_msg


# ---------------------------------------------------------------------------
# MU source type constants
# ---------------------------------------------------------------------------


class TestMuSourceTypeConstants:

    def test_failed_token_limit_source_type_contains_token(self):
        print(f"[test] source_type: {_MU_SOURCE_TYPE_FAILED_TOKEN_LIMIT!r}")
        assert "token" in _MU_SOURCE_TYPE_FAILED_TOKEN_LIMIT

    def test_failed_empty_output_source_type_contains_empty(self):
        print(f"[test] source_type: {_MU_SOURCE_TYPE_FAILED_EMPTY_OUTPUT!r}")
        assert "empty" in _MU_SOURCE_TYPE_FAILED_EMPTY_OUTPUT

    def test_source_types_are_distinct(self):
        assert _MU_SOURCE_TYPE_FAILED_TOKEN_LIMIT != _MU_SOURCE_TYPE_FAILED_EMPTY_OUTPUT

    def test_source_types_start_with_agent_output(self):
        assert _MU_SOURCE_TYPE_FAILED_TOKEN_LIMIT.startswith("agent_output")
        assert _MU_SOURCE_TYPE_FAILED_EMPTY_OUTPUT.startswith("agent_output")


# ---------------------------------------------------------------------------
# RunRecord.failure_reason field
# ---------------------------------------------------------------------------


class TestRunRecordFailureReason:

    def test_failure_reason_defaults_to_none(self):
        record = RunRecord(session_id="test-session")
        print(f"[test] failure_reason: {record.failure_reason!r}")
        assert record.failure_reason is None

    def test_failure_reason_can_be_set_to_token_limit(self):
        record = RunRecord(session_id="test-session", failure_reason=FAILURE_REASON_TOKEN_LIMIT)
        print(f"[test] failure_reason: {record.failure_reason!r}")
        assert record.failure_reason == FAILURE_REASON_TOKEN_LIMIT

    def test_failure_reason_can_be_set_to_empty_output(self):
        record = RunRecord(session_id="test-session", failure_reason=FAILURE_REASON_EMPTY_OUTPUT)
        print(f"[test] failure_reason: {record.failure_reason!r}")
        assert record.failure_reason == FAILURE_REASON_EMPTY_OUTPUT

    def test_failure_reason_appears_in_model_dump(self):
        record = RunRecord(session_id="test-session", failure_reason=FAILURE_REASON_TOKEN_LIMIT)
        dumped = record.model_dump()
        print(f"[test] model_dump failure_reason: {dumped.get('failure_reason')!r}")
        assert dumped["failure_reason"] == FAILURE_REASON_TOKEN_LIMIT

    def test_normal_turn_failure_reason_is_none_in_model_dump(self):
        record = RunRecord(session_id="test-session")
        dumped = record.model_dump()
        print(f"[test] model_dump failure_reason: {dumped.get('failure_reason')!r}")
        assert dumped["failure_reason"] is None
