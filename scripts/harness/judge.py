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
    even after one re-ask — malformed JSON, a verdict outside VALID_VERDICTS,
    or an HTTP failure talking to Ollama. Routes through
    error_policy["judge_unparseable"] (default soft_continue) in the runner.

    Deliberately NOT raised when the verdict itself is fine but the judge's
    criterion citation can't be resolved: that failure mode can only occur on
    "down" (an "up"/"none" citation is simply nulled), so raising on it made
    the harness lose "down" verdicts at a rate it could never lose "up"
    verdicts. In a calibration run measuring judge up-skew, an error that
    silently deletes only downs biases the very quantity being measured. See
    JudgeVerdict.criterion_unresolved for how that case is carried instead.
    """


@dataclass
class JudgeVerdict:
    verdict: str  # "up" | "down" | "none"
    rationale: Optional[str]
    criterion_triggered: Optional[str]
    # True only when verdict == "down" and the judge's cited criterion could
    # not be resolved to one of the prompt's criteria even after normalization
    # and the re-ask. The verdict is still trustworthy and is carried through;
    # the runner decides how to source the missing criterion (operator prompt
    # in --interactive, otherwise soft-continue on the verdict POST alone).
    criterion_unresolved: bool = False
    # The judge's raw, unresolvable citation — kept for the audit trail so a
    # calibration pass can see what the judge actually said rather than just
    # that it said something unusable.
    raw_criterion_citation: Optional[str] = None


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
    try:
        data = json.loads(raw_text)
    except (json.JSONDecodeError, TypeError) as exc:
        raise ValueError(f"judge response was not valid JSON: {exc}") from exc

    if not isinstance(data, dict):
        raise ValueError(f"judge response JSON was not an object: {raw_text[:200]!r}")

    verdict = data.get("verdict")
    if verdict not in VALID_VERDICTS:
        raise ValueError(f"judge verdict {verdict!r} not in {VALID_VERDICTS}")

    raw_citation = data.get("criterion_triggered")
    if not isinstance(raw_citation, str):
        raw_citation = None

    if verdict == "down":
        resolved = _resolve_criterion(raw_citation, criteria)
        return JudgeVerdict(
            verdict=verdict,
            rationale=data.get("rationale"),
            criterion_triggered=resolved,
            criterion_unresolved=resolved is None,
            raw_criterion_citation=None if resolved else raw_citation,
        )

    # "up"/"none" carry no criterion by definition; any citation the judge
    # attached is dropped without complaint. The "down" branch above is now
    # equally non-fatal, which is the point — see JudgeUnparseableError.
    return JudgeVerdict(
        verdict=verdict,
        rationale=data.get("rationale"),
        criterion_triggered=None,
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
    into a valid VERDICT even after one re-ask (covers malformed JSON, an
    unknown verdict value, and HTTP/connectivity failures talking to Ollama —
    all contained by the same retry-then-soft-continue path).

    A "down" whose criterion citation can't be resolved is NOT an unparseable
    response: it is returned with criterion_unresolved=True. The re-ask still
    happens (a second sample sometimes cites cleanly), but exhausting it now
    degrades the result instead of discarding it, so "down" verdicts survive
    citation trouble exactly as "up" verdicts always have.
    """
    if turn_type == TURN_TYPE_PROBE:
        raise ProbeTurnJudgeError("judge_turn must never be called for a probe turn")

    prompt = _build_judge_prompt(prompt_text, criteria, response_text)

    last_exc: Optional[Exception] = None
    unresolved: Optional[JudgeVerdict] = None
    for attempt in range(MAX_JUDGE_ATTEMPTS):
        try:
            raw_text = client.chat_json(judge_config.model, judge_config.temperature, prompt)
            verdict = _parse_verdict(raw_text, criteria)
        except (ValueError, KeyError, httpx.HTTPError) as exc:
            last_exc = exc
            logger.warning("judge: parse/call failure (attempt %d/%d): %s", attempt + 1, MAX_JUDGE_ATTEMPTS, exc)
            continue
        if not verdict.criterion_unresolved:
            return verdict
        # Keep the first unresolved "down" as the fallback and re-ask; the
        # judge runs at temperature <= 0.1, so a re-ask rarely changes a
        # citation, but when it does the resolved answer is strictly better.
        unresolved = unresolved or verdict
        logger.warning(
            "judge: unresolvable criterion citation %r (attempt %d/%d); verdict 'down' retained",
            verdict.raw_criterion_citation, attempt + 1, MAX_JUDGE_ATTEMPTS,
        )

    if unresolved is not None:
        return unresolved

    raise JudgeUnparseableError(
        f"judge response unparseable after {MAX_JUDGE_ATTEMPTS} attempt(s) "
        f"for prompt {prompt_text[:100]!r}: {last_exc}"
    )
