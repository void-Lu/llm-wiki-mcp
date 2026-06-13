"""Tests for wiki_gap module."""

from pathlib import Path

import pytest

from netsuite_llm_wiki_mcp.wiki_gap import wiki_gap


@pytest.fixture
def wiki_vault(tmp_path):
    """Create a minimal wiki vault for testing."""
    # Structure
    (tmp_path / "purpose.md").write_text("purpose", encoding="utf-8")
    (tmp_path / "schema.md").write_text("schema", encoding="utf-8")
    (tmp_path / "raw" / "sources" / "file" / "myproj" / "src-a").mkdir(parents=True)
    (tmp_path / "raw" / "sources" / "file" / "myproj" / "src-a" / "src-a.md").write_text("raw content", encoding="utf-8")
    (tmp_path / "raw" / "sources" / "file" / "myproj" / "src-b").mkdir(parents=True)
    (tmp_path / "raw" / "sources" / "file" / "myproj" / "src-b" / "src-b.md").write_text("raw content b", encoding="utf-8")
    (tmp_path / "raw" / "assets").mkdir(parents=True, exist_ok=True)
    (tmp_path / "wiki" / "projects" / "myproj" / "code").mkdir(parents=True)
    (tmp_path / "wiki" / "projects" / "myproj" / "decisions").mkdir(parents=True)
    (tmp_path / "wiki" / "projects" / "myproj" / "troubleshooting").mkdir(parents=True)
    (tmp_path / "wiki" / "projects" / "myproj" / "requirements").mkdir(parents=True)
    (tmp_path / "wiki" / "concepts" / "domain-a").mkdir(parents=True)
    (tmp_path / "wiki" / "sources").mkdir(parents=True)
    (tmp_path / "wiki" / "queries").mkdir(parents=True)
    (tmp_path / "wiki" / "maintenance").mkdir(parents=True)
    (tmp_path / "wiki" / "comparisons").mkdir(parents=True)
    (tmp_path / ".llm-wiki" / "ingest-cache" / "file" / "myproj").mkdir(parents=True)
    (tmp_path / ".obsidian").mkdir(parents=True)

    # Index pages
    (tmp_path / "wiki" / "index.md").write_text("---\ntype: index\ngenerated: true\n---\n# Index\n", encoding="utf-8")
    (tmp_path / "wiki" / "log.md").write_text("# Log\n", encoding="utf-8")
    (tmp_path / "wiki" / "overview.md").write_text("---\ntype: overview\ngenerated: true\n---\n# Overview\n", encoding="utf-8")
    (tmp_path / "wiki" / "projects" / "myproj" / "index.md").write_text(
        "---\ntype: index\ngenerated: true\n---\n# myproj\n", encoding="utf-8"
    )

    # A well-written concept page
    (tmp_path / "wiki" / "concepts" / "domain-a" / "concept-one.md").write_text(
        "---\ntype: concept\ntags: [netsuite]\n---\n# Concept One\n\n"
        + "This is a well-written concept page with enough content to pass the shallow check. "
        * 10
        + "\n\nSee also [[concept-two]] and [[missing-concept]].\n",
        encoding="utf-8",
    )

    # A shallow concept page
    (tmp_path / "wiki" / "concepts" / "domain-a" / "concept-two.md").write_text(
        "---\ntype: concept\ntags: [netsuite]\n---\n# Concept Two\n\nShort.\n",
        encoding="utf-8",
    )

    # A project code page that links to concept-one
    (tmp_path / "wiki" / "projects" / "myproj" / "code" / "module-x.md").write_text(
        "---\ntype: code\ngenerated: true\n---\n# Module X\n\n"
        + "This module implements [[concept-one]] logic. "
        * 8
        + "\n",
        encoding="utf-8",
    )

    # Source index page for src-a (marks it as ingested)
    (tmp_path / "wiki" / "sources" / "src-a.md").write_text(
        "---\ntype: source\ngenerated: true\n---\n# src-a\n\nSource summary.\n",
        encoding="utf-8",
    )

    # Cache for src-a
    import json
    (tmp_path / ".llm-wiki" / "ingest-cache" / "file" / "myproj" / "src-a.json").write_text(
        json.dumps({"source_name": "src-a"}), encoding="utf-8"
    )

    return tmp_path


