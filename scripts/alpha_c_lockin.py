"""
Plan Concept Alpha item C — early-feedback lottery / lock-in analysis.

Answers: is the eventual cluster winner effectively decided in session 1?

Reads archived experiment report JSONs only (no DB, no new runs) and computes,
per run x cluster slot:

  - session-1 sample-mode winner (and its tie set)
  - final-session (session-5) winner (and its tie set)
  - whether each matches the trained spoke
  - whether the final winner preserves the session-1 winner

Headline is P(final winner = session-1 winner) against the ~1/7 chance level
(7 spokes per cluster). Item C.c additionally requires that headline split by
condition (baseline vs. cfgA vs. cfgB), which is where the interesting
signal turned out to live.

NOT computed here: "first spoke to receive an upvote-driven edge increase."
That column needs per-update `weight_updates` rows (source/target MU ids,
feedback_event_id, turn ordering), which live in the run store and are NOT in
the archived reports — the reports only carry aggregated per-edge deltas
without endpoints. See `first_upvoted_spoke_available()` below; filling that
column requires the backend DB to be up.

Usage:  python scripts/alpha_c_lockin.py [--out docs/evaluations/<file>.md]
"""

from __future__ import annotations

import argparse
import json
import logging
import sqlite3
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

import yaml

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
logger = logging.getLogger("alpha_c_lockin")

REPO_ROOT = Path(__file__).resolve().parent.parent
REPORT_DIR = REPO_ROOT / "memory" / "reports"
GRAPH_DB = REPO_ROOT / "memory" / "graph.db"
PROBE_SET = REPO_ROOT / "probes" / "probe_set_v3.yaml"

# Spokes per cluster — sets the chance level for "final winner = session-1 winner".
SPOKES_PER_CLUSTER = 7
CHANCE_LEVEL = 1.0 / SPOKES_PER_CLUSTER

# The pre-registered scope: 8 learned runs (exp0b x3, exp01 x5).
# exp0-2026-07-26 is structurally identical but predates the judge calibration
# passes (2026-07-27 .. 2026-08-05), so it is reported separately rather than
# pooled into the headline.
RUNS: list[tuple[str, str, str]] = [
    # (report basename, condition group, short label)
    ("exp0b-2026-08-06", "baseline", "exp0b rep1"),
    ("exp0b-rep2-2026-08-09", "baseline", "exp0b rep2"),
    ("exp0b-rep3-2026-08-09", "baseline", "exp0b rep3"),
    ("exp01-cfgA-rep1-2026-08-12", "cfgA", "cfgA rep1"),
    ("exp01-cfgA-rep2-2026-08-12", "cfgA", "cfgA rep2"),
    ("exp01-cfgA-rep3-2026-08-17", "cfgA", "cfgA rep3"),
    ("exp01-cfgB-rep1-2026-08-12", "cfgB", "cfgB rep1"),
    ("exp01-cfgB-rep2-2026-08-12", "cfgB", "cfgB rep2"),
]
SUPPLEMENTARY_RUNS: list[tuple[str, str, str]] = [
    ("exp0-2026-07-26", "pre-calibration", "exp0 (uncalibrated judge)"),
]


