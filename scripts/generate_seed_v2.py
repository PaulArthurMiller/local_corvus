"""
Generate seed_v2 MU content via Gemma 4 (local Ollama).
Output: seed_fixtures/seed_v2_draft.json — review before freezing.

Usage: python scripts/generate_seed_v2.py
Requires: Ollama running at localhost:11434 with gemma4:e4b loaded

Produces 34 MUs across 4 clusters and 2 bridges, with 32 hub-and-spoke
edges, written to seed_fixtures/seed_v2_draft.json as uncompressed JSON.
That file is NOT seed_v2.json.gz — freeze manually after Paul's review:

  python -c "
  import gzip, json
  with open('seed_fixtures/seed_v2_draft.json') as f:
      data = json.load(f)
  data.pop('notes', None)
  with gzip.open('seed_fixtures/seed_v2.json.gz', 'wt', encoding='utf-8') as f:
      json.dump(data, f)
  print('Frozen seed_fixtures/seed_v2.json.gz')
  "
"""

import json
import logging
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import List, Optional

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
OUTPUT_PATH = PROJECT_ROOT / "seed_fixtures" / "seed_v2_draft.json"

# ---------------------------------------------------------------------------
# Fixed MU IDs — stable across reruns so probe_set_v2.yaml can reference them
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

# Fingerprint vocabulary per cluster — terms that MUST appear in each in-cluster MU
CLUSTER_A_FP = ["format", "structure", "output", "field", "delimiter"]
CLUSTER_B_FP = ["schema", "data", "shape", "type", "validation"]
CLUSTER_C_FP = ["rule", "constraint", "violation", "enforce", "comply"]
CLUSTER_D_FP = ["source", "provenance", "cite", "origin", "traceable"]

# Bridge MUs must NOT contain any fingerprint term from either adjacent cluster
BRIDGE_AB_FORBIDDEN = set(CLUSTER_A_FP) | set(CLUSTER_B_FP)
BRIDGE_CD_FORBIDDEN = set(CLUSTER_C_FP) | set(CLUSTER_D_FP)

# ---------------------------------------------------------------------------
# MU specifications: (key, cluster_id, source_type, generation_prompt,
#                     required_terms_or_None, forbidden_terms_or_None)
# ---------------------------------------------------------------------------

