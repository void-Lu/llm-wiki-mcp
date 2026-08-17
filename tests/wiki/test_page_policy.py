from __future__ import annotations

import pytest

from wiki.page_policy import PagePolicy, derive_page_policy


@pytest.mark.parametrize(
    ("frontmatter", "source_hashes", "expected"),
    [
        (
            {},
            None,
            PagePolicy(freshness="fresh", maintenance="manual", lifecycle="active", generated=False, replaced_by=None),
        ),
        (
            {"generated": True},
            None,
            PagePolicy(freshness="fresh", maintenance="auto", lifecycle="active", generated=True, replaced_by=None),
        ),
        (
            {"generated": True, "maintenance": "manual", "lifecycle": "stale", "freshness": "stale", "replaced_by": "wiki/new.md"},
            None,
            PagePolicy(freshness="stale", maintenance="manual", lifecycle="stale", generated=True, replaced_by="wiki/new.md"),
        ),
        (
            {"maintenance": "custom"},
            None,
            PagePolicy(freshness="fresh", maintenance="custom", lifecycle="active", generated=False, replaced_by=None),
        ),
        (
            {"lifecycle": "not-a-lifecycle", "freshness": "not-freshness"},
            {"raw/sources/a.md": "hash"},
            PagePolicy(freshness="review_required", maintenance="manual", lifecycle="review_required", generated=False, replaced_by=None),
        ),
        (
            {},
            {"raw/sources/a.md": "hash"},
            PagePolicy(freshness="fresh", maintenance="manual", lifecycle="active", generated=False, replaced_by=None),
        ),
        (
            {},
            {},
            PagePolicy(freshness="review_required", maintenance="manual", lifecycle="active", generated=False, replaced_by=None),
        ),
        (
            {"freshness": "review_required"},
            {},
            PagePolicy(freshness="review_required", maintenance="manual", lifecycle="active", generated=False, replaced_by=None),
        ),
        (
            {"replaced_by": ""},
            None,
            PagePolicy(freshness="fresh", maintenance="manual", lifecycle="active", generated=False, replaced_by=None),
        ),
    ],
)
def test_derive_page_policy_table(
    frontmatter: dict[str, object],
    source_hashes: dict[str, str] | None,
    expected: PagePolicy,
) -> None:
    assert derive_page_policy(frontmatter, source_hashes) == expected
