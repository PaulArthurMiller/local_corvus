"""
UM Gate sanitization layer.

Pure, stateless functions with no I/O side effects. Safe to unit-test in
isolation. Reads injection patterns and max_input_length from config.yaml at
module load time.

sanitize_in  — cleans data entering the agent (user input, tool results, LLM responses)
sanitize_out — cleans data leaving the agent (tool calls, final responses, memory writes)
"""

import os
import re
import logging
from pathlib import Path
from typing import Optional
import yaml
from pydantic import BaseModel

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Config — loaded once at import time
# ---------------------------------------------------------------------------

_CONFIG_PATH = Path(
    os.getenv("CONFIG_PATH", str(Path(__file__).parent.parent.parent / "config.yaml"))
)


def _load_gate_config() -> dict:
    try:
        with open(_CONFIG_PATH, "r", encoding="utf-8") as f:
            cfg = yaml.safe_load(f)
        return cfg.get("gate", {})
    except Exception as exc:
        logger.warning("Could not load config.yaml for gate: %s — using defaults", exc)
        return {}


_GATE_CFG = _load_gate_config()
_MAX_INPUT_LENGTH: int = _GATE_CFG.get("max_input_length", 4096)
_RAW_PATTERNS: list[str] = _GATE_CFG.get("injection_patterns", [])
_INJECTION_REGEXES: list[re.Pattern] = [
    re.compile(p, re.IGNORECASE) for p in _RAW_PATTERNS
]


# ---------------------------------------------------------------------------
# Result schema
# ---------------------------------------------------------------------------


class SanitizeResult(BaseModel):
    content: str
    flags: list[str]
    blocked: bool = False


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def sanitize_in(
    content: str,
    boundary: str,
    max_length: Optional[int] = None,
) -> SanitizeResult:
    """
    Sanitize content arriving at the agent.

    Checks length and injection patterns. Does NOT modify content beyond
    truncation — callers see exactly what was detected.
    """
    flags: list[str] = []
    effective_max = max_length if max_length is not None else _MAX_INPUT_LENGTH

    # --- Length check ---
    if len(content) > effective_max:
        flags.append(f"length_exceeded:{len(content)}>{effective_max}")
        content = content[:effective_max]
        logger.warning(
            "sanitize_in [%s]: content truncated to %d chars", boundary, effective_max
        )

    # --- Injection pattern scan ---
    for pattern in _INJECTION_REGEXES:
        if pattern.search(content):
            flag = f"injection_pattern:{pattern.pattern}"
            flags.append(flag)
            logger.warning(
                "sanitize_in [%s]: injection pattern matched — %s",
                boundary,
                pattern.pattern,
            )

    if flags:
        logger.info("sanitize_in [%s]: %d flag(s) raised", boundary, len(flags))
    else:
        logger.debug("sanitize_in [%s]: clean", boundary)

    return SanitizeResult(content=content, flags=flags)


def sanitize_out(
    content: str,
    boundary: str,
    max_length: Optional[int] = None,
) -> SanitizeResult:
    """
    Sanitize content leaving the agent.

    Currently applies the same checks as sanitize_in. Kept as a separate
    function so outbound-specific rules can diverge without touching inbound
    logic.
    """
    flags: list[str] = []
    effective_max = max_length if max_length is not None else _MAX_INPUT_LENGTH

    # --- Length check ---
    if len(content) > effective_max:
        flags.append(f"length_exceeded:{len(content)}>{effective_max}")
        content = content[:effective_max]
        logger.warning(
            "sanitize_out [%s]: content truncated to %d chars", boundary, effective_max
        )

    # --- Injection pattern scan on outbound content ---
    # Guards against the model echoing/amplifying injected instructions.
    for pattern in _INJECTION_REGEXES:
        if pattern.search(content):
            flag = f"injection_pattern:{pattern.pattern}"
            flags.append(flag)
            logger.warning(
                "sanitize_out [%s]: injection pattern in outbound content — %s",
                boundary,
                pattern.pattern,
            )

    if flags:
        logger.info("sanitize_out [%s]: %d flag(s) raised", boundary, len(flags))
    else:
        logger.debug("sanitize_out [%s]: clean", boundary)

    return SanitizeResult(content=content, flags=flags)
