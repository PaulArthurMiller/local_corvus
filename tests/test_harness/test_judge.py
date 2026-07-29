"""
Tests for scripts/harness/judge.py — Ollama judge call + verdict parsing.

Every judge call is mocked via FakeOllamaJudgeClient; no test here ever
hits a real Ollama instance. OllamaJudgeClient itself (the real HTTP client)
is exercised separately against httpx.MockTransport, so the request-building
logic is verified without any network call.
"""

import json
import os
import sys

import httpx
import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "scripts"))

from harness.judge import (
    DEFAULT_JUDGE_HOST,
    JudgeUnparseableError,
    JudgeVerdict,
    OllamaJudgeClient,
    ProbeTurnJudgeError,
    VALID_VERDICTS,
    judge_turn,
)
from harness.schema import JudgeConfig
from harness.turn_resolution import TURN_TYPE_BULK, TURN_TYPE_PROBE


class FakeOllamaJudgeClient:
    """Duck-typed stand-in for harness.judge.OllamaJudgeClient."""

    def __init__(self, responses=None, available=True):
        self._responses = list(responses) if responses is not None else []
        self.available = available
        self.calls = []

    def chat_json(self, model, temperature, prompt):
        self.calls.append({"model": model, "temperature": temperature, "prompt": prompt})
        if not self._responses:
            raise AssertionError("FakeOllamaJudgeClient exhausted; unexpected judge call")
        resp = self._responses.pop(0)
        if isinstance(resp, Exception):
            raise resp
        return resp

    def model_available(self, model):
        return self.available


def _judge_config(**overrides):
    kwargs = dict(model="gemma4:e4b", temperature=0.0)
    kwargs.update(overrides)
    return JudgeConfig(**kwargs)


def _verdict_json(verdict="up", rationale="ok", criterion_triggered=None):
    return json.dumps({"verdict": verdict, "rationale": rationale, "criterion_triggered": criterion_triggered})


# ---------------------------------------------------------------------------
# judge_turn — clean parses
# ---------------------------------------------------------------------------


def test_clean_up_verdict_parses():
    client = FakeOllamaJudgeClient([_verdict_json(verdict="up", rationale="covers all points")])

    result = judge_turn(client, "What is X?", ["mentions X"], "X is Y.", _judge_config(), TURN_TYPE_BULK)

    assert isinstance(result, JudgeVerdict)
    assert result.verdict == "up"
    assert result.rationale == "covers all points"
    assert result.criterion_triggered is None
    assert len(client.calls) == 1
    assert client.calls[0]["model"] == "gemma4:e4b"
    assert client.calls[0]["temperature"] == 0.0


def test_clean_none_verdict_parses():
    client = FakeOllamaJudgeClient([_verdict_json(verdict="none", rationale="ambiguous")])

    result = judge_turn(client, "What is X?", ["mentions X"], "irrelevant.", _judge_config(), TURN_TYPE_BULK)

    assert result.verdict == "none"
    assert result.criterion_triggered is None


def test_down_verdict_with_valid_criterion_parses():
    client = FakeOllamaJudgeClient([_verdict_json(verdict="down", rationale="missing X", criterion_triggered="mentions X")])

    result = judge_turn(client, "What is X?", ["mentions X"], "irrelevant.", _judge_config(), TURN_TYPE_BULK)

    assert result.verdict == "down"
    assert result.criterion_triggered == "mentions X"


# ---------------------------------------------------------------------------
# judge_turn — closed-vocabulary and parse-failure handling
# ---------------------------------------------------------------------------


def test_down_verdict_with_unresolvable_criterion_retries_then_keeps_verdict():
    """The asymmetry fix: an unresolvable citation degrades the result rather
    than discarding it, so a 'down' survives what an 'up' always survived."""
    criteria = ["mentions X", "mentions Y"]
    responses = [
        _verdict_json(verdict="down", criterion_triggered="not a real criterion"),
        _verdict_json(verdict="down", criterion_triggered="still not real"),
    ]
    client = FakeOllamaJudgeClient(responses)

    result = judge_turn(client, "What is X?", criteria, "irrelevant.", _judge_config(), TURN_TYPE_BULK)

    assert result.verdict == "down"
    assert result.criterion_triggered is None
    assert result.criterion_unresolved is True
    assert result.raw_criterion_citation == "not a real criterion"
    assert len(client.calls) == 2


