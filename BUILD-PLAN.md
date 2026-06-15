# Build Plan: Local Corvus

Generated: 2026-06-15  
Based on: `.agents/local_corvus_architecture_v0.1.md` (v0.1, 2026-06-14)

---

## Current State

A minimal FastAPI skeleton exists (`backend/main.py`) with three endpoints:
- `GET /health` — returns OK
- `GET /ollama-health` — pings Ollama
- `POST /chat` — passes message directly to Ollama with no UM gate, no memory, no session tracking

docker-compose.yml has only the backend service. No frontend, no admin UI, no graph store yet.

This plan builds the complete system in four phases, treating the skeleton as scaffolding that gets replaced rather than extended.

---

## System Architecture

```
┌─────────────────────────────────────────────────────────────┐
│                      Docker Compose                          │
│                                                             │
│  ┌─────────────┐   ┌─────────────┐   ┌──────────────────┐  │
│  │  Chat UI    │   │  Admin UI   │   │  Python Backend  │  │
│  │  (React)    │   │  (React)    │   │  (FastAPI)       │  │
│  │  port 3000  │   │  port 3001  │   │  port 8000       │  │
│  └──────┬──────┘   └──────┬──────┘   └────────┬─────────┘  │
│         │                 │                    │            │
│         └─────────────────┴──── HTTP ──────────┘            │
│                                                │            │
│              ┌─────────────────────────────────▼──────────┐ │
│              │           Core Orchestrator                │ │
│              │  session → retrieve → prompt → llm → tool  │ │
│              └──────────────────────────────────────────┬─┘ │
│                                                         │   │
│   ┌─────────────┐  ┌─────────────┐  ┌─────────────┐    │   │
│   │  UM Gate    │  │  Memory     │  │  Tool       │◀───┘   │
│   │  (trust     │  │  Engine     │  │  Registry   │        │
│   │  boundary)  │  │  (retrieval │  │  (universal │        │
│   └──────┬──────┘  │   + extract)│  │  dispatch)  │        │
│          │         └──────┬──────┘  └─────────────┘        │
│          │                │                                 │
│   ┌──────▼──────┐  ┌──────▼──────┐  ┌─────────────┐        │
│   │  Audit Log  │  │  Graph      │  │  Analytics  │        │
│   │  (append-   │  │  Store      │  │  Run Log +  │        │
│   │   only)     │  │  (SQLite)   │  │  Snapshots  │        │
│   └─────────────┘  └─────────────┘  └─────────────┘        │
│                                                             │
│   /app/memory  (named Docker volume — persists on restart)  │
└─────────────────────────────────────────────────────────────┘
                             │
                             │ host.docker.internal:11434
                             ▼
                  ┌─────────────────────┐
                  │  Ollama (native)    │
                  │  Gemma 4 E4B        │
                  │  RTX 5060 GPU       │
                  └─────────────────────┘
```

### Why the components are separated this way

**UM Gate is its own module** because it's a policy enforcer, not a data transformer. Every trust boundary crossing goes through it — user input, tool calls, memory reads, LLM responses. Keeping it separate means nothing ever accidentally bypasses it. You can also unit-test every sanitization rule in isolation, which matters when you're studying what the model produces.

**Memory Engine is separate from Graph Store** because retrieval logic (BM25, traversal, extraction) changes between experiments — but the raw database schema must not. Separating them lets you swap the retrieval algorithm without touching the tables.

**Tool Registry is separate from the Orchestrator** because the orchestrator shouldn't know which tools exist. It hands off a tool call request and gets back a result. Adding a new tool is a config entry + a new file, not a change to the main loop.

**Analytics is write-once, read-many** — the main loop writes run records, the Admin UI reads them. They share no code paths. This means you can query the run log without worrying about breaking an active session.

---

## Component Breakdown

### gate/ — UM Gate (Trust Boundary Enforcement)

**Purpose:** Wrap, sanitize, and log every message that crosses a trust boundary. Runs before and after every LLM call, tool call, and memory read/write.

**Why it's separate:** It's the security and observability layer. Every other component trusts that data arriving at it has already passed through the gate. If this is merged into the orchestrator, you'll shortcut it under deadline pressure.

**Key files:**
- `backend/gate/um.py` — UM record schema (dataclass or Pydantic model) + `create_um()` factory function
- `backend/gate/sanitize.py` — `sanitize_in(content, boundary)` and `sanitize_out(content, boundary)` as pure, stateless functions. No side effects; safe to unit-test.
- `backend/gate/audit.py` — `append_audit(um_record)` writes to `/app/memory/audit.log` (append-only JSONL)

