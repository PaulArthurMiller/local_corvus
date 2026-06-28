# Local Corvus — Three-Condition Experiment Protocol

This document is the step-by-step runbook for executing a complete three-condition
experiment (learned / static-weight / random-weight) from scratch. Follow the phases
in order. Each phase includes the exact API calls to make and the expected outputs.

---

## Prerequisites

- Docker Desktop running with Linux containers enabled
- Ollama running natively on Windows (not in Docker) with `gemma4:e4b` pulled
- `.env` file in the project root (see `.env.example`)

```
OLLAMA_HOST=http://host.docker.internal:11434
```

Start the stack:

```
docker-compose up --build
```

Verify all three services are healthy:

- Backend: `GET http://localhost:8000/health` → `{"status":"ok","db":"ok"}`
- Ollama: `GET http://localhost:8000/ollama-health` → `{"status":"ok","ollama":{...}}`
- Admin UI: open `http://localhost:3001` in a browser
- Chat UI: open `http://localhost:3000` in a browser

Run the full unit test suite (outside Docker, on host):

```
pytest tests/ --ignore=tests/integration
```

All 315 tests must pass before proceeding.

---

## Phase 1 — Wipe Graph and Register Seed Fixture

Every experiment begins from the same starting graph. If you are running a new
experiment (not a continuation), wipe the database and re-seed it.

### 1a. Wipe the graph

Delete `memory/graph.db` and let the backend recreate it on the next request.
This removes all MUs, edges, and run records from prior experiments.

```powershell
# Stop the stack first
docker-compose down

# Delete the database
Remove-Item memory\graph.db -ErrorAction SilentlyContinue

# Restart
docker-compose up -d
```

Verify the backend is healthy again before proceeding:

```
GET http://localhost:8000/health
→ {"status":"ok","db":"ok"}
```

### 1b. Register the seed fixture

Load the pre-generated seed MUs into the graph with correct cluster_id tags:

```
POST http://localhost:8000/admin/seed-graph
Body: {"version": "v1"}

Expected response:
{
  "status": "ok",
  "mu_count": 6,
  "version": "v1",
  "manifest_hash": "<sha256>"
}
```

### 1c. Verify seed graph completeness

```
GET http://localhost:8000/admin/graph/stats

Expected response:
{
  "mu_count": 6,
  "mu_with_cluster_id": 6,
  "mu_without_cluster_id": 0,
  "cluster_distribution": {"cluster_a": 3, "cluster_b": 2, "bridge_ab": 1}
}
```

If `mu_without_cluster_id > 0`, the cross-cluster transfer analysis (§6.1) will
degrade. Investigate before proceeding.

### 1d. Sync probes

Probe definitions load automatically on backend startup from `probes/probe_set_v1.yaml`.
Verify they are in the DB via the Admin UI → Probe Results tab.

---

## Phase 2 — Baseline Probe Snapshot (Session Index 0)

Take a probe battery snapshot before any training turns. This is the baseline
measurement against which all subsequent probe results are compared.

### 2a. Take pre-seed snapshot

```
POST http://localhost:8000/admin/probes/snapshot
Body: {"battery_index": 0, "phase": "pre_seed", "condition_label": "seed"}

Expected response:
{"status": "ok", "path": "/app/data/snapshots/battery_0_pre_seed_seed_<ts>.json.gz"}
```

### 2b. Run all probes at session index 0

Run each of the 5 probes in `probe_set_v1.yaml`:

```
POST http://localhost:8000/admin/run-probe
Body: {"probe_id": "p01", "measurement_session_index": 0, "condition_label": "seed"}

POST http://localhost:8000/admin/run-probe
Body: {"probe_id": "p02", "measurement_session_index": 0, "condition_label": "seed"}

POST http://localhost:8000/admin/run-probe
Body: {"probe_id": "p03", "measurement_session_index": 0, "condition_label": "seed"}

POST http://localhost:8000/admin/run-probe
Body: {"probe_id": "p04", "measurement_session_index": 0, "condition_label": "seed"}

POST http://localhost:8000/admin/run-probe
Body: {"probe_id": "p05", "measurement_session_index": 0, "condition_label": "seed"}
```

Each response contains `gold_mu_rank`, `retrieval_precision`, and `retrieval_recall`.
Record the results. On a clean seed graph, p02–p05 are expected to PASS (rank 1);
p01 probes file-related MUs and may FAIL until the workspace file is created in training.

---

## Phase 3 — Learned Condition

