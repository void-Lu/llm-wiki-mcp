from __future__ import annotations

from pathlib import Path

from netsuite_rag_mcp.wiki_io import write_wiki_page
from netsuite_rag_mcp.wiki_lint import wiki_lint
from netsuite_rag_mcp.wiki_models import WikiPage
from netsuite_rag_mcp.wiki_paths import create_wiki_root


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
            relative_path=Path("wiki/projects/alpha/code/script.md"),
            frontmatter={"generated": True, "type": "code_fact"},
            title="Script",
            body="body",
        ),
    )

    result = wiki_lint(root)

    codes = _issue_codes(result)
    assert "missing_frontmatter" in codes
    assert "generated_missing_sources" in codes


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
    (root / "wiki/projects/alpha/objects").mkdir(parents=True)

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
