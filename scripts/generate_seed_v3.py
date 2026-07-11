"""
Generate seed_v3 MU content via Gemma 4 (local Ollama).
Output: seed_fixtures/seed_v3_draft.json — review before freezing.

Usage: python scripts/generate_seed_v3.py
Requires: Ollama running at localhost:11434 with gemma4:e4b loaded

seed_v3 replaces seed_v2's true-in-general content with INVENTED,
unguessable specifics — a proper-noun rule family per cluster (Kestrel,
Marrow, Vessel, Ledger) with numbered rules, each paired with an invented
value token (TILDEBAR, three-to-one ratchet, WAIVERKEY, ...). Gemma has no
parametric knowledge of any of these, so a response can only contain the
right rule ID + value token if retrieval actually surfaced the gold MU.
This makes judge verdicts — and therefore M20 edge updates — causally
downstream of retrieval quality. Full rationale: seed_v3_mu_spec.md.

Two-tier vocabulary (spec §1):
  - Entry vocabulary: natural thematic words, saturated in the hub, one
    anchor word per spoke. Free in prompts (BM25 entry).
  - Answer tokens: invented value tokens, confined to ONE spoke each,
    forbidden in the hub and in every sibling spoke. This is what lets
    BM25 alone reach the hub but never the gold spoke — only graph
    traversal can.

Produces 34 MUs across 4 clusters and 2 bridges, with 64 RECIPROCAL edges
(v2 had 32 one-directional — see seed_v3_mu_spec.md §2), written to
seed_fixtures/seed_v3_draft.json as uncompressed JSON. Any MU that exhausts
MAX_RETRIES with an unresolved validation violation is recorded in
seed_fixtures/seed_v3_generation_manifest.json for hand-repair (spec §5).

That draft file is NOT seed_v3.json.gz — freeze manually after Paul's
review and after verify_seed_v3.py --draft passes all 9 checks:

  python -c "
  import gzip, json
  with open('seed_fixtures/seed_v3_draft.json') as f:
      data = json.load(f)
  data.pop('notes', None)
  with gzip.open('seed_fixtures/seed_v3.json.gz', 'wt', encoding='utf-8') as f:
      json.dump(data, f)
  print('Frozen seed_fixtures/seed_v3.json.gz')
  "

Then run: python scripts/verify_seed_v3.py --frozen
"""

import json
import logging
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import List, Optional, Tuple

import httpx

logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
logger = logging.getLogger(__name__)

OLLAMA_URL = "http://localhost:11434/v1/chat/completions"
MODEL = "gemma4:e4b"
TIMEOUT = 120
MAX_RETRIES = 3
RETRY_DELAY = 5

SCRIPT_DIR = Path(__file__).parent
PROJECT_ROOT = SCRIPT_DIR.parent
OUTPUT_PATH = PROJECT_ROOT / "seed_fixtures" / "seed_v3_draft.json"
MANIFEST_PATH = PROJECT_ROOT / "seed_fixtures" / "seed_v3_generation_manifest.json"

# ---------------------------------------------------------------------------
# Fixed MU IDs — copied verbatim from generate_seed_v2.py (same UUIDs).
# Preserves continuity for the future probe_set_v3 gold-MU references
# (spec §6: "golds are spoke MUs, IDs unchanged from v2 where convenient").
# ---------------------------------------------------------------------------

MU_IDS = {
    # cluster_a: structured file output / format discipline
    "a_hub":    "a0000001-aaaa-4aaa-8aaa-000000000001",
    "a_spoke1": "a0000002-aaaa-4aaa-8aaa-000000000002",
    "a_spoke2": "a0000003-aaaa-4aaa-8aaa-000000000003",
    "a_spoke3": "a0000004-aaaa-4aaa-8aaa-000000000004",
    "a_spoke4": "a0000005-aaaa-4aaa-8aaa-000000000005",
    "a_spoke5": "a0000006-aaaa-4aaa-8aaa-000000000006",
    "a_spoke6": "a0000007-aaaa-4aaa-8aaa-000000000007",
    "a_spoke7": "a0000008-aaaa-4aaa-8aaa-000000000008",
    # cluster_b: schema and data-shape reasoning
    "b_hub":    "b0000001-bbbb-4bbb-8bbb-000000000001",
    "b_spoke1": "b0000002-bbbb-4bbb-8bbb-000000000002",
    "b_spoke2": "b0000003-bbbb-4bbb-8bbb-000000000003",
    "b_spoke3": "b0000004-bbbb-4bbb-8bbb-000000000004",
    "b_spoke4": "b0000005-bbbb-4bbb-8bbb-000000000005",
    "b_spoke5": "b0000006-bbbb-4bbb-8bbb-000000000006",
    "b_spoke6": "b0000007-bbbb-4bbb-8bbb-000000000007",
    "b_spoke7": "b0000008-bbbb-4bbb-8bbb-000000000008",
    # cluster_c: rule-following under constraint
    "c_hub":    "c0000001-cccc-4ccc-8ccc-000000000001",
    "c_spoke1": "c0000002-cccc-4ccc-8ccc-000000000002",
    "c_spoke2": "c0000003-cccc-4ccc-8ccc-000000000003",  # gold MU for probe p14
    "c_spoke3": "c0000004-cccc-4ccc-8ccc-000000000004",
    "c_spoke4": "c0000005-cccc-4ccc-8ccc-000000000005",  # gold MU for probe p15
    "c_spoke5": "c0000006-cccc-4ccc-8ccc-000000000006",
    "c_spoke6": "c0000007-cccc-4ccc-8ccc-000000000007",
    "c_spoke7": "c0000008-cccc-4ccc-8ccc-000000000008",
    # cluster_d: self-sourcing / provenance discipline
    "d_hub":    "d0000001-dddd-4ddd-8ddd-000000000001",
    "d_spoke1": "d0000002-dddd-4ddd-8ddd-000000000002",
    "d_spoke2": "d0000003-dddd-4ddd-8ddd-000000000003",
    "d_spoke3": "d0000004-dddd-4ddd-8ddd-000000000004",
    "d_spoke4": "d0000005-dddd-4ddd-8ddd-000000000005",
    "d_spoke5": "d0000006-dddd-4ddd-8ddd-000000000006",
    "d_spoke6": "d0000007-dddd-4ddd-8ddd-000000000007",
    "d_spoke7": "d0000008-dddd-4ddd-8ddd-000000000008",
    # bridges
    "bridge_ab": "ab000001-abab-4aba-8aba-000000000001",
    "bridge_cd": "cd000001-cdcd-4cdc-8cdc-000000000001",
}