Set `experiment_id` and `condition_label` for the learned run. The recommended approach
is to set environment variables before starting the stack (they are resolved at startup):

```
# In .env or as shell variables before docker-compose up:
EXPERIMENT_ID=exp-learned-2026-07-01
CONDITION_LABEL=learning
```

Or edit `config.yaml`:

```yaml
experiment:
  experiment_id: "exp-learned-2026-07-01"
  condition_label: "learning"
```

Restart the backend if config.yaml was changed: `docker-compose restart backend`

Verify the experiment_id is stamped on new runs:

```
GET http://localhost:8000/admin/runs?limit=1
→ run record should show "experiment_id": "exp-learned-2026-07-01"
```

### 3a. Run training series

The learned condition uses active feedback. For each series:

1. Send 10 training turns via the Chat UI or `/chat` endpoint.
   Each turn: send a message, read the response, then provide feedback via
   the thumbs-up/thumbs-down buttons (or `POST /feedback`).
2. Run the mid-series probe battery (session_index = series_number × 2 + 1):
   ```
   POST http://localhost:8000/admin/run-probe
   Body: {"probe_id": "p01", ..., "measurement_session_index": 1, "condition_label": "learning"}
   ```
   Repeat for all 5 probes.
3. Run 10 more training turns with feedback.
4. Run the end-of-series probe battery (session_index = series_number × 2 + 2):
   ```
   POST http://localhost:8000/admin/run-probe
   Body: {"probe_id": "p01", ..., "measurement_session_index": 2, "condition_label": "learning"}
   ```
5. Generate the series report:
   ```
   POST http://localhost:8000/admin/reports/generate-series
   Body: {"experiment_id": "exp-learned-2026-07-01", "series_id": "<series_uuid>"}
   ```

Repeat for 5 series total. The `series_id` for each series is the UUID assigned by
the backend to the current chat session (visible in the chat API response).

### 3b. Provide feedback

After each agent response, POST feedback:

```
POST http://localhost:8000/feedback
Body: {
  "run_id": "<run_id from chat response>",
  "signal": 1,                    # 1 = thumbs up, -1 = thumbs down
  "verdict_basis": "checklist_pass",   # optional: name the reason
  "is_probe_turn": false
}
```

Weight updates are returned in the response. Verify `weight_updates` is non-empty
for thumbs-up on turns where memory MUs were retrieved.

---

## Phase 4 — Static-Weight Control

The static-weight control runs the same prompts with no weight updates.
No feedback is sent. Edge weights remain at `initial_edge_weight: 0.5`.

```
EXPERIMENT_ID=exp-static-2026-07-01
CONDITION_LABEL=static-weight
```

Restart backend to pick up the new experiment_id. Wipe the graph and re-seed
(repeat Phase 1) so this condition starts from the same initial state.

Run the same 5 series and 5 probe batteries as the learned condition, but:
- Do NOT send feedback after any turn
- Do NOT call `POST /feedback`

After all series, verify the static-weight sanity check:

```sql
-- Run inside the container or via /admin/runs inspection:
SELECT COUNT(*) FROM analytics
WHERE experiment_id = 'exp-static-2026-07-01'
  AND json_array_length(weight_updates) > 0;
-- Expected: 0
```

---

## Phase 5 — Random-Weight Control

The random-weight control re-randomises all edge weights at the start of each series.
No feedback is sent. Edge weights are re-randomised to uniform [0, 1] before each series.

```
EXPERIMENT_ID=exp-random-2026-07-01
CONDITION_LABEL=random-weight
```

Restart backend. Wipe graph and re-seed (repeat Phase 1).

Before starting each series, randomise edge weights:

```
POST http://localhost:8000/admin/edges/randomize
Body: {}

Expected response:
{"status": "ok", "edges_randomized": <N>}
```

Run the same 5 series and 5 probe batteries as the learned condition.
Do NOT send feedback.

After all series, verify the random-weight sanity check by checking that edge
weight variance is above the floor (done automatically by the report generator).

---

## Phase 6 — Report Generation

### 6a. Generate series reports

For each series across all three conditions:

```
POST http://localhost:8000/admin/reports/generate-series
Body: {"experiment_id": "<experiment_id>", "series_id": "<series_uuid>"}

→ {"report_id": "<uuid>", "json_path": "...", "md_path": "..."}
```

Retrieve and verify:

```
GET http://localhost:8000/admin/reports/<report_id>
→ JSON with keys: failure_summary, retrieval_trace, retrieval_rate,
  cross_series_edges, edge_weight_deltas, probe_integrity, mode3_count, ctx_pressure

GET http://localhost:8000/admin/reports/<report_id>/markdown
→ Full Markdown text
```

