from __future__ import annotations

from pathlib import Path
from typing import cast

import pytest

from archive.archive_manifest import content_hash, write_manifest
from archive.archive_models import ArchiveItem, ArchiveManifest
from retrieval.metadata_filters import (
    metadata_filter_fingerprint,
    normalize_metadata_filters,
    path_matches_prefix,
)
from retrieval.retrieval_index import RetrievalIndexStore
from wiki.catalog_cursor import ContentBodyCursor
from wiki.content_catalog import MAX_BODY_BUDGET, ContentCatalogError, ContentCatalogService
from wiki.content_reference import ContentRefV1


def _build_active(root: Path) -> None:
    page = root / "wiki" / "concepts" / "a-utf8.md"
    page.parent.mkdir(parents=True)
    page.write_text(
        "---\ntype: concept\ntitle: UTF8\ntags: [alpha]\nsummary: catalog page\n---\n\n# UTF8\n\n前缀 🔐 以及一段正文。\n",
        encoding="utf-8",
    )
    (page.parent / "b-second.md").write_text(
        "---\ntype: concept\ntitle: Second\n---\n\n# Second\n\nsecond page\n",
        encoding="utf-8",
    )
    RetrievalIndexStore(root).build(RetrievalIndexStore(root).iter_vault_pages())


def test_list_is_metadata_only_and_paginates_stably(tmp_path: Path) -> None:
    _build_active(tmp_path)
    service = ContentCatalogService(tmp_path, logical_vault="primary")

    first = service.list_items(page_size=1)
    assert first["ok"] is True
    first_items = first["items"]
    assert isinstance(first_items, list)
    assert len(first_items) == 1
    assert first["next_cursor"]
    first_item = cast(dict[str, object], first_items[0])
    assert "body" not in first_item
    assert "passages" not in first_items[0]
    assert Path(tmp_path / ".llm-wiki" / "retrieval.sqlite3").exists()

    second = service.list_items(page_size=1, cursor=str(first["next_cursor"]))
    assert second["ok"] is True
    second_items = cast(list[dict[str, object]], second["items"])
    assert len(second_items) == 1
    assert second_items[0]["identity"] != first_item["identity"]
    assert second["next_cursor"] is None


def test_get_defaults_to_metadata_and_body_is_utf8_safe(tmp_path: Path) -> None:
    _build_active(tmp_path)
    service = ContentCatalogService(tmp_path, logical_vault="primary")
    item = cast(list[dict[str, object]], service.list_items()["items"])[0]
    reference = str(item["content_ref"])

    metadata = service.get_item(reference)
    assert metadata["ok"] is True
    assert "body" not in metadata
    assert metadata["content_ref"] == reference

    body = service.get_item(reference, include_body=True, max_bytes=17)
    body_text = cast(str, body["body"])
    assert body_text.encode("utf-8").decode("utf-8") == body_text
    assert len(str(body["body"]).encode("utf-8")) <= 17
    assert body["round_trip_safe"] is True
    assert body["truncated"] is True

    next_cursor = body["next_body_cursor"]
    assert next_cursor
    decoded = ContentBodyCursor.decode(str(next_cursor))
    assert decoded.content_ref == reference
    full_body = str(service.get_item(reference, include_body=True, max_bytes=1000)["body"])
    emoji_offset = len(full_body[: full_body.index("🔐")].encode("utf-8"))
    emoji_cursor = ContentBodyCursor(reference, str(item["content_hash"]), emoji_offset).encode()
    with pytest.raises(ContentCatalogError) as error:
        service.get_item(reference, include_body=True, max_bytes=1, cursor=emoji_cursor)
    assert error.value.code == "content_budget_too_small"


def test_domain_body_budget_remains_hard_limited(tmp_path: Path) -> None:
    _build_active(tmp_path)
    service = ContentCatalogService(tmp_path, logical_vault="primary")
    item = cast(list[dict[str, object]], service.list_items()["items"])[0]

    with pytest.raises(ContentCatalogError) as error:
        service.get_item(str(item["content_ref"]), include_body=True, max_bytes=MAX_BODY_BUDGET + 1)

    assert error.value.code == "content_budget_exceeded"


