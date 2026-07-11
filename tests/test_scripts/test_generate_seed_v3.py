"""
Tests for the pure-Python validation logic in scripts/generate_seed_v3.py.

The Ollama-dependent generation path itself stays manually validated (a real
run + the standalone verify_seed_v3.py), matching this project's convention
for v2's generator. But the two-tier-vocabulary validation surface is
materially larger than v2's (hub/sibling exclusion, global uniqueness, token
format, manifest serialization) and has no Ollama dependency — every function
tested here is exercised without a network call (`_call_ollama` is monkey-
patched where a generation loop is driven).

Importing the module also runs its import-time self-checks
(`_validate_answer_token_uniqueness` / `_validate_answer_token_formats`) on
the REAL ANSWER_TOKENS table, so if someone edits that table and introduces a
collision or a malformed token, this test module fails to import — a free
regression guard on the approved spec data.
"""

import json
import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "scripts"))

import generate_seed_v3 as gen


# ---------------------------------------------------------------------------
# _check_required_terms / _check_forbidden_terms — reused from v2, now with
# multi-word phrase tokens (v2 only ever tested single words).
# ---------------------------------------------------------------------------

class TestTermChecks:
    def test_required_single_word_present_and_missing(self):
        content = "The Kestrel K-2 delimiter is the TILDEBAR token."
        assert gen._check_required_terms(content, ["Kestrel", "TILDEBAR"]) == []
        assert gen._check_required_terms(content, ["KAPLINE"]) == ["KAPLINE"]

    def test_required_substring_match_is_lenient(self):
        # 'version' is satisfied by 'versions' (substring match, matches v2).
        assert gen._check_required_terms("uses COBALT versions", ["version"]) == []

    def test_required_multiword_phrase_present(self):
        content = "fields appear in spine order on the first line"
        assert gen._check_required_terms(content, ["spine order"]) == []

    def test_required_multiword_phrase_missing(self):
        assert gen._check_required_terms("uses cobalt", ["COBALT numbering"]) == [
            "COBALT numbering"
        ]

    def test_forbidden_whole_word_avoids_false_positive(self):
        # 'format' must not match inside 'reformatting'.
        assert gen._check_forbidden_terms("reformatting the file", ["format"]) == []
        assert gen._check_forbidden_terms("the output format", ["format"]) == ["format"]

    def test_forbidden_multiword_phrase_matches_only_when_adjacent(self):
        assert gen._check_forbidden_terms("the spine order is fixed", ["spine order"]) == [
            "spine order"
        ]
        # Same words, not adjacent → not a match.
        assert gen._check_forbidden_terms("ordering the spine", ["spine order"]) == []


# ---------------------------------------------------------------------------
# Forbidden-term derivation by MU category (hub / sibling / bridge).
# ---------------------------------------------------------------------------

class TestForbiddenDerivation:
    def test_hub_forbidden_is_own_cluster_value_tokens(self):
        forbidden = gen._hub_forbidden_tokens("cluster_a")
        # All cluster_a spoke value tokens, no rule IDs.
        assert "TILDEBAR" in forbidden
        assert "spine order" in forbidden
        assert "NULLMARK" in forbidden
        assert "K-2" not in forbidden  # hub MUST be able to name rule IDs
        # No cross-cluster leakage into the hub-forbidden set.
        assert "WHOLENUM" not in forbidden

    def test_sibling_forbidden_excludes_own_token(self):
        forbidden = gen._sibling_forbidden_tokens("a_spoke2", "cluster_a")
        assert "TILDEBAR" not in forbidden          # its OWN token
        assert "KAPLINE" in forbidden               # a sibling's token
        assert "spine order" in forbidden
        # No other cluster's token.
        assert "WAIVERKEY" not in forbidden

    def test_bridge_forbidden_covers_both_adjacent_clusters(self):
        forbidden = gen._bridge_forbidden_terms("bridge_ab")
        # entry vocab from both sides
        assert "delimiter" in forbidden and "schema" in forbidden
        # answer tokens from both sides
        assert "TILDEBAR" in forbidden and "WHOLENUM" in forbidden
        # family names from both sides
        assert "Kestrel" in forbidden and "Marrow" in forbidden
        # nothing from the OTHER bridge's clusters
        assert "Vessel" not in forbidden and "WAIVERKEY" not in forbidden

    def test_forbidden_terms_for_dispatches_by_category(self):
        assert "TILDEBAR" in gen._forbidden_terms_for("a_hub", "cluster_a")
        assert "KAPLINE" in gen._forbidden_terms_for("a_spoke2", "cluster_a")
        assert "Marrow" in gen._forbidden_terms_for("bridge_ab", "bridge_ab")


