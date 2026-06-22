# Progress

## Current state

Project: Local Corvus — local LLM learning research platform  
Phase: Building  
Current milestone: 8 — Core Instrumentation — COMPLETE  
Next step: Review PR for milestone-8-core-instrumentation. After merge, start Milestone 9 (Experiment Framework). All 169 unit tests pass.

## Session log

- 2026-06-14: Architecture document drafted — `.agents/local_corvus_architecture_v0.1.md`
- 2026-06-15: Build plan created — `BUILD-PLAN.md`, 4 milestones defined
- 2026-06-15: /build — Milestone 1 complete
  - Created: `config.yaml`, `backend/gate/`, `backend/memory/store.py`, `backend/core/session.py`, `backend/api/chat.py`
  - Updated: `backend/main.py`, `backend/requirements.txt`
  - Added: `tests/test_gate/`, `tests/test_memory/`, `pytest.ini`
  - Tests: 32 new tests, all passing
  - Branch: milestone-1-foundation
  - PR: (see GitHub)
  - Bug found and fixed: aiosqlite `async with await connection` pattern starts the background thread twice on Python 3.9 — must use `db = await connect(...)` then explicit `await db.close()` instead
- 2026-06-15: /build — Milestone 2 complete
  - Created: `backend/memory/retrieval.py`, `backend/memory/extractor.py`, `backend/core/orchestrator.py`
  - Updated: `backend/memory/store.py` (added get_all_edges()), `backend/api/chat.py` (delegates to orchestrator), `backend/requirements.txt` (rank-bm25)
  - Added: `tests/test_memory/test_retrieval.py`, `tests/test_memory/test_extractor.py`
  - Tests: 36 new tests, 68 total — all passing
  - Branch: milestone-2-memory-engine
  - PR: (see GitHub)
  - Decision: BM25 index rebuilt on every retrieval call (simple; corpus is small for local research)
  - Decision: Multi-hit threshold is 2+ keyword hits for K1 (exact word matching, no stemming)
- 2026-06-17: /build — Milestone 3 complete
  - Created: `backend/tools/base.py`, `backend/tools/file_tool.py`, `backend/tools/registry.py`
  - Created: `backend/analytics/run_log.py`, `backend/memory/snapshot.py`
  - Created: `backend/api/admin.py` (GET /admin/runs, /admin/graph, /admin/config)
  - Updated: `backend/memory/store.py` (analytics table, insert_run, query_runs, get_run, update_run_feedback)
  - Updated: `backend/core/orchestrator.py` (tool loop with UM gating, analytics capture, graph snapshot)
  - Updated: `backend/api/chat.py` (exposes run_id and tool_call_mu_ids)
  - Updated: `backend/main.py` (registers admin router)
  - Added: `tests/test_tools/test_registry.py`, `tests/test_tools/test_file_tool.py`, `tests/test_analytics/test_run_log.py`
  - Tests: 40 new tests, 108 total — all passing
  - Branch: milestone-3-tool-system
  - PR: (see GitHub after push)
  - Fix: Python 3.9 incompatibility — `X | None` union syntax must be `Optional[X]` throughout

## First experiment task suite

**Task type chosen:** File creation + recall task (local-only; no external APIs)

**Task definition:**  
1. Send message: `"Please write the fact 'The boiling point of water is 100 degrees Celsius at sea level.' to a file called science_facts.txt"`
2. In a new session or after several unrelated turns, send: `"What temperature does water boil at sea level? Check your files if needed."`
3. Success: agent uses file_read tool to retrieve the file and includes the correct fact in its response.

**Why this task:**  
- Tests the full tool loop: detect tool call → gate → dispatch file_write → gate → MU → (later) detect file_read call → gate → dispatch → gate → MU
- Recall is observable: the agent either reads and cites the fact or it doesn't
- Fully local — no external APIs, no rate limits, deterministic file content

**Baseline experiment:** Run the task suite N=10 times with `initial_edge_weight: 0.5` and no learning active (feedback signal always None). Record latency_ms and whether the recall succeeded in PROGRESS.md after the baseline run.

