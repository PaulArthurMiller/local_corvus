"""
Tests for scripts/harness/calibration.py — the Milestone 37 per-criterion
agreement summary.

Pure aggregation over TurnCheckpoint rows, so everything here runs against
fabricated checkpoints with no runner, no backend, and no judge. That is the
point of the module boundary: the summary can also be recomputed after the
fact from a finished pass's checkpoint.json.
"""

import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "scripts"))

from harness.calibration import (
    CALIBRATION_SUMMARY_FILENAME,
    collect_calibration_record,
    render_calibration_summary,
    write_calibration_summary,
)
from harness.checkpoint import STATUS_OK, TurnCheckpoint
from harness.judge import (
    EVIDENCE_ANSWER_TOKEN_ABSENT,
    EVIDENCE_DEMOTED_NO_QUOTE,
    EVIDENCE_UNVERIFIED_QUOTE,
)


def _turn(**overrides):
    kwargs = dict(
        condition="learned", series_index=0, turn_index=0,
        turn_type="bulk", status=STATUS_OK,
    )
    kwargs.update(overrides)
    return TurnCheckpoint(**kwargs)


def test_agreement_is_counted_per_criterion_not_per_turn():
    """
    The measurement M37 exists to add. One turn carrying three criteria is
    three observations — on the current bank that is ~2.4x the turns, which is
    what makes a 20-turn pass informative rather than an uninformative
    override rate.
    """
    turns = [
        _turn(
            judge_model_verdict="up", verdict="none",
            judge_model_criteria_met=[True, True, True],
            criteria_met=[True, False, True],
        ),
    ]

    record = collect_calibration_record(turns)

    assert record.turns_judged == 1
    assert record.criteria_graded == 3
    assert record.criteria_agreed == 2
    assert record.criterion_agreement_rate == pytest.approx(2 / 3)


def test_disagreement_direction_is_counted_separately():
    """
    Direction is the finding, not the rate. All three overrides in the
    2026-08-04 audit were lenient — the judge crediting a criterion on a
    looser standard than it states — and a bare disagreement count would
    have hidden that.
    """
    turns = [
        _turn(judge_model_verdict="up", verdict="down",
              judge_model_criteria_met=[True, True], criteria_met=[False, False]),
        _turn(judge_model_verdict="down", verdict="up",
              judge_model_criteria_met=[False], criteria_met=[True]),
    ]

    record = collect_calibration_record(turns)

    assert record.criteria_judge_lenient == 2
    assert record.criteria_judge_strict == 1


def test_regraded_and_confirmed_turns_are_distinguished():
    """
    A regrade that changed nothing is an affirmation of the judge, not a
    correction — pooling the two would overstate how often the operator
    intervened.
    """
    turns = [
        _turn(judge_model_verdict="up", verdict="up",
              judge_model_criteria_met=[True, True], criteria_met=[True, True]),
        _turn(judge_model_verdict="up", verdict="none",
              judge_model_criteria_met=[True, True], criteria_met=[True, False]),
    ]

    record = collect_calibration_record(turns)

    assert record.turns_confirmed == 1
    assert record.turns_regraded == 1


def test_direct_verdict_override_is_counted_apart_from_criterion_data():
    """
    A verdict override leaves the checklist untouched, so scoring it as full
    per-criterion agreement would report the judge as agreeing on every
    criterion of a turn the operator just overturned.
    """
    turns = [
        _turn(judge_model_verdict="up", verdict="none",
              judge_model_criteria_met=None, criteria_met=[True]),
    ]

    record = collect_calibration_record(turns)

    assert record.turns_verdict_overridden == 1
    assert record.criteria_graded == 0
    assert record.criterion_agreement_rate is None


def test_unjudged_turns_are_excluded_entirely():
    """
    Probes and failed chats never reach the judge. Counting them would dilute
    every rate in the summary with turns nobody graded.
    """
    turns = [
        _turn(turn_type="probe", probe_id="p06"),
        _turn(status="error_soft_continue"),
        _turn(judge_model_verdict="up", verdict="up",
              judge_model_criteria_met=[True], criteria_met=[True]),
    ]

    record = collect_calibration_record(turns)

    assert record.turns_judged == 1
    assert record.criteria_graded == 1


