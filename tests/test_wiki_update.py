from __future__ import annotations

from netsuite_llm_wiki_mcp.wiki_update import apply_update, preview_update


def test_preview_apply_cas_and_generated_becomes_manual(tmp_path) -> None:
    page = tmp_path / "wiki/concepts/general/a.md"
    page.parent.mkdir(parents=True)
    page.write_text("---\ntype: concept\nconcept_id: concept_a\ngenerated: true\nmaintenance: auto\nsources: [raw/sources/a.md]\ncreated: '2026-01-01'\n---\n\n# A\n\nold\n", encoding="utf-8")
    preview = preview_update(tmp_path, "wiki/concepts/general/a.md", "new", {"sources": ["raw/sources/a.md"]})
    assert preview["ok"] and preview["plan_id"]
    assert apply_update(tmp_path, "wiki/concepts/general/a.md", "new", incoming_frontmatter={"sources": ["raw/sources/a.md"]}, plan_id=preview["plan_id"])["ok"]
    assert "maintenance: manual" in page.read_text(encoding="utf-8")
    assert apply_update(tmp_path, "wiki/concepts/general/a.md", "bad", incoming_frontmatter={"concept_id": "other"})["code"] == "locked_field"
