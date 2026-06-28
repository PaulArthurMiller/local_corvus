# Progress

## Current state

Project: Local Corvus — local LLM learning research platform  
Phase: Active — M18 planned, not started  
Current milestone: 18 — Seed Fixture v2 + Probe Set v2 (PLANNED — see BUILD-PLAN.md)  
Next step: Build M18 Phase 0 infrastructure fixes, then generate seed_v2 fixture (34 MUs, 32 edges) and probe_set_v2.yaml (10 probes) to enable the real three-condition experiment.

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

- 2026-06-22: /build — Milestone 9 complete
  - Created: `probes/probe_set_v1.yaml` (template: 5 probes — 3 phenomena, 2 bridge pairs)
  - Created: `backend/analytics/probes.py` (ProbeDefinition, ProbeResult, load_probe_set, compute_probe_result, record_probe_result)
  - Created: `backend/analytics/seed_fixture.py` (SeedFixtureManifest, compute_fixture_hash, register_fixture, load_and_verify_fixture)
  - Modified: `backend/memory/store.py` (3 new tables: probes, probe_results, seed_fixture_manifest; new methods: sync_probes, get_probe, insert_probe_result, query_probe_results, insert_seed_fixture_manifest, get_seed_fixture_manifest; migration for measurement_session_index + is_probe_turn)
  - Modified: `backend/memory/snapshot.py` (take_battery_snapshot, take_pre_pruning_snapshot)
  - Modified: `backend/analytics/run_log.py` (measurement_session_index + is_probe_turn added to RunRecord)
  - Modified: `backend/core/orchestrator.py` (CONDITION_LABEL env var overrides config.yaml condition_label)
  - Modified: `backend/api/admin.py` (POST /admin/run-probe, GET /admin/probe-results)
  - Modified: `backend/main.py` (startup probe sync)
  - Created: `admin/src/components/ProbeResults.jsx` (filter controls, per-phenomenon rank table, learning curves, bridge probe toggle)
  - Modified: `admin/src/App.jsx` (Probe Results tab)
  - Modified: `docker-compose.yml` (probes + seed_fixtures mounts, CONDITION_LABEL passthrough)
  - Added: 24 new tests (test_probes.py, test_seed_fixture.py, test_snapshot.py)
  - Tests: 24 new, 193 total — all passing
  - Branch: milestone-9-experiment-framework
  - Key decisions:
    - Probe content (prompt_text, gold_mu_ids) ships as template — filled in by experimenter after seed fixture generation
    - POST /admin/run-probe is strictly read-only: only calls retrieve(), no MU creation, no feedback events, run_id always None
    - CONDITION_LABEL env var takes priority over config.yaml (empty string falls through to config)
    - seed_fixtures directory mounted as Docker volume so fixtures are accessible without rebuilding the image

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

- 2026-06-26: /build — Milestone 10 complete
  - Modified: `config.yaml` (adds experiment.use_session_context: false)
  - Modified: `backend/core/orchestrator.py` (USE_SESSION_CONTEXT module-level constant resolved from env or config; conditional history injection in Step 7 with INFO log; use_session_context added to RunRecord)
  - Modified: `backend/analytics/run_log.py` (use_session_context: bool = False added to RunRecord)
  - Modified: `backend/memory/store.py` (use_session_context column in analytics table; migration entry; insert_run updated)
  - Modified: `docker-compose.yml` (USE_SESSION_CONTEXT env var passthrough)
  - Modified: `admin/src/components/RunLogTable.jsx` (session_ctx: on/off in expanded row detail)
  - Tests: 6 new tests, 199 total — all passing
  - Branch: milestone-10-session-context-flag
  - Key decisions:
    - USE_SESSION_CONTEXT is a module-level constant — changing config.yaml or env var requires a restart (consistent with all other module-level config)
    - Session history is still accumulated regardless of the flag (so a mid-session config change picks up full history on restart)
    - assembled_prompt_json in the run record is the authoritative audit — naturally reflects whether history was sent