def test_skipped_turns_count_as_judged_but_record_no_verdict():
    turns = [
        _turn(judge_model_verdict="up", verdict=None,
              judge_error="verdict skipped by operator (--interactive)",
              judge_model_criteria_met=[True], criteria_met=[True]),
    ]

    record = collect_calibration_record(turns)

    assert record.turns_judged == 1
    assert record.turns_skipped == 1
    assert record.recorded_verdicts == {}
    assert record.judge_verdicts == {"up": 1}


def test_judge_alone_up_rate_tracks_the_model_not_the_record():
    """
    M36's distinction, applied at the source: the judge-alone rate is the one
    that describes an unattended run, and it diverges from the recorded rate
    exactly when the operator intervened.
    """
    turns = [
        _turn(judge_model_verdict="up", verdict="up",
              judge_model_criteria_met=[True], criteria_met=[True]),
        _turn(judge_model_verdict="up", verdict="down",
              judge_model_criteria_met=[True], criteria_met=[False]),
    ]

    record = collect_calibration_record(turns)

    assert record.judge_up_rate == pytest.approx(1.0)
    assert record.recorded_verdicts == {"up": 1, "down": 1}


def test_evidence_flags_and_regimes_are_tallied():
    turns = [
        _turn(judge_model_verdict="none", verdict="none",
              judge_model_criteria_met=[False, True], criteria_met=[False, True],
              evidence_enforced=True,
              evidence_flags=[[EVIDENCE_DEMOTED_NO_QUOTE], [EVIDENCE_ANSWER_TOKEN_ABSENT]]),
        _turn(judge_model_verdict="up", verdict="up",
              judge_model_criteria_met=[True], criteria_met=[True],
              evidence_enforced=True, evidence_flags=[[EVIDENCE_UNVERIFIED_QUOTE]]),
        _turn(judge_model_verdict="up", verdict="up",
              judge_model_criteria_met=[True], criteria_met=[True],
              evidence_enforced=False),
    ]

    record = collect_calibration_record(turns)

    assert record.evidence_enforced_turns == 2
    assert record.evidence_unenforced_turns == 1
    assert record.flag_counts[EVIDENCE_DEMOTED_NO_QUOTE] == 1
    assert record.flag_counts[EVIDENCE_ANSWER_TOKEN_ABSENT] == 1
    assert record.flag_counts[EVIDENCE_UNVERIFIED_QUOTE] == 1


def test_summary_reports_counts_alongside_every_rate():
    """
    At a 20-turn pass every percentage here has a wide interval, and a bare
    percentage invites reading it as precise — the 2026-08-04 audit's own
    "3/20 = 15%, CI 5-36%" lesson.
    """
    turns = [
        _turn(judge_model_verdict="up", verdict="none",
              judge_model_criteria_met=[True, True], criteria_met=[True, False]),
    ]

    text = render_calibration_summary(collect_calibration_record(turns), "run-abc")

    assert "run-abc" in text
    assert "Criteria graded: **2**" in text
    assert "1/2 (50.0%)" in text
    assert "Judge lenient (judge met, operator not met): **1**" in text


def test_summary_renders_with_no_judged_turns():
    """
    An aborted pass that never reached a bulk turn still gets a summary
    rather than a ZeroDivisionError — the run it describes is exactly the
    one nobody wants to reconstruct by hand.
    """
    text = render_calibration_summary(collect_calibration_record([]), "run-empty")

    assert "n/a" in text
    assert "none recorded" in text


def test_summary_is_written_next_to_the_checkpoint(tmp_path):
    turns = [
        _turn(judge_model_verdict="up", verdict="up",
              judge_model_criteria_met=[True], criteria_met=[True]),
    ]

    path = write_calibration_summary(collect_calibration_record(turns), "run-xyz", tmp_path)

    assert path.name == CALIBRATION_SUMMARY_FILENAME
    assert path.exists()
    assert "run-xyz" in path.read_text(encoding="utf-8")
