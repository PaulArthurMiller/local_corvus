"""
Tests for scripts/harness/prompt_gen.py — the only file that talks to the
Anthropic API. Every Sonnet call is mocked via FakeAnthropicClient; no test
here ever hits the real API. Fixture-reading tests use the real, committed
seed_fixtures/seed_v2.json.gz (no network, no Docker required).
"""

import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "scripts"))
# backend/ too, so the keyword-window mirror tests can compare prompt_gen's
# duplicated tokenizer against retrieval.py's real one (same insert
# test_memory/test_retrieval.py uses; `memory` must resolve to backend/memory,
# not the repo-root memory/ data directory).
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "backend"))

import harness.prompt_gen as prompt_gen_module
from harness.prompt_bank import PromptBank, PromptBankEntry, save_bank
from harness.prompt_gen import (
    GenerationAborted,
    MissingApiKeyError,
    _finalize_entry,
    _generation_params,
    _get_api_key,
    build_bank,
    compute_bank_hash,
    compute_fixture_manifest_hash,
    distribute_counts,
    distribute_hop_levels,
    estimate_bank_cost_for_plan,
    estimate_generation_cost,
    fingerprint_scan,
    fixture_path_for_version,
    generate_cluster_prompts_safely,
    load_pricing,
    read_cluster_content,
)
from harness.schema import RunPlan

FIXTURES_DIR = Path(__file__).parent.parent.parent / "seed_fixtures"
FIXTURE_PATH = FIXTURES_DIR / "seed_v2.json.gz"
FIXTURE_PATH_V3 = FIXTURES_DIR / "seed_v3.json.gz"


class FakeTextBlock:
    def __init__(self, text):
        self.type = "text"
        self.text = text


class FakeUsage:
    def __init__(self, input_tokens=10, output_tokens=10):
        self.input_tokens = input_tokens
        self.output_tokens = output_tokens


class FakeResponse:
    def __init__(self, text, input_tokens=10, output_tokens=10):
        self.content = [FakeTextBlock(text)]
        self.usage = FakeUsage(input_tokens, output_tokens)


class FakeMessages:
    def __init__(self, responses):
        self._responses = list(responses)
        self.calls = []

    def create(self, model, max_tokens, messages, **kwargs):
        # **kwargs so the fake accepts every optional request field the real
        # SDK does — `thinking` in particular (see the coherence-gate
        # regression test below). Recorded, not ignored: tests assert on it.
        self.calls.append({
            "model": model, "max_tokens": max_tokens, "messages": messages,
            "streamed": False, **kwargs,
        })
        if not self._responses:
            raise AssertionError("FakeMessages exhausted; unexpected Sonnet call")
        resp = self._responses.pop(0)
        if isinstance(resp, Exception):
            raise resp
        return resp

    def stream(self, model, max_tokens, messages, **kwargs):
        """Generation calls stream (large max_tokens; see _call_sonnet). Recorded
        with streamed=True so tests can assert which endpoint was used."""
        self.calls.append({
            "model": model, "max_tokens": max_tokens, "messages": messages,
            "streamed": True, **kwargs,
        })
        if not self._responses:
            raise AssertionError("FakeMessages exhausted; unexpected Sonnet call")
        return FakeStreamContext(self._responses.pop(0))


class FakeStreamContext:
    """Mimics the SDK's messages.stream() context manager well enough for
    _call_sonnet: `with client.messages.stream(...) as s: s.get_final_message()`."""

    def __init__(self, response):
        self._response = response

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def get_final_message(self):
        if isinstance(self._response, Exception):
            raise self._response
        return self._response


class FakeAnthropicClient:
    def __init__(self, responses):
        self.messages = FakeMessages(responses)


def _plan(**overrides):
    kwargs = dict(
        plan_id="p1",
        plan_name="Test plan",
        conceptual_fields={"count": 4, "subjects": "auto"},
        series_count=2,
        turns_per_series=5,
        probe_ratio=0.2,
    )
    kwargs.update(overrides)
    return RunPlan(**kwargs)


# ---------------------------------------------------------------------------
# Fingerprint scan — pure function, no mocking needed
# ---------------------------------------------------------------------------