def test_filters_page_size_and_snapshot_cursor_are_bound(tmp_path: Path) -> None:
    _build_active(tmp_path)
    service = ContentCatalogService(tmp_path, logical_vault="primary")
    sibling = tmp_path / "wiki" / "concepts2" / "sibling.md"
    sibling.parent.mkdir(parents=True)
    sibling.write_text("---\ntype: concept\n---\n\n# Sibling\n", encoding="utf-8")
    store = RetrievalIndexStore(tmp_path)
    store.build(store.iter_vault_pages())
    filtered = service.list_items(filters={"type": "concept", "tags": ["alpha"], "path_prefix": "wiki\\concepts"})
    filtered_items = cast(list[dict[str, object]], filtered["items"])
    assert [item["identity"] for item in filtered_items] == ["wiki/concepts/a-utf8.md"]
    with pytest.raises(ContentCatalogError) as error:
        service.list_items(page_size=101)
    assert error.value.code == "catalog_page_size_invalid"

    first = service.list_items(page_size=1)
    cursor = str(first["next_cursor"])
    with pytest.raises(ContentCatalogError) as error:
        service.list_items(page_size=1, cursor=cursor, filters={"type": "entity"})
    assert error.value.code == "catalog_cursor_invalid"

    changed = tmp_path / "wiki" / "concepts" / "b-second.md"
    changed.write_text(changed.read_text(encoding="utf-8") + "changed\n", encoding="utf-8")
    store.build(store.iter_vault_pages())
    with pytest.raises(ContentCatalogError) as error:
        service.list_items(page_size=1, cursor=cursor)
    assert error.value.code == "catalog_cursor_stale"


def test_boundary_trailing_slash_changes_filter_fingerprint_but_not_matching() -> None:
    with_trailing = normalize_metadata_filters(
        {"path_prefix": "wiki/concepts/"},
        preserve_path_trailing=True,
    )
    without_trailing = normalize_metadata_filters(
        {"path_prefix": "wiki/concepts"},
        preserve_path_trailing=True,
    )

    assert metadata_filter_fingerprint(with_trailing) != metadata_filter_fingerprint(without_trailing)
    assert path_matches_prefix("wiki/concepts/page.md", str(with_trailing["path_prefix"]))
    assert path_matches_prefix("wiki/concepts/page.md", str(without_trailing["path_prefix"]))


def test_body_cursor_and_index_hash_detect_changes(tmp_path: Path) -> None:
    _build_active(tmp_path)
    service = ContentCatalogService(tmp_path, logical_vault="primary")
    item = cast(list[dict[str, object]], service.list_items()["items"])[0]
    reference = str(item["content_ref"])
    body = service.get_item(reference, include_body=True, max_bytes=4)
    page = tmp_path / "wiki" / "concepts" / "a-utf8.md"
    page.write_text(page.read_text(encoding="utf-8") + "changed", encoding="utf-8")

    with pytest.raises(ContentCatalogError) as error:
        service.get_item(reference, include_body=True, cursor=str(body["next_body_cursor"]))
    assert error.value.code == "content_hash_mismatch"


def test_missing_index_does_not_build_or_create_storage(tmp_path: Path) -> None:
    before = sorted(path.relative_to(tmp_path).as_posix() for path in tmp_path.rglob("*"))
    service = ContentCatalogService(tmp_path, logical_vault="primary")

    with pytest.raises(ContentCatalogError) as error:
        service.list_items()
    assert error.value.code == "index_missing"
    after = sorted(path.relative_to(tmp_path).as_posix() for path in tmp_path.rglob("*"))
    assert after == before


def test_empty_cursor_is_not_treated_as_first_page(tmp_path: Path) -> None:
    _build_active(tmp_path)
    service = ContentCatalogService(tmp_path, logical_vault="primary")
    with pytest.raises(ContentCatalogError) as error:
        service.list_items(cursor="")
    assert error.value.code == "catalog_cursor_invalid"
    reference = str(cast(list[dict[str, object]], service.list_items()["items"])[0]["content_ref"])
    with pytest.raises(ContentCatalogError) as error:
        service.get_item(reference, include_body=True, cursor="")
    assert error.value.code == "catalog_cursor_invalid"


