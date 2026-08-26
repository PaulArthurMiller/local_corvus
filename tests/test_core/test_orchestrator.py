"""
Tests for core/orchestrator.py — message construction logic.

Covers: _build_ollama_messages() which assembles the messages array sent to
Ollama on each turn. The key invariant: retrieved memory context appears once
in the system role only; the user role carries only the clean user input.
"""

import asyncio
import os
import sys
import tempfile
import uuid
from pathlib import Path
from unittest import mock
from unittest.mock import AsyncMock, MagicMock

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "backend"))

from core.orchestrator import (
    EDGE_RNG_SEED,
    _build_ollama_messages,
    _ollama_request_payload,
    _resolved_config_snapshot,
    _scrub_prompt_for_storage,
)
from analytics.run_log import RunRecord


def test_bulk_turn_server_default_sampling_is_recorded_without_pinning():
    payload = _ollama_request_payload([{"role": "user", "content": "hi"}], [])
    snapshot = _resolved_config_snapshot()

    assert "temperature" not in payload
    assert "temperature" not in snapshot["model"]
    assert snapshot["runtime"]["ollama_request"]["sampling_parameters"] == {}
    assert snapshot["runtime"]["ollama_request"]["sampling_source"] == "ollama_server_default"
    assert snapshot["runtime"]["edge_update_rng_seed"] == EDGE_RNG_SEED


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


# ---------------------------------------------------------------------------
# M20 Phase 1 — RUN_CONDITION / EDGE_UPDATE_CFG module constants
# ---------------------------------------------------------------------------


class TestMilestone20Orchestrator:

    def test_run_condition_defaults_to_static_weight_without_env_var(self):
        """No RUN_CONDITION env var set in the test process -> the pre-M20
        scalar-weight behavior (static_weight) is the safe default, not
        "learned" — plain chat-UI/dev usage must be unaffected by M20."""
        from core.orchestrator import RUN_CONDITION
        print(f"[test] RUN_CONDITION={RUN_CONDITION!r}")
        assert RUN_CONDITION == "static_weight"

    def test_edge_update_cfg_is_validated_and_resolved(self):
        from core.orchestrator import EDGE_UPDATE_CFG
        print(f"[test] EDGE_UPDATE_CFG={EDGE_UPDATE_CFG!r}")
        assert EDGE_UPDATE_CFG["prior_alpha"] > 0
        assert EDGE_UPDATE_CFG["prior_beta"] > 0
        assert EDGE_UPDATE_CFG["weight_mode"] in ("mean", "sample")

    def test_edge_rng_is_a_random_instance(self):
        import random
        from core.orchestrator import EDGE_RNG
        assert isinstance(EDGE_RNG, random.Random)


# ---------------------------------------------------------------------------
# M21 — _prompt_mu_content: prompt MUs must not store injected memory context
# ---------------------------------------------------------------------------


class TestPromptMuContent:
    """The stored prompt MU excludes the injected context block. MU content is
    retrievable, so storing the assembled prompt would copy every retrieved
    MU (plus the context markers) into a new node — and retrieving that node
    later would nest old context inside new context, compounding per turn."""

    def test_context_markers_never_stored_on_prompt_mu(self):
        from core.orchestrator import _prompt_mu_content

        content = _prompt_mu_content(
            condensed_context="water boils at 100 degrees",
            user_content="what temperature does water boil?",
        )
        print(f"[test] prompt MU content: {content!r}")
        assert "Context from memory" not in content
        assert "water boils at 100 degrees" not in content  # retrieved text not re-embedded

    def test_user_message_portion_is_preserved_with_label(self):
        from core.orchestrator import _prompt_mu_content

        content = _prompt_mu_content(
            condensed_context="some retrieved fact",
            user_content="what is the capital of France?",
        )
        assert content == "[User message]\nwhat is the capital of France?"

    def test_no_context_stores_bare_user_content(self):
        from core.orchestrator import _prompt_mu_content

        content = _prompt_mu_content(
            condensed_context="",
            user_content="hello there",
        )
        assert content == "hello there"


