"""
Prompt bank generator (M19 Phase 1) — the only file that talks to the
Anthropic API. Runs once per (seed fixture version + generation params),
host-side, from a terminal — never from inside Docker, never during an
experiment turn. Once a bank file exists, runs are fully offline again.

Reads cluster MU content directly from seed_fixtures/seed_v2.json.gz (mirrors
the verify_seed_v2.py convention — no backend dependency, works with Docker
down). Imports the fingerprint vocabulary from generate_seed_v2.py so the
lexical-distinctness check has exactly one source of truth for "what counts
as cluster C vocabulary".
"""

import gzip
import hashlib
import json
import logging
import os
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Optional

import anthropic
import yaml

from generate_seed_v2 import CLUSTER_A_FP, CLUSTER_B_FP, CLUSTER_C_FP, CLUSTER_D_FP, MU_IDS
from harness.prompt_bank import PromptBank, PromptBankEntry, bank_path_for_hash, compute_bank_hash, load_bank, save_bank
from harness.schema import RunPlan

logger = logging.getLogger("harness.prompt_gen")

CLUSTERS = ["cluster_a", "cluster_b", "cluster_c", "cluster_d"]

FINGERPRINT_BY_CLUSTER = {
    "cluster_a": CLUSTER_A_FP,
    "cluster_b": CLUSTER_B_FP,
    "cluster_c": CLUSTER_C_FP,
    "cluster_d": CLUSTER_D_FP,
}

SEED_FIXTURE_VERSION = "v2"
CHARS_PER_TOKEN_ESTIMATE = 4

CONFIG_PATH = Path(__file__).parent.parent.parent / "config.yaml"

# Fallback pricing if config.yaml has no prompt_gen section — Sonnet 5
# standard rate ($3/$15 per MTok; see config.yaml for the current value).
_DEFAULT_PRICE_IN = 3.0
_DEFAULT_PRICE_OUT = 15.0


class MissingApiKeyError(Exception):
    """ANTHROPIC_API_KEY is not set. Raised before any API call is attempted."""


class GenerationAborted(Exception):
    """User declined the cost-confirmation prompt. No API calls were made."""


def _get_api_key() -> str:
    key = os.environ.get("ANTHROPIC_API_KEY")
    if not key:
        raise MissingApiKeyError(
            "ANTHROPIC_API_KEY is not set. Export it before running prompt_gen.py "
            "(host-side only — never set inside the Docker container)."
        )
    return key


def load_pricing(config_path: Path = CONFIG_PATH) -> tuple:
    """Read Sonnet 5 per-MTok rates from config.yaml's prompt_gen section."""
    try:
        with open(config_path, "r", encoding="utf-8") as f:
            config = yaml.safe_load(f) or {}
    except FileNotFoundError:
        return _DEFAULT_PRICE_IN, _DEFAULT_PRICE_OUT
    pg = config.get("prompt_gen") or {}
    return (
        float(pg.get("sonnet_price_per_mtok_in", _DEFAULT_PRICE_IN)),
        float(pg.get("sonnet_price_per_mtok_out", _DEFAULT_PRICE_OUT)),
    )


# ---------------------------------------------------------------------------
# Cluster content loading
# ---------------------------------------------------------------------------

def _cluster_letter(cluster_id: str) -> str:
    return cluster_id.rsplit("_", 1)[-1]


def read_cluster_content(fixture_path: Path) -> dict:
    """
    Read seed_v2.json.gz and group MU content by cluster (bridges excluded —
    Phase 1 draws bulk-turn content from the 4 real clusters only).

    Returns {cluster_id: {"full_content": str, "hub_text": str, "spoke_text": str}}.
    """
    with gzip.open(fixture_path, "rt", encoding="utf-8") as f:
        data = json.load(f)

    mus = data.get("memory_units", [])
    by_cluster: dict = {}
    for mu in mus:
        cid = mu.get("cluster_id")
        if cid in CLUSTERS:
            by_cluster.setdefault(cid, []).append(mu)

    result = {}
    for cluster_id in CLUSTERS:
        mus_in_cluster = by_cluster.get(cluster_id, [])
        letter = _cluster_letter(cluster_id)
        hub_id = MU_IDS.get(f"{letter}_hub")
        spoke_id = MU_IDS.get(f"{letter}_spoke1")
        hub_text = next((m["raw_content"] for m in mus_in_cluster if m["mu_id"] == hub_id), "")
        spoke_text = next((m["raw_content"] for m in mus_in_cluster if m["mu_id"] == spoke_id), "")
        result[cluster_id] = {
            "full_content": "\n".join(m["raw_content"] for m in mus_in_cluster),
            "hub_text": hub_text,
            "spoke_text": spoke_text,
        }
    return result