def test_content_reference_scope_and_asset_body_are_enforced(tmp_path: Path) -> None:
    _build_active(tmp_path)
    service = ContentCatalogService(tmp_path, logical_vault="primary")
    forged = ContentRefV1("primary", "raw", "page", "wiki/concepts/utf8.md").encode()
    with pytest.raises(ContentCatalogError) as error:
        service.get_item(forged)
    assert error.value.code == "content_ref_scope_mismatch"


def test_raw_binary_asset_is_metadata_only(tmp_path: Path) -> None:
    asset = tmp_path / "raw" / "sources" / "file" / "demo" / "assets" / "diagram.bin"
    asset.parent.mkdir(parents=True)
    asset.write_bytes(b"\x00\x01\xff")
    store = RetrievalIndexStore(tmp_path, scope="raw")
    store.build(store.iter_vault_pages())
    service = ContentCatalogService(tmp_path, logical_vault="primary")

    listed = service.list_items(scope="raw")
    listed_items = cast(list[dict[str, object]], listed["items"])
    assert listed_items[0]["kind"] == "asset"
    assert listed_items[0]["body_available"] is False
    with pytest.raises(ContentCatalogError) as error:
        service.get_item(str(listed_items[0]["content_ref"]), include_body=True)
    assert error.value.code == "content_binary_body_unsupported"


def test_raw_text_source_can_be_read_as_a_page(tmp_path: Path) -> None:
    source = tmp_path / "raw" / "sources" / "file" / "demo" / "source.txt"
    source.parent.mkdir(parents=True)
    source.write_text("raw text source", encoding="utf-8")
    store = RetrievalIndexStore(tmp_path, scope="raw")
    store.build(store.iter_vault_pages())
    service = ContentCatalogService(tmp_path, logical_vault="primary")
    item = cast(list[dict[str, object]], service.list_items(scope="raw")["items"])[0]
    assert item["kind"] == "page"
    assert service.get_item(str(item["content_ref"]), include_body=True)["body"] == "raw text source"


def test_redacted_body_is_not_claimed_round_trip_safe(tmp_path: Path) -> None:
    page = tmp_path / "wiki" / "concepts" / "secret.md"
    page.parent.mkdir(parents=True)
    page.write_text(
        "---\ntype: concept\ntitle: Secret\n---\n\n# Secret\n\ncontact user@example.com and token sk-test-1234567890abcdef\n",
        encoding="utf-8",
    )
    store = RetrievalIndexStore(tmp_path)
    store.build(store.iter_vault_pages())
    service = ContentCatalogService(tmp_path, logical_vault="primary")
    reference = str(cast(list[dict[str, object]], service.list_items()["items"])[0]["content_ref"])
    result = service.get_item(reference, include_body=True)
    assert result["redacted"] is True
    assert result["round_trip_safe"] is False
    assert "user@example.com" not in cast(str, result["body"])


def test_archive_catalog_requires_manifest_and_payload_hash(tmp_path: Path) -> None:
    bundle = tmp_path / "archives" / "bundles" / "aa11" / "bb22" / "archive-001"
    payload = bundle / "wiki" / "concepts" / "archived.md"
    payload.parent.mkdir(parents=True)
    payload.write_text("---\ntype: concept\ntitle: Archived\n---\n\n# Archived\n\nold\n", encoding="utf-8")
    write_manifest(
        bundle,
        ArchiveManifest(
            archive_id="archive-001",
            operation_id="operation-001",
            reason="manual",
            archived_at="2026-08-10T00:00:00Z",
            items=(ArchiveItem("wiki/concepts/archived.md", "wiki/concepts/archived.md", content_hash(payload), "knowledge"),),
        ),
    )
    archive_store = RetrievalIndexStore(tmp_path, scope="archive")
    archive_store.build(archive_store.iter_vault_pages())
    service = ContentCatalogService(tmp_path, logical_vault="primary")

    listed = service.list_items(scope="archive")
    listed_items = cast(list[dict[str, object]], listed["items"])
    reference = str(listed_items[0]["content_ref"])
    body = service.get_item(reference, include_body=True)
    assert cast(str, body["body"]).endswith("old")

    payload.write_text(payload.read_text(encoding="utf-8") + "tampered\n", encoding="utf-8")
    service.list_items(scope="archive")
    with pytest.raises(ContentCatalogError) as error:
        service.get_item(reference, include_body=True)
    assert error.value.code == "archive_hash_mismatch"