def test_reask_that_cites_a_real_criterion_wins_over_first_unresolved_attempt():
    criteria = ["mentions X", "mentions Y"]
    responses = [
        _verdict_json(verdict="down", criterion_triggered="not a real criterion"),
        _verdict_json(verdict="down", criterion_triggered="mentions Y"),
    ]
    client = FakeOllamaJudgeClient(responses)

    result = judge_turn(client, "What is X?", criteria, "irrelevant.", _judge_config(), TURN_TYPE_BULK)

    assert result.criterion_triggered == "mentions Y"
    assert result.criterion_unresolved is False


def test_single_criterion_prompt_resolves_unmatched_citation_by_deduction():
    """A 'down' on a one-criterion prompt can only mean that criterion."""
    client = FakeOllamaJudgeClient([_verdict_json(verdict="down", criterion_triggered="garbled")])

    result = judge_turn(client, "What is X?", ["mentions X"], "irrelevant.", _judge_config(), TURN_TYPE_BULK)

    assert result.criterion_triggered == "mentions X"
    assert result.criterion_unresolved is False
    assert len(client.calls) == 1


def test_down_verdict_with_null_citation_on_multi_criterion_prompt_is_unresolved():
    client = FakeOllamaJudgeClient([
        _verdict_json(verdict="down", criterion_triggered=None),
        _verdict_json(verdict="down", criterion_triggered=None),
    ])

    result = judge_turn(client, "What is X?", ["mentions X", "mentions Y"], "no.", _judge_config(), TURN_TYPE_BULK)

    assert result.verdict == "down"
    assert result.criterion_unresolved is True


# ---------------------------------------------------------------------------
# Criterion normalization — the exp0 "answer: " prefix failure and friends
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("citation", [
    "local temporal readings/local time",           # the exact exp0 failure
    "answer: local temporal readings/local time",   # verbatim
    "**local temporal readings/local time**",       # markdown emphasis
    "Local Temporal Readings/Local Time.",          # case + trailing period
    '"local temporal readings/local time"',         # quoted
    "  local temporal   readings/local time  ",     # whitespace noise
    "criterion: answer: local temporal readings/local time",  # stacked labels
])
def test_normalized_citation_resolves_to_exact_bank_criterion(citation):
    criteria = ["answer: local temporal readings/local time", "mentions UTC"]
    client = FakeOllamaJudgeClient([_verdict_json(verdict="down", criterion_triggered=citation)])

    result = judge_turn(client, "Which reading?", criteria, "wrong.", _judge_config(), TURN_TYPE_BULK)

    # Always the bank's exact text, never the judge's wording — §6 groups by it.
    assert result.criterion_triggered == "answer: local temporal readings/local time"
    assert result.criterion_unresolved is False
    assert len(client.calls) == 1


def test_normalization_does_not_strip_leading_label_words_without_a_colon():
    """'states three' must survive: only 'label:' prefixes are stripped."""
    criteria = ["states three", "confirms flattening requirement"]
    client = FakeOllamaJudgeClient([_verdict_json(verdict="down", criterion_triggered="States three")])

    result = judge_turn(client, "How deep?", criteria, "wrong.", _judge_config(), TURN_TYPE_BULK)

    assert result.criterion_triggered == "states three"


def test_normalization_does_not_conflate_distinct_criteria():
    criteria = ["states three", "states four"]
    client = FakeOllamaJudgeClient([
        _verdict_json(verdict="down", criterion_triggered="states five"),
        _verdict_json(verdict="down", criterion_triggered="states five"),
    ])

    result = judge_turn(client, "How deep?", criteria, "wrong.", _judge_config(), TURN_TYPE_BULK)

    assert result.criterion_unresolved is True


def test_non_string_citation_type_does_not_crash_parsing():
    client = FakeOllamaJudgeClient([
        json.dumps({"verdict": "down", "rationale": "no", "criterion_triggered": ["mentions X"]}),
        json.dumps({"verdict": "down", "rationale": "no", "criterion_triggered": 7}),
    ])

    result = judge_turn(client, "What is X?", ["mentions X", "mentions Y"], "no.", _judge_config(), TURN_TYPE_BULK)

    assert result.verdict == "down"
    assert result.criterion_unresolved is True


