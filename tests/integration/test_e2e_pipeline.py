"""
End-to-end integration tests for the Local Corvus pipeline.

Requires the Docker stack to be running at localhost:8000.
Run explicitly: pytest tests/integration/ -m integration

These tests make real HTTP calls and write real data to the database.
They are excluded from the default test suite (see pytest.ini) to avoid
side effects during unit testing.

Coverage:
  - test_chat_turn_creates_memory_units
  - test_feedback_event_triggers_weight_update
  - test_series_report_generates_all_sections
  - test_experiment_report_learning_measures_populated
  - test_control_shadow_sanity_checks_pass
"""

import json
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Optional

import pytest

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

BASE_URL = "http://localhost:8000"
TIMEOUT_CHAT = 120   # Ollama inference can be slow
TIMEOUT_REPORT = 60  # Report generation hits the DB heavily
TIMEOUT_DEFAULT = 30

# Experiment IDs that must exist from the Phase 1-3 smoketest runs.
# These are queried dynamically so the tests work on any populated stack.
_LEARNED_EXPERIMENT_ID: Optional[str] = None
_STATIC_EXPERIMENT_ID: Optional[str] = None
_RANDOM_EXPERIMENT_ID: Optional[str] = None


# ---------------------------------------------------------------------------
# HTTP helpers
# ---------------------------------------------------------------------------


def _get(path: str, params: Optional[dict] = None, timeout: int = TIMEOUT_DEFAULT) -> Any:
    """GET request → parsed JSON. Raises on HTTP error."""
    url = BASE_URL + path
    if params:
        query = "&".join(f"{k}={urllib.parse.quote(str(v))}" for k, v in params.items())
        url = f"{url}?{query}"
    req = urllib.request.Request(url, method="GET")
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read())


def _post(path: str, body: dict, timeout: int = TIMEOUT_DEFAULT) -> Any:
    """POST JSON body → parsed JSON. Raises on HTTP error."""
    url = BASE_URL + path
    data = json.dumps(body).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=data,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read())


# ---------------------------------------------------------------------------
# Fixture helpers — discover existing data from the running stack
# ---------------------------------------------------------------------------


def _get_runs(limit: int = 100) -> list[dict]:
    """Return up to `limit` run records from /admin/runs."""
    result = _get(f"/admin/runs?limit={limit}")
    return result.get("runs", [])


def _find_experiment_ids() -> dict[str, Optional[str]]:
    """
    Find experiment IDs for the three conditions by querying run records.

    Reads condition_label directly from the analytics table (via /admin/runs)
    to avoid stale condition labels in generated reports.

    Returns a dict with keys: learned, static_weight, random_weight.
    Values are experiment_id strings or None if not found.
    """
    try:
        runs = _get_runs(limit=500)
    except Exception:
        return {"learned": None, "static_weight": None, "random_weight": None}

    # Map experiment_id → condition_label from run records (ground truth)
    exp_to_condition: dict[str, str] = {}
    for run in runs:
        eid = run.get("experiment_id")
        cond = run.get("condition_label")
        if eid and cond and eid not in exp_to_condition:
            exp_to_condition[eid] = cond

    found: dict[str, Optional[str]] = {
        "learned": None,
        "static_weight": None,
        "random_weight": None,
    }
    for exp_id, condition in exp_to_condition.items():
        if condition == "learning" and found["learned"] is None:
            found["learned"] = exp_id
        elif condition == "static-weight" and found["static_weight"] is None:
            found["static_weight"] = exp_id
        elif condition == "random-weight" and found["random_weight"] is None:
            found["random_weight"] = exp_id

    return found


# ---------------------------------------------------------------------------
# Backend availability guard
# ---------------------------------------------------------------------------


def _backend_available() -> bool:
    try:
        _get("/health", timeout=5)
        return True
    except Exception:
        return False


pytestmark = pytest.mark.integration

if not _backend_available():
    pytest.skip(
        "Backend at localhost:8000 is not running — skipping integration tests",
        allow_module_level=True,
    )


