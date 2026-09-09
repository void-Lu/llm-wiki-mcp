"""Deterministic index-time redaction and auditable hashes."""

from __future__ import annotations

from dataclasses import dataclass
from hashlib import sha256

from common.redaction import redact_sensitive_text

REDACTION_POLICY_VERSION = "redaction-v1"


@dataclass(frozen=True)
class RedactedContent:
    text: str
    original_hash: str
    redacted_hash: str
    policy_version: str = REDACTION_POLICY_VERSION


def redact_for_index(text: str) -> RedactedContent:
    redacted = redact_sensitive_text(text)
    return RedactedContent(
        text=redacted,
        original_hash=sha256(text.encode("utf-8")).hexdigest(),
        redacted_hash=sha256(redacted.encode("utf-8")).hexdigest(),
    )