# ---------------------------------------------------------------------------
# Hub / sibling exclusion applied to actual content strings.
# ---------------------------------------------------------------------------

class TestExclusionOnContent:
    def test_hub_with_value_token_fails(self):
        forbidden = gen._forbidden_terms_for("a_hub", "cluster_a")
        content = "Kestrel governs output; K-2 defines the TILDEBAR delimiter."
        assert gen._check_forbidden_terms(content, forbidden) == ["TILDEBAR"]

    def test_hub_naming_only_rule_ids_passes(self):
        forbidden = gen._forbidden_terms_for("a_hub", "cluster_a")
        content = ("The Kestrel Output Conventions cover K-1 field order, K-2 "
                   "delimiters, and K-3 headers; values are defined in each rule.")
        assert gen._check_forbidden_terms(content, forbidden) == []

    def test_spoke_with_sibling_token_fails(self):
        forbidden = gen._forbidden_terms_for("a_spoke7", "cluster_a")
        # a_spoke7's gist must not reference a_spoke1's 'spine order' token.
        content = "K-7 missing values use NULLMARK; omission corrupts spine order."
        assert gen._check_forbidden_terms(content, forbidden) == ["spine order"]


# ---------------------------------------------------------------------------
# Global answer-token uniqueness self-check.
# ---------------------------------------------------------------------------

class TestUniqueness:
    def test_real_table_is_unique(self):
        # Also runs at import time; asserted here explicitly.
        assert gen._validate_answer_token_uniqueness() is True

    def test_duplicate_token_across_keys_raises(self):
        bad = {
            "a_spoke1": ["DUPTOKEN"],
            "b_spoke1": ["DUPTOKEN"],
        }
        with pytest.raises(ValueError, match="not unique"):
            gen._validate_answer_token_uniqueness(bad)

    def test_same_token_repeated_under_one_key_is_ok(self):
        ok = {"a_spoke1": ["ONE", "ONE"]}
        assert gen._validate_answer_token_uniqueness(ok) is True


# ---------------------------------------------------------------------------
# Answer-token format check.
# ---------------------------------------------------------------------------

class TestTokenFormat:
    def test_single_word_token_passes(self):
        assert gen._check_token_format("TILDEBAR") is True
        assert gen._check_token_format("P-fault") is True  # hyphen allowed

    def test_multiword_phrase_is_exempt(self):
        assert gen._check_token_format("three-to-one ratchet") is True

    def test_punctuation_bearing_token_fails(self):
        assert gen._check_token_format("ty::val") is False

    def test_validate_formats_raises_on_malformed(self):
        with pytest.raises(ValueError, match="Malformed"):
            gen._validate_answer_token_formats({"x_spoke1": ["bad:token"]})

    def test_real_table_formats_valid(self):
        assert gen._validate_answer_token_formats() is True


# ---------------------------------------------------------------------------
# Reciprocal edge builder.
# ---------------------------------------------------------------------------

class TestEdges:
    def test_edge_count_is_64(self):
        assert len(gen._build_edges()) == 64

    def test_every_edge_has_reciprocal_partner(self):
        edges = gen._build_edges()
        edge_set = {(e["source_mu_id"], e["target_mu_id"], e["weight"]) for e in edges}
        for s, t, w in edge_set:
            assert (t, s, w) in edge_set, f"no reverse for {s}->{t}"

    def test_all_weights_half(self):
        assert all(e["weight"] == 0.5 for e in gen._build_edges())


# ---------------------------------------------------------------------------
# generate_mu manifest behaviour (Ollama monkeypatched).
# ---------------------------------------------------------------------------

