"""
Core agent orchestrator — the run_interaction() pipeline.

Coordinates gate -> session -> retrieval -> extraction -> prompt ->
Ollama (with tool loop) -> analytics -> snapshot -> return.

Does not implement business logic; routes data between modules.
"""

import base64
import hashlib
import json
import logging
import os
import random
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
from gate.sanitize import AttachmentSanitizeResult, sanitize_attachment, sanitize_in, sanitize_out
from gate.um import create_um
from learning.edge_update import resolve_rng_seed, validate_edge_update_config
from memory.extractor import extract
from memory.retrieval import retrieve
from memory.snapshot import take_snapshot
from memory.store import store as graph_store
from tools.registry import ToolLoopError, ToolNotFoundError, ToolRegistry

logger = logging.getLogger(__name__)

_CONFIG_PATH = os.getenv(
    "CONFIG_PATH",
    os.path.join(os.path.dirname(__file__), "..", "..", "config.yaml"),
)


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

# Compute once at module load — stable hash of the full resolved config
_CONFIG_HASH: str = hashlib.sha256(
    json.dumps(_CONFIG, sort_keys=True).encode()
).hexdigest()

# Resolved at import time — changing config.yaml or env var requires a restart.
_env_use_session = os.getenv("USE_SESSION_CONTEXT", "").strip().lower()
USE_SESSION_CONTEXT: bool = (
    _env_use_session in ("1", "true", "yes") if _env_use_session
    else bool(_CONFIG.get("experiment", {}).get("use_session_context", False))
)
logger.info("orchestrator: USE_SESSION_CONTEXT=%s", USE_SESSION_CONTEXT)

# Experiment ID — identifies this experiment run for series/MU attribution.
# Resolved from env → config → auto-generated default. Logged at WARNING if auto-generated
# so the researcher knows to set it explicitly before an experiment run.
def _resolve_experiment_id() -> str:
    env_val = os.getenv("EXPERIMENT_ID", "").strip()
    if env_val:
        return env_val
    cfg_val = str(_CONFIG.get("experiment", {}).get("experiment_id", "")).strip()
    if cfg_val:
        return cfg_val
    from datetime import date
    condition = _CONFIG.get("experiment", {}).get("condition_label", "unknown")
    auto_id = f"{condition}-{date.today().isoformat()}"
    logger.warning(
        "orchestrator: EXPERIMENT_ID not set — auto-generated '%s'. "
        "Set EXPERIMENT_ID env var or experiment.experiment_id in config.yaml "
        "for reproducibility.",
        auto_id,
    )
    return auto_id

EXPERIMENT_ID: str = _resolve_experiment_id()
logger.info("orchestrator: EXPERIMENT_ID=%s", EXPERIMENT_ID)

# M20 Phase 1 — edge_update/decay config validation. Hard-fails at import
# time (before any run state exists) on invalid values or decay.enabled=true.
EDGE_UPDATE_CFG: dict = validate_edge_update_config(_CONFIG)

# RUN_CONDITION — which of the three harness conditions (learned /
# static_weight / random_weight) this backend process is currently serving.
# Distinct from CONDITION_LABEL/experiment.condition_label above (an older,
# separate axis used by the M9 probe framework and M14-16 reports in a
# different vocabulary) — this one governs effective_weight() dispatch
# (backend/learning/edge_update.py) exclusively. Defaults to "static_weight"
# (the pre-M20 scalar-weight behavior) so plain chat-UI/dev usage without
# the harness is unaffected; the harness sets RUN_CONDITION="learned"
# alongside EXPERIMENT_ID when it restarts the backend for that condition.
_VALID_RUN_CONDITIONS = ("learned", "static_weight", "random_weight")


def _resolve_run_condition() -> str:
    env_val = os.getenv("RUN_CONDITION", "").strip()
    if not env_val:
        return "static_weight"
    if env_val not in _VALID_RUN_CONDITIONS:
        logger.warning(
            "orchestrator: RUN_CONDITION=%r not in %s — defaulting to 'static_weight'",
            env_val, _VALID_RUN_CONDITIONS,
        )
        return "static_weight"
    return env_val


RUN_CONDITION: str = _resolve_run_condition()
logger.info("orchestrator: RUN_CONDITION=%s", RUN_CONDITION)