@dataclass
class Slot:
    """One run x cluster slot — the unit of the lock-in headline."""

    run_label: str
    condition: str
    cluster: str
    trained_probe: str
    s1_winner: str
    s1_pass_rate: float
    s1_tied: list[str]
    final_winner: str
    final_pass_rate: float
    final_tied: list[str]
    pinned_probes: frozenset[str]
    # First spoke in this cluster to receive an upvote-driven edge increase.
    # None when the graph DB was unavailable, or when no seed spoke in the
    # cluster ever took a positive update under an "up" verdict.
    first_upvoted: Optional[str] = field(default=None)

    @property
    def s1_is_tied(self) -> bool:
        """A session-1 tie means argmax picked arbitrarily among equals."""
        return len(self.s1_tied) > 1

    @property
    def final_is_tied(self) -> bool:
        return len(self.final_tied) > 1

    @property
    def is_degenerate(self) -> bool:
        """
        A slot where nothing actually won: the final winner passed zero probes,
        or the "win" was a tie across most of the cluster. Counting these as
        preserved-or-not is meaningless, so they are reported separately.
        """
        return self.final_pass_rate <= 0.0 or len(self.final_tied) >= 4

    @property
    def preserved_strict(self) -> bool:
        """Same winner at both ends, with no tie at either end to muddy it."""
        return (
            not self.s1_is_tied
            and not self.final_is_tied
            and self.s1_winner == self.final_winner
        )

    @property
    def preserved_lenient(self) -> bool:
        """Final winner was among session 1's top scorers (ties count as credit)."""
        s1_top = set(self.s1_tied) | {self.s1_winner}
        return self.final_winner in s1_top

    @property
    def winner_is_pinned(self) -> bool:
        """
        The final winner passes at 1.0 in every session under a control, i.e.
        the system solves it without learning at all. An "aligned" slot whose
        winner is pinned is hollow — it counts toward alignment while the
        learned condition may be scoring far *below* its own control on that
        probe (rep3's cluster_a: vs_a1 pinned at 1.00, learned 0.04-0.12).
        """
        return self.final_winner in self.pinned_probes

    @property
    def final_aligned(self) -> bool:
        return self.final_winner == self.trained_probe

    @property
    def final_aligned_solid(self) -> bool:
        """Aligned on a probe the control does not already solve for free."""
        return self.final_aligned and not self.winner_is_pinned

    @property
    def first_upvoted_is_trained(self) -> bool:
        return self.first_upvoted is not None and self.first_upvoted == self.trained_probe

    @property
    def final_matches_first_upvoted(self) -> bool:
        """
        Item C.b's secondary number: does the first spoke to get positive
        credit go on to win the cluster? This is the sharper test of the
        lottery hypothesis than the session-1 winner, since it points at the
        specific feedback event the rich-get-richer loop would amplify.
        """
        return self.first_upvoted is not None and self.first_upvoted == self.final_winner

    @property
    def s1_aligned(self) -> bool:
        return self.s1_winner == self.trained_probe


def load_mu_to_probe(probe_set: Path = PROBE_SET) -> dict[str, str]:
    """
    Map gold seed MU id -> probe id from the probe set.

    Seed MU ids are deterministic per fixture version (a0000002-... is always
    cluster_a spoke 1), which is what lets edge updates from wiped-and-reseeded
    experiments still resolve to a spoke.
    """
    try:
        with probe_set.open(encoding="utf-8") as fh:
            spec = yaml.safe_load(fh)
    except (OSError, yaml.YAMLError) as exc:
        logger.error("could not read probe set %s: %s", probe_set, exc)
        return {}
    mapping: dict[str, str] = {}
    for probe in spec.get("probes") or []:
        for mu_id in probe.get("gold_mu_ids") or []:
            mapping[mu_id] = probe["probe_id"]
    logger.info("mapped %d gold MUs to probes", len(mapping))
    return mapping


def load_first_upvoted_spokes(
    db_path: Path, experiment_id: str, mu_to_probe: dict[str, str]
) -> dict[str, str]:
    """
    Per cluster, the first seed spoke to receive an upvote-driven edge increase.

    Reads `edge_update_events` (the per-update ledger the report aggregates)
    joined to `analytics` for the experiment id. Ordered chronologically by
    `created_at`, since updates span several series within one experiment.

    Opened read-only: this is analysis over experiment history and must never
    mutate the graph.
    """
    query = """
        SELECT e.mu_id, e.verdict, e.increment
        FROM edge_update_events e
        JOIN analytics a ON a.run_id = e.run_id
        WHERE a.experiment_id = ?
        ORDER BY e.created_at, e.turn_index
    """
    first: dict[str, str] = {}
    try:
        con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    except sqlite3.Error as exc:
        logger.error("could not open graph db %s read-only: %s", db_path, exc)
        return {}
    try:
        for mu_id, verdict, increment in con.execute(query, (experiment_id,)):
            # An upvote-driven *increase* only — down verdicts and the
            # negative-scope decrements they cause are not credit.
            if verdict != "up" or (increment or 0) <= 0:
                continue
            probe_id = mu_to_probe.get(mu_id)
            if not probe_id:
                continue  # conversation MU, or a spoke with no probe
            cluster = f"cluster_{probe_id[3]}"
            first.setdefault(cluster, probe_id)
    except sqlite3.Error as exc:
        logger.error("query failed for %s: %s", experiment_id, exc)
        return {}
    finally:
        con.close()
    return first


