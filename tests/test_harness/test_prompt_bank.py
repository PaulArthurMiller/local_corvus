"""Tests for scripts/harness/prompt_bank.py — bank schema, hashing, load/save."""

import os
import sys

import pytest
from pydantic import ValidationError

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "scripts"))

from harness.prompt_bank import (
    PromptBank,
    PromptBankEntry,
    bank_path_for_hash,
    compute_bank_hash,
    load_bank,
    save_bank,
)


def _entry(**overrides):
    kwargs = dict(
        prompt_id="cluster_a_0000",
        cluster_id="cluster_a",
        prompt_text="What is the rule about header rows?",
        hop_distance=0,
        criteria=["mentions header consistency"],
        seed_fixture_version="v2",
        generated_by="claude-sonnet-5@2026-07-04",
        validated=True,
    )
    kwargs.update(overrides)
    return PromptBankEntry(**kwargs)


def test_entry_with_zero_criteria_is_rejected():
    with pytest.raises(ValidationError) as exc_info:
        _entry(criteria=[])
    assert "criteria" in str(exc_info.value)


def test_entry_with_four_criteria_is_rejected():
    with pytest.raises(ValidationError) as exc_info:
        _entry(criteria=["a", "b", "c", "d"])
    assert "criteria" in str(exc_info.value)


def test_entry_with_one_to_three_criteria_is_accepted():
    for n in (1, 2, 3):
        entry = _entry(criteria=[f"criterion {i}" for i in range(n)])
        assert len(entry.criteria) == n


def test_bank_hash_is_deterministic():
    params = {"total_prompts": 20, "probe_ratio": 0.2}
    h1 = compute_bank_hash("fixture-hash-abc", params)
    h2 = compute_bank_hash("fixture-hash-abc", params)
    assert h1 == h2


def test_bank_hash_changes_when_params_change():
    h1 = compute_bank_hash("fixture-hash-abc", {"total_prompts": 20})
    h2 = compute_bank_hash("fixture-hash-abc", {"total_prompts": 30})
    assert h1 != h2


def test_bank_hash_changes_when_fixture_hash_changes():
    h1 = compute_bank_hash("fixture-hash-abc", {"total_prompts": 20})
    h2 = compute_bank_hash("fixture-hash-xyz", {"total_prompts": 20})
    assert h1 != h2


def test_save_then_load_round_trips(tmp_path):
    entries = [_entry(), _entry(prompt_id="cluster_a_0001", validated=False, exclusion_reason="test")]
    content_hash = compute_bank_hash("fixture-hash", {"total_prompts": 2})
    bank = PromptBank(entries=entries, generation_params={"total_prompts": 2}, content_hash=content_hash)

    path = save_bank(bank, tmp_path)
    assert path == bank_path_for_hash(content_hash, tmp_path)
    assert path.exists()

    loaded = load_bank(path)
    assert loaded.content_hash == content_hash
    assert len(loaded.entries) == 2
    assert loaded.entries[0].prompt_text == entries[0].prompt_text


def test_excluded_entries_are_retained_with_reason(tmp_path):
    entries = [
        _entry(),
        _entry(prompt_id="cluster_a_0001", validated=False, exclusion_reason="fingerprint leak: ['rule']"),
    ]
    content_hash = compute_bank_hash("fixture-hash", {})
    bank = PromptBank(entries=entries, generation_params={}, content_hash=content_hash)
    path = save_bank(bank, tmp_path)

    loaded = load_bank(path)
    excluded = [e for e in loaded.entries if not e.validated]
    assert len(excluded) == 1
    assert excluded[0].exclusion_reason == "fingerprint leak: ['rule']"

    # The runner only ever draws validated=True entries.
    assert loaded.validated_entries() == [loaded.entries[0]]


def test_load_bank_missing_file_raises_file_not_found_error(tmp_path):
    with pytest.raises(FileNotFoundError):
        load_bank(tmp_path / "does_not_exist.json")


def test_load_bank_invalid_json_raises_value_error(tmp_path):
    path = tmp_path / "bad.json"
    path.write_text("{not json", encoding="utf-8")
    with pytest.raises(ValueError):
        load_bank(path)


def test_load_bank_schema_violation_raises_value_error_with_field_name(tmp_path):
    path = tmp_path / "bad_schema.json"
    path.write_text('{"entries": [], "generation_params": {}, "content_hash": ""}', encoding="utf-8")
    with pytest.raises(ValueError) as exc_info:
        load_bank(path)
    assert "content_hash" in str(exc_info.value)