# ---------------------------------------------------------------------------
# M22 — Output-MU routing edges + prompt MU as routing dead end
#
# Full run_interaction() pipeline against a real temp-file SQLite GraphStore
# (audit/snapshot paths also redirected to the same temp dir), with
# retrieve()/extract() mocked to a known, deterministic shape and Ollama
# mocked via httpx — the same pattern as the M20 §8 trace test
# (tests/test_learning/test_edge_update_trace.py) and the failure-detection
# httpx mock (test_failure_detection.py).
# ---------------------------------------------------------------------------


def _run(coro):
    return asyncio.get_event_loop().run_until_complete(coro)


class _TempFullStore:
    """Context manager: GraphStore + audit log + snapshots all pointed at one
    temp dir, so a full run_interaction() call never touches /app/data."""

    def __enter__(self):
        import gate.audit as audit_module
        import memory.snapshot as snapshot_module
        import memory.store as store_module
        from memory.store import GraphStore

        self._tmpdir = tempfile.TemporaryDirectory()
        tmp_path = Path(self._tmpdir.name)
        self._patchers = [
            mock.patch.object(store_module, "_DB_PATH", tmp_path / "graph.db"),
            mock.patch.object(audit_module, "_AUDIT_LOG_PATH", tmp_path / "audit.log"),
            mock.patch.object(snapshot_module, "_SNAPSHOTS_DIR", tmp_path / "snapshots"),
        ]
        for p in self._patchers:
            p.start()
        self._store = GraphStore()
        return self._store

    def __exit__(self, *args):
        for p in reversed(self._patchers):
            p.stop()
        self._tmpdir.cleanup()


def _make_ollama_response(content: str, finish_reason="stop") -> dict:
    return {
        "choices": [{
            "finish_reason": finish_reason,
            "message": {"content": content, "tool_calls": None},
        }],
        "usage": {"prompt_tokens": 10, "completion_tokens": 5},
    }


def _patch_httpx(response_json: dict):
    mock_response = MagicMock()
    mock_response.json.return_value = response_json
    mock_response.raise_for_status = MagicMock()

    mock_client = AsyncMock()
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=False)
    mock_client.post = AsyncMock(return_value=mock_response)

    return mock.patch("core.orchestrator.httpx.AsyncClient", return_value=mock_client)


def _fake_retrieval_result(activated_mu_ids):
    from memory.retrieval import RetrievalResult
    return RetrievalResult(
        mus=[],
        keywords=["test"],
        activated_mu_ids=list(activated_mu_ids),
        activated_edge_ids=[],
    )


def _run_interaction(message, series_id, activated_mu_ids, summary_mu_id=None, ollama_response=None):
    from core.orchestrator import run_interaction

    retrieval_result = _fake_retrieval_result(activated_mu_ids)
    ollama_response = ollama_response or _make_ollama_response("a reply")
    with mock.patch("core.orchestrator.retrieve", AsyncMock(return_value=retrieval_result)), \
         mock.patch("core.orchestrator.extract", AsyncMock(return_value=("condensed context", summary_mu_id))), \
         _patch_httpx(ollama_response):
        return _run(run_interaction(message=message, series_id=series_id))


