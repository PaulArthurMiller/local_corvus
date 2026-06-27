# Progress

## Current state

Project: Local Corvus — local LLM learning research platform  
Phase: Building  
Current milestone: 16 — Experiment Report Control Shadow + Admin UI — COMPLETE  
Next step: Review PR, then run the three-condition experiment (learning / static-weight / random-weight) to produce real data for the reports.

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
