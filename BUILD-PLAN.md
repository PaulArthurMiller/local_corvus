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

### Milestone 7 — Multimodal Attachment Support

**Why this order:** Milestone 6 fixed how clean user input is separated from injected context. Milestone 7 now extends what "user input" can be — text plus images. The gate and orchestrator changes build directly on the corrected message structure from Milestone 6.

**Scope:** Image attachments only (JPEG, PNG, GIF, WEBP). Binary data is transient — never written to SQLite, never indexed for BM25 retrieval, never stored in the audit log as binary. The UM record stores a text description of the attachment. Session history stores text only; images are one-shot per message (design decision: see open questions).

**Step 0: Smoke test Ollama multimodal capability (before writing any code)**

Verify that Gemma 4 E4B accepts the OpenAI-compatible `image_url` content-parts format via Ollama before building anything. This is the riskiest assumption in the milestone.

Run the following Python script directly (not in Docker — run against `localhost:11434` from the host, or adjust host if running inside the container):

```python
import base64, httpx, pathlib

# Point to any small JPEG you have on disk
img_bytes = pathlib.Path("test_image.jpg").read_bytes()
img_b64   = base64.b64encode(img_bytes).decode()

resp = httpx.post(
    "http://localhost:11434/v1/chat/completions",
    json={
        "model": "gemma4:e4b",
        "messages": [{
            "role": "user",
            "content": [
                {"type": "text", "text": "Describe this image in one sentence."},
                {"type": "image_url", "url": f"data:image/jpeg;base64,{img_b64}"}
            ]
        }],
        "stream": False,
    },
    timeout=120,
)
print(resp.status_code)
print(resp.json()["choices"][0]["message"]["content"])
```

**Done when smoke test passes:** Ollama returns a coherent one-sentence description of the image. If the format is wrong, adjust the content-parts structure and re-test before proceeding to Step 1. Record the confirmed working format in PROGRESS.md.

---

**Build (after smoke test passes):**

1. `config.yaml` — add attachment limits under `gate:`:
   ```yaml
   gate:
     allowed_attachment_mimetypes:
       - "image/jpeg"
       - "image/png"
       - "image/gif"
       - "image/webp"
     max_attachment_size_bytes: 5242880   # 5 MB decoded
     max_attachments_per_message: 4
   ```

2. `backend/gate/sanitize.py` — add `sanitize_attachment(filename, mime_type, data_b64, config) -> AttachmentSanitizeResult`. Pure function, no I/O. Checks:
   - MIME type is in `allowed_attachment_mimetypes`
   - base64 decodes without error
   - Decoded byte length ≤ `max_attachment_size_bytes`
   - Magic bytes of decoded data match claimed MIME type (JPEG: `FF D8 FF`; PNG: `89 50 4E 47`; GIF: `47 49 46 38`; WEBP: bytes 8-11 are `57 45 42 50`)
   - Returns `AttachmentSanitizeResult(data_bytes, mime_type, filename, size_bytes, flags)`

3. `backend/api/chat.py` — add `AttachmentRequest(filename, mime_type, data_b64)` Pydantic model. Add `attachments: Optional[list[AttachmentRequest]] = None` to `ChatRequest`. Pass sanitized attachments to `run_interaction()`.

4. `backend/core/orchestrator.py` — two targeted changes:
   - `run_interaction()` Step 1: gate each attachment via `sanitize_attachment()`; build input MU `raw_content` as `{text}\n[Attached: {filename} {mime_type} {size_kb} KB]` for each file; binary data held in a local variable only
   - `_build_ollama_messages()`: if attachments are present, the final user message `content` becomes a list of parts `[{"type": "text", "text": ...}, {"type": "image_url", "url": "data:{mime};base64,{b64}"}]` instead of a plain string. Session history entry stores text only (images dropped from history — see open questions)

5. `frontend/src/components/ChatWindow.jsx` — add:
   - Hidden `<input type="file" accept="image/jpeg,image/png,image/gif,image/webp" multiple>` triggered by a paperclip button
   - Attachment state: `const [attachments, setAttachments] = useState([])`
   - Each attachment in state: `{filename, mime_type, data_b64, preview_url}` (preview_url is an `object URL` for thumbnail display, revoked after send)
   - Attachment preview strip above the textarea: filename + small thumbnail + remove (×) button per file
   - Clear attachments on successful send and on New Session

6. `frontend/src/api.js` — `sendMessage(message, sessionId, attachments = [])` — include `attachments` in JSON body as `[{filename, mime_type, data_b64}]` (omit `preview_url` — that's UI-only state)

**Tests:**
- `test_sanitize.py` (extend existing): `sanitize_attachment` passes a valid JPEG bytes (real or synthetic with correct magic bytes); rejects a MIME type not in allowlist; rejects a file that decodes to bytes larger than the limit; rejects base64 that doesn't decode; rejects bytes whose magic bytes don't match the claimed MIME type.
- Manual (smoke): attach a JPEG in the Chat UI, send "What do you see?", verify the model describes the image. Inspect backend logs for the attachment flag and the text description in the input MU.

**Done when:**
- `sanitize_attachment` has full unit test coverage for pass and each failure mode
- Attaching an image in the Chat UI and sending produces a coherent description from Gemma
- Input MU `raw_content` contains the text description of the attachment (not binary)
- Binary attachment data does not appear in `audit.log`, `graph.db`, or any snapshot file
- All 130 existing tests still pass

**Risks:**
- ⚠ Smoke test (Step 0) must pass before any code is written. If Ollama's multimodal format for Gemma differs from the OpenAI spec, the `_build_ollama_messages()` change is wrong before it's written.
- ⚠ `object URL` created by `URL.createObjectURL()` in the frontend must be revoked with `URL.revokeObjectURL()` after send to avoid memory leaks.
- ? Images in session history — v1 decision: images are dropped from the session history turn after they're sent. The model cannot refer back to "the image I showed you earlier" in a later turn. This is correct for v1 and avoids unbounded RAM growth. Revisit if the research need arises.
- ? Max attachment size is defined against the decoded byte length. The base64 JSON payload will be ~33% larger. A 5 MB attachment → ~6.7 MB in the request body. This is acceptable for local use; note it in PROGRESS.md.

**Estimate:** 1-2 sessions (smoke test may add a session if format negotiation is needed)

---

### Milestone 6 — Prompt Clarity: Separate Memory Context from User Message

**Why:** A latent bug in the orchestrator sends retrieved memory context twice to the LLM — once in the `system` role and again embedded inside the `user` role message (because the user role currently uses `prompt_gate.content`, which is the full assembled `prompt_text` including the context block). The LLM therefore sees the user's question buried inside what looks like user-generated text, rather than clearly separated background context. Fixing this removes the duplication and sharpens the signal to the model about what is injected background vs. what the user is actually asking.

**Build:**

1. `core/orchestrator.py` — three targeted changes inside `run_interaction()`:
   - **Step 4:** Update the `prompt_text` string (stored as the prompt MU) to use natural-language framing instead of bare `[Context from memory]` / `[User message]` markers, keeping it in sync with the actual message sent to Ollama.
   - **Step 7a:** Improve the `system` role message — replace `[Context from memory]\n{condensed_context}` with an explicit natural-language label that tells the model the block is injected background, not the user's words:
     ```
     The following text is retrieved context from your memory store.
     It is background information to inform your response — it is NOT the user's current message.

     --- Context from memory ---
     {condensed_context}
     --- End context from memory ---
     ```
   - **Step 7b:** Change the `user` role message from `prompt_gate.content` (which re-includes the full context block) to `input_result.content` (the clean, gated user input only). This eliminates the duplication.

**Tests:**
- Unit test (new) in `tests/test_core/test_orchestrator.py`: mock `condensed_context` and `input_result`; call `run_interaction()`; assert:
  - `ollama_messages[0]["role"] == "system"` and `"Context from memory"` is in its content
  - The final user message role has content equal to the clean user input only
  - `"Context from memory"` does NOT appear in the user role message
- Manual: send a chat message that triggers memory retrieval; inspect backend logs; verify context appears once (system role) and user message is clean.

**Done when:**
- Context appears exactly once in the Ollama message array (system role only)
- User role message contains only `input_result.content`
- Prompt MU stored in graph reflects the corrected format
- New unit test passes
- Manual smoke test confirms no duplication in logs

**Risks:** None — this is a contained change to one function with no downstream schema changes.

**Estimate:** 1 session

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

### Milestone 8 — Core Instrumentation: Retrieval Decomposition, Turn Indexing, Edge Lifecycle, Feedback-Event Log

**Why this milestone exists:** Seven milestones built the platform. This milestone makes it experimentally legible. Gap analysis against `.agents/INSTRUMENTATION_AUDIT_CHECKLIST.md` found that the current analytics pipeline captures run-level summaries but discards the intermediate quantities needed to reconstruct any individual retrieval from logs alone — a key sign-off requirement. Every gap addressed here is a field you will wish you had when a learning curve does something unexpected.

**What already exists:** `activated_mu_ids`, `activated_edge_ids`, `feedback_signal`, `weight_updates` ({edge_id, old_weight, new_weight}), `hyperparameters_snapshot` (hyperparameters section only), `prompt_mu_id` (prompt MU text), `learning_algo_id`.

**What's missing (all MISSING or PARTIAL in the checklist):** BM25 per-MU scores, K1 entry points, the candidate set before cutoff, structured traversal path with hop depth, edge contributions per candidate, the full assembled Ollama messages array, turn index, session turn index, condition label, full config snapshot, config hash, edge creation metadata (trigger + creating run), enriched weight-update records (source/target MU IDs, feedback-event FK), and a dedicated feedback-event log including an explicit "none" verdict for turns without feedback.

**Build:**

**1. `backend/memory/retrieval.py` — expand `RetrievalResult`**

Add six new fields (all values are already computed inside `retrieve()`; they just aren't returned):

```python
@dataclass
class RetrievalResult:
    mus: list[dict] = field(default_factory=list)
    keywords: list[str] = field(default_factory=list)
    activated_mu_ids: list[str] = field(default_factory=list)
    activated_edge_ids: list[str] = field(default_factory=list)
    # --- NEW ---
    bm25_scores_per_mu: dict[str, float] = field(default_factory=dict)
    # Cumulative BM25 score per candidate MU. Sourced from candidate_scores dict.

    entry_point_mu_ids: list[str] = field(default_factory=list)
    # K1 MU IDs — BM25 funnel entry points before traversal. Distinguishes
    # "surfaced via lexical match" from "surfaced via edge traversal."

    candidate_set_before_ranking: list[dict] = field(default_factory=list)
    # All multi-hit (>=2 keyword) candidates before K1 cutoff, each as:
    # {mu_id, keyword_hit_count, bm25_score, edge_weight_sum}

    traversal_path: list[dict] = field(default_factory=list)
    # Ordered hops K1→K2, each as:
    # {source_mu_id, edge_id, target_mu_id, hop_depth}
    # hop_depth=1 for the current single-hop design; field is future-proof.

    hop_depth_per_retrieved_mu: dict[str, int] = field(default_factory=dict)
    # {mu_id: depth} — 0 for K1 (lexical entry), 1 for K2 (traversal hop).
    # Cross-cluster transfer signature: gold MU surfaces at depth > 0.

    edge_contributions: dict[str, float] = field(default_factory=dict)
    # Total edge weight contribution per candidate MU (sum of connected edge
    # weights). Separates the edge-learned signal from the BM25 lexical signal
    # in the K1 composite score.
```

No logic changes needed inside `retrieve()` — populate these fields from values already computed and include them in the final `RetrievalResult(...)` constructor call.

**2. `backend/analytics/run_log.py` — expand `RunRecord`**

```python
# Claim 2 — retrieval decomposition
bm25_scores_per_mu: dict[str, Any] = Field(default_factory=dict)
entry_point_mu_ids: list[str] = Field(default_factory=list)
candidate_set_before_ranking: list[dict] = Field(default_factory=list)
traversal_path: list[dict] = Field(default_factory=list)
hop_depth_per_retrieved_mu: dict[str, Any] = Field(default_factory=dict)
edge_contributions: dict[str, Any] = Field(default_factory=dict)
assembled_prompt_json: list[dict] = Field(default_factory=list)
# Full Ollama messages array. Image base64 data replaced with
# "[image: {filename} {size_kb}KB]" — keeps it inspectable without bloat.

# Claim 3 — config traceability
config_hash: Optional[str] = None          # SHA-256 of full resolved config
full_config_snapshot: dict[str, Any] = Field(default_factory=dict)
condition_label: Optional[str] = None      # "learning" | "static-weight" | "random-weight"

# Turn/session indexing
turn_index: Optional[int] = None           # global sequential across all runs
session_turn_index: Optional[int] = None   # sequential within this session
```

**3. `backend/memory/store.py` — schema additions + migration**

a) Update `CREATE TABLE analytics` to include all new columns with defaults.

b) Update `CREATE TABLE edges` to include:
- `creation_run_id TEXT` — FK to analytics.run_id
- `creation_trigger TEXT` — "prompt_construction" | "co_activation" | "feedback" | "traversal"

c) Add `CREATE TABLE IF NOT EXISTS feedback_events`:
```sql
CREATE TABLE IF NOT EXISTS feedback_events (
    feedback_event_id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL REFERENCES analytics(run_id),
    verdict TEXT NOT NULL,     -- thumbs_up | thumbs_down | none
    verdict_basis TEXT,        -- checklist_pass | confabulation | rule_violation | user_named | none
    turn_index INTEGER,
    is_probe_turn INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_feedback_events_run ON feedback_events(run_id);
CREATE INDEX IF NOT EXISTS idx_feedback_events_probe ON feedback_events(is_probe_turn);
```

d) Add `_migrate_db()` function that runs `ALTER TABLE ... ADD COLUMN` for each new column inside a try/except block — idempotent for existing databases that pre-date this milestone.

e) Add new store methods:
- `get_next_turn_index() -> int` — `COUNT(*) + 1` from analytics
- `get_session_turn_count(session_id: str) -> int` — COUNT of runs for this session
- `insert_feedback_event(event: dict) -> None`
- `get_feedback_events_for_run(run_id: str) -> list[dict]`
- Update `create_edge()` to accept optional `creation_run_id` and `creation_trigger`
- Update `insert_run()` to serialize and store all new JSON fields

**4. `backend/core/orchestrator.py` — wire new fields**

a) Before building `RunRecord`, compute:
- `turn_index = await graph_store.get_next_turn_index()`
- `session_turn_index = await graph_store.get_session_turn_count(session_id)`
- `config_hash = hashlib.sha256(json.dumps(_CONFIG, sort_keys=True).encode()).hexdigest()`
- `condition_label = _CONFIG.get("experiment", {}).get("condition_label")`

b) When calling `graph_store.create_edge()` in Steps 5 and 9, pass:
- `creation_run_id=run_id`
- `creation_trigger="prompt_construction"`

c) Build `assembled_prompt_json` by copying `ollama_messages` and replacing any base64 image data in `image_url` content-parts with `"[image: {filename} {size_kb}KB]"`.

d) Pass all new `RetrievalResult` fields through to `RunRecord`.

**5. `backend/api/chat.py` — enrich feedback endpoint**

a) Add optional fields to `FeedbackRequest`:
```python
verdict_basis: Optional[str] = None    # naming the rule or issue
is_probe_turn: bool = False
```

b) After `update_edge_weight()` calls, create a `FeedbackEvent` record:
- `verdict = "thumbs_up" if signal == 1 else "thumbs_down"`
- Call `record_feedback_event()` with `run_id`, `verdict`, `verdict_basis`, `turn_index` (from run record), `is_probe_turn`

c) Enrich each entry in `weight_updates` list with `source_mu_id`, `target_mu_id` (already in the fetched edge record), and `feedback_event_id`.

**6. New: `backend/analytics/feedback_events.py`**

```python
class FeedbackEvent(BaseModel):
    feedback_event_id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    run_id: str
    verdict: str           # "thumbs_up" | "thumbs_down" | "none"
    verdict_basis: Optional[str] = None
    turn_index: Optional[int] = None
    is_probe_turn: bool = False

async def record_feedback_event(event: FeedbackEvent) -> None:
    await graph_store.insert_feedback_event(event.model_dump())
```

**7. `config.yaml` — add experiment section**

```yaml
experiment:
  condition_label: "learning"   # change to "static-weight" or "random-weight" for control runs
```

**Tests:**
- `test_retrieval.py` (extend): verify all six new `RetrievalResult` fields are populated; `bm25_scores_per_mu` contains only MUs that matched a keyword; `entry_point_mu_ids` matches K1; `traversal_path` is empty when no K2 traversal occurs; `candidate_set_before_ranking` includes all multi-hit candidates; `hop_depth_per_retrieved_mu` is 0 for K1 and 1 for K2 MUs.
- `test_run_log.py` (extend): `RunRecord` round-trips all new fields through SQLite; `config_hash` is consistent for same config.
- `test_store.py` (extend): `create_edge()` stores `creation_run_id` and `creation_trigger`; `get_next_turn_index()` returns 1 for empty DB and increments; `insert_feedback_event()` and `get_feedback_events_for_run()` round-trip correctly.
- `test_feedback_events.py` (new): `POST /feedback` with `verdict_basis="checklist_pass"` → verify `feedback_event` record created in DB; verify `weight_updates` list includes `source_mu_id`, `target_mu_id`, `feedback_event_id`.
- **Sign-off check (manual):** Run one interaction, then send feedback. Retrieve the run record from `/admin/runs`. Verify: `entry_point_mu_ids` is a non-empty list; `traversal_path` has `{source_mu_id, edge_id, target_mu_id, hop_depth}` entries; `assembled_prompt_json` shows the system/user/history structure; `config_hash` is a 64-char hex string; `feedback_events` table has exactly one row; that row's `run_id` matches the run; `weight_updates[0]` contains `source_mu_id` and `feedback_event_id`.

**Done when:**
- All new fields appear in a run record from `/admin/runs`
- A single run can be walked end-to-end from its record: `traversal_path` + `bm25_scores_per_mu` + `edge_contributions` explain the retrieved set; `assembled_prompt_json` shows the exact context sent to Gemma; `config_hash` + `full_config_snapshot` prove the config state — no re-execution required
- `feedback_events` table contains one row per `/feedback` call, not per run
- `edges` table has `creation_run_id` and `creation_trigger` populated for all edges created after this milestone
- `turn_index` increments monotonically across all runs; `session_turn_index` resets per session
- Schema migration runs safely against an existing populated `graph.db`
- All 141 existing tests still pass

**Risks:**
- ⚠ Schema migration — existing `graph.db` will not have new columns until `_migrate_db()` runs. Must test the migration path with a pre-populated database, not only a fresh one.
- ⚠ `assembled_prompt_json` — must strip base64 image data before storing (can be several MB). The stripping function must have its own unit test.
- ? `condition_label` in `config.yaml` must be changed manually between condition runs. Consider an environment variable override as an alternative in Milestone 9.

**Estimate:** 2-3 sessions

---

### Milestone 9 — Experiment Framework: Probe Set, Ground Truth Labels, Seed Fixture, Battery Snapshots

**Why this milestone exists:** Milestone 8 makes individual interactions fully traceable. This milestone builds the experiment-level structure — the fixed probe set that measures learning, the gold labels that define "correct," the seed fixtures that ensure all conditions start identically, and the battery snapshot system that makes each measurement session reproducible. Without this layer, you have rich per-run data with no systematic way to compare across conditions.

The sign-off gate requires: frozen probe-set version, gold MUs per probe, phenomenon tags, bridge-MU flags, seed-fixture version/manifest, and battery-level snapshots. All are built here.

**Design dependency:** The probe prompts, gold MU IDs, and phenomenon assignments are content decisions — they define what you're testing. This milestone builds the engineering scaffold. The actual probe content will be added to `probes/probe_set_v1.yaml` before the experiment runs, using the format defined here.

**Build:**

**1. `probes/probe_set_v1.yaml` — probe set definition file**

```yaml
version: "v1"
phenomenon_map:
  ranking_improvement:
    description: "Gold MU rank should decrease over sessions within the primary cluster"
  cross_cluster_transfer:
    description: "Bridge MU should surface on adjacent-cluster probes via edge traversal (hop_depth > 0)"
  process_rule_adherence:
    description: "Output must satisfy the named rule; measured on output, not retrieved set"

probes:
  - probe_id: "p01"
    phenomenon_type: "ranking_improvement"
    prompt_text: ""         # FILL IN before experiment
    gold_mu_ids: []         # FILL IN after seed fixture generation
    is_bridge_probe: false
    bridge_pair_id: null
  # ... additional entries, one per probe
```

File ships as a template with empty content fields. The schema is finalized here; content is added by the experimenter after seed fixture generation.

**2. `backend/analytics/probes.py` — probe management module**

```python
class ProbeDefinition(BaseModel):
    probe_id: str
    phenomenon_type: str    # ranking_improvement | cross_cluster_transfer | process_rule_adherence
    prompt_text: str
    gold_mu_ids: list[str] = Field(default_factory=list)
    is_bridge_probe: bool = False
    bridge_pair_id: Optional[str] = None
    probe_set_version: str

class ProbeResult(BaseModel):
    result_id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    probe_id: str
    run_id: Optional[str] = None
    measurement_session_index: int
    condition_label: str
    retrieved_mu_ids: list[str] = Field(default_factory=list)
    gold_mu_rank: Optional[int] = None    # 1-indexed; None if gold MU absent from retrieved set
    retrieval_precision: Optional[float] = None
    retrieval_recall: Optional[float] = None
    bm25_scores_per_mu: dict[str, float] = Field(default_factory=dict)

def load_probe_set(version: str = "v1") -> list[ProbeDefinition]:
    """Load and validate probe definitions from probes/probe_set_{version}.yaml."""

async def record_probe_result(result: ProbeResult) -> None:
    """Write a probe result to the probe_results table."""

async def compute_probe_result(
    probe: ProbeDefinition,
    retrieval_result: "RetrievalResult",
    run_id: str,
    measurement_session_index: int,
    condition_label: str,
) -> ProbeResult:
    """Compute precision, recall, and gold_mu_rank from a retrieval result."""
```

**3. `backend/memory/store.py` — three new tables**

```sql
CREATE TABLE IF NOT EXISTS probes (
    probe_id TEXT PRIMARY KEY,
    probe_set_version TEXT NOT NULL,
    phenomenon_type TEXT NOT NULL,
    prompt_text TEXT NOT NULL,
    gold_mu_ids TEXT NOT NULL DEFAULT '[]',    -- JSON list of mu_id strings
    is_bridge_probe INTEGER NOT NULL DEFAULT 0,
    bridge_pair_id TEXT
);

CREATE TABLE IF NOT EXISTS probe_results (
    result_id TEXT PRIMARY KEY,
    probe_id TEXT NOT NULL REFERENCES probes(probe_id),
    run_id TEXT REFERENCES analytics(run_id),
    measurement_session_index INTEGER NOT NULL,
    condition_label TEXT NOT NULL,
    retrieved_mu_ids TEXT NOT NULL DEFAULT '[]',
    gold_mu_rank INTEGER,
    retrieval_precision REAL,
    retrieval_recall REAL,
    bm25_scores_per_mu TEXT NOT NULL DEFAULT '{}'
);

CREATE TABLE IF NOT EXISTS seed_fixture_manifest (
    fixture_version TEXT PRIMARY KEY,
    manifest_hash TEXT NOT NULL,      -- SHA-256 of the fixture JSON file
    mu_count INTEGER NOT NULL,
    type_distribution TEXT NOT NULL DEFAULT '{}',    -- JSON: {source_type: count}
    generation_date TEXT NOT NULL,
    generation_record TEXT NOT NULL DEFAULT '[]'     -- JSON: per-MU generation details
);

CREATE INDEX IF NOT EXISTS idx_probe_results_probe ON probe_results(probe_id);
CREATE INDEX IF NOT EXISTS idx_probe_results_session ON probe_results(measurement_session_index);
CREATE INDEX IF NOT EXISTS idx_probe_results_condition ON probe_results(condition_label);
```

New store methods:
- `sync_probes(probes: list[dict]) -> None` — upsert probe definitions from YAML on app startup
- `insert_probe_result(result: dict) -> None`
- `query_probe_results(probe_id=None, condition_label=None, measurement_session_index=None) -> list[dict]`
- `insert_seed_fixture_manifest(manifest: dict) -> None`
- `get_seed_fixture_manifest(version: str) -> Optional[dict]`

**4. `backend/analytics/seed_fixture.py` — seed fixture provenance module**

```python
class SeedFixtureManifest(BaseModel):
    fixture_version: str
    manifest_hash: str                     # SHA-256 of the fixture JSON file
    mu_count: int
    type_distribution: dict[str, int]      # {source_type: count}
    generation_date: str
    generation_record: list[dict]          # [{mu_id, prompt, tool_call, response}] per seed MU

def compute_fixture_hash(fixture_path: Path) -> str:
    """SHA-256 of the fixture file's raw bytes."""

async def load_and_verify_fixture(version: str) -> SeedFixtureManifest:
    """Load manifest from DB, recompute hash from file, raise if mismatch."""

async def register_fixture(fixture_path: Path, version: str) -> SeedFixtureManifest:
    """Compute hash, count MUs by type, store manifest in DB."""
```

Fixture files: `seed_fixtures/seed_v{N}.json.gz` — same gzip-JSON format as graph snapshots.

**5. `backend/memory/snapshot.py` — battery snapshot wrappers**

```python
async def take_battery_snapshot(
    battery_index: int,
    phase: str,            # "pre" | "post"
    condition_label: str,
) -> str:
    """
    Named snapshot for a measurement battery.
    Written to /app/memory/snapshots/battery_{index}_{phase}_{condition}_{ts}.json.gz
    Returns the file path for logging.
    """

async def take_pre_pruning_snapshot() -> str:
    """
    Snapshot immediately before any pruning operation.
    Written to /app/memory/snapshots/pre_prune_{ts}.json.gz
    """
```

**6. `POST /admin/run-probe` endpoint in `backend/api/admin.py`**

```
POST /admin/run-probe
Body: {probe_id, measurement_session_index, condition_label}
```

This endpoint:
1. Loads the probe definition by `probe_id`
2. Runs `retrieve()` for the probe's `prompt_text` — read-only; no MU creation, no edges written
3. Computes `ProbeResult` (precision, recall, gold_mu_rank)
4. Stores the `ProbeResult` in `probe_results`
5. Returns the `ProbeResult`

**Invariant enforced by design:** This endpoint MUST NOT call the full `run_interaction()` pipeline. It only calls `retrieve()`. No MU is created. No feedback event is written. Any run_id in a ProbeResult is None unless explicitly associated. The sign-off check verifies: `probe_results` rows have no matching `feedback_events` row.

**7. `backend/analytics/run_log.py` — add probe-related fields**

```python
measurement_session_index: Optional[int] = None    # which battery session
is_probe_turn: bool = False                        # true if this run was a probe measurement
```

These are also added to the `analytics` table schema.

**8. Admin UI `admin/` — Probe Results view**

Add `ProbeResults.jsx` page:
- Filter controls: `condition_label`, `measurement_session_index` range
- Table: probe_id | phenomenon_type | gold_mu_rank | retrieval_precision | retrieval_recall
- Per-phenomenon learning curve: x = measurement_session_index, y = mean gold_mu_rank (one line per phenomenon)
- Bridge probe toggle filter

Add `/probe-results` route to `admin/src/App.jsx`.

**Tests:**
- `test_probes.py` (new): `load_probe_set()` parses YAML and returns `ProbeDefinition` list; `compute_probe_result()` correctly computes precision/recall when gold MU is rank 1; returns `gold_mu_rank=None` when gold MU not in retrieved set; `is_bridge_probe` filter works.
- `test_seed_fixture.py` (new): `compute_fixture_hash()` returns consistent hash for same file; `load_and_verify_fixture()` raises when fixture file has been modified.
- `test_snapshot.py` (extend): `take_battery_snapshot()` writes file with correct naming convention; `take_pre_pruning_snapshot()` writes to correct path.
- Manual: `POST /admin/run-probe` → verify `probe_results` row in DB; verify no `feedback_events` row for that run; verify `gold_mu_rank` is correct.
- Manual: Admin UI Probe Results page shows per-phenomenon learning curves with real data.

**Done when:**
- `probes/probe_set_v1.yaml` template exists with correct schema (content filled in by user before experiment)
- `probes`, `probe_results`, and `seed_fixture_manifest` tables exist and are queryable
- `register_fixture()` stores a verifiable manifest; `load_and_verify_fixture()` detects tampering
- `POST /admin/run-probe` returns a `ProbeResult` with precision/recall/gold_mu_rank
- Battery snapshot files written with correct naming via `take_battery_snapshot()`
- Admin UI Probe Results page renders per-phenomenon learning curves
- Instrumentation Audit Checklist: all cells fillable; end-to-end replay walkthrough succeeds (feedback event → edge update → weight-as-of-turn → traversal path → retrieved set → gold-MU rank, no re-execution required)
- All 141+ existing tests still pass

**Risks:**
- ⚠ Probe content (prompt text, gold MU IDs) cannot be finalized until seed fixtures are generated and loaded into the database. The YAML template ships empty. The experiment cannot begin until the experimenter fills in content.
- ⚠ `gold_mu_ids` references MU IDs that only exist after seed fixture generation. Fixture generation → probe labeling → experiment start must happen in this sequence.
- ? `measurement_session_index` is set by the experimenter when calling `/admin/run-probe`. The system does not auto-increment it — the experimenter decides "this is battery session 3." This is correct: measurement sessions are a decision, not an automatic counter.
- ? `condition_label` should be overridable via environment variable (`CONDITION_LABEL=static-weight`) to avoid config.yaml edits between runs. Add in this milestone alongside the probe infrastructure.

**Estimate:** 2-3 sessions

---

### Milestone 10 — Session Context Flag: Controllable History Injection

**Why this milestone exists:** Experiment runs must isolate the memory retrieval signal from session-level conversational confounds. When prior conversation turns (session history) are injected into every prompt, the model's response at turn N depends on what was said at turns 1 through N-1 — variance orthogonal to what the memory graph retrieved. This milestone makes history injection an explicit, auditable, config-driven decision. The flag is `false` by default; flipping it to `true` restores prior behavior without any other changes.

**What changes and what doesn't:**
- **Changes:** Whether `get_session_messages()` history is passed to Ollama inside `_build_ollama_messages()`.
- **Unchanged:** Memory context injection (condensed_context → system role), UM gate processing, MU creation, session history *accumulation* (history is still written on every turn; it's just not sent to Ollama when the flag is off), all other RunRecord fields, all other prompt setup.

**Design note — why still accumulate history when the flag is off:** The session message list is always appended to regardless of the flag so that a mid-session flag change (e.g., `USE_SESSION_CONTEXT=true` restart) picks up the full history. The flag controls what gets *sent*, not what gets *stored*.

**Build:**

**1. `config.yaml` — add flag to experiment section**

```yaml
experiment:
  use_session_context: false   # set true to restore multi-turn history injection
  condition_label: "learning"
```

**2. `backend/core/orchestrator.py` — read flag + apply at call site**

At module load (alongside the existing `CONDITION_LABEL` env-var pattern), resolve the boolean from env or config:

```python
_env_use_session = os.getenv("USE_SESSION_CONTEXT", "").strip().lower()
USE_SESSION_CONTEXT: bool = (
    _env_use_session in ("1", "true", "yes") if _env_use_session else
    bool(_CONFIG.get("experiment", {}).get("use_session_context", False))
)
```

In `run_interaction()`, Step 7 (assemble Ollama messages), replace the unconditional `get_session_messages()` call with:

```python
history = get_session_messages(session_id) if USE_SESSION_CONTEXT else []
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
```

In Step 11 (build RunRecord), add:
```python
use_session_context=USE_SESSION_CONTEXT,
```

**3. `backend/analytics/run_log.py` — add field to RunRecord**

```python
use_session_context: bool = False
```

**4. `backend/memory/store.py` — analytics table column + migration**

a) Add to `CREATE TABLE IF NOT EXISTS analytics`:
```sql
use_session_context INTEGER NOT NULL DEFAULT 0,
```

b) Add to `_migrate_db()`:
```python
try:
    await db.execute(
        "ALTER TABLE analytics ADD COLUMN use_session_context INTEGER NOT NULL DEFAULT 0"
    )
    await db.commit()
except Exception:
    pass
```

c) Update `insert_run()` to include `use_session_context` in the INSERT statement and params (store as `int(run["use_session_context"])`).

**5. `docker-compose.yml` — env var passthrough**

Under backend service `environment:`:
```yaml
- USE_SESSION_CONTEXT=${USE_SESSION_CONTEXT:-}
```

**6. `admin/src/components/RunLogTable.jsx` — show flag in expanded row detail**

In the expanded-row detail block, add one line after `algorithm`:
```jsx
<strong>session_ctx:</strong> {run.use_session_context ? 'on' : 'off'}<br />
```

**Tests:**
- Extend `tests/test_core/test_orchestrator.py`:
  - `test_session_context_excluded_when_flag_off`: call `_build_ollama_messages()` with `history=[]` (simulating flag=False); assert no role `user` or `assistant` history turns appear before the final user message.
  - `test_session_context_included_when_flag_on`: call `_build_ollama_messages()` with a non-empty history list; assert those turns appear in the messages array in order.
  - `test_use_session_context_field_in_run_record`: construct `RunRecord(use_session_context=False, ...)` and verify `model_dump()["use_session_context"]` is `False`.
- Extend store tests:
  - `insert_run()` with `use_session_context=False` → field round-trips as `0`; `use_session_context=True` → `1`.

**Done when:**
- Default behavior: history is NOT injected; Ollama receives only memory context (system role) + current user message
- `USE_SESSION_CONTEXT=true` (env) or `use_session_context: true` (config.yaml) restores full history injection; restart required to pick up config change
- Every run record has a `use_session_context` field queryable from `/admin/runs`
- RunLogTable expanded row shows `session_ctx: on/off` for at-a-glance auditing
- Container logs emit `use_session_context=False history_turns=0` (or `True` with real count) on every interaction
- `assembled_prompt_json` stored in the run record naturally reflects what was sent (history absent when flag is off — the most authoritative audit)
- Schema migration runs safely against existing `graph.db`
- All 193 existing tests still pass

**Risks:**
- ⚠ `USE_SESSION_CONTEXT` is a module-level constant resolved at import time. Changing `config.yaml` while the server is running has no effect — restart required. Consistent with all other module-level config in the orchestrator; note this clearly in logs at startup.
- ? Probe turns (`POST /admin/run-probe`) do not call `run_interaction()` and have no session history, so this flag has no effect on probe runs. No changes needed in `api/admin.py`.

**Estimate:** 1 session

---

### Milestone 11 — Empty-Response Failure Detection, Tagging, and Visibility

**Why this milestone exists:** When Ollama returns HTTP 200 with `finish_reason=length` and empty `message.content`, the model's completion budget was exhausted before any output token landed. Currently this stores a blank `agent_output` MU and returns an empty string to the UI (blank bubble), with no error flag and no trace in run-level summaries. This failure mode must become explicitly detected, named, tagged at every layer, and visible everywhere a normal completed turn would appear.

**What changes:**
- Detection in `_run_tool_loop` — one insertion point
- Failure signal threads through `_run_tool_loop` → `run_interaction` → `InteractionResult` → `ChatResponse` → Chat UI
- MU `source_type` extended to carry the cause (`agent_output_failed_token_limit` | `agent_output_failed_empty_output`)
- `RunRecord` + `analytics` table gain a `failure_reason` column
- Chat UI: blank bubble replaced by user-visible explanation
- Admin UI: failure indicator per row; failed turns queryable by SQL predicate

**Out of scope:** num_ctx / context window sizing; session history wiring; the unconditional input→output edge creation (flagged for later).

**Named constants (all new strings are constants, no inline literals):**

```python
# orchestrator.py — module level

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
```

**Build:**

1. `core/orchestrator.py` — six named constants after `USE_SESSION_CONTEXT`, before `_tool_registry`.

2. `core/orchestrator.py` — `_run_tool_loop` detection: in the text-response branch (lines 547–555), after computing `content = llm_message.get("content") or ""`, detect the failure condition:
   - `content.strip() == ""` AND `finish_reason == _OLLAMA_FINISH_REASON_LENGTH` → `FAILURE_REASON_TOKEN_LIMIT`
   - `content.strip() == ""` AND any other finish_reason → `FAILURE_REASON_EMPTY_OUTPUT`
   - otherwise → `None`
   - Log at WARNING for failures, INFO for normal.
   - Return type extends to 5-tuple: `(content, tool_call_mu_ids, tokens_in, tokens_out, failure_reason)`.
   - Also update the fallback return at end of while loop (max-iterations) to return `FAILURE_REASON_EMPTY_OUTPUT` as 5th element.

3. `core/orchestrator.py` — `run_interaction` Step 8: unpack 5-tuple. If `llm_failure_reason` is not None, substitute `FAILURE_UI_MESSAGES[llm_failure_reason]` into `llm_content` before Step 9 (session history, gate, MU creation all receive the substituted message — correct because history should reflect what the user saw).

4. `core/orchestrator.py` — Step 9 (`output_um` creation): compute `output_source_type` from `llm_failure_reason` using the named source-type constants; pass to `create_um()`.

5. `core/orchestrator.py` — Step 11 (`RunRecord`): add `failure_reason=llm_failure_reason`.

6. `core/orchestrator.py` — `InteractionResult` dataclass: add `failure_reason: Optional[str] = None`. Populate from `llm_failure_reason` in the return statement.

7. `analytics/run_log.py` — `RunRecord`: add `failure_reason: Optional[str] = None`.

8. `memory/store.py`:
   - `_INIT_SQL analytics` table: add `failure_reason TEXT` column.
   - `_migrate_db()`: add `("analytics", "failure_reason", "TEXT")` to migrations list.
   - `insert_run()`: add `failure_reason` to INSERT and params.
   - `query_runs()` / `get_run()`: no JSON decoding needed (plain TEXT).

9. `api/chat.py` — `ChatResponse`: add `failure_reason: Optional[str] = None`. Endpoint handler: populate from `result.failure_reason`.

10. `frontend/src/components/MessageBubble.jsx` — when `message.failure_reason` is truthy, apply distinct visual treatment (warning border/color, italic text). Content already has the UI message substituted in by the orchestrator.

11. `admin/src/components/RunLogTable.jsx` — show failure badge/label when `run.failure_reason` is not null.

**Tests:**

New file `tests/test_core/test_failure_detection.py`:
- `test_run_tool_loop_detects_token_limit` — empty content + `finish_reason=length` → `FAILURE_REASON_TOKEN_LIMIT`
- `test_run_tool_loop_detects_token_limit_whitespace` — whitespace-only content + `finish_reason=length` → same
- `test_run_tool_loop_detects_empty_output` — empty content + `finish_reason=stop` → `FAILURE_REASON_EMPTY_OUTPUT`
- `test_run_tool_loop_normal_response_no_failure` — non-empty content → `failure_reason is None`
- `test_output_mu_source_type_on_token_limit` — MU stored with `source_type == "agent_output_failed_token_limit"`
- `test_output_mu_source_type_on_empty_output` — MU stored with `source_type == "agent_output_failed_empty_output"`
- `test_run_record_failure_reason_stored` — `RunRecord.failure_reason` round-trips through `insert_run` → `query_runs`
- `test_normal_turn_failure_reason_null` — normal turn stores `failure_reason = NULL`
- `test_interaction_result_carries_failure_reason` — `InteractionResult.failure_reason` propagates end-to-end
- `test_ui_message_substituted_on_token_limit` — response is `FAILURE_UI_MESSAGES[FAILURE_REASON_TOKEN_LIMIT]`, not `""`