# ---------------------------------------------------------------------------
# Two-tier vocabulary constants — all values transcribed VERBATIM from
# seed_v3_mu_spec.md §3-4. These drive the *validation* logic (hub/sibling
# exclusion, uniqueness, token format) and the cross-cluster fingerprint
# scanning; the required-terms lists below are a separate verbatim copy so a
# derivation bug can never silently diverge from the approved spec.
# ---------------------------------------------------------------------------

# Short family names — strong BM25 anchors, present in every MU of a cluster.
# The full form ("Kestrel Output Conventions") is asserted in each hub's
# content gist; FAMILY_NAMES holds the short form used for fingerprint scans.
FAMILY_NAMES = {
    "cluster_a": "Kestrel", "cluster_b": "Marrow",
    "cluster_c": "Vessel",  "cluster_d": "Ledger",
}

# Entry vocabulary per cluster (spec §3). Saturated in the hub; free in
# prompts. Used by verify_seed_v3.py's bridge/probe checks and (future) by
# prompt_gen.py leakage scanning.
ENTRY_VOCAB = {
    "cluster_a": ["format", "structure", "output", "field", "delimiter",
                  "header", "quoting", "encoding", "padding"],
    "cluster_b": ["schema", "data", "shape", "type", "validation",
                  "column", "nesting", "version", "range"],
    "cluster_c": ["rule", "constraint", "violation", "enforce", "comply",
                  "severity", "exemption", "escalation", "deadline"],
    "cluster_d": ["source", "provenance", "cite", "origin", "traceable",
                  "attribution", "timestamp", "custody", "retraction"],
}

# Answer tokens: mu_key -> list of this MU's own invented value token(s).
# Hub keys map to [] (index only — spec §4 placement rule 1). Multi-word
# phrase tokens are natural-language value phrases; single-word tokens are
# the ALL-CAPS (and P-fault) forms subject to the format check.
ANSWER_TOKENS = {
    "a_hub": [],
    "a_spoke1": ["spine order"], "a_spoke2": ["TILDEBAR"],
    "a_spoke3": ["KAPLINE"],     "a_spoke4": ["P-fault"],
    "a_spoke5": ["grove quotes"], "a_spoke6": ["ENCK9"],
    "a_spoke7": ["NULLMARK"],
    "b_hub": [],
    "b_spoke1": ["WHOLENUM", "SPANTEXT", "FLAGBIT"],
    "b_spoke2": ["girth line"],  "b_spoke3": ["trident limit"],
    "b_spoke4": ["COBALT numbering"], "b_spoke5": ["lowmark case"],
    "b_spoke6": ["BOUNDSPAN"],   "b_spoke7": ["marrow order"],
    "c_hub": [],
    "c_spoke1": ["amber breach", "crimson breach"],
    "c_spoke2": ["tally stamp"], "c_spoke3": ["WAIVERKEY"],
    "c_spoke4": ["three-to-one ratchet"], "c_spoke5": ["nine clear days"],
    "c_spoke6": ["warden note"], "c_spoke7": ["echo penalty"],
    "d_hub": [],
    "d_spoke1": ["SRCLINE"],     "d_spoke2": ["origin docket"],
    "d_spoke3": ["relay chain"], "d_spoke4": ["GREYMARK"],
    "d_spoke5": ["ledger time"], "d_spoke6": ["quill code"],
    "d_spoke7": ["UNWRITE notice"],
}