### 6b. Generate learned-condition experiment report (with control shadow)

```
POST http://localhost:8000/admin/reports/generate-experiment
Body: {
  "experiment_id": "exp-learned-2026-07-01",
  "control_experiment_ids": {
    "static_weight": "exp-static-2026-07-01",
    "random_weight": "exp-random-2026-07-01"
  }
}
```

Expected response JSON structure:

```json
{
  "report_id": "<uuid>",
  "series_summary": { ... },
  "integrity_checks": {
    "seed_fixture_provenance": "pass",
    "graph_wipe_verification": "pass",
    "probe_replay_index_normalization": "pass"
  },
  "learning_measures": {
    "state_transitions": { ... },
    "probe_pass_rates": { ... },
    "edge_delta_ledger": { ... },
    "bridge_mu_analysis": { ... },
    "saturation_analysis": { ... }
  },
  "control_shadow": {
    "static_weight": {
      "sanity_checks": {
        "zero_weight_changes": true,
        "zero_feedback_edge_correlation": 0
      },
      "comparative_deltas": { ... }
    },
    "random_weight": {
      "sanity_checks": {
        "weights_rerandomized": true,
        "zero_feedback_edge_correlation": 0
      },
      "comparative_deltas": { ... }
    }
  }
}
```

All four sanity checks must pass before the comparative deltas are meaningful.

### 6c. Verify in Admin UI

Open `http://localhost:3001` → Reports tab:

- All generated reports appear in the list
- "View" renders the Markdown with correct headers and tables
- "↓JSON" downloads the raw JSON
- Comparison table auto-renders when `control_shadow` is non-null

---

## Phase 7 — Post-Experiment Validation

Run the integration test suite against the live stack:

```
pytest tests/integration/ -m integration -v
```

All 5 integration tests must pass:

1. `test_chat_turn_creates_memory_units`
2. `test_feedback_event_triggers_weight_update`
3. `test_series_report_generates_all_sections`
4. `test_experiment_report_learning_measures_populated`
5. `test_control_shadow_sanity_checks_pass`

---

## Quick Reference — All Admin API Endpoints

| Method | Path | Purpose |
|--------|------|---------|
| GET | `/health` | Backend + DB health |
| GET | `/ollama-health` | Ollama connectivity |
| POST | `/chat` | Send a chat turn |
| POST | `/feedback` | Send thumbs-up/down |
| GET | `/admin/runs` | List run records |
| GET | `/admin/graph` | Full MU + edge list |
| GET | `/admin/graph/stats` | MU count + cluster distribution |
| GET | `/admin/config` | Current config snapshot |
| POST | `/admin/seed-graph` | Load seed fixture into graph |
| POST | `/admin/edges/randomize` | Randomise all edge weights |
| POST | `/admin/run-probe` | Run a single probe (read-only) |
| GET | `/admin/probe-results` | List probe results |
| POST | `/admin/probes/snapshot` | Take a named battery snapshot |
| POST | `/admin/reports/generate-series` | Generate §5 series report |
| POST | `/admin/reports/generate-experiment` | Generate §6 experiment report |
| GET | `/admin/reports` | List all generated reports |
| GET | `/admin/reports/{id}` | Fetch report JSON |
| GET | `/admin/reports/{id}/markdown` | Fetch report Markdown |

---

## Troubleshooting

**Backend healthcheck fails (db: error):**
`memory/graph.db` may be locked or the volume mount is missing. Check
`docker-compose logs backend` for the SQLite traceback. Restarting the container
usually resolves transient lock contention.

**Probe all FAIL after a non-wiped graph run:**
Interaction MUs from prior runs outrank seed MUs. Start from a clean graph (Phase 1).

**`learning_measures` is null in experiment report:**
No probe results exist for the experiment_id. Run probe batteries (Phase 2–5) before
generating the experiment report.

**`control_shadow` sanity check `zero_weight_changes` FAIL for static-weight:**
Feedback was accidentally sent during the static-weight run. Inspect
`SELECT * FROM feedback_events JOIN analytics ON feedback_events.run_id = analytics.run_id
WHERE analytics.experiment_id = 'exp-static-...'` to identify the offending runs.

**`weights_rerandomized` FAIL for random-weight control:**
`POST /admin/edges/randomize` was not called before each series, or the graph had
very few edges at that point (low variance from a small edge count). Verify the
`edges_randomized` count in each randomize response is > 10.