# ---------------------------------------------------------------------------
# Test 1: POST /chat creates memory units
# ---------------------------------------------------------------------------


def test_chat_turn_creates_memory_units():
    """
    A single chat turn must produce at least two distinct MUs:
    one for user input and one for agent output.

    Verifies: POST /chat → response has run_id, input_mu_id, output_mu_id;
    input_mu_id and output_mu_id are non-empty and distinct.
    """
    payload = {"message": "Integration test: what is 2 + 2?"}
    response = _post("/chat", payload, timeout=TIMEOUT_CHAT)
    print(f"\nchat response keys: {list(response.keys())}")
    print(f"run_id: {response.get('run_id')}")
    print(f"input_mu_id: {response.get('input_mu_id')}")
    print(f"output_mu_id: {response.get('output_mu_id')}")

    assert "run_id" in response, "response missing run_id"
    assert "input_mu_id" in response, "response missing input_mu_id"
    assert "output_mu_id" in response, "response missing output_mu_id"

    run_id = response["run_id"]
    input_mu_id = response["input_mu_id"]
    output_mu_id = response["output_mu_id"]

    assert run_id, "run_id is empty"
    assert input_mu_id, "input_mu_id is empty"
    assert output_mu_id, "output_mu_id is empty"
    assert input_mu_id != output_mu_id, "input_mu_id and output_mu_id must be distinct (≥ 2 MUs)"

    # Confirm the run appears in /admin/runs
    runs_data = _get(f"/admin/runs?limit=5")
    run_ids_in_log = [r["run_id"] for r in runs_data.get("runs", [])]
    assert run_id in run_ids_in_log, f"run_id {run_id!r} not found in /admin/runs"


# ---------------------------------------------------------------------------
# Test 2: POST /feedback creates a feedback_events row and weight_updates
# ---------------------------------------------------------------------------


def test_feedback_event_triggers_weight_update():
    """
    Posting a thumbs-up feedback event for a run with activated edges must:
      1. Create a feedback_events row (verified via feedback_event_id in weight_updates)
      2. Return a non-empty weight_updates list

    Strategy: find any existing run with activated_edge_ids, post feedback for it.
    """
    runs = _get_runs(limit=100)
    # Only use learning-condition runs — sending feedback to a control run
    # contaminates the zero_feedback_edge_correlation sanity check (test 5).
    target_run = next(
        (r for r in runs
         if r.get("activated_edge_ids") and r.get("condition_label") == "learning"),
        None,
    )
    if target_run is None:
        pytest.skip("No learning-condition run with activated_edge_ids found; run a chat turn first")

    run_id = target_run["run_id"]
    edge_count = len(target_run["activated_edge_ids"])
    print(f"\nUsing run_id={run_id!r} with {edge_count} activated edge(s)")

    response = _post(
        "/feedback",
        {"run_id": run_id, "signal": 1, "verdict_basis": "integration_test"},
    )
    print(f"feedback response: edges_updated={response.get('edges_updated')} "
          f"weight_updates_count={len(response.get('weight_updates', []))}")

    assert "weight_updates" in response, "feedback response missing weight_updates"
    weight_updates = response["weight_updates"]

    # weight_updates must be non-empty (run has activated_edge_ids)
    assert len(weight_updates) > 0, (
        f"weight_updates is empty for run with {edge_count} activated edge(s)"
    )

    # Each weight_update must have a feedback_event_id (confirms feedback_events row)
    for wu in weight_updates:
        assert "feedback_event_id" in wu, f"weight_update missing feedback_event_id: {wu}"
        assert wu["feedback_event_id"], "feedback_event_id is empty"

    print(f"Confirmed: {len(weight_updates)} weight update(s) with feedback_event_id present")


# ---------------------------------------------------------------------------
# Test 3: Series report generates all §5 sections
# ---------------------------------------------------------------------------


