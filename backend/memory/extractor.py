"""
Analyzer/Extractor — three-mode content condenser.

Given retrieved MUs and query keywords, produces a condensed context string
that fits within the configured token budget.

Mode 1 (keyword-adjacent):
  Keep sentences containing a keyword plus the immediately adjacent sentence
  before and after. Produces the most targeted context.

Mode 2 (first/last fallback):
  Used when Mode 1 yields fewer than `extractor_min_sentences`. Returns
  the first and last sentence of each MU.

Mode 3 (summarization trigger):
  If the condensed result exceeds `summarization_trigger_tokens` estimated
  tokens, call Gemma via Ollama to summarize and store the result as a
  new system MU (so it becomes retrievable in future turns).

Token budget is estimated at 4 chars/token — rough but consistent for English.
"""

import logging
import os
import re
import uuid
from datetime import datetime, timezone
from typing import Optional

import httpx
import yaml

logger = logging.getLogger(__name__)

_CONFIG_PATH = os.getenv(
    "CONFIG_PATH",
    os.path.join(os.path.dirname(__file__), "..", "..", "config.yaml"),
)
_CHARS_PER_TOKEN = 4

_SENTENCE_END_RE = re.compile(r"(?<=[.!?])\s+")


def _load_config() -> dict:
    try:
        with open(_CONFIG_PATH, "r", encoding="utf-8") as f:
            return yaml.safe_load(f)
    except Exception as exc:
        logger.warning("extractor: could not load config.yaml: %s", exc)
        return {}


_CONFIG = _load_config()
_MODEL_CFG = _CONFIG.get("model", {})
_HP = _CONFIG.get("hyperparameters", {})

# Seed MUs (both seed_v2.json.gz and the seed_v2_draft.json fixture) are not
# distinguishable from real interaction MUs by source_type — every seed MU is
# stamped source_type="agent_output", identical to a genuine turn's response.
# series_id (prefix "seed:") is the only field that actually distinguishes
# seed-origin content — the same mechanism series_report.py §5.3 already uses.
# This prefix is a stated requirement for the seed v3 generator (see
# seed_v3_mu_spec.md) precisely because this label derivation depends on it.
_SEED_SERIES_PREFIX = "seed:"

# Bracketed source-kind labels prefixed onto each condensed chunk so mixed
# seed/interaction content stays legible to Gemma. Failed-output source types
# (agent_output_failed_token_limit/agent_output_failed_empty_output) have no
# entry here by design — they're excluded from retrieval candidacy entirely
# (see config.yaml: retrieval_exclude_source_types) rather than labeled, so
# they should never reach this function.
_LABEL_SEED_REFERENCE = "[memory — reference]"
_LABEL_PRIOR_USER_MESSAGE = "[memory — prior user message]"
_LABEL_PRIOR_RESPONSE = "[memory — prior response]"
_LABEL_SYNTHESIZED_SUMMARY = "[memory — synthesized summary]"
_LABEL_TOOL_RECORD = "[memory — tool record]"
_LABEL_OTHER = "[memory — other]"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _split_sentences(text: str) -> list[str]:
    """Split text on sentence-ending punctuation."""
    parts = _SENTENCE_END_RE.split(text.strip())
    return [s.strip() for s in parts if s.strip()]


def _contains_keyword(sentence: str, keywords: list[str]) -> bool:
    lower = sentence.lower()
    return any(kw.lower() in lower for kw in keywords)


def _source_kind_label(mu: dict) -> str:
    """
    Bracketed label describing an MU's origin, prefixed onto its condensed
    chunk. series_id is checked before source_type — it's the only field
    that distinguishes seed-origin content (see _SEED_SERIES_PREFIX above).
    """
    series_id = mu.get("series_id") or ""
    if series_id.startswith(_SEED_SERIES_PREFIX):
        return _LABEL_SEED_REFERENCE

    source_type = mu.get("source_type")
    if source_type == "user_input":
        return _LABEL_PRIOR_USER_MESSAGE
    if source_type == "agent_output":
        return _LABEL_PRIOR_RESPONSE
    if source_type == "system":
        return _LABEL_SYNTHESIZED_SUMMARY
    if source_type in ("tool_call", "tool_result"):
        return _LABEL_TOOL_RECORD
    return _LABEL_OTHER


def _mode1_extract(mus: list[dict], keywords: list[str]) -> list[str]:
    """
    Return keyword-adjacent sentences from each MU, prefixed with a
    source-kind label.

    Keeps any sentence containing a keyword, plus its ±1 neighbors.
    """
    chunks: list[str] = []
    for mu in mus:
        sentences = _split_sentences(mu["raw_content"])
        if not sentences:
            continue
        keep: set[int] = set()
        for i, sent in enumerate(sentences):
            if _contains_keyword(sent, keywords):
                keep.add(max(0, i - 1))
                keep.add(i)
                keep.add(min(len(sentences) - 1, i + 1))
        if keep:
            text = " ".join(sentences[i] for i in sorted(keep))
            chunks.append(f"{_source_kind_label(mu)}\n{text}")
    return chunks


def _mode2_extract(mus: list[dict]) -> list[str]:
    """
    Return first + last sentence of each MU as a fallback, prefixed with a
    source-kind label.

    For single-sentence MUs the sentence appears once.
    """
    chunks: list[str] = []
    for mu in mus:
        sentences = _split_sentences(mu["raw_content"])
        if not sentences:
            continue
        if len(sentences) == 1:
            text = sentences[0]
        else:
            text = f"{sentences[0]} ... {sentences[-1]}"
        chunks.append(f"{_source_kind_label(mu)}\n{text}")
    return chunks


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


