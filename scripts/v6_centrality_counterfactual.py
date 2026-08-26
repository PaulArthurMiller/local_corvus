"""
V6 pre-registration: price K1's edge_weight_sum centrality term from archived data.

Companion to `docs/sensitivity_baseline.md`'s "V6 pre-registration" entry and
to BUILD-PLAN.md's Milestone 47. Run this to regenerate every number in that
entry, or to re-run it once more baseline replicates exist.

WHY THIS IS EXACT, NOT A RECONSTRUCTION
---------------------------------------
`_k1_score()` (backend/memory/retrieval.py) ranks BM25 multi-hit candidates by

    k1_centrality_weight * edge_weight_sum[mu] + cumulative_bm25[mu]

Setting the coefficient to 0.0 changes only the *ranking* of the candidate
pool. The pool itself — MUs with >= 2 keyword hits — is produced upstream by
BM25 and is untouched by the coefficient. `analytics.candidate_set_before_
ranking` records every pool member together with both its `bm25_score` and its
`edge_weight_sum`, already sorted by the live composite score. So re-sorting
that recorded list by `bm25_score` alone yields the exact K1 slate V6 will
produce. Nothing is approximated at the K1 stage.

K2 is a different matter: it depends on the topology reachable from the changed
K1 set, which needs M45's reconstructor (scripts/alpha_d_pathback.py). This
script deliberately stops at K1 and says so rather than guessing.

READ-ONLY. Opens graph.db with mode=ro and writes nothing.

Usage:
    python scripts/v6_centrality_counterfactual.py
    python scripts/v6_centrality_counterfactual.py --db memory/graph.db --top-k 3
"""

from __future__ import annotations

import argparse
import collections
import gzip
import json
import logging
import sqlite3
import statistics as st
import sys
from pathlib import Path

logger = logging.getLogger("v6_counterfactual")

# The three Round 1 baseline learned runs (phase_one_experiment_program.md R1).
# cfgA is excluded — its seed prior does not touch the K1 scoring path. cfgB is
# excluded because its probe measurements are invalid (Milestone 44).
DEFAULT_RUNS = [
    "exp0b-2026-08-06_learned",
    "exp0b-rep2-2026-08-09_learned",
    "exp0b-rep3-2026-08-09_learned",
]

# Seed MUs whose fixture edge_weight_sum reaches this are the four cluster hubs.
# Measured on the frozen fixture: hubs 8.0, bridges 2.0, spokes 1.0.
HUB_EWS_THRESHOLD = 8.0

TURNS_PER_SESSION = 42  # 210 real turns / 5 series (program doc §7-4)


def load_seed_classes(fixture_path: Path) -> tuple[set[str], set[str]]:
    """
    Return (all_seed_mu_ids, hub_mu_ids) from the frozen seed fixture.

    Hubs are identified by their fixture edge_weight_sum rather than by name,
    so this keeps working if the fixture's ID scheme changes.
    """
    with gzip.open(fixture_path, "rt", encoding="utf-8") as fh:
        fixture = json.load(fh)

    seed_ids = {m["mu_id"] for m in fixture["memory_units"]}
    ews: dict[str, float] = collections.defaultdict(float)
    for edge in fixture["edges"]:
        # Undirected accumulation — mirrors retrieval.py's both-endpoints build.
        ews[edge["source_mu_id"]] += edge["weight"]
        ews[edge["target_mu_id"]] += edge["weight"]

    hubs = {mu_id for mu_id, total in ews.items() if total >= HUB_EWS_THRESHOLD}
    logger.info(
        "seed fixture: %d MUs, %d edges, %d hubs at edge_weight_sum >= %.1f",
        len(seed_ids), fixture["edge_count"], len(hubs), HUB_EWS_THRESHOLD,
    )
    return seed_ids, hubs


def classify(mu_id: str, seed_ids: set[str], hubs: set[str]) -> str:
    """One of 'seed-hub', 'seed-other', 'conv' — the three K1 competitor classes."""
    if mu_id in hubs:
        return "seed-hub"
    if mu_id in seed_ids:
        return "seed-other"
    return "conv"


def load_turns(conn: sqlite3.Connection, run: str) -> list[list[dict]]:
    """
    Return one candidate pool per real (non-probe) turn of `run`, in turn order.

    Empty pools are dropped: a turn where BM25 produced no multi-hit candidate
    has no K1 ranking to perturb, so it carries no information about the knob.
    """
    rows = conn.execute(
        "SELECT candidate_set_before_ranking FROM analytics "
        "WHERE experiment_id = ? AND is_probe_turn = 0 ORDER BY turn_index",
        (run,),
    ).fetchall()
    if not rows:
        raise SystemExit(
            f"no analytics rows for experiment_id={run!r} — check --db and the run list"
        )

    pools = []
    for row in rows:
        pool = json.loads(row["candidate_set_before_ranking"] or "[]")
        if pool:
            pools.append(pool)
    logger.info("%s: %d turns with a non-empty candidate pool (of %d)", run, len(pools), len(rows))
    return pools