**Boundaries gated:**

| Boundary | Direction | Gate |
|---|---|---|
| User input → Agent | in | sanitize_in |
| Tool result → Agent | in | sanitize_in |
| Memory read → Prompt | in | sanitize_in |
| Agent → Tool call | out | sanitize_out |
| Agent → Response | out | sanitize_out |
| Memory write ← Agent | out | sanitize_out |

---

### memory/ — Memory Engine + Graph Store

**Purpose:** Store every LLM interaction as a Memory Unit (MU), maintain a weighted directed graph of MU relationships, and retrieve relevant MUs for each new prompt.

**Why it's separate:** The graph store schema is the stable substrate — it must not change when algorithms change. The retrieval logic is experimental — it will change. Separating them enforces that boundary.

**Key files:**
- `backend/memory/store.py` — SQLite CRUD for `memory_units` and `edges` tables. The only file that talks to the database.
- `backend/memory/retrieval.py` — BM25 keyword index + traversal funnel (→ K₁ → K₂). Reads from store; writes nothing.
- `backend/memory/extractor.py` — Analyzer/Extractor: keyword+context filter → first/last sentence fallback → Gemma summarization if over token budget.
- `backend/memory/snapshot.py` — `take_snapshot()`: dumps full graph state to `/app/memory/snapshots/` as compressed JSON after each interaction.

**Graph schema:**

```sql
CREATE TABLE memory_units (
    mu_id TEXT PRIMARY KEY,
    created_at TEXT NOT NULL,
    source_type TEXT NOT NULL,  -- user_input | agent_output | tool_call | tool_result | prompt | system
    raw_content TEXT NOT NULL,
    last_activated_at TEXT,
    activation_count INTEGER DEFAULT 0,
    reference_count INTEGER DEFAULT 0,
    pruning_flag INTEGER DEFAULT 0
);

CREATE TABLE edges (
    edge_id TEXT PRIMARY KEY,
    source_mu_id TEXT NOT NULL REFERENCES memory_units(mu_id),
    target_mu_id TEXT NOT NULL REFERENCES memory_units(mu_id),
    created_at TEXT NOT NULL,
    weight REAL NOT NULL,
    direction TEXT NOT NULL,  -- forward | bidirectional
    last_traversed_at TEXT,
    traversal_count INTEGER DEFAULT 0
);
```

---

### core/ — Agent Orchestrator + Session Management

**Purpose:** The main agent loop. Takes a user message, runs the retrieve→prompt→llm→tool pipeline, and returns the response. Coordinates all other modules; contains no business logic of its own.

**Why it's separate:** The orchestrator is the traffic cop, not the worker. Keeping logic in the gate, memory engine, and tool registry means the orchestrator stays readable even as the system grows.

**Key files:**
- `backend/core/orchestrator.py` — `run_interaction(message, session_id)`: the full pipeline from user input to response. Calls gate, memory, tools, and analytics.
- `backend/core/session.py` — `create_session()`, `get_session(session_id)`: tracks active sessions. Sessions are lightweight — just an ID and start timestamp for now.

---

### tools/ — Universal Tool System

**Purpose:** Config-driven tool registration and dispatching. The orchestrator calls the dispatcher; the dispatcher finds the right tool; the tool executes. No tool-specific code in the orchestrator.

**Key files:**
- `backend/tools/base.py` — `Tool` base class and `ToolResult` schema (Pydantic)
- `backend/tools/registry.py` — `ToolRegistry`: loads tools from config, dispatches calls by name, enforces max iterations
- `backend/tools/file_tool.py` — `FileTool`: read/write files in `/app/memory/workspace/`. The first concrete tool.

**Tool registration flow:** Each tool is a file + an entry in `config.yaml`. The orchestrator never imports tool files directly.

---

### learning/ — Learning Algorithms

**Purpose:** Edge weight updaters. Receive the set of activated edges + a feedback signal; return a list of `(edge_id, new_weight)` pairs. Do not touch MU content or schema.

**Key files:**
- `backend/learning/interface.py` — `LearningAlgorithm` abstract base class with `update()` method
- `backend/learning/simple_rl.py` — Phase 4 implementation: thumbs up/down adjusts weights on activated edges by `weight_update_rate` (from config)

---

### analytics/ — Run Log

**Purpose:** Capture a complete record of every interaction for Admin UI queries and learning curve analysis.