# The one entry-vocabulary word each spoke may use (spec §4 "Anchor" column).
# Not enforced by the generator (entry vocab in a spoke is not a violation);
# kept here for documentation and future prompt_gen.py use.
ANCHOR_WORDS = {
    "a_spoke1": "field", "a_spoke2": "delimiter", "a_spoke3": "header",
    "a_spoke4": "padding", "a_spoke5": "quoting", "a_spoke6": "encoding",
    "a_spoke7": "field",
    "b_spoke1": "type", "b_spoke2": "column", "b_spoke3": "nesting",
    "b_spoke4": "version", "b_spoke5": "schema", "b_spoke6": "range",
    "b_spoke7": "validation",
    "c_spoke1": "severity", "c_spoke2": "violation", "c_spoke3": "exemption",
    "c_spoke4": "escalation", "c_spoke5": "deadline", "c_spoke6": "rule",
    "c_spoke7": "comply",
    "d_spoke1": "cite", "d_spoke2": "origin", "d_spoke3": "custody",
    "d_spoke4": "source", "d_spoke5": "timestamp", "d_spoke6": "attribution",
    "d_spoke7": "retraction",
}

RULE_IDS = {
    "a_spoke1": "K-1", "a_spoke2": "K-2", "a_spoke3": "K-3", "a_spoke4": "K-4",
    "a_spoke5": "K-5", "a_spoke6": "K-6", "a_spoke7": "K-7",
    "b_spoke1": "M-1", "b_spoke2": "M-2", "b_spoke3": "M-3", "b_spoke4": "M-4",
    "b_spoke5": "M-5", "b_spoke6": "M-6", "b_spoke7": "M-7",
    "c_spoke1": "V-1", "c_spoke2": "V-2", "c_spoke3": "V-3", "c_spoke4": "V-4",
    "c_spoke5": "V-5", "c_spoke6": "V-6", "c_spoke7": "V-7",
    "d_spoke1": "L-1", "d_spoke2": "L-2", "d_spoke3": "L-3", "d_spoke4": "L-4",
    "d_spoke5": "L-5", "d_spoke6": "L-6", "d_spoke7": "L-7",
}

# ---------------------------------------------------------------------------
# Required terms — copied VERBATIM from seed_v3_mu_spec.md §4 "Required terms"
# column, per MU. NOT derived from the dicts above, so a derivation bug can
# never silently diverge from the approved spec (BUILD-PLAN.md M25). Hub
# rule-ID ellipses ("K-1…K-7") are expanded here.
# ---------------------------------------------------------------------------

REQUIRED = {
    "a_hub": ["Kestrel", "K-1", "K-2", "K-3", "K-4", "K-5", "K-6", "K-7",
              "format", "structure", "output", "field", "delimiter",
              "header", "quoting", "encoding", "padding"],
    "a_spoke1": ["Kestrel", "K-1", "spine order", "field"],
    "a_spoke2": ["Kestrel", "K-2", "TILDEBAR", "delimiter"],
    "a_spoke3": ["Kestrel", "K-3", "KAPLINE", "header"],
    "a_spoke4": ["Kestrel", "K-4", "P-fault", "padding"],
    "a_spoke5": ["Kestrel", "K-5", "grove quotes", "quoting"],
    "a_spoke6": ["Kestrel", "K-6", "ENCK9", "encoding"],
    "a_spoke7": ["Kestrel", "K-7", "NULLMARK"],
    "b_hub": ["Marrow", "M-1", "M-2", "M-3", "M-4", "M-5", "M-6", "M-7",
              "schema", "data", "shape", "type", "validation",
              "column", "nesting", "version", "range"],
    "b_spoke1": ["Marrow", "M-1", "WHOLENUM", "SPANTEXT", "FLAGBIT", "type"],
    "b_spoke2": ["Marrow", "M-2", "girth line", "column"],
    "b_spoke3": ["Marrow", "M-3", "trident limit", "nesting"],
    "b_spoke4": ["Marrow", "M-4", "COBALT", "version"],
    "b_spoke5": ["Marrow", "M-5", "lowmark case"],
    "b_spoke6": ["Marrow", "M-6", "BOUNDSPAN", "range"],
    "b_spoke7": ["Marrow", "M-7", "marrow order", "validation"],
    "c_hub": ["Vessel", "V-1", "V-2", "V-3", "V-4", "V-5", "V-6", "V-7",
              "rule", "constraint", "violation", "enforce", "comply",
              "severity", "exemption", "escalation", "deadline"],
    "c_spoke1": ["Vessel", "V-1", "amber breach", "crimson breach", "severity"],
    "c_spoke2": ["Vessel", "V-2", "tally stamp", "violation"],
    "c_spoke3": ["Vessel", "V-3", "WAIVERKEY", "exemption"],
    "c_spoke4": ["Vessel", "V-4", "ratchet", "escalation"],
    "c_spoke5": ["Vessel", "V-5", "nine clear days", "deadline"],
    "c_spoke6": ["Vessel", "V-6", "warden note"],
    "c_spoke7": ["Vessel", "V-7", "echo penalty"],
    "d_hub": ["Ledger", "L-1", "L-2", "L-3", "L-4", "L-5", "L-6", "L-7",
              "source", "provenance", "cite", "origin", "traceable",
              "attribution", "timestamp", "custody", "retraction"],
    "d_spoke1": ["Ledger", "L-1", "SRCLINE"],
    "d_spoke2": ["Ledger", "L-2", "origin docket"],
    "d_spoke3": ["Ledger", "L-3", "relay chain", "custody"],
    "d_spoke4": ["Ledger", "L-4", "GREYMARK"],
    "d_spoke5": ["Ledger", "L-5", "ledger time", "timestamp"],
    "d_spoke6": ["Ledger", "L-6", "quill code", "attribution"],
    "d_spoke7": ["Ledger", "L-7", "UNWRITE notice", "retraction"],
}

