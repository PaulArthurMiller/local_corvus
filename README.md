# Local Corvus

Local Corvus is a local-first LLM learning research platform. It combines a FastAPI backend, two React/Vite frontends, an Ollama-hosted model, a SQLite-backed memory graph, a universal-message safety gate, a local file tool, analytics capture, graph snapshots, and a simple reinforcement-learning feedback loop.

The project is intended for experiments where you want to observe how a local agent retrieves memory, calls tools, records run metadata, and updates edge weights after thumbs-up or thumbs-down feedback.

## What runs where

| Component | Path | Default URL | Purpose |
| --- | --- | --- | --- |
| Backend API | `backend/` | <http://localhost:8000> | FastAPI app for chat, feedback, health checks, memory, analytics, and admin data. |
| Chat UI | `frontend/` | <http://localhost:3000> | End-user chat interface with per-response feedback buttons. |
| Admin UI | `admin/` | <http://localhost:3001> | Dashboard for run logs, learning curves, and memory graph inspection. |
| Ollama | native host process | <http://localhost:11434> | Local model server used through OpenAI-compatible chat completions. |
| Persistent memory | `./memory/` | mounted as `/app/memory` | SQLite graph database, audit log, snapshots, and tool workspace. |

## Architecture overview

A chat request goes through the full agent pipeline:

1. The backend resolves or creates a session.
2. The UM gate sanitizes the user input and records an audit entry.
3. The input is stored as a memory unit in SQLite.
4. BM25 retrieval and graph traversal select relevant prior memory units.
5. The extractor condenses retrieved context and injects it into the prompt when available.
6. The orchestrator calls Ollama with the configured model and available tools.
7. If the model requests tools, the tool registry dispatches them and feeds sanitized results back to the model.
8. The final response is sanitized, stored, returned to the chat UI, captured in analytics, and followed by a graph snapshot.
9. User feedback can later update activated edge weights with the simple RL algorithm.

Important backend modules:

- `backend/main.py` registers the chat and admin routers plus `/health` and `/ollama-health` utility endpoints.
- `backend/api/chat.py` exposes `POST /chat` and `POST /feedback`.
- `backend/api/admin.py` exposes read-only dashboard routes under `/admin`.
- `backend/core/orchestrator.py` coordinates gate, memory retrieval, prompt construction, Ollama calls, tools, analytics, and snapshots.
- `backend/memory/store.py` owns all direct SQLite access for memory units, edges, and analytics.
- `backend/tools/file_tool.py` provides local `file_read` and `file_write` tools inside the memory workspace.
- `backend/learning/simple_rl.py` updates activated edge weights from feedback.

## Repository layout

```text
.
├── admin/                 # React/Vite admin dashboard on port 3001
├── backend/               # FastAPI backend on port 8000
│   ├── analytics/         # Run-log capture and queries
│   ├── api/               # Thin HTTP route handlers
│   ├── core/              # Session management and orchestrator
│   ├── gate/              # Universal Message gate, sanitization, audit log
│   ├── learning/          # Learning algorithm interfaces and simple RL
│   ├── memory/            # SQLite graph store, retrieval, extraction, snapshots
│   └── tools/             # Tool base classes, registry, local file tool
├── frontend/              # React/Vite chat UI on port 3000
├── tests/                 # Pytest suite
├── config.yaml            # Model, gate, hyperparameter, and tool registry config
├── docker-compose.yml     # Backend, chat UI, and admin UI services
├── BUILD-PLAN.md          # Design plan and milestone rationale
└── PROGRESS.md            # Implementation history and experiment notes
```

## Prerequisites

Install these on the host machine:

- Docker and Docker Compose.
- Ollama running natively on the host, reachable at `localhost:11434`.
- The model configured in `config.yaml` pulled into Ollama. The default model name is `gemma4:e4b`.
- `OLLAMA_CONTEXT_LENGTH` set to match `model.context_window_tokens` in `config.yaml` (8192). Ollama's own default is VRAM-derived and was serving 4096 on the reference machine, which silently truncates long prompts while §5.8 of the series report still calls them comfortable. Nothing validates the two at startup — see the note on `context_window_tokens` in `config.yaml`.

Pull the default model if needed:

```bash
ollama pull gemma4:e4b
```

Verify Ollama responds:

```bash
curl http://localhost:11434/api/tags
```

Pin the context length (Windows; restart Ollama afterwards so it takes effect):

```powershell
setx OLLAMA_CONTEXT_LENGTH 8192
```

Confirm it applied after the next model load — this must print `8192`, not `4096`:

```powershell
Select-String "n_ctx  " "$env:LOCALAPPDATA\Ollama\server.log" | Select-Object -Last 1
```

> Docker services reach host Ollama through `host.docker.internal:11434`. The compose file also adds a Linux `host-gateway` mapping for that hostname.

## Configuration

Most runtime knobs live in `config.yaml`:

- `model.name`: Ollama model name.
- `model.ollama_host`: backend default Ollama URL when `OLLAMA_HOST` is not set.
- `model.timeout_seconds`: LLM request timeout.
- `gate.max_input_length` and `gate.injection_patterns`: deterministic input/output gate settings.
- `hyperparameters.*`: retrieval sizes, token budgets, initial edge weights, learning rate, snapshot frequency, and tool-loop limit.
- `tools.registered`: tool registry entries exposed to the model.

The Docker Compose backend service passes `OLLAMA_HOST=${OLLAMA_HOST}`. If you do not set it in your shell, the backend falls back to the value in `config.yaml`, currently `http://host.docker.internal:11434`.

Example override:

```bash
export OLLAMA_HOST=http://host.docker.internal:11434
docker compose up --build
```

## Startup instructions

### 1. Clone and enter the repo

```bash
git clone <repo-url>
cd local_corvus
```

### 2. Start Ollama on the host

```bash
ollama serve
```

In another terminal, ensure the configured model exists:

```bash
ollama pull gemma4:e4b
```

### 3. Start all app services

From the repository root:

```bash
docker compose up --build
```

This starts:

- Backend API: <http://localhost:8000>
- Chat UI: <http://localhost:3000>
- Admin UI: <http://localhost:3001>

### 4. Check backend health

```bash
curl http://localhost:8000/health
curl http://localhost:8000/ollama-health
```

`/health` should return `{"status":"ok"}`. `/ollama-health` should return `{"status":"ok", ...}` plus Ollama tag data. If `/ollama-health` fails, confirm Ollama is running and that `OLLAMA_HOST` points to the host-visible Ollama URL.

### 5. Send a first message

Open <http://localhost:3000>, type a message, and press Enter. The UI preserves the session ID for subsequent messages in that browser session and shows thumbs-up/thumbs-down controls under assistant responses.

You can also call the backend directly:

```bash
curl -X POST http://localhost:8000/chat \
  -H 'Content-Type: application/json' \
  -d '{"message":"Hello, what can you do?"}'
```

The response includes IDs useful for debugging: `session_id`, `run_id`, input/output/prompt memory-unit IDs, activated memory and edge IDs, tool-call memory IDs, and gate flags.

## Local development without Docker

You can run services directly if you prefer faster iteration.

### Backend

```bash
cd backend
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
export MEMORY_PATH=../memory
export OLLAMA_HOST=http://localhost:11434
uvicorn main:app --reload --host 0.0.0.0 --port 8000
```

### Chat UI

```bash
cd frontend
npm install
VITE_API_URL=http://localhost:8000 npm run dev -- --host 0.0.0.0 --port 3000
```

### Admin UI

```bash
cd admin
npm install
VITE_API_URL=http://localhost:8000 npm run dev -- --host 0.0.0.0 --port 3001
```

## Chat UI usage

The chat UI is a minimal research interface:

1. Open <http://localhost:3000>.
2. Send a message with Enter, or insert a newline with Shift+Enter.
3. Wait for the `thinking…` indicator to clear.
4. Use 👍 or 👎 on assistant messages to send a learning signal for that run.

Feedback submits `POST /feedback` with the response `run_id` and a signal of `+1` or `-1`. The backend loads the run record, finds activated edges, applies SimpleRL, updates edge weights, and writes the feedback plus weight-update details back into analytics.

## Admin dashboard usage

Open <http://localhost:3001>. The dashboard has three tabs.

### Run Log

Use this tab to inspect interaction history from `/admin/runs`.

What you can do:

- View recent runs in descending timestamp order.
- Filter by a full session ID.
- Page through results, 20 rows at a time.
- See latency, feedback signal, tool-call count, activated memory-unit count, and activated edge count.
- Click a row to expand details including full `run_id`, input/output memory IDs, algorithm, and any edge `weight_updates` caused by feedback.

Typical workflow:

1. Send messages in the Chat UI.
2. Switch to Admin → Run Log.
3. Click Refresh.
4. Expand the latest run.
5. Copy `session_id`, `run_id`, or MU IDs for deeper inspection.

### Learning Curve

Use this tab to visualize run-level metrics from `/admin/runs`.

The chart currently shows:

- Interaction number on the X axis, oldest to newest among the latest fetched runs.
- `latency_ms` on the left Y axis.
- Point color by feedback signal: green for `+1`, red for `-1`, grey for no feedback.
- Average post-update edge weight on the right Y axis when a run has `weight_updates`.

Typical workflow:

1. Run a repeated task suite in the Chat UI.
2. Provide thumbs-up/thumbs-down feedback.
3. Open Admin → Learning Curve.
4. Click Refresh after new feedback.
5. Watch latency and edge-weight trends over interactions.

### Memory Inspector

Use this tab to browse the memory graph from `/admin/graph`.

What you can do:

- Search memory units by content, `mu_id`, or `source_type`.
- Browse paginated memory units sorted by activation count.
- See source type, creation timestamp, activation count, and content preview.
- Click a memory unit to expand raw content and connected one-hop edges.
- Inspect edge IDs, direction, current weight, and traversal counts.

Source types you may see include:

- `user_input`
- `prompt`
- `agent_output`
- `tool_call`
- `tool_result`
- `system`