MU_SPECS = [
    # === cluster_a: structured file output / format discipline ===
    (
        "a_hub", "cluster_a", "agent_output",
        (
            "Write exactly 2-3 sentences stating a precise principle: "
            "consistent, well-defined structure in every output file depends on "
            "using explicit format rules for each field and a uniform delimiter. "
            "You MUST include all five of these words at least once each: "
            "format, structure, output, field, delimiter. "
            "Be direct and authoritative. No bullet points or lists."
        ),
        ["format", "structure", "output", "field", "delimiter"], None,
    ),
    (
        "a_spoke1", "cluster_a", "agent_output",
        (
            "Write exactly 2-3 sentences about maintaining consistent field ordering "
            "in structured output. Explain why every field must appear in the same "
            "position across all records for the output format to be machine-readable. "
            "You MUST include these words: field, output, format, structure. "
            "No bullet points."
        ),
        ["field", "output", "format"], None,
    ),
    (
        "a_spoke2", "cluster_a", "agent_output",
        (
            "Write exactly 2-3 sentences about delimiter choice in structured output. "
            "Explain why the delimiter must be consistent across all records and "
            "must never appear unescaped inside a field value. "
            "You MUST include these words: delimiter, field, output, format. "
            "No bullet points."
        ),
        ["delimiter", "field", "output"], None,
    ),
    (
        "a_spoke3", "cluster_a", "agent_output",
        (
            "Write exactly 2-3 sentences about header rows in structured output files. "
            "Explain why the header must mirror the structure of data rows exactly, "
            "and what happens to downstream consumers when it doesn't. "
            "You MUST include these words: structure, output, format, field. "
            "No bullet points."
        ),
        ["structure", "output", "field"], None,
    ),
    (
        "a_spoke4", "cluster_a", "agent_output",
        (
            "Write exactly 2-3 sentences about whitespace and padding in structured output. "
            "Explain why extra spaces or inconsistent padding inside a field value "
            "break the expected output format. "
            "You MUST include these words: field, format, output, structure. "
            "No bullet points."
        ),
        ["field", "format", "output"], None,
    ),
    (
        "a_spoke5", "cluster_a", "agent_output",
        (
            "Write exactly 2-3 sentences about quoting rules in delimited output. "
            "Explain when field values must be quoted and what happens to parsing "
            "when quoting is inconsistent across the output. "
            "You MUST include these words: field, delimiter, output, format. "
            "No bullet points."
        ),
        ["field", "delimiter", "output"], None,
    ),
    (
        "a_spoke6", "cluster_a", "agent_output",
        (
            "Write exactly 2-3 sentences about encoding consistency in structured file output. "
            "Explain why mixed character encodings within a single output file "
            "break every downstream parser that expects a uniform format. "
            "You MUST include these words: output, structure, format, field. "
            "No bullet points."
        ),
        ["output", "structure", "format"], None,
    ),
    (
        "a_spoke7", "cluster_a", "agent_output",
        (
            "Write exactly 2-3 sentences about missing values in structured output. "
            "Explain why absent fields must use a consistent placeholder in the output "
            "and why silently omitting a field corrupts the expected format and structure. "
            "You MUST include these words: field, output, structure, format, delimiter. "
            "No bullet points."
        ),
        ["field", "output", "structure", "delimiter"], None,
    ),

    # === cluster_b: schema and data-shape reasoning ===
    (
        "b_hub", "cluster_b", "agent_output",
        (
            "Write exactly 2-3 sentences stating a precise principle: "
            "a well-defined schema specifies the expected data shape and type for each element, "
            "making validation of incoming data unambiguous. "
            "You MUST include all five of these words at least once each: "
            "schema, data, shape, type, validation. "
            "Be direct and authoritative. No bullet points or lists."
        ),
        ["schema", "data", "shape", "type", "validation"], None,
    ),
    (
        "b_spoke1", "cluster_b", "agent_output",
        (
            "Write exactly 2-3 sentences about strict type checking in schema validation. "
            "Explain why accepting the wrong type for a data element silently "
            "corrupts downstream consumers that rely on the schema. "
            "You MUST include these words: type, schema, data, validation. "
            "No bullet points."
        ),
        ["type", "schema", "data", "validation"], None,
    ),
    (
        "b_spoke2", "cluster_b", "agent_output",
        (
            "Write exactly 2-3 sentences about required versus optional elements in a schema. "
            "Explain how failing to distinguish required from optional in the schema "
            "creates ambiguous data shapes that fail validation unpredictably. "
            "You MUST include these words: schema, data, shape, type, validation. "
            "No bullet points."
        ),
        ["schema", "data", "shape", "type", "validation"], None,
    ),
    (
        "b_spoke3", "cluster_b", "agent_output",
        (
            "Write exactly 2-3 sentences about schema evolution and backward compatibility. "
            "Explain why adding a required element to a schema without updating validation "
            "logic breaks existing data producers that conform to the old shape. "
            "You MUST include these words: schema, data, validation, type, shape. "
            "No bullet points."
        ),
        ["schema", "data", "validation", "shape"], None,
    ),
    (
        "b_spoke4", "cluster_b", "agent_output",
        (
            "Write exactly 2-3 sentences about null versus absent in data shape contracts. "
            "Explain the semantic difference between a null value and a missing element, "
            "and why schemas must specify which is permitted for each type. "
            "You MUST include these words: schema, data, shape, type, validation. "
            "No bullet points."
        ),
        ["schema", "data", "shape", "type"], None,
    ),
    (
        "b_spoke5", "cluster_b", "agent_output",
        (
            "Write exactly 2-3 sentences about union types in schema design. "
            "Explain why permitting a single element to carry multiple types "
            "creates validation ambiguity and what disciplines resolve it in practice. "
            "You MUST include these words: type, schema, data, shape, validation. "
            "No bullet points."
        ),
        ["type", "schema", "data", "validation"], None,
    ),
    (
        "b_spoke6", "cluster_b", "agent_output",
        (
            "Write exactly 2-3 sentences about schema-first versus code-first development. "
            "Explain why defining the data shape in a schema before writing any code "
            "reduces type mismatches that only emerge at validation time in production. "
            "You MUST include these words: schema, data, shape, validation, type. "
            "No bullet points."
        ),
        ["schema", "data", "shape", "validation", "type"], None,
    ),
    (
        "b_spoke7", "cluster_b", "agent_output",
        (
            "Write exactly 2-3 sentences about runtime schema validation at system boundaries. "
            "Explain why validating data shape at every entry point prevents corrupt type "
            "information from propagating through a system that trusts its schema. "
            "You MUST include these words: schema, data, shape, type, validation. "
            "No bullet points."
        ),
        ["schema", "data", "shape", "type", "validation"], None,
    ),

    # === cluster_c: rule-following under constraint ===
    (
        "c_hub", "cluster_c", "agent_output",
        (
            "Write exactly 2-3 sentences stating a precise principle: "
            "every rule exists to enforce a constraint, and any violation must be "
            "detected and reported rather than silently tolerated — "
            "systems that rely on agents to comply voluntarily without enforcement fail. "
            "You MUST include all five of these words at least once each: "
            "rule, constraint, violation, enforce, comply. "
            "Be direct and authoritative. No bullet points or lists."
        ),
        ["rule", "constraint", "violation", "enforce", "comply"], None,
    ),
    (
        "c_spoke1", "cluster_c", "agent_output",
        (
            "Write exactly 2-3 sentences about input validation rules at API boundaries. "
            "Explain why enforcing constraints on incoming requests at the entry point "
            "prevents violations from reaching business logic that cannot comply with corrupt input. "
            "You MUST include these words: rule, constraint, enforce, violation, comply. "
            "No bullet points."
        ),
        ["rule", "constraint", "enforce", "violation"], None,
    ),
    (
        # c0000003 — gold MU for probe p14 (idempotency rule)
        "c_spoke2", "cluster_c", "agent_output",
        (
            "Write exactly 2-3 sentences about idempotency rules in distributed systems. "
            "Explain why the constraint that an operation must produce the same result "
            "no matter how many times it runs must be enforced at every call site — "
            "any violation here causes systems to comply with different states on each run. "
            "You MUST include these words: rule, constraint, enforce, comply, violation. "
            "No bullet points."
        ),
        ["rule", "constraint", "enforce", "comply", "violation"], None,
    ),
    (
        "c_spoke3", "cluster_c", "agent_output",
        (
            "Write exactly 2-3 sentences about immutability rules in data pipelines. "
            "Explain why the constraint that raw data must never be modified in place "
            "protects the audit trail from violations introduced by downstream stages "
            "that fail to comply with the rule. "
            "You MUST include these words: rule, constraint, violation, enforce, comply. "
            "No bullet points."
        ),
        ["rule", "constraint", "violation", "enforce"], None,
    ),
    (
        # c0000005 — gold MU for probe p15 (access control rule)
        "c_spoke4", "cluster_c", "agent_output",
        (
            "Write exactly 2-3 sentences about access control rules in multi-tenant systems. "
            "Explain why every tenant boundary must have its constraint enforced by the platform "
            "and not rely on each consumer to comply voluntarily — "
            "voluntary compliance guarantees no violation detection. "
            "You MUST include these words: rule, constraint, enforce, comply, violation. "
            "No bullet points."
        ),
        ["rule", "constraint", "enforce", "comply", "violation"], None,
    ),
    (
        "c_spoke5", "cluster_c", "agent_output",
        (
            "Write exactly 2-3 sentences about rate-limiting rules in public APIs. "
            "Explain why the constraint on request frequency must be enforced server-side "
            "so that non-compliant clients trigger a violation that is logged and blocked. "
            "You MUST include these words: rule, constraint, enforce, comply, violation. "
            "No bullet points."
        ),
        ["rule", "constraint", "enforce", "comply", "violation"], None,
    ),
    (
        "c_spoke6", "cluster_c", "agent_output",
        (
            "Write exactly 2-3 sentences about ordering constraints in event-driven systems. "
            "Explain why the rule that events must be processed in sequence must be enforced, "
            "or violations will cause consumers to comply with inconsistent state. "
            "You MUST include these words: rule, constraint, enforce, violation, comply. "
            "No bullet points."
        ),
        ["rule", "constraint", "enforce", "violation"], None,
    ),
    (
        "c_spoke7", "cluster_c", "agent_output",
        (
            "Write exactly 2-3 sentences about naming convention rules in large codebases. "
            "Explain why enforcing a constraint on identifier names reduces violations "
            "that spread confusion across teams and makes the codebase comply with its own standards. "
            "You MUST include these words: rule, constraint, enforce, comply, violation. "
            "No bullet points."
        ),
        ["rule", "constraint", "enforce", "comply", "violation"], None,
    ),

    # === cluster_d: self-sourcing / provenance discipline ===
    (
        "d_hub", "cluster_d", "agent_output",
        (
            "Write exactly 2-3 sentences stating a precise principle: "
            "every claim in a system must cite its origin so that the provenance "
            "of each conclusion is fully traceable back to a primary source. "
            "You MUST include all five of these words at least once each: "
            "source, provenance, cite, origin, traceable. "
            "Be direct and authoritative. No bullet points or lists."
        ),
        ["source", "provenance", "cite", "origin", "traceable"], None,
    ),
    (
        "d_spoke1", "cluster_d", "agent_output",
        (
            "Write exactly 2-3 sentences about citing primary sources in research outputs. "
            "Explain why every factual claim must trace to its origin, and what makes "
            "provenance untrustworthy when the chain back to the primary source is broken. "
            "You MUST include these words: source, provenance, cite, origin, traceable. "
            "No bullet points."
        ),
        ["source", "provenance", "cite", "origin", "traceable"], None,
    ),
    (
        "d_spoke2", "cluster_d", "agent_output",
        (
            "Write exactly 2-3 sentences about data lineage in analytical pipelines. "
            "Explain why every derived dataset must record its origin and transformations "
            "so that its provenance is fully traceable back to the primary source. "
            "You MUST include these words: origin, provenance, source, traceable, cite. "
            "No bullet points."
        ),
        ["origin", "provenance", "source", "traceable"], None,
    ),
    (
        "d_spoke3", "cluster_d", "agent_output",
        (
            "Write exactly 2-3 sentences about version provenance in software artifacts. "
            "Explain why each build artifact must cite the exact source commit and dependency "
            "versions so that its origin is traceable for debugging and reproducibility. "
            "You MUST include these words: provenance, source, cite, origin, traceable. "
            "No bullet points."
        ),
        ["provenance", "source", "cite", "origin", "traceable"], None,
    ),
    (
        "d_spoke4", "cluster_d", "agent_output",
        (
            "Write exactly 2-3 sentences about attribution in AI-generated content. "
            "Explain why AI outputs that incorporate external information must cite "
            "the origin of each claim to maintain provenance and remain traceable to a source. "
            "You MUST include these words: cite, origin, provenance, source, traceable. "
            "No bullet points."
        ),
        ["cite", "origin", "provenance", "source", "traceable"], None,
    ),
    (
        "d_spoke5", "cluster_d", "agent_output",
        (
            "Write exactly 2-3 sentences about audit trails in financial systems. "
            "Explain why every transaction must record its source and origin so that "
            "the provenance of each balance change is fully traceable and can be cited. "
            "You MUST include these words: source, origin, provenance, traceable, cite. "
            "No bullet points."
        ),
        ["source", "origin", "provenance", "traceable", "cite"], None,
    ),
    (
        "d_spoke6", "cluster_d", "agent_output",
        (
            "Write exactly 2-3 sentences about supply chain provenance in hardware. "
            "Explain why each component must cite its manufacturing origin and primary source "
            "so that the supply chain is fully traceable for security audits. "
            "You MUST include these words: provenance, origin, source, cite, traceable. "
            "No bullet points."
        ),
        ["provenance", "origin", "source", "cite", "traceable"], None,
    ),
    (
        "d_spoke7", "cluster_d", "agent_output",
        (
            "Write exactly 2-3 sentences about model weight provenance in machine learning. "
            "Explain why every trained model must cite the datasets and procedures "
            "used at its origin so that its provenance is traceable to a verifiable source. "
            "You MUST include these words: provenance, origin, cite, source, traceable. "
            "No bullet points."
        ),
        ["provenance", "origin", "cite", "source", "traceable"], None,
    ),

    # === bridge_ab: "structure is a contract" ===
    # Must NOT use any of: format, structure, output, field, delimiter,
    #                       schema, data, shape, type, validation
    (
        "bridge_ab", "bridge_ab", "agent_output",
        (
            "Write exactly 2-3 sentences about the idea that any two parties who exchange "
            "information must agree in advance on how that information will be arranged and "
            "what each position means — this mutual compact makes interpretation unambiguous. "
            "Without such an agreement, neither party can trust what they send or receive. "
            "Do NOT use any of these words: "
            "format, structure, output, field, delimiter, schema, data, shape, type, validation. "
            "No bullet points or lists."
        ),
        None,
        list(BRIDGE_AB_FORBIDDEN),
    ),

    # === bridge_cd: "constraint over fluency" ===
    # Must NOT use any of: rule, constraint, violation, enforce, comply,
    #                       source, provenance, cite, origin, traceable
    (
        "bridge_cd", "bridge_cd", "agent_output",
        (
            "Write exactly 2-3 sentences about the idea that working within well-defined "
            "limits tends to produce more trustworthy and verifiable results than unconstrained "
            "expression. When an agent accepts defined boundaries, the resulting work can be "
            "checked and its reliability confirmed by others. "
            "Do NOT use any of these words: "
            "rule, constraint, violation, enforce, comply, "
            "source, provenance, cite, origin, traceable. "
            "No bullet points or lists."
        ),
        None,
        list(BRIDGE_CD_FORBIDDEN),
    ),
]


