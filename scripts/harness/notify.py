"""
Notification stub — channel-agnostic interface for run-completion events.

Default implementation logs to console + appends to a local file. Makes
no network calls and requires no credentials. Real notification channels
(email, Slack, etc.) are explicitly deferred to Phase 3.
"""

import json
import logging
from pathlib import Path
from typing import Optional

from harness.manifest import now_iso, run_dir_for

logger = logging.getLogger("harness.notify")


def build_notification_payload(
    run_id: str,
    status: str,
    error_summary: Optional[str] = None,
    report_path: Optional[str] = None,
) -> dict:
    return {
        "run_id": run_id,
        "status": status,
        "error_summary": error_summary,
        "report_path": report_path,
    }


def notify(event: str, payload: dict, base_dir: Path) -> Path:
    """
    Log the payload to console (INFO) and append it as a JSONL line to
    {base_dir}/{run_id}/notifications.log. No network calls.
    """
    logger.info("notify: event=%s payload=%s", event, payload)

    run_id = payload.get("run_id", "unknown")
    log_path = run_dir_for(run_id, base_dir) / "notifications.log"
    log_path.parent.mkdir(parents=True, exist_ok=True)

    record = {"event": event, "timestamp": now_iso(), **payload}
    with open(log_path, "a", encoding="utf-8") as f:
        f.write(json.dumps(record) + "\n")

    return log_path