**Key files:**
- `backend/analytics/run_log.py` — `capture_run(run_record)`: writes to `analytics` table in SQLite. `query_runs(filters)`: supports Admin UI queries.

**Run record schema (captured per interaction):**
```python
{
  "run_id": uuid,
  "session_id": uuid,
  "timestamp": iso8601,
  "input_mu_id": uuid,
  "output_mu_id": uuid,
  "tool_calls": list[uuid],
  "activated_mu_ids": list[uuid],
  "activated_edge_ids": list[uuid],
  "prompt_mu_id": uuid,
  "tokens_in": int,
  "tokens_out": int,
  "latency_ms": int,
  "feedback_signal": int | None,      # +1 / -1 / None
  "weight_updates": list[dict],        # [{edge_id, old_weight, new_weight}]
  "hyperparameters_snapshot": dict,    # registry state at time of run
  "algorithm": str
}
```

---

### api/ — Route Handlers

**Purpose:** Thin HTTP layer. Maps URLs to orchestrator calls. No business logic.

**Key files:**
- `backend/api/chat.py` — `POST /chat`, `POST /feedback`
- `backend/api/admin.py` — `GET /admin/runs`, `GET /admin/graph`, `GET /admin/snapshots`, `GET /admin/config`

---

### frontend/ and admin/ — React UIs

**Chat UI (frontend/):**
- `ChatWindow.jsx` — message thread display
- `MessageBubble.jsx` — single message with role styling
- `FeedbackButtons.jsx` — thumbs up/down per response (fires `POST /feedback`)

**Admin UI (admin/):**
- `RunLogTable.jsx` — filterable table of all run records
- `LearningCurveChart.jsx` — performance metric over time (x = interaction number, y = user-defined metric)
- `MemoryInspector.jsx` — browse MU graph; view edge weights; click to trace MU history

---

## Data Flows

### Chat interaction (full pipeline)

```
User message
      │
      ▼
POST /chat {message, session_id?}
      │
      ▼
UM Gate: sanitize_in
  ├── length check (config: max_input_length)
  ├── injection pattern scan (deterministic regex)
  ├── create UM record (source_type: user_input)
  └── write to audit.log
      │
      ▼
Orchestrator: session lookup / create
      │
      ▼
Memory Engine: retrieval
  ├── BM25 keyword index → candidate pool (top N per keyword)
  ├── candidates with 2+ keyword hits → sum edge weights → K₁
  └── follow directed edges from K₁ → sum weights → K₂
      │
      ▼
Analyzer/Extractor
  ├── keyword+context filter (matching sentence ± 1 neighbor)
  ├── fallback: first + last sentence per MU
  └── if condensed content > summarization_trigger_tokens → Gemma call
      │
      ▼
Prompt construction
  ├── [condensed context] + user message
  └── create prompt MU (edges to all contributing MUs)
      │
      ▼
UM Gate: sanitize_out (on prompt)
      │
      │ POST host.docker.internal:11434/v1/chat/completions
      ▼
Ollama / Gemma 4 E4B
      │
      ▼
UM Gate: sanitize_in (on LLM response)
└── create UM record (source_type: agent_output)
      │
      ├── [if tool_call in response] ──────────────────────┐
      │                                                    │
      │                               UM Gate: sanitize_out (on tool call)
      │                               create UM record (source_type: tool_call)
      │                               Tool Registry: dispatch to registered tool
      │                               Tool executes
      │                               UM Gate: sanitize_in (on result)
      │                               create UM record (source_type: tool_result)
      │                               ← loop back (max: tool_loop_max_iterations)
      │◀───────────────────────────────────────────────────┘
      │
      ▼
Analytics: capture run record + graph snapshot
      │
      ▼
UM Gate: sanitize_out (final response)
      │
      ▼
Return response to Chat UI
```

### Feedback and weight update

```
User clicks thumbs up/down
      │
      ▼
POST /feedback {run_id, signal: +1 | -1}
      │
      ▼
Load run record → get activated_edge_ids, activated_mu_ids
      │
      ▼
LearningAlgorithm.update(activated_edges, activated_mus, feedback)
      │
      ▼
Graph Store: batch write new edge weights
      │
      ▼
Run log: append weight_updates to run record
      │
      ▼
200 OK
```

---

## File Structure