# ---------------------------------------------------------------------------
# Content gists — what the 2-3 sentences must assert (spec §4 "Content gist"
# column; final prose is generated to taste). A few gists are paraphrased
# from the spec's literal prose to avoid naming a SIBLING spoke's answer
# token, which the generator's own sibling-exclusion validation forbids —
# e.g. the spec's a_spoke7 gist says "corrupts spine order" (a_spoke1's
# token) and c_spoke4's says "three amber breaches ... one crimson breach"
# (c_spoke1's tokens). No token, rule ID, or required-term is changed; only
# the surrounding prose. Documented in the M25 PR.
# ---------------------------------------------------------------------------

CONTENT_GIST = {
    "a_hub": (
        "The Kestrel Output Conventions govern structured file output through "
        "seven numbered rules: K-1 covers field order, K-2 the delimiter, K-3 "
        "the header, K-4 padding, K-5 quoting, K-6 encoding, and K-7 missing "
        "values. State that the specific values for each rule are defined in "
        "the individual rules, not in this index."
    ),
    "a_spoke1": (
        "Under the Kestrel Output Conventions, rule K-1 requires that fields "
        "appear in spine order — the fixed sequence declared on the file's "
        "first line — and reordering them is a K-1 violation."
    ),
    "a_spoke2": (
        "Under the Kestrel Output Conventions, rule K-2 sets the field "
        "delimiter to the TILDEBAR token, which is never escaped inline; "
        "values that contain it must instead be quoted."
    ),
    "a_spoke3": (
        "Under the Kestrel Output Conventions, rule K-3 requires the header "
        "row to begin with the KAPLINE marker and to mirror the data rows "
        "position-for-position."
    ),
    "a_spoke4": (
        "Under the Kestrel Output Conventions, rule K-4 forbids interior "
        "padding, and any padded value is logged as a P-fault."
    ),
    "a_spoke5": (
        "Under the Kestrel Output Conventions, rule K-5 requires values that "
        "contain the delimiter to be wrapped in grove quotes; quoting them "
        "inconsistently breaks parsing."
    ),
    "a_spoke6": (
        "Under the Kestrel Output Conventions, rule K-6 requires every file "
        "to declare the ENCK9 encoding tag on its second line, and mixed "
        "encodings are rejected outright."
    ),
    "a_spoke7": (
        "Under the Kestrel Output Conventions, rule K-7 requires missing "
        "values to use the NULLMARK placeholder rather than an empty field, "
        "because silently omitting a value corrupts the file's declared "
        "field ordering."
    ),
    "b_hub": (
        "The Marrow Schema Standard governs data shape through seven numbered "
        "rules: M-1 covers type declarations, M-2 column counts, M-3 nesting, "
        "M-4 versioning, M-5 naming, M-6 numeric ranges, and M-7 validation "
        "order. State that the specific values for each rule are defined in "
        "the individual rules, not in this index."
    ),
    "b_spoke1": (
        "Under the Marrow Schema Standard, rule M-1 requires every column to "
        "declare one of the three base types — WHOLENUM, SPANTEXT, or FLAGBIT."
    ),
    "b_spoke2": (
        "Under the Marrow Schema Standard, rule M-2 requires every table to "
        "declare its column count on a girth line; a mismatch fails the table "
        "before any other check."
    ),
    "b_spoke3": (
        "Under the Marrow Schema Standard, rule M-3 caps nesting at depth "
        "three — the trident limit — and deeper structures must be flattened."
    ),
    "b_spoke4": (
        "Under the Marrow Schema Standard, rule M-4 requires schema versions "
        "to use COBALT numbering, so consumers pin to a specific COBALT "
        "version rather than to 'latest'."
    ),
    "b_spoke5": (
        "Under the Marrow Schema Standard, rule M-5 requires all names in a "
        "schema to be written in lowmark case, and mixed casing is a naming "
        "violation."
    ),
    "b_spoke6": (
        "Under the Marrow Schema Standard, rule M-6 requires numeric columns "
        "to declare their range with the BOUNDSPAN keyword; values outside "
        "the range are rejected, not clamped."
    ),
    "b_spoke7": (
        "Under the Marrow Schema Standard, rule M-7 requires validation to "
        "run in marrow order — types first, then ranges, then names — because "
        "running the checks out of order masks failures."
    ),
    "c_hub": (
        "The Vessel Constraint Code governs rule-following through seven "
        "numbered rules: V-1 covers severity classes, V-2 violation logging, "
        "V-3 exemptions, V-4 escalation, V-5 correction deadlines, V-6 "
        "overrides, and V-7 repeat violations. State that the specific values "
        "for each rule are defined in the individual rules, not in this index. "
        "The system enforces these rules so that agents comply."
    ),
    "c_spoke1": (
        "Under the Vessel Constraint Code, rule V-1 classes every violation by "
        "severity as either an amber breach or a crimson breach, with no third "
        "class."
    ),
    "c_spoke2": (
        "Under the Vessel Constraint Code, rule V-2 gives every violation a "
        "tally stamp at the moment it is logged, and an unstamped violation "
        "cannot be enforced."
    ),
    "c_spoke3": (
        "Under the Vessel Constraint Code, rule V-3 requires an exemption to "
        "carry a WAIVERKEY issued before the act it covers; a retroactive "
        "WAIVERKEY is void."
    ),
    "c_spoke4": (
        "Under the Vessel Constraint Code, rule V-4 escalates three minor "
        "breaches into one major breach — the three-to-one ratchet — and the "
        "count resets after each escalation."
    ),
    "c_spoke5": (
        "Under the Vessel Constraint Code, rule V-5 sets a correction deadline "
        "of nine clear days, counted from the day after the violation is "
        "stamped."
    ),
    "c_spoke6": (
        "Under the Vessel Constraint Code, rule V-6 allows only a warden note "
        "to override a constraint, and the note must name the specific rule it "
        "overrides."
    ),
    "c_spoke7": (
        "Under the Vessel Constraint Code, rule V-7 imposes the echo penalty "
        "when the same violation repeats within one cycle: the sanction "
        "doubles, and continued failure to comply compounds it."
    ),
    "d_hub": (
        "The Ledger Provenance Protocol governs sourcing through seven "
        "numbered rules: L-1 covers citation markers, L-2 origin records, L-3 "
        "custody chains, L-4 unverified content, L-5 timestamps, L-6 "
        "attribution, and L-7 retractions. State that the specific values for "
        "each rule are defined in the individual rules, not in this index. "
        "Every claim must cite a traceable source to establish its provenance."
    ),
    "d_spoke1": (
        "Under the Ledger Provenance Protocol, rule L-1 requires every "
        "citation to carry a SRCLINE marker, and a claim that cites nothing is "
        "treated as unsourced."
    ),
    "d_spoke2": (
        "Under the Ledger Provenance Protocol, rule L-2 links every claim to "
        "an origin docket that records where the content first entered the "
        "system."
    ),
    "d_spoke3": (
        "Under the Ledger Provenance Protocol, rule L-3 records custody as a "
        "relay chain, with every hand-off logged in order, and a gap in the "
        "chain breaks traceability."
    ),
    "d_spoke4": (
        "Under the Ledger Provenance Protocol, rule L-4 flags unverified "
        "content as GREYMARK and keeps the flag until the source record is "
        "completed."
    ),
    "d_spoke5": (
        "Under the Ledger Provenance Protocol, rule L-5 records all timestamps "
        "in ledger time — UTC at minute precision — so local time never "
        "appears in the records."
    ),
    "d_spoke6": (
        "Under the Ledger Provenance Protocol, rule L-6 identifies authors by "
        "quill code rather than by name, so attribution survives renames."
    ),
    "d_spoke7": (
        "Under the Ledger Provenance Protocol, rule L-7 issues an UNWRITE "
        "notice for a retraction; the original record is never deleted, only "
        "marked."
    ),
    "bridge_ab": (
        "Two separate working groups maintain the standards for how records "
        "are written and how their contents are described, but the groups meet "
        "as one review board and revise their standards together each quarter, "
        "so a change on one side triggers a review on the other."
    ),
    "bridge_cd": (
        "Two separate practice areas cover accountability and record-keeping, "
        "and their findings are filed to one shared registry overseen by a "
        "single review board."
    ),
}

