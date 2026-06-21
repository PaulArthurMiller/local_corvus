"""
UM Gate sanitization layer.

Pure, stateless functions with no I/O side effects. Safe to unit-test in
isolation. Reads injection patterns and max_input_length from config.yaml at
module load time.

sanitize_in  — cleans data entering the agent (user input, tool results, LLM responses)
sanitize_out — cleans data leaving the agent (tool calls, final responses, memory writes)
"""

import base64
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

_ALLOWED_MIMETYPES: list[str] = _GATE_CFG.get(
    "allowed_attachment_mimetypes",
    ["image/jpeg", "image/png", "image/gif", "image/webp"],
)
_MAX_ATTACHMENT_SIZE: int = _GATE_CFG.get("max_attachment_size_bytes", 5_242_880)

# Magic byte signatures: mime_type -> (expected_bytes, offset)
_MAGIC_BYTES: dict[str, tuple[bytes, int]] = {
    "image/jpeg": (bytes([0xFF, 0xD8, 0xFF]), 0),
    "image/png":  (bytes([0x89, 0x50, 0x4E, 0x47]), 0),
    "image/gif":  (bytes([0x47, 0x49, 0x46, 0x38]), 0),
    # WEBP is checked separately: bytes 0-3 == "RIFF" and bytes 8-11 == "WEBP"
}


# ---------------------------------------------------------------------------
# Result schema
# ---------------------------------------------------------------------------


class SanitizeResult(BaseModel):
    content: str
    flags: list[str]
    blocked: bool = False


class AttachmentSanitizeResult(BaseModel):
    """Result of sanitizing a single image attachment."""
    data_bytes: bytes
    mime_type: str
    filename: str
    size_bytes: int
    flags: list[str]

    model_config = {"arbitrary_types_allowed": True}


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


def sanitize_attachment(
    filename: str,
    mime_type: str,
    data_b64: str,
    config: Optional[dict] = None,
) -> AttachmentSanitizeResult:
    """
    Sanitize an image attachment at the user_input->agent boundary.

    Pure function, no I/O. Checks: MIME allowlist, base64 decodability,
    decoded size limit, magic bytes match. Returns decoded bytes on success
    so callers never re-decode. Returns empty bytes and flags on any failure.
    """
    flags: list[str] = []
    cfg = config or _GATE_CFG

    allowed_mimes: list[str] = cfg.get("allowed_attachment_mimetypes", _ALLOWED_MIMETYPES)
    max_size: int = cfg.get("max_attachment_size_bytes", _MAX_ATTACHMENT_SIZE)

    # --- MIME type allowlist ---
    if mime_type not in allowed_mimes:
        flags.append(f"attachment_mime_rejected:{mime_type}")
        logger.warning(
            "sanitize_attachment: rejected MIME '%s' for '%s'", mime_type, filename
        )
        return AttachmentSanitizeResult(
            data_bytes=b"", mime_type=mime_type, filename=filename,
            size_bytes=0, flags=flags,
        )

    # --- Base64 decode ---
    try:
        data_bytes = base64.b64decode(data_b64, validate=True)
    except Exception as exc:
        flags.append(f"attachment_base64_invalid:{exc}")
        logger.warning(
            "sanitize_attachment: base64 decode failed for '%s': %s", filename, exc
        )
        return AttachmentSanitizeResult(
            data_bytes=b"", mime_type=mime_type, filename=filename,
            size_bytes=0, flags=flags,
        )

    size_bytes = len(data_bytes)

    # --- Size limit ---
    if size_bytes > max_size:
        flags.append(f"attachment_size_exceeded:{size_bytes}>{max_size}")
        logger.warning(
            "sanitize_attachment: '%s' decoded size %d exceeds limit %d",
            filename, size_bytes, max_size,
        )
        return AttachmentSanitizeResult(
            data_bytes=b"", mime_type=mime_type, filename=filename,
            size_bytes=size_bytes, flags=flags,
        )

    # --- Magic bytes ---
    if not _check_magic_bytes(data_bytes, mime_type):
        flags.append(f"attachment_magic_mismatch:{mime_type}")
        logger.warning(
            "sanitize_attachment: magic bytes for '%s' don't match claimed MIME '%s'",
            filename, mime_type,
        )
        return AttachmentSanitizeResult(
            data_bytes=b"", mime_type=mime_type, filename=filename,
            size_bytes=size_bytes, flags=flags,
        )

    logger.info(
        "sanitize_attachment: accepted '%s' mime=%s size=%d bytes",
        filename, mime_type, size_bytes,
    )
    return AttachmentSanitizeResult(
        data_bytes=data_bytes, mime_type=mime_type, filename=filename,
        size_bytes=size_bytes, flags=flags,
    )


def _check_magic_bytes(data: bytes, mime_type: str) -> bool:
    """Return True if the file's leading bytes match the claimed MIME type."""
    if len(data) < 12:
        return False

    if mime_type == "image/webp":
        return data[0:4] == b"RIFF" and data[8:12] == b"WEBP"

    magic_info = _MAGIC_BYTES.get(mime_type)
    if magic_info is None:
        return True  # allowlist already guarded; unknown type passes magic check

    expected, offset = magic_info
    return data[offset: offset + len(expected)] == expected
