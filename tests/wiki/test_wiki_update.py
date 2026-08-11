from __future__ import annotations

from hashlib import sha256

from retrieval.retrieval_index import RetrievalIndexStore
from wiki.page_mutation import PageMutationCoordinator
from wiki.update_plan_store import UpdatePlanStore
from wiki.wiki_update import apply_update, preview_update


def test_preview_apply_cas_and_generated_becomes_manual(tmp_path) -> None:
    page = tmp_path / "wiki/concepts/general/a.md"
    page.parent.mkdir(parents=True)
    source = tmp_path / "raw/sources/a.md"
    source.parent.mkdir(parents=True)
    source.write_text("raw evidence", encoding="utf-8")
    page.write_text("---\ntype: concept\nconcept_id: concept_a\ngenerated: true\nmaintenance: auto\nsources: [raw/sources/a.md]\ncreated: '2026-01-01'\n---\n\n# A\n\nold\n", encoding="utf-8")
    preview = preview_update(tmp_path, "wiki/concepts/general/a.md", "new", {"sources": ["raw/sources/a.md"]})
    assert preview["ok"] and preview["plan_id"]
    assert apply_update(tmp_path, "wiki/concepts/general/a.md", "new", incoming_frontmatter={"sources": ["raw/sources/a.md"]}, plan_id=preview["plan_id"], expected_hash=preview["current_hash"])["ok"]
    assert "maintenance: manual" in page.read_text(encoding="utf-8")
    assert "source_hashes:" in page.read_text(encoding="utf-8")
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

    preview = preview_update(
        tmp_path,
        "wiki/concepts/general/a.md",
        "central update sentinel token=abc1234567890",
        incoming_frontmatter={"summary": "token=abc1234567890"},
    )
    result = apply_update(
        tmp_path,
        "wiki/concepts/general/a.md",
        "central update sentinel token=abc1234567890",
        incoming_frontmatter={"summary": "token=abc1234567890"},
        plan_id=preview["plan_id"],
        expected_hash=preview["current_hash"],
    )

    assert result["ok"] is True
    text = page.read_text(encoding="utf-8")
    assert "token=abc1234567890" not in text
    assert store.search_fts("central update sentinel")
    assert all("token=abc1234567890" not in str(candidate) for candidate in store.page_candidates())


def test_legacy_page_body_update_remains_unverified(tmp_path) -> None:
    page = tmp_path / "wiki/concepts/general/legacy.md"
    page.parent.mkdir(parents=True)
    page.write_text("---\ntype: concept\ntitle: Legacy\n---\n\n# Legacy\n\nold\n", encoding="utf-8")

    result = apply_update(tmp_path, "wiki/concepts/general/legacy.md", "new", expected_hash=sha256(page.read_bytes()).hexdigest())

    assert result["ok"] is True
    assert result["provenance_status"] == "provenance_unverified"
    assert result["freshness"] == "review_required"
    text = page.read_text(encoding="utf-8")
    assert "provenance_unverified: true" in text
    assert "freshness: review_required" in text


def test_update_gates_require_expected_hash_and_plan_for_structure(tmp_path) -> None:
    page = tmp_path / "wiki/concepts/general/a.md"
    page.parent.mkdir(parents=True)
    page.write_text("---\ntype: concept\ntitle: A\n---\n\n# A\n\nold\n", encoding="utf-8")
    current_hash = sha256(page.read_bytes()).hexdigest()

    assert apply_update(tmp_path, "wiki/concepts/general/a.md", "new")["code"] == "expected_hash_required"
    assert apply_update(tmp_path, "wiki/concepts/general/a.md", "new", expected_hash=current_hash, incoming_frontmatter={"summary": "new"})["code"] == "update_plan_required"
    assert apply_update(tmp_path, "wiki/concepts/general/a.md", "new", expected_hash=current_hash, plan_id="unknown") ["code"] == "plan_unknown"


