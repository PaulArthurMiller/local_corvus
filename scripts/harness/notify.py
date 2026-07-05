"""
Notification stub — channel-agnostic interface for run-completion events.

Default implementation logs to console + appends to a local file. Makes
no network calls and requires no credentials.

M19 Phase 3 — notify() is now a dispatch table keyed by
NotificationConfig.channel. "log" is the original Phase 0 behavior,
unchanged. "email"/"webhook" are validated at the schema level
(schema.VALID_NOTIFICATION_CHANNELS) but not yet implemented — they log a
warning and fall back to "log" verbatim, so adding a real channel later is
one new handler function, no refactor of call sites.
"""

import json
import logging
from pathlib import Path
from typing import Callable, Optional

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


def _notify_log(event: str, payload: dict, base_dir: Path) -> Path:
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


def _make_unimplemented_channel_handler(channel_name: str) -> Callable[[str, dict, Path], Path]:
    def _handler(event: str, payload: dict, base_dir: Path) -> Path:
        logger.warning(
            "notify: channel %r not implemented, falling back to log", channel_name
        )
        return _notify_log(event, payload, base_dir)

    return _handler


_DISPATCH: dict = {
    "log": _notify_log,
    "email": _make_unimplemented_channel_handler("email"),
    "webhook": _make_unimplemented_channel_handler("webhook"),
}


def notify(event: str, payload: dict, base_dir: Path, channel: str = "log") -> Path:
    """
    Dispatch to the handler for `channel` (schema validation already
    guarantees channel is one of VALID_NOTIFICATION_CHANNELS by the time a
    RunPlan reaches here; unrecognized values fall back to "log" rather
    than raising, since a notification failure should never be why a
    completed run reports as aborted).
    """
    handler = _DISPATCH.get(channel, _notify_log)
    return handler(event, payload, base_dir)
