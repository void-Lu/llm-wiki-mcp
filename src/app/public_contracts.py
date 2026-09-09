"""Stable public MCP result and error contracts."""

from __future__ import annotations

from dataclasses import dataclass, field
import re
from pathlib import Path
from typing import Any, Mapping
from uuid import uuid4

from common.privacy_policy import PrivacyPolicy, project_public_result as _project_public_result


_SAFE_CODE = re.compile(r"^[a-z][a-z0-9_]{0,63}$")
_ERROR_MESSAGES: dict[str, str] = {
    "ambiguous_vault_selector": "vault selectors are ambiguous",
    "archive_state_incompatible": "archive state is incompatible",
    "archive_state_unavailable": "archive state is unavailable",
    "invalid_config": "runtime configuration is invalid",
    "invalid_status_detail": "detail must be summary, indexes, generation, or archive",
    "missing_default_vault": "no default vault is configured",
    "query_timeout": "query exceeded its execution limit",
    "query_cancelled": "query was cancelled",
    "query_capacity_exhausted": "query capacity is exhausted",
    "unknown_vault": "the requested vault is not configured",
    "validation_error": "the request is invalid",
    "internal_error": "the operation could not be completed",
    "invalid_action": "the requested action is invalid",
    "invalid_archive_reason": "the archive reason is invalid",
    "invalid_expansion_terms": "the expansion terms are invalid",
    "invalid_filters": "the query filters are invalid",
    "invalid_content_ref": "the content reference is invalid",
    "invalid_scope": "the query scope is invalid",
    "invalid_top_k": "the query limit is invalid",
    "missing_question": "question is required",
    "missing_note_type": "note_type is required",
    "missing_vault_root": "vault_root is required",
    "path_escape": "the requested path is outside the allowed scope",
    "sensitive_locator": "the locator violates the privacy policy",
    "unsafe_locator": "the locator violates the privacy policy",
    "source_capsules_removed": "legacy source capsule fields are retired",
    "source_hashes_server_owned": "source hashes are server-owned",
    "source_required": "at least one source is required",
    "sources_required": "at least one source is required",
    "source_path_not_allowed": "the source path is outside the allowed raw scope",
    "source_path_escape": "the source path escapes the vault",
    "source_not_found": "the source file was not found",
    "source_not_file": "the source must be a regular file",
    "source_read_failed": "the source could not be read",
    "source_changed_during_read": "the source changed during reading",
    "source_changed": "the source changed before the page was committed",
    "source_write_failed": "the source snapshot could not be committed",
    "source_provenance_invalid": "source provenance could not be verified",
    "invalid_sources": "the sources value is invalid",
    "dependency_update_failed": "the dependency projection is stale",
    "expected_hash_required": "the current page hash is required",
    "expected_hash_mismatch": "the page changed before the update was committed",
    "update_plan_required": "a fresh update plan is required for this change",
    "plan_unknown": "the update plan is unknown",
    "plan_expired": "the update plan has expired",
    "plan_claimed": "the update plan has already been claimed",
    "plan_used": "the update plan has already been used",
    "plan_base_mismatch": "the update plan does not match the current page",
    "plan_intent_drift": "the update intent no longer matches the plan",
    "operation_conflict": "the page changed during the operation",
    "operation_not_found": "the page operation was not found",
    "operation_not_committed": "the page operation has not committed its page fact",
    "write_failed_precommit": "the page fact was not committed",
    "projection_repair_required": "the page was committed and projections need repair",
    "projection_stage_missing": "the configured projection stage is unavailable",
    "plan_consume_pending": "the page was committed and the update plan needs repair",
    "page_state_busy": "the page state store is busy",
    "page_state_incompatible": "the page state store is incompatible",
    "index_missing": "the requested catalog index is missing",
    "index_incompatible": "the requested catalog index is incompatible",
    "index_corrupt": "the requested catalog index cannot be read",
    "catalog_page_size_invalid": "the catalog page size is invalid",
    "catalog_cursor_invalid": "the catalog cursor is invalid",
    "catalog_cursor_stale": "the catalog cursor is stale",
    "content_ref_scope_mismatch": "the content reference is outside the requested scope",
    "content_not_found": "the requested content was not found",
    "content_hash_mismatch": "the content changed before it was read",
    "content_read_failed": "the requested content could not be read",
    "content_budget_exceeded": "the requested body budget exceeds the server limit",
    "content_budget_too_small": "the requested body budget is too small for the next character",
    "content_binary_body_unsupported": "binary asset bodies are not available",
    "archive_manifest_invalid": "the archive manifest is invalid",
    "archive_payload_missing": "an archive payload is missing",
    "archive_hash_mismatch": "an archive payload hash does not match",
    "provenance_migration_cas_mismatch": "the page changed after the provenance plan was created",
    "provenance_source_drift": "a raw source changed after the provenance plan was created",
    "provenance_migration_rolled_back": "the provenance migration was rolled back",
    "provenance_migration_rollback_failed": "the provenance migration needs manual recovery",
    "privacy_audit_cas_mismatch": "the page changed after the privacy plan was created",
    "privacy_locator_review_required": "locator changes require explicit administrative approval",
    "privacy_locator_rename_collision": "the privacy rename target already exists",
    "privacy_audit_rolled_back": "the privacy audit was rolled back",
    "privacy_audit_rollback_failed": "the privacy audit needs manual recovery",
}
_RETRYABLE_CODES = frozenset({"archive_state_unavailable", "index_unavailable", "query_timeout", "query_cancelled", "query_capacity_exhausted", "write_failed", "page_state_busy", "projection_repair_required", "plan_consume_pending", "catalog_cursor_stale", "content_hash_mismatch"})