```
local_corvus/
├── BUILD-PLAN.md
├── PROGRESS.md
├── config.yaml                    ← hyperparameter registry (single source of truth)
├── docker-compose.yml             ← updated each milestone to add services
├── .env
├── .env.example
│
├── backend/
│   ├── Dockerfile
│   ├── requirements.txt
│   ├── main.py                    ← FastAPI app + router registration
│   │
│   ├── api/                       ← HTTP layer (thin, no logic)
│   │   ├── __init__.py
│   │   ├── chat.py                ← POST /chat, POST /feedback
│   │   └── admin.py               ← GET /admin/* endpoints
│   │
│   ├── core/                      ← Agent loop
│   │   ├── __init__.py
│   │   ├── orchestrator.py        ← run_interaction() pipeline
│   │   └── session.py             ← session create/lookup
│   │
│   ├── gate/                      ← Trust boundary enforcement
│   │   ├── __init__.py
│   │   ├── um.py                  ← UM record schema + create_um()
│   │   ├── sanitize.py            ← sanitize_in() / sanitize_out() (pure functions)
│   │   └── audit.py               ← append_audit() → /app/memory/audit.log
│   │
│   ├── memory/                    ← Graph store + retrieval
│   │   ├── __init__.py
│   │   ├── store.py               ← SQLite CRUD (only file that touches the DB)
│   │   ├── retrieval.py           ← BM25 + traversal funnel
│   │   ├── extractor.py           ← Analyzer/Extractor (3-mode cascade)
│   │   └── snapshot.py            ← take_snapshot() → /app/memory/snapshots/
│   │
│   ├── learning/                  ← Learning algorithms
│   │   ├── __init__.py
│   │   ├── interface.py           ← LearningAlgorithm base class
│   │   └── simple_rl.py           ← Phase 4: thumbs up/down RL
│   │
│   ├── tools/                     ← Universal tool system
│   │   ├── __init__.py
│   │   ├── base.py                ← Tool base class + ToolResult schema
│   │   ├── registry.py            ← config-driven registration + dispatch
│   │   └── file_tool.py           ← file read/write (first concrete tool)
│   │
│   └── analytics/
│       ├── __init__.py
│       └── run_log.py             ← capture_run() + query_runs()
│
├── frontend/                      ← Chat UI (React)
│   ├── Dockerfile
│   ├── package.json
│   └── src/
│       ├── App.jsx
│       ├── components/
│       │   ├── ChatWindow.jsx
│       │   ├── MessageBubble.jsx
│       │   └── FeedbackButtons.jsx
│       └── pages/
│           └── Chat.jsx
│
├── admin/                         ← Admin UI (React)
│   ├── Dockerfile
│   ├── package.json
│   └── src/
│       ├── App.jsx
│       ├── components/
│       │   ├── RunLogTable.jsx
│       │   ├── LearningCurveChart.jsx
│       │   └── MemoryInspector.jsx
│       └── pages/
│           ├── Runs.jsx
│           ├── Graph.jsx
│           └── Config.jsx
│
├── tests/
│   ├── test_gate/
│   │   ├── test_um.py
│   │   └── test_sanitize.py
│   ├── test_memory/
│   │   ├── test_store.py
│   │   └── test_retrieval.py
│   ├── test_tools/
│   │   └── test_registry.py
│   └── test_learning/
│       └── test_simple_rl.py
│
└── memory/                        ← Docker volume mount (./memory:/app/memory)
    ├── graph.db                   ← SQLite (created on first run)
    ├── audit.log                  ← JSONL, append-only
    └── snapshots/                 ← compressed JSON, one file per interaction
```

---

## Build Order

### Why this order

The hardest, highest-risk piece is the **UM Gate + Graph Store**. Everything else plugs into this substrate. Build it first, prove it works, then layer on complexity. The general rule: build the thing that, if it's wrong, makes everything else wrong.

Retrieval comes second because it's the first thing that makes the agent actually useful for the research purpose — memory. The current skeleton has no memory at all.

Tools come third because they're simpler than learning (pure input/output, no state) and the experiment baseline must be measured before learning begins.

Learning comes last. You can't study learning without a baseline. You can't have a baseline without experiments. You can't have experiments without tools.

Admin UI is built alongside Phase 3 and 4, because it needs run log data to display and the run log exists from Phase 3 onward.

---

### Milestone 1 — Foundation: UM Gate + Graph Store + Session Management

**Why first:** This is the substrate for everything. The UM gate wraps every data flow; the graph schema is the hardest thing to change later; session management is required before any stateful interaction. Prove these work before adding anything on top.

**Build:**

