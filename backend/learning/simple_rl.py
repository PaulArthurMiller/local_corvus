"""
SimpleRL — uniform weight update across all activated edges.

For each activated edge, applies:
    new_weight = clamp(old_weight + feedback * weight_update_rate, 0.0, 1.0)

This is the uniform distribution approach (see BUILD-PLAN.md open question #3).
Every activated edge receives the same delta regardless of its position in the
retrieval funnel or its traversal count.  This is the baseline; revisit after
the first post-learning experiment run.
"""

import logging
from typing import Any

from learning.interface import LearningAlgorithm

logger = logging.getLogger(__name__)


class SimpleRL(LearningAlgorithm):
    """
    Thumbs-up/thumbs-down RL: uniform weight adjustment over activated edges.

    weight_update_rate comes from config.yaml hyperparameters.weight_update_rate.
    """

    def __init__(self, weight_update_rate: float = 0.1) -> None:
        self.weight_update_rate = weight_update_rate

    def update(
        self,
        activated_edges: list[dict[str, Any]],
        activated_mus: list[dict[str, Any]],
        feedback: int,
    ) -> list[tuple[str, float]]:
        """
        Compute new weights.  Logs old and new weight for every edge updated.

        feedback must be +1 or -1; any other value is treated as 0 (no update).
        """
        if feedback not in (1, -1):
            logger.warning(
                "simple_rl: unexpected feedback value %r — no update applied", feedback
            )
            return []

        delta = feedback * self.weight_update_rate
        results: list[tuple[str, float]] = []

        for edge in activated_edges:
            edge_id = edge["edge_id"]
            old_weight = float(edge["weight"])
            raw_new = old_weight + delta
            new_weight = max(0.0, min(1.0, raw_new))

            logger.info(
                "simple_rl: edge_id=%s old_weight=%.4f delta=%.4f new_weight=%.4f "
                "(clamped=%s)",
                edge_id,
                old_weight,
                delta,
                new_weight,
                new_weight != raw_new,
            )
            results.append((edge_id, new_weight))

        logger.info(
            "simple_rl: updated %d edge(s) feedback=%+d rate=%.4f",
            len(results),
            feedback,
            self.weight_update_rate,
        )
        return results
