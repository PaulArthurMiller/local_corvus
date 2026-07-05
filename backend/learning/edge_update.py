"""
Learned-condition edge update mechanism (M20 Phase 1).

Each edge stores two evidence counters, alpha (positive credit) and beta
(negative credit), forming a Beta(alpha, beta) posterior over the edge's
usefulness. effective_weight() (via compute_effective_weight) is the sole
read path traversal uses — either the posterior mean (deterministic) or a
Thompson-sampled draw, dispatched by condition. Only the "learned" condition
reads/writes alpha/beta; "static_weight" and "random_weight" are unaffected
and keep reading the scalar `weight` column.

Feedback verdicts distribute fractional pseudo-count increments backward
along a MU's recorded traversal path with geometric decay (eligibility-trace
credit assignment) via process_verdict_for_learning() — the orchestration
entry point called from POST /feedback/verdict.

Decay (counter discounting / ACT-R) is schema-reserved (config.yaml `decay:`
block) but NOT implemented here — validate_edge_update_config() hard-fails
if decay.enabled is true.
"""

import logging
import random
import uuid
import zlib
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional

logger = logging.getLogger(__name__)

VALID_NEGATIVE_SCOPES = ("path", "terminal")
VALID_WEIGHT_MODES = ("mean", "sample")
LEARNED_CONDITION = "learned"


class EdgeUpdateConfigError(Exception):
    """Raised when config.yaml's edge_update or decay block fails validation."""


def validate_edge_update_config(config: dict) -> dict:
    """
    Parse and validate config.yaml's edge_update: and decay: blocks.

    Returns a resolved dict (defaults applied) for edge_update. Raises
    EdgeUpdateConfigError with a specific message on any violation — callers
    are expected to let this abort startup before any run state is created.
    """
    eu = config.get("edge_update", {}) or {}
    evidence_cap_raw = eu.get("evidence_cap") or {}

    resolved = {
        "prior_alpha": float(eu.get("prior_alpha", 1.0)),
        "prior_beta": float(eu.get("prior_beta", 1.0)),
        "seed_prior_alpha": float(eu.get("seed_prior_alpha", eu.get("prior_alpha", 1.0))),
        "seed_prior_beta": float(eu.get("seed_prior_beta", eu.get("prior_beta", 1.0))),
        "credit_scale": float(eu.get("credit_scale", 1.0)),
        "credit_lambda": float(eu.get("credit_lambda", 0.6)),
        "negative_scope": eu.get("negative_scope", "path"),
        "weight_mode": eu.get("weight_mode", "mean"),
        "evidence_cap": {
            "enabled": bool(evidence_cap_raw.get("enabled", False)),
            "n_max": float(evidence_cap_raw.get("n_max", 50.0)),
        },
        "rng_seed": eu.get("rng_seed", None),
    }

    if resolved["prior_alpha"] <= 0 or resolved["prior_beta"] <= 0:
        raise EdgeUpdateConfigError(
            "edge_update.prior_alpha and edge_update.prior_beta must be > 0 "
            f"(got prior_alpha={resolved['prior_alpha']}, prior_beta={resolved['prior_beta']})"
        )
    if resolved["seed_prior_alpha"] <= 0 or resolved["seed_prior_beta"] <= 0:
        raise EdgeUpdateConfigError(
            "edge_update.seed_prior_alpha and edge_update.seed_prior_beta must be > 0 "
            f"(got seed_prior_alpha={resolved['seed_prior_alpha']}, "
            f"seed_prior_beta={resolved['seed_prior_beta']})"
        )
    if resolved["credit_scale"] <= 0:
        raise EdgeUpdateConfigError(
            f"edge_update.credit_scale must be > 0 (got {resolved['credit_scale']})"
        )
    if not (0 < resolved["credit_lambda"] <= 1):
        raise EdgeUpdateConfigError(
            f"edge_update.credit_lambda must satisfy 0 < credit_lambda <= 1 "
            f"(got {resolved['credit_lambda']})"
        )
    if resolved["negative_scope"] not in VALID_NEGATIVE_SCOPES:
        raise EdgeUpdateConfigError(
            f"edge_update.negative_scope must be one of {VALID_NEGATIVE_SCOPES} "
            f"(got {resolved['negative_scope']!r})"
        )
    if resolved["weight_mode"] not in VALID_WEIGHT_MODES:
        raise EdgeUpdateConfigError(
            f"edge_update.weight_mode must be one of {VALID_WEIGHT_MODES} "
            f"(got {resolved['weight_mode']!r})"
        )
    if resolved["evidence_cap"]["enabled"]:
        n_max = resolved["evidence_cap"]["n_max"]
        prior_total = resolved["prior_alpha"] + resolved["prior_beta"]
        if n_max <= prior_total:
            raise EdgeUpdateConfigError(
                f"edge_update.evidence_cap.n_max ({n_max}) must be > "
                f"prior_alpha + prior_beta ({prior_total}) when evidence_cap is enabled"
            )

    decay = config.get("decay", {}) or {}
    if bool(decay.get("enabled", False)):
        raise EdgeUpdateConfigError(
            "decay.enabled: true is not supported — Milestone 20 Phase 1 reserves the "
            "decay config schema but does not implement decay. Set decay.enabled: false."
        )

    return resolved


