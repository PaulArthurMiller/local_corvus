"""
Graph Store — the only file in the codebase that touches SQLite directly.

All other modules call these methods; none open their own connections.
Uses aiosqlite for async-safe access from FastAPI's async request handlers.

Tables are created on first connect via _open_db().
"""

import logging
import os
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import aiosqlite

logger = logging.getLogger(__name__)

_DB_PATH = Path(os.getenv("MEMORY_PATH", "/app/data")) / "graph.db"


# ---------------------------------------------------------------------------
# Schema initializer
# ---------------------------------------------------------------------------

_INIT_SQL = """
CREATE TABLE IF NOT EXISTS memory_units (
    mu_id TEXT PRIMARY KEY,
    created_at TEXT NOT NULL,
    source_type TEXT NOT NULL,
    raw_content TEXT NOT NULL,
    last_activated_at TEXT,
    activation_count INTEGER DEFAULT 0,
    reference_count INTEGER DEFAULT 0,
    pruning_flag INTEGER DEFAULT 0,
    series_id TEXT,
    experiment_id TEXT,
    cluster_id TEXT
);

CREATE TABLE IF NOT EXISTS edges (
    edge_id TEXT PRIMARY KEY,
    source_mu_id TEXT NOT NULL REFERENCES memory_units(mu_id),
    target_mu_id TEXT NOT NULL REFERENCES memory_units(mu_id),
    created_at TEXT NOT NULL,
    weight REAL NOT NULL,
    direction TEXT NOT NULL,
    last_traversed_at TEXT,
    traversal_count INTEGER DEFAULT 0,
    creation_run_id TEXT,
    creation_trigger TEXT
);

CREATE TABLE IF NOT EXISTS analytics (
    run_id TEXT PRIMARY KEY,
    series_id TEXT NOT NULL,
    experiment_id TEXT,
    timestamp TEXT NOT NULL,
    input_mu_id TEXT,
    output_mu_id TEXT,
    tool_calls TEXT NOT NULL DEFAULT '[]',
    activated_mu_ids TEXT NOT NULL DEFAULT '[]',
    activated_edge_ids TEXT NOT NULL DEFAULT '[]',
    prompt_mu_id TEXT,
    tokens_in INTEGER,
    tokens_out INTEGER,
    latency_ms INTEGER,
    feedback_signal INTEGER,
    weight_updates TEXT NOT NULL DEFAULT '[]',
    hyperparameters_snapshot TEXT NOT NULL DEFAULT '{}',
    algorithm TEXT,
    bm25_scores_per_mu TEXT NOT NULL DEFAULT '{}',
    entry_point_mu_ids TEXT NOT NULL DEFAULT '[]',
    candidate_set_before_ranking TEXT NOT NULL DEFAULT '[]',
    traversal_path TEXT NOT NULL DEFAULT '[]',
    hop_depth_per_retrieved_mu TEXT NOT NULL DEFAULT '{}',
    edge_contributions TEXT NOT NULL DEFAULT '{}',
    assembled_prompt_json TEXT NOT NULL DEFAULT '[]',
    config_hash TEXT,
    full_config_snapshot TEXT NOT NULL DEFAULT '{}',
    condition_label TEXT,
    turn_index INTEGER,
    session_turn_index INTEGER,
    measurement_session_index INTEGER,
    is_probe_turn INTEGER NOT NULL DEFAULT 0,
    use_session_context INTEGER NOT NULL DEFAULT 0,
    failure_reason TEXT
);

CREATE TABLE IF NOT EXISTS feedback_events (
    feedback_event_id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL REFERENCES analytics(run_id),
    verdict TEXT NOT NULL,
    verdict_basis TEXT,
    turn_index INTEGER,
    is_probe_turn INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS probes (
    probe_id TEXT PRIMARY KEY,
    probe_set_version TEXT NOT NULL,
    phenomenon_type TEXT NOT NULL,
    prompt_text TEXT NOT NULL,
    gold_mu_ids TEXT NOT NULL DEFAULT '[]',
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
    bm25_scores_per_mu TEXT NOT NULL DEFAULT '{}',
    outcome_override TEXT
);

CREATE TABLE IF NOT EXISTS seed_fixture_manifest (
    fixture_version TEXT PRIMARY KEY,
    manifest_hash TEXT NOT NULL,
    mu_count INTEGER NOT NULL,
    type_distribution TEXT NOT NULL DEFAULT '{}',
    generation_date TEXT NOT NULL,
    generation_record TEXT NOT NULL DEFAULT '[]'
);

CREATE TABLE IF NOT EXISTS generated_reports (
    report_id TEXT PRIMARY KEY,
    report_type TEXT NOT NULL,
    experiment_id TEXT NOT NULL,
    series_id TEXT,
    generated_at TEXT NOT NULL,
    json_path TEXT NOT NULL,
    md_path TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_generated_reports_experiment ON generated_reports(experiment_id);
CREATE INDEX IF NOT EXISTS idx_generated_reports_type ON generated_reports(report_type);

CREATE INDEX IF NOT EXISTS idx_edges_source ON edges(source_mu_id);
CREATE INDEX IF NOT EXISTS idx_edges_target ON edges(target_mu_id);
CREATE INDEX IF NOT EXISTS idx_analytics_timestamp ON analytics(timestamp);
CREATE INDEX IF NOT EXISTS idx_feedback_events_run ON feedback_events(run_id);
CREATE INDEX IF NOT EXISTS idx_feedback_events_probe ON feedback_events(is_probe_turn);
CREATE INDEX IF NOT EXISTS idx_probe_results_probe ON probe_results(probe_id);
CREATE INDEX IF NOT EXISTS idx_probe_results_session ON probe_results(measurement_session_index);
CREATE INDEX IF NOT EXISTS idx_probe_results_condition ON probe_results(condition_label);
"""