class TestAnalyzeStage:
    def test_basic_analysis(self, wiki_vault):
        result = wiki_gap(str(wiki_vault), stage="analyze")
        assert result["ok"] is True
        assert result["stage"] == "analyze"
        assert "summary" in result
        assert result["summary"]["total_pages"] > 0

    def test_finds_shallow_pages(self, wiki_vault):
        result = wiki_gap(str(wiki_vault), stage="analyze")
        shallow = result["shallow_pages"]
        shallow_titles = [p["title"] for p in shallow]
        assert "concept-two" in shallow_titles

    def test_finds_dangling_links(self, wiki_vault):
        result = wiki_gap(str(wiki_vault), stage="analyze")
        dangling = result["dangling_links"]
        dangling_names = [d["missing_page"] for d in dangling]
        assert "missing-concept" in dangling_names

    def test_finds_uningested_sources(self, wiki_vault):
        result = wiki_gap(str(wiki_vault), stage="analyze")
        uningested = result["uningested_sources"]
        uningested_names = [s["source_name"] for s in uningested]
        # src-b has raw source but no wiki/sources page and no cache
        assert "src-b" in uningested_names
        # src-a is ingested (has wiki/sources page + cache)
        assert "src-a" not in uningested_names

    def test_taxonomy_check(self, wiki_vault):
        taxonomy = ["concept-one", "concept-two", "concept-three", "concept-four"]
        result = wiki_gap(str(wiki_vault), stage="analyze", taxonomy=taxonomy)
        missing = result["missing_from_taxonomy"]
        # concept-one and concept-two exist
        assert "concept-one" not in missing
        assert "concept-two" not in missing
        # concept-three and concept-four don't exist
        assert "concept-three" in missing
        assert "concept-four" in missing

    def test_project_filter(self, wiki_vault):
        result = wiki_gap(str(wiki_vault), stage="analyze", project="myproj")
        assert result["ok"] is True
        # Should still find uningested sources for myproj
        uningested = result["uningested_sources"]
        for s in uningested:
            assert s["project"] == "myproj"

    def test_orphan_detection(self, wiki_vault):
        result = wiki_gap(str(wiki_vault), stage="analyze")
        orphan_titles = [o["title"] for o in result["orphan_pages"]]
        # module-x is not linked to by anyone
        assert "module-x" in orphan_titles


class TestSuggestStage:
    def test_basic_suggestions(self, wiki_vault):
        result = wiki_gap(str(wiki_vault), stage="suggest")
        assert result["ok"] is True
        assert result["stage"] == "suggest"
        assert result["total_suggestions"] > 0
        assert len(result["suggestions"]) > 0

    def test_suggests_ingest_for_uningested(self, wiki_vault):
        result = wiki_gap(str(wiki_vault), stage="suggest")
        ingest_suggestions = [
            s for s in result["suggestions"] if s["action"] == "wiki_ingest_llm"
        ]
        assert len(ingest_suggestions) > 0
        assert ingest_suggestions[0]["params"]["source_name"] == "src-b"

    def test_suggests_enrich_for_shallow(self, wiki_vault):
        result = wiki_gap(str(wiki_vault), stage="suggest")
        enrich_suggestions = [
            s for s in result["suggestions"] if s["action"] == "enrich_or_rewrite"
        ]
        assert len(enrich_suggestions) > 0

    def test_suggests_create_for_dangling(self, wiki_vault):
        result = wiki_gap(str(wiki_vault), stage="suggest")
        create_suggestions = [
            s for s in result["suggestions"]
            if s["action"] == "wiki_write_note_or_research"
        ]
        assert len(create_suggestions) > 0

    def test_taxonomy_suggestions(self, wiki_vault):
        taxonomy = ["concept-one", "new-topic"]
        result = wiki_gap(str(wiki_vault), stage="suggest", taxonomy=taxonomy)
        concept_suggestions = [
            s for s in result["suggestions"] if s["action"] == "create_concept"
        ]
        assert any(s["missing_concept"] == "new-topic" for s in concept_suggestions)


class TestEdgeCases:
    def test_no_wiki_dir(self, tmp_path):
        result = wiki_gap(str(tmp_path), stage="analyze")
        assert result["ok"] is False
        assert result["code"] == "no_wiki"

    def test_invalid_stage(self, wiki_vault):
        result = wiki_gap(str(wiki_vault), stage="invalid")
        assert result["ok"] is False
        assert result["code"] == "invalid_stage"

    def test_empty_wiki(self, tmp_path):
        (tmp_path / "wiki").mkdir()
        result = wiki_gap(str(tmp_path), stage="analyze")
        assert result["ok"] is True
        assert result["summary"]["total_pages"] == 0

    def test_nonexistent_project(self, wiki_vault):
        result = wiki_gap(str(wiki_vault), stage="analyze", project="nonexistent")
        assert result["ok"] is True
        # Should still work, just with fewer results
        assert result["summary"]["total_pages"] >= 0