1. `config.yaml` — full hyperparameter registry with all v1 values (see Section 8 of architecture doc). This file is the experimental record; every uncertain value goes here.
2. `gate/um.py` — `UniversalMessage` Pydantic model matching the architecture doc UM record fields. `create_um(content, boundary, direction, session_id, channel_id)` factory.
3. `gate/sanitize.py` — `sanitize_in(content, boundary) -> SanitizeResult` and `sanitize_out(content, boundary) -> SanitizeResult`. Pure functions, no I/O. Check: length (config: `max_input_length`), prompt injection patterns (deterministic regex list). Return sanitized content + any flags raised.
4. `gate/audit.py` — `append_audit(um: UniversalMessage)` writes to `/app/memory/audit.log` as JSONL. Opens in append mode; never truncates.
5. `memory/store.py` — `GraphStore` class with `create_mu()`, `get_mu()`, `create_edge()`, `get_edges_for_mu()`, `update_edge_weight()`, `update_mu_activation()`. SQLite via Python stdlib `sqlite3`. Initializes tables on first connect.
6. `core/session.py` — `create_session() -> str` (returns UUID), `get_session(session_id) -> Session | None`.
7. Update `main.py` + `api/chat.py` — wire the `/chat` endpoint through UM gate (sanitize_in on request, sanitize_out on response). Store user_input MU and agent_output MU on every call. Replace direct Ollama call with orchestrator stub.
8. Update `docker-compose.yml` — add `./memory:/app/memory` volume mount if not already present (it is). Confirm volume persists on restart.

**Tests:**
- `test_sanitize.py`: sanitize_in returns flags for a prompt injection string; sanitize_in passes a clean message; length limit triggers correct flag.
- `test_um.py`: create_um returns a valid UniversalMessage with all required fields; append_audit writes JSONL line that parses back correctly.
- `test_store.py`: create_mu round-trips to SQLite; create_edge creates edge with correct schema; update_edge_weight updates only the weight field.
- Manual: send a `/chat` message, verify MU records appear in SQLite and UM record appears in `audit.log`.

**Done when:**
- Every `/chat` call produces a user_input MU + agent_output MU in SQLite
- Both MUs appear in `audit.log` as JSONL
- Sanitize functions have full unit test coverage
- Graph store CRUD tests passing
- Container restart preserves graph.db content

**Risks:**
- ⚠ SQLite concurrent access — FastAPI is async; use a connection-per-request pattern or a dedicated connection pool with thread-safe access (Python `sqlite3` module is not async-safe by default — use `aiosqlite` or serialize through a queue)
- ? UM record `flags` field — decide whether flags are an enum list or free strings during implementation

**Estimate:** 2 sessions

---

### Milestone 2 — Memory Engine: BM25 Retrieval + Traversal Funnel + Analyzer/Extractor

**Why second:** The agent currently has no memory between turns. This milestone makes it a memory-enabled agent for the first time and produces the first measurable retrieval quality signal.

**Build:**

1. `memory/retrieval.py` — BM25 index over `raw_content` of all MUs. On each new prompt: tokenize, query index, get top N per keyword (N = `retrieval_keyword_pool_size`). Use `rank_bm25` Python package.
2. `memory/retrieval.py` — traversal funnel: candidates with 2+ keyword matches → sum first-level edge weights → top K₁ (`retrieval_level1_top_k`). From K₁, follow directed edges → sum second-level edge weights × directional multiplier → top K₂ (`retrieval_level2_top_k`).
3. `memory/extractor.py` — `extract(mus: list[MU], keywords: list[str], token_budget: int) -> str`. Mode 1: keep sentences containing a keyword ± 1 sentence neighbor. Mode 2 (fallback if Mode 1 yields < min_sentences): first + last sentence per MU. Mode 3 (if result > `summarization_trigger_tokens`): Gemma summarize call — result stored as new MU (type: system).
4. `core/orchestrator.py` — `run_interaction(message, session_id)`: call retrieval → extractor → build prompt with injected context → UM sanitize_out → Ollama call → UM sanitize_in. Create prompt MU connected to all contributing MUs. (No tool handling yet — tool loop added in Milestone 3.)
5. Update `api/chat.py` to call orchestrator instead of direct Ollama.

**Tests:**
- `test_retrieval.py`: populate 10 MUs; query with keyword present in 3 — verify candidate pool; verify traversal returns MUs connected by edges to keyword matches.
- `test_extractor.py`: MU with 5 sentences — verify Mode 1 returns only keyword-adjacent sentences. MU with no keyword matches — verify Mode 2 fallback. Content over token budget — verify Mode 3 triggers Gemma call (mock the Gemma call in tests).