async def _migrate_db(db: aiosqlite.Connection) -> None:
    """
    Idempotent schema migration — adds or renames columns for all milestones.

    Each ALTER TABLE is wrapped in try/except so a column that already
    exists (fresh install) does not cause an error.
    SQLite >= 3.25 is required for RENAME COLUMN (released 2018-09-15).
    """
    # --- M8: add retrieval decomposition + new analytics columns ---
    add_column_migrations = [
        ("edges", "creation_run_id", "TEXT"),
        ("edges", "creation_trigger", "TEXT"),
        ("analytics", "bm25_scores_per_mu", "TEXT NOT NULL DEFAULT '{}'"),
        ("analytics", "entry_point_mu_ids", "TEXT NOT NULL DEFAULT '[]'"),
        ("analytics", "candidate_set_before_ranking", "TEXT NOT NULL DEFAULT '[]'"),
        ("analytics", "traversal_path", "TEXT NOT NULL DEFAULT '[]'"),
        ("analytics", "hop_depth_per_retrieved_mu", "TEXT NOT NULL DEFAULT '{}'"),
        ("analytics", "edge_contributions", "TEXT NOT NULL DEFAULT '{}'"),
        ("analytics", "assembled_prompt_json", "TEXT NOT NULL DEFAULT '[]'"),
        ("analytics", "config_hash", "TEXT"),
        ("analytics", "full_config_snapshot", "TEXT NOT NULL DEFAULT '{}'"),
        ("analytics", "condition_label", "TEXT"),
        ("analytics", "turn_index", "INTEGER"),
        ("analytics", "session_turn_index", "INTEGER"),
        ("analytics", "measurement_session_index", "INTEGER"),
        ("analytics", "is_probe_turn", "INTEGER NOT NULL DEFAULT 0"),
        ("analytics", "use_session_context", "INTEGER NOT NULL DEFAULT 0"),
        ("analytics", "failure_reason", "TEXT"),
        # M12: new columns
        ("analytics", "experiment_id", "TEXT"),
        ("memory_units", "series_id", "TEXT"),
        ("memory_units", "experiment_id", "TEXT"),
        ("memory_units", "cluster_id", "TEXT"),
        # M15: outcome_override on probe_results
        ("probe_results", "outcome_override", "TEXT"),
    ]
    for table, column, col_type in add_column_migrations:
        try:
            await db.execute(f"ALTER TABLE {table} ADD COLUMN {column} {col_type}")
            await db.commit()
            logger.info("store: migrated %s.%s", table, column)
        except Exception:
            # Column already exists — expected on fresh installs
            pass

    # --- M12: rename analytics.session_id → series_id (SQLite >= 3.25) ---
    try:
        await db.execute("ALTER TABLE analytics RENAME COLUMN session_id TO series_id")
        await db.commit()
        logger.info("store: migrated analytics.session_id → series_id")
    except Exception:
        # Column already renamed (fresh install uses series_id directly)
        pass

    # Create the series index regardless of path (removed from _INIT_SQL because Python 3.12
    # executescript raises OperationalError when the column doesn't yet exist in old DBs)
    try:
        await db.execute(
            "CREATE INDEX IF NOT EXISTS idx_analytics_series ON analytics(series_id)"
        )
        await db.commit()
    except Exception:
        pass

    # --- M13: generated_reports table (created via _INIT_SQL on fresh install; ---
    # --- migration is a no-op here since CREATE TABLE IF NOT EXISTS handles it) ---
    # Existing DBs get the table on next _open_db() call via executescript(_INIT_SQL).
    # No ALTER TABLE needed — this is a wholly new table.


async def _open_db() -> aiosqlite.Connection:
    """
    Open an aiosqlite connection, set row_factory, and ensure schema exists.

    Pattern: `db = await _open_db()` → use db → `await db.close()`.
    Do NOT use `async with await _open_db()` — that would try to start
    the underlying thread twice (once on await, once on __aenter__).
    """
    _DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    db = await aiosqlite.connect(str(_DB_PATH))
    db.row_factory = aiosqlite.Row
    await db.executescript(_INIT_SQL)
    await db.commit()
    await _migrate_db(db)
    return db


# ---------------------------------------------------------------------------
# GraphStore
# ---------------------------------------------------------------------------


