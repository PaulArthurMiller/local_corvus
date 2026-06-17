"""
LearningAlgorithm abstract base class.

All learning algorithms implement update() and receive the same inputs:
activated edges, activated MUs, and a feedback signal (+1 or -1).
Return value is a list of (edge_id, new_weight) pairs to be written to
the graph store by the caller.

Separating the interface from the implementation means the orchestrator
and feedback endpoint never import SimpleRL directly — they import the
ABC and receive whichever algorithm the config specifies.
"""

from abc import ABC, abstractmethod
from typing import Any


class LearningAlgorithm(ABC):
    """
    Contract that every learning algorithm must satisfy.

    Implementations are stateless with respect to the graph — they compute
    weight deltas and return them; the caller writes them to the store.
    """

    @abstractmethod
    def update(
        self,
        activated_edges: list[dict[str, Any]],
        activated_mus: list[dict[str, Any]],
        feedback: int,
    ) -> list[tuple[str, float]]:
        """
        Compute new weights for all activated edges given a feedback signal.

        Args:
            activated_edges: Edge dicts from GraphStore (must include
                             'edge_id' and 'weight' keys).
            activated_mus:   MU dicts from GraphStore (for algorithms that
                             weight by MU properties; unused by SimpleRL).
            feedback:        +1 (positive) or -1 (negative).

        Returns:
            List of (edge_id, new_weight) pairs.  Only edges that should
            be updated need to appear; the caller ignores unchanged edges.
        """
        raise NotImplementedError
