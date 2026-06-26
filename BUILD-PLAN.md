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

## Status

```
Current milestone: 11 — Empty-Response Failure Detection — COMPLETE
Last updated: 2026-06-26
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
