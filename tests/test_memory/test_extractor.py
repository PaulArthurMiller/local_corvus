"""
Tests for memory/extractor.py

Covers:
  - Mode 1: keyword-adjacent sentence extraction
  - Mode 2: first/last sentence fallback
  - Mode 3: summarization trigger (Gemma call mocked)
  - Empty input guard
  - Single-sentence MUs
"""

import asyncio
import os
import sys
from unittest import mock

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "backend"))

from memory.extractor import (
    extract,
    _mode1_extract,
    _mode2_extract,
    _split_sentences,
    _contains_keyword,
    _source_kind_label,
    _LABEL_SEED_REFERENCE,
    _LABEL_PRIOR_USER_MESSAGE,
    _LABEL_PRIOR_RESPONSE,
    _LABEL_SYNTHESIZED_SUMMARY,
    _LABEL_TOOL_RECORD,
    _LABEL_OTHER,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def run(coro):
    return asyncio.get_event_loop().run_until_complete(coro)


def _mu(content: str, source_type: str = "user_input", series_id: str = None, mu_id: str = "test-mu-id") -> dict:
    """Build a minimal MU dict with the fields extractor.py reads."""
    return {
        "mu_id": mu_id,
        "raw_content": content,
        "source_type": source_type,
        "series_id": series_id,
    }


# ---------------------------------------------------------------------------
# Sentence splitting
# ---------------------------------------------------------------------------


class TestSplitSentences:

    def test_splits_on_period_space(self):
        sentences = _split_sentences("Hello world. How are you? Fine.")
        print(f"[test] sentences: {sentences}")
        assert len(sentences) == 3

    def test_single_sentence_returns_one_item(self):
        sentences = _split_sentences("Only one sentence here")
        assert len(sentences) == 1
        assert sentences[0] == "Only one sentence here"

    def test_empty_string_returns_empty_list(self):
        assert _split_sentences("") == []

    def test_strips_whitespace_from_sentences(self):
        sentences = _split_sentences("  Hello.   World.  ")
        assert all(s == s.strip() for s in sentences)


# ---------------------------------------------------------------------------
# Keyword detection
# ---------------------------------------------------------------------------


class TestContainsKeyword:

    def test_finds_keyword_case_insensitive(self):
        assert _contains_keyword("The ELEPHANT is large.", ["elephant"]) is True

    def test_returns_false_when_no_match(self):
        assert _contains_keyword("Lions are fast.", ["elephant"]) is False

    def test_multiple_keywords_any_match(self):
        assert _contains_keyword("Giraffes eat leaves.", ["elephant", "giraffe"]) is True


# ---------------------------------------------------------------------------
# Mode 1: keyword-adjacent extraction
# ---------------------------------------------------------------------------


class TestMode1Extract:

    def test_returns_sentence_with_keyword(self):
        mus = [_mu("The elephant is large. Lions are fast. Birds can fly.")]
        chunks = _mode1_extract(mus, ["elephant"])
        print(f"[test] Mode 1 chunks: {chunks}")
        assert len(chunks) == 1
        assert "elephant" in chunks[0].lower()

    def test_includes_neighbor_sentences(self):
        mus = [_mu(
            "Sentence A. The elephant is large. Sentence C. Sentence D."
        )]
        chunks = _mode1_extract(mus, ["elephant"])
        print(f"[test] Mode 1 with neighbors: {chunks}")
        # Should include Sentence A (before), elephant sentence, Sentence C (after)
        assert "Sentence A" in chunks[0]
        assert "elephant" in chunks[0].lower()
        assert "Sentence C" in chunks[0]

    def test_no_keyword_match_returns_empty_chunk_list(self):
        mus = [_mu("Lions are the apex predators. They hunt in groups.")]
        chunks = _mode1_extract(mus, ["elephant"])
        print(f"[test] Mode 1 no match: {chunks}")
        assert chunks == []

    def test_multiple_mus_extracts_from_each(self):
        mus = [
            _mu("The elephant is large. Lions are fast."),
            _mu("Birds can fly. The elephant uses its trunk. Water is wet."),
        ]
        chunks = _mode1_extract(mus, ["elephant"])
        print(f"[test] Mode 1 multi-MU: {chunks}")
        assert len(chunks) == 2

    def test_no_duplicate_sentences_when_adjacent_hits_overlap(self):
        """Two adjacent sentences both match — overlapping neighbor indices should deduplicate."""
        mus = [_mu("The elephant is large. The elephant uses trunk. Lions are fast.")]
        chunks = _mode1_extract(mus, ["elephant"])
        text = chunks[0] if chunks else ""
        # Count occurrences of "elephant" — should be 2 (two matching sentences) but no triple
        print(f"[test] deduplicated output: {text}")
        count = text.lower().count("elephant")
        assert count == 2


# ---------------------------------------------------------------------------
# Mode 2: first/last fallback
# ---------------------------------------------------------------------------


class TestMode2Extract:

    def test_returns_first_and_last_sentence(self):
        mus = [_mu("First sentence. Middle sentence. Last sentence.")]
        chunks = _mode2_extract(mus)
        print(f"[test] Mode 2 chunks: {chunks}")
        assert "First sentence" in chunks[0]
        assert "Last sentence" in chunks[0]

    def test_single_sentence_mu_returns_that_sentence(self):
        mus = [_mu("Only one sentence.")]
        chunks = _mode2_extract(mus)
        print(f"[test] Mode 2 single sentence: {chunks}")
        assert chunks[0] == f"{_LABEL_PRIOR_USER_MESSAGE}\nOnly one sentence."

    def test_empty_mu_produces_no_chunk(self):
        mus = [_mu("")]
        chunks = _mode2_extract(mus)
        print(f"[test] Mode 2 empty MU: {chunks}")
        assert chunks == []

    def test_multiple_mus_produces_one_chunk_each(self):
        mus = [
            _mu("Alpha. Beta. Gamma."),
            _mu("One. Two."),
        ]
        chunks = _mode2_extract(mus)
        print(f"[test] Mode 2 multi-MU: {chunks}")
        assert len(chunks) == 2


# ---------------------------------------------------------------------------
# extract() — integration (Modes 1 & 2 without Mode 3 trigger)
# ---------------------------------------------------------------------------


class TestExtractIntegration:

    def test_empty_mus_returns_empty_string(self):
        result, mu_id = run(extract(mus=[], keywords=["elephant"]))
        print(f"[test] empty mus: result='{result}' mu_id={mu_id}")
        assert result == ""
        assert mu_id is None

    def test_mode1_used_when_keywords_match(self):
        mus = [_mu("The elephant is large. Lions are fast. Birds can fly.")]
        result, mu_id = run(extract(mus=mus, keywords=["elephant"]))
        print(f"[test] Mode 1 via extract(): '{result}'")
        assert "elephant" in result.lower()
        assert mu_id is None

    def test_mode2_fallback_when_no_keyword_matches(self):
        """No keyword hits → Mode 1 yields 0 sentences → Mode 2 fallback."""
        mus = [_mu("First sentence here. Middle sentence. Last sentence here.")]
        result, mu_id = run(extract(mus=mus, keywords=["nonexistent_keyword_xyz"]))
        print(f"[test] Mode 2 fallback: '{result}'")
        assert "First sentence" in result
        assert "Last sentence" in result
        assert mu_id is None

    def test_returns_tuple_of_str_and_none_normally(self):
        mus = [_mu("The quick brown fox. Jumped over the fence. The end.")]
        result, mu_id = run(extract(mus=mus, keywords=["fox"]))
        print(f"[test] return types: result={type(result)} mu_id={mu_id}")
        assert isinstance(result, str)
        assert mu_id is None


# ---------------------------------------------------------------------------
# Mode 3: summarization trigger
# ---------------------------------------------------------------------------


def _build_large_mus(num_mus: int = 5, sentences_per_mu: int = 40) -> list[dict]:
    """Build MUs with enough text to exceed summarization_trigger_tokens."""
    chunks = []
    for i in range(num_mus):
        # Each sentence is ~80 chars; 40 sentences * 80 chars = 3200 chars ~= 800 tokens
        sentences = [
            f"The elephant roams across the savannah searching for water. "
            f"Sentence {i}-{j} adds more detail about elephant behavior."
            for j in range(sentences_per_mu)
        ]
        chunks.append(_mu(" ".join(sentences), mu_id=f"source-mu-{i}"))
    return chunks


class TestMode3Summarization:

    def test_mode3_triggers_gemma_call_when_over_token_budget(self):
        """When condensed text exceeds trigger, _summarize should be called."""
        mus = _build_large_mus()

        mock_store = mock.AsyncMock()
        mock_store.create_mu = mock.AsyncMock(return_value={})

        mock_summary = "Condensed: elephants roam savannahs."

        with mock.patch(
            "memory.extractor._summarize",
            new=mock.AsyncMock(return_value=(mock_summary, "mock-summary-mu-id")),
        ) as mock_sum:
            result, mu_id = run(
                extract(
                    mus=mus,
                    keywords=["elephant"],
                    store=mock_store,
                    session_id="test-session",
                )
            )
            print(f"[test] Mode 3 triggered: result='{result[:50]}' mu_id={mu_id}")
            # _summarize was called because text exceeded token budget
            assert mock_sum.called
            assert result == mock_summary
            assert mu_id == "mock-summary-mu-id"

    def test_mode3_not_triggered_when_under_budget(self):
        """Short condensed text must not trigger Mode 3."""
        mus = [_mu("The elephant is large. It uses its trunk.")]

        mock_store = mock.AsyncMock()

        with mock.patch(
            "memory.extractor._summarize",
            new=mock.AsyncMock(return_value=("summary", "mu-id")),
        ) as mock_sum:
            result, mu_id = run(
                extract(
                    mus=mus,
                    keywords=["elephant"],
                    store=mock_store,
                    session_id="test-session",
                )
            )
            print(f"[test] Mode 3 not triggered: called={mock_sum.called}")
            assert not mock_sum.called
            assert mu_id is None

    def test_mode3_fallback_on_ollama_failure_returns_original_text(self):
        """If Gemma call fails, _summarize returns (original_text, None)."""
        import memory.extractor as extractor_module

        mus = _build_large_mus()
        mock_store = mock.AsyncMock()
        mock_store.create_mu = mock.AsyncMock(return_value={})

        with mock.patch(
            "memory.extractor._summarize",
            new=mock.AsyncMock(return_value=("original long text", None)),
        ):
            result, mu_id = run(
                extract(
                    mus=mus,
                    keywords=["elephant"],
                    store=mock_store,
                    session_id="test-session",
                )
            )
            print(f"[test] fallback result: mu_id={mu_id}")
            assert mu_id is None
            assert isinstance(result, str)


# ---------------------------------------------------------------------------
# Milestone 23 — source-kind labels
# ---------------------------------------------------------------------------


class TestSourceKindLabel:
    """series_id is checked before source_type — it's the only field that
    distinguishes seed-origin content (every seed MU is stamped
    source_type="agent_output", identical to a real turn's response)."""

    def test_seed_series_id_labels_reference_regardless_of_source_type(self):
        mu = _mu("content", source_type="agent_output", series_id="seed:cluster_a")
        label = _source_kind_label(mu)
        print(f"[test] seed label: {label}")
        assert label == _LABEL_SEED_REFERENCE

    def test_non_seed_series_id_does_not_trigger_reference_label(self):
        mu = _mu("content", source_type="agent_output", series_id="real-series-1")
        label = _source_kind_label(mu)
        print(f"[test] non-seed agent_output label: {label}")
        assert label == _LABEL_PRIOR_RESPONSE

    def test_user_input_labels_prior_user_message(self):
        mu = _mu("content", source_type="user_input")
        assert _source_kind_label(mu) == _LABEL_PRIOR_USER_MESSAGE

    def test_agent_output_labels_prior_response(self):
        mu = _mu("content", source_type="agent_output")
        assert _source_kind_label(mu) == _LABEL_PRIOR_RESPONSE

    def test_system_labels_synthesized_summary(self):
        mu = _mu("content", source_type="system")
        assert _source_kind_label(mu) == _LABEL_SYNTHESIZED_SUMMARY

    def test_tool_call_labels_tool_record(self):
        mu = _mu("content", source_type="tool_call")
        assert _source_kind_label(mu) == _LABEL_TOOL_RECORD

    def test_tool_result_labels_tool_record(self):
        mu = _mu("content", source_type="tool_result")
        assert _source_kind_label(mu) == _LABEL_TOOL_RECORD

    def test_genuinely_unknown_source_type_labels_other(self):
        mu = _mu("content", source_type="something_never_seen_before")
        assert _source_kind_label(mu) == _LABEL_OTHER

    def test_missing_series_id_key_does_not_crash(self):
        """get_all_mus() rows always have series_id, but be defensive: a dict
        missing the key entirely must not raise."""
        mu = {"mu_id": "x", "raw_content": "y", "source_type": "user_input"}
        assert _source_kind_label(mu) == _LABEL_PRIOR_USER_MESSAGE

    def test_none_series_id_does_not_crash(self):
        mu = _mu("content", source_type="user_input", series_id=None)
        assert _source_kind_label(mu) == _LABEL_PRIOR_USER_MESSAGE


class TestModeLabelPrefixes:
    """Mode 1 and Mode 2 both prefix each chunk with the MU's source-kind
    label before joining into the condensed context."""

    def test_mode1_prefixes_seed_label(self):
        mus = [_mu(
            "The elephant is large. Lions are fast. Birds can fly.",
            source_type="agent_output", series_id="seed:cluster_a",
        )]
        chunks = _mode1_extract(mus, ["elephant"])
        print(f"[test] Mode 1 seed-labeled chunk: {chunks[0]}")
        assert chunks[0].startswith(f"{_LABEL_SEED_REFERENCE}\n")
        assert "elephant" in chunks[0].lower()

    def test_mode1_prefixes_user_input_label(self):
        mus = [_mu("The elephant is large. Lions are fast.", source_type="user_input")]
        chunks = _mode1_extract(mus, ["elephant"])
        assert chunks[0].startswith(f"{_LABEL_PRIOR_USER_MESSAGE}\n")

    def test_mode1_prefixes_agent_output_label(self):
        mus = [_mu("The elephant is large. Lions are fast.", source_type="agent_output")]
        chunks = _mode1_extract(mus, ["elephant"])
        assert chunks[0].startswith(f"{_LABEL_PRIOR_RESPONSE}\n")

    def test_mode1_prefixes_system_label(self):
        mus = [_mu("The elephant is large. Lions are fast.", source_type="system")]
        chunks = _mode1_extract(mus, ["elephant"])
        assert chunks[0].startswith(f"{_LABEL_SYNTHESIZED_SUMMARY}\n")

    def test_mode1_multiple_mus_each_get_their_own_label(self):
        mus = [
            _mu("The elephant is large. Lions are fast.", source_type="user_input"),
            _mu("Birds can fly. The elephant uses its trunk. Water is wet.", source_type="agent_output", series_id="seed:x"),
        ]
        chunks = _mode1_extract(mus, ["elephant"])
        print(f"[test] Mode 1 multi-MU labels: {chunks}")
        assert chunks[0].startswith(f"{_LABEL_PRIOR_USER_MESSAGE}\n")
        assert chunks[1].startswith(f"{_LABEL_SEED_REFERENCE}\n")

    def test_mode2_prefixes_label(self):
        mus = [_mu(
            "First sentence. Middle sentence. Last sentence.",
            source_type="agent_output", series_id="seed:cluster_a",
        )]
        chunks = _mode2_extract(mus)
        print(f"[test] Mode 2 seed-labeled chunk: {chunks[0]}")
        assert chunks[0].startswith(f"{_LABEL_SEED_REFERENCE}\n")
        assert "First sentence" in chunks[0]
        assert "Last sentence" in chunks[0]

    def test_mode2_tool_result_label(self):
        mus = [_mu("A tool ran. It returned data.", source_type="tool_result")]
        chunks = _mode2_extract(mus)
        assert chunks[0].startswith(f"{_LABEL_TOOL_RECORD}\n")


# ---------------------------------------------------------------------------
# Milestone 23 — summary -> source edges
# ---------------------------------------------------------------------------


def _patch_extractor_httpx(summary_text: str):
    ollama_response = {"choices": [{"message": {"content": summary_text}}]}
    mock_response = mock.MagicMock()
    mock_response.json.return_value = ollama_response
    mock_response.raise_for_status = mock.MagicMock()

    mock_client = mock.AsyncMock()
    mock_client.__aenter__ = mock.AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = mock.AsyncMock(return_value=False)
    mock_client.post = mock.AsyncMock(return_value=mock_response)

    return mock.patch("memory.extractor.httpx.AsyncClient", return_value=mock_client)


class TestMilestone23SummaryEdges:
    """A Mode-3 summary MU must not be an edge-orphan: extract() creates one
    summary->source edge per pre-summarization MU."""

    def test_summary_creates_one_edge_per_source_mu(self):
        mus = _build_large_mus()

        mock_store = mock.AsyncMock()
        mock_store.create_mu = mock.AsyncMock(return_value={})
        mock_store.create_edge = mock.AsyncMock(return_value={})

        with _patch_extractor_httpx("A short summary of elephant behavior."):
            result, mu_id = run(
                extract(
                    mus=mus,
                    keywords=["elephant"],
                    store=mock_store,
                    session_id="test-session",
                    run_id="run-123",
                )
            )

        print(f"[test] summary mu_id={mu_id} edge calls={mock_store.create_edge.call_count}")
        assert mu_id is not None
        assert mock_store.create_edge.call_count == len(mus)

        called_targets = {c.kwargs["target_mu_id"] for c in mock_store.create_edge.call_args_list}
        assert called_targets == {m["mu_id"] for m in mus}

        for c in mock_store.create_edge.call_args_list:
            assert c.kwargs["source_mu_id"] == mu_id
            assert c.kwargs["creation_trigger"] == "co_activation"
            assert c.kwargs["creation_run_id"] == "run-123"
            assert c.kwargs["alpha"] == 1.0
            assert c.kwargs["beta"] == 1.0

    def test_no_summary_mu_means_no_edges_created(self):
        """Mode 1/2 paths (no Mode 3 trigger) never call create_edge."""
        mus = [_mu("The elephant is large. It uses its trunk.")]
        mock_store = mock.AsyncMock()
        mock_store.create_edge = mock.AsyncMock(return_value={})

        result, mu_id = run(
            extract(mus=mus, keywords=["elephant"], store=mock_store, session_id="s", run_id="run-1")
        )
        print(f"[test] no-summary edge calls: {mock_store.create_edge.call_count}")
        assert mu_id is None
        assert mock_store.create_edge.call_count == 0

    def test_summary_edge_creation_survives_missing_run_id(self):
        """run_id is optional — probe-style or legacy callers that don't pass
        one must not crash edge creation."""
        mus = _build_large_mus()
        mock_store = mock.AsyncMock()
        mock_store.create_mu = mock.AsyncMock(return_value={})
        mock_store.create_edge = mock.AsyncMock(return_value={})

        with _patch_extractor_httpx("Summary text."):
            result, mu_id = run(
                extract(mus=mus, keywords=["elephant"], store=mock_store, session_id="s")
            )

        assert mu_id is not None
        assert mock_store.create_edge.call_count == len(mus)
        for c in mock_store.create_edge.call_args_list:
            assert c.kwargs["creation_run_id"] is None
