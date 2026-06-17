from __future__ import annotations

import hashlib
import json
from pathlib import Path

from netsuite_llm_wiki_mcp.wiki_io import write_wiki_page
from netsuite_llm_wiki_mcp.wiki_lint import wiki_lint
from netsuite_llm_wiki_mcp.wiki_models import WikiPage
from netsuite_llm_wiki_mcp.wiki_paths import create_wiki_root


def _issue_codes(result: dict[str, object]) -> set[str]:
    return {str(issue["code"]) for issue in result["issues"]}  # type: ignore[index]


def test_wiki_lint_returns_ok_for_initialized_empty_wiki(tmp_path: Path):
    root = tmp_path / "vault"
    create_wiki_root(root)

    result = wiki_lint(root)

    assert result["ok"] is True
    assert result["issues"] == []


def test_wiki_lint_reports_missing_required_structure(tmp_path: Path):
    root = tmp_path / "vault"
    root.mkdir()

    result = wiki_lint(root)

    codes = _issue_codes(result)
    assert result["ok"] is False
    assert "missing_required_file" in codes
    assert "missing_required_directory" in codes


def test_wiki_lint_reports_missing_frontmatter_and_generated_sources(tmp_path: Path):
    root = tmp_path / "vault"
    create_wiki_root(root)
    (root / "wiki/concepts/no-frontmatter.md").write_text("# No Frontmatter\n", encoding="utf-8")
    write_wiki_page(
        root,
        WikiPage(
            relative_path=Path("wiki/projects/alpha/architecture/script.md"),
            frontmatter={"generated": True, "type": "architecture"},
            title="Script",
            body="body",
        ),
    )

    result = wiki_lint(root)

    codes = _issue_codes(result)
    assert "missing_frontmatter" in codes
    assert "generated_missing_sources" in codes


def test_wiki_lint_ignores_archived_markdown_content(tmp_path: Path):
    root = tmp_path / "vault"
    create_wiki_root(root)
    archived = root / "wiki/archives/2026/06/16/raw/sources/codegraph/legacy.md"
    archived.parent.mkdir(parents=True)
    archived.write_text("# Legacy raw markdown without frontmatter\n\n[[missing-old-page]]", encoding="utf-8")

    result = wiki_lint(root)

    assert result["ok"] is True
    assert result["issues"] == []


def test_wiki_lint_reports_broken_wikilink_and_old_directories(tmp_path: Path):
    root = tmp_path / "vault"
    create_wiki_root(root)
    write_wiki_page(
        root,
        WikiPage(
            relative_path=Path("wiki/concepts/source.md"),
            frontmatter={"generated": False, "type": "concept"},
            title="Source",
            body="See [[missing-page.md]].",
        ),
        overwrite_generated_only=False,
    )
    (root / "wiki/code").mkdir()
    (root / "raw/projects").mkdir(parents=True)
    (root / "wiki/comparisons").mkdir()
    (root / "wiki/maintenance").mkdir()
    (root / "wiki/projects/alpha/objects").mkdir(parents=True)
    (root / "wiki/projects/alpha/sources").mkdir(parents=True)

    result = wiki_lint(root)

    codes = _issue_codes(result)
    assert "broken_wikilink" in codes
    assert "old_structure_present" in codes


def test_wiki_lint_reports_index_entry_missing_target(tmp_path: Path):
    root = tmp_path / "vault"
    create_wiki_root(root)
    (root / "wiki/index.md").write_text("---\ngenerated: true\n---\n\n# Index\n\n- [[concepts/missing.md|Missing]]\n", encoding="utf-8")

    result = wiki_lint(root)

    assert "index_target_missing" in _issue_codes(result)


def test_wiki_lint_reports_missing_generated_raw_source(tmp_path: Path):
    root = tmp_path / "vault"
    create_wiki_root(root)
    write_wiki_page(
        root,
        WikiPage(
            Path("wiki/projects/alpha/architecture/script.md"),
            {"title": "Script", "type": "architecture", "generated": True, "sources": ["raw/sources/file/alpha/docs/missing.md"]},
            "Script",
            "Body",
        ),
        overwrite_generated_only=False,
    )

    result = wiki_lint(root)

    assert result["ok"] is False
    assert any(issue["code"] == "source_missing" and issue["path"] == "wiki/projects/alpha/architecture/script.md" for issue in result["issues"])


def test_wiki_lint_reports_cache_manifest_missing_path(tmp_path: Path):
    root = tmp_path / "vault"
    create_wiki_root(root)
    cache = root / ".llm-wiki/ingest-cache/alpha/docs.json"
    cache.parent.mkdir(parents=True, exist_ok=True)
    cache.write_text(json.dumps({"manifest": [{"path": "raw/sources/file/alpha/docs/missing.md", "stored_sha256": "abc"}]}), encoding="utf-8")

    result = wiki_lint(root)

    assert result["ok"] is False
    assert any(issue["code"] == "cache_manifest_path_missing" and issue["path"] == ".llm-wiki/ingest-cache/alpha/docs.json" for issue in result["issues"])


