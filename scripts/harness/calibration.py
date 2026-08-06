"""
Calibration summary (Milestone 37) — what a --interactive pass measured.

A calibration pass exists to answer one question: where and in which
direction is the judge wrong? Milestone 36 established that a number nobody
reports is a number nobody has — its whole trigger was three operator
overrides that appeared in no report — so the per-criterion agreement data
M37 collects gets its own artifact rather than living only as fields in
checkpoint.json.

This module is pure aggregation over TurnCheckpoint rows: no I/O beyond the
one write, no backend calls, no dependency on the runner. That keeps it
testable against fabricated checkpoints and re-runnable after the fact
against a checkpoint from a pass that has already finished.

Deliberately harness-side rather than a report-generator section: it reads
per-criterion data that (per M31) is never sent to the backend, and it
describes an operator-in-the-loop calibration pass, not an experiment.
"""

import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from harness.checkpoint import JUDGE_ERROR_RECORDED_NONE
from harness.judge import (
    EVIDENCE_ANSWER_TOKEN_ABSENT,
    EVIDENCE_DEMOTED_NO_QUOTE,
    EVIDENCE_UNVERIFIED_QUOTE,
)
from harness.manifest import run_dir_for

logger = logging.getLogger("harness.calibration")

CALIBRATION_SUMMARY_FILENAME = "calibration_summary.md"


@dataclass
class CalibrationRecord:
    """
    Aggregated agreement between the judge and the operator over one pass.

    The two layers are counted separately on purpose. Verdict-level counts
    describe what the run recorded; criterion-level counts are the
    measurement M37 added, because a turn's verdict can agree while the
    criteria underneath it disagree in both directions at once — and it is
    the criterion layer the 2026-08-04 audit found to be wrong.
    """

    turns_judged: int = 0
    turns_regraded: int = 0  # operator changed at least one criterion
    turns_confirmed: int = 0  # operator regraded and agreed with every criterion
    turns_verdict_overridden: int = 0  # direct verdict override, no per-criterion data
    turns_skipped: int = 0
    # The judge produced no verdict at all and something else supplied one:
    # the operator by hand, or the record_none policy. Added 2026-08-06 —
    # the pass that day had one such turn, and because it matched no bucket
    # the turn-level outcomes summed to 19 of 20 while claiming to account
    # for the pass. A summary whose parts do not add up to its whole is one
    # nobody can check.
    turns_hand_graded: int = 0
    turns_recorded_none: int = 0

    criteria_graded: int = 0
    criteria_agreed: int = 0
    # Direction is the finding, not the rate: "lenient" is the judge marking a
    # criterion met that the operator did not, which is the failure mode the
    # 2026-08-04 audit found in all three of its overrides.
    criteria_judge_lenient: int = 0
    criteria_judge_strict: int = 0

    recorded_verdicts: dict = field(default_factory=dict)
    judge_verdicts: dict = field(default_factory=dict)

    # Host-side evidence checks (see judge._check_evidence). demoted_no_quote
    # already changed a judgment; the other two are flags awaiting exactly
    # this measurement before anyone decides whether to enforce them.
    evidence_enforced_turns: int = 0
    evidence_unenforced_turns: int = 0
    flag_counts: dict = field(default_factory=dict)

    @property
    def turns_accounted(self) -> int:
        """
        Turns landing in one of the outcome buckets. Equals turns_judged when
        the accounting is complete; the summary prints both so a future gap
        announces itself instead of having to be found by subtraction.
        """
        return (
            self.turns_regraded
            + self.turns_confirmed
            + self.turns_verdict_overridden
            + self.turns_skipped
            + self.turns_hand_graded
            + self.turns_recorded_none
        )

    @property
    def turns_judge_produced_no_verdict(self) -> int:
        """Turns the judge-alone verdict mix cannot include, because there is
        no judge verdict to count — its denominator is turns_judged minus
        this."""
        return self.turns_hand_graded + self.turns_recorded_none

    @property
    def criterion_agreement_rate(self) -> Optional[float]:
        if not self.criteria_graded:
            return None
        return self.criteria_agreed / self.criteria_graded

    @property
    def judge_up_rate(self) -> Optional[float]:
        """
        Up-rate over the judge's own verdicts — the number that describes an
        unattended run (M36's distinction, applied here at the source).
        """
        total = sum(self.judge_verdicts.values())
        if not total:
            return None
        return self.judge_verdicts.get("up", 0) / total