def resolve_rng_seed(edge_update_cfg: dict, experiment_id: str) -> int:
    """
    Resolve the RNG seed for weight_mode: sample.

    edge_update.rng_seed overrides when set (an explicit integer). Otherwise
    derive deterministically from experiment_id (the closest per-run identity
    the backend has visibility into) so re-running the same experiment_id
    reproduces the same sample stream. Recorded in run metadata (config_hash
    / full_config_snapshot already capture edge_update.rng_seed verbatim).
    """
    override = edge_update_cfg.get("rng_seed")
    if override is not None:
        return int(override)
    return zlib.crc32((experiment_id or "").encode("utf-8"))


@dataclass
class EdgeWeightInfo:
    """Result of dispatching an edge's weight read through effective_weight()."""

    weight: float
    alpha: Optional[float] = None
    beta: Optional[float] = None
    mode: str = "static"  # "mean" | "sample" | <condition> for non-learned


def compute_effective_weight(
    edge: dict,
    condition: str,
    weight_mode: str,
    rng: random.Random,
) -> EdgeWeightInfo:
    """
    Sole read path for an edge's weight, dispatched by condition.

    learned: derives from the edge's Beta(alpha, beta) posterior — the mean
      (deterministic) or a fresh Thompson sample. static_weight/random_weight:
      unchanged — reads the existing scalar `weight` column.
    """
    if condition != LEARNED_CONDITION:
        return EdgeWeightInfo(weight=float(edge.get("weight", 0.5)), mode=condition)

    alpha = float(edge.get("alpha") if edge.get("alpha") is not None else 1.0)
    beta = float(edge.get("beta") if edge.get("beta") is not None else 1.0)
    total = alpha + beta

    if weight_mode == "sample":
        weight = rng.betavariate(alpha, beta) if alpha > 0 and beta > 0 else 0.5
        mode = "sample"
    else:
        weight = alpha / total if total > 0 else 0.5
        mode = "mean"

    return EdgeWeightInfo(weight=weight, alpha=alpha, beta=beta, mode=mode)


def compute_path_increments(
    path_edge_ids_entry_to_terminal: list[str],
    credit_scale: float,
    credit_lambda: float,
) -> list[tuple[str, int, float]]:
    """
    Compute the path-decayed credit increment for each edge on a path.

    path_edge_ids_entry_to_terminal is ordered from the BM25 entry edge to
    the terminal edge (last element = terminal, hop_k=0). Returns
    (edge_id, hop_k, increment) triples, increment = credit_scale * credit_lambda ** hop_k.
    """
    n = len(path_edge_ids_entry_to_terminal)
    return [
        (edge_id, n - 1 - i, credit_scale * (credit_lambda ** (n - 1 - i)))
        for i, edge_id in enumerate(path_edge_ids_entry_to_terminal)
    ]


def apply_evidence_cap(alpha: float, beta: float, n_max: float) -> tuple[float, float]:
    """
    Rescale (alpha, beta) to sum to n_max if their sum exceeds it.

    Preserves the mean (alpha / (alpha + beta)) exactly — uniform scaling.
    No-op if alpha + beta <= n_max.
    """
    total = alpha + beta
    if total <= n_max:
        return alpha, beta
    scale = n_max / total
    return alpha * scale, beta * scale


def get_path_edge_ids_for_mu(
    mu_id: str,
    hop_depth_per_retrieved_mu: dict,
    traversal_path: list[dict],
) -> list[str]:
    """
    Return the edge_id(s) whose traversal produced mu_id, for credit assignment.

    A MU retrieved directly by BM25 (hop_depth 0, or missing from the dict —
    e.g. a summary MU appended after retrieval) has no path edges: returns [].
    A MU reached via graph traversal (hop_depth >= 1) may have more than one
    incoming edge (multiple K1 sources reaching the same target) — each is
    treated as its own independent single-edge path (matches "an edge
    appearing on multiple verdict-bearing paths... receives multiple
    independent increments").
    """
    hop_depth = hop_depth_per_retrieved_mu.get(mu_id)
    if not hop_depth:
        return []
    return [hop["edge_id"] for hop in traversal_path if hop.get("target_mu_id") == mu_id]