# MU generation order (matches seed_v2). Each entry: (mu_key, cluster_id).
# cluster_id for bridges is the bridge key itself (matches v2 convention).
MU_ORDER: List[Tuple[str, str]] = (
    [("a_hub", "cluster_a")] + [(f"a_spoke{i}", "cluster_a") for i in range(1, 8)]
    + [("b_hub", "cluster_b")] + [(f"b_spoke{i}", "cluster_b") for i in range(1, 8)]
    + [("c_hub", "cluster_c")] + [(f"c_spoke{i}", "cluster_c") for i in range(1, 8)]
    + [("d_hub", "cluster_d")] + [(f"d_spoke{i}", "cluster_d") for i in range(1, 8)]
    + [("bridge_ab", "bridge_ab"), ("bridge_cd", "bridge_cd")]
)

BRIDGE_CLUSTERS = {
    "bridge_ab": ("cluster_a", "cluster_b"),
    "bridge_cd": ("cluster_c", "cluster_d"),
}


# ---------------------------------------------------------------------------
# Import-time self-checks — run before any Ollama call so a spec-transcription
# bug (a duplicated or malformed token) fails loudly and immediately, not as a
# confusing K2-traversal ambiguity three milestones later.
# ---------------------------------------------------------------------------

_TOKEN_FORMAT_RE = re.compile(r"^[A-Za-z0-9-]+$")


