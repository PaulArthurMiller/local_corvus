"""Tests for scripts/harness/notify.py — channel dispatch (M19 Phase 3).
"log" is the original Phase 0 behavior; "email"/"webhook" fall back to it
with a logged warning since neither is implemented yet."""

import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "scripts"))

from harness.notify import build_notification_payload, notify


def _payload(run_id="run-1"):
    return build_notification_payload(run_id=run_id, status="complete", error_summary=None, report_path="/reports/x.md")


def test_log_channel_writes_jsonl_line(tmp_path):
    log_path = notify("harness_run_complete", _payload(), tmp_path, channel="log")

    assert log_path.exists()
    lines = log_path.read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) == 1
    record = json.loads(lines[0])
    assert record["event"] == "harness_run_complete"
    assert record["run_id"] == "run-1"
    assert record["report_path"] == "/reports/x.md"


def test_email_channel_falls_back_to_log(tmp_path, caplog):
    with caplog.at_level("WARNING"):
        log_path = notify("harness_run_complete", _payload(), tmp_path, channel="email")

    assert log_path.exists()  # the log fallback still wrote the JSONL line
    assert any("not implemented" in r.message for r in caplog.records)


def test_webhook_channel_falls_back_to_log(tmp_path, caplog):
    with caplog.at_level("WARNING"):
        log_path = notify("harness_run_complete", _payload(), tmp_path, channel="webhook")

    assert log_path.exists()
    assert any("not implemented" in r.message for r in caplog.records)


def test_default_channel_is_log(tmp_path):
    log_path = notify("harness_run_complete", _payload(), tmp_path)

    assert log_path.exists()
