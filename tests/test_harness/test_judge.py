"""
Tests for scripts/harness/judge.py — Ollama judge call + verdict parsing.

Milestone 31 (Paul, 2026-08-02) replaced the judge's free-text
verdict/criterion_triggered protocol with a per-criterion checklist: the judge
grades each criterion independently (criteria_met, a list of booleans in the
same order as the prompt's criteria) and _parse_verdict() derives the verdict
host-side — all met -> "up", none met -> "down", anything mixed -> "none".
This file tests that derivation truth table plus the malformed-payload cases
that must be direction-neutral (JudgeUnparseableError, never a guessed
verdict).

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
    _normalize_criterion,
    _resolve_criterion,
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


def _judge_config(**overrides):
    kwargs = dict(model="gemma4:e4b", temperature=0.0)
    kwargs.update(overrides)
    return JudgeConfig(**kwargs)


def _checklist_json(criteria_met, rationale="ok"):
    return json.dumps({"criteria_met": criteria_met, "rationale": rationale})


# ---------------------------------------------------------------------------
# judge_turn — derivation truth table
# ---------------------------------------------------------------------------


def test_all_criteria_met_derives_up():
    client = FakeOllamaJudgeClient([_checklist_json([True, True], rationale="covers all points")])

    result = judge_turn(
        client, "What is X and Y?", ["mentions X", "mentions Y"], "X is A, Y is B.",
        _judge_config(), TURN_TYPE_BULK,
    )

    assert isinstance(result, JudgeVerdict)
    assert result.verdict == "up"
    assert result.rationale == "covers all points"
    assert result.criterion_triggered is None
    assert result.criteria_met == [True, True]
    assert len(client.calls) == 1
    assert client.calls[0]["model"] == "gemma4:e4b"
    assert client.calls[0]["temperature"] == 0.0


def test_no_criteria_met_derives_down_citing_first_criterion():
    client = FakeOllamaJudgeClient([_checklist_json([False, False], rationale="missed both")])

    result = judge_turn(
        client, "What is X and Y?", ["mentions X", "mentions Y"], "irrelevant.",
        _judge_config(), TURN_TYPE_BULK,
    )

    assert result.verdict == "down"
    # All failed, so the specific citation is uninformative by construction —
    # the first listed criterion is cited, never guessed or paraphrased.
    assert result.criterion_triggered == "mentions X"
    assert result.criteria_met == [False, False]


def test_down_citation_is_always_the_first_criterion_regardless_of_order():
    """The derived citation is positional (criteria[0]), not tied to which
    specific criterion 'feels' most representative of the failure."""
    client = FakeOllamaJudgeClient([_checklist_json([False, False, False])])

    result = judge_turn(
        client, "prompt", ["first", "second", "third"], "response",
        _judge_config(), TURN_TYPE_BULK,
    )

    assert result.criterion_triggered == "first"


@pytest.mark.parametrize("criteria_met", [
    [True, False],
    [False, True],
    [True, True, False],
    [False, True, True],
    [True, False, True],
])
def test_mixed_criteria_met_derives_none(criteria_met):
    criteria = [f"criterion {i}" for i in range(len(criteria_met))]
    client = FakeOllamaJudgeClient([_checklist_json(criteria_met, rationale="partial")])

    result = judge_turn(client, "prompt", criteria, "response", _judge_config(), TURN_TYPE_BULK)

    assert result.verdict == "none"
    assert result.criterion_triggered is None
    assert result.criteria_met == criteria_met


def test_single_criterion_prompt_can_only_derive_up_or_down_never_none():
    """A one-criterion prompt has no 'mixed' state — partial credit is
    impossible by construction, not by a special case in the derivation."""
    client_up = FakeOllamaJudgeClient([_checklist_json([True])])
    result_up = judge_turn(client_up, "prompt", ["mentions X"], "response", _judge_config(), TURN_TYPE_BULK)
    assert result_up.verdict == "up"

    client_down = FakeOllamaJudgeClient([_checklist_json([False])])
    result_down = judge_turn(client_down, "prompt", ["mentions X"], "response", _judge_config(), TURN_TYPE_BULK)
    assert result_down.verdict == "down"
    assert result_down.criterion_triggered == "mentions X"


# ---------------------------------------------------------------------------
# judge_turn — malformed criteria_met and parse-failure handling
# (all direction-neutral: JudgeUnparseableError, never a guessed verdict —
# the M-19/07-27 asymmetry lesson applied to the new protocol)
# ---------------------------------------------------------------------------


def test_missing_criteria_met_key_retries_then_raises():
    client = FakeOllamaJudgeClient([
        json.dumps({"rationale": "no checklist field"}),
        json.dumps({"rationale": "no checklist field"}),
    ])

    with pytest.raises(JudgeUnparseableError):
        judge_turn(client, "prompt", ["mentions X"], "response", _judge_config(), TURN_TYPE_BULK)

    assert len(client.calls) == 2


def test_criteria_met_wrong_length_retries_then_raises():
    client = FakeOllamaJudgeClient([
        _checklist_json([True]),  # prompt has 2 criteria
        _checklist_json([True, True, True]),  # still wrong on the re-ask
    ])

    with pytest.raises(JudgeUnparseableError):
        judge_turn(client, "prompt", ["mentions X", "mentions Y"], "response", _judge_config(), TURN_TYPE_BULK)

    assert len(client.calls) == 2


def test_criteria_met_too_short_retries_then_succeeds_on_correct_reask():
    client = FakeOllamaJudgeClient([
        _checklist_json([True]),  # wrong length
        _checklist_json([True, False]),  # correct on re-ask
    ])

    result = judge_turn(client, "prompt", ["mentions X", "mentions Y"], "response", _judge_config(), TURN_TYPE_BULK)

    assert result.verdict == "none"
    assert len(client.calls) == 2


@pytest.mark.parametrize("bad_criteria_met", [
    ["yes", "no"],           # strings, not booleans
    [1, 0],                  # ints — must not be silently truthy-coerced
    [True, None],            # one good, one null
    "true,false",            # not a list at all
    None,                    # missing entirely (explicit null)
])
def test_non_bool_or_non_list_criteria_met_retries_then_raises(bad_criteria_met):
    client = FakeOllamaJudgeClient([
        json.dumps({"criteria_met": bad_criteria_met, "rationale": "bad"}),
        json.dumps({"criteria_met": bad_criteria_met, "rationale": "bad"}),
    ])

    with pytest.raises(JudgeUnparseableError):
        judge_turn(client, "prompt", ["mentions X", "mentions Y"], "response", _judge_config(), TURN_TYPE_BULK)


def test_malformed_json_retries_once_then_raises():
    client = FakeOllamaJudgeClient(["not json", "still not json"])

    with pytest.raises(JudgeUnparseableError):
        judge_turn(client, "What is X?", ["mentions X"], "X is Y.", _judge_config(), TURN_TYPE_BULK)

    assert len(client.calls) == 2


def test_retry_succeeds_after_first_malformed_response():
    client = FakeOllamaJudgeClient(["not json", _checklist_json([False], rationale="ambiguous... no, clearly missing")])

    result = judge_turn(client, "What is X?", ["mentions X"], "X is Y.", _judge_config(), TURN_TYPE_BULK)

    assert result.verdict == "down"
    assert len(client.calls) == 2


def test_non_object_json_retries_then_raises():
    responses = [json.dumps([True, False]), json.dumps([True, False])]
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
# _build_judge_prompt — regression coverage for the checklist protocol's
# request shape (no live model call; asserts on the composed prompt text)
# ---------------------------------------------------------------------------


def test_prompt_asks_for_criteria_met_list_in_order():
    from harness.judge import _build_judge_prompt

    prompt = _build_judge_prompt("What is X?", ["mentions X", "mentions Y"], "X is Y.")

    assert "criteria_met" in prompt
    assert "1. mentions X" in prompt
    assert "2. mentions Y" in prompt
    assert "exactly 2 true/false booleans" in prompt
    # The old free-text protocol's field must not leak back in.
    assert "criterion_triggered" not in prompt


# ---------------------------------------------------------------------------
# _normalize_criterion / _resolve_criterion — retained per BUILD-PLAN.md's
# Milestone 31 spec ("stays for citation resolution wherever the judge names
# criteria in prose") even though the checklist protocol's primary parse path
# no longer calls them. Tested directly now, not through judge_turn(), since
# judge_turn() no longer exercises citation resolution at all.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("citation", [
    "local temporal readings/local time",           # the exact exp0 failure
    "answer: local temporal readings/local time",   # verbatim
    "**local temporal readings/local time**",       # markdown emphasis
    "Local Temporal Readings/Local Time.",           # case + trailing period
    '"local temporal readings/local time"',          # quoted
    "  local temporal   readings/local time  ",      # whitespace noise
    "criterion: answer: local temporal readings/local time",  # stacked labels
])
def test_normalized_citation_resolves_to_exact_bank_criterion(citation):
    criteria = ["answer: local temporal readings/local time", "mentions UTC"]

    resolved = _resolve_criterion(citation, criteria)

    # Always the bank's exact text, never the judge's wording.
    assert resolved == "answer: local temporal readings/local time"


def test_normalization_does_not_strip_leading_label_words_without_a_colon():
    """'states three' must survive: only 'label:' prefixes are stripped."""
    criteria = ["states three", "confirms flattening requirement"]

    resolved = _resolve_criterion("States three", criteria)

    assert resolved == "states three"


def test_normalization_does_not_conflate_distinct_criteria():
    criteria = ["states three", "states four"]

    resolved = _resolve_criterion("states five", criteria)

    assert resolved is None


def test_single_criterion_prompt_resolves_unmatched_citation_by_deduction():
    """A 'down' on a one-criterion prompt can only mean that criterion."""
    resolved = _resolve_criterion("garbled", ["mentions X"])

    assert resolved == "mentions X"


def test_resolve_criterion_null_citation_on_multi_criterion_prompt_is_unresolved():
    resolved = _resolve_criterion(None, ["mentions X", "mentions Y"])

    assert resolved is None


@pytest.mark.parametrize("raw,expected", [
    ("answer: UTC", "utc"),
    ("**UTC**", "utc"),
    ("UTC.", "utc"),
    ("  UTC  ", "utc"),
    ('"UTC"', "utc"),
    ("criterion: answer: UTC", "utc"),
])
def test_normalize_criterion_strips_labels_formatting_and_case(raw, expected):
    assert _normalize_criterion(raw) == expected


# ---------------------------------------------------------------------------
# OllamaJudgeClient — real HTTP request shape, via httpx.MockTransport
# (no network call; verifies the request the module actually sends)
# ---------------------------------------------------------------------------


def test_ollama_judge_client_chat_json_sends_expected_request():
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        captured["body"] = json.loads(request.content)
        return httpx.Response(200, json={"message": {"content": _checklist_json([True])}})

    client = OllamaJudgeClient(host=DEFAULT_JUDGE_HOST)
    client._client = httpx.Client(base_url=DEFAULT_JUDGE_HOST, transport=httpx.MockTransport(handler))

    result = client.chat_json("gemma4:e4b", 0.0, "grade this")

    assert captured["url"].endswith("/api/chat")
    assert captured["body"]["model"] == "gemma4:e4b"
    assert captured["body"]["format"] == "json"
    assert captured["body"]["options"]["temperature"] == 0.0
    assert result == _checklist_json([True])


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