def _check_token_format(token: str) -> bool:
    """Single-word answer tokens must be word-like (letters/digits/hyphens
    only — spec §4 placement rule 4). Multi-word phrase tokens are natural
    value phrases that tokenize normally under BM25 and are exempt."""
    if " " in token:
        return True
    return bool(_TOKEN_FORMAT_RE.match(token))


def _validate_answer_token_uniqueness(tokens_by_key: Optional[dict] = None) -> bool:
    """Assert no answer-token string appears under more than one mu_key
    (spec §4 placement rule 3: global uniqueness). Raises ValueError on a
    collision. Comparison is case-insensitive to match the content scans."""
    table = tokens_by_key if tokens_by_key is not None else ANSWER_TOKENS
    seen: dict[str, str] = {}
    for key, toks in table.items():
        for tok in toks:
            norm = tok.lower()
            if norm in seen and seen[norm] != key:
                raise ValueError(
                    f"Answer token {tok!r} is not unique: appears under both "
                    f"{seen[norm]!r} and {key!r}"
                )
            seen[norm] = key
    return True


def _validate_answer_token_formats(tokens_by_key: Optional[dict] = None) -> bool:
    """Assert every single-word answer token is word-like (spec §4 rule 4).
    Raises ValueError listing any malformed tokens."""
    table = tokens_by_key if tokens_by_key is not None else ANSWER_TOKENS
    bad = [(key, tok) for key, toks in table.items()
           for tok in toks if not _check_token_format(tok)]
    if bad:
        raise ValueError(f"Malformed answer token(s) (spec §4 rule 4): {bad}")
    return True


# Run the self-checks at import time on the real spec data.
_validate_answer_token_uniqueness()
_validate_answer_token_formats()


# ---------------------------------------------------------------------------
# Per-MU-category forbidden-term derivation (new relative to v2, which only
# forbade terms on bridges). Derived from the dicts above — this IS the
# hub/sibling/bridge exclusion logic, expressed as forbidden-term populations
# so the existing whole-word matcher handles all three uniformly.
# ---------------------------------------------------------------------------

def _cluster_letter(cluster_id: str) -> str:
    """'cluster_a' -> 'a'. Bridges are not passed here."""
    return cluster_id.split("_")[1]


def _answer_tokens_for_cluster(cluster_id: str) -> List[str]:
    """Every answer token belonging to any spoke in this cluster."""
    letter = _cluster_letter(cluster_id)
    tokens: List[str] = []
    for key, toks in ANSWER_TOKENS.items():
        if key.startswith(letter + "_"):
            tokens.extend(toks)
    return tokens


def _hub_forbidden_tokens(cluster_id: str) -> List[str]:
    """A hub must contain NONE of its own cluster's value tokens (it names
    rule IDs but defines no values — spec §4 placement rule 1)."""
    return _answer_tokens_for_cluster(cluster_id)


def _sibling_forbidden_tokens(mu_key: str, cluster_id: str) -> List[str]:
    """A spoke must contain none of its SIBLING spokes' answer tokens within
    the same cluster (spec §4 placement rule 2)."""
    letter = _cluster_letter(cluster_id)
    tokens: List[str] = []
    for key, toks in ANSWER_TOKENS.items():
        if key.startswith(letter + "_") and key != mu_key:
            tokens.extend(toks)
    return tokens


def _bridge_forbidden_terms(bridge_key: str) -> List[str]:
    """A bridge must contain none of either adjacent cluster's entry
    vocabulary, answer tokens, or family name (spec §4 bridge "Forbidden"
    column — a superset of v2's 5-term fingerprint list)."""
    terms: List[str] = []
    for cluster_id in BRIDGE_CLUSTERS[bridge_key]:
        terms.extend(ENTRY_VOCAB[cluster_id])
        terms.append(FAMILY_NAMES[cluster_id])
        terms.extend(_answer_tokens_for_cluster(cluster_id))
    return terms


def _forbidden_terms_for(mu_key: str, cluster_id: str) -> List[str]:
    """Dispatch the correct forbidden-term set for an MU by category."""
    if mu_key.endswith("_hub"):
        return _hub_forbidden_tokens(cluster_id)
    if cluster_id.startswith("bridge_"):
        return _bridge_forbidden_terms(cluster_id)
    return _sibling_forbidden_tokens(mu_key, cluster_id)


# ---------------------------------------------------------------------------
# Prompt construction
# ---------------------------------------------------------------------------