def test_fingerprint_scan_fails_for_wrong_cluster():
    text = "This response discusses the rule and the constraint in detail."
    leaked = fingerprint_scan(text, "cluster_a")
    assert "rule" in leaked
    assert "constraint" in leaked


def test_fingerprint_scan_passes_for_own_cluster():
    text = "This response discusses the rule and the constraint in detail."
    assert fingerprint_scan(text, "cluster_c") == []


# ---------------------------------------------------------------------------
# Coherence gate must disable thinking (regression, 2026-07-26)
#
# On claude-sonnet-5 adaptive thinking is ON when the `thinking` parameter is
# omitted, and thinking.display defaults to "omitted". Since max_tokens caps
# thinking + response text together, the coherence gate's tight budget was
# consumed entirely by an empty-text thinking block: _call_sonnet extracted
# "" and EVERY prompt that got past the fingerprint scan was scored
# "coherence validation failed", making a usable bank impossible to generate.
# ---------------------------------------------------------------------------

def test_validate_one_disables_thinking_on_the_coherence_call():
    client = FakeAnthropicClient([FakeResponse("YES")])

    reasons = prompt_gen_module._validate_one(
        client, "claude-sonnet-5", "cluster content here",
        "A prompt with no leaked vocabulary at all.", "cluster_a", "v3",
    )

    assert reasons == []
    call = client.messages.calls[0]
    assert call["thinking"] == {"type": "disabled"}, (
        "the coherence gate must disable thinking — with it on by default, the "
        "max_tokens budget goes to an empty-text thinking block and the gate "
        "always reads ''"
    )


def test_validate_one_budget_survives_a_preamble():
    """The gate is a substring match, so max_tokens must leave room for more
    than a bare 'YES' — a 10-token budget was the original failure mode."""
    client = FakeAnthropicClient([FakeResponse("YES")])

    prompt_gen_module._validate_one(
        client, "claude-sonnet-5", "cluster content here",
        "A prompt with no leaked vocabulary at all.", "cluster_a", "v3",
    )

    assert client.messages.calls[0]["max_tokens"] >= 32


def test_validate_one_empty_text_is_still_reported_as_a_failure():
    """If a future model returns no text despite thinking being disabled, the
    gate must fail loudly rather than silently passing the prompt."""
    client = FakeAnthropicClient([FakeResponse("")])

    reasons = prompt_gen_module._validate_one(
        client, "claude-sonnet-5", "cluster content here",
        "A prompt with no leaked vocabulary at all.", "cluster_a", "v3",
    )

    assert len(reasons) == 1
    assert "coherence validation failed" in reasons[0]


class TestGenerationOutputBudget:
    """The flat 400-per-target budget broke in both directions once adaptive
    thinking became the default — see generation_max_tokens()."""

    def test_single_target_gets_the_floor_not_400(self):
        """The per-target retry/fallback path is what got 400 and returned ''."""
        assert prompt_gen_module.generation_max_tokens(1) >= 8000

    def test_budget_scales_with_target_count(self):
        assert prompt_gen_module.generation_max_tokens(53) > prompt_gen_module.generation_max_tokens(3)

    def test_a_full_cluster_batch_has_real_headroom(self):
        """53 prompts is the real experiment-0 per-cluster batch; the old
        formula gave 21200 for it, all of which had to cover thinking too."""
        assert prompt_gen_module.generation_max_tokens(53) >= 700 * 53

    def test_zero_targets_still_gets_the_floor(self):
        assert prompt_gen_module.generation_max_tokens(0) >= 8000

    def test_generation_streams(self):
        """A large max_tokens non-streaming risks an SDK HTTP timeout."""
        client = FakeAnthropicClient([FakeResponse('[{"prompt_text": "q", "criteria": ["c"], "hop_distance": 0}]')])
        prompt_gen_module.generate_cluster_prompts(
            client, "claude-sonnet-5", {"full_content": "content"},
            [{"hop_distance": 0, "target_text": "t"}], version="v3", cluster_id="cluster_a",
        )
        call = client.messages.calls[0]
        assert call["streamed"] is True
        assert call["max_tokens"] == prompt_gen_module.generation_max_tokens(1)

    def test_coherence_gate_does_not_stream(self):
        """Small budget, tiny response — streaming buys nothing there."""
        client = FakeAnthropicClient([FakeResponse("YES")])
        prompt_gen_module._validate_one(
            client, "claude-sonnet-5", "content", "A clean prompt.", "cluster_a", "v3",
        )
        assert client.messages.calls[0]["streamed"] is False


