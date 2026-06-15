# Local Corvus — Architecture Design Document
**Version:** 0.1 (Skeleton)
**Status:** In Design — Not For Implementation
**Last Updated:** 2026-06-14

---

## 1. Project Purpose

Local Corvus is a **learning research platform** whose primary purpose is to experimentally demonstrate, measure, and compare machine learning algorithms operating on a local LLM agent. The agent's practical usefulness as an assistant is secondary to its role as a controlled, observable test subject.

### Design Tenets
1. **Simple and observable over impressive.** Every component does one thing and exposes its state.
2. **Config over code.** Behavioral changes happen in YAML, not Python.
3. **Stable substrate, swappable algorithms.** Memory structure does not change when learning algorithms change.
4. **Measure everything.** If it can't be measured, it can't be learned from.
5. **Knobs and sliders.** All uncertain parameters live in the Hyperparameter Registry.

---

## 2. Model and Runtime

| Property | Decision |
|---|---|
| Model | Gemma 4 E4B |
| Runtime | Ollama (native Windows/WSL2, NOT in Docker) |
| API format | OpenAI-compatible at `localhost:11434/v1` |
| Rationale | Google DeepMind provenance; 8GB VRAM fit; native tool calling; large context window |

**Ollama runs outside Docker.** The Python application layer is containerized; Ollama handles its own GPU access natively. The app container reaches Ollama at `host.docker.internal:11434`.

---

## 3. High-Level Architecture

```
┌─────────────────────────────────────────────────────┐
│                   Docker Container                   │
│                                                     │
│  ┌──────────┐   ┌──────────┐   ┌────────────────┐  │
│  │ Chat UI  │   │ Admin UI │   │  Python Backend│  │
│  │ (React)  │   │ (React)  │   │  (FastAPI)     │  │
│  └────┬─────┘   └────┬─────┘   └───────┬────────┘  │
│       │              │                 │            │
│       └──────────────┴────── HTTP ─────┘            │
│                                        │            │
│                          ┌─────────────▼──────────┐ │
│                          │   Core Agent Loop      │ │
│                          │   (Orchestrator)       │ │
│                          └─────┬──────────────────┘ │
│                                │                    │
│          ┌─────────────────────┼──────────────┐     │
│          │                     │              │     │
│  ┌───────▼──────┐   ┌──────────▼──────┐  ┌───▼───┐ │
│  │   Memory     │   │  Tool Registry  │  │  UM   │ │
│  │   Engine     │   │  (Universal)    │  │  Gate │ │
│  └───────┬──────┘   └─────────────────┘  └───────┘ │
│          │                                          │
│  ┌───────▼──────┐   ┌─────────────────┐            │
│  │  Graph Store │   │  Analytics /    │            │
│  │  (SQLite)    │   │  Snapshot Store │            │
│  └──────────────┘   └─────────────────┘            │
│                                                     │
│  /app/memory  (named Docker volume)                 │
└─────────────────────────────────────────────────────┘
                          │
                          │ host.docker.internal:11434
                          ▼
               ┌─────────────────────┐
               │  Ollama (native)    │
               │  Gemma 4 E4B        │
               │  RTX 5060 GPU       │
               └─────────────────────┘
```

---

## 4. Security Layer — UniversalMessage / Pitch-Catch

Carried forward from OpenRattler. Simplified for local-only operation.

### Core Principle
Every message that crosses a trust boundary is wrapped, sanitized, and logged. The **boundary** is the product, not the message schema.

### Trust Boundaries (Local Corvus)
| Boundary | Direction | Gate Required |
|---|---|---|
| User input → Agent | Inbound | UM sanitize-in |
| Tool result → Agent | Inbound | UM sanitize-in |
| Agent → Tool call | Outbound | UM sanitize-out |
| Agent → Response | Outbound | UM sanitize-out |
| Memory read → Prompt | Inbound | UM sanitize-in |
| Memory write ← Agent | Outbound | UM sanitize-out |

### UM Record Fields (Minimum)
```python
{
  "um_id": uuid,
  "timestamp": iso8601,
  "boundary": str,          # e.g. "user_input", "tool_result"
  "direction": "in" | "out",
  "raw_content": str,       # pre-sanitization
  "sanitized_content": str, # post-sanitization
  "session_id": uuid,
  "channel_id": str,
  "flags": list[str]        # any issues detected
}
```

### Session and Channel Isolation
- Each conversation session has a unique `session_id`
- Channels (chat, heartbeat, tool, memory) are isolated; cross-channel injection requires explicit UM gate
- Audit log append-only; stored in Analytics Store

### Boundary Validation
- Input length limits (configurable)
- Prompt injection pattern detection (deterministic, no model call)
- Schema validation on tool calls and results
- Sanitization functions are pure, stateless, unit-testable

---

## 5. Memory Engine

### 5.1 Memory Unit (MU) Schema

```python
{
  # Identity (immutable after creation)
  "mu_id": uuid,
  "created_at": iso8601,
  "source_type": "user_input" | "agent_output" | "tool_call" 
                  | "tool_result" | "prompt" | "system",
  "raw_content": str,            # immutable original content

  # Operational bookkeeping (mutable)
  "last_activated_at": iso8601,
  "activation_count": int,
  "reference_count": int,        # number of active edges pointing here
  "pruning_flag": bool
}
```

