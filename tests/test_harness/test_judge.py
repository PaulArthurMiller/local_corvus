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

Milestone 37 (Paul, 2026-08-05) tightened the layer above the derivation,
which the 2026-08-04 calibration audit found systematically permissive: the
checklist now carries a supporting quote per criterion, criteria are tagged
[EXACT] or [CONCEPT] and held to different standards, and the host verifies
the evidence. See the "Milestone 37 — the evidence layer" section below for
what that verification can and cannot change about a verdict.

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
    CRITERION_KIND_CONCEPT,
    CRITERION_KIND_EXACT,
    DEFAULT_JUDGE_HOST,
    EVIDENCE_ANSWER_TOKEN_ABSENT,
    EVIDENCE_DEMOTED_NO_QUOTE,
    EVIDENCE_UNVERIFIED_QUOTE,
    JudgeUnparseableError,
    JudgeVerdict,
    OllamaJudgeClient,
    ProbeTurnJudgeError,
    _criterion_kind,
    _exact_answer_tokens,
    _normalize_criterion,
    _resolve_criterion,
    derive_verdict,
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


def test_prompt_asks_for_a_quote_bearing_checklist_in_order():
    from harness.judge import _build_judge_prompt

    prompt = _build_judge_prompt("What is X?", ["mentions X", "mentions Y"], "X is Y.")

    assert '"checklist"' in prompt
    assert "1. [CONCEPT] mentions X" in prompt
    assert "2. [CONCEPT] mentions Y" in prompt
    assert "exactly 2 objects" in prompt
    # M37 — the quote field is what makes a judgment checkable; it must be
    # requested before "met" in the schema example, so the model commits to
    # evidence before the judgment rather than justifying one it already made.
    assert prompt.index('"quote"') < prompt.index('"met"')
    # The old free-text protocol's field must not leak back in.
    assert "criterion_triggered" not in prompt


def test_prompt_tags_exact_answer_criteria_differently_from_concept_criteria():
    """
    M37 — the bank holds two criterion families and one blanket grading rule
    breaks whichever it was not written for. "answer: L-2" demands the literal
    token; "mentions fixed ordering" is deliberately paraphrase-tolerant, and
    holding it to a literal standard would swap a lenient judge for an
    over-strict one rather than fixing the calibration.
    """
    from harness.judge import _build_judge_prompt

    prompt = _build_judge_prompt(
        "Which rule?", ["answer: L-2", "mentions fixed ordering"], "It is V-1.",
    )

    assert "1. [EXACT] answer: L-2" in prompt
    assert "2. [CONCEPT] mentions fixed ordering" in prompt
    assert "Never treat two different labels as synonyms." in prompt
    assert "If you are unsure, answer false. False is the default." in prompt


@pytest.mark.parametrize("criterion,kind,tokens", [
    ("answer: L-2", CRITERION_KIND_EXACT, ["L-2"]),
    ("answer: UTC (Universal Coordinated Time)", CRITERION_KIND_EXACT,
     ["UTC", "Universal Coordinated Time"]),
    ("answer: local temporal readings/local time", CRITERION_KIND_EXACT,
     ["local temporal readings", "local time"]),
    ("answer: seven (L-1 through L-7)", CRITERION_KIND_EXACT, ["seven", "L-1 through L-7"]),
    # An "answer:" whose answer is a whole clause has no token to match
    # literally — it is a concept criterion wearing an answer label.
    ("answer: all claims must remain fully auditable", CRITERION_KIND_CONCEPT, []),
    ("mentions consistent/fixed field ordering", CRITERION_KIND_CONCEPT, []),
])
def test_criterion_family_classification(criterion, kind, tokens):
    assert _criterion_kind(criterion) == kind
    assert _exact_answer_tokens(criterion) == tokens


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


# ---------------------------------------------------------------------------
# Milestone 37 — the evidence layer.
#
# M31 fixed how the verdict is derived from the checklist. The 2026-08-04
# calibration audit then found the layer above it systematically permissive:
# all three operator overrides were the judge marking a criterion true on a
# looser standard than the criterion states, every one lenient in direction.
# These tests pin what the quote requirement can and cannot change.
# ---------------------------------------------------------------------------


def _checklist_objects_json(entries, rationale="ok"):
    """entries: list of (quote, met) pairs, in criterion order."""
    return json.dumps({
        "checklist": [{"quote": q, "met": m} for q, m in entries],
        "rationale": rationale,
    })


