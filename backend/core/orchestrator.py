"""
Core agent orchestrator — the run_interaction() pipeline.

Coordinates gate -> session -> retrieval -> extraction -> prompt ->
Ollama (with tool loop) -> analytics -> snapshot -> return.

Does not implement business logic; routes data between modules.
"""

import json
import logging
import os
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional

import httpx
import yaml

from analytics.run_log import RunRecord, capture_run
from core.session import append_session_message, get_session_messages
from gate.audit import append_audit
from gate.sanitize import sanitize_in, sanitize_out
from gate.um import create_um
from memory.extractor import extract
from memory.retrieval import retrieve
from memory.snapshot import take_snapshot
from memory.store import store as graph_store
from tools.registry import ToolLoopError, ToolNotFoundError, ToolRegistry

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

# Singleton registry — initialized once when the module loads.
_tool_registry = ToolRegistry(config=_CONFIG)


@dataclass
class InteractionResult:
    response: str
    session_id: str
    run_id: str
    input_mu_id: str
    output_mu_id: str
    prompt_mu_id: str
    activated_mu_ids: list[str] = field(default_factory=list)
    activated_edge_ids: list[str] = field(default_factory=list)
    tool_call_mu_ids: list[str] = field(default_factory=list)
    flags: list[str] = field(default_factory=list)


async def run_interaction(message: str, session_id: str) -> InteractionResult:
    """
    Run the full agent pipeline for one user turn.

    Pipeline:
      sanitize_in -> create input MU -> retrieve -> extract ->
      build prompt -> create prompt MU + edges -> sanitize_out ->
      Ollama (with tool loop) -> sanitize_in -> create output MU ->
      capture_run -> take_snapshot -> return
    """
    start_time = time.monotonic()
    run_id = str(uuid.uuid4())
    all_flags: list[str] = []
    tool_call_mu_ids: list[str] = []

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

    # --- Step 7: Build multi-turn messages array ---
    ollama_messages: list[dict] = []
    if condensed_context:
        ollama_messages.append({
            "role": "system",
            "content": f"[Context from memory]\n{condensed_context}",
        })
    history = get_session_messages(session_id)
    ollama_messages.extend(history)
    ollama_messages.append({"role": "user", "content": prompt_gate.content})

    # --- Step 8: Tool loop — call Ollama; dispatch tool calls if present ---
    tool_schemas = _tool_registry.get_all_schemas()
    llm_content, tool_call_mu_ids = await _run_tool_loop(
        messages=ollama_messages,
        session_id=session_id,
        tool_schemas=tool_schemas,
        all_flags=all_flags,
    )

    # Append clean turn to session history (without memory context injection)
    append_session_message(session_id, {"role": "user", "content": input_result.content})
    append_session_message(session_id, {"role": "assistant", "content": llm_content})

    # --- Step 9: Gate LLM response (entering agent boundary) ---
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

    # Edge: input MU -> output MU
    await graph_store.create_edge(
        source_mu_id=input_um.mu_id,
        target_mu_id=output_um.mu_id,
        weight=INITIAL_EDGE_WEIGHT,
        direction="forward",
    )

    append_audit(output_um)

    # --- Step 10: Gate final response leaving the agent ---
    final_result = sanitize_out(output_result.content, boundary="agent->user")
    all_flags.extend(final_result.flags)

    latency_ms = int((time.monotonic() - start_time) * 1000)

    logger.info(
        "orchestrator: complete session_id=%s run_id=%s input=%s prompt=%s output=%s "
        "activated_mus=%d activated_edges=%d tool_calls=%d flags=%d latency_ms=%d",
        session_id,
        run_id,
        input_um.mu_id,
        prompt_um.mu_id,
        output_um.mu_id,
        len(activated_mu_ids),
        len(activated_edge_ids),
        len(tool_call_mu_ids),
        len(all_flags),
        latency_ms,
    )

    # --- Step 11: Capture analytics run record ---
    run_record = RunRecord(
        run_id=run_id,
        session_id=session_id,
        input_mu_id=input_um.mu_id,
        output_mu_id=output_um.mu_id,
        tool_calls=tool_call_mu_ids,
        activated_mu_ids=activated_mu_ids,
        activated_edge_ids=activated_edge_ids,
        prompt_mu_id=prompt_um.mu_id,
        latency_ms=latency_ms,
        hyperparameters_snapshot=_HP,
        algorithm=str(_HP.get("algorithm", "simple_rl")),
    )
    await capture_run(run_record)

    # --- Step 12: Take graph snapshot ---
    await take_snapshot()

    return InteractionResult(
        response=final_result.content,
        session_id=session_id,
        run_id=run_id,
        input_mu_id=input_um.mu_id,
        output_mu_id=output_um.mu_id,
        prompt_mu_id=prompt_um.mu_id,
        activated_mu_ids=activated_mu_ids,
        activated_edge_ids=activated_edge_ids,
        tool_call_mu_ids=tool_call_mu_ids,
        flags=all_flags,
    )


