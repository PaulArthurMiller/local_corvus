"""
Plan Concept Alpha item D — path-back instrumentation.

Phase 1 (this file, current scope): the graph reconstructor and its
three-component tie-back. Rebuilds per-run graph topology and edge (alpha,
beta) trajectories at any turn index, purely from sources that survive a
graph wipe (`memory/graph.db`'s `analytics` and `edge_update_events` tables,
plus `seed_fixtures/seed_v3.json.gz`) — never from the live `edges` /
`memory_units` tables, which by now hold only the most recent wipe+reseed's
state and no longer carry the 8 archived runs' conversation topology at all.

Phase 2 (this file, added below the Phase 1 tie-back) adds the metrics
layer on top of the validated reconstructor: the four-bucket path-back
partition, chase-down yield, the K2 runner-up rank distribution, and anchor
strength — computed against the 8 runs' PROBE measurements (`probe_results`,
battery_mode="mean"), never against real turns, since real turns have no
"gold" concept. See BUILD-PLAN.md's "Path-Back Instrumentation (Plan Alpha
D) — Milestone 45" section for the full design, including why the partition
is directed / horizon-2 / and why archived data (not a fresh run) can
answer this.

--------------------------------------------------------------------------
Phase 2 — why probe hyperparameters are hardcoded, not read per-run
--------------------------------------------------------------------------
`POST /admin/run-probe` read `retrieval_level1_top_k` / `retrieval_level2_
top_k` / `directional_edge_weight_multiplier` from config.yaml **on disk**
for every one of these 8 archived runs — the exact bug Milestone 44 fixed
(BUILD-PLAN.md's Milestone 44, PROGRESS.md's 2026-08-20(2) entry). cfgB
declared `retrieval_level2_top_k: 2` and its real turns used it (5 = 3+2
retrieved), but its probe rows were still served under the base value (4 =
3+1) — confirmed empirically against `retrieved_mu_ids` length below.
Phase 2's probe-side reconstruction (K2 candidate scoring, K1/K2 slicing of
`retrieved_mu_ids`) therefore uses the three PROBE_* constants below for
every run, never a run's own `hyperparameters_snapshot` — that snapshot
describes real-turn retrieval, not what served these probes.

--------------------------------------------------------------------------
Phase 2 — probe measurement session -> real-turn graph state
--------------------------------------------------------------------------
Probe measurement writes no `analytics` row and therefore no `turn_index`;
what it does carry is `measurement_session_index`. `resolve_turn_sequence()`
+ `assign_probe_schedule()` (scripts/harness/turn_resolution.py) place a
probe battery at the end of each series' bulk-turn block for every plan in
scope here, so `measurement_session_index=k` reflects the graph exactly as
it stood after bulk session k's real turns completed — one turn_index
before bulk session k+1's first turn (or, for the run's last session, after
its very last real turn). `compute_session_cutoffs()` derives this per-run
from `analytics.session_turn_index` resets to 0 (empirical, not assumed —
consistent with this script's whole approach) rather than hardcoding a
turns-per-session constant that need not hold for every plan.

--------------------------------------------------------------------------
Phase 2 — battery_mode="mean" only
--------------------------------------------------------------------------
Every metric below reads `battery_mode="mean"` probe rows exclusively (28
probes x 5 sessions x 8 runs = 1,120 measurements) — the single
deterministic posterior-mean reading per probe per session, matching the
existing report-layer convention of scoping headline metrics to mean mode
(`_section_61_learning_measures()`'s `battery_mode="mean"` filtering).
`battery_mode="sample"`'s 26-repeat Thompson-sampling variance (3,500 rows
per run) is a separate question this milestone does not answer.

--------------------------------------------------------------------------
Why no independent edge_id for conversation edges
--------------------------------------------------------------------------
`retrieve()` (`backend/memory/retrieval.py:104`) is the sole read path this
reconstructor must agree with. Its edges fall into two identity classes:

  SEED edges   — deterministic ids from seed_fixtures/seed_v3.json.gz
                 ("ea000003", "ea000003r", ...). Known in full, upfront.
  CONV edges   — prompt->input, input->output, output->each-activated-MU,
                 created via `graph_store.create_edge()` calls in
                 `backend/core/orchestrator.py` (steps ~517/601/621) that
                 never pass an `edge_id` — the store assigns a fresh random
                 UUID internally. That UUID is not derivable from anything
                 this reconstructor reads, and the live `edges` table that
                 once held it has since been wiped and reseeded several
                 times over (only 34 seed MUs remain there today).

The reconstructor therefore never tries to guess a conv edge's literal id.
Instead it works from the fact that every conv edge's (source_mu_id,
target_mu_id) pair is created **at most once ever** in a run's history —
every prompt/input/output MU is unique to its own turn and is never reused
as a source in a later turn's creation step — so the pair itself is an
unambiguous identity. Topology existence and completeness (component 2,
below) are checked by (source, target) pair; a turn's own recorded
`traversal_path` (which DOES carry the real edge_id alongside the pair) is
then trusted as the enumeration for replaying K1->K2 selection (component
3) — validated by component 2 having just confirmed that enumeration is
topologically correct, not invented or missing anything.

--------------------------------------------------------------------------
The three tie-back components (BUILD-PLAN.md's Done-when for Phase 1)
--------------------------------------------------------------------------
  (1) ALPHA/BETA   reconstructed (alpha, beta) == recorded edge_weights_used,
                    for every traversed edge, at every turn. Pure event-
                    sourcing over edge_update_events by edge_id + created_at
                    order; needs no topology at all.
  (2) TOPOLOGY      every (source, target) pair recorded in a turn's
                    traversal_path exists in the reconstructed graph by that
                    turn (existence), and every reconstructed K1-outgoing
                    pair to a non-K1 target appears in traversal_path
                    (completeness) — together these validate the topology
                    replay in both directions.
  (3) SLATE         replayed K1 (from recorded, already-sorted
                    candidate_set_before_ranking) + K2 (from recorded
                    traversal_path, with edge_weights_used weights INJECTED
                    rather than resampled — weight_mode: sample draws are
                    not regenerable) == recorded activated_mu_ids.

RNG boundary: only component 3 touches sampled edge weights, and it reads
them from the recorded columns rather than resampling. Components 1 and 2
are fully deterministic and carry no such caveat.
"""

from __future__ import annotations

import argparse
import gzip
import json
import logging
import math
import re
import sqlite3
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

import yaml

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
logger = logging.getLogger("alpha_d_pathback")

REPO_ROOT = Path(__file__).resolve().parent.parent
GRAPH_DB = REPO_ROOT / "memory" / "graph.db"
AUDIT_LOG = REPO_ROOT / "memory" / "audit.log"
SEED_FIXTURES_DIR = REPO_ROOT / "seed_fixtures"
PROBES_DIR = REPO_ROOT / "probes"
PLANS_DIR = REPO_ROOT / "plans"

# All 8 archived learned runs in scope post-date Milestone 25 (Seed Fixture
# v3) and were run against a freshly wiped-and-reseeded v3 graph each time
# (confirmed empirically: memory_units currently holds only the 34 "seed:v3"
# MUs from the most recent reseed — no v2 MUs survive). load_seed_fixture()
# still verifies this per-run rather than assuming it silently: any run
# whose activated MUs reference a seed-looking id absent from v3 fails loud
# in the tie-back rather than mis-attributing topology.
SEED_FIXTURE_VERSION = "v3"
PROBE_SET_VERSION = "v3"

# See the module docstring's "why probe hyperparameters are hardcoded" note:
# every archived probe row in scope was served under these base config.yaml
# values regardless of what the run declared, including cfgB.
PROBE_RETRIEVAL_LEVEL1_TOP_K = 3
PROBE_RETRIEVAL_LEVEL2_TOP_K = 1
PROBE_DIRECTIONAL_EDGE_WEIGHT_MULTIPLIER = 1.5

# The hop horizon the partition and chase-down yield are computed at — see
# BUILD-PLAN.md's "Horizon is 2" note. A large OUT_OF_DEPTH bucket is the
# argument for revisiting this, with data, not a reason to change it here.
PATHBACK_HORIZON = 2

