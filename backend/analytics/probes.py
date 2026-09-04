"""
Probe management — load, compute, and record probe results.

Probes are fixed-content queries with known gold MU IDs. Running a probe
through the retrieval engine (without full run_interaction) produces a
ProbeResult with precision/recall/rank metrics.

Only retrieve() is ever called for a probe turn — no MUs are created,
no feedback events are written, no edges are modified.
"""

import logging
import os
import uuid
from pathlib import Path
from typing import TYPE_CHECKING, Optional

import yaml
from pydantic import BaseModel, Field, field_validator

from memory.store import store as graph_store

if TYPE_CHECKING:
    from memory.retrieval import RetrievalResult

logger = logging.getLogger(__name__)

_PROBES_PATH = Path(os.getenv(
    "PROBES_PATH",
    os.path.join(os.path.dirname(__file__), "..", "..", "probes"),
))


class ProbeDefinition(BaseModel):
    probe_id: str
    phenomenon_type: str    # ranking_improvement | cross_cluster_transfer | process_rule_adherence
    prompt_text: str
    gold_mu_ids: list[str] = Field(default_factory=list)
    is_bridge_probe: bool = False
    bridge_pair_id: Optional[str] = None
    probe_set_version: str


VALID_BATTERY_MODES = ("mean", "sample")


class ProbeResult(BaseModel):
    result_id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    probe_id: str
    run_id: Optional[str] = None
    measurement_session_index: int
    condition_label: str
    retrieved_mu_ids: list[str] = Field(default_factory=list)
    gold_mu_rank: Optional[int] = None    # 1-indexed; None if no gold MU in retrieved set
    retrieval_precision: Optional[float] = None
    retrieval_recall: Optional[float] = None
    bm25_scores_per_mu: dict[str, float] = Field(default_factory=dict)
    # M21 — which experiment this measurement belongs to. condition_label alone
    # is shared across attempts/dry-runs, so reports scope by experiment_id.
    experiment_id: Optional[str] = None
    # M30 — dual-mode probe battery. series_id lets series_report.py scope
    # probe measurements to one series (probe turns create no analytics row,
    # so this is the only way to join a probe result back to a series —
    # mirrors the §5.3/§5.4 series_id convention). battery_mode distinguishes
    # the single deterministic weight_mode="mean" pass at a checkpoint from
    # each of the K weight_mode="sample" repeats; battery_repeat_index is the
    # 0-indexed repeat number for a "sample" row, None for "mean".
    series_id: Optional[str] = None
    battery_mode: str = "mean"
    battery_repeat_index: Optional[int] = None
    # M46 — probe traversal instrumentation. Mirrors RetrievalResult's own
    # fields of the same name (backend/memory/retrieval.py) so a probe row
    # carries exactly what a real analytics row already does, closing the
    # gap that forced M45's reconstructor to work from real turns only.
    traversal_path: list[dict] = Field(default_factory=list)
    hop_depth_per_retrieved_mu: dict[str, int] = Field(default_factory=dict)

    @field_validator("battery_mode")
    @classmethod
    def _validate_battery_mode(cls, v: str) -> str:
        if v not in VALID_BATTERY_MODES:
            raise ValueError(f"battery_mode must be one of {VALID_BATTERY_MODES}, got {v!r}")
        return v


def load_probe_set(version: str = "v1") -> list[ProbeDefinition]:
    """Load and validate probe definitions from probes/probe_set_{version}.yaml."""
    probe_file = _PROBES_PATH / f"probe_set_{version}.yaml"
    logger.info("probes: loading probe set from %s", probe_file)
    with open(probe_file, "r", encoding="utf-8") as f:
        data = yaml.safe_load(f)

    probe_set_version = data.get("version", version)
    probes: list[ProbeDefinition] = []
    for entry in data.get("probes", []):
        probe = ProbeDefinition(
            probe_id=entry["probe_id"],
            phenomenon_type=entry["phenomenon_type"],
            prompt_text=entry.get("prompt_text", ""),
            gold_mu_ids=entry.get("gold_mu_ids") or [],
            is_bridge_probe=entry.get("is_bridge_probe", False),
            bridge_pair_id=entry.get("bridge_pair_id"),
            probe_set_version=probe_set_version,
        )
        probes.append(probe)

    logger.info(
        "probes: loaded %d probe definitions (version=%s)", len(probes), probe_set_version
    )
    return probes


async def record_probe_result(result: ProbeResult) -> None:
    """Write a probe result to the probe_results table."""
    await graph_store.insert_probe_result(result.model_dump())
    logger.info(
        "probes: recorded result_id=%s probe_id=%s rank=%s precision=%s recall=%s",
        result.result_id,
        result.probe_id,
        result.gold_mu_rank,
        result.retrieval_precision,
        result.retrieval_recall,
    )


async def compute_probe_result(
    probe: ProbeDefinition,
    retrieval_result: "RetrievalResult",
    run_id: Optional[str],
    measurement_session_index: int,
    condition_label: str,
    experiment_id: Optional[str] = None,
    series_id: Optional[str] = None,
    battery_mode: str = "mean",
    battery_repeat_index: Optional[int] = None,
) -> ProbeResult:
    """
    Compute precision, recall, and gold_mu_rank from a retrieval result.

    gold_mu_rank: 1-indexed rank of the first gold MU in the retrieved list.
    None if no gold MU appears in the retrieved set.

    retrieval_precision = |retrieved ∩ gold| / |retrieved|  (None if retrieved is empty)
    retrieval_recall    = |retrieved ∩ gold| / |gold|        (None if gold list is empty)
    """
    retrieved_ids = list(retrieval_result.activated_mu_ids)
    gold_ids_set = set(probe.gold_mu_ids)
    retrieved_set = set(retrieved_ids)

    # First gold MU found in retrieved ordering (1-indexed)
    gold_mu_rank: Optional[int] = None
    for i, mu_id in enumerate(retrieved_ids, start=1):
        if mu_id in gold_ids_set:
            gold_mu_rank = i
            break

    hits = retrieved_set & gold_ids_set
    retrieval_precision = len(hits) / len(retrieved_ids) if retrieved_ids else None
    retrieval_recall = len(hits) / len(probe.gold_mu_ids) if probe.gold_mu_ids else None

    return ProbeResult(
        probe_id=probe.probe_id,
        run_id=run_id,
        measurement_session_index=measurement_session_index,
        condition_label=condition_label,
        retrieved_mu_ids=retrieved_ids,
        gold_mu_rank=gold_mu_rank,
        retrieval_precision=retrieval_precision,
        retrieval_recall=retrieval_recall,
        bm25_scores_per_mu=dict(retrieval_result.bm25_scores_per_mu),
        experiment_id=experiment_id,
        series_id=series_id,
        battery_mode=battery_mode,
        battery_repeat_index=battery_repeat_index,
        # M46 — nothing new computed here: retrieve() has always returned
        # these on RetrievalResult (see the K2 traversal + hop-depth build
        # in retrieval.py), only stored for real turns until now.
        traversal_path=list(retrieval_result.traversal_path),
        hop_depth_per_retrieved_mu=dict(retrieval_result.hop_depth_per_retrieved_mu),
    )