**Done when:**
- After 5+ `/chat` turns, a new message about a prior topic retrieves relevant MUs
- Prompt includes condensed context in header (visible in logs)
- Prompt MU created with edges back to contributing MUs
- Extractor unit tests passing for all three modes

**Risks:**
- ⚠ BM25 index must be rebuilt or updated incrementally when new MUs are added — decide during implementation whether to rebuild on every retrieval call (simple, slow) or maintain an in-memory index (fast, requires cache invalidation)
- ⚠ Summarization call in Mode 3 creates a new MU — that new MU must itself be retrievable; verify it gets indexed
- ? How many BM25 keywords to extract per query? Start with top 5 tokens by TF-IDF score; tune in config.

**Estimate:** 2-3 sessions

---

### Milestone 3 — Tool System + First Experiment Task Suite + Baseline Measurements

**Why third:** Tools are prerequisite for the first experiment. The baseline must exist before learning begins — you need a "before" to compare against. Measure first, then change the system.

**Build:**

1. `tools/base.py` — `Tool` abstract class + `ToolResult` Pydantic model (fields: `tool_name`, `params_used`, `output`, `error`, `latency_ms`).
2. `tools/registry.py` — `ToolRegistry`: reads tool list from `config.yaml`. `dispatch(tool_name, params) -> ToolResult`. Enforces max iterations (`tool_loop_max_iterations`). Raises `ToolNotFoundError` for unknown tool names.
3. `tools/file_tool.py` — `FileTool`: reads/writes files in `/app/memory/workspace/`. Validates paths (no traversal outside workspace). Registered in config as `file_write` and `file_read`.
4. Update `core/orchestrator.py` — detect tool_call in Gemma response; UM sanitize_out on call; dispatch; UM sanitize_in on result; create tool_call + tool_result MUs; loop up to max iterations.
5. `analytics/run_log.py` — `capture_run(run_record: RunRecord)` writes to `analytics` table in `graph.db`. `query_runs(session_id=None, limit=100, offset=0)` for Admin UI.
6. `memory/snapshot.py` — `take_snapshot()`: dump all MUs + edges + weights as gzip-compressed JSON to `/app/memory/snapshots/{timestamp}.json.gz`.
7. Update orchestrator to call `capture_run()` and `take_snapshot()` after each interaction.
8. Define first experiment task suite: a set of N repeated tasks with measurable outcomes. Document the task definition and success metric in PROGRESS.md.
9. Run baseline: execute task suite N times with no learning active. Record run log. This is the control group.

**Tests:**
- `test_registry.py`: dispatch to a registered tool returns ToolResult; dispatch to unknown tool raises ToolNotFoundError; exceeding max_iterations raises ToolLoopError.
- Manual: send a message that triggers a tool call; verify tool_call MU + tool_result MU in graph; verify run record in analytics table; verify snapshot file written.

**Done when:**
- Tool calls route through UM gate and create correct MU types
- Run record captured after every interaction with all required fields
- Graph snapshot written after every interaction
- Baseline experiment complete: N run records logged, snapshots saved
- First experiment task type and success metric defined and documented

**Risks:**
- ⚠ Gemma's tool call format must match the OpenAI-compatible function-calling schema that Ollama exposes — verify against Ollama docs before wiring the dispatcher
- ? First experiment task definition — weather tool is mentioned in the architecture doc as the primary experiment tool, but an external weather API call is out of scope for v1. Decide on a local-only task (e.g., file creation + recall task) during this milestone.

**Estimate:** 2-3 sessions

---

### Milestone 4 — Learning Algorithm + Feedback Capture + Admin UI

**Why last:** The entire purpose of the platform. But it can only be built on top of working tools, run logs, and snapshots. The baseline from Milestone 3 is the comparison point.

**Build:**

1. `learning/interface.py` — `LearningAlgorithm` abstract base class:
   ```python
   def update(
       self,
       activated_edges: list[Edge],
       activated_mus: list[MU],
       feedback: int  # +1 or -1
   ) -> list[tuple[str, float]]:  # (edge_id, new_weight)
       raise NotImplementedError
   ```