def compute_fixture_manifest_hash(fixture_path: Path) -> str:
    """
    SHA-256 of the fixture file's raw bytes. Reimplemented locally (rather
    than importing backend.analytics.seed_fixture.compute_fixture_hash)
    because that module pulls in the backend's DB layer — not appropriate
    for a standalone host-side script.
    """
    sha = hashlib.sha256()
    with open(fixture_path, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            sha.update(chunk)
    return sha.hexdigest()


# ---------------------------------------------------------------------------
# Deterministic N-distribution
# ---------------------------------------------------------------------------

def distribute_counts(n_total: int, clusters: list = CLUSTERS) -> dict:
    """Round-robin distribution of n_total across clusters; remainder goes
    to the earliest clusters (deterministic)."""
    n_total = max(0, n_total)
    base, remainder = divmod(n_total, len(clusters))
    counts = {c: base for c in clusters}
    for i in range(remainder):
        counts[clusters[i]] += 1
    return counts


def distribute_hop_levels(n: int, levels: list) -> list:
    """Deterministic round-robin sequence of hop_distance values, length n."""
    if not levels:
        return [0] * n
    return [levels[i % len(levels)] for i in range(n)]


# ---------------------------------------------------------------------------
# Lexical-distinctness check (pure Python, no API call)
# ---------------------------------------------------------------------------

def fingerprint_scan(prompt_text: str, cluster_id: str) -> list:
    """
    Return fingerprint terms from OTHER clusters found in prompt_text
    (whole-word, case-insensitive). A prompt aimed at cluster_id must not
    leak another cluster's vocabulary — see the module docstring in
    generate_seed_v2.py for why this corrupts the hop-distance measurement.
    """
    lower = prompt_text.lower()
    leaked = []
    for other_cluster, terms in FINGERPRINT_BY_CLUSTER.items():
        if other_cluster == cluster_id:
            continue
        for term in terms:
            if re.search(r"\b" + re.escape(term.lower()) + r"\b", lower):
                leaked.append(term)
    return leaked


# ---------------------------------------------------------------------------
# Sonnet calls
# ---------------------------------------------------------------------------

def _call_sonnet(client, model: str, prompt: str, max_tokens: int):
    """Single-message, non-streaming call. Returns (text, input_tokens, output_tokens)."""
    response = client.messages.create(
        model=model,
        max_tokens=max_tokens,
        messages=[{"role": "user", "content": prompt}],
    )
    text = "".join(block.text for block in response.content if block.type == "text")
    return text, response.usage.input_tokens, response.usage.output_tokens


def _extract_json_array(text: str) -> list:
    """Defensively extract a JSON array from a Sonnet response that may have
    prose wrapped around it. Never regex-matches content — only locates the
    array boundaries, then hands the substring to json.loads."""
    text = text.strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    match = re.search(r"\[.*\]", text, re.DOTALL)
    if not match:
        raise ValueError(f"no JSON array found in Sonnet response: {text[:200]!r}")
    return json.loads(match.group(0))


_WORKED_EXAMPLE = (
    '{"prompt_text": "What is the rule about how a file\'s header row should '
    'relate to its data rows?", "criteria": ["mentions the header must mirror '
    'data row structure", "mentions consequences for downstream consumers"], '
    '"hop_distance": 0}'
)


def _build_generation_prompt(cluster_content: str, targets: list, subject_constraint: Optional[str]) -> str:
    target_lines = "\n".join(
        f"{i + 1}. hop_distance={t['hop_distance']} — steer this prompt's vocabulary toward: {t['target_text']!r}"
        for i, t in enumerate(targets)
    )
    subject_line = f"\nFocus the prompts on this subject if possible: {subject_constraint}\n" if subject_constraint else ""
    return (
        "You are building a bulk-interaction prompt bank for a memory-retrieval experiment.\n\n"
        "Below is the full content of one conceptual cluster from the experiment's seed graph:\n"
        "---\n"
        f"{cluster_content}\n"
        "---\n"
        f"{subject_line}\n"
        f"Generate exactly {len(targets)} prompts. Each prompt must be a question or task a user "
        "would naturally ask an assistant that already knows the cluster content above — the "
        "prompt itself must NOT quote or repeat the cluster's distinctive vocabulary verbatim; it "
        "should only be answerable using that content, testing whether the assistant retrieves it.\n\n"
        "For each prompt, also produce 1-3 binary-checkable criteria a grader could use to verify "
        "the response is correct, and echo back the hop_distance you were given.\n\n"
        f"Worked example of a well-formed item:\n{_WORKED_EXAMPLE}\n\n"
        f"Targets:\n{target_lines}\n\n"
        f"Return ONLY a JSON array of exactly {len(targets)} objects, each with keys "
        '"prompt_text", "criteria" (list of 1-3 strings), "hop_distance" (integer). No prose, '
        "no markdown fences, just the raw JSON array."
    )


def _build_validation_prompt(cluster_content: str, prompt_text: str) -> str:
    return (
        "Cluster content:\n---\n"
        f"{cluster_content}\n"
        "---\n"
        f"Candidate prompt: {prompt_text!r}\n\n"
        "Is this prompt coherent and fully answerable using only the cluster content above? "
        "Answer with exactly one word: YES or NO."
    )


def generate_cluster_prompts(client, model: str, content: dict, targets: list, subject_constraint: Optional[str] = None) -> list:
    """One Sonnet call producing len(targets) prompt items. Raises ValueError/
    json.JSONDecodeError on malformed output — caller decides how to retry."""
    prompt = _build_generation_prompt(content["full_content"], targets, subject_constraint)
    text, _, _ = _call_sonnet(client, model, prompt, max_tokens=400 * max(1, len(targets)))
    items = _extract_json_array(text)
    if not isinstance(items, list):
        raise ValueError("Sonnet generation response JSON was not a list")
    return items


def generate_cluster_prompts_safely(
    client, model: str, cluster_id: str, content: dict, targets: list,
    subject_constraint: Optional[str], print_fn: Callable[[str], None],
) -> list:
    """
    One Sonnet call per cluster (happy path). On malformed JSON or a
    length mismatch, retry the same batch call once. If still bad, fall back
    to generating each target individually so one bad response never loses
    the whole cluster — per-prompt validation/retry handles the rest.
    """
    for attempt in range(2):
        try:
            items = generate_cluster_prompts(client, model, content, targets, subject_constraint)
            if len(items) == len(targets):
                return items
            print_fn(f"  {cluster_id}: expected {len(targets)} items, got {len(items)} (attempt {attempt + 1})")
        except (ValueError, json.JSONDecodeError) as exc:
            logger.error("prompt_gen: %s malformed Sonnet JSON (attempt %d): %s", cluster_id, attempt + 1, exc)
            print_fn(f"  {cluster_id}: malformed Sonnet JSON (attempt {attempt + 1}): {exc}")

    print_fn(f"  {cluster_id}: batch generation failed after retry — falling back to per-prompt generation")
    items = []
    for target in targets:
        try:
            single = generate_cluster_prompts(client, model, content, [target], subject_constraint)
            items.append(single[0] if single else {})
        except (ValueError, json.JSONDecodeError) as exc:
            logger.error("prompt_gen: %s per-prompt fallback also malformed: %s", cluster_id, exc)
            items.append({})
    return items


def _validate_one(client, model: str, content_full: str, prompt_text: str, cluster_id: str) -> list:
    """Return a list of failure reasons; empty list means the prompt passed."""
    if not prompt_text:
        return ["missing prompt_text from generation"]

    reasons = []
    leaked = fingerprint_scan(prompt_text, cluster_id)
    if leaked:
        reasons.append(f"fingerprint leak: {leaked}")
        return reasons  # skip the Sonnet call — already failed lexically

    verdict_text, _, _ = _call_sonnet(client, model, _build_validation_prompt(content_full, prompt_text), max_tokens=10)
    if "yes" not in verdict_text.strip().lower():
        reasons.append(f"coherence validation failed: {verdict_text.strip()!r}")
    return reasons


def _finalize_entry(
    client, model: str, cluster_id: str, content: dict, target: dict, item: dict,
    seed_fixture_version: str, generated_by: str, max_retries: int, prompt_id: str,
    print_fn: Callable[[str], None],
) -> PromptBankEntry:
    """Validate one generated item, retrying (regenerating) up to max_retries
    times on failure. Persistent failures are excluded with a reason —
    never silently dropped."""
    attempt_item = item or {}
    reasons: list = []

    for attempt in range(max_retries + 1):
        prompt_text = attempt_item.get("prompt_text", "") or ""
        criteria = attempt_item.get("criteria", []) or []
        reasons = []

        if not prompt_text:
            reasons.append("missing prompt_text from generation")
        else:
            reasons.extend(_validate_one(client, model, content["full_content"], prompt_text, cluster_id))
        if not (1 <= len(criteria) <= 3):
            reasons.append(f"criteria count out of range: {len(criteria)}")

        if not reasons:
            return PromptBankEntry(
                prompt_id=prompt_id,
                cluster_id=cluster_id,
                prompt_text=prompt_text,
                hop_distance=target["hop_distance"],
                criteria=criteria,
                seed_fixture_version=seed_fixture_version,
                generated_by=generated_by,
                validated=True,
            )

        if attempt < max_retries:
            print_fn(f"  {prompt_id}: validation failed ({reasons}) — retrying ({attempt + 1}/{max_retries})")
            try:
                raw = generate_cluster_prompts(client, model, content, [target])
                attempt_item = raw[0] if raw else {}
            except (ValueError, json.JSONDecodeError) as exc:
                logger.error("prompt_gen: %s retry generation malformed: %s", prompt_id, exc)
                attempt_item = {}

    logger.error("prompt_gen: %s excluded after %d attempts: %s", prompt_id, max_retries + 1, reasons)
    return PromptBankEntry(
        prompt_id=prompt_id,
        cluster_id=cluster_id,
        prompt_text=(attempt_item.get("prompt_text") or "[generation failed]"),
        hop_distance=target["hop_distance"],
        criteria=(attempt_item.get("criteria") or ["placeholder"])[:3] or ["placeholder"],
        seed_fixture_version=seed_fixture_version,
        generated_by=generated_by,
        validated=False,
        exclusion_reason="; ".join(str(r) for r in reasons) or "unknown validation failure",
    )


# ---------------------------------------------------------------------------
# Cost estimate + confirmation gate
# ---------------------------------------------------------------------------

def estimate_tokens(text: str) -> int:
    return max(1, len(text) // CHARS_PER_TOKEN_ESTIMATE)


def estimate_generation_cost(full_content_by_cluster: dict, counts: dict, price_in_per_mtok: float, price_out_per_mtok: float) -> dict:
    """
    Rough token/cost estimate before any API call. Generation: one call per
    cluster (input = cluster content + scaffolding, output = N prompts as
    JSON). Validation: one call per generated prompt (input = cluster
    content + prompt text, output = one word).
    """
    est_input_tokens = 0
    est_output_tokens = 0
    for cluster_id, count in counts.items():
        if count == 0:
            continue
        content_tokens = estimate_tokens(full_content_by_cluster[cluster_id])
        est_input_tokens += content_tokens + 300
        est_output_tokens += count * 120
        est_input_tokens += count * (content_tokens + 100)
        est_output_tokens += count * 5

    cost_in = est_input_tokens / 1_000_000 * price_in_per_mtok
    cost_out = est_output_tokens / 1_000_000 * price_out_per_mtok
    return {
        "estimated_input_tokens": est_input_tokens,
        "estimated_output_tokens": est_output_tokens,
        "estimated_cost_usd": round(cost_in + cost_out, 4),
        "total_prompts": sum(counts.values()),
    }


def confirm_cost(estimate: dict, input_fn: Callable[[str], str], print_fn: Callable[[str], None]) -> bool:
    print_fn(
        f"Estimated cost for generating {estimate['total_prompts']} prompts: "
        f"${estimate['estimated_cost_usd']:.4f} "
        f"(~{estimate['estimated_input_tokens']} input tokens, "
        f"~{estimate['estimated_output_tokens']} output tokens)"
    )
    answer = input_fn("Proceed with Sonnet API calls? [y/N]: ").strip().lower()
    return answer in ("y", "yes")


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------

def _generation_params(plan: RunPlan) -> dict:
    return {
        "total_prompts": plan.total_prompts,
        "probe_ratio": plan.probe_ratio,
        "hop_distance_levels": plan.difficulty.hop_distance_levels,
        "subjects": plan.conceptual_fields.subjects,
        "model": plan.prompt_gen.model,
    }


def build_bank(
    plan: RunPlan,
    fixture_path: Path,
    output_dir: Path,
    client,
    input_fn: Callable[[str], str] = input,
    print_fn: Callable[[str], None] = print,
) -> PromptBank:
    """
    Generate (or reuse) a prompt bank for `plan`. Writes nothing and makes no
    API calls if an identical-hash bank already exists, or if the user
    declines the cost-confirmation prompt.
    """
    fixture_manifest_hash = compute_fixture_manifest_hash(fixture_path)
    params = _generation_params(plan)
    content_hash = compute_bank_hash(fixture_manifest_hash, params)

    existing_path = bank_path_for_hash(content_hash, output_dir)
    if existing_path.exists():
        print_fn(f"Prompt bank already exists for hash={content_hash} — reusing (no API calls).")
        return load_bank(existing_path)

    cluster_content = read_cluster_content(fixture_path)
    n_bulk = round((plan.total_prompts or 0) * (1 - plan.probe_ratio))
    counts = distribute_counts(n_bulk)

    price_in, price_out = load_pricing()
    full_content_by_cluster = {c: cluster_content[c]["full_content"] for c in CLUSTERS}
    estimate = estimate_generation_cost(full_content_by_cluster, counts, price_in, price_out)

    if not confirm_cost(estimate, input_fn=input_fn, print_fn=print_fn):
        print_fn("Aborted — no API calls made, no charges, no files written.")
        raise GenerationAborted("user declined the cost-confirmation prompt")

    subject_constraint = None
    if isinstance(plan.conceptual_fields.subjects, list):
        subject_constraint = ", ".join(plan.conceptual_fields.subjects)

    generated_by = f"{plan.prompt_gen.model}@{datetime.now(timezone.utc).date().isoformat()}"

    all_entries: list = []
    per_cluster_counts = {}
    for cluster_id in CLUSTERS:
        count = counts[cluster_id]
        if count == 0:
            continue
        content = cluster_content[cluster_id]
        hop_sequence = distribute_hop_levels(count, plan.difficulty.hop_distance_levels)
        targets = [
            {
                "hop_distance": hop,
                "target_text": content["hub_text"] if hop == 0 else (content["spoke_text"] or content["hub_text"]),
            }
            for hop in hop_sequence
        ]

        print_fn(f"Generating {count} prompt(s) for {cluster_id}...")
        items = generate_cluster_prompts_safely(client, plan.prompt_gen.model, cluster_id, content, targets, subject_constraint, print_fn)

        entries = []
        for i, (target, item) in enumerate(zip(targets, items)):
            prompt_id = f"{cluster_id}_{i:04d}"
            entry = _finalize_entry(
                client, plan.prompt_gen.model, cluster_id, content, target, item,
                SEED_FIXTURE_VERSION, generated_by, plan.prompt_gen.max_retries, prompt_id, print_fn,
            )
            entries.append(entry)
        all_entries.extend(entries)
        per_cluster_counts[cluster_id] = {
            "generated": len(entries),
            "validated": sum(1 for e in entries if e.validated),
            "excluded": sum(1 for e in entries if not e.validated),
        }

    bank = PromptBank(entries=all_entries, generation_params=params, content_hash=content_hash)
    path = save_bank(bank, output_dir)
    print_fn(f"Prompt bank saved: {path}")
    print_fn(f"Per-cluster counts: {per_cluster_counts}")
    return bank


def generate_bank_for_plan(
    plan: RunPlan,
    fixture_path: Path,
    output_dir: Path,
    input_fn: Callable[[str], str] = input,
    print_fn: Callable[[str], None] = print,
) -> PromptBank:
    """CLI-facing entry point: checks the API key, constructs the Anthropic
    client, and builds (or reuses) the prompt bank for `plan`."""
    api_key = _get_api_key()
    client = anthropic.Anthropic(api_key=api_key)
    return build_bank(plan, fixture_path, output_dir, client, input_fn=input_fn, print_fn=print_fn)
