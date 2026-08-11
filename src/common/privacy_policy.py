"""Schema-aware privacy and public-locator projections.

The Wiki domain stores a mixture of human-readable metadata, file locators and
integrity identifiers.  Treating all strings as display text is unsafe: a
redaction regex may make a path unusable or silently change a hash.  This
module owns the small, explicit field taxonomy used by storage and MCP
adapters.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import re
from typing import Any, Literal, Mapping

from common.redaction import redact_sensitive_text


FieldClass = Literal["display", "locator", "integrity", "internal"]

_INTERNAL_FIELDS = frozenset(
    {
        "absolute_path",
        "archive_root",
        "config_path",
        "executable",
        "exception",
        "model_path",
        "workspace_root",
        "index_path",
        "documents_path",
        "executable_path",
        "pid",
        "process_id",
        "username",
        "user_name",
        "home_path",
        "stack_trace",
        "state_path",
        "traceback",
        "vault_root",
    }
)
_LOCATOR_FIELDS = frozenset(
    {
        "archive_path",
        "original_path",
        "page_path",
        "path",
        "path_prefix",
        "project",
        "domain",
        "source_path",
        "relative_path",
        "source",
        "sources",
        "target",
        "identity",
    }
)
_INTEGRITY_FIELDS = frozenset(
    {
        "archive_id",
        "content_hash",
        "correlation_id",
        "cursor",
        "cursor_id",
        "expected_hash",
        "fingerprint",
        "hash",
        "operation_id",
        "page_hash",
        "plan_hash",
        "plan_id",
        "redacted_hash",
        "revision",
        "request_id",
        "session_id",
        "source_hash",
        "source_hashes",
        "source_id",
        "content_ref",
        "page_ref",
        "next_cursor",
        "next_body_cursor",
    }
)
_PATH_SCOPE = "vault_relative"
_ABSOLUTE_PATH = re.compile(r"(?i)(?:^[a-z]:[\\/]|^\\\\|^//|^\\\\\.\\|^/)")
_SENSITIVE_PATH_TEXT = re.compile(r"(?i)(?:[a-z]:[\\/]|\\\\|/home/|/users/|\\users\\|\\appdata\\|globalroot)")


class LocatorError(ValueError):
    """Raised when a value cannot be represented as a safe public locator."""

    def __init__(self, code: str, message: str = "locator is not vault-relative") -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class PrivacyPolicy:
    """Immutable privacy settings shared by writers, projections and MCP."""

    credential_redaction_enabled: bool = True
    redaction_rule_version: str = "v1"
    pii_policy: Literal["preserve", "redact"] = "preserve"

    @classmethod
    def from_settings(cls, settings: object | None) -> "PrivacyPolicy":
        if settings is None:
            return cls()
        return cls(
            credential_redaction_enabled=bool(getattr(settings, "credential_redaction_enabled", True)),
            redaction_rule_version=str(getattr(settings, "redaction_rule_version", "v1")),
            pii_policy=getattr(settings, "pii_policy", "preserve") if getattr(settings, "pii_policy", "preserve") in {"preserve", "redact"} else "preserve",
        )

    def redact_display_text(self, value: str) -> str:
        """Redact credentials always; the configured PII mode is declarative.

        ``credential_redaction_enabled`` is retained in the public status for
        compatibility with the runtime configuration.  A public response or
        persisted Wiki page must not become a secret transport merely because
        a caller supplied ``False``; the common redaction primitive therefore
        remains the final safety boundary.
        """

        return redact_sensitive_text(value)

    def project(self, value: object, *, field: str | None = None, vault_root: str | Path | None = None) -> object:
        return project_public_value(value, policy=self, field=field, vault_root=vault_root)

    def redact_metadata(self, value: object, *, field: str | None = None) -> object:
        return redact_storage_value(value, policy=self, field=field)


def field_class(field: str | None) -> FieldClass:
    """Return the explicit class for a mapping key.

    Unknown keys are display fields by default.  New locator/integrity fields
    must be added here rather than relying on a broad string redactor.
    """

    normalized = field.casefold() if field else ""
    if normalized in _INTERNAL_FIELDS:
        return "internal"
    if normalized in _LOCATOR_FIELDS:
        return "locator"
    if normalized in _INTEGRITY_FIELDS:
        return "integrity"
    return "display"


def normalize_vault_relative(value: str | Path) -> str:
    """Normalize a path-like locator to POSIX form without changing identity."""

    if isinstance(value, Path):
        if value.is_absolute():
            raise LocatorError("absolute_path_forbidden")
        text = value.as_posix()
    else:
        text = str(value).replace("\\", "/")
    if not text or "\x00" in text or _ABSOLUTE_PATH.search(text):
        raise LocatorError("absolute_path_forbidden")
    parts = text.split("/")
    if any(not part or part in {".", ".."} for part in parts):
        raise LocatorError("path_escape")
    if redact_sensitive_text(text) != text:
        raise LocatorError("sensitive_locator")
    return "/".join(parts)


def _relative_to_vault(value: object, vault_root: str | Path | None) -> str | None:
    if not isinstance(value, (str, Path)):
        return None
    if isinstance(value, Path):
        candidate_text = str(value)
    else:
        candidate_text = value
    if not _ABSOLUTE_PATH.search(candidate_text):
        try:
            return normalize_vault_relative(candidate_text)
        except LocatorError:
            return None
    if vault_root is None:
        return None
    try:
        root = Path(vault_root).expanduser().resolve()
        candidate = Path(candidate_text).expanduser().resolve(strict=False)
        if not candidate.is_relative_to(root):
            return None
        return candidate.relative_to(root).as_posix()
    except (OSError, RuntimeError, ValueError):
        return None


def _is_sensitive_path_text(value: str) -> bool:
    if _SENSITIVE_PATH_TEXT.search(value):
        return True
    try:
        return Path(value).is_absolute()
    except (OSError, ValueError):
        return False


def _safe_message(value: str, fallback: str = "The operation could not be completed.") -> str:
    if _is_sensitive_path_text(value):
        return fallback
    redacted = redact_sensitive_text(value)
    if redacted != value:
        return fallback
    return value


def _safe_warning(value: object, *, vault_root: str | Path | None, policy: PrivacyPolicy) -> object:
    if isinstance(value, Mapping):
        return project_public_value(value, policy=policy, field="warning", vault_root=vault_root)
    if not isinstance(value, str):
        return policy.redact_metadata(value, field="warning")
    if value in {"deprecated_vault_root", "absolute_path_removed"}:
        return value
    return _safe_message(value, "warning details omitted for privacy")


def project_public_value(
    value: object,
    *,
    policy: PrivacyPolicy | None = None,
    field: str | None = None,
    vault_root: str | Path | None = None,
) -> object:
    """Project a domain value into the public schema without path leakage."""

    active_policy = policy or PrivacyPolicy()
    category = field_class(field) if field is not None else "display"
    if category == "internal":
        return None
    if category == "integrity":
        if isinstance(value, Mapping):
            return {
                str(key): item
                for key, raw_item in value.items()
                if (item := project_public_value(raw_item, policy=active_policy, field="hash", vault_root=vault_root)) is not None
            }
        if isinstance(value, (list, tuple)):
            return [project_public_value(item, policy=active_policy, field="hash", vault_root=vault_root) for item in value]
        return value
    if category == "locator":
        if isinstance(value, (list, tuple)):
            return [
                normalized
                for item in value
                if (normalized := _relative_to_vault(item, vault_root)) is not None
            ]
        return _relative_to_vault(value, vault_root)
    if isinstance(value, Mapping):
        projected: dict[str, object] = {}
        for raw_key, raw_item in value.items():
            key = str(raw_key)
            if field_class(key) == "internal":
                continue
            if key == "vault" and isinstance(raw_item, (str, Path)) and _is_sensitive_path_text(str(raw_item)):
                continue
            if key == "warnings":
                projected[key] = [
                    _safe_warning(item, vault_root=vault_root, policy=active_policy)
                    for item in (raw_item if isinstance(raw_item, (list, tuple)) else [raw_item])
                ]
                continue
            if key in {"error", "message"} and isinstance(raw_item, str):
                projected[key] = _safe_message(raw_item)
                continue
            item = project_public_value(raw_item, policy=active_policy, field=key, vault_root=vault_root)
            if item is not None:
                projected[key] = item
        return projected
    if isinstance(value, (list, tuple)):
        return [project_public_value(item, policy=active_policy, vault_root=vault_root) for item in value]
    if isinstance(value, str):
        return active_policy.redact_display_text(value)
    return value


def redact_storage_value(value: object, *, policy: PrivacyPolicy | None = None, field: str | None = None) -> object:
    """Apply display redaction while preserving locator and integrity identity."""

    active_policy = policy or PrivacyPolicy()
    category = field_class(field) if field is not None else "display"
    if category in {"locator", "integrity"}:
        if isinstance(value, Mapping):
            return {str(key): redact_storage_value(item, policy=active_policy, field=str(key)) for key, item in value.items()}
        if isinstance(value, (list, tuple)):
            return [redact_storage_value(item, policy=active_policy, field=field) for item in value]
        if category == "locator" and isinstance(value, (str, Path)):
            if str(value) == "":
                return value
            return normalize_vault_relative(value)
        # Integrity values are validated by their owning domain and must never
        # be passed through a display redactor.
        return value
    if isinstance(value, Mapping):
        return {str(key): redact_storage_value(item, policy=active_policy, field=str(key)) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [redact_storage_value(item, policy=active_policy, field=field) for item in value]
    if isinstance(value, str):
        return active_policy.redact_display_text(value)
    return value


def redact_display_metadata(value: object, policy: PrivacyPolicy | None = None) -> object:
    """Compatibility name for the shared display projection primitive."""

    return redact_storage_value(value, policy=policy)


def _contains_public_locator(value: object) -> bool:
    if isinstance(value, Mapping):
        return any(
            (field_class(str(key)) == "locator" and item not in (None, [], "")) or _contains_public_locator(item)
            for key, item in value.items()
        )
    if isinstance(value, (list, tuple)):
        return any(_contains_public_locator(item) for item in value)
    return False


def project_public_result(
    payload: Mapping[str, Any],
    *,
    logical_vault: str | None = None,
    vault_root: str | Path | None = None,
    policy: PrivacyPolicy | None = None,
) -> dict[str, Any]:
    """Project one complete domain result at the MCP/CLI boundary."""

    active_policy = policy or PrivacyPolicy()
    had_absolute_path = "absolute_path" in payload or "vault_root" in payload
    projected_raw = project_public_value(payload, policy=active_policy, vault_root=vault_root)
    projected = dict(projected_raw) if isinstance(projected_raw, Mapping) else {"ok": False, "code": "internal_error"}
    has_locator = _contains_public_locator(projected)
    if logical_vault is not None and (has_locator or "vault" in projected):
        projected["vault"] = logical_vault
    if has_locator:
        projected.setdefault("path_scope", _PATH_SCOPE)
    if had_absolute_path and projected.get("ok") is True:
        warnings = projected.get("warnings", [])
        if not isinstance(warnings, list):
            warnings = [warnings]
        if "absolute_path_removed" not in warnings:
            projected["warnings"] = [*warnings, "absolute_path_removed"]
    return projected


__all__ = [
    "FieldClass",
    "LocatorError",
    "PrivacyPolicy",
    "field_class",
    "normalize_vault_relative",
    "project_public_result",
    "project_public_value",
    "redact_display_metadata",
    "redact_storage_value",
]