def test_generation_call_does_not_disable_thinking():
    """Only the coherence gate disables thinking. Generation gets a large
    max_tokens and benefits from reasoning, so it keeps the model default."""
    client = FakeAnthropicClient([FakeResponse('[{"prompt_text": "q", "criteria": ["c"], "hop_distance": 0}]')])

    prompt_gen_module.generate_cluster_prompts(
        client, "claude-sonnet-5",
        {"full_content": "cluster content"},
        [{"hop_distance": 0, "target_text": "target"}],
        version="v3",
    )

    assert "thinking" not in client.messages.calls[0]


# ---------------------------------------------------------------------------
# v3 keyword-window scoping (2026-07-26)
#
# The cross-cluster entry-vocabulary rule is enforced only inside the BM25
# keyword window — the first retrieval_keyword_count non-stopword tokens,
# which is all retrieve() ever reads as keywords. Answer tokens stay
# whole-prompt. See fingerprint_scan's docstring for the full rationale.
# ---------------------------------------------------------------------------

class TestBm25KeywordWindowMirror:
    """prompt_gen duplicates retrieval.py's tokenizer (host-side script, no
    backend import). These tests fail if the copies ever diverge."""

    def test_stopwords_match_the_backend(self):
        from memory import retrieval

        assert prompt_gen_module._STOPWORDS == retrieval._STOPWORDS

    def test_word_regex_matches_the_backend(self):
        from memory import retrieval

        assert prompt_gen_module._WORD_RE.pattern == retrieval._WORD_RE.pattern

    def test_tokenization_matches_the_backend(self):
        from memory import retrieval

        text = "Format, structure and the delimiter -- what does rule K-2 require of a file?"
        assert (
            prompt_gen_module.bm25_keywords(text, keyword_count=5)
            == retrieval._tokenize(text)[:5]
        )

    def test_keyword_count_read_from_config(self):
        """The real config.yaml value is what generation is validated against."""
        assert prompt_gen_module.load_retrieval_keyword_count() == 5


class TestV3FingerprintKeywordWindow:
    def test_other_cluster_vocab_inside_the_window_still_leaks(self):
        # "schema" (cluster_b) lands in the first 5 non-stopword tokens.
        prompt = "Schema and data questions aside, what does the Kestrel index cover?"
        leaked = fingerprint_scan(prompt, "cluster_a", "v3")
        assert "schema" in leaked

    def test_other_cluster_vocab_past_the_window_is_allowed(self):
        # Own-cluster vocab front-loaded; "rule" (cluster_c) sits well past
        # token 5, where retrieve() can never see it. This is the exact shape
        # that was previously unsatisfiable.
        prompt = (
            "Format, structure, delimiter, header, encoding — which numbered "
            "rule covers the ordering of columns in a data file?"
        )
        assert fingerprint_scan(prompt, "cluster_a", "v3") == []

    def test_the_word_rule_is_now_usable_outside_the_window(self):
        """The headline unblocker: cluster_a/b/d prompts may say "rule"."""
        prompt = "Kestrel output format conventions — which rule governs padding?"
        assert "rule" not in fingerprint_scan(prompt, "cluster_a", "v3")

    def test_answer_tokens_leak_anywhere_in_the_prompt(self):
        """Tier 1 is unchanged: whole-prompt, every cluster. An answer token
        past the window still hands the agent the answer, so it must fail."""
        prompt = (
            "Format, structure, delimiter, header, encoding — does this file "
            "have to follow the declared spine order?"
        )
        leaked = fingerprint_scan(prompt, "cluster_a", "v3")
        assert "spine order" in leaked

    def test_own_cluster_answer_token_also_leaks(self):
        prompt = "Marrow schema shape rules: what is a girth line?"
        assert "girth line" in fingerprint_scan(prompt, "cluster_b", "v3")

    def test_own_cluster_entry_vocab_is_free_inside_the_window(self):
        prompt = "Format, structure, output, field, header conventions — what applies?"
        assert fingerprint_scan(prompt, "cluster_a", "v3") == []

    def test_window_width_is_injectable(self):
        """A wider window catches a term a narrower one lets through, proving
        the scan is genuinely position-scoped rather than accidentally passing."""
        prompt = "Format structure output field schema questions"  # "schema" is token 5
        assert fingerprint_scan(prompt, "cluster_a", "v3", keyword_count=4) == []
        assert "schema" in fingerprint_scan(prompt, "cluster_a", "v3", keyword_count=5)

    def test_v2_scan_is_untouched_by_the_change(self):
        """v2 keeps whole-prompt semantics — no v2 bank is being regenerated."""
        text = "A long preamble that eventually gets around to the constraint at the end."
        assert "constraint" in fingerprint_scan(text, "cluster_a", "v2")