async def extract(
    mus: list[dict],
    keywords: list[str],
    store=None,
    session_id: str = "",
    run_id: Optional[str] = None,
    ollama_host: Optional[str] = None,
    model_name: Optional[str] = None,
) -> tuple[str, Optional[str]]:
    """
    Condense retrieved MUs into a context string for prompt injection.

    Returns:
        (condensed_text, summary_mu_id | None)

    summary_mu_id is non-None only when Mode 3 triggered and a new system
    MU was written to the store. That MU will be retrievable in future turns.
    """
    if not mus:
        return "", None

    min_sentences: int = _HP.get("extractor_min_sentences", 2)
    trigger_tokens: int = _HP.get("summarization_trigger_tokens", 1500)

    # --- Mode 1 ---
    mode1_chunks = _mode1_extract(mus, keywords)
    mode1_sentence_count = sum(
        len(_split_sentences(chunk)) for chunk in mode1_chunks
    )

    if mode1_sentence_count >= min_sentences:
        condensed = "\n\n".join(mode1_chunks)
        logger.info(
            "extractor: Mode 1 — %d sentences from %d MUs",
            mode1_sentence_count,
            len(mode1_chunks),
        )
    else:
        # --- Mode 2 ---
        mode2_chunks = _mode2_extract(mus)
        condensed = "\n\n".join(mode2_chunks)
        logger.info(
            "extractor: Mode 1 yielded %d sentences (min=%d) — Mode 2 fallback (%d MUs)",
            mode1_sentence_count,
            min_sentences,
            len(mode2_chunks),
        )

    # --- Mode 3 ---
    estimated_tokens = len(condensed) // _CHARS_PER_TOKEN
    if estimated_tokens > trigger_tokens and store is not None:
        logger.info(
            "extractor: ~%d tokens > trigger=%d — Mode 3 summarization",
            estimated_tokens,
            trigger_tokens,
        )
        return await _summarize(
            condensed,
            mus=mus,
            store=store,
            session_id=session_id,
            run_id=run_id,
            ollama_host=ollama_host,
            model_name=model_name,
        )

    return condensed, None


# ---------------------------------------------------------------------------
# Mode 3: Gemma summarization
# ---------------------------------------------------------------------------


async def _summarize(
    text: str,
    mus: list[dict],
    store,
    session_id: str,
    run_id: Optional[str],
    ollama_host: Optional[str],
    model_name: Optional[str],
) -> tuple[str, Optional[str]]:
    """
    Call Gemma to summarize `text`. Store result as a new system MU.

    mus is the pre-summarization list the condensed text was built from —
    used to wire summary->source edges so the new MU isn't an edge-orphan
    (found only lexically via BM25, never by traversal; the same disease
    M22 cured on the output MU).

    Returns (summary_text, summary_mu_id).
    On Ollama failure, returns (original_text, None) as safe fallback.
    """
    host = ollama_host or os.getenv(
        "OLLAMA_HOST",
        _MODEL_CFG.get("ollama_host", "http://host.docker.internal:11434"),
    )
    model = model_name or _MODEL_CFG.get("name", "gemma4:e4b")
    timeout = float(_MODEL_CFG.get("timeout_seconds", 60))

    prompt = (
        "Summarize the following context into a compact paragraph that preserves "
        "the most important facts and relationships. Be concise.\n\n"
        f"{text}"
    )

    try:
        async with httpx.AsyncClient() as client:
            resp = await client.post(
                f"{host}/v1/chat/completions",
                json={
                    "model": model,
                    "messages": [{"role": "user", "content": prompt}],
                    "stream": False,
                },
                timeout=timeout,
            )
            resp.raise_for_status()
        summary = resp.json()["choices"][0]["message"]["content"]
        logger.info("extractor: summarization → %d chars", len(summary))
    except Exception as exc:
        logger.error("extractor: summarization failed — %s — using original text", exc)
        return text, None

    # Store summary as a system MU so it is retrievable in future turns.
    # Pre-existing gap, not fixed here (out of this milestone's scope): unlike
    # every other create_mu() call site, this one passes no series_id/
    # experiment_id, so the summary MU is stamped series_id=NULL.
    mu_id = str(uuid.uuid4())
    now = datetime.now(timezone.utc).isoformat()
    await store.create_mu(
        mu_id=mu_id,
        source_type="system",
        raw_content=summary,
        created_at=now,
    )
    logger.info("extractor: summary MU stored mu_id=%s", mu_id)

    # Edges: summary -> each source MU it was built from. Without these the
    # summary MU is an edge-orphan — reachable only by lexical BM25 match,
    # never by K2 traversal, so a later turn that enters retrieval at this
    # summary can't route back to the MUs it condensed. Same priors pattern
    # (edge_update.prior_alpha/prior_beta) used at every other edge-creation
    # call site; orchestrator.py already hard-fails startup on invalid
    # edge_update config, so no second validation pass is needed here.
    edge_update_cfg = _CONFIG.get("edge_update", {})
    prior_alpha = edge_update_cfg.get("prior_alpha", 1.0)
    prior_beta = edge_update_cfg.get("prior_beta", 1.0)
    initial_edge_weight = float(_HP.get("initial_edge_weight", 0.5))
    for mu in mus:
        await store.create_edge(
            source_mu_id=mu_id,
            target_mu_id=mu["mu_id"],
            weight=initial_edge_weight,
            direction="forward",
            creation_run_id=run_id,
            creation_trigger="co_activation",
            alpha=prior_alpha,
            beta=prior_beta,
        )
    logger.info(
        "extractor: created %d summary->source edges for mu_id=%s",
        len(mus), mu_id,
    )

    return summary, mu_id