# The pre-registered scope: 8 learned runs (exp0b x3, exp01 x5). Same set
# and same rationale as scripts/alpha_c_lockin.py's RUNS — kept as an
# independent copy here since the two scripts read different DB tables and
# should not import each other's module-level state.
RUNS: list[tuple[str, str, str]] = [
    # (experiment_id basename, condition group, short label)
    ("exp0b-2026-08-06", "baseline", "exp0b rep1"),
    ("exp0b-rep2-2026-08-09", "baseline", "exp0b rep2"),
    ("exp0b-rep3-2026-08-09", "baseline", "exp0b rep3"),
    ("exp01-cfgA-rep1-2026-08-12", "cfgA", "cfgA rep1"),
    ("exp01-cfgA-rep2-2026-08-12", "cfgA", "cfgA rep2"),
    ("exp01-cfgA-rep3-2026-08-17", "cfgA", "cfgA rep3"),
    ("exp01-cfgB-rep1-2026-08-12", "cfgB", "cfgB rep1"),
    ("exp01-cfgB-rep2-2026-08-12", "cfgB", "cfgB rep2"),
]

_FLOAT_TOL = 1e-6


# ---------------------------------------------------------------------------
# Data loading
# ---------------------------------------------------------------------------


@dataclass
class SeedFixture:
    """Static topology from seed_fixtures/seed_{version}.json.gz."""

    version: str
    mu_ids: set[str]
    edges: dict[str, dict]  # edge_id -> {source_mu_id, target_mu_id}
    edge_by_pair: dict[tuple[str, str], str]  # (source, target) -> edge_id


def load_seed_fixture(
    version: str = SEED_FIXTURE_VERSION, path: Optional[Path] = None
) -> SeedFixture:
    """Load seed MU ids and edge topology from the gzip-compressed fixture file."""
    fixture_path = path or (SEED_FIXTURES_DIR / f"seed_{version}.json.gz")
    with gzip.open(fixture_path, "rt", encoding="utf-8") as fh:
        data = json.load(fh)

    mu_ids = {mu["mu_id"] for mu in data.get("memory_units", [])}
    edges: dict[str, dict] = {}
    edge_by_pair: dict[tuple[str, str], str] = {}
    for e in data.get("edges", []):
        edge_id = e["edge_id"]
        pair = (e["source_mu_id"], e["target_mu_id"])
        edges[edge_id] = {"source_mu_id": pair[0], "target_mu_id": pair[1]}
        if pair in edge_by_pair:
            # Would break the pair-based identity assumption for seed edges;
            # log loudly rather than silently pick one.
            logger.warning(
                "alpha_d: seed fixture %s has duplicate (source,target) pair %s "
                "(edge_ids %s and %s) — pair-keyed lookups will use the last one",
                version, pair, edge_by_pair[pair], edge_id,
            )
        edge_by_pair[pair] = edge_id

    logger.info(
        "alpha_d: loaded seed fixture version=%s mus=%d edges=%d",
        version, len(mu_ids), len(edges),
    )
    return SeedFixture(version=version, mu_ids=mu_ids, edges=edges, edge_by_pair=edge_by_pair)