2. `learning/simple_rl.py` — `SimpleRL(LearningAlgorithm)`: for each activated edge, apply `new_weight = clamp(old_weight + feedback * weight_update_rate, 0.0, 1.0)`. Log old and new weight.
3. `api/chat.py` — `POST /feedback {run_id, signal}` endpoint: load run record, call learning algorithm, write weight updates to graph store, append weight_updates to run record.
4. Admin UI (admin/): implement all three views using data from `/admin/runs`, `/admin/graph`, `/admin/snapshots`:
   - `RunLogTable.jsx` — paginated, filterable table of run records
   - `LearningCurveChart.jsx` — plot performance metric over interaction index (metric defined in Milestone 3)
   - `MemoryInspector.jsx` — force-directed graph or simple list of MUs + edge weights; click MU to see raw content and edge history
5. Chat UI (frontend/): minimal React app. ChatWindow, MessageBubble, FeedbackButtons (thumbs up/down fires `POST /feedback`). This is research tooling, not a product — function over form.
6. Update `docker-compose.yml` — add frontend (port 3000) and admin (port 3001) services with Dockerfiles.
7. Run post-learning experiment: repeat task suite N times with simple RL active. Compare run log metrics against baseline.

**Tests:**
- `test_simple_rl.py`: thumbs_up increases edge weights by `weight_update_rate`; thumbs_down decreases; weights clamp at 0.0 and 1.0; non-activated edges are unchanged.
- Manual: complete 10+ interactions with feedback; open Admin UI; verify run log shows feedback_signal values; verify learning curve shows trend; verify memory inspector shows updated edge weights.

**Done when:**
- Feedback signal updates edge weights in graph store
- Weight updates appear in run log `weight_updates` field
- Admin UI: all three views display real data from run log and graph store
- Chat UI: functional chat + feedback buttons
- Post-learning experiment run, results visible in Admin UI learning curve
- Qualitative comparison between baseline and post-learning metrics documented in PROGRESS.md

**Risks:**
- ⚠ **Open question — RL reward distribution (see below):** this design decision must be made before implementing `simple_rl.py`. The current placeholder (uniform weight update across all activated edges) is the simplest possible approach; document the choice and rationale.
- ⚠ Admin UI force-directed graph for MemoryInspector can become unrenderable with large MU counts — add a depth limit to the graph query from the start.

**Estimate:** 3-4 sessions

---

## Open Questions (Flagged — Not Resolved)

These cannot be answered without empirical data. Build Milestones 1-3, run the baseline, then decide. Record each decision and its rationale in PROGRESS.md at the moment it's made.

### 1. Weight Initialization Value

**Where it surfaces:** `config.yaml: initial_edge_weight`, `memory/store.py` (edge creation), every learning curve comparison.

**The question:** Should new edges start at a flat value (e.g., 0.5), a low value near zero (discovery mode), or a source-type-based prior (e.g., edges from `agent_output` start higher than edges from `tool_result`)?

**Why it matters:** The initialization value is the baseline from which learning diverges. A flat 0.5 means the system starts neutral and learning pulls weights toward extremes. A low value near zero means every edge must earn its weight. Source-type priors encode a prior belief about which connections matter — which may or may not reflect what the model actually needs.

**Current placeholder:** `initial_edge_weight: 0.5` in config.yaml. Change before Milestone 3 baseline.

---

### 2. Decay Function Shape

**Where it surfaces:** `config.yaml: decay_rate`, `learning/interface.py` (if decay is part of the learning interface), and the dreaming cycle (Phase 2 / deferred).

**The question:** How do edge weights decay over time? Linear, exponential, or sigmoid? When does decay trigger — per retrieval, per interaction, or only during the dreaming cycle (offline)?

**Why it matters:** Decay shape determines how quickly the system "forgets." Exponential decay favors recent interactions strongly. Linear decay is uniform. Sigmoid keeps weight stable until a threshold, then drops sharply. The trigger timing determines whether decay is part of the live interaction loop or offline maintenance.

**Current placeholder:** `decay_rate: 0.01` with no trigger implementation. This must be designed before Phase 4 results are meaningful.

---

### 3. RL Reward Distribution Along Activated Edges

**Where it surfaces:** `learning/simple_rl.py` — specifically, the `update()` method implementation.

**The question:** When a feedback signal arrives, how does the reward distribute across all edges that were activated during the interaction?