class TestV3GenerationPromptStatesItsRules:
    def test_brief_names_own_vocab_and_other_cluster_vocab(self):
        brief = prompt_gen_module._v3_vocabulary_brief("cluster_a", keyword_count=5)
        assert "Kestrel" in brief          # own family name — required up front
        assert "delimiter" in brief        # own entry vocab
        assert "rule" in brief             # cluster_c's, to keep out of the window
        assert "first 5 non-trivial words" in brief

    def test_v3_generation_prompt_includes_the_brief(self):
        prompt = prompt_gen_module._build_generation_prompt(
            "cluster content", [{"hop_distance": 0, "target_text": "t"}],
            None, "v3", "cluster_a",
        )
        assert "VOCABULARY RULES" in prompt
        # v2's blanket instruction must not also be present — it points the
        # opposite way for v3 (own-cluster vocab is required, not forbidden).
        assert "must NOT quote or repeat the cluster's distinctive vocabulary" not in prompt

    def test_v2_generation_prompt_unchanged(self):
        prompt = prompt_gen_module._build_generation_prompt(
            "cluster content", [{"hop_distance": 0, "target_text": "t"}], None, "v2",
        )
        assert "VOCABULARY RULES" not in prompt
        assert "must NOT quote or repeat the cluster's distinctive vocabulary" in prompt

    def test_generation_threads_cluster_id_into_the_prompt(self):
        client = FakeAnthropicClient([FakeResponse('[{"prompt_text": "q", "criteria": ["c"], "hop_distance": 0}]')])
        prompt_gen_module.generate_cluster_prompts(
            client, "claude-sonnet-5", {"full_content": "content"},
            [{"hop_distance": 0, "target_text": "t"}], version="v3", cluster_id="cluster_c",
        )
        sent = client.messages.calls[0]["messages"][0]["content"]
        assert "VOCABULARY RULES" in sent
        assert "Vessel" in sent  # cluster_c's family name


def test_fingerprint_scan_uses_whole_word_matching():
    # "format" as a substring of "reformatting" must not falsely trigger.
    text = "We are reformatting the document."
    assert fingerprint_scan(text, "cluster_b") == []


# ---------------------------------------------------------------------------
# M28 — v3 two-tier fingerprint scan (spec §5)
# ---------------------------------------------------------------------------

def test_v3_fingerprint_scan_passes_own_cluster_entry_vocab():
    # cluster_a's own entry vocabulary is free — that's how BM25 is meant
    # to reach the hub.
    text = "What format and structure applies to the output?"
    assert fingerprint_scan(text, "cluster_a", version="v3") == []


def test_v3_fingerprint_scan_fails_other_cluster_entry_vocab():
    text = "What does the schema say about column validation?"
    leaked = fingerprint_scan(text, "cluster_a", version="v3")
    assert "schema" in leaked
    assert "column" in leaked


def test_v3_fingerprint_scan_fails_other_cluster_family_name():
    text = "Ask Marrow about this."
    assert "Marrow" in fingerprint_scan(text, "cluster_a", version="v3")


