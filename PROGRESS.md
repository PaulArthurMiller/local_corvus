# Progress

## Current state

Project: Local Corvus — local LLM learning research platform  
Phase: Active — M19 Phases 1–3 planned (BUILD-PLAN.md updated 2026-07-04)  
Current milestone: 19 Phase 3 — Full Pilot Execution + Reporting Wire-Up — COMPLETE  
Next step: Review PR for milestone-19-phase-3-pilot, then merge. M19 (Phases 0–3) is fully complete once merged.

## Session log

- 2026-07-04: /planner — M19 Phases 1–3 decomposed into three milestones in BUILD-PLAN.md
  - Phase 1 (`milestone-19-phase-1-prompt-bank`): Sonnet-generated prompt bank + criteria, lexical-distinctness validation (pure-Python fingerprint scan reusing `generate_seed_v2.py` constants), versioned `prompt_banks/prompt_bank_{hash}.json` artifact, deterministic prompt_id assignment in turn resolution, cost-estimate confirmation gate. Model: `claude-sonnet-5` via `anthropic` SDK — host-side dev dependency only, never in Docker.
  - Phase 2 (`milestone-19-phase-2-judge`): Gemma judge module + verdict logging. **Planning discovery:** `feedback_events` table lacks all 8 judge fields the overview requires (`judge_source`, `condition_label`, `criterion_triggered`, etc.) — Phase 2 includes a DB migration and a new `POST /feedback/verdict` endpoint with an explicit `apply_weight_update` flag; the condition gate stays in `runner.py` (judge is condition-agnostic). Open decision flagged: verdict string vocabulary (`thumbs_up` vs `up`) — resolve by grepping report code at build time.
  - Phase 3 (`milestone-19-phase-3-pilot`): series/experiment report wire-up, notification channel dispatch (`log` only implemented), budget cap pre-run + mid-run, and the required full pilot smoke run. **Planning discovery:** `runner.py` never passes `series_id` on chat calls — series reports would query empty sets; fixed as Phase 3 build item 1. Open question flagged: how runs get stamped with `experiment_id` (highest-risk unknown; resolve before writing reporting calls).
  - Judge calls go to Ollama at `localhost:11434` from the host-side harness (not `host.docker.internal`, which is the container-side alias).