# Process-scoped seeded RNG for weight_mode: sample — one instance for the
# process lifetime (not reset per-turn) so Thompson draws legitimately vary
# turn-to-turn while remaining reproducible given the same EXPERIMENT_ID/seed.
EDGE_RNG_SEED: int = resolve_rng_seed(EDGE_UPDATE_CFG, EXPERIMENT_ID)
EDGE_RNG: random.Random = random.Random(EDGE_RNG_SEED)
logger.info("orchestrator: edge_update rng_seed=%s weight_mode=%s", EDGE_RNG_SEED, EDGE_UPDATE_CFG["weight_mode"])

# --- Failure detection constants ---
# finish_reason value Ollama returns when the model exhausted its token budget
_OLLAMA_FINISH_REASON_LENGTH: str = "length"

FAILURE_REASON_TOKEN_LIMIT: str = "token_limit_exhausted"
FAILURE_REASON_EMPTY_OUTPUT: str = "empty_output"

_MU_SOURCE_TYPE_FAILED_TOKEN_LIMIT: str = "agent_output_failed_token_limit"
_MU_SOURCE_TYPE_FAILED_EMPTY_OUTPUT: str = "agent_output_failed_empty_output"

FAILURE_UI_MESSAGES: dict[str, str] = {
    FAILURE_REASON_TOKEN_LIMIT: (
        "[The model hit its completion-token limit before producing any output. "
        "Response is empty. This turn has been flagged for review.]"
    ),
    FAILURE_REASON_EMPTY_OUTPUT: (
        "[The model returned an empty response unexpectedly "
        "(finish_reason was not 'length'). This turn has been flagged for review.]"
    ),
}

# Singleton registry — initialized once when the module loads.
_tool_registry = ToolRegistry(config=_CONFIG)


def _scrub_prompt_for_storage(messages: list[dict]) -> list[dict]:
    """
    Return a copy of the Ollama messages array with base64 image data replaced
    by a human-readable placeholder. Keeps the prompt inspectable without bloating
    the analytics table with MB of binary data.
    """
    scrubbed = []
    for msg in messages:
        content = msg.get("content")
        if isinstance(content, list):
            new_parts = []
            for part in content:
                if part.get("type") == "image_url":
                    url = (part.get("image_url") or {}).get("url", "")
                    # Extract mime type and approximate size from data URI
                    if url.startswith("data:"):
                        header, _, b64 = url.partition(",")
                        mime = header.replace("data:", "").replace(";base64", "")
                        size_kb = round(len(b64) * 3 / 4 / 1024, 1)
                        new_parts.append({
                            "type": "image_url",
                            "image_url": {"url": f"[image: {mime} {size_kb}KB]"},
                        })
                    else:
                        new_parts.append(part)
                else:
                    new_parts.append(part)
            scrubbed.append({**msg, "content": new_parts})
        else:
            scrubbed.append(msg)
    return scrubbed


def _prompt_mu_content(condensed_context: str, user_content: str) -> str:
    """
    Content stored on a turn's prompt MU — the model-facing prompt WITHOUT
    the injected memory context.

    MU content is retrievable: if the prompt MU stored the full assembled
    prompt, every retrieved MU's text (and the "--- Context from memory ---"
    markers) would be copied into a new graph node, and retrieving that node
    on a later turn would inject old context — markers and all — inside the
    new turn's context block, compounding each time. The assembled prompt is
    still fully audited in analytics.assembled_prompt_json, and which MUs
    contributed context is recorded structurally by the output MU's outgoing
    edges (see Step 9 in run_interaction) — the prompt MU is a routing dead
    end by design, not the traversal node context linkage lives on.

    The "[User message]" label is kept when context was injected so the
    stored content matches the user-message portion of the prompt that was
    actually sent (see Step 4 in run_interaction).
    """
    if condensed_context:
        return f"[User message]\n{user_content}"
    return user_content


@dataclass
class AttachmentInput:
    """Raw attachment data from the API layer, before sanitization."""
    filename: str
    mime_type: str
    data_b64: str


@dataclass
class InteractionResult:
    response: str
    series_id: str
    run_id: str
    input_mu_id: str
    output_mu_id: str
    prompt_mu_id: str
    activated_mu_ids: list[str] = field(default_factory=list)
    activated_edge_ids: list[str] = field(default_factory=list)
    tool_call_mu_ids: list[str] = field(default_factory=list)
    flags: list[str] = field(default_factory=list)
    tokens_in: Optional[int] = None
    tokens_out: Optional[int] = None
    failure_reason: Optional[str] = None