def test_series_report_generates_all_sections():
    """
    POST /admin/reports/generate-series must return a report dict with all
    eight §5 subsection keys.
    """
    runs = _get_runs(limit=50)
    # Find a run that has series_id and experiment_id
    target_run = next(
        (r for r in runs if r.get("series_id") and r.get("experiment_id")),
        None,
    )
    if target_run is None:
        pytest.skip("No run with series_id and experiment_id found")

    series_id = target_run["series_id"]
    experiment_id = target_run["experiment_id"]
    print(f"\nGenerating series report for series_id={series_id!r} experiment_id={experiment_id!r}")

    gen_response = _post(
        "/admin/reports/generate-series",
        {"experiment_id": experiment_id, "series_id": series_id},
        timeout=TIMEOUT_REPORT,
    )
    print(f"generate-series response keys: {list(gen_response.keys())}")
    assert "report_id" in gen_response, f"generate-series response missing report_id: {gen_response}"

    # Fetch the full report JSON to inspect sections
    report_id = gen_response["report_id"]
    response = _get(f"/admin/reports/{report_id}", timeout=TIMEOUT_REPORT)
    print(f"full report keys: {list(response.keys())}")

    assert "sections" in response, f"series report missing 'sections' key; full keys: {list(response.keys())}"
    sections = response["sections"]
    print(f"sections keys: {list(sections.keys())}")

    required_sections = [
        "s5_1_failure_summary",
        "s5_2_retrieval_trace_digest",
        "s5_3_retrieval_rate_classification",
        "s5_4_cross_series_edges",
        "s5_5_edge_weight_deltas",
        "s5_6_probe_integrity",
        "s5_7_mode3_trigger_count",
        "s5_8_num_ctx_pressure",
    ]
    for key in required_sections:
        assert key in sections, f"sections missing required §5 key: {key!r}"
        assert sections[key] is not None, f"section {key!r} is null"

    print(f"All {len(required_sections)} §5 sections present and non-null")


# ---------------------------------------------------------------------------
# Test 4: Experiment report has learning_measures populated
# ---------------------------------------------------------------------------


def test_experiment_report_learning_measures_populated():
    """
    POST /admin/reports/generate-experiment for the learned condition must
    return a report where learning_measures is not null and contains at least
    probe_pass_rates and state_transitions.
    """
    exp_ids = _find_experiment_ids()
    experiment_id = exp_ids.get("learned")
    if not experiment_id:
        pytest.skip("No learned-condition experiment found in generated reports")

    print(f"\nGenerating experiment report for experiment_id={experiment_id!r}")

    gen_response = _post(
        "/admin/reports/generate-experiment",
        {"experiment_id": experiment_id},
        timeout=TIMEOUT_REPORT,
    )
    print(f"generate-experiment response keys: {list(gen_response.keys())}")
    assert "report_id" in gen_response, f"generate-experiment response missing report_id: {gen_response}"

    # Fetch the full report JSON to inspect learning_measures
    report_id = gen_response["report_id"]
    response = _get(f"/admin/reports/{report_id}", timeout=TIMEOUT_REPORT)
    print(f"full report keys: {list(response.keys())}")

    assert "learning_measures" in response, "experiment report missing 'learning_measures' key"
    lm = response["learning_measures"]

    assert lm is not None, "learning_measures is null"
    assert isinstance(lm, dict), f"learning_measures is {type(lm).__name__}, expected dict"

    print(f"learning_measures keys: {list(lm.keys())}")

    assert "probe_pass_rates" in lm, (
        f"learning_measures missing 'probe_pass_rates'; found keys: {list(lm.keys())}"
    )
    assert "state_transitions" in lm, (
        f"learning_measures missing 'state_transitions'; found keys: {list(lm.keys())}"
    )

    print(f"probe_pass_rates: {lm['probe_pass_rates']}")
    print(f"state_transitions keys: {list(lm['state_transitions'].keys())}")


# ---------------------------------------------------------------------------
# Test 5: Control shadow sanity checks pass
# ---------------------------------------------------------------------------