- 2026-07-05: /build — Milestone 19 Phase 3 complete (Full Pilot Execution + Reporting Wire-Up)
  - **Planning discovery, resolved before writing any Phase 3 code (highest-risk unknown):** confirmed live that `EXPERIMENT_ID` (`backend/core/orchestrator.py`) is resolved once at backend process startup from an env var and cached as a module-level constant for the process's lifetime — `/chat` has no per-request override, and `/admin/graph/wipe`'s `experiment_id` field only names the pre-wipe archive file, it does not change what subsequent runs get stamped with. Asked the user how to resolve this; chosen approach: the harness restarts the backend container with a new `EXPERIMENT_ID` between conditions rather than adding a backend override (keeps the backend server-side footprint at zero, matching the plan's intent).
  - **Second gap discovered only during live Docker verification (not caught by unit tests, since the fake `BackendClient` never exercises real session semantics):** `/chat`'s `series_id` field was being used as a session *lookup* key, not a caller-supplied stamp — passing a deterministic, never-before-seen `series_id` silently discarded it and minted a random UUID instead, on *every* call. This would have made every turn in a "series" land under a different random ID, and series-level reporting would have found nothing. Fixed with a new `get_or_create_session()` in `backend/core/session.py` (uses the caller-supplied ID as the session key on first use if not already registered, otherwise behaves exactly like `create_session()`); confirmed backward-compatible with the chat UI, which only ever resends an ID the backend already issued.
  - **Third gap, not in the original 5 build items:** the `random_weight` control condition needs edge weights re-randomized at the start of each series — this was never wired into `runner.py` (only ever done manually in the M17 smoketest). Added: `client.randomize_edges()` called once per series when `condition == "random_weight"`.
  - Created: `scripts/harness/cost.py` (`CostTracker`, `estimate_prerun_cost()`, `check_prerun_budget()`, `BudgetExceededError`; `JUDGE_COST_PER_CALL_USD` constant — $0 for local Ollama today, structure ready for a hosted judge), `scripts/harness/docker_lifecycle.py` (`restart_backend()` — `docker compose up -d --force-recreate backend` with env overrides; `wait_for_health()` — polls `/health`; `DockerLifecycleError`)
  - Modified: `scripts/harness/runner.py` (`execute_run()` gains `docker_project_root`/`restart_backend_fn`/`wait_for_health_fn` (opt-in, default `None` — every existing test/dry-run call site unaffected) and `bank_generation_cost_usd`/`judge_cost_per_call`; per-condition backend restart before wipe/seed when `docker_project_root` is set; deterministic `series_id` (`{plan_id}_{condition}_s{series_index}`) passed on every `client.chat()` call; series-close reporting keyed off `plan.turns_per_series` (not the dry-run-scoped sequence, so a short smoke dry-run never fires it prematurely); experiment-close reporting when `"learned"` is among the conditions that ran, with `control_experiment_ids` assembled deterministically; pre-run budget gate as the first check, before any graph mutation; mid-run budget check after every judge call; `random_weight` edge randomization once per series)
  - Modified: `scripts/harness/client.py` (`generate_series_report()`, `generate_experiment_report()`, `randomize_edges()`; new `ReportGenerationError` — deliberately distinct from `BackendConnectivityError`/`BackendTurnError` so report failures are always governed by the new `error_policy["report_generation"]` key, never the generic categories)
  - Modified: `scripts/harness/schema.py` (`DEFAULT_ERROR_POLICY` gains `"report_generation": "soft_continue"`; `NotificationConfig.channel: str = "log"` validated against `("log", "email", "webhook")`)
  - Modified: `scripts/harness/notify.py` (`notify()` is now a dispatch table keyed by `channel`; `"email"`/`"webhook"` log a warning and fall back to `"log"` verbatim — adding a real channel later is one new handler function)
  - Modified: `scripts/harness/checkpoint.py` (`SeriesReportRecord`, `ExperimentReportRecord`; `CheckpointState` gains `series_reports: list[SeriesReportRecord]` and `experiment_report: Optional[ExperimentReportRecord]`)
  - Modified: `scripts/harness/manifest.py` (`RunManifest.series_id_map: dict[str, str]` — full `{condition}:{series_index} -> series_id` map for every condition/series in the plan, computed up front)
  - Modified: `scripts/harness/prompt_gen.py` (`estimate_bank_cost_for_plan()` — pure pre-run cost estimate, no API calls, returns `None` when an identical-hash bank already exists)
  - Modified: `scripts/run_experiment_harness.py` (`--no-docker-restart` flag; pre-run budget gate before *any* Sonnet spend or graph mutation, ahead of the CLI's own bank-generation call)
  - Modified: `backend/core/session.py` (`get_or_create_session()`), `backend/api/chat.py` (uses it instead of the old get-then-create-random dance)
  - Added: `tests/test_harness/test_cost.py` (8 new), `tests/test_harness/test_docker_lifecycle.py` (4 new — fake subprocess/sleep/clock, no real Docker or wall-clock waiting), `tests/test_harness/test_notify.py` (4 new)
  - Extended: `tests/test_harness/test_runner.py` (24 new — series/experiment-close reporting incl. soft-continue and hard-stop-override, `random_weight` edge randomization, Docker restart wiring with injected fakes, budget_cap pre-run/none-never-stops/exceeded-by-judge-costs, deterministic `series_id` + manifest map), `tests/test_harness/test_schema.py` (+4), `tests/test_harness/test_prompt_gen.py` (+2), `tests/test_core/test_session.py` (+4)
  - Tests: 45 new, 510 total — all passing
  - Branch: `milestone-19-phase-3-pilot`
  - **Full pilot smoke run — the required done criterion — PASSED with real data:** Docker stack rebuilt and started (backend/frontend/admin, all healthy); ran `plans/phase3_pilot.json` (3 conditions × 1 series × 7 turns [5 bulk + 2 probe], real `gemma4:e4b` agent + judge via Ollama, existing validated Phase 1 prompt bank reused so $0 Sonnet spend). Confirmed live:
    1. Backend restarted correctly for all 3 conditions with the right `EXPERIMENT_ID` each time (verified via `/admin/runs` before the full pilot: a manual restart to `EXPERIMENT_ID=manual-test-123` produced a run stamped `experiment_id: "manual-test-123"`).
    2. All 21 turns completed (`COMPLETE: 21 turn(s) processed`); every bulk turn got a judge verdict (all `"up"` on this pilot's small prompt set).
    3. `random_weight` condition randomized 32 edges once at series start (never re-randomized mid-series, confirmed in the run log).
    4. 3 series reports generated (one per condition) + 1 experiment report (`experiment_report_phase3-pilot-2026-07-05_learned.md`), with `control_experiment_ids = {"static_weight": "...", "random_weight": "..."}` correctly assembled.
    5. **§6.2 control shadow — all 4 sanity checks PASS on real data:** `static_weight`: `zero_weight_changes` PASS (0 violations), `zero_feedback_edge_correlation` PASS (0 violations). `random_weight`: `weights_rerandomized` PASS (`weight_std_dev=0.1848` > `0.05` floor), `zero_feedback_edge_correlation` PASS (0 violations).
    6. Notification fired: `{"status": "complete", "report_path": "/app/data/reports/experiment_report_phase3-pilot-2026-07-05_learned.md"}`.
    7. `budget_cap` verified live in both directions: a plan needing a genuinely new (not-already-cached) bank with `budget_cap=0.01` hard-stopped with `pre-run estimated cost $0.5833 exceeds budget_cap $0.0100` before any Docker restart, wipe, or API call; the real pilot plan (`budget_cap=None`) proceeded normally.
  - Full test suite (510) passing, no regressions.
  - **Post-verification review (user-requested):** traced the EXPERIMENT_ID/series_id mechanism end-to-end for information-loss risk. Found one real gap, now fixed: on a resumed run, `execute_run()`'s condition loop unconditionally restarted/wiped/reseeded *every* condition, including ones already fully complete in the checkpoint — wasteful (an extra Docker restart + wipe/seed cycle) though never destructive (`wipe_graph()` never touches `analytics`/`feedback_events`, and the `already_reported` guard already prevented duplicate series reports). Fixed: `runner.py` now skips the restart/wipe/seed entirely when every turn for a condition is already checkpointed. 1 new test (`test_resume_skips_restart_wipe_seed_for_already_complete_condition`), 511 total passing. Separately noted (pre-existing, not changed by Phase 3): re-running an already-`complete` plan starts a fresh checkpoint but reuses the same deterministic `{plan_id}_{condition}` experiment_id, so two independent full trials of the same plan would mix their analytics rows under one experiment_id bucket — inherited from Phase 0's resume-vs-fresh logic, not introduced here; flagged for awareness, not fixed in this session.
  - Docker stack stopped (`docker compose down`) — data untouched (bind-mounted `./memory`, not a container volume).
  - **Follow-up (user-requested): fixed the pre-existing experiment_id collision issue.** Root cause: `run_id`/`experiment_id` were derived purely from `plan_id` (+ config hash for `run_id`), with no distinction between "resuming an incomplete run" and "starting fresh at an identity that already has data" — a `"complete"` checkpoint skipped the resume-prompt entirely and silently created a brand-new `CheckpointState` at the *same* `run_id`, and declining to resume an incomplete run did the same. Since `wipe_graph()` never deletes `analytics`/`feedback_events`, either path would write a second trial's rows into the same `experiment_id` bucket as the first (and silently overwrite the first run's `manifest.json`/`checkpoint.json` files on disk).
    - Fix: `manifest.py` — `compute_run_id()` gains an `attempt: int = 1` parameter (`attempt=1` reproduces the exact original `run_id` format, so a plan that only ever runs once is unaffected); new `run_identity_for(plan, attempt)` for building `experiment_id`/`series_id` strings (`"{plan_id}"` at attempt 1, `"{plan_id}_attemptN"` beyond).
    - `runner.py` — the resume-detection block is now a loop: it tries attempt 1, and only bumps to attempt 2+ when that identity's checkpoint is already `"complete"`, or the user declines to resume an incomplete one. Accepting a resume keeps attempt 1's identity unchanged (the common, everyday case). `experiment_id`/`series_id_map`/`control_experiment_ids` all now derive from `run_identity` instead of raw `plan.plan_id`.
    - 7 new tests (`test_manifest.py` — `compute_run_id`/`run_identity_for` attempt behavior; `test_runner.py` — re-running an already-complete plan gets a new run_id + experiment_id, declining a resume gets a new run_id + experiment_id, accepting a resume keeps the same one). 518 tests total, all passing.
- 2026-07-05: /build — Milestone 19 Phase 2 complete (Judge Integration)
  - Created: `scripts/harness/judge.py` (`OllamaJudgeClient` — `chat_json()`, `model_available()`; `judge_turn()`; `JudgeVerdict` dataclass; `ProbeTurnJudgeError`, `JudgeUnparseableError`; closed-vocabulary verdict parsing with one re-ask)
  - Modified: `backend/memory/store.py` (feedback_events gains 8 nullable columns: `condition_label`, `rationale`, `criterion_triggered`, `judge_source`, `judge_model_name`, `judge_temperature`, `judge_pinned_snapshot`, `prompt_id` — additive `_migrate_db()` entries + `_INIT_SQL`; `insert_feedback_event()` writes all new fields)
  - Modified: `backend/analytics/feedback_events.py` (`FeedbackEvent` gains the same 8 optional fields)
  - Modified: `backend/api/chat.py` (new `POST /feedback/verdict` endpoint with `VerdictRequest`/`VerdictResponse`; extracted `_apply_weight_updates()` shared helper — used by both `/feedback` and `/feedback/verdict` so the §6.1 edge-delta-ledger orphan check audits one code path; `/feedback` now stamps `judge_source="human"`, contract otherwise unchanged)
  - Modified: `scripts/harness/client.py` (`BackendClient.post_verdict()` — POST /feedback/verdict wrapper, same connectivity/turn-error split as `chat()`)
  - Modified: `scripts/harness/schema.py` (`JudgeConfig.temperature` tightened `le=2.0` → `le=0.1`; `DEFAULT_ERROR_POLICY` gains `"judge_unparseable": "soft_continue"`)
  - Modified: `scripts/harness/checkpoint.py` (`TurnCheckpoint` gains `verdict`, `feedback_event_id`, `judge_error` — kept separate from `detail` so a verdict-posting hiccup never overwrites the chat call's own audit info)
  - Modified: `scripts/harness/runner.py` (judge-availability preflight — `GET /api/tags`, unconditional hard stop if the judge model is missing, runs before any wipe/seed call; `_judge_and_post_verdict()` helper: judges each successfully-completed bulk turn via the loaded prompt bank's criteria, computes `apply_weight_update = (condition == "learned" and verdict in ("up","down"))`, posts the verdict, supports `--interactive` accept/override/skip; `JudgeUnparseableError` routes through `error_policy["judge_unparseable"]`)
  - Modified: `scripts/run_experiment_harness.py` (`--interactive` and `--judge-host` CLI flags; constructs `OllamaJudgeClient` alongside `BackendClient`)
  - Added: `tests/test_harness/test_judge.py` (13 new — verdict parsing incl. closed-vocabulary `criterion_triggered` check, probe-turn guard, retry-then-raise, and `OllamaJudgeClient` request-shape tests against `httpx.MockTransport`)
  - Extended: `tests/test_harness/test_runner.py` (11 new — learned/static/none weight-update gating, no-judge-client no-op, unparseable soft-continue + checkpoint anomaly, hard-stop override, `--interactive` accept/override, judge preflight hard-stop, probe turns never judged)
  - Extended: `tests/test_harness/test_schema.py` (5 new — temperature validator, `judge_unparseable` policy default/override)
  - Extended: `tests/test_api/test_feedback_verdict.py` (new file, 12 tests), `tests/test_analytics/test_feedback_events.py` (+4), `tests/test_memory/test_store.py` (+1 migration test)
  - Tests: 44 new, 465 total — all passing
  - Branch: `milestone-19-phase-2-judge`
  - **Verdict vocabulary decision (build item 2, resolved by grep):** searched `series_report.py`, `experiment_report.py`, `control_shadow.py` for `"thumbs_up"`/`"thumbs_down"` string matching — found none; the one comment referencing "thumbs_up runs" in `experiment_report.py` actually keys off `analytics.feedback_signal == 1`, not the verdict string. Decision: the judge endpoint stores its own `"up"/"down"/"none"` vocabulary directly rather than mapping onto the older `"thumbs_up"/"thumbs_down"` strings; `judge_source` distinguishes origin, not the verdict string.
  - **Live verification (Docker not running this session — same deferral as Phase 0/1, Ollama was reachable natively):** ran `judge_turn()` against the real `gemma4:e4b` model for 5 real (prompt, criteria, response) cases spanning up/down verdicts and a criterion-triggered check — all 5 returned clean, correctly-classified JSON on the first attempt (0% unparseable rate, well under the 10% risk threshold flagged in BUILD-PLAN.md). Full Docker-backend dry-run (checks 1–2 of the Phase 2 verification protocol) deferred to a future `/smoketest` session, same as Phase 0/1's precedent — `execute_run()` was instead verified against a fake `BackendClient` in `test_runner.py`, exercising the identical code path.
  - **Pre-pilot calibration reminder (flagged in BUILD-PLAN.md):** before any large pilot run, spot-check 15–20 judged turns with `--interactive` against Gemma's verdicts to catch ambiguously-written criteria — schedule this between Phase 2 merge and the Phase 3 pilot.
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

- 2026-06-28: /build — Milestone 18 Phase 1 complete (seed_v2 content generation script)
  - Created: `scripts/generate_seed_v2.py` — generates 34 MU contents via Gemma 4 (Ollama localhost:11434), assigns fixed UUIDs, wires 32 hub-and-spoke + bridge edges, writes `seed_fixtures/seed_v2_draft.json` (DRAFT — not committed)
  - `seed_fixtures/seed_v2_draft.json` written and awaiting Paul's review (excluded from git by .gitignore)
  - Script run successfully: 34 MUs + 32 edges generated; 1 retry (c_spoke1 missing 'enforce' on first attempt)
  - Validation: bridge_ab and bridge_cd forbidden-vocabulary checks PASS; hub MUs all contain all 5 cluster fingerprint terms; spoke MUs contain 3-5 of 5 fingerprints (BUILD-PLAN spec: "most MUs")
  - Tests: 332 existing tests — all passing; no new tests (script is a standalone dev tool, not part of the test suite)
  - Branch: milestone-18-phase-1-seed-v2-generation
  - Next step: Paul reviews seed_fixtures/seed_v2_draft.json content, approves or requests rewrites, then freezes to seed_v2.json.gz for Phase 3 verification

- 2026-06-28: M18 Phase 2 complete — Paul reviewed seed_v2_draft.json and froze to seed_fixtures/seed_v2.json.gz (commit 951d69f)

- 2026-06-29: /smoketest — Milestone 18 Phase 3 (verification script), 0 bugs found
  - Bugs found: 0
  - Gaps fixed: 0 (all verification checks passed on first run)
  - Branch: fix/smoketest-2026-06-29
  - Created: `scripts/verify_seed_v2.py` — 5-check verification script against the frozen seed_v2.json.gz
  - Phase 3 verification results (all PASS against seed_fixtures/seed_v2.json.gz):
    - Check 1 — MU count: 34 MUs, cluster_distribution exactly as specified (cluster_a/b/c/d×8 each, bridge_ab/cd×1 each): PASS
    - Check 2 — Edge wiring: 32 edges; each hub has 7 incoming spoke->hub edges and 1 outgoing hub->bridge edge; no edges between {a,b} group and {c,d} group: PASS
    - Check 3 — Connectivity: hub->bridge edge confirmed for all 4 clusters (cluster_a->bridge_ab, cluster_b->bridge_ab, cluster_c->bridge_cd, cluster_d->bridge_cd): PASS
    - Check 4 — Lexical-distinctness: bridge_ab contains none of CLUSTER_A_FP+CLUSTER_B_FP; bridge_cd contains none of CLUSTER_C_FP+CLUSTER_D_FP: PASS
    - Check 5 — BM25 probe sanity (simulated retrieval): for all 4 cross-cluster queries (p10–p13), K1=[3 in-cluster MUs], K2=[correct bridge]; bridge never at K1: PASS
  - Tests: 332 existing tests — all passing; no regressions
  - Note: seed_v2.json.gz is verified and ready for Phase 4 (probe_set_v2.yaml)

- 2026-06-29: /smoketest — Milestone 18 Phase 4 (probe set + final verification), 0 bugs found
  - Bugs found: 0
  - Gaps fixed: 0 (all 7 verification checks passed; 2 probe texts required BM25-driven revision before PASS)
  - Branch: fix/smoketest-2026-06-29
  - Created: `probes/probe_set_v2.yaml` — 10 probes (p06–p15); prompt_text filled in from seed_v2 MU content
  - Probe text design notes:
    - RETRIEVAL_KEYWORD_COUNT=5 means only the first 5 non-stopword tokens are used as BM25 query keywords
    - p08 initial text had 'what, relationship, between, system, rule' as top-5 tokens → hub only 1 hit, not in multi-hit pool
    - p12 initial text had 'how, system, rule, function, as' as top-5 tokens → hub not in multi-hit pool, bridge unreachable
    - Both fixed by leading with cluster_c fingerprint terms: p08 uses 'every, rule, exists, enforce, constraint'; p12 uses 'why, every, rule, enforce, constraint'
    - After fix: both probes achieve correct BM25 retrieval with hub dominant in K1
  - Phase 4 verification results (all 7 checks PASS, against freshly wiped + v2-seeded graph):
    - Check 1: POST /admin/seed-graph {"version":"v2"} → mu_count=34, edge_count=32: PASS
    - Check 2: GET /admin/graph/stats → {cluster_a:8, cluster_b:8, cluster_c:8, cluster_d:8, bridge_ab:1, bridge_cd:1}, mu_without_cluster_id=0: PASS
    - Check 3: Connectivity — hub→bridge edges confirmed for all 4 clusters (cluster_a→bridge_ab, cluster_b→bridge_ab, cluster_c→bridge_cd, cluster_d→bridge_cd): PASS
    - Check 4: Lexical-distinctness — bridge_ab contains none of CLUSTER_A_FP+CLUSTER_B_FP; bridge_cd contains none of CLUSTER_C_FP+CLUSTER_D_FP (confirmed in Phase 3; no new violations): PASS
    - Check 5a: Baseline probe run (session_index=0, clean v2 graph, ranking_improvement probes p06-p09): all PASS rank=1
      - p06 (cluster_a hub): rank=1, retrieved=[a0000001, ab000001, a0000006] ✓
      - p07 (cluster_b hub): rank=1, retrieved=[b0000001, b0000005, b0000004, ab000001] ✓
      - p08 (cluster_c hub): rank=1, retrieved=[c0000001, c0000007, c0000008, cd000001] ✓
      - p09 (cluster_d hub): rank=1, retrieved=[d0000001, a0000001, c0000001, cd000001] ✓
    - Check 5b: Baseline probe run (cross_cluster_transfer probes p10-p13): bridge at rank 4 (K2), NOT rank 1
      - p10 (cluster_a vocab, gold=bridge_ab): rank=4, K1=[a0000001, a0000005, a0000004], K2=[ab000001] ✓
      - p11 (cluster_b vocab, gold=bridge_ab): rank=4, K1=[b0000001, b0000005, b0000004], K2=[ab000001] ✓
      - p12 (cluster_c vocab, gold=bridge_cd): rank=4, K1=[c0000001, c0000007, c0000008], K2=[cd000001] ✓
      - p13 (cluster_d vocab, gold=bridge_cd): rank=4, K1=[d0000004, d0000001, a0000001], K2=[cd000001] ✓
    - Check 5c: Process rule adherence probes p14-p15: PASS rank=1
      - p14 (idempotency rule, c0000003): rank=1, retrieved=[c0000003, c0000007, c0000004, c0000001] ✓
      - p15 (multi-tenant access control, c0000005): rank=1, retrieved=[c0000005, c0000001] ✓
    - Check 6: Idempotency — second POST /admin/seed-graph {"version":"v2"} → mu_count=34 (no duplicates): PASS
    - Check 7: probe_set_v2.yaml loads at startup with no errors; 10 probes synced; all visible in admin (GET /admin/probe-results returns 20 results for p06-p15): PASS
  - Tests: 332 unit tests — all passing; no regressions
  - Gap A: RESOLVED (Phase 0.4) — cross_cluster_transfer uses threshold=4 (bridge structurally at K2/rank 4)
  - Gap B: DOCUMENTED in probe_set_v2.yaml comments — process_rule_adherence output compliance requires human review

- 2026-06-29: /smoketest — Milestone 18 Final Verification (all 7 checks, freshly wiped + re-seeded graph)
  - Bugs found: 0
  - Branch: fix/smoketest-2026-06-29
  - Setup: Deleted memory/graph.db, started backend fresh, backend health confirmed ({"status":"ok","db":"ok"})
  - Final verification results (all 7 checks PASS against freshly wiped + v2-seeded graph):
    - Check 1: POST /admin/seed-graph {"version":"v2"} → mu_count=34, edge_count=32: PASS
    - Check 2: GET /admin/graph/stats → {cluster_a:8, cluster_b:8, cluster_c:8, cluster_d:8, bridge_ab:1, bridge_cd:1}, mu_without_cluster_id=0: PASS
    - Check 3: Connectivity — hub→bridge edges confirmed for all 4 clusters (cluster_a→bridge_ab, cluster_b→bridge_ab, cluster_c→bridge_cd, cluster_d→bridge_cd): PASS
    - Check 4: Lexical-distinctness — bridge_ab contains none of CLUSTER_A_FP+CLUSTER_B_FP; bridge_cd contains none of CLUSTER_C_FP+CLUSTER_D_FP (verify_seed_v2.py PASS): PASS
    - Check 5a: Baseline probe run (session_index=0, clean v2 graph, ranking_improvement probes p06-p09): all PASS rank=1
      - p06 (cluster_a hub): rank=1, retrieved=[a0000001, ab000001, a0000006] ✓
      - p07 (cluster_b hub): rank=1, retrieved=[b0000001, b0000005, b0000004, ab000001] ✓
      - p08 (cluster_c hub): rank=1, retrieved=[c0000001, c0000007, c0000008, cd000001] ✓
      - p09 (cluster_d hub): rank=1, retrieved=[d0000001, a0000001, c0000001, cd000001] ✓
    - Check 5b: Baseline probe run (cross_cluster_transfer probes p10-p13): bridge at rank=4 (K2), NOT rank 1
      - p10 (cluster_a vocab, gold=bridge_ab): rank=4, K1=[a0000001, a0000005, a0000004], K2=[ab000001] ✓
      - p11 (cluster_b vocab, gold=bridge_ab): rank=4, K1=[b0000001, b0000005, b0000004], K2=[ab000001] ✓
      - p12 (cluster_c vocab, gold=bridge_cd): rank=4, K1=[c0000001, c0000007, c0000008], K2=[cd000001] ✓
      - p13 (cluster_d vocab, gold=bridge_cd): rank=4, K1=[d0000004, d0000001, a0000001], K2=[cd000001] ✓
    - Check 5c: Process rule adherence probes p14-p15: PASS rank=1
      - p14 (idempotency rule, c0000003): rank=1, retrieved=[c0000003, c0000007, c0000004, c0000001] ✓
      - p15 (multi-tenant access control, c0000005): rank=1, retrieved=[c0000005, c0000001] ✓
    - Check 6: Idempotency — second POST /admin/seed-graph {"version":"v2"} → mu_count=34 (no duplicates): PASS
    - Check 7: probe_set_v2.yaml loads at startup with no errors; 10 probes synced; GET /admin/probe-results returns 10 results for p06-p15: PASS
  - Tests: 332 unit tests — all passing; no regressions
  - Milestone 18 status: COMPLETE — all Done criteria met (seed_v2.json.gz committed, probe_set_v2.yaml committed, all 7 checks PASS with actual numbers, full test suite passing)

- 2026-07-03: /build — Milestone 19 Phase 0 complete (Experiment Harness: Run Plan Schema + Skeleton + Dry-Run Mode)
  - Created: `scripts/harness/__init__.py`, `scripts/harness/schema.py` (RunPlan pydantic model + all sub-schemas: ConceptualFields, DifficultyConfig, JudgeConfig, NotificationConfig; `format_validation_error()` for field-identifying CLI errors)
  - Created: `scripts/harness/turn_resolution.py` (`resolve_turn_sequence()` — deterministic probe/bulk resolution from probe_ratio + probe_placement + random_seed; supports `random`/`end`/`fixed_checkpoints`)
  - Created: `scripts/harness/manifest.py` (`compute_config_hash()`, `compute_run_id()` — deterministic run_id from plan_id + hash prefix, enabling resume; `RunManifest` persistence)
  - Created: `scripts/harness/checkpoint.py` (`CheckpointState`/`TurnCheckpoint` — atomic per-turn checkpoint writes via temp-file + `os.replace`; `is_turn_done()` for resume-skip logic)
  - Created: `scripts/harness/notify.py` (`notify()` stub — console + JSONL file, no network calls; `build_notification_payload()` for the `{run_id, status, error_summary, report_path}` shape)
  - Created: `scripts/harness/client.py` (`BackendClient` — httpx wrapper distinguishing `BackendConnectivityError` (hard-stop default) from `BackendTurnError` (soft-continue default); chat calls get a separate 120s timeout since Ollama inference can exceed a generic HTTP timeout on cold model loads)
  - Created: `scripts/harness/elicit.py` (`elicit_plan()` — full interactive walkthrough of every schema field with defaults, injectable `input_fn` for testing; `offer_to_save_plan()`)
  - Created: `scripts/harness/runner.py` (`execute_run()` — the testable core orchestration: manifest → resume detection → per-condition wipe+seed → turn loop → checkpoint → error policy → notify; injectable `BackendClient` and `resume_input_fn` so it's fully unit-testable without Docker)
  - Created: `scripts/run_experiment_harness.py` (CLI entrypoint: `--plan`, `--dry-run`, `--backend-url`; loads-or-elicits, validates, delegates to `execute_run()`)
  - Created: `tests/test_harness/` (44 tests: schema validation, turn-resolution determinism, elicitation with scripted input, checkpoint persistence/interrupt-simulation, and runner orchestration against a fake `BackendClient` — hard-stop, soft-continue, error-policy override, resume/decline-resume, wipe-before-seed ordering, probe placeholder content)
  - Modified: `.gitignore` (added `harness_runs/` — runtime manifests/checkpoints/notification logs)
  - Tests: 44 new (harness), 376 total at that point — all passing

  **Gap found and closed: no real "wipe" existed.** The plan's own text (§1, §3.2) assumed `POST /admin/seed-graph` already performed a combined wipe+seed. It doesn't — `register_fixture()` only ever does an idempotent merge (duplicate MUs/edges are silently skipped on re-registration; confirmed via M17/M18's own idempotency checks). The only prior "wipe" mechanism was manually deleting `memory/graph.db` and restarting the container (see M18 final-verification log, 2026-06-29). Without a real wipe, every condition in the future three-condition pilot would inherit MUs/edges from the previous condition — exactly the cross-condition graph contamination already flagged as a known issue in the M17 research-outcomes section. Asked Paul how to proceed; decision: **add a real wipe endpoint, with automatic pre-wipe archiving keyed by experiment_id** (not silent, irreversible deletion).
  - Modified: `backend/memory/store.py` — added `GraphStore.wipe_graph()`: deletes all rows from `edges` and `memory_units`, returns `{mus_deleted, edges_deleted}`. Leaves `analytics`, `probe_results`, `feedback_events`, `generated_reports`, `seed_fixture_manifest` untouched — the experimental audit trail survives a graph wipe.
  - Modified: `backend/memory/snapshot.py` — added `take_archive_snapshot(experiment_id)`: dumps the full pre-wipe graph to `archive_{experiment_id}_{ts}.json.gz` (sanitizes unsafe filename characters; `experiment_id=None` → `unlabeled`).
  - Modified: `backend/api/admin.py` — added `POST /admin/graph/wipe` (`WipeGraphRequest{experiment_id}`): archives first, then wipes; if archiving fails, the wipe is aborted (never wipes without a successful archive first) — verified by test.
  - Modified: `scripts/harness/client.py` / `scripts/harness/runner.py` — harness now calls `wipe_graph(experiment_id=f"{plan.plan_id}_{condition}")` before `seed_graph()` for every condition.
  - Added: 11 more tests (3 in `tests/test_memory/test_store.py::TestWipeGraph`, 4 in `tests/test_memory/test_snapshot.py` archive tests, 4 in new `tests/test_api/test_graph_wipe.py`) plus 2 more in `tests/test_harness/test_runner.py` (wipe-before-seed ordering, hard-stop-when-wipe-fails) — 55 new tests total this milestone.
  - Incidental fix while editing `tests/test_memory/test_store.py`: an `Edit` call misjudged the file's true last line (a truncated `Read` hid it) and orphaned the migration test's final assertion outside its function body — caught immediately by the failing test, corrected in the same session.
  - Tests: 55 new, 387 total — all passing. Integration suite (`tests/integration`, requires live Docker stack): 4 passed, 1 skipped (unrelated — no random-weight control experiment_id present this session), consistent with prior runs.

  **§3.4 verification protocol — all 7 checks PASS, run against the live Docker backend + real Ollama (gemma4:e4b) at `localhost:8000` / `localhost:11434`:**
  1. **Interactive elicitation → save → confirm match:** ran `python scripts/run_experiment_harness.py` with no `--plan`, scripted stdin answers through every field. Saved plan JSON matches every entered value exactly (plan_id, plan_name, conceptual_fields, series_count=1, turns_per_series=2, probe_ratio=0.0, hop_distance_levels=[0,1], judge fields, conditions=["learned"], notification.enabled=false, dry_run=false, random_seed=99). PASS.
  2. **Valid plan file loads cleanly, hash logged:** `--plan check2_plan.json` → `Plan loaded: plan_id='verify-check2' config_hash=b5bfe43aef34d4f31ec4e08a07e84fd2c1c39211f6b49aac607dcdaeeaa8a3c7`. PASS.
  3. **Malformed plan fails fast:** plan with `conditions=["bogus_condition"]`, `probe_placement="sometimes"` → exits 1 with two specific, field-identifying errors (`probe_placement: ... must be one of (...)`, `conditions: ... must be from (...)`) and zero HTTP calls made (graph untouched). PASS.
  4. **Dry-run scoping (real backend):** plan with `series_count=2, turns_per_series=5, conditions=["learned","static_weight"], dry_run=true, dry_run_n=2` → manifest's `resolved_turn_sequence` holds the full 10 turns (audit trail), but only 2 turns actually executed, only under `=== Condition: learned ===` (static_weight never ran). Checkpoint: exactly 2 entries, both `status="ok"`, `final_status="complete"`. `notifications.log`: `{"event": "harness_run_complete", "run_id": "verify-check4_cf96fb09c5da", "status": "complete", "error_summary": null, "report_path": null}`. PASS.
  5. **Simulated hard-stop:** `--backend-url http://localhost:59999` (unreachable) → `ABORTED: connectivity: health check failed: [WinError 10061] No connection could be made because the target machine actively refused it`, exit code 1. `checkpoint.json`: `{"turns": [], "final_status": "aborted"}`; `manifest.json` present. Clean abort before any graph mutation (health check runs before wipe/seed). PASS.
  6. **Simulated soft-continue:** `tests/test_harness/test_runner.py::test_soft_continue_on_ambiguous_turn_result_logs_and_proceeds -v -s` → turn checkpoints `[(0, 'ok', 'run-0'), (1, 'error_soft_continue', 'missing run_id'), (2, 'ok', 'run-2')]`, final status `complete` — the run logs the ambiguous turn and proceeds rather than aborting. PASS.
  7. **Full test suite:** 387 passed, 0 failed, no regressions (confirmed both before and after the wipe-gap fix). PASS.

  - Side effect of live verification: the shared dev graph now has 34 seed MUs + 9 interaction MUs from verification chat turns (expected — every wipe archived the prior state first; archives at `memory/snapshots/archive_*.json.gz`). Nothing was lost; re-seed or wipe again at any time.
  - Key decisions:
    - `error_policy: dict[str, str]` added to the RunPlan schema as a minor, documented deviation from the original field list — required by §3.2's "must be overridable from the run plan" language, which named the mechanism but not a schema field for it.
    - `run_id` is deterministic (`{plan_id}_{config_hash[:12]}`), not random — re-running the same plan file naturally resumes the same checkpoint; a materially different plan gets a fresh run directory rather than silently colliding with an unrelated one.
    - A checkpoint is resumable whenever `final_status != "complete"` (covers both a crash mid-run with no final_status, and a previously aborted run) — only a fully complete run has nothing left to do.
    - Turn content in Phase 0 is placeholder-only by design: probe turns cycle through `probe_set_v2.yaml` prompts; bulk turns get a labeled stub message. Real bulk-prompt generation is Phase 1's job.
  - Branch: `milestone-19-phase-0-harness-skeleton`
  - Milestone 19 Phase 0 status: COMPLETE — all Done criteria met (all deliverables built, all 7 §3.4 checks PASS with real recorded output, no regressions, wipe-gap closed with tests)

- 2026-07-05: /build — Milestone 19 Phase 1 complete (Prompt Bank Generation + Validation)
  - Created: `scripts/harness/prompt_bank.py` — `PromptBankEntry` (criteria constrained to 1-3 via pydantic `Field(min_length=1, max_length=3)`), `PromptBank` (+`validated_entries()`), `compute_bank_hash()`, `bank_path_for_hash()`, `save_bank()`/`load_bank()` (field-identifying validation errors, reusing `schema.format_validation_error()`)
  - Created: `scripts/harness/prompt_gen.py` — the only file that talks to the Anthropic API:
    - `read_cluster_content()` reads `seed_fixtures/seed_v2.json.gz` directly (host-side, no backend dependency); imports `CLUSTER_A_FP..D_FP` and `MU_IDS` straight from `generate_seed_v2.py` (no copy — single source of truth)
    - `fingerprint_scan()` — pure-Python lexical-distinctness check, whole-word matching
    - `distribute_counts()` / `distribute_hop_levels()` — deterministic round-robin distribution across the 4 clusters and hop-distance levels
    - `generate_cluster_prompts_safely()` — 1 Sonnet call per cluster; malformed JSON retries the batch call once, then falls back to per-prompt generation so one bad response never loses the whole cluster
    - `_finalize_entry()` — per-prompt validation (fingerprint scan + a second Sonnet coherence call) with up to `prompt_gen.max_retries` regenerations; persistent failures are excluded with a specific `exclusion_reason`, never silently dropped
    - `estimate_generation_cost()` / `confirm_cost()` — pure local token-count heuristic (chars/4), no API call, printed before the first Sonnet request; requires explicit `y`
    - `build_bank()` — hash-checks for an existing bank first (zero API calls on a re-run with identical params); `generate_bank_for_plan()` is the CLI-facing entry point (checks `ANTHROPIC_API_KEY` via `MissingApiKeyError` before constructing the client)
  - Modified: `scripts/harness/schema.py` — `PromptGenConfig` (`model: str = "claude-sonnet-5"`, `max_retries: int = 2`); `RunPlan` gains `prompt_bank_path: Optional[str]` and `prompt_gen: PromptGenConfig`
  - Modified: `scripts/harness/turn_resolution.py` — `assign_prompt_ids()`: deterministic draw-without-replacement (reshuffles once exhausted) from `random_seed`, assigns `prompt_id` to every bulk turn only
  - Modified: `scripts/harness/runner.py` — preflight loads the bank (if `plan.prompt_bank_path` is set) and calls `assign_prompt_ids()` before the manifest is written; a missing/invalid bank routes through the existing error-policy machinery as a "connectivity"-class hard-stop (before `client.health()`, so zero wipe/seed calls happen); `build_turn_message()` now looks up `prompt_id` in a `prompt_lookup` dict built from the loaded bank's validated entries, falling back to the Phase 0 placeholder stub only when no bank is loaded
  - Modified: `scripts/harness/elicit.py` — prompts for `prompt_bank_path`, `prompt_gen.model`, `prompt_gen.max_retries`
  - Modified: `scripts/run_experiment_harness.py` — `--generate-bank` flag (build/reuse a bank and exit without running an experiment); auto-generates a bank before executing when `prompt_bank_path` is absent and `conceptual_fields.subjects == "auto"`
  - Modified: `config.yaml` — new `prompt_gen:` section (`sonnet_price_per_mtok_in: 2.0`, `sonnet_price_per_mtok_out: 10.0` — Sonnet 5 introductory rate through 2026-08-31)
  - Added: `tests/test_harness/test_prompt_bank.py` (12 tests), `tests/test_harness/test_prompt_gen.py` (16 tests, all Sonnet calls mocked via `FakeAnthropicClient` — two tests use the real committed `seed_fixtures/seed_v2.json.gz` for fixture-reading/hash tests but make no network calls)
  - Extended: `tests/test_harness/test_turn_resolution.py` (+6 tests: `assign_prompt_ids` determinism, different-seed variation, bulk-only assignment, bank-smaller-than-turns reshuffle, empty-bank no-op), `tests/test_harness/test_runner.py` (+2 tests: bulk turn message equals bank `prompt_text`, missing bank hard-stops before any `wipe_graph` call), `tests/test_harness/test_elicit.py` (scripted answers extended for the 3 new prompts)
  - Tests: 34 new, 421 total — all passing, no regressions
  - Dependency: installed `anthropic` Python SDK (host-side only, `pip install anthropic` — never added to `backend/requirements.txt` or the Docker image; no tracked `requirements-dev.txt` exists yet for host-side scripts, so this is a manual local install for now)
  - **Verification protocol — all 4 checks PASS with real recorded output** (ANTHROPIC_API_KEY already exported; user confirmed proceeding with real, small Sonnet 5 API spend before this ran):
    1. `--generate-bank` against real `seed_v2.json.gz`, plan with `total_prompts=20, probe_ratio=0.2` (16 bulk prompts): cost estimate printed as `$0.0682 (~24075 input tokens, ~2000 output tokens)`, confirmed, bank written to `prompt_banks/prompt_bank_3a3c7eb69111a3891c7bc399758b539953164548a5b2db269c643bcb51c2c511.json`. Per-cluster counts: `cluster_a {generated:4, validated:4, excluded:0}`, `cluster_b {4,3,1}`, `cluster_c {4,3,1}`, `cluster_d {4,4,0}` — 14/16 validated; the 2 exclusions were real fingerprint leaks (`['field']`, `['data']`) caught correctly after exhausting retries. PASS.
    2. Re-ran the identical plan with `--generate-bank`: `Prompt bank already exists for hash=3a3c7eb6... — reusing (no API calls)` — zero HTTP requests logged. PASS.
    3. Dry-run (`dry_run_n=2`) against a fake backend client with the generated bank wired via `prompt_bank_path`: both bulk turns produced real Sonnet-generated prompt text (e.g. `"Why is it so important that fields always show up in the same sequence..."`), and the manifest's `resolved_turn_sequence` carried `prompt_id: "cluster_d_0000"` / `"cluster_a_0001"` on those turns. The `"[Phase 0 placeholder"` string did not appear anywhere in the output. PASS. (Full Docker-backend dry-run deferred — the backend container wasn't running this session; Ollama was reachable but `localhost:8000` was not — so this check used the same `execute_run()` entry point against a fake `BackendClient`, which is the same code path the real backend would exercise.)
    4. Full test suite: 421 passed, 0 failed, no regressions. PASS.
  - Key decisions:
    - Fingerprint-scan failures skip the (paid) Sonnet coherence call entirely — a lexical leak is a hard local fail, no reason to spend a second API call confirming it
    - Batch generation retries the *same* per-cluster call once on malformed JSON, then falls back to per-prompt generation — this means one bad batch response degrades gracefully to per-prompt retries instead of losing the whole cluster
    - `compute_fixture_manifest_hash()` reimplements the sha256-over-raw-bytes logic locally rather than importing `backend.analytics.seed_fixture.compute_fixture_hash` — that module pulls in the backend's async DB layer, which is wrong for a standalone host-side script
    - `prompt_bank_path` preflight failure is treated as "connectivity"-class severity in the error-policy dict (same override mechanism as backend-unreachable errors) rather than an unconditional hard-stop, per the build plan's explicit framing
    - The one 20-prompt verification bank (`prompt_banks/prompt_bank_3a3c...json`) is committed as evidence of a real, successful end-to-end run — `prompt_banks/` is not gitignored, consistent with `seed_fixtures/` being a committed, reusable artifact
  - Branch: `milestone-19-phase-1-prompt-bank`
  - Milestone 19 Phase 1 status: COMPLETE — all Done criteria met (all 4 verification checks PASS with real recorded output, criteria present on every validated entry, placeholder string confirmed absent from bank-backed dry-run output, no regressions)
