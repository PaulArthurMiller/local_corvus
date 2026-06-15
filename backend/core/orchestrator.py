"""
Core agent orchestrator — the run_interaction() pipeline.

Coordinates gate -> session -> retrieval -> extraction -> prompt ->
Ollama -> memory persistence -> audit log.

Does not implement business logic; routes data between modules.
Tool handling is added in Milestone 3.
"""

import logging
import os
from dataclasses import dataclass, field
from typing import Optional

import httpx
import yaml

from gate.audit import append_audit
from gate.sanitize import sanitize_in, sanitize_out
from gate.um import create_um
from memory.extractor import extract
from memory.retrieval import retrieve
from memory.store import store as graph_store

logger = logging.getLogger(__name__)

_CONFIG_PATH = os.path.join(os.path.dirname(__file__), "..", "..", "config.yaml")


def _load_config() -> dict:
    try:
        with open(_CONFIG_PATH, "r", encoding="utf-8") as f:
            return yaml.safe_load(f)
    except Exception as exc:
        logger.warning("orchestrator: could not load config.yaml: %s", exc)
        return {}


_CONFIG = _load_config()
_MODEL_CFG = _CONFIG.get("model", {})
_HP = _CONFIG.get("hyperparameters", {})

OLLAMA_HOST: str = os.getenv(
    "OLLAMA_HOST",
    _MODEL_CFG.get("ollama_host", "http://host.docker.internal:11434"),
)
MODEL_NAME: str = _MODEL_CFG.get("name", "gemma4:e4b")
OLLAMA_TIMEOUT: float = float(_MODEL_CFG.get("timeout_seconds", 60))
INITIAL_EDGE_WEIGHT: float = float(_HP.get("initial_edge_weight", 0.5))


@dataclass
class InteractionResult:
    response: str
    session_id: str
    input_mu_id: str
    output_mu_id: str
    prompt_mu_id: str
    activated_mu_ids: list[str] = field(default_factory=list)
    activated_edge_ids: list[str] = field(default_factory=list)
    flags: list[str] = field(default_factory=list)


