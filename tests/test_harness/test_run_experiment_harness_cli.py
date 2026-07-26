"""
Tests for scripts/run_experiment_harness.py's CLI-level error handling.

Regression coverage for a bug found during the 2026-07-26 smoketest: the CLI
called generate_bank_for_plan() at two sites and caught neither of
prompt_gen.py's two operator-facing exceptions, so declining the cost gate —
a deliberate operator choice — printed a raw traceback, as did a missing
ANTHROPIC_API_KEY. Both are expected outcomes, and a stack trace directly
after a cost-confirmation prompt reads as a crash mid-API-call, leaving the
operator unsure whether spend or a file write happened.

No Anthropic client is constructed here: generate_bank_for_plan is
monkey-patched to raise, since what's under test is the CLI's handling of the
exception, not the generation path itself (covered in test_prompt_gen.py).
"""

import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "scripts"))

import run_experiment_harness as cli
from harness.prompt_gen import GenerationAborted, MissingApiKeyError
from harness.schema import RunPlan

FIXTURES_DIR = Path(__file__).parent.parent.parent / "seed_fixtures"


def _plan() -> RunPlan:
    return RunPlan(
        plan_id="cli-error-handling-test",
        plan_name="CLI error handling test",
        conceptual_fields={"count": 4, "subjects": "auto"},
        series_count=1,
        turns_per_series=4,
        seed_fixture_version="v3",
    )


class TestGenerateBankOrExit:
    def test_declined_cost_gate_exits_1_without_traceback(self, monkeypatch, capsys):
        """A declined cost gate must exit cleanly, not propagate GenerationAborted."""
        def _raise(*args, **kwargs):
            raise GenerationAborted("user declined the cost-confirmation prompt")

        monkeypatch.setattr(cli, "generate_bank_for_plan", _raise)

        with pytest.raises(SystemExit) as exc_info:
            cli.generate_bank_or_exit(_plan(), FIXTURES_DIR / "seed_v3.json.gz", Path("prompt_banks"))

        assert exc_info.value.code == 1
        out = capsys.readouterr().out
        assert "declined" in out.lower()
        assert "Traceback" not in out

    def test_missing_api_key_exits_1_with_the_setup_message(self, monkeypatch, capsys):
        """The missing-key message must reach the operator as a plain ERROR line
        (docs/harness_usage.md's troubleshooting table quotes it)."""
        def _raise(*args, **kwargs):
            raise MissingApiKeyError("ANTHROPIC_API_KEY is not set. Export it before ...")

        monkeypatch.setattr(cli, "generate_bank_for_plan", _raise)

        with pytest.raises(SystemExit) as exc_info:
            cli.generate_bank_or_exit(_plan(), FIXTURES_DIR / "seed_v3.json.gz", Path("prompt_banks"))

        assert exc_info.value.code == 1
        out = capsys.readouterr().out
        assert "ERROR: ANTHROPIC_API_KEY is not set" in out
        assert "Traceback" not in out

    def test_returns_the_bank_on_success(self, monkeypatch):
        """The wrapper must be transparent on the happy path — the caller at the
        run path needs the returned bank to stamp plan.prompt_bank_path."""
        sentinel = object()
        monkeypatch.setattr(cli, "generate_bank_for_plan", lambda *a, **k: sentinel)

        result = cli.generate_bank_or_exit(
            _plan(), FIXTURES_DIR / "seed_v3.json.gz", Path("prompt_banks")
        )

        assert result is sentinel
