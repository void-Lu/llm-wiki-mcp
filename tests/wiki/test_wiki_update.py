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