class GraphStore:
    """
    Async CRUD interface for memory_units and edges tables.

    Each method opens its own connection and closes it on completion.
    This is connection-per-operation — safe for aiosqlite's threading
    model and appropriate for the expected volume of a local research
    platform.
    """

    async def create_mu(
        self,
        mu_id: str,
        source_type: str,
        raw_content: str,
        created_at: Optional[str] = None,
        series_id: Optional[str] = None,
        experiment_id: Optional[str] = None,
        cluster_id: Optional[str] = None,
    ) -> dict:
        """Insert a new memory unit. Returns the inserted row as a dict."""
        now = created_at or datetime.now(timezone.utc).isoformat()
        db = await _open_db()
        try:
            await db.execute(
                """
                INSERT INTO memory_units
                    (mu_id, created_at, source_type, raw_content,
                     series_id, experiment_id, cluster_id)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (mu_id, now, source_type, raw_content, series_id, experiment_id, cluster_id),
            )
            await db.commit()
        finally:
            await db.close()
        logger.info(
            "store: created MU mu_id=%s source_type=%s series_id=%s",
            mu_id, source_type, series_id,
        )
        return {
            "mu_id": mu_id,
            "created_at": now,
            "source_type": source_type,
            "raw_content": raw_content,
            "last_activated_at": None,
            "activation_count": 0,
            "reference_count": 0,
            "pruning_flag": 0,
            "series_id": series_id,
            "experiment_id": experiment_id,
            "cluster_id": cluster_id,
        }

    async def get_mu(self, mu_id: str) -> Optional[dict]:
        """Fetch a single memory unit by ID. Returns None if not found."""
        db = await _open_db()
        try:
            async with db.execute(
                "SELECT * FROM memory_units WHERE mu_id = ?", (mu_id,)
            ) as cursor:
                row = await cursor.fetchone()
        finally:
            await db.close()
        return dict(row) if row is not None else None

    async def create_edge(
        self,
        source_mu_id: str,
        target_mu_id: str,
        weight: float,
        direction: str = "forward",
        edge_id: Optional[str] = None,
        creation_run_id: Optional[str] = None,
        creation_trigger: Optional[str] = None,
    ) -> dict:
        """
        Insert a directed edge between two MUs.

        direction: "forward" | "bidirectional"
        weight: initial weight from config initial_edge_weight
        creation_trigger: "prompt_construction" | "co_activation" | "feedback" | "traversal"
        """
        eid = edge_id or str(uuid.uuid4())
        now = datetime.now(timezone.utc).isoformat()
        db = await _open_db()
        try:
            await db.execute(
                """
                INSERT INTO edges
                    (edge_id, source_mu_id, target_mu_id, created_at, weight, direction,
                     creation_run_id, creation_trigger)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (eid, source_mu_id, target_mu_id, now, weight, direction,
                 creation_run_id, creation_trigger),
            )
            await db.commit()
        finally:
            await db.close()
        logger.info(
            "store: created edge edge_id=%s %s→%s weight=%.3f trigger=%s",
            eid,
            source_mu_id,
            target_mu_id,
            weight,
            creation_trigger,
        )
        return {
            "edge_id": eid,
            "source_mu_id": source_mu_id,
            "target_mu_id": target_mu_id,
            "created_at": now,
            "weight": weight,
            "direction": direction,
            "last_traversed_at": None,
            "traversal_count": 0,
            "creation_run_id": creation_run_id,
            "creation_trigger": creation_trigger,
        }

    async def get_edges_for_mu(self, mu_id: str) -> list[dict]:
        """Return all edges where source_mu_id or target_mu_id matches."""
        db = await _open_db()
        try:
            async with db.execute(
                """
                SELECT * FROM edges
                WHERE source_mu_id = ? OR target_mu_id = ?
                """,
                (mu_id, mu_id),
            ) as cursor:
                rows = await cursor.fetchall()
        finally:
            await db.close()
        return [dict(r) for r in rows]

    async def update_edge_weight(self, edge_id: str, new_weight: float) -> bool:
        """
        Update only the weight field of an edge. Returns True if a row was updated.

        Clamps weight to [0.0, 1.0] before writing — callers should not rely on
        this clamping; it is a safety net.
        """
        clamped = max(0.0, min(1.0, new_weight))
        db = await _open_db()
        try:
            cursor = await db.execute(
                "UPDATE edges SET weight = ? WHERE edge_id = ?",
                (clamped, edge_id),
            )
            await db.commit()
            updated = cursor.rowcount > 0
        finally:
            await db.close()
        if updated:
            logger.info(
                "store: updated edge edge_id=%s new_weight=%.3f", edge_id, clamped
            )
        else:
            logger.warning("store: edge_id=%s not found for weight update", edge_id)
        return updated

    async def update_mu_activation(self, mu_id: str) -> bool:
        """
        Increment activation_count and set last_activated_at to now.
        Returns True if a row was updated.
        """
        now = datetime.now(timezone.utc).isoformat()
        db = await _open_db()
        try:
            cursor = await db.execute(
                """
                UPDATE memory_units
                SET activation_count = activation_count + 1,
                    last_activated_at = ?
                WHERE mu_id = ?
                """,
                (now, mu_id),
            )
            await db.commit()
            updated = cursor.rowcount > 0
        finally:
            await db.close()
        if updated:
            logger.debug("store: activated mu_id=%s at %s", mu_id, now)
        return updated

    async def get_all_mus(self) -> list[dict]:
        """Return all memory units. Used by retrieval layer."""
        db = await _open_db()
        try:
            async with db.execute("SELECT * FROM memory_units") as cursor:
                rows = await cursor.fetchall()
        finally:
            await db.close()
        return [dict(r) for r in rows]

    async def get_all_edges(self) -> list[dict]:
        """Return all edges. Used by retrieval traversal funnel."""
        db = await _open_db()
        try:
            async with db.execute("SELECT * FROM edges") as cursor:
                rows = await cursor.fetchall()
        finally:
            await db.close()
        return [dict(r) for r in rows]

    async def insert_run(self, run: dict) -> None:
        """
        Insert a run record into the analytics table.

        JSON-serializable list/dict fields are stored as JSON strings.
        """
        import json
        db = await _open_db()
        try:
            await db.execute(
                """
                INSERT INTO analytics (
                    run_id, series_id, experiment_id, timestamp, input_mu_id, output_mu_id,
                    tool_calls, activated_mu_ids, activated_edge_ids, prompt_mu_id,
                    tokens_in, tokens_out, latency_ms, feedback_signal,
                    weight_updates, hyperparameters_snapshot, algorithm,
                    bm25_scores_per_mu, entry_point_mu_ids, candidate_set_before_ranking,
                    traversal_path, hop_depth_per_retrieved_mu, edge_contributions,
                    assembled_prompt_json, config_hash, full_config_snapshot,
                    condition_label, turn_index, session_turn_index,
                    measurement_session_index, is_probe_turn, use_session_context,
                    failure_reason
                ) VALUES (
                    :run_id, :series_id, :experiment_id, :timestamp, :input_mu_id, :output_mu_id,
                    :tool_calls, :activated_mu_ids, :activated_edge_ids, :prompt_mu_id,
                    :tokens_in, :tokens_out, :latency_ms, :feedback_signal,
                    :weight_updates, :hyperparameters_snapshot, :algorithm,
                    :bm25_scores_per_mu, :entry_point_mu_ids, :candidate_set_before_ranking,
                    :traversal_path, :hop_depth_per_retrieved_mu, :edge_contributions,
                    :assembled_prompt_json, :config_hash, :full_config_snapshot,
                    :condition_label, :turn_index, :session_turn_index,
                    :measurement_session_index, :is_probe_turn, :use_session_context,
                    :failure_reason
                )
                """,
                {
                    "run_id": run["run_id"],
                    "series_id": run["series_id"],
                    "experiment_id": run.get("experiment_id"),
                    "timestamp": run["timestamp"],
                    "input_mu_id": run.get("input_mu_id"),
                    "output_mu_id": run.get("output_mu_id"),
                    "tool_calls": json.dumps(run.get("tool_calls", [])),
                    "activated_mu_ids": json.dumps(run.get("activated_mu_ids", [])),
                    "activated_edge_ids": json.dumps(run.get("activated_edge_ids", [])),
                    "prompt_mu_id": run.get("prompt_mu_id"),
                    "tokens_in": run.get("tokens_in"),
                    "tokens_out": run.get("tokens_out"),
                    "latency_ms": run.get("latency_ms"),
                    "feedback_signal": run.get("feedback_signal"),
                    "weight_updates": json.dumps(run.get("weight_updates", [])),
                    "hyperparameters_snapshot": json.dumps(run.get("hyperparameters_snapshot", {})),
                    "algorithm": run.get("algorithm"),
                    "bm25_scores_per_mu": json.dumps(run.get("bm25_scores_per_mu", {})),
                    "entry_point_mu_ids": json.dumps(run.get("entry_point_mu_ids", [])),
                    "candidate_set_before_ranking": json.dumps(run.get("candidate_set_before_ranking", [])),
                    "traversal_path": json.dumps(run.get("traversal_path", [])),
                    "hop_depth_per_retrieved_mu": json.dumps(run.get("hop_depth_per_retrieved_mu", {})),
                    "edge_contributions": json.dumps(run.get("edge_contributions", {})),
                    "assembled_prompt_json": json.dumps(run.get("assembled_prompt_json", [])),
                    "config_hash": run.get("config_hash"),
                    "full_config_snapshot": json.dumps(run.get("full_config_snapshot", {})),
                    "condition_label": run.get("condition_label"),
                    "turn_index": run.get("turn_index"),
                    "session_turn_index": run.get("session_turn_index"),
                    "measurement_session_index": run.get("measurement_session_index"),
                    "is_probe_turn": 1 if run.get("is_probe_turn") else 0,
                    "use_session_context": 1 if run.get("use_session_context") else 0,
                    "failure_reason": run.get("failure_reason"),
                },
            )
            await db.commit()
        finally:
            await db.close()
        logger.info(
            "store: inserted run run_id=%s series_id=%s experiment_id=%s",
            run["run_id"], run["series_id"], run.get("experiment_id"),
        )

    async def query_runs(
        self,
        series_id: Optional[str] = None,
        limit: int = 100,
        offset: int = 0,
    ) -> list[dict]:
        """
        Query run records from the analytics table.

        Optionally filter by series_id. Returns rows ordered by timestamp DESC.
        JSON string fields are decoded back to Python objects.
        """
        import json
        db = await _open_db()
        try:
            if series_id:
                async with db.execute(
                    """
                    SELECT * FROM analytics
                    WHERE series_id = ?
                    ORDER BY timestamp DESC
                    LIMIT ? OFFSET ?
                    """,
                    (series_id, limit, offset),
                ) as cursor:
                    rows = await cursor.fetchall()
            else:
                async with db.execute(
                    """
                    SELECT * FROM analytics
                    ORDER BY timestamp DESC
                    LIMIT ? OFFSET ?
                    """,
                    (limit, offset),
                ) as cursor:
                    rows = await cursor.fetchall()
        finally:
            await db.close()

        json_fields = {
            "tool_calls", "activated_mu_ids", "activated_edge_ids",
            "weight_updates", "hyperparameters_snapshot",
            "bm25_scores_per_mu", "entry_point_mu_ids", "candidate_set_before_ranking",
            "traversal_path", "hop_depth_per_retrieved_mu", "edge_contributions",
            "assembled_prompt_json", "full_config_snapshot",
        }
        result = []
        for row in rows:
            r = dict(row)
            for field in json_fields:
                if r.get(field):
                    try:
                        r[field] = json.loads(r[field])
                    except (ValueError, TypeError):
                        pass
            result.append(r)
        return result

    async def update_run_feedback(
        self,
        run_id: str,
        feedback_signal: int,
        weight_updates: list[dict],
    ) -> bool:
        """
        Update a run record with feedback signal and resulting weight updates.

        Returns True if a row was updated.
        """
        import json
        db = await _open_db()
        try:
            cursor = await db.execute(
                """
                UPDATE analytics
                SET feedback_signal = ?, weight_updates = ?
                WHERE run_id = ?
                """,
                (feedback_signal, json.dumps(weight_updates), run_id),
            )
            await db.commit()
            updated = cursor.rowcount > 0
        finally:
            await db.close()
        if updated:
            logger.info(
                "store: updated run feedback run_id=%s signal=%d updates=%d",
                run_id, feedback_signal, len(weight_updates),
            )
        return updated

    async def get_run(self, run_id: str) -> Optional[dict]:
        """Fetch a single run record by ID. Returns None if not found."""
        import json
        db = await _open_db()
        try:
            async with db.execute(
                "SELECT * FROM analytics WHERE run_id = ?", (run_id,)
            ) as cursor:
                row = await cursor.fetchone()
        finally:
            await db.close()
        if row is None:
            return None
        r = dict(row)
        json_fields = {
            "tool_calls", "activated_mu_ids", "activated_edge_ids",
            "weight_updates", "hyperparameters_snapshot",
            "bm25_scores_per_mu", "entry_point_mu_ids", "candidate_set_before_ranking",
            "traversal_path", "hop_depth_per_retrieved_mu", "edge_contributions",
            "assembled_prompt_json", "full_config_snapshot",
        }
        for field in json_fields:
            if r.get(field):
                try:
                    r[field] = json.loads(r[field])
                except (ValueError, TypeError):
                    pass
        return r


    async def get_next_turn_index(self) -> int:
        """Return global sequential turn index: COUNT(*) + 1 from analytics."""
        db = await _open_db()
        try:
            async with db.execute("SELECT COUNT(*) FROM analytics") as cursor:
                row = await cursor.fetchone()
        finally:
            await db.close()
        count = row[0] if row else 0
        return count + 1

    async def get_series_turn_count(self, series_id: str) -> int:
        """Return how many runs exist for this series (the next session_turn_index)."""
        db = await _open_db()
        try:
            async with db.execute(
                "SELECT COUNT(*) FROM analytics WHERE series_id = ?", (series_id,)
            ) as cursor:
                row = await cursor.fetchone()
        finally:
            await db.close()
        return row[0] if row else 0

    async def insert_feedback_event(self, event: dict) -> None:
        """Insert a feedback event record into the feedback_events table."""
        db = await _open_db()
        try:
            await db.execute(
                """
                INSERT INTO feedback_events
                    (feedback_event_id, run_id, verdict, verdict_basis,
                     turn_index, is_probe_turn)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    event["feedback_event_id"],
                    event["run_id"],
                    event["verdict"],
                    event.get("verdict_basis"),
                    event.get("turn_index"),
                    1 if event.get("is_probe_turn") else 0,
                ),
            )
            await db.commit()
        finally:
            await db.close()
        logger.info(
            "store: inserted feedback_event id=%s run_id=%s verdict=%s",
            event["feedback_event_id"],
            event["run_id"],
            event["verdict"],
        )

    async def get_feedback_events_for_run(self, run_id: str) -> list[dict]:
        """Fetch all feedback events for a given run_id."""
        db = await _open_db()
        try:
            async with db.execute(
                "SELECT * FROM feedback_events WHERE run_id = ?", (run_id,)
            ) as cursor:
                rows = await cursor.fetchall()
        finally:
            await db.close()
        return [dict(r) for r in rows]

    async def get_edge(self, edge_id: str) -> Optional[dict]:
        """Fetch a single edge by ID. Returns None if not found."""
        db = await _open_db()
        try:
            async with db.execute(
                "SELECT * FROM edges WHERE edge_id = ?", (edge_id,)
            ) as cursor:
                row = await cursor.fetchone()
        finally:
            await db.close()
        return dict(row) if row is not None else None


    async def sync_probes(self, probes: list[dict]) -> None:
        """
        Upsert probe definitions from YAML into the probes table.
        Called on app startup to keep the DB in sync with the probe set file.
        """
        import json as _json
        db = await _open_db()
        try:
            for probe in probes:
                await db.execute(
                    """
                    INSERT INTO probes
                        (probe_id, probe_set_version, phenomenon_type, prompt_text,
                         gold_mu_ids, is_bridge_probe, bridge_pair_id)
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(probe_id) DO UPDATE SET
                        probe_set_version=excluded.probe_set_version,
                        phenomenon_type=excluded.phenomenon_type,
                        prompt_text=excluded.prompt_text,
                        gold_mu_ids=excluded.gold_mu_ids,
                        is_bridge_probe=excluded.is_bridge_probe,
                        bridge_pair_id=excluded.bridge_pair_id
                    """,
                    (
                        probe["probe_id"],
                        probe["probe_set_version"],
                        probe["phenomenon_type"],
                        probe.get("prompt_text", ""),
                        _json.dumps(probe.get("gold_mu_ids", [])),
                        1 if probe.get("is_bridge_probe") else 0,
                        probe.get("bridge_pair_id"),
                    ),
                )
            await db.commit()
        finally:
            await db.close()
        logger.info("store: synced %d probes to DB", len(probes))

    async def get_probe(self, probe_id: str) -> Optional[dict]:
        """Fetch a single probe definition by ID."""
        import json as _json
        db = await _open_db()
        try:
            async with db.execute(
                "SELECT * FROM probes WHERE probe_id = ?", (probe_id,)
            ) as cursor:
                row = await cursor.fetchone()
        finally:
            await db.close()
        if row is None:
            return None
        r = dict(row)
        r["gold_mu_ids"] = _json.loads(r.get("gold_mu_ids") or "[]")
        r["is_bridge_probe"] = bool(r.get("is_bridge_probe", 0))
        return r

    async def insert_probe_result(self, result: dict) -> None:
        """Insert a probe result into the probe_results table."""
        import json as _json
        db = await _open_db()
        try:
            await db.execute(
                """
                INSERT INTO probe_results
                    (result_id, probe_id, run_id, measurement_session_index,
                     condition_label, retrieved_mu_ids, gold_mu_rank,
                     retrieval_precision, retrieval_recall, bm25_scores_per_mu,
                     outcome_override)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    result["result_id"],
                    result["probe_id"],
                    result.get("run_id"),
                    result["measurement_session_index"],
                    result["condition_label"],
                    _json.dumps(result.get("retrieved_mu_ids", [])),
                    result.get("gold_mu_rank"),
                    result.get("retrieval_precision"),
                    result.get("retrieval_recall"),
                    _json.dumps(result.get("bm25_scores_per_mu", {})),
                    result.get("outcome_override"),
                ),
            )
            await db.commit()
        finally:
            await db.close()
        logger.info(
            "store: inserted probe_result result_id=%s probe_id=%s",
            result["result_id"],
            result["probe_id"],
        )

    async def query_probe_results(
        self,
        probe_id: Optional[str] = None,
        condition_label: Optional[str] = None,
        measurement_session_index: Optional[int] = None,
    ) -> list[dict]:
        """Query probe results with optional filters."""
        import json as _json
        conditions = []
        params: list = []
        if probe_id:
            conditions.append("probe_id = ?")
            params.append(probe_id)
        if condition_label:
            conditions.append("condition_label = ?")
            params.append(condition_label)
        if measurement_session_index is not None:
            conditions.append("measurement_session_index = ?")
            params.append(measurement_session_index)

        where = f"WHERE {' AND '.join(conditions)}" if conditions else ""
        sql = f"SELECT * FROM probe_results {where} ORDER BY measurement_session_index ASC"

        db = await _open_db()
        try:
            async with db.execute(sql, params) as cursor:
                rows = await cursor.fetchall()
        finally:
            await db.close()

        result = []
        for row in rows:
            r = dict(row)
            r["retrieved_mu_ids"] = _json.loads(r.get("retrieved_mu_ids") or "[]")
            r["bm25_scores_per_mu"] = _json.loads(r.get("bm25_scores_per_mu") or "{}")
            # outcome_override is plain TEXT — no JSON decode needed
            result.append(r)
        return result

    async def insert_seed_fixture_manifest(self, manifest: dict) -> None:
        """Insert or replace a seed fixture manifest record."""
        import json as _json
        db = await _open_db()
        try:
            await db.execute(
                """
                INSERT INTO seed_fixture_manifest
                    (fixture_version, manifest_hash, mu_count,
                     type_distribution, generation_date, generation_record)
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(fixture_version) DO UPDATE SET
                    manifest_hash=excluded.manifest_hash,
                    mu_count=excluded.mu_count,
                    type_distribution=excluded.type_distribution,
                    generation_date=excluded.generation_date,
                    generation_record=excluded.generation_record
                """,
                (
                    manifest["fixture_version"],
                    manifest["manifest_hash"],
                    manifest["mu_count"],
                    _json.dumps(manifest.get("type_distribution", {})),
                    manifest["generation_date"],
                    _json.dumps(manifest.get("generation_record", [])),
                ),
            )
            await db.commit()
        finally:
            await db.close()
        logger.info(
            "store: inserted seed_fixture_manifest version=%s", manifest["fixture_version"]
        )

    async def get_seed_fixture_manifest(self, version: str) -> Optional[dict]:
        """Fetch a seed fixture manifest by version. Returns None if not found."""
        import json as _json
        db = await _open_db()
        try:
            async with db.execute(
                "SELECT * FROM seed_fixture_manifest WHERE fixture_version = ?", (version,)
            ) as cursor:
                row = await cursor.fetchone()
        finally:
            await db.close()
        if row is None:
            return None
        r = dict(row)
        r["type_distribution"] = _json.loads(r.get("type_distribution") or "{}")
        r["generation_record"] = _json.loads(r.get("generation_record") or "[]")
        return r

    # -------------------------------------------------------------------------
    # M13: Series report support queries
    # -------------------------------------------------------------------------

    async def get_all_runs_for_series(self, series_id: str) -> list[dict]:
        """
        Return all run records for a series ordered by turn_index ASC.

        JSON fields are decoded. No pagination — callers need the full set
        for report generation.
        """
        import json as _json
        db = await _open_db()
        try:
            async with db.execute(
                "SELECT * FROM analytics WHERE series_id = ? ORDER BY turn_index ASC",
                (series_id,),
            ) as cursor:
                rows = await cursor.fetchall()
        finally:
            await db.close()

        json_fields = {
            "tool_calls", "activated_mu_ids", "activated_edge_ids",
            "weight_updates", "hyperparameters_snapshot",
            "bm25_scores_per_mu", "entry_point_mu_ids", "candidate_set_before_ranking",
            "traversal_path", "hop_depth_per_retrieved_mu", "edge_contributions",
            "assembled_prompt_json", "full_config_snapshot",
        }
        result = []
        for row in rows:
            r = dict(row)
            for field in json_fields:
                if r.get(field):
                    try:
                        r[field] = _json.loads(r[field])
                    except (ValueError, TypeError):
                        pass
            result.append(r)
        return result

    async def get_mus_by_source_type_pattern(
        self, pattern: str, series_id: Optional[str] = None
    ) -> list[dict]:
        """
        Return MUs whose source_type matches a LIKE pattern.

        If series_id is given, restricts to that series. Used by §5.7 (Mode-3)
        and §5.1 (failed MUs).
        """
        db = await _open_db()
        try:
            if series_id is not None:
                async with db.execute(
                    "SELECT * FROM memory_units WHERE source_type LIKE ? AND series_id = ? ORDER BY created_at ASC",
                    (pattern, series_id),
                ) as cursor:
                    rows = await cursor.fetchall()
            else:
                async with db.execute(
                    "SELECT * FROM memory_units WHERE source_type LIKE ? ORDER BY created_at ASC",
                    (pattern,),
                ) as cursor:
                    rows = await cursor.fetchall()
        finally:
            await db.close()
        return [dict(r) for r in rows]

    async def get_mu_series_ids(self, mu_ids: list[str]) -> dict[str, Optional[str]]:
        """
        Return {mu_id: series_id} mapping for a list of MU IDs.

        MUs not found in the DB are excluded from the result. Used by §5.3
        to classify retrieved MUs by their originating series.
        """
        if not mu_ids:
            return {}
        db = await _open_db()
        try:
            placeholders = ",".join("?" * len(mu_ids))
            async with db.execute(
                f"SELECT mu_id, series_id FROM memory_units WHERE mu_id IN ({placeholders})",
                mu_ids,
            ) as cursor:
                rows = await cursor.fetchall()
        finally:
            await db.close()
        return {row["mu_id"]: row["series_id"] for row in rows}

    async def get_cross_series_edges(self, series_id: str) -> list[dict]:
        """
        Return edges where one endpoint belongs to series_id and the other
        belongs to a different (or NULL) series.

        Used by §5.4 — cross-series edge analysis.
        """
        db = await _open_db()
        try:
            async with db.execute(
                """
                SELECT e.edge_id, e.weight,
                       s.series_id AS source_series, s.mu_id AS source_mu,
                       t.series_id AS target_series, t.mu_id AS target_mu
                FROM edges e
                JOIN memory_units s ON e.source_mu_id = s.mu_id
                JOIN memory_units t ON e.target_mu_id = t.mu_id
                WHERE (s.series_id = ? OR t.series_id = ?)
                  AND (s.series_id != t.series_id
                       OR s.series_id IS NULL
                       OR t.series_id IS NULL)
                """,
                (series_id, series_id),
            ) as cursor:
                rows = await cursor.fetchall()
        finally:
            await db.close()
        return [dict(r) for r in rows]

    async def get_edges_by_ids(self, edge_ids: list[str]) -> list[dict]:
        """
        Return edge records for a list of edge IDs.

        Used by §5.5 to get current weights for edges activated in a series.
        """
        if not edge_ids:
            return []
        db = await _open_db()
        try:
            placeholders = ",".join("?" * len(edge_ids))
            async with db.execute(
                f"SELECT * FROM edges WHERE edge_id IN ({placeholders})",
                edge_ids,
            ) as cursor:
                rows = await cursor.fetchall()
        finally:
            await db.close()
        return [dict(r) for r in rows]

    async def count_probe_feedback_events_for_series(
        self, series_id: str
    ) -> tuple[int, list[dict]]:
        """
        Count feedback events with is_probe_turn=1 for runs belonging to series_id.

        Returns (count, list_of_offending_events). Used by §5.6 probe integrity check.
        """
        db = await _open_db()
        try:
            async with db.execute(
                """
                SELECT fe.feedback_event_id, fe.run_id, fe.verdict, fe.turn_index
                FROM feedback_events fe
                JOIN analytics a ON fe.run_id = a.run_id
                WHERE a.series_id = ? AND fe.is_probe_turn = 1
                """,
                (series_id,),
            ) as cursor:
                rows = await cursor.fetchall()
        finally:
            await db.close()
        events = [dict(r) for r in rows]
        return len(events), events

    # -------------------------------------------------------------------------
    # M13: Generated reports CRUD
    # -------------------------------------------------------------------------

    async def insert_generated_report(self, report: dict) -> None:
        """Insert a generated report record into the generated_reports table."""
        db = await _open_db()
        try:
            await db.execute(
                """
                INSERT INTO generated_reports
                    (report_id, report_type, experiment_id, series_id,
                     generated_at, json_path, md_path)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    report["report_id"],
                    report["report_type"],
                    report["experiment_id"],
                    report.get("series_id"),
                    report["generated_at"],
                    report["json_path"],
                    report["md_path"],
                ),
            )
            await db.commit()
        finally:
            await db.close()
        logger.info(
            "store: inserted generated_report report_id=%s type=%s",
            report["report_id"],
            report["report_type"],
        )

    async def query_generated_reports(
        self,
        report_type: Optional[str] = None,
        experiment_id: Optional[str] = None,
    ) -> list[dict]:
        """
        Query generated report records with optional filters.

        Returns rows ordered by generated_at DESC.
        """
        conditions: list[str] = []
        params: list = []
        if report_type:
            conditions.append("report_type = ?")
            params.append(report_type)
        if experiment_id:
            conditions.append("experiment_id = ?")
            params.append(experiment_id)
        where = f"WHERE {' AND '.join(conditions)}" if conditions else ""
        sql = f"SELECT * FROM generated_reports {where} ORDER BY generated_at DESC"

        db = await _open_db()
        try:
            async with db.execute(sql, params) as cursor:
                rows = await cursor.fetchall()
        finally:
            await db.close()
        return [dict(r) for r in rows]

    async def get_generated_report(self, report_id: str) -> Optional[dict]:
        """Fetch a single generated report record by ID."""
        db = await _open_db()
        try:
            async with db.execute(
                "SELECT * FROM generated_reports WHERE report_id = ?", (report_id,)
            ) as cursor:
                row = await cursor.fetchone()
        finally:
            await db.close()
        return dict(row) if row is not None else None

    # -------------------------------------------------------------------------
    # M14: Experiment report support queries
    # -------------------------------------------------------------------------

    async def get_all_series_for_experiment(self, experiment_id: str) -> list[str]:
        """
        Return distinct series_ids for an experiment, ordered by earliest turn_index.

        Used by M14 experiment report to iterate over series.
        """
        db = await _open_db()
        try:
            async with db.execute(
                """
                SELECT series_id
                FROM analytics
                WHERE experiment_id = ? AND series_id IS NOT NULL
                GROUP BY series_id
                ORDER BY MIN(turn_index) ASC
                """,
                (experiment_id,),
            ) as cursor:
                rows = await cursor.fetchall()
        finally:
            await db.close()
        return [row[0] for row in rows]

    async def get_earliest_run_timestamp_for_experiment(
        self, experiment_id: str
    ) -> Optional[str]:
        """
        Return the earliest run timestamp for an experiment.

        Used by the graph-wipe verification check in §6.3.
        """
        db = await _open_db()
        try:
            async with db.execute(
                "SELECT MIN(timestamp) FROM analytics WHERE experiment_id = ?",
                (experiment_id,),
            ) as cursor:
                row = await cursor.fetchone()
        finally:
            await db.close()
        return row[0] if row and row[0] is not None else None

    async def count_pre_experiment_foreign_mus(
        self, experiment_id: str, earliest_timestamp: str
    ) -> int:
        """
        Count MUs from a different experiment that existed before this experiment started.

        A non-zero count indicates a possible incomplete graph wipe before this
        experiment run. Excludes NULL experiment_id (pre-M12 MUs) and seed-prefixed
        experiment_ids (excluded by the experiment_id != :eid check, since seed MUs
        share the same experiment_id). Per §6.3 graph-wipe verification.
        """
        db = await _open_db()
        try:
            async with db.execute(
                """
                SELECT COUNT(*) FROM memory_units
                WHERE experiment_id IS NOT NULL
                  AND experiment_id != ?
                  AND experiment_id NOT LIKE 'seed:%'
                  AND created_at < ?
                """,
                (experiment_id, earliest_timestamp),
            ) as cursor:
                row = await cursor.fetchone()
        finally:
            await db.close()
        return row[0] if row else 0

    async def get_distinct_seed_series_ids(self) -> list[str]:
        """
        Return all distinct seed series_ids from memory_units (format: 'seed:{version}').

        Used by §6.3 seed-fixture provenance check to enumerate which fixture
        versions are present in the database.
        """
        db = await _open_db()
        try:
            async with db.execute(
                "SELECT DISTINCT series_id FROM memory_units WHERE series_id LIKE 'seed:%'"
            ) as cursor:
                rows = await cursor.fetchall()
        finally:
            await db.close()
        return [row[0] for row in rows]

    async def get_probe_session_indices_for_condition(
        self, condition_label: str
    ) -> list[int]:
        """
        Return distinct measurement_session_index values for a condition label, sorted ASC.

        Used by §6.3 probe-replay-index normalization check to detect gaps.
        """
        db = await _open_db()
        try:
            async with db.execute(
                """
                SELECT DISTINCT measurement_session_index
                FROM probe_results
                WHERE condition_label = ?
                ORDER BY measurement_session_index ASC
                """,
                (condition_label,),
            ) as cursor:
                rows = await cursor.fetchall()
        finally:
            await db.close()
        return [row[0] for row in rows]

    async def get_condition_label_for_experiment(
        self, experiment_id: str
    ) -> Optional[str]:
        """
        Return the condition_label associated with an experiment (from analytics).

        Used to link probe_results (filtered by condition_label) to the experiment.
        """
        db = await _open_db()
        try:
            async with db.execute(
                "SELECT condition_label FROM analytics WHERE experiment_id = ? LIMIT 1",
                (experiment_id,),
            ) as cursor:
                row = await cursor.fetchone()
        finally:
            await db.close()
        return row[0] if row else None

    # -------------------------------------------------------------------------
    # M15: Learning measures query methods
    # -------------------------------------------------------------------------

    async def get_all_runs_for_experiment(self, experiment_id: str) -> list[dict]:
        """
        Return all run records for an experiment ordered by turn_index ASC.

        JSON fields are decoded. Used by §6.1 learning measures to access
        weight_updates and feedback_signal across the full experiment.
        """
        import json as _json
        db = await _open_db()
        try:
            async with db.execute(
                "SELECT * FROM analytics WHERE experiment_id = ? ORDER BY turn_index ASC",
                (experiment_id,),
            ) as cursor:
                rows = await cursor.fetchall()
        finally:
            await db.close()

        json_fields = {
            "tool_calls", "activated_mu_ids", "activated_edge_ids",
            "weight_updates", "hyperparameters_snapshot",
            "bm25_scores_per_mu", "entry_point_mu_ids", "candidate_set_before_ranking",
            "traversal_path", "hop_depth_per_retrieved_mu", "edge_contributions",
            "assembled_prompt_json", "full_config_snapshot",
        }
        result = []
        for row in rows:
            r = dict(row)
            for field in json_fields:
                if r.get(field):
                    try:
                        r[field] = _json.loads(r[field])
                    except (ValueError, TypeError):
                        pass
            result.append(r)
        return result

    async def get_all_probes(self) -> list[dict]:
        """
        Return all probe definitions from the probes table.

        Used by §6.1 to iterate over probes for state-transition analysis
        and bridge-MU detection.
        """
        import json as _json
        db = await _open_db()
        try:
            async with db.execute(
                "SELECT * FROM probes ORDER BY probe_id ASC"
            ) as cursor:
                rows = await cursor.fetchall()
        finally:
            await db.close()
        result = []
        for row in rows:
            r = dict(row)
            r["gold_mu_ids"] = _json.loads(r.get("gold_mu_ids") or "[]")
            r["is_bridge_probe"] = bool(r.get("is_bridge_probe", 0))
            result.append(r)
        return result

    async def get_mu_info_batch(self, mu_ids: list[str]) -> dict[str, dict]:
        """
        Return {mu_id: {series_id, cluster_id, source_type}} for a list of MU IDs.

        MUs not found in the DB are excluded from the result. Used by §6.1 to
        classify retrieved MUs by cluster and series origin.
        """
        if not mu_ids:
            return {}
        placeholders = ",".join("?" for _ in mu_ids)
        db = await _open_db()
        try:
            async with db.execute(
                f"SELECT mu_id, series_id, cluster_id, source_type FROM memory_units WHERE mu_id IN ({placeholders})",
                mu_ids,
            ) as cursor:
                rows = await cursor.fetchall()
        finally:
            await db.close()
        return {
            row["mu_id"]: {
                "series_id": row["series_id"],
                "cluster_id": row["cluster_id"],
                "source_type": row["source_type"],
            }
            for row in rows
        }


# Module-level singleton — imported by other modules
store = GraphStore()
