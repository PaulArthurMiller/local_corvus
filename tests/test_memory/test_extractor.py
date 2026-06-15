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
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def run(coro):
    return asyncio.get_event_loop().run_until_complete(coro)


def _mu(content: str) -> dict:
    """Build a minimal MU dict with just the raw_content field."""
    return {"mu_id": "test-mu-id", "raw_content": content, "source_type": "user_input"}


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
        assert chunks[0] == "Only one sentence."

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


class TestMode3Summarization:

    def _build_large_mus(self, num_mus: int = 5, sentences_per_mu: int = 40) -> list[dict]:
        """Build MUs with enough text to exceed summarization_trigger_tokens."""
        chunks = []
        for i in range(num_mus):
            # Each sentence is ~80 chars; 40 sentences * 80 chars = 3200 chars ~= 800 tokens
            sentences = [
                f"The elephant roams across the savannah searching for water. "
                f"Sentence {i}-{j} adds more detail about elephant behavior."
                for j in range(sentences_per_mu)
            ]
            chunks.append(_mu(" ".join(sentences)))
        return chunks

    def test_mode3_triggers_gemma_call_when_over_token_budget(self):
        """When condensed text exceeds trigger, _summarize should be called."""
        mus = self._build_large_mus()

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

        mus = self._build_large_mus()
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
