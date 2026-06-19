"""
Graph snapshot — dumps the full MU + edge graph to compressed JSON after
each interaction.

Snapshots are written to /app/data/snapshots/{timestamp}.json.gz.
They are append-only; nothing in this module reads or deletes existing files.
"""

import gzip
import json
import logging
import os
from datetime import datetime, timezone
from pathlib import Path

from memory.store import store as graph_store

logger = logging.getLogger(__name__)

_SNAPSHOTS_DIR = Path(os.getenv("MEMORY_PATH", "/app/data")) / "snapshots"


async def take_snapshot() -> str:
    """
    Dump all MUs and edges as gzip-compressed JSON.

    Returns the path of the written snapshot file.
    """
    _SNAPSHOTS_DIR.mkdir(parents=True, exist_ok=True)

    mus = await graph_store.get_all_mus()
    edges = await graph_store.get_all_edges()

    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    filename = f"{timestamp}.json.gz"
    filepath = _SNAPSHOTS_DIR / filename

    payload = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "mu_count": len(mus),
        "edge_count": len(edges),
        "memory_units": mus,
        "edges": edges,
    }

    with gzip.open(filepath, "wt", encoding="utf-8") as f:
        json.dump(payload, f)

    logger.info(
        "snapshot: wrote %d MUs and %d edges to %s",
        len(mus),
        len(edges),
        filename,
    )
    return str(filepath)
