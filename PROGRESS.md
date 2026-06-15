# Progress

## Current state

Project: Local Corvus — local LLM learning research platform  
Phase: Building  
Current milestone: 2 — Memory Engine — COMPLETE  
Next step: Review and merge Milestone 2 PR, then start Milestone 3 (Tool System)

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

## What exists

- `backend/main.py` — FastAPI app with UM-gated /chat, /health, /ollama-health
- `backend/api/chat.py` — POST /chat: session management, UM gate (in/out), MU persistence, audit log
- `backend/gate/um.py` — UniversalMessage schema + create_um() factory
- `backend/gate/sanitize.py` — sanitize_in() / sanitize_out() (pure, stateless)
- `backend/gate/audit.py` — append_audit() → /app/memory/audit.log (JSONL, append-only)
- `backend/memory/store.py` — GraphStore: async CRUD for memory_units + edges (aiosqlite)
- `backend/core/session.py` — create_session() / get_session() (in-memory)
- `backend/Dockerfile` + `docker-compose.yml` — backend service, ./memory:/app/memory volume
- `config.yaml` — full hyperparameter registry (single source of truth)
- `tests/test_gate/` — 20 tests for sanitize + um/audit
- `tests/test_memory/` — 12 tests for GraphStore CRUD

## Decisions made

- **Single edge type:** weight + directionality only, no semantic labels
- **No embeddings in v1:** BM25 keyword retrieval only
- **No external API calls in v1:** all tools are local
- **Ollama runs natively on Windows**, not in Docker — app container reaches it at `host.docker.internal:11434`
- **aiosqlite** for async-safe SQLite access (stdlib `sqlite3` is not async-safe)
- **Admin UI is a separate React app** (port 3001), not routes in the chat UI
- **flags field is free strings**, not an enum — patterns are embedded in the flag text for debugging visibility
- **Connection-per-operation** pattern for aiosqlite (not pooling) — appropriate for local research volume and avoids thread lifecycle bugs

## Open questions

1. **Weight initialization value** — flat 0.5 vs. source-type-based prior? Decide before Milestone 3 baseline.
2. **Decay function shape** — linear / exponential / sigmoid? When does decay trigger? Decide before Phase 4 results are meaningful.
3. **RL reward distribution along activated edges** — uniform / proportional to position / exponential decay from output? Build Milestone 4 with uniform as default; revisit after first learning run.
4. **First experiment task definition** — weather tool (primary experiment in architecture doc) requires an external API call which is out of scope for v1. Decide on a local-only task during Milestone 3.

## Upcoming milestones

1. ~~**Foundation** — UM Gate + Graph Store + Session Management~~ ✅ COMPLETE
2. ~~**Memory Engine** — BM25 Retrieval + Traversal Funnel + Analyzer/Extractor~~ ✅ COMPLETE
3. **Tool System** — Universal Tool Interface + File Tool + Task Suite + Baseline Measurements (2-3 sessions)
4. **Learning** — Simple RL + Feedback Capture + Admin UI + Chat UI (3-4 sessions)
