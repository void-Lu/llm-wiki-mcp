from __future__ import annotations

from pathlib import Path

import pytest

from netsuite_llm_wiki_mcp.wiki_io import WikiWriteError, read_markdown_page, write_wiki_page
from netsuite_llm_wiki_mcp.wiki_models import WikiPage
from netsuite_llm_wiki_mcp.wiki_paths import create_wiki_root


def test_write_and_read_markdown_page_with_frontmatter(tmp_path: Path):
    root = tmp_path / "vault"
    create_wiki_root(root)
    page = WikiPage(
        relative_path=Path("wiki/projects/alpha/architecture/script-a.md"),
        frontmatter={"type": "architecture", "generated": True, "sources": ["raw/sources/projects/alpha/codegraph/status.json"]},
        title="Script A",
        body="Call 13800138000 before release.",
    )

    result = write_wiki_page(root, page)

    assert result["ok"] is True
    target = root / "wiki/projects/alpha/architecture/script-a.md"
    assert target.is_file()
    text = target.read_text(encoding="utf-8")
    assert "[REDACTED_PHONE]" in text
    assert "13800138000" not in text
    parsed = read_markdown_page(target, root)
    assert parsed.relative_path == Path("wiki/projects/alpha/architecture/script-a.md")
    assert parsed.frontmatter["type"] == "architecture"
    assert parsed.title == "Script A"
    assert "[REDACTED_PHONE]" in parsed.body


def test_write_wiki_page_redacts_title_and_frontmatter(tmp_path: Path):
    root = tmp_path / "vault"
    create_wiki_root(root)

    write_wiki_page(
        root,
        WikiPage(
            relative_path=Path("wiki/concepts/security/secret.md"),
            frontmatter={"generated": True, "summary": "Contact a@example.com", "sources": ["token=abc1234567890"]},
            title="Call 13800138000",
            body="safe body",
        ),
    )

    text = (root / "wiki/concepts/security/secret.md").read_text(encoding="utf-8")
    assert "13800138000" not in text
    assert "a@example.com" not in text
    assert "token=abc1234567890" not in text
    assert "[REDACTED_PHONE]" in text
    assert "[REDACTED_EMAIL]" in text
    assert "[REDACTED_SECRET]" in text


def test_write_wiki_page_refuses_to_overwrite_manual_page(tmp_path: Path):
    root = tmp_path / "vault"
    create_wiki_root(root)
    target = root / "wiki/projects/alpha/specs/manual.md"
    target.parent.mkdir(parents=True)
    target.write_text("---\ngenerated: false\n---\n\n# Manual\n\nKeep me", encoding="utf-8")

    with pytest.raises(WikiWriteError) as exc_info:
        write_wiki_page(
            root,
            WikiPage(
                relative_path=Path("wiki/projects/alpha/specs/manual.md"),
                frontmatter={"generated": True, "type": "spec", "sources": []},
                title="Replacement",
                body="new",
            ),
        )

    assert exc_info.value.code == "manual_page_exists"
    assert "Keep me" in target.read_text(encoding="utf-8")


@pytest.mark.parametrize(
    "relative_path",
    [
        Path("wiki/code/old.md"),
        Path("wiki/decisions/old.md"),
        Path("wiki/synthesis/old.md"),
        Path("wiki/knowledge/old.md"),
        Path("wiki/comparisons/old.md"),
        Path("wiki/maintenance/old.md"),
        Path("wiki/projects/alpha/sources/source.md"),
        Path("wiki/projects/alpha/objects/object.md"),
        Path("projects/alpha/wiki/objects/object.md"),
    ],
)
def test_write_wiki_page_rejects_old_or_objects_paths(tmp_path: Path, relative_path: Path):
    root = tmp_path / "vault"
    create_wiki_root(root)

    with pytest.raises(WikiWriteError) as exc_info:
        write_wiki_page(
            root,
            WikiPage(
                relative_path=relative_path,
                frontmatter={"generated": True, "type": "generated"},
                title="Bad",
                body="bad",
            ),
        )

    assert exc_info.value.code == "invalid_wiki_path"


def test_write_wiki_page_rejects_path_escape(tmp_path: Path):
    root = tmp_path / "vault"
    create_wiki_root(root)

    with pytest.raises(WikiWriteError) as exc_info:
        write_wiki_page(
            root,
            WikiPage(
                relative_path=Path("../escape.md"),
                frontmatter={"generated": True},
                title="Escape",
                body="bad",
            ),
        )

    assert exc_info.value.code == "path_escape"


@pytest.mark.parametrize(
    "relative_path",
    [
        Path("wiki/projects/CON/specs/page.md"),
        Path("wiki/projects/alpha/specs/bad:name.md"),
        Path("wiki/projects/alpha/specs/COM1.md"),
        Path("wiki/projects/alpha/specs/trailing .md"),
    ],
)
def test_write_wiki_page_rejects_windows_invalid_path_components(tmp_path: Path, relative_path: Path):
    root = tmp_path / "vault"
    create_wiki_root(root)

    with pytest.raises(WikiWriteError) as exc_info:
        write_wiki_page(
            root,
            WikiPage(
                relative_path=relative_path,
                frontmatter={"generated": True},
                title="Bad",
                body="bad",
            ),
        )

    assert exc_info.value.code == "invalid_path_component"


def test_read_markdown_page_treats_malformed_frontmatter_as_empty(tmp_path: Path):
    root = tmp_path / "vault"
    create_wiki_root(root)
    target = root / "wiki/concepts/general/bad.md"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("---\ntitle: [broken\n---\n\n# Bad\n\nbody", encoding="utf-8")

    parsed = read_markdown_page(target, root)

    assert parsed.relative_path == Path("wiki/concepts/general/bad.md")
    assert parsed.frontmatter == {}
    assert parsed.title == "Bad"