- 2026-06-26: /build — Milestone 11 complete
  - Modified: `backend/core/orchestrator.py` (6 named constants: _OLLAMA_FINISH_REASON_LENGTH, FAILURE_REASON_TOKEN_LIMIT, FAILURE_REASON_EMPTY_OUTPUT, _MU_SOURCE_TYPE_FAILED_TOKEN_LIMIT, _MU_SOURCE_TYPE_FAILED_EMPTY_OUTPUT, FAILURE_UI_MESSAGES; _run_tool_loop now returns 5-tuple with failure_reason; run_interaction substitutes UI message on failure; output MU source_type encodes failure cause; RunRecord and InteractionResult gain failure_reason field)
  - Modified: `backend/analytics/run_log.py` (failure_reason: Optional[str] = None added to RunRecord)
  - Modified: `backend/memory/store.py` (failure_reason TEXT column in analytics table; migration entry; insert_run updated)
  - Modified: `backend/api/chat.py` (failure_reason: Optional[str] = None added to ChatResponse; populated from result.failure_reason)
  - Modified: `frontend/src/components/MessageBubble.jsx` (failureReason prop; warning visual treatment: amber border, warm background, italic text)
  - Modified: `frontend/src/components/ChatWindow.jsx` (failure_reason from API response threaded into message object; passed to MessageBubble)
  - Modified: `admin/src/components/RunLogTable.jsx` (Failure column in table header; failure indicator per row; failure_reason in expanded detail)
  - Created: `tests/test_core/test_failure_detection.py` (19 new tests)
  - Tests: 19 new tests, 218 total — all passing
  - Branch: milestone-11-empty-response-detection
  - Key decisions:
    - UI message substitution happens in run_interaction() BEFORE session history append, gate, and MU creation — so the audit trail reflects exactly what the user saw
    - output MU source_type is one of: "agent_output", "agent_output_failed_token_limit", "agent_output_failed_empty_output" — queryable by failure mode via SQL
    - finish_reason is guarded with `(finish_reason or "") == _OLLAMA_FINISH_REASON_LENGTH` to handle None from older Ollama versions
    - Max-iterations tool loop exhaustion uses FAILURE_REASON_EMPTY_OUTPUT (not a new constant — same bucket as other unexpected empties)

- 2026-06-27: /build — Milestone 13 complete
  - Created: `backend/analytics/reports/__init__.py`
  - Created: `backend/analytics/reports/series_report.py` (8-section §5 generator: failure summary, retrieval trace, retrieval rate, cross-series edges, edge weight deltas, probe integrity, Mode-3 count, num_ctx pressure)
  - Created: `backend/analytics/reports/markdown_renderer.py` (JSON → Markdown renderer for all 8 sections)
  - Modified: `backend/memory/store.py` (added `generated_reports` table + 10 new query methods for report support)
  - Modified: `backend/api/admin.py` (4 new endpoints: POST /admin/reports/generate-series, GET /admin/reports, GET /admin/reports/{id}, GET /admin/reports/{id}/markdown)
  - Modified: `config.yaml` (added `reporting.mode3_min_expected_length: 100`)
  - Created: `tests/test_analytics/test_series_report.py` (13 new tests)
  - Created: `tests/test_analytics/test_markdown_renderer.py` (8 new tests)
  - Tests: 21 new, 256 total — all passing
  - Branch: milestone-13-series-report-generator
  - Key decisions:
    - All DB access for report generation goes through new GraphStore methods — no direct SQLite in the report module
    - Ghost-MU check uses Python-side set intersection (activated_mu_ids is a JSON array — SQLite json_each() not needed for this query)
    - §5.2 trace digest truncates MU IDs to 8 chars for readability; full IDs in JSON
    - Markdown renderer caps table rows at 50 (§5.4) and 30 (§5.5) with a "…and N more" note for large datasets
    - `generated_reports` table uses CREATE TABLE IF NOT EXISTS — no ALTER TABLE migration needed (new table, not schema change to existing)

- 2026-06-27: /build — Milestone 16 complete
  - Created: `backend/analytics/reports/control_shadow.py` (§6.2: three sanity checks + comparative delta computation + generate_control_shadow() orchestrator)
  - Modified: `backend/analytics/reports/experiment_report.py` (generate_experiment_report() accepts optional control_experiment_ids; calls control shadow when provided; control_shadow key now populated)
  - Modified: `backend/analytics/reports/markdown_renderer.py` (_render_s62() replaces M14/M15 §6.2 placeholder; renders sanity checks + comparative delta table + cross-condition comparison)
  - Modified: `backend/api/admin.py` (GenerateExperimentReportRequest accepts optional control_experiment_ids dict)
  - Modified: `config.yaml` (adds reporting.random_control_variance_floor: 0.05)
  - Created: `admin/src/pages/Reports.jsx` (Reports page: list, generate form, viewer, comparison table)
  - Created: `admin/src/components/ReportsList.jsx` (filterable list with View + ↓ JSON buttons)
  - Created: `admin/src/components/ReportViewer.jsx` (react-markdown renderer for Markdown reports, GFM tables)
  - Created: `admin/src/components/ExperimentComparison.jsx` (auto-renders when control_shadow present; sanity check status badges + delta table)
  - Modified: `admin/src/App.jsx` (adds Reports tab)
  - Modified: `admin/package.json` (adds react-markdown + remark-gfm)
  - Created: `tests/test_analytics/test_control_shadow.py` (13 new tests)
  - Tests: 13 new, 315 total — all passing
  - Branch: milestone-16-control-shadow-admin-ui
  - Key decisions:
    - Control shadow embedded in the learned-condition report (not a separate file) — comparison can't drift out of sync
    - generate_control_shadow() runs _section_61_learning_measures() for each control — same code path as the learned condition, no separate implementation
    - react-markdown + remark-gfm renders GFM tables (| delimited) correctly in browser
    - Comparison table auto-renders when control_shadow is non-null in the fetched report JSON