def test_plan_is_consumed_before_projection_failure_and_replay_is_already_applied(tmp_path, monkeypatch) -> None:
    page = tmp_path / "wiki/concepts/general/a.md"
    page.parent.mkdir(parents=True)
    page.write_text("---\ntype: concept\ntitle: A\n---\n\n# A\n\nold\n", encoding="utf-8")
    preview = preview_update(tmp_path, "wiki/concepts/general/a.md", "new")

    def failing_projections(self, operation):
        return {"dependencies": lambda: {"ok": False, "code": "dependency_unavailable"}}

    monkeypatch.setattr(PageMutationCoordinator, "projections_for", failing_projections)
    result = apply_update(tmp_path, "wiki/concepts/general/a.md", "new", plan_id=preview["plan_id"], expected_hash=preview["current_hash"])
    assert result["ok"] is True
    assert result["state"] == "repair_pending"
    consumed_plan = UpdatePlanStore(tmp_path).get(preview["plan_id"])
    assert consumed_plan is not None
    assert consumed_plan.state == "consumed"
    committed = page.read_bytes()

    replay = apply_update(tmp_path, "wiki/concepts/general/a.md", "new", plan_id=preview["plan_id"], expected_hash=preview["current_hash"])
    assert replay["ok"] is True and replay["state"] == "already_applied"
    assert page.read_bytes() == committed


def test_preview_and_apply_reject_invalid_sources_before_writes(tmp_path) -> None:
    page = tmp_path / "wiki/concepts/general/a.md"
    page.parent.mkdir(parents=True)
    original = "---\ntype: concept\ntitle: A\n---\n\n# A\n\nold\n"
    page.write_text(original, encoding="utf-8")

    preview = preview_update(tmp_path, "wiki/concepts/general/a.md", "new", {"sources": ["wiki/other.md"]})
    result = apply_update(tmp_path, "wiki/concepts/general/a.md", "new", incoming_frontmatter={"sources": ["wiki/other.md"]})

    assert preview["code"] == result["code"] == "source_path_not_allowed"
    assert page.read_text(encoding="utf-8") == original
    assert not (tmp_path / ".llm-wiki").exists()


def test_source_hashes_are_server_owned(tmp_path) -> None:
    page = tmp_path / "wiki/concepts/general/a.md"
    page.parent.mkdir(parents=True)
    page.write_text("---\ntype: concept\ntitle: A\n---\n\n# A\n\nold\n", encoding="utf-8")

    result = apply_update(
        tmp_path,
        "wiki/concepts/general/a.md",
        "new",
        incoming_frontmatter={"source_hashes": {"raw/sources/a.md": "fake"}},
    )

    assert result["ok"] is False
    assert result["code"] == "source_hashes_server_owned"


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
    result = apply_update(tmp_path, "wiki/concepts/general/a.md", "new", plan_id=preview["plan_id"], expected_hash=preview["current_hash"], related_pages=related_pages)

    assert result["ok"] is True
    assert result["related_pages_skipped"] == []
    assert "[[wiki/concepts/general/related|Related]]" in page.read_text(encoding="utf-8")


def test_update_skips_invalid_related_pages_and_keeps_writing(tmp_path) -> None:
    page = tmp_path / "wiki/concepts/general/a.md"
    page.parent.mkdir(parents=True)
    page.write_text("---\ntype: concept\ntitle: A\n---\n\n# A\n\nold\n", encoding="utf-8")

    preview = preview_update(
        tmp_path,
        "wiki/concepts/general/a.md",
        "new",
        related_pages=[
            {"path": "raw/sources/reference.txt", "title": "Raw"},
            {"path": "wiki/concepts/general/missing.md", "title": "Missing"},
        ],
    )
    result = apply_update(
        tmp_path,
        "wiki/concepts/general/a.md",
        "new",
        related_pages=[
            {"path": "raw/sources/reference.txt", "title": "Raw"},
            {"path": "wiki/concepts/general/missing.md", "title": "Missing"},
        ],
        plan_id=preview["plan_id"],
        expected_hash=preview["current_hash"],
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

    result = apply_update(tmp_path, "wiki/concepts/general/a.md", body, plan_id=preview["plan_id"], expected_hash=preview["current_hash"])
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
        expected_hash=preview["current_hash"],
        related_pages=related_pages, related_pages_heading="## 相关深度文档",
    )
    assert result["ok"] is True
    assert "## 相关深度文档" in page.read_text(encoding="utf-8")
