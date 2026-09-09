from __future__ import annotations

import pytest

from retrieval.metadata_filters import (
    QUERY_METADATA_FILTERS,
    metadata_filter_fingerprint,
    normalize_metadata_filters,
    page_matches_filters,
    path_matches_prefix,
)
from wiki.catalog_cursor import CatalogCursor


def test_filter_seams_keep_matching_and_bind_distinct_cursor_fingerprints() -> None:
    mcp_filters = normalize_metadata_filters(
        {"path_prefix": "wiki\\concepts/"},
        allowed=QUERY_METADATA_FILTERS,
        preserve_path_trailing=True,
    )
    catalog_filters = normalize_metadata_filters(
        {"path_prefix": "wiki\\concepts/"},
        preserve_path_trailing=False,
    )

    assert mcp_filters == {"path_prefix": "wiki/concepts/"}
    assert catalog_filters == {"path_prefix": "wiki/concepts"}
    for page_path, expected in (
        ("wiki/concepts/page.md", True),
        ("wiki/concept-extras/page.md", False),
    ):
        assert path_matches_prefix(page_path, str(mcp_filters["path_prefix"])) is expected
        assert path_matches_prefix(page_path, str(catalog_filters["path_prefix"])) is expected

    mcp_fingerprint = metadata_filter_fingerprint(mcp_filters)
    catalog_fingerprint = metadata_filter_fingerprint(catalog_filters)
    assert mcp_fingerprint != catalog_fingerprint

    for fingerprint in (mcp_fingerprint, catalog_fingerprint):
        cursor = CatalogCursor(
            scope="active",
            filter_fingerprint=fingerprint,
            snapshot_fingerprint="a" * 64,
            last_key=("wiki/concepts/page.md", "b" * 64),
            vault="fixture",
        )
        assert CatalogCursor.decode(cursor.encode()).filter_fingerprint == fingerprint


def test_filter_aliases_normalize_and_reject_ambiguous_values() -> None:
    assert normalize_metadata_filters({"pathPrefix": "wiki\\concepts"}) == {
        "path_prefix": "wiki/concepts"
    }
    assert normalize_metadata_filters(
        {"path_prefix": "wiki/concepts", "pathPrefix": "wiki/concepts"}
    ) == {"path_prefix": "wiki/concepts"}

    with pytest.raises(ValueError, match="path_prefix and filters.pathPrefix must match"):
        normalize_metadata_filters(
            {"path_prefix": "wiki/concepts", "pathPrefix": "wiki/projects"}
        )


def test_query_filter_whitelist_rejects_catalog_only_fields() -> None:
    with pytest.raises(ValueError, match="unsupported fields: project"):
        normalize_metadata_filters({"project": "demo"}, allowed=QUERY_METADATA_FILTERS)


@pytest.mark.parametrize("invalid_prefix", ["wiki//concepts", "wiki/./concepts", "wiki/../concepts"])
def test_path_prefix_rejects_non_vault_relative_segments(invalid_prefix: str) -> None:
    with pytest.raises(ValueError, match="must be vault-relative"):
        normalize_metadata_filters({"path_prefix": invalid_prefix})

    assert normalize_metadata_filters(
        {"path_prefix": "wiki\\concepts\\domain/"},
        preserve_path_trailing=True,
    ) == {"path_prefix": "wiki/concepts/domain/"}


def test_page_matches_filters_keeps_the_shared_metadata_semantics() -> None:
    assert page_matches_filters({"project": "Demo"}, "wiki", project="demo") is True
    assert page_matches_filters({}, "wiki", project="demo") is True
    assert page_matches_filters({"project": "Other"}, "wiki", project="demo") is False

    assert page_matches_filters({}, "entity", page_type="entity") is True
    assert page_matches_filters({}, "entity", page_type="concept") is False

    assert page_matches_filters({"tags": "alpha"}, "wiki", tags=("alpha",)) is False
    assert page_matches_filters({"tags": ["alpha", "beta"]}, "wiki", tags=("beta",)) is True
    assert page_matches_filters(
        {"tags": ["alpha", "beta"]}, "wiki", tags=("beta", "missing")
    ) is False


def test_metadata_filter_fingerprint_is_stable_for_tuple_and_list_values() -> None:
    tuple_filters = {"type": "concept", "tags": ("alpha", "beta")}
    list_filters = {"tags": ["alpha", "beta"], "type": "concept"}

    assert metadata_filter_fingerprint(tuple_filters) == metadata_filter_fingerprint(list_filters)