# ---------------------------------------------------------------------------
# Edge definitions
# ---------------------------------------------------------------------------

def _build_edges() -> list[dict]:
    """Build all 32 hub-and-spoke + bridge edges with fixed IDs."""
    edges = []
    created_at = datetime.now(timezone.utc).isoformat()

    # cluster_a: 7 spokes → hub + hub → bridge_ab
    for i in range(1, 8):
        edges.append({
            "edge_id": f"ea{i + 1:06d}",
            "source_mu_id": MU_IDS[f"a_spoke{i}"],
            "target_mu_id": MU_IDS["a_hub"],
            "weight": 0.5,
            "direction": "forward",
            "created_at": created_at,
        })
    edges.append({
        "edge_id": "eab0001a",
        "source_mu_id": MU_IDS["a_hub"],
        "target_mu_id": MU_IDS["bridge_ab"],
        "weight": 0.5,
        "direction": "forward",
        "created_at": created_at,
    })

    # cluster_b: 7 spokes → hub + hub → bridge_ab
    for i in range(1, 8):
        edges.append({
            "edge_id": f"eb{i + 1:06d}",
            "source_mu_id": MU_IDS[f"b_spoke{i}"],
            "target_mu_id": MU_IDS["b_hub"],
            "weight": 0.5,
            "direction": "forward",
            "created_at": created_at,
        })
    edges.append({
        "edge_id": "eab0001b",
        "source_mu_id": MU_IDS["b_hub"],
        "target_mu_id": MU_IDS["bridge_ab"],
        "weight": 0.5,
        "direction": "forward",
        "created_at": created_at,
    })

    # cluster_c: 7 spokes → hub + hub → bridge_cd
    for i in range(1, 8):
        edges.append({
            "edge_id": f"ec{i + 1:06d}",
            "source_mu_id": MU_IDS[f"c_spoke{i}"],
            "target_mu_id": MU_IDS["c_hub"],
            "weight": 0.5,
            "direction": "forward",
            "created_at": created_at,
        })
    edges.append({
        "edge_id": "ecd0001c",
        "source_mu_id": MU_IDS["c_hub"],
        "target_mu_id": MU_IDS["bridge_cd"],
        "weight": 0.5,
        "direction": "forward",
        "created_at": created_at,
    })

    # cluster_d: 7 spokes → hub + hub → bridge_cd
    for i in range(1, 8):
        edges.append({
            "edge_id": f"ed{i + 1:06d}",
            "source_mu_id": MU_IDS[f"d_spoke{i}"],
            "target_mu_id": MU_IDS["d_hub"],
            "weight": 0.5,
            "direction": "forward",
            "created_at": created_at,
        })
    edges.append({
        "edge_id": "ecd0001d",
        "source_mu_id": MU_IDS["d_hub"],
        "target_mu_id": MU_IDS["bridge_cd"],
        "weight": 0.5,
        "direction": "forward",
        "created_at": created_at,
    })

    return edges


