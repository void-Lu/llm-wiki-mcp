from __future__ import annotations

from pathlib import Path

import pytest

from netsuite_rag_mcp.page_merge import (
    apply_page_merge,
    merge_frontmatter,
    prepare_body_merge,
    validate_merged_body,
)


def test_merge_frontmatter_unions_sources():
    existing = {"type": "code", "title": "Foo", "created": "2024-01-01", "sources": ["a.ts"], "tags": ["netsuite"]}
    incoming = {"type": "entity", "title": "Bar", "created": "2025-01-01", "sources": ["b.ts"], "tags": ["suiteql"]}
    merged = merge_frontmatter(existing, incoming)
    assert merged["type"] == "code"
    assert merged["title"] == "Foo"
    assert merged["created"] == "2024-01-01"
    assert set(merged["sources"]) == {"a.ts", "b.ts"}
    assert set(merged["tags"]) == {"netsuite", "suiteql"}
    assert merged["generated"] is True
    assert "updated" in merged


def test_merge_frontmatter_dedup():
    existing = {"sources": ["a.ts", "b.ts"], "tags": ["x"]}
    incoming = {"sources": ["B.ts", "c.ts"], "tags": ["X", "y"]}
    merged = merge_frontmatter(existing, incoming)
    assert len(merged["sources"]) == 3
    assert len(merged["tags"]) == 2


def test_prepare_body_merge_identical():
    result = prepare_body_merge("hello world", "  hello   world  ")
    assert result["needs_merge"] is False


def test_prepare_body_merge_different():
    result = prepare_body_merge("version A content", "version B content")
    assert result["needs_merge"] is True
    assert "prompt" in result
    assert "version A content" in result["prompt"]


def test_validate_merged_body_ok():
    result = validate_merged_body("a" * 100, "b" * 80, "c" * 90)
    assert result["valid"] is True


def test_validate_merged_body_too_short():
    result = validate_merged_body("a" * 100, "b" * 80, "c" * 10)
    assert result["valid"] is False
    assert "shrink_ratio" in result


@pytest.fixture
def merge_root(tmp_path: Path) -> Path:
    root = tmp_path / "vault"
    page_dir = root / "wiki" / "projects" / "proj" / "code"
    page_dir.mkdir(parents=True)
    page = page_dir / "my-page.md"
    page.write_text(
        "---\ntype: code\ntitle: My Page\ncreated: '2024-01-01'\ngenerated: true\nsources:\n- old-source.ts\ntags:\n- netsuite\n---\n\n# My Page\n\nExisting body content.\n",
        encoding="utf-8",
    )
    return root


def test_apply_page_merge_no_body_merge(merge_root: Path):
    result = apply_page_merge(
        str(merge_root),
        "wiki/projects/proj/code/my-page.md",
        incoming_frontmatter={"sources": ["new-source.ts"], "tags": ["suiteql"]},
        incoming_body="New body content.",
    )
    assert result["ok"] is True
    assert result["body_merged"] is False

    content = (merge_root / "wiki" / "projects" / "proj" / "code" / "my-page.md").read_text(encoding="utf-8")
    assert "My Page" in content
    assert "New body content" in content
    assert "old-source.ts" in content
    assert "new-source.ts" in content


def test_apply_page_merge_refuses_manual_page(merge_root: Path):
    page = merge_root / "wiki" / "projects" / "proj" / "code" / "my-page.md"
    page.write_text("---\ntype: code\ntitle: My Page\n---\n\n# My Page\n\nManual.\n", encoding="utf-8")
    result = apply_page_merge(str(merge_root), "wiki/projects/proj/code/my-page.md", {}, "new")
    assert result["ok"] is False
    assert result["code"] == "manual_page"
