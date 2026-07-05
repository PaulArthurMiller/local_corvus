"""
Tests for core/session.py

Covers: session creation, message history accumulation,
        get_session_messages, append_session_message, unknown session guards.
"""

import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "backend"))

import core.session as session_module
from core.session import (
    Session,
    append_session_message,
    create_session,
    get_or_create_session,
    get_session,
    get_session_messages,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _fresh_registry(monkeypatch):
    """Replace the module-level _sessions dict with an empty one for each test."""
    monkeypatch.setattr(session_module, "_sessions", {})


# ---------------------------------------------------------------------------
# Session creation
# ---------------------------------------------------------------------------


class TestCreateSession:

    def test_create_session_returns_string_id(self, monkeypatch):
        _fresh_registry(monkeypatch)
        sid = create_session()
        print(f"[test] created session_id={sid}")
        assert isinstance(sid, str)
        assert len(sid) == 36  # UUID format

    def test_create_session_is_retrievable(self, monkeypatch):
        _fresh_registry(monkeypatch)
        sid = create_session()
        session = get_session(sid)
        print(f"[test] retrieved: {session}")
        assert session is not None
        assert session.session_id == sid

    def test_create_session_starts_with_empty_messages(self, monkeypatch):
        _fresh_registry(monkeypatch)
        sid = create_session()
        session = get_session(sid)
        print(f"[test] initial messages: {session.messages}")
        assert session.messages == []

    def test_create_session_each_call_returns_unique_id(self, monkeypatch):
        _fresh_registry(monkeypatch)
        sid1 = create_session()
        sid2 = create_session()
        print(f"[test] sid1={sid1} sid2={sid2}")
        assert sid1 != sid2

    def test_get_session_returns_none_for_unknown_id(self, monkeypatch):
        _fresh_registry(monkeypatch)
        result = get_session("does-not-exist")
        print(f"[test] missing session: {result}")
        assert result is None


# ---------------------------------------------------------------------------
# Message history
# ---------------------------------------------------------------------------


class TestSessionMessages:

    def test_append_session_message_user_turn(self, monkeypatch):
        _fresh_registry(monkeypatch)
        sid = create_session()
        result = append_session_message(sid, {"role": "user", "content": "Hello"})
        print(f"[test] append result: {result}")
        assert result is True
        messages = get_session_messages(sid)
        assert len(messages) == 1
        assert messages[0] == {"role": "user", "content": "Hello"}

    def test_append_session_message_full_turn_sequence(self, monkeypatch):
        _fresh_registry(monkeypatch)
        sid = create_session()
        append_session_message(sid, {"role": "user", "content": "What is 2+2?"})
        append_session_message(sid, {"role": "assistant", "content": "4"})
        append_session_message(sid, {"role": "user", "content": "And 3+3?"})
        append_session_message(sid, {"role": "assistant", "content": "6"})

        messages = get_session_messages(sid)
        print(f"[test] 2-turn history: {messages}")
        assert len(messages) == 4
        assert messages[0]["role"] == "user"
        assert messages[1]["role"] == "assistant"
        assert messages[2]["role"] == "user"
        assert messages[3]["role"] == "assistant"

    def test_get_session_messages_returns_copy_not_reference(self, monkeypatch):
        """Mutating the returned list must not affect the stored history."""
        _fresh_registry(monkeypatch)
        sid = create_session()
        append_session_message(sid, {"role": "user", "content": "Hi"})

        messages = get_session_messages(sid)
        messages.append({"role": "user", "content": "injected"})

        stored = get_session_messages(sid)
        print(f"[test] stored after external mutation: {stored}")
        assert len(stored) == 1  # original unchanged

    def test_get_session_messages_unknown_session_returns_empty_list(self, monkeypatch):
        _fresh_registry(monkeypatch)
        result = get_session_messages("no-such-session")
        print(f"[test] unknown session messages: {result}")
        assert result == []

    def test_append_to_unknown_session_returns_false(self, monkeypatch):
        _fresh_registry(monkeypatch)
        result = append_session_message("no-such-session", {"role": "user", "content": "x"})
        print(f"[test] append to unknown: {result}")
        assert result is False

    def test_messages_are_isolated_between_sessions(self, monkeypatch):
        _fresh_registry(monkeypatch)
        sid1 = create_session()
        sid2 = create_session()

        append_session_message(sid1, {"role": "user", "content": "session 1 message"})
        append_session_message(sid2, {"role": "user", "content": "session 2 message"})

        msgs1 = get_session_messages(sid1)
        msgs2 = get_session_messages(sid2)

        print(f"[test] sid1 messages: {msgs1}")
        print(f"[test] sid2 messages: {msgs2}")
        assert len(msgs1) == 1
        assert msgs1[0]["content"] == "session 1 message"
        assert len(msgs2) == 1
        assert msgs2[0]["content"] == "session 2 message"

    def test_message_order_is_preserved(self, monkeypatch):
        _fresh_registry(monkeypatch)
        sid = create_session()
        for i in range(5):
            append_session_message(sid, {"role": "user", "content": f"message {i}"})

        messages = get_session_messages(sid)
        print(f"[test] ordered messages: {[m['content'] for m in messages]}")
        for i, msg in enumerate(messages):
            assert msg["content"] == f"message {i}"


# ---------------------------------------------------------------------------
# get_or_create_session (M19 Phase 3) — lets a caller supply its own
# deterministic series_id (e.g. the experiment harness's
# "{plan_id}_{condition}_s{series_index}") instead of always getting a
# fresh random UUID on first use.
# ---------------------------------------------------------------------------


class TestGetOrCreateSession:

    def test_no_requested_id_behaves_like_create_session(self, monkeypatch):
        _fresh_registry(monkeypatch)
        sid = get_or_create_session()
        print(f"[test] created session_id={sid}")
        assert isinstance(sid, str)
        assert len(sid) == 36  # UUID format
        assert get_session(sid) is not None

    def test_unregistered_requested_id_is_used_verbatim_not_discarded(self, monkeypatch):
        _fresh_registry(monkeypatch)
        sid = get_or_create_session("p1_learned_s0")
        print(f"[test] session_id={sid}")
        assert sid == "p1_learned_s0"
        assert get_session("p1_learned_s0") is not None

    def test_repeated_calls_with_same_requested_id_reuse_one_session(self, monkeypatch):
        _fresh_registry(monkeypatch)
        sid1 = get_or_create_session("p1_learned_s0")
        append_session_message(sid1, {"role": "user", "content": "turn 1"})

        sid2 = get_or_create_session("p1_learned_s0")
        append_session_message(sid2, {"role": "user", "content": "turn 2"})

        print(f"[test] sid1={sid1} sid2={sid2}")
        assert sid1 == sid2 == "p1_learned_s0"
        messages = get_session_messages("p1_learned_s0")
        assert [m["content"] for m in messages] == ["turn 1", "turn 2"]

    def test_requested_id_matching_existing_uuid_session_is_reused(self, monkeypatch):
        """Mirrors the chat UI's own flow: it only ever resends an ID the
        backend already issued via create_session()."""
        _fresh_registry(monkeypatch)
        sid = create_session()

        result = get_or_create_session(sid)

        assert result == sid