def load_slots(
    basename: str,
    condition: str,
    label: str,
    first_upvoted: Optional[dict[str, str]] = None,
) -> list[Slot]:
    """Extract the four cluster slots from one archived experiment report."""
    first_upvoted = first_upvoted or {}
    path = REPORT_DIR / f"experiment_report_{basename}_learned.json"
    try:
        with path.open(encoding="utf-8") as fh:
            report = json.load(fh)
    except FileNotFoundError:
        logger.error("report not found, skipping run: %s", path)
        return []
    except (OSError, ValueError) as exc:
        logger.error("could not read %s: %s", path, exc)
        return []

    # Union the pinned-probe lists across every control shadow in the report —
    # a probe pinned under any control is one the system gets for free.
    pinned: set[str] = set()
    for control in (report.get("control_shadow") or {}).values():
        if not isinstance(control, dict):
            continue
        adjusted = control.get("pin_adjusted_mean_deltas") or {}
        pinned.update(adjusted.get("pinned_probe_ids") or [])

    measures = report.get("learning_measures") or {}
    winner_table = measures.get("cluster_winner_table") or {}
    trained = measures.get("trained_probes") or {}
    if not winner_table or not trained:
        logger.error("run %s has no cluster winner table / trained probes", label)
        return []

    slots: list[Slot] = []
    for cluster, entry in sorted(winner_table.items()):
        sessions = entry.get("sessions") or []
        final = entry.get("final_session_winner") or {}
        s1 = next((s for s in sessions if s.get("session_index") == 1), None)
        if s1 is None or not final:
            logger.warning("run %s cluster %s missing session 1 or final", label, cluster)
            continue
        slots.append(
            Slot(
                run_label=label,
                condition=condition,
                cluster=cluster,
                trained_probe=(trained.get(cluster) or {}).get("probe_id", "?"),
                s1_winner=s1.get("winner_probe_id", "?"),
                s1_pass_rate=float(s1.get("winner_pass_rate") or 0.0),
                s1_tied=list(s1.get("tied_with") or []),
                final_winner=final.get("winner_probe_id", "?"),
                final_pass_rate=float(final.get("winner_pass_rate") or 0.0),
                final_tied=list(final.get("tied_with") or []),
                pinned_probes=frozenset(pinned),
                first_upvoted=first_upvoted.get(cluster),
            )
        )
    logger.info("run %s: %d slots loaded", label, len(slots))
    return slots


def rate(numerator: int, denominator: int) -> str:
    """Format a count as 'n/d (pp%)', guarding the empty-denominator case."""
    if denominator == 0:
        return "0/0 (n/a)"
    return f"{numerator}/{denominator} ({100.0 * numerator / denominator:.0f}%)"


def summarize(slots: list[Slot], exclude_degenerate: bool) -> dict[str, str]:
    """Compute the headline rates over a set of slots, under both tie policies."""
    pool = [s for s in slots if not (exclude_degenerate and s.is_degenerate)]
    n = len(pool)
    return {
        "n": str(n),
        "strict": rate(sum(1 for s in pool if s.preserved_strict), n),
        "lenient": rate(sum(1 for s in pool if s.preserved_lenient), n),
        "final_aligned": rate(sum(1 for s in pool if s.final_aligned), n),
        "final_aligned_solid": rate(sum(1 for s in pool if s.final_aligned_solid), n),
        "s1_aligned": rate(sum(1 for s in pool if s.s1_aligned), n),
        # Denominator is slots where the first-upvoted spoke is known at all.
        "first_upvoted_wins": rate(
            sum(1 for s in pool if s.final_matches_first_upvoted),
            sum(1 for s in pool if s.first_upvoted is not None),
        ),
        "first_upvoted_trained": rate(
            sum(1 for s in pool if s.first_upvoted_is_trained),
            sum(1 for s in pool if s.first_upvoted is not None),
        ),
    }