async def _run_tool_loop(
    messages: list[dict],
    session_id: str,
    tool_schemas: list[dict],
    all_flags: list[str],
) -> tuple[str, list[str]]:
    """
    Call Ollama with registered tools, dispatch any tool calls, and repeat
    until Ollama returns a text response or max_iterations is reached.

    Returns (final_text_response, list_of_tool_call_mu_ids).
    """
    tool_call_mu_ids: list[str] = []
    iteration = 0
    max_iter = _tool_registry.max_iterations

    while iteration <= max_iter:
        logger.info(
            "orchestrator: Ollama call iteration=%d messages=%d tools=%d",
            iteration,
            len(messages),
            len(tool_schemas),
        )

        try:
            async with httpx.AsyncClient() as client:
                resp = await client.post(
                    f"{OLLAMA_HOST}/v1/chat/completions",
                    json={
                        "model": MODEL_NAME,
                        "messages": messages,
                        "tools": tool_schemas,
                        "stream": False,
                    },
                    timeout=OLLAMA_TIMEOUT,
                )
                resp.raise_for_status()
        except httpx.HTTPStatusError as exc:
            logger.error(
                "orchestrator: Ollama HTTP %s — %s", exc.response.status_code, exc
            )
            raise
        except httpx.RequestError as exc:
            logger.error("orchestrator: Ollama connection error — %s", exc)
            raise

        choice = resp.json()["choices"][0]
        finish_reason = choice.get("finish_reason", "")
        llm_message = choice["message"]

        # No tool calls — Ollama returned a final text response
        if finish_reason != "tool_calls" or not llm_message.get("tool_calls"):
            content = llm_message.get("content") or ""
            logger.info(
                "orchestrator: Ollama final response len=%d finish_reason=%s",
                len(content),
                finish_reason,
            )
            return content, tool_call_mu_ids

        # Process each tool call in this response
        tool_calls = llm_message["tool_calls"]
        logger.info(
            "orchestrator: Ollama returned %d tool call(s) at iteration=%d",
            len(tool_calls),
            iteration,
        )

        # Append the assistant's tool-calling message to the conversation
        messages.append(llm_message)

        for tc in tool_calls:
            call_id = tc.get("id", str(uuid.uuid4()))
            func = tc.get("function", {})
            tool_name = func.get("name", "")
            raw_args = func.get("arguments", "{}")

            # Parse arguments — Ollama sends them as a JSON string
            try:
                params = json.loads(raw_args) if isinstance(raw_args, str) else raw_args
            except (json.JSONDecodeError, TypeError):
                params = {}
                logger.warning(
                    "orchestrator: could not parse tool args for '%s': %s",
                    tool_name,
                    raw_args,
                )

            # Gate the outgoing tool call
            call_content = json.dumps({"tool": tool_name, "params": params})
            call_gate = sanitize_out(call_content, boundary="agent->tool")
            all_flags.extend(call_gate.flags)

            # Create tool_call MU
            tc_um = create_um(
                content=call_content,
                source_type="tool_call",
                boundary="agent->tool",
                direction="out",
                session_id=session_id,
            )
            await graph_store.create_mu(
                mu_id=tc_um.mu_id,
                source_type=tc_um.source_type,
                raw_content=tc_um.raw_content,
                created_at=tc_um.created_at,
            )
            tool_call_mu_ids.append(tc_um.mu_id)
            append_audit(tc_um)
            logger.info(
                "orchestrator: tool_call MU mu_id=%s tool='%s'",
                tc_um.mu_id,
                tool_name,
            )

            # Dispatch to tool registry
            try:
                tool_result = await _tool_registry.dispatch(
                    tool_name=tool_name,
                    params=params,
                    iteration=iteration,
                )
                result_content = tool_result.output or tool_result.error or ""
            except ToolNotFoundError as exc:
                result_content = f"error: {exc}"
                logger.error("orchestrator: tool not found — %s", exc)
            except ToolLoopError as exc:
                result_content = f"error: {exc}"
                logger.error("orchestrator: tool loop limit — %s", exc)

            # Gate the incoming tool result
            result_gate = sanitize_in(result_content, boundary="tool->agent")
            all_flags.extend(result_gate.flags)

            # Create tool_result MU
            tr_um = create_um(
                content=result_gate.content,
                source_type="tool_result",
                boundary="tool->agent",
                direction="in",
                session_id=session_id,
            )
            await graph_store.create_mu(
                mu_id=tr_um.mu_id,
                source_type=tr_um.source_type,
                raw_content=tr_um.raw_content,
                created_at=tr_um.created_at,
            )
            tool_call_mu_ids.append(tr_um.mu_id)
            append_audit(tr_um)
            logger.info(
                "orchestrator: tool_result MU mu_id=%s tool='%s' len=%d",
                tr_um.mu_id,
                tool_name,
                len(result_gate.content),
            )

            # Append tool result to conversation in OpenAI tool-result format
            messages.append({
                "role": "tool",
                "tool_call_id": call_id,
                "content": result_gate.content,
            })

        iteration += 1

    # If we exit the while loop without returning, the loop hit max iterations
    # without a final text response. Return whatever we have.
    logger.warning(
        "orchestrator: tool loop hit max_iterations=%d without text response; "
        "returning empty string",
        max_iter,
    )
    return "", tool_call_mu_ids
