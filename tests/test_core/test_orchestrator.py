"""
Tests for core/orchestrator.py — message construction logic.

Covers: _build_ollama_messages() which assembles the messages array sent to
Ollama on each turn. The key invariant: retrieved memory context appears once
in the system role only; the user role carries only the clean user input.
"""

import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "backend"))

from core.orchestrator import _build_ollama_messages


# ---------------------------------------------------------------------------
# _build_ollama_messages — context present
# ---------------------------------------------------------------------------


class TestBuildOllamaMessagesWithContext:

    def test_system_message_is_first_when_context_present(self):
        messages = _build_ollama_messages(
            condensed_context="some retrieved fact",
            history=[],
            user_content="what is the capital of France?",
        )
        print(f"[test] messages: {messages}")
        assert messages[0]["role"] == "system"

    def test_system_message_contains_context_label(self):
        messages = _build_ollama_messages(
            condensed_context="water boils at 100 degrees",
            history=[],
            user_content="what temperature does water boil?",
        )
        print(f"[test] system content: {messages[0]['content']}")
        assert "Context from memory" in messages[0]["content"]

    def test_system_message_contains_the_context_text(self):
        context = "water boils at 100 degrees"
        messages = _build_ollama_messages(
            condensed_context=context,
            history=[],
            user_content="what temperature does water boil?",
        )
        assert context in messages[0]["content"]

    def test_user_message_is_last(self):
        user_input = "what is the capital of France?"
        messages = _build_ollama_messages(
            condensed_context="some context",
            history=[],
            user_content=user_input,
        )
        print(f"[test] last message: {messages[-1]}")
        assert messages[-1]["role"] == "user"
        assert messages[-1]["content"] == user_input

    def test_user_message_does_not_contain_context(self):
        """Context must not appear in the user role — it belongs in system only."""
        messages = _build_ollama_messages(
            condensed_context="some retrieved fact",
            history=[],
            user_content="what is the capital of France?",
        )
        user_content = messages[-1]["content"]
        print(f"[test] user content: {user_content!r}")
        assert "Context from memory" not in user_content
        assert "some retrieved fact" not in user_content

    def test_history_inserted_between_system_and_user(self):
        history = [
            {"role": "user", "content": "hi"},
            {"role": "assistant", "content": "hello"},
        ]
        messages = _build_ollama_messages(
            condensed_context="background info",
            history=history,
            user_content="follow up question",
        )
        print(f"[test] messages: {messages}")
        # [system, user(hi), assistant(hello), user(follow up)]
        assert len(messages) == 4
        assert messages[0]["role"] == "system"
        assert messages[1]["role"] == "user"
        assert messages[1]["content"] == "hi"
        assert messages[2]["role"] == "assistant"
        assert messages[3]["role"] == "user"
        assert messages[3]["content"] == "follow up question"

    def test_system_message_is_only_one(self):
        messages = _build_ollama_messages(
            condensed_context="some context",
            history=[{"role": "user", "content": "prev"}, {"role": "assistant", "content": "ans"}],
            user_content="new question",
        )
        system_messages = [m for m in messages if m["role"] == "system"]
        print(f"[test] system messages: {system_messages}")
        assert len(system_messages) == 1


# ---------------------------------------------------------------------------
# _build_ollama_messages — no context
# ---------------------------------------------------------------------------


class TestBuildOllamaMessagesWithoutContext:

    def test_no_system_message_when_no_context(self):
        messages = _build_ollama_messages(
            condensed_context=None,
            history=[],
            user_content="hello",
        )
        print(f"[test] messages: {messages}")
        roles = [m["role"] for m in messages]
        assert "system" not in roles

    def test_only_user_message_when_no_context_and_no_history(self):
        messages = _build_ollama_messages(
            condensed_context=None,
            history=[],
            user_content="hello",
        )
        assert len(messages) == 1
        assert messages[0]["role"] == "user"
        assert messages[0]["content"] == "hello"

    def test_empty_string_context_treated_as_no_context(self):
        messages = _build_ollama_messages(
            condensed_context="",
            history=[],
            user_content="hello",
        )
        print(f"[test] messages: {messages}")
        roles = [m["role"] for m in messages]
        assert "system" not in roles

    def test_history_plus_user_when_no_context(self):
        history = [
            {"role": "user", "content": "first"},
            {"role": "assistant", "content": "response"},
        ]
        messages = _build_ollama_messages(
            condensed_context=None,
            history=history,
            user_content="second",
        )
        print(f"[test] messages: {messages}")
        assert len(messages) == 3
        assert messages[0]["role"] == "user"
        assert messages[0]["content"] == "first"
        assert messages[1]["role"] == "assistant"
        assert messages[2]["role"] == "user"
        assert messages[2]["content"] == "second"