def render_slot_table(slots: list[Slot]) -> list[str]:
    """The per-slot detail table — item C.a's deliverable."""
    lines = [
        "| run | cluster | trained | session-1 winner | first upvoted "
        "| final winner | preserved? | aligned? |",
        "|---|---|---|---|---|---|---|---|",
    ]
    for s in slots:
        s1 = f"{s.s1_winner} ({s.s1_pass_rate:.2f})"
        if s.s1_is_tied:
            s1 += f" tie×{len(s.s1_tied)}"
        fin = f"{s.final_winner} ({s.final_pass_rate:.2f})"
        if s.final_is_tied:
            fin += f" tie×{len(s.final_tied)}"
        if s.is_degenerate:
            fin += " ⚠"
        pres = "✅" if s.preserved_strict else ("~" if s.preserved_lenient else "—")
        if s.final_aligned:
            aligned = "✅ (pinned)" if s.winner_is_pinned else "✅"
        else:
            aligned = "—"
        upvoted = s.first_upvoted or "—"
        if s.first_upvoted_is_trained:
            upvoted += " ✅"
        lines.append(
            f"| {s.run_label} | {s.cluster.replace('cluster_', '')} | {s.trained_probe} "
            f"| {s1} | {upvoted} | {fin} | {pres} | {aligned} |"
        )
    return lines


def render_alignment_transitions(slots: list[Slot], exclude_degenerate: bool) -> list[str]:
    """
    Cross-tab session-1 alignment against final alignment, per condition.

    This is the read that separates the two competing mechanisms for a config
    effect. "Held" means the trained spoke won session 1 and still held at
    session 5 — the config sustained an early win. "Gained" means the trained
    spoke lost session 1 but won by session 5 — the config let a late correct
    signal overturn the early lottery. A config whose alignment comes entirely
    from "held" is steering the session-0/1 window; one whose alignment comes
    from "gained" is keeping the contest open long enough to be overturned.
    """
    lines = [
        "| group | slots | held (s1 ✅ → final ✅) | gained (s1 — → final ✅) "
        "| lost (s1 ✅ → final —) | never (s1 — → final —) |",
        "|---|---|---|---|---|---|",
    ]
    for group in ["baseline", "cfgA", "cfgB"]:
        subset = [s for s in slots if s.condition == group]
        if exclude_degenerate:
            subset = [s for s in subset if not s.is_degenerate]
        if not subset:
            continue
        held = sum(1 for s in subset if s.s1_aligned and s.final_aligned)
        gained = sum(1 for s in subset if not s.s1_aligned and s.final_aligned)
        lost = sum(1 for s in subset if s.s1_aligned and not s.final_aligned)
        never = sum(1 for s in subset if not s.s1_aligned and not s.final_aligned)
        lines.append(
            f"| {group} | {len(subset)} | {held} | {gained} | {lost} | {never} |"
        )
    return lines


def render_summary_table(slots: list[Slot], exclude_degenerate: bool) -> list[str]:
    """Preservation and alignment rates, overall and split by condition (item C.c)."""
    lines = [
        "| group | slots | preserved (strict) | preserved (lenient) "
        "| final = first-upvoted | final aligned | final aligned (excl. pinned) "
        "| session-1 aligned |",
        "|---|---|---|---|---|---|---|---|",
    ]

    def row(name: str, subset: list[Slot]) -> str:
        r = summarize(subset, exclude_degenerate)
        return (
            f"| {name} | {r['n']} | {r['strict']} | {r['lenient']} "
            f"| {r['first_upvoted_wins']} | {r['final_aligned']} "
            f"| {r['final_aligned_solid']} | {r['s1_aligned']} |"
        )

    for group in ["baseline", "cfgA", "cfgB"]:
        subset = [s for s in slots if s.condition == group]
        if subset:
            lines.append(row(group, subset))
    lines.append(row("**pooled**", slots))
    pct = f"_{CHANCE_LEVEL * 100:.0f}%_"
    lines.append(f"| _chance_ | — | {pct} | {pct} | {pct} | {pct} | {pct} | {pct} |")
    return lines