**MU creation rule:** Every input to and output from the LLM becomes an MU automatically. No judgment call about "importance." All content-based manipulation (search, extraction, summarization) occurs outside the MU.

### 5.2 Edge Schema

```python
{
  # Identity (immutable after creation)
  "edge_id": uuid,
  "source_mu_id": uuid,
  "target_mu_id": uuid,
  "created_at": iso8601,

  # Learning surface (mutable)
  "weight": float,               # 0.0 - 1.0; ONLY field learning algos write to
  "direction": "forward" | "bidirectional",

  # Bookkeeping (mutable)
  "last_traversed_at": iso8601,
  "traversal_count": int
}
```

**Single edge type.** No semantic type labels. Directionality + weight carry all structural meaning. The LLM determines semantic relationships after prompt injection; the graph carries only activation strength.

### 5.3 Retrieval Algorithm (Graph-Guided RAG)

**Entry:** BM25/TF-IDF keyword index over MU `raw_content` (no separate embedding model; transparent and swappable)

**Traversal funnel:**
```
1. Keyword match → candidate pool (top N per keyword; N configurable)
2. Candidates with 2+ keyword matches → sum first-level edge weights → top K₁
3. From K₁, follow directed edges → sum second-level edge weights → top K₂
4. Directional edges get weight multiplier (configurable)
5. Final set passed to Analyzer/Extractor
```

All K and N values live in the Hyperparameter Registry.

### 5.4 Analyzer/Extractor Module

Pluggable module. Called by retrieval and by learning algorithms. Default cascade:

```
1. Keyword-plus-context filter:
   Keep sentences containing keywords + one sentence before/after.
   Fast, no model call, transparent.

2. First/last sentence filter:
   Fallback if (1) yields too little content.
   Crude but captures directives and results reliably.

3. Summarization call (Gemma):
   Triggered only if filtered content exceeds token budget threshold (configurable).
   One additional model call; result becomes its own MU.
```

The summarization-trigger threshold is a hyperparameter. The module interface is fixed; the implementation is swappable.

### 5.5 Prompt Construction

1. Retrieve activated MU tree (retrieval algorithm above)
2. Analyzer/Extractor condenses to token budget
3. Condensed context injected into prompt header
4. **Prompt itself becomes a new MU** (connected to all MUs that contributed to it)
5. Prompt-MU serves as consolidation hub; future traversals can route through it

Session context compression: when session length approaches configurable limit, earlier turns are summarized (Gemma call) and the summary becomes an MU, replacing the raw turns in active context.

---

## 6. Learning System

### 6.1 Architecture Principle

Learning algorithms are **edge weight updaters.** They receive:
- The set of MUs and edges activated in an interaction
- A feedback signal (varies by algorithm)

They output:
- A set of `(edge_id, new_weight)` pairs

They do not modify MU content or schema. The graph is the stable substrate.

### 6.2 Learning Algorithm Interface (Pseudocode)

```python
class LearningAlgorithm:
    def update(
        self,
        activated_edges: list[Edge],
        activated_mus: list[MU],
        feedback: FeedbackSignal
    ) -> list[tuple[uuid, float]]:  # (edge_id, new_weight)
        raise NotImplementedError
```

### 6.3 Algorithm Options (To Be Explored)

| Algorithm | Feedback Signal | Status |
|---|---|---|
| Simple RL | User thumbs up/down | Starting point |
| Bayesian (Path 1) | Response = prediction; positive/negative feedback | Near-term |
| Bayesian (Path 2) | LC outputs confidence score; used as force multiplier | Deferred — requires confidence number reliability testing |
| Combined B+RL | Confidence × reward signal | Open research question |

**Open question:** How does a Bayesian update and a simple RL reward each adjust the same edge weights after the same interaction? This must be answered before finalizing the learning module interface.

### 6.4 Weight Initialization and Decay

**OPEN — Requires Design Decision**

Outstanding questions:
- Initial edge weight value (flat start vs. source-type-based prior)
- Decay function shape (linear, exponential, sigmoid)
- Whether decay is per-MU, per-edge, or per-retrieval
- Age factor and its configurable parameters

All decay and initialization parameters live in the Hyperparameter Registry.

### 6.5 Dreaming Cycle (Deferred)

Async background process for offline consolidation:
- Decay weight updates
- Prune edges below threshold weight
- Compress snapshot logs
- Potential: merge highly co-activated MU clusters

Does not block main interaction loop. Implementation deferred to Phase 2.

---

## 7. Tool System

### 7.1 Universal Tool Interface

```python
class Tool:
    name: str
    description: str
    parameter_schema: dict  # JSON Schema
    
    def execute(self, params: dict) -> ToolResult:
        raise NotImplementedError
```

Tool registration is config-driven. Adding a tool = adding a file + a config entry. The agent loop does not know about specific tools.

### 7.2 Tool Call Loop