def slates(pool: list[dict], top_k: int) -> tuple[list[str], list[str]]:
    """
    (K1 slate as it ran, K1 slate at k1_centrality_weight=0.0) for one turn.

    The recorded pool is already sorted by the live composite score, so the
    "as it ran" slate is just its head — no re-scoring, and therefore no chance
    of this script disagreeing with what retrieve() actually did.
    """
    as_ran = [c["mu_id"] for c in pool][:top_k]
    bm25_only = [
        c["mu_id"] for c in sorted(pool, key=lambda c: c["bm25_score"], reverse=True)
    ][:top_k]
    return as_ran, bm25_only


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--db", default="memory/graph.db", type=Path)
    parser.add_argument("--fixture", default="seed_fixtures/seed_v3.json.gz", type=Path)
    parser.add_argument("--top-k", type=int, default=3, help="retrieval_level1_top_k (default 3)")
    parser.add_argument("--runs", nargs="*", default=DEFAULT_RUNS)
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO if args.verbose else logging.WARNING,
        format="%(levelname)s %(name)s: %(message)s",
    )

    for path in (args.db, args.fixture):
        if not path.exists():
            print(f"ERROR: {path} not found (run from the repo root)", file=sys.stderr)
            return 1

    seed_ids, hubs = load_seed_classes(args.fixture)

    # mode=ro: this script must never be the reason graph.db changes.
    conn = sqlite3.connect(f"file:{args.db.as_posix()}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row

    ews_all: list[float] = []
    bm25_all: list[float] = []
    centrality_share: list[float] = []
    composition_on: collections.Counter = collections.Counter()
    composition_off: collections.Counter = collections.Counter()
    promoted: collections.Counter = collections.Counter()
    displaced: collections.Counter = collections.Counter()
    per_session: dict[int, dict] = collections.defaultdict(
        lambda: {"turns": 0, "hub_on": 0, "hub_off": 0, "set_chg": 0, "top1_chg": 0, "max_ews": []}
    )
    totals = {"turns": 0, "set_chg": 0, "top1_chg": 0}

    try:
        for run in args.runs:
            for i, pool in enumerate(load_turns(conn, run)):
                as_ran, bm25_only = slates(pool, args.top_k)
                session = i // TURNS_PER_SESSION + 1
                cell = per_session[session]

                totals["turns"] += 1
                cell["turns"] += 1

                if set(as_ran) != set(bm25_only):
                    totals["set_chg"] += 1
                    cell["set_chg"] += 1
                if as_ran[:1] != bm25_only[:1]:
                    totals["top1_chg"] += 1
                    cell["top1_chg"] += 1

                for mu_id in as_ran:
                    composition_on[classify(mu_id, seed_ids, hubs)] += 1
                for mu_id in bm25_only:
                    composition_off[classify(mu_id, seed_ids, hubs)] += 1
                for mu_id in set(as_ran) - set(bm25_only):
                    promoted[classify(mu_id, seed_ids, hubs)] += 1
                for mu_id in set(bm25_only) - set(as_ran):
                    displaced[classify(mu_id, seed_ids, hubs)] += 1

                cell["hub_on"] += sum(1 for m in as_ran if m in hubs)
                cell["hub_off"] += sum(1 for m in bm25_only if m in hubs)
                cell["max_ews"].append(max(c["edge_weight_sum"] for c in pool))

                for candidate in pool:
                    ews = candidate["edge_weight_sum"]
                    bm25 = candidate["bm25_score"]
                    ews_all.append(ews)
                    bm25_all.append(bm25)
                    if ews + bm25 > 0:
                        centrality_share.append(ews / (ews + bm25))
    finally:
        conn.close()

    def pct_of(counter: collections.Counter) -> str:
        total = sum(counter.values()) or 1
        return "  ".join(f"{k}={v / total:.1%}" for k, v in counter.most_common())

    def quantile(values: list[float], q: float) -> float:
        return sorted(values)[int(q * len(values))]

    n = totals["turns"]
    print("\nV6 counterfactual: k1_centrality_weight 1.0 vs 0.0")
    print(f"runs={len(args.runs)}  turns={n}  candidate rows={len(ews_all)}  level1_top_k={args.top_k}\n")

    print("Finding 1 - magnitude vs. effect")
    print(f"  edge_weight_sum   median={st.median(ews_all):6.2f}  mean={st.mean(ews_all):6.2f}"
          f"  p90={quantile(ews_all, 0.9):6.2f}  max={max(ews_all):6.2f}")
    print(f"  cumulative BM25   median={st.median(bm25_all):6.2f}  mean={st.mean(bm25_all):6.2f}"
          f"  p90={quantile(bm25_all, 0.9):6.2f}  max={max(bm25_all):6.2f}")
    print(f"  centrality share of K1 score: median={st.median(centrality_share):.1%}"
          f"  mean={st.mean(centrality_share):.1%}")
    print(f"  K1 set changed:  {totals['set_chg'] / n:.1%} of turns")
    print(f"  top-1 changed:   {totals['top1_chg'] / n:.1%} of turns\n")

    print("Finding 2 - slate composition")
    print(f"  with centrality (today):     {pct_of(composition_on)}")
    print(f"  coefficient 0.0 (V6):        {pct_of(composition_off)}")
    print(f"  term PROMOTES  (n={sum(promoted.values()):4d}):      {pct_of(promoted)}")
    print(f"  term DISPLACES (n={sum(displaced.values()):4d}):      {pct_of(displaced)}\n")

    print("Finding 3 - per-session trajectory")
    print(f"  {'session':>7} {'turns':>6} {'hub share ON':>13} {'hub share OFF':>14}"
          f" {'set chg':>8} {'top1 chg':>9} {'med max ews':>12}")
    for session in sorted(per_session):
        cell = per_session[session]
        slots = cell["turns"] * args.top_k
        print(f"  {session:>7} {cell['turns']:>6} {cell['hub_on'] / slots:>12.1%}"
              f" {cell['hub_off'] / slots:>13.1%} {cell['set_chg'] / cell['turns']:>7.1%}"
              f" {cell['top1_chg'] / cell['turns']:>8.1%} {st.median(cell['max_ews']):>11.2f}")

    print("\nK1 stage only. K2's response to the changed K1 set needs "
          "scripts/alpha_d_pathback.py.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