def test_malformed_json_retries_once_then_raises():
    client = FakeOllamaJudgeClient(["not json", "still not json"])

    with pytest.raises(JudgeUnparseableError):
        judge_turn(client, "What is X?", ["mentions X"], "X is Y.", _judge_config(), TURN_TYPE_BULK)

    assert len(client.calls) == 2


def test_retry_succeeds_after_first_malformed_response():
    client = FakeOllamaJudgeClient(["not json", _verdict_json(verdict="none", rationale="ambiguous")])

    result = judge_turn(client, "What is X?", ["mentions X"], "X is Y.", _judge_config(), TURN_TYPE_BULK)

    assert result.verdict == "none"
    assert len(client.calls) == 2


def test_invalid_verdict_value_retries_then_raises():
    responses = [_verdict_json(verdict="maybe"), _verdict_json(verdict="maybe")]
    client = FakeOllamaJudgeClient(responses)

    with pytest.raises(JudgeUnparseableError):
        judge_turn(client, "What is X?", ["mentions X"], "X is Y.", _judge_config(), TURN_TYPE_BULK)


def test_non_object_json_retries_then_raises():
    responses = [json.dumps(["up", "ok", None]), json.dumps(["up", "ok", None])]
    client = FakeOllamaJudgeClient(responses)

    with pytest.raises(JudgeUnparseableError):
        judge_turn(client, "What is X?", ["mentions X"], "X is Y.", _judge_config(), TURN_TYPE_BULK)


def test_http_error_from_client_is_contained_by_retry_then_raises():
    client = FakeOllamaJudgeClient([httpx.ConnectError("refused"), httpx.ConnectError("refused")])

    with pytest.raises(JudgeUnparseableError):
        judge_turn(client, "What is X?", ["mentions X"], "X is Y.", _judge_config(), TURN_TYPE_BULK)


# ---------------------------------------------------------------------------
# judge_turn — probe turn guard
# ---------------------------------------------------------------------------


def test_probe_turn_raises_before_any_call():
    client = FakeOllamaJudgeClient([])

    with pytest.raises(ProbeTurnJudgeError):
        judge_turn(client, "probe prompt", ["c1"], "response", _judge_config(), TURN_TYPE_PROBE)

    assert client.calls == []


# ---------------------------------------------------------------------------
# OllamaJudgeClient — real HTTP request shape, via httpx.MockTransport
# (no network call; verifies the request the module actually sends)
# ---------------------------------------------------------------------------


def test_ollama_judge_client_chat_json_sends_expected_request():
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        captured["body"] = json.loads(request.content)
        return httpx.Response(200, json={"message": {"content": _verdict_json()}})

    client = OllamaJudgeClient(host=DEFAULT_JUDGE_HOST)
    client._client = httpx.Client(base_url=DEFAULT_JUDGE_HOST, transport=httpx.MockTransport(handler))

    result = client.chat_json("gemma4:e4b", 0.0, "grade this")

    assert captured["url"].endswith("/api/chat")
    assert captured["body"]["model"] == "gemma4:e4b"
    assert captured["body"]["format"] == "json"
    assert captured["body"]["options"]["temperature"] == 0.0
    assert result == _verdict_json()


def test_ollama_judge_client_model_available_true_for_exact_match():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"models": [{"name": "gemma4:e4b"}, {"name": "llama3"}]})

    client = OllamaJudgeClient(host=DEFAULT_JUDGE_HOST)
    client._client = httpx.Client(base_url=DEFAULT_JUDGE_HOST, transport=httpx.MockTransport(handler))

    assert client.model_available("gemma4:e4b") is True
    assert client.model_available("mistral") is False


def test_ollama_judge_client_model_available_false_on_http_error():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500)

    client = OllamaJudgeClient(host=DEFAULT_JUDGE_HOST)
    client._client = httpx.Client(base_url=DEFAULT_JUDGE_HOST, transport=httpx.MockTransport(handler))

    assert client.model_available("gemma4:e4b") is False
