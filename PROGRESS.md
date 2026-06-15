# Progress

## Current state

Project: Local Corvus — local LLM learning research platform  
Phase: Planning complete  
Current milestone: 1 — Foundation (not started)  
Next step: Create `config.yaml` with the full hyperparameter registry (see BUILD-PLAN.md)

## Session log

- 2026-06-14: Architecture document drafted — `.agents/local_corvus_architecture_v0.1.md`
- 2026-06-15: Build plan created — `BUILD-PLAN.md`, 4 milestones defined

## What exists

- `backend/main.py` — minimal FastAPI skeleton with `/health`, `/ollama-health`, `/chat`
- `backend/Dockerfile` + `docker-compose.yml` — backend service only, working
- `backend/requirements.txt` — fastapi, uvicorn, httpx
- `.env` — `OLLAMA_HOST=http://host.docker.internal:11434`
- `.agents/local_corvus_architecture_v0.1.md` — full architecture design document

The skeleton connects to Ollama but has no UM gate, no graph store, no session management, and no memory. The `/chat` endpoint calls Ollama directly and discards the response after returning it.

## Decisions made

- **Single edge type:** weight + directionality only, no semantic labels
- **No embeddings in v1:** BM25 keyword retrieval only
- **No external API calls in v1:** all tools are local
- **Ollama runs natively on Windows**, not in Docker — app container reaches it at `host.docker.internal:11434`
- **aiosqlite** for async-safe SQLite access (stdlib `sqlite3` is not async-safe)
- **Admin UI is a separate React app** (port 3001), not routes in the chat UI

## Open questions

1. **Weight initialization value** — flat 0.5 vs. source-type-based prior? Decide before Milestone 3 baseline.
2. **Decay function shape** — linear / exponential / sigmoid? When does decay trigger? Decide before Phase 4 results are meaningful.
3. **RL reward distribution along activated edges** — uniform / proportional to position / exponential decay from output? Build Milestone 4 with uniform as default; revisit after first learning run.
4. **First experiment task definition** — weather tool (primary experiment in architecture doc) requires an external API call which is out of scope for v1. Decide on a local-only task during Milestone 3.

## Upcoming milestones

1. **Foundation** — UM Gate + Graph Store + Session Management (2 sessions)
2. **Memory Engine** — BM25 Retrieval + Traversal Funnel + Analyzer/Extractor (2-3 sessions)
3. **Tool System** — Universal Tool Interface + File Tool + Task Suite + Baseline Measurements (2-3 sessions)
4. **Learning** — Simple RL + Feedback Capture + Admin UI + Chat UI (3-4 sessions)
