from __future__ import annotations

import pytest

from wiki.content_reference import (
    ContentRefV1,
    ContentReferenceError,
    content_ref_for_query_hit,
)


def test_content_reference_round_trips_without_a_filesystem_path() -> None:
    reference = ContentRefV1("primary", "active", "page", "wiki/concepts/utf8.md")
    encoded = reference.encode()

    assert encoded.startswith("cr1_")
    assert ContentRefV1.decode(encoded) == reference
    assert "C:" not in encoded
    assert "/Users/" not in encoded


@pytest.mark.parametrize(
    "reference",
    [
        "C:/vault/wiki/page.md",
        "cr1_eyJpZGVudGl0eSI6IkM6L3ZhubHQifQ",
        "",
        "not-a-reference",
    ],
)
def test_content_reference_rejects_unsafe_or_malformed_values(reference: str) -> None:
    with pytest.raises(ContentReferenceError) as error:
        ContentRefV1.decode(reference)
    assert error.value.code == "invalid_content_ref"


def test_content_reference_rejects_absolute_identity() -> None:
    with pytest.raises(ContentReferenceError) as error:
        ContentRefV1("primary", "active", "page", "C:/vault/wiki/page.md")
    assert error.value.code == "invalid_content_ref"


@pytest.mark.parametrize(
    ("path", "source_kind", "corpus", "scope"),
    [
        ("wiki/concepts/page.md", "knowledge", "active", "active"),
        ("history/2026/page.md", "history", "active", "active"),
        ("raw/sources/references/page.md", "raw", "raw", "raw"),
        ("archives/bundles/a/b/archive/wiki/page.md", "archive", "archive", "archive"),
    ],
)
def test_query_hit_reference_uses_catalog_scope_mapping(
    path: str,
    source_kind: str,
    corpus: str,
    scope: str,
) -> None:
    encoded = content_ref_for_query_hit(
        "primary",
        path=path,
        source_kind=source_kind,
        corpus=corpus,
    )

    assert encoded is not None
    reference = ContentRefV1.decode(encoded)
    assert reference.scope == scope
    assert reference.identity == path


def test_query_hit_reference_omits_unsafe_paths_instead_of_guessing() -> None:
    assert content_ref_for_query_hit("primary", path="C:/vault/wiki/page.md") is None