def test_criterion_met_with_no_quote_is_forced_false():
    """
    The protocol tells the judge that an empty quote requires met=false, so
    (met=true, quote="") is the checklist contradicting itself — the exact
    turn-5 defect from the 2026-08-04 audit. Forcing it false resolves the
    contradiction the only way the protocol allows; it is not a second guess
    at the judgment.
    """
    client = FakeOllamaJudgeClient([
        _checklist_objects_json([("", True), ("the fields stay in order", True)])
    ])

    result = judge_turn(
        client, "Why does order matter?", ["answer: L-2", "mentions fixed ordering"],
        "The fields stay in order.", _judge_config(), TURN_TYPE_BULK,
    )

    assert result.criteria_met == [False, True]
    assert result.verdict == "none"  # mixed -> abstain, per M31's rule
    assert result.evidence_flags[0] == [EVIDENCE_DEMOTED_NO_QUOTE]
    assert result.evidence_flags[1] == []
    assert result.evidence_enforced is True


def test_invented_synonym_is_flagged_but_not_demoted():
    """
    Turn 12 of the 2026-08-04 audit: the judge credited "answer: L-2" because
    it decided a different rule ID was "a recognized synonym". The quote is
    real, so nothing here is a protocol violation — the host flags it for the
    operator and the calibration summary, and deliberately does NOT change
    the verdict. Silently reclassifying on a heuristic would corrupt the
    record the calibration pass exists to produce.
    """
    client = FakeOllamaJudgeClient([
        _checklist_objects_json([("the applicable rule is V-1", True)],
                                rationale="V-1 is a recognized synonym for L-2")
    ])

    result = judge_turn(
        client, "Which rule applies?", ["answer: L-2"],
        "The applicable rule is V-1.", _judge_config(), TURN_TYPE_BULK,
    )

    assert result.criteria_met == [True]
    assert result.verdict == "up"
    assert EVIDENCE_ANSWER_TOKEN_ABSENT in result.evidence_flags[0]


def test_quote_absent_from_the_response_is_flagged():
    client = FakeOllamaJudgeClient([
        _checklist_objects_json([("the applicable rule is L-2", True)])
    ])

    result = judge_turn(
        client, "Which rule?", ["answer: L-2"], "I could not determine the rule.",
        _judge_config(), TURN_TYPE_BULK,
    )

    assert result.criteria_met == [True]  # flagged, not demoted
    assert EVIDENCE_UNVERIFIED_QUOTE in result.evidence_flags[0]


@pytest.mark.parametrize("quote", [
    "The fields stay in order",          # exact
    "the fields stay in order",          # case
    "  the fields   stay in order  ",    # whitespace
    '"the fields stay in order"',        # wrapped in quote marks
    "**the fields stay in order**",      # markdown emphasis
    "the fields stay in order...",       # trailing ellipsis
])
def test_cosmetically_reformatted_quotes_still_verify(quote):
    """
    The check is strict about words and loose about presentation. A judge that
    re-cases or re-wraps a real quote is still quoting; one that changes the
    words is not. Being brittle here would flood the calibration pass with
    false flags and make the real ones invisible.
    """
    client = FakeOllamaJudgeClient([_checklist_objects_json([(quote, True)])])

    result = judge_turn(
        client, "Why?", ["mentions fixed ordering"],
        "The fields stay in order, always.", _judge_config(), TURN_TYPE_BULK,
    )

    assert result.evidence_flags[0] == []


def test_bare_boolean_checklist_still_parses_with_enforcement_skipped():
    """
    Backward tolerance for the M31 shape. Rejecting a correctly-graded
    checklist that arrived under the old key costs a re-ask and possibly a
    whole turn's verdict; accepting it costs nothing and changes no judgment.
    evidence_enforced records that this turn was graded under the other
    regime, so the two are never pooled in analysis.
    """
    client = FakeOllamaJudgeClient([_checklist_json([True, True])])

    result = judge_turn(
        client, "What is X?", ["mentions X", "mentions Y"], "X and Y.",
        _judge_config(), TURN_TYPE_BULK,
    )

    assert result.verdict == "up"
    assert result.criteria_met == [True, True]
    assert result.evidence_enforced is False
    assert result.criteria_evidence is None
    assert result.evidence_flags is None


def test_checklist_omitting_the_quote_key_entirely_skips_enforcement():
    """
    A 4B judge that drops an output field is a protocol miss, not a response
    that failed everything. Demoting the whole checklist would manufacture a
    "down" out of a formatting failure — the one direction of error this
    milestone must not introduce while fixing the other.
    """
    client = FakeOllamaJudgeClient([
        json.dumps({"checklist": [{"met": True}, {"met": True}], "rationale": "ok"})
    ])

    result = judge_turn(
        client, "What is X?", ["mentions X", "mentions Y"], "X and Y.",
        _judge_config(), TURN_TYPE_BULK,
    )

    assert result.verdict == "up"
    assert result.criteria_met == [True, True]
    assert result.evidence_enforced is False


