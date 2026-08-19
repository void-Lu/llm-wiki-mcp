"""Opaque, vault-relative content references for the read surface."""

from __future__ import annotations

import base64
import binascii
import json
from pathlib import Path
import re
from dataclasses import dataclass
from typing import Literal

from common.privacy_policy import LocatorError, normalize_vault_relative

StoreScope = Literal["active", "raw", "archive"]
ObjectKind = Literal["page", "source", "asset"]
_PREFIX = "cr1_"
_LOGICAL_VAULT = re.compile(r"^[^/\\:\x00-\x1f]{1,128}$")
_KINDS = frozenset({"page", "source", "asset"})
_SCOPES = frozenset({"active", "raw", "archive"})
_TEXT_SUFFIXES = frozenset(
    {
        ".csv",
        ".html",
        ".htm",
        ".json",
        ".log",
        ".md",
        ".rst",
        ".text",
        ".txt",
        ".xml",
        ".yaml",
        ".yml",
    }
)


class ContentReferenceError(ValueError):
    def __init__(self, code: str, message: str = "content reference is invalid") -> None:
        super().__init__(message)
        self.code = code


def _validate_vault(value: str) -> str:
    if not isinstance(value, str) or not _LOGICAL_VAULT.fullmatch(value) or value in {".", ".."}:
        raise ContentReferenceError("invalid_content_ref", "logical vault is invalid")
    return value


def _validate_identity(value: str) -> str:
    try:
        return normalize_vault_relative(value)
    except LocatorError as exc:
        raise ContentReferenceError("invalid_content_ref", "content identity must be vault-relative") from exc


@dataclass(frozen=True, slots=True)
class ContentRefV1:
    """Versioned reference containing no filesystem path or vault root."""

    vault: str
    scope: StoreScope
    kind: ObjectKind
    identity: str
    schema_version: int = 1

    def __post_init__(self) -> None:
        if self.schema_version != 1:
            raise ContentReferenceError("invalid_content_ref", "unsupported content reference version")
        _validate_vault(self.vault)
        if self.scope not in _SCOPES:
            raise ContentReferenceError("invalid_content_ref", "content scope is invalid")
        if self.kind not in _KINDS:
            raise ContentReferenceError("invalid_content_ref", "content kind is invalid")
        identity = _validate_identity(self.identity)
        object.__setattr__(self, "identity", identity)

    def to_payload(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "vault": self.vault,
            "scope": self.scope,
            "kind": self.kind,
            "identity": self.identity,
        }

    def encode(self) -> str:
        raw = json.dumps(self.to_payload(), ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
        return _PREFIX + base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")

    @classmethod
    def decode(cls, value: str) -> "ContentRefV1":
        if not isinstance(value, str) or not value.startswith(_PREFIX):
            raise ContentReferenceError("invalid_content_ref", "content reference encoding is invalid")
        encoded = value[len(_PREFIX) :]
        if not encoded or not re.fullmatch(r"[A-Za-z0-9_-]+", encoded):
            raise ContentReferenceError("invalid_content_ref", "content reference encoding is invalid")
        try:
            padding = "=" * (-len(encoded) % 4)
            raw = base64.b64decode(encoded + padding, altchars=b"-_", validate=True)
            payload = json.loads(raw.decode("utf-8"))
        except (ValueError, UnicodeDecodeError, json.JSONDecodeError, binascii.Error) as exc:
            raise ContentReferenceError("invalid_content_ref", "content reference encoding is invalid") from exc
        if not isinstance(payload, dict) or set(payload) != {"schema_version", "vault", "scope", "kind", "identity"}:
            raise ContentReferenceError("invalid_content_ref", "content reference fields are invalid")
        if not all(isinstance(payload.get(key), str) for key in ("vault", "scope", "kind", "identity")):
            raise ContentReferenceError("invalid_content_ref", "content reference fields are invalid")
        if not isinstance(payload.get("schema_version"), int) or isinstance(payload["schema_version"], bool):
            raise ContentReferenceError("invalid_content_ref", "content reference version is invalid")
        try:
            return cls(
                vault=payload["vault"],
                scope=payload["scope"],  # type: ignore[arg-type]
                kind=payload["kind"],  # type: ignore[arg-type]
                identity=payload["identity"],
                schema_version=payload["schema_version"],
            )
        except ContentReferenceError:
            raise
        except (TypeError, ValueError) as exc:
            raise ContentReferenceError("invalid_content_ref", "content reference fields are invalid") from exc

    @property
    def ref(self) -> str:
        return self.encode()


def content_kind_for_identity(identity: str) -> ObjectKind:
    """Return the catalog kind for a normalized vault-relative identity."""

    return "page" if Path(identity).suffix.casefold() in _TEXT_SUFFIXES else "asset"


def content_scope_for_query_hit(
    *,
    path: str,
    source_kind: str | None = None,
    corpus: str | None = None,
) -> StoreScope:
    """Map a query hit to the physical catalog scope without path guessing."""

    normalized = path.replace("\\", "/").casefold()
    if source_kind == "raw" or corpus == "raw" or normalized.startswith("raw/"):
        return "raw"
    if source_kind == "archive" or corpus == "archive" or normalized.startswith("archives/"):
        return "archive"
    return "active"


def content_ref_for_query_hit(
    logical_vault: str,
    *,
    path: str,
    source_kind: str | None = None,
    corpus: str | None = None,
) -> str | None:
    """Build a canonical reference for a readable query hit.

    Invalid or non-vault-relative paths are omitted instead of being guessed
    into a different catalog entry.  The catalog remains the authority that
    validates the returned reference at read time.
    """

    if not isinstance(path, str) or not path:
        return None
    try:
        identity = _validate_identity(path)
        scope = content_scope_for_query_hit(
            path=identity,
            source_kind=source_kind,
            corpus=corpus,
        )
        return ContentRefV1(
            logical_vault,
            scope,
            content_kind_for_identity(identity),
            identity,
        ).encode()
    except (ContentReferenceError, TypeError, ValueError):
        return None


ContentReference = ContentRefV1

__all__ = [
    "ContentRefV1",
    "ContentReference",
    "ContentReferenceError",
    "ObjectKind",
    "StoreScope",
    "content_kind_for_identity",
    "content_ref_for_query_hit",
    "content_scope_for_query_hit",
]
