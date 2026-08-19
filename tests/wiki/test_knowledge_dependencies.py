from __future__ import annotations

import pytest

from wiki.knowledge_dependencies import KnowledgeDependencies
from wiki.page_policy import PagePolicy


def test_change_propagation_freshness_and_archive_contract(tmp_path) -> None:
    deps = KnowledgeDependencies(tmp_path)
    deps.update_page(
        "wiki/concepts/x.md",
        "page",
        {"raw/sources/a.md": "one"},
        policy=PagePolicy(freshness="fresh", maintenance="auto", lifecycle="active", generated=True, replaced_by=None),
    )
    assert deps.source_changed("raw/sources/a.md", "one") == []
    assert deps.source_changed("raw/sources/a.md") == ["wiki/concepts/x.md"]
    assert deps.mark_fresh_if_sources("wiki/concepts/x.md", {"raw/sources/a.md": "one"})
    assert deps.lifecycle("wiki/concepts/x.md", state="superseded", replaced_by="wiki/concepts/y.md")["archive_ready"]
    assert deps.archive_ready("wiki/concepts/x.md")


def test_change_propagation_only_marks_edges_with_the_old_hash(tmp_path) -> None:
    deps = KnowledgeDependencies(tmp_path)
    policy = PagePolicy(freshness="fresh", maintenance="auto", lifecycle="active", generated=True, replaced_by=None)
    deps.update_page("wiki/concepts/one.md", "one", {"raw/sources/a.md": "old"}, policy=policy)
    deps.update_page("wiki/concepts/two.md", "two", {"raw/sources/a.md": "current"}, policy=policy)

    assert deps.source_changed("raw/sources/a.md", "current") == ["wiki/concepts/one.md"]
    assert deps.dependents("raw/sources/a.md") == ["wiki/concepts/one.md", "wiki/concepts/two.md"]


@pytest.mark.parametrize(
    ("policy", "message"),
    [
        (PagePolicy(freshness="fresh", maintenance="auto", lifecycle="unknown", generated=True, replaced_by=None), "invalid lifecycle"),
        (PagePolicy(freshness="fresh", maintenance="auto", lifecycle="superseded", generated=True, replaced_by=None), "superseded pages require replaced_by"),
        (PagePolicy(freshness="unknown", maintenance="auto", lifecycle="active", generated=True, replaced_by=None), "invalid freshness"),
    ],
)
def test_update_page_keeps_policy_validation_fail_closed(tmp_path, policy: PagePolicy, message: str) -> None:
    with pytest.raises(ValueError, match=message):
        KnowledgeDependencies(tmp_path).update_page("wiki/concepts/invalid.md", "page", {}, policy=policy)
