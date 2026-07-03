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
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from memory.store import store as graph_store

logger = logging.getLogger(__name__)

_SNAPSHOTS_DIR = Path(os.getenv("MEMORY_PATH", "/app/data")) / "snapshots"


def _sanitize_for_filename(value: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]", "_", value) or "unlabeled"


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


async def take_battery_snapshot(
    battery_index: int,
    phase: str,
    condition_label: str,
) -> str:
    """
    Named snapshot for a measurement battery.

    phase: "pre" | "post"
    Written to: /app/data/snapshots/battery_{index}_{phase}_{condition}_{ts}.json.gz
    Returns the file path for logging.
    """
    _SNAPSHOTS_DIR.mkdir(parents=True, exist_ok=True)

    mus = await graph_store.get_all_mus()
    edges = await graph_store.get_all_edges()

    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    filename = f"battery_{battery_index}_{phase}_{condition_label}_{timestamp}.json.gz"
    filepath = _SNAPSHOTS_DIR / filename

    payload = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "snapshot_type": "battery",
        "battery_index": battery_index,
        "phase": phase,
        "condition_label": condition_label,
        "mu_count": len(mus),
        "edge_count": len(edges),
        "memory_units": mus,
        "edges": edges,
    }

    with gzip.open(filepath, "wt", encoding="utf-8") as f:
        json.dump(payload, f)

    logger.info(
        "snapshot: battery snapshot battery=%d phase=%s condition=%s -> %s",
        battery_index,
        phase,
        condition_label,
        filename,
    )
    return str(filepath)


async def take_pre_pruning_snapshot() -> str:
    """
    Snapshot immediately before any pruning operation.

    Written to: /app/data/snapshots/pre_prune_{ts}.json.gz
    Returns the file path for logging.
    """
    _SNAPSHOTS_DIR.mkdir(parents=True, exist_ok=True)

    mus = await graph_store.get_all_mus()
    edges = await graph_store.get_all_edges()

    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    filename = f"pre_prune_{timestamp}.json.gz"
    filepath = _SNAPSHOTS_DIR / filename

    payload = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "snapshot_type": "pre_prune",
        "mu_count": len(mus),
        "edge_count": len(edges),
        "memory_units": mus,
        "edges": edges,
    }

    with gzip.open(filepath, "wt", encoding="utf-8") as f:
        json.dump(payload, f)

    logger.info(
        "snapshot: pre-pruning snapshot -> %s (%d MUs %d edges)",
        filename,
        len(mus),
        len(edges),
    )
    return str(filepath)


async def take_archive_snapshot(experiment_id: Optional[str] = None) -> str:
    """
    Archive the full current graph state, keyed by experiment_id.

    Called by POST /admin/graph/wipe immediately before deleting rows, so
    the pre-wipe graph state is never silently lost — the experiment
    harness (M19) archives-then-wipes rather than wiping directly.

    Written to: /app/data/snapshots/archive_{experiment_id}_{ts}.json.gz
    """
    _SNAPSHOTS_DIR.mkdir(parents=True, exist_ok=True)

    mus = await graph_store.get_all_mus()
    edges = await graph_store.get_all_edges()

    label = _sanitize_for_filename(experiment_id) if experiment_id else "unlabeled"
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    filename = f"archive_{label}_{timestamp}.json.gz"
    filepath = _SNAPSHOTS_DIR / filename

    payload = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "snapshot_type": "archive",
        "experiment_id": experiment_id,
        "mu_count": len(mus),
        "edge_count": len(edges),
        "memory_units": mus,
        "edges": edges,
    }

    with gzip.open(filepath, "wt", encoding="utf-8") as f:
        json.dump(payload, f)

    logger.info(
        "snapshot: archive snapshot experiment_id=%s -> %s (%d MUs, %d edges)",
        experiment_id,
        filename,
        len(mus),
        len(edges),
    )
    return str(filepath)
