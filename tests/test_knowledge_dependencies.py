from __future__ import annotations

from netsuite_llm_wiki_mcp.knowledge_dependencies import KnowledgeDependencies


def test_change_propagation_freshness_and_archive_contract(tmp_path) -> None:
    deps = KnowledgeDependencies(tmp_path)
    deps.update_page("wiki/concepts/x.md", "page", {"raw/sources/a.md": "one"}, generated=True)
    assert deps.source_changed("raw/sources/a.md") == ["wiki/concepts/x.md"]
    assert deps.mark_fresh_if_sources("wiki/concepts/x.md", {"raw/sources/a.md": "one"})
    assert deps.lifecycle("wiki/concepts/x.md", state="superseded", replaced_by="wiki/concepts/y.md")["archive_ready"]
    assert deps.archive_ready("wiki/concepts/x.md")