def test_v3_fingerprint_scan_fails_answer_token_from_own_cluster():
    # An answer token from the prompt's OWN target cluster is still a leak
    # in v3 — it would let BM25 reach the gold spoke directly.
    text = "What is TILDEBAR used for in this format?"
    assert "TILDEBAR" in fingerprint_scan(text, "cluster_a", version="v3")


def test_v3_fingerprint_scan_fails_answer_token_from_any_cluster():
    # WAIVERKEY belongs to cluster_c; leaking it into a cluster_a prompt
    # must still be caught (any-cluster rule, not just other-cluster).
    text = "Does this relate to a WAIVERKEY somehow?"
    assert "WAIVERKEY" in fingerprint_scan(text, "cluster_a", version="v3")


def test_v3_fingerprint_scan_multiword_answer_token():
    text = "Is this about spine order in the file?"
    assert "spine order" in fingerprint_scan(text, "cluster_b", version="v3")


# ---------------------------------------------------------------------------
# M28 — fixture_path_for_version
# ---------------------------------------------------------------------------

def test_fixture_path_for_version_v2():
    assert fixture_path_for_version("v2", FIXTURES_DIR) == FIXTURE_PATH


def test_fixture_path_for_version_v3():
    assert fixture_path_for_version("v3", FIXTURES_DIR) == FIXTURE_PATH_V3


# ---------------------------------------------------------------------------
# Deterministic N-distribution
# ---------------------------------------------------------------------------

def test_distribute_counts_is_exact_and_deterministic():
    counts1 = distribute_counts(21)
    counts2 = distribute_counts(21)
    assert counts1 == counts2
    assert sum(counts1.values()) == 21
    assert counts1 == {"cluster_a": 6, "cluster_b": 5, "cluster_c": 5, "cluster_d": 5}


def test_distribute_counts_handles_zero():
    assert distribute_counts(0) == {"cluster_a": 0, "cluster_b": 0, "cluster_c": 0, "cluster_d": 0}


def test_distribute_hop_levels_round_robin():
    assert distribute_hop_levels(5, [0, 1]) == [0, 1, 0, 1, 0]


def test_distribute_hop_levels_empty_levels_defaults_to_zero():
    assert distribute_hop_levels(3, []) == [0, 0, 0]


# ---------------------------------------------------------------------------
# Missing API key
# ---------------------------------------------------------------------------