**Done when:**
- A turn with `finish_reason=length` + empty content logs a WARNING (not INFO)
- The Chat UI shows the cause message, not a blank bubble
- The output MU in `memory_units` has `source_type` of `"agent_output_failed_token_limit"` — not `"agent_output"`
- `SELECT * FROM analytics WHERE failure_reason IS NOT NULL` returns the failed turn
- Admin UI run row shows failure indicator for the failed turn
- All new tests pass; all 199 existing tests still pass

**Risks:**
- ⚠ `finish_reason` may be `None` or absent in some Ollama versions — use `(finish_reason or "") == _OLLAMA_FINISH_REASON_LENGTH` to guard
- ⚠ Session history stores the substituted UI message, not `""` — behavioral change; correct but worth noting in the PR

**Estimate:** 1 session

---

### Milestone 12 — Series ID on MUs + Experiment ID + Terminology Rename (Prerequisite Gate)

**Why this milestone exists:** The Series and Experiment Reports scope every analysis by `series_id` and `experiment_id`. Neither field exists on `memory_units` today — `session_id` is tracked only in the `analytics` table, and `experiment_id` is absent from the data layer entirely. Without `series_id` on MUs, §5.3 (intra-series vs. cross-series retrieval rate) and §5.4 (cross-series edges) are impossible to compute: the only way to determine which series a retrieved MU belongs to would be a multi-step join through `analytics.activated_mu_ids` — not a query, a re-derivation.

This milestone also executes the project-wide terminology rename: `session` → `series`. Per the spec: "Reserving 'session' for a future iteration that actually carries context forward keeps the vocabulary honest about current behavior." This is a rename only — no behavior change.

**Confirmed field audit (§3 of report spec):**
- `source_type` and `created_at` on MUs: ✅ exist
- `finish_reason=length` empty-failure tag on agent_output MUs: ✅ Milestone 11 (`source_type = "agent_output_failed_token_limit"`)
- Per-edge-creation records with trigger + creating run: ✅ Milestone 8 (`edges.creation_run_id`, `edges.creation_trigger`)
- Per-edge-update records with old/new weight and feedback FK: ✅ Milestone 8 (`analytics.weight_updates` with `feedback_event_id`)
- Feedback-event log with FK: ✅ Milestone 8 (`feedback_events` table)
- Traversal-chain records: ✅ Milestone 8 (`traversal_path`, `entry_point_mu_ids`, `hop_depth_per_retrieved_mu`)
- Per-probe ground-truth labels with phenomenon tags and bridge-MU flags: ✅ Milestone 9 (`probes` table)
- Seed-fixture provenance: ✅ Milestone 9 (`seed_fixture_manifest` table)
- `series_id` on MUs: ❌ **MISSING** — this milestone closes the gap
- `experiment_id` anywhere: ❌ **MISSING** — this milestone closes the gap

**Build:**

**1. `config.yaml` — extend experiment section**
```yaml
experiment:
  experiment_id: ""              # set by experimenter at experiment start
  condition_label: "learning"
  use_session_context: false
```
If `experiment_id` is empty at startup, generate one as `{condition_label}-{YYYY-MM-DD}` and log it clearly at WARNING level (experimenter should set it explicitly for reproducibility).

**2. `backend/memory/store.py` — schema additions + migration**

a) `memory_units` table: add three columns:
```sql
series_id TEXT,      -- NULL for seed-fixture MUs (which get "seed:{fixture_version}")
experiment_id TEXT,  -- NULL for pre-M12 MUs
cluster_id TEXT      -- "cluster_a" | "cluster_b" | "bridge" | NULL; set at seed time; NULL for conversational MUs
```
`cluster_id` is added here because the §6.1 bridge-MU / cross-cluster analysis (Milestone 15) requires cluster membership on MUs. Adding it now while the schema is already being modified avoids a second migration.

b) `analytics` table: rename `session_id` → `series_id`, add `experiment_id`:
```python
# In _migrate_db() — requires SQLite >= 3.25 (released 2018-09-15):
("analytics", "RENAME COLUMN session_id TO series_id"),
("analytics", "ADD COLUMN experiment_id TEXT"),
("memory_units", "ADD COLUMN series_id TEXT"),
("memory_units", "ADD COLUMN experiment_id TEXT"),
("memory_units", "ADD COLUMN cluster_id TEXT"),
```
If `RENAME COLUMN` fails (SQLite < 3.25), fall back to table-recreation. Verify the SQLite version in the Docker image at the start of this milestone — run `python -c "import sqlite3; print(sqlite3.sqlite_version)"` in the container.

c) Update `create_mu()`:
```python
async def create_mu(
    mu_id: str,
    source_type: str,
    raw_content: str,
    series_id: Optional[str] = None,
    experiment_id: Optional[str] = None,
    cluster_id: Optional[str] = None,
) -> None:
```

d) Update `insert_run()` to use `series_id` (renamed) and include `experiment_id`.

e) Rename `get_session_turn_count()` → `get_series_turn_count()`.

f) Update all other store methods that reference `session_id` (query filters, INSERT params).

**3. `gate/um.py`**
Add `series_id: Optional[str] = None` and `experiment_id: Optional[str] = None` to `UniversalMessage`. Update `create_um()` signature.

**4. `core/orchestrator.py`**
- Resolve `EXPERIMENT_ID` at module load: `os.getenv("EXPERIMENT_ID", "")` → config `experiment.experiment_id` → auto-generated default. Log at startup.
- Pass `series_id=session_id, experiment_id=EXPERIMENT_ID` to all `create_um()` calls.
- Add `experiment_id=EXPERIMENT_ID` to `RunRecord`.
- Rename all internal `session_id` variables to `series_id`.

**5. `analytics/run_log.py`**
- Rename `session_id` → `series_id` in `RunRecord`.
- Add `experiment_id: Optional[str] = None`.

**6. `api/chat.py` and `api/admin.py`**
- Rename `session_id` → `series_id` in all request/response models and query parameters.

**7. Seed-fixture MUs**
When `register_fixture()` / `load_and_verify_fixture()` loads seed MUs into `memory_units`, stamp:
- `series_id = f"seed:{fixture_version}"`
- `experiment_id = EXPERIMENT_ID`
- `cluster_id` from the fixture generation record's per-MU metadata (if present; otherwise NULL)

**8. Admin UI** — rename "Session" → "Series" everywhere: RunLogTable column headers, ProbeResults filter labels, MemoryInspector labels.

**9. `docker-compose.yml`** — add `EXPERIMENT_ID` env var passthrough alongside the existing `CONDITION_LABEL` and `USE_SESSION_CONTEXT` passthroughs.

**Tests:**
- `test_store.py` (extend): `create_mu()` stores `series_id`, `experiment_id`, and `cluster_id`; all three round-trip from SQLite correctly; `get_series_turn_count()` returns correct count per series_id.
- `test_store.py` (migration): run `_migrate_db()` against a pre-populated DB that has `session_id` in `analytics` and no `series_id`/`experiment_id`/`cluster_id` in `memory_units`; verify migration completes without data loss and existing values are preserved.
- `test_orchestrator.py` (extend): `RunRecord.series_id` is populated from the current session; `RunRecord.experiment_id` matches the resolved `EXPERIMENT_ID`; newly created MU has `series_id` set.
- `test_um.py` (extend): `create_um()` with `series_id` includes it in the returned `UniversalMessage`.

**Done when:**
- Every MU created after M12 has `series_id` and `experiment_id` populated in `memory_units`
- `SELECT DISTINCT series_id FROM memory_units WHERE series_id IS NOT NULL` returns one row per series from that point forward
- `analytics` table has `series_id` column (renamed from `session_id`) and `experiment_id`
- `EXPERIMENT_ID` is logged at container startup
- Admin UI shows "Series" not "Session" throughout
- Schema migration runs safely against an existing populated `graph.db`
- All 218 existing tests still pass

**Risks:**
- ⚠ `ALTER TABLE ... RENAME COLUMN` requires SQLite ≥ 3.25. Check before writing migration code — the Docker image may use an older SQLite. If < 3.25, use table-recreation pattern (create new table, copy data, drop old, rename).
- ⚠ `EXPERIMENT_ID` is a module-level constant resolved at import time — requires a container restart to pick up a new value. Log it clearly at INFO level on every interaction (not just startup) so the run record is auditable without checking container logs.
- ? Seed-fixture MUs already in `memory_units` before M12 will have `series_id = NULL`. These pre-M12 MUs are not part of any formal experiment run, so NULL is correct — flag them as "pre-experiment" in the report if they surface in a retrieval.

**Estimate:** 1-2 sessions

---

### Milestone 13 — Series Report Generator (§5)

**Why this milestone exists:** The instrumentation now captures every intermediate quantity needed to answer the per-series diagnostic questions. Without a generator that reads that data and renders a fixed, human-readable document, every series review requires a manual join across four tables and raw log inspection. The Series Report is the interface between the data layer and the researcher.

**What it builds:**

**New files:**
- `backend/analytics/reports/__init__.py`
- `backend/analytics/reports/series_report.py` — the generator
- `backend/analytics/reports/markdown_renderer.py` — JSON dict → Markdown template

**New DB table in `store.py`:**
```sql
CREATE TABLE IF NOT EXISTS generated_reports (
    report_id TEXT PRIMARY KEY,
    report_type TEXT NOT NULL,      -- "series" | "experiment"
    experiment_id TEXT NOT NULL,
    series_id TEXT,                 -- NULL for experiment-level reports
    generated_at TEXT NOT NULL,
    json_path TEXT NOT NULL,
    md_path TEXT NOT NULL
);
```

**New admin endpoints in `api/admin.py`:**
- `POST /admin/reports/generate-series` — `{experiment_id, series_id}` → generates report, stores paths in `generated_reports`, returns `report_id`
- `GET /admin/reports` — list all reports (filterable by `report_type`, `experiment_id`)
- `GET /admin/reports/{report_id}` — return the report JSON
- `GET /admin/reports/{report_id}/markdown` — return the `.md` file as plain text

**Output files** go to `/app/memory/reports/` (new subdirectory, created on first generation):
```
series_report_{experiment_id}_{series_id}_{timestamp}.json
series_report_{experiment_id}_{series_id}_{timestamp}.md
```

**Section implementations:**

**§5.1 — Failure summary**
- Failed turns: `SELECT run_id, turn_index, failure_reason FROM analytics WHERE series_id = :sid AND failure_reason IS NOT NULL ORDER BY turn_index`
- Ghost-MU check: for each MU in `memory_units WHERE source_type LIKE 'agent_output_failed%' AND series_id = :sid`, check if that `mu_id` appears in `activated_mu_ids` of any later run (by `turn_index`) in the same series. Flag any match as a ghost-MU incident. This confirms the failure mode flagged during design (failed MU being retrieved back into context) isn't recurring.

**§5.2 — Retrieval trace digest**
For each run in the series, join `analytics.traversal_path`, `analytics.entry_point_mu_ids`, `analytics.bm25_scores_per_mu`, `analytics.turn_index`. Render as a per-turn table row: `[turn N] terms:{keywords} → entry:{mu_ids} → hops:{traversal_path} → retrieved:{final_set_count} MUs`. Not raw JSON — a readable trace with MU IDs truncated to 8 chars for legibility.

**§5.3 — Intra-series vs. cross-series retrieval rate**
For each run in the series, for each `mu_id` in `analytics.activated_mu_ids`, join to `memory_units.series_id`. Classify:
- `intra_series`: `memory_units.series_id == :current_series`
- `seed_fixture`: `memory_units.series_id LIKE 'seed:%'`
- `cross_series`: any other non-NULL series_id
- `unknown`: `series_id IS NULL` (pre-M12 MUs)

Series-level rate: `intra_count / total_retrieved`. Per-turn breakdown in the JSON. Requires M12 (`series_id` on MUs).

**§5.4 — Cross-series edges**
```sql
SELECT e.edge_id, e.weight,
       s.series_id AS source_series, s.mu_id AS source_mu,
       t.series_id AS target_series, t.mu_id AS target_mu
FROM edges e
JOIN memory_units s ON e.source_mu_id = s.mu_id
JOIN memory_units t ON e.target_mu_id = t.mu_id
WHERE (s.series_id = :sid OR t.series_id = :sid)
  AND (s.series_id != t.series_id OR s.series_id IS NULL OR t.series_id IS NULL)
```
Include the other series_id and edge weight for each result.

**§5.5 — Edge weight deltas (series-scoped)**
For each `edge_id` that appears in any run's `activated_edge_ids` within this series:
- Start weight: `old_weight` from the first `weight_updates` entry (by `turn_index`) in this series for this edge; if never updated in this series, use `edges.weight` (no delta).
- End weight: `new_weight` from the last `weight_updates` entry; if never updated, use `edges.weight`.
- Delta: end - start.

Sort by `abs(delta)` descending. Flag: any edge with `delta == 0.0` that was traversed > 5 times in this series (high-traversal, zero-reinforcement — worth noting).

**§5.6 — Probe integrity check**
```sql
SELECT COUNT(*) FROM feedback_events fe
JOIN analytics a ON fe.run_id = a.run_id
WHERE a.series_id = :sid AND fe.is_probe_turn = 1
```
If count > 0: HARD FAIL — list each offending `feedback_event_id`, `run_id`, `turn_index`. If count == 0: PASS. This is not a soft warning; per the spec, any violation is a data-integrity flag.

**§5.7 — Mode-3 trigger count**
```sql
SELECT mu_id, raw_content, created_at FROM memory_units
WHERE source_type = 'system' AND series_id = :sid
ORDER BY created_at
```
Count = number of Mode-3 summarization MUs. Anomaly check: flag any system MU where `len(raw_content) < 100` (suspiciously short for a summarization output — possible truncation). Threshold is configurable via `config.yaml` (add `reporting.mode3_min_expected_length: 100`).

**§5.8 — num_ctx pressure**
```sql
SELECT turn_index, tokens_in, json_array_length(activated_mu_ids) as retrieved_count
FROM analytics WHERE series_id = :sid ORDER BY turn_index
```
Flag any turn where `tokens_in > 0.8 * context_window_tokens` (config: `model.context_window_tokens`). For flagged turns, note whether `retrieved_count` is above the series mean — if yes, attribute to retrieval-volume growth (the regression check tied to the ceiling discussion).

**Tests:**
- `test_series_report.py` (new):
  - §5.1: ghost-MU check correctly identifies a failed MU `mu_id` appearing in a later turn's `activated_mu_ids`; a clean series produces ghost_mu_count=0.
  - §5.3: correctly classifies intra-series, seed, cross-series, and unknown MUs; rate computation matches manual calculation.
  - §5.5: start/end weight correctly derived from `weight_updates` first/last entries; zero-delta edges with high traversal count are flagged.
  - §5.6: probe integrity check returns HARD FAIL when a fabricated `feedback_events` row has `is_probe_turn=1`; PASS on clean data.
  - §5.7: mode-3 anomaly flag fires when a system MU has `len(raw_content) < 100`.
  - §5.8: pressure flag fires when `tokens_in > 0.8 * context_window_tokens`; retrieval-volume attribution correct.
- `test_markdown_renderer.py` (new): all 8 sections render to non-empty Markdown; each section header is present; JSON round-trips correctly from the renderer input.
- Manual: `POST /admin/reports/generate-series` produces `.json` and `.md` files in `/app/memory/reports/`; `GET /admin/reports` lists the new report; `GET /admin/reports/{id}/markdown` returns the file content.

**Done when:**
- `POST /admin/reports/generate-series` produces both files for a completed series
- All 8 §5 sections have real data from a completed series (not mocked)
- Ghost-MU check runs correctly against real data
- Probe integrity check passes (verified by confirming `feedback_events.is_probe_turn` is never 1 on probe runs)
- Report files written to `/app/memory/reports/` and queryable via `GET /admin/reports`
- All 218+ existing tests still pass

**Risks:**
- ⚠ Requires M12 complete first — §5.3 and §5.4 will produce all-NULL `series_id` results if M12 hasn't run.
- ⚠ Verify `source_type = 'system'` is the correct type for Mode-3 summarization MUs before building §5.7 — check `memory/extractor.py` at build time.
- ? §5.5 start/end weight: if an edge was created and its first traversal produced an update, `old_weight` in the first `weight_updates` entry equals `initial_edge_weight`. But if the edge was created in an earlier series and only traversed (no update) in this series, the "start weight" is the inherited weight from the prior series — note this in the report output so the researcher knows the delta is series-scoped, not edge-lifetime-scoped.
- ? `activated_mu_ids` in `analytics` is stored as a JSON array — use `json_each()` in SQLite to unnest it for joins. Verify this SQLite feature is available in the Docker image.

**Estimate:** 2-3 sessions

---

### Milestone 14 — Experiment Report: Non-Learning Rollup + Integrity Checks (§5 aggregation + §6.3 + §6.4)

**Why this order:** Before building the learning measures (§6.1, the hardest analytical section), build the structural skeleton of the Experiment Report — §5 rolled up across all series in the experiment, plus the data-integrity checks that must pass before any learning comparison is valid. This milestone produces a complete, usable report even without the learning-measures module; it also validates the aggregation plumbing before adding the state-transition and bridge-MU logic.

**What it builds:**

New file: `backend/analytics/reports/experiment_report.py`

New admin endpoint: `POST /admin/reports/generate-experiment` — `{experiment_id}` → generates experiment report, stores in `generated_reports`, returns `report_id`. The output embeds all §5 series data, §6.3 integrity checks, and §6.4 caveat text. §6.1 (learning measures) and §6.2 (control shadow) are added as empty/placeholder keys in the JSON structure so the schema is stable before Milestones 15 and 16 fill them in.

**Output files:**
```
experiment_report_{experiment_id}.json
experiment_report_{experiment_id}.md
```

**§5 aggregation across series:**
For each §5 section, aggregate across all series where `analytics.experiment_id = :eid`:
- §5.1: total failure count; per-series breakdown; list of ghost-MU incidents across the experiment.
- §5.3: mean intra-series retrieval rate ± range across series; if any series is far from the mean, flag it.
- §5.5: aggregate edge weight change ledger — total edges touched, count increased / decreased / unchanged, mean |delta|.
- §5.6: aggregate probe integrity — any failure in any series is a hard flag for the whole experiment.
- §5.7: total Mode-3 trigger count; flag if later series trigger Mode-3 more often than earlier ones (retrieval growth leading to summarization pressure).
- §5.8: turn count above num_ctx threshold per series; flag if the count is growing (increasing context pressure = expected, but worth surfacing).

Series reports are generated independently via M13 — this milestone reads from `generated_reports` to find existing series reports and either reads their JSON or re-queries the DB directly. Prefer re-querying (so the experiment report is always current, not dependent on a prior series report generation run).

**§6.3 — Data-integrity / confound checks:**

**Seed-fixture provenance check:**
- Read `seed_fixture_manifest` for the fixture version used in this experiment.
- If multiple experiments exist, verify their `manifest_hash` values match (same starting graph).
- Query: for each series in this experiment, check if any MU with `series_id LIKE 'seed:%'` has a `series_id` that doesn't match the registered fixture version → flag as provenance mismatch.

**Graph-wipe verification:**
- For this experiment, check that no `memory_units` row with a different `experiment_id` (not NULL, not `seed:*`) existed before the earliest run in this experiment.
- Query: `SELECT COUNT(*) FROM memory_units WHERE experiment_id IS NOT NULL AND experiment_id != :eid AND experiment_id NOT LIKE 'seed:%' AND created_at < (SELECT MIN(timestamp) FROM analytics WHERE experiment_id = :eid)`. If count > 0: flag as possible incomplete wipe.

**Probe-replay-index normalization check:**
- Verify `measurement_session_index` values in `probe_results` for this experiment are contiguous (no gaps: 1, 2, 3, ... not 1, 3, 5).
- If gaps exist, list them — report a gap as a missing measurement session (no data for that session index, which will appear as a gap in the learning curves).
- Report probe learning curves with x-axis as `measurement_session_index` (not wall-clock time or `turn_index`).

**§6.4 — Statistical caveat (rendered text in template):**
Hard-coded in the Markdown template — not a computed field. The text is:
```markdown
## §6.4 Statistical Note

At ~100+ prompts total per experiment, this report states raw counts and simple
deltas without implying statistical significance the sample size cannot support.
All reported differences are directional signals for hypothesis generation, not
confirmed effects.
```

**JSON structure (keys present but empty/null for §6.1 and §6.2 until later milestones):**
```json
{
  "experiment_id": "...",
  "condition_label": "...",
  "generated_at": "...",
  "series_summary": { ... },       // §5 aggregated
  "integrity_checks": { ... },    // §6.3
  "learning_measures": null,      // filled by Milestone 15
  "control_shadow": null          // filled by Milestone 16
}
```

**Tests:**
- `test_experiment_report.py` (new):
  - §5 aggregation: total failure count is sum of per-series counts; mean intra-series rate is correct for synthetic multi-series data.
  - §6.3 seed-fixture provenance: check passes when experiment has one fixture version; fails when a fabricated MU has a mismatched `series_id`.
  - §6.3 graph-wipe: check passes on clean data; fires when a fabricated pre-experiment MU from a different experiment_id is present.
  - §6.3 probe-replay-index normalization: correctly identifies a gap in session indices (1, 2, 4 → gap at 3).
  - §6.4 statistical caveat text appears verbatim in Markdown output.
  - JSON output has `learning_measures: null` and `control_shadow: null` placeholder keys.

**Done when:**
- `POST /admin/reports/generate-experiment` produces valid JSON + Markdown for an experiment with at least 2 completed series
- All three §6.3 integrity checks correctly pass on clean data and fire on fabricated violations
- §6.4 statistical caveat text present in every generated experiment report Markdown
- `learning_measures` and `control_shadow` keys present in JSON as null (ready for M15/M16)
- All 218+ existing tests still pass

**Risks:**
- ⚠ Requires M12 (for `experiment_id` in `analytics` and `memory_units`) and M13 (for the series-level section structure to borrow from).
- ⚠ Graph-wipe verification relies on `memory_units.experiment_id` being populated — MUs created before M12 will have `experiment_id = NULL`, which may generate false positives. Treat `NULL` experiment_id as "pre-experiment / unattributed" and exclude from the wipe check.
- ? The §5 aggregation re-queries the DB directly rather than reading series report JSON files. This is intentional (always current) but means the aggregation SQL must handle cases where series reports haven't been generated yet — which is the normal case for a mid-experiment rollup.

**Estimate:** 2-3 sessions

---

### Milestone 15 — Experiment Report: Learning Measures (§6.1)

**Why its own milestone:** The learning measures section is the analytical core of the whole reporting system. State-transition detection, bridge-MU rank trajectory, and saturation checks require non-trivial logic that doesn't share code paths with any other section. The spec explicitly calls this out as the most logic-heavy piece. Isolating it means Milestone 14 can be validated (and used) while this section is still being built.

**What it builds:**

Extends `experiment_report.py` with the §6.1 section generator. Adds to `POST /admin/reports/generate-experiment` so it now populates the `learning_measures` key in the JSON.

**§6.1 — Learning measures:**

**Probe outcome summary — state transitions:**

For each probe in `probes`, across all `probe_results` for this experiment ordered by `measurement_session_index`:

1. Classify each result as:
   - `pass`: `gold_mu_rank IS NOT NULL AND gold_mu_rank <= pass_rank_threshold` (add `reporting.pass_rank_threshold: 3` to `config.yaml` — a gold MU in the top 3 retrieved MUs counts as a pass)
   - `fail`: `gold_mu_rank IS NULL OR gold_mu_rank > pass_rank_threshold`
   - `negative_flagged`: explicit override (requires adding `outcome_override: Optional[str]` to `probe_results` table — see open items)

2. Compute state sequence: `[fail, fail, pass, pass, pass]`

3. Identify transitions:
   - `fail_to_pass`: session N is fail, session N+1 is pass → learning event
   - `pass_to_fail`: session N is pass, session N+1 is fail → regression event
   - `flat_fail`: all sessions are fail → no learning detected for this probe
   - `flat_pass`: all sessions are pass → probe may not be discriminative (learned from fixtures, not from the experiment)

4. Aggregate by `phenomenon_type` (across all probes with that tag). Roll up for the whole experiment.

**MUs pulled by probe prompts, segmented by outcome:**
For each `probe_result`, group by classified outcome (pass / fail). List `retrieved_mu_ids`, `source_type`, and `series_id` for each retrieved MU. The data structure enables the "what was retrieved when it worked vs. when it didn't" comparison directly from the JSON.

**Edge weight delta ledger:**
```sql
-- All weight_updates across this experiment, enriched with edge endpoints
SELECT wu.edge_id, wu.old_weight, wu.new_weight,
       wu.new_weight - wu.old_weight AS delta,
       wu.feedback_event_id,
       e.source_mu_id, e.target_mu_id,
       s.cluster_id AS source_cluster, t.cluster_id AS target_cluster
FROM analytics a,
     json_each(a.weight_updates) AS wu_raw,
     -- parse the JSON weight_updates array
     edges e ON e.edge_id = wu_raw.value->>'edge_id'
JOIN memory_units s ON e.source_mu_id = s.mu_id
JOIN memory_units t ON e.target_mu_id = t.mu_id
WHERE a.experiment_id = :eid
```

(Note: the exact SQLite JSON unnesting syntax will depend on the version — use `json_each()` with the correct path.)

- Count: increased, decreased, unchanged.
- Magnitude: min, max, mean |delta|.
- **Orphan update check:** count entries where `feedback_event_id IS NULL` — should be 0 in the learned condition (every weight update traces to a feedback event). Report the count explicitly; a non-zero count is a process violation, not a result.
- **Within-cluster vs. cross-cluster segmentation:** using `source_cluster` and `target_cluster` from the join. Cross-cluster = `source_cluster != target_cluster AND source_cluster IS NOT NULL AND target_cluster IS NOT NULL`. Report cross-cluster deltas in their own subsection — this is the more diagnostic signal.

**Cross-series MUs underlying probe results:**
For each `probe_result` in this experiment: for each `mu_id` in `retrieved_mu_ids`, report its `series_id` and `cluster_id`. The output is a per-probe, per-session table showing which series contributed to each retrieval — directly answers whether cross-series MUs appear in probe retrievals.

**Bridge-MU / cross-cluster transfer subsection:**

For each probe where `is_bridge_probe = true`:
1. Collect `gold_mu_rank` across all `measurement_session_index` values → the rank trajectory. Lower ranks at later indices = bridge MU surfacing more reliably = possible transfer signal.
2. For each rank improvement (session N rank > session N+1 rank): identify which reinforcement events preceded that improvement.
   - A "reinforcement event" = a `feedback_events` row with `verdict = 'thumbs_up'` in any series belonging to this experiment.
   - "Cluster-A-only reinforcement" = all reinforcement events in the window between session N and N+1 have `run_id` that maps to series where the touched MUs have `cluster_id = 'cluster_a'`.
3. If a bridge-MU rank improvement on a cluster-B probe follows cluster-A-only reinforcement: flag as a **cross-cluster transfer candidate** — the strongest available evidence for genuine learning.
4. Report explicitly, with the reinforcement event IDs and session indices, so the researcher can inspect the raw data.

**Saturation / runaway-reinforcement check:**
For each edge in this experiment:
1. Collect its weight update sequence chronologically.
2. Flag as **saturation risk** if: `edges.weight > saturation_threshold` (add `reporting.saturation_threshold: 0.9` to `config.yaml`).
3. Flag as **runaway reinforcement** if: any three consecutive deltas are each larger than the previous (accelerating, not diminishing). The concern: unbounded positive feedback on a small dataset.
4. Report flagged edges with their current weight and delta sequence.

**Tests:**
- `test_learning_measures.py` (new):
  - State-transition detection: `[fail, fail, pass, pass]` → 1 fail_to_pass, 0 pass_to_fail; `[pass, fail, pass]` → 1 fail_to_pass, 1 pass_to_fail; `[fail, fail, fail]` → flat_fail.
  - Probe aggregation by phenomenon_type: correct counts per type for multi-probe synthetic data.
  - Orphan update count: 0 for data with all `feedback_event_id` populated; N for fabricated orphans.
  - Cross-cluster edge segmentation: correctly separates within-cluster from cross-cluster edges using `cluster_id`.
  - Saturation check: fires at weight > 0.9; does not fire at 0.85.
  - Runaway-reinforcement: fires for deltas [0.05, 0.07, 0.10]; does not fire for [0.10, 0.07, 0.05] (diminishing).
  - Bridge-MU rank trajectory: correctly identified rank improvement in the right session window.

**Done when:**
- `POST /admin/reports/generate-experiment` now populates the `learning_measures` key in the JSON (not null)
- State-transition analysis produces correct output for all transition types (verified against synthetic test data)
- Orphan update count is 0 for clean test data
- Bridge-MU rank trajectory renders correctly for the bridge probes in `probes` table
- Saturation and runaway-reinforcement checks fire correctly on fabricated cases
- All 218+ existing tests still pass

**Risks:**
- ⚠ SQLite JSON unnesting syntax for `weight_updates` (stored as a JSON array in `analytics`): `json_each()` is available in SQLite 3.38+ (2022). For older SQLite, a Python-side post-processing approach is needed (load the JSON in Python, unnest in memory, then query edges). Verify during build and choose the appropriate approach.
- ⚠ `negative_flagged` probe outcome state: the spec defines a tri-state (pass / fail / negative-flagged). Adding `outcome_override TEXT` to `probe_results` requires a DB migration. If not added, `negative_flagged` probes can't be excluded from averages — which the spec warns can mask regressions. Decide at the start of M15: add the column (recommended) or document the limitation.
- ⚠ Bridge-MU cross-cluster transfer detection requires `cluster_id` on MUs (added in M12) and that seed MUs were actually tagged with `cluster_id` during fixture generation. If `cluster_id` is NULL on most MUs, this analysis degrades gracefully to "cluster_id unknown" — report that rather than silently omitting the section.
- ? `pass_rank_threshold` default of 3: if the typical retrieved set size is small (e.g., K2 = 1 by default config), a gold MU at rank 1 or 2 is the only realistic pass. Verify the threshold is appropriate for the actual K1/K2 config values before finalizing.

**Estimate:** 3-4 sessions

---

### Milestone 16 — Experiment Report: Control Shadow + Admin UI Integration (§6.2 + Reports Dashboard)

**Why last:** The control shadow requires running §6.1 analysis against two additional experiments (static-weight and random-weight conditions). Those experiments don't exist until after the learned condition has been run and the report system (M13-M15) has been validated. The admin dashboard integration waits until both report types are complete so the UI can present them coherently.

**What it builds:**

**§6.2 — Control shadow:**

The control shadow is a sub-structure embedded in the learned-condition experiment report — not a separate file. Per the spec: "the comparison can't drift out of sync with the primary results."

Extends `POST /admin/reports/generate-experiment` with an optional body parameter:
```json
{
  "experiment_id": "exp-2026-07-01-learned",
  "control_experiment_ids": {
    "static_weight": "exp-2026-07-10-static",
    "random_weight": "exp-2026-07-10-random"
  }
}
```

When `control_experiment_ids` is provided:
1. Run the full §6.1 learning-measures analysis for each control experiment.
2. Run control-specific sanity checks:
   - **Static control — `zero_weight_changes`:** `SELECT COUNT(*) FROM analytics WHERE experiment_id = :static_eid AND json_array_length(weight_updates) > 0`. Must be 0. A non-zero count is a bug (static control should have no weight changes), not a result.
   - **Random control — `weights_rerandomized`:** verify that edge weight variance across runs is not near-zero (which would indicate weights were accidentally frozen). If the standard deviation of sampled edge weights across series is < `reporting.random_control_variance_floor: 0.05`, flag as possibly static.
   - **Both controls — `zero_feedback_edge_correlation`:** for each feedback event in the control experiment, verify `weight_updates` for that run is empty. Count violations — should be 0 (feedback is causally inert in both controls by design).
3. Compute comparative deltas for each learning measure: `learned_value - control_value`:
   - Final probe pass-rate: learned vs. static, learned vs. random
   - Bridge-MU rank at final measurement session: learned vs. controls
   - Mean edge weight delta magnitude: learned vs. controls
   - Mean gold_mu_rank at final session: learned vs. controls
4. Embed in `experiment_report_{experiment_id}.json` under `control_shadow` key:
```json
{
  "control_shadow": {
    "static_weight": {
      "experiment_id": "...",
      "learning_measures": { ... },
      "sanity_checks": {
        "zero_weight_changes": true,
        "zero_feedback_edge_correlation": 0
      },
      "comparative_deltas": {
        "probe_pass_rate_delta": 0.32,
        "bridge_mu_rank_delta": -2.7,
        "edge_weight_delta_magnitude": 0.18
      }
    },
    "random_weight": { ... }
  }
}
```

**Admin UI — Reports Dashboard:**

New files:
- `admin/src/pages/Reports.jsx` — reports page
- `admin/src/components/ReportsList.jsx` — filterable list with view/download
- `admin/src/components/ExperimentComparison.jsx` — side-by-side comparison table when control shadow is present
- `admin/src/components/ReportViewer.jsx` — renders Markdown content as HTML (use `react-markdown` package)

Add `Reports` tab to `admin/src/App.jsx` (alongside Runs, Graph, Config, Probe Results).

Reports page layout:
```
┌──────────────────────────────────────────────────────────┐
│  Filter: experiment_id ▾  type ▾     [Generate New...]   │
├──────────────────────────────────────────────────────────┤
│  📄 experiment_report_learned-2026-07-01  [View][↓JSON]  │
│  📄 series_report_learned_series-1        [View][↓JSON]  │
│  📄 series_report_learned_series-2        [View][↓JSON]  │
└──────────────────────────────────────────────────────────┘

(when experiment report has control_shadow — comparison table auto-renders)

┌──────────────────────────────────────────────────────────┐
│  Condition Comparison                                     │
│  Measure                   Learned   Static   Random     │
│  ──────────────────────── ───────── ──────── ─────────   │
│  Final probe pass rate      73%       41%      38%       │
│  Bridge-MU rank (final)      2.1       4.8      5.1      │
│  Edge Δ magnitude (mean)    0.18      0.00     0.02      │
│  Orphan updates               0         0        0       │
└──────────────────────────────────────────────────────────┘
```

The "Generate New..." button opens a small form: select `experiment_id`, optional `control_experiment_ids`. Calls `POST /admin/reports/generate-series` or `POST /admin/reports/generate-experiment` depending on report type selected.

**New backend dependency:** add `react-markdown` to `admin/package.json` for in-browser Markdown rendering.

**Tests:**
- `test_control_shadow.py` (new):
  - Static-control `zero_weight_changes` check passes on clean data; fails when a fabricated `weight_updates` entry is present for a static-control run.
  - Random-control `weights_rerandomized` check fails on fabricated near-zero variance data; passes when variance > floor.
  - `zero_feedback_edge_correlation` check: fires when a fabricated feedback event for a control run has non-empty `weight_updates`; passes on clean data.
  - Comparative delta computation: correct values for synthetic learned/control pairs.
- Manual (Admin UI): generate an experiment report with control shadow IDs → Reports page shows the comparison table with correct values; "View" button renders the Markdown in the browser; "↓JSON" downloads the raw JSON.

**Done when:**
- `POST /admin/reports/generate-experiment` with `control_experiment_ids` produces a report with embedded `control_shadow` (not null)
- All three sanity checks run correctly and fire on fabricated violations
- Comparative deltas appear in JSON and Markdown for all learning measures
- Admin UI Reports page lists all generated reports with view/download
- Comparison table auto-renders when a control shadow is present in the selected experiment report
- Markdown files render correctly in the browser (headers, tables, code blocks)
- All 218+ existing tests still pass

**Risks:**
- ⚠ The control shadow can only be fully tested with real control-condition experiment data. Build M16 against synthetic test data; the real validation happens after the three-condition experiment run.
- ⚠ `react-markdown` must handle tables (GFM extension) — the report Markdown uses `|`-delimited tables throughout. Verify this renders correctly; if not, use `remark-gfm` plugin alongside `react-markdown`.
- ? If probe set versions differ between the learned condition and a control (should never happen), the comparison table displays incomparable numbers. Add a probe-set-version match check to the control shadow pre-computation — if versions differ, refuse to generate the comparison and return an error with the mismatch.
- ? `Generate New...` form in the Reports page: keep it minimal (text inputs for experiment IDs). Don't add autocomplete or dropdowns that require a new `/admin/experiments` endpoint — that's out of scope for this milestone.

**Estimate:** 2-3 sessions

---

### Milestone 17 — End-to-End Integration + Hardening

**Why this milestone exists:** Every milestone from M1-M16 built and tested against synthetic fixtures. The platform has never been run for its actual research purpose: a three-condition experiment (learned / static-weight / random-weight) generating real data and a real report suite. Several known risks from prior milestones can only be resolved with real data flowing through the full stack. M17 closes that gap.

```
┌─────────────────────────────────────────────────────────────┐
│          Three-Condition Experiment Flow                     │
│                                                             │
│  Seed Graph ──▶ Probe Snapshot                              │
│       │                │                                    │
│       ▼                ▼                                    │
│  ┌──────────┐   ┌─────────────────────────────────────────┐ │
│  │  Learned │   │  Probe Battery (same probes, all runs)  │ │
│  │ Condition│──▶│  Runs at: start / mid / end of series   │ │
│  │ (M1-M11) │   └─────────────────────────────────────────┘ │
│  └────┬─────┘                    │                          │
│       │ weight updates on        │ probe results            │
│       ▼                          ▼                          │
│  ┌──────────┐   ┌────────────────────────────────────────┐  │
│  │  Static  │   │  Analytics DB                          │  │
│  │  Control │──▶│  run_log / probe_results /             │  │
│  │ (frozen  │   │  feedback_events / weight_updates      │  │
│  │ weights) │   └──────────────────┬─────────────────────┘  │
│  └────┬─────┘                      │                        │
│       │                            ▼                        │
│  ┌──────────┐   ┌────────────────────────────────────────┐  │
│  │  Random  │   │  Report Generators (M13-M16)           │  │
│  │  Control │──▶│  series_report_{id}.json / .md         │  │
│  │ (per-run │   │  experiment_report_{id}.json / .md     │  │
│  │ rerandom)│   │  + control_shadow embedded              │  │
│  └──────────┘   └──────────────────┬─────────────────────┘  │
│                                    │                        │
│                                    ▼                        │
│                      ┌─────────────────────────┐           │
│                      │  Admin UI Reports Tab   │           │
│                      │  comparison table       │           │
│                      │  Markdown rendering     │           │
│                      └─────────────────────────┘           │
└─────────────────────────────────────────────────────────────┘
```

**What it builds:**