async def process_verdict_for_learning(
    run: dict,
    verdict: str,
    condition_label: str,
    feedback_event_id: str,
    store,
    cfg: dict,
) -> list[dict]:
    """
    Apply path-decayed credit to every retrieved MU's path edges for one verdict.

    Only acts when condition_label == "learned" and verdict is "up"/"down" —
    this is enforced here (not just by the caller's apply_weight_update flag)
    so static_weight/random_weight conditions and "none" verdicts are
    guaranteed to never write counters or edge_update_events rows, regardless
    of what the caller passes. Probe turns (run["is_probe_turn"]) are also
    refused as a belt-and-suspenders guard — the runner/judge never route
    probes here, but the invariant is enforced at this layer too.

    Returns a list of per-edge update summaries (backward compatible with the
    pre-M20 weight_updates shape — edge_id/old_weight/new_weight/source_mu_id/
    target_mu_id/feedback_event_id — plus alpha/beta before/after, hop_k, and
    increment) for the API response and analytics.weight_updates.
    """
    if condition_label != LEARNED_CONDITION:
        logger.info(
            "edge_update: condition_label=%s is not %r — no counter writes",
            condition_label, LEARNED_CONDITION,
        )
        return []
    if verdict not in ("up", "down"):
        logger.debug("edge_update: verdict=%r requires no update", verdict)
        return []
    if run.get("is_probe_turn"):
        logger.warning(
            "edge_update: run_id=%s is a probe turn — refusing to update counters",
            run.get("run_id"),
        )
        return []

    activated_mu_ids: list[str] = run.get("activated_mu_ids") or []
    hop_depths: dict = run.get("hop_depth_per_retrieved_mu") or {}
    traversal_path: list[dict] = run.get("traversal_path") or []

    credit_scale = cfg["credit_scale"]
    credit_lambda = cfg["credit_lambda"]
    negative_scope = cfg["negative_scope"]
    evidence_cap_enabled = cfg["evidence_cap"]["enabled"]
    n_max = cfg["evidence_cap"]["n_max"]

    run_id = run.get("run_id")
    turn_index = run.get("turn_index")

    results: list[dict] = []

    for mu_id in activated_mu_ids:
        path_edge_ids = get_path_edge_ids_for_mu(mu_id, hop_depths, traversal_path)
        if not path_edge_ids:
            continue

        # Each incoming edge into mu_id is its own independent single-edge
        # path under the current single-hop retrieval design (see docstring
        # of get_path_edge_ids_for_mu) — process them independently so an
        # edge shared across multiple retrieved MUs' paths gets multiple
        # independent increments rather than being deduplicated.
        for edge_id in path_edge_ids:
            increments = compute_path_increments([edge_id], credit_scale, credit_lambda)
            if verdict == "down" and negative_scope == "terminal":
                increments = [(eid, k, inc) for eid, k, inc in increments if k == 0]

            for eid, hop_k, increment in increments:
                edge = await store.get_edge(eid)
                if edge is None:
                    logger.warning("edge_update: edge_id=%s not found — skipping", eid)
                    continue

                alpha_before = float(edge.get("alpha") if edge.get("alpha") is not None else 1.0)
                beta_before = float(edge.get("beta") if edge.get("beta") is not None else 1.0)

                if verdict == "up":
                    alpha_after, beta_after = alpha_before + increment, beta_before
                else:
                    alpha_after, beta_after = alpha_before, beta_before + increment

                if evidence_cap_enabled:
                    alpha_after, beta_after = apply_evidence_cap(alpha_after, beta_after, n_max)

                await store.update_edge_counters(
                    edge_id=eid,
                    alpha=alpha_after,
                    beta=beta_after,
                    evidence_updated_at_turn=turn_index,
                )

                event = {
                    "event_id": str(uuid.uuid4()),
                    "run_id": run_id,
                    "turn_index": turn_index,
                    "verdict_id": feedback_event_id,
                    "mu_id": mu_id,
                    "edge_id": eid,
                    "hop_k": hop_k,
                    "verdict": verdict,
                    "increment": increment,
                    "alpha_before": alpha_before,
                    "alpha_after": alpha_after,
                    "beta_before": beta_before,
                    "beta_after": beta_after,
                    "created_at": datetime.now(timezone.utc).isoformat(),
                }
                await store.insert_edge_update_event(event)

                mean_before = (
                    alpha_before / (alpha_before + beta_before)
                    if (alpha_before + beta_before) else 0.5
                )
                mean_after = (
                    alpha_after / (alpha_after + beta_after)
                    if (alpha_after + beta_after) else 0.5
                )
                results.append({
                    "edge_id": eid,
                    "old_weight": mean_before,
                    "new_weight": mean_after,
                    "source_mu_id": edge.get("source_mu_id"),
                    "target_mu_id": edge.get("target_mu_id"),
                    "feedback_event_id": feedback_event_id,
                    "mu_id": mu_id,
                    "hop_k": hop_k,
                    "increment": increment,
                    "alpha_before": alpha_before,
                    "alpha_after": alpha_after,
                    "beta_before": beta_before,
                    "beta_after": beta_after,
                })

    logger.info(
        "edge_update: processed verdict=%s run_id=%s edges_updated=%d",
        verdict, run_id, len(results),
    )
    return results
