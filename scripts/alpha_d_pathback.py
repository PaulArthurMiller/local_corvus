"""
Plan Concept Alpha item D — path-back instrumentation.

Phase 1 (this file, current scope): the graph reconstructor and its
three-component tie-back. Rebuilds per-run graph topology and edge (alpha,
beta) trajectories at any turn index, purely from sources that survive a
graph wipe (`memory/graph.db`'s `analytics` and `edge_update_events` tables,
plus `seed_fixtures/seed_v3.json.gz`) — never from the live `edges` /
`memory_units` tables, which by now hold only the most recent wipe+reseed's
state and no longer carry the 8 archived runs' conversation topology at all.

Phase 2 (future) adds the metrics layer on top of this validated
reconstructor: the four-bucket path-back partition, chase-down yield, the
K2 runner-up rank distribution, and anchor strength. See BUILD-PLAN.md's
"Path-Back Instrumentation (Plan Alpha D) — Milestone 45" section for the
full design, including why the partition is directed / horizon-2 / and why
archived data (not a fresh run) can answer this.

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
import sqlite3
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
logger = logging.getLogger("alpha_d_pathback")

REPO_ROOT = Path(__file__).resolve().parent.parent
GRAPH_DB = REPO_ROOT / "memory" / "graph.db"
AUDIT_LOG = REPO_ROOT / "memory" / "audit.log"
SEED_FIXTURES_DIR = REPO_ROOT / "seed_fixtures"

# All 8 archived learned runs in scope post-date Milestone 25 (Seed Fixture
# v3) and were run against a freshly wiped-and-reseeded v3 graph each time
# (confirmed empirically: memory_units currently holds only the 34 "seed:v3"
# MUs from the most recent reseed — no v2 MUs survive). load_seed_fixture()
# still verifies this per-run rather than assuming it silently: any run
# whose activated MUs reference a seed-looking id absent from v3 fails loud
# in the tie-back rather than mis-attributing topology.
SEED_FIXTURE_VERSION = "v3"

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
    "run_id", "turn_index", "timestamp",
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
        SELECT e.edge_id, e.created_at, e.alpha_after, e.beta_after
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
    for edge_id, created_at, alpha_after, beta_after in rows:
        events.setdefault(edge_id, []).append(
            EdgeUpdateEvent(edge_id, created_at, alpha_after, beta_after)
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


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Alpha D Phase 1 — path-back reconstructor tie-back"
    )
    parser.add_argument("--db", type=Path, default=GRAPH_DB, help="graph.db path, read-only")
    parser.add_argument(
        "--out", type=Path, default=None,
        help="optional path to write a JSON mismatch dump for debugging",
    )
    args = parser.parse_args()

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

    if all_mismatches:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