def _build_prompt(mu_key: str, cluster_id: str, required: Optional[List[str]],
                  forbidden: List[str]) -> str:
    """Assemble the Ollama generation prompt for one MU from its content gist
    plus its required/forbidden term lists. The MUST-NOT clause is included
    only for hubs (don't define values) and bridges (don't use cluster
    vocabulary) — a spoke's forbidden set is its siblings' invented tokens,
    which Gemma has no reason to produce, so listing them would only add
    noise to the prompt (they are still validated against)."""
    gist = CONTENT_GIST[mu_key]
    parts = ["Write exactly 2-3 sentences.", gist]
    if required:
        parts.append(
            "You MUST include all of these exact terms, each at least once: "
            + ", ".join(required) + "."
        )
    include_forbidden = mu_key.endswith("_hub") or cluster_id.startswith("bridge_")
    if forbidden and include_forbidden:
        parts.append(
            "You MUST NOT use any of these words or phrases: "
            + ", ".join(sorted(set(forbidden))) + "."
        )
    parts.append("Be direct and authoritative. No bullet points or lists.")
    return " ".join(parts)


# ---------------------------------------------------------------------------
# Edge definitions — 64 reciprocal edges (spec §2)
# ---------------------------------------------------------------------------

def _build_edges() -> list[dict]:
    """Build 64 reciprocal edges: 56 spoke<->hub (7 spokes x 4 clusters x 2
    directions) + 8 hub<->bridge (4 clusters x 2 directions).

    Each v2 edge (spoke->hub, hub->bridge) is emitted in BOTH directions with
    the same weight=0.5. The reverse edge reuses the forward edge_id with an
    'r' suffix. `direction="forward"` is a schema literal on every edge — M24
    confirmed this column does not encode which way an edge points; the actual
    directionality is source_mu_id -> target_mu_id (which is what K2 traversal
    walks). register_fixture() hardcodes creation_trigger="seed" for every
    edge it inserts, so that is not a fixture-file field."""
    created_at = datetime.now(timezone.utc).isoformat()
    edges: list[dict] = []

    def add_reciprocal(edge_id: str, source: str, target: str) -> None:
        edges.append({
            "edge_id": edge_id,
            "source_mu_id": source,
            "target_mu_id": target,
            "weight": 0.5,
            "direction": "forward",
            "created_at": created_at,
        })
        edges.append({
            "edge_id": edge_id + "r",
            "source_mu_id": target,
            "target_mu_id": source,
            "weight": 0.5,
            "direction": "forward",
            "created_at": created_at,
        })

    # Per-cluster: 7 spoke<->hub pairs + 1 hub<->bridge pair.
    cluster_letters = ["a", "b", "c", "d"]
    hub_to_bridge = {
        "a": "bridge_ab", "b": "bridge_ab",
        "c": "bridge_cd", "d": "bridge_cd",
    }
    bridge_edge_id = {
        "a": "eab0001a", "b": "eab0001b",
        "c": "ecd0001c", "d": "ecd0001d",
    }
    for letter in cluster_letters:
        hub_id = MU_IDS[f"{letter}_hub"]
        for i in range(1, 8):
            add_reciprocal(
                f"e{letter}{i + 1:06d}",
                MU_IDS[f"{letter}_spoke{i}"],
                hub_id,
            )
        add_reciprocal(
            bridge_edge_id[letter],
            hub_id,
            MU_IDS[hub_to_bridge[letter]],
        )

    return edges


# ---------------------------------------------------------------------------
# Ollama generation + validation
# ---------------------------------------------------------------------------

def _call_ollama(prompt: str) -> str:
    """Call Ollama to generate text. Returns stripped response content."""
    payload = {
        "model": MODEL,
        "messages": [{"role": "user", "content": prompt}],
        "stream": False,
    }
    with httpx.Client(timeout=TIMEOUT) as client:
        response = client.post(OLLAMA_URL, json=payload)
        response.raise_for_status()
        data = response.json()
        return data["choices"][0]["message"]["content"].strip()


def _check_required_terms(content: str, terms: list[str]) -> list[str]:
    """Return required terms missing from content (case-insensitive substring
    match — lenient, so 'version' is satisfied by 'versions'). Matches v2."""
    lower = content.lower()
    return [t for t in terms if t.lower() not in lower]


def _check_forbidden_terms(content: str, terms: list[str]) -> list[str]:
    """Return forbidden terms present in content, using whole-word/whole-phrase
    matching (case-insensitive) to avoid false positives like 'format' inside
    'reformatting'. Multi-word phrase tokens ('spine order') match as a whole
    phrase. Matches v2's forbidden-term semantics."""
    lower = content.lower()
    found = []
    for t in terms:
        if re.search(r"\b" + re.escape(t.lower()) + r"\b", lower):
            found.append(t)
    return found


