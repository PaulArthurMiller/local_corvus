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


def _mode1_extract(mus: list[dict], keywords: list[str]) -> list[str]:
    """
    Return keyword-adjacent sentences from each MU.

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
            chunks.append(" ".join(sentences[i] for i in sorted(keep)))
    return chunks


def _mode2_extract(mus: list[dict]) -> list[str]:
    """
    Return first + last sentence of each MU as a fallback.

    For single-sentence MUs the sentence appears once.
    """
    chunks: list[str] = []
    for mu in mus:
        sentences = _split_sentences(mu["raw_content"])
        if not sentences:
            continue
        if len(sentences) == 1:
            chunks.append(sentences[0])
        else:
            chunks.append(f"{sentences[0]} ... {sentences[-1]}")
    return chunks


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


async def extract(
    mus: list[dict],
    keywords: list[str],
    store=None,
    session_id: str = "",
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
            store=store,
            session_id=session_id,
            ollama_host=ollama_host,
            model_name=model_name,
        )

    return condensed, None


# ---------------------------------------------------------------------------
# Mode 3: Gemma summarization
# ---------------------------------------------------------------------------


async def _summarize(
    text: str,
    store,
    session_id: str,
    ollama_host: Optional[str],
    model_name: Optional[str],
) -> tuple[str, Optional[str]]:
    """
    Call Gemma to summarize `text`. Store result as a new system MU.

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

    # Store summary as a system MU so it is retrievable in future turns
    mu_id = str(uuid.uuid4())
    now = datetime.now(timezone.utc).isoformat()
    await store.create_mu(
        mu_id=mu_id,
        source_type="system",
        raw_content=summary,
        created_at=now,
    )
    logger.info("extractor: summary MU stored mu_id=%s", mu_id)
    return summary, mu_id