```
1. Agent produces tool call request
2. UM sanitize-out gate
3. Universal dispatcher routes to registered tool
4. Tool executes
5. UM sanitize-in gate on result
6. Result returned as MU (type: tool_result)
7. Agent loop continues (max iterations: configurable)
```

No parallel tool calls (single GPU, single model instance; sequential is cleaner for debugging and measurement).

### 7.3 Initial Tool Set (Minimal)

- **Weather check** (primary learning experiment tool)
- **Clock/date** (heartbeat scheduling)
- **File read/write** (memory file access if needed outside graph)
- **Search** (future; deferred)

---

## 8. Hyperparameter Registry

All uncertain behavioral parameters. Lives in `config.yaml`. Part of the experimental record.

```yaml
hyperparameters:
  # Retrieval
  retrieval_keyword_pool_size: 20       # N per keyword
  retrieval_level1_top_k: 3            # K₁
  retrieval_level2_top_k: 1            # K₂  
  directional_edge_weight_multiplier: 1.5
  
  # Context
  context_token_budget: 2048
  summarization_trigger_tokens: 1500
  session_compression_threshold: 8000
  
  # Learning
  initial_edge_weight: 0.5             # OPEN
  weight_update_rate: 0.1              # configurable learning rate
  decay_rate: 0.01                     # OPEN
  pruning_threshold: 0.05
  
  # System
  tool_loop_max_iterations: 5
  snapshot_frequency: "every_interaction"
  dreaming_cycle_interval_minutes: 60  # deferred
```

---

## 9. Analytics and Admin Layer

First-class architectural component. Designed before learning algorithms, not after.

### 9.1 Data Captured Per Interaction

```python
{
  "run_id": uuid,
  "session_id": uuid,
  "timestamp": iso8601,
  "input_mu_id": uuid,
  "output_mu_id": uuid,
  "tool_calls": list[uuid],            # MU IDs
  "activated_mu_ids": list[uuid],
  "activated_edge_ids": list[uuid],
  "prompt_mu_id": uuid,
  "tokens_in": int,
  "tokens_out": int,
  "latency_ms": int,
  "feedback_signal": any,
  "weight_updates": list[{edge_id, old_weight, new_weight}],
  "hyperparameters_snapshot": dict,    # registry state at time of run
  "algorithm": str                     # which learning algorithm was active
}
```

### 9.2 Graph Snapshot

Full graph state (all MUs + edges + weights) captured after each interaction. Stored as compressed JSON. Diff-based log optional future optimization.

### 9.3 Admin UI Components (To Be Designed)

| View | Purpose |
|---|---|
| Run log | Raw interaction history; filterable |
| Learning curve | Performance metric over iterations for a task type |
| Algorithm comparison | Side-by-side metrics for two algorithm configurations on same task suite |
| Memory inspector | Browse MU graph; view edge weights; trace MU history |
| Hyperparameter log | Timeline of registry changes correlated with performance |
| Weight distribution | Histogram of edge weights over time |

### 9.4 Metrics (To Be Defined)

Performance metrics for "this learning algorithm is working" are task-type dependent and must be defined before each experiment. Examples:
- Task completion rate
- User correction rate (negative feedback events)
- Response relevance score (human-rated or automated)
- Token efficiency (tokens used / outcome quality)

---

## 10. Docker Setup

```
docker-compose.yml
├── service: backend (Python/FastAPI)
│   ├── ports: 8000:8000
│   ├── volumes: ./memory:/app/memory
│   └── env: OLLAMA_HOST=host.docker.internal:11434
├── service: frontend (React, chat UI)
│   └── ports: 3000:3000
└── service: admin (React, admin UI)
    └── ports: 3001:3001
```

Named volume `/app/memory` persists graph store and snapshots across container restarts. Accessible from Windows via Docker Desktop.

---

## 11. Open Questions (Pre-Implementation)

| # | Question | Priority |
|---|---|---|
| 1 | Weight initialization: flat vs. source-type-based prior? | High |
| 2 | Decay function shape and parameters | High |
| 3 | Bayesian Path 1 vs Path 2 vs Combined: which first? | High |
| 4 | RL action space: how does reward distribute back along activated edges? | High |
| 5 | Performance metrics for first task type (weather experiment) | High |
| 6 | Admin UI: separate React app or same app with routes? | Medium |
| 7 | BM25 vs simple keyword index for entry search | Medium |
| 8 | Snapshot compression format (gzip JSON, SQLite blob, other) | Medium |
| 9 | Dreaming cycle implementation approach | Low (Phase 2) |
| 10 | Embeddings upgrade path for Analyzer/Extractor | Low (Phase 2) |

---

## 12. What Is Explicitly Out of Scope

- Parallel or sub-agents (single GPU, single model instance)
- External API escalation (no frontier model router in v1)
- Multi-user sessions
- Complex entity hierarchy in memory (no special node types; Paul is a bag of MUs)
- Edge type labels beyond weight + directionality
- Embeddings model (deferred; BM25 first)
- Dreaming cycle (Phase 2)

---

*End of v0.1 skeleton. Subject to revision after review.* 