Because `/admin/graph` returns the full graph, this view is best for local research volumes. If the graph becomes very large, use search terms and pagination to keep inspection manageable.

## Tool workspace and file tasks

The built-in file tools are exposed to the model as `file_read` and `file_write`. Both are constrained to the memory workspace:

- In Docker: `/app/memory/workspace/`
- On the host via compose bind mount: `./memory/workspace/`
- In local backend development if `MEMORY_PATH=../memory`: `../memory/workspace/`

Example experiment prompt:

```text
Please write the fact 'The boiling point of water is 100 degrees Celsius at sea level.' to a file called science_facts.txt
```

Later, ask:

```text
What temperature does water boil at sea level? Check your files if needed.
```

Then use the Admin UI to inspect whether tool-call and tool-result memory units were created, whether the run log captured the tool call, and whether feedback updated activated edge weights.

## Persistent data

The compose setup bind-mounts `./memory` into the backend container at `/app/memory`. Generated runtime artifacts include:

- `memory/graph.db`: SQLite database with `memory_units`, `edges`, and `analytics` tables.
- `memory/audit.log`: append-only JSONL audit records from the UM gate.
- `memory/snapshots/*.json.gz`: compressed graph snapshots.
- `memory/workspace/`: files read and written by the local file tools.

To reset local state, stop the containers and remove the memory directory:

```bash
docker compose down
rm -rf memory
```

## API reference

### `GET /health`

Basic backend health check.

### `GET /ollama-health`

Attempts to fetch Ollama tags from the configured Ollama host.

### `POST /chat`

Request:

```json
{
  "message": "Hello",
  "session_id": null
}
```

Response includes:

```json
{
  "response": "...",
  "session_id": "...",
  "run_id": "...",
  "input_mu_id": "...",
  "output_mu_id": "...",
  "prompt_mu_id": "...",
  "activated_mu_ids": [],
  "activated_edge_ids": [],
  "tool_call_mu_ids": [],
  "flags": []
}
```

### `POST /feedback`

Request:

```json
{
  "run_id": "...",
  "signal": 1
}
```

`signal` must be `1` or `-1`.

Response includes the number of edges updated and a list of weight updates.

### `GET /admin/runs`

Query parameters:

- `session_id` optional.
- `limit` default `100`, min `1`, max `1000`.
- `offset` default `0`.

Returns paginated analytics rows.

### `GET /admin/graph`

Returns all memory units and edges for the memory inspector.

### `GET /admin/config`

Returns the parsed runtime configuration from `config.yaml`.

## Testing and checks

Run the Python test suite:

```bash
pytest
```

Run frontend lint/build checks:

```bash
cd frontend
npm run lint
npm run build
```

Run admin lint/build checks:

```bash
cd admin
npm run lint
npm run build
```

## Troubleshooting

### `/ollama-health` returns an error

- Ensure `ollama serve` is running on the host.
- Check that `curl http://localhost:11434/api/tags` works on the host.
- If running under Docker, keep `OLLAMA_HOST=http://host.docker.internal:11434` or use the `config.yaml` default.

### Chat requests time out

- Confirm the configured model exists in Ollama.
- Try a smaller model or increase `model.timeout_seconds` in `config.yaml`.
- Check backend logs from `docker compose logs -f backend`.

### `/chat` returns 502 with a 500 from `host.docker.internal:11434`

Docker networking is fine here — the request reached Ollama and Ollama's model
runner died mid-inference. Confirm in `%LOCALAPPDATA%\Ollama\server.log`
(timestamps there are local time; backend logs are UTC):

```
CUDA error: out of memory
level=ERROR msg="llama-server terminated" exit.hex=0xc0000409
level=WARN  msg="runtime OOM detected; expiring loaded models..."
```

This is self-healing — Ollama expires the OOMed model and the next request
reloads it — so the harness retries `/chat` twice with backoff rather than
aborting the run (`scripts/harness/client.py`). If it recurs:

- Restart Ollama before a long run. With `OLLAMA_KEEP_ALIVE=-1` the runner
  never cycles, and its prompt cache (limit: 8192 MiB, larger than the whole
  8 GB GPU) accumulates across days of uptime until an eviction has no VRAM
  left to work in. The 2026-08-06 crash came from a runner that had been up
  since 2026-08-04.
- Close other GPU consumers. LibreOffice held GPU memory during that run.
- Reserve headroom with `OLLAMA_GPU_OVERHEAD` (bytes) if it still recurs.

Note this leaves orphan MUs: the orchestrator persists the input and prompt MUs
before calling Ollama and does not roll them back when the call fails, so a
crashed turn (and each retry of it) leaves a retrievable `user_input` MU behind.

### Admin UI shows no runs

- Send at least one successful message through the Chat UI or `POST /chat`.
- Click Refresh in the admin tab.
- Confirm the backend is using the expected `MEMORY_PATH` and `graph.db`.

### File tools cannot find a file

- Use relative paths only, such as `notes.txt` or `subdir/notes.txt`.
- Files live under `memory/workspace/`, not the repository root.
- Path traversal outside the workspace is rejected by design.