def test_missing_api_key_fails_fast_with_named_error(monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    with pytest.raises(MissingApiKeyError):
        _get_api_key()


# ---------------------------------------------------------------------------
# Malformed JSON handling
# ---------------------------------------------------------------------------

def test_generate_cluster_prompts_safely_falls_back_after_malformed_json_retry():
    client = FakeAnthropicClient([
        FakeResponse("this is prose, not json"),   # batch attempt 1
        FakeResponse("still not json"),             # batch attempt 2 (the "one retry")
        FakeResponse("nope, still prose"),          # per-prompt fallback for the only target
    ])
    content = {"full_content": "cluster a content", "hub_text": "hub text", "spoke_text": "spoke text"}
    targets = [{"hop_distance": 0, "target_text": "hub text"}]

    items = generate_cluster_prompts_safely(
        client, "claude-sonnet-5", "cluster_a", content, targets, None, print_fn=lambda *_a, **_k: None,
    )

    assert items == [{}]


def test_finalize_entry_v3_excludes_answer_token_leak():
    # _finalize_entry must thread seed_fixture_version into _validate_one's
    # fingerprint_scan call — an item whose prompt_text leaks an answer
    # token must be excluded without ever reaching the Sonnet coherence
    # call (fingerprint_scan short-circuits it), so no Sonnet response is
    # queued at all — an unexpected call would raise AssertionError.
    client = FakeAnthropicClient([
        FakeResponse('[{"prompt_text": "clean retry", "criteria": ["c1"]}]'),  # regeneration attempt
        FakeResponse("YES"),  # coherence check on the clean retry
    ])
    content = {"full_content": "cluster a content", "hub_text": "hub text", "spoke_text": "spoke text"}
    target = {"hop_distance": 0, "target_text": "hub text"}
    leaking_item = {"prompt_text": "What is TILDEBAR for?", "criteria": ["c1"]}

    entry = _finalize_entry(
        client, "claude-sonnet-5", "cluster_a", content, target, leaking_item,
        "v3", "claude-sonnet-5@2026-07-11", max_retries=1, prompt_id="cluster_a_0000",
        print_fn=lambda *_a, **_k: None,
    )

    assert entry.validated is True  # the retry succeeded cleanly
    assert entry.prompt_text == "clean retry"
    assert entry.seed_fixture_version == "v3"


def test_malformed_json_triggers_one_retry_then_exclusion():
    # After generate_cluster_prompts_safely gives up, _finalize_entry must
    # exclude the prompt (never crash) with a specific reason.
    client = FakeAnthropicClient([FakeResponse("not json")])  # the one regeneration attempt
    content = {"full_content": "cluster a content"}
    target = {"hop_distance": 0, "target_text": "hub text"}

    entry = _finalize_entry(
        client, "claude-sonnet-5", "cluster_a", content, target, {},
        "v2", "claude-sonnet-5@2026-07-04", max_retries=1, prompt_id="cluster_a_0000",
        print_fn=lambda *_a, **_k: None,
    )

    assert entry.validated is False
    assert "missing prompt_text" in entry.exclusion_reason


# ---------------------------------------------------------------------------
# Cost estimate (pure function)
# ---------------------------------------------------------------------------

def test_estimate_generation_cost_scales_with_prompt_count():
    content = {c: "x" * 1000 for c in ("cluster_a", "cluster_b", "cluster_c", "cluster_d")}
    small = estimate_generation_cost(content, {"cluster_a": 1, "cluster_b": 0, "cluster_c": 0, "cluster_d": 0}, 2.0, 10.0)
    large = estimate_generation_cost(content, {"cluster_a": 10, "cluster_b": 0, "cluster_c": 0, "cluster_d": 0}, 2.0, 10.0)
    assert small["total_prompts"] == 1
    assert large["total_prompts"] == 10
    assert small["estimated_cost_usd"] < large["estimated_cost_usd"]


def test_load_pricing_reads_config_yaml():
    price_in, price_out = load_pricing()
    assert price_in == 2.0
    assert price_out == 10.0


# ---------------------------------------------------------------------------
# Real fixture reading (no API calls)
# ---------------------------------------------------------------------------

def test_read_cluster_content_loads_all_four_clusters():
    content = read_cluster_content(FIXTURE_PATH)
    assert set(content.keys()) == {"cluster_a", "cluster_b", "cluster_c", "cluster_d"}
    for cluster_id, c in content.items():
        assert c["hub_text"], f"{cluster_id} hub_text should not be empty"
        assert c["spoke_text"], f"{cluster_id} spoke_text should not be empty"
        assert c["full_content"]


def test_read_cluster_content_loads_v3_fixture():
    content = read_cluster_content(FIXTURE_PATH_V3, version="v3")
    assert set(content.keys()) == {"cluster_a", "cluster_b", "cluster_c", "cluster_d"}
    for cluster_id, c in content.items():
        assert c["hub_text"], f"{cluster_id} hub_text should not be empty"
        assert c["spoke_text"], f"{cluster_id} spoke_text should not be empty"
        assert c["full_content"]
    # cluster_a's hub asserts the Kestrel family name and every K-rule ID —
    # a real content spot check, not just "non-empty".
    assert "Kestrel" in content["cluster_a"]["hub_text"]


def test_read_cluster_content_uses_requested_non_spoke1_trained_spoke():
    default_content = read_cluster_content(FIXTURE_PATH)
    override_content = read_cluster_content(
        FIXTURE_PATH,
        trained_spokes={
            "cluster_a": "a_spoke2",
            "cluster_b": "b_spoke1",
            "cluster_c": "c_spoke1",
            "cluster_d": "d_spoke1",
        },
    )
    assert override_content["cluster_a"]["spoke_text"] != default_content["cluster_a"]["spoke_text"]
    # untouched clusters keep spoke1's content
    assert override_content["cluster_b"]["spoke_text"] == default_content["cluster_b"]["spoke_text"]


def test_read_cluster_content_raises_on_unresolvable_spoke_key():
    with pytest.raises(ValueError) as exc_info:
        read_cluster_content(
            FIXTURE_PATH,
            trained_spokes={
                "cluster_a": "a_spoke1",
                "cluster_b": "b_spoke9",  # does not exist
                "cluster_c": "c_spoke1",
                "cluster_d": "d_spoke1",
            },
        )
    message = str(exc_info.value)
    assert "v2" in message
    assert "cluster_b" in message
    assert "b_spoke9" in message


def test_compute_fixture_manifest_hash_is_deterministic():
    assert compute_fixture_manifest_hash(FIXTURE_PATH) == compute_fixture_manifest_hash(FIXTURE_PATH)


def test_compute_fixture_manifest_hash_differs_between_v2_and_v3():
    assert compute_fixture_manifest_hash(FIXTURE_PATH) != compute_fixture_manifest_hash(FIXTURE_PATH_V3)


# ---------------------------------------------------------------------------
# M28 — version dispatch reaches the actual Sonnet generation prompt
# ---------------------------------------------------------------------------

def test_generate_cluster_prompts_safely_uses_v3_worked_example():
    client = FakeAnthropicClient([
        FakeResponse('[{"prompt_text": "q", "criteria": ["names rule K-1"], "hop_distance": 0}]'),
    ])
    content = {"full_content": "cluster a content", "hub_text": "hub text", "spoke_text": "spoke text"}
    targets = [{"hop_distance": 0, "target_text": "hub text"}]

    generate_cluster_prompts_safely(
        client, "claude-sonnet-5", "cluster_a", content, targets, None,
        print_fn=lambda *_a, **_k: None, version="v3",
    )

    sent_prompt = client.messages.calls[0]["messages"][0]["content"]
    assert "names rule M-2" in sent_prompt  # the v3 worked example, not v2's


# ---------------------------------------------------------------------------
# build_bank orchestration — reuse and cost-decline paths make zero API calls
# ---------------------------------------------------------------------------

def test_build_bank_reuses_existing_hash_with_no_api_calls(tmp_path):
    plan = _plan(total_prompts=10)
    fixture_hash = compute_fixture_manifest_hash(FIXTURE_PATH)
    params = _generation_params(plan)
    content_hash = compute_bank_hash(fixture_hash, params)

    existing_entry = PromptBankEntry(
        prompt_id="cluster_a_0000", cluster_id="cluster_a", prompt_text="existing prompt",
        hop_distance=0, criteria=["c1"], seed_fixture_version="v2",
        generated_by="claude-sonnet-5@2026-01-01", validated=True,
    )
    existing_bank = PromptBank(entries=[existing_entry], generation_params=params, content_hash=content_hash)
    save_bank(existing_bank, tmp_path)

    client = FakeAnthropicClient([])  # any call would raise AssertionError
    bank = build_bank(plan, FIXTURE_PATH, tmp_path, client, input_fn=lambda *_a: "y", print_fn=lambda *_a, **_k: None)

    assert bank.content_hash == content_hash
    assert bank.entries[0].prompt_text == "existing prompt"
    assert client.messages.calls == []


def test_build_bank_aborts_with_no_files_when_cost_declined(tmp_path):
    plan = _plan(total_prompts=10)
    client = FakeAnthropicClient([])  # any call would raise AssertionError

    with pytest.raises(GenerationAborted):
        build_bank(plan, FIXTURE_PATH, tmp_path, client, input_fn=lambda *_a: "n", print_fn=lambda *_a, **_k: None)

    assert list(tmp_path.iterdir()) == []
    assert client.messages.calls == []


# ---------------------------------------------------------------------------
# M19 Phase 3 — estimate_bank_cost_for_plan: pure pre-run estimate, no API
# calls, no file writes, used by the budget_cap preflight in cost.py.
# ---------------------------------------------------------------------------

def test_estimate_bank_cost_for_plan_returns_none_when_bank_already_exists(tmp_path):
    plan = _plan(total_prompts=10)
    fixture_hash = compute_fixture_manifest_hash(FIXTURE_PATH)
    params = _generation_params(plan)
    content_hash = compute_bank_hash(fixture_hash, params)

    existing_entry = PromptBankEntry(
        prompt_id="cluster_a_0000", cluster_id="cluster_a", prompt_text="existing prompt",
        hop_distance=0, criteria=["c1"], seed_fixture_version="v2",
        generated_by="claude-sonnet-5@2026-01-01", validated=True,
    )
    existing_bank = PromptBank(entries=[existing_entry], generation_params=params, content_hash=content_hash)
    save_bank(existing_bank, tmp_path)

    estimate = estimate_bank_cost_for_plan(plan, FIXTURE_PATH, tmp_path)

    assert estimate is None


def test_estimate_bank_cost_for_plan_returns_cost_estimate_when_no_bank_exists(tmp_path):
    plan = _plan(total_prompts=10)

    estimate = estimate_bank_cost_for_plan(plan, FIXTURE_PATH, tmp_path)

    assert estimate is not None
    assert estimate["estimated_cost_usd"] > 0
    assert estimate["total_prompts"] == 8  # 10 total * (1 - probe_ratio=0.2)


# ---------------------------------------------------------------------------
# M29 — per-cluster trained-spoke selection threaded through generation
# ---------------------------------------------------------------------------

_NON_DEFAULT_TRAINED_SPOKES = {
    "cluster_a": "a_spoke2",
    "cluster_b": "b_spoke3",
    "cluster_c": "c_spoke1",
    "cluster_d": "d_spoke1",
}


def test_generation_params_for_plan_includes_trained_spokes():
    plan = _plan(total_prompts=10)
    params = _generation_params(plan)
    assert params["trained_spokes"] == plan.trained_spokes


def test_plans_differing_only_in_trained_spokes_produce_different_content_hash():
    plan_default = _plan(total_prompts=10)
    plan_override = _plan(total_prompts=10, trained_spokes=_NON_DEFAULT_TRAINED_SPOKES)

    fixture_hash = compute_fixture_manifest_hash(FIXTURE_PATH)
    hash_default = compute_bank_hash(fixture_hash, _generation_params(plan_default))
    hash_override = compute_bank_hash(fixture_hash, _generation_params(plan_override))

    assert hash_default != hash_override


def test_estimate_bank_cost_for_plan_threads_trained_spokes(tmp_path, monkeypatch):
    plan = _plan(total_prompts=10, trained_spokes=_NON_DEFAULT_TRAINED_SPOKES)
    calls = []
    real_read_cluster_content = prompt_gen_module.read_cluster_content

    def spy(fixture_path, version="v2", trained_spokes=None):
        calls.append(trained_spokes)
        return real_read_cluster_content(fixture_path, version, trained_spokes)

    monkeypatch.setattr(prompt_gen_module, "read_cluster_content", spy)

    estimate_bank_cost_for_plan(plan, FIXTURE_PATH, tmp_path)

    assert calls == [_NON_DEFAULT_TRAINED_SPOKES]


def test_build_bank_threads_trained_spokes(tmp_path, monkeypatch):
    plan = _plan(total_prompts=10, trained_spokes=_NON_DEFAULT_TRAINED_SPOKES)
    calls = []
    real_read_cluster_content = prompt_gen_module.read_cluster_content

    def spy(fixture_path, version="v2", trained_spokes=None):
        calls.append(trained_spokes)
        return real_read_cluster_content(fixture_path, version, trained_spokes)

    monkeypatch.setattr(prompt_gen_module, "read_cluster_content", spy)
    client = FakeAnthropicClient([])  # any call would raise AssertionError

    with pytest.raises(GenerationAborted):
        build_bank(plan, FIXTURE_PATH, tmp_path, client, input_fn=lambda *_a: "n", print_fn=lambda *_a, **_k: None)

    assert calls == [_NON_DEFAULT_TRAINED_SPOKES]
    assert list(tmp_path.iterdir()) == []  # no files written, no API calls possible