def collect_calibration_record(turns: list) -> CalibrationRecord:
    """
    Aggregate a checkpoint's turns into a CalibrationRecord.

    Accepts TurnCheckpoint objects (attribute access) so it can run over a
    live checkpoint mid-run or a loaded one afterwards. Turns with no judging
    at all — probes, chat failures, unjudged bulk turns — are skipped
    entirely rather than counted as agreements, which would dilute every rate
    below with turns nobody graded.
    """
    record = CalibrationRecord()
    for turn in turns:
        judge_verdict = getattr(turn, "judge_model_verdict", None)
        verdict = getattr(turn, "verdict", None)
        judge_criteria = getattr(turn, "judge_model_criteria_met", None)
        criteria = getattr(turn, "criteria_met", None)
        if judge_verdict is None and verdict is None:
            continue

        record.turns_judged += 1
        if judge_verdict is not None:
            record.judge_verdicts[judge_verdict] = record.judge_verdicts.get(judge_verdict, 0) + 1
        if verdict is not None:
            record.recorded_verdicts[verdict] = record.recorded_verdicts.get(verdict, 0) + 1
        else:
            record.turns_skipped += 1

        enforced = getattr(turn, "evidence_enforced", None)
        if enforced is True:
            record.evidence_enforced_turns += 1
        elif enforced is False:
            record.evidence_unenforced_turns += 1
        for flags in (getattr(turn, "evidence_flags", None) or []):
            for flag in flags:
                record.flag_counts[flag] = record.flag_counts.get(flag, 0) + 1

        # Turns the judge never graded, where a verdict was recorded anyway.
        # Checked before the criterion comparison because both leave every
        # checklist field None, so they would otherwise fall through every
        # branch below and be counted nowhere.
        judge_error = getattr(turn, "judge_error", None) or ""
        if judge_verdict is None and verdict is not None:
            if judge_error.startswith(JUDGE_ERROR_RECORDED_NONE):
                record.turns_recorded_none += 1
            else:
                record.turns_hand_graded += 1
            continue

        # Per-criterion agreement needs both checklists at equal length. A
        # direct verdict override leaves criteria_met untouched, so it
        # contributes no criterion observations — counted on its own line
        # rather than silently scored as full agreement.
        if (
            judge_criteria is not None
            and criteria is not None
            and len(judge_criteria) == len(criteria)
        ):
            changed = False
            for judged, final in zip(judge_criteria, criteria):
                record.criteria_graded += 1
                if judged == final:
                    record.criteria_agreed += 1
                    continue
                changed = True
                if judged and not final:
                    record.criteria_judge_lenient += 1
                else:
                    record.criteria_judge_strict += 1
            if changed:
                record.turns_regraded += 1
            else:
                record.turns_confirmed += 1
        elif verdict is not None and judge_verdict is not None and verdict != judge_verdict:
            record.turns_verdict_overridden += 1

    return record


def _rate(numerator: int, denominator: int) -> str:
    if not denominator:
        return "n/a"
    return f"{numerator}/{denominator} ({numerator / denominator:.1%})"


def _verdict_mix(counts: dict) -> str:
    total = sum(counts.values())
    if not total:
        return "none recorded"
    parts = [f"{counts.get(v, 0)} {v}" for v in ("up", "down", "none") if counts.get(v)]
    up_rate = counts.get("up", 0) / total
    return f"{' / '.join(parts)} — up-rate {up_rate:.1%} of {total}"


