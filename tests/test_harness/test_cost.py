"""Tests for scripts/harness/cost.py — budget estimate arithmetic and the
CostTracker's cumulative-actuals ledger (M19 Phase 3)."""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "scripts"))

import pytest

from harness.cost import BudgetExceededError, CostTracker, check_prerun_budget, estimate_prerun_cost


def test_estimate_prerun_cost_combines_bank_and_judge_costs():
    estimate = estimate_prerun_cost(bank_generation_cost_usd=1.5, n_judge_calls=10, judge_cost_per_call=0.02)

    assert estimate["bank_generation_cost_usd"] == 1.5
    assert estimate["judge_calls_cost_usd"] == 0.2
    assert estimate["n_judge_calls"] == 10
    assert estimate["estimated_total_cost_usd"] == 1.7


def test_estimate_prerun_cost_defaults_judge_cost_per_call_to_zero():
    estimate = estimate_prerun_cost(bank_generation_cost_usd=3.0, n_judge_calls=1000)

    assert estimate["judge_calls_cost_usd"] == 0.0
    assert estimate["estimated_total_cost_usd"] == 3.0


def test_check_prerun_budget_none_never_stops():
    estimate = estimate_prerun_cost(bank_generation_cost_usd=99999.0, n_judge_calls=99999, judge_cost_per_call=1.0)

    check_prerun_budget(estimate, budget_cap=None)  # must not raise


def test_check_prerun_budget_raises_when_estimate_exceeds_cap():
    estimate = estimate_prerun_cost(bank_generation_cost_usd=5.0, n_judge_calls=0)

    with pytest.raises(BudgetExceededError, match="exceeds budget_cap"):
        check_prerun_budget(estimate, budget_cap=1.0)


def test_check_prerun_budget_allows_estimate_under_cap():
    estimate = estimate_prerun_cost(bank_generation_cost_usd=0.5, n_judge_calls=0)

    check_prerun_budget(estimate, budget_cap=1.0)  # must not raise


def test_cost_tracker_check_budget_none_cap_never_stops():
    tracker = CostTracker(budget_cap=None, cumulative_cost_usd=0.0)
    tracker.add_cost("anything", 10_000.0)

    tracker.check_budget("test")  # must not raise


def test_cost_tracker_check_budget_raises_once_cumulative_crosses_cap():
    tracker = CostTracker(budget_cap=1.0, cumulative_cost_usd=0.9)

    tracker.add_cost("judge_call", 0.05)
    tracker.check_budget("still under")  # 0.95 <= 1.0, must not raise

    tracker.add_cost("judge_call", 0.10)
    with pytest.raises(BudgetExceededError, match="exceeds budget_cap"):
        tracker.check_budget("now over")  # 1.05 > 1.0


def test_cost_tracker_check_budget_allows_exact_cap_match():
    tracker = CostTracker(budget_cap=1.0, cumulative_cost_usd=1.0)

    tracker.check_budget("exactly at cap")  # not strictly greater than, must not raise
