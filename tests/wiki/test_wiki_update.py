from __future__ import annotations

from retrieval.retrieval_index import RetrievalIndexStore
from wiki.wiki_update import apply_update, preview_update


def test_preview_apply_cas_and_generated_becomes_manual(tmp_path) -> None:
    page = tmp_path / "wiki/concepts/general/a.md"
    page.parent.mkdir(parents=True)
    page.write_text("---\ntype: concept\nconcept_id: concept_a\ngenerated: true\nmaintenance: auto\nsources: [raw/sources/a.md]\ncreated: '2026-01-01'\n---\n\n# A\n\nold\n", encoding="utf-8")
    preview = preview_update(tmp_path, "wiki/concepts/general/a.md", "new", {"sources": ["raw/sources/a.md"]})
    assert preview["ok"] and preview["plan_id"]
    assert apply_update(tmp_path, "wiki/concepts/general/a.md", "new", incoming_frontmatter={"sources": ["raw/sources/a.md"]}, plan_id=preview["plan_id"])["ok"]
    assert "maintenance: manual" in page.read_text(encoding="utf-8")
    assert apply_update(tmp_path, "wiki/concepts/general/a.md", "bad", incoming_frontmatter={"concept_id": "other"})["code"] == "locked_field"


def test_apply_update_uses_redacted_writer_and_refreshes_existing_retrieval_index(tmp_path) -> None:
    page = tmp_path / "wiki/concepts/general/a.md"
    page.parent.mkdir(parents=True)
    page.write_text(
        "---\ntype: concept\ntitle: A\ngenerated: true\nsources: [raw/sources/a.md]\n---\n\n# A\n\nold\n",
        encoding="utf-8",
    )
    store = RetrievalIndexStore(tmp_path)
    store.build(store.iter_vault_pages())

    result = apply_update(
        tmp_path,
        "wiki/concepts/general/a.md",
        "central update sentinel token=abc1234567890",
        incoming_frontmatter={"summary": "token=abc1234567890"},
    )

    assert result["ok"] is True
    text = page.read_text(encoding="utf-8")
    assert "token=abc1234567890" not in text
    assert store.search_fts("central update sentinel")
    assert all("token=abc1234567890" not in str(candidate) for candidate in store.page_candidates())


def test_preview_and_apply_append_related_page_section(tmp_path) -> None:
    page = tmp_path / "wiki/concepts/general/a.md"
    page.parent.mkdir(parents=True)
    page.write_text("---\ntype: concept\ntitle: A\ngenerated: true\n---\n\n# A\n\nold\n", encoding="utf-8")
    related = tmp_path / "wiki/concepts/general/related.md"
    related.write_text("---\ntype: concept\n---\n\n# Related\n", encoding="utf-8")
    related_pages = [{"path": "wiki/concepts/general/related.md", "title": "Related"}]

    preview = preview_update(tmp_path, "wiki/concepts/general/a.md", "new", related_pages=related_pages)

    assert preview["ok"] is True
    assert "## 参考来源" in preview["diff"]
    result = apply_update(tmp_path, "wiki/concepts/general/a.md", "new", plan_id=preview["plan_id"], related_pages=related_pages)

    assert result["ok"] is True
    assert result["related_pages_skipped"] == []
    assert "[[wiki/concepts/general/related|Related]]" in page.read_text(encoding="utf-8")


def test_update_skips_invalid_related_pages_and_keeps_writing(tmp_path) -> None:
    page = tmp_path / "wiki/concepts/general/a.md"
    page.parent.mkdir(parents=True)
    page.write_text("---\ntype: concept\ntitle: A\n---\n\n# A\n\nold\n", encoding="utf-8")

    result = apply_update(
        tmp_path,
        "wiki/concepts/general/a.md",
        "new",
        related_pages=[
            {"path": "raw/sources/reference.txt", "title": "Raw"},
            {"path": "wiki/concepts/general/missing.md", "title": "Missing"},
        ],
    )

    assert result["ok"] is True
    assert [item["reason"] for item in result["related_pages_skipped"]] == [
        "raw_source_use_sources",
        "not_found",
    ]
    assert result["warnings"]
    assert "## 参考来源" not in page.read_text(encoding="utf-8")


def test_preview_reports_broken_wikilinks(tmp_path) -> None:
    """Preview should include broken_wikilinks for targets that don't match any file."""
    page = tmp_path / "wiki/concepts/general/a.md"
    page.parent.mkdir(parents=True)
    (tmp_path / "wiki/concepts/general/Target-Page.md").write_text("# Target", encoding="utf-8")
    page.write_text("---\ntype: concept\ntitle: A\n---\n\n# A\n\nold\n", encoding="utf-8")

    body = "See [[Target Page]] and [[Nonexistent]] for details."
    preview = preview_update(tmp_path, "wiki/concepts/general/a.md", body)

    assert preview["ok"] is True
    assert preview["normalized_wikilinks"] == 1  # "Target Page" -> "Target-Page"
    broken = preview["broken_wikilinks"]
    assert len(broken) == 1
    assert broken[0]["target"] == "Nonexistent"


def test_apply_auto_normalizes_wikilinks(tmp_path) -> None:
    """Apply should auto-normalize space-based wikilink targets to filename stems."""
    page = tmp_path / "wiki/concepts/general/a.md"
    page.parent.mkdir(parents=True)
    (tmp_path / "wiki/concepts/general/Target-Page.md").write_text("# Target", encoding="utf-8")
    page.write_text("---\ntype: concept\ntitle: A\n---\n\n# A\n\nold\n", encoding="utf-8")

    body = "See [[Target Page]] for details."
    preview = preview_update(tmp_path, "wiki/concepts/general/a.md", body)
    assert preview["normalized_wikilinks"] == 1

    result = apply_update(tmp_path, "wiki/concepts/general/a.md", body, plan_id=preview["plan_id"])
    assert result["ok"] is True
    assert result["normalized_wikilinks"] == 1
    assert "[[Target-Page]]" in page.read_text(encoding="utf-8")


def test_preview_and_apply_with_custom_heading(tmp_path) -> None:
    """related_pages_heading should override the default section title."""
    page = tmp_path / "wiki/concepts/general/a.md"
    page.parent.mkdir(parents=True)
    page.write_text("---\ntype: concept\ntitle: A\n---\n\n# A\n\nold\n", encoding="utf-8")
    related = tmp_path / "wiki/concepts/general/related.md"
    related.write_text("---\ntype: concept\n---\n\n# Related\n", encoding="utf-8")
    related_pages = [{"path": "wiki/concepts/general/related.md", "title": "Related"}]

    preview = preview_update(
        tmp_path, "wiki/concepts/general/a.md", "new",
        related_pages=related_pages, related_pages_heading="## 相关深度文档",
    )
    assert preview["ok"] is True
    assert "## 相关深度文档" in preview["diff"]
    assert "## 参考来源" not in preview["diff"]

    result = apply_update(
        tmp_path, "wiki/concepts/general/a.md", "new",
        plan_id=preview["plan_id"],
        related_pages=related_pages, related_pages_heading="## 相关深度文档",
    )
    assert result["ok"] is True
    assert "## 相关深度文档" in page.read_text(encoding="utf-8")