class TestMilestone22OutputRoutingEdges:
    """Context-linkage edges relocate from prompt->retrieved to
    output->retrieved (+ output->summary when Mode 3 fires); the prompt MU
    becomes a routing dead end (its only edge is prompt->input)."""

    def test_n_retrieved_mus_no_summary_produces_n_plus_2_edges(self):
        with _TempFullStore() as s:
            mu1, mu2 = str(uuid.uuid4()), str(uuid.uuid4())
            _run(s.create_mu(mu_id=mu1, source_type="agent_output", raw_content="alpha"))
            _run(s.create_mu(mu_id=mu2, source_type="user_input", raw_content="beta"))

            result = _run_interaction("hi there", "series-1", [mu1, mu2])

            edges = _run(s.get_all_edges())
            print(f"[test] edges: {edges}")
            assert len(edges) == 4  # prompt->input, input->output, 2x output->retrieved

            by_trigger = {}
            for e in edges:
                by_trigger.setdefault(e["creation_trigger"], []).append(e)
            assert len(by_trigger.get("prompt_construction", [])) == 2
            co_activation = by_trigger.get("co_activation", [])
            assert len(co_activation) == 2
            assert {e["target_mu_id"] for e in co_activation} == {mu1, mu2}
            assert {e["source_mu_id"] for e in co_activation} == {result.output_mu_id}

    def test_mode3_summary_produces_n_plus_3_edges(self):
        with _TempFullStore() as s:
            mu1 = str(uuid.uuid4())
            summary_mu = str(uuid.uuid4())
            _run(s.create_mu(mu_id=mu1, source_type="agent_output", raw_content="alpha"))
            _run(s.create_mu(mu_id=summary_mu, source_type="system", raw_content="condensed summary"))

            result = _run_interaction("hi there", "series-2", [mu1], summary_mu_id=summary_mu)

            edges = _run(s.get_all_edges())
            print(f"[test] edges: {edges}")
            assert len(edges) == 4  # prompt->input, input->output, output->mu1, output->summary

            co_activation = [e for e in edges if e["creation_trigger"] == "co_activation"]
            assert {e["target_mu_id"] for e in co_activation} == {mu1, summary_mu}
            assert {e["source_mu_id"] for e in co_activation} == {result.output_mu_id}

    def test_prompt_mu_has_no_outgoing_edges_except_to_input(self):
        with _TempFullStore() as s:
            mu1 = str(uuid.uuid4())
            _run(s.create_mu(mu_id=mu1, source_type="agent_output", raw_content="alpha"))

            result = _run_interaction("hi there", "series-3", [mu1])

            edges = _run(s.get_all_edges())
            prompt_sourced = [e for e in edges if e["source_mu_id"] == result.prompt_mu_id]
            print(f"[test] edges sourced from the prompt MU: {prompt_sourced}")
            assert len(prompt_sourced) == 1
            assert prompt_sourced[0]["target_mu_id"] == result.input_mu_id

    def test_failed_output_produces_same_edge_shape(self):
        """A failed-output turn (empty content, finish_reason=length) still
        gets the full output fan-out — failed/empty MUs are retained
        analytic fixtures, not suppressed."""
        with _TempFullStore() as s:
            mu1 = str(uuid.uuid4())
            _run(s.create_mu(mu_id=mu1, source_type="agent_output", raw_content="alpha"))

            failed_response = _make_ollama_response("", finish_reason="length")
            result = _run_interaction("hi there", "series-4", [mu1], ollama_response=failed_response)

            edges = _run(s.get_all_edges())
            print(f"[test] edges (failed output): {edges}")
            assert len(edges) == 3  # prompt->input, input->output, output->mu1
            co_activation = [e for e in edges if e["creation_trigger"] == "co_activation"]
            assert len(co_activation) == 1
            assert co_activation[0]["source_mu_id"] == result.output_mu_id
            assert co_activation[0]["target_mu_id"] == mu1

    def test_edge_values_identical_across_run_conditions(self):
        """weight/alpha/beta on the new output->retrieved edges must not
        vary by RUN_CONDITION — M22's wiring changes are condition-invariant
        by construction (edge creation never branches on condition)."""
        with _TempFullStore() as s:
            mu1 = str(uuid.uuid4())
            _run(s.create_mu(mu_id=mu1, source_type="agent_output", raw_content="alpha"))

            seen = {}
            for condition in ("learned", "static_weight", "random_weight"):
                with mock.patch("core.orchestrator.RUN_CONDITION", condition):
                    result = _run_interaction("hi there", f"series-cond-{condition}", [mu1])
                edges = _run(s.get_all_edges())
                co_activation = [
                    e for e in edges
                    if e["creation_trigger"] == "co_activation" and e["source_mu_id"] == result.output_mu_id
                ]
                assert len(co_activation) == 1
                seen[condition] = (co_activation[0]["weight"], co_activation[0]["alpha"], co_activation[0]["beta"])

            print(f"[test] edge values by condition: {seen}")
            assert len(set(seen.values())) == 1
