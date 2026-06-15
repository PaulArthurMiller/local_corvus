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

CREATE INDEX IF NOT EXISTS idx_edges_source ON edges(source_mu_id);
CREATE INDEX IF NOT EXISTS idx_edges_target ON edges(target_mu_id);
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


# Module-level singleton — imported by other modules
store = GraphStore()