async def run_interaction(message: str, session_id: str) -> InteractionResult:
    """
    Run the full agent pipeline for one user turn.

    Pipeline:
      sanitize_in -> create input MU -> retrieve -> extract ->
      build prompt -> create prompt MU + edges -> sanitize_out ->
      Ollama -> sanitize_in -> create output MU + edge -> audit -> return
    """
    all_flags: list[str] = []

    # --- Step 1: Gate user input ---
    input_result = sanitize_in(message, boundary="user_input->agent")
    all_flags.extend(input_result.flags)

    input_um = create_um(
        content=input_result.content,
        source_type="user_input",
        boundary="user_input->agent",
        direction="in",
        session_id=session_id,
        flags=input_result.flags,
    )
    await graph_store.create_mu(
        mu_id=input_um.mu_id,
        source_type=input_um.source_type,
        raw_content=input_um.raw_content,
        created_at=input_um.created_at,
    )
    append_audit(input_um)
    logger.info(
        "orchestrator: input MU mu_id=%s session_id=%s",
        input_um.mu_id,
        session_id,
    )

    # --- Step 2: Retrieve relevant memory ---
    retrieval = await retrieve(input_result.content, graph_store, _CONFIG)
    logger.info(
        "orchestrator: retrieved %d MUs keywords=%s",
        len(retrieval.mus),
        retrieval.keywords,
    )

    # --- Step 3: Extract condensed context ---
    condensed_context, summary_mu_id = await extract(
        mus=retrieval.mus,
        keywords=retrieval.keywords,
        store=graph_store,
        session_id=session_id,
        ollama_host=OLLAMA_HOST,
        model_name=MODEL_NAME,
    )

    activated_mu_ids = list(retrieval.activated_mu_ids)
    activated_edge_ids = list(retrieval.activated_edge_ids)
    if summary_mu_id:
        activated_mu_ids.append(summary_mu_id)

    # --- Step 4: Build prompt with injected context ---
    if condensed_context:
        prompt_text = (
            f"[Context from memory]\n{condensed_context}\n\n"
            f"[User message]\n{input_result.content}"
        )
        logger.info(
            "orchestrator: injected %d chars of context", len(condensed_context)
        )
    else:
        prompt_text = input_result.content

    # --- Step 5: Create prompt MU + edges to contributing MUs ---
    prompt_um = create_um(
        content=prompt_text,
        source_type="prompt",
        boundary="agent->llm",
        direction="out",
        session_id=session_id,
    )
    await graph_store.create_mu(
        mu_id=prompt_um.mu_id,
        source_type=prompt_um.source_type,
        raw_content=prompt_um.raw_content,
        created_at=prompt_um.created_at,
    )

    # Edge: prompt -> input MU
    await graph_store.create_edge(
        source_mu_id=prompt_um.mu_id,
        target_mu_id=input_um.mu_id,
        weight=INITIAL_EDGE_WEIGHT,
        direction="forward",
    )
    # Edges: prompt -> each retrieved contributing MU
    for mu_id in retrieval.activated_mu_ids:
        await graph_store.create_edge(
            source_mu_id=prompt_um.mu_id,
            target_mu_id=mu_id,
            weight=INITIAL_EDGE_WEIGHT,
            direction="forward",
        )

    # --- Step 6: Gate prompt before Ollama ---
    prompt_gate = sanitize_out(prompt_text, boundary="agent->llm")
    all_flags.extend(prompt_gate.flags)

    # --- Step 7: Call Ollama ---
    try:
        async with httpx.AsyncClient() as client:
            resp = await client.post(
                f"{OLLAMA_HOST}/v1/chat/completions",
                json={
                    "model": MODEL_NAME,
                    "messages": [{"role": "user", "content": prompt_gate.content}],
                    "stream": False,
                },
                timeout=OLLAMA_TIMEOUT,
            )
            resp.raise_for_status()
        llm_content: str = resp.json()["choices"][0]["message"]["content"]
        logger.info(
            "orchestrator: Ollama response len=%d session_id=%s",
            len(llm_content),
            session_id,
        )
    except httpx.HTTPStatusError as exc:
        logger.error(
            "orchestrator: Ollama HTTP %s — %s", exc.response.status_code, exc
        )
        raise
    except httpx.RequestError as exc:
        logger.error("orchestrator: Ollama connection error — %s", exc)
        raise

    # --- Step 8: Gate LLM response (entering agent boundary) ---
    output_result = sanitize_in(llm_content, boundary="llm->agent")
    all_flags.extend(output_result.flags)

    output_um = create_um(
        content=output_result.content,
        source_type="agent_output",
        boundary="llm->agent",
        direction="in",
        session_id=session_id,
        flags=output_result.flags,
    )
    await graph_store.create_mu(
        mu_id=output_um.mu_id,
        source_type=output_um.source_type,
        raw_content=output_um.raw_content,
        created_at=output_um.created_at,
    )

    # Edge: input MU -> output MU (connects user turn to agent response)
    await graph_store.create_edge(
        source_mu_id=input_um.mu_id,
        target_mu_id=output_um.mu_id,
        weight=INITIAL_EDGE_WEIGHT,
        direction="forward",
    )

    append_audit(output_um)

    # --- Step 9: Gate final response leaving the agent ---
    final_result = sanitize_out(output_result.content, boundary="agent->user")
    all_flags.extend(final_result.flags)

    logger.info(
        "orchestrator: complete session_id=%s input=%s prompt=%s output=%s "
        "activated_mus=%d activated_edges=%d flags=%d",
        session_id,
        input_um.mu_id,
        prompt_um.mu_id,
        output_um.mu_id,
        len(activated_mu_ids),
        len(activated_edge_ids),
        len(all_flags),
    )

    return InteractionResult(
        response=final_result.content,
        session_id=session_id,
        input_mu_id=input_um.mu_id,
        output_mu_id=output_um.mu_id,
        prompt_mu_id=prompt_um.mu_id,
        activated_mu_ids=activated_mu_ids,
        activated_edge_ids=activated_edge_ids,
        flags=all_flags,
    )