Options:
- **Uniform:** Every activated edge gets the same `Δweight = feedback × learning_rate`. Simple, treats all edges equally.
- **Proportional to traversal position:** Edges traversed earlier in the funnel get larger updates (they're more "causal" to what was retrieved).
- **Proportional to traversal count:** Edges with higher `traversal_count` get smaller updates (already well-established) or larger updates (reinforce the strong connections further).
- **Exponential decay from output:** Edge closest to the output MU gets full reward; earlier edges get reward × decay_factor per hop back. Like backpropagation, but on a graph.

**Why it matters:** This is the core of what the RL algorithm actually does. The uniform approach is the starting point; it may or may not produce meaningful differentiation. The other options are research questions.

**Current approach:** Build Milestone 4 with uniform distribution as the default. It's in config as `weight_update_rate`. After baseline + first learning run, revisit.

---

## Hyperparameter Registry Template

The full `config.yaml` is written in Milestone 1. Template:

```yaml
model:
  name: "gemma4:e4b"
  ollama_host: "http://host.docker.internal:11434"
  timeout_seconds: 60

gate:
  max_input_length: 4096
  injection_patterns: []  # populated with regex list

hyperparameters:
  # Retrieval
  retrieval_keyword_pool_size: 20
  retrieval_level1_top_k: 3
  retrieval_level2_top_k: 1
  directional_edge_weight_multiplier: 1.5
  retrieval_keyword_count: 5

  # Context
  context_token_budget: 2048
  summarization_trigger_tokens: 1500
  session_compression_threshold: 8000
  extractor_min_sentences: 2

  # Learning
  initial_edge_weight: 0.5        # OPEN — see open questions
  weight_update_rate: 0.1
  decay_rate: 0.01                 # OPEN — see open questions
  pruning_threshold: 0.05
  algorithm: "simple_rl"

  # System
  tool_loop_max_iterations: 5
  snapshot_frequency: "every_interaction"
  dreaming_cycle_interval_minutes: 60  # deferred

tools:
  registered:
    - name: "file_read"
      module: "tools.file_tool"
      class: "FileTool"
      operation: "read"
    - name: "file_write"
      module: "tools.file_tool"
      class: "FileTool"
      operation: "write"
```

---

## Docker Setup

Updated `docker-compose.yml` (final state after Milestone 4):

```yaml
services:
  backend:
    build: ./backend
    ports:
      - "8000:8000"
    volumes:
      - ./backend:/app
      - ./memory:/app/memory
    environment:
      - OLLAMA_HOST=${OLLAMA_HOST}
    extra_hosts:
      - "host.docker.internal:host-gateway"

  frontend:
    build: ./frontend
    ports:
      - "3000:3000"
    environment:
      - REACT_APP_API_URL=http://localhost:8000

  admin:
    build: ./admin
    ports:
      - "3001:3001"
    environment:
      - REACT_APP_API_URL=http://localhost:8000
```

Frontend and admin services are added in Milestone 4. The backend service is the only one present for Milestones 1-3.

---

## Dependencies

### Backend (to add to requirements.txt across milestones)

| Package | Milestone | Purpose |
|---|---|---|
| `aiosqlite` | 1 | Async-safe SQLite access |
| `pydantic` | 1 | UM record and ToolResult schemas |
| `rank_bm25` | 2 | BM25 keyword search index |
| (no additional packages for tools, learning, analytics) | 3-4 | Use stdlib + existing packages |

### Frontend / Admin

- React (Vite scaffold)
- `recharts` — learning curve chart
- `react-force-graph` — memory inspector graph visualization (evaluate during Milestone 4; may substitute simpler table view if performance is poor with large MU counts)

---

## Conventions for /build

These must be followed consistently across all milestones:

- **Every LLM input/output is a MU.** No exceptions, no judgment calls about importance.
- **UM gate wraps every boundary crossing.** No direct Ollama calls except from inside the gated pipeline.
- **No SQLite except in `memory/store.py`.** All other modules call store methods; none open their own database connections.
- **Hyperparameter names in code must match config.yaml keys exactly.** The config is the experimental record; key drift creates confusion.
- **All uncertain parameters come from `config.yaml`.** Nothing hardcoded in Python that belongs in config.
- **Logs are generous.** Log every MU creation, every gate pass, every retrieval result count, every tool call. This is a research platform; visibility is a first-class requirement.
- **Single edge type.** No edge type labels. Weight + direction only. Resist the urge to add `semantic_type` or similar fields.
- **Tests mirror source structure.** `tests/test_gate/test_sanitize.py` tests `gate/sanitize.py`. Easy to find the test for any file.

---

## Status

```
Current milestone: 1 — Foundation (not started)
Last updated: 2026-06-15
```

**Milestone 1 first action:** Create `config.yaml` in the project root with the full hyperparameter registry template above. This is the first artifact and the reference point for all subsequent implementation decisions.
