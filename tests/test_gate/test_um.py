"""
Tests for gate/um.py and gate/audit.py

Covers: UniversalMessage schema validation, create_um factory,
        append_audit JSONL write/parse round-trip.
"""

import json
import sys
import os
import tempfile
from pathlib import Path
from unittest import mock

# Add backend to path
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "backend"))

from gate.um import UniversalMessage, create_um
import gate.audit as audit_module


class TestCreateUm:

    def test_create_um_returns_universal_message(self):
        um = create_um(
            content="Hello",
            source_type="user_input",
            boundary="user_input→agent",
            direction="in",
            series_id="session-abc",
        )

        print(f"[test] create_um: mu_id={um.mu_id}, source_type={um.source_type}")
        assert isinstance(um, UniversalMessage)

    def test_create_um_has_all_required_fields(self):
        um = create_um(
            content="Test content",
            source_type="agent_output",
            boundary="llm→agent",
            direction="in",
            series_id="sess-123",
        )

        print(f"[test] required fields: mu_id={um.mu_id}, created_at={um.created_at}")
        assert um.mu_id  # non-empty UUID string
        assert um.created_at  # ISO8601 timestamp
        assert um.source_type == "agent_output"
        assert um.raw_content == "Test content"
        assert um.boundary == "llm→agent"
        assert um.direction == "in"
        assert um.series_id == "sess-123"

    def test_create_um_mu_id_is_unique(self):
        um1 = create_um("a", "user_input", "b1", "in", "s1")
        um2 = create_um("a", "user_input", "b1", "in", "s1")

        print(f"[test] unique IDs: {um1.mu_id} vs {um2.mu_id}")
        assert um1.mu_id != um2.mu_id

    def test_create_um_flags_default_to_empty_list(self):
        um = create_um("content", "prompt", "agent→llm", "out", "sess-xyz")

        print(f"[test] default flags: {um.flags}")
        assert um.flags == []

    def test_create_um_accepts_flags(self):
        flags = ["injection_pattern:foo", "length_exceeded:4097>4096"]
        um = create_um(
            content="content",
            source_type="user_input",
            boundary="test",
            direction="in",
            series_id="s",
            flags=flags,
        )

        print(f"[test] supplied flags: {um.flags}")
        assert um.flags == flags

    def test_create_um_accepts_channel_id(self):
        um = create_um(
            content="x",
            source_type="system",
            boundary="test",
            direction="out",
            series_id="s",
            channel_id="chan-1",
        )

        print(f"[test] channel_id: {um.channel_id}")
        assert um.channel_id == "chan-1"

    def test_um_source_type_must_be_valid(self):
        import pytest
        with pytest.raises(Exception):
            UniversalMessage(
                source_type="invalid_type",
                raw_content="x",
                boundary="test",
                direction="in",
                series_id="s",
            )


class TestAppendAudit:

    def test_append_audit_writes_parseable_jsonl(self):
        um = create_um(
            content="audit test content",
            source_type="user_input",
            boundary="user_input→agent",
            direction="in",
            series_id="audit-session",
        )

        with tempfile.TemporaryDirectory() as tmpdir:
            log_path = Path(tmpdir) / "audit.log"

            # Patch the audit log path
            with mock.patch.object(audit_module, "_AUDIT_LOG_PATH", log_path):
                audit_module.append_audit(um)

            assert log_path.exists(), "audit.log was not created"
            line = log_path.read_text(encoding="utf-8").strip()
            print(f"[test] audit line: {line}")

            parsed = json.loads(line)
            assert parsed["mu_id"] == um.mu_id
            assert parsed["source_type"] == "user_input"
            assert parsed["raw_content"] == "audit test content"

    def test_append_audit_appends_multiple_records(self):
        um1 = create_um("first", "user_input", "b", "in", "s")
        um2 = create_um("second", "agent_output", "b", "in", "s")

        with tempfile.TemporaryDirectory() as tmpdir:
            log_path = Path(tmpdir) / "audit.log"

            with mock.patch.object(audit_module, "_AUDIT_LOG_PATH", log_path):
                audit_module.append_audit(um1)
                audit_module.append_audit(um2)

            lines = log_path.read_text(encoding="utf-8").strip().splitlines()
            print(f"[test] line count: {len(lines)}")
            assert len(lines) == 2

            ids = {json.loads(l)["mu_id"] for l in lines}
            assert um1.mu_id in ids
            assert um2.mu_id in ids

    def test_append_audit_does_not_raise_on_bad_path(self):
        """
        If the path is unwriteable, append_audit must log and continue
        rather than crashing. We simulate this by patching the path to
        a directory that can't be created.
        """
        um = create_um("x", "system", "test", "out", "s")

        # Patch to a path where mkdir will fail
        bad_path = Path("/nonexistent/readonly/dir/audit.log")
        with mock.patch.object(audit_module, "_AUDIT_LOG_PATH", bad_path):
            # Should NOT raise
            audit_module.append_audit(um)


# ---------------------------------------------------------------------------
# Milestone 12 — series_id on UniversalMessage
# ---------------------------------------------------------------------------


class TestCreateUmM12:

    def test_create_um_with_series_id_includes_it_in_returned_um(self):
        """create_um() with series_id stores it on the returned UniversalMessage."""
        sid = "series-m12-test"
        um = create_um(
            content="hello m12",
            source_type="user_input",
            boundary="user_input→agent",
            direction="in",
            series_id=sid,
        )
        print(f"[test] um.series_id={um.series_id}")
        assert um.series_id == sid

    def test_create_um_with_experiment_id_includes_it_in_returned_um(self):
        """create_um() with experiment_id stores it on the returned UniversalMessage."""
        um = create_um(
            content="hello",
            source_type="agent_output",
            boundary="llm→agent",
            direction="in",
            series_id="s",
            experiment_id="exp-2026",
        )
        print(f"[test] um.experiment_id={um.experiment_id}")
        assert um.experiment_id == "exp-2026"

    def test_create_um_experiment_id_defaults_to_none(self):
        """experiment_id is None when not supplied to create_um()."""
        um = create_um(
            content="x",
            source_type="system",
            boundary="test",
            direction="out",
            series_id="s",
        )
        print(f"[test] um.experiment_id={um.experiment_id!r}")
        assert um.experiment_id is None