**Phase 1 — Environment smoke test:**
1. Merge all pending PRs (M9–M16 branches) to main in order. Resolve any merge conflicts.
2. Run `docker-compose up` — all three services (backend, chat UI, admin UI) must reach healthy state.
3. Run the full test suite: all 218+ existing tests must pass before any experiment begins.
4. Smoke-test the admin UI: Runs, Graph, Config, Probe Results, and Reports tabs must all load without errors.
5. Verify Ollama connectivity: `GET /ollama-health` returns OK; a simple test chat turn completes.

**Phase 2 — Seed graph + probe setup:**
1. `POST /admin/seed-graph` — initialize the seed fixture with cluster_id tags on all seed MUs.
2. Verify via `GET /admin/graph/stats` that all seed MUs have `cluster_id` populated (not NULL). If any are NULL, the bridge-MU cross-cluster transfer analysis in the experiment report will degrade; document and proceed.
3. `POST /admin/probes/snapshot` — create the baseline probe snapshot.
4. Confirm the probe set version appears in the admin Probe Results tab.
5. Check `config.yaml` K2 value: if K2 = 1, update `reporting.pass_rank_threshold` to 1 (otherwise all probes will fail trivially). This is the M15 risk deferred to M17.

**Phase 3 — Three-condition experiment run:**

Run each condition as a separate experiment, recording its `experiment_id` in `config.yaml`. For each condition:
- Start with a short pilot (3 series × 5 turns) to validate the pipeline end-to-end before committing to a full run.
- After the pilot passes, run the full planned N series.
- After each series: call `POST /admin/reports/generate-series` to confirm the series report generates without error.

**Learned condition** (`experiment_id: exp-learned-YYYY-MM-DD`):
- Weight updates enabled (`WEIGHT_UPDATES_ENABLED=true` in config)
- Feedback loop active (researcher provides thumbs-up/thumbs-down via admin UI after each turn)
- Probe battery at series_index 0, 2, 4 (start, mid, end of each series)
- Target: 5 series

**Static-weight control** (`experiment_id: exp-static-YYYY-MM-DD`):
- Weight updates disabled (`WEIGHT_UPDATES_ENABLED=false`)
- Same probe set, same series count
- After run: `SELECT COUNT(*) FROM analytics WHERE experiment_id = :static_eid AND json_array_length(weight_updates) > 0` must return 0 (M16 sanity check)

**Random-weight control** (`experiment_id: exp-random-YYYY-MM-DD`):
- Weights re-randomized at the start of each series (reset to uniform random in [0, 1])
- Same probe set, same series count
- After run: verify edge weight variance across series is > `reporting.random_control_variance_floor: 0.05`

**Phase 4 — Full report generation and validation:**
1. `POST /admin/reports/generate-experiment` for the learned condition — verify `learning_measures` key is not null in the response JSON.
2. `POST /admin/reports/generate-experiment` for learned condition WITH `control_experiment_ids` body parameter — verify `control_shadow` key is populated for both static and random controls.
3. Check all three control shadow sanity checks:
   - `zero_weight_changes`: true for static control
   - `weights_rerandomized`: true for random control
   - `zero_feedback_edge_correlation`: 0 violations for both controls
4. Open the Admin UI Reports tab — verify:
   - All generated reports appear in the list
   - "View" renders the Markdown content (headers, tables, code blocks)
   - "↓JSON" downloads the raw JSON correctly
   - Comparison table renders with the correct values from the control shadow
5. Verify GFM table rendering specifically: the report Markdown uses pipe-delimited tables throughout. If `react-markdown` without `remark-gfm` renders them as plain text, add the `remark-gfm` plugin (this is the M16 risk deferred to real data).

**Phase 5 — Integration tests:**

New file: `tests/integration/test_e2e_pipeline.py`

This file covers the full pipeline with real database writes (not mocks). It requires the Docker stack to be running and targets `localhost:8000`:
- `test_chat_turn_creates_memory_units`: POST to `/chat`, verify ≥ 2 MUs appear in graph store (user_input + agent_output at minimum).
- `test_feedback_event_triggers_weight_update`: POST a thumbs-up feedback event, verify a `feedback_events` row exists and at least one edge has a non-empty `weight_updates` JSON array.
- `test_series_report_generates_all_sections`: Generate a series report for a completed series, verify all §5 subsection keys are present and non-null in the JSON.
- `test_experiment_report_learning_measures_populated`: Generate an experiment report for a completed experiment, verify `learning_measures` key is not null and contains at least `probe_pass_rates` and `state_transitions`.
- `test_control_shadow_sanity_checks_pass`: Generate an experiment report with control shadow for matching static and random conditions, verify all three sanity checks return expected values.

Mark the integration test file with `pytest.mark.integration` and exclude from the default test run (add `--ignore=tests/integration` to `pytest.ini` default args). Run integration tests explicitly: `pytest tests/integration/ -m integration`.

**Phase 6 — Hardening + documentation:**
1. Fix any bugs discovered in phases 1-5. For each bug: write a regression unit test before fixing.
2. Verify Docker healthcheck for the backend correctly reflects database availability (not just process up).
3. Add comprehensive inline comments to `config.yaml` for all hyperparameters — document the default value, the reasoning behind it, and valid range (this is a research artifact and must be self-explaining).
4. Write `docs/experiment_protocol.md` (new): step-by-step guide for running a three-condition experiment from scratch (seed → probe setup → learned run → static run → random run → generate reports). Include the exact API calls and expected outputs at each step.
5. Update PROGRESS.md with: research outcomes, probe pass-rate results across conditions, any anomalies observed, and open questions for future work.

**Tests:**
- `tests/integration/test_e2e_pipeline.py` (new) — integration tests described in Phase 5 above
- Regression unit tests for any bugs found during the integration run
- All 218+ existing tests must still pass

**Done when:**
- All pending PRs (M9–M16) merged to main, no merge conflicts outstanding
- `docker-compose up` starts all three services cleanly (backend, chat UI, admin UI)
- Full test suite (218+ tests) passes
- Three-condition experiment run completed: ≥ 3 series per condition with probe batteries at start/mid/end
- Full report suite generated: series reports for each condition + experiment report with control shadow
- All three control shadow sanity checks pass on real data
- Admin UI Reports tab renders reports with correct Markdown and comparison table
- Integration test suite passes (`pytest tests/integration/`)
- `config.yaml` has inline comments on all hyperparameter fields
- `docs/experiment_protocol.md` exists and covers the full run procedure
- PROGRESS.md updated with research outcomes

**Risks:**
- ⚠ Pending PRs (M9–M15 still show "PR pending merge" in the Status section). These must be merged before M17 begins — merge conflicts between M9 and M15 are likely since they all touch the analytics and graph store layers. Resolve in milestone order (M9 first, M10 second, etc.) to minimize conflict surface.
- ⚠ `pass_rank_threshold` default of 3 (from M15): if the actual config has K2 = 1 (default), every probe will trivially fail against this threshold since only 1 MU is returned. Check `config.yaml` immediately in Phase 2 and correct before running.
- ⚠ The full three-condition experiment requires real Ollama inference time: 3 conditions × 5 series × ~10 turns = ~150 Ollama calls. At typical Gemma 4 E4B inference speeds on the RTX 5060, this may take 2-4 hours of wall-clock time. Plan for an uninterrupted run.
- ⚠ GFM table rendering: `react-markdown` does not render pipe-delimited tables by default. The M16 build plan deferred this risk to M17. If tables render as plain text, add `remark-gfm` to the admin package dependencies.
- ? `cluster_id` on seed MUs: if `cluster_id` was not populated during seed graph initialization, cross-cluster transfer analysis will produce "cluster_id unknown" results throughout. This is acceptable — document the gap rather than retroactively patching the seed fixture.
- ? Docker resource limits under sustained load: 150 Ollama calls in sequence may trigger OOM if the GPU is also running other processes. Monitor GPU memory during the pilot run.

**Estimate:** 3-5 sessions

---

---

### Milestone 18 — Seed Fixture v2 + Probe Set v2 (Experiment Pilot Prep)

**Why this milestone exists:** seed_v1 (6 MUs, 1 bridge, 5 probes) was built to smoke-test the pipeline machinery. It cannot produce a measurable cross-cluster transfer signal — the clusters share only 2–3 MUs each, the lexical separation is minimal, and the graph has no edges at all (edges are empty in the fixture). seed_v2 expands to 34 MUs across 4 conceptual clusters plus 2 bridge MUs, with explicit hub-and-spoke graph wiring, to give the 100+ prompt pilot a fixture rich enough to produce a signal. seed_v1 stays unchanged; it remains the smoke-test fixture.

**Pre-build inspection results (all four required inspections completed):**

**1. seed_v1.json.gz schema (confirmed):**
```json
{
  "timestamp": "ISO-8601",
  "mu_count": 6,
  "edge_count": 0,
  "memory_units": [
    {
      "mu_id": "UUID",
      "source_type": "agent_output",
      "raw_content": "...",
      "created_at": "ISO-8601",
      "cluster_id": "cluster_a | cluster_b | bridge_ab"
    }
  ],
  "edges": []
}
```
`edges` is an empty list — seed_v1 has zero graph edges. The bridge concept in v1 was purely lexical (bridge MU content straddled both vocabularies). No graph traversal was needed or possible.

**2. register_fixture() genericity:**
`POST /admin/seed-graph {"version": "v2"}` works today with zero code changes — the endpoint constructs `fixture_path = seed_fixtures/seed_{version}.json.gz` and passes `version` through. **However**, `register_fixture()` processes only `memory_units`; it reads `data.get("edges", [])` but never acts on it. Since seed_v1 has no edges this was never an issue. For seed_v2 the hub-and-spoke + bridge wiring must be stored in the fixture and applied on registration. This is a small, clearly-flagged infrastructure change (Phase 0 below).

**3. probe_set_v1.yaml schema (confirmed):**
Top-level: `{version, phenomenon_map, probes: [...]}`.
Per probe: `{probe_id, phenomenon_type, prompt_text, gold_mu_ids: [], is_bridge_probe, bridge_pair_id}`.
`probe_set_version` is added by `load_probe_set()` at load time from the top-level `version` key.

Two v1 hardcodes require fixing before v2 probes are accessible:
- `backend/main.py:32` — `load_probe_set(version="v1")` at startup
- `backend/api/admin.py:237` — `load_probe_set(version="v1")` inside `run_probe`

**4. Bridge MU wiring (v1 has no pattern — v2 introduces this concept):**
v1's bridge_ab MU has no edges. The traversal funnel follows outgoing directed edges from K1 MUs (`source_mu_id` → `target_mu_id`). For the bridge to appear as K2 via graph traversal:
- The hub must be a K1 entry point (it must surface via BM25 + high edge_weight_sum)
- The hub must have an OUTGOING edge to the bridge: `source=hub, target=bridge, direction="forward"`
- Spokes boost the hub's edge_weight_sum: `source=spoke, target=hub, direction="forward"` × 7 per cluster
- With initial_edge_weight=0.5: hub edge_weight_sum = 4.0 (8 edges), bridge edge_weight_sum = 0.5 (1 edge), spokes = 0.5 each. Hub dominates K1.

**Two design gaps — resolutions decided:**

> **Gap A — cross_cluster_transfer pass criterion (RESOLVED: per-phenomenon thresholds, in scope for Phase 0):**
> With K1=3 and K2=1, the bridge MU can only appear at rank 4 (the single K2 slot). A global `pass_rank_threshold: 1` would make cross-cluster probes permanently FAIL regardless of training. **Resolution:** Make pass criteria phenomenon-aware. `ranking_improvement` keeps rank ≤ 1. `cross_cluster_transfer` passes on presence in the retrieved set at rank ≤ 4 (K1+K2 = 3+1), since the bridge structurally occupies the K2 slot. `process_rule_adherence` keeps rank ≤ 1. Raw `gold_mu_rank` is always stored unchanged in `probe_results` — report sections always have full data regardless of which threshold applies. Code change is Phase 0.4 below.

> **Gap B — process_rule_adherence output compliance (DOCUMENT ONLY, deferred):**
> The current probe evaluation only measures `gold_mu_rank` (retrieval rank). It cannot verify that the agent's actual output complied with the rule in the retrieved MU. The 2 process_rule_adherence probes in v2 are valid retrieval probes — they measure whether the rule MU is surfaced. The compliance check is a human review step, not a machine check. Noted in probe_set_v2.yaml comments and added to PROGRESS.md "Open questions for future work" as a candidate for a future automation-focused milestone (output-compliance grading via judge/evaluator). No code change in this milestone.

---

**Structure:**

| cluster_id | concept | MU count |
|---|---|---|
| cluster_a | structured file output / format discipline | 8 |
| cluster_b | schema and data-shape reasoning | 8 |
| cluster_c | rule-following under constraint | 8 |
| cluster_d | self-sourcing / provenance discipline | 8 |
| bridge_ab | "structure is a contract" | 1 |
| bridge_cd | "constraint over fluency" | 1 |
| **Total** | | **34** |

**Graph wiring (per cluster):**
```
spoke_1 ──▶ hub ──▶ bridge
spoke_2 ──▶ hub
spoke_3 ──▶ hub
spoke_4 ──▶ hub
spoke_5 ──▶ hub
spoke_6 ──▶ hub
spoke_7 ──▶ hub

cluster_a + cluster_b: connect to bridge_ab
cluster_c + cluster_d: connect to bridge_cd
NO edges between cluster_a/b content and cluster_c/d content
```

Each edge: `direction="forward"`, `weight=initial_edge_weight (0.5)`, `creation_trigger="seed"`.
Total edges: 4 clusters × 8 edges (7 spoke→hub + 1 hub→bridge) = 32 edges.

---

**Phase 0 — Infrastructure fixes (small, clearly-flagged code changes; committed separately before any fixture work):**

**0.1: Extend `register_fixture()` to insert edges**

In `backend/analytics/seed_fixture.py`, after the `memory_units` loop, add:

```python
edges = data.get("edges", [])
for edge in edges:
    edge_id = edge.get("edge_id")
    if not edge_id:
        continue
    try:
        await graph_store.create_edge(
            edge_id=edge_id,
            source_mu_id=edge["source_mu_id"],
            target_mu_id=edge["target_mu_id"],
            weight=float(edge.get("weight", 0.5)),
            direction=edge.get("direction", "forward"),
            creation_run_id=None,
            creation_trigger="seed",
        )
    except Exception as exc:
        # Edge already exists (idempotent re-registration) — skip
        logger.debug(
            "seed_fixture: edge %s already in DB (skipping): %s", edge_id, exc
        )

# Update generation_record to include edge count
generation_record.append_count: tracked separately — add edge_count to manifest
```

Also update `SeedFixtureManifest` to add `edge_count: int = 0` field, and update the manifest construction to count edges from the fixture.

**0.2: Add `probe_set_version` parameter to `POST /admin/run-probe`**

In `backend/api/admin.py`, extend `RunProbeRequest`:

```python
class RunProbeRequest(BaseModel):
    probe_id: str
    measurement_session_index: int
    condition_label: str
    probe_set_version: str = "v1"   # NEW — default "v1" preserves backward compat
```

Replace the hardcoded `load_probe_set(version="v1")` call with `load_probe_set(version=body.probe_set_version)`.

**0.3: Extend startup probe sync in `backend/main.py`**

Replace the single `load_probe_set(version="v1")` call with a loop over known versions:

```python
for version in ["v1", "v2"]:
    try:
        probes = load_probe_set(version=version)
        await graph_store.sync_probes([p.model_dump() for p in probes])
        logger.info("startup: synced %d probe definitions (version=%s)", len(probes), version)
    except FileNotFoundError:
        logger.info("startup: probe_set_%s.yaml not found — skipping", version)
    except Exception as exc:
        logger.warning("startup: probe sync version=%s failed (non-fatal) — %s", version, exc)
```

This is non-fatal and degrades gracefully when `probe_set_v2.yaml` doesn't yet exist.

**Phase 0.4: Phenomenon-aware pass criteria in `experiment_report.py` (Gap A fix)**

The pass/fail classification currently uses a single global `reporting.pass_rank_threshold`. The fix adds per-phenomenon overrides while keeping the raw `gold_mu_rank` field untouched in every `probe_results` row.

**`config.yaml` — add under `reporting:`:**
```yaml
reporting:
  pass_rank_threshold: 1         # global default
  pass_rank_thresholds:          # per-phenomenon overrides; missing keys fall back to global
    ranking_improvement: 1
    cross_cluster_transfer: 4    # K1(3) + K2(1); bridge structurally occupies K2 slot
    process_rule_adherence: 1
```

**`backend/analytics/reports/experiment_report.py` — change `_classify_probe_outcome`:**

```python
def _classify_probe_outcome(
    result: dict,
    default_threshold: int,
    phenomenon_type: str = "",
    phenomenon_thresholds: Optional[dict] = None,
) -> str:
    """Classify a single probe result as 'pass', 'fail', or 'negative_flagged'.

    threshold is looked up per phenomenon_type first, then falls back to default_threshold.
    gold_mu_rank in the result is always the raw rank — never modified here.
    """
    if result.get("outcome_override") == "negative_flagged":
        return "negative_flagged"
    rank = result.get("gold_mu_rank")
    threshold = (phenomenon_thresholds or {}).get(phenomenon_type, default_threshold)
    if rank is not None and rank <= threshold:
        return "pass"
    return "fail"
```

Update all four call sites in `experiment_report.py` to pass `phenomenon_type` from the enclosing probe definition and `phenomenon_thresholds` from the loaded config. In `_section_61_learning_measures()`:

```python
reporting_cfg = config.get("reporting", {})
pass_rank_threshold = reporting_cfg.get("pass_rank_threshold", 1)
phenomenon_thresholds = reporting_cfg.get("pass_rank_thresholds", {})
```

Thread both through to each `_classify_probe_outcome` call.

**`backend/analytics/reports/control_shadow.py`** — update `_compute_final_probe_pass_rate()` to read `phenomenon_thresholds` from the `measures` dict and pass it to `_classify_probe_outcome`. Store `phenomenon_thresholds` in the `learning_measures` return dict so control_shadow can read it without reloading config.

**`backend/analytics/reports/markdown_renderer.py`** — update the §6.1 header line from `"**Pass-rank threshold:** top-{N}"` to show per-phenomenon thresholds:
```
**Pass-rank thresholds:** ranking_improvement=1, cross_cluster_transfer=4, process_rule_adherence=1
```

**Tests for Phase 0:**
- Extend `test_seed_fixture.py`: `register_fixture()` with a fixture containing edges inserts those edges into the graph store; a second call (idempotent) does not duplicate edges.
- `test_admin.py` (new or extend): `POST /admin/run-probe` with `probe_set_version="v2"` loads v2 probes and returns 404 for a probe_id that only exists in v1; `probe_set_version="v1"` (default) still works.
- Startup sync test: mock both yaml files; verify both are loaded; verify missing v2 file logs INFO and doesn't crash.
- Extend `test_learning_measures.py`: `_classify_probe_outcome` with `phenomenon_type="cross_cluster_transfer"` and `rank=4` → PASS; same with `rank=5` → FAIL. With `phenomenon_type="ranking_improvement"` and `rank=2` → FAIL even with cross-cluster threshold present. With no `phenomenon_thresholds` arg → falls back to `default_threshold`. Verify `gold_mu_rank` in probe_results row is untouched (raw rank, not capped at threshold).

---

