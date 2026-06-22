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
    pruning_flag INTEGER DEFAULT 0
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
    session_id TEXT NOT NULL,
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
    is_probe_turn INTEGER NOT NULL DEFAULT 0
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
    bm25_scores_per_mu TEXT NOT NULL DEFAULT '{}'
);

CREATE TABLE IF NOT EXISTS seed_fixture_manifest (
    fixture_version TEXT PRIMARY KEY,
    manifest_hash TEXT NOT NULL,
    mu_count INTEGER NOT NULL,
    type_distribution TEXT NOT NULL DEFAULT '{}',
    generation_date TEXT NOT NULL,
    generation_record TEXT NOT NULL DEFAULT '[]'
);

CREATE INDEX IF NOT EXISTS idx_edges_source ON edges(source_mu_id);
CREATE INDEX IF NOT EXISTS idx_edges_target ON edges(target_mu_id);
CREATE INDEX IF NOT EXISTS idx_analytics_session ON analytics(session_id);
CREATE INDEX IF NOT EXISTS idx_analytics_timestamp ON analytics(timestamp);
CREATE INDEX IF NOT EXISTS idx_feedback_events_run ON feedback_events(run_id);
CREATE INDEX IF NOT EXISTS idx_feedback_events_probe ON feedback_events(is_probe_turn);
CREATE INDEX IF NOT EXISTS idx_probe_results_probe ON probe_results(probe_id);
CREATE INDEX IF NOT EXISTS idx_probe_results_session ON probe_results(measurement_session_index);
CREATE INDEX IF NOT EXISTS idx_probe_results_condition ON probe_results(condition_label);
"""


async def _migrate_db(db: aiosqlite.Connection) -> None:
    """
    Idempotent schema migration — adds columns introduced in Milestone 8
    to existing databases that pre-date this milestone.

    Each ALTER TABLE is wrapped in try/except so a column that already
    exists (fresh install) does not cause an error.
    """
    migrations = [
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
    ]
    for table, column, col_type in migrations:
        try:
            await db.execute(f"ALTER TABLE {table} ADD COLUMN {column} {col_type}")
            await db.commit()
            logger.info("store: migrated %s.%s", table, column)
        except Exception:
            # Column already exists — this is the expected case on fresh installs
            pass


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
    ) -> dict:
        """Insert a new memory unit. Returns the inserted row as a dict."""
        now = created_at or datetime.now(timezone.utc).isoformat()
        db = await _open_db()
        try:
            await db.execute(
                """
                INSERT INTO memory_units
                    (mu_id, created_at, source_type, raw_content)
                VALUES (?, ?, ?, ?)
                """,
                (mu_id, now, source_type, raw_content),
            )
            await db.commit()
        finally:
            await db.close()
        logger.info(
            "store: created MU mu_id=%s source_type=%s", mu_id, source_type
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
                    run_id, session_id, timestamp, input_mu_id, output_mu_id,
                    tool_calls, activated_mu_ids, activated_edge_ids, prompt_mu_id,
                    tokens_in, tokens_out, latency_ms, feedback_signal,
                    weight_updates, hyperparameters_snapshot, algorithm,
                    bm25_scores_per_mu, entry_point_mu_ids, candidate_set_before_ranking,
                    traversal_path, hop_depth_per_retrieved_mu, edge_contributions,
                    assembled_prompt_json, config_hash, full_config_snapshot,
                    condition_label, turn_index, session_turn_index,
                    measurement_session_index, is_probe_turn
                ) VALUES (
                    :run_id, :session_id, :timestamp, :input_mu_id, :output_mu_id,
                    :tool_calls, :activated_mu_ids, :activated_edge_ids, :prompt_mu_id,
                    :tokens_in, :tokens_out, :latency_ms, :feedback_signal,
                    :weight_updates, :hyperparameters_snapshot, :algorithm,
                    :bm25_scores_per_mu, :entry_point_mu_ids, :candidate_set_before_ranking,
                    :traversal_path, :hop_depth_per_retrieved_mu, :edge_contributions,
                    :assembled_prompt_json, :config_hash, :full_config_snapshot,
                    :condition_label, :turn_index, :session_turn_index,
                    :measurement_session_index, :is_probe_turn
                )
                """,
                {
                    "run_id": run["run_id"],
                    "session_id": run["session_id"],
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
                },
            )
            await db.commit()
        finally:
            await db.close()
        logger.info("store: inserted run run_id=%s session_id=%s", run["run_id"], run["session_id"])

    async def query_runs(
        self,
        session_id: Optional[str] = None,
        limit: int = 100,
        offset: int = 0,
    ) -> list[dict]:
        """
        Query run records from the analytics table.

        Optionally filter by session_id. Returns rows ordered by timestamp DESC.
        JSON string fields are decoded back to Python objects.
        """
        import json
        db = await _open_db()
        try:
            if session_id:
                async with db.execute(
                    """
                    SELECT * FROM analytics
                    WHERE session_id = ?
                    ORDER BY timestamp DESC
                    LIMIT ? OFFSET ?
                    """,
                    (session_id, limit, offset),
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

    async def get_session_turn_count(self, session_id: str) -> int:
        """Return how many runs exist for this session (the next session_turn_index)."""
        db = await _open_db()
        try:
            async with db.execute(
                "SELECT COUNT(*) FROM analytics WHERE session_id = ?", (session_id,)
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
                     retrieval_precision, retrieval_recall, bm25_scores_per_mu)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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


# Module-level singleton — imported by other modules
store = GraphStore()
