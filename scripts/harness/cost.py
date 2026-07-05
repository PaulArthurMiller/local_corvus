"""
Budget tracking for a harness run (M19 Phase 3).

CostTracker absorbs Phase 1's isolated cost-estimate function
(prompt_gen.estimate_generation_cost) into a running budget: a pre-run
estimate (Sonnet prompt-bank generation, if one must still be built, plus
judge calls) checked against plan.budget_cap before any graph mutation,
and a cumulative-actuals ledger updated as the run progresses.

Local Ollama judge calls cost $0 today — JUDGE_COST_PER_CALL_USD is kept
as an explicit, named constant (not a bare 0 inline) and every call site
accepts it as an override, so wiring a hosted judge later is a config
change, not an archaeology dig through runner.py.
"""

import logging
from dataclasses import dataclass
from typing import Optional

logger = logging.getLogger("harness.cost")

JUDGE_COST_PER_CALL_USD = 0.0


class BudgetExceededError(Exception):
    """Cumulative or pre-run estimated cost has crossed plan.budget_cap."""


@dataclass
class CostTracker:
    """Running cumulative-cost ledger, checked after every actual spend."""

    budget_cap: Optional[float]
    cumulative_cost_usd: float = 0.0

    def add_cost(self, description: str, amount: float) -> None:
        self.cumulative_cost_usd = round(self.cumulative_cost_usd + amount, 6)
        logger.debug(
            "cost.CostTracker: +$%s (%s) -> cumulative=$%s",
            amount, description, self.cumulative_cost_usd,
        )

    def check_budget(self, context: str) -> None:
        """Raise BudgetExceededError if cumulative cost has crossed
        budget_cap. No-op when budget_cap is None (uncapped, the schema
        default)."""
        if self.budget_cap is not None and self.cumulative_cost_usd > self.budget_cap:
            raise BudgetExceededError(
                f"cumulative cost ${self.cumulative_cost_usd:.4f} exceeds budget_cap "
                f"${self.budget_cap:.4f} ({context})"
            )


def estimate_prerun_cost(
    bank_generation_cost_usd: float,
    n_judge_calls: int,
    judge_cost_per_call: float = JUDGE_COST_PER_CALL_USD,
) -> dict:
    """
    Combine the known/estimated Sonnet bank-generation cost (0.0 when an
    existing bank is being reused — no Sonnet calls will be made) with the
    projected judge-call cost for this run, into one pre-run total.
    """
    judge_calls_cost_usd = round(n_judge_calls * judge_cost_per_call, 6)
    return {
        "bank_generation_cost_usd": round(bank_generation_cost_usd, 6),
        "judge_calls_cost_usd": judge_calls_cost_usd,
        "n_judge_calls": n_judge_calls,
        "estimated_total_cost_usd": round(bank_generation_cost_usd + judge_calls_cost_usd, 6),
    }


def check_prerun_budget(estimate: dict, budget_cap: Optional[float]) -> None:
    """
    Hard-stop check before any graph mutation. budget_cap=None is uncapped
    (the schema default) and never stops.
    """
    if budget_cap is None:
        return
    total = estimate["estimated_total_cost_usd"]
    if total > budget_cap:
        raise BudgetExceededError(
            f"pre-run estimated cost ${total:.4f} exceeds budget_cap ${budget_cap:.4f} — "
            f"increase plan.budget_cap or reduce total_prompts/series_count/turns_per_series "
            f"before running"
        )