# ---------------------------------------------------------------------------
# Ollama generation
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
    """Return list of required terms missing from content (case-insensitive)."""
    lower = content.lower()
    return [t for t in terms if t.lower() not in lower]


def _check_forbidden_terms(content: str, terms: list[str]) -> list[str]:
    """Return list of forbidden terms found in content (case-insensitive)."""
    lower = content.lower()
    # Check as whole words to avoid false positives (e.g. "format" in "reformatting")
    import re
    found = []
    for t in terms:
        if re.search(r'\b' + re.escape(t.lower()) + r'\b', lower):
            found.append(t)
    return found


def generate_mu(key: str, cluster_id: str, source_type: str, prompt: str,
                required: Optional[List[str]], forbidden: Optional[List[str]]) -> dict:
    """Generate content for one MU, retrying if validation fails."""
    mu_id = MU_IDS[key]
    logger.info("Generating %s (%s) [mu_id=%s...]", key, cluster_id, mu_id[:8])

    for attempt in range(1, MAX_RETRIES + 1):
        try:
            content = _call_ollama(prompt)
        except Exception as exc:
            logger.warning("  Attempt %d: Ollama call failed: %s", attempt, exc)
            if attempt < MAX_RETRIES:
                time.sleep(RETRY_DELAY)
                continue
            raise

        # Validate required terms
        missing = _check_required_terms(content, required or [])
        if missing:
            logger.warning(
                "  Attempt %d: missing required terms %s — retrying", attempt, missing
            )
            if attempt < MAX_RETRIES:
                time.sleep(2)
                continue

        # Validate forbidden terms
        present = _check_forbidden_terms(content, forbidden or [])
        if present:
            logger.warning(
                "  Attempt %d: forbidden terms found %s — retrying", attempt, present
            )
            if attempt < MAX_RETRIES:
                time.sleep(2)
                continue

        if missing or present:
            logger.warning(
                "  All retries exhausted for %s — keeping best output with warnings", key
            )
            logger.warning("    Missing: %s  Forbidden: %s", missing, present)

        logger.info("  OK: %d chars, %d sentences (approx)", len(content), content.count('.'))
        break

    return {
        "mu_id": mu_id,
        "source_type": source_type,
        "raw_content": content,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "cluster_id": cluster_id,
    }


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    logger.info("=== generate_seed_v2.py ===")
    logger.info("Model: %s at %s", MODEL, OLLAMA_URL)
    logger.info("Output: %s", OUTPUT_PATH)
    logger.info("MUs to generate: %d", len(MU_SPECS))

    # Verify Ollama is reachable before starting
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

    memory_units = []
    for spec in MU_SPECS:
        key, cluster_id, source_type, prompt, required, forbidden = spec
        mu = generate_mu(key, cluster_id, source_type, prompt, required, forbidden)
        memory_units.append(mu)

    edges = _build_edges()

    fixture = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "mu_count": len(memory_units),
        "edge_count": len(edges),
        "fixture_version": "v2",
        "notes": (
            "DRAFT — review before freezing to seed_v2.json.gz. "
            "See scripts/generate_seed_v2.py for freeze instructions."
        ),
        "memory_units": memory_units,
        "edges": edges,
    }

    with open(OUTPUT_PATH, "w", encoding="utf-8") as f:
        json.dump(fixture, f, indent=2, ensure_ascii=False)

    logger.info("=== Done ===")
    logger.info("Wrote %d MUs and %d edges to %s", len(memory_units), len(edges), OUTPUT_PATH)
    logger.info("Review the draft, then freeze with:")
    logger.info(
        '  python -c "'
        'import gzip,json; d=json.load(open(\'seed_fixtures/seed_v2_draft.json\')); '
        'd.pop(\'notes\',None); '
        'gzip.open(\'seed_fixtures/seed_v2.json.gz\',\'wt\').write(json.dumps(d))'
        '"'
    )


if __name__ == "__main__":
    main()