def test_wiki_lint_reports_cache_manifest_hash_mismatch(tmp_path: Path):
    root = tmp_path / "vault"
    create_wiki_root(root)
    raw = root / "raw/sources/file/alpha/docs/notes.md"
    raw.parent.mkdir(parents=True, exist_ok=True)
    raw.write_text("current", encoding="utf-8")
    cache = root / ".llm-wiki/ingest-cache/alpha/docs.json"
    cache.parent.mkdir(parents=True, exist_ok=True)
    cache.write_text(json.dumps({"manifest": [{"path": "raw/sources/file/alpha/docs/notes.md", "stored_sha256": hashlib.sha256(b"old").hexdigest()}]}), encoding="utf-8")

    result = wiki_lint(root)

    assert result["ok"] is False
    assert any(issue["code"] == "cache_manifest_hash_mismatch" and issue["severity"] == "warning" for issue in result["issues"])


def test_wiki_lint_reports_orphan_pages(tmp_path: Path):
    root = tmp_path / "vault"
    create_wiki_root(root)
    write_wiki_page(
        root,
        WikiPage(
            Path("wiki/concepts/orphan.md"),
            {"title": "Orphan", "type": "concept", "generated": True, "sources": []},
            "Orphan",
            "Nobody links here and index does not reference it.",
        ),
        overwrite_generated_only=False,
    )

    result = wiki_lint(root)

    assert any(issue["code"] == "orphan_page" and issue["path"] == "wiki/concepts/orphan.md" for issue in result["issues"])


def test_wiki_lint_prepare_semantic_review_returns_llm_prompt(tmp_path: Path):
    root = tmp_path / "vault"
    create_wiki_root(root)
    write_wiki_page(
        root,
        WikiPage(
            Path("wiki/concepts/invoice.md"),
            {"title": "Invoice", "type": "concept", "generated": True, "sources": ["raw/sources/a.md"]},
            "Invoice",
            "Invoices are always synced synchronously.",
        ),
        overwrite_generated_only=False,
    )

    result = wiki_lint(root, stage="prepare_semantic_review", project="alpha")

    assert result["ok"] is True
    assert result["stage"] == "prepare_semantic_review"
    assert "contradictions" in result["prompt"]
    assert "stale claims" in result["prompt"]
    assert "Invoices are always synced synchronously" in result["prompt"]
    assert result["next_call"]["stage"] == "apply_semantic_review"


def test_wiki_lint_apply_semantic_review_writes_report(tmp_path: Path):
    root = tmp_path / "vault"
    create_wiki_root(root)

    result = wiki_lint(
        root,
        stage="apply_semantic_review",
        semantic_review="<think>hidden</think>\n\n## Findings\n\n- Missing concept: retry policy.",
        project="alpha",
    )

    assert result["ok"] is True
    assert result["stage"] == "apply_semantic_review"
    assert result["path"].startswith("wiki/queries/")
    text = (root / result["path"]).read_text(encoding="utf-8")
    assert "semantic-lint" in text
    assert "<think>" not in text
    assert "retry policy" in text
    assert "semantic_lint" in (root / "wiki/log.md").read_text(encoding="utf-8")

def test_wiki_lint_reports_unescaped_wikilink_alias_pipe_in_table(tmp_path: Path):
    root = tmp_path / "vault"
    create_wiki_root(root)
    write_wiki_page(
        root,
        WikiPage(
            Path("wiki/concepts/target.md"),
            {"title": "Target", "type": "concept", "generated": False},
            "Target",
            "Referenced by a table.",
        ),
        overwrite_generated_only=False,
    )
    write_wiki_page(
        root,
        WikiPage(
            Path("wiki/concepts/table.md"),
            {"title": "Table", "type": "concept", "generated": False},
            "Table",
            "| Example | Description |\n|---|---|\n| [[target|Target Page]] | text |",
        ),
        overwrite_generated_only=False,
    )

    result = wiki_lint(root)

    assert "table_wikilink_alias_pipe" in _issue_codes(result)


def test_wiki_lint_accepts_escaped_wikilink_alias_pipe_in_table(tmp_path: Path):
    root = tmp_path / "vault"
    create_wiki_root(root)
    write_wiki_page(
        root,
        WikiPage(
            Path("wiki/concepts/target.md"),
            {"title": "Target", "type": "concept", "generated": False},
            "Target",
            "Referenced by a table.",
        ),
        overwrite_generated_only=False,
    )
    write_wiki_page(
        root,
        WikiPage(
            Path("wiki/concepts/table.md"),
            {"title": "Table", "type": "concept", "generated": False},
            "Table",
            "| Example | Description |\n|---|---|\n| [[target\\|Target Page]] | text |",
        ),
        overwrite_generated_only=False,
    )

    result = wiki_lint(root)
    codes = _issue_codes(result)

    assert "table_wikilink_alias_pipe" not in codes
    assert "broken_wikilink" not in codes