def render_calibration_summary(record: CalibrationRecord, run_id: str) -> str:
    """
    Render the record as Markdown.

    Ordered by what the reader needs first: the per-criterion agreement (the
    measurement), then its direction, then the verdict-level view for
    continuity with M36's §5.2, then the evidence checks. Rates are always
    shown with their raw counts — at a 20-turn pass every percentage here has
    a wide interval, and a bare percentage invites reading it as precise.
    """
    flagged_total = record.flag_counts.get(EVIDENCE_UNVERIFIED_QUOTE, 0) + record.flag_counts.get(
        EVIDENCE_ANSWER_TOKEN_ABSENT, 0
    )
    lines = [
        f"# Judge calibration summary — `{run_id}`",
        "",
        "Per-criterion agreement between the judge model and the operator, from one",
        "`--interactive` pass. Counts accompany every rate deliberately: at calibration",
        "sample sizes the intervals are wide, and the composition of the disagreements",
        "is the finding, not the rate.",
        "",
        "## Per-criterion agreement (the measurement)",
        "",
        f"- Criteria graded: **{record.criteria_graded}** across {record.turns_judged} judged turn(s)",
        f"- Agreement: **{_rate(record.criteria_agreed, record.criteria_graded)}**",
        f"- Judge lenient (judge met, operator not met): **{record.criteria_judge_lenient}**",
        f"- Judge strict (operator met, judge not met): **{record.criteria_judge_strict}**",
        "",
        "A lenient/strict split skewed to one side is a systematic grading standard",
        "problem; a balanced split at a similar total is noise in a hard judgment call.",
        "",
        "## Turn-level outcomes",
        "",
        f"- Regraded (at least one criterion changed): {record.turns_regraded}",
        f"- Confirmed as graded (regraded, no change): {record.turns_confirmed}",
        f"- Verdict overridden directly (no per-criterion data): {record.turns_verdict_overridden}",
        f"- Skipped by operator: {record.turns_skipped}",
        f"- Hand-graded after an unparseable judge response: {record.turns_hand_graded}",
        f"- Recorded as none after an unparseable judge response: {record.turns_recorded_none}",
        f"- **Accounted: {record.turns_accounted} of {record.turns_judged} judged turn(s)**",
        "",
        "The last two are turns the judge never graded. Unattended they are the",
        "`record_none` line, and their rate is the share of a real run's turns that carry",
        "no judgment — worth watching on its own, since nothing else in this file",
        "distinguishes them from turns that were graded and came out mixed.",
        "",
        "## Verdict mix",
        "",
        f"- Recorded (what the run stored): {_verdict_mix(record.recorded_verdicts)}",
        f"- Judge alone (what an unattended run would store): {_verdict_mix(record.judge_verdicts)}",
        f"- Excluded from judge-alone (judge produced no verdict): "
        f"{record.turns_judge_produced_no_verdict}",
        "",
        "\"Judge alone\" is measured after the host-side evidence checks below, because",
        "those run unattended too — it is the real no-operator number, not the model's raw",
        "claim. The count of criteria forced false is what separates the two. Its",
        "denominator is smaller than the recorded mix's by exactly the excluded count",
        "above — compare the two as rates, never as counts.",
        "",
        "## Evidence checks",
        "",
        f"- Turns with evidence enforced: {record.evidence_enforced_turns}",
        f"- Turns where the judge omitted the quote field (enforcement skipped): "
        f"{record.evidence_unenforced_turns}",
        f"- Criteria forced false for a missing quote: "
        f"{record.flag_counts.get(EVIDENCE_DEMOTED_NO_QUOTE, 0)}",
        f"- Criteria flagged — quote not found in the response: "
        f"{record.flag_counts.get(EVIDENCE_UNVERIFIED_QUOTE, 0)}",
        f"- Criteria flagged — required answer absent from the response: "
        f"{record.flag_counts.get(EVIDENCE_ANSWER_TOKEN_ABSENT, 0)}",
        "",
        f"The last two rows are advisory: {flagged_total} criterion/criteria flagged, none",
        "demoted. They are heuristics over normalized text, and this pass is what says",
        "whether they fire often enough, and cleanly enough, to be worth promoting into",
        "enforcement. On 2026-08-06 the answer was no — the single flag that fired was on",
        "a criterion the operator confirmed met, so enforcing it would have flipped a",
        "correct verdict; and it caught none of that pass's three real (lenient) errors,",
        "whose quotes were genuinely present in the response and simply did not support",
        "the criterion.",
        "",
    ]
    return "\n".join(lines)


def write_calibration_summary(record: CalibrationRecord, run_id: str, base_dir: Path) -> Path:
    """Write the summary next to the run's checkpoint; returns the path."""
    path = run_dir_for(run_id, base_dir) / CALIBRATION_SUMMARY_FILENAME
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(render_calibration_summary(record, run_id), encoding="utf-8")
    logger.info("calibration: wrote summary to %s", path)
    return path