def generate_mu(mu_key: str, cluster_id: str, source_type: str, prompt: str,
                required: Optional[List[str]], forbidden: Optional[List[str]]
                ) -> Tuple[dict, Optional[dict]]:
    """Generate content for one MU, retrying if required/forbidden validation
    fails. Returns (mu_dict, failure_record_or_None). The failure record is
    populated only when MAX_RETRIES is exhausted with an unresolved violation;
    the best-effort content is kept in the draft either way (flagged via the
    manifest, does not block the run)."""
    mu_id = MU_IDS[mu_key]
    logger.info("Generating %s (%s) [mu_id=%s...]", mu_key, cluster_id, mu_id[:8])

    content = ""
    missing: List[str] = []
    present: List[str] = []
    attempts = 0

    for attempt in range(1, MAX_RETRIES + 1):
        attempts = attempt
        try:
            content = _call_ollama(prompt)
        except Exception as exc:
            logger.warning("  Attempt %d: Ollama call failed: %s", attempt, exc)
            if attempt < MAX_RETRIES:
                time.sleep(RETRY_DELAY)
                continue
            raise

        missing = _check_required_terms(content, required or [])
        present = _check_forbidden_terms(content, forbidden or [])

        if not missing and not present:
            logger.info(
                "  OK: %d chars, %d sentences (approx)",
                len(content), content.count("."),
            )
            break

        logger.warning(
            "  Attempt %d: missing=%s forbidden=%s", attempt, missing, present
        )
        if attempt < MAX_RETRIES:
            time.sleep(2)
            continue

        logger.warning(
            "  All %d retries exhausted for %s — recording manifest failure, "
            "keeping best-effort content in draft", MAX_RETRIES, mu_key
        )

    failure = None
    if missing or present:
        failure = {
            "mu_id": mu_id,
            "mu_key": mu_key,
            "cluster_id": cluster_id,
            "attempts": attempts,
            "missing_required": missing,
            "forbidden_leaked": present,
            "last_attempted_text": content,
        }

    mu = {
        "mu_id": mu_id,
        "source_type": source_type,
        "raw_content": content,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "cluster_id": cluster_id,
    }
    return mu, failure


# ---------------------------------------------------------------------------
# Manifest
# ---------------------------------------------------------------------------

def _write_manifest(failures: List[dict]) -> None:
    """Write the generation manifest — even when empty. An empty
    "failures": [] is a meaningful PASS signal (present, not absent), matching
    this project's empty-result-structure convention."""
    manifest = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "source_script": "generate_seed_v3.py",
        "mu_count_failed": len(failures),
        "failures": failures,
    }
    with open(MANIFEST_PATH, "w", encoding="utf-8") as f:
        json.dump(manifest, f, indent=2, ensure_ascii=False)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    logger.info("=== generate_seed_v3.py ===")
    logger.info("Model: %s at %s", MODEL, OLLAMA_URL)
    logger.info("Output: %s", OUTPUT_PATH)
    logger.info("Manifest: %s", MANIFEST_PATH)
    logger.info("MUs to generate: %d", len(MU_ORDER))

    # Verify Ollama is reachable before starting.
    try:
        with httpx.Client(timeout=10) as client:
            r = client.get("http://localhost:11434/api/tags")
            r.raise_for_status()
        logger.info("Ollama connection: OK")
    except Exception as exc:
        logger.error("Cannot reach Ollama at localhost:11434: %s", exc)
        logger.error("Start Ollama and load gemma4:e4b before running this script.")
        sys.exit(1)

    OUTPUT_PATH.parent.mkdir(parents=True, exist_ok=True)

    memory_units: List[dict] = []
    failures: List[dict] = []
    for mu_key, cluster_id in MU_ORDER:
        required = REQUIRED.get(mu_key)
        forbidden = _forbidden_terms_for(mu_key, cluster_id)
        prompt = _build_prompt(mu_key, cluster_id, required, forbidden)
        mu, failure = generate_mu(
            mu_key, cluster_id, "agent_output", prompt, required, forbidden
        )
        memory_units.append(mu)
        if failure is not None:
            failures.append(failure)

    edges = _build_edges()

    fixture = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "mu_count": len(memory_units),
        "edge_count": len(edges),
        "fixture_version": "v3",
        "notes": (
            "DRAFT — review before freezing to seed_v3.json.gz. "
            "See scripts/generate_seed_v3.py for freeze instructions. "
            "Run scripts/verify_seed_v3.py --draft first."
        ),
        "memory_units": memory_units,
        "edges": edges,
    }

    with open(OUTPUT_PATH, "w", encoding="utf-8") as f:
        json.dump(fixture, f, indent=2, ensure_ascii=False)

    _write_manifest(failures)

    logger.info("=== Done ===")
    logger.info(
        "Wrote %d MUs and %d edges to %s", len(memory_units), len(edges), OUTPUT_PATH
    )

    # Manifest summary — one line per failure, plus the exact next step.
    if failures:
        logger.warning("%d MU(s) failed validation after %d retries:",
                       len(failures), MAX_RETRIES)
        for f in failures:
            logger.warning(
                "  %s (%s): missing=%s leaked=%s",
                f["mu_key"], f["cluster_id"],
                f["missing_required"], f["forbidden_leaked"],
            )
        first = failures[0]
        logger.warning(
            "Next step: edit seed_v3_draft.json's raw_content for %s (and any "
            "others above), then rerun `python scripts/verify_seed_v3.py --draft`",
            first["mu_key"],
        )
    else:
        logger.info("All MUs passed required/forbidden validation (manifest "
                    "failures: []). Next: python scripts/verify_seed_v3.py --draft")


if __name__ == "__main__":
    main()