def test_control_shadow_sanity_checks_pass():
    """
    POST /admin/reports/generate-experiment with control_experiment_ids must
    return a report where:
      - static_weight sanity_checks: zero_weight_changes status == 'PASS'
      - static_weight sanity_checks: zero_feedback_edge_correlation status == 'PASS'
      - random_weight sanity_checks: weights_rerandomized status == 'PASS'
      - random_weight sanity_checks: zero_feedback_edge_correlation status == 'PASS'
    """
    exp_ids = _find_experiment_ids()
    learned_id = exp_ids.get("learned")
    static_id = exp_ids.get("static_weight")
    random_id = exp_ids.get("random_weight")

    if not learned_id:
        pytest.skip("No learned-condition experiment found")
    if not static_id:
        pytest.skip("No static-weight control experiment found")
    if not random_id:
        pytest.skip("No random-weight control experiment found")

    print(
        f"\nGenerating experiment report with control shadow:\n"
        f"  learned={learned_id!r}\n"
        f"  static_weight={static_id!r}\n"
        f"  random_weight={random_id!r}"
    )

    gen_response = _post(
        "/admin/reports/generate-experiment",
        {
            "experiment_id": learned_id,
            "control_experiment_ids": {
                "static_weight": static_id,
                "random_weight": random_id,
            },
        },
        timeout=TIMEOUT_REPORT,
    )
    print(f"generate-experiment response keys: {list(gen_response.keys())}")
    assert "report_id" in gen_response, f"generate-experiment response missing report_id: {gen_response}"

    # Fetch the full report JSON to inspect control_shadow
    report_id = gen_response["report_id"]
    response = _get(f"/admin/reports/{report_id}", timeout=TIMEOUT_REPORT)
    print(f"full report keys: {list(response.keys())}")

    assert "control_shadow" in response, "experiment report missing 'control_shadow'"
    cs = response["control_shadow"]
    assert cs is not None, "control_shadow is null (control_experiment_ids were provided)"
    assert isinstance(cs, dict), f"control_shadow is {type(cs).__name__}, expected dict"

    print(f"control_shadow keys: {list(cs.keys())}")

    # --- Static-weight control ---
    assert "static_weight" in cs, f"control_shadow missing 'static_weight'; keys: {list(cs.keys())}"
    sw = cs["static_weight"]
    assert "sanity_checks" in sw, "static_weight missing sanity_checks"
    sw_checks = sw["sanity_checks"]
    print(f"static_weight sanity_checks: {json.dumps(sw_checks, indent=2)}")

    assert "zero_weight_changes" in sw_checks, "static_weight missing zero_weight_changes check"
    assert sw_checks["zero_weight_changes"]["status"] == "PASS", (
        f"static_weight zero_weight_changes FAIL: {sw_checks['zero_weight_changes']}"
    )

    assert "zero_feedback_edge_correlation" in sw_checks, (
        "static_weight missing zero_feedback_edge_correlation check"
    )
    assert sw_checks["zero_feedback_edge_correlation"]["status"] == "PASS", (
        f"static_weight zero_feedback_edge_correlation FAIL: {sw_checks['zero_feedback_edge_correlation']}"
    )

    # --- Random-weight control ---
    assert "random_weight" in cs, f"control_shadow missing 'random_weight'; keys: {list(cs.keys())}"
    rw = cs["random_weight"]
    assert "sanity_checks" in rw, "random_weight missing sanity_checks"
    rw_checks = rw["sanity_checks"]
    print(f"random_weight sanity_checks: {json.dumps(rw_checks, indent=2)}")

    assert "weights_rerandomized" in rw_checks, "random_weight missing weights_rerandomized check"
    assert rw_checks["weights_rerandomized"]["status"] == "PASS", (
        f"random_weight weights_rerandomized FAIL: {rw_checks['weights_rerandomized']}"
    )

    assert "zero_feedback_edge_correlation" in rw_checks, (
        "random_weight missing zero_feedback_edge_correlation check"
    )
    assert rw_checks["zero_feedback_edge_correlation"]["status"] == "PASS", (
        f"random_weight zero_feedback_edge_correlation FAIL: {rw_checks['zero_feedback_edge_correlation']}"
    )

    print("All control shadow sanity checks PASS")