def build_report(slots: list[Slot], supplementary: list[Slot]) -> str:
    """Assemble the full markdown deep-dive for docs/evaluations/."""
    degenerate = [s for s in slots if s.is_degenerate]
    out: list[str] = [
        "# Alpha C — early-feedback lock-in analysis",
        "",
        "Generated by `scripts/alpha_c_lockin.py` from archived experiment reports.",
        "Existing data only — no new runs, no DB access.",
        "",
        "## Headline — all slots",
        "",
        *render_summary_table(slots, exclude_degenerate=False),
        "",
        f"## Headline — excluding {len(degenerate)} degenerate slots",
        "",
        "_A degenerate slot is one where nothing won: the final winner scored 0.00,",
        "or the win was a tie across 4+ of the cluster's 7 spokes._",
        "",
        *render_summary_table(slots, exclude_degenerate=True),
        "",
        "## Alignment transitions — how alignment arrives",
        "",
        "_Does a config's alignment come from sustaining an early win, or from",
        "overturning an early loss? Degenerate slots excluded._",
        "",
        *render_alignment_transitions(slots, exclude_degenerate=True),
        "",
        "## Per-slot detail",
        "",
        "_`preserved`: ✅ = same winner at both ends with no ties; ~ = final winner was",
        "among session 1's tied top scorers; — = not preserved. ⚠ marks a degenerate slot._",
        "",
        *render_slot_table(slots),
        "",
    ]
    if supplementary:
        out += [
            "## Supplementary — outside the pre-registered scope",
            "",
            "_`exp0-2026-07-26` is structurally identical (28 probes, 210 runs, same",
            "trained spokes) but predates the judge-calibration passes, so it is not",
            "pooled into the headline above._",
            "",
            *render_slot_table(supplementary),
            "",
            *render_summary_table(supplementary, exclude_degenerate=False),
            "",
        ]
    missing = [s for s in slots if s.first_upvoted is None]
    if missing:
        out += [
            "## Coverage note — first-upvoted-spoke column",
            "",
            f"{len(missing)} of {len(slots)} slots have no first-upvoted spoke recorded.",
            "That means either the graph DB was unavailable, or no seed spoke in that",
            "cluster ever took a positive edge update under an `up` verdict — in which",
            "case the cluster's whole upvote history landed on conversation MUs.",
            "Those slots are excluded from the `final = first-upvoted` denominator.",
            "",
        ]
    return "\n".join(out)


def main() -> None:
    parser = argparse.ArgumentParser(description="Alpha C lock-in analysis")
    parser.add_argument(
        "--out",
        type=Path,
        default=REPO_ROOT / "docs" / "evaluations" / "alpha_c_lockin_analysis.md",
        help="output markdown path",
    )
    parser.add_argument(
        "--db",
        type=Path,
        default=GRAPH_DB,
        help="graph.db path, read-only, for the first-upvoted-spoke column",
    )
    args = parser.parse_args()

    mu_to_probe: dict[str, str] = {}
    if args.db.exists():
        mu_to_probe = load_mu_to_probe()
        if not mu_to_probe:
            logger.warning("probe set unusable — first-upvoted column will be empty")
    else:
        logger.warning("graph db not found at %s — skipping first-upvoted column", args.db)

    def collect(runs: list[tuple[str, str, str]]) -> list[Slot]:
        out: list[Slot] = []
        for basename, condition, label in runs:
            upvoted: dict[str, str] = {}
            if mu_to_probe:
                upvoted = load_first_upvoted_spokes(
                    args.db, f"{basename}_learned", mu_to_probe
                )
            out.extend(load_slots(basename, condition, label, upvoted))
        return out

    slots = collect(RUNS)
    supplementary = collect(SUPPLEMENTARY_RUNS)

    if not slots:
        logger.error("no slots loaded — nothing to report")
        raise SystemExit(1)
    logger.info("loaded %d slots across %d runs", len(slots), len(RUNS))

    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(build_report(slots, supplementary), encoding="utf-8")
    logger.info("wrote %s", args.out)

    # Console summary so a run of the script is readable without opening the file.
    for line in render_summary_table(slots, exclude_degenerate=True):
        print(line)


if __name__ == "__main__":
    main()
