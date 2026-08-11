from __future__ import annotations

from wiki.knowledge_dependencies import KnowledgeDependencies


def test_change_propagation_freshness_and_archive_contract(tmp_path) -> None:
    deps = KnowledgeDependencies(tmp_path)
    deps.update_page("wiki/concepts/x.md", "page", {"raw/sources/a.md": "one"}, generated=True)
    assert deps.source_changed("raw/sources/a.md", "one") == []
    assert deps.source_changed("raw/sources/a.md") == ["wiki/concepts/x.md"]
    assert deps.mark_fresh_if_sources("wiki/concepts/x.md", {"raw/sources/a.md": "one"})
    assert deps.lifecycle("wiki/concepts/x.md", state="superseded", replaced_by="wiki/concepts/y.md")["archive_ready"]
    assert deps.archive_ready("wiki/concepts/x.md")


def test_change_propagation_only_marks_edges_with_the_old_hash(tmp_path) -> None:
    deps = KnowledgeDependencies(tmp_path)
    deps.update_page("wiki/concepts/one.md", "one", {"raw/sources/a.md": "old"}, generated=True)
    deps.update_page("wiki/concepts/two.md", "two", {"raw/sources/a.md": "current"}, generated=True)

    assert deps.source_changed("raw/sources/a.md", "current") == ["wiki/concepts/one.md"]
    assert deps.dependents("raw/sources/a.md") == ["wiki/concepts/one.md", "wiki/concepts/two.md"]
