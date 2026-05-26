from __future__ import annotations

import re

REDACTION_PATTERNS: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r"\b1[3-9]\d{9}\b"), "[REDACTED_PHONE]"),
    (re.compile(r"\b[\w.+-]+@[\w-]+(?:\.[\w-]+)+\b"), "[REDACTED_EMAIL]"),
    (re.compile(r"\b\d{17}[0-9Xx]\b"), "[REDACTED_ID_CARD]"),
    (re.compile(r"\b(?:\d[ -]*?){13,19}\b"), "[REDACTED_BANK_CARD]"),
    (re.compile(r"^(\s*Authorization\s*:\s*)[^\r\n]+", re.IGNORECASE | re.MULTILINE), r"\1[REDACTED_SECRET]"),
    (re.compile(r"^(\s*(?:Set-Cookie|Cookie)\s*:\s*).+$", re.IGNORECASE | re.MULTILINE), r"\1[REDACTED_SECRET]"),
    (re.compile(r"\b(session_?id)\s*=\s*[^\s,;]+", re.IGNORECASE), r"\1=[REDACTED_SECRET]"),
    (re.compile(r"\b(?:sk|pk|api|token|secret)[-_][A-Za-z0-9_-]{10,}\b", re.IGNORECASE), "[REDACTED_SECRET]"),
    (re.compile(r"(?i)(password|passwd|secret|token|api_key|apikey)\s*[:=]\s*[^\s,;]+"), r"\1=[REDACTED_SECRET]"),
]


def redact_sensitive_text(text: str) -> str:
    redacted = text
    for pattern, replacement in REDACTION_PATTERNS:
        redacted = pattern.sub(replacement, redacted)
    return redacted


def count_redactions(original_text: str, redacted_text: str) -> int:
    """Count the number of redaction placeholders applied during redaction.

    Compares original and redacted text to count occurrences of REDACTION_PLACEHOLDER_PREFIX.

    Args:
        original_text: The text before redaction.
        redacted_text: The text after redaction.

    Returns:
        Number of redaction placeholders found in the redacted text.
    """
    count = 0
    for marker in _REDACTION_MARKERS:
        # Count occurrences of each redaction marker in the redacted text
        # that were NOT present in the original text
        count += redacted_text.count(marker) - original_text.count(marker)
    return max(count, 0)  # Never return negative


_REDACTION_MARKERS = (
    "[REDACTED_PHONE]",
    "[REDACTED_EMAIL]",
    "[REDACTED_ID_CARD]",
    "[REDACTED_BANK_CARD]",
    "[REDACTED_SECRET]",
)