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

## Status

```
Current milestone: 19 Phase 0 — Experiment Harness Skeleton — COMPLETE
Last updated: 2026-07-03
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
