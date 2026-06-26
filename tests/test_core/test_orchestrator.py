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

from core.orchestrator import _build_ollama_messages, _scrub_prompt_for_storage
from analytics.run_log import RunRecord


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


# ---------------------------------------------------------------------------
# Milestone 8 — _scrub_prompt_for_storage
# ---------------------------------------------------------------------------


class TestScrubPromptForStorage:

    def test_plain_text_messages_pass_through_unchanged(self):
        """Messages with no image content-parts are returned as-is."""
        messages = [
            {"role": "system", "content": "context here"},
            {"role": "user", "content": "hello"},
        ]
        scrubbed = _scrub_prompt_for_storage(messages)
        print(f"[test] scrubbed plain messages: {scrubbed}")
        assert scrubbed[0]["content"] == "context here"
        assert scrubbed[1]["content"] == "hello"

    def test_base64_image_data_replaced_with_placeholder(self):
        """base64 data in image_url content-parts is replaced by a readable placeholder."""
        import base64
        fake_bytes = b"\xff\xd8\xff" + b"X" * 100  # fake JPEG magic + padding
        b64 = base64.b64encode(fake_bytes).decode()
        messages = [
            {
                "role": "user",
                "content": [
                    {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{b64}"}},
                    {"type": "text", "text": "What do you see?"},
                ],
            }
        ]
        scrubbed = _scrub_prompt_for_storage(messages)
        image_part = scrubbed[0]["content"][0]
        url = image_part["image_url"]["url"]
        print(f"[test] scrubbed image url: {url}")
        assert url.startswith("[image: image/jpeg")
        assert "KB]" in url
        assert b64 not in url

    def test_text_parts_in_content_list_pass_through_unchanged(self):
        """Text content-parts within a list are not modified."""
        messages = [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": "hello there"},
                ],
            }
        ]
        scrubbed = _scrub_prompt_for_storage(messages)
        text_part = scrubbed[0]["content"][0]
        print(f"[test] text part: {text_part}")
        assert text_part["text"] == "hello there"

    def test_original_messages_list_is_not_mutated(self):
        """The original messages list must not be modified in-place."""
        import base64
        fake_bytes = b"\xff\xd8\xff" + b"Y" * 50
        b64 = base64.b64encode(fake_bytes).decode()
        original_url = f"data:image/jpeg;base64,{b64}"
        messages = [
            {
                "role": "user",
                "content": [
                    {"type": "image_url", "image_url": {"url": original_url}},
                ],
            }
        ]
        _scrub_prompt_for_storage(messages)
        # The original must still have the full data URI
        assert messages[0]["content"][0]["image_url"]["url"] == original_url

    def test_empty_messages_list_returns_empty_list(self):
        assert _scrub_prompt_for_storage([]) == []


# ---------------------------------------------------------------------------
# Milestone 10 — Session context flag
# ---------------------------------------------------------------------------


class TestSessionContextFlag:

    def test_session_context_excluded_when_history_empty(self):
        """Passing an empty history list (flag=False path) produces no history turns."""
        messages = _build_ollama_messages(
            condensed_context=None,
            history=[],
            user_content="hello",
        )
        print(f"[test] messages with empty history: {messages}")
        # Only the final user message should be present
        assert len(messages) == 1
        assert messages[0]["role"] == "user"
        assert messages[0]["content"] == "hello"

    def test_session_context_included_when_history_provided(self):
        """Passing a non-empty history list produces those turns in the messages array."""
        history = [
            {"role": "user", "content": "turn 1 question"},
            {"role": "assistant", "content": "turn 1 answer"},
        ]
        messages = _build_ollama_messages(
            condensed_context=None,
            history=history,
            user_content="turn 2 question",
        )
        print(f"[test] messages with history: {messages}")
        # [user(turn1), assistant(turn1), user(turn2)]
        assert len(messages) == 3
        assert messages[0]["role"] == "user"
        assert messages[0]["content"] == "turn 1 question"
        assert messages[1]["role"] == "assistant"
        assert messages[1]["content"] == "turn 1 answer"
        assert messages[2]["role"] == "user"
        assert messages[2]["content"] == "turn 2 question"

    def test_use_session_context_field_defaults_false_in_run_record(self):
        """RunRecord.use_session_context defaults to False."""
        record = RunRecord(series_id="test-session")
        print(f"[test] use_session_context: {record.use_session_context}")
        assert record.model_dump()["use_session_context"] is False

    def test_use_session_context_field_can_be_set_true_in_run_record(self):
        """RunRecord.use_session_context can be set to True."""
        record = RunRecord(series_id="test-session", use_session_context=True)
        print(f"[test] use_session_context: {record.use_session_context}")
        assert record.model_dump()["use_session_context"] is True


# ---------------------------------------------------------------------------
# Milestone 12 — EXPERIMENT_ID constant and RunRecord fields
# ---------------------------------------------------------------------------


class TestMilestone12Orchestrator:

    def test_experiment_id_module_constant_is_non_empty_string(self):
        """EXPERIMENT_ID is resolved at import time and is a non-empty string."""
        from core.orchestrator import EXPERIMENT_ID
        print(f"[test] EXPERIMENT_ID={EXPERIMENT_ID!r}")
        assert isinstance(EXPERIMENT_ID, str)
        assert len(EXPERIMENT_ID) > 0

    def test_run_record_series_id_field_exists_and_accepts_value(self):
        """RunRecord has a series_id field that stores the provided value."""
        import uuid
        sid = str(uuid.uuid4())
        record = RunRecord(series_id=sid)
        print(f"[test] series_id={record.series_id!r}")
        assert record.series_id == sid

    def test_run_record_experiment_id_field_exists_and_accepts_value(self):
        """RunRecord has an experiment_id field that stores the provided value."""
        import uuid
        record = RunRecord(series_id=str(uuid.uuid4()), experiment_id="learning-2026-06-26")
        print(f"[test] experiment_id={record.experiment_id!r}")
        assert record.experiment_id == "learning-2026-06-26"

    def test_run_record_experiment_id_matches_module_constant(self):
        """A RunRecord built with the module EXPERIMENT_ID constant stores it correctly."""
        import uuid
        from core.orchestrator import EXPERIMENT_ID
        record = RunRecord(series_id=str(uuid.uuid4()), experiment_id=EXPERIMENT_ID)
        print(f"[test] record.experiment_id={record.experiment_id!r}")
        assert record.experiment_id == EXPERIMENT_ID
