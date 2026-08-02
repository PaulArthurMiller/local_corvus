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
import re
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
    """Raised when the judge's response fails to parse into a valid VERDICT
    even after one re-ask — malformed JSON, a missing/wrong-length/non-bool
    criteria_met list, or an HTTP failure talking to Ollama. Routes through
    error_policy["judge_unparseable"] (default soft_continue) in the runner.

    Under the Milestone 31 per-criterion checklist protocol the verdict is
    always derived host-side from criteria_met (never asserted by the judge
    directly), so every parse failure is direction-neutral by construction —
    there is no longer an "unresolved citation" failure mode that could only
    ever destroy "down" verdicts (the asymmetry the 2026-07-27 fix addressed
    under the old free-text criterion_triggered protocol).
    """


@dataclass
class JudgeVerdict:
    verdict: str  # "up" | "down" | "none" — derived host-side from criteria_met
    rationale: Optional[str]
    criterion_triggered: Optional[str]
    # Milestone 31 — the judge's per-criterion checklist, same order as the
    # prompt's criteria list. Carried through to TurnCheckpoint so a
    # calibration or M33-style analysis pass can see which specific criteria
    # were met/failed, not just the derived up/down/none.
    criteria_met: Optional[list[bool]] = None


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
    """
    Milestone 31 per-criterion checklist protocol (Paul, 2026-08-02): the
    judge grades each criterion independently; the runner derives the overall
    verdict from the checklist rather than asking the judge to apply an
    up/down rule itself. The old any-criterion-unmet-means-down framing
    produced a punishment-dominated 34% up-rate against exp0's demanding
    criteria — grading one criterion at a time is also an easier task for a
    small local judge model than applying a global rule in one shot.
    """
    criteria_lines = "\n".join(f"{i}. {c}" for i, c in enumerate(criteria, 1))
    return (
        "You are grading an AI assistant's response against a fixed checklist of criteria.\n\n"
        f"Original prompt given to the assistant:\n{prompt_text}\n\n"
        f"Criteria to grade independently, in order:\n{criteria_lines}\n\n"
        f"Assistant's response:\n{response_text}\n\n"
        "For EACH criterion above, judge on its own whether the response satisfies it. Do "
        "not apply any overall up/down/pass-fail judgment yourself — that is derived "
        "separately from your per-criterion answers.\n\n"
        'Return ONLY a JSON object with keys "criteria_met" (a list of exactly '
        f"{len(criteria)} true/false booleans, in the SAME order as the criteria listed "
        'above — one boolean per criterion, no more, no fewer) and "rationale" (a short '
        "string explaining your per-criterion judgments). No prose, no markdown fences."
    )


# Criterion-citation normalization, built for the pre-Milestone-31 protocol
# where the judge cited a criterion's text directly. The Milestone 31
# per-criterion checklist protocol (_parse_verdict below) identifies criteria
# by list position instead, so citation resolution is no longer on the
# primary parse path — kept for citation resolution wherever the judge names
# criteria in prose (e.g. a future free-text fallback, or rationale parsing),
# per BUILD-PLAN.md's Milestone 31 spec.
#
# Leading "label:" prefixes a judge routinely drops when quoting a criterion
# back — the observed exp0 failure was a criterion written as
# "answer: local temporal readings/local time" cited as
# "local temporal readings/local time". The colon is required for a match, so
# criteria that legitimately open with one of these words ("states three",
# "notes fixed sequence is mandatory") are never touched.
_LABEL_PREFIXES = (
    "answer",
    "criterion",
    "criteria",
    "expected",
    "expected answer",
    "required",
    "response",
)
_LABEL_PREFIX_RE = re.compile(
    r"^\s*(?:" + "|".join(re.escape(p) for p in _LABEL_PREFIXES) + r")\s*:\s*",
    re.IGNORECASE,
)
# Markdown emphasis and quote characters the judge wraps citations in.
_WRAPPER_CHARS = "*_`\"'“”‘’ \t"


def _normalize_criterion(text: str) -> str:
    """
    Reduce a criterion string to a comparison key, so a judge citation that
    differs only cosmetically still resolves to the bank's criterion.

    Exact string equality was the original rule and it is too brittle for a
    small local judge: it treats "answer: UTC", "**UTC**", and "UTC." as three
    different criteria. Normalization is deliberately conservative — it only
    removes formatting and label noise, never content words — because the
    resolved value is what §6's criterion-level analysis keys on, so a
    too-eager match would attribute a failure to the wrong criterion.
    """
    normalized = text.strip().strip(_WRAPPER_CHARS)
    # Repeat: a judge may emit "criterion: answer: X" when quoting back.
    while True:
        stripped = _LABEL_PREFIX_RE.sub("", normalized)
        if stripped == normalized:
            break
        normalized = stripped
    normalized = normalized.strip().strip(_WRAPPER_CHARS)
    normalized = re.sub(r"\s+", " ", normalized)
    normalized = normalized.rstrip(".,;:!")
    return normalized.casefold()