def new_correlation_id() -> str:
    """Return a non-sensitive request identifier suitable for public errors."""

    return uuid4().hex


def _safe_code(value: object, fallback: str = "internal_error") -> str:
    text = str(value) if value is not None else fallback
    return text if _SAFE_CODE.fullmatch(text) else fallback


def _message_for(code: str, supplied: object | None = None) -> str:
    if code in _ERROR_MESSAGES:
        return _ERROR_MESSAGES[code]
    return _ERROR_MESSAGES["internal_error"]


@dataclass(frozen=True)
class PublicError:
    code: str
    message: str
    retryable: bool = False
    correlation_id: str = field(default_factory=new_correlation_id)

    def to_dict(self) -> dict[str, object]:
        return {
            "code": self.code,
            "message": self.message,
            "retryable": self.retryable,
            "correlation_id": self.correlation_id,
        }

    def to_payload(self) -> dict[str, object]:
        """Serialize as the existing flat MCP envelope plus the new fields."""

        return {"ok": False, **self.to_dict(), "error": self.message}

    @classmethod
    def from_exception(
        cls,
        exc: BaseException,
        *,
        fallback_code: str = "internal_error",
        correlation: str | None = None,
    ) -> "PublicError":
        code = _safe_code(getattr(exc, "code", None), fallback_code)
        if not getattr(exc, "code", None):
            code = fallback_code
        return cls(code, _message_for(code), code in _RETRYABLE_CODES, correlation or new_correlation_id())

    @classmethod
    def from_payload(cls, payload: Mapping[str, Any], *, fallback_code: str = "internal_error") -> "PublicError":
        code = _safe_code(payload.get("code"), fallback_code)
        correlation = payload.get("correlation_id")
        correlation_value = str(correlation) if isinstance(correlation, str) and correlation else new_correlation_id()
        return cls(code, _message_for(code, payload.get("message", payload.get("error"))), bool(payload.get("retryable", code in _RETRYABLE_CODES)), correlation_value)


@dataclass(frozen=True)
class PublicResult:
    """Small typed owner for the public result envelope.

    Domain dictionaries remain accepted at the MCP edge for compatibility;
    this class is the single place for adapters that need a typed success or
    failure result.
    """

    payload: Mapping[str, Any]

    @classmethod
    def success(cls, payload: Mapping[str, Any] | None = None, **fields: Any) -> "PublicResult":
        result = dict(payload or {})
        result.update(fields)
        result.setdefault("ok", True)
        return cls(result)

    @classmethod
    def failure(cls, error: PublicError | str, *, code: str = "internal_error", **fields: Any) -> "PublicResult":
        public_error = error if isinstance(error, PublicError) else PublicError(code, str(error))
        result = public_error.to_payload()
        result.update(fields)
        return cls(result)

    def to_dict(self) -> dict[str, Any]:
        return dict(self.payload)


def public_error(
    code: str,
    *,
    message: str | None = None,
    retryable: bool | None = None,
    correlation: str | None = None,
) -> dict[str, object]:
    normalized = _safe_code(code)
    error = PublicError(
        normalized,
        message if message is not None and "\\" not in message and "\n" not in message else _message_for(normalized),
        _RETRYABLE_CODES.__contains__(normalized) if retryable is None else retryable,
        correlation or new_correlation_id(),
    )
    return error.to_payload()


def public_error_from_exception(exc: BaseException, *, fallback_code: str = "internal_error") -> dict[str, object]:
    return PublicError.from_exception(exc, fallback_code=fallback_code).to_payload()


def project_public_result(
    payload: Mapping[str, Any],
    *,
    logical_vault: str | None = None,
    vault_root: str | Path | None = None,
    policy: PrivacyPolicy | None = None,
) -> dict[str, Any]:
    return _project_public_result(payload, logical_vault=logical_vault, vault_root=vault_root, policy=policy)


__all__ = [
    "PublicError",
    "PublicResult",
    "new_correlation_id",
    "project_public_result",
    "public_error",
    "public_error_from_exception",
]
