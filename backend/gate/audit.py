"""
Audit log — append-only JSONL writer.

Every UM record that passes through the gate gets a line written here.
The file is opened in append mode on every call; never truncated.
Path is /app/data/audit.log inside the container (Docker volume).
"""

import json
import logging
import os
from pathlib import Path

from gate.um import UniversalMessage

logger = logging.getLogger(__name__)

_AUDIT_LOG_PATH = Path(os.getenv("MEMORY_PATH", "/app/data")) / "audit.log"


def append_audit(um: UniversalMessage) -> None:
    """
    Append a single UM record as a JSONL line to the audit log.

    Creates parent directories if they don't exist. On any write error, logs
    the exception but does NOT raise — a failed audit write must never kill a
    live interaction.
    """
    try:
        _AUDIT_LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
        line = um.model_dump_json() + "\n"
        with open(_AUDIT_LOG_PATH, "a", encoding="utf-8") as f:
            f.write(line)
        logger.debug(
            "audit: wrote mu_id=%s source_type=%s boundary=%s",
            um.mu_id,
            um.source_type,
            um.boundary,
        )
    except Exception as exc:
        logger.error(
            "audit: failed to write mu_id=%s — %s",
            um.mu_id,
            exc,
        )
