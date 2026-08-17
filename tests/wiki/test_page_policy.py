from __future__ import annotations

import pytest

from wiki.page_policy import PagePolicy, PagePolicySourceFacts, derive, derive_page_policy, provenance_status, stamp


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


@pytest.mark.parametrize(
    ("frontmatter", "source_hashes", "expected"),
    [
        ({}, {}, {"freshness": "review_required", "provenance_unverified": True}),
        (
            {"generated": True, "maintenance": "manual", "replaced_by": "wiki/new.md", "freshness": "stale"},
            {"raw/sources/a.md": "hash"},
            {"freshness": "fresh", "provenance_unverified": False},
        ),
        (
            {"source_hashes": {"raw/sources/a.md": "hash"}, "freshness": "stale"},
            None,
            {"freshness": "stale", "provenance_unverified": False},
        ),
        (
            {"source_hashes": {"raw/sources/a.md": "hash"}, "freshness": "fresh"},
            {},
            {"freshness": "review_required", "provenance_unverified": True},
        ),
        (
            {"freshness": "fresh"},
            None,
            {"freshness": "review_required", "provenance_unverified": True},
        ),
    ],
)
def test_stamp_source_combinations(
    frontmatter: dict[str, object],
    source_hashes: dict[str, str] | None,
    expected: dict[str, object],
) -> None:
    stamped = stamp(frontmatter, source_hashes=source_hashes)

    assert stamped == expected
    assert provenance_status(stamped) == ("provenance_unverified" if expected["provenance_unverified"] else "verified")


def test_stamp_accepts_source_facts_and_does_not_own_writer_fields() -> None:
    facts = PagePolicySourceFacts(
        frontmatter={"generated": True, "maintenance": "manual", "replaced_by": "wiki/new.md"},
        source_hashes={"raw/sources/a.md": "hash"},
    )

    assert stamp(facts) == {"freshness": "fresh", "provenance_unverified": False}


@pytest.mark.parametrize("field", ["maintenance", "replaced_by"])
def test_stamp_validates_writer_owned_field_shapes(field: str) -> None:
    with pytest.raises(ValueError, match=field):
        stamp({field: ["invalid"]})


def test_derive_short_entry_matches_compatibility_entry() -> None:
    frontmatter = {"generated": True, "lifecycle": "stale", "freshness": "stale"}

    assert derive(frontmatter, {"raw/sources/a.md": "hash"}) == derive_page_policy(frontmatter, {"raw/sources/a.md": "hash"})