**Phase 1 — Content generation (Gemma 4 via script; output is a DRAFT for Paul's review):**

**New file: `scripts/generate_seed_v2.py`**

This script generates the MU content via Gemma 4 (through Ollama at `localhost:11434`). It does NOT commit anything — it writes a draft JSON to `seed_fixtures/seed_v2_draft.json` for review.

```python
"""
Generate seed_v2 MU content via Gemma 4 (local Ollama).
Output: seed_fixtures/seed_v2_draft.json — review before freezing.

Usage: python scripts/generate_seed_v2.py
Requires: Ollama running at localhost:11434 with gemma4:e4b loaded
"""
```

Script responsibilities:
1. Define 34 MU prompts (4 clusters × 8 MUs + 2 bridge MUs) as constants in the script.
2. For each in-cluster MU: send a generation prompt to Gemma 4 asking for a short (2–3 sentence) self-contained statement about the cluster's concept, using the cluster's vocabulary fingerprint terms. Each cluster has 3–5 "fingerprint" terms that must appear in most MUs; these are fixed constants in the script.
3. For each bridge MU: send a generation prompt explicitly instructing Gemma 4 to write a statement that connects both clusters' concepts WITHOUT using either cluster's fingerprint vocabulary. This instruction is the load-bearing constraint.
4. Assign stable MU IDs (fixed UUIDs, not randomly generated) so that `gold_mu_ids` in probe_set_v2.yaml can be filled in at write time, not after DB insertion.
5. Wire the hub-and-spoke + bridge edges using these fixed IDs.
6. Write the draft to `seed_fixtures/seed_v2_draft.json` (uncompressed, for readability during review). This file is NOT `seed_v2.json.gz` — the compressed fixture is created manually after Paul's review and approval.

**Cluster vocabulary fingerprints (fixed in script constants):**

| cluster_id | concept | required fingerprint terms |
|---|---|---|
| cluster_a | structured file output / format discipline | "format", "structure", "output", "field", "delimiter" |
| cluster_b | schema and data-shape reasoning | "schema", "data", "shape", "type", "validation" |
| cluster_c | rule-following under constraint | "rule", "constraint", "violation", "enforce", "comply" |
| cluster_d | self-sourcing / provenance discipline | "source", "provenance", "cite", "origin", "traceable" |
| bridge_ab | "structure is a contract" | ← must avoid ALL cluster_a and cluster_b fingerprints |
| bridge_cd | "constraint over fluency" | ← must avoid ALL cluster_c and cluster_d fingerprints |

**Stable MU ID scheme (fixed UUIDs in script):**

```
cluster_a:
  a0000001-aaaa-4aaa-8aaa-000000000001  (hub)
  a0000002-aaaa-4aaa-8aaa-000000000002  (spoke 1)
  ...
  a0000008-aaaa-4aaa-8aaa-000000000008  (spoke 7)

cluster_b:
  b0000001-bbbb-4bbb-8bbb-000000000001  (hub)
  ...
  b0000008-bbbb-4bbb-8bbb-000000000008  (spoke 7)

cluster_c:
  c0000001-cccc-4ccc-8ccc-000000000001  (hub)
  ...
  c0000008-cccc-4ccc-8ccc-000000000008  (spoke 7)

cluster_d:
  d0000001-dddd-4ddd-8ddd-000000000001  (hub)
  ...
  d0000008-dddd-4ddd-8ddd-000000000008  (spoke 7)

bridge_ab:
  ab000001-abab-4aba-8aba-000000000001

bridge_cd:
  cd000001-cdcd-4cdc-8cdc-000000000001
```

**Edge IDs (fixed):**

```python
# cluster_a: 7 spoke→hub + 1 hub→bridge_ab
edges_cluster_a = [
  {"edge_id": "ea0000X2", "source": "a000000X", "target": "a0000001"}  # spoke→hub
  ...  # × 7
  {"edge_id": "eab00001", "source": "a0000001", "target": "ab000001"}  # hub→bridge_ab
]
# Same pattern for cluster_b, cluster_c, cluster_d
```

**Draft output format** (`seed_v2_draft.json`):
```json
{
  "timestamp": "ISO-8601",
  "mu_count": 34,
  "edge_count": 32,
  "fixture_version": "v2",
  "notes": "DRAFT — review before freezing to seed_v2.json.gz",
  "memory_units": [ ... ],
  "edges": [ ... ]
}
```

Note: `seed_v2_draft.json` is NOT compressed and NOT `seed_v2.json.gz`. It exists solely for Paul's review.

---

**Phase 2 — Paul review + freeze (human step, no code):**

Paul reads `seed_fixtures/seed_v2_draft.json` and checks:
1. Each in-cluster MU is self-contained and uses the cluster's fingerprint vocabulary.
2. Bridge MUs contain no fingerprint vocabulary from either adjacent cluster.
3. The topic and tone are appropriate for the experiment (sufficiently distinct, not trivially separable or overlapping).
4. MU content is free of factual errors that would confuse the model.

Paul may request specific MU rewrites. The script can be rerun for individual MUs if needed. Once Paul approves, the freeze step is:
```bash
python -c "
import gzip, json
with open('seed_fixtures/seed_v2_draft.json') as f:
    data = json.load(f)
# Remove the draft notes field before freezing
data.pop('notes', None)
with gzip.open('seed_fixtures/seed_v2.json.gz', 'wt', encoding='utf-8') as f:
    json.dump(data, f)
print('Frozen seed_fixtures/seed_v2.json.gz')
"
```

The frozen `seed_v2.json.gz` is committed to the repo. The draft `seed_v2_draft.json` is NOT committed (add to `.gitignore`).

---

**Phase 3 — Lexical-distinctness verification script:**

**New file: `scripts/verify_seed_v2.py`**

This script runs before probe_set_v2.yaml is written. It implements the lexical-distinctness check (Gap A mitigation) and the connectivity check from Deliverable 3.

```python
"""
Verification checks for seed_v2 fixture.
Run BEFORE registering the fixture in the DB.

Checks:
  1. All 34 MUs present with correct cluster_id
  2. All 32 edges present with correct wiring
  3. Connectivity: hub→bridge edge exists for each bridge
  4. Lexical-distinctness: bridge MUs share no non-stopword terms with adjacent clusters
  5. Baseline probe sanity: cross-cluster probes should NOT trivially surface bridge at rank 1

Usage: python scripts/verify_seed_v2.py
"""
```

**Check 1 — MU count and cluster distribution:**
```python
assert mu_count == 34
assert cluster_distribution == {
    "cluster_a": 8, "cluster_b": 8,
    "cluster_c": 8, "cluster_d": 8,
    "bridge_ab": 1, "bridge_cd": 1,
}
```

**Check 2 — Edge count and wiring:**
```python
assert edge_count == 32
# For each cluster, verify:
#   hub has 7 incoming spoke→hub edges
#   hub has 1 outgoing hub→bridge edge
#   no edges connect cluster_a/b to cluster_c/d content
```

**Check 3 — Connectivity (reachability):**
For each cluster, confirm the hub has an outgoing edge to its bridge MU. Walk the edge list in-memory; this check does NOT require a running DB.

**Check 4 — Lexical-distinctness (correctness gate, not a warning):**
```python
STOPWORDS = {...}  # same as retrieval.py _STOPWORDS

def cluster_vocab(mus, cluster_id):
    tokens = set()
    for mu in mus:
        if mu["cluster_id"] == cluster_id:
            tokens |= set(mu["raw_content"].lower().split()) - STOPWORDS
    return tokens

# bridge_ab must share no terms with cluster_a vocab OR cluster_b vocab
bridge_ab_tokens = tokenize(bridge_ab_mu["raw_content"])
cluster_a_tokens = cluster_vocab(mus, "cluster_a")
cluster_b_tokens = cluster_vocab(mus, "cluster_b")

shared_a = bridge_ab_tokens & cluster_a_tokens
shared_b = bridge_ab_tokens & cluster_b_tokens
if shared_a or shared_b:
    print(f"FAIL: bridge_ab shares terms with cluster_a={shared_a} cluster_b={shared_b}")
    sys.exit(1)

# Same for bridge_cd / cluster_c / cluster_d
```

**Check 5 — Baseline probe sanity (simulated retrieval):**
Implement a stripped-down BM25 retrieval simulation (same logic as retrieval.py) against the draft fixture. For each planned cross-cluster probe's `prompt_text`, verify the bridge MU does NOT surface at K1 (rank ≤ 3). Bridge at rank 4 (K2) is expected and correct. Bridge at rank 1–3 would mean BM25 alone surfaces it — a lexical-distinctness failure not caught by Check 4 (BM25 scoring differs from raw term overlap).

**All checks must PASS before proceeding to Phase 4.** If any fail, rerun the generation script for the failing MU and repeat verification.

---

**Phase 4 — Probe set + final verification:**

**New file: `probes/probe_set_v2.yaml`**

```yaml
version: "v2"

# NOTE on pass criterion for cross_cluster_transfer probes:
#   With K1=3 and K2=1, the bridge MU can only appear at rank 4 (K2 position).
#   pass_rank_threshold=1 means these probes measure "bridge at rank 1", which
#   cannot occur unless lexical-distinctness is violated. The intended signal is
#   "bridge appears in the retrieved set at any position" (hop_depth > 0 in
#   traversal_path). At baseline (measurement_session_index=0), bridge IS
#   expected to appear as K2 (rank 4) because hub→bridge edge exists with
#   initial_edge_weight=0.5. The cross-cluster measurement tracks bridge_mu_rank
#   trajectory over sessions, not a binary PASS/FAIL at rank 1.
#   See BUILD-PLAN.md Gap A for resolution options.
#
# NOTE on process_rule_adherence probes:
#   Gold MU retrieval rank is measured. Output compliance is not machine-checked.
#   Manual review is required to verify the model's response actually complied
#   with the rule described in the gold MU. See BUILD-PLAN.md Gap B.

phenomenon_map:
  ranking_improvement:
    description: "Gold MU rank should decrease over sessions within the primary cluster"
  cross_cluster_transfer:
    description: "Bridge MU should surface on adjacent-cluster probes via edge traversal (hop_depth > 0)"
  process_rule_adherence:
    description: "Gold MU is a rule/constraint MU from cluster_c; output compliance is a human review step"

probes:
  # 4× ranking_improvement — one per cluster, gold is an in-cluster MU
  - probe_id: "p06"
    phenomenon_type: "ranking_improvement"
    prompt_text: "..."   # FILL IN: cluster_a vocabulary query
    gold_mu_ids:
      - "a0000001-aaaa-4aaa-8aaa-000000000001"   # cluster_a hub
    is_bridge_probe: false
    bridge_pair_id: null

  - probe_id: "p07"
    phenomenon_type: "ranking_improvement"
    prompt_text: "..."   # FILL IN: cluster_b vocabulary query
    gold_mu_ids:
      - "b0000001-bbbb-4bbb-8bbb-000000000001"   # cluster_b hub
    is_bridge_probe: false
    bridge_pair_id: null

  - probe_id: "p08"
    phenomenon_type: "ranking_improvement"
    prompt_text: "..."   # FILL IN: cluster_c vocabulary query
    gold_mu_ids:
      - "c0000001-cccc-4ccc-8ccc-000000000001"   # cluster_c hub
    is_bridge_probe: false
    bridge_pair_id: null

  - probe_id: "p09"
    phenomenon_type: "ranking_improvement"
    prompt_text: "..."   # FILL IN: cluster_d vocabulary query
    gold_mu_ids:
      - "d0000001-dddd-4ddd-8ddd-000000000001"   # cluster_d hub
    is_bridge_probe: false
    bridge_pair_id: null

  # 4× cross_cluster_transfer — one per direction per bridge
  - probe_id: "p10"
    phenomenon_type: "cross_cluster_transfer"
    prompt_text: "..."   # FILL IN: cluster_a vocab only; gold is bridge_ab
    gold_mu_ids:
      - "ab000001-abab-4aba-8aba-000000000001"
    is_bridge_probe: true
    bridge_pair_id: "p11"

  - probe_id: "p11"
    phenomenon_type: "cross_cluster_transfer"
    prompt_text: "..."   # FILL IN: cluster_b vocab only; gold is bridge_ab
    gold_mu_ids:
      - "ab000001-abab-4aba-8aba-000000000001"
    is_bridge_probe: true
    bridge_pair_id: "p10"

  - probe_id: "p12"
    phenomenon_type: "cross_cluster_transfer"
    prompt_text: "..."   # FILL IN: cluster_c vocab only; gold is bridge_cd
    gold_mu_ids:
      - "cd000001-cdcd-4cdc-8cdc-000000000001"
    is_bridge_probe: true
    bridge_pair_id: "p13"

  - probe_id: "p13"
    phenomenon_type: "cross_cluster_transfer"
    prompt_text: "..."   # FILL IN: cluster_d vocab only; gold is bridge_cd
    gold_mu_ids:
      - "cd000001-cdcd-4cdc-8cdc-000000000001"
    is_bridge_probe: true
    bridge_pair_id: "p12"

  # 2× process_rule_adherence — gold MU is a rule/constraint MU from cluster_c
  - probe_id: "p14"
    phenomenon_type: "process_rule_adherence"
    prompt_text: "..."   # FILL IN: query that should surface a cluster_c rule MU
    gold_mu_ids:
      - "c0000003-cccc-4ccc-8ccc-000000000003"   # a spoke in cluster_c (rule-heavy content)
    is_bridge_probe: false
    bridge_pair_id: null

  - probe_id: "p15"
    phenomenon_type: "process_rule_adherence"
    prompt_text: "..."   # FILL IN: second cluster_c rule query
    gold_mu_ids:
      - "c0000005-cccc-4ccc-8ccc-000000000005"   # another cluster_c spoke
    is_bridge_probe: false
    bridge_pair_id: null
```

Note: `prompt_text` fields are filled in after the fixture is generated and verified, using vocabulary from the actual MU content (not from the ID scheme). The probe IDs are p06–p15 to avoid collisions with v1 probes (p01–p05).

**Final verification (all 7 checks — run against a freshly wiped + re-seeded graph):**

Before calling this milestone done, document each check as PASS with actual numbers:

| # | Check | Expected | Pass? |
|---|---|---|---|
| 1 | `POST /admin/seed-graph {"version":"v2"}` | `mu_count: 34` | ? |
| 2 | `GET /admin/graph/stats` | cluster_distribution matches table above; `mu_without_cluster_id: 0` | ? |
| 3 | Connectivity: for each cluster, hub has outgoing edge to its bridge | 4 edges confirmed | ? |
| 4 | Lexical-distinctness: bridge MUs share no non-stopword terms with adjacent clusters | 0 shared terms | ? |
| 5a | Baseline probe run (session_index=0, clean v2 graph): ranking_improvement probes | PASS (rank 1) for p06–p09 | ? |
| 5b | Baseline probe run: cross_cluster_transfer probes p10–p13 | bridge at rank 4 (K2), NOT rank 1 | ? |
| 6 | Idempotency: second `POST /admin/seed-graph {"version":"v2"}` | no duplicate MUs | ? |
| 7 | probe_set_v2.yaml loads at startup with no errors; all 10 probes visible in Admin UI Probe Results | 10 probes synced | ? |

These verification results are documented in PROGRESS.md with actual numbers, following the M17 phase log format.

---

**File changes summary for Milestone 18:**

*New files:*
- `seed_fixtures/seed_v2.json.gz` — frozen fixture (34 MUs, 32 edges)
- `seed_fixtures/seed_v2_draft.json` — NOT committed (add to .gitignore)
- `probes/probe_set_v2.yaml` — 10 probes (p06–p15)
- `scripts/generate_seed_v2.py` — generation script (Gemma 4 via Ollama)
- `scripts/verify_seed_v2.py` — connectivity + lexical-distinctness checks

*Modified files:*
- `backend/analytics/seed_fixture.py` — Phase 0.1: insert edges on registration; add `edge_count` to manifest
- `backend/api/admin.py` — Phase 0.2: `probe_set_version` param on `RunProbeRequest`
- `backend/main.py` — Phase 0.3: multi-version probe sync at startup
- `config.yaml` — add `seed.fixture_version: "v2"` and any new v2-specific constants under a `seed_v2:` block (MU counts, cluster names)
- `.gitignore` — add `seed_fixtures/*_draft.json` and `scripts/__pycache__/`

*Tests:*
- Extend `tests/test_analytics/test_seed_fixture.py` — edge insertion + idempotency
- New `tests/test_api/test_run_probe_versioned.py` — probe_set_version param
- New `tests/test_startup/test_probe_sync.py` — multi-version startup sync

**Existing tests:** All 315 unit + 5 integration tests must still pass. No regressions.

**Done when:**
1. `seed_v2.json.gz` committed, reviewed and approved by Paul, 34 MUs + 32 edges, all verification checks in the table above documented as PASS with actual numbers
2. `probe_set_v2.yaml` committed, 10 probes (p06–p15), loads cleanly at startup
3. Gap A decision documented in PROGRESS.md (pass criterion for cross_cluster_transfer probes confirmed or adjusted)
4. Gap B noted in probe YAML comments — no code change needed
5. All 7 verification checks documented as PASS with numbers in PROGRESS.md
6. Full existing test suite (315 unit + 5 integration) still passing
7. PROGRESS.md updated with new session-log entry following M17 phase log format

**Out of scope:**
- No changes to experiment_protocol.md or pilot structure
- No changes to learning algorithm, decay, or saturation logic
- No new probe_type enum values

**Risks:**
- ⚠ **Gap A (cross_cluster_transfer pass criterion):** Resolution is required before probe_set_v2.yaml is frozen. If the decision is to add per-phenomenon pass thresholds (threshold=4 for cross_cluster_transfer), that is a code change in `backend/analytics/reports/experiment_report.py` (the state-transition classifier). Scope it as part of this milestone if needed; do not leave it unresolved.
- ⚠ **Gemma 4 content quality:** The script prompts Gemma 4 to generate MU content. The output may be too verbose (needs trimming to 2–3 sentences), too similar across MUs (needs fingerprint enforcement), or may accidentally include bridge vocabulary. Budget one review-and-rerun cycle in the estimate.
- ⚠ **Lexical-distinctness check may be strict:** Raw term overlap (tokenized lowercased words) versus BM25 scoring are different. A term that appears once in a bridge MU might have a BM25 score near zero for the cluster query. Run Check 5 (simulated retrieval) to confirm BM25 behavior, not just term overlap.
- ? **probe_set_v2 probe_ids p06–p15:** These are probe IDs, not a continuation of the v1 probe sequence. The DB `probes` table uses `probe_id` as a primary key — p06–p15 will coexist with p01–p05 without conflict.

**Estimate:** 3-4 sessions (content generation + review + verification)
  - Phase 0 (infrastructure): 0.5 session
  - Phase 1 (generation script + run): 1 session
  - Phase 2 (Paul review): async / no code time
  - Phase 3 (verification script + checks): 0.5 session
  - Phase 4 (probe set + final verification): 1 session

---

# Local Corvus — Milestone 19, Phase 0
## Experiment Harness: Run Plan Schema + Skeleton + Dry-Run Mode

**Branch:** `milestone-19-phase-0-harness-skeleton`

---

### 1. Context

M18 is complete: `seed_fixtures/seed_v2.json.gz` and `probes/probe_set_v2.yaml` are
frozen, verified, and committed. The platform is now ready for the formal 100+
prompt, three-condition pilot (learned / static-weight / random-weight).

M19 builds the **experiment harness** that drives that pilot — and future pilots
— end to end: wipe/seed, run turns across conditions, judge responses, and
generate reports. M19 is split into phases so each layer can be smoketested
before the next depends on it. **This planner covers Phase 0 only.**

#### Design decisions already made (binding context, not open questions)

- **Judge model:** starts as Gemma (the same local model being studied — a
  deliberate "contained self-improvement" framing), but must be config-swappable
  (`judge.model`) since this will likely change. Pin model identity, temperature,
  and snapshot per run; log all three. *Judge integration itself is Phase 2 —
  Phase 0 only needs the config field and pinning mechanism to exist in the
  schema.*
- **Judge scope:** the judge runs (and its verdict is logged) for **all three
  conditions**, but only the learned condition uses the verdict to update edge
  weights. Static/random conditions log verdicts for comparative analysis only.
- **Feedback verdicts:** up / down / none. Down is reserved for a named,
  closed-vocabulary criterion violation (confabulation or stated-rule
  violation) — never a vague "bad response." None is the default whenever the
  judge can't cleanly determine a verdict; ambiguity should never be forced
  into up or down.
- **Manual judging:** the harness must support an occasional human-override
  judgment during a real run, but this is a sometimes-used override, not the
  standing mechanism (human-in-the-loop-as-default was explicitly rejected for
  consistency reasons). Every turn's judgment must be tagged with
  `judge_source` (`model:<model_name>` or `human`) so manually-judged turns
  are never silently analyzed as if model-judged. *Phase 0 only needs the
  schema field for this — no judging logic yet.*
- **Difficulty mechanism (Phase 0 in-scope):** hop-distance only — number of
  graph hops between the BM25 entry point and the gold MU. This maps onto the
  existing spoke→hub→bridge topology already in seed_v2.
- **Difficulty mechanism explicitly NOT built:** a "lexically-independent
  transfer" mechanism was considered and rejected as something to seed
  deliberately. Decision: no new fixture content, no new edges, no change to
  seed_v2 or the existing bridge MUs. Any cross-series lexical connections
  that arise will do so organically from unconstrained model response content
  (responses are never lexically restricted — only seed MUs and certain
  deliberately-constructed prompts are). This is being left as a future
  **observational/instrumentation** target (detecting when an MU from one
  series gets retrieved into a different series' context), not a Phase 0
  build item. Do not add anything for this in Phase 0.
- **Probe ratio/placement:** two independent config axes — `probe_ratio`
  (fraction of total turns that are measurement probes) and `probe_placement`
  (`random` / `end` / `fixed_checkpoints` with explicit checkpoint
  positions). Regardless of placement strategy, each turn's type (`probe` vs
  `bulk`) must be resolved deterministically from the run plan *before*
  execution begins (given a fixed random seed), and probe turns must never be
  eligible for feedback/weight updates.
- **Notification:** stub only. Channel-agnostic interface, default
  implementation logs to console/file and makes no network calls. Structured
  payload: `{run_id, status, error_summary, report_path}`.
- **Run plan philosophy:** the harness executes plans; it does not design
  them. If a plan file is supplied, load and validate it. If not, walk the
  user through an interactive elicitation flow and offer to save the result
  as a reusable plan file.
- **Tuning knobs (e.g. `initial_edge_weight`, `decay_rate`,
  `saturation_threshold`):** manually specified via the plan for now. Full
  automated tuning ("run an experiment, evaluate, adjust, rerun until
  plateau") is an explicit future milestone, not part of M19.

#### Explicitly out of scope for Phase 0 (later M19 phases)

- Sonnet-based prompt generation/validation for the ~100+ bulk interaction
  prompts — **Phase 1**.
- Gemma judge integration (criteria storage, judge calls, verdict + rationale
  logging) — **Phase 2**.
- Full pilot execution against real bulk-turn content, final report
  generation wiring, and real notification channel wiring — **Phase 3**.
- Any new seed fixture content — not needed (see decision above).

---

### 2. Goal of Phase 0

Build the skeleton that every later phase will sit inside, and prove it works
end-to-end using only infrastructure that already exists today
(`/admin/seed-graph`, existing probe/report endpoints). Concretely:

1. Define and validate the full **Experiment Run Plan schema** (all fields,
   even ones not yet acted on by Phase 0).
2. Build the harness CLI: load-a-plan or elicit-a-plan, resolve turn
   types, wipe+seed per condition, run a turn loop, checkpoint per turn,
   apply an explicit error-handling policy, call the notification stub.
3. Implement **dry-run mode**: a tiny, cheap, fast end-to-end pass that
   exercises the whole skeleton without needing Phase 1/2 components.

---

### 3. Deliverables

#### 3.1 Experiment Run Plan schema

A validated schema (pydantic model recommended, consistent with existing
codebase patterns) covering:

- `plan_id`, `plan_name`
- `conceptual_fields`: `{ count: int, subjects: list[str] | "auto" }`
  *(Phase 0 only stores/validates this — "auto" generation is Phase 1.)*
- `total_prompts: int`, `series_count: int`, `turns_per_series: int`
  (derive one from the others if only two are given; validate consistency)
- `probe_ratio: float` (0–1), `probe_placement: "random" | "end" |
  "fixed_checkpoints"`, `probe_checkpoint_positions: list[int] | None`
- `difficulty: { hop_distance_levels: list[int] }`
- `judge: { model: str, temperature: float, pinned_snapshot: str | None }`
- `conditions: list["learned" | "static_weight" | "random_weight"]`,
  `condition_order: list[str]` (must be a permutation of `conditions`)
- `tuning_knobs: dict[str, float]` (manual overrides; absent = config
  defaults)
- `notification: { enabled: bool }`
- `budget_cap: float | None`
- `dry_run: bool`, `dry_run_n: int` (default small, e.g. 2)
- `random_seed: int` (for deterministic turn-type resolution)

Validation must fail fast with a specific, field-identifying error message —
not a raw stack trace — for any malformed or inconsistent plan.

#### 3.2 Harness CLI skeleton

New file, e.g. `scripts/run_experiment_harness.py`, following the same
execution pattern as `scripts/generate_seed_v2.py` /
`scripts/verify_seed_v2.py` (host-side Python script calling the Dockerized
backend's API — not running inside the container).

Required behavior:

- `--plan <path>` optional argument.
  - **If provided:** load, validate against the schema above. On failure,
    print the specific error and exit non-zero without touching the graph.
  - **If absent:** run an interactive elicitation flow (stdlib `input()` is
    fine) that walks through every schema field with sensible defaults
    offered. At the end, offer to save the collected plan to a file.
- Persist the **resolved plan + a computed config hash** as a run manifest
  (reuse the existing config-hash pattern already used elsewhere in the
  codebase, or implement an equivalent local hash if that's not directly
  reusable from a standalone script).
- **Per-condition wipe + seed:** call the existing `POST /admin/seed-graph`
  flow before each condition begins (not once per experiment — once per
  condition). Confirm via `/admin/graph/stats` that the wipe+seed succeeded
  before proceeding.
- **Turn-type resolution:** before any turns run, deterministically resolve
  each turn's type (`probe` / `bulk`) from `probe_ratio`, `probe_placement`,
  and `random_seed`. Persist this resolved sequence as part of the run
  manifest so it's auditable after the fact.
- **Turn loop skeleton:** iterate series → turns per the resolved plan. Phase
  0 does not need real prompt content or real judging yet — see dry-run mode
  below for what Phase 0 actually exercises here.
- **Per-turn checkpointing:** after each turn, write progress (condition,
  series index, turn index, turn type, status) to a local checkpoint
  file/table. On startup, if a checkpoint from an incomplete run is found,
  surface it and ask whether to resume or start fresh (resume logic can be
  minimal — the requirement is that progress is never silently lost, not
  full crash-resume automation).
- **Error-handling policy:** implement a config-driven mapping of error type
  → `hard_stop` or `soft_continue`. Default policy: connectivity/DB failures
  → hard stop (abort cleanly, preserve manifest/checkpoint state, surface a
  clear message); any single ambiguous/missing per-turn result → soft
  continue (log and proceed). This mapping must be overridable from the run
  plan, not hardcoded.
- **Notification stub:** a `notify(event: str, payload: dict)` function.
  Default implementation logs the payload (console + file); must not require
  any credential or make any network call. Payload shape:
  `{run_id, status, error_summary, report_path}`.
- **Dry-run mode:** triggered by `--dry-run` or `dry_run: true` in the plan.
  Limits execution to `dry_run_n` turns (default 2), 1 series, learned
  condition only by default. Since Phase 1/2 don't exist yet, dry-run should
  use 1–2 of the existing `probe_set_v2.yaml` prompts as placeholder content
  purely to validate that wipe → seed → turn loop → checkpoint → notify
  completes without error against the real backend.

#### 3.3 Tests

- Schema validation: valid plan loads; several distinct invalid plans each
  produce a specific, identifiable error (not a generic exception).
- Interactive elicitation flow: scripted/mocked input, confirm the saved plan
  file matches what was entered.
- Turn-type resolution: same `random_seed` + plan → same resolved sequence
  (determinism); changing `probe_ratio` changes the proportion as expected.
- Dry-run end-to-end: against the real (or test-mode) backend, confirms the
  full wipe→seed→loop→checkpoint→notify sequence completes for `dry_run_n`
  turns.
- Error-handling policy: simulate a hard-stop condition (e.g. point at an
  unreachable backend) → clean abort, manifest/checkpoint preserved. Simulate
  a soft-continue condition → logged, run proceeds, final checkpoint reflects
  the anomaly.
- Checkpoint persistence: interrupt mid-run, confirm the checkpoint file
  accurately reflects the last completed turn.

#### 3.4 Verification / inspection protocol (mandatory — report actual output, not just "PASS")

1. Run harness with no plan → interactive flow completes → plan saved →
   confirm saved file contents match entries.
2. Run harness with a valid plan file → loads cleanly → config hash logged.
3. Run harness with a deliberately malformed plan file → fails fast with a
   specific, field-identifying error message.
4. Dry-run (`dry_run_n=2`, 1 series, learned condition) against the real
   backend → graph wiped+seeded, 2 turns processed, checkpoint shows 2/2
   complete, `notify()` stub logs final status with the correct payload
   shape.
5. Simulated hard-stop (unreachable backend) → harness aborts cleanly,
   checkpoint/manifest preserved, clear error surfaced.
6. Simulated soft-continue condition → harness logs and continues; final
   checkpoint reflects the logged anomaly.
7. Full existing test suite (332+ tests) still passing — no regressions.

---

### 4. Done criteria

- All deliverables in §3 created and committed.
- All 7 checks in §3.4 PASS with actual recorded output (numbers/messages,
  not just a checkmark).
- No regressions in the existing test suite.
- `PROGRESS.md` updated with the same level of detail as prior milestone
  entries (files created/modified, decisions made, verification results).

---

### 5. What comes after Phase 0 (for context only — not part of this build)

- **Phase 1:** Sonnet-based generation/validation of the ~100+ bulk
  interaction prompts from cluster MU content; conceptual-fields "auto" mode
  becomes real.
- **Phase 2:** Judge integration — criteria storage per prompt, real Gemma
  judge calls, verdict + rationale logging, `judge_source` tagging, manual
  override path.
- **Phase 3:** Full pilot execution mode wired to real report generation and
  a real notification channel (if one is ever wanted beyond the stub).

---

# Local Corvus — Milestone 19, Phases 1–3
## Prompt Bank → Judge → Full Pilot

Planned by /planner on 2026-07-04, from the M19 Phases 1–3 overview.
Phase 0 (harness skeleton) is complete; these three phases fill in the
skeleton's placeholders with real content, real judging, and real reporting.

### How the three phases fit together

```
                    M19 PHASE MAP (1 → 2 → 3, hard dependency chain)
═══════════════════════════════════════════════════════════════════════════════

 PHASE 1: what goes IN          PHASE 2: what comes BACK       PHASE 3: what it MEANS
 (prompt bank)                  (judge verdicts)               (reports + pilot)

 seed_v2.json.gz                Gemma response per bulk turn   series complete
      │                              │                              │
      ▼                              ▼                              ▼
 Sonnet generates            Gemma judge evaluates          POST /admin/reports/
 prompts + criteria          against Phase 1 criteria        generate-series
 per cluster                        │                              │
      │                             ▼                        all conditions done
      ▼                      verdict logged to                     │
 validation pass             feedback_events                       ▼
 (coherence + lexical)       (ALL conditions)                POST /admin/reports/
      │                             │                        generate-experiment
      ▼                             ▼                        (+ control shadow)
 prompt_banks/               learned condition ONLY:               │
 prompt_bank_{hash}.json     weight update fires                   ▼
      │                             │                         notify() with
      └──── harness draws ──────────┴──── budget cap ──────── report_path
            bulk turns                    watches all
```

**Why this order is a hard chain:** the judge (Phase 2) evaluates responses
*against the criteria attached to each prompt* — no prompt bank, nothing to
judge against. The reports (Phase 3) are only meaningful once verdicts and
weight updates exist — a series report over placeholder turns with no feedback
events measures nothing. Do not parallelize or reorder.

**Where each phase's code lives.** All three phases extend the same harness
package built in Phase 0. Nothing moves; new modules slot in beside existing
ones:

```
scripts/harness/
├── schema.py          ← extended each phase (RunPlan is the config contract)
├── runner.py          ← extended each phase (the orchestration loop)
├── client.py          ← extended Phase 2 + 3 (new backend endpoints)
├── prompt_gen.py      ← NEW Phase 1 (Sonnet generation + validation)
├── prompt_bank.py     ← NEW Phase 1 (bank schema, load/save, content hash)
├── judge.py           ← NEW Phase 2 (Ollama judge call + verdict parsing)
├── cost.py            ← NEW Phase 3 (cost estimate + budget-cap tracking;
│                         Phase 1 ships a simple estimate function it absorbs)
├── notify.py          ← extended Phase 3 (channel dispatch)
├── checkpoint.py      ← minor extension Phase 3 (report paths)
├── manifest.py        ← minor extension Phase 1 (prompt_id in sequence)
├── turn_resolution.py ← extended Phase 1 (prompt_id assignment)
└── elicit.py          ← extended as schema grows (new fields prompted)
```

Backend changes are deliberately small: Phase 2 adds a `feedback_events`
migration + one endpoint; Phase 3 adds nothing server-side (report endpoints
already exist from M13–M16).

---

### Milestone 19 Phase 1 — Prompt Bank Generation + Validation

**Branch:** `milestone-19-phase-1-prompt-bank`

**Why now:** The harness runs end-to-end but every bulk turn says "[Phase 0
placeholder]". Nothing downstream (judging, learning, reports) means anything
until the agent is answering real, cluster-grounded questions with
machine-checkable criteria attached. This is also the only phase with real
external API cost, so the cost-estimate guard is built here, where it's first
needed.

**The key boundary to understand:** Sonnet is a *fixture builder*, not a
runtime dependency. It runs once per (seed fixture version + generation
params), host-side, from your terminal — never from inside Docker, never
during an experiment turn. The architecture rule "no external API calls"
applies to the agent under study (Gemma); the prompt bank is built *before*
the experiment starts, the same way `generate_seed_v2.py` built the seed
fixture. Once the bank file exists, runs are fully offline again.

```
GENERATION FLOW (one-time, host-side, before any experiment)
─────────────────────────────────────────────────────────────

seed_fixtures/seed_v2.json.gz          scripts/generate_seed_v2.py
(34 MUs, 4 clusters, cluster_id        (CLUSTER_A_FP..D_FP fingerprint
 on every MU)                           vocabulary — reused, not rebuilt)
        │                                        │
        ▼                                        │
  prompt_gen.py reads cluster MU content         │
        │                                        │
        ▼                                        │
  COST ESTIMATE printed → user confirms [y/N] ───┼─── abort = no API calls,
        │                                        │    no charges, no files
        ▼                                        │
  per cluster: 1 Sonnet call                     │
  → N prompts, each with 1–3 criteria            │
    + hop_distance per plan config               │
        │                                        │
        ▼                                        ▼
  validation:                          lexical-distinctness check is
  ├─ Sonnet call per prompt:           PURE PYTHON — prompt for cluster A
  │  "coherent + answerable            must contain zero fingerprint terms
  │   from this cluster content?"      from clusters B/C/D. Deterministic,
  └─ fingerprint scan (local) ─────────free, testable without mocking.
        │
        ├─ fail → regenerate (up to retry limit) → still failing:
        │         logged + excluded, never silently dropped
        ▼
  prompt_banks/prompt_bank_{hash}.json     ← immutable, reusable artifact
  hash = sha256(fixture manifest hash + generation params)
```

**Why the lexical check matters (the "why" behind the fingerprint scan):**
retrieval attribution in this experiment works by BM25 keyword entry points.
If a prompt aimed at cluster A accidentally contains cluster C's vocabulary
("rule", "constraint", …), BM25 will surface cluster C MUs and the
hop-distance measurement is corrupted — you can no longer tell whether the
graph *learned* a connection or the prompt *leaked* one. The fingerprint
vocabularies in `generate_seed_v2.py` (`CLUSTER_A_FP` … `CLUSTER_D_FP`)
already define exactly what "cluster C vocabulary" means, so the check is a
set-intersection in Python, not an LLM judgment call.

**Build:**

1. **`scripts/harness/prompt_bank.py`** — the artifact layer, no API calls:
   - `PromptBankEntry` pydantic model: `prompt_id`, `cluster_id`,
     `prompt_text`, `hop_distance`, `criteria: list[str]` (1–3, validated),
     `seed_fixture_version`, `generated_by` (model name + date),
     `validated: bool`.
   - `PromptBank` model: entries + generation params + `content_hash`.
   - `compute_bank_hash(fixture_manifest_hash, params) -> str`,
     `save_bank()`, `load_bank(path)` with specific, field-identifying
     validation errors (same convention as `schema.py`).
   - Excluded prompts are retained in the file with `validated: false` and an
     `exclusion_reason` — auditable, never silently dropped; the runner only
     ever draws `validated: true` entries.

2. **`scripts/harness/prompt_gen.py`** — the generator (only file that talks
   to the Anthropic API):
   - Reads cluster MU content directly from `seed_fixtures/seed_v2.json.gz`
     (host-side file read, mirrors `verify_seed_v2.py` convention — no
     backend dependency, works with Docker down).
   - Imports the fingerprint constants from `generate_seed_v2.py` (they are
     module-level data behind a main guard). If import proves awkward,
     extract them to a small shared `scripts/cluster_fingerprints.py` — but
     try the import first; one source of truth beats a copy.
   - Model: `claude-sonnet-5` via the `anthropic` Python SDK (new dev
     dependency, host-side only — do NOT add to `backend/requirements.txt`
     or the Docker image). API key from `ANTHROPIC_API_KEY` env var;
     fail fast with a clear message if absent, before any other work.
   - Per-cluster generation call: cluster MU content + optional manual
     subject constraint + count + hop-distance targets → JSON array of
     {prompt_text, criteria, hop_distance}. Validate the JSON against
     `PromptBankEntry` and retry the call once on malformed output.
   - Hop-distance targeting: hop-0 prompts are steered toward the cluster's
     hub MU vocabulary; hop-1 prompts toward a named spoke MU's vocabulary.
     The generation prompt includes the specific MU text for the target.
   - `N = total_prompts × (1 − probe_ratio)`, distributed round-robin across
     the 4 clusters (remainder goes to the earliest clusters — deterministic).
   - Validation pass: one Sonnet call per prompt (coherent? answerable from
     this cluster's content?) + the local fingerprint scan. Failures
     regenerate up to `prompt_gen.max_retries`; persistent failures are
     excluded with reason.
   - **Cost estimate + confirmation:** before the first API call, estimate
     tokens (cluster content + prompts × validation) and print an estimated
     dollar cost at Sonnet 5 rates ($3/MTok in, $15/MTok out — $2/$10 intro
     through 2026-08-31; put the rates in config.yaml, not hardcoded, so a
     price change is a config edit). Require explicit `y` to proceed.
     Expected magnitude for a ~100-prompt bank: well under $5 — the guard is
     against config typos (e.g. `total_prompts: 10000`), not routine cost.

3. **`scripts/harness/schema.py`** — extend `RunPlan` (never bypass it):
   - `prompt_bank_path: Optional[str]` — path to an existing bank. If absent
     and `conceptual_fields.subjects == "auto"`, the harness generates and
     saves one before executing (with the cost-confirmation gate).
   - New `PromptGenConfig` sub-model: `model: str = "claude-sonnet-5"`,
     `max_retries: int = 2`. Manual `conceptual_fields.subjects` strings
     (when provided) are passed into the generation prompt as topic
     constraints.

4. **`scripts/harness/turn_resolution.py` + `runner.py`** — harness
   integration:
   - At turn-sequence resolution time, each bulk turn is assigned a
     `prompt_id` deterministically from `random_seed` (draw without
     replacement until the bank is exhausted, then reshuffle — same seed,
     same sequence, always). The assignment lands in the manifest's
     `resolved_turn_sequence`, so a resumed run replays identical prompts.
   - `build_turn_message()` replaces `_placeholder_bulk_message()` with a
     lookup of `prompt_id` → `prompt_text` from the loaded bank. Probe turns
     unchanged.
   - Preflight: if the plan names a `prompt_bank_path` that doesn't exist or
     fails validation, hard-stop before any wipe (routes through the
     existing error-policy machinery, "connectivity"-class severity).

5. **`scripts/run_experiment_harness.py` + `elicit.py`** — prompt for the
   new fields in the interactive flow; `--generate-bank` convenience flag to
   build a bank without running an experiment.

**Tests (all Sonnet calls mocked — no test ever hits the API):**
- `test_prompt_bank.py`: entry validation (0 criteria rejected, 4 criteria
  rejected); hash is deterministic and changes when params change;
  save→load round-trip; excluded entries preserved with reasons.
- `test_prompt_gen.py`: fingerprint scan pure function (prompt with cluster C
  term fails for cluster A, passes for cluster C); N-distribution across
  clusters is exact and deterministic; malformed Sonnet JSON triggers one
  retry then exclusion; missing API key fails fast with a named error.
- `test_turn_resolution.py` (extend): same seed → same prompt_id sequence;
  different seed → different sequence; bank smaller than bulk-turn count
  reshuffles rather than crashing.
- `test_runner.py` (extend): bulk turn message equals the bank entry's
  prompt_text; missing bank file hard-stops before `wipe_graph` is called
  (assert the fake client saw zero wipe calls).

**Verification protocol (report actual output, not just PASS):**
1. `--generate-bank` against real seed_v2 with a small plan (e.g. 20 prompts)
   → cost estimate printed → confirm → bank file written; report per-cluster
   counts, validation pass/fail counts, and the content hash.
2. Re-run with identical params → harness detects the existing hash and
   reuses the bank without any API call.
3. Dry-run (`dry_run_n=2`) → both bulk turns show real bank prompts in the
   checkpoint/log, with their `prompt_id`s in the manifest.
4. Full test suite: 387+ passing, no regressions.

**Done when:** all four checks pass with recorded output; criteria present on
every validated entry; the placeholder message string no longer appears in
any dry-run output.

**Risks:**
- ⚠ Sonnet returns prose around the JSON → parse defensively (extract the
  JSON array), one retry, then fail that cluster loudly. Never regex-match
  serialized JSON for content — always `json.loads` then validate.
- ⚠ Criteria quality is the experiment's Achilles' heel — vague criteria
  poison Phase 2 verdicts. The generation prompt must demand binary-checkable
  statements and show 2 worked examples. (The pre-pilot human spot-check in
  Phase 2's calibration note is the real safety net.)
- ? Exact hop-1 steering fidelity (does a spoke-vocabulary prompt actually
  enter at the spoke?) can't be verified until real retrieval runs — note
  observed entry points during the Phase 3 pilot and record in PROGRESS.md.

**Estimate:** 2 sessions

---

### Milestone 19 Phase 2 — Judge Integration

**Branch:** `milestone-19-phase-2-judge`

**Why second:** With real prompts and criteria in place, the missing link is
the feedback signal. This phase closes the loop that the whole platform
exists to study: response → verdict → (learned condition only) weight update.

**Discovery from planning — the overview understates the backend work:** the
existing `feedback_events` table (M8/M9) has only 6 columns
(`feedback_event_id`, `run_id`, `verdict`, `verdict_basis`, `turn_index`,
`is_probe_turn`). Phase 2's required fields — `condition_label`, `rationale`,
`criterion_triggered`, `judge_source`, `judge_model_name`,
`judge_temperature`, `judge_pinned_snapshot`, `prompt_id` — do not exist yet.
So Phase 2 includes a DB migration (the additive-column pattern already
established in `store.py`'s `_migrate_db()`) and one new backend endpoint.
Existing rows keep NULLs in the new columns; only `judge_source` gets
backfilled semantics: any pre-existing row is by definition `"human"` (it
came from the chat UI thumbs), which matches the standing rule — *any verdict
not produced by an automated judge call is "human"*.

```
JUDGE FLOW (per bulk turn, per condition — probe turns NEVER enter this path)
──────────────────────────────────────────────────────────────────────────────

 runner.py                      judge.py                    backend (Docker)
 ─────────                      ────────                    ────────────────
 bulk turn completes
 (turn_type == "bulk" —
  guard #1, upstream)
      │
      ▼
 look up prompt entry ────▶ precondition: raise if
 by prompt_id               turn is probe (guard #2,
      │                     belt-and-suspenders)
      │                          │
      │                          ▼
      │                     Ollama /api/chat ──▶ localhost:11434 (host-side —
      │                     judge.model, temp 0    NOT host.docker.internal;
      │                          │                 that alias is for the
      │                          ▼                 backend container only)
      │                     parse JSON verdict:
      │                     {verdict, rationale,
      │                      criterion_triggered}
      │                          │
      │            unparseable ──┤ parseable
      │            after retry   │
      │                 │        ▼
      │                 ▼   verdict returned
      │            error_policy
      │            "judge_unparseable"
      │            (soft_continue default —
      │             logged, no verdict record)
      ▼
 POST /feedback/verdict  ──────────────────────▶  INSERT feedback_events
 apply_weight_update =                            (full judge metadata)
   (condition == "learned"                             │
    AND verdict in up/down)                            ▼
      │                                          if apply_weight_update:
      │                                            SimpleRL.update()
      ▼                                            weight_updates carry
 checkpoint records                                feedback_event_id FK
 verdict + event_id                                (§6.1 orphan check
                                                    verifies this free)
```

**Why the condition gate lives in the runner, not the judge:** the judge is
condition-agnostic by design — it scores response quality identically for all
three conditions, which is what makes cross-condition quality comparison
valid. If the judge knew about conditions, a bug there could silently bias
the measurement. The runner is the orchestrator; "who acts on the verdict" is
orchestration policy. The backend endpoint takes an explicit
`apply_weight_update` flag rather than inferring, so the decision is visible
in every request log.

**Build:**

1. **Backend — `memory/store.py` migration + `analytics/feedback_events.py`:**
   - `_migrate_db()`: add nullable columns `condition_label`, `rationale`,
     `criterion_triggered`, `judge_source`, `judge_model_name`,
     `judge_temperature`, `judge_pinned_snapshot`, `prompt_id` to
     `feedback_events`. Additive only — existing rows and the 387-test
     baseline untouched.
   - Extend the `FeedbackEvent` pydantic model with the same optional fields.
   - The chat-UI path (`POST /feedback`) starts setting
     `judge_source="human"` on the events it creates — the field is
     mandatory going forward, and UI thumbs are human verdicts by rule.

2. **Backend — `api/chat.py`: new `POST /feedback/verdict` endpoint:**
   - Request: `run_id`, `verdict` (validated against the closed set),
     `rationale`, `criterion_triggered` (required iff verdict is down;
     must be one of the prompt's criteria strings — the harness enforces
     the closed vocabulary, the endpoint validates non-empty),
     `condition_label`, `judge_source`, `judge_model_name`,
     `judge_temperature`, `judge_pinned_snapshot`, `prompt_id`,
     `apply_weight_update: bool`.
   - Always inserts the feedback event (verdict `"none"` is an explicit
     stored string — the prior instrumentation audit's NULL-as-none problem
     must not reappear). Returns `feedback_event_id`.
   - When `apply_weight_update` is true and verdict is up/down: run the same
     load-run → SimpleRL → write-weights → update-run path as `POST
     /feedback`. **Refactor that logic out of the existing `/feedback`
     handler into a shared helper** rather than duplicating it — two copies
     of the weight-update path is how the §6.1 ledger check ends up
     verifying only one of them. `/feedback` itself keeps its exact current
     request/response contract (the chat UI doesn't change).
   - Weight-update records carry the returned `feedback_event_id` as the
     trigger FK — the existing §6.1 orphan-update check then audits the
     wiring for free.
   - ⚠ **Verdict vocabulary decision to resolve at build time:** the table
     today stores `"thumbs_up"/"thumbs_down"/"none"`; the judge speaks
     `"up"/"down"/"none"`. Before choosing, grep the report code
     (`series_report.py`, `experiment_report.py`, `control_shadow.py`) for
     verdict-string matching. If any query matches on `"thumbs_*"`, map
     judge up/down onto the existing strings at the endpoint boundary (one
     vocabulary in the DB, `judge_source` distinguishes origin). If nothing
     matches on the strings, store `"up"/"down"` directly. Record the choice
     and the grep evidence in PROGRESS.md.

3. **`scripts/harness/judge.py`** — the judge module:
   - `judge_turn(prompt_text, criteria, response_text, judge_config) ->
     JudgeVerdict` — raises `ProbeTurnJudgeError` if handed a probe turn
     (precondition guard; the runner also never routes probes here — both
     layers, deliberately).
   - Ollama call to `judge.model` at `judge.temperature` (validate ≤ 0.1 at
     plan-load time — reproducibility requires near-0), `format: "json"`
     requested, structured prompt containing: the original prompt, the
     criteria list verbatim, the response, and explicit instruction that
     `"none"` is the correct output whenever up/down can't be cleanly
     determined — none is the honest answer, not a failure.
   - Response parsing: `verdict` must be in the closed set;
     `criterion_triggered` required for down and must exactly match one of
     the supplied criteria strings (closed vocabulary — a freeform complaint
     is a parse failure). One re-ask on parse failure, then raise
     `JudgeUnparseableError`.
   - Judge availability preflight: `GET /api/tags` on Ollama, confirm
     `judge.model` is present. Runs before any graph mutation; missing model
     is a hard stop.

4. **`scripts/harness/runner.py` + `client.py`** — wiring:
   - `client.py`: `post_verdict(...)` method (new endpoint), plus the
     judge-availability check helper.
   - After each successful bulk-turn chat call: judge → decide
     `apply_weight_update = (condition == "learned" and verdict in
     ("up","down"))` → post verdict → record `feedback_event_id` + verdict in
     the turn checkpoint.
   - `JudgeUnparseableError` routes through `error_policy` under a new key
     `"judge_unparseable"` (default `soft_continue`: prominently logged, no
     verdict record, turn checkpoint notes the anomaly). `error_policy` is
     already `dict[str, str]` — extending the key set needs no schema change,
     only a documented default in `DEFAULT_ERROR_POLICY`.
   - `--interactive` CLI flag: after each verdict, print it and prompt
     `Accept judge verdict, or override? [a/o/skip]`. Override replaces the
     verdict and sets `judge_source="human"` on the logged record. Default
     mode (no flag) is fully automated — this exists for calibration
     spot-checks, not routine use.

5. **`scripts/harness/schema.py`:** no new judge fields (binding constraint —
   `judge.model/temperature/pinned_snapshot` already exist). Only the
   `judge_unparseable` default lands in `DEFAULT_ERROR_POLICY`, and the
   temperature validator tightens per item 3.

**Tests:**
- Backend: migration adds columns and preserves existing rows; `/feedback/
  verdict` inserts all fields (explicit `"none"` string stored, never NULL);
  `apply_weight_update=false` → zero weight changes (assert edge weights
  before/after); `=true` + up/down → weights change and updates carry the
  FK; `=true` + none → no update (silence is not a signal); down without
  `criterion_triggered` → 422; `/feedback` (old endpoint) behavior unchanged
  and now stamps `judge_source="human"`.
- Harness: `judge.py` with a fake Ollama — clean verdict parses; down with a
  criterion not in the list → parse failure → one re-ask → error; probe turn
  → raises; runner + fake client — learned/up posts with
  `apply_weight_update=true`, static/up posts with `false`, learned/none
  posts with `false`; unparseable judge soft-continues and the checkpoint
  records the anomaly; `--interactive` override path sets
  `judge_source="human"` (scripted input).

**Verification protocol:**
1. Dry-run (learned condition, `dry_run_n=2`) against the real stack → 2
   verdict rows in `feedback_events` with `judge_source="model:<name>"`,
   `judge_model_name`, explicit verdict strings; report the actual rows.
2. Extend the dry-run scope to all 3 conditions (temporarily lift the
   dry-run first-condition-only scoping via plan config, or run three plans)
   → static/random rows logged with zero weight updates; confirm via the
   §6.2 static-control sanity check.
3. §5.6 probe-integrity check: zero verdict records on probe turns.
4. §6.1 ledger: orphan-update count = 0 in the learned condition.
5. `--interactive` on one turn → override recorded as `"human"`.
6. Full test suite passing, no regressions.

**Done when:** all six checks pass with recorded output.

**Risks:**
- ⚠ Gemma judging Gemma is an accepted, documented confound — the
  `judge_source` + `judge_model_name` on every row is what keeps it auditable
  after the fact. Don't paper over it; don't re-litigate it.
- ⚠ Gemma may not reliably emit clean JSON even with `format: "json"` — the
  re-ask + `judge_unparseable` soft-continue path is the containment. If the
  unparseable rate in verification exceeds ~10%, stop and tighten the judge
  prompt before Phase 3; a lossy judge starves the learned condition of
  signal.
- ? Verdict-vocabulary mapping (see build item 2) — resolve with grep
  evidence, record in PROGRESS.md.
- 📋 **Pre-pilot calibration (human step, not a harness feature):** before
  any large run, Paul reviews 15–20 judged turns against Gemma's verdicts
  using `--interactive`, to catch ambiguously-written criteria before they
  poison a full run. Schedule it between Phase 2 merge and the Phase 3 pilot.

**Estimate:** 2–3 sessions

---

### Milestone 19 Phase 3 — Full Pilot Execution + Reporting Wire-Up

**Branch:** `milestone-19-phase-3-pilot`

**Why last:** Everything upstream now produces real data; this phase makes
the harness *finish the job* — reports generated at the right moments, budget
enforced, notification dispatched — and then proves it with the first real
experiment data the system has ever produced.

```
RUN LIFECYCLE AFTER PHASE 3 (per plan, all conditions)
──────────────────────────────────────────────────────

 pre-run                per condition              experiment close
 ───────                ─────────────              ────────────────
 cost estimate          wipe+seed                  POST generate-experiment
 vs budget_cap ──over──▶ HARD STOP                   experiment_id =
 (before any               │                         {plan_id}_learned
  graph mutation)          ▼                         control_experiment_ids =
      │                 series loop:                 {static_weight: ...,
      ▼                   turns → judge →             random_weight: ...}
 judge preflight          verdicts                   (assembled from manifest,
 (Phase 2)                 │                          never user input)
      │                    ▼ series complete              │
      ▼                 POST generate-series              ▼
 execute            ┌── report path → checkpoint     report_path → payload
      │             │      + manifest                     │
      │             │   (failure = SOFT continue,        ▼
      │             │    logged prominently —       notify("complete")
      │             │    a broken report is         channel dispatch:
      │             │    re-callable later;         "log" implemented,
      │             │    an aborted run is not)     email/webhook stubs
      ▼             │
 cumulative cost ───┘
 crosses budget_cap
 mid-run → hard stop
 (archive + abort cleanly, notify("aborted"))
```

**Build:**

1. **Series identity — the wiring gap to close first.** The report endpoint
   needs `(experiment_id, series_id)`, and `series_report.py` keys everything
   on `series_id` — but `runner.py` currently calls `client.chat(message)`
   without passing `series_id` at all, so every harness turn lands in
   whatever session the backend assigns. Fix: the runner passes a
   deterministic `series_id` per (condition, series_index) — e.g.
   `{plan_id}_{condition}_s{series_index}` — on every chat call
   (`client.chat` already accepts the parameter). Record the mapping in the
   manifest.
   - ? **Verify during build:** how runs get stamped with `experiment_id`
     (M12 added the column — find the mechanism: chat param, admin endpoint,
     or config). The wipe step already uses `{plan_id}_{condition}` as the
     experiment identity; the runs and probe results must carry the same
     value or the reports will query an empty set. This is the highest-risk
     unknown in the phase — resolve it before writing the reporting calls,
     and record the answer in PROGRESS.md.

2. **Series-close + experiment-close reporting** (`runner.py`, `client.py`,
   `checkpoint.py`):
   - `client.py`: `generate_series_report(experiment_id, series_id)` and
     `generate_experiment_report(experiment_id, control_experiment_ids)` —
     both endpoints exist (M13/M14–16); request shapes verified against
     `backend/api/admin.py`.
   - Runner detects series completion (last turn of a series index within a
     condition), calls generate-series, logs the returned `md_path`/`json_path`
     into the checkpoint and manifest. Report-generation failures are
     **soft-continue regardless of the connectivity default** — a reporting
     failure must not abort a run that otherwise succeeded (the report can be
     re-generated manually; the run cannot). New error-policy key
     `"report_generation"` defaulting to `soft_continue`, prominently logged.
   - At experiment close (all conditions finished), call generate-experiment
     with the learned condition's experiment_id and the controls dict
     assembled from the manifest (`{plan_id}_{condition}` — deterministic,
     never asked of the user). The returned report path becomes
     `report_path` in the notification payload — exactly the field the
     Phase 0 stub already defined.

3. **Notification channel dispatch** (`notify.py`, `schema.py`):
   - `NotificationConfig` gains `channel: str = "log"` validated against
     `("log", "email", "webhook")`.
   - `notify.py` becomes a dispatch table: `{"log": _notify_log}` where
     `_notify_log` is the current behavior verbatim. `email`/`webhook`
     resolve to a handler that logs "channel not implemented, falling back
     to log" and delegates to `_notify_log` — the structure is the
     deliverable; adding a real channel later is one new handler function,
     no refactor. Fires on completion and on hard-stop (both already exist
     in the Phase 0 flow).

4. **Budget cap enforcement** (`scripts/harness/cost.py`, `runner.py`):
   - `cost.py`: absorb Phase 1's estimate function into a `CostTracker` —
     pre-run estimate (Sonnet generation + validation if a bank must be
     built; judge calls × local-Ollama rate, which is 0 but the structure
     exists so a hosted judge later is a config change) and cumulative
     actuals (every Sonnet call reports usage tokens; validation retries
     count).
   - Pre-run: estimate > `budget_cap` → hard stop *before any graph
     mutation*, message names the estimate, the cap, and the plan field to
     change. `budget_cap: None` = uncapped (already the schema default).
   - Mid-run: cumulative crossing the cap → hard stop through the existing
     HardStopError path (checkpoint preserved, notify("aborted") with a
     budget-breach error_summary). Note the wipe already archives, so
     "archive+abort cleanly" is inherited, not new code.

5. **Full pilot smoke run — a required done criterion, not optional:**
   - One real run: all 3 conditions, 1 series each, ≥5 bulk + ≥2 probe turns
     per condition, real Gemma agent, real Gemma judge, real Sonnet bank.
   - Confirm: 3 series reports generated; experiment report with populated
     control shadow; notification log shows `"status": "complete"` with
     non-null `report_path`; §6.2 control sanity checks pass (zero weight
     changes in static, re-randomized weights in random, zero
     feedback→update correlation in both controls).
   - This is the first real experiment data the system has produced —
     record the report paths and headline numbers in PROGRESS.md.

**Tests:**
- Runner + fake client: series completion triggers exactly one
  generate-series call per (condition, series) with the right IDs; report
  failure soft-continues and the checkpoint records it; experiment close
  sends the correct `control_experiment_ids` dict; hard-stop mid-run skips
  experiment-close reporting but still notifies with `"aborted"`.
- `notify.py`: channel dispatch — "log" writes the JSONL line (existing
  behavior); "email" falls back with a logged warning; invalid channel
  rejected at schema validation with a field-identifying error.
- `cost.py`: estimate arithmetic; pre-run over-cap → hard stop with zero
  client calls; mid-run crossing → HardStopError; `budget_cap: None` never
  stops.
- `client.chat` passes series_id; turn loop uses the deterministic mapping.

**Verification protocol:**
1. Multi-series dry-run against the real stack → one series report per
   completed series, paths in manifest and checkpoint.
2. Deliberately break report generation (e.g. bogus series_id) → run
   completes, failure logged prominently, notification still fires.
3. Plan with `budget_cap: 0.01` and no existing bank → hard stop before any
   wipe, clear message.
4. The full pilot smoke run (build item 5) — all confirmations recorded with
   actual numbers.
5. Full test suite passing, no regressions.

**Done when:** all five checks pass with recorded output — the pilot run is
the gate; if it fails, Phase 3 is not complete.

**Risks:**
- ⚠ The experiment_id stamping mechanism (build item 1's open question) is
  the phase's critical unknown — if runs aren't tagged the way the reports
  query, every report comes back empty. Resolve it first, before any other
  Phase 3 code.
- ⚠ The pilot is long-running (3 conditions × 7+ turns × local inference +
  judge calls per bulk turn). Budget wall-clock time; the per-turn
  checkpoint + resume from Phase 0 is the recovery path if it's interrupted.
- ? Cross-series retrieval and cross-series edges will appear in the
  existing §5.3/§5.4 metrics automatically — do NOT add a separate
  "cross-cluster organic transfer" metric (binding constraint from the
  overview).

**Estimate:** 2–3 sessions (pilot run and its verification may take a
session on their own)

---

### Milestone 20 Phase 1 — Learned-Condition Edge Update Mechanism

**Why this milestone exists:** M19 built the three-condition experiment harness (learned / static_weight / random_weight) but the "learned" condition has never actually learned anything from judge verdicts beyond the M4-era uniform SimpleRL delta on the raw scalar `weight` column. This phase replaces that with the real mechanism the research design calls for: a Beta(alpha, beta) posterior per edge, a single derived `effective_weight()` read path, and path-decayed credit assignment from judge verdicts. Decay (counter discounting / ACT-R) is explicitly out of scope — schema-reserved only.

**Design summary:**
- Each edge stores `alpha` (positive evidence) and `beta` (negative evidence), forming a Beta(alpha, beta) posterior. `effective_weight(edge)` derives either the posterior mean (deterministic) or a Thompson sample, dispatched by condition — `static_weight`/`random_weight` are unaffected and keep reading the existing scalar `weight` column; only `learned` reads alpha/beta.
- Feedback verdicts distribute fractional pseudo-count increments backward along the MU's recorded traversal path with geometric decay (`increment = credit_scale * credit_lambda ** hop_k`), added to alpha (up) or beta (down). The current single-hop retrieval funnel means most paths are length 0 (direct BM25 — no update) or length 1 (K1→K2 traversal edge, hop_k=0); the credit-decay math is implemented generically and unit-tested against synthetic multi-hop paths for when multi-hop traversal exists.
- An append-only `edge_update_events` table makes every increment independently reconstructable (feedback event → per-edge increment → weight-as-of-turn), which is the backbone of the required trace-test sign-off gate.
- `decay:` config block is schema-validated (hard-fail if `enabled: true`) but implements nothing this phase.

**Scope boundary:** No decay, no traversal-scoring changes beyond swapping the raw `weight` read for `effective_weight()`, no pruning, no competitive normalization, no judge/verdict-vocabulary changes. Static and random conditions continue to log verdicts but never write counters (existing M19 Phase 0 invariant).

**Planning discovery (resolved before writing code):** the backend has no existing signal for "which of the three harness conditions is this process currently running" — `CONDITION_LABEL`/`experiment.condition_label` is a *different*, pre-existing axis (M9 probe/report vocabulary: `"learning"`/`"static-weight"`/`"random-weight"`) untouched by this phase, and `runner.py`'s per-condition backend restart only ever overrides `EXPERIMENT_ID`. Added a new, additive `RUN_CONDITION` env var (harness vocabulary: `learned`/`static_weight`/`random_weight`), resolved once at orchestrator startup exactly like `EXPERIMENT_ID`, defaulting to `static_weight` when unset — so plain chat-UI/dev usage (no harness) is behaviorally unchanged (still the legacy scalar-weight + SimpleRL path). The harness now also passes `RUN_CONDITION` on its per-condition backend restart, alongside `EXPERIMENT_ID`.

**Build:**
1. `config.yaml` — new `edge_update:` block (§6 of the spec: `prior_alpha`/`prior_beta`, `seed_prior_alpha`/`seed_prior_beta`, `credit_scale`, `credit_lambda`, `negative_scope`, `weight_mode`, `evidence_cap`, `rng_seed`) and a reserved `decay:` block (`enabled: false` enforced).
2. `backend/learning/edge_update.py` (new) — `validate_edge_update_config()` (hard-fails on any invalid value, including `decay.enabled: true`), `compute_effective_weight()` (sole condition-dispatched read path), `compute_path_increments()` (pure credit-decay math), `apply_evidence_cap()`, `get_path_edge_ids_for_mu()`, `resolve_rng_seed()`, and `process_verdict_for_learning()` (the end-to-end verdict → counter-write → event-log orchestration, gated internally on `condition_label == "learned"` regardless of caller).
3. `backend/memory/store.py` — `edges` table gains `alpha`/`beta`/`evidence_updated_at_turn`; new append-only `edge_update_events` table; `create_edge()` accepts `alpha`/`beta`; new `update_edge_counters()`, `insert_edge_update_event()`, `get_edge_update_events_for_edge()`, `get_edge_update_events_for_run()`.
4. `backend/memory/retrieval.py` — `retrieve()` gains a `condition` parameter (default `"static_weight"` — backward compatible for every caller that doesn't pass it); every raw `edge["weight"]` read is replaced by a per-turn-cached `effective_weight()` call (computed once per edge so Thompson-mode draws are stable within one turn); new `RetrievalResult.edge_weights_used` field logs `{alpha, beta, weight_used, mode}` for every edge appearing in `traversal_path`.
5. `backend/core/orchestrator.py` — resolves `RUN_CONDITION` at module load (mirrors `EXPERIMENT_ID`); validates `edge_update`/`decay` config at import time (hard-fail on bad config, before any run state exists); passes `condition=RUN_CONDITION` into `retrieve()`; passes `alpha`/`beta` (from the validated `prior_alpha`/`prior_beta`) into every `create_edge()` call (all orchestrator-created edges are runtime, not seed); threads `edge_weights_used` into `RunRecord`.
6. `backend/analytics/seed_fixture.py` — seed-loaded edges get `seed_prior_alpha`/`seed_prior_beta` instead of the runtime prior.
7. `backend/analytics/run_log.py` — `RunRecord.edge_weights_used` field + `store.py` analytics column/JSON-field plumbing.
8. `backend/api/chat.py` — `POST /feedback/verdict`'s `apply_weight_update` branch now calls `edge_update.process_verdict_for_learning()` instead of the M4-era `SimpleRL` path; response shape (`edges_updated`, `weight_updates`) is unchanged so existing report generators keep working (`old_weight`/`new_weight` are now the posterior mean before/after, with `alpha_before/after`, `beta_before/after`, `hop_k`, `increment` added). The human-facing `POST /feedback` endpoint (chat UI thumbs) is untouched — out of scope, no condition concept there.
9. `backend/api/admin.py` — `POST /admin/run-probe` passes `condition=RUN_CONDITION` to `retrieve()` so probe measurements taken under a `learned`-condition backend reflect Beta-derived weights.
10. `scripts/harness/runner.py` / `docker_lifecycle.py` — per-condition backend restart now also sets `RUN_CONDITION` alongside `EXPERIMENT_ID`.
11. `docker-compose.yml` — `RUN_CONDITION` env passthrough.

**Tests:** unit coverage for every item in the spec's §8 (3-hop credit decay math, `negative_scope: path` vs `terminal`, `none`/zero-hop no-ops, order-independence, evidence cap rescaling preserving mean, mean-mode vs sample-mode `effective_weight`, static/random conditions never write counters or event rows, probe turns never write, config validation failures for every rule incl. `decay.enabled: true`) plus an integration trace test replaying a seeded run segment and reconstructing feedback → increment → weight-as-of-turn → traversal path → retrieved set from logs alone.

**Done when:** all §8 unit tests pass; the trace test passes against real backend calls; `decay.enabled: true` refuses to start; full existing suite has no regressions.

**Deferred (tracked, not built):** counter-discounting decay, ACT-R activation, usage-gated decay, soft-exclusion pruning, the `negative_scope`/`weight_mode` ablation *runs* themselves (the knobs exist; the comparison experiments are a future protocol item), competitive normalization of outgoing edges.

---

### Milestone 21 — Harness Probe Battery Wiring (+ prompt-MU nesting fix)

**Why this milestone exists:** the 125-prompt stub pilot (PR #39) surfaced the biggest remaining gap between "harness runs" and "harness measures learning": the harness never called `POST /admin/run-probe`. Its "probe" turns were Phase-0-style `/chat` placeholders that mutated the graph, weren't stamped `is_probe_turn`, and recorded no `probe_results` rows — so §6.1 `probe_pass_rates` were all null and state-transition/bridge trajectories were empty. This milestone makes probe turns real measurements.

**Build:**
1. `scripts/harness/client.py` — `BackendClient.run_probe()` (POST /admin/run-probe, same connectivity/turn-error split as `chat()`, validates `result_id` in the response).
2. `scripts/harness/turn_resolution.py` — `assign_probe_schedule(sequence, probe_ids)`: stamps every probe turn with `probe_id` + `measurement_session_index` before the manifest is written. Purely positional (no RNG): within each series, probe turns walk the battery in order; each completed walk is one measurement session; session indices are 1-based and globally contiguous (the §6.3 no-gaps requirement).
3. `scripts/harness/schema.py` — `RunPlan.probe_set_version` (default `"v2"`), passed through to run-probe and used to pick `probes/probe_set_{version}.yaml`.
4. `scripts/harness/runner.py` — probe turns dispatch to `client.run_probe()` (never `/chat`, never judged); probe-battery preflight (unconditional hard stop before any graph mutation if the plan has probe turns but no battery loaded); per-condition restart env gains `CONDITION_LABEL` alongside `EXPERIMENT_ID`/`RUN_CONDITION` (the report layer resolves an experiment's condition from analytics rows — without it all three conditions inherit config.yaml's single default label); `load_probe_prompts` → `load_probe_battery`.
5. `scripts/harness/checkpoint.py` — `TurnCheckpoint.probe_id`/`measurement_session_index`; `detail` holds the probe `result_id` (the probe analogue of a chat turn's run_id).
6. **Backend contamination guard:** `probe_results` gains an `experiment_id` column (additive migration), stamped server-side in `POST /admin/run-probe` from the process-level `EXPERIMENT_ID` — `condition_label` alone is shared across attempts/dry-runs of the same condition, the same contamination class fixed twice in M19 (attempt collisions, dry-run `_dryrun` suffix). §6.1 and §6.3 now scope probe queries by `experiment_id` first, with a `condition_label` fallback for pre-M21 legacy rows.
7. **Prompt-MU nesting fix (also surfaced by PR #39):** prompt MUs no longer store the assembled prompt (with the `--- Context from memory ---` block); they store only the user-message portion (`_prompt_mu_content()`). MU content is retrievable, so the old behavior re-embedded every retrieved MU into a new node and nested old context inside new context on later turns. The full assembled prompt remains audited in `analytics.assembled_prompt_json`; context linkage remains structural (prompt→retrieved-MU edges).

**Done when:** probe turns hit `/admin/run-probe` with correct scheduling and bookkeeping; probe turns never chat and are never judged; runs with probe turns and no battery hard-stop before wiping; §6.1/§6.3 scope by experiment_id with legacy fallback; prompt MUs carry no context markers; full suite passes with no regressions.

---

## Turn-Wiring Fixes — Milestones 22–24 (Organic MU/Edge Topology)

**Source:** planning session 2026-07-09, decomposing `seed_v3_mu_spec.md`'s sibling doc on turn-wiring. Prerequisite for the full experimental run. **Not** the seed v3 fixture work — that stays a separate artifact.

### Why this exists

The run's secondary claim — that the system retrieves and routes through *constructed* knowledge (interaction MUs), not just seed MUs — requires a turn's output MU to route traversal to the MUs that grounded it. It currently can't.

```
CURRENT PER-TURN WIRING                        TARGET PER-TURN WIRING (M22+)
════════════════════════                       ══════════════════════════════

 ┌────────┐      ┌───────┐      ┌────────┐      ┌────────┐      ┌───────┐      ┌────────┐
 │ prompt │─────▶│ input │─────▶│ output │      │ prompt │─────▶│ input │─────▶│ output │
 └───┬────┘      └───────┘      └────────┘      └────────┘      └───────┘      └───┬────┘
     │ prompt_construction           ▲            (no longer fans out to               │ co_activation
     ▼                               │             retrieved MUs)                      │
 ┌────────┐  ┌────────┐              │                                    ┌────────────┼────────────┐
 │retrieved│ │retrieved│  ... N      │                                    ▼            ▼             ▼
 │  MU 1   │ │  MU 2   │  edges      │                              ┌────────┐   ┌────────┐   ┌─────────┐
 └────────┘  └────────┘        (dead end —                          │retrieved│  │retrieved│  │ summary │
                                output has no                        │  MU 1   │  │  MU 2   │  │   MU    │
                                outgoing edges)                      └────────┘   └────────┘  └────┬────┘
                                                                                                     │ co_activation
                                                                                          ┌──────────┴──────────┐
                                                                                          ▼                     ▼
                                                                                    ┌──────────┐         ┌──────────┐
                                                                                    │source MU │         │source MU │
                                                                                    │(fed the  │         │(fed the  │
                                                                                    │ summary) │         │ summary) │
                                                                                    └──────────┘         └──────────┘
```

**Why this matters (sanity narrative):** turn 5's response synthesizes a "roster file" from two seed rules. Turn 9 asks "write one compliant roster entry" — no cluster vocabulary, so BM25 enters at turn 5's *output* MU (the only place "roster" exists with substance). Under current wiring, traversal from that MU reaches nothing and the answer degrades. Under target wiring, `output₅→spoke_a` / `output₅→spoke_b` are one hop away: context assembles constructed knowledge *plus* the seed rules it depends on, and M20's path credit strengthens exactly those routing edges — constructed MUs entering the same learning loop as seed edges, zero learning-code changes required.

**Experimental-integrity constraint (applies to every milestone below):** all wiring changes are condition-invariant — identical edges, identical `initial_edge_weight`, identical Beta priors (`edge_update.prior_alpha/prior_beta`) across learned / static_weight / random_weight. Nothing in M22–M24 branches on `RUN_CONDITION`.

### Why this build order

M22 is the hard/risky part — it changes what edges exist and where. Get it right and provable (edge-count acceptance tests) before touching the extractor. M23 depends on M22 only stylistically (same `creation_trigger="co_activation"` pattern, same priors) — it's a separate file (`extractor.py`) and a separable review, so it's its own milestone rather than folded into M22. M24 is verification-only and comes last on purpose: it needs M22+M23's actual edge population shape to audit against, and its required "done" proof (a smoketest series report with sane §5.3/§5.4 numbers) only means something once the new wiring is live.

---

### Milestone 22 — Output-MU Routing Edges + Retrieval Candidacy Exclusion

**Why together:** both tasks touch the same call path (`orchestrator.py` Step 5 → `retrieve()`), and T2 is the direct fix for the lexical-duplication problem that T1's edge relocation makes room for — input and prompt MUs are near-identical text (`_prompt_mu_content()`), so leaving prompt MUs in the BM25 pool would waste K1 slots on duplicates now that context linkage lives on the output MU instead.

**Build:**

1. **`backend/core/orchestrator.py` Step 5 — remove the retrieval fan-out.** Delete the `for mu_id in retrieval.activated_mu_ids: await graph_store.create_edge(source_mu_id=prompt_um.mu_id, ...)` loop (lines ~412–423). Keep the `prompt → input` edge unchanged. Update the surrounding comment block (currently says context linkage "lives in this MU's prompt->retrieved-MU edges" — it no longer does) and `_prompt_mu_content()`'s docstring, which makes the same now-false claim.

2. **`backend/core/orchestrator.py` Step 9 area — add the output fan-out.** After the output MU is created and its `input → output` edge is written, add one `create_edge(source_mu_id=output_um.mu_id, target_mu_id=mu_id, ..., creation_trigger="co_activation", ...)` call per MU in `activated_mu_ids` (the variable already computed at line ~350, which already includes `summary_mu_id` when Mode 3 fires — so `output → summary` arrives for free, no extra code). Use `weight=INITIAL_EDGE_WEIGHT`, `alpha/beta=EDGE_UPDATE_CFG["prior_alpha"/"prior_beta"]`, `creation_run_id=run_id` — identical pattern to every other edge-creation call site in this function.

   **Note — don't touch `activated_edge_ids`.** `RunRecord.activated_edge_ids`/`InteractionResult.activated_edge_ids` stay exactly `retrieval.activated_edge_ids` (the K1→K2 traversal edges). The new co_activation edges are not traversal-path edges and M20's credit assignment (`process_verdict_for_learning()`) reads `traversal_path`, not this field — adding them here would be a no-op at best and misleading at worst.

3. **`config.yaml` — new hyperparameter.** The spec calls this a key "under `retrieval:`", but `config.yaml`'s actual registry is a flat `hyperparameters:` block with a `retrieval_` prefix convention (`retrieval_keyword_pool_size`, `retrieval_level1_top_k`, ...). Match that: add `hyperparameters.retrieval_exclude_source_types: ["prompt"]` (a YAML list), not a new nested section. Document it inline the way every other hyperparameter is documented (range/purpose comment above it).

4. **`backend/memory/retrieval.py` — candidacy filter.** In `retrieve()`, immediately after `all_mus = await store.get_all_mus()`, filter: `excluded = set(hp.get("retrieval_exclude_source_types", ["prompt"])); all_mus = [m for m in all_mus if m["source_type"] not in excluded]`. Log a line reporting how many were excluded (`logger.info("retrieve: excluded %d MUs by source_type filter=%s", ...)`). Do **not** filter `all_edges` — `edge_weight_sum` is built from the unfiltered edge list, so excluded MUs' edges still contribute to their neighbors' centrality scores, exactly as the acceptance criterion requires. Leave the filter as a single `source_type not in excluded` predicate — this is also the future home of `pruning_flag`-based soft exclusion; don't implement that now, just don't structure the filter in a way that makes adding a second predicate awkward later.

   **Side-effect worth knowing, not fixing:** because `mu_by_id` (used to resolve K2 traversal targets) is built from the same filtered `all_mus`, an excluded MU could never surface via K2 traversal either (silently dropped by the existing `if mid in mu_by_id` guard), not just via BM25. Today nothing ever creates an edge *targeting* a prompt MU, so this never fires — but flag it here so a future exclusion type doesn't discover it the hard way.

**Tests:**
- `test_orchestrator.py`: a turn with N retrieved MUs and no Mode-3 summary produces exactly N+2 edges (`prompt→input`, `input→output`, N×`output→retrieved`) with `creation_trigger="co_activation"` on the new ones; with a Mode-3 summary, N+3 edges (adds `output→summary`). A turn with a failed-output source type (`agent_output_failed_*`) produces the same edge shape — failed/empty MUs are retained analytic fixtures, not suppressed. All edge values (`weight`, `alpha`, `beta`) are identical regardless of `RUN_CONDITION`.
- `test_retrieval.py`: with the default config, prompt MUs never appear in `candidate_set_before_ranking`, `entry_point_mu_ids`, or the retrieved set; a query matching a prompt MU's keywords resolves to zero candidates from it. Setting `retrieval_exclude_source_types: []` restores current (pre-M22) retrieval behavior exactly — same candidate set as before the filter existed.

**Done when:** both acceptance criteria above hold under real pytest runs; full suite passes with no regressions; a manual `/chat` exchange over 2+ turns shows (via logs) an `output→retrieved` edge on turn 2 pointing back at turn 1's retrieved MUs, and zero `prompt→retrieved` edges anywhere in the run.

**Risks:** ⚠ Every existing test that asserts exact edge counts/shapes for a turn (there are several, going back to M8/M12) will need updating — this is expected fallout, not a regression signal; audit `test_orchestrator.py` for stale assertions before declaring the suite clean.

---

### Milestone 23 — Extractor: Summary Edges + Source-Kind Labels

**Why second:** closes the two remaining organic-wiring gaps, both localized to `extractor.py`: Mode-3 summary MUs are edge-orphans (found lexically, never by traversal — the same disease M22 just cured on the output MU), and mixed seed/interaction content in the condensed context is illegible to Gemma without a cue for what kind of memory it's looking at.

**Build:**

**(a) Summary → source edges.**

- `orchestrator.py` must thread `run_id` into `extract()` — it currently isn't passed (`extract(mus=..., keywords=..., store=..., session_id=..., ollama_host=..., model_name=...)` has no `run_id` param today). Add `run_id=run_id` to the call and to `extract()`'s signature.
- `extract()`'s Mode 3 branch must pass the full `mus` list into `_summarize()` (currently `_summarize()` only receives the condensed `text`, not which MUs produced it) — add a `mus` param to `_summarize()`.
- In `_summarize()`, after `create_mu()` succeeds, create one `summary → mu` edge per MU in `mus`, `creation_trigger="co_activation"`, using the same priors pattern as everywhere else.
- **Priors source:** `extractor.py` already loads its own module-level `_CONFIG` (mirrors `orchestrator.py`'s `_load_config()` pattern, duplicated on purpose per the existing convention). Read `_CONFIG.get("edge_update", {}).get("prior_alpha", 1.0)` / `("prior_beta", 1.0)` directly — no need to import `validate_edge_update_config`; `orchestrator.py` already hard-fails startup on invalid `edge_update` config before any request reaches `extract()`, so a second validation pass here would be redundant.
- **Pre-existing gap, flagged not fixed:** `_summarize()`'s `create_mu()` call doesn't pass `series_id`/`experiment_id` (unlike every other `create_mu()` call site in the codebase) — the summary MU is stamped with `series_id=NULL`. This predates this milestone and isn't in the spec's scope; note it in PROGRESS.md as a follow-up rather than silently fixing it here, since `experiment_id` isn't currently plumbed into `extractor.py` at all and doing it properly is a bigger change than this milestone's acceptance criteria call for.

**(b) Source-kind content labels.**

Prefix each chunk in `_mode1_extract()`/`_mode2_extract()` with a bracketed label derived from the MU's origin, before joining into `condensed`.

**Open decision — the spec's label scheme doesn't match the data model; resolve before coding.** The spec says to derive labels from `source_type` (e.g. `"[memory — reference]"` for seed MUs). But seed MUs are *not* distinguishable by `source_type` — every seed MU in both `seed_v2.json.gz` and the `seed_v2_draft.json` fixture is stamped `source_type: "agent_output"`, identical to a real turn's response. The only field that distinguishes seed-origin content is `series_id` (prefix `"seed:"`) — the exact mechanism `series_report.py` §5.3 already uses (`mu_series.startswith("seed:")`) to classify MUs as `seed_fixture` vs `intra_series`/`cross_series`. `get_all_mus()` returns full rows (including `series_id`), so this field is already present on every MU dict `extract()` receives.

Proposed label derivation (series_id check first, source_type fallback second):

| Condition | Label |
|---|---|
| `series_id` starts with `"seed:"` | `[memory — reference]` |
| `source_type == "user_input"` | `[memory — prior user message]` |
| `source_type == "agent_output"` (non-seed) | `[memory — prior response]` |
| `source_type in ("agent_output_failed_token_limit", "agent_output_failed_empty_output")` | `[memory — prior response]` (a build-time judgment call — folding failed variants into the same label keeps the vocabulary small; split into `[memory — prior response (failed)]` if judge/Gemma behavior on failed-turn content warrants it during testing) |
| `source_type == "system"` (a Mode-3 summary MU, now itself retrievable) | `[memory — synthesized summary]` |
| anything else (`tool_call`, `tool_result`, unknown) | `[memory — other]` |

Confirm this table (or a revision) with the user before implementing — it's a real deviation from the written spec, not just an implementation detail.

**Tests:**
- `test_extractor.py`: Mode-3 summary MU has outgoing edges to every MU in the pre-summarization `mus` list, `creation_trigger="co_activation"`, priors from config. Label prefixes appear correctly for a seed MU (keyed by `series_id`, not `source_type`), a real `agent_output` MU, a `user_input` MU, and a `system` (summary) MU, in both Mode 1 and Mode 2 output.
- Regression: existing Mode 1/Mode 2/Mode 3 tests updated to expect labeled chunks (their assertions currently check raw sentence text).

**Done when:** labels appear in condensed context for both Mode 1 and Mode 2 paths; Mode-3 summary MUs have outgoing edges to all their sources; full suite passes with no regressions.

**Risks:** ⚠ Label text becomes part of `assembled_prompt_json` and judge inputs — once judged turns exist against a label vocabulary, changing it later invalidates comparability across runs. Pick the table above deliberately, not provisionally.

---

### Milestone 24 — Verifications + Reports/Admin Audit + Smoketest

**Why last:** every item here is confirm-or-fix against M22+M23's actual edge population, not new design. Needs the new wiring live to mean anything.

**Build (verification-first — only touch code where a check actually fails):**

1. **SimpleRL unreachable in the run configuration.** Confirmed already by inspection: `POST /feedback/verdict` (the harness's endpoint) routes exclusively through `learning/edge_update.process_verdict_for_learning()`, which itself refuses to write anything unless `condition_label == "learned"` (belt-and-suspenders, not just trusting the caller). `learning/simple_rl.py` is only reachable from `POST /feedback` (the chat-UI thumbs endpoint, human feedback, outside the harness/experiment path). No fix needed — this is a clean pass; just record the confirmation in PROGRESS.md so it isn't re-litigated.
2. **Probe turns create no MUs/edges, unaffected by M22.** `probes.py`'s `run_probe()` (via `POST /admin/run-probe`) only ever calls `retrieve()`, never `run_interaction()` — M22's changes are entirely inside `run_interaction()`'s Step 5/9, so this invariant is untouched by construction. Confirm with a quick grep/read pass, not new tests.
3. **Probe retrieval passes `condition=RUN_CONDITION`.** Already true — `admin.py`'s `run_probe()` calls `retrieve(..., condition=RUN_CONDITION, rng=EDGE_RNG)` (confirmed at read time; this was evidently fixed since the spec was drafted). No fix needed; record the confirmation.
4. **`edges.direction` / `directional_edge_weight_multiplier` documented as inert.** Confirmed: every `create_edge()` call site in the codebase passes `direction="forward"` (grepped `backend/`, all matches are literal `"forward"` or `"in"/"out"` on the UM boundary field, not the edge table), and `retrieval.py`'s K2 scoring applies `dir_multiplier` uniformly to every traversed edge regardless of any direction check — it is a constant factor on all K2 scores, not a forward-vs-bidirectional bias as the config comment implies. Add a note to the `edges` table's schema comment in `store.py` (near the `CREATE TABLE edges` block) and to the `directional_edge_weight_multiplier` comment in `config.yaml` so this isn't rediscovered as a bug later.

5. **`series_report.py` §5.3/§5.4 audit.** Read, don't guess: §5.3 (`_section_53...`) classifies by `run["activated_mu_ids"]` and `mu_series` prefix — no `creation_trigger` filter anywhere, so it already picks up co_activation-sourced retrievals without changes. §5.4 (`_section_54_cross_series_edges` → `store.get_cross_series_edges()`) queries all edges touching a series with no `creation_trigger` filter either. Both are confirmed unaffected by inspection; if the M22/M23 smoketest (step 7 below) shows otherwise, fix then — don't pre-emptively add filters.
6. **Admin/frontend edge listings.** Grepped `admin/src` for `creation_trigger` — no matches; nothing there enumerates trigger values today, so there's nothing to update.
7. **Required smoketest.** Run a short smoketest series (5–10 turns, real backend) after M22+M23 are merged. Generate a series report. Confirm: §5.3/§5.4 numbers are sane (no unclassified-edge warnings, cross-series counts reflect the new `output→retrieved` edges regularly crossing series boundaries — expected, not an anomaly), and the edge population shows the new shape (output MUs at degree N+1 instead of degree 1, `co_activation`-triggered edges present, `prompt_construction` edge counts per turn dropped).

**Tests:** no new unit tests expected beyond what M22/M23 already added, unless step 7's smoketest surfaces a real bug — in which case it gets a regression test like every other smoketest finding in this project's history.

**Done when:** all 4 verification items (1–4) are confirmed and documented (fixed only if actually broken); the report audit (5–6) confirms no changes needed or applies the minimal fix if it does; the smoketest (7) produces a series report with sane §5.3/§5.4 numbers and no unclassified-edge warnings.

---

### Non-Goals (M22–M24)

- **Reciprocal organic edges** (retrieved→output direction): deferred behind a future `organic_reciprocal` config knob, default off. Reverse edges would pull past responses into hub K2 competition and contaminate the spoke-discrimination measurement planned for the upcoming run.
- **Multi-hop traversal** (`retrieval_max_hops`): a later milestone. `compute_path_increments()` (M20) already supports multi-hop credit math; retrieval itself does not.
- **Decay / pruning-flag filtering / MU-level counters:** future milestones. M22's exclusion filter is structured to accept a second predicate later, nothing more.
- **Tool MU wiring:** `tool_call`/`tool_result` MUs stay edge-orphaned this run (no tools fire in experiment turns); revisit alongside the application build.
- **Seed v3 fixture changes** (reciprocal seed edges, counterfactual content, and — newly relevant after this planning session's discovery — giving seed MUs a `source_type` that actually distinguishes them from interaction MUs instead of relying solely on `series_id`): a separate artifact, `seed_v3_mu_spec.md`. **M23 hardens `series_id` prefix `"seed:"` into a stated requirement for that future v3 generator, not just a v2 convention** — `extractor.py`'s source-kind label derivation (`_source_kind_label()`) keys its `[memory — reference]` label directly off `series_id.startswith("seed:")`, so any v3 generator that stops stamping that prefix silently mislabels seed content as `[memory — prior response]` instead, with no error raised. If v3 ever gives seed MUs a distinguishing `source_type`, `_source_kind_label()`'s series_id-first check should be revisited alongside it — until then, the prefix is load-bearing. **Now planned as Milestone 25, below** (the `source_type`-distinguishing idea is not part of M25's scope — `seed_v3_mu_spec.md` §7.3a confirms the `series_id` prefix mechanism is sufficient and requires no generator special-casing).

---

## Seed Fixture v3 — Milestone 25 (Counterfactual Content Generation + Verification)

**Source:** `seed_v3_mu_spec.md` (approved by Paul, 2026-07-11) — the full per-MU
content table, vocabulary design, and resolved open questions live there, not
here. This section is the *build* spec: what `generate_seed_v3.py` and
`verify_seed_v3.py` must do, how they differ from their v2 predecessors, and
the new failure-manifest requirement.

### Why this exists

seed_v2's content is true-in-general ("a well-defined schema specifies the
expected data shape and type for each element..."). Gemma already knows this
from pretraining — it can produce a plausible-sounding correct-shaped answer
whether or not retrieval surfaced the right MU. That decouples judge verdicts
from retrieval quality, which decouples edge updates from anything real:
M20's learning mechanism would be reinforcing noise, not retrieval success.

seed_v3 replaces every cluster's content with **invented, unguessable
specifics** — a proper-noun rule family (Kestrel, Marrow, Vessel, Ledger)
with numbered rules, each rule paired with an invented value token
(`TILDEBAR`, `three-to-one ratchet`, `WAIVERKEY`, ...). Gemma has no
parametric knowledge of any of these, so a response can only contain the
right rule ID + value token pair if retrieval actually surfaced the gold MU's
content. This makes judge verdicts — and therefore edge updates — causally
downstream of retrieval quality, which is the entire point of the experiment.

The vocabulary is additionally split into two tiers (entry vocabulary,
saturated in the hub and free in prompts; answer tokens, confined to one
spoke each and forbidden in prompts) so that BM25 alone can find the *hub*
but never the *gold spoke* — only graph traversal can, which is what makes
the measurement a test of the memory graph rather than a test of keyword
search. Full rationale: `seed_v3_mu_spec.md` §1.

A second, independent fix rides along: v2's edges are one-directional
(spoke→hub→bridge), so a query that enters at the hub can never reach a
spoke via K2 traversal — confirmed by reading `retrieval.py`'s Stage 3, which
walks `source_mu_id` only. v3 makes every edge reciprocal:

```
v2 (32 edges, one-way):                    v3 (64 edges, reciprocal):

  spoke ───────▶ hub ───────▶ bridge         spoke ◀──────▶ hub ◀──────▶ bridge
                                              (0.5 each direction)

  Query enters at hub → traversal            Query enters at hub → traversal
  can reach the bridge, never a spoke.        CAN reach a spoke (the actual
  Spokes are structurally unreachable         retrieval physics the two-tier
  from a hub-anchored query.                  vocabulary design depends on).
```

Full rationale for reciprocal-not-undirected: `seed_v3_mu_spec.md` §2.

### Pipeline (what this milestone builds)

```
seed_v3_mu_spec.md (frozen, approved — §4's per-MU table is the source of
                     truth for every required/forbidden term list below)
        │
        ▼
generate_seed_v3.py ──(Ollama gemma4:e4b, per-MU generate+validate+retry)──┐
        │                                                                  │
        │ MAX_RETRIES exhausted with an unresolved violation               │
        ▼                                                                  ▼
seed_v3_generation_manifest.json                              seed_v3_draft.json
  {mu_id, mu_key, cluster_id,                                  (34 MUs, 64 edges,
   attempts, missing_required,                                  fixture_version="v3")
   forbidden_leaked, last_attempted_text}
        │
        │  Paul reads the manifest, hand-authors the
        │  failed MU(s)' raw_content directly in the draft ────────┐
        │  (no research reason seed prose must be Gemma-written —  │
        │   see spec §5)                                           │
        ▼                                                          ▼
verify_seed_v3.py --draft   (9 checks — structural, placement,     │
                              lexical, probe-sanity; see below)     │
        │                                                           │
   FAIL │ (fix content, rerun)  ◀──────────────────────────────────┘
        │
   PASS ▼
Paul freezes: seed_v3_draft.json → seed_v3.json.gz (human step, same
              gzip-and-strip-notes one-liner as v2)
        │
        ▼
verify_seed_v3.py --frozen   (final confirmation against the committed
                               artifact — belt-and-suspenders, catches any
                               difference the freeze step itself introduced)
        │
        ▼
committed to repo — seed_v3.json.gz + seed_v3_mu_spec.md.
Everything in spec §6 (probe set v3, prompt bank v3, harness fixture-path
switch, weight_mode decision) is future-milestone work that re-derives from
this frozen artifact. Not built here.
```

**Workflow deviation from v2, and why:** v2 froze to `.gz` *then* ran
`verify_seed_v2.py` against the frozen file (Phase 2 then Phase 3 in
Milestone 18). v3 reverses that: `verify_seed_v3.py` gets a `--draft`/
`--frozen` flag and runs against the *draft* first. This is what actually
makes the manifest-driven repair loop practical — Paul iterates on
`seed_v3_draft.json` (rerun verify, no gzip round-trip) until every check
passes, and only then freezes. `--frozen` still exists as a final sanity
check post-freeze, matching v2's original guarantee.

### Per-MU generation + validation + manifest (the state machine `generate_mu()` implements)

```
                    ┌───────────────────────┐
                    │  call Ollama (attempt) │
                    └───────────┬────────────┘
                                ▼
                    ┌───────────────────────────┐
                    │ check required terms       │  (spec §4 "Required terms"
                    │ check forbidden terms       │   column, copied verbatim
                    │ check hub token exclusion   │◀── NEW: hub content must
                    │   (hub MUs only)             │    contain none of its own
                    │ check sibling token exclusion│   cluster's value tokens
                    │   (spoke MUs only)           │◀── NEW: spoke must contain
                    └───────────┬───────────────┘    none of its siblings' tokens
                     all pass?  │
             ┌───────yes────────┴────────no───────┐
             ▼                                     ▼
      keep content,                     attempt < MAX_RETRIES?
      move to next MU                  ┌─────yes────┴────no─────┐
                                        ▼                        ▼
                                  retry (sleep,          append failure record
                                  attempt += 1)           to in-memory list:
                                                           {mu_id, mu_key,
                                                            cluster_id, attempts,
                                                            missing_required,
                                                            forbidden_leaked,
                                                            last_attempted_text}
                                                           keep best-effort content
                                                           in the draft (flagged,
                                                           does not block the run)
```

At the end of `main()`, the failure list — even if empty — is written to
`seed_fixtures/seed_v3_generation_manifest.json`:

```json
{
  "generated_at": "2026-07-14T18:03:00+00:00",
  "source_script": "generate_seed_v3.py",
  "mu_count_failed": 1,
  "failures": [
    {
      "mu_id": "c0000004-cccc-4ccc-8ccc-000000000004",
      "mu_key": "c_spoke3",
      "cluster_id": "cluster_c",
      "attempts": 3,
      "missing_required": ["WAIVERKEY"],
      "forbidden_leaked": [],
      "last_attempted_text": "Vessel V-3 governs exemptions: an exemption must be issued before the act it covers, and any exemption issued afterward is void on its face..."
    }
  ]
}
```

An empty `"failures": []` is a meaningful, present result (PASS signal), not
an absent file — matches this project's existing convention of preserving
empty-result structure rather than omitting the artifact (e.g. the M21
"empty-section contract" in `series_report.py`). Console output at the end of
`main()` prints a one-line-per-failure summary and, if any exist, the exact
next step ("edit seed_v3_draft.json's raw_content for c_spoke3, then rerun
`python scripts/verify_seed_v3.py --draft`"). The manifest is a working
artifact, not a frozen one — gitignored alongside the draft (see File
Structure below), since its relevance ends once the draft is repaired.

### Two-tier vocabulary → generator constants

`generate_seed_v3.py` (a copy of `generate_seed_v2.py`, per spec §5) replaces
the single `CLUSTER_*_FP` lists with four dicts, keyed by cluster and by MU
key respectively — all values copied verbatim from `seed_v3_mu_spec.md` §3–4,
not re-derived:

```python
# MU_IDS: copied verbatim from generate_seed_v2.py's MU_IDS dict — same UUIDs.
# Preserves continuity for the future probe_set_v3 gold-MU references (spec
# §6: "golds are spoke MUs, IDs unchanged from v2 where convenient").

FAMILY_NAMES = {
    "cluster_a": "Kestrel", "cluster_b": "Marrow",
    "cluster_c": "Vessel",  "cluster_d": "Ledger",
}  # full family names ("Kestrel Output Conventions") also required in hub
   # content per spec §4; FAMILY_NAMES here is the short form used for
   # cross-cluster fingerprint scanning (matches how spec §1 describes them
   # as "strong BM25 anchors").

ENTRY_VOCAB = {
    "cluster_a": ["format", "structure", "output", "field", "delimiter",
                  "header", "quoting", "encoding", "padding"],
    "cluster_b": ["schema", "data", "shape", "type", "validation",
                  "column", "nesting", "version", "range"],
    "cluster_c": ["rule", "constraint", "violation", "enforce", "comply",
                  "severity", "exemption", "escalation", "deadline"],
    "cluster_d": ["source", "provenance", "cite", "origin", "traceable",
                  "attribution", "timestamp", "custody", "retraction"],
}

# ANSWER_TOKENS: mu_key -> list of this MU's own value token(s). Hub keys map
# to [] (index only, spec §4 placement rule 1). Used for (a) required-terms
# assembly, (b) hub/sibling exclusion checks, (c) the global-uniqueness
# self-check below.
ANSWER_TOKENS = {
    "a_hub": [],
    "a_spoke1": ["spine order"], "a_spoke2": ["TILDEBAR"],
    "a_spoke3": ["KAPLINE"],     "a_spoke4": ["P-fault"],
    "a_spoke5": ["grove quotes"], "a_spoke6": ["ENCK9"],
    "a_spoke7": ["NULLMARK"],
    # ... b_*, c_*, d_* per spec §4 (b_spoke1 has three: WHOLENUM, SPANTEXT,
    # FLAGBIT — ANSWER_TOKENS values are always a list, never a bare string)
}

ANCHOR_WORDS = {
    "a_spoke1": "field", "a_spoke2": "delimiter", "a_spoke3": "header",
    "a_spoke4": "padding", "a_spoke5": "quoting", "a_spoke6": "encoding",
    "a_spoke7": "field",
    # ... b_*, c_*, d_* per spec §4 "Anchor" column
}

RULE_IDS = {
    "a_spoke1": "K-1", "a_spoke2": "K-2", ...,  # per spec §4 "Rule" column
}
```

**Required-terms lists are copied verbatim from spec §4's "Required terms"
column per MU** — not derived programmatically from the dicts above — to
avoid a derivation bug silently diverging from the approved spec. The dicts
above exist for the *validation* logic (hub/sibling exclusion, uniqueness),
not for reconstructing required-terms strings.

**Forbidden-terms logic, per MU category (new relative to v2, which only
forbade terms on bridges):**

| MU category | Forbidden terms |
|---|---|
| Hub | Its own cluster's answer tokens (all spokes' value tokens — NOT rule IDs; the hub must name rule IDs, e.g. "K-2 governs delimiters", but never define a value). |
| Spoke | Every *sibling* spoke's answer tokens within the same cluster (placement rule 2: "no other spoke's tokens"). |
| Bridge | All entry vocabulary + all answer tokens + family names from **both** adjacent clusters (spec §4 "Forbidden" column — a superset of v2's 5-term-per-cluster fingerprint list). |

**Global answer-token uniqueness — self-check at import time, before any
Ollama call:** a `_validate_answer_token_uniqueness()` function iterates
`ANSWER_TOKENS.values()` and asserts no token string appears under more than
one `mu_key`. This catches a spec-transcription bug (e.g. two clusters
accidentally sharing a token) immediately and loudly, rather than surfacing
as a confusing K2-traversal ambiguity three milestones later.

**Answer-token format check:** placement rule 4 says tokens must be
word-like (letters/digits/hyphens only) with no punctuation-dependent forms.
This applies cleanly to the single-word ALL-CAPS tokens (`TILDEBAR`,
`KAPLINE`, `ENCK9`, `NULLMARK`, `WHOLENUM`, `SPANTEXT`, `FLAGBIT`,
`BOUNDSPAN`, `WAIVERKEY`, `GREYMARK`, `SRCLINE`) — checked against
`^[A-Za-z0-9-]+$`. The multi-word phrase tokens (`spine order`, `grove
quotes`, `three-to-one ratchet`, ...) are natural-language value phrases, not
single "words" in the rule-4 sense — they tokenize normally under BM25 and
aren't punctuation-dependent, so the format check only applies to the
single-token category. Documented here as a deliberate scope reading of rule
4, not an oversight.

### Reciprocal edges (64 total)

```python
def _build_edges() -> list[dict]:
    """56 spoke<->hub edges (7 spokes x 4 clusters x 2 directions) +
    8 hub<->bridge edges (4 clusters x 2 directions) = 64. Every edge:
    weight=0.5, direction="forward" (schema literal — see M24's finding
    that this column doesn't encode which way the edge points; the edge's
    actual directionality is source_mu_id -> target_mu_id), creation_trigger
    is not a fixture-file field (register_fixture() hardcodes "seed" for
    every edge it inserts — unchanged from v2)."""
```

Each v2 edge becomes two v3 edges (forward + reverse), same weight. Edge ID
scheme: suffix `r` on the existing v2 edge_id for the reverse direction
(e.g. `ea000002` forward stays as-is, `ea000002r` is the new spoke→hub... —
wait, hub→spoke — reverse). Exact IDs are a `/build`-time implementation
detail; the acceptance criterion is reciprocity, not a specific naming
scheme.

### `verify_seed_v3.py` — 9 checks (v2 had 5)

1. **MU count / cluster distribution** — unchanged from v2 (34 MUs, same
   per-cluster counts).
2. **Edge count / wiring** — 64 edges (not 32); for every forward edge
   `(s→t, w)` a reverse edge `(t→s, w)` with the same weight must also
   exist. Hub touches 16 edges (7 in + 7 out spoke pairs, 1 in + 1 out
   bridge pair); each spoke touches 2 (1 in + 1 out).
3. **Hub↔bridge connectivity, both directions** — v2 only checked
   hub→bridge; v3 also requires bridge→hub.
4. **Lexical-distinctness (bridges)** — upgraded from v2's 5-term
   fingerprint to the full two-tier set: bridges must contain none of
   either adjacent cluster's entry vocabulary, answer tokens, or family
   names (spec §4 bridge "Forbidden" column).
5. **NEW — Hub token exclusion:** each hub's content contains zero answer
   tokens from its own cluster.
6. **NEW — Spoke sibling-token exclusion:** each spoke's content contains
   none of its sibling spokes' answer tokens (same cluster).
7. **NEW — Global answer-token leakage scan (content-level, not just spec-
   level):** for every MU, scan its `raw_content` for every *other* MU's
   answer tokens project-wide (whole-phrase, case-insensitive). This is the
   strongest check in the set — it validates the frozen text itself, so it
   also catches a hand-authored repair (from the manifest workflow) that
   accidentally pastes in the wrong token.
8. **NEW — Answer-token format check** — the `^[A-Za-z0-9-]+$` check
   described above, run against the single-word token category.
9. **Baseline probe sanity (BM25 simulation, extends v2's check 5):**
   - Bridge queries (cluster entry vocabulary) still must NOT surface the
     bridge at K1 — same as v2.
   - **NEW — hub queries must reach a same-cluster spoke at K2.** This is
     the actual capability v3's reciprocal edges exist to enable (spec §2);
     a query using only hub-saturated entry vocabulary should place the hub
     at K1 and *some* spoke from the same cluster at K2. (K2's `top_k=1`
     with all-equal 0.5 weights means *which* spoke wins is
     fixture-order/tie-break dependent — spec §6 flags this as
     "query-independent, winner-take-all" and explicitly future work; the
     check only asserts cluster membership, never a specific spoke.)
   - **NEW — two-hit bar sanity:** for a query containing all of a
     cluster's entry vocabulary but no spoke's anchor word, no spoke should
     appear in the K1 multi-hit set (spokes only get one BM25 hit — the
     family name — without their anchor word present). Reproduces placement
     rule 6's reasoning as a check on the frozen fixture, not just a
     property asserted in prose.

`verify_seed_v3.py` duplicates its own copy of `ENTRY_VOCAB`/`ANSWER_TOKENS`/
`FAMILY_NAMES`/`ANCHOR_WORDS` rather than importing them from
`generate_seed_v3.py` — same "verify what's frozen, not what was intended"
convention v2 already uses (`verify_seed_v2.py`'s `CLUSTER_*_FP` constants
are a standalone copy, not an import).

### File structure

```
seed_fixtures/
├── seed_v2.json.gz                    (unchanged — stays the smoke-test/
│                                        prior-experiment fixture)
├── seed_v3_draft.json                 (NEW — gitignored, matches existing
│                                        *_draft.json glob)
├── seed_v3_generation_manifest.json   (NEW — gitignored, add explicit
│                                        pattern to .gitignore)
└── seed_v3.json.gz                    (NEW — committed once frozen)

scripts/
├── generate_seed_v3.py                (NEW — copy of generate_seed_v2.py +
│                                        two-tier vocab + manifest emission)
└── verify_seed_v3.py                  (NEW — copy of verify_seed_v2.py +
                                         4 new checks + --draft/--frozen)

seed_v3_mu_spec.md                     (already committed this session —
                                         source of truth for §4's content)

tests/test_scripts/
└── test_generate_seed_v3.py           (NEW — see Tests below)
```

`.gitignore` gains one line: `seed_fixtures/*_generation_manifest.json`
(the existing `seed_fixtures/*_draft.json` pattern already covers the draft).

### Tests

v2's generator/verifier have no pytest coverage — they're Ollama-dependent
scripts validated by a manual run + the standalone verify script. v3 keeps
that convention for the Ollama-dependent path (no mocked-Ollama pytest
suite), but the *pure-Python* validation logic is materially more complex
now (two-tier vocab, hub/sibling exclusion, uniqueness assertion, manifest
serialization) and has no Ollama dependency at all — worth covering per
CLAUDE.md's global testing standard. New `tests/test_scripts/test_generate_seed_v3.py`:
- `_check_required_terms` / `_check_forbidden_terms` — reused from v2
  unchanged, but add a case with a multi-word phrase token (v2 only ever
  tested single words).
- Hub-exclusion check: hub content containing a value token fails; hub
  content naming only rule IDs passes.
- Sibling-exclusion check: spoke content containing a sibling's token fails.
- `_validate_answer_token_uniqueness()`: raises on a deliberately duplicated
  token fixture; passes on the real `ANSWER_TOKENS` table (this doubles as a
  regression test on the actual spec data — if someone edits `ANSWER_TOKENS`
  and introduces a collision, the test suite catches it without Ollama).
- Manifest emission: a MU that exhausts `MAX_RETRIES` with a missing
  required term produces exactly one failure record with the right
  `mu_id`/`missing_required`/`last_attempted_text`; a fully-passing run
  writes `"failures": []`, not an absent file.
- Token format check: `TILDEBAR` passes, a deliberately punctuation-bearing
  fake token (`ty::val`) fails.

### Build steps

1. Copy `generate_seed_v2.py` → `generate_seed_v3.py`. Replace `MU_SPECS`
   with the two-tier constants above, all values transcribed verbatim from
   `seed_v3_mu_spec.md` §3–4. Add hub/sibling exclusion checks to the
   per-MU validation branch in `generate_mu()`. Add manifest accumulation
   and the end-of-run manifest write. Update `fixture_version` to `"v3"` in
   the output JSON (this alone is what gives every seed MU the correct
   `series_id="seed:v3"` at registration time — confirmed in spec §7.3a,
   no other change needed for that).
2. Copy `verify_seed_v2.py` → `verify_seed_v3.py`. Add checks 5–8 above.
   Extend check 9's probe-sanity simulation with the hub→spoke and
   two-hit-bar cases. Add `--draft`/`--frozen` CLI flag (default `--draft`).
3. Add `.gitignore` line for the manifest pattern.
4. Write `tests/test_scripts/test_generate_seed_v3.py` per Tests above; run
   `pytest tests/test_scripts/test_generate_seed_v3.py` — must pass before
   the real Ollama run.
5. **Required live run:** `python scripts/generate_seed_v3.py` against real
   Ollama `gemma4:e4b`. Confirm the manifest mechanism actually fires:
   if the real run has zero failures on the first try (plausible — v2's
   fingerprint terms are easier than v3's answer tokens, but not
   guaranteed), deliberately sabotage one MU spec's required-terms list
   (e.g. add an impossible term) in a throwaway copy to force a manifest
   record, confirm its shape matches the schema above, then revert the
   sabotage. This is the milestone's proof that "failed MUs are explicitly
   documented in a manifest" actually works end to end, not just in unit
   tests against synthetic data.
6. `python scripts/verify_seed_v3.py --draft` against the real draft. Fix
   any failing MU content directly in `seed_v3_draft.json` (by rerunning
   the generator for just that MU, or hand-authoring) until all 9 checks
   pass.
7. Hand this off to Paul for the manual content review (spec §4 gists vs.
   generated prose) — same human step v2's Phase 2 had. Paul freezes to
   `seed_v3.json.gz` using the same gzip-and-strip-notes one-liner as v2
   (update the path/version in `generate_seed_v3.py`'s docstring).
8. `python scripts/verify_seed_v3.py --frozen` — final confirmation.

### Not in this milestone (deferred, per spec §6/§7 and BUILD-PLAN.md convention)

- `probe_set_v3.yaml` — depends on the frozen fixture's actual content/hash.
- Prompt bank v3 regeneration, and updating `prompt_gen.py`'s
  `SEED_FIXTURE_VERSION`/`from generate_seed_v2 import ...` to point at v3.
- `config.yaml` `weight_mode` decision (`sample` vs `mean`) for the v3 run.
- Harness fixture-path switch for a real experiment run.
- Any prompt-side leakage-scan changes in `prompt_gen.py` consuming the new
  two-tier fingerprint export (spec §5 items 2–3 describe the export shape;
  `generate_seed_v3.py` produces `ENTRY_VOCAB`/`ANSWER_TOKENS` in that shape,
  but wiring `prompt_gen.py` to import and use them is separate work).

**Done when:** `generate_seed_v3.py` and `verify_seed_v3.py` exist and pass
their new pytest coverage; a real run against Ollama produces
`seed_v3_draft.json` (34 MUs, 64 edges) and
`seed_v3_generation_manifest.json`; the sabotage test (build step 5) proves
the manifest correctly records `mu_id`/missing-terms/leaked-terms/last-
attempted-text for an intentionally-failing MU; `verify_seed_v3.py --draft`
passes all 9 checks against the real (Paul-reviewed, manifest-repaired if
needed) draft; Paul freezes to `seed_v3.json.gz`; `verify_seed_v3.py
--frozen` passes against the committed artifact.

**Risks:**
⚠ Higher retry/failure rate than v2 is expected (spec §5 says so directly)
  — the manifest is the mitigation, not a sign something's wrong.
⚠ Hand-authored repairs (via the manifest workflow) bypass Gemma's "sounds
  natural" tendency — a human-written sentence stuffed with 4-5 required
  terms can read awkwardly. Not a correctness problem (spec §5: there's no
  research reason seed prose must be Gemma-written), but worth a light
  pass for readability during Paul's manual review.
? Whether the two-hit-bar probe-sanity check (9) needs tuning once real
  content is in — the check encodes a property that should hold by
  construction, but BM25 scoring on real generated prose (vs. the fixed
  fingerprint word lists) could have edge cases worth a WARN-not-FAIL
  treatment, same as v2's check 5 did for the "bridge not in K2" case.

**Estimate:** 1 session (generator + verifier + tests are mechanical
extensions of an existing, well-understood pattern; the live Ollama run and
Paul's content review are the only wall-clock-bound steps).

---

## Sequential Batch Queue Runner — Milestones 26–27

**Source:** Paul's planner document (2026-07-11) — design decisions there are
settled; this section records the decomposition, the four open-question
resolutions (with rationale), and one corrected mechanism detail found by
reading `runner.py` (see "Identity subtlety, corrected" below).

### Why this exists

Each full experiment run takes ~2+ hours. The screening series ahead
(replicates + one-factor sweeps) needs 10–15 runs, and the future Karpathy
loop needs unattended sequential execution as its substrate. The queue is a
**playlist player**: it takes an ordered list of run-plan files and feeds
them one at a time through the existing `execute_run()`, overnight, with
zero interaction after launch. It contains no intelligence — it never reads
results, never edits plans, never decides anything.

### The layer picture

```
┌────────────────────────────────────────────────────────────────────┐
│  QUEUE LAYER (new — M26/M27)                                       │
│                                                                    │
│  queues/screening_a.json          scripts/run_experiment_queue.py  │
│  { queue_id, plans[], ... } ────▶ (CLI: --queue, --dry-run,        │
│                                    --resume-dir)                   │
│                                        │                           │
│                                        ▼                           │
│                            scripts/harness/queue.py                │
│                            preflight → loop → checkpoint → summary │
│                                        │                           │
│      one plan at a time, in-process    │  RunOutcome (structured)  │
└────────────────────────────────────────┼───────────────────────────┘
                                         ▼
┌────────────────────────────────────────────────────────────────────┐
│  HARNESS LAYER (existing — unchanged behavior)                     │
│                                                                    │
│  execute_run(plan, client, ...)                                    │
│    per-condition container restart · wipe+seed · turn loop ·       │
│    per-turn checkpoint · judge · series/experiment reports         │
└────────────────────────────────────────┬───────────────────────────┘
                                         ▼
              Docker backend ── graph DB ── Ollama (gemma4:e4b)
```

The boundary matters for the Karpathy loop: everything above the line reads
plan files and writes status files; everything below owns experimental
semantics. The queue's only contract with the harness is `execute_run()`'s
existing signature and `RunOutcome` — no new parameters on the harness side
except what already exists (`resume_input_fn`).

### Open questions — resolved

1. **Queue file format: JSON.** Every execution-side artifact in this repo
   is JSON (plans, prompt banks, manifests, checkpoints); YAML is reserved
   for human-edited configuration (`config.yaml`, probe sets). The queue
   file is an execution artifact — and the Karpathy loop will machine-write
   it, where `json.dump` of a dict is the trivially-correct path. The
   `enabled: false` flag substitutes for YAML's comment-out idiom.

2. **Resume mechanism: auto-detect latest matching directory, guarded by a
   content hash; `--resume-dir` as explicit override.** On launch, the CLI
   scans `harness_runs/queues/` for directories matching `{queue_id}_*`
   whose checkpoint is incomplete. If the newest one's manifest records the
   same queue-content hash as the file being launched → resume it (logged
   loudly). If the hash differs → hard stop with a message naming both
   options (relaunch under a new directory, or pass `--resume-dir`
   explicitly) — never guess. Rationale: the operator relaunching the same
   command at 7 AM after an overnight crash is the primary use case; making
   them hunt a timestamped directory name is friction exactly when they're
   least awake, and the hash guard removes the ambiguity that would
   otherwise argue for explicit-only.

3. **Disk space: projected-archive-count log line + a stdlib free-space
   WARN, no hard stop.** `shutil.disk_usage` is one cross-platform call, so
   the actual check is nearly free; but predicting archive sizes accurately
   is guesswork, so it warns below a conservative module-level threshold
   constant rather than blocking a launch. The projected archive count
   (enabled plans × conditions) always appears in the launch log.

4. **In-process `execute_run()`, confirmed.** Read of `runner.py` found no
   isolation reason to subprocess: `execute_run()` returns a structured
   `RunOutcome`, never calls `sys.exit()`, and contains its own
   `HardStopError` unwinding (an aborted plan **returns**
   `status="aborted"`, it does not raise). Harness modules hold no run-scoped
   module-level mutable state. Backend-state isolation is already handled by
   the harness's own per-condition container restart. Subprocess would force
   the queue to parse stdout for what `RunOutcome` already provides
   structurally.

**Also decided — queue settings live in the queue file only, not
`config.yaml`.** `on_plan_failure`, `max_total_budget_usd`, etc. are
per-queue operational choices, not experimental knobs under study; putting
them in the Hyperparameter Registry would imply they're variables the
science sweeps over. `config.yaml` stays untouched by this work.

### Queue file schema (`queues/*.json`)

```json
{
  "queue_id": "screening_a",
  "on_plan_failure": "continue",
  "max_total_budget_usd": 25.0,
  "notification": { "enabled": true, "channel": "log" },
  "plans": [
    { "plan_path": "plans/replicate_seed1.json" },
    { "plan_path": "plans/replicate_seed2.json", "enabled": true },
    { "plan_path": "plans/sweep_k1_4.json",      "enabled": false }
  ]
}
```

- `plan_path` resolves **relative to the queue file's own directory** when
  not absolute (machine-writer friendly: the Karpathy loop writes both the
  plan and the queue entry without knowing the invoker's CWD).
- `enabled` defaults `true`; disabled entries are recorded in the manifest
  as `skipped_disabled` but never validated or executed.
- `on_plan_failure`: `"continue"` (default) | `"halt"`.
- `notification` reuses the existing `NotificationConfig` schema verbatim.
- Validation errors go through the `format_validation_error()` pattern —
  field-identifying, never a stack trace.

### Directory layout & state files

```
harness_runs/
├── queues/
│   └── screening_a_20260712T0300Z/        ← {queue_id}_{launch_ts}
│       ├── queue_manifest.json            ← resolved queue: plan list, per-plan
│       │                                     config_hash + expected run_id,
│       │                                     queue-content hash, preflight results
│       ├── queue_checkpoint.json          ← per-plan status + timings (atomic
│       │                                     temp-file + os.replace, same pattern
│       │                                     as checkpoint.py)
│       ├── queue_summary.md               ← written at queue end (M27)
│       └── notifications.log              ← notify.py reuse: base_dir=queues/,
│                                             run_id=the queue dir name
└── {plan_id}_{config_hash[:12]}/          ← harness run dirs, exactly as today
```

Per-plan checkpoint entry: `status` (`pending | running | complete | failed
| skipped`), `plan_path`, `config_hash`, harness `run_id`, start/end
timestamps, wall-clock duration, per-condition durations, and the harness's
`error_summary` on failure. **Per-condition durations are derived from the
harness checkpoint's own per-turn timestamps after the plan finishes**
(min/max `TurnCheckpoint.timestamp` per condition) — the queue never
observes condition boundaries live, so this needs zero `runner.py` changes
and respects the layer boundary. Durations are not decoration: they feed
future "how many runs fit in 10 hours" scheduling.

### Non-interactive execution — the four touchpoints

| Harness touchpoint | Queue-mode resolution |
|---|---|
| `elicit_plan()` | Never invoked — queue entries are files; a missing file is a preflight hard stop. |
| Resume prompt (`resume_input_fn`) | Queue passes `resume_input_fn=lambda _: "y"` — always resume, never restart-from-scratch (an unattended process must never silently discard partial experimental data). Existing injection point; resume logic is not forked. |
| Sonnet bank cost gate | Must never fire mid-queue. Preflight resolves every plan's **effective bank**: explicit `prompt_bank_path` → file must exist and pass `load_bank()`; else `subjects == "auto"` → `estimate_bank_cost_for_plan()` returning `None` means a cached identical-hash bank exists (resolve its path via the same hash and stamp it onto the in-memory plan); returning non-`None` means fresh Sonnet generation would be needed → **hard stop** telling the user to run `--generate-bank` for those plans interactively first. A plan with neither bank nor `auto` also hard-stops: placeholder-message turns in an unattended overnight run are wasted GPU-hours, never intended. The queue makes zero Anthropic API calls. |
| `--interactive` judge override | The queue CLI simply does not expose the flag — structurally impossible, nothing to reject. |

### Preflight protocol (M26 — all pass before any graph mutation)

```
--queue file
     │
 1.  ▼ queue file parses + validates (field-identifying errors)
 2.  ▼ EVERY enabled plan loads + validates via RunPlan
     │   (a malformed plan #7 fails at launch, not six hours in)
 3.  ▼ duplicate config_hash detection across enabled plans → hard stop
     │   ("replicates need distinct random_seed values" — see below)
 4.  ▼ per-plan effective-bank resolution (table above) → hard stop if
     │   any plan would need fresh Sonnet generation
 5.  ▼ backend health (client.health()) + judge model availability
     │   (model_available() per distinct judge model across plans)
 6.  ▼ aggregate budget: Σ estimate_prerun_cost(0, n_judge_calls) per plan
     │   ≤ max_total_budget_usd (banks pre-exist → bank cost is 0)
 7.  ▼ disk-space note: projected archive count (plans × conditions) logged;
     │   shutil.disk_usage free-space WARN below threshold
     ▼
 launch (M27) — or, with --dry-run: print resolved execution order and exit 0
```

Honest budget note (per the source doc): with pre-generated banks and a
local judge, marginal per-run spend is ~$0 today. The aggregate cap is a
guardrail for future configurations (e.g., an API judge), not because
current runs cost money.

**Identity subtlety, corrected.** The source doc predicted a byte-identical
duplicate plan would "resume-as-complete instead of re-running." Reading
`runner.py` (the attempt-resolution loop): a **completed** checkpoint
actually bumps to `_attempt2` and re-runs under a suffixed identity. The
consequence differs but is still silently wrong for the replicate tranche —
an identical plan re-run is a same-`random_seed` pseudo-replicate with an
identical prompt schedule, indistinguishable in intent from a real
replicate but scientifically not one. The preflight duplicate-hash hard
stop therefore stands unchanged, with the error message the doc specified:
duplicate entries share `config_hash` → "replicates need distinct
random_seed values."

### Execution loop, failure semantics & resume (M27)

```
for each enabled plan (in order):
  queue checkpoint: plan → running          (atomic write BEFORE execute_run)
        │
        ▼
  outcome = execute_run(plan, client, RUNS_DIR, probes, judge_client,
                        docker_project_root, resume_input_fn=always_yes)
        │
        ├─ outcome.status == "complete" ──▶ plan → complete (+ durations,
        │                                   report paths from outcome.checkpoint)
        ├─ outcome.status == "aborted"  ──▶ plan → failed (+ error_summary)
        │       │
        │       ├─ on_plan_failure == "halt" ──▶ remaining plans → skipped, stop
        │       └─ "continue" ──▶ re-run health checks (backend + judge);
        │                         health FAILS → halt regardless of policy
        │                         (a dead backend converts 1 failure into N)
        └─ unexpected exception (queue bug) ──▶ plan → failed, checkpoint
                written, notification sent, exit nonzero — NEVER swallow an
                unknown-state error and continue
```

Between plans the queue does **no graph handling** — the harness already
wipes+seeds per condition; a queue-level wipe would double-archive and
violate the layer boundary.

**Resume state machine** (re-invocation with the same queue file, or
`--resume-dir`):

- `complete` → skip (never calls `execute_run`, so no attempt-bump).
- `pending` → run normally.
- `running` (crashed mid-flight) → **reconcile against the harness
  checkpoint first**: if the harness checkpoint for that run identity shows
  `final_status == "complete"`, the crash landed in the window between
  harness completion and the queue's own checkpoint write — mark `complete`
  and skip (re-entering `execute_run` would attempt-bump and silently
  re-run ~2 hours). If it shows `"aborted"`, mark `failed` and apply
  `on_plan_failure`. Otherwise re-enter `execute_run()`, where
  `resume_input_fn=always_yes` hands turn-level recovery to the harness's
  existing checkpoint resume. To locate "that run identity" without forking
  the attempt-resolution logic, extract `runner.py`'s attempt-probe loop
  into a small shared helper (e.g. `resolve_run_attempt(plan, config_hash,
  run_base_dir)`) used by both `execute_run()` and the queue — a minimal,
  behavior-preserving refactor honoring the "do not fork the resume logic"
  constraint.
- `failed` / `skipped` → left as-is; a fresh queue launch (new directory)
  is the way to retry a failed tranche.

**Notifications** (through `notify.py` as-is — no new channels):
`queue_started` (plan count, projected archive count), `queue_plan_complete`
(per plan: status, duration, report paths), `queue_complete` (final tally),
`queue_aborted` (unexpected exception or health-check halt). Exit code: 0
only when every enabled plan is `complete`; nonzero otherwise.

**`queue_summary.md`** (written at queue end, whatever the outcome): a
table of plan_id, status, duration, harness run_id, and paths to the
series/experiment reports the harness generated — pulled from each plan's
`RunOutcome.checkpoint` records, no metrics beyond what those reports
already contain (no result interpretation — that's the layer boundary the
Karpathy loop depends on).

### Non-goals (rationale recorded per the source doc)

- **No result interpretation** — analysis lives downstream (sensitivity doc
  now, Karpathy loop later); building it here would duplicate
  `series_report.py`/experiment reports and blur the layer boundary.
- **No parallelism** — one GPU, one backend container, one graph DB;
  sequential is a hardware fact, not a limitation to engineer around.
- **No plan generation/mutation** — a wrong plan fails preflight and a
  human fixes it.
- **No dynamic queue changes mid-run** — the queue file is read once at
  launch; if the Karpathy loop needs append-while-running, it gets designed
  then against the loop's actual needs.
- **No new notification channels** — `notify.py` as-is (email/webhook still
  fall back to log with a warning).
- **Forward-compatibility is design-only:** the Karpathy loop will emit
  run-plan files in the exact hand-written format and append queue entries;
  the JSON schema and relative-path resolution above are what make that
  trivially machine-writable. No directory watching, no agent integration.

---

### Milestone 26: Queue Schema + Preflight + Dry-Run

**Branch:** `claude-queue-preflight`

**Build:**
1. `scripts/harness/queue.py` — queue file schema (pydantic, mirroring
   `schema.py` conventions incl. `format_validation_error` reuse), queue
   manifest + checkpoint models with atomic `save_*` writers
   (temp-file + `os.replace`, same pattern as `checkpoint.py`), and the
   full preflight protocol (steps 1–7 above) as pure, injectable functions
   (fake `BackendClient` / fake judge client / fake `disk_usage_fn` — the
   existing test pattern).
2. `scripts/run_experiment_queue.py` — CLI: `--queue <file>`, `--dry-run`,
   `--backend-url`, `--judge-host`, `--resume-dir` (parsed now, used in
   M27). `--dry-run` runs full preflight, prints the resolved execution
   order (per plan: plan_id, config_hash, expected run_id, bank path,
   condition list), and exits 0 — zero HTTP mutation calls, zero graph
   mutation (health checks are reads and DO run in dry-run; that's the
   point of preflight). Without `--dry-run`, M26's CLI stops after
   preflight with an explicit "execution loop lands in M27" message —
   the milestone ships a working preflight validator, not a stub.
3. Example queue file `queues/example_queue.json` referencing the committed
   verification plans/banks (`plans/phase3_pilot.json` et al.), committed
   as living documentation of the schema.

**Tests** (`tests/test_harness/test_queue.py`): queue-file validation
(missing/duplicate fields, bad `on_plan_failure` value, empty plans list);
missing plan file; invalid plan (schema error identifies which plan);
duplicate config-hash hard stop (error message names the entries and says
"replicates need distinct random_seed values"); bank resolution — explicit
path missing, explicit path valid, cached-hash resolution stamps the plan,
fresh-generation-needed hard stop, neither-bank-nor-auto hard stop;
aggregate budget breach; health-check failure; relative plan_path
resolution; manifest/checkpoint atomic-write round-trip.

**Verification protocol (recorded output required):** items 1–2 from the
source doc — (a) 3-plan `--dry-run` with full preflight output, resolved
order, zero mutation; (b) three preflight hard-stops (missing plan file,
fresh-bank-needed, duplicate config hash), each exiting nonzero with a
plan-identifying message before any backend mutation call.

**Done when:** dry-run and all preflight hard-stops behave per the
verification protocol with recorded output; full test suite passes, no
regressions; example queue file committed.

**Risks:**
⚠ Bank-hash resolution reuses `prompt_gen` internals
  (`_generation_params` is private) — may need a small public helper in
  `prompt_gen.py` rather than importing a private; keep it a one-liner.
? Whether `wait_for_health()`'s 90s default is right for a cold launch
  check vs. a single immediate `client.health()` — decide at build time;
  preflight wants "is it up now", not "wait for it to come up".

**Estimate:** 1 session.

---

### Milestone 27: Queue Execution Loop + Resume + Summary

**Branch:** `claude-queue-execution`

**Depends on:** Milestone 26.

**Build:**
1. Extract `runner.py`'s attempt-resolution loop into a shared helper
   (`resolve_run_attempt()` or similar) — behavior-preserving refactor,
   existing runner tests must pass unchanged.
2. Orchestration loop in `queue.py` (or a sibling `queue_runner.py` if the
   module crosses the ~300-line smell threshold — /build's judgment call):
   the execution loop, failure semantics, health recheck after a failed
   plan, and the running→complete/failed transitions with atomic
   checkpoint writes at every transition (crash between any two writes
   loses at most the in-flight plan's status, which resume reconciliation
   recovers).
3. Resume: auto-detect (queue-content-hash-guarded) + `--resume-dir`
   override + the `running`-state reconciliation against the harness
   checkpoint.
4. Per-plan and per-condition duration capture (harness checkpoint
   timestamp derivation), `queue_summary.md` writer, notification events,
   exit codes.
5. Wire the CLI's non-dry-run path to the loop (replacing M26's stop
   message).

**Tests** (extending `test_queue.py`, fake `execute_run` injected):
`on_plan_failure` both modes (continue → next plan runs; halt → remaining
marked skipped); health-recheck halt after a failed plan overrides
`continue`; crash-resume — completed plans skipped, in-flight plan
re-entered, `running`-but-harness-complete reconciled to complete without
re-running; queue-level unexpected exception marks in-flight plan failed,
writes checkpoint, exits nonzero; duration fields populated from fake
harness checkpoints; summary content (statuses, run_ids, report paths);
checkpoint atomicity (interrupted write leaves the previous valid file).

**Verification protocol (recorded output required):** items 3–6 —
(a) live micro-queue: 2 tiny plans (1 series × 2–3 turns, 1–2 conditions,
existing committed bank) against real Docker + real `gemma4:e4b`, both
`complete` with durations, `queue_summary.md` correct; (b) crash-resume:
kill mid-plan-1, re-invoke, plan 1 resumes at the correct turn via the
harness checkpoint, plan 2 runs, no duplicate seeding beyond the harness's
own wipe+seed; (c) failure-continue: plan 1 forced to fail (unreachable
judge host per its error policy) with `continue` → plan 1 `failed`, health
recheck passes, plan 2 `complete`; (d) full test suite, no regressions.

**Done when:** all four verification items recorded; `queue_summary.md`
and notifications match the spec above; full suite green.

**Risks:**
⚠ The live micro-queue needs Docker running (recent sessions ran uvicorn
  directly because the daemon was down) — the harness's per-condition
  restart is Docker-compose-based, so M27's live verification genuinely
  requires the Docker path. Plan the session accordingly.
⚠ Crash-resume verification (killing a live process mid-plan on Windows)
  can be finicky — `taskkill` the python process, not the terminal, so
  the backend container stays up and resume exercises only queue+harness
  recovery.
? Whether `queue_plan_complete` notifications should also fire for
  `failed` plans or a distinct `queue_plan_failed` event — decide at build
  time; keep the payload shape shared either way.

**Estimate:** 1–2 sessions (the live verification runs are wall-clock-bound).

---

## Seed Fixture v3 — Harness Wiring — Milestone 28

**Source:** `seed_v3_mu_spec.md` §6 "Downstream impacts (hash cascade)" — the
deferred work flagged since Milestone 25. `seed_v3.json.gz` is frozen (Paul's
review + freeze already done; the file exists on disk, just not yet
committed) but every harness-side consumer still hardcodes `v2`. This
milestone is pure plumbing — make `v3` selectable end-to-end so the first
full-length experiment can run against it. It deliberately does **not**
decide anything about how that experiment is designed (which spoke gets
trained, `weight_mode`, turn counts, probe ratio, budget) — those are
`RunPlan`/`config.yaml` choices for whoever writes the actual experiment
plan, out of scope here.

### Why this exists

`POST /admin/seed-graph` already accepts `{"version": "v3"}` with zero
backend changes (confirmed during M25 planning). Everything hardcoded to
`v2` is harness-side: `runner.py`'s `client.seed_graph(version="v2")` call,
`run_experiment_harness.py`/`run_experiment_queue.py`'s module-level
`SEED_FIXTURE_PATH` constant, and `prompt_gen.py`'s
`from generate_seed_v2 import ...` + single-tier `CLUSTER_*_FP` fingerprint
scan (v3 needs a two-tier scan — entry vocabulary leaking from *other*
clusters, answer tokens leaking from *any* cluster including its own, per
spec §5). Without this, a plan can request `seed_v3` at the backend layer
but the harness's own bank-generation and leak-checking machinery is still
silently validating against v2's vocabulary.

### Scope

1. **`schema.py`:** new `RunPlan.seed_fixture_version: str = "v2"` field —
   same pattern as the existing `probe_set_version` (free string, resolved
   at runtime, no enum validator; a bad value fails loudly when the fixture
   file or generator module can't be found, not at schema-validation time).

2. **`runner.py`:** `client.seed_graph(version=plan.seed_fixture_version)`
   replaces the hardcoded `"v2"`.

3. **`run_experiment_harness.py` / `run_experiment_queue.py`:** the
   module-level `SEED_FIXTURE_PATH` constant goes away — fixture path is
   resolved per plan (`seed_fixtures/seed_{plan.seed_fixture_version}.json.gz`).
   `queue.py`'s `run_preflight()` currently takes one shared `fixture_path`
   for every plan in the queue; since different plans in one queue could
   reference different fixture versions, this becomes a per-plan resolution
   inside `_resolve_bank_for_plan()` (takes a fixtures *directory*, not a
   single file path).

4. **`prompt_gen.py`:** version-dispatched fixture module loading
   (`generate_seed_v2` vs `generate_seed_v3`) instead of one hardcoded
   import. `fingerprint_scan()` becomes version-aware:
   - v2 (unchanged behavior): leak = another cluster's `CLUSTER_*_FP` term.
   - v3: leak = another cluster's entry vocabulary or family name (`ENTRY_VOCAB`/
     `FAMILY_NAMES`), **or** any cluster's answer token including the
     prompt's own target cluster (`ANSWER_TOKENS`) — an answer token in a
     bulk prompt would let BM25 reach the gold spoke directly and destroy
     the headroom the two-tier design exists to create.
   - `MU_IDS`/hub-spoke text lookup dispatches by version too (v3's `MU_IDS`
     reuses v2's UUIDs verbatim, so this is a low-risk swap, not a
     renumbering).
   - `seed_fixture_version` threaded from `plan.seed_fixture_version` into
     each `PromptBankEntry` instead of the hardcoded module constant.
   - **Designated trained spoke stays spoke1 per cluster** — matches
     `read_cluster_content()`'s existing hardcoded `f"{letter}_spoke1"`
     lookup exactly as-is; per spec §6 this must be exactly one spoke per
     cluster (K2's `top_k=1` is winner-take-all), and the other six become
     the negative-control spokes probe_set_v3.yaml measures.
   - Add a v3-specific worked example to `_build_generation_prompt()`
     nudging Sonnet toward the spec's criteria template
     (`["names rule <ID>", "states <value token>"]`) — a two-item
     binary-checkable checklist, judge-gradeable exactly like v2's.

5. **`probes/probe_set_v3.yaml`:** one probe per spoke per cluster (28 total
   — 7 spokes × 4 clusters), single new `phenomenon_type: "spoke_retrieval"`
   (`ProbeDefinition.phenomenon_type` is an unvalidated string — no schema
   change needed for a new value). Gold is the spoke MU. Prompt text
   front-loads only that cluster's entry vocabulary (`ENTRY_VOCAB`), never
   the probed spoke's own anchor word (placement rule 6) and never any
   answer token. Probing all 7 spokes per cluster — not just the one that
   ends up trained — is deliberate: per spec §6 the six untrained spokes
   are negative controls (their probe trajectories should stay flat; that's
   discriminative-learning evidence, not noise). Which spoke is "trained"
   is decided later, by the actual RunPlan's prompt bank — the probe set
   itself doesn't encode that.

6. **`config.yaml`:** add `reporting.pass_rank_thresholds.spoke_retrieval: 4`
   — same structural reasoning as the existing `cross_cluster_transfer: 4`
   (K2 traversal position; a purely-BM25 hub hit can never reach a spoke at
   rank 1). Zero report-generator code changes needed — confirmed by
   reading `experiment_report.py`: thresholds are looked up purely by
   `phenomenon_type` key, `reporting_cfg.get("pass_rank_thresholds", {})`.

7. **Commit `seed_fixtures/seed_v3.json.gz`** (currently untracked, already
   frozen) as part of this branch — every other piece of this milestone is
   dead code without it.

### Required verification (live, real data — not just unit tests)

Every v3 probe must pass a baseline check on a freshly wiped-and-reseeded
`v3` graph **before** the probe set is considered frozen: run each probe via
the real retrieval path and confirm the gold spoke is not trivially
surfaced pre-training (spec §6's explicit pre-freeze requirement). Record
the actual observed `gold_mu_rank` distribution at baseline — the exact
numeric behavior (None vs. structural rank 4 for whichever spoke wins the
K2 tie-break among 7 equal-weight edges) isn't fully predictable from the
spec alone and should be established empirically, the same way M25 recorded
real generator output rather than assuming it. Also: generate a small v3
prompt bank via `--generate-bank` against a real Sonnet call to confirm the
two-tier fingerprint scan actually rejects an answer-token leak (mirrors
M25's "sabotage proof" pattern — deliberately construct one leaking
candidate prompt and confirm it's caught, not just that clean prompts pass).

### Explicitly out of scope

`weight_mode` (`sample` vs `mean`), which spoke is trained, probe/turn
counts, `budget_cap` — all real-experiment-design decisions that belong in
the actual `RunPlan` JSON + `config.yaml` values chosen when the first v3
experiment is planned, not in this wiring milestone.

**Estimate:** 1 session.

### Completion

Complete — all 7 scope items built exactly as planned: `RunPlan.seed_fixture_version`
(`schema.py`), `runner.py`'s `seed_graph(version=plan.seed_fixture_version)`,
per-plan fixture-path resolution replacing the hardcoded `SEED_FIXTURE_PATH`
constant in both `run_experiment_harness.py` and `run_experiment_queue.py`/
`queue.py` (`_resolve_bank_for_plan` now takes a `fixtures_dir` and resolves
`seed_{version}.json.gz` per plan, with a field-identifying `QueuePreflightError`
— not a raw `FileNotFoundError` — when a plan's `seed_fixture_version` has no
fixture on disk), `prompt_gen.py`'s version-dispatched two-tier fingerprint
scan (`fingerprint_scan(..., version="v3")`: other-cluster entry vocabulary/
family-name leak, plus any-cluster answer-token leak including the prompt's
own target cluster — the fresh tier v2 never had), `probes/probe_set_v3.yaml`
(28 `spoke_retrieval` probes, one per spoke per cluster), and
`config.yaml`'s `reporting.pass_rank_thresholds.spoke_retrieval: 4`.
`seed_fixtures/seed_v3.json.gz` (frozen in a prior session, previously
untracked) is now committed. 30 new tests, 727 total, no regressions.

**Live verification (real Docker backend + the real frozen `seed_v3.json.gz`,
Docker was up this session) — two real authoring bugs caught and fixed by
actually running the probe battery, not just reading the spec:**

1. **First draft: every `cluster_d` probe returned zero candidates.**
   `retrieve()` only takes the first `hyperparameters.retrieval_keyword_count`
   (5) non-stopword tokens of the query as BM25 keywords (`backend/memory/
   retrieval.py`) — the first-draft templates put the 3 picked entry-vocab
   words at the *end* of the sentence, past position 5, so they were never
   even considered as keywords. Fixed by front-loading them as the first
   3 tokens of every prompt. This is the same "front-load exact vocabulary"
   lesson the 2026-07-05 pilot bank already recorded — now it's cost a
   second discovery on the probe side too.
2. **Second draft: `vs_a2`'s gold surfaced directly at baseline (`gold_mu_rank=2`),
   not via traversal.** The first fix excluded only each spoke's documented
   "anchor" word from its own probe's word pool — but content gists are
   free-form prose (spec §4 says "to taste"), and several spokes' real
   generated content uses *more* entry-vocab words than just their assigned
   anchor (e.g. `a_spoke2`'s frozen content contains "field" despite its
   anchor being "delimiter"). Fixed by excluding, per spoke, every
   entry-vocab word that actually appears (whole-word) in that spoke's real
   frozen content from `seed_v3.json.gz` — not just its anchor label.

**Final baseline check (fresh wipe + `seed_v3` reseed, zero training turns,
all 28 probes run for real against the live backend):** 26/28 show
`gold_mu_rank=None` (gold not surfaced at all). The remaining 2
(`vs_b1`→rank 4, `vs_d1`→rank 2) are not authoring defects — inspecting the
full retrieval trace confirmed both reach their gold purely via K2 hub→spoke
traversal (never a direct lexical hit), using only generic, per-spoke-content-
excluded vocabulary. This is the "seven/eight-way tie" property itself: every
hub starts with 7 spoke edges *and* 1 hub↔bridge edge, all identical
`Beta(1,1)`/weight 0.5 — something always wins that tie deterministically
under `weight_mode: mean`. For `cluster_a` and `cluster_c` the winner happens
to be the bridge (`bridge_ab`/`bridge_cd`), so all 14 of their spoke probes
read clean at baseline; for `cluster_b` and `cluster_d` the winner happens to
be `b_spoke1`/`d_spoke1` specifically. `_classify_probe_outcome()` uses
`rank <= threshold` (confirmed by reading `experiment_report.py`), so this
doesn't corrupt the pass/fail mechanism — but it **is** a real, useful
finding for whoever designs the first real experiment: **`b_spoke1` and
`d_spoke1` already read as "passing" at session 0 under the frozen fixture's
default tie-break — picking a different spoke as the intentionally-trained
target in each of those two clusters will show a cleaner fail→pass
transition than picking the one that already wins by default.** (This is
purely a property of today's frozen fixture + `weight_mode: mean`'s
deterministic tie-break; `weight_mode: sample` would make it non-deterministic
per query instead — one more reason that decision belongs to the actual
experiment plan, not this milestone.)

**Not done (explicitly out of scope, unchanged):** the actual v3 prompt bank
— generating it requires a real, cost-incurring Sonnet call, which Paul
initiates himself (`--generate-bank`) rather than the harness/agent
triggering paid API spend unprompted.

---

## Per-Cluster Trained-Spoke Selection — Milestone 29

**Source:** M28's live baseline check (see M28's "Completion" section above)
found that `b_spoke1`/`d_spoke1` already read as a baseline PASS
(`gold_mu_rank` 4 and 2 respectively) under the frozen `seed_v3.json.gz` +
`weight_mode: mean`'s deterministic 7-way tie-break — a property of the
seed fixture itself, not a defect. `build_bank()`/`read_cluster_content()`
hardcode `f"{letter}_spoke1"` as the trained target in every cluster (a v2
convention carried into v3 unchanged), so half of a 4-cluster run
(`cluster_b`, `cluster_d`) would start the "learned" condition from a graph
with zero retrieval headroom on its trained spoke — no fail→pass transition
is measurable there, which guts the run's headline observable. Hand-tuning
those two clusters' edge weights to force a baseline fail was considered and
rejected: the seven-identically-weighted-edges baseline *is* the control
condition, and hand-nudging it to produce the desired result is exactly the
kind of initialization confound the "learning did this, not the graph you
built" claim can't survive. The correct fix is to make the trained spoke a
per-cluster `RunPlan` choice, selected by baseline headroom (`gold_mu_rank`
is `None` or clearly past the `spoke_retrieval` pass threshold) before any
paid generation happens — headroom is a design requirement here, not a
result being cherry-picked, as long as the same spokes are then used
identically across all three conditions (already guaranteed for free: one
`RunPlan` produces one prompt bank shared by `learned`/`static_weight`/
`random_weight`).

### Why this exists

`read_cluster_content()`/`build_bank()` currently *decide* which spoke gets
trained (always spoke1) instead of *reading* that decision from the plan.
That's the same class of hardcoded-value problem `seed_fixture_version`
(M28) and `probe_set_version` (M21) already solved for fixture/probe-battery
selection — this milestone applies the identical pattern one level down, to
spoke selection within a fixture. Scope is contained to `schema.py` and
`prompt_gen.py`: `read_cluster_content()` is called only from
`estimate_bank_cost_for_plan()` and `build_bank()`, both in `prompt_gen.py`
— `runner.py`, `queue.py`, and both CLI entry points need no changes.

### Scope

1. **`schema.py`:** new `RunPlan.trained_spokes: dict[str, str]` field,
   default `{"cluster_a": "a_spoke1", "cluster_b": "b_spoke1", "cluster_c":
   "c_spoke1", "cluster_d": "d_spoke1"}` — preserves today's exact hardcoded
   behavior for every plan that doesn't set this field (no silent behavior
   change for existing v2 plans or anyone who hasn't read this milestone).
   A `field_validator` checks *shape* only (keys are exactly the 4 known
   cluster ids, values match `^[abcd]_spoke[1-7]$`) — it cannot check a
   value actually exists in a given fixture version's `MU_IDS`, because
   `schema.py` must not import `generate_seed_v2`/`generate_seed_v3` or
   `prompt_gen.py` (prompt_gen.py already imports `RunPlan` from
   `schema.py` — importing back would be circular). Semantic validation
   (does this key resolve to real content) happens at runtime in
   `read_cluster_content()`, same convention `seed_fixture_version`'s own
   doc comment already states: "a bad value fails loudly... not at
   schema-validation time."

2. **`prompt_gen.py` — `read_cluster_content()`:** add a `trained_spokes:
   dict[str, str]` parameter. Replace the hardcoded `mu_ids.get(f"{letter}
   _spoke1")` with `mu_ids.get(trained_spokes[cluster_id])`. **Raise a
   named `ValueError` (fixture version + cluster_id + requested spoke key)
   if the resolved `spoke_id` is `None` or if no MU in `mus_in_cluster`
   matches it** — today this fails silently via `next(..., "")`, returning
   an empty `spoke_text` that would quietly generate spoke-less prompts.
   This is a real gap the new per-cluster knob makes worth closing now: a
   typo'd spoke key (`"b_spoke9"`, or a v2-only key against a v3 fixture)
   must stop the run, not silently degrade prompt quality.

3. **`prompt_gen.py` — `build_bank()` / `estimate_bank_cost_for_plan()`:**
   thread `plan.trained_spokes` into both `read_cluster_content()` calls.
   No other logic in either function changes — `targets[]` construction,
   Sonnet generation, and validation are all spoke-agnostic already.

4. **`prompt_gen.py` — `generation_params_for_plan()`:** add
   `"trained_spokes": plan.trained_spokes` to the returned dict. **Required,
   not cosmetic** — this dict feeds `compute_bank_hash()`, and the fixture
   file's bytes (already hashed separately via `fixture_manifest_hash`)
   don't change when the trained-spoke choice changes. Without this, two
   plans differing only in `trained_spokes` would produce the same
   `content_hash` and silently reuse each other's bank — `build_bank()`'s
   own hash-reuse short-circuit (`if existing_path.exists(): return
   load_bank(...)`) would serve a `b_spoke3`-trained bank to a plan that
   asked for `b_spoke5`, with no error.

5. **`docs/experiment_protocol.md`:** add `trained_spokes` to the RunPlan
   field reference table once Paul's in-flight rewrite of that file is
   committed (out of scope for this milestone's build — do not edit a file
   with uncommitted changes from another session).

### Required verification

Unit tests only for this milestone — no live Docker/Sonnet run needed,
since this is a pure config-threading change with no new retrieval or
generation behavior:
- `test_schema.py`: default value, explicit override, shape-validator
  rejects a missing/extra cluster key and a malformed spoke key
  (`"b_spoke9"`, `"cluster_e"`, `"spoke1"`).
- `test_prompt_gen.py`: `read_cluster_content()` returns the requested
  spoke's content when `trained_spokes` picks a non-spoke1 key; raises the
  named `ValueError` on an unresolvable key; `build_bank()`/
  `estimate_bank_cost_for_plan()` thread the plan's `trained_spokes`
  through unchanged; `generation_params_for_plan()` includes
  `trained_spokes` in its output; two plans differing only in
  `trained_spokes` produce different `content_hash` values.

### Explicitly out of scope

Picking the actual spoke values for the first real v3 `RunPlan` — that
requires re-running the 28-probe baseline check once or twice to confirm
the M28 tie-break is stable across a fresh wipe+reseed (not just the one
snapshot M28 recorded), and ideally under each condition's actual retrieval
mode (`mean` vs whatever `weight_mode` the plan ends up choosing for
`learned`), not just `weight_mode: mean`. That's real-experiment-design
work for whoever writes the plan, done *after* this knob exists and *before*
`--generate-bank` is run against it. Also out of scope: `weight_mode`
itself, turn/probe counts, `budget_cap` — unchanged from M28's own
out-of-scope list.

**Estimate:** well under 1 session — a contained, mechanical config-threading
change plus tests, no new runtime behavior to verify live.

---

## Dual-Mode Probe Battery — Milestone 30

**Source:** the 2026-07-15 smoketest finding (PR #51,
`docs/m28_tiebreak_learned_mode_findings_2026-07-15.md`) that M28/M29's
"pick a spoke that deterministically fails" premise is `weight_mode:
mean`-specific and doesn't hold under `weight_mode: sample` — every probe
reads a noisy 0–36% pass rate there, not a clean split. That finding left
`trained_spokes` selection blocked on a methodology decision: either chase
a larger N to rule out noise, or stop trying to pre-select a "clean-fail"
spoke and measure pass-*rate* directly instead. This milestone builds the
instrumentation for the second path — it does not make the call itself.

This milestone has two parts, built in two separate sessions without a
BUILD-PLAN entry at the time (a process gap this section exists to close —
see the Part A note below).

### Part A — Harness + backend dual-mode dispatch (ALREADY BUILT)

Built 2026-07-15, commit `aea3a24` on `fix/smoketest-2026-07-15` —
**code-complete and tested, but never written up here or in PROGRESS.md,
and still unpushed** (local-only, along with docs commit `1f39c23`). Recorded
retroactively:

- At every probe checkpoint, the harness now issues **1 deterministic
  `weight_mode="mean"` battery pass + K independent `weight_mode="sample"`
  repeats** (K = `probe_measurement.sample_battery_repeats`, default 25,
  new config.yaml Hyperparameter Registry entry, host-read by
  `runner.py` — no backend restart needed to change it), identically in
  all three conditions.
- `retrieve()` gains `weight_mode_override`, scoped to the single
  `POST /admin/run-probe` call — real training turns never pass it, so
  training-turn behavior is completely unaffected.
- `RunProbeRequest`/`ProbeResult`/`probe_results` gain `series_id`,
  `battery_mode` (`"mean"|"sample"`), `battery_repeat_index`.
- **Real bug fixed:** `admin.py`'s `run_probe()` was passing the
  orchestrator's process-scoped `EDGE_RNG` into `retrieve()` — the same RNG
  stream real training turns use for `weight_mode: sample`. A probe
  checkpoint run under `sample` mode was silently perturbing the
  reproducibility of subsequent training turns' sample draws. Fixed by no
  longer passing `rng=` from `run_probe()` — matches `retrieval.py`'s own
  `_DEFAULT_RNG` docstring, which already documented probe callers as the
  intended user of the unseeded fallback.
- 25 new tests (785 total, all passing): dispatch/error-handling/config
  default (`test_runner.py`), `ProbeResult` new fields
  (`test_probes.py`), the admin.py endpoint incl. the RNG fix
  (`test_run_probe_dual_mode.py`), `weight_mode_override`
  (`test_retrieval.py`), `series_id`/`battery_mode` filtering + migration
  (`test_store.py`).

### Part B — §6.1 report consumption (NEW SCOPE — this is what /build builds)

Part A stores the dual-mode data correctly but **nothing reads it back
correctly**: `experiment_report.py`'s §6.1 section (`_section_61_learning_measures`)
calls `store.query_probe_results(probe_id=pid, experiment_id=experiment_id)`
with no `battery_mode` filter, and both the mean pass and all K sample
repeats share the same `measurement_session_index`. The moment a real
dual-mode battery runs through a real experiment, every §6.1 subsection that
assumes one result per probe per checkpoint — `state_transitions`
(fail→pass/pass→fail detection), `bridge_mu_analysis` (rank trajectory,
transfer-candidate detection), `probe_pass_rates`, `mus_by_probe_outcome`,
`cross_series_probe_mus` — receives 26 rows instead of 1 and silently
corrupts. None of Part A's 25 tests touch `experiment_report.py`, so this
wasn't caught.

1. **`backend/analytics/reports/experiment_report.py` — `_section_61_learning_measures()`:**
   add `battery_mode="mean"` to both `query_probe_results()` calls (the
   `experiment_id`-scoped call and the `condition_label`-fallback call).
   Behavior-preserving for every experiment ever recorded — pre-M30 rows
   already default to `battery_mode='mean'` at the schema level, so this
   changes nothing for existing data and fixes the corruption for future
   dual-mode data. This is the regression fix; give it its own test (below).

2. **Same file — new `_compute_sample_pass_rates()`:** parallel in shape to
   the existing `_compute_state_transitions()`. Fetch `battery_mode="sample"`
   rows per probe the same way the mean rows are fetched (mirror the
   `experiment_id`-first, `condition_label`-fallback pattern for symmetry).
   Group by `measurement_session_index`; classify each row via the existing
   `_classify_probe_outcome()` (reuse, don't reimplement); per session
   compute `pass_count`, `fail_count` (excluding `negative_flagged` from the
   denominator — same convention `probe_pass_rates` already uses),
   `repeats_completed = len(rows)`, `pass_rate = pass_count / non_flagged_count`
   (or `None` if zero), and `partial_battery = repeats_completed < configured_repeats`
   (a soft-continued battery that stopped short of K — must be visible, not
   silently averaged as if it were a full read, per this project's own
   never-fail-silently convention). Read `configured_repeats` from
   `config.get("probe_measurement", {}).get("sample_battery_repeats", 25)` —
   same default `runner.py` already uses. Return
   `{"per_probe": [...], "by_phenomenon": {...}}`, same two-level shape as
   `state_transitions`. Wire into `_section_61_learning_measures()`'s
   returned dict as `"sample_battery_repeats_configured"` and
   `"sample_pass_rates"`.

3. **`backend/analytics/reports/markdown_renderer.py` — `_render_s61()`:**
   new "### Sample-Repeat Pass Rate (`weight_mode=sample`)" block, placed
   after "State Transitions" (same probe-measurement grouping). Table:
   `probe_id | session | pass_rate | repeats_completed/configured | flag`.
   A partial-battery row gets a `⚠ PARTIAL BATTERY` flag — same visual
   pattern as the existing orphan-update flag in the Edge Delta Ledger
   block.

4. **Tests — `tests/test_analytics/test_experiment_report.py`:** extend the
   `_insert_probe_row()` helper with `battery_mode` (default `"mean"`, so
   every existing call site is unaffected) and `battery_repeat_index`
   params.
   - `test_learning_measures_ignores_sample_repeats_for_mean_sections` —
     **the regression test for the actual bug found this session**: 1 mean
     row (passing) + 5 sample rows (mixed pass/fail) at the same session
     index; assert `state_transitions.per_probe[0].session_indices` has
     length 1 (not 6) and `probe_pass_rates` reflects only the mean row.
   - `test_sample_pass_rates_computed_from_repeats` — N sample rows at one
     session with a known pass/fail mix; assert `pass_count`/`fail_count`/
     `repeats_completed`/`pass_rate` match exactly.
   - `test_sample_pass_rates_full_battery_not_flagged_partial` — insert
     exactly `configured_repeats` rows (override config to a small number
     for test speed); assert `partial_battery is False`.
   - `test_sample_pass_rates_scoped_to_experiment_id` — mirror the existing
     `test_learning_measures_probe_results_scoped_to_experiment_id` pattern:
     a contaminating attempt under a different `experiment_id` must not leak
     into this experiment's sample pass rate.
   - `test_sample_pass_rates_empty_when_no_sample_rows` — an experiment with
     only mean-mode rows (every pre-M30 experiment) still generates §6.1
     cleanly, `sample_pass_rates.per_probe` present with empty `sessions`,
     no crash — preserves the existing "still generates with empty data"
     contract.
   - `tests/test_analytics/test_markdown_renderer.py`:
     `test_s61_renders_sample_pass_rate_section` — construct a section dict
     with `sample_pass_rates` populated (including one `partial_battery=True`
     entry) and assert the rendered markdown contains the new header and the
     ⚠ flag text.

### Branch / PR handling (operational note, so /build doesn't have to guess)

Do **not** branch fresh off `main` — `main` doesn't have Part A's code yet,
and the current local `fix/smoketest-2026-07-15` branch already has it
(commits `1f39c23`, `aea3a24`, both unpushed), with its first commit
already open as PR #51. Build Part B as new commits on this same branch;
pushing updates PR #51 automatically. Retitle/re-describe PR #51 once done
to reflect the fuller scope (tie-break finding → dual-mode battery →
report fix) rather than opening a second PR — these three commits tell one
continuous story and splitting them now would just be git archaeology
later. Also backfill, as part of this same push: a PROGRESS.md session-log
entry for Part A (retroactive, dated 2026-07-15 — built via Claude Code,
not logged at the time) and a new entry for Part B.

### Explicitly out of scope

- **The actual `weight_mode`/`trained_spokes` decision.** This milestone
  builds the measurement tool; it does not make the call. Next step after
  this ships: run a real dual-mode probe battery through a real experiment
  and read `sample_pass_rates` to actually decide.
- `docs/experiment_protocol.md`'s still-missing `trained_spokes` row — M29's
  own flagged, still-outstanding doc debt. Separate and small; not folded
  in here to keep this milestone's diff reviewable.
- Any live Docker/Sonnet verification. This is a report-generation-layer
  change over already-stored DB rows, not new runtime behavior — unit tests
  (TempDB-backed, matching the existing §6.1 test style, which is itself a
  real-SQLite integration test) are sufficient, same reasoning M29 used for
  a similarly contained, non-runtime-behavior change. A live proof (real
  dual-mode battery → real report → real `sample_pass_rates`) happens
  naturally once the weight_mode/trained_spokes decision unblocks an actual
  experiment run — not a blocker for this milestone.

**Estimate:** under 1 session — two report-layer files plus tests, no new
runtime behavior, no backend/harness changes.

---

## Experiment-0 Follow-Ups — Milestones 31–35

**Source:** the experiment-0 baseline run (2026-08-02,
`exp0-2026-07-26_learned` + control shadows; report at
`memory/reports/experiment_report_exp0-2026-07-26_learned.md`) and the
post-run analysis session of the same date. The run was an infrastructure
success (0 failed turns, 0 ghost MUs, all §6.3 integrity checks PASS) and a
science null against controls, but the analysis surfaced findings the
report layer either computed by hand or couldn't see at all:

1. **Environment drift (MU accumulation).** Seed-fixture MUs' share of the 4
   retrieved slots fell across series in every condition — learned 42% (s0)
   → 14% (s4), random_weight → 4% — because the graph is wiped between
   conditions but accumulates conversation MUs between series, and those
   MUs out-compete the seed knowledge in BM25. Judge up-rate declined in
   lock-step across all three conditions (learned 52%→26%, static
   62%→38%, random 60%→14% by series), so this drift, not learning, is the
   dominant time trend. **Paul's decision (2026-08-02): accumulation is a
   real-world feature — measure it, don't control it.** → M32.
2. **78% of weight updates landed on "unknown cluster" edges** (176 of 225
   in the §6.1 ledger) — conversation-MU edges the probes don't measure.
   **Paul's direction: this is likely where cross-domain real-world
   learning shows up long-term; build classification/quantification now.**
   → M33.
3. **Punishment-dominated regime.** 34% up-rate overall (70 up / 138 down /
   2 none in the learned condition) against a reinforcement-shaped
   hypothesis. The judge rule ("down whenever any criterion is unmet") is
   too harsh for the v3 criteria. **Paul's decision: up = ALL criteria
   met, down = NO criteria met, partial answers get no feedback.** → M31.
4. **§6.2 compares only mean-mode probe results**, which are flat nearly
   everywhere — it is blind to the one learning-shaped trajectory in the
   run (vs_d4's sample pass rate climbing 4%→60%). Its "Learned" column
   also renders `—` instead of the baseline values. → M34.
5. **Two instrumentation defects:** §5.5 shows `traversals: 0` on edges
   with `updates ≥ 1` (impossible — updates only happen on traversed
   edges), and all 13 "runaway-reinforcement" flags are actually normal
   Beta-mean convergence streaks (1/2→1/3→1/4→1/5, shrinking steps —
   the opposite of runaway). → M35.

**Sequencing:** M32–M35 are report-layer only, independently reviewable,
and retroactively valid against exp0's already-stored data — build in any
order, one branch/PR each. **M31 changes the learning signal itself: build
it last**, recalibrate the judge on the new rule before any real run, and
note that once M31 lands, exp0 stops being a comparable baseline for
verdict-driven measures — a re-baseline run (exp0b) under the new scheme
is expected before the config-change experiment series (Paul's call on
timing). TODO-1 (§6.1 N+1 query, tracked in PROGRESS.md) remains separate:
fix before the 10–15-run screening series, not blocked on any of these.

---

## Judge Verdict Scheme: Partial Answers Abstain — Milestone 31

**Decision being implemented (Paul, 2026-08-02):** `up` only when **all**
of a prompt's criteria are satisfied; `down` only when **none** are;
anything in between (partial answers) gets `none` — recorded, but no
weight update. Rationale: the current any-criterion-unmet-→-down rule
produced a 34% up-rate and 144-down/81-up update ledger on exp0's fairly
demanding criteria — a punishment-dominated regime under a
reinforcement-shaped hypothesis — and partial-credit-means-silence is
also the more realistic model of real-world human feedback.

**What already works (verified 2026-08-02, do not rebuild):**
`POST /feedback/verdict` already accepts `verdict="none"`, always inserts
the feedback_events row, and only calls `process_verdict_for_learning()`
for `up`/`down` (`backend/api/chat.py:311`) — the M8 "absence of signal is
an explicit observation" contract. The runner already posts `none`
verdicts (exp0 recorded 3). **No backend change is required.**

1. **Judge protocol — `scripts/harness/judge.py`.** Two design options;
   the recommendation is (b), with calibration as the arbiter:
   - (a) Minimal: rewrite `_build_judge_prompt()`'s grading rule in place
     ("up" = every criterion satisfied; "down" = no criterion satisfied;
     "none" = some but not all, or genuinely unassessable). Smallest diff,
     but relies on `gemma4:e4b` applying an all-vs-none rule correctly in
     one shot.
   - (b) **Recommended — per-criterion checklist protocol:** ask the judge
     to return a per-criterion judgment (e.g. `criteria_met` as a list of
     booleans in listed order, plus `rationale`), and have
     `_parse_verdict()` **derive** the verdict host-side: all true → `up`,
     all false → `down`, mixed → `none`. The rule is then applied exactly,
     per-criterion judgments are easier for a small judge model than a
     global rule, and the per-criterion data is recorded for free
     (feeds M33-style analysis). `criterion_triggered` on a derived
     `down`: cite the first listed criterion (all failed, so the specific
     choice is uninformative by construction; the rationale carries the
     rest) — keeps the backend's down-requires-criterion 422 contract
     untouched. The `_resolve_criterion()` normalization machinery stays
     for citation resolution wherever the judge names criteria in prose.
2. **Interactive mode (`scripts/harness/runner.py`):** the override path
   must offer `none` as an operator choice (partial answers are now an
   expected, common verdict, not an edge case), and the calibration
   context printout should show the per-criterion judgments when protocol
   (b) is used.
3. **Recalibration before any real run:** new calibration plan (clone of
   `plans/exp0_calibration_2026-07-27.json` with a new `plan_id`; same
   20-turn slice) and a 20-turn `--interactive` pass. The criteria are
   unchanged but the rule is new — `gemma4:e4b` has never been graded
   under it. Record the up/down/none mix; expectation from exp0's ledger
   is a large `none` share.
4. **Tests:** derivation truth table (all met → up, none met → down, each
   mixed combination → none, single-criterion prompt where partial is
   impossible), malformed per-criterion payloads (wrong length, non-bool)
   → `JudgeUnparseableError` (direction-neutral, per the M-19/07-27
   asymmetry lesson), citation on derived downs, prompt-text regression.
5. **Expected side effects — verify, don't be surprised by:** total weight
   updates per run drop (partial turns now update nothing), so per-turn
   learning slows and future runs may need more turns for equivalent
   signal; the runaway-flag noise should largely vanish (fewer long
   down-streaks) — though M35 fixes the detector itself regardless.

**Out of scope:** any change to the bank's criteria text (content_hash
must survive — same reasoning as the 07-27 normalization fix), any
backend change, the exp0b re-baseline run itself.

**Estimate:** 1 session including the live recalibration pass.

---

## Environment-Drift Metrics: Seed-Share + Verdict Mix — Milestone 32

**Status: COMPLETE (2026-08-02).** See the Milestone 32 trailer entry near
the end of this file for what was built.

**Goal:** promote the two measures that had to be computed by hand in the
2026-08-02 analysis into first-class report sections, because
MU-accumulation drift is a permanent feature of the system (Paul's call:
measure, don't control) and every future config comparison sits on top of
it.

1. **`backend/analytics/reports/series_report.py` —
   `_section_53_retrieval_rate_classification()`:** the per-turn
   seed/intra/cross counts already exist here. Add `seed_fixture_rate` and
   `cross_series_rate` to the returned dict alongside the existing
   `intra_series_rate` (same denominator: total retrieved). Markdown: two
   new summary lines in the §5.3 block.
2. **`backend/analytics/reports/experiment_report.py` — §5.3
   aggregation:** the experiment report reads stored series-close reports
   (per the M21/PR-41 rule — never recompute from a graph later conditions
   wiped). Extend the §5.3 table with `seed_share` and `cross_share`
   columns, rows in series order, and add a one-line drift statement
   (first-series vs last-series seed_share delta) so the trend is visible
   without reading five series reports. Older stored series JSONs won't
   have the new fields — render `—`, never fail (backward-compat with
   every pre-M32 report on disk).
3. **New verdict-mix subsection (experiment report):** per series: `up`,
   `down`, `none` counts and up-rate = up/(up+down), from
   `feedback_events` scoped by `experiment_id` (bulk turns only —
   `is_probe_turn=False` is already guaranteed on this path), series
   attribution via the run join. This is exactly the table hand-built from
   the console log on 2026-08-02; the report should own it. Place near
   §5.1 (it describes the run environment, not learning).
4. **Tests:** rate arithmetic, aggregation carry-through, missing-field
   backward compat, verdict-mix scoping (a contaminating experiment_id
   must not leak — same pattern as the existing §6.1 scoping tests),
   zero-feedback series (all-probe or dry-run) renders cleanly.

**Out of scope:** any change to retrieval behavior, wiping policy, or MU
creation — this milestone is measurement only, by explicit decision.

**Estimate:** under 1 session — two report-layer files plus tests.

---

## Conversation-Edge Classification — Milestone 33

**Status: COMPLETE (2026-08-02).** See the Milestone 33 trailer entry near
the end of this file for what was built and the resolved exp0 distribution.

**Goal:** in exp0, 176 of 225 weight updates (78%) hit edges the §6.1
ledger classifies "unknown cluster" — edges touching harness-created
conversation MUs rather than seed MUs. The probes are blind to them, so
most of what the mechanism learned was invisible. Paul's direction:
this territory is where cross-domain real-world learning is expected to
appear over time, so the controlled experiments must classify and
quantify it now.

1. **Attribution, derived at report time — no schema change.** Two
   independent signals, both from already-stored data:
   - **Prompt lineage:** `feedback_events.prompt_id` → bank entry →
     `target cluster`. Every conversation MU created by a bulk turn
     inherits that turn's prompt's target cluster.
   - **Seed adjacency:** an edge with a seed-MU endpoint inherits that
     endpoint's cluster (seed clusters are already known to the report).
   Where both signals exist and disagree, classify the edge `mixed` and
   count it separately — a disagreement **is** a cross-domain link, which
   is precisely the phenomenon worth watching, not an error to hide.
   Derivation (not stamping at creation) is deliberate: it works
   retroactively on exp0's stored data and keeps the backend
   condition-agnostic.
2. **`backend/analytics/reports/experiment_report.py` — §6.1 delta
   ledger:** replace the flat within/cross/unknown counts with a typed
   breakdown: edge class (`seed↔seed`, `conv→seed`, `conv↔conv`) ×
   cluster relation (`same-cluster`, `cross-cluster`, `mixed`,
   `unresolvable`). "Unknown" should become a small residual (edges whose
   turn has no prompt_id and no seed endpoint), not 78%.
3. **New markdown subsection:** update counts and mean |delta| per class
   per series — the longitudinal view. The specific quantity to watch over
   future experiments: cross-cluster `conv↔conv` and `conv→seed` updates,
   as the operational definition of emergent cross-domain linkage.
4. **Verification against real data:** regenerate exp0's experiment report
   and record the resolved distribution of the 176 formerly-unknown
   updates in PROGRESS.md. This is the milestone's done-criterion — a
   classification scheme that can't classify the run that motivated it
   isn't done.
5. **Tests:** both attribution signals, disagreement → `mixed`, missing
   prompt_id → seed-adjacency fallback, fully unresolvable → residual,
   pre-M33 report JSONs still render.

**Estimate:** 1 session.

---

## §6.2 Control Shadow: Sample-Mode Comparison + Rendering — Milestone 34

**Status: COMPLETE (2026-08-02).** See the Milestone 34 trailer entry near
the end of this file for what was built and the resolved vs_d4/vs_c1 read.

**Goal:** §6.2's comparative deltas read exactly 0.0000 on exp0 because
they compare only `battery_mode="mean"` probe results, which are flat
nearly everywhere in all conditions — the comparison is insensitive
precisely where the signal lives (`sample_pass_rates`, M30 Part B). The
one learning-shaped trajectory in exp0 (vs_d4 sample pass rate 4%→60%)
was never compared against controls at all.

1. **`backend/analytics/reports/control_shadow.py`:** add a sample-mode
   pass-rate comparison — per-probe, per-session learned-minus-control
   pass-rate deltas computed from each condition's `sample_pass_rates`,
   aggregated by phenomenon. Keep the existing mean-mode comparison
   alongside (labeled as such); do not silently redefine the old measure.
2. **Fix the "Learned" column rendering** — it shows `—` for the learned
   baseline values in every row of the exp0 report; the comparison table
   should show learned's own value, the control's, and the delta.
3. **Clarify `edge_weight_delta_magnitude` presentation:** controls are
   structurally 0 (verified by their own sanity checks), so presenting
   `+0.1109` as a "delta" implies a learning effect where the measure is
   true by construction. Show both values and label the control column
   `0 by design` for static/random.
4. **Live verification doubles as the open science question:** regenerate
   exp0's learned report with both control shadows attached and read
   vs_d4 and vs_c1's learned-vs-control sample trajectories. If vs_d4
   climbs in the controls too, its rise is environment drift; if it climbs
   only under `learned`, it is the run's first real (if mistargeted —
   d4 was not a trained spoke) learning signal. Record the answer in
   PROGRESS.md either way.
5. **Tests:** mirror the existing control_shadow test patterns —
   sample-delta arithmetic, missing-sample-data controls (pre-M30
   experiments) degrade to mean-only without crashing, experiment_id
   scoping.

**Estimate:** under 1 session.

---

## Instrumentation Fixes: Traversal Counter + Runaway Detector — Milestone 35

**Status: COMPLETE (2026-08-02).** See the Milestone 35 trailer entry near
the end of this file for what was built and the resolved live-verification
read against real exp0 data.

1. **§5.5 `traversals: 0` on updated edges
   (`backend/analytics/reports/series_report.py` —
   `_section_55_edge_weight_deltas()`):** every edge in exp0's §5.5 tables
   shows `traversals: 0` alongside `updates ≥ 1`, which is impossible —
   `process_verdict_for_learning()` credits only edges on the traversal
   path. Trace where the traversal count should come from (analytics
   rows vs. weight_updates payloads) and either fix the join or remove
   the column — a silently-zero metric is worse than no metric (the
   project's own never-fail-silently rule, applied to reporting).
2. **Runaway-reinforcement detector redefinition:** the current heuristic
   (3+ consecutive same-direction updates) flagged 13 edges on exp0, all
   of them ordinary Beta(1,1) posterior-mean convergence — e.g. weight
   1/2→1/3→1/4→1/5, deltas -0.1667/-0.0833/-0.05, **shrinking** each
   step, which is the update rule working exactly as designed. Redefine
   the flag to fire only on genuinely anomalous streaks: consecutive
   same-direction updates with **non-decreasing |delta|**, or a streak
   that carries a weight past the saturation threshold. Keep the plain
   same-direction streak count as a separate, informational statistic
   (renamed so it stops crying wolf). Note: M31's abstain scheme will
   independently shorten down-streaks, but the detector must be correct
   on its own terms regardless.
3. **Tests:** regression cases built from the exact exp0 sequences —
   the decaying `[-0.1667, -0.0833, -0.05]` streak must NOT flag; a
   constructed non-decreasing streak and a threshold-crossing streak
   must; the informational streak count still reports both.

**Estimate:** under 1 session; can share a branch with M32 if convenient
(both touch series/experiment report internals), otherwise separate.

---

## Status

```
Milestones 22–24 (Turn-Wiring Fixes) complete. Milestone 25 (Seed Fixture v3)
code complete and in PR review — generator + verifier + tests built, the real
Ollama run produced a clean 34-MU/64-edge draft (manifest failures: []), all 9
verify --draft checks pass, and the sabotage proof confirmed the manifest
mechanism fires end to end. AWAITING PAUL: content review of the generated
draft prose, then the human freeze step (seed_v3_draft.json -> seed_v3.json.gz)
and a final verify --frozen. Only after freeze does the full experimental pilot
run gain seed_v3's counterfactual-content science value over seed_v2.
Milestone 26 (Queue Schema + Preflight + Dry-Run) merged to main.
Milestone 27 (Queue Execution Loop + Resume + Summary) CODE COMPLETE, in PR
review — resolve_run_attempt() extracted from runner.py (behavior-preserving,
new bump_on_complete param), harness.queue_runner.run_queue() (on_plan_failure
continue/halt, post-failure health recheck, crash-resume reconciliation,
duration capture, queue_summary.md), queue.py gains find_latest_launch_dir()
+ report_paths; run_experiment_queue.py wired for real resume (auto-detect or
--resume-dir, queue_content_hash-guarded) and execution. 20 new tests (730
total, all passing). All 4 verification items run live this session against a
real Docker backend + real gemma4:e4b (Docker daemon was up this session,
unlike prior ones): (a) 2-plan micro-queue both completed with correct
durations/summary; (b) a real taskkill mid-plan-1 (5/6 turns recorded) then
re-invoke correctly resumed at turn 5, skipped wipe/seed for the resumed
condition, and plan 2 ran fresh; (c) Paul briefly stopped/restarted Ollama —
plan 1 failed with a genuine connectivity error, the backend-only health
recheck passed (by design /health never checks Ollama), and the queue
correctly continued to plan 2 rather than halting (plan 2 itself also hit the
tail end of Ollama's restart and failed independently — resolved cleanly on a
second invocation, which also exercised resume-of-a-fully-aborted-checkpoint);
(d) full suite green throughout. AWAITING PAUL: PR review.

Milestone 28 (Seed Fixture v3 — Harness Wiring) is MERGED to main (PR #48).
Its live baseline check found `b_spoke1`/`d_spoke1` already read as a
baseline PASS under the frozen fixture's default tie-break, motivating
Milestone 29 (Per-Cluster Trained-Spoke Selection — see section above): a
`RunPlan.trained_spokes` knob so each cluster's trained target is a
documented, headroom-checked plan choice instead of a hardcoded spoke1.

Milestone 29 (Per-Cluster Trained-Spoke Selection) is MERGED to main (PR
#49) — `RunPlan.trained_spokes` added (schema.py, shape-validated), threaded
through `read_cluster_content()`/`build_bank()`/`estimate_bank_cost_for_plan()`/
`generation_params_for_plan()` (prompt_gen.py), with a named `ValueError` on
an unresolvable spoke key and `trained_spokes` now part of the hashed
prompt-bank generation params. 30 new tests (760 total, all passing).

**Protocol note (added 2026-07-15, applies going forward):** any baseline
probe claim must record which `condition` (`static_weight`/`random_weight`/
`learned`) and `weight_mode` (`mean`/`sample`) it was measured under.
`RUN_CONDITION` silently defaults to `static_weight` when the env var is
unset (`orchestrator.py`) — the exact same class of silent-default hazard
the probe-integrity principle (`retrieve()` must run with
`condition=RUN_CONDITION`, never a default) already guards against
elsewhere. Two smoketest passes (M28's original + PR #50's re-confirmation)
both silently measured `static_weight`/`mean` without setting the env var,
and neither was labeled as such at the time. Record condition + weight_mode
explicitly in the run log for every future baseline check.

**2026-07-15 follow-up (PR #50, see `docs/m28_tiebreak_learned_mode_findings_2026-07-15.md`
for full detail):** re-ran the 28-probe battery under `condition="learned"`.
`weight_mode: mean` reproduces the `static_weight` result bit-for-bit (both
paths compute `alpha/(alpha+beta)=0.5` from identical Beta(1,1) priors,
independent of condition) — not a bug, confirms CC's prediction. `weight_mode:
sample` (25 repeats, no reseed needed — probes are read-only) is a
**qualitatively different regime**: every one of the 28 probes shows a noisy
0–36% pass rate under Thompson sampling, not a clean deterministic split.
Only `vs_a3`/`vs_a6` read 0/25, but a binomial check shows that's
statistically indistinguishable from noise at N=25 if every spoke shares a
true win probability near 1/8 (P(≥2 probes falsely read 0/25 by chance) ≈
26%). **The M28/M29 premise — "some spokes deterministically already pass,
avoid those" — is a `weight_mode: mean`-specific finding and does not carry
over to `sample` in the form originally expected; there is no evident stable
"clean-fail" subset to select `trained_spokes` from under `sample`.** The
K2-traversal-only property (no lexical leakage) was re-confirmed under
`learned`/`sample` via direct trace inspection (`hop_depth=1` on all 3
spot-checked passes) — that part of M28's finding does generalize.
**AWAITING PAUL:** decide how to proceed — a much larger-N sample-mode run
to check for a real (not noise-floor) low-probability spoke, or accept the
session-0 pass-rate noise as inherent to `weight_mode: sample` and handle it
via RunPlan measurement methodology (e.g. averaging multiple session-0
probe batteries) rather than `trained_spokes` pre-selection. `--generate-bank`
should not run until this is resolved.

**Milestone 30 (Dual-Mode Probe Battery) is COMPLETE — pushed to PR #51,
awaiting Paul's review/merge.** Part A (harness + backend dual-mode
dispatch, commit `aea3a24`) was built 2026-07-15 but never logged here or
in PROGRESS.md at the time, and stayed unpushed until reconciled during a
2026-07-26 /catchup. Part B (§6.1 report-layer consumption of the
dual-mode data — `battery_mode="mean"` filtering on both
`query_probe_results()` calls in `_section_61_learning_measures()`, plus a
new `_compute_sample_pass_rates()` subsection surfaced as
`sample_pass_rates` in both the JSON report and `markdown_renderer.py`'s
`_render_s61()`) was built 2026-07-26, closing the gap found during that
same reconciliation: existing report sections never filtered by
`battery_mode`, so they'd have silently corrupted the moment a real
dual-mode run happened. 6 new tests (791 total, all passing, no
regressions). Both parts landed as commits on the existing
`fix/smoketest-2026-07-15` branch, updating the already-open PR #51 rather
than opening a second one. This milestone is the direct answer to the
paragraph above: it builds the tool for measuring `sample_pass_rates`
directly rather than pre-selecting a clean-fail `trained_spokes`, but does
not itself make that decision — that's still Paul's call once the PR is
merged and a real dual-mode battery has been run through a real
experiment.

**Milestone 32 (Environment-Drift Metrics: Seed-Share + Verdict Mix) is
COMPLETE**, the first of the "Experiment-0 Follow-Ups — Milestones 31–35"
built (see that section above for the source finding). Report-layer only,
per the milestone's explicit scope — no retrieval, wiping, or MU-creation
change. `series_report.py`'s §5.3 gained `seed_fixture_rate`/
`cross_series_rate` (same denominator as the existing `intra_series_rate`).
`experiment_report.py`'s §5.3 aggregation carries those through per-series
as `seed_share`/`cross_share` and adds a first-series-vs-last-series
`seed_share_drift` statement; a pre-M32 stored series report (missing the
new fields) renders `seed_share`/`cross_share` as `None` → `—` in Markdown
rather than being recomputed from raw counts, so the aggregation can never
silently disagree with what series_report.py itself would report. New
verdict-mix subsection (`_aggregate_verdict_mix()` + `store.
get_verdict_counts_by_series()`) reads bulk-turn `feedback_events` live
(scoped by `experiment_id` via the `analytics` join, `is_probe_turn=0`,
per-series `up`/`down`/`none` counts and `up_rate = up/(up+down)`), placed
near §5.1 in both JSON and Markdown — exactly the table hand-built from the
console log during the 2026-08-02 exp0 analysis. 14 new tests (872 total,
all passing, no regressions): rate arithmetic, aggregation carry-through,
missing-field backward compat (both the per-series values and the drift
statement), verdict-mix scoping (a contaminating `experiment_id` does not
leak, mirroring the existing §6.1 scoping-test pattern), and zero-feedback
series (all-probe/dry-run) rendering cleanly with `up_rate=None` rather
than a fabricated `0.0`. Branch: `milestone-32-seed-share-verdict-mix`.

**Milestone 33 (Conversation-Edge Classification) is COMPLETE.** §6.1's
edge delta ledger replaces the flat within/cross/`unknown_cluster_count`
counts (176/225 = 78% "unknown" in exp0) with a typed breakdown: edge class
(`seed_seed`/`conv_seed`/`conv_conv`) x cluster relation
(`same_cluster`/`cross_cluster`/`mixed`/`unresolvable`), derived entirely at
report time from already-stored data — no schema change, no backend
behavior change. Two independent signals: **seed adjacency** (unchanged —
`mu_info.cluster_id`, non-null only for seed MUs) and **prompt lineage**
(new — `store.get_prompt_ids_by_run()` joins `feedback_events.prompt_id` to
`analytics.run_id` in one query per experiment; `_cluster_from_prompt_id()`
reads the target cluster straight off the ID string, since
`prompt_gen.py` names every bulk prompt `f"{cluster_id}_{i:04d}"` —
`_build_mu_lineage_clusters()` then credits every MU a bulk turn created
(`input_mu_id`/`prompt_mu_id`/`output_mu_id`/`tool_calls`) with that
prompt's cluster). `_classify_edge_update()` combines both signals per
edge: `conv_seed` when the two signals disagree is `mixed` (kept distinct
from `cross_cluster`, which is same-signal-type disagreement) — a
disagreement here is a real cross-domain link, not an artifact of one
segmentation scheme. New `edge_classification` dict (`by_class_relation`
grid, `totals_by_relation`, `cross_domain_watch` list of
`conv_seed`/`mixed` + `conv_conv`/`cross_cluster` edges, and `by_series`
longitudinal counts) replaces the old flat keys entirely;
`markdown_renderer.py`'s §6.1 renders the grid, watch list, and per-series
table, and degrades to nothing (no crash) when `edge_classification` is
absent from a pre-M33 stored report JSON.
**Verified live against the real exp0 "learned" data** (`memory/graph.db`,
read-only — `_section_61_learning_measures()` called directly rather than
the full report regeneration path, which hits an unrelated pre-existing
bug where `generated_reports` stores container-absolute paths
`/app/data/reports/...` that don't resolve on the host): the `seed_seed`
same/cross counts reproduced exactly (42/7, matching the stored report's
old `within_cluster_count`/`cross_cluster_count` byte-for-byte — the new
code is a strict refinement, not a divergent recomputation), and **all 176
formerly-"unknown" updates resolved with zero residual**: `conv_seed` 59
same-cluster + 8 mixed, `conv_conv` 109 same-cluster + 0 cross-cluster + 0
unresolvable. The 8 `mixed` edges are the full cross-domain-linkage watch
list for this run (`conv_conv`/`cross_cluster` contributed none) — see
PROGRESS.md for the per-series breakdown and reading. This was possible
despite the graph being wiped between exp0's three conditions because
seed_v3's MU ids are fixture-fixed (not random per reseed), so seed
adjacency still resolves correctly against whichever condition's reseed is
currently live; prompt lineage never touched `memory_units` at all. 30 new
tests (870 baseline -> 900, all passing, no regressions): classification truth table
(all class x relation combinations), prompt-lineage derivation and its
run-scoping, cross-domain watch-list membership, per-series ordering,
backward-compat rendering, and end-to-end store round-trips (including an
experiment-scoping contamination guard, mirroring the M32/§6.1 pattern).
**Also fixed in this PR (out of M33's original scope, folded in at Paul's
request):** the container-absolute stored-report-path bug the live
verification above surfaced (`_load_stored_series_data()` couldn't read
exp0's stored series reports from the host because `generated_reports`
stores the in-container `/app/data/reports/...` path literally). New
`resolve_report_path()` in `series_report.py` reconstructs the file
location from the current `_REPORTS_DIR` plus only the stored path's
basename; used by `_load_stored_series_data()` and by `admin.py`'s two
report-fetch-by-id endpoints (same bug pattern). 4 more new tests (904
total). Confirmed live: the full `generate_experiment_report()` now reads
all 5 of exp0's stored series reports cleanly from the host with no
"unreadable" warnings.
Branch: `milestone-33-conversation-edge-classification`.

**Milestone 34 (§6.2 Control Shadow: Sample-Mode Comparison + Rendering) is
COMPLETE.** `control_shadow.py`'s `_compute_comparative_deltas()` now returns
the raw learned/control values alongside every delta (not just the delta
itself), and a new `_compute_sample_pass_rate_comparison()` builds a
per-probe, per-session learned-minus-control delta (matched on
`session_index`) from each condition's own `sample_pass_rates` (already
computed by `_section_61_learning_measures()` for both the learned and
control experiments), aggregated by phenomenon (`mean_delta` across all
sessions, `final_session_mean_delta` for the trajectory's endpoint) —
additive to, not a replacement for, the existing mean-mode comparison.
Degrades to an empty comparison (not a crash) when either side has no
sample-mode data (every pre-M30 experiment). `markdown_renderer.py`'s
`_render_s62()` fixed: the "Comparative Deltas" table's Learned column no
longer hardcodes `—`; it and the cross-condition "Condition Comparison
Summary" table (previously hardcoding `(baseline)`) now both show the real
learned value alongside the control's and the delta.
`edge_weight_delta_magnitude`'s control column shows `0 by design` instead
of a bare `0.0000` when the control value is genuinely zero — true by
construction (M24: `process_verdict_for_learning()` only ever writes when
`condition_label == "learned"`, so static/random controls never accumulate
weight_updates) rather than an empirical near-zero result, while still
rendering the real number if that invariant were ever violated. 17 new
tests (900 baseline -> 917, all passing, no regressions): per-session and
by-phenomenon sample comparison arithmetic, missing-sample-data and
probe-only-in-one-condition graceful degradation, the two rendering fixes,
and the `0 by design` labeling (both the by-design and the
would-be-a-bug-if-nonzero paths).
**Live verification — the milestone's open science question — run against
the real exp0 data** (`generate_experiment_report()` called directly
against `memory/graph.db`, read-only, both control shadows attached; the
M33 stored-report-path fix means this now exercises the full report path,
not just `_section_61_learning_measures()` in isolation): **vs_d4's sample
pass-rate climb (4%→24%→56%→60%→48%) does NOT replicate in either
control** — under `static_weight` it stays flat at exactly 0.0000 every
session (same environment/graph growth as `learned`, but no weight
updates), and under `random_weight` it's uncorrelated noise (100%→0%→0%→
100%→100%, tracking each session's independent re-randomization, not a
trend). Read: the climb is real signal from the `learned` condition's own
weight updates, not shared environment drift — Paul's framing of it as the
run's first real (if mistargeted — d4 was never a trained spoke) learning
signal is confirmed, not just plausible. A second, unplanned finding from
the same run: `vs_c1` (a trained spoke) is near-ceiling under `learned`
(0.92-1.00) and *also* near-ceiling under `static_weight` (flat 1.0000) but
flat at 0.0000 under `random_weight` — c1's high pass rate is explained by
the graph's default static weight structure already favoring it, not by
the RL update mechanism specifically; `random_weight`'s reshuffling is what
actually breaks it. Recorded in PROGRESS.md per the milestone's explicit
requirement to answer the question either way.
Branch: `milestone-34-control-shadow-sample-mode`.

**Milestone 35 (Instrumentation Fixes: Traversal Counter + Runaway Detector)
is COMPLETE**, the last of the report-layer "Experiment-0 Follow-Ups —
Milestones 31–35" (M31, the judge-scheme change, is still last per the
sequencing rule since it changes the learning signal itself). Two
independent instrumentation defects fixed, report-layer only, no schema or
backend behavior change:
**(1) §5.5 `traversals: 0`:** `series_report.py`'s
`_section_55_edge_weight_deltas()` previously read `traversal_count` from
`edges.traversal_count` — a DB column that defaults to 0 and that nothing
in the codebase ever increments (`update_edge_weight()`/
`update_edge_counters()` only ever touch weight/alpha/beta). Every edge
reported `traversals: 0` regardless of actual traversal count, including
edges with `updates >= 1`, which is impossible since
`process_verdict_for_learning()` only credits edges on the traversal path.
Fixed by deriving the count from counting each edge_id's appearances across
runs' `activated_edge_ids` (already stored per-turn on the analytics row) —
`retrieval.py` confirms `activated_edge_ids` IS `traversed_edge_ids`, so
this is the correct source, and it required no backend change since the
data was already there.
**(2) Runaway-reinforcement false positives:** the old heuristic (any 3
consecutive algebraically-increasing deltas) flagged exp0's ordinary
Beta(1,1) posterior-mean convergence streaks (e.g. -0.1667/-0.0833/-0.05 —
shrinking magnitude, but numerically increasing since the values are
negative) as "runaway." `_compute_saturation_analysis()` now separates two
concepts: `same_direction_streaks` (informational only — any 3-consecutive
same-sign-delta streak, renamed so it stops crying wolf) and `runaway_flags`
(the actual anomaly, a subset of same-direction streaks — fires only on
non-decreasing `|delta|`, i.e. genuinely accelerating reinforcement, or a
streak that carries the edge's weight across `saturation_threshold` even if
each step is individually shrinking, since that's dangerous regardless of
trajectory shape). `markdown_renderer.py` renders both counts separately
(never conflated) and shows each runaway flag's reason(s).
**Live-verified against real exp0 data** (`memory/graph.db`, read-only,
`MEMORY_PATH` pointed at the host-mounted `./memory` — same pattern as
M33/M34): re-running `_section_61_learning_measures()` for
`exp0-2026-07-26_learned` now returns `runaway_flag_count=0` (down from the
stored report's 13) and `same_direction_streak_count=17` — confirming the
exp0 analysis finding exactly: all 13 were false positives from ordinary
convergence, not genuine runaway reinforcement. Re-running
`_section_55_edge_weight_deltas()` for series s0 against its real analytics
rows now shows nonzero `traversal_count_total` for every edge (e.g. an edge
with `update_count_in_series=4` now correctly shows
`traversal_count_total=7`, versus the stored report's `0`) — and surfaces
15 `zero_delta_high_traversal` flags in that one series alone, a signal
that could never fire before since the traversal count it depends on was
always 0. 5 new tests (917 baseline -> 922, all passing, no regressions):
the exact exp0 decaying-streak sequence (must NOT flag as runaway, must
count as informational), a constructed non-decreasing-magnitude streak and
a threshold-crossing-despite-shrinking streak (both must flag, with the
correct reason recorded), a regression proving traversal count is derived
from `activated_edge_ids` and not the dead DB column, and a
markdown-rendering test for the two-statistic split.
Branch: `milestone-35-instrumentation-fixes`.
Last updated: 2026-08-02
```

**Milestone 1:** Complete — branch `milestone-1-foundation`, PR merged to main.

**Milestone 2:** Complete — branch `milestone-2-memory-engine`, PR merged to main.

**Milestone 3:** Complete — branch `milestone-3-tool-system`, PR merged to main.

**Milestone 4:** Complete — branch `milestone-4-learning-admin-ui`, PR merged to main.

**Milestone 5:** Complete — branch `milestone-5-chat-ui-quality`, PR merged to main.

**Milestone 6:** Complete — branch `milestone-6-prompt-clarity`, PR merged to main.

**Milestone 7:** Complete — branch `milestone-7-multimodal`, PR merged to main.

**Known limitation (Milestone 7):** Ollama 0.30.x on Windows has a known regression (GitHub Issue #16532) where `gemma4:e4b` processes image tokens (server logs confirm "image decoded") but the model does not interpret the content — it responds as if no image was provided. Root cause: PR #16031 "Remove CGO engines, use llama-server exclusively" (merged 2026-05-07) broke gemma4 vision on Windows. The implementation is correct per the OpenAI-compatible spec (`/v1/chat/completions` with nested `image_url` content-parts, images before text). End-to-end manual testing is blocked until Ollama ships a fix. All unit tests pass. Roll back to Ollama v0.24.0 to test multimodal locally in the meantime.

**Milestone 8:** Complete — branch `milestone-8-core-instrumentation`, PR merged to main.

**Milestone 9:** Complete — branch `milestone-9-experiment-framework`, PR pending merge.

**Milestone 10:** Complete — branch `milestone-10-session-context-flag`, PR pending merge.

**Milestone 11:** Complete — branch `milestone-11-empty-response-detection`, PR pending merge.

**Milestone 12:** Complete — branch `milestone-12-series-id-prerequisite-gate`, PR pending merge.

**Milestone 13:** Complete — branch `milestone-13-series-report-generator`, PR pending merge.

**Milestone 14:** Complete — branch `milestone-14-experiment-report-nonlearning`, PR pending merge.

**Milestone 15:** Complete — branch `milestone-15-experiment-report-learning`, PR pending merge.

**Milestone 16:** Complete — branch `milestone-16-control-shadow-admin-ui`, PR merged to main.

**Milestone 17:** Complete — branch `fix/smoketest-2026-06-28-3`, PR merged to main.

**Milestone 18:** Complete — all 7 verification checks PASS with actual numbers on a freshly wiped + re-seeded graph (see PROGRESS.md 2026-06-29 entry), 332 unit tests passing, no regressions. Branches: `milestone-18-phase-0-infra-fixes` (PR #28), `milestone-18-phase-1-seed-v2-generation` (PR #29), `fix/smoketest-2026-06-29` phases 2–4 and final verification (PR #30, #31, #32) — all merged to main.

**Milestone 19 Phase 0:** Complete — Run Plan schema, harness CLI skeleton (elicit/load, turn resolution, manifest, checkpoint, error policy, notification stub), and dry-run mode, all built and verified against the real Docker backend. Discovered and closed a real infrastructure gap along the way: `/admin/seed-graph` only ever did an idempotent merge, never a wipe — added `POST /admin/graph/wipe` (archive-then-delete) so per-condition graph resets are now real. All 7 §3.4 checks PASS with actual recorded output (see PROGRESS.md 2026-07-03 entry). 387 tests passing (332 baseline + 55 new), no regressions. Branch: `milestone-19-phase-0-harness-skeleton`.

**Milestone 19 Phase 1:** Complete — `prompt_bank.py` (artifact schema, content hash, load/save with field-identifying validation) and `prompt_gen.py` (Sonnet-based generation + validation, cost-estimate confirmation gate, pure-Python fingerprint scan). `schema.py`, `turn_resolution.py`, `runner.py`, `elicit.py`, and `run_experiment_harness.py` wired end-to-end (deterministic `prompt_id` assignment, bank-backed `build_turn_message`, hard-stop preflight on a missing/invalid bank). All 4 verification checks PASS with real output against real seed_v2 + Sonnet 5 (see PROGRESS.md 2026-07-05 entry). 421 tests passing (387 baseline + 34 new), no regressions. Branch: `milestone-19-phase-1-prompt-bank`.

**Milestone 19 Phase 2:** Complete — backend: `feedback_events` migration (8 nullable judge-metadata columns), `POST /feedback/verdict` endpoint with an explicit `apply_weight_update` flag, shared `_apply_weight_updates()` helper extracted so both `/feedback` and `/feedback/verdict` audit through one code path. Harness: `judge.py` (new — `OllamaJudgeClient`, `judge_turn()`, closed-vocabulary verdict parsing with one re-ask, `ProbeTurnJudgeError`/`JudgeUnparseableError`), `client.py` (`post_verdict()`), `schema.py` (`judge.temperature` tightened to `<= 0.1`, `judge_unparseable` added to `DEFAULT_ERROR_POLICY` as `soft_continue`), `checkpoint.py` (`verdict`/`feedback_event_id`/`judge_error` fields on `TurnCheckpoint`), `runner.py` (judge-availability preflight before any graph mutation, per-turn judging + verdict posting, `--interactive` override path), `run_experiment_harness.py` (`--interactive` and `--judge-host` flags). Verdict vocabulary decision (grep evidence): no report generator matches on the `"thumbs_*"` literal strings, so the judge endpoint stores `"up"/"down"/"none"` directly. Live-verified against the real Ollama/Gemma instance (Docker wasn't running this session — same deferral pattern as Phase 0/1): 5/5 real judge calls returned clean, correctly-classified JSON verdicts, 0% unparseable rate (well under the 10% risk threshold). 465 tests passing (421 baseline + 44 new), no regressions. Branch: `milestone-19-phase-2-judge`.

**Milestone 20 Phase 1:** Complete — Beta(alpha, beta) evidence counters on edges, `effective_weight()` as the sole condition-dispatched weight read path (learned: posterior mean/Thompson sample; static_weight/random_weight: unchanged scalar `weight` column), path-decayed credit assignment from judge verdicts (`process_verdict_for_learning()`), and the append-only `edge_update_events` log required by the trace-test sign-off gate. Closed a real gap discovered during planning: the backend had no signal for "which condition is this process running" — added an additive `RUN_CONDITION` env var (defaults to `static_weight` so non-harness usage is unaffected) alongside the existing `EXPERIMENT_ID` per-condition restart mechanism. Decay is schema-reserved (`config.yaml: decay:`) and hard-fails startup if `enabled: true`, per scope. 62 new tests (582 total), no regressions, including the required §8 trace test (replays a real up/down verdict sequence on a real traversal edge and reconstructs weight-as-of-turn/traversal-path/retrieved-set from persisted logs alone, no re-execution). Branch: `milestone-20-phase-1-edge-update`.

**Milestone 21:** Complete — harness probe turns now run real `POST /admin/run-probe` measurements (read-only retrieval, `probe_results` rows) instead of Phase-0 `/chat` placeholders that mutated the graph. `assign_probe_schedule()` gives every probe turn a deterministic `probe_id` + globally contiguous 1-based `measurement_session_index` recorded in the manifest; probe turns are never judged and never chat; a plan with probe turns and no loaded battery hard-stops before any graph mutation. `probe_results.experiment_id` (additive migration, stamped server-side) plus experiment-scoped §6.1/§6.3 queries (condition_label fallback for legacy rows) prevent cross-attempt/dry-run probe contamination; the per-condition restart env now also sets `CONDITION_LABEL`. Also fixed the PR #39-flagged prompt-MU nesting: prompt MUs store only the user-message portion (`_prompt_mu_content()`), never the injected `--- Context from memory ---` block (full prompt still audited in `analytics.assembled_prompt_json`). Migration lesson recorded: indexes on migrated columns belong in `_migrate_db()`, never `_INIT_SQL` (which runs first and would fail on every pre-migration DB); `_open_db()` now closes the connection on init failure so a schema error can't leave a process-hanging aiosqlite thread behind. 611 tests passing, no regressions. Branch: `milestone-21-probe-battery-wiring`.

**Milestone 22:** Complete — context-linkage edges relocated from `prompt->retrieved` to `output->retrieved` (+ `output->summary` when Mode 3 fires), `creation_trigger="co_activation"`; the prompt MU is now a routing dead end (only `prompt->input`). New `hyperparameters.retrieval_exclude_source_types` (default `["prompt"]`) filters prompt MUs out of retrieval candidacy — `all_edges` stays unfiltered so excluded MUs' edges still feed neighbor centrality scoring. Discovered and fixed a real, pre-existing bug while writing the milestone's own required failed-output test: `gate/um.py`'s `SourceType` Literal never included `agent_output_failed_token_limit`/`agent_output_failed_empty_output` (added in M11), so `create_um()` has crashed with a pydantic `ValidationError` on every real token-limit or empty-output failure since M11 — no prior test drove a failure through the full `run_interaction()` pipeline, so it was never caught. Fixed by adding both literals. 626 tests passing (617 baseline + 9 new), no regressions. **Manually verified live** against a local (non-Docker) backend + real Ollama `gemma4:e4b` over 6 turns: after padding the corpus past the point where `rank_bm25`'s classic IDF formula goes negative for majority-document terms (a pre-existing library quirk on tiny corpora, not an M22 concern), a later turn's output MU gained a `co_activation` edge directly to an earlier turn's *output* MU — the exact cross-turn routing scenario M22 targets — and every prompt MU across the run had exactly one outgoing edge (`prompt->input`), never a `prompt->retrieved` edge. Branch: `milestone-22-output-mu-routing`.

**Milestone 23:** Complete — Mode-3 summary MUs are no longer edge-orphans: `extract()` now threads `run_id` through to `_summarize()`, which creates one `summary->mu` `co_activation` edge (same `edge_update.prior_alpha/prior_beta` priors pattern as every other edge-creation site) per MU the summary was built from. `_mode1_extract()`/`_mode2_extract()` now prefix each condensed chunk with a bracketed source-kind label (`_source_kind_label()`) so mixed seed/interaction content stays legible to Gemma — `series_id` (`"seed:"` prefix) is checked before `source_type`, since seed MUs are stamped `source_type="agent_output"` identically to real responses. Three user-directed deviations from the spec table drafted during planning: (1) failed-output source types (`agent_output_failed_token_limit`/`agent_output_failed_empty_output`) get no label at all — they're excluded from retrieval candidacy instead, extending `hyperparameters.retrieval_exclude_source_types`'s default from `["prompt"]` to also cover both failed-output types (config.yaml and its code-level fallback in `retrieval.py`); (2) `tool_call`/`tool_result` get an explicit `[memory — tool record]` label, with `[memory — other]` reserved for genuinely unrecognized source types; (3) the `series_id` `"seed:"` prefix is now documented as a stated requirement for the future seed v3 generator (see the Non-Goals note above), not just a v2 fixture convention, since `_source_kind_label()`'s correctness depends on it. 649 tests passing (626 baseline + 23 new), no regressions. Branch: `milestone-23-extractor-summary-edges`.

**Milestone 25:** Code complete, in PR review (awaiting Paul's content review + human freeze) — `scripts/generate_seed_v3.py` (two-tier counterfactual vocabulary, per-MU-category hub/sibling/bridge forbidden derivation, import-time answer-token uniqueness + format self-checks, 64 reciprocal edges, and a `seed_v3_generation_manifest.json` failure ledger), `scripts/verify_seed_v3.py` (9 checks vs. v2's 5, with a `--draft`/`--frozen` flag defaulting to `--draft` so the manifest-repair loop runs on the uncompressed draft), and `tests/test_scripts/test_generate_seed_v3.py` (31 tests over the pure-Python validation surface; importing the module runs the self-checks on the real spec data as a free regression guard). Required-terms lists copied verbatim from `seed_v3_mu_spec.md` §4; a few content gists were paraphrased in the generation prompts to avoid naming a sibling spoke's answer token (which sibling-exclusion forbids — e.g. `a_spoke7`'s "spine order", `c_spoke4`'s "amber/crimson breach"), with no token/rule-ID/required-term changed. **Live run (real Ollama `gemma4:e4b`):** produced `seed_v3_draft.json` (34 MUs, 64 edges) with a clean manifest (`failures: []`) on the first pass — no MU needed hand-repair. `verify_seed_v3.py --draft` passes all 9 checks; check 9's two-hit-bar sub-check WARNs (not FAIL) on `a_spoke3`/`a_spoke5`, whose generated prose adds "structure" on top of the "output" that the family name "Kestrel Output Conventions" already injects into every cluster_a MU — a content-review item, not a defect. **Sabotage proof (build-step 5):** forced one MU to fail against real Ollama by validating against an impossible term the prompt never mentions (lesson: telling Gemma to "include X" makes it copy any nonsense X verbatim, so the impossible token must be hidden from the prompt) — 3 real attempts exhausted, manifest record matched the BUILD-PLAN schema exactly. 680 tests passing (649 baseline + 31 new), no regressions. **Still Paul's to do:** review the draft prose, freeze to `seed_v3.json.gz`, run `verify_seed_v3.py --frozen`. Branch: `milestone-25-seed-fixture-v3`.

**Milestone 25 (planned, pre-build):** planned 2026-07-11 via /planner. See "Seed Fixture v3 — Milestone 25" section above; build spec derived from the approved `seed_v3_mu_spec.md`.

**Milestone 30:** Complete — see "Dual-Mode Probe Battery — Milestone 30" section above. Part A (harness + backend dual-mode dispatch, `retrieve()`'s `weight_mode_override`, the `run_probe()` EDGE_RNG fix, 25 tests) was code-complete 2026-07-15 but unpushed and undocumented until a 2026-07-26 /catchup. Part B (§6.1 report consumption fix — `battery_mode="mean"` filtering on both `query_probe_results()` calls in `_section_61_learning_measures()`, new `_compute_sample_pass_rates()` wired in as `sample_pass_rates`, and a matching `markdown_renderer.py` section, 6 tests) built 2026-07-26. 791 tests passing (785 + 6 new), no regressions. Both parts pushed together on `fix/smoketest-2026-07-15`, updating the already-open PR #51. Branch: `fix/smoketest-2026-07-15`, PR #51 (awaiting Paul's review/merge).

**Milestone 26:** Complete — `scripts/harness/queue.py` (`QueueFile`/`QueuePlanEntry` schema, `QueueManifest`/`QueueCheckpointState` atomic persistence, and the full 7-step `run_preflight()` protocol), `scripts/run_experiment_queue.py` CLI (`--dry-run`), `queues/example_queue.json`. 30 new tests (710 total), all 3 required preflight hard-stops verified live via the real CLI. Branch: `milestone-26-queue-preflight`, merged to main (PR #46).

**Milestone 27:** Complete — `resolve_run_attempt()` extracted from `runner.py`'s `execute_run()` (behavior-preserving; new `bump_on_complete` param, default `True` matching the prior inline behavior exactly, `False` for the queue's own crash-reconciliation peek). New `harness/queue_runner.py`: `run_queue()` — the execution loop calling `execute_run()` once per enabled plan, `on_plan_failure` continue/halt semantics with a post-failure `client.health()` recheck that can override "continue" (a failed plan's root cause might mean the backend itself is unhealthy), crash-resume reconciliation (every plan gets a `resolve_run_attempt(bump_on_complete=False)` peek at its own harness checkpoint before ever calling `execute_run()` again — a plan whose harness run already reached `final_status="complete"` is reconciled without a wasted re-run, closing the exact crash window the planner flagged: harness finishes but the queue's own status write is lost), per-plan/per-condition duration capture from the harness checkpoint's own turn timestamps, `queue_plan_complete`/`queue_plan_failed`/`queue_complete`/`queue_aborted` notifications, and a `queue_summary.md` writer. `queue.py` gains `find_latest_launch_dir()` (lexicographic `{queue_id}_*` scan backing resume auto-detect) and `QueuePlanCheckpointEntry.report_paths`. `run_experiment_queue.py`'s non-dry-run path now resumes (auto-detected latest launch, or explicit `--resume-dir`, both guarded by an exact `queue_content_hash` match — a mismatch hard-stops rather than guessing) or preflights fresh, then runs the queue to completion with a nonzero exit on any non-"complete" final status. 20 new tests (730 total, all passing, no regressions). **All 4 verification items run live this session** — Docker daemon was up for the first time in several sessions, `gemma4:e4b` confirmed on Ollama: (a) a real 2-plan micro-queue against Docker + Ollama, both plans complete with correct real durations and a correct `queue_summary.md`; (b) a real crash-resume — `taskkill`'d the python process mid-plan-1 at 5/6 turns recorded (harness checkpoint `final_status=None`), re-invoked, and it resumed at exactly turn index 5, correctly skipped wipe/seed for the already-in-progress condition, and plan 2 ran a normal fresh wipe/seed; (c) Paul manually stopped and restarted Ollama mid-plan-1 — plan 1 failed with a genuine `connectivity` error (502 from the backend's own upstream Ollama call), the health recheck against the backend (which only checks its DB, never Ollama, by design) passed, and the queue correctly continued to plan 2 rather than halting; plan 2 independently hit the tail end of Ollama's own restart window and failed too, which resolved cleanly on a second invocation (also exercising resume of a fully-aborted, zero-turn checkpoint) once Ollama was confirmed back up; (d) full suite green throughout. Branch: `claude-queue-execution`.

**Milestone 24:** Complete — verification-only milestone, confirm-or-fix against M22+M23's live edge population. All 4 verification items confirmed clean passes by inspection, no fixes needed: (1) SimpleRL is unreachable from the harness/experiment path — `POST /feedback/verdict` routes exclusively through `edge_update.process_verdict_for_learning()`, which refuses writes unless `condition_label == "learned"` regardless of caller intent, and `scripts/harness/client.py` has no code path calling the chat-UI's `/feedback` endpoint at all; (2)+(3) `admin.py`'s `run_probe()` docstring states the invariant directly ("MUST NOT call run_interaction()") and the code confirms it — only `retrieve()` is called, already passing `condition=RUN_CONDITION, rng=EDGE_RNG`; (4) `direction` is inert — grepped every `create_edge()` call site (runtime code + both seed fixture files) and all write `"forward"`, never `"bidirectional"`, and `retrieval.py`'s K2 scoring applies `directional_edge_weight_multiplier` uniformly with no check against the column — documented in `store.py`'s `edges` table schema comment and `config.yaml`'s hyperparameter comment so this isn't rediscovered as a bug. The §5.3/§5.4 report audit (item 5) and admin/frontend grep (item 6) also confirmed clean by inspection, no changes needed. **Required smoketest (item 7) — real backend, real `gemma4:e4b`, 8-turn series against the seed_v2 fixture:** confirmed live the exact cross-turn routing scenario M22/M23 targeted — turn 3 retrieved turn 1's *output* MU via BM25, and the generated series report's §5.4 showed 15 cross-series edges, all `output->retrieved` `co_activation` edges landing in `seed:v2` (expected per M22, not an anomaly). Direct DB inspection confirmed the predicted edge-population shape: every output MU now has out-degree 3–4 (total degree N+1, no longer a dead end), 31 `co_activation` edges (exactly matching the sum of `activated_mu_ids` across all 8 turns), and `prompt_construction` edges dropped to 2/turn (down from the pre-M22 N+2). §5.3 showed 0 `unknown_count` (no unclassified-edge warnings) with a 51.6% intra-series retrieval rate. One transient Ollama stall occurred mid-run (a single request took ~4.5 min before erroring); retried immediately and succeeded in 29s, and the backend recovered cleanly with no orphaned state beyond the expected unfinished prompt/input MU pair from the failed attempt — not reproducible, not related to M22/M23 code, not filed as a bug. 649 tests passing, no regressions (2 doc-only commits, no new tests required — per the milestone's own "Tests" note, none were expected unless the smoketest surfaced a real bug, and it didn't). Branch: `fix/smoketest-2026-07-11`.

**Milestone 19 Phase 3:** Complete — resolved the phase's flagged highest-risk unknown (confirmed live that `EXPERIMENT_ID` is a module-level constant resolved once at backend startup, no per-request override) by having the harness restart the backend container per condition (`docker_lifecycle.py` — `restart_backend()` + `wait_for_health()`); discovered and fixed a second, deeper gap only visible under real Docker verification: `/chat`'s `series_id` was a session *lookup* key that silently discarded an unrecognized caller-supplied ID and minted a random UUID instead — fixed with `get_or_create_session()` (`backend/core/session.py`), which is what makes the harness's deterministic `{plan_id}_{condition}_s{series_index}` series identity actually work. Also closed an undiscussed gap: `random_weight` edge re-randomization was never wired into `runner.py` — added. Built: series/experiment-close reporting (`client.py`: `generate_series_report()`/`generate_experiment_report()`; new `ReportGenerationError` governed by a dedicated `error_policy["report_generation"]` key, soft_continue by default), notification channel dispatch (`notify.py` — "log" implemented, "email"/"webhook" fall back with a warning), and budget_cap enforcement (`cost.py` — `CostTracker`, pre-run + mid-run checks). 510 tests passing (465 baseline + 45 new), no regressions. **Full pilot smoke run (the phase's required done criterion) PASSED with real data:** 3 conditions × 1 series × 7 turns, real `gemma4:e4b` agent + judge, existing Phase 1 prompt bank reused ($0 spend) — all 21 turns completed, correct per-condition `EXPERIMENT_ID` confirmed via `/admin/runs`, `random_weight` randomized 32 edges once at series start, 3 series reports + 1 experiment report generated, and all 4 §6.2 control-shadow sanity checks PASS on real data (`zero_weight_changes`, `zero_feedback_edge_correlation` ×2, `weights_rerandomized` with `std_dev=0.1848` > the `0.05` floor). `budget_cap` verified live in both directions (hard-stop on a genuine $0.58-over-cap estimate; normal completion when uncapped). See PROGRESS.md 2026-07-05 entry for full detail. Branch: `milestone-19-phase-3-pilot`.