- 2026-06-27: /build — Milestone 15 complete
  - Modified: `backend/analytics/reports/experiment_report.py` (§6.1 section: state-transition detection, MUs by probe outcome, edge delta ledger with orphan-update check, cross-series probe MUs, bridge-MU rank trajectory + transfer candidate detection, saturation/runaway-reinforcement flags; integrated into `generate_experiment_report()`)
  - Modified: `backend/analytics/reports/markdown_renderer.py` (`_render_s61()` — full Markdown renderer for all §6.1 sub-sections; replaces M14 placeholder)
  - Modified: `backend/memory/store.py` (`outcome_override TEXT` on `probe_results` schema + migration; 3 new methods: `get_all_runs_for_experiment`, `get_all_probes`, `get_mu_info_batch`)
  - Modified: `config.yaml` (`reporting.pass_rank_threshold: 3`, `reporting.saturation_threshold: 0.9`)
  - Modified: `tests/test_analytics/test_experiment_report.py` (updated M14 null-placeholder assertion to match M15 behavior)
  - Created: `tests/test_analytics/test_learning_measures.py` (33 new tests)
  - Tests: 33 new, 302 total — all passing
  - Branch: milestone-15-experiment-report-learning
  - PR: https://github.com/PaulArthurMiller/local_corvus/pull/19
  - Key decisions:
    - Weight update processing is Python-side (iterate over decoded `weight_updates` JSON arrays) to avoid SQLite `json_each()` version dependency
    - `outcome_override` is the correct mechanism for negative_flagged outcome — checked first in `_classify_probe_outcome()` before pass/fail rank logic
    - Bridge-MU transfer detection uses all reinforcement events for the experiment (no precise temporal windowing between probe sessions — probe results don't carry turn_index)
    - `learning_measures` key in the experiment report JSON is now a dict, not null

- 2026-06-27: /build — Milestone 14 complete
  - Created: `backend/analytics/reports/experiment_report.py` (§5 aggregation across series, §6.3 integrity checks, §6.4 statistical caveat, §6.1/§6.2 null placeholders)
  - Modified: `backend/analytics/reports/markdown_renderer.py` (added `render_experiment_report_markdown()`)
  - Modified: `backend/api/admin.py` (added `POST /admin/reports/generate-experiment` endpoint)
  - Modified: `backend/memory/store.py` (6 new M14 query methods: `get_all_series_for_experiment`, `get_earliest_run_timestamp_for_experiment`, `count_pre_experiment_foreign_mus`, `get_distinct_seed_series_ids`, `get_probe_session_indices_for_condition`, `get_condition_label_for_experiment`)
  - Created: `tests/test_analytics/test_experiment_report.py` (13 new tests)
  - Tests: 13 new, 269 total — all passing
  - Branch: milestone-14-experiment-report-nonlearning
  - Key decisions:
    - §5 aggregation re-queries the DB directly per-series rather than reading from prior series report files — always current, even for mid-experiment rollups
    - Outlier series in §5.3 flagged when intra-series rate differs from mean by >15 percentage points (configurable threshold)
    - §6.3 probe replay check links to experiment via `condition_label` (probe_results table has no experiment_id FK)
    - Graph-wipe check excludes NULL experiment_id MUs (pre-M12) — they're "unattributed", not foreign
    - `STATISTICAL_CAVEAT_TEXT` exported as a module-level constant so tests can assert verbatim inclusion
    - `learning_measures` and `control_shadow` keys present as `null` in JSON schema — stable for M15/M16 to fill in

- 2026-06-26: /build — Milestone 12 complete
  - Modified: `config.yaml` (adds `experiment.experiment_id: ""`)
  - Modified: `backend/gate/um.py` (`session_id` → `series_id` on UniversalMessage + create_um(); added `experiment_id: Optional[str]`)
  - Modified: `backend/analytics/run_log.py` (`session_id` → `series_id`; added `experiment_id` on RunRecord; `query_runs` param renamed)
  - Modified: `backend/memory/store.py` (`memory_units` table gains `series_id`, `experiment_id`, `cluster_id`; `analytics` table renames `session_id` → `series_id`, adds `experiment_id`; `_migrate_db` handles RENAME COLUMN; `get_session_turn_count` → `get_series_turn_count`; `create_mu` signature extended)
  - Modified: `backend/core/orchestrator.py` (`_resolve_experiment_id()` + `EXPERIMENT_ID` constant; full project rename `session_id` → `series_id` throughout; all MU + UM creation stamped with `series_id` + `experiment_id`)
  - Modified: `backend/analytics/seed_fixture.py` (seed MUs stamped with `series_id="seed:{version}"`, `experiment_id`, `cluster_id`)
  - Modified: `backend/api/chat.py` + `backend/api/admin.py` (`session_id` → `series_id` in request/response)
  - Modified: `docker-compose.yml` (adds `EXPERIMENT_ID` env var passthrough)
  - Modified: `frontend/src/api.js` + `frontend/src/components/ChatWindow.jsx` (series_id rename, "New Series" button)
  - Modified: `admin/src/api.js` + `admin/src/components/RunLogTable.jsx` (series_id rename, experiment_id in expanded detail)
  - Modified: `admin/src/components/MemoryInspector.jsx` (series_id, experiment_id, cluster_id in expanded MU detail)
  - Modified: `admin/src/components/ProbeResults.jsx` (placeholder clarified: "meas. session idx")
  - Updated: 7 existing test files (all `session_id` → `series_id`; SQL column renamed; `get_series_turn_count`)
  - Added: 17 new M12 tests (store, run_log, um, orchestrator)
  - Tests: 17 new, 235 total — all passing
  - Branch: milestone-12-series-id-prerequisite-gate
  - Key decisions:
    - EXPERIMENT_ID resolved at startup: env var → config.yaml → auto-generated `{condition_label}-{YYYY-MM-DD}` (logs WARNING if auto-generated)
    - SQLite RENAME COLUMN requires >= 3.25 (confirmed 3.38.2 in this environment)
    - `session.py` internal module NOT renamed — it stores UUIDs in `series_id` variables via the orchestrator
    - Seed MU registration is idempotent (duplicate on re-registration caught silently)

## Upcoming milestones

1. ~~**Foundation** — UM Gate + Graph Store + Session Management~~ ✅ COMPLETE
2. ~~**Memory Engine** — BM25 Retrieval + Traversal Funnel + Analyzer/Extractor~~ ✅ COMPLETE
3. ~~**Tool System** — Universal Tool Interface + File Tool + Analytics + Snapshots~~ ✅ COMPLETE
4. ~~**Learning** — Simple RL + Feedback Capture + Admin UI + Chat UI~~ ✅ COMPLETE
5. ~~**Chat UI Quality** — Token counter, copy button, New Session button~~ ✅ COMPLETE
6. ~~**Prompt Clarity** — Separate memory context from user message in Ollama call~~ ✅ COMPLETE
7. ~~**Multimodal Attachment Support** — Image attachments through the UM gate; binary transient only~~ ✅ COMPLETE
8. ~~**Core Instrumentation** — Retrieval decomposition, turn indexing, edge lifecycle, feedback-event log~~ ✅ COMPLETE
9. ~~**Experiment Framework** — Probe set, ground truth labels, seed fixture, battery snapshots~~ ✅ COMPLETE
10. ~~**Session Context Flag** — Config-driven flag to disable session history injection; off by default; per-run auditability in RunRecord and Admin UI~~ ✅ COMPLETE
11. ~~**Empty-Response Failure Detection** — Detect `finish_reason=length` + empty content; tag MU source_type with cause; thread `failure_reason` field through InteractionResult → ChatResponse → UI; `failure_reason` column in analytics; Admin UI failure indicator~~ ✅ COMPLETE
12. ~~**Series ID Prerequisite Gate** — Stamp `series_id` + `experiment_id` + `cluster_id` on `memory_units`; rename `session_id` → `series_id` project-wide; add `EXPERIMENT_ID` env var. Gates all reporting milestones.~~ ✅ COMPLETE
13. ~~**Series Report Generator (§5)** — All 8 series-report sections (failures, retrieval digest, intra/cross rate, cross-series edges, weight deltas, probe integrity, Mode-3 count, num_ctx pressure); JSON + Markdown output to `/app/memory/reports/`; admin endpoints.~~ ✅ COMPLETE
14. ~~**Experiment Report — Non-Learning Rollup (§5 aggregation + §6.3 + §6.4)** — §5 rolled up across all series; data-integrity / confound checks (seed provenance, graph-wipe, probe-replay-index normalization); statistical caveat text in template.~~ ✅ COMPLETE
15. ~~**Experiment Report — Learning Measures (§6.1)** — State-transition detection (fail→pass, pass→fail, flat); bridge-MU rank trajectory; cross-cluster transfer candidates; edge delta ledger with orphan-update check; saturation / runaway-reinforcement flags.~~ ✅ COMPLETE
16. ~~**Experiment Report — Control Shadow + Admin UI (§6.2 + dashboard)** — Control shadow embedded in learned-condition report; static/random control sanity checks; comparative deltas; Reports page with list, viewer, and comparison table.~~ ✅ COMPLETE

- 2026-06-27: /smoketest — Milestone 17 Phase 1 checks, 2 bugs found and fixed
  - Bugs found: 2
  - Bugs fixed: 2
  - Branch: fix/smoketest-2026-06-27
  - PR: https://github.com/PaulArthurMiller/local_corvus/pull/22
  - Fix 1: Python 3.12 strict `executescript()` blocked M12 migration — `_INIT_SQL` contained `CREATE INDEX idx_analytics_series ON analytics(series_id)` which fails on pre-M12 DBs (still have `session_id`). Python 3.12 made executescript raise on first failing statement, aborting `_open_db()` before `_migrate_db()` could run the RENAME COLUMN. Every admin endpoint returned 500 "no such column: series_id". Fixed by removing the index from `_INIT_SQL` and creating it in `_migrate_db()` outside the RENAME COLUMN try/except.
  - Fix 2: `memory/workspace/` and `memory/reports/` were missing from `.gitignore` — runtime files created by the file tool and report generator were being tracked by git.
  - Phase 1 status: All endpoints pass (/health, /ollama-health, all /admin/* tabs). Full pipeline verified: chat → tool use → feedback → weight updates. 315 tests passing.

- 2026-06-27: /smoketest — Milestone 17 Phase 2 (seed graph + probe setup), 4 gaps found and fixed
  - Gaps found: 4
  - Gaps fixed: 4
  - Branch: fix/smoketest-2026-06-27-2
  - PR: (see GitHub after push)
  - Fix 1: `POST /admin/seed-graph` was missing — added; reads `seed_fixtures/seed_{version}.json.gz`, calls `register_fixture()`, stamps all seed MUs with `series_id="seed:v1"` and `cluster_id`.
  - Fix 2: `GET /admin/graph/stats` was missing — added; returns `mu_count`, `mu_with_cluster_id`, `mu_without_cluster_id`, and `cluster_distribution`. Confirmed all 6 seed MUs have cluster_id.
  - Fix 3: `POST /admin/probes/snapshot` was missing — added; calls `take_battery_snapshot(battery_index, phase, condition_label)`. Confirmed snapshot file written at `battery_0_pre_seed_*.json.gz`.
  - Fix 4: `probe_set_v1.yaml` was an empty template — filled in all 5 probes with prompt text and gold_mu_ids keyed to seed fixture v1 MU IDs. `seed_fixtures/seed_v1.json.gz` created with 6 MUs: cluster_a×3, cluster_b×2, bridge_ab×1.
  - Config: `pass_rank_threshold` corrected from 3 to 1 (M15 deferred risk — K2=1 means at most 4 MUs retrieved; threshold=1 makes gold-rank-1 the meaningful pass criterion).
  - Probe baseline results (session_index=0, condition=seed, graph contaminated by Phase 1 MUs):
    - p01 (ranking_improvement, file_write): FAIL — Phase-1 MUs outscored seed MU; expected in polluted graph; will pass on clean graph
    - p02 (ranking_improvement, BM25 concepts): PASS rank=1
    - p03 (cross_cluster_transfer, bridge): PASS rank=1
    - p04 (cross_cluster_transfer, bridge): PASS rank=1
    - p05 (process_rule_adherence): PASS rank=1
  - Note: 47 non-seed MUs from Phase 1 lack cluster_id (NULL). These are regular interaction MUs. Cross-cluster analysis will correctly exclude them (they have no cluster_id). Real experiment conditions start from a wiped graph + seed fixture only.
  - Phase 2 status: All Phase 2 steps verified. Seed graph operational. Probe machinery confirmed working. 315 tests still passing.

- 2026-06-27: /smoketest — Milestone 17 Phase 3 (three-condition pilot), 2 bugs found and fixed
  - Bugs found: 2
  - Bugs fixed: 2
  - Branch: fix/smoketest-2026-06-27-3
  - PR: (see GitHub after push)
  - Fix 1: `POST /admin/edges/randomize` was missing — added to admin.py; `GraphStore.randomize_all_edge_weights()` added to store.py. Required for the random-weight control condition: sets all edge weights to uniform random [0, 1] before each series. Edge counts grew correctly (179 → 209 → 239 across 3 series).
  - Fix 2: `generate_experiment_report()` read `condition_label` from the CURRENT runtime config.yaml instead of querying the DB. In a multi-condition session (or when reports are generated after a config switch), all experiment report headers incorrectly showed the active condition rather than the experiment's actual condition. Fixed: now queries `get_condition_label_for_experiment()` first; falls back to config.yaml only when no runs exist.
  - Pilot results (3 series × 5 turns per condition, shared graph — no wipe between conditions):
    - Learned condition (exp-learned-2026-06-27): 15/15 turns PASS, 15 feedback events (+1), edges_updated active (0–8 per turn), 3/3 series reports generated
    - Static-weight control (exp-static-2026-06-27): 15/15 turns PASS, no feedback, 3/3 series reports generated
    - Random-weight control (exp-random-2026-06-27): 15/15 turns PASS, 3 edge randomizations (179/209/239 edges), no feedback, 3/3 series reports generated
  - Control shadow (all 4 sanity checks PASS on real data):
    - `zero_weight_changes` for static control: PASS (0 runs with weight_updates)
    - `zero_feedback_edge_correlation` for static control: PASS
    - `weights_rerandomized` for random control: PASS (std_dev > 0.05 floor)
    - `zero_feedback_edge_correlation` for random control: PASS
  - Probe results: all probes FAIL at session_index=2 (after pilot turns) because the shared graph is contaminated — interaction MUs from prior turns outrank seed MUs. Expected: real experiments must start from a wiped graph + re-seeded state per condition.
  - Phase 3 status: All pilot machinery verified end-to-end. 315 tests passing. Config restored to neutral (experiment_id="", condition_label="learning").

- 2026-06-28: /smoketest — Milestone 17 Phase 4 (full report generation and validation), 0 bugs found
  - Bugs found: 0
  - Bugs fixed: 0
  - Branch: fix/smoketest-2026-06-28
  - Phase 4 checks (all PASS):
    - Check 1: `POST /admin/reports/generate-experiment` for learned condition → `learning_measures` not null (11 keys populated: state_transitions, edge_delta_ledger, bridge_mu_analysis, saturation_analysis, etc.)
    - Check 2: Same endpoint with `control_experiment_ids: {static_weight: ..., random_weight: ...}` → `control_shadow` populated with both controls. Note: UI and code use `static_weight` / `random_weight` as key names (not `static` / `random`).
    - Check 3: All 4 sanity checks PASS — `zero_weight_changes` (static_weight, 0 violations), `zero_feedback_edge_correlation` (static_weight, 0 violations), `weights_rerandomized` (random_weight, std_dev=0.281, floor=0.05), `zero_feedback_edge_correlation` (random_weight, 0 violations).
    - Check 4: Admin UI Reports tab verified via API + code inspection: 17 reports appear in list (8 experiment + 9 series), Markdown endpoint returns full content with all sections (§5.1/§5.3/§5.5–§5.8, §6.1, §6.2, §6.3, §6.4), ↓JSON download implemented via Blob in ReportsList.jsx, ExperimentComparison.jsx reads control_shadow and renders comparative deltas. Browser extension unavailable — visual confirmation to be done by user.
    - Check 5: GFM table rendering — `remark-gfm` v4.0.1 installed in admin/package.json and wired up in ReportViewer.jsx with `remarkPlugins={[remarkGfm]}`. Markdown report contains 48 GFM table rows. M16 risk resolved.
  - Phase 4 status: All report generation machinery verified end-to-end. 315 tests passing. No code changes required.

- 2026-06-28: /smoketest — Milestone 17 Phase 5 (integration test suite), 3 gaps fixed
  - Gaps found: 3
  - Gaps fixed: 3
  - Branch: fix/smoketest-2026-06-28-2
  - PR: https://github.com/PaulArthurMiller/local_corvus/pull/26
  - Fix 1: `probe_pass_rates` missing from `learning_measures` — BUILD-PLAN §6.1 required it; added to `_section_61_learning_measures()` in `experiment_report.py`. Computed from `state_transitions.per_probe`, fraction of non-flagged outcomes that are "pass", rounded to 4 decimal places.
  - Fix 2: `tests/integration/test_e2e_pipeline.py` — created full integration test suite with 5 `pytest.mark.integration` tests using stdlib urllib only (no extra deps). Tests call real HTTP endpoints at `localhost:8000`. Tests 3 and 4 correctly call `GET /admin/reports/{report_id}` after the generate POST (the generate endpoints return only metadata). `_find_experiment_ids()` reads condition_label from `/admin/runs` (ground truth) not stale report metadata.
  - Fix 3: `pytest.ini` — added `--ignore=tests/integration` and registered `integration` marker so integration tests are excluded from the default `pytest` run.
  - Phase 5 integration test results (5/5 PASS):
    - test_chat_turn_creates_memory_units: PASS (run_id, input_mu_id, output_mu_id all distinct, run visible in /admin/runs)
    - test_feedback_event_triggers_weight_update: PASS (12 weight_updates returned, each with feedback_event_id; learning-condition run used to avoid contaminating control sanity checks)
    - test_series_report_generates_all_sections: PASS (all 8 §5 section keys present and non-null)
    - test_experiment_report_learning_measures_populated: PASS (probe_pass_rates + state_transitions present in learning_measures)
    - test_control_shadow_sanity_checks_pass: PASS (zero_weight_changes, zero_feedback_edge_correlation for static; weights_rerandomized, zero_feedback_edge_correlation for random — all PASS)
  - Note: test 2 was initially contaminating the random-weight control by sending feedback to a run in exp-random-2026-06-27 (selecting the first run with activated_edge_ids). Fixed by filtering to learning-condition runs only. The contaminating feedback_events row and weight_updates were cleaned up from the DB directly.
  - Phase 5 status: All 5 integration tests passing. 315 unit tests still passing. No regressions.

- 2026-06-28: /smoketest — Milestone 17 Phase 6 (hardening + documentation), 1 gap fixed
  - Gaps found: 1 (Docker healthcheck reflected process-up only, not DB availability)
  - Changes made: 3
  - Branch: fix/smoketest-2026-06-28-3
  - Change 1: Docker healthcheck — `GET /health` now executes a lightweight `SELECT 1` via a new `GraphStore.ping()` method. Returns `{"status":"ok","db":"ok"}` when the DB is reachable and `{"status":"degraded","db":"error"}` when it is not. Dockerfile `HEALTHCHECK` instruction added (30 s interval, 10 s timeout, 15 s start period, 3 retries). `docker-compose.yml` frontend/admin now use `condition: service_healthy` so they wait for a confirmed healthy backend before starting.
  - Change 2: `config.yaml` — comprehensive inline comments added to all hyperparameters. Each entry now documents: what the parameter does, the default value and its rationale, valid range, and any open questions or deferred decisions. File is now self-explaining as a research artifact.
  - Change 3: `docs/experiment_protocol.md` (new) — step-by-step runbook for running a three-condition experiment from scratch. Covers: environment setup and healthcheck verification; graph wipe and seed fixture registration; baseline probe battery; learned / static-weight / random-weight condition runs (with exact API calls and expected outputs); report generation; integration test validation; quick-reference endpoint table; troubleshooting guide for common failure modes.
  - Phase 6 status: All hardening and documentation tasks complete. 315 unit tests still passing. No regressions.

## Milestone 17 — Research Outcomes and Open Questions

### Probe pass-rates across conditions (pilot data, shared graph — not clean-condition results)

All probe results below were collected on a shared graph contaminated by interaction
MUs from prior phases. These are machinery-validation results, not experiment results.
A real experiment must start from a wiped graph + re-seeded state per condition.

- **Pilot (3 series × 5 turns, learned condition):** 15/15 turns PASS; feedback active;
  3–8 edges updated per turn; 3/3 series reports generated successfully.
- **Static-weight control:** 15/15 turns PASS; 0 weight updates confirmed; 3/3 series reports.
- **Random-weight control:** 15/15 turns PASS; edge variance std_dev=0.281 (floor=0.05 PASS);
  3/3 series reports.
- **Baseline probe battery (session_index=0, seed graph + Phase 1 MUs):** p01 FAIL
  (Phase-1 MUs outscored seed MU — expected on polluted graph); p02–p05 all PASS rank=1.
- **Post-pilot probe battery (session_index=2, polluted graph):** all 5 probes FAIL —
  interaction MUs dominate the retrieved set. Expected. Confirms clean-graph requirement.

### Anomalies observed

1. **Python 3.12 strict `executescript()` (Phase 1):** `CREATE INDEX` on a non-existent
   column aborted `_open_db()` before `_migrate_db()` could rename it. Fixed by moving
   the index creation to `_migrate_db()` outside the RENAME COLUMN try/except block.
2. **`generate_experiment_report()` reading condition_label from live config (Phase 3):**
   Multi-condition sessions showed the active condition, not the experiment's actual condition,
   in report headers. Fixed by querying `get_condition_label_for_experiment()` first.
3. **Integration test contaminating random-weight control (Phase 5):** Test 2 was selecting
   the first run with `activated_edge_ids` regardless of condition, sending feedback to
   a random-weight run and poisoning the `zero_feedback_edge_correlation` sanity check.
   Fixed by filtering to learning-condition runs only.

### Open questions for future work

1. **Full three-condition experiment:** The pilot used a shared graph and 3 series × 5 turns.
   A real experiment requires separate clean graphs per condition and at least 5 series × 10 turns
   each with probe batteries at start/mid/end. The `docs/experiment_protocol.md` describes
   the exact procedure.
2. **Weight initialization value (open since M1):** Flat 0.5 neutral prior. After the first
   real learning run, compare probe pass-rate trajectories against initial_edge_weight ∈ {0.1, 0.5, 0.9}.
3. **Decay function (open since M1):** `decay_rate: 0.01` is defined but the decay trigger
   is not yet implemented (deferred to Phase 2). Without decay, high-activation MUs accumulate
   weight indefinitely — the saturation check (threshold=0.9) is the current guard.
4. **RL reward distribution (resolved as uniform, open for improvement):** All activated edges
   receive the same delta. Backpropagation-style decay from the output MU would more accurately
   attribute learning to causal edges.
5. **Cross-cluster transfer signal:** The bridge-MU rank trajectory section requires probe
   batteries across multiple measurement sessions with a clean graph. The pilot did not produce
   meaningful transfer signal — graph contamination dominated retrieval.
6. **multimodal (Ollama regression, M7):** Ollama 0.30.x on Windows has a regression where
   gemma4:e4b decodes image tokens but does not interpret image content. Monitor the Ollama
   changelog for a fix to GitHub Issue #16532 before using attachment features in experiments.
7. **Output-compliance grading for `process_rule_adherence` probes:** The current probe pass
   criterion checks only retrieval rank (gold MU appears in top-K). It does not verify whether
   the agent's generated response actually follows the process rule described in the seed MU.
   A full compliance check would require a judge/evaluator step (LLM-as-judge or structured
   output parsing) that goes beyond rank-checking. Deferred as a candidate for a future
   automation-focused milestone — not addressed in M18.

- 2026-06-28: /build — Milestone 18 Phase 0 complete (infrastructure fixes)
  - Modified: `backend/analytics/seed_fixture.py` (Phase 0.1: `register_fixture()` now inserts edges from fixture; `SeedFixtureManifest` gains `edge_count: int = 0`; idempotent — duplicate edges are skipped with DEBUG log)
  - Modified: `backend/api/admin.py` (Phase 0.2: `RunProbeRequest` gains `probe_set_version: str = "v1"`; hardcoded "v1" replaced with `body.probe_set_version` in `run_probe`)
  - Modified: `backend/main.py` (Phase 0.3: startup probe sync now loops over ["v1", "v2"]; missing yaml logs INFO and continues; exceptions log WARNING and continue)
  - Modified: `config.yaml` (Phase 0.4: added `reporting.pass_rank_thresholds` dict with per-phenomenon overrides: `cross_cluster_transfer: 4`, `ranking_improvement: 1`, `process_rule_adherence: 1`)
  - Modified: `backend/analytics/reports/experiment_report.py` (Phase 0.4: `_classify_probe_outcome` gains `phenomenon_type` and `phenomenon_thresholds` optional params; `_compute_state_transitions` and `_compute_mus_by_probe_outcome` thread phenomenon-aware thresholds; `_section_61_learning_measures` reads and stores `phenomenon_thresholds` in return dict)
  - Modified: `backend/analytics/reports/control_shadow.py` (Phase 0.4: `_final_pass_rate` uses `_classify_probe_outcome` with thresholds from measures dict)
  - Modified: `backend/analytics/reports/markdown_renderer.py` (Phase 0.4: §6.1 header shows per-phenomenon thresholds when `phenomenon_thresholds` is non-empty)
  - Modified: `.gitignore` (added `seed_fixtures/*_draft.json` and `scripts/__pycache__/`)
  - Added: `tests/test_analytics/test_seed_fixture.py` extensions (3 new tests: edge insertion, edge_count=0 for no edges, idempotent duplicate)
  - Added: `tests/test_api/test_run_probe_versioned.py` (5 new tests: default version, v2 accepted, FileNotFoundError for missing version, v1 loads for v1, v2 loads for v2)
  - Added: `tests/test_startup/test_probe_sync.py` (4 new tests: both synced, missing v2, missing v1, exception non-fatal)
  - Added: `tests/test_analytics/test_learning_measures.py` extensions (6 new tests: phenomenon-aware classify — cross_cluster pass/fail, ranking_improvement override, fallback to default, gold_mu_rank untouched)
  - Tests: 17 new, 332 total — all passing
  - Branch: milestone-18-phase-0-infra-fixes
  - Gap A (cross_cluster_transfer pass criterion): RESOLVED — per-phenomenon threshold config added; cross_cluster_transfer uses threshold=4 (K1+K2); raw gold_mu_rank never modified
  - Gap B (process_rule_adherence output compliance): DOCUMENTED ONLY — noted in probe YAML comments; no code change needed
