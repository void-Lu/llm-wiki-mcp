from __future__ import annotations

import hashlib

import pytest

from wiki.catalog_cursor import CatalogCursor, CatalogCursorError, ContentBodyCursor


def _hash(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def test_catalog_cursor_round_trips_keyset_bindings() -> None:
    cursor = CatalogCursor(
        vault="primary",
        scope="active",
        filter_fingerprint=_hash("filters"),
        snapshot_fingerprint=_hash("snapshot"),
        last_key=("wiki/concepts/a.md", _hash("page")),
    )

    assert CatalogCursor.decode(cursor.encode()) == cursor


def test_catalog_cursor_rejects_tampering_and_unsafe_last_key() -> None:
    with pytest.raises(CatalogCursorError) as error:
        CatalogCursor.decode("cc1_not-valid")
    assert error.value.code == "catalog_cursor_invalid"

    with pytest.raises(CatalogCursorError) as error:
        CatalogCursor(
            vault="primary",
            scope="active",
            filter_fingerprint=_hash("filters"),
            snapshot_fingerprint=_hash("snapshot"),
            last_key=("../escape.md", _hash("page")),
        )
    assert error.value.code == "catalog_cursor_invalid"


def test_body_cursor_is_hash_bound() -> None:
    cursor = ContentBodyCursor("cr1_reference", _hash("page"), 12)
    assert ContentBodyCursor.decode(cursor.encode()) == cursor
