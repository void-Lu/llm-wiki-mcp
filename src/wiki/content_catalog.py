"""Read-only metadata catalog and bounded exact-content reads."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from archive.archive_manifest import load_manifest, verify_bundle
from archive.archive_models import ArchiveError
from common.privacy_policy import LocatorError, PrivacyPolicy, normalize_vault_relative
from retrieval.metadata_filters import metadata_filter_fingerprint, normalize_metadata_filters
from retrieval.retrieval_index import RetrievalIndexError, RetrievalIndexStore, eligible_path, page_from_file
from wiki.catalog_cursor import CatalogCursor, CatalogCursorError, ContentBodyCursor
from wiki.content_reference import ContentRefV1, ContentReferenceError, ObjectKind, StoreScope
from wiki.wiki_io import split_frontmatter
from wiki.wiki_paths import filesystem_path

DEFAULT_CATALOG_PAGE_SIZE = 20
MAX_CATALOG_PAGE_SIZE = 100
DEFAULT_BODY_BUDGET = 16 * 1024
MAX_BODY_BUDGET = DEFAULT_BODY_BUDGET
_TEXT_SUFFIXES = frozenset({
    ".csv", ".html", ".htm", ".json", ".log", ".md", ".rst", ".text", ".txt", ".xml", ".yaml", ".yml",
})


class ContentCatalogError(RuntimeError):
    def __init__(self, code: str, message: str = "content catalog operation failed") -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True, slots=True)
class CatalogItem:
    content_ref: str
    page_ref: str
    identity: str
    scope: StoreScope
    kind: ObjectKind
    title: str
    page_type: str
    summary: str
    project: str
    tags: tuple[str, ...]
    corpus: str
    authority: str
    lifecycle_status: str
    source_kind: str
    freshness: str
    content_hash: str
    size_bytes: int
    mtime_ns: int
    session_id: str
    occurred_at: str
    body_available: bool

    def to_dict(self) -> dict[str, object]:
        return {
            "content_ref": self.content_ref,
            "page_ref": self.page_ref,
            "identity": self.identity,
            "scope": self.scope,
            "kind": self.kind,
            "title": self.title,
            "type": self.page_type,
            "summary": self.summary,
            "project": self.project,
            "tags": list(self.tags),
            "corpus": self.corpus,
            "authority": self.authority,
            "lifecycle_status": self.lifecycle_status,
            "source_kind": self.source_kind,
            "freshness": self.freshness,
            "content_hash": self.content_hash,
            "size_bytes": self.size_bytes,
            "mtime_ns": self.mtime_ns,
            "session_id": self.session_id,
            "occurred_at": self.occurred_at,
            "body_available": self.body_available,
        }


def _safe_tags(frontmatter: object) -> tuple[str, ...]:
    if not isinstance(frontmatter, dict):
        return ()
    raw = frontmatter.get("tags", ())
    if isinstance(raw, str):
        return (raw,) if raw else ()
    if not isinstance(raw, (list, tuple)):
        return ()
    return tuple(dict.fromkeys(str(item) for item in raw if isinstance(item, (str, int, float)) and str(item)))


def _int_field(row: Mapping[str, object], key: str) -> int:
    value = row.get(key)
    if value is None:
        return 0
    if isinstance(value, int):
        return value
    if isinstance(value, (str, bytes, bytearray, float)):
        return int(value)
    return 0


def _kind_for(identity: str) -> ObjectKind:
    return "page" if Path(identity).suffix.casefold() in _TEXT_SUFFIXES else "asset"


def _utf8_prefix(value: str, budget: int) -> tuple[str, bool]:
    encoded = value.encode("utf-8")
    if len(encoded) <= budget:
        return value, False
    chunk = encoded[:budget]
    while chunk:
        try:
            return chunk.decode("utf-8"), True
        except UnicodeDecodeError:
            chunk = chunk[:-1]
    return "", True


class ContentCatalogService:
    """The sole owner of list/get catalog semantics.

    Construction and all read methods are side-effect free: a missing index
    is an explicit result and never triggers directory creation or a build.
    """

    def __init__(self, vault_root: str | Path, *, logical_vault: str) -> None:
        self.root = Path(vault_root).expanduser().resolve()
        self.logical_vault = logical_vault
        if not logical_vault:
            raise ContentCatalogError("invalid_content_ref", "logical vault is required")

    def list_items(
        self,
        *,
        scope: StoreScope = "active",
        filters: Mapping[str, Any] | None = None,
        page_size: int = DEFAULT_CATALOG_PAGE_SIZE,
        cursor: str | None = None,
    ) -> dict[str, object]:
        if scope not in {"active", "raw", "archive"}:
            raise ContentCatalogError("invalid_scope", "catalog scope must be active, raw, or archive")
        if page_size < 1 or page_size > MAX_CATALOG_PAGE_SIZE:
            raise ContentCatalogError("catalog_page_size_invalid", "catalog page size exceeds the hard limit")
        try:
            normalized = normalize_metadata_filters(filters)
        except ValueError as exc:
            raise ContentCatalogError("invalid_filters", str(exc)) from exc
        filter_hash = metadata_filter_fingerprint(normalized)
        after: tuple[str, str] | None = None
        decoded: CatalogCursor | None = None
        if cursor is not None:
            try:
                decoded = CatalogCursor.decode(cursor)
            except CatalogCursorError as exc:
                raise ContentCatalogError(exc.code) from exc
            if decoded.vault != self.logical_vault or decoded.scope != scope or decoded.filter_fingerprint != filter_hash:
                raise ContentCatalogError("catalog_cursor_invalid", "catalog cursor does not match this request")
            after = decoded.last_key

        store = RetrievalIndexStore(self.root, scope=scope)
        try:
            result = store.list_catalog_items(filters=normalized, after=after, limit=page_size)
        except RetrievalIndexError as exc:
            raise ContentCatalogError(exc.code, "catalog index is unavailable") from exc
        fingerprint = str(result.get("fingerprint", ""))
        if cursor is not None:
            assert decoded is not None
            if decoded.snapshot_fingerprint != fingerprint:
                raise ContentCatalogError("catalog_cursor_stale", "catalog snapshot changed")

        raw_items = result.get("items", [])
        items = [
            self._item_from_row(row, scope)
            for row in raw_items
            if isinstance(row, dict)
        ] if isinstance(raw_items, list) else []
        if scope == "archive":
            self._validate_archive_manifests(items)
        next_cursor: str | None = None
        if bool(result.get("has_more")) and items:
            last = items[-1]
            next_cursor = CatalogCursor(
                vault=self.logical_vault,
                scope=scope,
                filter_fingerprint=filter_hash,
                snapshot_fingerprint=fingerprint,
                last_key=(last.identity, last.content_hash),
            ).encode()
        return {
            "ok": True,
            "scope": scope,
            "items": [item.to_dict() for item in items],
            "next_cursor": next_cursor,
            "snapshot_fingerprint": fingerprint,
            "page_size": page_size,
        }

    def get_item(
        self,
        content_ref: str,
        *,
        include_body: bool = False,
        max_bytes: int = DEFAULT_BODY_BUDGET,
        cursor: str | None = None,
    ) -> dict[str, object]:
        try:
            ref = ContentRefV1.decode(content_ref)
        except ContentReferenceError as exc:
            raise ContentCatalogError(exc.code) from exc
        if ref.vault != self.logical_vault:
            raise ContentCatalogError("content_ref_scope_mismatch", "content reference belongs to another vault")
        if max_bytes < 1 or max_bytes > MAX_BODY_BUDGET:
            raise ContentCatalogError("content_budget_exceeded", "body budget exceeds the server limit")
        if cursor is not None and not include_body:
            raise ContentCatalogError("catalog_cursor_invalid", "body cursor requires include_body")
        if not eligible_path(ref.identity, scope=ref.scope):
            raise ContentCatalogError("content_ref_scope_mismatch", "content reference is outside its scope")
        if ref.kind != _kind_for(ref.identity):
            raise ContentCatalogError("content_ref_scope_mismatch", "content reference kind does not match the catalog")

        store = RetrievalIndexStore(self.root, scope=ref.scope)
        try:
            row = store.get_catalog_item(ref.identity)
        except RetrievalIndexError as exc:
            raise ContentCatalogError(exc.code, "catalog index is unavailable") from exc
        if row is None:
            raise ContentCatalogError("content_not_found", "content is not present in the selected catalog")
        item = self._item_from_row(row, ref.scope)
        if item.content_ref != content_ref:
            raise ContentCatalogError("content_ref_scope_mismatch", "content reference is not canonical")
        result: dict[str, object] = {"ok": True, **item.to_dict(), "item": item.to_dict()}
        if not include_body:
            return result
        if ref.kind == "asset":
            raise ContentCatalogError("content_binary_body_unsupported", "binary asset bodies are not available in v1")

        target, archive_manifest = self._trusted_target(ref)
        if archive_manifest is not None:
            self._verify_archive_reference(ref, archive_manifest)
        page, redactions_applied = self._read_current_page(ref, target, str(row["content_hash"]))
        body = page.body
        content_hash = str(row["content_hash"])
        body_cursor = None
        offset = 0
        if cursor is not None:
            try:
                body_cursor = ContentBodyCursor.decode(cursor)
            except CatalogCursorError as exc:
                raise ContentCatalogError(exc.code) from exc
            if body_cursor.content_ref != content_ref or body_cursor.content_hash != content_hash:
                raise ContentCatalogError("catalog_cursor_stale", "content changed since the body cursor was issued")
            offset = body_cursor.offset
            if offset > len(body):
                raise ContentCatalogError("catalog_cursor_stale", "body cursor is outside the current content")
        fragment, truncated = _utf8_prefix(body[offset:], max_bytes)
        if truncated and not fragment:
            raise ContentCatalogError("content_budget_too_small", "body budget cannot contain the next UTF-8 character")
        next_body_cursor = None
        if truncated:
            next_offset = offset + len(fragment)
            next_body_cursor = ContentBodyCursor(content_ref, content_hash, next_offset).encode()
        result.update(
            {
                "body": fragment,
                "body_bytes": len(fragment.encode("utf-8")),
                "truncated": truncated,
                "next_body_cursor": next_body_cursor,
                "redacted": redactions_applied,
                "redactions_applied": redactions_applied,
                "round_trip_safe": not redactions_applied,
            }
        )
        return result

    list = list_items
    get = get_item

    def _item_from_row(self, row: dict[str, object], scope: StoreScope) -> CatalogItem:
        identity = str(row.get("path", ""))
        try:
            canonical = normalize_vault_relative(identity)
        except LocatorError as exc:
            raise ContentCatalogError("invalid_content_ref", "catalog contains an unsafe identity") from exc
        kind = _kind_for(canonical)
        ref = ContentRefV1(self.logical_vault, scope, kind, canonical).encode()
        frontmatter = row.get("frontmatter") if isinstance(row.get("frontmatter"), dict) else {}
        return CatalogItem(
            content_ref=ref,
            page_ref=ref,
            identity=canonical,
            scope=scope,
            kind=kind,
            title=str(row.get("title") or ""),
            page_type=str(row.get("page_type") or ""),
            summary=str(row.get("summary") or ""),
            project=str(row.get("project") or ""),
            tags=_safe_tags(frontmatter),
            corpus=str(row.get("corpus") or ""),
            authority=str(row.get("authority") or ""),
            lifecycle_status=str(row.get("lifecycle_status") or ""),
            source_kind=str(row.get("source_kind") or ""),
            freshness=str(row.get("freshness") or ""),
            content_hash=str(row.get("content_hash") or ""),
            size_bytes=_int_field(row, "size_bytes"),
            mtime_ns=_int_field(row, "mtime_ns"),
            session_id=str(row.get("session_id") or ""),
            occurred_at=str(row.get("occurred_at") or ""),
            body_available=kind == "page",
        )

    def _trusted_target(self, ref: ContentRefV1) -> tuple[Path, object | None]:
        try:
            identity = normalize_vault_relative(ref.identity)
        except LocatorError as exc:
            raise ContentCatalogError("invalid_content_ref") from exc
        if ref.scope == "archive":
            bundle, manifest = self._archive_bundle(identity)
            relative = identity.split("/", 5)[-1]
            target = (bundle / relative).resolve()
            if not target.is_relative_to(bundle.resolve()):
                raise ContentCatalogError("content_ref_scope_mismatch")
            return target, manifest
        candidate = self.root / identity
        target = candidate.resolve()
        if candidate.is_symlink() or not target.is_relative_to(self.root) or not target.is_file():
            raise ContentCatalogError("content_not_found")
        return target, None

    def _archive_bundle(self, identity: str) -> tuple[Path, object]:
        parts = identity.split("/")
        if len(parts) < 6 or parts[:2] != ["archives", "bundles"]:
            raise ContentCatalogError("content_ref_scope_mismatch")
        bundle = self._safe_archive_bundle(parts)
        try:
            manifest = verify_bundle(self.root, bundle)
        except ArchiveError as exc:
            raise ContentCatalogError(exc.code, "archive bundle failed verification") from exc
        return bundle, manifest

    def _validate_archive_manifests(self, items: list[CatalogItem]) -> None:
        checked: set[str] = set()
        for item in items:
            parts = item.identity.split("/")
            if len(parts) < 5:
                raise ContentCatalogError("archive_manifest_invalid")
            bundle_key = "/".join(parts[:5])
            if bundle_key in checked:
                continue
            bundle = self._safe_archive_bundle(parts)
            try:
                manifest = load_manifest(bundle)
            except ArchiveError as exc:
                raise ContentCatalogError(exc.code, "archive manifest failed validation") from exc
            if bundle.name != manifest.archive_id:
                raise ContentCatalogError("archive_manifest_invalid")
            relative = "/".join(parts[5:])
            if not any(getattr(entry, "archive_path", None) == relative for entry in manifest.items):
                raise ContentCatalogError("archive_manifest_invalid")
            checked.add(bundle_key)

    def _safe_archive_bundle(self, parts: list[str]) -> Path:
        candidate = self.root.joinpath(*parts[:5])
        try:
            bundle = candidate.resolve()
        except (OSError, RuntimeError) as exc:
            raise ContentCatalogError("archive_manifest_invalid") from exc
        is_junction = getattr(candidate, "is_junction", None)
        if (
            not candidate.is_dir()
            or candidate.is_symlink()
            or bool(is_junction and is_junction())
            or not bundle.is_relative_to(self.root)
            or bundle.name != parts[4]
        ):
            raise ContentCatalogError("archive_manifest_invalid")
        return bundle

    def _verify_archive_reference(self, ref: ContentRefV1, manifest: object) -> None:
        items = getattr(manifest, "items", None)
        if not isinstance(items, (list, tuple)):
            raise ContentCatalogError("archive_manifest_invalid")
        relative = ref.identity.split("/", 5)[-1]
        if not any(getattr(item, "archive_path", None) == relative for item in items):
            raise ContentCatalogError("content_not_found")

    def _read_current_page(self, ref: ContentRefV1, target: Path, expected_hash: int | str) -> tuple[Any, bool]:
        fs_root = filesystem_path(self.root)
        fs_target = filesystem_path(target)
        page = page_from_file(fs_root, fs_target, scope=ref.scope)
        if page is None:
            raise ContentCatalogError("content_ref_scope_mismatch")
        if page.redacted_content_hash != str(expected_hash):
            raise ContentCatalogError("content_hash_mismatch")
        try:
            raw_text = target.read_text(encoding="utf-8", errors="ignore")
            frontmatter, raw_body = split_frontmatter(raw_text)
            policy = PrivacyPolicy()
            projected = policy.project(frontmatter)
            redacted_frontmatter = projected if isinstance(projected, dict) else {}
            redactions_applied = redacted_frontmatter != frontmatter or policy.redact_display_text(raw_body) != raw_body
        except (OSError, UnicodeError) as exc:
            raise ContentCatalogError("content_read_failed") from exc
        return page, redactions_applied


__all__ = [
    "CatalogItem",
    "ContentCatalogError",
    "ContentCatalogService",
    "DEFAULT_BODY_BUDGET",
    "DEFAULT_CATALOG_PAGE_SIZE",
    "MAX_BODY_BUDGET",
    "MAX_CATALOG_PAGE_SIZE",
]
