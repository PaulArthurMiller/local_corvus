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

Grading protocol, in the order it was built:

  M19 P2  The judge asserted a verdict and cited a criterion in free text.
  M31     It grades each criterion independently instead, and the verdict is
          derived host-side (derive_verdict) — all met -> up, none met ->
          down, mixed -> none/abstain.
  M37     Each checklist entry must carry the words it copied out of the
          response, criteria are graded against family-specific standards
          ([EXACT] vs [CONCEPT]), and the host verifies the evidence. This
          answers the 2026-08-04 calibration audit, which found the grading
          layer above M31's derivation systematically lenient.
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
    checklist, or an HTTP failure talking to Ollama. Routes through
    error_policy["judge_unparseable"] (default soft_continue) in the runner.

    Under the per-criterion checklist protocol the verdict is always derived
    host-side from the checklist (never asserted by the judge directly), so
    every parse failure is direction-neutral by construction — there is no
    longer an "unresolved citation" failure mode that could only ever destroy
    "down" verdicts (the asymmetry the 2026-07-27 fix addressed under the old
    free-text criterion_triggered protocol).
    """


# --- Milestone 37: criterion families -------------------------------------
#
# The prompt bank writes criteria in two distinct styles, and a single
# grading rule cannot serve both:
#
#   "answer: L-2"                         -> a specific required answer
#   "mentions consistent/fixed field ordering" -> an idea, worded freely
#
# The 2026-08-04 calibration audit found the judge crediting an answer
# criterion because it judged a *different* rule ID to be "a recognized
# synonym". That is the failure literal-match discipline has to kill. But
# applying literal matching to the "mentions ..." criteria — three quarters
# of the bank — would be the opposite error, since those are deliberately
# paraphrase-tolerant by construction. So the judge prompt tags each
# criterion with its family and states a different standard for each.
CRITERION_KIND_EXACT = "exact"
CRITERION_KIND_CONCEPT = "concept"

_ANSWER_PREFIX_RE = re.compile(r"^\s*answer\s*:\s*", re.IGNORECASE)
# An "answer:" criterion whose answer is a whole clause ("all claims must
# remain fully auditable") is a concept criterion wearing an answer label —
# there is no token to match literally. Only short answers ("L-2", "UTC",
# "minute precision") get the exact standard.
MAX_EXACT_ANSWER_WORDS = 4


def _answer_alternatives(criterion: str) -> list[str]:
    """
    Split an "answer: ..." criterion into the answer variants that satisfy it.

    Slashes separate accepted alternatives ("local temporal readings/local
    time"), and a parenthetical is itself an alternative rather than required
    trailing text ("UTC (Universal Coordinated Time)" is satisfied by either
    form). Returns [] for a criterion that is not answer-shaped.
    """
    if not _ANSWER_PREFIX_RE.match(criterion or ""):
        return []
    body = _ANSWER_PREFIX_RE.sub("", criterion).strip()
    alternatives: list[str] = []
    for part in body.split("/"):
        part = part.strip()
        if not part:
            continue
        # "seven (L-1 through L-7)" -> "seven" and "L-1 through L-7".
        match = re.match(r"^(.*?)\s*\(([^)]*)\)\s*$", part)
        if match:
            outside, inside = match.group(1).strip(), match.group(2).strip()
            alternatives.extend(a for a in (outside, inside) if a)
        else:
            alternatives.append(part)
    return alternatives


def _exact_answer_tokens(criterion: str) -> list[str]:
    """
    The short answer variants of an exact-answer criterion — the strings the
    response has to actually contain. Empty for concept criteria and for
    answer criteria whose every variant is a full clause.
    """
    return [a for a in _answer_alternatives(criterion) if len(a.split()) <= MAX_EXACT_ANSWER_WORDS]


def _criterion_kind(criterion: str) -> str:
    """Which grading standard a criterion is held to (see the family note above)."""
    return CRITERION_KIND_EXACT if _exact_answer_tokens(criterion) else CRITERION_KIND_CONCEPT


# --- Milestone 37: evidence flags ------------------------------------------
#
# Recorded per criterion, in the same order as criteria_met, so a calibration
# pass can count how often each judge failure mode fired instead of
# rediscovering them by hand as the 2026-08-04 audit had to.
EVIDENCE_DEMOTED_NO_QUOTE = "demoted_no_quote"
EVIDENCE_UNVERIFIED_QUOTE = "unverified_quote"
EVIDENCE_ANSWER_TOKEN_ABSENT = "answer_token_absent"


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
    # Milestone 37 — the words the judge copied out of the response as its
    # evidence for each criterion, same order/length as criteria_met. None
    # when the judge answered with a bare boolean list and never engaged with
    # the quote field at all.
    criteria_evidence: Optional[list[str]] = None
    # Milestone 37 — per-criterion evidence-check outcomes, same order as
    # criteria_met; each entry is a (possibly empty) list of the EVIDENCE_*
    # constants above. Empty list = the criterion's evidence checked out.
    evidence_flags: Optional[list[list[str]]] = None
    # True when the judge supplied at least one quote, so the evidence rules
    # were applied to this turn. False means the judge returned a bare
    # checklist and enforcement was skipped wholesale (see _parse_verdict).
    evidence_enforced: bool = False


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


def _build_judge_prompt(
    prompt_text: str,
    criteria: list[str],
    response_text: str,
    parse_error: Optional[str] = None,
) -> str:
    """
    Build the judge prompt: Milestone 31's per-criterion checklist, tightened
    for literal-match discipline in Milestone 37.

    M31 (Paul, 2026-08-02) moved the up/down rule off the judge — it grades
    each criterion independently and the harness derives the verdict — because
    the old any-criterion-unmet-means-down framing produced a
    punishment-dominated 34% up-rate, and one criterion at a time is an easier
    task for a small local judge than a global rule in one shot.

    M37 (Paul, 2026-08-05) targets the layer M31 left untouched: the
    2026-08-04 calibration audit found all three operator overrides were the
    same defect — the judge marking a criterion true on a looser standard than
    the criterion states (a checklist contradicting its own rationale, an
    invented synonym, a paraphrase accepted as a match), every one lenient in
    direction. Three changes answer that, all shaped by what gemma4:e4b can
    actually be relied on to do:

    1. **Quote before verdict.** Each checklist entry asks for the exact words
       from the response first and the boolean second. A small model that has
       to produce the evidence before the judgment cannot reach the judgment
       by vibe, and the quote is what makes the claim checkable — host-side
       (see _parse_verdict) and by the operator during calibration.
    2. **Per-family standards.** Criteria are tagged [EXACT] or [CONCEPT] and
       given different rules, because the bank contains both kinds and one
       blanket rule breaks whichever family it wasn't written for.
    3. **An explicit default.** "If unsure, answer false" — a small instruct
       model with no stated default drifts agreeable, which is precisely the
       observed error direction.

    Rules are kept short, imperative, and free of nested conditionals: a 4B
    judge follows a handful of flat rules far more reliably than a precise
    but layered rubric. The response is placed before the criteria so the
    rules land last, closest to the generation.

    The 2026-08-06 recalibration (attempt2, same 20-turn slice as M31) found
    the three changes above had not moved the grading standard: judge-alone
    "up" landed on 14 of 20 in both passes, and all three operator
    corrections were again lenient, zero strict. Reading the judge's own
    quotes, they were one failure mode, not three — the quote named the
    *topic* of the criterion without asserting its *content*:

      criterion "states fields must appear in the fixed declared order"
        quoted "K-1: Addresses field order."             -> marked met
      criterion "notes deviation invalidates parsing"
        quoted "failure to comply will result in a P-fault" -> marked met
      criterion "states fields must appear in declared spine order"
        quoted "Every field must strictly conform to its designated
                position ... defined by Rule K-1."       -> marked met

    None of these were caught by the evidence checks: every quote really was
    in the response, it just did not support the criterion. So the fix has to
    be in the standard, not the verifier. Two more flat rules and one worked
    example target that specific move — naming a subject is not stating a
    fact about it, and the quote has to stand on its own rather than being
    completed by the rest of the response. The example is deliberately
    domain-neutral: an example drawn from the Kestrel fixtures the bank is
    built on would prime the judge on the very turns it grades.

    parse_error carries the previous attempt's rejection reason on a re-ask.
    Without it the retry re-sent a byte-identical prompt at temperature 0.0,
    which is very nearly a deterministic repeat of the failure — the retry
    budget existed but bought almost nothing.
    """
    criteria_lines = "\n".join(
        f"{i}. [{_criterion_kind(c).upper()}] {c}" for i, c in enumerate(criteria, 1)
    )
    # Built by concatenation rather than one f-string: the schema example is
    # dense in braces, and doubling every one of them to survive f-string
    # interpolation makes the literal the model sees unreadable in source.
    schema_example = (
        '{"checklist": [{"quote": "<exact words copied from the response, or an '
        'empty string>", "met": true or false}], "rationale": "<one or two sentences>"}'
    )
    # A structurally exact copy of the observed failure, in a subject the
    # prompt bank never touches — the judge must learn the move, not the
    # fixture.
    worked_example = (
        'Worked example. Criterion: "states the timeout must be set to 30 seconds".\n'
        '  Quote "Section 4 covers the timeout setting." -> met: false. It names the '
        "subject and never states the value.\n"
        '  Quote "Set the timeout to 30 seconds." -> met: true. It states the thing the '
        "criterion asks for."
    )
    correction = (
        (
            f"\n\nYour previous answer was rejected: {parse_error}\n"
            "Return only the JSON object described above — no prose, no markdown fences, "
            f"exactly {len(criteria)} checklist entries."
        )
        if parse_error
        else ""
    )
    return (
        "You are grading an AI assistant's response against a fixed checklist of criteria.\n\n"
        f"Original prompt given to the assistant:\n{prompt_text}\n\n"
        f"Assistant's response:\n{response_text}\n\n"
        f"Criteria to grade independently, in order:\n{criteria_lines}\n\n"
        "Grading rules — apply them strictly:\n"
        "- Grade only what the response actually says. Never mark a criterion met because "
        "the response is close to it, related to it, or implies it.\n"
        "- For each criterion, first copy the exact words from the response that satisfy it "
        'into "quote". If no words in the response satisfy it, "quote" must be an empty '
        'string and "met" must be false.\n'
        "- Naming a subject is not stating a fact about it. A quote that only points at "
        "what the criterion is about — a rule number, a heading, a label, or a sentence "
        "saying the subject matters — does not meet a criterion asking the response to "
        "state something specific about that subject.\n"
        "- The quote by itself must satisfy the criterion. Do not use anything else in the "
        "response to fill in what the quote leaves out.\n"
        "- [EXACT] criteria name a specific required answer. The response must state that "
        "answer itself. A different code, label, number, or term does not count, even if it "
        "looks similar or you believe it means the same thing. Never treat two different "
        "labels as synonyms.\n"
        "- [CONCEPT] criteria describe an idea. The response may word the idea differently, "
        "but it must actually state the idea, not merely hint at it or leave it implied. A "
        "different claim about the same subject is not the idea.\n"
        "- A criterion that asks for more than one thing is met only if every part is present.\n"
        "- If you are unsure, answer false. False is the default.\n"
        "- Judge each criterion on its own. Do not give an overall pass/fail — that is "
        "derived from your answers, not by you.\n\n"
        f"{worked_example}\n\n"
        f"Return ONLY a JSON object of this exact shape:\n{schema_example}\n\n"
        f'"checklist" must hold exactly {len(criteria)} objects, one per criterion, in the '
        "SAME order as the criteria listed above — no more, no fewer. No prose, no markdown "
        f"fences.{correction}"
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


def derive_verdict(criteria_met: list, criteria: list[str]) -> tuple:
    """
    Apply the Milestone 31 derivation rule to a checklist, returning
    (verdict, criterion_triggered): all criteria met -> "up", none met ->
    "down", anything mixed -> "none" (a partial answer gets no feedback and no
    weight update, matching the backend's "absence of signal is an explicit
    observation" contract, M8).

    Public and shared on purpose. The runner's --interactive per-criterion
    regrade (M37) re-derives the verdict from the operator's corrected
    checklist, and that has to be the same rule the judge path uses — a
    calibration pass whose two sides applied different rules would measure
    the rule, not the judge. The 2026-08-04 audit found no override
    indicting this rule, so it is deliberately unchanged by M37.
    """
    if all(criteria_met):
        return "up", None
    if not any(criteria_met):
        # All criteria failed, so which one is cited is uninformative by
        # construction — the rationale carries the real detail. The first
        # listed criterion satisfies the backend's down-requires-criterion
        # contract without inventing or guessing anything.
        return "down", criteria[0]
    return "none", None


def _normalize_for_evidence(text: str) -> str:
    """
    Reduce text to a form in which a judge's quote can be checked against the
    response it was supposedly copied from.

    Deliberately loose about presentation (case, whitespace, the quote and
    emphasis characters a model wraps citations in, and trailing ellipses)
    and strict about words: a model that re-cases or re-wraps a real quote is
    still quoting, but one that changes the words is not.
    """
    normalized = (text or "").strip().strip(_WRAPPER_CHARS)
    normalized = normalized.replace("…", " ").replace("...", " ")
    normalized = re.sub(r"[\"'“”‘’`*_]", "", normalized)
    # Collapse *then* strip: collapsing "order..." to "order " leaves a
    # trailing space that would defeat the substring check against a response
    # reading "order, always" — a false "unverified_quote" on a real quote.
    normalized = re.sub(r"\s+", " ", normalized).strip()
    return normalized.casefold()


def _check_evidence(
    index: int,
    criterion: str,
    met: bool,
    quote: str,
    normalized_response: str,
) -> tuple:
    """
    Verify one criterion's evidence, returning (met, flags).

    Only one check can change a judgment, and only in the strict direction:
    **a criterion marked met with no quote at all is forced to false.** That
    is not second-guessing the judge — the protocol tells it that an empty
    quote requires `met: false`, so the pair (met=true, quote="") is the
    checklist contradicting itself, which is exactly the defect the
    2026-08-04 audit found on turn 5. Forcing it resolves the contradiction
    the only way the protocol allows.

    The two content checks — a quote that does not appear in the response,
    and an [EXACT] criterion whose required answer is nowhere in the response
    — only raise flags. They are heuristics over normalized text (a judge may
    legitimately stitch two clauses together, and "minute-level precision"
    against a response saying "minute precision" would fire), and silently
    reclassifying a verdict on a heuristic would corrupt the record this run
    exists to produce. Flagged criteria are surfaced in --interactive
    calibration and counted in the calibration summary, so the recalibration
    pass measures how often each fires before anyone decides to promote a
    flag into a demotion. Deciding that is Paul's call on real numbers, not
    a guess made here.
    """
    flags: list[str] = []
    if not met:
        return met, flags

    normalized_quote = _normalize_for_evidence(quote)
    if not normalized_quote:
        logger.warning(
            "judge: criterion %d marked met with no supporting quote — forcing false "
            "(protocol requires met=false when quote is empty)", index + 1,
        )
        return False, [EVIDENCE_DEMOTED_NO_QUOTE]

    if normalized_quote not in normalized_response:
        flags.append(EVIDENCE_UNVERIFIED_QUOTE)

    tokens = _exact_answer_tokens(criterion)
    if tokens and not any(_normalize_for_evidence(t) in normalized_response for t in tokens):
        flags.append(EVIDENCE_ANSWER_TOKEN_ABSENT)

    return met, flags


def _extract_checklist(data: dict, criteria: list[str]) -> tuple:
    """
    Pull (criteria_met, criteria_evidence) out of the judge's JSON, accepting
    every shape gemma4:e4b plausibly emits for the M37 schema.

    Tolerance here is not sloppiness — it is the cheapest available lever on
    the unparseable rate. A rejected response costs a re-ask and, if that also
    fails, a whole turn's verdict; accepting a correctly-graded checklist that
    arrived under the M31 key name costs nothing and changes no judgment.
    Accepted shapes:

      {"checklist":   [{"quote": "...", "met": true}, ...]}   <- M37, requested
      {"criteria":    [{"quote": "...", "met": true}, ...]}   <- key drift
      {"criteria_met": [true, false, ...]}                    <- M31 bare list
      {"checklist":   [true, false, ...]}                     <- shape drift

    Returns (criteria_met, criteria_evidence, quote_field_present).
    criteria_evidence is None for the two bare-list shapes: the judge never
    engaged with the checklist-object schema at all, so there is no evidence
    to check.

    quote_field_present distinguishes the two ways a checklist can arrive
    without evidence, and the distinction decides whether a met criterion
    gets demoted (see _parse_verdict). An entry that *omits* "quote" is a
    model that dropped an output field. An entry that supplies "quote": ""
    while claiming met is a model answering the field and contradicting
    itself — the protocol says an empty quote requires met=false. Collapsing
    those two into one "no evidence" case is what an earlier draft did, and
    it silently exempted every single-criterion prompt from the demotion
    rule: with one criterion there is no other entry to carry a quote, so
    the whole turn looked like a dropped field. Single-criterion prompts are
    the entire cluster_d "answer: X" family — precisely where the
    2026-08-04 audit's synonym defect lives.

    Raises ValueError on anything else — wrong length, non-bool "met",
    missing key — which routes through judge_turn's direction-neutral retry.
    """
    entries = None
    for key in ("checklist", "criteria", "criteria_met"):
        if isinstance(data.get(key), list):
            entries = data[key]
            break
    if entries is None:
        raise ValueError(
            f"judge response carried no checklist: expected a list under "
            f'"checklist" (or "criteria_met"), got keys {sorted(data)!r}'
        )
    if len(entries) != len(criteria):
        raise ValueError(
            f"judge checklist has {len(entries)} entries but the prompt carried "
            f"{len(criteria)} criteria — one entry per criterion, in order, is required"
        )

    criteria_met: list[bool] = []
    criteria_evidence: list[str] = []
    saw_object = False
    quote_field_present = False
    for i, entry in enumerate(entries):
        if isinstance(entry, bool):
            criteria_met.append(entry)
            criteria_evidence.append("")
            continue
        if isinstance(entry, dict) and isinstance(entry.get("met"), bool):
            saw_object = True
            criteria_met.append(entry["met"])
            quote = entry.get("quote")
            if isinstance(quote, str):
                quote_field_present = True
            criteria_evidence.append(quote if isinstance(quote, str) else "")
            continue
        raise ValueError(
            f"judge checklist entry {i + 1} is {entry!r} — expected "
            '{"quote": "...", "met": true/false} or a bare true/false'
        )

    return criteria_met, (criteria_evidence if saw_object else None), quote_field_present


def _parse_verdict(raw_text: str, criteria: list[str], response_text: str = "") -> JudgeVerdict:
    """
    Parse the judge's per-criterion checklist, verify its evidence, and derive
    the verdict host-side.

    Deriving the verdict from a fixed-length checklist rather than asking the
    judge to apply the all-vs-none rule itself is the M31 point: the old
    any-criterion-unmet-means-down framing produced a punishment-dominated
    34% up-rate on exp0's demanding criteria, and per-criterion grading is an
    easier task for a small local judge than a global rule. M37 adds the
    evidence layer above it — see _check_evidence for what can and cannot
    change a judgment.

    Evidence enforcement is all-or-nothing per turn by design. If the judge
    returned a bare boolean list with no quotes anywhere, the quote rule is
    skipped entirely rather than demoting every met criterion — a 4B model
    that ignores an output field is a protocol miss, and turning that into a
    turn where nothing is met would manufacture a "down" out of a formatting
    failure. `evidence_enforced` records which regime the turn was graded
    under so the two are never pooled in analysis.

    Raises ValueError (caught by judge_turn's retry loop, direction-neutral
    per JudgeUnparseableError) on the malformed shapes _extract_checklist
    rejects.
    """
    try:
        data = json.loads(raw_text)
    except (json.JSONDecodeError, TypeError) as exc:
        raise ValueError(f"judge response was not valid JSON: {exc}") from exc

    if not isinstance(data, dict):
        raise ValueError(f"judge response JSON was not an object: {raw_text[:200]!r}")

    criteria_met, criteria_evidence, quote_field_present = _extract_checklist(data, criteria)

    # Enforce whenever the judge answered in checklist-object form, with one
    # bailout: it returned objects that omit the "quote" key entirely while
    # still marking something met. That is a model dropping an output field,
    # not a response that failed every criterion, and demoting the lot would
    # manufacture a "down" out of a formatting miss — the one direction of
    # error this milestone must not introduce while fixing the other. A
    # supplied-but-empty quote is NOT this case: see _extract_checklist.
    dropped_quote_field = (
        criteria_evidence is not None and any(criteria_met) and not quote_field_present
    )
    if dropped_quote_field:
        logger.warning(
            "judge: checklist marked %d/%d criteria met but omitted the quote field "
            "entirely — evidence enforcement skipped for this turn",
            sum(1 for m in criteria_met if m), len(criteria_met),
        )
    evidence_enforced = criteria_evidence is not None and not dropped_quote_field
    evidence_flags: Optional[list[list[str]]] = None
    if evidence_enforced:
        normalized_response = _normalize_for_evidence(response_text)
        checked = [
            _check_evidence(i, criterion, met, quote, normalized_response)
            for i, (criterion, met, quote) in enumerate(
                zip(criteria, criteria_met, criteria_evidence)
            )
        ]
        criteria_met = [met for met, _ in checked]
        evidence_flags = [flags for _, flags in checked]

    verdict, criterion_triggered = derive_verdict(criteria_met, criteria)

    return JudgeVerdict(
        verdict=verdict,
        rationale=data.get("rationale"),
        criterion_triggered=criterion_triggered,
        criteria_met=criteria_met,
        criteria_evidence=criteria_evidence,
        evidence_flags=evidence_flags,
        evidence_enforced=evidence_enforced,
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
    missing/wrong-length/non-bool checklist, and HTTP/connectivity
    failures talking to Ollama — all contained by the same
    retry-then-soft-continue path). Every failure here is direction-neutral:
    unlike the pre-Milestone-31 free-text citation protocol, there is no verdict
    to partially trust when the checklist is malformed.

    The response text is passed to _parse_verdict as well as into the prompt
    (M37) — the judge's quoted evidence is checked against it host-side.

    The re-ask names the reason the previous answer was rejected. Judges run
    at temperature 0.0, so re-sending the identical prompt was close to
    asking the same deterministic question twice: the 2026-08-06 calibration
    spent its whole retry budget on 1 of 20 turns and still lost the verdict.
    HTTP failures are the exception — there is no rejected answer to correct,
    so those retry unchanged.
    """
    if turn_type == TURN_TYPE_PROBE:
        raise ProbeTurnJudgeError("judge_turn must never be called for a probe turn")

    last_exc: Optional[Exception] = None
    parse_error: Optional[str] = None
    for attempt in range(MAX_JUDGE_ATTEMPTS):
        prompt = _build_judge_prompt(prompt_text, criteria, response_text, parse_error=parse_error)
        try:
            raw_text = client.chat_json(judge_config.model, judge_config.temperature, prompt)
            return _parse_verdict(raw_text, criteria, response_text)
        except (ValueError, KeyError) as exc:
            last_exc = exc
            parse_error = str(exc)
            logger.warning("judge: parse failure (attempt %d/%d): %s", attempt + 1, MAX_JUDGE_ATTEMPTS, exc)
        except httpx.HTTPError as exc:
            last_exc = exc
            logger.warning("judge: call failure (attempt %d/%d): %s", attempt + 1, MAX_JUDGE_ATTEMPTS, exc)

    raise JudgeUnparseableError(
        f"judge response unparseable after {MAX_JUDGE_ATTEMPTS} attempt(s) "
        f"for prompt {prompt_text[:100]!r}: {last_exc}"
    )
