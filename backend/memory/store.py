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

_DB_PATH = Path(os.getenv("MEMORY_PATH", "/app/memory")) / "graph.db"


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
    traversal_count INTEGER DEFAULT 0
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
    algorithm TEXT
);

CREATE INDEX IF NOT EXISTS idx_edges_source ON edges(source_mu_id);
CREATE INDEX IF NOT EXISTS idx_edges_target ON edges(target_mu_id);
CREATE INDEX IF NOT EXISTS idx_analytics_session ON analytics(session_id);
CREATE INDEX IF NOT EXISTS idx_analytics_timestamp ON analytics(timestamp);
"""


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
    ) -> dict:
        """
        Insert a directed edge between two MUs.

        direction: "forward" | "bidirectional"
        weight: initial weight from config initial_edge_weight
        """
        eid = edge_id or str(uuid.uuid4())
        now = datetime.now(timezone.utc).isoformat()
        db = await _open_db()
        try:
            await db.execute(
                """
                INSERT INTO edges
                    (edge_id, source_mu_id, target_mu_id, created_at, weight, direction)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (eid, source_mu_id, target_mu_id, now, weight, direction),
            )
            await db.commit()
        finally:
            await db.close()
        logger.info(
            "store: created edge edge_id=%s %s→%s weight=%.3f",
            eid,
            source_mu_id,
            target_mu_id,
            weight,
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
                    weight_updates, hyperparameters_snapshot, algorithm
                ) VALUES (
                    :run_id, :session_id, :timestamp, :input_mu_id, :output_mu_id,
                    :tool_calls, :activated_mu_ids, :activated_edge_ids, :prompt_mu_id,
                    :tokens_in, :tokens_out, :latency_ms, :feedback_signal,
                    :weight_updates, :hyperparameters_snapshot, :algorithm
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
        }
        for field in json_fields:
            if r.get(field):
                try:
                    r[field] = json.loads(r[field])
                except (ValueError, TypeError):
                    pass
        return r


# Module-level singleton — imported by other modules
store = GraphStore()