def load_audit_registry(path: Path = AUDIT_LOG) -> dict[str, str]:
    """
    mu_id -> source_type, read from the append-only JSONL audit log.

    Covers conversation MUs written via `gate.audit.append_audit()` (input,
    output, tool_call, tool_result) — NOT prompt MUs (never audited by
    design; see orchestrator.py's Step 5 comment) and NOT seed MUs (they
    bypass the audit boundary entirely, per the BUILD-PLAN's source table).

    Not required by any of Phase 1's three tie-back components (worked out
    to be unnecessary — see the module docstring), but built here as the
    reusable "six sources" registry Phase 2's metrics layer will read from.
    Returns {} (with a warning, not a raise) if the file is absent — callers
    that don't need it should not be blocked by its absence.
    """
    if not path.exists():
        logger.warning("alpha_d: audit log not found at %s — registry will be empty", path)
        return {}
    registry: dict[str, str] = {}
    malformed = 0
    with open(path, "r", encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                malformed += 1
                continue
            mu_id = record.get("mu_id")
            if mu_id:
                registry[mu_id] = record.get("source_type", "unknown")
    if malformed:
        logger.warning("alpha_d: skipped %d malformed audit log lines", malformed)
    logger.info("alpha_d: loaded audit registry with %d MU ids", len(registry))
    return registry


_TURN_COLUMNS = [
    "run_id", "turn_index", "timestamp", "session_turn_index",
    "prompt_mu_id", "input_mu_id", "output_mu_id",
    "activated_mu_ids", "entry_point_mu_ids", "candidate_set_before_ranking",
    "traversal_path", "hop_depth_per_retrieved_mu", "edge_weights_used",
    "hyperparameters_snapshot", "full_config_snapshot",
]


@dataclass
class Turn:
    """One real turn's retrieval-relevant analytics columns, decoded."""

    run_id: str
    turn_index: int
    timestamp: str
    prompt_mu_id: Optional[str]
    input_mu_id: Optional[str]
    output_mu_id: Optional[str]
    activated_mu_ids: list[str]
    entry_point_mu_ids: list[str]
    candidate_set_before_ranking: list[dict]
    traversal_path: list[dict]
    hop_depth_per_retrieved_mu: dict
    edge_weights_used: dict
    hyperparameters_snapshot: dict
    edge_update_cfg: dict = field(default_factory=dict)
    # Phase 2 — 0-indexed position within a bulk-turn session, used purely
    # to find session boundaries (compute_session_cutoffs()). Absent from
    # Phase 1's tie-back needs, so it defaults to None for any caller
    # (tests included) that builds a Turn without it.
    session_turn_index: Optional[int] = None


def load_turns(db_path: Path, experiment_id: str) -> list[Turn]:
    """
    Load every real turn for one experiment_id, ordered by the global
    turn_index. `is_probe_turn = 0` is asserted in the query rather than
    assumed — probe measurement (POST /admin/run-probe) writes no analytics
    rows at all today (the M46 gap this milestone doesn't depend on), but a
    future world where it does must not silently pollute this reconstructor.
    """
    con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    try:
        rows = con.execute(
            f"SELECT {', '.join(_TURN_COLUMNS)} FROM analytics "
            "WHERE experiment_id = ? AND is_probe_turn = 0 ORDER BY turn_index",
            (experiment_id,),
        ).fetchall()
    except sqlite3.Error as exc:
        logger.error("alpha_d: could not read analytics for %s: %s", experiment_id, exc)
        return []
    finally:
        con.close()

    turns: list[Turn] = []
    for r in rows:
        if r["turn_index"] is None:
            logger.warning(
                "alpha_d: run_id=%s in experiment %s has NULL turn_index — skipping",
                r["run_id"], experiment_id,
            )
            continue
        full_cfg = json.loads(r["full_config_snapshot"] or "{}")
        turns.append(Turn(
            run_id=r["run_id"],
            turn_index=r["turn_index"],
            timestamp=r["timestamp"],
            session_turn_index=r["session_turn_index"],
            prompt_mu_id=r["prompt_mu_id"],
            input_mu_id=r["input_mu_id"],
            output_mu_id=r["output_mu_id"],
            activated_mu_ids=json.loads(r["activated_mu_ids"] or "[]"),
            entry_point_mu_ids=json.loads(r["entry_point_mu_ids"] or "[]"),
            candidate_set_before_ranking=json.loads(r["candidate_set_before_ranking"] or "[]"),
            traversal_path=json.loads(r["traversal_path"] or "[]"),
            hop_depth_per_retrieved_mu=json.loads(r["hop_depth_per_retrieved_mu"] or "{}"),
            edge_weights_used=json.loads(r["edge_weights_used"] or "{}"),
            hyperparameters_snapshot=json.loads(r["hyperparameters_snapshot"] or "{}"),
            edge_update_cfg=full_cfg.get("edge_update", {}) or {},
        ))
    logger.info("alpha_d: loaded %d real turns for %s", len(turns), experiment_id)
    return turns


@dataclass
class EdgeUpdateEvent:
    edge_id: str
    created_at: str
    alpha_after: float
    beta_after: float
    # Phase 2 — the retrieval turn being credited, in the same global
    # turn_index space as analytics.turn_index (confirmed empirically: this
    # column's range matches analytics' exactly for every archived run).
    # Phase 1's timestamp-keyed reconstructed_alpha_beta() doesn't need
    # this; Phase 2's alpha_beta_as_of_turn_index() uses it for an exact
    # turn-index cutoff instead of an approximate timestamp compare.
    turn_index: Optional[int] = None


def load_edge_update_events(
    db_path: Path, experiment_id: str
) -> dict[str, list[EdgeUpdateEvent]]:
    """
    edge_id -> chronologically ordered list of counter-update events, for
    every edge ever credited within one experiment's turns.

    Joins through analytics.run_id (edge_update_events.run_id is the run
    whose retrieved slate is being credited — the retrieval run, not the
    feedback-submission turn — same join alpha_c_lockin.py's
    load_first_upvoted_spokes() uses), ordered by created_at so the alpha/
    beta trajectory replay below never has to re-sort.
    """
    query = """
        SELECT e.edge_id, e.created_at, e.alpha_after, e.beta_after, e.turn_index
        FROM edge_update_events e
        JOIN analytics a ON a.run_id = e.run_id
        WHERE a.experiment_id = ?
        ORDER BY e.created_at, e.turn_index
    """
    con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    try:
        rows = con.execute(query, (experiment_id,)).fetchall()
    except sqlite3.Error as exc:
        logger.error("alpha_d: could not read edge_update_events for %s: %s", experiment_id, exc)
        return {}
    finally:
        con.close()

    events: dict[str, list[EdgeUpdateEvent]] = {}
    for edge_id, created_at, alpha_after, beta_after, turn_index in rows:
        events.setdefault(edge_id, []).append(
            EdgeUpdateEvent(edge_id, created_at, alpha_after, beta_after, turn_index)
        )
    logger.info(
        "alpha_d: loaded update events for %d distinct edges (%d events) for %s",
        len(events), sum(len(v) for v in events.values()), experiment_id,
    )
    return events


# ---------------------------------------------------------------------------
# The reconstructor
# ---------------------------------------------------------------------------


class GraphReconstructor:
    """
    Rebuilds one run's graph topology and edge weight trajectory from the
    sources above — never from the live `edges` / `memory_units` tables.
    """

    def __init__(
        self,
        seed: SeedFixture,
        turns: list[Turn],
        edge_events: dict[str, list[EdgeUpdateEvent]],
    ):
        self.seed = seed
        self.turns = turns
        self.edge_events = edge_events
        # (source_mu_id, target_mu_id) -> the turn_index whose processing
        # created this conv edge. See the module docstring for why this is
        # unambiguous (every conv pair is created at most once ever).
        self.pair_to_creation_turn: dict[tuple[str, str], int] = {}
        self._replay_conv_topology()
        # Phase 2 — (source, target) -> edge_id, for every pair some real
        # turn's traversal_path actually walked. edge_update_events is keyed
        # by edge_id, not by pair, so this is how Phase 2 resolves a conv
        # edge's weight; Phase 1 never needed it because it always already
        # had the edge_id in hand (from a turn's own recorded traversal_path).
        self.traversed_pair_edge_ids: dict[tuple[str, str], str] = {}
        self._index_traversed_pairs()

    def _replay_conv_topology(self) -> None:
        """Replay orchestrator.py's three edge-creation sites (~517/601/621)."""
        for t in self.turns:
            if t.prompt_mu_id and t.input_mu_id:
                self._record_pair(t.prompt_mu_id, t.input_mu_id, t.turn_index)
            if t.input_mu_id and t.output_mu_id:
                self._record_pair(t.input_mu_id, t.output_mu_id, t.turn_index)
            if t.output_mu_id:
                for mu_id in t.activated_mu_ids:
                    self._record_pair(t.output_mu_id, mu_id, t.turn_index)

    def _record_pair(self, source: str, target: str, turn_index: int) -> None:
        key = (source, target)
        existing = self.pair_to_creation_turn.get(key)
        if existing is not None and existing != turn_index:
            logger.warning(
                "alpha_d: pair %s recreated at turn %d (first seen turn %d) — "
                "the at-most-once assumption does not hold for this pair",
                key, turn_index, existing,
            )
            continue_turn = min(existing, turn_index)
            self.pair_to_creation_turn[key] = continue_turn
            return
        self.pair_to_creation_turn[key] = turn_index

    def _index_traversed_pairs(self) -> None:
        """(source, target) -> edge_id, from every real turn's own recorded
        traversal_path. First write wins — irrelevant in practice since a
        conv pair is created (and thus traversable) at most once ever, so
        every hop recording it carries the same edge_id."""
        for t in self.turns:
            for hop in t.traversal_path:
                pair = (hop["source_mu_id"], hop["target_mu_id"])
                self.traversed_pair_edge_ids.setdefault(pair, hop["edge_id"])

    def is_seed_pair(self, source: str, target: str) -> bool:
        return (source, target) in self.seed.edge_by_pair

    def is_seed_edge_id(self, edge_id: str) -> bool:
        return edge_id in self.seed.edges

    def edge_exists_at(self, source: str, target: str, turn_index: int) -> bool:
        """
        True if the (source, target) edge is visible to a turn's retrieval —
        i.e. created strictly before `turn_index`. Seed edges are always
        available; a conv edge created BY turn_index itself is not yet
        visible to that same turn's own retrieval (retrieval runs before
        that turn's edges are written — see orchestrator.py Step ordering).
        """
        if self.is_seed_pair(source, target):
            return True
        creation_turn = self.pair_to_creation_turn.get((source, target))
        return creation_turn is not None and creation_turn < turn_index

    def outgoing_pairs_at(self, source: str, turn_index: int) -> set[tuple[str, str]]:
        """All (source, target) pairs sourced at `source`, visible by turn_index."""
        pairs = {pair for pair in self.seed.edge_by_pair if pair[0] == source}
        for (s, t), creation_turn in self.pair_to_creation_turn.items():
            if s == source and creation_turn < turn_index:
                pairs.add((s, t))
        return pairs

    def creation_prior(self, edge_id: str, turn: Turn) -> tuple[float, float]:
        """The (alpha, beta) an edge started at when created, before any updates."""
        eu = turn.edge_update_cfg
        if self.is_seed_edge_id(edge_id):
            return (
                float(eu.get("seed_prior_alpha", 1.0)),
                float(eu.get("seed_prior_beta", 1.0)),
            )
        return float(eu.get("prior_alpha", 1.0)), float(eu.get("prior_beta", 1.0))

    def reconstructed_alpha_beta(
        self, edge_id: str, before_timestamp: str, turn: Turn
    ) -> tuple[float, float]:
        """
        (alpha, beta) as of just before `before_timestamp` — the latest
        edge_update_events entry with created_at < before_timestamp, or the
        creation prior if the edge has never been updated by then.
        """
        latest: Optional[EdgeUpdateEvent] = None
        for ev in self.edge_events.get(edge_id, []):
            if ev.created_at < before_timestamp:
                latest = ev
            else:
                break  # events are loaded pre-sorted by created_at
        if latest is not None:
            return latest.alpha_after, latest.beta_after
        return self.creation_prior(edge_id, turn)

    # -----------------------------------------------------------------
    # Phase 2 additions — probe-side reconstruction
    # -----------------------------------------------------------------

    def creation_prior_for_pair(self, source: str, target: str, turn: Turn) -> tuple[float, float]:
        """
        Pair-keyed analogue of creation_prior(). For a conv pair that has
        never appeared in any turn's traversal_path, its edge_id is
        genuinely unrecoverable (see the module docstring), but its prior
        is not: only a traversed edge can ever receive a weight update
        (credit assignment walks traversal_path — see
        gold_co_activation_census()'s docstring for the same fact applied
        the other direction), so an untraversed pair is provably still
        sitting at whatever prior it was created with.
        """
        eu = turn.edge_update_cfg
        if self.is_seed_pair(source, target):
            return float(eu.get("seed_prior_alpha", 1.0)), float(eu.get("seed_prior_beta", 1.0))
        return float(eu.get("prior_alpha", 1.0)), float(eu.get("prior_beta", 1.0))

    def alpha_beta_as_of_turn_index(
        self, edge_id: str, as_of_turn_index: int, turn: Turn
    ) -> tuple[float, float]:
        """
        (alpha, beta) as of just before a probe measurement session's
        cutoff turn_index — the probe-side analogue of
        reconstructed_alpha_beta(), which is timestamp-keyed because real-
        turn tie-back needs sub-turn precision within a single timestamp.
        Probe cutoffs are exact turn-index boundaries (see
        compute_session_cutoffs()), so this filters on
        edge_update_events.turn_index directly — exact, not approximate,
        and independent of event list ordering (no pre-sortedness assumed).
        """
        candidates = [
            ev for ev in self.edge_events.get(edge_id, [])
            if ev.turn_index is not None and ev.turn_index < as_of_turn_index
        ]
        if not candidates:
            return self.creation_prior(edge_id, turn)
        latest = max(candidates, key=lambda ev: ev.turn_index)
        return latest.alpha_after, latest.beta_after

    def mean_weight_for_pair(
        self, source: str, target: str, as_of_turn_index: int, turn: Turn
    ) -> float:
        """
        Posterior mean edge weight alpha/(alpha+beta) for a (source,
        target) pair as of a probe-session cutoff — the same formula
        compute_effective_weight() uses for weight_mode="mean"
        (backend/learning/edge_update.py). Seed pairs resolve their
        deterministic edge_id directly; conv pairs use their edge_id if the
        pair was ever traversed by some real turn (recoverable via
        traversed_pair_edge_ids), and fall back straight to the creation
        prior if not — an untraversed conv pair can only be at its creation
        prior, per creation_prior_for_pair()'s docstring.
        """
        if self.is_seed_pair(source, target):
            edge_id = self.seed.edge_by_pair[(source, target)]
            alpha, beta = self.alpha_beta_as_of_turn_index(edge_id, as_of_turn_index, turn)
        else:
            edge_id = self.traversed_pair_edge_ids.get((source, target))
            if edge_id is not None:
                alpha, beta = self.alpha_beta_as_of_turn_index(edge_id, as_of_turn_index, turn)
            else:
                alpha, beta = self.creation_prior_for_pair(source, target, turn)
        total = alpha + beta
        return alpha / total if total > 0 else 0.5


# ---------------------------------------------------------------------------
# The three tie-back components
# ---------------------------------------------------------------------------


@dataclass
class Mismatch:
    component: str
    run_label: str
    turn_index: int
    detail: str


def _close(a: float, b: float) -> bool:
    return math.isclose(a, b, rel_tol=_FLOAT_TOL, abs_tol=_FLOAT_TOL)


def verify_alpha_beta_tieback(
    reconstructor: GraphReconstructor, turns: list[Turn], run_label: str
) -> list[Mismatch]:
    """
    Component 1 — for every edge a turn actually traversed, the (alpha,
    beta) this reconstructor derives purely by event-sourcing
    edge_update_events must equal what retrieve() actually read and
    recorded in that turn's edge_weights_used. Needs no topology.
    """
    mismatches: list[Mismatch] = []
    for t in turns:
        for edge_id, info in t.edge_weights_used.items():
            recon_alpha, recon_beta = reconstructor.reconstructed_alpha_beta(
                edge_id, t.timestamp, t
            )
            recorded_alpha = info.get("alpha")
            recorded_beta = info.get("beta")
            if recorded_alpha is None or recorded_beta is None:
                continue  # not a "learned" condition turn — nothing to tie back
            if not _close(recon_alpha, recorded_alpha) or not _close(recon_beta, recorded_beta):
                mismatches.append(Mismatch(
                    component="alpha_beta",
                    run_label=run_label,
                    turn_index=t.turn_index,
                    detail=(
                        f"edge_id={edge_id} reconstructed=({recon_alpha:.6f},{recon_beta:.6f}) "
                        f"recorded=({recorded_alpha:.6f},{recorded_beta:.6f})"
                    ),
                ))
    return mismatches


def verify_topology_tieback(
    reconstructor: GraphReconstructor, turns: list[Turn], run_label: str
) -> list[Mismatch]:
    """
    Component 2 — existence AND completeness, per turn.

    Existence: every (source, target) pair recorded in traversal_path must
    already exist in the reconstructed graph by that turn (and for seed
    pairs, the recorded edge_id must match the fixture's deterministic id —
    a stronger check available only for that class).

    Completeness: every reconstructed K1-outgoing pair to a non-K1 target
    must appear in the recorded traversal_path — i.e. the reconstructor
    invents nothing the real retrieval didn't also walk.
    """
    mismatches: list[Mismatch] = []
    for t in turns:
        recorded_pairs: dict[tuple[str, str], str] = {
            (hop["source_mu_id"], hop["target_mu_id"]): hop["edge_id"]
            for hop in t.traversal_path
        }

        for (source, target), edge_id in recorded_pairs.items():
            if not reconstructor.edge_exists_at(source, target, t.turn_index):
                mismatches.append(Mismatch(
                    component="topology_existence",
                    run_label=run_label,
                    turn_index=t.turn_index,
                    detail=f"traversed pair {(source, target)} (edge_id={edge_id}) not reconstructible by this turn",
                ))
                continue
            if reconstructor.is_seed_pair(source, target):
                expected_id = reconstructor.seed.edge_by_pair[(source, target)]
                if expected_id != edge_id:
                    mismatches.append(Mismatch(
                        component="topology_seed_id",
                        run_label=run_label,
                        turn_index=t.turn_index,
                        detail=(
                            f"seed pair {(source, target)} recorded edge_id={edge_id} "
                            f"but fixture assigns {expected_id}"
                        ),
                    ))

        k1_set = set(t.entry_point_mu_ids)
        for k1_mu in t.entry_point_mu_ids:
            for (source, target) in reconstructor.outgoing_pairs_at(k1_mu, t.turn_index):
                if target in k1_set:
                    continue
                if (source, target) not in recorded_pairs:
                    mismatches.append(Mismatch(
                        component="topology_completeness",
                        run_label=run_label,
                        turn_index=t.turn_index,
                        detail=(
                            f"reconstructed K1-outgoing pair {(source, target)} "
                            "missing from recorded traversal_path"
                        ),
                    ))
    return mismatches


def verify_slate_tieback(turns: list[Turn], run_label: str) -> list[Mismatch]:
    """
    Component 3 — replay K1 (from the already-sorted candidate_set_before_
    ranking) and K2 (from the recorded traversal_path, with edge_weights_used
    weights INJECTED rather than resampled), and compare against recorded
    activated_mu_ids. Trusts traversal_path as the correct enumeration of
    edges walked — that trust is exactly what verify_topology_tieback just
    validated, so this must run after it, not instead of it.
    """
    mismatches: list[Mismatch] = []
    for t in turns:
        hp = t.hyperparameters_snapshot
        level1_k = hp.get("retrieval_level1_top_k", 3)
        level2_k = hp.get("retrieval_level2_top_k", 1)
        dir_multiplier = float(hp.get("directional_edge_weight_multiplier", 1.5))

        # --- K1 ---
        k1_expected = [c["mu_id"] for c in t.candidate_set_before_ranking[:level1_k]]
        if k1_expected != t.entry_point_mu_ids:
            mismatches.append(Mismatch(
                component="slate_k1",
                run_label=run_label,
                turn_index=t.turn_index,
                detail=f"expected={k1_expected} recorded={t.entry_point_mu_ids}",
            ))
            continue  # K2 replay below assumes a correct K1 set; skip rather than cascade

        # --- K2 ---
        k1_set = set(t.entry_point_mu_ids)
        k2_scores: dict[str, float] = {}
        missing_weight = False
        for hop in t.traversal_path:
            target = hop["target_mu_id"]
            if target in k1_set:
                continue
            edge_id = hop["edge_id"]
            weight_info = t.edge_weights_used.get(edge_id)
            if weight_info is None or weight_info.get("weight_used") is None:
                mismatches.append(Mismatch(
                    component="slate_k2_missing_weight",
                    run_label=run_label,
                    turn_index=t.turn_index,
                    detail=f"edge_id={edge_id} has no recorded weight_used to inject",
                ))
                missing_weight = True
                continue
            weight_used = float(weight_info["weight_used"])
            k2_scores[target] = k2_scores.get(target, 0.0) + weight_used * dir_multiplier
        if missing_weight:
            continue

        sorted_k2 = sorted(k2_scores.items(), key=lambda x: x[1], reverse=True)
        k2_expected = [mu_id for mu_id, _ in sorted_k2[:level2_k]]
        k2_recorded = t.activated_mu_ids[len(t.entry_point_mu_ids):]
        if k2_expected != k2_recorded:
            mismatches.append(Mismatch(
                component="slate_k2",
                run_label=run_label,
                turn_index=t.turn_index,
                detail=f"expected={k2_expected} recorded={k2_recorded}",
            ))
    return mismatches


# ---------------------------------------------------------------------------
# Phase 2 — probe-side data loading (gold labels, probe measurements, plans)
# ---------------------------------------------------------------------------


def load_probe_set(version: str = PROBE_SET_VERSION, path: Optional[Path] = None) -> dict[str, str]:
    """
    probe_id -> gold_mu_id, from probes/probe_set_{version}.yaml.

    v3's spoke_retrieval battery is one gold per probe (verified against
    every probe in the file). A probe with zero or more than one
    gold_mu_ids is logged and skipped rather than guessed at — none exist
    in v3 today, but a future battery (e.g. a bridge probe with two golds)
    should not be silently mishandled by this partition, which assumes a
    single gold.
    """
    probe_set_path = path or (PROBES_DIR / f"probe_set_{version}.yaml")
    with open(probe_set_path, "r", encoding="utf-8") as fh:
        data = yaml.safe_load(fh)

    gold_map: dict[str, str] = {}
    skipped = 0
    for p in data.get("probes", []):
        golds = p.get("gold_mu_ids") or []
        if len(golds) != 1:
            logger.warning(
                "alpha_d: probe %s has %d gold_mu_ids (expected exactly 1) — "
                "skipped for Phase 2 metrics", p.get("probe_id"), len(golds),
            )
            skipped += 1
            continue
        gold_map[p["probe_id"]] = golds[0]
    logger.info(
        "alpha_d: loaded probe set %s with %d single-gold probes (%d skipped)",
        version, len(gold_map), skipped,
    )
    return gold_map


@dataclass
class ProbeMeasurement:
    """One battery_mode="mean" probe_results row — one deterministic
    reading of one probe at one measurement session."""

    probe_id: str
    measurement_session_index: int
    retrieved_mu_ids: list[str]
    gold_mu_rank: Optional[int]


def load_probe_measurements(
    db_path: Path, experiment_id: str, battery_mode: str = "mean"
) -> list[ProbeMeasurement]:
    """
    Mean-mode probe rows for one experiment — see the module docstring's
    "battery_mode='mean' only" note for why sample-mode rows are excluded.
    """
    con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    try:
        rows = con.execute(
            "SELECT probe_id, measurement_session_index, retrieved_mu_ids, gold_mu_rank "
            "FROM probe_results WHERE experiment_id = ? AND battery_mode = ? "
            "ORDER BY measurement_session_index, probe_id",
            (experiment_id, battery_mode),
        ).fetchall()
    except sqlite3.Error as exc:
        logger.error("alpha_d: could not read probe_results for %s: %s", experiment_id, exc)
        return []
    finally:
        con.close()

    measurements = [
        ProbeMeasurement(
            probe_id=r["probe_id"],
            measurement_session_index=r["measurement_session_index"],
            retrieved_mu_ids=json.loads(r["retrieved_mu_ids"] or "[]"),
            gold_mu_rank=r["gold_mu_rank"],
        )
        for r in rows
    ]
    logger.info(
        "alpha_d: loaded %d battery_mode=%s probe measurements for %s",
        len(measurements), battery_mode, experiment_id,
    )
    return measurements


def plan_path_for_basename(basename: str) -> Path:
    """
    Plan filenames put `_` where RUNS basenames put `-` before the trailing
    date (e.g. basename "exp0b-rep2-2026-08-09" -> plan file
    "exp0b-rep2_2026-08-09.json") — derived from the naming convention
    rather than hardcoded per run, since it holds for all 8 plan files in
    scope (checked against the real plans/ directory).
    """
    date_part = basename[-10:]
    prefix = basename[:-11]
    return PLANS_DIR / f"{prefix}_{date_part}.json"


def load_trained_spoke_golds(basename: str, gold_map: dict[str, str]) -> dict[str, str]:
    """
    cluster letter -> gold_mu_id of that cluster's TRAINED spoke, for
    anchor strength (a longitudinal metric about the spoke the run actually
    trained, not the 6 negative-control spokes per cluster).

    Read from the run's plan file (RunPlan.trained_spokes, e.g.
    {"cluster_a": "a_spoke1"} — not from the archived report JSON's
    learning_measures.trained_probes, which is empty {} in every report
    checked during Phase 2 build — a pre-existing report-generation gap,
    out of this milestone's scope to fix). Resolved to a probe_id
    (`vs_{letter}{n}`, the v3 naming convention — see probe_set_v3.yaml's
    header comment) and then to a gold_mu_id via the probe set.

    Returns {} (warning, not raise) on any lookup failure — anchor strength
    is a secondary metric and a missing/unparseable plan should not abort
    the whole run's bucket/K2-rank read.
    """
    path = plan_path_for_basename(basename)
    if not path.exists():
        logger.warning(
            "alpha_d: no plan file at %s — cannot resolve trained spokes for anchor strength", path
        )
        return {}
    with open(path, "r", encoding="utf-8") as fh:
        plan = json.load(fh)
    trained_spokes = plan.get("trained_spokes") or {}

    result: dict[str, str] = {}
    for cluster_key, spoke_name in trained_spokes.items():
        cluster = cluster_key.replace("cluster_", "")
        m = re.match(r"^([a-d])_spoke(\d+)$", spoke_name)
        if not m or m.group(1) != cluster:
            logger.warning(
                "alpha_d: could not parse trained spoke %r for %s in %s",
                spoke_name, cluster_key, path,
            )
            continue
        probe_id = f"vs_{cluster}{m.group(2)}"
        gold = gold_map.get(probe_id)
        if gold is None:
            logger.warning("alpha_d: trained spoke probe %s has no gold in the probe set", probe_id)
            continue
        result[cluster] = gold
    return result


# ---------------------------------------------------------------------------
# Phase 2 — session boundaries
# ---------------------------------------------------------------------------


def compute_session_cutoffs(turns: list[Turn]) -> dict[int, int]:
    """
    measurement_session_index -> as_of_turn_index — the turn_index to pass
    into edge_exists_at() / outgoing_pairs_at() / alpha_beta_as_of_turn_index()
    so the reconstructed graph state matches exactly what a probe battery at
    that session saw.

    Derived empirically from analytics.session_turn_index resets to 0 (each
    such row marks a new bulk-turn session's first real turn) rather than
    assumed from a constant turns-per-session — see the module docstring's
    "probe measurement session -> real-turn graph state" note for why the
    end-of-session alignment holds for every run in scope. measurement_
    session_index k (1-based) maps to "the state after bulk session k
    completes": strictly before session k+1's first turn_index, or — for
    the run's last session — one past its very last real turn_index.
    """
    starts = sorted(t.turn_index for t in turns if t.session_turn_index == 0)
    if not starts:
        return {}
    max_turn_index = max(t.turn_index for t in turns)
    cutoffs: dict[int, int] = {}
    for i, start in enumerate(starts):
        session_number = i + 1
        cutoffs[session_number] = starts[i + 1] if i + 1 < len(starts) else max_turn_index + 1
    return cutoffs


# ---------------------------------------------------------------------------
# Phase 2 — the four-bucket partition, chase-down yield, K2 runner-up rank
# ---------------------------------------------------------------------------

BUCKET_REACHED = "reached"
BUCKET_OUTBID = "outbid"
BUCKET_OUT_OF_DEPTH = "out_of_depth"
BUCKET_DISCONNECTED = "disconnected"
ALL_BUCKETS = (BUCKET_REACHED, BUCKET_OUTBID, BUCKET_OUT_OF_DEPTH, BUCKET_DISCONNECTED)


def min_hop_distance(
    reconstructor: GraphReconstructor,
    sources: set[str],
    target: str,
    as_of_turn_index: int,
    max_hops: int = PATHBACK_HORIZON,
) -> Optional[int]:
    """
    Shortest directed distance from the union of `sources` to `target`, up
    to max_hops, or None if unreachable within that horizon. BFS over
    outgoing_pairs_at() — the same visible-topology-at-a-turn view Phase 1's
    topology tie-back already validated against real turns; the `direction`
    column is never consulted (see BUILD-PLAN.md's directed-reachability
    risk item).
    """
    frontier = set(sources)
    visited = set(sources)
    for hop in range(1, max_hops + 1):
        next_frontier: set[str] = set()
        for node in frontier:
            for (_source, dest) in reconstructor.outgoing_pairs_at(node, as_of_turn_index):
                if dest == target:
                    return hop
                if dest not in visited:
                    next_frontier.add(dest)
        visited |= next_frontier
        frontier = next_frontier
        if not frontier:
            break
    return None


def classify_bucket(
    reconstructor: GraphReconstructor,
    retrieved_mu_ids: list[str],
    gold_mu_id: str,
    as_of_turn_index: int,
) -> str:
    """
    The four-bucket exclusive partition — BUILD-PLAN.md's "The partition
    (headline metric)" section. Priority order matters: each condition is
    only checked after the previous one has failed, which is what keeps the
    four buckets exclusive and exhaustive by construction.

    REACHED and OUTBID describe what retrieve() actually computed (gold was
    retrieved, or gold was a real K2 candidate that lost a one-slot argmax).
    OUT_OF_DEPTH is COUNTERFACTUAL — retrieve() never walks a second hop —
    and must be labeled as such wherever this bucket is displayed.
    """
    if gold_mu_id in retrieved_mu_ids:
        return BUCKET_REACHED
    distance = min_hop_distance(reconstructor, set(retrieved_mu_ids), gold_mu_id, as_of_turn_index)
    if distance == 1:
        return BUCKET_OUTBID
    if distance == 2:
        return BUCKET_OUT_OF_DEPTH
    return BUCKET_DISCONNECTED


def chase_down_reaches_gold(
    reconstructor: GraphReconstructor,
    start_mu_id: str,
    gold_mu_id: str,
    as_of_turn_index: int,
    turn: Turn,
    max_hops: int = PATHBACK_HORIZON,
) -> bool:
    """
    COUNTERFACTUAL — retrieve() never performs this walk; it dry-runs item
    F's proposed read-side chase-down stage. Greedily follows the single
    highest-mean-weight outgoing edge from `start_mu_id`, up to `max_hops`
    steps, and reports whether that one path lands on gold. Ties broken by
    target_mu_id for determinism (documented, not expected to matter at
    this graph's scale — spoke fan-out is small).
    """
    current = start_mu_id
    for _ in range(max_hops):
        pairs = reconstructor.outgoing_pairs_at(current, as_of_turn_index)
        if not pairs:
            return False
        ranked = sorted(
            pairs,
            key=lambda st: (
                -reconstructor.mean_weight_for_pair(st[0], st[1], as_of_turn_index, turn),
                st[1],
            ),
        )
        _, next_mu = ranked[0]
        if next_mu == gold_mu_id:
            return True
        current = next_mu
    return False


def turn_chase_down_succeeds(
    reconstructor: GraphReconstructor,
    retrieved_mu_ids: list[str],
    gold_mu_id: str,
    as_of_turn_index: int,
    turn: Turn,
) -> bool:
    """Bucket-level chase-down: succeeds if ANY retrieved MU's own greedy
    walk reaches gold within the horizon. Meaningful only for OUTBID/
    OUT_OF_DEPTH turns — a REACHED turn has already succeeded by definition
    and a DISCONNECTED turn has no path to find."""
    return any(
        chase_down_reaches_gold(reconstructor, mu, gold_mu_id, as_of_turn_index, turn)
        for mu in retrieved_mu_ids
    )


def k2_runner_up_rank(
    reconstructor: GraphReconstructor,
    retrieved_mu_ids: list[str],
    gold_mu_id: str,
    as_of_turn_index: int,
    turn: Turn,
    level1_k: int = PROBE_RETRIEVAL_LEVEL1_TOP_K,
    dir_multiplier: float = PROBE_DIRECTIONAL_EDGE_WEIGHT_MULTIPLIER,
) -> Optional[int]:
    """
    1-indexed rank of gold among K2 candidates (targets of K1-outgoing
    edges, excluding K1 itself), scored score(target) = sum over K1 sources
    of mean_weight(source, target) * dir_multiplier — replaying retrieve()'s
    real K2 scoring formula (the same one verify_slate_tieback validated
    against real turns in Phase 1) using the reconstructed posterior mean.

    NOT a counterfactual: battery_mode="mean" probe measurement IS the
    mean-mode read, so this reproduces what retrieve() actually computed
    for this probe, not a hypothetical. Returns None if gold never entered
    the K2 candidate pool at all (no K1-outgoing edge reaches it).
    """
    k1_set = retrieved_mu_ids[:level1_k]
    scores: dict[str, float] = {}
    for k1_mu in k1_set:
        for (source, target) in reconstructor.outgoing_pairs_at(k1_mu, as_of_turn_index):
            if target in k1_set:
                continue
            weight = reconstructor.mean_weight_for_pair(source, target, as_of_turn_index, turn)
            scores[target] = scores.get(target, 0.0) + weight * dir_multiplier
    if gold_mu_id not in scores:
        return None
    ranked = sorted(scores.items(), key=lambda item: -item[1])
    for rank, (mu_id, _score) in enumerate(ranked, start=1):
        if mu_id == gold_mu_id:
            return rank
    return None  # unreachable given the membership check above


# ---------------------------------------------------------------------------
# Phase 2 — anchor strength (the write-side longitudinal metric)
# ---------------------------------------------------------------------------


def hub_mu_id_for_gold(seed: SeedFixture, gold_mu_id: str) -> Optional[str]:
    """
    The cluster hub's mu_id for a spoke gold, derived from the fixture's
    own naming convention (`{cluster}0000001-...` is always the hub —
    confirmed against every MU id in seed_v3.json.gz) rather than
    hardcoded, so this stays correct if the fixture ever changes. Returns
    None (with a warning) if the convention doesn't resolve to exactly one
    MU — e.g. for a bridge MU gold, which has no single cluster-letter hub.
    """
    if not gold_mu_id:
        return None
    cluster = gold_mu_id[0]
    prefix = f"{cluster}0000001-"
    matches = [mu_id for mu_id in seed.mu_ids if mu_id.startswith(prefix)]
    if len(matches) != 1:
        logger.warning(
            "alpha_d: could not resolve a unique hub for gold=%s (prefix %r matched %d MUs)",
            gold_mu_id, prefix, len(matches),
        )
        return None
    return matches[0]


def hub_to_gold_trajectory(
    reconstructor: GraphReconstructor,
    hub_mu_id: str,
    gold_mu_id: str,
    session_cutoffs: dict[int, int],
    turn: Turn,
) -> dict[int, tuple[float, float]]:
    """
    alpha/beta of the seed-fixture hub->gold edge, as of each session
    cutoff — part 1 of anchor strength. Empty (with a warning) if no such
    seed edge exists, which should not happen for the v3 spoke_retrieval
    battery (every spoke has a reciprocal hub<->spoke pair) but is checked
    rather than assumed.
    """
    pair = (hub_mu_id, gold_mu_id)
    edge_id = reconstructor.seed.edge_by_pair.get(pair)
    if edge_id is None:
        logger.warning(
            "alpha_d: no seed edge %s -> %s — cannot trace anchor strength", hub_mu_id, gold_mu_id
        )
        return {}
    return {
        session: reconstructor.alpha_beta_as_of_turn_index(edge_id, cutoff, turn)
        for session, cutoff in session_cutoffs.items()
    }


@dataclass
class CoActivationCensus:
    created: int
    traversed: int
    credited: int


def gold_co_activation_census(reconstructor: GraphReconstructor, gold_mu_id: str) -> CoActivationCensus:
    """
    Part 2 of anchor strength: output->gold co-activation edges over the
    WHOLE run (these only ever accumulate, so there is no session-scoping
    question). Counts how many were ever created (gold appeared in some
    real turn's activated_mu_ids), how many of those pairs were ever
    traversed by a LATER turn's own retrieval (recoverable edge_id, via
    traversed_pair_edge_ids), and how many of those were ever credited
    (received a weight-update event). An edge can only be credited if it
    was traversed at least once — credit assignment walks a turn's
    traversal_path — so "traversed" is a precondition "credited" can never
    skip, and this is the same structural fact
    creation_prior_for_pair()/mean_weight_for_pair() lean on in the other
    direction (an untraversed pair is provably never credited).
    """
    created_pairs = [pair for pair in reconstructor.pair_to_creation_turn if pair[1] == gold_mu_id]
    traversed_pairs = [p for p in created_pairs if p in reconstructor.traversed_pair_edge_ids]
    credited_pairs = [
        p for p in traversed_pairs
        if reconstructor.edge_events.get(reconstructor.traversed_pair_edge_ids[p])
    ]
    return CoActivationCensus(
        created=len(created_pairs), traversed=len(traversed_pairs), credited=len(credited_pairs),
    )


# ---------------------------------------------------------------------------
# Driver
# ---------------------------------------------------------------------------


def run_tieback_for_experiment(
    db_path: Path, seed: SeedFixture, experiment_basename: str, run_label: str
) -> tuple[list[Turn], list[Mismatch]]:
    """Load one run and execute all three tie-back components against it."""
    experiment_id = f"{experiment_basename}_learned"
    turns = load_turns(db_path, experiment_id)
    if not turns:
        logger.error("alpha_d: no turns loaded for %s — cannot tie back", experiment_id)
        return [], [Mismatch("load", run_label, -1, f"no turns found for {experiment_id}")]

    edge_events = load_edge_update_events(db_path, experiment_id)
    reconstructor = GraphReconstructor(seed, turns, edge_events)

    mismatches: list[Mismatch] = []
    mismatches += verify_alpha_beta_tieback(reconstructor, turns, run_label)
    mismatches += verify_topology_tieback(reconstructor, turns, run_label)
    # Slate tie-back trusts traversal_path's enumeration, which topology
    # tie-back just validated — run it after, not concurrently.
    mismatches += verify_slate_tieback(turns, run_label)
    return turns, mismatches


@dataclass
class RunPathbackMetrics:
    """One run's full Phase 2 read: the bucket partition, chase-down yield,
    K2 runner-up rank distribution, and anchor strength for its 4 trained
    spokes."""

    run_label: str
    condition: str
    bucket_counts: dict[str, int]
    bucket_counts_by_session: dict[int, dict[str, int]]
    chase_down_successes: int
    chase_down_eligible: int
    k2_rank_histogram: dict[int, int]
    k2_no_rank_count: int
    anchor_trajectories: dict[str, dict[int, tuple[float, float]]]
    co_activation_census: dict[str, CoActivationCensus]


def compute_run_metrics(
    db_path: Path,
    seed: SeedFixture,
    gold_map: dict[str, str],
    basename: str,
    condition: str,
    run_label: str,
) -> Optional[RunPathbackMetrics]:
    """Load one run's real turns + probe measurements and compute every
    Phase 2 metric against it. Returns None (logged) if the run's real
    turns can't be loaded — Phase 2 needs them to build the reconstructor,
    exactly as Phase 1 does."""
    experiment_id = f"{basename}_learned"
    turns = load_turns(db_path, experiment_id)
    if not turns:
        logger.error("alpha_d: no real turns for %s — cannot compute Phase 2 metrics", experiment_id)
        return None

    edge_events = load_edge_update_events(db_path, experiment_id)
    reconstructor = GraphReconstructor(seed, turns, edge_events)
    session_cutoffs = compute_session_cutoffs(turns)
    # Any turn's edge_update_cfg works here — the run's edge-update config
    # (priors, seed priors) is constant across all its turns.
    reference_turn = turns[0]

    probes = load_probe_measurements(db_path, experiment_id, battery_mode="mean")
    expected_sessions = set(session_cutoffs)
    seen_sessions = {p.measurement_session_index for p in probes}
    if seen_sessions != expected_sessions:
        logger.warning(
            "alpha_d: %s — real-turn sessions %s and probe sessions %s disagree",
            run_label, sorted(expected_sessions), sorted(seen_sessions),
        )

    bucket_counts: dict[str, int] = {b: 0 for b in ALL_BUCKETS}
    bucket_counts_by_session: dict[int, dict[str, int]] = {}
    chase_down_successes = 0
    chase_down_eligible = 0
    k2_rank_histogram: dict[int, int] = {}
    k2_no_rank_count = 0

    for p in probes:
        gold = gold_map.get(p.probe_id)
        if gold is None:
            continue  # unresolvable gold — already warned by load_probe_set()
        as_of = session_cutoffs.get(p.measurement_session_index)
        if as_of is None:
            continue  # already warned above

        bucket = classify_bucket(reconstructor, p.retrieved_mu_ids, gold, as_of)
        bucket_counts[bucket] += 1
        session_counts = bucket_counts_by_session.setdefault(
            p.measurement_session_index, {b: 0 for b in ALL_BUCKETS}
        )
        session_counts[bucket] += 1

        if bucket in (BUCKET_OUTBID, BUCKET_OUT_OF_DEPTH):
            chase_down_eligible += 1
            if turn_chase_down_succeeds(reconstructor, p.retrieved_mu_ids, gold, as_of, reference_turn):
                chase_down_successes += 1

        rank = k2_runner_up_rank(reconstructor, p.retrieved_mu_ids, gold, as_of, reference_turn)
        if rank is None:
            k2_no_rank_count += 1
        else:
            k2_rank_histogram[rank] = k2_rank_histogram.get(rank, 0) + 1

    trained_golds = load_trained_spoke_golds(basename, gold_map)
    anchor_trajectories: dict[str, dict[int, tuple[float, float]]] = {}
    co_activation_census: dict[str, CoActivationCensus] = {}
    for cluster, gold in trained_golds.items():
        hub = hub_mu_id_for_gold(seed, gold)
        if hub is not None:
            anchor_trajectories[cluster] = hub_to_gold_trajectory(
                reconstructor, hub, gold, session_cutoffs, reference_turn
            )
        co_activation_census[cluster] = gold_co_activation_census(reconstructor, gold)

    return RunPathbackMetrics(
        run_label=run_label,
        condition=condition,
        bucket_counts=bucket_counts,
        bucket_counts_by_session=bucket_counts_by_session,
        chase_down_successes=chase_down_successes,
        chase_down_eligible=chase_down_eligible,
        k2_rank_histogram=k2_rank_histogram,
        k2_no_rank_count=k2_no_rank_count,
        anchor_trajectories=anchor_trajectories,
        co_activation_census=co_activation_census,
    )


def render_phase2_markdown(all_metrics: list[RunPathbackMetrics]) -> str:
    """The docs/evaluations/alpha_d_pathback_analysis.md content — headline
    bucket table (pooled by condition and per-run), the two counterfactuals
    labeled as such, the K2 runner-up rank distribution priced at K=2/3/5,
    and per-run anchor strength."""
    lines: list[str] = []
    lines.append("# Alpha D — path-back instrumentation analysis")
    lines.append("")
    lines.append(
        "Generated by `scripts/alpha_d_pathback.py --phase2`. Existing data only — "
        "no new runs, no live backend. All numbers below are computed against "
        "`battery_mode=\"mean\"` probe measurements (28 probes x 5 sessions x 8 "
        "runs = 1,120 measurements)."
    )
    lines.append("")

    # --- Headline: pooled bucket distribution by condition ---
    lines.append("## Headline — bucket distribution by condition")
    lines.append("")
    lines.append(
        "_REACHED and OUTBID are **observed** — they describe what `retrieve()` "
        "actually computed. OUT_OF_DEPTH and DISCONNECTED describe topology "
        "**beyond** what retrieval currently walks; OUT_OF_DEPTH in particular is "
        "a COUNTERFACTUAL (a 2-hop walk `retrieve()` never performs)._"
    )
    lines.append("")
    header = "| condition | n | " + " | ".join(b for b in ALL_BUCKETS) + " |"
    lines.append(header)
    lines.append("|" + "---|" * (len(ALL_BUCKETS) + 2))
    by_condition: dict[str, list[RunPathbackMetrics]] = {}
    for m in all_metrics:
        by_condition.setdefault(m.condition, []).append(m)
    for condition, metrics_list in by_condition.items():
        pooled: dict[str, int] = {b: 0 for b in ALL_BUCKETS}
        for m in metrics_list:
            for b in ALL_BUCKETS:
                pooled[b] += m.bucket_counts[b]
        n = sum(pooled.values())
        cells = " | ".join(f"{pooled[b]}/{n} ({pooled[b]/n:.0%})" if n else "—" for b in ALL_BUCKETS)
        lines.append(f"| {condition} | {n} | {cells} |")
    lines.append("")

    # --- Per-run bucket table ---
    lines.append("## Per-run bucket distribution")
    lines.append("")
    per_run_header = "| run | n | " + " | ".join(b for b in ALL_BUCKETS) + " |"
    lines.append(per_run_header)
    lines.append("|" + "---|" * (len(ALL_BUCKETS) + 2))
    for m in all_metrics:
        n = sum(m.bucket_counts.values())
        cells = " | ".join(
            f"{m.bucket_counts[b]}/{n} ({m.bucket_counts[b]/n:.0%})" if n else "—" for b in ALL_BUCKETS
        )
        lines.append(f"| {m.run_label} | {n} | {cells} |")
    lines.append("")

    # --- Chase-down yield (counterfactual) ---
    lines.append("## Chase-down yield — COUNTERFACTUAL")
    lines.append("")
    lines.append(
        "_Conditional rate within OUTBID+OUT_OF_DEPTH turns: from each retrieved "
        "MU, greedily follow the highest-mean-weight outgoing edge up to 2 hops — "
        "does any such walk land on gold? `retrieve()` never performs this walk; "
        "this dry-runs item F's proposed read-side chase-down stage._"
    )
    lines.append("")
    lines.append("| run | eligible (outbid+out_of_depth) | reaches gold | yield |")
    lines.append("|---|---|---|---|")
    total_eligible = 0
    total_success = 0
    for m in all_metrics:
        total_eligible += m.chase_down_eligible
        total_success += m.chase_down_successes
        rate = f"{m.chase_down_successes/m.chase_down_eligible:.0%}" if m.chase_down_eligible else "—"
        lines.append(f"| {m.run_label} | {m.chase_down_eligible} | {m.chase_down_successes} | {rate} |")
    pooled_rate = f"{total_success/total_eligible:.0%}" if total_eligible else "—"
    lines.append(f"| **pooled** | {total_eligible} | {total_success} | {pooled_rate} |")
    lines.append("")

    # --- K2 runner-up rank distribution ---
    lines.append("## Gold-as-K2-runner-up rank distribution")
    lines.append("")
    lines.append(
        "_Of turns where gold entered the K2 candidate pool, its rank there "
        "(descending score). Prices `retrieval_level2_top_k` retrospectively: "
        "P(rank <= K) is the pass rate a real config declaring that K would have "
        "measured, at every probe in scope — not just cfgB's declared value, "
        "which M44 found never actually reached the probe path. NOT a "
        "counterfactual — battery_mode=\"mean\" probe measurement already used "
        "these exact posterior-mean weights._"
    )
    lines.append("")
    max_rank = max((r for m in all_metrics for r in m.k2_rank_histogram), default=0)
    lines.append(
        "| run | never entered pool | " + " | ".join(f"rank {r}" for r in range(1, max_rank + 1))
        + " | P(<=2) | P(<=3) | P(<=5) |"
    )
    lines.append("|" + "---|" * (max_rank + 5))
    pooled_hist: dict[int, int] = {}
    pooled_no_rank = 0
    for m in all_metrics:
        pooled_no_rank += m.k2_no_rank_count
        total_in_pool = sum(m.k2_rank_histogram.values())
        cells = " | ".join(str(m.k2_rank_histogram.get(r, 0)) for r in range(1, max_rank + 1))
        p_at = {
            k: sum(c for r, c in m.k2_rank_histogram.items() if r <= k) / total_in_pool
            if total_in_pool else 0.0
            for k in (2, 3, 5)
        }
        lines.append(
            f"| {m.run_label} | {m.k2_no_rank_count} | {cells} | "
            f"{p_at[2]:.0%} | {p_at[3]:.0%} | {p_at[5]:.0%} |"
        )
        for r, c in m.k2_rank_histogram.items():
            pooled_hist[r] = pooled_hist.get(r, 0) + c
    pooled_total_in_pool = sum(pooled_hist.values())
    pooled_cells = " | ".join(str(pooled_hist.get(r, 0)) for r in range(1, max_rank + 1))
    pooled_p_at = {
        k: sum(c for r, c in pooled_hist.items() if r <= k) / pooled_total_in_pool
        if pooled_total_in_pool else 0.0
        for k in (2, 3, 5)
    }
    lines.append(
        f"| **pooled** | {pooled_no_rank} | {pooled_cells} | "
        f"{pooled_p_at[2]:.0%} | {pooled_p_at[3]:.0%} | {pooled_p_at[5]:.0%} |"
    )
    lines.append("")

    # --- Anchor strength ---
    lines.append("## Anchor strength — trained spokes only")
    lines.append("")
    lines.append(
        "_alpha/beta of the seed-fixture hub->gold edge across sessions, for "
        "each run's 4 trained spokes (not the 24 negative-control spokes). "
        "mean weight = alpha/(alpha+beta)._"
    )
    lines.append("")
    for m in all_metrics:
        if not m.anchor_trajectories:
            continue
        lines.append(f"### {m.run_label}")
        lines.append("")
        sessions = sorted({s for traj in m.anchor_trajectories.values() for s in traj})
        lines.append("| cluster | " + " | ".join(f"s{s} mean" for s in sessions) + " | created | traversed | credited |")
        lines.append("|" + "---|" * (len(sessions) + 4))
        for cluster, traj in sorted(m.anchor_trajectories.items()):
            cell_values = []
            for s in sessions:
                if s not in traj:
                    cell_values.append("—")
                    continue
                alpha, beta = traj[s]
                cell_values.append(f"{alpha/(alpha+beta):.3f}" if (alpha + beta) > 0 else "—")
            cells = " | ".join(cell_values)
            census = m.co_activation_census.get(cluster)
            census_cells = (
                f"{census.created} | {census.traversed} | {census.credited}" if census else "— | — | —"
            )
            lines.append(f"| {cluster} | {cells} | {census_cells} |")
        lines.append("")

    return "\n".join(lines)


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Alpha D — path-back reconstructor tie-back (Phase 1) and metrics (Phase 2)"
    )
    parser.add_argument("--db", type=Path, default=GRAPH_DB, help="graph.db path, read-only")
    parser.add_argument(
        "--out", type=Path, default=None,
        help="optional path to write a JSON mismatch dump for debugging",
    )
    parser.add_argument(
        "--phase2", action="store_true",
        help="also compute Phase 2 metrics (buckets, chase-down yield, K2 rank, anchor strength)",
    )
    parser.add_argument(
        "--phase2-out", type=Path, default=None,
        help="path to write the Phase 2 markdown analysis (implies --phase2)",
    )
    args = parser.parse_args()
    if args.phase2_out:
        args.phase2 = True

    if not args.db.exists():
        logger.error("alpha_d: graph db not found at %s", args.db)
        raise SystemExit(1)

    seed = load_seed_fixture()

    all_mismatches: list[Mismatch] = []
    total_turns = 0
    print(f"{'run':<14} {'turns':>6} {'alpha/beta':>11} {'topology':>9} {'slate':>7}")
    for basename, _condition, label in RUNS:
        turns, mismatches = run_tieback_for_experiment(args.db, seed, basename, label)
        total_turns += len(turns)
        all_mismatches += mismatches
        by_component: dict[str, int] = {}
        for m in mismatches:
            by_component[m.component] = by_component.get(m.component, 0) + 1
        alpha_beta_n = by_component.get("alpha_beta", 0)
        topology_n = sum(
            by_component.get(c, 0)
            for c in ("topology_existence", "topology_seed_id", "topology_completeness")
        )
        slate_n = sum(
            by_component.get(c, 0)
            for c in ("slate_k1", "slate_k2", "slate_k2_missing_weight")
        )
        print(f"{label:<14} {len(turns):>6} {alpha_beta_n:>11} {topology_n:>9} {slate_n:>7}")

    print()
    if all_mismatches:
        print(f"FAIL — {len(all_mismatches)} mismatches across {total_turns} turns")
    else:
        print(f"PASS — 0 mismatches across {total_turns} turns")

    if args.out:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(
            json.dumps([m.__dict__ for m in all_mismatches], indent=2),
            encoding="utf-8",
        )
        logger.info("alpha_d: wrote %d mismatches to %s", len(all_mismatches), args.out)

    if args.phase2:
        gold_map = load_probe_set()
        print()
        print("Phase 2 — bucket distribution by run")
        print(f"{'run':<14} {'n':>5} " + " ".join(f"{b:>13}" for b in ALL_BUCKETS))
        all_metrics: list[RunPathbackMetrics] = []
        for basename, condition, label in RUNS:
            metrics = compute_run_metrics(args.db, seed, gold_map, basename, condition, label)
            if metrics is None:
                continue
            all_metrics.append(metrics)
            n = sum(metrics.bucket_counts.values())
            print(f"{label:<14} {n:>5} " + " ".join(f"{metrics.bucket_counts[b]:>13}" for b in ALL_BUCKETS))

        if args.phase2_out:
            args.phase2_out.parent.mkdir(parents=True, exist_ok=True)
            args.phase2_out.write_text(render_phase2_markdown(all_metrics), encoding="utf-8")
            logger.info("alpha_d: wrote Phase 2 analysis to %s", args.phase2_out)

    if all_mismatches:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