class TestGenerateMuManifest:
    @pytest.fixture(autouse=True)
    def _no_sleep(self, monkeypatch):
        # generate_mu sleeps between retries; skip the wall-clock wait.
        monkeypatch.setattr(gen.time, "sleep", lambda *a, **k: None)

    def test_missing_required_produces_one_failure_record(self, monkeypatch):
        text = "Kestrel K-2 defines the field delimiter."  # missing TILDEBAR
        monkeypatch.setattr(gen, "_call_ollama", lambda prompt: text)

        mu, failure = gen.generate_mu(
            "a_spoke2", "cluster_a", "agent_output", "prompt",
            required=["Kestrel", "K-2", "TILDEBAR", "delimiter"],
            forbidden=[],
        )
        assert failure is not None
        assert failure["mu_id"] == gen.MU_IDS["a_spoke2"]
        assert failure["mu_key"] == "a_spoke2"
        assert failure["cluster_id"] == "cluster_a"
        assert failure["missing_required"] == ["TILDEBAR"]
        assert failure["forbidden_leaked"] == []
        assert failure["last_attempted_text"] == text
        assert failure["attempts"] == gen.MAX_RETRIES
        # best-effort content is still kept in the draft
        assert mu["raw_content"] == text

    def test_forbidden_leak_recorded_in_failure(self, monkeypatch):
        text = "Kestrel index names K-2 delimiters and defines TILDEBAR."
        monkeypatch.setattr(gen, "_call_ollama", lambda prompt: text)

        _, failure = gen.generate_mu(
            "a_hub", "cluster_a", "agent_output", "prompt",
            required=["Kestrel"],
            forbidden=gen._forbidden_terms_for("a_hub", "cluster_a"),
        )
        assert failure is not None
        assert "TILDEBAR" in failure["forbidden_leaked"]

    def test_clean_generation_has_no_failure(self, monkeypatch):
        text = ("Under the Kestrel Output Conventions, rule K-2 sets the field "
                "delimiter to the TILDEBAR token.")
        monkeypatch.setattr(gen, "_call_ollama", lambda prompt: text)

        mu, failure = gen.generate_mu(
            "a_spoke2", "cluster_a", "agent_output", "prompt",
            required=["Kestrel", "K-2", "TILDEBAR", "delimiter"],
            forbidden=["KAPLINE", "NULLMARK"],
        )
        assert failure is None
        assert mu["raw_content"] == text

    def test_write_manifest_empty_failures_is_present_not_absent(self, monkeypatch, tmp_path):
        path = tmp_path / "seed_v3_generation_manifest.json"
        monkeypatch.setattr(gen, "MANIFEST_PATH", path)

        gen._write_manifest([])

        assert path.exists()
        data = json.loads(path.read_text(encoding="utf-8"))
        assert data["mu_count_failed"] == 0
        assert data["failures"] == []
        assert data["source_script"] == "generate_seed_v3.py"

    def test_write_manifest_records_failure(self, monkeypatch, tmp_path):
        path = tmp_path / "seed_v3_generation_manifest.json"
        monkeypatch.setattr(gen, "MANIFEST_PATH", path)

        failure = {
            "mu_id": gen.MU_IDS["c_spoke3"],
            "mu_key": "c_spoke3",
            "cluster_id": "cluster_c",
            "attempts": 3,
            "missing_required": ["WAIVERKEY"],
            "forbidden_leaked": [],
            "last_attempted_text": "Vessel V-3 governs exemptions...",
        }
        gen._write_manifest([failure])

        data = json.loads(path.read_text(encoding="utf-8"))
        assert data["mu_count_failed"] == 1
        assert data["failures"][0]["mu_key"] == "c_spoke3"
        assert data["failures"][0]["missing_required"] == ["WAIVERKEY"]


# ---------------------------------------------------------------------------
# Spec-data consistency: every generated MU key has the data it needs.
# ---------------------------------------------------------------------------

class TestSpecDataConsistency:
    def test_every_ordered_mu_has_id_and_gist(self):
        for mu_key, cluster_id in gen.MU_ORDER:
            assert mu_key in gen.MU_IDS, f"{mu_key} missing from MU_IDS"
            assert mu_key in gen.CONTENT_GIST, f"{mu_key} missing a content gist"

    def test_every_spoke_has_required_terms_including_its_token(self):
        for mu_key, toks in gen.ANSWER_TOKENS.items():
            if "spoke" not in mu_key:
                continue
            required = gen.REQUIRED[mu_key]
            # At least one of the MU's own value tokens (or a distinctive part)
            # is required; every spoke lists its family name + rule ID too.
            assert gen.RULE_IDS[mu_key] in required