**Success metric:** Fraction of recall turns where the agent uses file_read and returns the correct temperature. Baseline is expected to be ~0.8 (tool call works every time Gemma decides to use it; the question is whether it decides to).

## What exists

- `backend/main.py` — FastAPI app with /chat, /health, /ollama-health, /admin/* routes
- `backend/api/chat.py` — POST /chat: full pipeline including tool loop, returns run_id
- `backend/api/admin.py` — GET /admin/runs, /admin/graph, /admin/config
- `backend/gate/um.py` — UniversalMessage schema + create_um() factory
- `backend/gate/sanitize.py` — sanitize_in() / sanitize_out() (pure, stateless)
- `backend/gate/audit.py` — append_audit() → /app/memory/audit.log (JSONL, append-only)
- `backend/memory/store.py` — GraphStore: async CRUD for memory_units, edges, analytics tables
- `backend/memory/retrieval.py` — BM25 retrieval + traversal funnel
- `backend/memory/extractor.py` — 3-mode extractor (keyword, fallback, Gemma summarize)
- `backend/memory/snapshot.py` — take_snapshot() → /app/memory/snapshots/*.json.gz
- `backend/core/session.py` — create_session() / get_session() (in-memory)
- `backend/core/orchestrator.py` — full pipeline: gate → retrieve → extract → prompt → tool loop → analytics → snapshot
- `backend/tools/base.py` — Tool ABC + ToolResult schema
- `backend/tools/file_tool.py` — FileTool: file_read + file_write in /app/memory/workspace/
- `backend/tools/registry.py` — ToolRegistry: config-driven dispatch, ToolNotFoundError, ToolLoopError
- `backend/analytics/run_log.py` — RunRecord schema + capture_run() + query_runs()
- `backend/Dockerfile` + `docker-compose.yml` — backend service, ./memory:/app/memory volume
- `config.yaml` — full hyperparameter registry (single source of truth)

## Decisions made

- **Single edge type:** weight + directionality only, no semantic labels
- **No embeddings in v1:** BM25 keyword retrieval only
- **No external API calls in v1:** all tools are local
- **Ollama runs natively on Windows**, not in Docker — app container reaches it at `host.docker.internal:11434`
- **aiosqlite** for async-safe SQLite access (stdlib `sqlite3` is not async-safe)
- **Admin UI is a separate React app** (port 3001), not routes in the chat UI
- **flags field is free strings**, not an enum — patterns are embedded in the flag text for debugging visibility
- **Connection-per-operation** pattern for aiosqlite (not pooling) — appropriate for local research volume and avoids thread lifecycle bugs
- **Analytics table lives in graph.db** alongside memory_units and edges — one database for all persistent state, accessed only through GraphStore
- **Tool schemas passed on every Ollama call** — Gemma decides whether to invoke a tool per turn; no pre-filtering
- **First experiment task:** file creation + recall (see experiment section above)

## Open questions

1. **Weight initialization value** — flat 0.5 vs. source-type-based prior? Keeping 0.5 for baseline; revisit after first learning run.
2. **Decay function shape** — linear / exponential / sigmoid? Decide before Phase 4 results are meaningful.
3. **RL reward distribution along activated edges** — Build Milestone 4 with uniform distribution as default; revisit after first learning run.

- 2026-06-17: /build — Milestone 4 complete
  - Created: `backend/learning/interface.py` (LearningAlgorithm ABC)
  - Created: `backend/learning/simple_rl.py` (uniform weight update RL)
  - Updated: `backend/api/chat.py` (POST /feedback endpoint with SimpleRL)
  - Created: `admin/` — React Admin UI (port 3001): RunLogTable, LearningCurveChart, MemoryInspector
  - Created: `frontend/` — React Chat UI (port 3000): ChatWindow, MessageBubble, FeedbackButtons
  - Updated: `docker-compose.yml` (adds frontend and admin services)
  - Added: `tests/test_learning/test_simple_rl.py` (11 tests)
  - Tests: 11 new tests, 119 total — all passing
  - Branch: milestone-4-learning-admin-ui
  - Decision: Uniform RL reward distribution (open question #3) — all activated edges receive same delta
  - Decision: MemoryInspector uses paginated table (not force-directed graph) for reliability at scale

- 2026-06-19: /smoketest — full Docker stack tested, 4 bugs found and fixed
  - Bugs found: 4
  - Bugs fixed: 4
  - Branch: fix/smoketest-2026-06-19
  - PR: (see GitHub)
  - Fix 1: Missing CORS middleware (critical — browser blocked all API calls from port 3000 to 8000)
  - Fix 2: OLLAMA_HOST env var passing empty string when not set in shell (bypassed Python fallback)
  - Fix 3: config.yaml not accessible in Docker — path ../../config.yaml resolves to /config.yaml in container (fixed in orchestrator.py, chat.py, gate/sanitize.py, tools/registry.py, memory/extractor.py, api/admin.py)
  - Fix 4: gate/sanitize.py missing `import os` needed for the CONFIG_PATH fix
  - Status: all 3 containers start cleanly; /chat → /feedback pipeline works end-to-end; memory retrieval active on second message; admin endpoints return correct data; 119 tests passing

- 2026-06-19: /build — Milestone 5 complete
  - Modified: `config.yaml` (added model.context_window_tokens: 8192)
  - Modified: `backend/core/orchestrator.py` (extract Ollama usage tokens; return tokens_in/tokens_out from _run_tool_loop; populate RunRecord and InteractionResult)
  - Modified: `backend/api/chat.py` (tokens_in/tokens_out added to ChatResponse)
  - Modified: `frontend/src/components/ChatWindow.jsx` (token bar, New Session button, /admin/config fetch for context window size)
  - Modified: `frontend/src/components/MessageBubble.jsx` (Copy button with Copied! confirmation)
  - Tests: 119 existing tests — all passing; no new backend tests (frontend-heavy milestone; manual verification required)
  - Branch: milestone-5-chat-ui-quality
  - Note: navigator.clipboard requires secure context (HTTPS or localhost) — safe in development, note for production

- 2026-06-20: /build — Milestone 6 complete
  - Modified: `backend/core/orchestrator.py` — three changes in run_interaction():
    1. Step 4: updated prompt_text (prompt MU content) label to match new system message format
    2. Step 7a: replaced bare `[Context from memory]` tag with explicit natural-language label in system role
    3. Step 7b: fixed user role message from `prompt_gate.content` (duplicated context) to `input_result.content` (clean user input only)
    4. Extracted `_build_ollama_messages()` pure helper for testability
  - Created: `tests/test_core/test_orchestrator.py` (11 tests covering message construction with/without context)
  - Tests: 11 new tests, 130 total — all passing
  - Branch: milestone-6-prompt-clarity

- 2026-06-21: /build — Milestone 7 complete
  - Modified: `config.yaml` (added `allowed_attachment_mimetypes`, `max_attachment_size_bytes: 5242880`, `max_attachments_per_message: 4` under `gate:`)
  - Modified: `backend/gate/sanitize.py` (`AttachmentSanitizeResult` Pydantic model, `sanitize_attachment()`, `_check_magic_bytes()` — MIME allowlist, base64 decode, size limit, magic bytes validation; flags: `attachment_mime_rejected`, `attachment_base64_invalid`, `attachment_size_exceeded`, `attachment_magic_mismatch`; on any failure returns `data_bytes=b""`)
  - Modified: `backend/api/chat.py` (`AttachmentRequest` model; `attachments` field on `ChatRequest`; passes `AttachmentInput` list to `run_interaction()`)
  - Modified: `backend/core/orchestrator.py` (`AttachmentInput` dataclass; `run_interaction()` Step 1 gates each attachment, builds text-only MU content with `[Attached: …]` descriptions; `_build_ollama_messages()` builds content-parts list with images before text per Google spec)
  - Modified: `frontend/src/components/ChatWindow.jsx` (paperclip button + hidden file input, FileReader pipeline with `URL.createObjectURL()` thumbnails revoked on send, attachment preview strip, send enabled for attachments-only, New Session revokes objectURLs)
  - Modified: `frontend/src/api.js` (`sendMessage` sends `attachments` array in JSON body, strips `preview_url`)
  - Created: `tests/smoke_test_multimodal.py` (smoke test — not part of pytest suite)
  - Added: 11 new tests in `tests/test_gate/test_sanitize.py` (`TestSanitizeAttachment` — synthetic pure-Python image bytes, all MIME types, all rejection modes)
  - Tests: 11 new, 141 total — all passing
  - Branch: milestone-7-multimodal
  - ⚠ Known limitation: Ollama 0.30.x on Windows has a regression (GitHub Issue #16532) where `gemma4:e4b` decodes image tokens but does not interpret image content. Root cause: PR #16031 (merged 2026-05-07). Implementation is correct per OpenAI-compatible spec. End-to-end manual test blocked until Ollama fixes the regression. Workaround: roll back to Ollama v0.24.0.
  - Key decisions:
    - Binary attachment data never written to SQLite, audit.log, or snapshots — only text descriptions stored in MUs
    - Images are one-shot per message — dropped from session history to avoid unbounded RAM growth
    - Images placed BEFORE text in content-parts array per Google's Gemma multimodal spec
    - WEBP magic check uses RIFF header (bytes 0-3) + WEBP subtype (bytes 8-11) — different from other formats
    - `AttachmentSanitizeResult` needs `model_config = {"arbitrary_types_allowed": True}` for `bytes` field in Pydantic BaseModel

- 2026-06-22: /build — Milestone 8 complete
  - Modified: `backend/memory/retrieval.py` (RetrievalResult gains 6 new fields: bm25_scores_per_mu, entry_point_mu_ids, candidate_set_before_ranking, traversal_path, hop_depth_per_retrieved_mu, edge_contributions — all populated from values already computed in retrieve())
  - Modified: `backend/analytics/run_log.py` (RunRecord gains retrieval decomposition fields, assembled_prompt_json, config_hash, full_config_snapshot, condition_label, turn_index, session_turn_index)
  - Created: `backend/analytics/feedback_events.py` (FeedbackEvent model + record_feedback_event(); one DB row per /feedback call)
  - Modified: `backend/memory/store.py` (edges table: creation_run_id + creation_trigger; analytics table: all new RunRecord columns; feedback_events table added; _migrate_db() for idempotent ALTER TABLE on existing DBs; new methods: get_next_turn_index, get_session_turn_count, insert_feedback_event, get_feedback_events_for_run, get_edge)
  - Modified: `backend/core/orchestrator.py` (wires new RunRecord fields; passes creation_run_id + creation_trigger to create_edge; adds _scrub_prompt_for_storage() helper)
  - Modified: `backend/api/chat.py` (FeedbackRequest gains verdict_basis + is_probe_turn; feedback endpoint creates FeedbackEvent; weight_updates enriched with source_mu_id, target_mu_id, feedback_event_id)
  - Modified: `config.yaml` (adds experiment.condition_label: "learning")
  - Tests: 28 new tests, 169 total — all passing
  - Branch: milestone-8-core-instrumentation
  - PR: (see GitHub)
  - Key decision: _scrub_prompt_for_storage() strips base64 image data by estimating decoded size from b64 string length (len * 3/4) — avoids re-decoding just for size reporting

## Upcoming milestones

1. ~~**Foundation** — UM Gate + Graph Store + Session Management~~ ✅ COMPLETE
2. ~~**Memory Engine** — BM25 Retrieval + Traversal Funnel + Analyzer/Extractor~~ ✅ COMPLETE
3. ~~**Tool System** — Universal Tool Interface + File Tool + Analytics + Snapshots~~ ✅ COMPLETE
4. ~~**Learning** — Simple RL + Feedback Capture + Admin UI + Chat UI~~ ✅ COMPLETE
5. ~~**Chat UI Quality** — Token counter, copy button, New Session button~~ ✅ COMPLETE
6. ~~**Prompt Clarity** — Separate memory context from user message in Ollama call~~ ✅ COMPLETE
7. ~~**Multimodal Attachment Support** — Image attachments through the UM gate; binary transient only~~ ✅ COMPLETE
8. ~~**Core Instrumentation** — Retrieval decomposition, turn indexing, edge lifecycle, feedback-event log~~ ✅ COMPLETE