def _resolve_criterion(citation: Optional[str], criteria: list[str]) -> Optional[str]:
    """
    Map the judge's cited criterion onto the exact criterion text from the
    prompt bank, or return None if it can't be resolved.

    Always returns a string from `criteria` (never the judge's own wording) —
    downstream analysis groups failures by criterion text, so letting the
    judge's paraphrase through would fragment those groups.

    Resolution order, most to least certain:
      1. Exact match — unchanged from the original behavior.
      2. Normalized match (see _normalize_criterion).
      3. Single-criterion prompts: if the prompt carries exactly one
         criterion, a "down" can only mean that one. This is deduction, not
         guessing, and it covers the cluster_c/cluster_d "answer: X" prompts
         where the observed failure occurred.
    Anything else is left unresolved rather than fuzzy-matched — attributing
    a failure to a guessed criterion would corrupt the record this run exists
    to produce.
    """
    if citation:
        if citation in criteria:
            return citation
        normalized_citation = _normalize_criterion(citation)
        if normalized_citation:
            for criterion in criteria:
                if _normalize_criterion(criterion) == normalized_citation:
                    return criterion
    if len(criteria) == 1:
        return criteria[0]
    return None


def _parse_verdict(raw_text: str, criteria: list[str]) -> JudgeVerdict:
    """
    Parse the judge's per-criterion checklist and derive the verdict
    host-side (Milestone 31, Paul's 2026-08-02 decision): all criteria met ->
    "up", none met -> "down", anything mixed -> "none" (a partial answer gets
    no feedback — no weight update — matching the backend's existing
    "absence of signal is an explicit observation" contract, M8).

    Deriving the verdict from a fixed-length list of booleans rather than
    asking the judge to apply the all-vs-none rule itself is the point: the
    old any-criterion-unmet-means-down framing produced a punishment-dominated
    34% up-rate on exp0's fairly demanding criteria, and per-criterion grading
    is an easier task for a small local judge model than a global rule.

    Raises ValueError (caught by judge_turn's retry loop, direction-neutral
    per JudgeUnparseableError) if criteria_met is missing, not a list, the
    wrong length, or contains a non-bool element — the exact malformed shapes
    the model can produce when it doesn't follow the schema.
    """
    try:
        data = json.loads(raw_text)
    except (json.JSONDecodeError, TypeError) as exc:
        raise ValueError(f"judge response was not valid JSON: {exc}") from exc

    if not isinstance(data, dict):
        raise ValueError(f"judge response JSON was not an object: {raw_text[:200]!r}")

    criteria_met = data.get("criteria_met")
    if (
        not isinstance(criteria_met, list)
        or len(criteria_met) != len(criteria)
        or not all(isinstance(v, bool) for v in criteria_met)
    ):
        raise ValueError(
            f"judge criteria_met {criteria_met!r} must be a list of exactly "
            f"{len(criteria)} booleans (one per criterion, in order)"
        )

    if all(criteria_met):
        verdict = "up"
        criterion_triggered = None
    elif not any(criteria_met):
        verdict = "down"
        # All criteria failed, so which one is cited is uninformative by
        # construction — the rationale carries the real detail. The first
        # listed criterion satisfies the backend's down-requires-criterion
        # contract without inventing or guessing anything.
        criterion_triggered = criteria[0]
    else:
        verdict = "none"
        criterion_triggered = None

    return JudgeVerdict(
        verdict=verdict,
        rationale=data.get("rationale"),
        criterion_triggered=criterion_triggered,
        criteria_met=criteria_met,
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

    Raises JudgeUnparseableError only if the judge's response fails to parse
    into a valid checklist even after one re-ask (covers malformed JSON, a
    missing/wrong-length/non-bool criteria_met, and HTTP/connectivity
    failures talking to Ollama — all contained by the same
    retry-then-soft-continue path). Every failure here is direction-neutral:
    unlike the pre-Milestone-31 free-text citation protocol, there is no verdict
    to partially trust when criteria_met is malformed.
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
