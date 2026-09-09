"""Versioned cursors bound to catalog snapshots and body hashes."""

from __future__ import annotations

import base64
import binascii
import json
import re
from dataclasses import dataclass

from common.privacy_policy import LocatorError, normalize_vault_relative
from wiki.content_reference import StoreScope

_PREFIX = "cc1_"
_BODY_PREFIX = "bc1_"
_HEX = re.compile(r"^[0-9a-f]{64}$")


class CatalogCursorError(ValueError):
    def __init__(self, code: str, message: str = "catalog cursor is invalid") -> None:
        super().__init__(message)
        self.code = code


def _encoded_payload(value: str, prefix: str) -> dict[str, object]:
    if not isinstance(value, str) or not value.startswith(prefix):
        raise CatalogCursorError("catalog_cursor_invalid")
    encoded = value[len(prefix) :]
    if not encoded or not re.fullmatch(r"[A-Za-z0-9_-]+", encoded):
        raise CatalogCursorError("catalog_cursor_invalid")
    try:
        raw = base64.b64decode(encoded + "=" * (-len(encoded) % 4), altchars=b"-_", validate=True)
        payload = json.loads(raw.decode("utf-8"))
    except (ValueError, UnicodeDecodeError, json.JSONDecodeError, binascii.Error) as exc:
        raise CatalogCursorError("catalog_cursor_invalid") from exc
    if not isinstance(payload, dict):
        raise CatalogCursorError("catalog_cursor_invalid")
    return payload


def _encode_payload(payload: dict[str, object], prefix: str) -> str:
    raw = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return prefix + base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _validate_fingerprint(value: object) -> str:
    if not isinstance(value, str) or not _HEX.fullmatch(value):
        raise CatalogCursorError("catalog_cursor_invalid")
    return value


@dataclass(frozen=True, slots=True)
class CatalogCursor:
    scope: StoreScope
    filter_fingerprint: str
    snapshot_fingerprint: str
    last_key: tuple[str, str]
    vault: str = ""
    schema_version: int = 1

    def __post_init__(self) -> None:
        if self.schema_version != 1 or self.scope not in {"active", "raw", "archive"}:
            raise CatalogCursorError("catalog_cursor_invalid")
        _validate_fingerprint(self.filter_fingerprint)
        _validate_fingerprint(self.snapshot_fingerprint)
        if not isinstance(self.last_key, tuple) or len(self.last_key) != 2 or not all(isinstance(item, str) for item in self.last_key):
            raise CatalogCursorError("catalog_cursor_invalid")
        _validate_fingerprint(self.last_key[1])
        try:
            identity = normalize_vault_relative(self.last_key[0])
        except LocatorError as exc:
            raise CatalogCursorError("catalog_cursor_invalid") from exc
        object.__setattr__(self, "last_key", (identity, self.last_key[1]))

    def encode(self) -> str:
        return _encode_payload(
            {
                "schema_version": self.schema_version,
                "vault": self.vault,
                "scope": self.scope,
                "filter_fingerprint": self.filter_fingerprint,
                "snapshot_fingerprint": self.snapshot_fingerprint,
                "last_key": list(self.last_key),
            },
            _PREFIX,
        )

    to_string = encode

    @classmethod
    def decode(cls, value: str) -> "CatalogCursor":
        payload = _encoded_payload(value, _PREFIX)
        expected = {"schema_version", "vault", "scope", "filter_fingerprint", "snapshot_fingerprint", "last_key"}
        schema_version = payload.get("schema_version")
        if set(payload) != expected or not isinstance(schema_version, int) or isinstance(schema_version, bool):
            raise CatalogCursorError("catalog_cursor_invalid")
        last_key = payload.get("last_key")
        vault = payload.get("vault")
        if not isinstance(last_key, list) or len(last_key) != 2 or not all(isinstance(item, str) for item in last_key):
            raise CatalogCursorError("catalog_cursor_invalid")
        if not isinstance(vault, str) or not isinstance(payload.get("scope"), str):
            raise CatalogCursorError("catalog_cursor_invalid")
        try:
            return cls(
                scope=payload["scope"],  # type: ignore[arg-type]
                filter_fingerprint=_validate_fingerprint(payload["filter_fingerprint"]),
                snapshot_fingerprint=_validate_fingerprint(payload["snapshot_fingerprint"]),
                last_key=(last_key[0], last_key[1]),
                vault=vault,
                schema_version=schema_version,
            )
        except CatalogCursorError:
            raise
        except (TypeError, ValueError) as exc:
            raise CatalogCursorError("catalog_cursor_invalid") from exc

    from_string = decode


@dataclass(frozen=True, slots=True)
class ContentBodyCursor:
    content_ref: str
    content_hash: str
    offset: int
    schema_version: int = 1

    def __post_init__(self) -> None:
        if self.schema_version != 1 or not isinstance(self.content_ref, str) or not self.content_ref:
            raise CatalogCursorError("catalog_cursor_invalid")
        _validate_fingerprint(self.content_hash)
        if not isinstance(self.offset, int) or isinstance(self.offset, bool) or self.offset < 0:
            raise CatalogCursorError("catalog_cursor_invalid")

    def encode(self) -> str:
        return _encode_payload(
            {
                "schema_version": self.schema_version,
                "content_ref": self.content_ref,
                "content_hash": self.content_hash,
                "offset": self.offset,
            },
            _BODY_PREFIX,
        )

    to_string = encode

    @classmethod
    def decode(cls, value: str) -> "ContentBodyCursor":
        payload = _encoded_payload(value, _BODY_PREFIX)
        expected = {"schema_version", "content_ref", "content_hash", "offset"}
        schema_version = payload.get("schema_version")
        content_ref = payload.get("content_ref")
        offset = payload.get("offset")
        if set(payload) != expected or not isinstance(schema_version, int) or isinstance(schema_version, bool):
            raise CatalogCursorError("catalog_cursor_invalid")
        if not isinstance(content_ref, str) or not isinstance(offset, int) or isinstance(offset, bool):
            raise CatalogCursorError("catalog_cursor_invalid")
        try:
            return cls(
                content_ref=content_ref,
                content_hash=_validate_fingerprint(payload["content_hash"]),
                offset=offset,
                schema_version=schema_version,
            )
        except CatalogCursorError:
            raise
        except (TypeError, ValueError) as exc:
            raise CatalogCursorError("catalog_cursor_invalid") from exc

    from_string = decode


__all__ = ["CatalogCursor", "CatalogCursorError", "ContentBodyCursor"]