async def run_interaction(
    message: str,
    series_id: str,
    attachments: Optional[list[AttachmentInput]] = None,
) -> InteractionResult:
    """
    Run the full agent pipeline for one user turn.

    Pipeline:
      sanitize_in -> gate attachments -> create input MU -> retrieve -> extract ->
      build prompt -> create prompt MU + edges -> sanitize_out ->
      Ollama (with tool loop) -> sanitize_in -> create output MU ->
      capture_run -> take_snapshot -> return
    """
    start_time = time.monotonic()
    run_id = str(uuid.uuid4())
    all_flags: list[str] = []
    tool_call_mu_ids: list[str] = []

    # --- Step 1: Gate user input and attachments ---
    input_result = sanitize_in(message, boundary="user_input->agent")
    all_flags.extend(input_result.flags)

    # Gate each attachment; binary held in memory only — never written to DB or log
    accepted_attachments: list[AttachmentSanitizeResult] = []
    attachment_descriptions: list[str] = []
    for att in (attachments or []):
        att_result = sanitize_attachment(
            filename=att.filename,
            mime_type=att.mime_type,
            data_b64=att.data_b64,
        )
        all_flags.extend(att_result.flags)
        if att_result.flags:
            attachment_descriptions.append(
                f"[Rejected: {att.filename} {att.mime_type} — {att_result.flags}]"
            )
            logger.warning(
                "orchestrator: attachment rejected filename=%s flags=%s",
                att.filename, att_result.flags,
            )
        else:
            size_kb = round(att_result.size_bytes / 1024, 1)
            accepted_attachments.append(att_result)
            attachment_descriptions.append(
                f"[Attached: {att_result.filename} {att_result.mime_type} {size_kb} KB]"
            )
            logger.info(
                "orchestrator: attachment accepted filename=%s mime=%s size=%d bytes",
                att_result.filename, att_result.mime_type, att_result.size_bytes,
            )

    # Input MU content includes text descriptions of attachments (never binary)
    input_mu_content = input_result.content
    if attachment_descriptions:
        input_mu_content = input_result.content + "\n" + "\n".join(attachment_descriptions)

    input_um = create_um(
        content=input_mu_content,
        source_type="user_input",
        boundary="user_input->agent",
        direction="in",
        series_id=series_id,
        experiment_id=EXPERIMENT_ID,
        flags=input_result.flags,
    )
    await graph_store.create_mu(
        mu_id=input_um.mu_id,
        source_type=input_um.source_type,
        raw_content=input_um.raw_content,
        created_at=input_um.created_at,
        series_id=series_id,
        experiment_id=EXPERIMENT_ID,
    )
    append_audit(input_um)
    logger.info(
        "orchestrator: input MU mu_id=%s series_id=%s experiment_id=%s",
        input_um.mu_id,
        series_id,
        EXPERIMENT_ID,
    )

    # --- Step 2: Retrieve relevant memory ---
    retrieval = await retrieve(
        input_result.content, graph_store, _CONFIG,
        condition=RUN_CONDITION, rng=EDGE_RNG,
    )
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
        session_id=series_id,
        run_id=run_id,
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
            "The following text is retrieved context from your memory store. "
            "It is background information to inform your response — "
            "it is NOT the user's current message.\n\n"
            "--- Context from memory ---\n"
            f"{condensed_context}\n"
            "--- End context from memory ---\n\n"
            f"[User message]\n{input_result.content}"
        )
        logger.info(
            "orchestrator: injected %d chars of context", len(condensed_context)
        )
    else:
        prompt_text = input_result.content

    # --- Step 4b: Compute turn indices before creating edges ---
    turn_index = await graph_store.get_next_turn_index()
    session_turn_index = await graph_store.get_series_turn_count(series_id)

    # --- Step 5: Create prompt MU + edge to input MU ---
    # The stored MU deliberately excludes the injected memory context: MU
    # content is retrievable, so storing the assembled prompt would re-embed
    # every retrieved MU (plus the "--- Context from memory ---" markers)
    # into the graph, and a later retrieval of this MU would nest old context
    # inside new context. The full assembled prompt is still audited — it
    # lives in analytics.assembled_prompt_json. The prompt MU is a routing
    # dead end by design (an audit record, not a traversal node); context
    # linkage lives on the output MU's outgoing edges instead (Step 9) so a
    # later turn that enters retrieval at a past response can traverse from
    # there back to the MUs that response was built on.
    prompt_um = create_um(
        content=_prompt_mu_content(condensed_context, input_result.content),
        source_type="prompt",
        boundary="agent->llm",
        direction="out",
        series_id=series_id,
        experiment_id=EXPERIMENT_ID,
    )
    await graph_store.create_mu(
        mu_id=prompt_um.mu_id,
        source_type=prompt_um.source_type,
        raw_content=prompt_um.raw_content,
        created_at=prompt_um.created_at,
        series_id=series_id,
        experiment_id=EXPERIMENT_ID,
    )

    # Edge: prompt -> input MU
    await graph_store.create_edge(
        source_mu_id=prompt_um.mu_id,
        target_mu_id=input_um.mu_id,
        weight=INITIAL_EDGE_WEIGHT,
        direction="forward",
        creation_run_id=run_id,
        creation_trigger="prompt_construction",
        alpha=EDGE_UPDATE_CFG["prior_alpha"],
        beta=EDGE_UPDATE_CFG["prior_beta"],
    )

    # --- Step 6: Gate prompt before Ollama ---
    prompt_gate = sanitize_out(prompt_text, boundary="agent->llm")
    all_flags.extend(prompt_gate.flags)

    # --- Step 7: Build multi-turn messages array ---
    history = get_session_messages(series_id) if USE_SESSION_CONTEXT else []
    logger.info(
        "orchestrator: use_session_context=%s history_turns=%d",
        USE_SESSION_CONTEXT,
        len(history),
    )
    ollama_messages = _build_ollama_messages(
        condensed_context=condensed_context,
        history=history,
        user_content=input_result.content,
        sanitized_attachments=accepted_attachments or None,
    )

    # --- Step 8: Tool loop — call Ollama; dispatch tool calls if present ---
    tool_schemas = _tool_registry.get_all_schemas()
    llm_content, tool_call_mu_ids, tokens_in, tokens_out, llm_failure_reason = await _run_tool_loop(
        messages=ollama_messages,
        series_id=series_id,
        tool_schemas=tool_schemas,
        all_flags=all_flags,
    )

    # Substitute UI message for failure cases before any downstream processing.
    # History, gate, and MU creation all receive the substituted message so the
    # audit trail reflects exactly what the user saw.
    if llm_failure_reason is not None:
        llm_content = FAILURE_UI_MESSAGES[llm_failure_reason]
        logger.warning(
            "orchestrator: failure detected reason=%s — substituting UI message into response",
            llm_failure_reason,
        )

    # Append clean turn to session history (without memory context injection)
    append_session_message(series_id, {"role": "user", "content": input_result.content})
    append_session_message(series_id, {"role": "assistant", "content": llm_content})

    # --- Step 9: Gate LLM response (entering agent boundary) ---
    output_result = sanitize_in(llm_content, boundary="llm->agent")
    all_flags.extend(output_result.flags)

    # Source type encodes the failure cause so the MU is queryable by failure mode
    output_source_type = (
        _MU_SOURCE_TYPE_FAILED_TOKEN_LIMIT if llm_failure_reason == FAILURE_REASON_TOKEN_LIMIT
        else _MU_SOURCE_TYPE_FAILED_EMPTY_OUTPUT if llm_failure_reason == FAILURE_REASON_EMPTY_OUTPUT
        else "agent_output"
    )

    output_um = create_um(
        content=output_result.content,
        source_type=output_source_type,
        boundary="llm->agent",
        direction="in",
        series_id=series_id,
        experiment_id=EXPERIMENT_ID,
        flags=output_result.flags,
    )
    await graph_store.create_mu(
        mu_id=output_um.mu_id,
        source_type=output_um.source_type,
        raw_content=output_um.raw_content,
        created_at=output_um.created_at,
        series_id=series_id,
        experiment_id=EXPERIMENT_ID,
    )

    # Edge: input MU -> output MU
    await graph_store.create_edge(
        source_mu_id=input_um.mu_id,
        target_mu_id=output_um.mu_id,
        weight=INITIAL_EDGE_WEIGHT,
        direction="forward",
        creation_run_id=run_id,
        creation_trigger="prompt_construction",
        alpha=EDGE_UPDATE_CFG["prior_alpha"],
        beta=EDGE_UPDATE_CFG["prior_beta"],
    )

    # Edges: output MU -> each retrieved/contributing MU (incl. the Mode-3
    # summary MU, already folded into activated_mu_ids above). This is the
    # routing fix: a later turn that enters retrieval at this output MU (a
    # BM25 match on synthesized content) can now traverse one hop back to the
    # MUs this response was built on, instead of the output MU being a dead
    # end. Not part of activated_edge_ids — these are co_activation edges,
    # not K1->K2 traversal-path edges, and M20's credit assignment
    # (process_verdict_for_learning) reads traversal_path, not this field.
    for mu_id in activated_mu_ids:
        await graph_store.create_edge(
            source_mu_id=output_um.mu_id,
            target_mu_id=mu_id,
            weight=INITIAL_EDGE_WEIGHT,
            direction="forward",
            creation_run_id=run_id,
            creation_trigger="co_activation",
            alpha=EDGE_UPDATE_CFG["prior_alpha"],
            beta=EDGE_UPDATE_CFG["prior_beta"],
        )

    append_audit(output_um)

    # --- Step 10: Gate final response leaving the agent ---
    final_result = sanitize_out(output_result.content, boundary="agent->user")
    all_flags.extend(final_result.flags)

    latency_ms = int((time.monotonic() - start_time) * 1000)

    logger.info(
        "orchestrator: complete series_id=%s run_id=%s input=%s prompt=%s output=%s "
        "activated_mus=%d activated_edges=%d tool_calls=%d flags=%d latency_ms=%d",
        series_id,
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
    assembled_prompt_json = _scrub_prompt_for_storage(ollama_messages)

    run_record = RunRecord(
        run_id=run_id,
        series_id=series_id,
        experiment_id=EXPERIMENT_ID,
        input_mu_id=input_um.mu_id,
        output_mu_id=output_um.mu_id,
        tool_calls=tool_call_mu_ids,
        activated_mu_ids=activated_mu_ids,
        activated_edge_ids=activated_edge_ids,
        prompt_mu_id=prompt_um.mu_id,
        tokens_in=tokens_in,
        tokens_out=tokens_out,
        latency_ms=latency_ms,
        hyperparameters_snapshot=_HP,
        algorithm=str(_HP.get("algorithm", "simple_rl")),
        bm25_scores_per_mu=retrieval.bm25_scores_per_mu,
        entry_point_mu_ids=retrieval.entry_point_mu_ids,
        candidate_set_before_ranking=retrieval.candidate_set_before_ranking,
        traversal_path=retrieval.traversal_path,
        hop_depth_per_retrieved_mu=retrieval.hop_depth_per_retrieved_mu,
        edge_contributions=retrieval.edge_contributions,
        edge_weights_used=retrieval.edge_weights_used,
        assembled_prompt_json=assembled_prompt_json,
        config_hash=_CONFIG_HASH,
        full_config_snapshot=_CONFIG,
        condition_label=os.getenv("CONDITION_LABEL") or _CONFIG.get("experiment", {}).get("condition_label"),
        turn_index=turn_index,
        session_turn_index=session_turn_index,
        use_session_context=USE_SESSION_CONTEXT,
        failure_reason=llm_failure_reason,
    )
    await capture_run(run_record)

    # --- Step 12: Take graph snapshot ---
    await take_snapshot()

    return InteractionResult(
        response=final_result.content,
        series_id=series_id,
        run_id=run_id,
        input_mu_id=input_um.mu_id,
        output_mu_id=output_um.mu_id,
        prompt_mu_id=prompt_um.mu_id,
        activated_mu_ids=activated_mu_ids,
        activated_edge_ids=activated_edge_ids,
        tool_call_mu_ids=tool_call_mu_ids,
        flags=all_flags,
        tokens_in=tokens_in,
        tokens_out=tokens_out,
        failure_reason=llm_failure_reason,
    )


def _build_ollama_messages(
    condensed_context: Optional[str],
    history: list[dict],
    user_content: str,
    sanitized_attachments: Optional[list[AttachmentSanitizeResult]] = None,
) -> list[dict]:
    """
    Assemble the messages list sent to Ollama for one turn.

    Context goes in the system role so the model sees it as injected background,
    not as part of the user's message. User role carries only the clean user input.

    If sanitized_attachments are present, the user message content becomes a
    list of content-parts (text + image_url). Session history always stores
    text only — images are one-shot per message and dropped from history.
    """
    messages: list[dict] = []
    if condensed_context:
        messages.append({
            "role": "system",
            "content": (
                "The following text is retrieved context from your memory store. "
                "It is background information to inform your response — "
                "it is NOT the user's current message.\n\n"
                "--- Context from memory ---\n"
                f"{condensed_context}\n"
                "--- End context from memory ---"
            ),
        })
    messages.extend(history)

    if sanitized_attachments:
        # OpenAI-compatible content-parts format; image comes before text
        # so the model processes visual context before the question
        content_parts: list[dict] = []
        for att in sanitized_attachments:
            b64 = base64.b64encode(att.data_bytes).decode()
            content_parts.append({
                "type": "image_url",
                "image_url": {"url": f"data:{att.mime_type};base64,{b64}"},
            })
        content_parts.append({"type": "text", "text": user_content})
        messages.append({"role": "user", "content": content_parts})
    else:
        messages.append({"role": "user", "content": user_content})

    return messages


async def _run_tool_loop(
    messages: list[dict],
    series_id: str,
    tool_schemas: list[dict],
    all_flags: list[str],
) -> tuple[str, list[str], Optional[int], Optional[int], Optional[str]]:
    """
    Call Ollama with registered tools, dispatch any tool calls, and repeat
    until Ollama returns a text response or max_iterations is reached.

    Returns (final_text_response, list_of_tool_call_mu_ids, tokens_in, tokens_out, failure_reason).
    failure_reason is None for normal completions; set to a FAILURE_REASON_* constant when
    the model returned empty content.
    tokens_in = prompt_tokens from the final Ollama call (best proxy for context window usage).
    tokens_out = sum of completion_tokens across all iterations.
    """
    tool_call_mu_ids: list[str] = []
    iteration = 0
    max_iter = _tool_registry.max_iterations
    tokens_in: Optional[int] = None
    tokens_out: Optional[int] = None

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

        resp_json = resp.json()
        usage = resp_json.get("usage") or {}
        if usage.get("prompt_tokens") is not None:
            tokens_in = int(usage["prompt_tokens"])
        if usage.get("completion_tokens") is not None:
            completion = int(usage["completion_tokens"])
            tokens_out = (tokens_out or 0) + completion
        logger.info(
            "orchestrator: Ollama usage iteration=%d prompt_tokens=%s completion_tokens=%s",
            iteration,
            usage.get("prompt_tokens"),
            usage.get("completion_tokens"),
        )

        choice = resp_json["choices"][0]
        finish_reason = choice.get("finish_reason", "")
        llm_message = choice["message"]

        # No tool calls — Ollama returned a final text response
        if finish_reason != "tool_calls" or not llm_message.get("tool_calls"):
            content = llm_message.get("content") or ""
            # Detect empty-response failure conditions
            if content.strip() == "":
                if (finish_reason or "") == _OLLAMA_FINISH_REASON_LENGTH:
                    loop_failure_reason: Optional[str] = FAILURE_REASON_TOKEN_LIMIT
                    logger.warning(
                        "orchestrator: empty response finish_reason=length — %s",
                        FAILURE_REASON_TOKEN_LIMIT,
                    )
                else:
                    loop_failure_reason = FAILURE_REASON_EMPTY_OUTPUT
                    logger.warning(
                        "orchestrator: empty response finish_reason=%s — %s",
                        finish_reason,
                        FAILURE_REASON_EMPTY_OUTPUT,
                    )
            else:
                loop_failure_reason = None
                logger.info(
                    "orchestrator: Ollama final response len=%d finish_reason=%s",
                    len(content),
                    finish_reason,
                )
            return content, tool_call_mu_ids, tokens_in, tokens_out, loop_failure_reason

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
                series_id=series_id,
                experiment_id=EXPERIMENT_ID,
            )
            await graph_store.create_mu(
                mu_id=tc_um.mu_id,
                source_type=tc_um.source_type,
                raw_content=tc_um.raw_content,
                created_at=tc_um.created_at,
                series_id=series_id,
                experiment_id=EXPERIMENT_ID,
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
                series_id=series_id,
                experiment_id=EXPERIMENT_ID,
            )
            await graph_store.create_mu(
                mu_id=tr_um.mu_id,
                source_type=tr_um.source_type,
                raw_content=tr_um.raw_content,
                created_at=tr_um.created_at,
                series_id=series_id,
                experiment_id=EXPERIMENT_ID,
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
    # without a final text response.
    logger.warning(
        "orchestrator: tool loop hit max_iterations=%d without text response; "
        "returning empty string with failure_reason=%s",
        max_iter,
        FAILURE_REASON_EMPTY_OUTPUT,
    )
    return "", tool_call_mu_ids, tokens_in, tokens_out, FAILURE_REASON_EMPTY_OUTPUT
