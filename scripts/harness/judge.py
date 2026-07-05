"""
Judge module (M19 Phase 2) — Ollama judge call + verdict parsing.

Scores one bulk-turn response against the criteria attached to its prompt
(from the prompt bank built in Phase 1). The judge is condition-agnostic by
design: it scores response quality identically for "learned", "static_weight",
and "random_weight" — the runner is what decides whether a verdict triggers a
weight update, keeping that policy out of the measurement itself.

Runs host-side against Ollama at localhost:11434 — NOT host.docker.internal,
which is the container-side alias used by the backend's own Ollama calls.
This module calls the same local Ollama instance the backend talks to, just
from outside Docker.
"""

import json
import logging
from dataclasses import dataclass
from typing import Optional

import httpx

from harness.schema import JudgeConfig
from harness.turn_resolution import TURN_TYPE_PROBE

logger = logging.getLogger("harness.judge")

DEFAULT_JUDGE_HOST = "http://localhost:11434"
VALID_VERDICTS = ("up", "down", "none")

# One initial call + one re-ask on parse failure — matches the retry budget
# used elsewhere in the harness (prompt_gen's per-prompt validation retry).
MAX_JUDGE_ATTEMPTS = 2


class ProbeTurnJudgeError(Exception):
    """Raised if judge_turn is handed a probe turn. Probes never get judged —
    both the runner and this module refuse them (belt-and-suspenders)."""


class JudgeUnparseableError(Exception):
    """Raised when the judge's response fails to parse into a valid verdict
    even after one re-ask. Routes through error_policy["judge_unparseable"]
    (default soft_continue) in the runner."""


@dataclass
class JudgeVerdict:
    verdict: str  # "up" | "down" | "none"
    rationale: Optional[str]
    criterion_triggered: Optional[str]


class OllamaJudgeClient:
    """
    Thin sync HTTP client for host-side Ollama judge calls.

    Duck-typed for testability (see FakeOllamaJudgeClient in
    tests/test_harness/test_judge.py) — judge_turn() only relies on
    chat_json(); model_available() is used by the availability preflight.
    """

    def __init__(self, host: str = DEFAULT_JUDGE_HOST, timeout: float = 60.0):
        self._client = httpx.Client(base_url=host, timeout=timeout)

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> "OllamaJudgeClient":
        return self

    def __exit__(self, *exc_info) -> None:
        self.close()

    def chat_json(self, model: str, temperature: float, prompt: str) -> str:
        """POST /api/chat with format=json; returns the raw message content string."""
        resp = self._client.post(
            "/api/chat",
            json={
                "model": model,
                "messages": [{"role": "user", "content": prompt}],
                "format": "json",
                "options": {"temperature": temperature},
                "stream": False,
            },
        )
        resp.raise_for_status()
        data = resp.json()
        return data["message"]["content"]

    def model_available(self, model: str) -> bool:
        """
        GET /api/tags — confirm `model` is present before any graph
        mutation. Never raises; caller decides hard-stop policy on False.
        """
        try:
            resp = self._client.get("/api/tags")
            resp.raise_for_status()
            data = resp.json()
        except httpx.HTTPError as exc:
            logger.error("judge: model availability check failed: %s", exc)
            return False
        models = {m.get("name") for m in data.get("models", [])}
        # Ollama tag names may carry a ":latest"/version suffix the plan's
        # bare model name doesn't include — match by prefix too.
        return model in models or any((m or "").startswith(model) for m in models)


def _build_judge_prompt(prompt_text: str, criteria: list[str], response_text: str) -> str:
    criteria_lines = "\n".join(f"- {c}" for c in criteria)
    return (
        "You are grading an AI assistant's response against a fixed checklist of criteria.\n\n"
        f"Original prompt given to the assistant:\n{prompt_text}\n\n"
        f"Criteria the response should satisfy:\n{criteria_lines}\n\n"
        f"Assistant's response:\n{response_text}\n\n"
        'Decide whether the response satisfies the criteria ("up"), clearly fails to satisfy '
        'them ("down"), or whether it is genuinely ambiguous ("none"). "none" is the honest '
        "answer whenever up/down can't be cleanly determined — it is not a failure to answer.\n\n"
        'If your verdict is "down", criterion_triggered must be the exact text of ONE of the '
        'criteria listed above that the response failed. If your verdict is "up" or "none", '
        "criterion_triggered must be null.\n\n"
        'Return ONLY a JSON object with keys "verdict" (one of "up"/"down"/"none"), '
        '"rationale" (a short string explaining the verdict), and "criterion_triggered" '
        "(string or null). No prose, no markdown fences."
    )


def _parse_verdict(raw_text: str, criteria: list[str]) -> JudgeVerdict:
    try:
        data = json.loads(raw_text)
    except (json.JSONDecodeError, TypeError) as exc:
        raise ValueError(f"judge response was not valid JSON: {exc}") from exc

    if not isinstance(data, dict):
        raise ValueError(f"judge response JSON was not an object: {raw_text[:200]!r}")

    verdict = data.get("verdict")
    if verdict not in VALID_VERDICTS:
        raise ValueError(f"judge verdict {verdict!r} not in {VALID_VERDICTS}")

    criterion_triggered = data.get("criterion_triggered")
    if verdict == "down":
        if not criterion_triggered or criterion_triggered not in criteria:
            raise ValueError(
                f"verdict is 'down' but criterion_triggered {criterion_triggered!r} "
                f"is not one of the prompt's criteria {criteria}"
            )
    else:
        criterion_triggered = None

    return JudgeVerdict(
        verdict=verdict,
        rationale=data.get("rationale"),
        criterion_triggered=criterion_triggered,
    )


def judge_turn(
    client,
    prompt_text: str,
    criteria: list[str],
    response_text: str,
    judge_config: JudgeConfig,
    turn_type: str,
) -> JudgeVerdict:
    """
    Evaluate one bulk-turn response against its criteria.

    Raises ProbeTurnJudgeError if turn_type indicates a probe turn — this is
    a precondition guard; the runner must never route probes here either
    (both layers, deliberately, per BUILD-PLAN.md).

    Raises JudgeUnparseableError if the judge's response fails to parse into
    a valid verdict even after one re-ask (covers both malformed JSON and
    HTTP/connectivity failures talking to Ollama — both are contained by the
    same retry-then-soft-continue path).
    """
    if turn_type == TURN_TYPE_PROBE:
        raise ProbeTurnJudgeError("judge_turn must never be called for a probe turn")

    prompt = _build_judge_prompt(prompt_text, criteria, response_text)

    last_exc: Optional[Exception] = None
    for attempt in range(MAX_JUDGE_ATTEMPTS):
        try:
            raw_text = client.chat_json(judge_config.model, judge_config.temperature, prompt)
            return _parse_verdict(raw_text, criteria)
        except (ValueError, KeyError, httpx.HTTPError) as exc:
            last_exc = exc
            logger.warning("judge: parse/call failure (attempt %d/%d): %s", attempt + 1, MAX_JUDGE_ATTEMPTS, exc)

    raise JudgeUnparseableError(
        f"judge response unparseable after {MAX_JUDGE_ATTEMPTS} attempt(s) "
        f"for prompt {prompt_text[:100]!r}: {last_exc}"
    )