def test_supplied_empty_quote_is_demoted_even_on_a_single_criterion_prompt():
    """
    The distinction the dropped-field bailout must not swallow. Omitting
    "quote" is a dropped field; supplying "quote": "" while claiming met is
    the model answering the field and contradicting itself.

    Single-criterion prompts are the case that makes this matter: with one
    criterion there is no sibling entry to carry a quote, so an earlier draft
    that keyed the bailout on "no non-empty quote anywhere" exempted every
    one of them — and single-criterion prompts are the entire cluster_d
    "answer: X" family, exactly where the 2026-08-04 audit's synonym defect
    lives. The bailout keys on the *key's presence* instead.
    """
    client = FakeOllamaJudgeClient([_checklist_objects_json([("", True)])])

    result = judge_turn(
        client, "Which rule?", ["answer: L-2"], "I am not sure which rule applies.",
        _judge_config(), TURN_TYPE_BULK,
    )

    assert result.criteria_met == [False]
    assert result.verdict == "down"
    assert result.criterion_triggered == "answer: L-2"
    assert result.evidence_enforced is True
    assert result.evidence_flags[0] == [EVIDENCE_DEMOTED_NO_QUOTE]


def test_checklist_under_the_criteria_key_is_accepted():
    """Key drift ("criteria" instead of "checklist") is a re-ask not worth spending."""
    client = FakeOllamaJudgeClient([
        json.dumps({"criteria": [{"quote": "X is here", "met": True}], "rationale": "ok"})
    ])

    result = judge_turn(
        client, "What is X?", ["mentions X"], "X is here.", _judge_config(), TURN_TYPE_BULK,
    )

    assert result.verdict == "up"
    assert result.criteria_evidence == ["X is here"]


@pytest.mark.parametrize("payload", [
    '{"checklist": [{"quote": "a", "met": true}]}',                               # too short
    '{"checklist": [{"quote": "a", "met": true}, {"met": true}, {"met": false}]}',  # too long
    '{"checklist": [{"quote": "a", "met": "yes"}, {"met": true}]}',               # non-bool met
    '{"rationale": "no checklist at all"}',                                       # missing key
    '{"checklist": "not a list"}',
    "not json at all",
])
def test_malformed_checklists_raise_unparseable_after_reask(payload):
    """
    Every parse failure stays direction-neutral (the M31/2026-07-27 lesson):
    there is no half-trusted verdict to salvage, so nothing here can
    preferentially destroy "down" verdicts the way the old free-text citation
    protocol could.
    """
    client = FakeOllamaJudgeClient([payload, payload])

    with pytest.raises(JudgeUnparseableError):
        judge_turn(
            client, "What is X?", ["mentions X", "mentions Y"], "X and Y.",
            _judge_config(), TURN_TYPE_BULK,
        )

    assert len(client.calls) == 2  # one call + one re-ask


def test_response_text_reaches_both_the_judge_prompt_and_the_evidence_check():
    """
    A regression guard on the wiring: the same response has to reach both the
    model (so it can quote) and the host (so the quote can be checked). If
    only the prompt got it, every quote would fail verification and the flag
    would be meaningless noise.
    """
    client = FakeOllamaJudgeClient([_checklist_objects_json([("stays in order", True)])])

    result = judge_turn(
        client, "Why?", ["mentions fixed ordering"], "It stays in order.",
        _judge_config(), TURN_TYPE_BULK,
    )

    assert "It stays in order." in client.calls[0]["prompt"]
    assert result.evidence_flags[0] == []


@pytest.mark.parametrize("criteria_met,expected_verdict,expected_citation", [
    ([True], "up", None),
    ([True, True], "up", None),
    ([False], "down", "c1"),
    ([False, False], "down", "c1"),
    ([True, False], "none", None),
    ([False, True], "none", None),
])
def test_derive_verdict_is_the_single_shared_rule(criteria_met, expected_verdict, expected_citation):
    """
    Public and shared with the runner's --interactive regrade. If the two
    sides applied different rules, a calibration pass would measure the rule
    rather than the judge. M37 leaves the rule itself untouched — the
    2026-08-04 audit found no override indicting it.
    """
    criteria = ["c1", "c2"][:len(criteria_met)]

    assert derive_verdict(criteria_met, criteria) == (expected_verdict, expected_citation)
